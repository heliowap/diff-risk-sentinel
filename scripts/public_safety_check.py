#!/usr/bin/env python3
"""
Blocks personal data (PII), secrets and proprietary material from reaching this public repository.

Checks every text that would be published — file contents and commit messages — for:
  * PII: CPF/CNPJ with valid check digits, e-mail addresses outside the allowlist, Brazilian
    phone numbers;
  * secrets: API keys and tokens, private keys;
  * local absolute paths (/Users/..., /home/...), which leak user and project names;
  * private terms: words whose SHA-256 is in .public-safety/denylist.sha256 (hashed, so the
    public list does not reveal them; add with --add-term);
  * eval data files: data under evals/ must be listed in .public-safety/allowed-data.txt, and
    evals/private/ is never published;
  * private repositories (local only): commit SHAs and distinctive identifiers (compound
    function and class names with 3+ parts or 16+ characters, defined in exactly one private
    repository and not in this one) of the repositories listed in
    ~/.config/public-safety/private-repos.txt or $PUBLIC_SAFETY_PRIVATE_REPOS (os.pathsep-separated).

Strings in .public-safety/allowlist.txt (one per line) are removed before scanning.

Usage:
    python3 scripts/public_safety_check.py --staged            # pre-commit
    python3 scripts/public_safety_check.py --message FILE      # commit-msg
    python3 scripts/public_safety_check.py --range A..B        # pre-push (..B: new branch)
    python3 scripts/public_safety_check.py --all               # CI / full audit
    python3 scripts/public_safety_check.py --add-term WORD     # add a private term (stored hashed)
    add --jev to also ask TypeSafe Jev (scripts/sensitive_judge.py) about the text being published
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sensitive_judge  # noqa: E402

POLICY_DIR = ".public-safety"
PRIVATE_REPOS_FILE = os.path.expanduser("~/.config/public-safety/private-repos.txt")
DATA_EXTENSIONS = (".json", ".jsonl", ".csv", ".tsv", ".parquet", ".pkl", ".db", ".sqlite", ".xml", ".yaml", ".yml")
ALLOWED_EMAIL_RE = re.compile(
    r"(@example\.(com|org|net)$|^noreply@anthropic\.com$|@users\.noreply\.github\.com$"
    r"|@\d+x\.(png|jpe?g|gif|svg|webp|avif)$"                                    # retina image names (icon@2x.png)
    r"|^(you|your|user|username|name|email|me|someone)@(your)?(email|domain|company|example)\.[a-z]+$)", re.I)
PLACEHOLDER_USERS = {"yourname", "username", "user", "you", "me", "name", "<user>", "$user", "${user}", "your-user"}

_EMAIL_RE = re.compile(r"(?<![\w.+\\-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_CPF_RE = re.compile(r"(?<![\d.-])(\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})(?![\d-])")
_CNPJ_RE = re.compile(r"(?<![\d./-])(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})(?![\d/-])")
_PHONE_RE = re.compile(r"(\+55\s?\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}|\(\d{2}\)\s?9\d{4}-?\d{4})")
_SECRET_RES = [
    re.compile(r"\bapikey_[A-Za-z0-9]{12,}"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]
_LOCAL_PATH_RE = re.compile(r"(?<![\w.])(/Users/[A-Za-z0-9<$][^/\s'\"`]*|/home/[A-Za-z0-9<$][^/\s'\"`]*|"
                            r"[A-Za-z]:\\Users\\[A-Za-z0-9<$][^\\\s'\"`]*)")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*")
_PART_RE = re.compile(r"[A-Za-z0-9]+")
_HEX_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{7,40}(?![0-9a-fA-F])")
_COMPOUND_RE = re.compile(r"[a-z0-9]_[A-Za-z]|[a-z][A-Z]")
_DEFINITION_RE = re.compile(r"(?:\bdef|\bclass|\bfunction|\binterface|\btype|\bconst|\blet)\s+([A-Za-z_$][\w$]{9,})")


@dataclass
class Finding:
    kind: str
    path: str
    line: int
    detail: str

    def __str__(self):
        return f"{self.path}:{self.line}: [{self.kind}] {self.detail}"


@dataclass
class Policy:
    denylist: Set[str] = field(default_factory=set)
    allowlist: List[str] = field(default_factory=list)
    allowed_data: List[str] = field(default_factory=list)

    @classmethod
    def load(cls, root: str) -> "Policy":
        def lines(name):
            path = os.path.join(root, POLICY_DIR, name)
            if not os.path.exists(path):
                return []
            with open(path, encoding="utf-8") as fh:
                return [x.strip() for x in fh if x.strip() and not x.startswith("#")]
        return cls(set(lines("denylist.sha256")), lines("allowlist.txt"), lines("allowed-data.txt"))


def _digest(term: str) -> str:
    return hashlib.sha256(term.lower().encode("utf-8")).hexdigest()


def _cpf_ok(digits: str) -> bool:
    if len(set(digits)) == 1:
        return False
    for n in (9, 10):
        total = sum(int(d) * w for d, w in zip(digits[:n], range(n + 1, 1, -1)))
        if (total * 10 % 11) % 10 != int(digits[n]):
            return False
    return True


def _cnpj_ok(digits: str) -> bool:
    if len(set(digits)) == 1:
        return False
    for n, weights in ((12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]), (13, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])):
        r = sum(int(d) * w for d, w in zip(digits[:n], weights)) % 11
        if (0 if r < 2 else 11 - r) != int(digits[n]):
            return False
    return True


class PrivateIndex:
    """Commit SHAs and distinctive identifiers of local private repositories."""

    def __init__(self, repos: Iterable[str], own_repo: Optional[str] = None):
        self.repos = [r for r in repos if os.path.isdir(r)]
        own = _definitions(own_repo) if own_repo else set()
        seen = Counter()
        for r in self.repos:
            seen.update(_definitions(r))
        # distinctive: compound (snake_case or camelCase), defined in exactly one private repository
        # (names several repositories define — parse_arguments — are generic), not defined here
        self.identifiers: Set[str] = {i for i, n in seen.items() if n == 1 and i not in own and _distinctive(i)}

    @classmethod
    def from_config(cls, own_repo: str) -> Optional["PrivateIndex"]:
        paths = [p for p in os.environ.get("PUBLIC_SAFETY_PRIVATE_REPOS", "").split(os.pathsep) if p]
        if os.path.exists(PRIVATE_REPOS_FILE):
            with open(PRIVATE_REPOS_FILE, encoding="utf-8") as fh:
                paths += [os.path.expanduser(x.strip()) for x in fh if x.strip() and not x.startswith("#")]
        return cls(paths, own_repo) if paths else None

    def is_commit(self, hexstr: str) -> bool:
        for r in self.repos:
            res = subprocess.run(["git", "-C", r, "cat-file", "-e", f"{hexstr}^{{commit}}"], capture_output=True)
            if res.returncode == 0:
                return True
        return False


def _distinctive(name: str) -> bool:
    """Compound names of 3+ parts (InvoiceReconciliationService, invoice_total_errors) or 16+ characters;
    two-part names (queryClient, create_index) and dunders are shared by too many codebases."""
    if name.startswith("__") or not _COMPOUND_RE.search(name):
        return False
    parts = [p for p in re.split(r"_+|(?<=[a-z0-9])(?=[A-Z])", name) if p]
    return len(parts) >= 3 or len(name) >= 16


def _definitions(repo: str) -> Set[str]:
    res = subprocess.run(["git", "-C", repo, "grep", "-h", "-I", "-o", "-E",
                          r"(def|class|function|interface|type|const|let)[[:space:]]+[A-Za-z_$][A-Za-z0-9_$]{9,}",
                          "HEAD", "--", ":!**/node_modules/**"], capture_output=True, text=True)
    return {m.group(1) for m in _DEFINITION_RE.finditer(res.stdout)}


def _without_allowed(text: str, policy: Policy) -> str:
    for allowed in policy.allowlist:
        text = text.replace(allowed, " " * len(allowed))
    return text


def scan_text(path: str, text: str, policy: Policy, private: Optional[PrivateIndex] = None) -> List[Finding]:
    text = _without_allowed(text, policy)
    found: List[Finding] = []
    for n, line in enumerate(text.split("\n"), 1):
        def add(kind, detail):
            found.append(Finding(kind, path, n, detail))
        for m in _EMAIL_RE.finditer(line):
            if not ALLOWED_EMAIL_RE.search(m.group(0)):
                add("pii:email", m.group(0))
        for m in _CPF_RE.finditer(line):
            if _cpf_ok(re.sub(r"\D", "", m.group(1))):
                add("pii:cpf", m.group(1))
        for m in _CNPJ_RE.finditer(line):
            if _cnpj_ok(re.sub(r"\D", "", m.group(1))):
                add("pii:cnpj", m.group(1))
        for m in _PHONE_RE.finditer(line):
            add("pii:phone", m.group(1))
        for rx in _SECRET_RES:
            for m in rx.finditer(line):
                add("secret", m.group(0)[:12] + "…")
        for m in _LOCAL_PATH_RE.finditer(line):
            if m.group(1).rstrip("/.,;:)").split("/")[-1].split("\\")[-1].lower() not in PLACEHOLDER_USERS:
                add("local-path", m.group(1))
        if policy.denylist:
            words = set()
            for w in _WORD_RE.findall(line):
                words.add(w.lower())
                words.update(p.lower() for p in _PART_RE.findall(w))
            for w in words:
                if _digest(w) in policy.denylist:
                    add("private-term", w)
        if private:
            for m in _HEX_RE.finditer(line):
                if private.is_commit(m.group(0)):
                    add("private-commit", m.group(0))
            for w in set(re.findall(r"[A-Za-z_$][\w$]{9,}", line)) & private.identifiers:
                add("private-identifier", w)
    return found


def scan_path(path: str, policy: Policy) -> List[Finding]:
    if path.startswith("evals/private/"):
        return [Finding("eval-data", path, 0, "evals/private/ is never published")]
    if path.startswith("evals/") and path.lower().endswith(DATA_EXTENSIONS) and path not in policy.allowed_data:
        return [Finding("eval-data", path, 0, f"data file under evals/ not in {POLICY_DIR}/allowed-data.txt")]
    return []


def _git(root: str, *args: str) -> str:
    # published text may be in any encoding (latin-1 files, binary diffs); never crash the gate on it
    return subprocess.run(["git", "-C", root, *args], capture_output=True, check=True).stdout.decode("utf-8", "replace")


def _blob(root: str, spec: str) -> Optional[str]:
    res = subprocess.run(["git", "-C", root, "show", spec], capture_output=True)
    if res.returncode != 0 or b"\x00" in res.stdout[:8000]:
        return None
    return res.stdout.decode("utf-8", "replace")


def _added_text(root: str, diff_args: List[str]) -> dict:
    """path -> the lines a diff adds (what would become public), hunk by hunk."""
    out, path = {}, None
    for line in _git(root, "diff", "--no-color", "--no-ext-diff", "-U0", *diff_args).split("\n"):
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and line.startswith("+") and not line.startswith("+++"):
            out.setdefault(path, []).append(line[1:])
        elif path and line.startswith("@@") and out.get(path):
            out[path].append("")
    return {p: "\n".join(lines).strip() for p, lines in out.items() if "".join(lines).strip()}


def _chunks(text: str, size: int = 3000) -> List[str]:
    parts, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if cur and len(cur) + len(para) > size:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    return parts + ([cur] if cur.strip() else [])


def _judge_all(key: str, units: List[tuple], workers: int = 16) -> List[Finding]:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        answers = list(pool.map(lambda u: sensitive_judge.judge(key, u[0], u[1]), units))
    found, failed = [], 0
    for (path, _), a in zip(units, answers):
        if "error" in a:
            failed += 1
        elif a["p_personal_data"] >= sensitive_judge.PERSONAL_DATA_BLOCK:
            found.append(Finding("jev:personal-data", path, 0, f"p={a['p_personal_data']:.2f}"))
        elif (a.get("p_private_project") or 0.0) >= sensitive_judge.PRIVATE_PROJECT_BLOCK:
            found.append(Finding("jev:private-project", path, 0, f"p={a['p_private_project']:.2f}"))
    if failed:
        print(f"public-safety: warning — {failed} chunk(s) could not be judged by Jev; regex checks still applied.",
              file=sys.stderr)
    return found


def check(root: str, mode: str, policy: Optional[Policy] = None, rev_range: Optional[str] = None,
          message_file: Optional[str] = None, private: Optional[PrivateIndex] = None,
          jev_key: Optional[str] = None) -> List[Finding]:
    policy = policy if policy is not None else Policy.load(root)
    found: List[Finding] = []
    units: List[tuple] = []  # (path, text) sent to the Jev judge when jev_key is set
    if mode == "message":
        with open(message_file, encoding="utf-8") as fh:
            text = "\n".join(x for x in fh.read().split("\n") if not x.startswith("#"))
        found = scan_text("commit message", text, policy, private)
        text = _without_allowed(text, policy)
        return found + (_judge_all(jev_key, [("commit message", text)]) if jev_key and text.strip() else [])
    if mode == "staged":
        paths = [p for p in _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR").split("\n") if p]
        specs = [(p, f":{p}") for p in paths]
    elif mode == "range":
        tip = rev_range.split("..")[-1] or "HEAD"
        if rev_range.startswith(".."):
            # new branch: nothing on the remote yet — the whole tree and every commit not on a remote
            paths = [p for p in _git(root, "ls-tree", "-r", "--name-only", tip).split("\n") if p]
            commits = _git(root, "rev-list", tip, "--not", "--remotes")
        else:
            paths = [p for p in _git(root, "diff", "--name-only", "--diff-filter=ACMR", rev_range).split("\n") if p]
            commits = _git(root, "rev-list", rev_range)
        specs = [(p, f"{tip}:{p}") for p in paths]
        for sha in [s for s in commits.split("\n") if s]:
            message = _git(root, "log", "-1", "--format=%B", sha)
            found += scan_text("commit message", message, policy, private)
            units.append(("commit message", message))
    else:
        specs = [(p, f"HEAD:{p}") for p in _git(root, "ls-files").split("\n") if p]
    for path, spec in specs:
        if path.startswith(POLICY_DIR + "/"):
            continue
        found += scan_path(path, policy)
        text = _blob(root, spec)
        if text is not None:
            found += scan_text(path, text, policy, private)
            if jev_key and mode == "all":
                units += [(path, c) for c in _chunks(text)]
    if jev_key:
        if mode in ("staged", "range"):
            diff_args = ["--cached"] if mode == "staged" else (
                [rev_range] if not rev_range.startswith("..") else ["4b825dc642cb6eb9a060e54bf8d69288fbee4904", tip])
            for path, text in _added_text(root, diff_args).items():
                if not path.startswith(POLICY_DIR + "/"):
                    units += [(path, c) for c in _chunks(text)]
        units = [(path, _without_allowed(text, policy)) for path, text in units]
        found += _judge_all(jev_key, [u for u in units if u[1].strip()])
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true")
    g.add_argument("--all", action="store_true")
    g.add_argument("--range")
    g.add_argument("--message")
    g.add_argument("--add-term", help="Add a private term to the hashed denylist")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--jev", action="store_true",
                    help="Also ask TypeSafe Jev about personal data and private-project material (needs "
                         "TYPESAFE_API_KEY; sends the added text to api.typesafe.ai)")
    args = ap.parse_args()
    root = _git(args.repo, "rev-parse", "--show-toplevel").strip()

    if args.add_term:
        path = os.path.join(root, POLICY_DIR, "denylist.sha256")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_digest(args.add_term.strip()) + "\n")
        print(f"added one hashed term to {POLICY_DIR}/denylist.sha256")
        return 0

    private = PrivateIndex.from_config(root)
    mode = "staged" if args.staged else "message" if args.message else "range" if args.range else "all"
    jev_key = os.environ.get("TYPESAFE_API_KEY") if args.jev else None
    if args.jev and not jev_key:
        print("public-safety: --jev without TYPESAFE_API_KEY; running the regex checks only.", file=sys.stderr)
    found = check(root, mode, rev_range=args.range, message_file=args.message, private=private, jev_key=jev_key)
    if not found:
        return 0
    print("public-safety: blocked — this would publish sensitive or proprietary material:", file=sys.stderr)
    for f in found:
        print(f"  {f}", file=sys.stderr)
    print("Remove it (aggregates only for evals on private repositories; keep raw data in evals/private/),"
          f" or, if it is genuinely public, add the exact string to {POLICY_DIR}/allowlist.txt.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

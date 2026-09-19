"""
Consumers outside the diff: code the ranking cannot see because the diff did not touch it,
but which references identifiers or literal fragments the diff changed (re-signatured
functions, constants, key formats, removed hard-coded values). On real history this found a
merged defect in an untouched function that no ranking of the touched functions could reach.
"""

import re
import subprocess
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .complexity import extract_functions, supported_extensions
from .diff import read_blobs

TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|specs?|e2e|fixtures?)/"
    r"|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$"
)

# Applied migrations are history, not consumers anyone should re-review.
MIGRATION_PATH_RE = re.compile(r"(^|/)(migrations|alembic)/(versions/)?[^/]*$|(^|/)versions/\d+_[^/]*\.py$")

_COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*)")

_SIGNATURE_RES = [
    re.compile(r"\bdef\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    re.compile(r"\bclass\s+([A-Za-z_]\w*)"),
]
_CONSTANT_RE = re.compile(r"^\s*(?:export\s+)?(?:const\s+)?([A-Z][A-Z0-9_]{3,})\s*(?::[^=]+)?=")
_LITERAL_RE = re.compile(r"""[fFrRbBuU]{0,2}(["'`])((?:\\.|(?!\1).){2,}?)\1""")
_PLACEHOLDER_RE = re.compile(r"\$\{[^}]*\}|\{[^}]*\}|%[sdrf]|\\\w")
_FRAGMENT_RE = re.compile(r"^[\w.:/-]+$")
_STOPWORDS = {
    "true", "false", "null", "none", "self", "this", "name", "type", "data", "value", "values",
    "error", "string", "number", "utf-8", "utf8", "json", "text", "http", "https", "default",
    "items", "item", "list", "dict", "object", "result", "status", "message", "content", "code",
    "date", "time", "user", "users", "info", "debug", "warning", "return", "async", "await",
}


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def _changed(snippet: str) -> Iterable[Tuple[str, str]]:
    for line in snippet.splitlines():
        kind, body = line[:1], line[1:]
        if kind not in "+-" or not kind or not body.strip() or _COMMENT_RE.match(body):
            continue
        yield kind, body


def _literal_fragments(literal: str) -> Set[str]:
    out = set()
    for frag in _PLACEHOLDER_RE.split(literal):
        frag = frag.strip()
        if len(frag) >= 4 and _FRAGMENT_RE.match(frag) and re.search(r"[A-Za-z]", frag) \
                and frag.lower() not in _STOPWORDS:
            out.add(frag)
    return out


def changed_tokens(snippet: str) -> Set[str]:
    """
    Identifiers and literal fragments whose contract the diff changed: names of functions or
    classes declared on changed lines, constants assigned on changed lines, and fragments of
    string literals that appear on only one side of the change (new key formats, removed
    hard-coded values).
    """
    tokens: Set[str] = set()
    literals = {"+": set(), "-": set()}
    for kind, body in _changed(snippet):
        for rx in _SIGNATURE_RES:
            for m in rx.finditer(body):
                if len(m.group(1)) >= 4:
                    tokens.add(m.group(1))
        m = _CONSTANT_RE.match(body)
        if m:
            tokens.add(m.group(1))
        for m in _LITERAL_RE.finditer(body):
            literals[kind].add(m.group(2))
    for literal in literals["+"] ^ literals["-"]:
        tokens |= _literal_fragments(literal)
    return {t for t in tokens if t.lower() not in _STOPWORDS}


def find_consumers(
    repo: str,
    rev: str,
    tokens: Set[str],
    exclude_files: Optional[Set[str]] = None,
    exclude_functions: Optional[Set[Tuple[str, str]]] = None,
    max_files_per_token: int = 25,
    max_tokens: int = 300,
    token_files: Optional[Dict[str, Set[str]]] = None,
) -> List[Dict]:
    """
    Functions at `rev` (outside tests and the excluded files/functions) that reference any
    of `tokens`. Tokens found in more than `max_files_per_token` files are too generic and
    dropped. `token_files` restricts a token to the given files (private `_names` are
    module-local, so a same-named helper elsewhere is a different function).
    Returns [{file, function, lines, tokens, hits}] with the most tokens first.
    """
    token_files = token_files or {}
    exclude_files = exclude_files or set()
    exclude_functions = exclude_functions or set()
    tokens = sorted(tokens, key=lambda t: (-len(t), t))[:max_tokens]
    if not tokens:
        return []

    patterns = [f"*{ext}" for ext in supported_extensions()]
    cmd = ["git", "grep", "-n", "-o", "-F", "--no-color"]
    for t in tokens:
        cmd += ["-e", t]
    cmd += [rev, "--", *patterns]
    res = subprocess.run(cmd, cwd=repo, capture_output=True)
    if res.returncode not in (0, 1):
        return []

    hits: Dict[str, List[Tuple[int, str]]] = {}
    files_per_token: Dict[str, Set[str]] = {}
    prefix = f"{rev}:"
    for raw in res.stdout.decode("utf-8", "replace").split("\n"):
        if not raw.startswith(prefix):
            continue
        path, _, rest = raw[len(prefix):].partition(":")
        line_no, _, token = rest.partition(":")
        if not line_no.isdigit() or is_test_path(path) or path in exclude_files or MIGRATION_PATH_RE.search(path):
            continue
        if token in token_files and path not in token_files[token]:
            continue
        files_per_token.setdefault(token, set()).add(path)
        hits.setdefault(path, []).append((int(line_no), token))

    generic = {t for t, files in files_per_token.items() if len(files) > max_files_per_token}
    blobs = read_blobs(repo, [(rev, p) for p in hits])
    consumers: Dict[Tuple[str, str], Dict] = {}
    for path, file_hits in hits.items():
        functions = extract_functions(path, blobs.get((rev, path)) or "") or []
        for line_no, token in file_hits:
            if token in generic:
                continue
            owners = [f for f in functions if f["start_line"] <= line_no <= f["end_line"]]
            if not owners:
                continue
            fn = min(owners, key=lambda f: f["end_line"] - f["start_line"])
            if (path, fn["name"]) in exclude_functions:
                continue
            entry = consumers.setdefault((path, fn["name"]), {
                "file": path, "function": fn["name"], "lines": f"{fn['start_line']}-{fn['end_line']}",
                "tokens": set(), "hits": 0,
            })
            entry["tokens"].add(token)
            entry["hits"] += 1

    result = []
    for entry in consumers.values():
        entry["tokens"] = sorted(entry["tokens"])
        result.append(entry)
    result.sort(key=lambda e: (-len(e["tokens"]), -e["hits"], e["file"], e["function"]))
    return result

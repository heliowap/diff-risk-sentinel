import codecs
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple


# Pin output format regardless of user/global git config (diff.noprefix,
# diff.mnemonicPrefix, color.ui=always, external diff drivers...).
DIFF_FLAGS = [
    "--no-color",
    "--no-ext-diff",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--find-renames",
    "--unified=3",
]

HUNK_HEADER_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


class GitError(RuntimeError):
    pass


@dataclass
class FileDiff:
    old_path: Optional[str]
    new_path: Optional[str]
    # (kind, position, text): kind in '+', '-', ' '. For '+' and ' ' the position is the
    # line number in the new file. A '-' line directly replaced by '+' lines is anchored
    # to the first replacement line. A pure deletion sits *between* two new-file lines,
    # so it gets the position n + 0.5: only a function spanning both n and n + 1 contains
    # it, and deleting a whole function does not touch its neighbour.
    lines: List[Tuple[str, float, str]] = field(default_factory=list)

    @property
    def added_lines(self) -> Set[int]:
        return {pos for kind, pos, _ in self.lines if kind == "+"}

    @property
    def touched_lines(self) -> Set[float]:
        if self.new_path is None:
            return set()
        return {pos for kind, pos, _ in self.lines if kind in "+-" and pos > 0}

    @property
    def removed_count(self) -> int:
        return sum(1 for kind, _, _ in self.lines if kind == "-")

    def snippet_for(self, start: int, end: int) -> str:
        return "\n".join(
            f"{kind}{text}" for kind, pos, text in self.lines if start <= pos <= end
        )


def run_git(args: Sequence[str], repo: Optional[str] = None) -> str:
    cmd = ["git", "-c", "core.quotePath=false", *args]
    try:
        # Bytes, not text mode: universal newlines would turn a lone "\r" inside a line into
        # a line break and shift every following line number.
        res = subprocess.run(cmd, cwd=repo, capture_output=True)
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    if res.returncode != 0:
        raise GitError(f"`{' '.join(cmd)}` failed: {res.stderr.decode('utf-8', 'replace').strip()}")
    return res.stdout.decode("utf-8", "replace")


def _rev_parse(rev: str, repo: Optional[str]) -> str:
    try:
        return run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], repo).strip()
    except GitError:
        raise GitError(f"unknown revision '{rev}' (not a commit in this repository)") from None


def default_base(repo: Optional[str] = None) -> str:
    """The branch a feature branch is usually compared against: origin/HEAD, else main/master."""
    try:
        ref = run_git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo).strip()
        if ref:
            return ref
    except GitError:
        pass
    for candidate in ("origin/main", "origin/master", "main", "master"):
        try:
            _rev_parse(candidate, repo)
            return candidate
        except GitError:
            continue
    raise GitError("could not find a default base branch (origin/HEAD, main or master); pass --base")


def resolve_revisions(diff_target: str, repo: Optional[str] = None) -> Tuple[str, str]:
    """
    Resolves a diff target into concrete (old_sha, new_sha):
      'base'       -> (merge-base(base, HEAD), HEAD)
      'a...b'      -> (merge-base(a, b), b)
      'a..b'       -> (a, b)
    Empty sides default to HEAD, as in git.
    """
    if "..." in diff_target:
        left, right = diff_target.split("...", 1)
        use_merge_base = True
    elif ".." in diff_target:
        left, right = diff_target.split("..", 1)
        use_merge_base = False
    else:
        left, right = diff_target, "HEAD"
        use_merge_base = True

    old = _rev_parse(left or "HEAD", repo)
    new = _rev_parse(right or "HEAD", repo)
    if use_merge_base:
        try:
            old = run_git(["merge-base", old, new], repo).strip() or old
        except GitError:
            pass  # unrelated histories: compare the two tips directly
    return old, new


def _unquote_path(raw: str) -> str:
    raw = raw.rstrip("\t")
    if raw.startswith('"') and raw.endswith('"'):
        inner = raw[1:-1]
        return codecs.escape_decode(inner.encode("latin-1", "backslashreplace"))[0].decode("utf-8", "replace")
    return raw


def _strip_prefix(path: str, prefix: str) -> Optional[str]:
    path = _unquote_path(path)
    if path == "/dev/null":
        return None
    return path[len(prefix):] if path.startswith(prefix) else path


def parse_unified_diff(text: str) -> List[FileDiff]:
    files: List[FileDiff] = []
    current: Optional[FileDiff] = None
    new_line = 0
    in_hunk = False
    pending_removed: List[str] = []

    def flush_removed(anchor_next: Optional[int]):
        if not pending_removed or current is None:
            pending_removed.clear()
            return
        pos = anchor_next if anchor_next is not None else new_line - 0.5
        for txt in pending_removed:
            current.lines.append(("-", pos, txt))
        pending_removed.clear()

    # Only "\n" ends a diff line; str.splitlines() would also split on form feed,
    # U+2028, \x85 … that can legitimately appear inside source lines.
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    for line in raw_lines:
        if line.startswith("diff --git "):
            flush_removed(None)
            current = FileDiff(old_path=None, new_path=None)
            files.append(current)
            in_hunk = False
            continue
        if current is None:
            continue
        if not in_hunk or line.startswith("@@ "):
            if line.startswith("--- "):
                current.old_path = _strip_prefix(line[4:], "a/")
            elif line.startswith("+++ "):
                current.new_path = _strip_prefix(line[4:], "b/")
            elif line.startswith("@@ "):
                flush_removed(None)
                m = HUNK_HEADER_RE.match(line)
                if m:
                    start = int(m.group(1))
                    count = int(m.group(2)) if m.group(2) is not None else 1
                    # For empty new-side hunks git reports the line *before* the change.
                    new_line = start + 1 if count == 0 else start
                    in_hunk = True
            continue

        kind, body = line[:1], line[1:]
        if kind == "-":
            pending_removed.append(body)
        elif kind == "+":
            flush_removed(new_line)
            current.lines.append(("+", new_line, body))
            new_line += 1
        elif kind in (" ", ""):
            flush_removed(None)
            current.lines.append((" ", new_line, body))
            new_line += 1
        elif kind == "\\":
            continue  # "\ No newline at end of file"
        else:
            flush_removed(None)
            in_hunk = False
    flush_removed(None)
    return [f for f in files if f.old_path is not None or f.new_path is not None]


def get_diff(diff_target: str, repo: Optional[str] = None) -> Tuple[str, str, List[FileDiff]]:
    """Returns (old_sha, new_sha, file diffs) for the given target."""
    old, new = resolve_revisions(diff_target, repo)
    out = run_git(["diff", *DIFF_FLAGS, old, new], repo)
    return old, new, parse_unified_diff(out)


def read_blobs(repo: Optional[str], specs: Sequence[Tuple[str, str]]) -> Dict[Tuple[str, str], Optional[str]]:
    """
    Reads many `rev:path` blobs in a single `git cat-file --batch` process.
    Missing objects map to None.
    """
    result: Dict[Tuple[str, str], Optional[str]] = {}
    specs = list(dict.fromkeys(specs))
    if not specs:
        return result
    request = "".join(f"{rev}:{path}\n" for rev, path in specs).encode("utf-8")
    try:
        res = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, input=request,
                             capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise GitError(f"git cat-file failed: {exc}") from exc

    data = res.stdout
    offset = 0
    for spec in specs:
        nl = data.index(b"\n", offset)
        header = data[offset:nl].decode("utf-8", "replace")
        offset = nl + 1
        parts = header.split()
        if len(parts) == 3 and parts[2].isdigit():
            size = int(parts[2])
            if parts[1] == "blob":
                result[spec] = data[offset:offset + size].decode("utf-8", "replace")
            else:
                result[spec] = None  # a tree or commit: skip its content to stay aligned
            offset += size + 1  # content + trailing LF
        else:
            result[spec] = None  # "<obj> missing"
    return result

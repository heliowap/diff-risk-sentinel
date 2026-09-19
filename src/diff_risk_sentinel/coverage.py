import os
import posixpath
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional


LineMap = Dict[int, bool]


class CoverageMap:
    """
    Line coverage keyed by repository-relative POSIX paths.

    Cobertura `filename` attributes are relative to a `<source>` root (often a package
    directory, not the repository root), so each entry is resolved against the declared
    sources and the report's own directory. Entries that cannot be resolved on disk are
    kept verbatim and matched by unique path suffix.
    """

    def __init__(self):
        self.files: Dict[str, LineMap] = {}
        self.unresolved: Dict[str, LineMap] = {}
        self.reports: List[str] = []
        self.errors: List[str] = []

    @staticmethod
    def _merge(target: Dict[str, LineMap], key: str, lines: LineMap):
        merged = target.setdefault(key, {})
        for nr, hit in lines.items():
            merged[nr] = merged.get(nr, False) or hit

    def lookup(self, repo_path: str) -> Optional[LineMap]:
        if repo_path in self.files:
            return self.files[repo_path]
        matches = [
            key for key in self.unresolved
            if repo_path.endswith("/" + key) or key.endswith("/" + repo_path) or key == repo_path
        ]
        if not matches:
            return None
        best = max(len(k) for k in matches)
        longest = [k for k in matches if len(k) == best]
        if len(longest) != 1:
            return None  # ambiguous suffix: refuse to guess
        return self.unresolved[longest[0]]


def _to_repo_relative(path: str, repo_root: str) -> Optional[str]:
    rel = os.path.relpath(os.path.realpath(path), repo_root)
    if rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def _parse_report(cov_path: str, repo_root: str, cmap: CoverageMap):
    root = ET.parse(cov_path).getroot()
    report_dir = os.path.dirname(os.path.abspath(cov_path))
    sources = []
    for src in root.findall("./sources/source"):
        if src.text and src.text.strip():
            s = src.text.strip()
            sources.append(s if os.path.isabs(s) else os.path.join(report_dir, s))

    for cls in root.iter("class"):
        fn = cls.get("filename")
        if not fn:
            continue
        lines: LineMap = {}
        for line in cls.iter("line"):
            try:
                nr = int(line.get("number", 0))
                hits = int(float(line.get("hits", 0)))
            except ValueError:
                continue
            lines[nr] = lines.get(nr, False) or hits > 0

        if os.path.isabs(fn):
            candidates = [fn]
        else:
            # Cobertura does not say which <source> a relative filename belongs to; if it
            # exists under more than one, any choice could attribute lines to the wrong file.
            candidates = [os.path.join(s, fn) for s in sources if os.path.isfile(os.path.join(s, fn))]
            if len({os.path.realpath(c) for c in candidates}) > 1:
                cmap.errors.append(f"{cov_path}: '{fn}' is ambiguous across <source> entries; coverage ignored for it")
                continue
            candidates += [os.path.join(b, fn) for b in (report_dir, repo_root)]
        key = None
        for cand in candidates:
            if os.path.isfile(cand):
                key = _to_repo_relative(cand, repo_root)
                if key:
                    break
        if key:
            cmap._merge(cmap.files, key, lines)
        else:
            cmap._merge(cmap.unresolved, posixpath.normpath(fn.replace("\\", "/")).lstrip("/"), lines)


def load_coverage(paths: Iterable[str], repo_root: str) -> CoverageMap:
    """Parses one or more Cobertura XML reports into a single CoverageMap."""
    cmap = CoverageMap()
    repo_root = os.path.realpath(repo_root)
    for path in paths:
        if not path:
            continue
        if not os.path.isfile(path):
            cmap.errors.append(f"{path}: file not found")
            continue
        try:
            _parse_report(path, repo_root, cmap)
            cmap.reports.append(path)
        except (ET.ParseError, OSError) as exc:
            cmap.errors.append(f"{path}: {exc}")
    return cmap

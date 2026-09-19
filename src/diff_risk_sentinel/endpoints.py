"""
Orphan HTTP endpoints: Python route handlers whose path nothing in the repository requests.

Function-level dead-code analysis cannot see these — a route is reached through its URL, not
its name. For every `@<router>.get/post/put/patch/delete/api_route/websocket/route("path")`
handler, the path (with its same-file `...Router(prefix=...)`) is searched in every file
that can call it: code in any language, configs, scripts. Not counted as callers: tests,
docs and generated API specs, route decorators (a facade and its backend share paths), and
the handler's own body (a proxy calling the same path downstream). Parameters match anything
the client interpolates; the last segment may also be interpolated (`/catalog/{id}/{action}`)
when another literal segment anchors the match. Paths without a distinctive literal
(`/`, `/{id}`) are not judged.

Callers outside the repository (provider webhooks, OAuth callbacks, manual ops endpoints)
cannot be seen; docs that mention the path are listed so a reviewer can tell. On a private
monorepo, a blind review of held-out findings found most removable; see
evals/dead_code_results.md.
"""

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .complexity import extract_functions
from .diff import read_blobs, run_git
from .signals import MIGRATION_PATH_RE, is_test_path

SKIP_DIRS = ("node_modules/", "/dist/", "/build/", "/.venv/", "/vendor/", "/__pycache__/")
MAX_TEXT_BYTES = 1_000_000
_TEST_SUPPORT_RE = re.compile(r"(^|/)(setupTests|test-utils|testUtils|conftest)\.[a-z]+$")
_NON_EXECUTING_RE = re.compile(r"(^|/)docs?/|\.(md|mdx|rst|adoc|txt)$|(^|/)(openapi|swagger)[^/]*\.(json|ya?ml)$", re.I)
_ROUTE_RE = re.compile(r"@(\w+)(?:\.\w+)*\.(get|post|put|patch|delete|api_route|websocket|route)\(\s*"
                       r"(?:path\s*=\s*)?[rf]?([\"'])(.*?)\3", re.S)
_ROUTER_PREFIX_RE = re.compile(r"^(\w+)\s*(?::[^=\n]+)?=\s*\w*Router\((?:[^()]|\([^()]*\))*?prefix\s*=\s*[\"']([^\"']*)[\"']",
                               re.M | re.S)
_DEF_RE = re.compile(r"\s*(async\s+)?def\s")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][\w-]*")  # no `$`: `reauthorize${suffix}` must yield `reauthorize`
_PARAM = r"(?:\$\{[^}]*\}|\{[^}]*\}|:\w+|[^/\"'`\s?]+)"
_CLIENT_VAR = r"(?:\$\{[^}]*\}|\{[^}]*\})"


def _is_param(segment: str) -> bool:
    return segment.startswith(("{", ":", "<"))


def _path_pattern(path: str) -> Optional[Dict[str, Any]]:
    segs = [s for s in path.strip("/").split("/") if s]
    static = [s for s in segs if not _is_param(s)]
    if not static or len("".join(static)) < 4:
        return None
    parts = [_PARAM if _is_param(s) else re.escape(s) for s in segs]
    anchors = static
    if not _is_param(segs[-1]) and len(static) >= 2:
        parts[-1] = f"(?:{re.escape(segs[-1])}|{_CLIENT_VAR})"
        anchors = static[:-1]
    # the client URL must end where the route ends (`/tasks/{id}` is not `/tasks/{id}/archive`)
    return {"regex": re.compile(r"(?<![\w-])" + "/".join(parts) + r"(?![\w-]|/[\w${])"),
            "anchor": max(anchors, key=len)}


def _text_files(repo: str, rev: str) -> Dict[str, str]:
    files = []
    for row in run_git(["ls-tree", "-r", "-l", rev], repo).split("\n"):
        meta, _, path = row.partition("\t")
        parts = meta.split()
        if len(parts) == 4 and parts[1] == "blob" and parts[3].isdigit() and int(parts[3]) <= MAX_TEXT_BYTES \
                and not any(s in f"/{path}" for s in SKIP_DIRS):
            files.append(path)
    blobs = read_blobs(repo, [(rev, f) for f in files])
    return {p: t for (_, p), t in blobs.items() if t and "\x00" not in t[:1000]}


def _routes(path: str, text: str) -> List[Dict[str, Any]]:
    lines = text.split("\n")
    prefixes = {m.group(1): m.group(2) for m in _ROUTER_PREFIX_RE.finditer(text)}
    found = []
    for fn in extract_functions(path, text) or []:
        head = []
        for line in lines[fn["start_line"] - 1:fn["end_line"]]:
            if _DEF_RE.match(line):
                break
            head.append(line)
        routes = [(m.group(2), prefixes.get(m.group(1), "").rstrip("/") + "/" + m.group(4).lstrip("/"))
                  for m in _ROUTE_RE.finditer("\n".join(head))]
        if routes:
            found.append({"function": fn["name"], "start": fn["start_line"], "end": fn["end_line"], "routes": routes})
    return found


def orphan_endpoints(repo: str, rev: str = "HEAD") -> List[Dict[str, Any]]:
    """Route handlers no file in the repository requests, at `rev`."""
    texts = _text_files(repo, rev)
    strip = lambda t: _ROUTE_RE.sub("", t)  # noqa: E731
    callers, docs = {}, {}
    for p, t in texts.items():
        if is_test_path(p) or _TEST_SUPPORT_RE.search(p):
            continue
        if _NON_EXECUTING_RE.search(p):
            docs[p] = t
        else:
            callers[p] = strip(t) if p.endswith(".py") else t
    tokens = defaultdict(set)
    for p, t in callers.items():
        for w in set(_TOKEN_RE.findall(t)):
            tokens[w].add(p)

    report = []
    for p, t in texts.items():
        if not p.endswith(".py") or p not in callers or MIGRATION_PATH_RE.search(p):
            continue
        lines = t.split("\n")
        for handler in _routes(p, t):
            patterns = [_path_pattern(path) for _, path in handler["routes"]]
            if any(pat is None for pat in patterns):
                continue
            own = strip("\n".join(lines[:handler["start"] - 1] + lines[handler["end"]:]))

            def requested(pat):
                if pat["regex"].search(own):
                    return True
                return any(pat["regex"].search(callers[c]) for c in tokens.get(pat["anchor"], ()) if c != p)

            if any(requested(pat) for pat in patterns):
                continue
            report.append({
                "file": p,
                "function": handler["function"],
                "lines": f"{handler['start']}-{handler['end']}",
                "method": handler["routes"][0][0],
                "path": " | ".join(path for _, path in handler["routes"]),
                "documented_in": sorted(d for d, dt in docs.items() if any(pat["regex"].search(dt) for pat in patterns))[:5],
            })
    report.sort(key=lambda r: (r["file"], int(r["lines"].split("-")[0])))
    return report

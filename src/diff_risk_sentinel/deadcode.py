"""
Repository-wide dead-code scan (static).

A production function is reported when its name is referenced nowhere else in production
code: `unreferenced` (not even in tests) or `tests_only`. Entry points that are reached
without being named are excluded: decorated Python functions (routes, validators, tasks),
`export default`, dunders, JS/TS `constructor`s, well-known framework hooks (http.server
`do_*`/`log_message`, unittest `setUp`, alembic `upgrade`, React class lifecycle, `main`),
`onXxx` callbacks handed to libraries, and names built for dynamic dispatch
(`getattr(obj, f"_tool_{name}")` excludes every `_tool_*`).

On a 10k-function production monorepo, a team acting on the report removed 97% of the
`unreferenced` functions and 62% of the `tests_only` ones (the CLI lists the latter apart).
Asking TypeSafe Jev whether a function "does no real work" did not raise precision; judging
the reference evidence does (deadcode_evidence.py). Routes reached by URL are covered by
endpoints.py. Evidence: evals/dead_code_results.md.
"""

import ast
import re
import textwrap
from collections import Counter
from typing import Any, Dict, List

from .complexity import _strip_js, extract_functions, supported_extensions
from .diff import read_blobs, run_git
from .signals import MIGRATION_PATH_RE, is_test_path

SKIP_DIRS = ("node_modules/", "/dist/", "/build/", "/.venv/", "/vendor/", "/__pycache__/")
TEST_SUPPORT_RE = re.compile(r"(^|/)(setupTests|test-utils|testUtils|conftest)\.[a-z]+$")
FRAMEWORK_HOOKS = {
    "do_GET", "do_POST", "do_PUT", "do_PATCH", "do_DELETE", "do_HEAD", "do_OPTIONS",
    "log_message", "log_request", "log_error", "handle", "setUp", "tearDown", "setUpClass",
    "tearDownClass", "main", "upgrade", "downgrade", "run",
    "constructor",  # JS/TS: runs on `new Class(...)`, never called by name
    # React class lifecycle
    "render", "componentDidMount", "componentDidUpdate", "componentWillUnmount", "componentDidCatch",
    "shouldComponentUpdate", "getSnapshotBeforeUpdate", "getDerivedStateFromError", "getDerivedStateFromProps",
}
_EVENT_CALLBACK_RE = re.compile(r"^on[A-Z]")  # handlers handed to libraries/components in option objects
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_DYNAMIC_PREFIX_RES = [
    re.compile(r"getattr\([^,()]+,\s*f[\"']([A-Za-z_]\w*?)\{"),
    re.compile(r"getattr\([^,()]+,\s*[\"']([A-Za-z_]\w*)[\"']\s*\+"),
]
_TS_STUB_RE = re.compile(
    r"^(?:return\s*(?:\[\s*\]|\{\s*\}|null|undefined|false|true|0|''|\"\"|``|new\s+(?:Set|Map)\(\s*\)|"
    r"Promise\.resolve\(\s*(?:\[\s*\]|\{\s*\}|null|undefined)?\s*\))?\s*;?|throw\s+new\s+Error\([^)]*\)\s*;?)?$"
)


def _python_stub(code: str) -> bool:
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError:
        return False
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if fn is None:
        return False
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body:
        return True
    if len(body) != 1:
        return False
    s = body[0]
    if isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                                   and s.value.value is Ellipsis):
        return True
    if isinstance(s, ast.Raise):
        return True
    if isinstance(s, ast.Return):
        v = s.value
        if v is None or isinstance(v, ast.Constant):
            return True
        if isinstance(v, (ast.List, ast.Tuple, ast.Set)) and not v.elts:
            return True
        if isinstance(v, ast.Dict) and not v.keys:
            return True
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and not v.args and not v.keywords \
                and v.func.id in {"set", "list", "dict", "tuple", "frozenset"}:
            return True
    return False


def _ts_stub(code: str) -> bool:
    clean = _strip_js(code)
    start, end = clean.find("{"), clean.rfind("}")
    if start < 0 or end <= start:
        return False
    return bool(_TS_STUB_RE.match(" ".join(clean[start + 1:end].split())))


def is_stub(path: str, code: str) -> bool:
    """Body only passes, returns a constant/empty value, or raises."""
    return _python_stub(code) if path.endswith(".py") else _ts_stub(code)


def _is_test(path: str) -> bool:
    return is_test_path(path) or bool(TEST_SUPPORT_RE.search(path))


def _entry_point(short: str, first_line: str, prev_line: str, is_py: bool, dynamic_prefixes) -> bool:
    if short.startswith(("<", "test")) or len(short) < 3 or short in FRAMEWORK_HOOKS:
        return True
    if short.startswith("__") and short.endswith("__"):
        return True
    if any(short.startswith(p) for p in dynamic_prefixes):
        return True
    if is_py:
        return first_line.startswith("@") and not first_line.startswith(("@staticmethod", "@classmethod", "@property"))
    if _EVENT_CALLBACK_RE.match(short):
        return True
    return "export default" in first_line or "export default" in prev_line


def scan_repository(repo: str, rev: str = "HEAD") -> List[Dict[str, Any]]:
    """Unreferenced / tests-only production functions at `rev`, most certain first."""
    exts = supported_extensions()
    paths = [p for p in run_git(["ls-tree", "-r", "--name-only", rev], repo).split("\n")
             if p.endswith(exts) and not p.endswith(".d.ts") and not any(s in f"/{p}" for s in SKIP_DIRS)]
    blobs = read_blobs(repo, [(rev, p) for p in paths])

    prod_words: Counter = Counter()
    test_words: Counter = Counter()
    dynamic_prefixes = set()
    for p in paths:
        text = blobs.get((rev, p)) or ""
        (test_words if _is_test(p) else prod_words).update(_IDENT_RE.findall(text))
        if not _is_test(p):
            for rx in _DYNAMIC_PREFIX_RES:
                dynamic_prefixes.update(m.group(1) for m in rx.finditer(text))

    candidates = []
    for p in paths:
        if _is_test(p) or MIGRATION_PATH_RE.search(p):
            continue
        text = blobs.get((rev, p)) or ""
        lines = text.split("\n")
        for fn in extract_functions(p, text) or []:
            short = fn["name"].split(".")[-1].split("#")[0]
            first = lines[fn["start_line"] - 1].strip() if fn["start_line"] <= len(lines) else ""
            prev = lines[fn["start_line"] - 2].strip() if fn["start_line"] >= 2 else ""
            candidates.append((p, fn, short, _entry_point(short, first, prev, p.endswith(".py"), dynamic_prefixes)))

    definitions = Counter(short for _, _, short, _ in candidates)
    report = []
    for p, fn, short, excluded in candidates:
        if excluded or prod_words[short] - definitions[short] > 0:
            continue
        code = "\n".join((blobs.get((rev, p)) or "").split("\n")[fn["start_line"] - 1:fn["end_line"]])
        report.append({
            "file": p,
            "function": fn["name"],
            "lines": f"{fn['start_line']}-{fn['end_line']}",
            "status": "tests_only" if test_words[short] else "unreferenced",
            "test_references": test_words[short],
            "stub": is_stub(p, code),
        })
    report.sort(key=lambda r: (r["status"] != "unreferenced", not r["stub"], r["file"], int(r["lines"].split("-")[0])))
    return report

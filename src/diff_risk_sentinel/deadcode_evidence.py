"""
Evidence packs for dead-code judgments.

Whether a function is dead depends on how the rest of the repository refers to it, not on
its body. For each production function this module collects, at a revision:
  * the function source (with decorators),
  * every line that mentions its name as a word, labelled production/test, same file or
    not, and whether the line merely *defines* another function with the same name,
  * the files that import its module,
  * dynamic-dispatch prefixes used in the repository (getattr(obj, f"_tool_{x}")).
A cheap generous rule picks candidates; a judge (TypeSafe Jev) reads the evidence, not the body.
"""

import ast
import os
import re
import textwrap
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from .complexity import extract_functions, supported_extensions
from .deadcode import is_stub
from .diff import read_blobs, run_git
from .signals import MIGRATION_PATH_RE, is_test_path

SKIP_DIRS = ("node_modules/", "/dist/", "/build/", "/.venv/", "/vendor/", "/__pycache__/")
TEST_SUPPORT_RE = re.compile(r"(^|/)(setupTests|test-utils|testUtils|conftest)\.[a-z]+$")
_DYNAMIC_PREFIX_RES = [
    re.compile(r"getattr\([^,()]+,\s*f[\"']([A-Za-z_]\w*?)\{"),
    re.compile(r"getattr\([^,()]+,\s*[\"']([A-Za-z_]\w*)[\"']\s*\+"),
]
MAX_CODE_CHARS = 6000
MAX_REFS = 30


def is_test_file(path: str) -> bool:
    return is_test_path(path) or bool(TEST_SUPPORT_RE.search(path))


def _definition_re(name: str) -> re.Pattern:
    n = re.escape(name)
    return re.compile(
        rf"(\bdef\s+{n}\s*\(|\bfunction\s*\*?\s*{n}\s*[<(]|\b(?:const|let|var)\s+{n}\s*(?::[^=]+)?=|"
        rf"^\s*(?:(?:public|private|protected|static|async|get|set|readonly|override)\s+)*{n}\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^={{]+)?\{{)"
    )


class RepoIndex:
    """All code files at a revision, their text, and their functions."""

    def __init__(self, repo: str, rev: str):
        self.repo, self.rev = repo, rev
        exts = supported_extensions()
        self.paths = [p for p in run_git(["ls-tree", "-r", "--name-only", rev], repo).split("\n")
                      if p.endswith(exts) and not p.endswith(".d.ts") and not any(s in f"/{p}" for s in SKIP_DIRS)]
        blobs = read_blobs(repo, [(rev, p) for p in self.paths])
        self.text = {p: blobs.get((rev, p)) or "" for p in self.paths}
        self.lines = {p: t.split("\n") for p, t in self.text.items()}
        self.functions: Dict[str, List[Dict[str, Any]]] = {}
        self.defs_by_name: Dict[str, List[str]] = defaultdict(list)
        self.dynamic_prefixes = set()
        for p in self.paths:
            fns = extract_functions(p, self.text[p]) or []
            self.functions[p] = fns
            for f in fns:
                self.defs_by_name[f["name"].split(".")[-1].split("#")[0]].append(p)
            if not is_test_file(p):
                for rx in _DYNAMIC_PREFIX_RES:
                    self.dynamic_prefixes.update(m.group(1) for m in rx.finditer(self.text[p]))
        words = Counter()
        self.word_files: Dict[str, set] = defaultdict(set)
        for p, t in self.text.items():
            for w in set(re.findall(r"[A-Za-z_$][\w$]*", t)):
                self.word_files[w].add(p)

    def production_functions(self):
        for p in self.paths:
            if is_test_file(p) or MIGRATION_PATH_RE.search(p):
                continue
            for f in self.functions[p]:
                yield p, f

    def find(self, path: str, name: str) -> Optional[Dict[str, Any]]:
        return next((f for f in self.functions.get(path, []) if f["name"] == name), None)


def short_name(qualified: str) -> str:
    return qualified.split(".")[-1].split("#")[0]


def evidence(index: RepoIndex, path: str, fn: Dict[str, Any]) -> Dict[str, Any]:
    name = short_name(fn["name"])
    lines = index.lines[path]
    code = "\n".join(lines[fn["start_line"] - 1:fn["end_line"]])[:MAX_CODE_CHARS]
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    define = _definition_re(name)
    refs = []
    for p in sorted(index.word_files.get(name, ())):
        for i, line in enumerate(index.lines[p], 1):
            if p == path and fn["start_line"] <= i <= fn["end_line"]:
                continue  # the function's own body (and signature)
            if word.search(line):
                refs.append({
                    "file": p, "line": i, "text": line.strip()[:200],
                    "test": is_test_file(p), "same_file": p == path,
                    "defines_same_name": bool(define.search(line)),
                })
    counts = {
        "production_other_files": sum(1 for r in refs if not r["test"] and not r["same_file"] and not r["defines_same_name"]),
        "production_same_file": sum(1 for r in refs if r["same_file"] and not r["defines_same_name"]),
        "tests": sum(1 for r in refs if r["test"]),
        "other_definitions_same_name": sum(1 for r in refs if r["defines_same_name"]),
    }
    refs.sort(key=lambda r: (r["test"], r["same_file"], r["defines_same_name"]))
    module = os.path.splitext(os.path.basename(path))[0]
    importers = sorted(p for p in index.word_files.get(module, ()) if p != path
                       and re.search(rf"(import|from)[^\n]*\b{re.escape(module)}\b", index.text[p]))
    return {
        "file": path,
        "function": fn["name"],
        "code": code,
        "reference_counts": counts,
        "references": refs[:MAX_REFS],
        "references_truncated": len(refs) > MAX_REFS,
        "module_imported_by": importers[:10],
        "module_imported_by_count": len(importers),
        "dynamic_dispatch_prefixes_matching": sorted(p for p in index.dynamic_prefixes if name.startswith(p)),
    }


def is_candidate(ev: Dict[str, Any], max_uses_elsewhere: int = 3) -> bool:
    """Generous pre-filter: rarely used by name in other production files."""
    return ev["reference_counts"]["production_other_files"] <= max_uses_elsewhere


# Operating points chosen on a labelled dev half and checked once on the held-out half and by a
# blind repo-wide review (evals/dead_code_results.md): a static finding Jev scores below
# VETO_BELOW is dropped (framework hooks the static rules miss); a function that production
# mentions only through homonyms, export lists or its own file is proposed as probable at
# PROBABLE_FROM or above.
VETO_BELOW = 0.5
PROBABLE_FROM = 0.7

JUDGE_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "removable": {
        "type": "noul",
        "instructions": (
            "Decide from `evidence_summary` and `mentions` only (not from what the function looks like): could this "
            "function be deleted without changing what runs in production? Mentions in tests, docs, comments or "
            "export/re-export lists do not keep it alive. A mention that defines or calls a different function "
            "with the same name does not keep it alive. A decorator in `signature_and_decorators` that registers "
            "it with a framework (HTTP route, validator, event handler), a lifecycle/callback method name, or a "
            "matching dynamic-dispatch prefix does keep it alive."
        ),
        "criteria": {
            "true": "No production mention actually calls, passes, renders or registers this function.",
            "false": "At least one production mention (or a decorator/callback/dynamic dispatch) actually uses this function.",
        },
    },
}

STUB_INTENT_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "is_intentional_stub": {
        "type": "noul",
        "instructions": (
            "The function `{function}` in `{file}` has a stub/no-op body (passes, returns empty/None, or raises NotImplementedError). "
            "Examine its signature, decorators, docstring, class context, and body:\n"
            "Is this function an intentional architectural pattern (abstract interface method, protocol specification, "
            "default no-op hook for subclasses/plugins, or fail-safe fallback adapter)?\n"
            "Or is it abandoned, unfinished stub code (forgotten placeholder, incomplete implementation, or leftover from a deleted feature)?"
        ),
        "criteria": {
            "true": "Intentional architectural stub: abstract method, protocol hook, base class default, or adapter fallback.",
            "false": "Abandoned, unfinished stub, forgotten placeholder, or dead code that should be deleted.",
        },
    },
    "stub_pattern": {
        "type": "choice",
        "instructions": "Classify the pattern or architectural role of this stub function:",
        "criteria": {
            "protocol_or_abstract": "Abstract method or protocol definition intended to be implemented by subclasses.",
            "hook_or_default": "Deliberate no-op default hook or lifecycle callback intended for optional override.",
            "fallback_adapter": "Fail-safe dummy adapter, null object, or simulator implementation.",
            "abandoned_stub": "Unimplemented, dead, or obsolete placeholder with no architectural justification.",
        },
    },
}

TESTS_ONLY_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "is_testability_seam": {
        "type": "noul",
        "instructions": (
            "The production function `{function}` in `{file}` has zero production callers and is only mentioned in tests. "
            "Examine its implementation and the test usage excerpts in `test_mentions`:\n"
            "Is this function an intentional testability seam (such as a cache reset hook, state teardown, dependency injection setter, "
            "test fake/mock hook, or test fixture utility designed to enable testing of other production systems)?\n"
            "Or is it a dead/abandoned production feature that was retired or never adopted, leaving only obsolete tests behind?"
        ),
        "criteria": {
            "true": "Intentional testability seam, fixture hook, state resetter, or test helper for live code.",
            "false": "Abandoned/dead feature or unused helper whose tests were left behind or forgotten.",
        },
    },
    "test_usage_role": {
        "type": "choice",
        "instructions": "Classify the role of this tests-only function:",
        "criteria": {
            "testability_seam": "Deliberate hook, setter, or reset method used by tests to control or verify live production code.",
            "orphaned_feature": "Obsolete or abandoned feature whose only caller is a leftover test asserting its own dead behavior.",
            "fixture_or_utility": "General test fixture, test double, or test assertion helper located in a production file.",
        },
    },
}


def _extract_class_context(lines: List[str], start_line: int, fn_name: str) -> str:
    """Finds enclosing class or interface definition header."""
    if "." not in fn_name and "#" not in fn_name:
        return ""
    class_name = fn_name.split(".")[0].split("#")[0]
    rx = re.compile(
        rf"^\s*(?:export\s+)?(?:abstract\s+)?(?:class|interface)\s+{re.escape(class_name)}\b"
    )
    for i in range(max(0, start_line - 2), -1, -1):
        line = lines[i]
        if rx.search(line):
            header = [line.strip()]
            if "{" in line or ":" in line:
                return " ".join(header)
            for j in range(i + 1, min(len(lines), i + 5)):
                header.append(lines[j].strip())
                if "{" in lines[j] or ":" in lines[j]:
                    break
            return " ".join(header)
    return ""


def _extract_docstring(code: str, lines: List[str], start_line: int, is_py: bool) -> str:
    """Extracts function docstring or leading documentation comments."""
    if is_py:
        try:
            tree = ast.parse(textwrap.dedent(code))
            fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
            if fn:
                doc = ast.get_docstring(fn)
                if doc:
                    return doc.strip()
        except Exception:
            pass

    doc_lines = []
    in_block = False
    for i in range(max(0, start_line - 2), -1, -1):
        l = lines[i].strip()
        if not in_block and l.endswith("*/"):
            in_block = True
            doc_lines.append(l)
            if l.startswith("/*"):
                break
        elif in_block:
            doc_lines.append(l)
            if l.startswith("/*"):
                break
        elif l.startswith(("@", "#", "//")) or not l:
            if l.startswith(("#", "//")):
                doc_lines.append(l)
            continue
        else:
            break
    if doc_lines:
        return "\n".join(reversed(doc_lines))
    return ""


def stub_state(index: RepoIndex, path: str, fn: Dict[str, Any]) -> Dict[str, Any]:
    """Prepares evidence state for judging stub/no-op intentionality."""
    lines = index.lines[path]
    start_line = fn["start_line"]
    end_line = fn["end_line"]
    code = "\n".join(lines[start_line - 1:end_line])[:MAX_CODE_CHARS]

    class_context = _extract_class_context(lines, start_line, fn["name"])
    docstring = _extract_docstring(code, lines, start_line, path.endswith(".py"))
    signature = "\n".join(code.split("\n")[:4])

    return {
        "function": fn["name"],
        "file": path,
        "class_context": class_context or "(top-level function)",
        "signature_and_decorators": signature,
        "docstring": docstring or "(no docstring)",
        "code": code,
        "is_stub": is_stub(path, code),
    }


def tests_only_state(ev: Dict[str, Any], index: Optional[RepoIndex] = None) -> Dict[str, Any]:
    """Prepares evidence state for judging whether a tests-only function is a test seam or dead feature."""
    test_mentions = []
    for r in ev.get("references", []):
        if r.get("test"):
            snippet = r.get("text", "")
            if index and r.get("file") in index.lines:
                ln = r["line"] - 1
                flines = index.lines[r["file"]]
                start_l = max(0, ln - 1)
                end_l = min(len(flines), ln + 2)
                context_lines = [flines[k].strip() for k in range(start_l, end_l) if flines[k].strip()]
                if context_lines:
                    snippet = "  |  ".join(context_lines)
            test_mentions.append(f"[{r.get('file', '')}:{r.get('line', '')}] {snippet}")

    code = ev.get("code", "")
    return {
        "function": ev.get("function", ""),
        "file": ev.get("file", ""),
        "signature_and_decorators": "\n".join(code.split("\n")[:4]),
        "function_code": code[:2000],
        "test_references_count": ev.get("reference_counts", {}).get("tests", 0),
        "test_mentions": test_mentions[:15] or ["(no test references)"],
    }


def judge_state(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Evidence first: counts in words and every mention tagged, without the function body.

    Given the body and a nested reference list, Jev judged what the code looked like and often
    answered "used by other files" for functions with zero production references.
    """
    c = ev["reference_counts"]
    mentions = []
    for r in ev["references"]:
        tags = ["TEST" if r["test"] else "PRODUCTION", "SAME FILE" if r["same_file"] else "OTHER FILE"]
        if r["defines_same_name"]:
            tags.append("DEFINES ANOTHER FUNCTION WITH THE SAME NAME")
        mentions.append(f"[{', '.join(tags)}] {r['file']}:{r['line']}: {r['text']}")
    summary = (f"The name `{short_name(ev['function'])}` appears {c['production_other_files']} time(s) in other "
               f"production files, {c['production_same_file']} time(s) elsewhere in its own file, {c['tests']} "
               f"time(s) in tests, and {c['other_definitions_same_name']} line(s) define a different function "
               f"with the same name.")
    if ev.get("references_truncated"):
        summary += " Only the first mentions are listed."
    if ev["dynamic_dispatch_prefixes_matching"]:
        summary += (f" The repository calls methods dynamically by the prefix(es) "
                    f"{ev['dynamic_dispatch_prefixes_matching']}, which match this name.")
    return {"function": ev["function"], "file": ev["file"],
            "signature_and_decorators": "\n".join(ev["code"].split("\n")[:4]),
            "evidence_summary": summary,
            "mentions": mentions or ["(no mentions anywhere else in the repository)"]}


def judge(api_key: str, ev: Dict[str, Any], **kw) -> Dict[str, Any]:
    from .jev import ask_jev
    data = ask_jev(api_key, judge_state(ev), JUDGE_QUESTIONS, **kw)
    if "error" in data:
        return data
    try:
        return {"p_removable": float(data["answers"]["removable"]["noul"]),
                "input_tokens": (data.get("usage") or {}).get("input_tokens")}
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"malformed answer: {exc}"}


def judge_stub(api_key: str, state: Dict[str, Any], **kw) -> Dict[str, Any]:
    """Queries Jev to determine if a stub/no-op is an intentional architectural pattern."""
    from .jev import ask_jev
    data = ask_jev(api_key, state, STUB_INTENT_QUESTIONS, **kw)
    if "error" in data:
        return data
    try:
        answers = data.get("answers", {})
        p = float(answers["is_intentional_stub"]["noul"])
        pat = str(answers.get("stub_pattern", {}).get("choice", "unknown"))
        return {
            "is_intentional_stub": p,
            "stub_pattern": pat,
            "input_tokens": (data.get("usage") or {}).get("input_tokens"),
        }
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"malformed answer: {exc}"}


def judge_tests_only(api_key: str, state: Dict[str, Any], **kw) -> Dict[str, Any]:
    """Queries Jev to determine if a tests-only function is a testability seam or dead feature."""
    from .jev import ask_jev
    data = ask_jev(api_key, state, TESTS_ONLY_QUESTIONS, **kw)
    if "error" in data:
        return data
    try:
        answers = data.get("answers", {})
        p = float(answers["is_testability_seam"]["noul"])
        role = str(answers.get("test_usage_role", {}).get("choice", "unknown"))
        return {
            "is_testability_seam": p,
            "test_usage_role": role,
            "input_tokens": (data.get("usage") or {}).get("input_tokens"),
        }
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"malformed answer: {exc}"}


def review_with_jev(repo: str, rev: str, found: List[Dict[str, Any]], api_key: str, workers: int = 16,
                    judge_fn=None, stub_judge_fn=None, tests_only_judge_fn=None,
                    classify_stubs: bool = True, classify_tests_only: bool = True) -> Dict[str, Any]:
    """Jev review of a static scan: veto static findings, propose probable ones,

    and classify intentional stubs (protocols/adapters) vs testability seams (resets/DI).
    Static findings whose judgment fails are kept (the static rule stands on its own).
    """
    from concurrent.futures import ThreadPoolExecutor
    from .deadcode import _entry_point

    index = RepoIndex(repo, rev)
    static = {(f["file"], f["function"]) for f in found}
    evs, probable_evs = {}, []
    for path, fn in index.production_functions():
        key = (path, fn["name"])
        if key in static:
            evs[key] = evidence(index, path, fn)
            continue
        lines, short = index.lines[path], short_name(fn["name"])
        first = lines[fn["start_line"] - 1].strip() if fn["start_line"] <= len(lines) else ""
        prev = lines[fn["start_line"] - 2].strip() if fn["start_line"] >= 2 else ""
        if "<anonymous>" in fn["name"] or _entry_point(short, first, prev, path.endswith(".py"), index.dynamic_prefixes):
            continue
        ev = evidence(index, path, fn)
        if is_candidate(ev):
            probable_evs.append(ev)

    # Resolve judges
    try:
        from unittest.mock import Mock
        is_mock_judge = isinstance(judge, Mock)
    except ImportError:
        is_mock_judge = False

    custom_judge_provided = judge_fn is not None or is_mock_judge
    primary_judge = judge_fn or (judge if is_mock_judge else (lambda key, ev: judge(key, ev)))

    if stub_judge_fn is None:
        if custom_judge_provided:
            stub_judge_fn = primary_judge
        else:
            stub_judge_fn = lambda key, s: judge_stub(key, s)

    if tests_only_judge_fn is None:
        if custom_judge_provided:
            tests_only_judge_fn = primary_judge
        else:
            tests_only_judge_fn = lambda key, s: judge_tests_only(key, s)

    tasks = []
    for f in found:
        key = (f["file"], f["function"])
        fn = index.find(f["file"], f["function"])
        ev = evs.get(key)
        if classify_stubs and f.get("stub") and fn:
            s_state = stub_state(index, f["file"], fn)
            tasks.append((f, "stub", s_state))
        elif classify_tests_only and f.get("status") == "tests_only" and ev:
            t_state = tests_only_state(ev, index)
            tasks.append((f, "tests_only", t_state))
        else:
            tasks.append((f, "standard", ev or (evidence(index, f["file"], fn) if fn else {})))

    prob_tasks = [(ev, "probable", ev) for ev in probable_evs]
    all_tasks = tasks + prob_tasks

    def _execute_task(task):
        item, kind, payload = task
        if kind == "stub":
            return kind, stub_judge_fn(api_key, payload)
        elif kind == "tests_only":
            return kind, tests_only_judge_fn(api_key, payload)
        else:
            return kind, primary_judge(api_key, payload)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(_execute_task, all_tasks))

    dead, vetoed, intentional_stubs, test_seams = [], [], [], []
    failures = 0

    for (f, kind, _), (res_kind, ans) in zip(tasks, results[:len(tasks)]):
        if not ans or "error" in ans:
            failures += 1
            dead.append({**f, "jev_error": (ans or {}).get("error", "no response")})
            continue

        if kind == "stub" and "is_intentional_stub" in ans:
            p_intent = ans["is_intentional_stub"]
            pat = ans.get("stub_pattern")
            item = {
                **f,
                "is_intentional_stub": p_intent,
                "stub_pattern": pat,
                "jev_input_tokens": ans.get("input_tokens"),
            }
            if p_intent >= 0.5 or pat in ("protocol_or_abstract", "hook_or_default", "fallback_adapter"):
                item["veto_reason"] = "intentional_stub"
                intentional_stubs.append(item)
                vetoed.append(item)
            else:
                item["jev_p_removable"] = round(1.0 - p_intent, 3)
                dead.append(item)
        elif kind == "tests_only" and "is_testability_seam" in ans:
            p_seam = ans["is_testability_seam"]
            role = ans.get("test_usage_role")
            item = {
                **f,
                "is_testability_seam": p_seam,
                "test_usage_role": role,
                "jev_input_tokens": ans.get("input_tokens"),
            }
            if p_seam >= 0.5 or role in ("testability_seam", "fixture_or_utility"):
                item["veto_reason"] = "testability_seam"
                test_seams.append(item)
                vetoed.append(item)
            else:
                item["jev_p_removable"] = round(1.0 - p_seam, 3)
                dead.append(item)
        else:
            score = ans.get("p_removable")
            if score is None:
                failures += 1
                dead.append(f)
            else:
                item = {**f, "jev_p_removable": score, "jev_input_tokens": ans.get("input_tokens")}
                if score < VETO_BELOW:
                    item["veto_reason"] = "framework_hook"
                    vetoed.append(item)
                else:
                    dead.append(item)

    probable = []
    for (ev, kind, _), (res_kind, ans) in zip(prob_tasks, results[len(tasks):]):
        if not ans or "error" in ans or "p_removable" not in ans:
            failures += 1
            continue
        score = ans["p_removable"]
        if score >= PROBABLE_FROM:
            fn = index.find(ev["file"], ev["function"])
            lines_str = f"{fn['start_line']}-{fn['end_line']}" if fn else ""
            probable.append({
                "file": ev["file"],
                "function": ev["function"],
                "lines": lines_str,
                "jev_p_removable": score,
                "reference_counts": ev["reference_counts"],
                "jev_input_tokens": ans.get("input_tokens"),
            })

    probable.sort(key=lambda x: -x["jev_p_removable"])
    return {
        "dead_code": dead,
        "vetoed_by_jev": vetoed,
        "probable_dead": probable,
        "intentional_stubs": intentional_stubs,
        "test_seams": test_seams,
        "jev_judged": len(all_tasks),
        "jev_failures": failures,
    }

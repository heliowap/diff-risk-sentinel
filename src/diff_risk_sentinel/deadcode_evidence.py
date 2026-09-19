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

import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from .complexity import extract_functions, supported_extensions
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


def review_with_jev(repo: str, rev: str, found: List[Dict[str, Any]], api_key: str, workers: int = 16,
                    judge_fn=None) -> Dict[str, Any]:
    """Jev review of a static scan: veto static findings, propose probable ones.

    Static findings whose judgment fails are kept (the static rule stands on its own).
    """
    from concurrent.futures import ThreadPoolExecutor
    from .deadcode import _entry_point
    judge_fn = judge_fn or (lambda key, ev: judge(key, ev))
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
    todo = list(evs.values()) + probable_evs
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        answers = list(pool.map(lambda ev: judge_fn(api_key, ev), todo))
    p = {(ev["file"], ev["function"]): a.get("p_removable") for ev, a in zip(todo, answers)}

    dead, vetoed = [], []
    for f in found:
        score = p.get((f["file"], f["function"]))
        item = {**f, "jev_p_removable": score}
        (vetoed if score is not None and score < VETO_BELOW else dead).append(item)
    probable = []
    for ev in probable_evs:
        score = p[(ev["file"], ev["function"])]
        if score is not None and score >= PROBABLE_FROM:
            fn = index.find(ev["file"], ev["function"])
            probable.append({"file": ev["file"], "function": ev["function"],
                             "lines": f"{fn['start_line']}-{fn['end_line']}", "jev_p_removable": score,
                             "reference_counts": ev["reference_counts"]})
    probable.sort(key=lambda x: -x["jev_p_removable"])
    return {"dead_code": dead, "vetoed_by_jev": vetoed, "probable_dead": probable,
            "jev_judged": len(todo), "jev_failures": sum(1 for a in answers if "p_removable" not in a)}

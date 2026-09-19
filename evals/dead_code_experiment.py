#!/usr/bin/env python3
"""
Can TypeSafe Jev detect dead code / no-op functions?

Static ground truth over every production function at a revision:
  * stub: the body only passes, returns a constant/empty value, or raises NotImplementedError;
  * unreferenced: the function's name appears nowhere else in the repository's code
    (framework entry points excluded: decorated Python functions, `export default`,
    dunders, main/upgrade/downgrade, test_*).
Jev is asked two questions about each function (code only, no repository context):
  * no_real_work: is it a stub / placeholder / no-op rather than an implementation?
  * looks_obsolete: does it look like a leftover of a removed feature or integration?
Reports AUC and precision/recall against the static labels, and writes the functions Jev
flags that static analysis does not (candidates for manual review) to --candidates.

Usage:
    TYPESAFE_API_KEY=... python3 evals/dead_code_experiment.py --repo /path/to/repo --rev <sha> \\
        [--cache /tmp/jev_dead_cache.json] [--workers 16] [--output results.json] [--candidates cands.json]
"""

import argparse
import bisect
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from diff_risk_sentinel.complexity import extract_functions, supported_extensions  # noqa: E402
from diff_risk_sentinel.deadcode import is_stub  # noqa: E402
from diff_risk_sentinel.signals import MIGRATION_PATH_RE, is_test_path  # noqa: E402

API_URL = "https://api.typesafe.ai/v1/systemone"
MAX_CODE_CHARS = 48_000
QUESTIONS = {
    "no_real_work": {
        "type": "noul",
        "instructions": (
            "`code` is one function from a production codebase. Does this function do no real work — is it a "
            "stub, placeholder or no-op rather than an implementation of behavior? Examples of no real work: "
            "it returns a fixed or empty value regardless of its inputs, it only logs, it only raises "
            "'not implemented', or its logic is explicitly disabled."
        ),
        "criteria": {
            "true": "The function is a stub, placeholder, no-op or disabled implementation.",
            "false": "The function implements real behavior (computes, transforms, validates, calls, persists).",
        },
    },
    "looks_obsolete": {
        "type": "noul",
        "instructions": (
            "Does this function look like a leftover of a removed feature or integration — for example "
            "references to a retired system, a compatibility shim kept after a migration, a disabled path, "
            "a comment saying it should be removed or reactivated later, or logic that expects a data shape "
            "that the rest of the system may no longer produce?"
        ),
        "criteria": {
            "true": "Signs that the function is obsolete, vestigial or kept only for compatibility.",
            "false": "Nothing suggests the function is obsolete.",
        },
    },
}
SKIP_DIRS = ("node_modules/", "/dist/", "/build/", "/.venv/", "/vendor/", "/__pycache__/")
ENTRYPOINT_NAMES = {"main", "upgrade", "downgrade", "setup", "teardown", "setUp", "tearDown", "run", "handler"}
IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def export_tree(repo, rev):
    raw = subprocess.run(["git", "archive", "--format=tar", rev], cwd=repo, capture_output=True, check=True).stdout
    files = {}
    exts = supported_extensions()
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for m in tar.getmembers():
            if not m.isfile() or not m.name.endswith(exts) or m.name.endswith(".d.ts"):
                continue
            if any(s in f"/{m.name}" for s in SKIP_DIRS):
                continue
            files[m.name] = tar.extractfile(m).read().decode("utf-8", "replace")
    return files


def python_stub(code):
    return is_stub("f.py", code)


def ts_stub(code):
    return is_stub("f.ts", code)


def decorated_or_exported(lines, start_line, is_py):
    prev = lines[start_line - 2].strip() if start_line >= 2 else ""
    first = lines[start_line - 1].strip() if start_line >= 1 else ""
    if is_py:
        if first.startswith("@"):
            return not first.startswith(("@staticmethod", "@classmethod", "@property"))
        return False
    return "export default" in first or "export default" in prev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--rev", required=True)
    ap.add_argument("--cache", default="/tmp/jev_dead_cache.json")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--output")
    ap.add_argument("--candidates")
    args = ap.parse_args()
    api_key = os.environ["TYPESAFE_API_KEY"]

    files = export_tree(args.repo, args.rev)
    words = Counter()
    for text in files.values():
        words.update(IDENT_RE.findall(text))
    functions = []
    for path, text in files.items():
        if is_test_path(path) or MIGRATION_PATH_RE.search(path):
            continue
        lines = text.split("\n")
        for fn in extract_functions(path, text) or []:
            code = "\n".join(lines[fn["start_line"] - 1:fn["end_line"]])
            functions.append({"file": path, "function": fn["name"], "code": code,
                              "start": fn["start_line"], "is_py": path.endswith(".py"), "lines": lines})
    defs = Counter(f["function"].split(".")[-1].split("#")[0] for f in functions)

    for f in functions:
        short = f["function"].split(".")[-1].split("#")[0]
        f["stub"] = python_stub(f["code"]) if f["is_py"] else ts_stub(f["code"])
        excluded = (short.startswith(("<", "test")) or (short.startswith("__") and short.endswith("__"))
                    or short in ENTRYPOINT_NAMES or len(short) < 3
                    or decorated_or_exported(f["lines"], f["start"], f["is_py"]))
        f["unreferenced"] = (not excluded) and words[short] <= defs[short]
        f["static_dead"] = f["stub"] or f["unreferenced"]
        del f["lines"]
    print(f"{len(functions)} production functions; stub={sum(f['stub'] for f in functions)} "
          f"unreferenced={sum(f['unreferenced'] for f in functions)}", flush=True)

    cache = json.load(open(args.cache)) if os.path.exists(args.cache) else {}
    key = lambda f: f"{args.rev}|{f['file']}|{f['function']}"  # noqa: E731
    todo = [f for f in functions if key(f) not in cache]
    t0 = time.time()

    def ask(f):
        body = json.dumps({"model": "jev-latest", "questions": QUESTIONS,
                           "state": {"file": f["file"], "function": f["function"], "code": f["code"][:MAX_CODE_CHARS]}}).encode()
        for attempt in range(6):
            req = urllib.request.Request(API_URL, data=body, method="POST", headers={
                "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                a = data["answers"]
                return {"no_real_work": a["no_real_work"]["noul"], "looks_obsolete": a["looks_obsolete"]["noul"],
                        "input_tokens": data.get("usage", {}).get("input_tokens")}
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                return {"error": f"HTTP {exc.code}"}
            except Exception:
                time.sleep(2 ** attempt)
        return {"error": "retries exhausted"}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(ask, f): f for f in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            cache[key(futs[fut])] = fut.result()
            if n % 2000 == 0:
                json.dump(cache, open(args.cache, "w"))
                print(f"  {n}/{len(todo)} ({time.time() - t0:.0f}s)", flush=True)
    json.dump(cache, open(args.cache, "w"))
    for f in functions:
        f.update(cache.get(key(f), {}))
    ok = [f for f in functions if "no_real_work" in f]
    tokens = sum(f.get("input_tokens") or 0 for f in ok)
    print(f"Jev done in {time.time() - t0:.0f}s; answered {len(ok)}/{len(functions)}; input tokens={tokens:,}", flush=True)

    def auc(score, label):
        pos = [f[score] for f in ok if f[label]]
        neg = sorted(f[score] for f in ok if not f[label])
        if not pos or not neg:
            return None
        tot = sum(bisect.bisect_left(neg, p) + (bisect.bisect_right(neg, p) - bisect.bisect_left(neg, p)) / 2 for p in pos)
        return round(tot / (len(pos) * len(neg)), 3)

    report = {"functions": len(functions), "answered": len(ok), "input_tokens": tokens,
              "labels": {lab: sum(f[lab] for f in ok) for lab in ("stub", "unreferenced", "static_dead")}}
    for score in ("no_real_work", "looks_obsolete"):
        for label in ("stub", "unreferenced", "static_dead"):
            report[f"auc_{score}_vs_{label}"] = auc(score, label)
        for t in (0.5, 0.7, 0.9):
            flagged = [f for f in ok if f[score] >= t]
            tp = sum(f["static_dead"] for f in flagged)
            report[f"{score}>={t}"] = {"flagged": len(flagged),
                                       "precision_vs_static": round(tp / len(flagged), 3) if flagged else None,
                                       "recall_of_stubs": round(sum(f["stub"] for f in flagged) / max(1, report["labels"]["stub"]), 3),
                                       "beyond_static": len(flagged) - tp}
    print(json.dumps(report, indent=1))
    if args.output:
        json.dump(report, open(args.output, "w"), indent=1)
    if args.candidates:
        beyond = sorted((f for f in ok if not f["static_dead"] and max(f["no_real_work"], f["looks_obsolete"]) >= 0.7),
                        key=lambda f: -max(f["no_real_work"], f["looks_obsolete"]))
        json.dump([{k: f[k] for k in ("file", "function", "no_real_work", "looks_obsolete")} for f in beyond],
                  open(args.candidates, "w"), indent=1)
        print(f"{len(beyond)} Jev-only candidates (>=0.7) written to {args.candidates}")


if __name__ == "__main__":
    main()

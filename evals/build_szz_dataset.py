#!/usr/bin/env python3
"""
Builds a ground-truth dataset of bug-introducing commits from a repository's history
(forward SZZ).

For every non-merge commit whose subject starts with `fix`, the production-code lines it
removed or modified are blamed on its parent. The commit that last touched most of those
lines is taken as the bug-introducing commit, and the functions (at the parent revision)
containing the lines blamed to it are the "fixed functions". A case is kept only when
those functions are among the production functions the introducing commit touched, so a
ranking of that commit's changes can be scored against it.

Usage:
    python3 evals/build_szz_dataset.py --repo /path/to/repo --output szz_dataset.json [--workers 8]
"""

import argparse
import contextlib
import io
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from diff_risk_sentinel.cli import run_sentinel  # noqa: E402
from diff_risk_sentinel.complexity import extract_functions, supported_extensions  # noqa: E402
from diff_risk_sentinel.diff import GitError, read_blobs, run_git  # noqa: E402
from diff_risk_sentinel.signals import MIGRATION_PATH_RE, is_test_path  # noqa: E402

HUNK_OLD_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? ")
FIX_RE = re.compile(r"^fix(\(|:|!)", re.I)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def production_removed_lines(repo: str, fix: str) -> Dict[str, List[int]]:
    out = run_git(["diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/",
                   "-U0", f"{fix}~1", fix], repo)
    exts = supported_extensions()
    result: Dict[str, List[int]] = {}
    path = None
    for line in out.split("\n"):
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("--- ") and path is None:
            p = line[4:].rstrip("\t")
            ok = p.startswith("a/") and p[2:].endswith(exts) and not is_test_path(p[2:]) \
                and not MIGRATION_PATH_RE.search(p[2:])
            path = p[2:] if ok else ""
        elif line.startswith("@@ ") and path:
            m = HUNK_OLD_RE.match(line)
            start, count = int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1
            result.setdefault(path, []).extend(range(start, start + count))
    return result


def analyze_fix(repo: str, fix: str, subject: str) -> Optional[Dict]:
    try:
        removed = production_removed_lines(repo, fix)
        if not removed:
            return None
        parent = run_git(["rev-parse", f"{fix}~1"], repo).strip()
        line_owner: Dict[str, Dict[int, str]] = {}
        votes: Counter = Counter()
        for path, lines in removed.items():
            args = ["blame", "--porcelain"]
            for ln in lines:
                args += ["-L", f"{ln},{ln}"]
            porcelain = run_git(args + [parent, "--", path], repo)
            owners = {}
            for row in porcelain.split("\n"):
                parts = row.split(" ")
                if len(parts) >= 3 and SHA_RE.match(parts[0]) and parts[2].isdigit():
                    owners[int(parts[2])] = parts[0]  # final line number in parent -> commit
            line_owner[path] = owners
            votes.update(owners.values())
        if not votes:
            return None
        intro, _ = votes.most_common(1)[0]
        blobs = read_blobs(repo, [(parent, p) for p in removed])
        fixed: Dict[str, List[str]] = {}
        for path, owners in line_owner.items():
            funcs = extract_functions(path, blobs.get((parent, path)) or "") or []
            names = set()
            for ln, sha in owners.items():
                if sha != intro:
                    continue
                inside = [f for f in funcs if f["start_line"] <= ln <= f["end_line"]]
                if inside:
                    names.add(min(inside, key=lambda f: f["end_line"] - f["start_line"])["name"])
            if names:
                fixed[path] = sorted(names)
        if not fixed:
            return None
        return {"fix": fix, "subject": subject, "intro": intro, "fixed_functions": fixed}
    except GitError:
        return None


def touched_production_functions(repo: str, intro: str) -> Optional[List[Dict]]:
    out = f"/tmp/_szz_{intro[:12]}.json"
    with contextlib.redirect_stdout(io.StringIO()):
        code = run_sentinel(base=f"{intro}~1..{intro}", repo=repo, coverage=[], output=out, top=10 ** 6,
                            top_consumers=0, threshold_crap=0, threshold_ccn=0, threshold_delta=0)
    if code != 0 or not os.path.exists(out):
        return None
    with open(out) as fh:
        data = json.load(fh)
    os.remove(out)
    return [{"file": t["file"], "function": t["function"]} for t in data["targets"] if not is_test_path(t["file"])]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    log = run_git(["log", "--all", "--no-merges", "--format=%H%x09%s"], args.repo)
    fixes = [line.split("\t", 1) for line in log.split("\n") if "\t" in line]
    fixes = [(sha, subj) for sha, subj in fixes if FIX_RE.match(subj)]
    print(f"{len(fixes)} fix commits")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        raw = [r for r in pool.map(lambda f: analyze_fix(args.repo, *f), fixes) if r]
    print(f"{len(raw)} fixes with blameable production lines")

    intros = sorted({r["intro"] for r in raw})
    # Sequential on purpose: run_sentinel prints, and redirect_stdout is process-wide.
    touched = {i: touched_production_functions(args.repo, i) for i in intros}

    cases, dropped = [], Counter()
    seen = set()
    for r in raw:
        t = touched.get(r["intro"])
        if not t:
            dropped["intro has no scorable production functions"] += 1
            continue
        names = {(x["file"], x["function"]) for x in t}
        present = {p: [f for f in fns if (p, f) in names] for p, fns in r["fixed_functions"].items()}
        present = {p: fns for p, fns in present.items() if fns}
        if not present:
            dropped["fixed function not touched by the introducing commit"] += 1
            continue
        key = (r["intro"], json.dumps(present, sort_keys=True))
        if key in seen:
            dropped["duplicate (same intro and fixed functions)"] += 1
            continue
        seen.add(key)
        cases.append({**r, "fixed_functions": present, "touched_production_functions": len(t)})

    with open(args.output, "w") as fh:
        json.dump({"repository": os.path.basename(os.path.abspath(args.repo)), "fix_commits": len(fixes),
                   "cases": cases, "dropped": dict(dropped)}, fh, indent=1, ensure_ascii=False)
    sizes = [c["touched_production_functions"] for c in cases]
    print(f"{len(cases)} cases from {len({c['intro'] for c in cases})} introducing commits; dropped: {dict(dropped)}")
    if sizes:
        sizes.sort()
        print(f"touched production functions per intro: median {sizes[len(sizes)//2]}, max {sizes[-1]}")


if __name__ == "__main__":
    main()

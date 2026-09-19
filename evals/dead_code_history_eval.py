#!/usr/bin/env python3
"""
Dead-code recall against the repository's own history ("deletion SZZ").

Ground truth: production functions deleted by commits whose message says the code was dead,
unused or orphaned (--grep). A deleted function is dropped when a function with the same
name is (re)defined in another file the commit changed (a move or rename). At the parent
revision each remaining function is classified by its production mentions:
  * direct     — nothing in production mentions it outside code the same commit deletes
                 wholesale (no mention at all, or only tests/homonym definitions);
  * transitive — mentioned only from functions or files the same commit deletes;
  * live       — mentioned by production code the commit keeps (a feature was retired or
                 a call site rewritten; not dead at the parent).
The scan (`deadcode.scan_repository` + the Jev review of `deadcode_evidence`) is run at the
parent and scored on those classes: `static` (static rule), `main` (static after the Jev
veto), `probable` (Jev tier) and `any` (main or probable). Only the deleted functions and
the parent's static findings are sent to Jev.

Usage:
    TYPESAFE_API_KEY=... python3 evals/dead_code_history_eval.py --repo /path/to/repo \\
        [--ref origin/main] [--grep REGEX] [--workers 32] [--output results.json]
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from diff_risk_sentinel.complexity import extract_functions, supported_extensions  # noqa: E402
from diff_risk_sentinel.deadcode import _entry_point, scan_repository  # noqa: E402
from diff_risk_sentinel.deadcode_evidence import (PROBABLE_FROM, VETO_BELOW, RepoIndex, evidence,  # noqa: E402
                                                  is_candidate, is_test_file, judge, short_name)
from diff_risk_sentinel.diff import read_blobs, run_git  # noqa: E402
from diff_risk_sentinel.signals import MIGRATION_PATH_RE  # noqa: E402

DEFAULT_GREP = (r"unused|dead[ -]?code|c[oó]digo morto|morto|[oó]rf[aã]|orphan|refer[eê]ncia zero|sem uso|"
                r"n[aã]o usad|never used|no longer used|ningu[eé]m (usa|adotou|chama)|nobody")
EXCLUDE_SUBJECT = r"^(release|promote|chore\(release\))"  # squashes of many unrelated PRs
SKIP_DIRS = ("node_modules/", "/dist/", "/build/", "/.venv/", "/vendor/", "/__pycache__/")


def production_code(path):
    return (path.endswith(supported_extensions()) and not path.endswith(".d.ts") and not is_test_file(path)
            and not MIGRATION_PATH_RE.search(path) and not any(s in f"/{path}" for s in SKIP_DIRS))


def deleted_functions(repo, commit):
    parent = f"{commit}~1"
    changes = [line.split("\t") for line in run_git(["diff", "--no-renames", "--name-status", parent, commit],
                                                   repo).split("\n") if line]
    changed = [c for c in changes if production_code(c[-1])]
    if not changed:
        return parent, [], set()
    deleted_files = {c[1] for c in changed if c[0] == "D"}
    blobs = read_blobs(repo, [(parent, c[-1]) for c in changed if c[0] != "A"]
                       + [(commit, c[-1]) for c in changed if c[0] != "D"])
    before = {c[-1]: extract_functions(c[-1], blobs.get((parent, c[-1])) or "") or [] for c in changed if c[0] != "A"}
    after = {c[-1]: extract_functions(c[-1], blobs.get((commit, c[-1])) or "") or [] for c in changed if c[0] != "D"}
    defined_after = defaultdict(set)
    for path, fns in after.items():
        for f in fns:
            defined_after[short_name(f["name"])].add(path)
    gone = []
    for path, fns in before.items():
        names_after = {f["name"] for f in after.get(path, [])}
        for f in fns:
            if f["name"] in names_after or "<anonymous>" in f["name"]:
                continue
            if defined_after[short_name(f["name"])] - {path}:
                continue  # moved or re-created elsewhere in the same commit
            gone.append((path, f))
    return parent, gone, deleted_files


def classify(ev, gone_ranges, deleted_files):
    live = False
    for r in ev["references"]:
        if r["test"] or r["defines_same_name"]:
            continue
        if r["file"] in deleted_files:
            continue
        if any(a <= r["line"] <= b for a, b in gone_ranges.get(r["file"], ())):
            continue
        live = True
    if live:
        return "live"
    prod = ev["reference_counts"]["production_other_files"] + ev["reference_counts"]["production_same_file"]
    return "transitive" if prod else "direct"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--grep", default=DEFAULT_GREP)
    ap.add_argument("--exclude-subject", default=EXCLUDE_SUBJECT)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--max-commits", type=int, default=0)
    ap.add_argument("--output")
    args = ap.parse_args()
    api_key = os.environ["TYPESAFE_API_KEY"]

    log = run_git(["log", args.ref, "--no-merges", "-i", "-E", f"--grep={args.grep}", "--format=%H%x09%s"], args.repo)
    skip = re.compile(args.exclude_subject, re.I)
    commits = [c.split("\t", 1)[0] for c in log.split("\n") if c and not skip.search(c.split("\t", 1)[1])]
    if args.max_commits:
        commits = commits[:args.max_commits]
    print(f"{len(commits)} commits match", flush=True)

    rows = []
    for n, commit in enumerate(commits, 1):
        parent, gone, deleted_files = deleted_functions(args.repo, commit)
        if not gone:
            continue
        index = RepoIndex(args.repo, parent)
        static = {(f["file"], f["function"]): f["status"] for f in scan_repository(args.repo, parent)}
        gone_ranges = defaultdict(list)
        for path, f in gone:
            gone_ranges[path].append((f["start_line"], f["end_line"]))
        items = []
        for path, f in gone:
            fn = index.find(path, f["name"])
            if fn is None:
                continue
            ev = evidence(index, path, fn)
            lines = index.lines[path]
            first = lines[fn["start_line"] - 1].strip()
            prev = lines[fn["start_line"] - 2].strip() if fn["start_line"] >= 2 else ""
            gated = not _entry_point(short_name(fn["name"]), first, prev, path.endswith(".py"), index.dynamic_prefixes)
            items.append({"commit": commit[:12], "file": path, "function": fn["name"],
                          "class": classify(ev, gone_ranges, deleted_files),
                          "static": (path, fn["name"]) in static,
                          "static_status": static.get((path, fn["name"])),
                          "judge": (path, fn["name"]) in static or (gated and is_candidate(ev)), "_ev": ev})
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            answers = list(pool.map(lambda it: judge(api_key, it["_ev"]) if it["judge"] else {}, items))
        for it, a in zip(items, answers):
            p = a.get("p_removable")
            it["jev_p"] = p
            it["main"] = it["static"] and not (p is not None and p < VETO_BELOW)
            it["probable"] = (not it["static"]) and p is not None and p >= PROBABLE_FROM
            del it["_ev"], it["judge"]
            rows.append(it)
        print(f"  [{n}/{len(commits)}] {commit[:10]}: {len(items)} deleted functions", flush=True)

    report = {"commits_matching": len(commits), "commits_with_deletions": len({r["commit"] for r in rows}),
              "deleted_functions": len(rows), "classes": dict(Counter(r["class"] for r in rows))}
    for cls in ("direct", "transitive", "live"):
        sub = [r for r in rows if r["class"] == cls]
        if sub:
            report[f"recall_{cls}"] = {
                k: f"{sum(r[k] for r in sub)}/{len(sub)}" for k in ("static", "main", "probable")}
            report[f"recall_{cls}"]["any"] = f"{sum(r['main'] or r['probable'] for r in sub)}/{len(sub)}"
    print(json.dumps(report, indent=1))
    if args.output:
        json.dump({"report": report, "rows": rows}, open(args.output, "w"), indent=1)


if __name__ == "__main__":
    main()

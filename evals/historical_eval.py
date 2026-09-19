#!/usr/bin/env python3
"""
Historical Evaluation Harness for Diff Risk Sentinel.

Replays labeled commits of a target repository (--dataset, see evals/dataset.example.json) through the sentinel,
using the same defaults as the CLI, and reports:

* Fix-commit flag rate: share of bug-fix commits whose diff gets a high-risk target.
  This is a weak proxy (it looks at the *fix*, after the fact) and is compared with a
  naive baseline that flags every commit touching code.
* SZZ bug-introduction hit rate (--szz): for each bug fix, `git blame` the lines the fix
  removed to find the commit that introduced them, replay that commit, and check whether
  the function later fixed is among the top-N targets. Compared with the hit rate of a
  random ranking of the touched methods (N / touched).
* Refactor ΔCRAP accuracy: share of refactor commits with negative combined ΔCRAP.
* False-positive rate on safe commits, split into docs-only (filtered by extension, a
  trivial pass) and code-bearing commits.

All rates come with Wilson 95% confidence intervals: with N≈25 they are wide.
"""

import argparse
import contextlib
import datetime
import io
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from diff_risk_sentinel import __version__  # noqa: E402
from diff_risk_sentinel.cli import run_sentinel  # noqa: E402
from diff_risk_sentinel.complexity import extract_functions, supported_extensions  # noqa: E402
from diff_risk_sentinel.diff import GitError, read_blobs, run_git  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STRICT_ACTIONS = {"CRITICAL_SEMANTIC_AUDIT", "HIGH_RISK_REFACTOR"}
LENIENT_ACTIONS = STRICT_ACTIONS | {"NEEDS_ATTENTION_TESTS", "SEMANTIC_REVIEW"}
BUG_CATEGORIES = {"CRITICAL_BUG", "EDGE_CASE_BUG"}


def wilson(successes: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    if n == 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half) * 100, 1), round(min(1.0, centre + half) * 100, 1)


def rate(successes: int, n: int) -> Dict[str, Any]:
    lo, hi = wilson(successes, n)
    return {
        "hits": successes,
        "n": n,
        "percent": round(successes / n * 100, 1) if n else None,
        "ci95": [lo, hi],
    }


def fmt(r: Dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a (0 cases)"
    return f"{r['percent']}% ({r['hits']}/{r['n']}, IC95% {r['ci95'][0]}–{r['ci95'][1]}%)"


def replay(repo: str, target: str, top: int, jev: bool, thresholds: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """Runs the sentinel quietly and returns its JSON payload (None on git errors)."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        out = fh.name
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = run_sentinel(
                base=target, output=out, top=top, jev=jev, repo=repo, coverage=[],
                threshold_crap=thresholds["crap"], threshold_ccn=int(thresholds["ccn"]),
                threshold_delta=thresholds["delta"],
            )
        if code != 0:
            return None
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        if os.path.exists(out):
            os.remove(out)


# --------------------------------------------------------------------------- SZZ

HUNK_OLD_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? ")


def removed_old_lines(repo: str, commit: str) -> Dict[str, List[int]]:
    """Old-side line numbers removed/modified by `commit`, per path (code files only)."""
    out = run_git(["diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/",
                   "-U0", f"{commit}~1", commit], repo)
    exts = supported_extensions()
    result: Dict[str, List[int]] = {}
    path = None
    for line in out.splitlines():
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("--- ") and path is None:
            p = line[4:].rstrip("\t")
            path = p[2:] if p.startswith("a/") and p[2:].endswith(exts) else ""
        elif line.startswith("@@ ") and path:
            m = HUNK_OLD_RE.match(line)
            start, count = int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1
            result.setdefault(path, []).extend(range(start, start + count))
    return result


def szz_introducing(repo: str, commit: str) -> Optional[Dict[str, Any]]:
    """
    SZZ-style lookup: blames the lines removed by the fix on the parent revision.
    Returns the most-blamed commit and the qualified names of the functions that were fixed.
    """
    removed = removed_old_lines(repo, commit)
    if not removed:
        return None  # fix only added lines: nothing to blame
    parent = run_git(["rev-parse", f"{commit}~1"], repo).strip()
    blamed: Counter = Counter()
    fixed_functions: Dict[str, set] = {}
    blobs = read_blobs(repo, [(parent, p) for p in removed])
    for path, lines in removed.items():
        funcs = extract_functions(path, blobs.get((parent, path)) or "")
        for ln in lines:
            owners = [f for f in funcs if f["start_line"] <= ln <= f["end_line"]]
            if owners:
                inner = min(owners, key=lambda f: f["end_line"] - f["start_line"])
                fixed_functions.setdefault(path, set()).add(inner["name"])
        porcelain = run_git(["blame", "--porcelain", *sum((["-L", f"{ln},{ln}"] for ln in lines), []),
                             parent, "--", path], repo)
        for row in porcelain.splitlines():
            sha = row.split(" ", 1)[0]
            if re.fullmatch(r"[0-9a-f]{40}", sha) and not sha.startswith("0" * 40):
                blamed[sha] += 1
    if not blamed or not fixed_functions:
        return None
    sha, _ = blamed.most_common(1)[0]
    return {"commit": sha, "fixed_functions": {p: sorted(n) for p, n in fixed_functions.items()}}


def random_hit_probability(touched: int, fixed: int, top: int) -> float:
    """Chance that a random ranking of `touched` methods puts at least one of `fixed` in the top N."""
    if touched <= 0:
        return 0.0
    fixed = max(1, min(fixed, touched))
    top = min(top, touched)
    return 1.0 - math.comb(touched - fixed, top) / math.comb(touched, top)


def szz_rank(payload: Dict[str, Any], fixed: Dict[str, List[str]]) -> Optional[int]:
    """1-based rank of the first fixed function among the ranked targets (None if absent)."""
    for idx, target in enumerate(payload.get("targets", []), 1):
        if target["function"] in fixed.get(target["file"], []):
            return idx
    return None


# --------------------------------------------------------------------------- main loop

def _case_row(tc: Dict[str, Any], payload: Dict[str, Any], elapsed: float) -> Dict[str, Any]:
    summary, meta = payload["otterwise_summary"], payload["meta"]
    actions = [t["action"] for t in payload["targets"]]
    return {
        "commit": tc["commit"],
        "title": tc["title"],
        "category": tc["category"],
        "code_files": meta["code_files_analyzed"],
        "top_action": actions[0] if actions else "FAST_PASS_NO_TARGETS",
        "actions": actions,
        "flag_strict": any(a in STRICT_ACTIONS for a in actions),
        "flag_lenient": any(a in LENIENT_ACTIONS for a in actions),
        "flag_naive": meta["code_files_analyzed"] > 0,
        "combined_delta_crap": summary["combined_delta_crap"],
        "average_delta_crap": summary["average_delta_crap"],
        "touched_methods": summary["total_methods"],
        "jev_failures": meta["jev_failures"],
        "elapsed_seconds": round(elapsed, 2),
    }


def _szz_row(repo_dir: str, commit: str, top: int, jev: bool, thresholds: Dict[str, float]) -> Optional[Dict[str, Any]]:
    try:
        intro = szz_introducing(repo_dir, commit)
    except GitError:
        return None
    if not intro:
        return None
    ranked = replay(repo_dir, f"{intro['commit']}~1..{intro['commit']}", 10 ** 6, jev, thresholds)
    if ranked is None:
        return None
    rank = szz_rank(ranked, intro["fixed_functions"])
    touched = ranked["otterwise_summary"]["total_methods"]
    return {
        "introducing_commit": intro["commit"][:12],
        "fixed_functions": intro["fixed_functions"],
        "rank": rank,
        "hit": rank is not None and rank <= top,
        "touched_methods": touched,
        "random_hit_probability": round(random_hit_probability(
            touched, sum(len(names) for names in intro["fixed_functions"].values()), top), 3),
    }


def _scorecard(results: List[Dict[str, Any]], szz: bool, total_time: float) -> Dict[str, Any]:
    ok = [r for r in results if not r.get("error")]
    bugs = [r for r in ok if r["category"] in BUG_CATEGORIES]
    refactors = [r for r in ok if r["category"] == "BENEFICIAL_REFACTOR"]
    safe = [r for r in ok if r["category"] == "SAFE_FAST_PASS"]
    safe_code = [r for r in safe if r["code_files"] > 0]

    scorecard = {
        "fix_commit_flag_rate_strict": rate(sum(r["flag_strict"] for r in bugs), len(bugs)),
        "fix_commit_flag_rate_lenient": rate(sum(r["flag_lenient"] for r in bugs), len(bugs)),
        "fix_commit_flag_rate_naive_baseline": rate(sum(r["flag_naive"] for r in bugs), len(bugs)),
        "refactor_negative_delta_crap": rate(sum(r["combined_delta_crap"] < 0 for r in refactors), len(refactors)),
        "safe_false_positive_strict": rate(sum(r["flag_strict"] for r in safe), len(safe)),
        "safe_false_positive_lenient": rate(sum(r["flag_lenient"] for r in safe), len(safe)),
        "safe_docs_only_cases": len(safe) - len(safe_code),
        "safe_code_bearing_false_positive_lenient": rate(sum(r["flag_lenient"] for r in safe_code), len(safe_code)),
        "skipped_cases": len(results) - len(ok),
        "total_time_seconds": round(total_time, 1),
        "average_time_per_commit": round(total_time / max(len(results), 1), 2),
    }
    if szz:
        szz_rows = [r["szz"] for r in bugs if "szz" in r]
        expected = sum(s["random_hit_probability"] for s in szz_rows)
        scorecard["szz_bug_introduction_hit_rate"] = rate(sum(s["hit"] for s in szz_rows), len(szz_rows))
        scorecard["szz_random_ranking_expected_percent"] = round(expected / len(szz_rows) * 100, 1) if szz_rows else None
        scorecard["szz_unavailable_cases"] = len(bugs) - len(szz_rows)
    return scorecard


def _has_uncommitted_src(root: str) -> Optional[bool]:
    try:
        return bool(run_git(["status", "--porcelain", "--", "src"], root).strip())
    except GitError:
        return None  # not a git checkout (e.g. installed from an archive)


def _git_head(path: str) -> Optional[str]:
    try:
        return run_git(["rev-parse", "HEAD"], path).strip()
    except GitError:
        return None


def _print_scorecard(scorecard: Dict[str, Any], top: int, szz: bool):
    print("\n" + "=" * 80)
    print("📊 EVALUATION SCORECARD")
    print("=" * 80)
    print(f"Fix commits flagged (strict):   {fmt(scorecard['fix_commit_flag_rate_strict'])}")
    print(f"Fix commits flagged (lenient):  {fmt(scorecard['fix_commit_flag_rate_lenient'])}")
    print(f"  naive baseline (touches code): {fmt(scorecard['fix_commit_flag_rate_naive_baseline'])}")
    if szz:
        print(f"SZZ: fixed function in top-{top} when introduced: {fmt(scorecard['szz_bug_introduction_hit_rate'])}")
        print(f"  random ranking would hit:     {scorecard['szz_random_ranking_expected_percent']}% "
              f"({scorecard['szz_unavailable_cases']} bug case(s) without blameable lines)")
    print(f"Refactors with ΔCRAP < 0:       {fmt(scorecard['refactor_negative_delta_crap'])}")
    print(f"Safe commits flagged (strict):  {fmt(scorecard['safe_false_positive_strict'])}")
    print(f"Safe commits flagged (lenient): {fmt(scorecard['safe_false_positive_lenient'])}")
    print(f"  of which code-bearing:         {fmt(scorecard['safe_code_bearing_false_positive_lenient'])} "
          f"({scorecard['safe_docs_only_cases']} docs-only case(s) pass trivially)")
    print(f"Total time: {scorecard['total_time_seconds']}s (avg {scorecard['average_time_per_commit']}s/commit)")
    print("=" * 80)


def run_eval(repo_dir: str, dataset_path: str, output_file: str, top: int, jev: bool,
             thresholds: Dict[str, float], szz: bool) -> Dict[str, Any]:
    with open(dataset_path, encoding="utf-8") as fh:
        cases = json.load(fh)["cases"]

    print("=" * 80)
    print("🔬 DIFF RISK SENTINEL — HISTORICAL EVALUATION HARNESS")
    print(f"Target Repository: {repo_dir}")
    print(f"Cases: {len(cases)} | top={top} | jev={'on' if jev else 'off'} | thresholds={thresholds} | szz={szz}")
    print("=" * 80)

    results = []
    start_total = time.time()
    for idx, tc in enumerate(cases, 1):
        commit, category = tc["commit"], tc["category"]
        t0 = time.time()
        payload = replay(repo_dir, f"{commit}~1..{commit}", top, jev, thresholds)
        elapsed = time.time() - t0
        if payload is None:
            print(f"[{idx}/{len(cases)}] {commit}: ⚠️  git error, case skipped")
            results.append({"commit": commit, "category": category, "error": True})
            continue

        row = _case_row(tc, payload, elapsed)
        szz_note = ""
        if szz and category in BUG_CATEGORIES:
            szz_info = _szz_row(repo_dir, commit, top, jev, thresholds)
            if szz_info:
                row["szz"] = szz_info
                szz_note = f" | SZZ {szz_info['introducing_commit'][:9]} rank={szz_info['rank']} of {szz_info['touched_methods']}"
        results.append(row)
        print(f"[{idx}/{len(cases)}] {commit} {category:19} strict={'Y' if row['flag_strict'] else 'n'} "
              f"lenient={'Y' if row['flag_lenient'] else 'n'} top={row['top_action']} "
              f"ΔCRAP={row['combined_delta_crap']:+}{szz_note} ({elapsed:.1f}s)")

    scorecard = _scorecard(results, szz, time.time() - start_total)
    root = os.path.dirname(HERE)
    config = {
        "sentinel_version": __version__,
        "sentinel_commit": _git_head(root),
        "sentinel_uncommitted_changes": _has_uncommitted_src(root),
        "target_repo_head": _git_head(repo_dir),
        "dataset": os.path.relpath(dataset_path, root),
        "top": top,
        "jev_enabled": jev,
        "thresholds": thresholds,
        "szz": szz,
        "run_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    _print_scorecard(scorecard, top, szz)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump({"config": config, "scorecard": scorecard, "details": results}, f, indent=2, ensure_ascii=False)
    print(f"📁 Detailed report saved to '{output_file}'.")
    return scorecard


def main():
    parser = argparse.ArgumentParser(description="Run historical evals for Diff Risk Sentinel")
    parser.add_argument("--repo", required=True, help="Target git repository containing the dataset commits")
    parser.add_argument("--dataset", required=True, help="Labeled cases (JSON; format: evals/dataset.example.json)")
    parser.add_argument("--output", default="eval_results.json", help="Output JSON results")
    parser.add_argument("--top", type=int, default=5, help="Top-N targets considered (CLI default: 5)")
    parser.add_argument("--jev", action="store_true", help="Enable TypeSafe Jev (sends snippets to the API)")
    parser.add_argument("--szz", action="store_true", help="Also evaluate the bug-introducing commits (SZZ)")
    parser.add_argument("--threshold-crap", type=float, default=15.0)
    parser.add_argument("--threshold-ccn", type=int, default=10)
    parser.add_argument("--threshold-delta", type=float, default=10.0)
    args = parser.parse_args()

    if args.jev and not os.environ.get("TYPESAFE_API_KEY"):
        parser.error("--jev requires TYPESAFE_API_KEY")

    run_eval(
        repo_dir=args.repo,
        dataset_path=args.dataset,
        output_file=args.output,
        top=args.top,
        jev=args.jev,
        thresholds={"crap": args.threshold_crap, "ccn": args.threshold_ccn, "delta": args.threshold_delta},
        szz=args.szz,
    )


if __name__ == "__main__":
    main()

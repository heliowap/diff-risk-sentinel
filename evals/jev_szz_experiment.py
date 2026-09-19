#!/usr/bin/env python3
"""
Pre-registered comparison of rankings on a forward-SZZ dataset (build_szz_dataset.py).

Rankings: CRAP, each Jev question on its own, and CRAP + all Jev questions (rank average).
Baseline: a random ordering of the introducing commit's touched production functions.
Metrics, per case (introducing commit + functions a later fix changed):
  * best percentile of a fixed function in the ranking (tie-aware; 1.0 = first);
  * hit@K for K in 5, 8, 16.
Uncertainty: 95% bootstrap intervals resampling introducing commits (cases of the same
commit are correlated). Cases from ranges used in earlier exploration are reported apart.

Usage:
    TYPESAFE_API_KEY=... python3 evals/jev_szz_experiment.py --repo /path/to/repo \\
        --dataset szz_dataset.json [--explored sha1,sha2] [--cache /tmp/jev_szz_cache.json] [--workers 16] [--output results.json]
"""

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import historical_eval as h  # noqa: E402
from jev_eval_common import MAX_DIFF_CHARS, jev, touched_functions  # noqa: E402

JEV_KEYS = ("introduces_bug", "edge_cases", "behavior_change", "semantic_risk")
KS = (5, 8, 16)


def percentiles(items, scores):
    """Tie-aware percentile (0..1) of every item under `scores` (higher score = earlier)."""
    n = len(items)
    order = sorted(range(n), key=lambda i: scores[i])
    pct = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        value = ((i + j) / 2) / (n - 1) if n > 1 else 1.0
        for k in range(i, j + 1):
            pct[order[k]] = value
        i = j + 1
    return pct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--cache", default="/tmp/jev_szz_cache.json")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--output")
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--explored", default="",
                    help="Comma-separated introducing-commit prefixes inspected while designing the "
                         "questions; they are excluded from the held-out comparison")
    args = ap.parse_args()
    explored = tuple(p for p in args.explored.split(",") if p)
    api_key = os.environ["TYPESAFE_API_KEY"]

    cases = json.load(open(args.dataset))["cases"]
    intros = sorted({c["intro"] for c in cases})
    t0 = time.time()
    functions = {i: touched_functions(args.repo, f"{i}~1..{i}") for i in intros}
    print(f"{len(cases)} cases, {len(intros)} intros, {sum(len(v) for v in functions.values())} functions "
          f"(collected in {time.time() - t0:.0f}s)", flush=True)

    cache = json.load(open(args.cache)) if os.path.exists(args.cache) else {}
    key = lambda i, t: f"{i}|{t['file']}|{t['function']}"  # noqa: E731
    todo = [(i, t) for i, items in functions.items() for t in items if key(i, t) not in cache]
    print(f"{len(todo)} Jev queries to run", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(jev, api_key, {"file": t["file"], "function": t["function"], "old_code": t["old_code"],
                                           "new_code": t["new_code"], "diff": t["diff_snippet"][:MAX_DIFF_CHARS]}): (i, t)
                for i, t in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            i, t = futs[fut]
            cache[key(i, t)] = fut.result()
            if n % 1000 == 0:
                json.dump(cache, open(args.cache, "w"))
                print(f"  {n}/{len(todo)} ({time.time() - t0:.0f}s)", flush=True)
    json.dump(cache, open(args.cache, "w"))
    answers = {i: [cache.get(key(i, t), {}) for t in functions[i]] for i in intros}
    errors = sum(1 for v in answers.values() for a in v if "error" in a or not a)
    tokens = sum(a.get("input_tokens") or 0 for v in answers.values() for a in v)
    print(f"Jev done in {time.time() - t0:.0f}s; errors={errors}; input tokens={tokens:,}", flush=True)

    # --- per-intro percentiles for every ranking --------------------------------------
    rankings = ["crap", *JEV_KEYS, "crap+jev"]
    pct = {}
    for i in intros:
        items, ans = functions[i], answers[i]
        base = {"crap": [t["composite_risk"] for t in items]}
        for k in JEV_KEYS:
            base[k] = [a.get(k, 0.0) for a in ans]
        per = {name: percentiles(items, base[name]) for name in ["crap", *JEV_KEYS]}
        per["crap+jev"] = percentiles(items, [sum(per[n][j] for n in ["crap", *JEV_KEYS]) for j in range(len(items))])
        pct[i] = per

    rows = []
    for c in cases:
        i = c["intro"]
        items = functions[i]
        idx = [j for j, t in enumerate(items) if t["function"] in c["fixed_functions"].get(t["file"], [])]
        if not idx:
            continue
        n, k = len(items), len(idx)
        row = {"intro": i, "n": n, "k": k, "explored": i.startswith(explored),
               "bucket": "<30" if n < 30 else "30-150" if n < 150 else ">=150",
               "random_pct": k / (k + 1)}
        for K in KS:
            row[f"random_hit@{K}"] = h.random_hit_probability(n, k, K)
        for name in rankings:
            best = max(pct[i][name][j] for j in idx)
            row[f"{name}_pct"] = best
            for K in KS:
                # rank position of the best fixed function (1-based, ties broken pessimistically)
                row[f"{name}_hit@{K}"] = best >= 1 - (K - 1) / (n - 1) if n > 1 else True
        rows.append(row)

    def summarize(sub):
        out = {"cases": len(sub), "intros": len({r["intro"] for r in sub})}
        for m in ["pct", *[f"hit@{K}" for K in KS]]:
            out[f"random_{m}"] = round(sum(r[f"random_{m}"] for r in sub) / len(sub), 3)
            for name in rankings:
                out[f"{name}_{m}"] = round(sum(r[f"{name}_{m}"] for r in sub) / len(sub), 3)
        return out

    def bootstrap(sub, a, b, metric="pct"):
        by_intro = {}
        for r in sub:
            by_intro.setdefault(r["intro"], []).append(r)
        groups = list(by_intro.values())
        rng = random.Random(7)
        diffs = []
        for _ in range(args.bootstrap):
            sample = [r for g in (rng.choice(groups) for _ in groups) for r in g]
            va = sum(r[f"{a}_{metric}"] for r in sample) / len(sample)
            vb = sum(r[f"{b}_{metric}"] for r in sample) / len(sample)
            diffs.append(va - vb)
        diffs.sort()
        return round(diffs[int(0.025 * len(diffs))], 3), round(diffs[int(0.975 * len(diffs))], 3)

    held = [r for r in rows if not r["explored"]]
    report = {"errors": errors, "input_tokens": tokens, "all": summarize(rows), "held_out": summarize(held),
              "by_size": {b: summarize([r for r in held if r["bucket"] == b]) for b in ("<30", "30-150", ">=150")},
              "ci_vs_random_pct": {n: bootstrap(held, n, "random") for n in rankings},
              "ci_vs_crap_pct": {n: bootstrap(held, n, "crap") for n in rankings if n != "crap"},
              "ci_vs_crap_hit@8": {n: bootstrap(held, n, "crap", "hit@8") for n in rankings if n != "crap"}}

    def line(label, s):
        cols = ["random", *rankings]
        return (f"{label:18} n={s['cases']:3} | pct " + " ".join(f"{c}={s[f'{c}_pct']:.2f}" for c in cols)
                + "\n" + " " * 26 + "hit@8 " + " ".join(f"{c}={s[f'{c}_hit@8']:.2f}" for c in cols))
    print()
    print(line("held-out (all)", report["held_out"]))
    for b, s in report["by_size"].items():
        if s["cases"]:
            print(line(f"held-out {b}", s))
    print("\n95% CI of mean-percentile difference vs random:", report["ci_vs_random_pct"])
    print("95% CI of mean-percentile difference vs CRAP:  ", report["ci_vs_crap_pct"])
    print("95% CI of hit@8 difference vs CRAP:            ", report["ci_vs_crap_hit@8"])
    if args.output:
        json.dump({"report": report, "rows": rows}, open(args.output, "w"), indent=1)


if __name__ == "__main__":
    main()

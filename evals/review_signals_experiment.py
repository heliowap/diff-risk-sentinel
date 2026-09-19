#!/usr/bin/env python3
"""
Do atomic facts about a change, and narrow bug-pattern checks, point at the functions that
later needed a fix better than the current CRAP + Jev ranking?

For every production function an introducing commit touched (forward-SZZ dataset from
build_szz_dataset.py), TypeSafe Jev answers, over the function's diff and new code:
  * one Choice — what kind of change this is;
  * Nouls about observable facts of the change (condition, default, validation, persisted
    format, error handling, concurrency, external contract);
  * Nouls for three bug patterns seen in real merged defects (falsy-but-valid value, initial
    state equal to a legitimate value, validation that no longer holds after a transform).
Each signal becomes a ranking of the commit's functions; rankings are compared with CRAP,
the current CRAP + Jev triage and random on the "best percentile of a later-fixed function"
and hit@K. Cases are split by introducing commit into dev (question analysis, composition
choice) and test (evaluated once with the compositions fixed on dev).

Usage:
    TYPESAFE_API_KEY=... python3 evals/review_signals_experiment.py --repo /path/to/repo \\
        --dataset szz_dataset.json --baseline-cache jev_szz_cache.json --split dev \\
        [--cache review_signals_cache.json] [--compose "crap+jev,patterns"] [--workers 32]
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import historical_eval as h  # noqa: E402
from jev_eval_common import MAX_DIFF_CHARS, touched_functions  # noqa: E402
from jev_szz_experiment import JEV_KEYS, percentiles  # noqa: E402
from diff_risk_sentinel.jev import ask_jev  # noqa: E402

CONTEXT = ("`diff` shows the lines changed inside one function (`-` removed, `+` added); `new_code` is the "
           "function after the change. Answer about the change, not about the rest of the function.")


def _noul(question, true, false):
    return {"type": "noul", "instructions": f"{CONTEXT} {question}", "criteria": {"true": true, "false": false}}


QUESTIONS = {
    "change_type": {
        "type": "choice",
        "instructions": f"{CONTEXT} What kind of change is this, mainly?",
        "criteria": {
            "refactor": "Restructures code without changing what it does (rename, extract, move, simplify)",
            "feature": "Adds new behavior or a new case",
            "fix": "Corrects existing behavior that was wrong",
            "config": "Changes constants, settings, flags, limits or wiring only",
            "observability": "Logging, metrics, tracing or messages only",
            "cosmetic": "Formatting, comments, types or naming only",
        },
    },
    "fact_condition": _noul("Does the change add, remove or alter a condition, comparison or branch?",
                            "A condition, comparison or branch is added, removed or changed.",
                            "No condition or branch changes."),
    "fact_default": _noul("Does the change alter a default value, an initial state, a sentinel or a fallback?",
                          "A default, initial state, sentinel or fallback value changes.",
                          "No default, initial or fallback value changes."),
    "fact_validation": _noul("Does the change add, remove or alter validation, filtering or normalization of inputs?",
                             "Validation, filtering or normalization of inputs changes.",
                             "Input validation, filtering and normalization are untouched."),
    "fact_format": _noul("Does the change alter the shape or format of data that is stored, sent or read elsewhere "
                         "(keys, identifiers, columns, payload fields, date or unit formats)?",
                         "The format of stored, sent or shared data changes.",
                         "No shared or persisted data format changes."),
    "fact_errors": _noul("Does the change alter how errors, exceptions, timeouts or failures are handled?",
                         "Error or failure handling changes.", "Error handling is unchanged."),
    "fact_concurrency": _noul("Does the change touch transactions, locks, ordering of asynchronous work, retries or "
                              "idempotency?", "It touches transactions, locking, async ordering, retries or idempotency.",
                              "None of these are involved."),
    "fact_contract": _noul("Does the change alter a function signature, a return shape, an API route or an event "
                           "that other code depends on?", "An interface other code depends on changes.",
                           "No interface other code depends on changes."),
    "pattern_falsy": _noul("In the changed lines, can a falsy but valid value (0, empty string, None/null/undefined, "
                           "empty list, False) take the wrong branch, be skipped or be dropped?",
                           "A realistic falsy-but-valid value is mishandled by the changed lines.",
                           "Falsy values are handled correctly or cannot occur."),
    "pattern_initial_state": _noul("Does the changed code compare against or store an initial or sentinel value "
                                   "(null, 0, empty, -1) that a legitimate value could also equal, so the two "
                                   "cases are confused?",
                                   "An initial or sentinel value can collide with a legitimate value.",
                                   "No such collision is possible."),
    "pattern_validation_drift": _noul("Is a value or collection checked or counted and then transformed, filtered "
                                      "or re-read so that the check no longer holds for what is actually used?",
                                      "A check is made on data that is then changed before use.",
                                      "Checks apply to the data actually used."),
}
FACTS = [k for k in QUESTIONS if k.startswith("fact_")]
PATTERNS = [k for k in QUESTIONS if k.startswith("pattern_")]
SPEC_HASH = hashlib.sha256(json.dumps([CONTEXT, QUESTIONS], sort_keys=True).encode()).hexdigest()[:12]
KS = (5, 8, 16)


def split_of(intro):
    return "dev" if int(hashlib.md5(intro.encode()).hexdigest(), 16) % 2 == 0 else "test"


def ask(api_key, t):
    state = {"file": t["file"], "function": t["function"], "diff": t["diff_snippet"][:MAX_DIFF_CHARS],
             "new_code": t["new_code"][:12000]}
    data = ask_jev(api_key, state, QUESTIONS)
    if "error" in data:
        return data
    a = data["answers"]
    out = {k: float(a[k]["noul"]) for k in QUESTIONS if k != "change_type"}
    out["change_type"] = a["change_type"]["choice"]
    out["change_type_probs"] = a["change_type"].get("probabilities", {})
    return out


def signals(t, base, rev):
    """Every candidate ranking signal of one function (higher = read earlier)."""
    s = {"crap": t["composite_risk"]}
    for k in JEV_KEYS:
        s[k] = base.get(k, 0.0)
    for k in FACTS + PATTERNS:
        s[k] = rev.get(k, 0.0)
    s["facts_sum"] = sum(rev.get(k, 0.0) for k in FACTS)
    s["patterns_max"] = max(rev.get(k, 0.0) for k in PATTERNS) if rev else 0.0
    probs = rev.get("change_type_probs") or {}
    s["not_cosmetic"] = 1.0 - sum(probs.get(k, 0.0) for k in ("cosmetic", "observability", "refactor"))
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--baseline-cache", required=True, help="Cache of jev_szz_experiment.py (current triage answers)")
    ap.add_argument("--cache", default="review_signals_cache.json")
    ap.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    ap.add_argument("--compose", default="",
                    help="Comma-separated rank-average compositions, each '+'-joined signal names, "
                         "e.g. 'crap+jev,crap+jev+patterns_max' ('jev' expands to the four triage answers)")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--bootstrap", type=int, default=2000)
    args = ap.parse_args()
    api_key = os.environ["TYPESAFE_API_KEY"]

    cases = [c for c in json.load(open(args.dataset))["cases"] if args.split == "all" or split_of(c["intro"]) == args.split]
    intros = sorted({c["intro"] for c in cases})
    t0 = time.time()
    functions = {i: touched_functions(args.repo, f"{i}~1..{i}") for i in intros}
    print(f"split={args.split}: {len(cases)} cases, {len(intros)} intros, "
          f"{sum(len(v) for v in functions.values())} functions ({time.time() - t0:.0f}s)", flush=True)

    base_cache = json.load(open(args.baseline_cache))
    cache = json.load(open(args.cache)) if os.path.exists(args.cache) else {}
    key = lambda i, t: f"{SPEC_HASH}|{i}|{t['file']}|{t['function']}"  # noqa: E731
    todo = [(i, t) for i, items in functions.items() for t in items if key(i, t) not in cache]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for (i, t), ans in zip(todo, pool.map(lambda it: ask(api_key, it[1]), todo)):
            if "error" not in ans:
                cache[key(i, t)] = ans
    json.dump(cache, open(args.cache, "w"))
    missing = sum(1 for i, items in functions.items() for t in items if key(i, t) not in cache)
    print(f"Jev: {len(todo)} queried in {time.time() - t0:.0f}s, {missing} missing", flush=True)

    compositions = {}
    for spec in [x for x in args.compose.split(",") if x]:
        names = []
        for part in spec.split("+"):
            names += list(JEV_KEYS) if part == "jev" else [part]
        compositions[spec] = names
    single = list(signals({"composite_risk": 0}, {}, {"change_type_probs": {}}).keys())

    rows = []
    for c in cases:
        i = c["intro"]
        items = functions[i]
        idx = [j for j, t in enumerate(items) if t["function"] in c["fixed_functions"].get(t["file"], [])]
        if not idx:
            continue
        sig = [signals(t, base_cache.get(f"{i}|{t['file']}|{t['function']}", {}), cache.get(key(i, t), {})) for t in items]
        pct = {name: percentiles(items, [s[name] for s in sig]) for name in single}
        for spec, names in compositions.items():
            pct[spec] = percentiles(items, [sum(pct[n][j] for n in names) for j in range(len(items))])
        n, k = len(items), len(idx)
        row = {"intro": i, "n": n, "random_pct": k / (k + 1), **{f"random_hit@{K}": h.random_hit_probability(n, k, K) for K in KS}}
        for name in pct:
            best = max(pct[name][j] for j in idx)
            row[f"{name}_pct"] = best
            for K in KS:
                row[f"{name}_hit@{K}"] = best >= 1 - (K - 1) / (n - 1) if n > 1 else True
        rows.append(row)

    def mean(name, m):
        return sum(r[f"{name}_{m}"] for r in rows) / len(rows)

    def ci(a, b, m="pct"):
        groups = {}
        for r in rows:
            groups.setdefault(r["intro"], []).append(r)
        groups = list(groups.values())
        rng, diffs = random.Random(7), []
        for _ in range(args.bootstrap):
            sample = [r for g in (rng.choice(groups) for _ in groups) for r in g]
            diffs.append(sum(r[f"{a}_{m}"] - r[f"{b}_{m}"] for r in sample) / len(sample))
        diffs.sort()
        return diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs))]

    print(f"\n{len(rows)} scored cases; best percentile of a later-fixed function (higher = found earlier)")
    print(f"{'ranking':34} {'pct':>6} {'hit@5':>6} {'hit@8':>6} {'hit@16':>7}")
    names = ["random", *single, *compositions]
    for name in names:
        print(f"{name:34} {mean(name, 'pct'):6.3f} {mean(name, 'hit@5'):6.2f} {mean(name, 'hit@8'):6.2f} {mean(name, 'hit@16'):7.2f}")
    ref = "crap+jev" if "crap+jev" in compositions else "crap"
    for spec in compositions:
        if spec != ref:
            lo, hi = ci(spec, ref)
            lo8, hi8 = ci(spec, ref, "hit@8")
            print(f"95% CI {spec} − {ref}: pct {lo:+.3f}…{hi:+.3f}; hit@8 {lo8:+.3f}…{hi8:+.3f}")


if __name__ == "__main__":
    main()

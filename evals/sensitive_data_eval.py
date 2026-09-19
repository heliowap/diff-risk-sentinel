#!/usr/bin/env python3
"""
Can TypeSafe Jev tell publishable text from personal data or proprietary material?

Input: a JSONL of labeled chunks — {"text", "label" (1 = must not be published), "category",
"path" (a neutral path shown to the judge), "split" ("dev" | "test")}. Keep such datasets in
evals/private/ (gitignored): they are made of the material they test for.

Compares, per category and split, the regex checker (scripts/public_safety_check.py, with and
without the local private-repository index) and the Jev judge (sensitive_judge.JUDGE_QUESTIONS).

Usage:
    TYPESAFE_API_KEY=... python3 evals/sensitive_data_eval.py --data evals/private/sensitive_dataset.jsonl \\
        --split dev [--cache evals/private/sensitive_cache.json] [--workers 16]
"""

import argparse
import importlib.util
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
_spec = importlib.util.spec_from_file_location("psc", os.path.join(ROOT, "scripts", "public_safety_check.py"))
psc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(psc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    ap.add_argument("--cache", default=os.path.join(HERE, "private", "sensitive_cache.json"))
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--thresholds", default="0.5,0.7,0.9")
    args = ap.parse_args()
    items = [json.loads(x) for x in open(args.data) if x.strip()]
    items = [i for i in items if args.split == "all" or i["split"] == args.split]

    policy = psc.Policy.load(ROOT)
    private = psc.PrivateIndex.from_config(ROOT)
    for it in items:
        it["regex"] = bool(psc.scan_text(it["path"], it["text"], policy))
        it["regex_local"] = it["regex"] or bool(private and psc.scan_text(it["path"], it["text"], psc.Policy(), private))

    judge = psc.sensitive_judge
    cache = json.load(open(args.cache)) if os.path.exists(args.cache) else {}
    key_of = lambda it: judge.cache_key(it["path"], it["text"])  # noqa: E731
    todo = [it for it in items if key_of(it) not in cache]
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if todo and api_key:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for it, ans in zip(todo, pool.map(lambda it: judge.judge(api_key, it["path"], it["text"]), todo)):
                if "error" not in ans:
                    cache[key_of(it)] = ans
        os.makedirs(os.path.dirname(args.cache), exist_ok=True)
        json.dump(cache, open(args.cache, "w"))
    for it in items:
        it["jev"] = cache.get(key_of(it))

    ts = [float(t) for t in args.thresholds.split(",")]
    by_cat = defaultdict(list)
    for it in items:
        by_cat[it["category"]].append(it)
    header = f"{'category':22} {'n':>4} {'regex':>6} {'+local':>7} " + " ".join(f"{'jev≥' + str(t):>8}" for t in ts) + f" {'q_pii':>6} {'q_priv':>6}"
    print(f"split={args.split}  (share flagged; for label-0 categories lower is better)\n{header}")
    for cat in sorted(by_cat):
        rows = [r for r in by_cat[cat] if r["jev"]]
        if not rows:
            continue
        rate = lambda f: sum(1 for r in rows if f(r)) / len(rows)  # noqa: E731
        cells = " ".join(f"{rate(lambda r, t=t: r['jev']['p_sensitive'] >= t):8.0%}" for t in ts)
        print(f"{cat:22} {len(rows):4} {rate(lambda r: r['regex']):6.0%} {rate(lambda r: r['regex_local']):7.0%} {cells} "
              f"{sum(r['jev']['p_personal_data'] for r in rows) / len(rows):6.2f} "
              f"{sum(r['jev'].get('p_private_project') or 0 for r in rows) / len(rows):6.2f}")
    judged = [r for r in items if r["jev"]]
    pos = [r for r in judged if r["label"] == 1]
    print(f"\n{'rule':24} {'precision':>9} {'recall':>7} {'flagged':>8}")
    rules = [("regex", lambda r: r["regex"]), ("regex + local index", lambda r: r["regex_local"])]
    rules += [(f"jev ≥ {t}", lambda r, t=t: r["jev"]["p_sensitive"] >= t) for t in ts]
    rules += [(f"regex+local or jev ≥ {t}", lambda r, t=t: r["regex_local"] or r["jev"]["p_sensitive"] >= t) for t in ts]
    rules.append(("DEPLOYED rule", lambda r: r["regex_local"] or judge.blocks(r["jev"])))
    for q in ("p_personal_data", "p_private_project"):
        rules += [(f"{q[2:]} ≥ {t}", lambda r, t=t, q=q: (r["jev"].get(q) or 0) >= t) for t in ts]
    for name, f in rules:
        flagged = [r for r in judged if f(r)]
        tp = sum(r["label"] for r in flagged)
        print(f"{name:24} {tp / max(1, len(flagged)):9.0%} {tp / max(1, len(pos)):7.0%} {len(flagged):8}")
    print(f"\njudged {len(judged)}/{len(items)}")


if __name__ == "__main__":
    main()

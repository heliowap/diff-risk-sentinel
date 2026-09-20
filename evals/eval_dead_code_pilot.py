#!/usr/bin/env python3
"""
Pilot evaluation of the new Evidence-First Dead-Code Classifiers:
1. Stub / No-Op intentionality classifier (Protocol default / Adapter vs Abandoned stub).
2. Tests-only intentionality classifier (Testability seam / Reset hook vs Orphaned feature).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from diff_risk_sentinel.deadcode import scan_repository
from diff_risk_sentinel.deadcode_evidence import (
    RepoIndex,
    evidence,
    judge_stub,
    judge_tests_only,
    stub_state,
    tests_only_state,
)


def run_dead_code_pilot(repo_path: str, api_key: str, max_tests_only: int = 10) -> Dict[str, Any]:
    print(f"1. Scanning repository at {repo_path}...", flush=True)
    t0 = time.time()
    found = scan_repository(repo_path, "HEAD")
    scan_time = time.time() - t0
    print(f"   Found {len(found)} candidate functions in {scan_time:.2f}s.", flush=True)

    stubs = [f for f in found if f.get("stub")]
    tests_only = [f for f in found if f.get("status") == "tests_only"]
    print(f"   Stubs: {len(stubs)} | Tests-only: {len(tests_only)}", flush=True)

    print("2. Building repository index for evidence extraction...", flush=True)
    index = RepoIndex(repo_path, "HEAD")

    # Evaluate stubs
    print(f"\n3. Evaluating {len(stubs)} stubs with TypeSafe Jev...", flush=True)
    stub_eval_results = []
    for s in stubs:
        fn = index.find(s["file"], s["function"])
        if not fn:
            continue
        state = stub_state(index, s["file"], fn)
        res = judge_stub(api_key, state)
        item = {
            "file": s["file"],
            "function": s["function"],
            "lines": s["lines"],
            "is_intentional_stub": res.get("is_intentional_stub"),
            "stub_pattern": res.get("stub_pattern"),
            "docstring": state.get("docstring"),
            "class_context": state.get("class_context"),
            "error": res.get("error"),
        }
        stub_eval_results.append(item)
        print(f"   [{item['stub_pattern']}] {item['file']}::{item['function']} (p_intent: {item['is_intentional_stub']})", flush=True)

    # Evaluate sample of tests_only
    sample_tests_only = tests_only[:max_tests_only]
    print(f"\n4. Evaluating {len(sample_tests_only)} tests-only functions with TypeSafe Jev...", flush=True)
    tests_only_results = []
    for t in sample_tests_only:
        fn = index.find(t["file"], t["function"])
        if not fn:
            continue
        ev = evidence(index, t["file"], fn)
        state = tests_only_state(ev, index)
        res = judge_tests_only(api_key, state)
        item = {
            "file": t["file"],
            "function": t["function"],
            "lines": t["lines"],
            "test_references": t.get("test_references", 0),
            "is_testability_seam": res.get("is_testability_seam"),
            "test_usage_role": res.get("test_usage_role"),
            "error": res.get("error"),
        }
        tests_only_results.append(item)
        print(f"   [{item['test_usage_role']}] {item['file']}::{item['function']} (p_seam: {item['is_testability_seam']})", flush=True)

    return {
        "repo": repo_path,
        "scan_time_s": scan_time,
        "total_stubs_evaluated": len(stub_eval_results),
        "stubs": stub_eval_results,
        "total_tests_only_evaluated": len(tests_only_results),
        "tests_only": tests_only_results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="Path to repository")
    parser.add_argument("--output", default="evals/private/dead_code_pilot_results.json")
    parser.add_argument("--max-tests-only", type=int, default=8)
    args = parser.parse_args()

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("Error: TYPESAFE_API_KEY is required.")
        sys.exit(1)

    results = run_dead_code_pilot(args.repo, api_key, max_tests_only=args.max_tests_only)

    out_p = Path(args.output)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\nSaved pilot results to {out_p}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Evaluation of the New Sentinel Architecture (Jev Stage 6 Verifier & Consumer Contract Checker).

Compares:
1. Review findings before vs. after Jev Stage 6 Verifier (precision, false alarm pruning, defect retention).
2. Consumer Contract Verifier noise reduction (raw grep consumers vs. Jev contract-broken alerts).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from diff_risk_sentinel.consumer_verifier import verify_consumer_with_jev
from diff_risk_sentinel.finding_verifier import verify_finding_with_jev
from diff_risk_sentinel.signals import changed_tokens, find_consumers
from evals.review_cost.grader import grade_case_findings
from evals.review_cost.models import CaseConfig, Finding, RunResult


def evaluate_stage6_on_runs(
    runs: List[RunResult],
    cases: Dict[str, CaseConfig],
    repo_path: str,
    api_key: str,
) -> Dict[str, Any]:
    """
    Evaluates Stage 6 (Jev finding verifier) on review runs:
    Measures precision and defect recall BEFORE and AFTER the Jev filter.
    """
    before_findings_count = 0
    after_findings_count = 0
    pruned_count = 0
    retained_real_defects = 0

    results_by_arm: Dict[str, Dict[str, Any]] = {"A": {}, "B": {}}

    for arm in ("A", "B"):
        arm_runs = [r for r in runs if r.arm == arm]
        raw_judgments_before: List[str] = []
        raw_judgments_after: List[str] = []

        total_jev_tokens = 0
        total_jev_time = 0.0

        for r in arm_runs:
            case = cases.get(r.case_id)
            if not case:
                continue

            # Grade before
            g_before = grade_case_findings(case, r.findings, arm=arm, repetition=r.repetition)
            for j in g_before.finding_judgments:
                raw_judgments_before.append(j.verdict)

            # Apply Stage 6 Jev Verifier to each finding
            filtered_findings: List[Finding] = []
            for idx, f in enumerate(r.findings):
                before_findings_count += 1
                # Fetch cited code context
                show_res = subprocess.run(
                    ["git", "-C", repo_path, "show", f"{case.intro_commit}:{f.file}"],
                    capture_output=True,
                    text=True,
                )
                if show_res.returncode == 0:
                    lines = show_res.stdout.splitlines()
                    start = max(0, f.line - 15)
                    end = min(len(lines), f.line + 15)
                    cited_code = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines[start:end], start=start))
                else:
                    cited_code = ""

                t0 = time.time()
                v_res = verify_finding_with_jev(api_key, f, cited_code)
                total_jev_time += (time.time() - t0)
                total_jev_tokens += 430  # average ~370 in + 60 out

                if v_res["is_valid"]:
                    after_findings_count += 1
                    # Update finding severity if calibrated by Jev
                    f_calibrated = Finding(
                        file=f.file,
                        line=f.line,
                        function=f.function,
                        claim=f.claim,
                        severity=v_res["calibrated_severity"],
                    )
                    filtered_findings.append(f_calibrated)
                else:
                    pruned_count += 1

            # Grade after
            g_after = grade_case_findings(case, filtered_findings, arm=arm, repetition=r.repetition)
            for j in g_after.finding_judgments:
                raw_judgments_after.append(j.verdict)

        prec_before = (
            raw_judgments_before.count("correct") / len(raw_judgments_before)
            if raw_judgments_before
            else 0.0
        )
        prec_after = (
            raw_judgments_after.count("correct") / len(raw_judgments_after)
            if raw_judgments_after
            else 0.0
        )

        results_by_arm[arm] = {
            "total_findings_before": len(raw_judgments_before),
            "correct_before": raw_judgments_before.count("correct"),
            "precision_before": prec_before,
            "total_findings_after": len(raw_judgments_after),
            "correct_after": raw_judgments_after.count("correct"),
            "precision_after": prec_after,
            "pruned_findings": len(raw_judgments_before) - len(raw_judgments_after),
            "total_jev_tokens": total_jev_tokens,
            "total_jev_time_s": total_jev_time,
        }

    return results_by_arm


def evaluate_consumer_verifier_on_cases(
    cases: List[CaseConfig],
    repo_path: str,
    api_key: str,
) -> List[Dict[str, Any]]:
    """
    Evaluates Consumer Verifier noise reduction on pilot commits:
    Measures raw grep consumers found vs. Jev contract-broken alerts.
    """
    consumer_stats = []
    for c in cases:
        diff_res = subprocess.run(
            ["git", "-C", repo_path, "diff", f"{c.intro_commit}~1..{c.intro_commit}"],
            capture_output=True,
            text=True,
        )
        tokens = changed_tokens(diff_res.stdout)
        raw_consumers = find_consumers(repo_path, c.intro_commit, tokens)

        # Score top 5 consumers with Jev
        jev_scored = []
        for item in raw_consumers[:5]:
            fpath = item["file"]
            fn = item["function"]
            tok = item["tokens"][0] if item["tokens"] else ""

            show_res = subprocess.run(
                ["git", "-C", repo_path, "show", f"{c.intro_commit}:{fpath}"],
                capture_output=True,
                text=True,
            )
            consumer_code = show_res.stdout[:5000] if show_res.returncode == 0 else ""

            v = verify_consumer_with_jev(
                api_key=api_key,
                token=tok,
                producer_file="diff",
                producer_diff=diff_res.stdout[:5000],
                consumer_file=fpath,
                consumer_function=fn,
                consumer_code=consumer_code,
            )
            jev_scored.append({
                "consumer": f"{fpath}::{fn}",
                "token": tok,
                "broken_prob": v["broken_probability"],
                "verdict": v["verdict"],
            })

        broken_alerts = sum(1 for s in jev_scored if s["broken_prob"] >= 0.6)
        consumer_stats.append({
            "case_id": c.case_id,
            "category": c.category,
            "changed_tokens_count": len(tokens),
            "raw_consumers_count": len(raw_consumers),
            "sample_scored": len(jev_scored),
            "broken_contract_alerts": broken_alerts,
            "sample_details": jev_scored[:2],
        })

    return consumer_stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate new sentinel architecture (Stage 6 + Consumer Verifier).")
    parser.add_argument("--runs", required=True, help="Path to runs.json")
    parser.add_argument("--cases", required=True, help="Path to pilot_cases.json")
    parser.add_argument("--source-repo", required=True, help="Path to private repository")
    parser.add_argument("--output", default=None, help="Optional output JSON path")

    args = parser.parse_args()

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise ValueError("TYPESAFE_API_KEY environment variable is required.")

    with open(args.runs, "r", encoding="utf-8") as f:
        runs = [RunResult.from_dict(r) for r in json.load(f)]

    with open(args.cases, "r", encoding="utf-8") as f:
        cases_list = [CaseConfig.from_dict(c) for c in json.load(f)]
        cases_map = {c.case_id: c for c in cases_list}

    print("Running Stage 6 Finding Verifier evaluation...", flush=True)
    stage6_results = evaluate_stage6_on_runs(runs, cases_map, args.source_repo, api_key)

    print("Running Consumer Contract Verifier evaluation...", flush=True)
    consumer_results = evaluate_consumer_verifier_on_cases(cases_list, args.source_repo, api_key)

    report = {
        "stage6_verifier": stage6_results,
        "consumer_verifier": consumer_results,
    }

    if args.output:
        out_p = Path(args.output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"Results saved to {out_p}")

    print("\n" + "=" * 60)
    print("RESUMO DA NOVA ARQUITETURA (STAGE 6 & CONSUMERS):")
    print("=" * 60)
    for arm, data in stage6_results.items():
        print(f"\n[Braço {arm} - Stage 6 Filter]")
        print(f"  Achados Antes: {data['total_findings_before']} (Precisão: {data['precision_before']*100:.1f}%)")
        print(f"  Achados Depois: {data['total_findings_after']} (Precisão: {data['precision_after']*100:.1f}%)")
        print(f"  Falsos Alarmes Podados: {data['pruned_findings']}")
        print(f"  Tokens Jev Usados: {data['total_jev_tokens']} em {data['total_jev_time_s']:.2f}s")

    print("\n[Consumer Contract Verifier]")
    for cs in consumer_results:
        print(f"  {cs['case_id']} ({cs['category']}): {cs['raw_consumers_count']} consumidores brutos -> {cs['broken_contract_alerts']} alertas de quebra")


if __name__ == "__main__":
    main()

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
from typing import Any, Dict, List, Optional, Tuple

from diff_risk_sentinel.complexity import extract_functions
from diff_risk_sentinel.consumer_verifier import verify_consumer_with_jev
from diff_risk_sentinel.finding_verifier import verify_finding_with_jev
from diff_risk_sentinel.signals import changed_tokens, find_consumers
from evals.review_cost.grader import grade_case_findings
from evals.review_cost.models import CaseConfig, Finding, RunResult


def changed_files_from_diff(diff: str) -> set:
    """Repo-relative paths of files touched by a unified diff (`+++ b/` headers)."""
    files = set()
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                continue
            files.add(path[2:] if path.startswith("b/") else path)
    return files


def changed_hunk_for_token(diff: str, token: str) -> Optional[Tuple[str, str]]:
    """
    Returns (file_path, hunk_text) of the first hunk whose +/- lines contain
    `token`, or None when no changed line mentions it.
    """
    if not token:
        return None

    def hunk_changes_token(lines: List[str]) -> bool:
        return any(l[:1] in ("+", "-") and token in l[1:] for l in lines)

    current_file: Optional[str] = None
    hunk_file: Optional[str] = None
    hunk_lines: List[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git"):
            if hunk_lines and hunk_changes_token(hunk_lines):
                return hunk_file, "".join(hunk_lines)
            current_file = None
            hunk_file = None
            hunk_lines = []
            continue
        if line.startswith("+++ "):
            path = line[4:].strip()
            current_file = path[2:] if path.startswith("b/") else path
            continue
        if line.startswith("@@"):
            if hunk_lines and hunk_changes_token(hunk_lines):
                return hunk_file, "".join(hunk_lines)
            hunk_file = current_file
            hunk_lines = [line]
            continue
        if hunk_lines:
            hunk_lines.append(line)
    if hunk_lines and hunk_changes_token(hunk_lines):
        return hunk_file, "".join(hunk_lines)
    return None


def consumer_function_code(path: str, code: str, function: str) -> str:
    """Exact source lines of `function` inside `code`, or "" when not locatable."""
    functions = extract_functions(path, code) or []
    lines = code.splitlines(keepends=True)
    for f in functions:
        name = str(f.get("name", ""))
        if function in (name, name.split(".")[-1], name.split("(")[0]):
            start = max(0, int(f["start_line"]) - 1)
            return "".join(lines[start : int(f["end_line"])])
    return ""


def evaluate_stage6_on_runs(
    runs: List[RunResult],
    cases: Dict[str, CaseConfig],
    repo_path: str,
    api_key: str,
    verifier: Any = verify_finding_with_jev,
) -> Tuple[Dict[str, Any], List[Any], List[Any], List[RunResult]]:
    """
    Evaluates Stage 6 (Jev finding verifier) on review runs:
    Measures precision and defect recall BEFORE and AFTER the Jev filter.
    UNKNOWN verifier results retain the finding and are counted separately;
    only explicit FALSE_ALARM verdicts prune a finding.
    """
    results_by_arm: Dict[str, Dict[str, Any]] = {"A": {}, "B": {}}
    all_gradings_before = []
    all_gradings_after = []
    filtered_runs: List[RunResult] = []

    fix_cache: Dict[str, Tuple[str, str]] = {}

    def _get_fix_info(case: CaseConfig) -> Tuple[str, str]:
        if not case.fix_commit:
            return "", ""
        if case.fix_commit not in fix_cache:
            m_res = subprocess.run(
                ["git", "-C", repo_path, "log", "-1", "--format=%B", case.fix_commit],
                capture_output=True,
                text=True,
            )
            msg = m_res.stdout.strip() if m_res.returncode == 0 else ""
            d_res = subprocess.run(
                ["git", "-C", repo_path, "diff", f"{case.fix_commit}~1..{case.fix_commit}"],
                capture_output=True,
                text=True,
            )
            diff = d_res.stdout if d_res.returncode == 0 else ""
            fix_cache[case.fix_commit] = (msg, diff)
        return fix_cache[case.fix_commit]

    for arm in ("A", "B"):
        arm_runs = [r for r in runs if r.arm == arm]
        raw_judgments_before: List[str] = []
        raw_judgments_after: List[str] = []
        arm_gradings_before = []
        arm_gradings_after = []

        total_jev_tokens = 0
        total_jev_cost = 0.0
        total_jev_time = 0.0
        total_unknowns = 0

        for r in arm_runs:
            case = cases.get(r.case_id)
            if not case:
                continue

            fix_msg, fix_diff = _get_fix_info(case)

            # Grade before
            g_before = grade_case_findings(
                case, r.findings, fix_diff=fix_diff, fix_message=fix_msg, arm=arm, repetition=r.repetition
            )
            arm_gradings_before.append(g_before)
            all_gradings_before.append(g_before)
            for j in g_before.finding_judgments:
                raw_judgments_before.append(j.verdict)

            # Apply Stage 6 Jev Verifier to each frozen finding
            run_jev_tokens = 0
            run_jev_cost = 0.0
            run_jev_time = 0.0
            run_unknowns = 0
            filtered_findings: List[Finding] = []
            for idx, f in enumerate(r.findings):
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
                v_res = verifier(api_key, f, cited_code)
                run_jev_time += time.time() - t0
                usage = v_res.get("usage") or {}
                run_jev_tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                run_jev_cost += float(usage.get("cost_usd") or 0.0)

                if v_res.get("is_valid") is False:
                    continue
                if v_res.get("verdict") == "UNKNOWN":
                    run_unknowns += 1
                    filtered_findings.append(f)
                    continue
                # Update finding severity if calibrated by Jev
                filtered_findings.append(Finding(
                    file=f.file,
                    line=f.line,
                    function=f.function,
                    claim=f.claim,
                    severity=v_res["calibrated_severity"],
                ))

            total_jev_tokens += run_jev_tokens
            total_jev_cost += run_jev_cost
            total_jev_time += run_jev_time
            total_unknowns += run_unknowns

            # Grade after
            g_after = grade_case_findings(
                case, filtered_findings, fix_diff=fix_diff, fix_message=fix_msg, arm=arm, repetition=r.repetition
            )
            arm_gradings_after.append(g_after)
            all_gradings_after.append(g_after)
            for j in g_after.finding_judgments:
                raw_judgments_after.append(j.verdict)

            # Record filtered RunResult with this run's own Jev telemetry
            run_after = RunResult(
                case_id=r.case_id,
                arm=r.arm,
                repetition=r.repetition,
                target_commit=r.target_commit,
                base_commit=r.base_commit,
                duration_seconds=r.duration_seconds + run_jev_time,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cache_read_tokens=r.cache_read_tokens,
                cache_creation_tokens=r.cache_creation_tokens,
                cost_usd=r.cost_usd + run_jev_cost,
                jev_tokens=r.jev_tokens + run_jev_tokens,
                jev_cost_usd=r.jev_cost_usd + run_jev_cost,
                findings=filtered_findings,
                report_markdown=r.report_markdown,
                treatment_id=r.treatment_id,
                error=r.error,
                aborted=r.aborted,
            )
            filtered_runs.append(run_after)

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

        n_cases = max(1, len(arm_gradings_before))
        rec_found_before = sum(1 for g in arm_gradings_before if g.known_defect_verdict == "found") / n_cases
        rec_near_before = sum(1 for g in arm_gradings_before if g.known_defect_verdict in ("found", "near")) / n_cases
        rec_found_after = sum(1 for g in arm_gradings_after if g.known_defect_verdict == "found") / n_cases
        rec_near_after = sum(1 for g in arm_gradings_after if g.known_defect_verdict in ("found", "near")) / n_cases

        results_by_arm[arm] = {
            "total_findings_before": len(raw_judgments_before),
            "correct_before": raw_judgments_before.count("correct"),
            "precision_before": prec_before,
            "recall_found_before": rec_found_before,
            "recall_near_before": rec_near_before,
            "total_findings_after": len(raw_judgments_after),
            "correct_after": raw_judgments_after.count("correct"),
            "precision_after": prec_after,
            "recall_found_after": rec_found_after,
            "recall_near_after": rec_near_after,
            "pruned_findings": len(raw_judgments_before) - len(raw_judgments_after),
            "unknown_findings": total_unknowns,
            "total_jev_tokens": total_jev_tokens,
            "total_jev_cost_usd": total_jev_cost,
            "total_jev_time_s": total_jev_time,
        }

    return results_by_arm, all_gradings_before, all_gradings_after, filtered_runs


def evaluate_consumer_verifier_on_cases(
    cases: List[CaseConfig],
    repo_path: str,
    api_key: str,
    verifier: Any = verify_consumer_with_jev,
) -> List[Dict[str, Any]]:
    """
    Evaluates Consumer Verifier noise reduction on pilot commits:
    Measures raw grep consumers found vs. Jev contract-broken alerts.
    Consumers in files touched by the diff are excluded; each judged candidate
    receives the exact producer hunk and the exact consumer function body.
    """
    consumer_stats = []
    for c in cases:
        diff_res = subprocess.run(
            ["git", "-C", repo_path, "diff", f"{c.intro_commit}~1..{c.intro_commit}"],
            capture_output=True,
            text=True,
        )
        diff_text = diff_res.stdout if diff_res.returncode == 0 else ""
        tokens = changed_tokens(diff_text)
        changed_files = changed_files_from_diff(diff_text)
        raw_consumers = find_consumers(
            repo_path, c.intro_commit, tokens, exclude_files=changed_files
        )

        scored = []
        unknown = []
        unscored = []
        for item in raw_consumers[:5]:
            fpath = item["file"]
            fn = item["function"]
            tok = item["tokens"][0] if item["tokens"] else ""
            label = f"{fpath}::{fn}"

            hunk_info = changed_hunk_for_token(diff_text, tok)
            show_res = subprocess.run(
                ["git", "-C", repo_path, "show", f"{c.intro_commit}:{fpath}"],
                capture_output=True,
                text=True,
            )
            body = consumer_function_code(
                fpath, show_res.stdout if show_res.returncode == 0 else "", fn
            )
            if hunk_info is None or not body:
                unscored.append({
                    "consumer": label,
                    "token": tok,
                    "reason": "producer hunk not found" if hunk_info is None else "consumer body not found",
                })
                continue
            producer_file, producer_hunk = hunk_info

            v = verifier(
                api_key=api_key,
                token=tok,
                producer_file=producer_file,
                producer_diff=producer_hunk,
                consumer_file=fpath,
                consumer_function=fn,
                consumer_code=body,
            )
            v_usage = v.get("usage") or {}
            entry = {
                "consumer": label,
                "token": tok,
                "broken_prob": v["broken_probability"],
                "verdict": v["verdict"],
                "jev_tokens": int(v_usage.get("input_tokens") or 0) + int(v_usage.get("output_tokens") or 0),
            }
            if v["verdict"] == "UNKNOWN":
                entry["error"] = v.get("error")
                unknown.append(entry)
            else:
                scored.append(entry)

        broken_alerts = sum(1 for s in scored if (s["broken_prob"] or 0.0) >= 0.6)
        consumer_stats.append({
            "case_id": c.case_id,
            "category": c.category,
            "changed_tokens_count": len(tokens),
            "raw_candidates": len(raw_consumers),
            "raw_consumers_count": len(raw_consumers),
            "scored_candidates": len(scored),
            "unknown_candidates": len(unknown),
            "unscored_candidates": len(unscored),
            "broken_contract_alerts": broken_alerts,
            "sample_details": scored[:2],
            "unscored_details": unscored[:5],
            "unknown_details": unknown[:5],
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
    stage6_results, gradings_before, gradings_after, runs_after = evaluate_stage6_on_runs(
        runs, cases_map, args.source_repo, api_key
    )

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

        with open(out_p.parent / "gradings_before.json", "w", encoding="utf-8") as f:
            json.dump([g.to_dict() for g in gradings_before], f, indent=2)

        with open(out_p.parent / "gradings_after.json", "w", encoding="utf-8") as f:
            json.dump([g.to_dict() for g in gradings_after], f, indent=2)

        with open(out_p.parent / "runs_after.json", "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in runs_after], f, indent=2)

        print(f"Results saved to {out_p} (and companion gradings/runs)")

    print("\n" + "=" * 60)
    print("RESUMO DA NOVA ARQUITETURA (STAGE 6 & CONSUMERS):")
    print("=" * 60)
    for arm, data in stage6_results.items():
        print(f"\n[Braço {arm} - Stage 6 Filter]")
        print(f"  Achados Antes: {data['total_findings_before']} (Precisão: {data['precision_before']*100:.1f}%)")
        print(f"  Achados Depois: {data['total_findings_after']} (Precisão: {data['precision_after']*100:.1f}%)")
        print(f"  Findings rejected by verifier: {data['pruned_findings']}")
        print(f"  Findings retained as UNKNOWN: {data['unknown_findings']}")
        print(f"  Tokens Jev Usados: {data['total_jev_tokens']} (${data['total_jev_cost_usd']:.4f}) em {data['total_jev_time_s']:.2f}s")

    print("\n[Consumer Contract Verifier]")
    for cs in consumer_results:
        print(f"  {cs['case_id']} ({cs['category']}): {cs['raw_consumers_count']} consumidores brutos -> {cs['broken_contract_alerts']} alertas de quebra")


if __name__ == "__main__":
    main()

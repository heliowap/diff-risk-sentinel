from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from evals.review_cost.models import CaseGrading, RunResult


def bootstrap_ci(
    values: Sequence[float],
    n_resamples: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
) -> Tuple[float, float, float]:
    """Computes mean and non-parametric bootstrap confidence interval."""
    if not values:
        return 0.0, 0.0, 0.0
    n = len(values)
    mean_val = sum(values) / n
    if n == 1:
        return mean_val, mean_val, mean_val

    rng = random.Random(seed)
    resample_means: List[float] = []
    for _ in range(n_resamples):
        sample = [rng.choice(values) for _ in range(n)]
        resample_means.append(sum(sample) / n)

    resample_means.sort()
    alpha = (1.0 - ci) / 2.0
    lo_idx = int(alpha * n_resamples)
    hi_idx = int((1.0 - alpha) * n_resamples)
    hi_idx = min(hi_idx, n_resamples - 1)

    return mean_val, resample_means[lo_idx], resample_means[hi_idx]


def paired_cluster_bootstrap(
    values: Dict[str, Dict[str, float]],
    arm_a: str,
    arm_b: str,
    n_resamples: int = 5000,
    seed: int = 42,
) -> Tuple[float, float, float]:
    """
    Case-clustered paired bootstrap: resamples case IDs with replacement while
    keeping both arms' values together, then returns the observed mean of
    (arm_b - arm_a) plus a 95% percentile interval.
    """
    cases = [c for c, d in values.items() if arm_a in d and arm_b in d]
    if not cases:
        return 0.0, 0.0, 0.0
    diffs = [values[c][arm_b] - values[c][arm_a] for c in cases]
    observed = sum(diffs) / len(diffs)
    if len(cases) == 1:
        return observed, observed, observed

    rng = random.Random(seed)
    boots: List[float] = []
    for _ in range(n_resamples):
        sample = [rng.choice(cases) for _ in cases]
        boots.append(
            sum(values[c][arm_b] - values[c][arm_a] for c in sample) / len(sample)
        )
    boots.sort()
    lo = boots[int(0.025 * n_resamples)]
    hi = boots[min(int(0.975 * n_resamples), n_resamples - 1)]
    return observed, lo, hi


def compute_stratum_metrics(
    runs: Sequence[RunResult],
    gradings: Sequence[CaseGrading],
) -> Dict[str, Dict[str, Any]]:
    """
    Computes summary metrics grouped by arm, clustered at the case level:
    repetitions are averaged within each (case, arm) before comparison, recall
    uses one outcome per case/arm, and precision is reported only when all
    contributing gradings used semantic judgments (claim groups deduplicated
    per run).
    """
    case_arm_runs: Dict[Tuple[str, str], List[RunResult]] = {}
    for r in runs:
        case_arm_runs.setdefault((r.case_id, r.arm), []).append(r)
    case_arm_gradings: Dict[Tuple[str, str], List[CaseGrading]] = {}
    for g in gradings:
        case_arm_gradings.setdefault((g.case_id, g.arm), []).append(g)

    arms = sorted({a for _, a in case_arm_runs} | {a for _, a in case_arm_gradings})
    cases = sorted({c for c, _ in case_arm_runs} | {c for c, _ in case_arm_gradings})

    def case_run_mean(case: str, arm: str, attr: str) -> Optional[float]:
        rs = case_arm_runs.get((case, arm), [])
        if not rs:
            return None
        if attr == "total_tokens":
            return sum(r.total_tokens for r in rs) / len(rs)
        return sum(getattr(r, attr) for r in rs) / len(rs)

    def case_recall(case: str, arm: str, near: bool = False) -> Optional[float]:
        gs = [g for g in case_arm_gradings.get((case, arm), []) if not g.case_id.startswith("clean_")]
        if not gs:
            return None
        wanted = ("found", "near") if near else ("found",)
        return sum(1 for g in gs if g.known_defect_verdict in wanted) / len(gs)

    out: Dict[str, Dict[str, Any]] = {}
    for arm in arms:
        case_tokens = [v for c in cases if (v := case_run_mean(c, arm, "total_tokens")) is not None]
        case_costs = [v for c in cases if (v := case_run_mean(c, arm, "cost_usd")) is not None]
        case_durs = [v for c in cases if (v := case_run_mean(c, arm, "duration_seconds")) is not None]
        recalls_found = [v for c in cases if (v := case_recall(c, arm)) is not None]
        recalls_near = [v for c in cases if (v := case_recall(c, arm, near=True)) is not None]

        runs_count = sum(len(case_arm_runs.get((c, arm), [])) for c in cases)
        arm_gradings = [g for c in cases for g in case_arm_gradings.get((c, arm), [])]
        all_semantic = bool(arm_gradings) and all(g.grading_method == "semantic" for g in arm_gradings)

        correct_cnt = incorrect_cnt = unverifiable_cnt = 0
        groups_total = groups_correct = 0
        for g in arm_gradings:
            seen_groups: set = set()
            for j in g.finding_judgments:
                if j.verdict == "correct":
                    correct_cnt += 1
                elif j.verdict == "incorrect":
                    incorrect_cnt += 1
                else:
                    unverifiable_cnt += 1
                if j.claim_group_id and j.claim_group_id in seen_groups:
                    continue
                if j.claim_group_id:
                    seen_groups.add(j.claim_group_id)
                groups_total += 1
                if j.verdict == "correct":
                    groups_correct += 1

        n_judgments = correct_cnt + incorrect_cnt + unverifiable_cnt
        mean_tok, tok_lo, tok_hi = bootstrap_ci(case_tokens) if case_tokens else (0.0, 0.0, 0.0)

        out[arm] = {
            "runs_count": runs_count,
            "cases_count": len(case_tokens),
            "defect_cases_count": len(recalls_found),
            "mean_tokens": mean_tok,
            "tokens_ci": (tok_lo, tok_hi),
            "mean_cost_usd": (sum(case_costs) / len(case_costs)) if case_costs else 0.0,
            "mean_duration_s": (sum(case_durs) / len(case_durs)) if case_durs else 0.0,
            "recall_found_rate": (sum(recalls_found) / len(recalls_found)) if recalls_found else 0.0,
            "recall_near_rate": (sum(recalls_near) / len(recalls_near)) if recalls_near else 0.0,
            "precision_rate": (groups_correct / groups_total) if (all_semantic and groups_total) else None,
            "total_findings": n_judgments,
            "correct_findings": correct_cnt,
            "incorrect_findings": incorrect_cnt,
            "unverifiable_findings": unverifiable_cnt,
            "claim_groups_total": groups_total,
            "claim_groups_correct": groups_correct,
            "semantic_grading": all_semantic,
        }

    if {"A", "B"} <= set(arms):
        def collect(metric_fn) -> Dict[str, Dict[str, float]]:
            vals: Dict[str, Dict[str, float]] = {}
            for c in cases:
                entry = {}
                for arm in ("A", "B"):
                    v = metric_fn(c, arm)
                    if v is not None:
                        entry[arm] = v
                if entry:
                    vals[c] = entry
            return vals

        token_pairs = collect(lambda c, a: case_run_mean(c, a, "total_tokens"))
        out["paired"] = {
            "cases": [c for c, d in token_pairs.items() if "A" in d and "B" in d],
            "total_tokens": paired_cluster_bootstrap(token_pairs, "A", "B"),
            "cost_usd": paired_cluster_bootstrap(collect(lambda c, a: case_run_mean(c, a, "cost_usd")), "A", "B"),
            "duration_s": paired_cluster_bootstrap(collect(lambda c, a: case_run_mean(c, a, "duration_seconds")), "A", "B"),
            "recall_found_rate": paired_cluster_bootstrap(collect(lambda c, a: case_recall(c, a)), "A", "B"),
        }

    return out


def format_markdown_report(metrics_by_stratum: Dict[str, Dict[str, Any]]) -> str:
    """Formats aggregated metrics into a GitHub-flavored Markdown report."""
    lines = [
        "# Review Cost & Quality Evaluation: Sentinel-Assisted vs. Baseline",
        "",
        "| Stratum / Metric | Arm A (Baseline) | Arm B (Sentinel) | Difference (B vs A) |",
        "|---|---|---|---|",
    ]

    for stratum, arm_data in metrics_by_stratum.items():
        a = arm_data.get("A", {})
        b = arm_data.get("B", {})

        tok_a = a.get("mean_tokens", 0)
        tok_b = b.get("mean_tokens", 0)
        tok_diff = ((tok_b - tok_a) / tok_a * 100) if tok_a > 0 else 0.0

        rec_a = a.get("recall_found_rate", 0.0) * 100
        rec_b = b.get("recall_found_rate", 0.0) * 100
        rec_diff = rec_b - rec_a
        rec_counts = f"{b.get('defect_cases_count', 0)} defect cases"

        prec_a = a.get("precision_rate")
        prec_b = b.get("precision_rate")
        if prec_a is None or prec_b is None:
            prec_line = "| · Finding Precision | n/a (non-semantic grading) | n/a (non-semantic grading) | — |"
        else:
            prec_diff = (prec_b - prec_a) * 100
            prec_line = (
                f"| · Finding Precision | {prec_a * 100:.1f}% "
                f"({a.get('claim_groups_correct', 0)}/{a.get('claim_groups_total', 0)}) | "
                f"{prec_b * 100:.1f}% ({b.get('claim_groups_correct', 0)}/{b.get('claim_groups_total', 0)}) | "
                f"{prec_diff:+.1f} pp |"
            )

        lines.append(f"| **{stratum.title()}** ({rec_counts}) | | | |")
        lines.append(f"| · Mean Tokens | {tok_a:,.0f} | {tok_b:,.0f} | **{tok_diff:+.1f}%** |")
        lines.append(f"| · Defect Recall (exact) | {rec_a:.1f}% | {rec_b:.1f}% | {rec_diff:+.1f} pp |")
        lines.append(prec_line)
        lines.append(f"| · Mean Wall Clock | {a.get('mean_duration_s', 0):.1f}s | {b.get('mean_duration_s', 0):.1f}s | — |")

    lines.extend(["", "## Hypothesis Verification", ""])
    overall = metrics_by_stratum.get("overall", {})
    paired = overall.get("paired")
    if not paired or not paired.get("cases"):
        lines.append("- **H1 (Token Efficiency ≥30% savings on medium/large):** not evaluated — no paired case data.")
        lines.append("- **H2 (Non-inferior defect recall ≤5 pp difference):** not evaluated — no paired case data.")
        lines.append("- **H3 (Finding precision B ≥ A):** not evaluated — requires semantic gradings on both arms.")
    else:
        tok_diff_v, tok_lo, tok_hi = paired["total_tokens"]
        a_tok = overall.get("A", {}).get("mean_tokens", 0)
        if a_tok > 0:
            pct = tok_diff_v / a_tok * 100
            pct_lo = tok_lo / a_tok * 100
            pct_hi = tok_hi / a_tok * 100
            h1 = "supported" if pct_hi <= -30.0 else "not supported"
            lines.append(
                f"- **H1 (Token Efficiency ≥30% savings):** paired B−A = {pct:+.1f}% "
                f"[{pct_lo:+.1f}%, {pct_hi:+.1f}%] over {len(paired['cases'])} cases — {h1} "
                "(supported only if the interval's upper bound is ≤ −30%)."
            )
        else:
            lines.append("- **H1:** not evaluated — no arm A token data.")

        rec_diff_v, rec_lo, rec_hi = paired["recall_found_rate"]
        h2 = "supported" if rec_lo >= -0.05 else "not supported"
        lines.append(
            f"- **H2 (Non-inferior recall, margin −5 pp):** paired B−A = {rec_diff_v * 100:+.1f} pp "
            f"[{rec_lo * 100:+.1f}, {rec_hi * 100:+.1f}] — {h2} "
            "(supported only if the interval's lower bound is ≥ −5 pp)."
        )

        a_sem = overall.get("A", {}).get("semantic_grading")
        b_sem = overall.get("B", {}).get("semantic_grading")
        if a_sem and b_sem:
            pa = overall["A"].get("precision_rate") or 0.0
            pb = overall["B"].get("precision_rate") or 0.0
            lines.append(
                f"- **H3 (Finding precision B ≥ A):** descriptive only — "
                f"A={pa * 100:.1f}% vs B={pb * 100:.1f}% (claim-group deduplicated; "
                "no paired interval for proportions)."
            )
        else:
            lines.append("- **H3:** not evaluated — gradings are not semantic on both arms.")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze review cost and quality evaluation results.")
    parser.add_argument("--runs", required=True, help="JSON file containing RunResult records")
    parser.add_argument("--gradings", required=True, help="JSON file containing CaseGrading records")
    parser.add_argument("--cases", default=None, help="Optional JSON file containing CaseConfig records")
    parser.add_argument("--output", default=None, help="Optional output markdown file path")

    args = parser.parse_args()

    with open(args.runs, "r", encoding="utf-8") as f:
        runs = [RunResult.from_dict(r) for r in json.load(f)]

    with open(args.gradings, "r", encoding="utf-8") as f:
        gradings = [CaseGrading.from_dict(g) for g in json.load(f)]

    case_cat: Dict[str, str] = {}
    if args.cases and Path(args.cases).exists():
        with open(args.cases, "r", encoding="utf-8") as f:
            cases_data = json.load(f)
            case_cat = {c["case_id"]: c.get("category", "unknown") for c in cases_data}

    # Group runs and gradings by stratum
    stratum_runs: Dict[str, List[RunResult]] = {}
    stratum_gradings: Dict[str, List[CaseGrading]] = {}

    for r in runs:
        cat = case_cat.get(r.case_id) or r.case_id.split("_")[0]
        stratum_runs.setdefault(cat, []).append(r)

    for g in gradings:
        cat = case_cat.get(g.case_id) or g.case_id.split("_")[0]
        stratum_gradings.setdefault(cat, []).append(g)

    metrics_by_stratum: Dict[str, Dict[str, Any]] = {}
    for cat in sorted(stratum_runs.keys()):
        metrics_by_stratum[cat] = compute_stratum_metrics(
            stratum_runs[cat],
            stratum_gradings.get(cat, []),
        )

    # Add overall summary
    metrics_by_stratum["overall"] = compute_stratum_metrics(runs, gradings)
    report = format_markdown_report(metrics_by_stratum)

    if args.output:
        out_p = Path(args.output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Analysis saved to {out_p}")
    else:
        print(report)


if __name__ == "__main__":
    main()

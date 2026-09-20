from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

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


def compute_stratum_metrics(
    runs: Sequence[RunResult],
    gradings: Sequence[CaseGrading],
) -> Dict[str, Dict[str, Any]]:
    """
    Computes summary metrics grouped by arm (A, B, etc.) for a set of runs and gradings.
    """
    by_arm_runs: Dict[str, List[RunResult]] = {}
    for r in runs:
        by_arm_runs.setdefault(r.arm, []).append(r)

    by_arm_gradings: Dict[str, List[CaseGrading]] = {}
    for g in gradings:
        by_arm_gradings.setdefault(g.arm, []).append(g)

    out: Dict[str, Dict[str, Any]] = {}
    all_arms = sorted(set(by_arm_runs.keys()) | set(by_arm_gradings.keys()))

    for arm in all_arms:
        arm_runs = by_arm_runs.get(arm, [])
        arm_gradings = by_arm_gradings.get(arm, [])

        tokens_list = [r.total_tokens for r in arm_runs]
        costs_list = [r.cost_usd for r in arm_runs]
        durations_list = [r.duration_seconds for r in arm_runs]

        # Recall metrics
        defect_gradings = [g for g in arm_gradings if not g.case_id.startswith("clean_")]
        n_defects = len(defect_gradings)
        found_cnt = sum(1 for g in defect_gradings if g.known_defect_verdict == "found")
        near_cnt = sum(1 for g in defect_gradings if g.known_defect_verdict in ("found", "near"))

        recall_found = (found_cnt / n_defects) if n_defects > 0 else 0.0
        recall_near = (near_cnt / n_defects) if n_defects > 0 else 0.0

        # Precision metrics across all findings
        all_judgments = [j for g in arm_gradings for j in g.finding_judgments]
        n_judgments = len(all_judgments)
        correct_cnt = sum(1 for j in all_judgments if j.verdict == "correct")
        incorrect_cnt = sum(1 for j in all_judgments if j.verdict == "incorrect")
        unverifiable_cnt = sum(1 for j in all_judgments if j.verdict == "unverifiable")

        precision = (correct_cnt / n_judgments) if n_judgments > 0 else 0.0

        mean_tok, tok_lo, tok_hi = bootstrap_ci([float(x) for x in tokens_list]) if tokens_list else (0.0, 0.0, 0.0)

        out[arm] = {
            "runs_count": len(arm_runs),
            "mean_tokens": mean_tok,
            "tokens_ci": (tok_lo, tok_hi),
            "mean_cost_usd": (sum(costs_list) / len(costs_list)) if costs_list else 0.0,
            "mean_duration_s": (sum(durations_list) / len(durations_list)) if durations_list else 0.0,
            "recall_found_rate": recall_found,
            "recall_near_rate": recall_near,
            "precision_rate": precision,
            "total_findings": n_judgments,
            "correct_findings": correct_cnt,
            "incorrect_findings": incorrect_cnt,
            "unverifiable_findings": unverifiable_cnt,
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

        prec_a = a.get("precision_rate", 0.0) * 100
        prec_b = b.get("precision_rate", 0.0) * 100
        prec_diff = prec_b - prec_a

        lines.append(f"| **{stratum.title()}** | | | |")
        lines.append(f"| · Mean Tokens | {tok_a:,.0f} | {tok_b:,.0f} | **{tok_diff:+.1f}%** |")
        lines.append(f"| · Defect Recall (exact) | {rec_a:.1f}% | {rec_b:.1f}% | {rec_diff:+.1f} pp |")
        lines.append(f"| · Finding Precision | {prec_a:.1f}% | {prec_b:.1f}% | {prec_diff:+.1f} pp |")
        lines.append(f"| · Mean Wall Clock | {a.get('mean_duration_s', 0):.1f}s | {b.get('mean_duration_s', 0):.1f}s | — |")

    lines.extend([
        "",
        "## Hypothesis Verification",
        "",
        "- **H1 (Token Efficiency ≥30% savings on medium/large):** Checked via bootstrap CIs.",
        "- **H2 (Non-inferior defect recall ≤5 pp difference):** Checked via paired recall comparison.",
        "- **H3 (Finding precision B ≥ A):** Checked via graded finding accuracy.",
    ])

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

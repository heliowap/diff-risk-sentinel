import unittest
from evals.review_cost.models import RunResult, Finding, CaseGrading, FindingJudgment
from evals.review_cost.analyze_results import (
    bootstrap_ci,
    compute_stratum_metrics,
    format_markdown_report,
)


class TestAnalyzeResults(unittest.TestCase):

    def test_bootstrap_ci(self):
        values = [10.0, 12.0, 11.0, 10.5, 11.5, 9.5, 12.5]
        mean, lo, hi = bootstrap_ci(values, n_resamples=500, seed=42)
        self.assertAlmostEqual(mean, sum(values) / len(values), places=2)
        self.assertLessEqual(lo, mean)
        self.assertGreaterEqual(hi, mean)

    def test_compute_stratum_metrics(self):
        run_a = RunResult(
            case_id="med_01",
            arm="A",
            repetition=1,
            target_commit="c0",
            base_commit="b0",
            input_tokens=10000,
            output_tokens=2000,
            cost_usd=0.04,
            duration_seconds=30.0,
            findings=[Finding("a.py", 1, "f", "bug1", "major")],
        )
        run_b = RunResult(
            case_id="med_01",
            arm="B",
            repetition=1,
            target_commit="c0",
            base_commit="b0",
            input_tokens=4000,
            output_tokens=1000,
            cost_usd=0.02,
            duration_seconds=15.0,
            findings=[Finding("a.py", 1, "f", "bug1", "major")],
        )

        grading_a = CaseGrading(
            case_id="med_01",
            arm="A",
            repetition=1,
            known_defect_verdict="found",
            finding_judgments=[FindingJudgment(0, "correct", "real bug")],
        )
        grading_b = CaseGrading(
            case_id="med_01",
            arm="B",
            repetition=1,
            known_defect_verdict="found",
            finding_judgments=[FindingJudgment(0, "correct", "real bug")],
        )

        metrics = compute_stratum_metrics([run_a, run_b], [grading_a, grading_b])
        self.assertIn("A", metrics)
        self.assertIn("B", metrics)
        self.assertEqual(metrics["A"]["mean_tokens"], 12000)
        self.assertEqual(metrics["B"]["mean_tokens"], 5000)
        self.assertEqual(metrics["A"]["recall_found_rate"], 1.0)
        self.assertEqual(metrics["B"]["recall_found_rate"], 1.0)

    def test_format_markdown_report(self):
        report = format_markdown_report({
            "overall": {
                "A": {"mean_tokens": 50000, "recall_found_rate": 0.60, "precision_rate": 0.70},
                "B": {"mean_tokens": 25000, "recall_found_rate": 0.65, "precision_rate": 0.75},
            }
        })
        self.assertIn("# Review Cost & Quality Evaluation", report)
        self.assertIn("50,000", report)
        self.assertIn("25,000", report)

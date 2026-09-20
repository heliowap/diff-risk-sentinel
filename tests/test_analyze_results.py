import unittest
from evals.review_cost.models import RunResult, Finding, CaseGrading, FindingJudgment
from evals.review_cost.analyze_results import (
    bootstrap_ci,
    compute_stratum_metrics,
    format_markdown_report,
    paired_cluster_bootstrap,
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

    def test_paired_cluster_bootstrap(self):
        pairs = {
            "case_1": {"A": 0.0, "B": 1.0},
            "case_2": {"A": 1.0, "B": 1.0},
        }
        mean, lo, hi = paired_cluster_bootstrap(
            pairs, arm_a="A", arm_b="B", n_resamples=500, seed=42
        )
        self.assertEqual(mean, 0.5)
        self.assertLessEqual(lo, mean)
        self.assertGreaterEqual(hi, mean)

    def test_compute_stratum_metrics_clusters_cases_and_dedupes_claim_groups(self):
        runs = []
        gradings = []
        for rep in (1, 2):
            runs.append(
                RunResult(
                    case_id="case_1",
                    arm="A",
                    repetition=rep,
                    target_commit="t",
                    base_commit="b",
                    input_tokens=100,
                    output_tokens=10,
                    cost_usd=0.01,
                    duration_seconds=2.0,
                    findings=[
                        Finding("a.py", i, "f", "dup claim", "major") for i in range(10)
                    ],
                )
            )
            gradings.append(
                CaseGrading(
                    case_id="case_1",
                    arm="A",
                    repetition=rep,
                    known_defect_verdict="found" if rep == 1 else "missed",
                    finding_judgments=[
                        FindingJudgment(
                            i,
                            "correct",
                            "same defect",
                            matches_known_defect=True,
                            claim_group_id="g1",
                        )
                        for i in range(10)
                    ],
                    grading_method="semantic",
                )
            )
            runs.append(
                RunResult(
                    case_id="case_2",
                    arm="A",
                    repetition=rep,
                    target_commit="t",
                    base_commit="b",
                    input_tokens=200,
                    output_tokens=20,
                    cost_usd=0.02,
                    duration_seconds=4.0,
                    findings=[
                        Finding("b.py", 1, "g", "real", "major"),
                        Finding("b.py", 2, "h", "noise", "minor"),
                    ],
                )
            )
            gradings.append(
                CaseGrading(
                    case_id="case_2",
                    arm="A",
                    repetition=rep,
                    known_defect_verdict="missed",
                    finding_judgments=[
                        FindingJudgment(0, "correct", "real", claim_group_id="g2"),
                        FindingJudgment(1, "incorrect", "noise", claim_group_id="g3"),
                    ],
                    grading_method="semantic",
                )
            )

        metrics = compute_stratum_metrics(runs, gradings)
        a = metrics["A"]
        # recall: case_1 found in 1/2 reps -> 0.5; case_2 -> 0.0; mean over cases = 0.25
        self.assertAlmostEqual(a["recall_found_rate"], 0.25)
        # tokens: case means 110 and 220 -> 165 across cases
        self.assertAlmostEqual(a["mean_tokens"], 165.0)
        # precision: case_1 contributes 1 correct group per run (10 dups deduped);
        # case_2 contributes 1 correct + 1 incorrect per run -> 4/6
        self.assertAlmostEqual(a["precision_rate"], 4 / 6)

    def test_precision_is_none_for_location_proxy_gradings(self):
        runs = [
            RunResult(
                case_id="case_1",
                arm="A",
                repetition=1,
                target_commit="t",
                base_commit="b",
                findings=[Finding("a.py", 1, "f", "claim", "major")],
            )
        ]
        gradings = [
            CaseGrading(
                case_id="case_1",
                arm="A",
                repetition=1,
                known_defect_verdict="near",
                finding_judgments=[FindingJudgment(0, "unverifiable", "proxy only")],
                grading_method="location_proxy",
            )
        ]
        metrics = compute_stratum_metrics(runs, gradings)
        self.assertIsNone(metrics["A"]["precision_rate"])

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

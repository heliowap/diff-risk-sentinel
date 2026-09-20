import tempfile
import unittest

from evals.review_cost.eval_new_architecture import (
    changed_files_from_diff,
    changed_hunk_for_token,
    consumer_function_code,
    evaluate_consumer_verifier_on_cases,
    evaluate_stage6_on_runs,
)
from evals.review_cost.models import CaseConfig, Finding, RunResult


def _case(case_id: str) -> CaseConfig:
    return CaseConfig(
        case_id=case_id,
        intro_commit="a" * 40,
        base_commit="b" * 40,
        category="small",
        touched_production_functions=1,
    )


def _run(case_id: str, arm: str = "A") -> RunResult:
    return RunResult(
        case_id=case_id,
        arm=arm,
        repetition=1,
        target_commit="a" * 40,
        base_commit="b" * 40,
        duration_seconds=5.0,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.01,
        findings=[Finding(file="x.py", line=1, function="f", claim="bug")],
    )


class TestStage6Eval(unittest.TestCase):

    def test_per_run_jev_accounting_not_cumulative(self):
        cases = {"c1": _case("c1"), "c2": _case("c2")}
        runs = [_run("c1"), _run("c2")]
        verdicts = iter(
            [
                {
                    "is_valid": True,
                    "calibrated_severity": "major",
                    "verdict": "VALID",
                    "usage": {"input_tokens": 10, "output_tokens": 2, "cost_usd": 0.001},
                },
                {
                    "is_valid": True,
                    "calibrated_severity": "major",
                    "verdict": "VALID",
                    "usage": {"input_tokens": 20, "output_tokens": 3, "cost_usd": 0.002},
                },
            ]
        )

        def fake_verifier(api_key, finding, cited_code):
            return next(verdicts)

        _, _, _, filtered = evaluate_stage6_on_runs(
            runs, cases, repo_path=".", api_key="k", verifier=fake_verifier
        )

        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0].jev_tokens, 12)
        self.assertEqual(filtered[1].jev_tokens, 23)
        self.assertAlmostEqual(filtered[0].jev_cost_usd, 0.001)
        self.assertAlmostEqual(filtered[1].jev_cost_usd, 0.002)
        self.assertEqual(filtered[0].input_tokens, 100)
        self.assertEqual(filtered[0].output_tokens, 50)

    def test_unknown_findings_retained_and_counted(self):
        cases = {"c1": _case("c1")}
        runs = [_run("c1")]

        def fake_verifier(api_key, finding, cited_code):
            return {
                "is_valid": None,
                "verdict": "UNKNOWN",
                "error": "HTTP 503",
                "usage": {"input_tokens": 7},
            }

        results_by_arm, _, _, filtered = evaluate_stage6_on_runs(
            runs, cases, repo_path=".", api_key="k", verifier=fake_verifier
        )

        self.assertEqual(len(filtered[0].findings), 1)
        self.assertEqual(filtered[0].findings[0].claim, "bug")
        self.assertEqual(results_by_arm["A"]["unknown_findings"], 1)
        self.assertEqual(results_by_arm["A"]["pruned_findings"], 0)

    def test_false_alarm_findings_are_pruned(self):
        cases = {"c1": _case("c1")}
        runs = [_run("c1")]

        def fake_verifier(api_key, finding, cited_code):
            return {
                "is_valid": False,
                "verdict": "FALSE_ALARM",
                "calibrated_severity": "false_alarm",
                "usage": {"input_tokens": 8, "output_tokens": 1, "cost_usd": 0.0005},
            }

        results_by_arm, _, _, filtered = evaluate_stage6_on_runs(
            runs, cases, repo_path=".", api_key="k", verifier=fake_verifier
        )

        self.assertEqual(len(filtered[0].findings), 0)
        self.assertEqual(results_by_arm["A"]["pruned_findings"], 1)
        self.assertEqual(filtered[0].jev_tokens, 9)


class TestConsumerEvidence(unittest.TestCase):

    def test_changed_files_from_diff(self):
        diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1,2 +1,2 @@\n-foo\n+bar\n"
            "diff --git a/src/b.py b/src/b.py\n"
            "--- a/src/b.py\n+++ b/src/b.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        self.assertEqual(
            changed_files_from_diff(diff), {"src/a.py", "src/b.py"}
        )

    def test_changed_hunk_for_token_finds_late_hunk(self):
        filler = (
            "diff --git a/src/filler.py b/src/filler.py\n"
            "--- a/src/filler.py\n+++ b/src/filler.py\n"
            "@@ -1,400 +1,400 @@\n" + "-# filler line\n" * 400
        )
        target = (
            "diff --git a/src/billing.py b/src/billing.py\n"
            "--- a/src/billing.py\n+++ b/src/billing.py\n"
            "@@ -10,3 +10,3 @@ def calc\n"
            " context\n"
            "-def calc_tax(amount):\n+def calc_tax(amount, currency):\n"
            " context2\n"
        )
        res = changed_hunk_for_token(filler + target, "calc_tax")
        self.assertIsNotNone(res)
        path, hunk = res
        self.assertEqual(path, "src/billing.py")
        self.assertIn("calc_tax(amount, currency)", hunk)
        self.assertNotIn("filler", hunk)

    def test_changed_hunk_for_token_returns_none_when_absent(self):
        diff = "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertIsNone(changed_hunk_for_token(diff, "missing_token"))

    def test_consumer_function_code_extracts_exact_body(self):
        padding = "\n".join(f"# padding line {i}" for i in range(400))
        code = (
            padding
            + "\n\ndef checkout(order):\n    tax = calc_tax(order.amount)\n"
            "    return order.amount + tax\n"
        )
        body = consumer_function_code("src/orders.py", code, "checkout")
        self.assertIn("calc_tax(order.amount)", body)
        self.assertNotIn("padding", body)

    def test_consumer_eval_skips_touched_files_and_sends_exact_evidence(self):
        from tests.gitutil import GitRepo

        with tempfile.TemporaryDirectory() as td:
            src = GitRepo(td)
            src.commit(
                {
                    "billing.py": "def calc_tax(amount):\n    return amount * 0.1\n",
                    "orders.py": "def checkout(order):\n"
                    "    return order + calc_tax(order)\n",
                },
                "init",
            )
            base = src.head()
            src.commit(
                {
                    "billing.py": "def calc_tax(amount, currency):\n"
                    "    return amount * 0.1\n"
                },
                "change signature",
            )
            intro = src.head()

            case = CaseConfig(
                case_id="c1",
                intro_commit=intro,
                base_commit=base,
                category="small",
                touched_production_functions=1,
            )

            seen = []

            def fake_verifier(api_key, token, producer_file, producer_diff,
                              consumer_file, consumer_function, consumer_code):
                seen.append(
                    {
                        "token": token,
                        "producer_file": producer_file,
                        "producer_diff": producer_diff,
                        "consumer_file": consumer_file,
                        "consumer_function": consumer_function,
                        "consumer_code": consumer_code,
                    }
                )
                return {
                    "is_broken": True,
                    "broken_probability": 0.9,
                    "verdict": "PROBABLE_CONTRACT_BREAK",
                    "usage": {"input_tokens": 5},
                }

            stats = evaluate_consumer_verifier_on_cases(
                [case], td, "k", verifier=fake_verifier
            )

            self.assertEqual(len(seen), 1)
            call = seen[0]
            self.assertEqual(call["consumer_file"], "orders.py")
            self.assertEqual(call["consumer_function"], "checkout")
            self.assertEqual(call["producer_file"], "billing.py")
            self.assertIn("calc_tax(amount, currency)", call["producer_diff"])
            self.assertNotIn("orders.py", call["producer_diff"])
            self.assertIn("calc_tax(order)", call["consumer_code"])
            self.assertNotIn("def calc_tax", call["consumer_code"])

            self.assertEqual(stats[0]["scored_candidates"], 1)
            self.assertEqual(stats[0]["unscored_candidates"], 0)
            self.assertEqual(stats[0]["broken_contract_alerts"], 1)


if __name__ == "__main__":
    unittest.main()

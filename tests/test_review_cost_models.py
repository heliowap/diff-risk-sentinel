import unittest
from evals.review_cost.models import (
    Finding,
    ReviewOutput,
    CaseConfig,
    RunResult,
    FindingJudgment,
    CaseGrading,
    parse_review_output,
)


class TestReviewCostModels(unittest.TestCase):

    def test_finding_validation(self):
        f = Finding(
            file="src/service.py",
            line=42,
            function="Service.process",
            claim="Falsy check treats empty list as None",
            severity="critical",
        )
        self.assertEqual(f.severity, "critical")
        self.assertEqual(f.to_dict()["line"], 42)

    def test_legacy_fix_subject_is_not_treated_as_intro_subject(self):
        case = CaseConfig.from_dict({
            "case_id": "small_01",
            "intro_commit": "a" * 40,
            "base_commit": "a" * 40 + "~1",
            "category": "small",
            "touched_production_functions": 3,
            "fix_commit": "b" * 40,
            "fixed_functions": {"src/example.py": ["calculate"]},
            "subject": "fix: correct calculation",
        })
        self.assertEqual(case.intro_subject, "")
        self.assertEqual(case.fix_subject, "fix: correct calculation")
        self.assertNotIn("subject", case.to_dict())

    def test_parse_review_output_clean_json(self):
        raw = """{
            "findings": [
                {
                    "file": "app.py",
                    "line": 10,
                    "function": "calculate",
                    "claim": "Off-by-one in loop upper bound",
                    "severity": "major"
                }
            ],
            "report_markdown": "# Review Report\\n## Findings\\n- L10: bug"
        }"""
        out = parse_review_output(raw)
        self.assertEqual(len(out.findings), 1)
        self.assertEqual(out.findings[0].file, "app.py")
        self.assertEqual(out.findings[0].line, 10)
        self.assertEqual(out.findings[0].severity, "major")
        self.assertIn("# Review Report", out.report_markdown)

    def test_parse_review_output_with_markdown_fencing(self):
        raw = """Here is the review:
```json
{
    "findings": [
        {
            "file": "pkg/auth.py",
            "line": 99,
            "function": "verify_token",
            "claim": "Missing expiration check",
            "severity": "critical"
        }
    ],
    "report_markdown": "Full report"
}
```
Done!"""
        out = parse_review_output(raw)
        self.assertEqual(len(out.findings), 1)
        self.assertEqual(out.findings[0].function, "verify_token")

    def test_parse_review_output_preserves_treatment_id(self):
        raw = '{"findings": [], "report_markdown": "ok", "treatment_id": "marker-1"}'
        out = parse_review_output(raw)
        self.assertEqual(out.treatment_id, "marker-1")

    def test_parse_review_output_handles_empty_or_malformed(self):
        out = parse_review_output("I found no structured json")
        self.assertEqual(len(out.findings), 0)
        self.assertIn("I found no structured json", out.report_markdown)

    def test_run_result_serialization(self):
        res = RunResult(
            case_id="case_001",
            arm="B",
            repetition=1,
            target_commit="a" * 40,
            base_commit="b" * 40,
            duration_seconds=12.5,
            input_tokens=1500,
            output_tokens=350,
            cache_read_tokens=500,
            cache_creation_tokens=100,
            cost_usd=0.015,
            jev_tokens=4000,
            jev_cost_usd=0.008,
            findings=[
                Finding(
                    file="a.py",
                    line=5,
                    function="f",
                    claim="bug",
                    severity="minor",
                )
            ],
            report_markdown="report text",
        )
        d = res.to_dict()
        self.assertEqual(d["case_id"], "case_001")
        self.assertEqual(d["total_tokens"], 1500 + 350 + 500 + 100 + 4000)
        self.assertEqual(len(d["findings"]), 1)
        self.assertEqual(d["findings"][0]["claim"], "bug")

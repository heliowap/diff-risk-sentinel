import unittest
from evals.review_cost.models import CaseConfig
from evals.review_cost.runner import build_review_prompt, ARM_A_INSTRUCTIONS, ARM_B_INSTRUCTIONS


class TestRunner(unittest.TestCase):

    def test_build_review_prompt_arm_a(self):
        case = CaseConfig(
            case_id="med_01",
            intro_commit="c0" * 20,
            base_commit="b0" * 20,
            category="medium",
            touched_production_functions=45,
            subject="Add billing retry mechanism",
        )
        prompt = build_review_prompt(case, arm="A")
        self.assertIn("ARM A", prompt)
        self.assertIn(ARM_A_INSTRUCTIONS[:30], prompt)
        self.assertIn(case.intro_commit, prompt)
        self.assertIn('"findings"', prompt)

    def test_build_review_prompt_arm_b(self):
        case = CaseConfig(
            case_id="med_01",
            intro_commit="c0" * 20,
            base_commit="b0" * 20,
            category="medium",
            touched_production_functions=45,
            subject="Add billing retry mechanism",
        )
        prompt = build_review_prompt(case, arm="B")
        self.assertIn("ARM B", prompt)
        self.assertIn("diff-risk-sentinel", prompt)
        self.assertIn("spec_worksheet", prompt)
        self.assertIn(case.intro_commit, prompt)
        self.assertIn('"findings"', prompt)

    def test_run_case_review_end_to_end(self):
        import tempfile
        from pathlib import Path
        from tests.gitutil import GitRepo
        from evals.review_cost.runner import run_case_review

        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "src"
            src_dir.mkdir(parents=True, exist_ok=True)
            src = GitRepo(str(src_dir))

            src.commit({"calc.py": "def add(a, b):\n    return a + b\n"}, "init")
            c_base = src.head()

            src.commit({"calc.py": "def add(a, b):\n    return a - b\n"}, "buggy add")
            c_intro = src.head()

            case = CaseConfig(
                case_id="pilot_01",
                intro_commit=c_intro,
                base_commit=c_base,
                category="small",
                touched_production_functions=1,
                subject="buggy add",
            )

            # Custom mock runner to verify findings are recorded
            def mock_command_runner(prompt: str, repo_path: Path):
                return {
                    "output": """```json
{
  "findings": [
    {"file": "calc.py", "line": 2, "function": "add", "claim": "Subtracts instead of adds", "severity": "critical"}
  ],
  "report_markdown": "Identified subtraction bug"
}
```""",
                    "input_tokens": 1200,
                    "output_tokens": 150,
                    "cost_usd": 0.005,
                }

            result = run_case_review(
                case=case,
                source_repo=src_dir,
                arm="B",
                repetition=1,
                command_runner=mock_command_runner,
            )

            self.assertEqual(result.case_id, "pilot_01")
            self.assertEqual(result.arm, "B")
            self.assertEqual(result.input_tokens, 1200)
            self.assertEqual(result.output_tokens, 150)
            self.assertEqual(len(result.findings), 1)
            self.assertEqual(result.findings[0].function, "add")
            self.assertEqual(result.findings[0].claim, "Subtracts instead of adds")
            self.assertEqual(result.findings[0].severity, "critical")


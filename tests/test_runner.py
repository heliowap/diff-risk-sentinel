import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evals.review_cost.models import CaseConfig
from evals.review_cost.runner import (
    build_review_prompt,
    ARM_A_INSTRUCTIONS,
    ARM_B_INSTRUCTIONS,
)


class TestRunner(unittest.TestCase):

    def test_build_review_prompt_arm_a(self):
        case = CaseConfig(
            case_id="med_01",
            intro_commit="c0" * 20,
            base_commit="b0" * 20,
            category="medium",
            touched_production_functions=45,
            intro_subject="Add billing retry mechanism",
        )
        prompt = build_review_prompt(case, arm="A")
        self.assertIn("ARM A", prompt)
        self.assertIn(ARM_A_INSTRUCTIONS[:30], prompt)
        self.assertIn(case.intro_commit, prompt)
        self.assertIn('"findings"', prompt)

    def test_review_prompt_never_contains_fix_subject(self):
        case = CaseConfig(
            case_id="small_01",
            intro_commit="a" * 40,
            base_commit="a" * 40 + "~1",
            category="small",
            touched_production_functions=3,
            fix_commit="b" * 40,
            intro_subject="feat: add calculation",
            fix_subject="fix: correct calculation",
        )
        prompt = build_review_prompt(case, arm="A")
        self.assertIn("feat: add calculation", prompt)
        self.assertNotIn("fix: correct calculation", prompt)

    def test_build_review_prompt_arm_b(self):
        case = CaseConfig(
            case_id="med_01",
            intro_commit="c0" * 20,
            base_commit="b0" * 20,
            category="medium",
            touched_production_functions=45,
            intro_subject="Add billing retry mechanism",
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
                intro_subject="buggy add",
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
                    "cache_read_tokens": 500,
                    "cache_creation_tokens": 100,
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
            self.assertEqual(result.cache_read_tokens, 500)
            self.assertEqual(result.cache_creation_tokens, 100)
            self.assertEqual(len(result.findings), 1)
            self.assertEqual(result.findings[0].function, "add")
            self.assertEqual(result.findings[0].claim, "Subtracts instead of adds")
            self.assertEqual(result.findings[0].severity, "critical")

    def _make_case_repo(self, td):
        from tests.gitutil import GitRepo

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
            intro_subject="buggy add",
        )
        return src_dir, case

    @staticmethod
    def _artifact_builder(case, repo_path, sentinel_path, worksheet_path):
        from evals.review_cost.runner import SentinelArtifacts

        sentinel_path.write_text(
            '{"meta": {"treatment_id": "marker-1", "jev_tokens": 321, '
            '"jev_cost_usd": 0.001}, "targets": []}'
        )
        worksheet_path.write_text("Treatment-ID: marker-1\n")
        return SentinelArtifacts(
            sentinel_path=sentinel_path,
            worksheet_path=worksheet_path,
            treatment_id="marker-1",
            jev_tokens=321,
            jev_cost_usd=0.001,
        )

    def test_claude_command_runner_uses_safe_mode_for_both_arms(self):
        from evals.review_cost.runner import claude_command_runner

        proc = Mock(
            returncode=0,
            stdout=json.dumps({"result": "{}", "usage": {}, "total_cost_usd": 0.0}),
            stderr="",
        )
        for arm in ("A", "B"):
            with self.subTest(arm=arm):
                with patch(
                    "evals.review_cost.runner.subprocess.run", return_value=proc
                ) as mock_run:
                    claude_command_runner("prompt", Path("/tmp"), arm=arm)
                cmd = mock_run.call_args[0][0]
                self.assertIn("--safe-mode", cmd)

    def test_run_case_review_arm_b_attests_treatment_marker(self):
        from evals.review_cost.runner import run_case_review

        with tempfile.TemporaryDirectory() as td:
            src_dir, case = self._make_case_repo(td)
            captured = {}

            def mock_command_runner(prompt, repo_path):
                captured["prompt"] = prompt
                return {
                    "output": '{"findings": [], "report_markdown": "ok", '
                    '"treatment_id": "marker-1"}',
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": 0.001,
                }

            result = run_case_review(
                case=case,
                source_repo=src_dir,
                arm="B",
                repetition=1,
                command_runner=mock_command_runner,
                artifact_builder=self._artifact_builder,
            )

            self.assertFalse(result.aborted)
            self.assertIsNone(result.error)
            self.assertEqual(result.treatment_id, "marker-1")
            self.assertEqual(result.jev_tokens, 321)
            self.assertEqual(result.jev_cost_usd, 0.001)
            self.assertNotIn("marker-1", captured["prompt"])

    def test_run_case_review_arm_b_aborts_on_missing_marker(self):
        from evals.review_cost.runner import run_case_review

        with tempfile.TemporaryDirectory() as td:
            src_dir, case = self._make_case_repo(td)

            def mock_command_runner(prompt, repo_path):
                return {
                    "output": '{"findings": [], "report_markdown": "ok"}',
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": 0.001,
                }

            result = run_case_review(
                case=case,
                source_repo=src_dir,
                arm="B",
                repetition=1,
                command_runner=mock_command_runner,
                artifact_builder=self._artifact_builder,
            )

            self.assertTrue(result.aborted)
            self.assertIsNotNone(result.error)
            self.assertIn("treatment", result.error.lower())

    def test_run_case_review_arm_a_skips_artifact_builder(self):
        from evals.review_cost.runner import run_case_review

        with tempfile.TemporaryDirectory() as td:
            src_dir, case = self._make_case_repo(td)

            def artifact_builder(*args, **kwargs):
                raise AssertionError("artifact builder must not run for arm A")

            def mock_command_runner(prompt, repo_path):
                return {
                    "output": '{"findings": [], "report_markdown": "ok"}',
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": 0.001,
                }

            result = run_case_review(
                case=case,
                source_repo=src_dir,
                arm="A",
                repetition=1,
                command_runner=mock_command_runner,
                artifact_builder=artifact_builder,
            )

            self.assertFalse(result.aborted)
            self.assertIsNone(result.treatment_id)


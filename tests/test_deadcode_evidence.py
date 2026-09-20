import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from diff_risk_sentinel.deadcode import scan_repository
from diff_risk_sentinel.deadcode_evidence import (
    RepoIndex,
    _extract_class_context,
    _extract_docstring,
    evidence,
    judge_state,
    judge_stub,
    judge_tests_only,
    review_with_jev,
    stub_state,
    tests_only_state,
)
from tests.gitutil import GitRepo

FILES = {
    "app/store.ts": ("export function clearPending() {\n  return 1;\n}\n"
                     "export function usedEverywhere() {\n  return 2;\n}\n"),
    "app/other.ts": ("import { usedEverywhere } from './store';\n"
                     "const cache = {\n  clearPending() {\n    return 0;\n  },\n};\n"
                     "export const a = usedEverywhere();\nexport const b = cache.clearPending();\n"),
    "app/plugin.ts": "export function closeBundle() {\n  return undefined;\n}\n",
    "app/lonely.py": "def lonely():\n    return None\n",
    "tests/test_store.ts": "import { clearPending } from '../app/store';\nclearPending();\n",
}


def _score(scores, ev):
    return scores.get(f"{ev['file']}::{ev['function']}", scores.get(ev["function"], 0.1))


class _Repo(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.git = GitRepo(self.tmp.name)
        self.git.commit(FILES, "init")

    def tearDown(self):
        self.tmp.cleanup()


class TestJudgeState(_Repo):

    def test_state_leads_with_counts_and_tags_every_mention(self):
        index = RepoIndex(self.tmp.name, "HEAD")
        state = judge_state(evidence(index, "app/store.ts", index.find("app/store.ts", "clearPending")))
        self.assertIn("1 time(s) in other production files", state["evidence_summary"])
        self.assertIn("1 line(s) define a different function with the same name", state["evidence_summary"])
        self.assertTrue(any(m.startswith("[TEST, OTHER FILE]") for m in state["mentions"]))
        self.assertTrue(any("DEFINES ANOTHER FUNCTION WITH THE SAME NAME" in m for m in state["mentions"]))
        self.assertNotIn("code", state)  # the body is not evidence of reachability

    def test_function_nobody_mentions_says_so(self):
        index = RepoIndex(self.tmp.name, "HEAD")
        state = judge_state(evidence(index, "app/lonely.py", index.find("app/lonely.py", "lonely")))
        self.assertEqual(state["mentions"], ["(no mentions anywhere else in the repository)"])


class TestReviewWithJev(_Repo):

    def fake_judge(self, scores):
        return lambda api_key, ev: {"p_removable": _score(scores, ev), "input_tokens": 10}

    def test_jev_vetoes_static_findings_and_adds_probable_ones(self):
        found = scan_repository(self.tmp.name, "HEAD")
        self.assertEqual({f["function"] for f in found}, {"closeBundle", "lonely"})
        scores = {"closeBundle": 0.3, "lonely": 0.95, "app/store.ts::clearPending": 0.9, "usedEverywhere": 0.1}
        result = review_with_jev(self.tmp.name, "HEAD", found, "k", judge_fn=self.fake_judge(scores))
        self.assertEqual([f["function"] for f in result["dead_code"]], ["lonely"])
        self.assertEqual(result["dead_code"][0]["jev_p_removable"], 0.95)
        self.assertEqual([f["function"] for f in result["vetoed_by_jev"]], ["closeBundle"])
        # clearPending is mentioned in production only by a same-named method: a probable finding.
        self.assertEqual([f["function"] for f in result["probable_dead"]], ["clearPending"])

    def test_failed_judgments_keep_static_findings(self):
        found = scan_repository(self.tmp.name, "HEAD")
        result = review_with_jev(self.tmp.name, "HEAD", found, "k",
                                 judge_fn=lambda api_key, ev: {"error": "HTTP 503"})
        self.assertEqual({f["function"] for f in result["dead_code"]}, {"closeBundle", "lonely"})
        self.assertGreaterEqual(result["jev_judged"], 3)  # both static findings + the probable candidates
        self.assertEqual(result["jev_failures"], result["jev_judged"])
        self.assertEqual(result["probable_dead"], [])


class TestDeadCodeCommandWithJev(_Repo):

    def run_it(self, env, **kw):
        from diff_risk_sentinel.cli import run_dead_code
        out = os.path.join(self.tmp.name, "dead.json")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            code = run_dead_code(repo=self.tmp.name, rev="HEAD", output=out, top=10, **kw)
        return code, json.load(open(out)), buf.getvalue()

    def test_jev_review_is_written_to_the_report(self):
        scores = {"closeBundle": 0.3, "lonely": 0.95, "app/store.ts::clearPending": 0.9}
        with mock.patch("diff_risk_sentinel.deadcode_evidence.judge",
                        side_effect=lambda api_key, ev: {"p_removable": _score(scores, ev)}):
            code, data, out = self.run_it({"TYPESAFE_API_KEY": "k"}, jev=True)
        self.assertEqual(code, 0)
        self.assertTrue(data["meta"]["jev_enabled"])
        self.assertEqual([f["function"] for f in data["dead_code"]], ["lonely"])
        self.assertEqual([f["function"] for f in data["vetoed_by_jev"]], ["closeBundle"])
        self.assertEqual([f["function"] for f in data["probable_dead"]], ["clearPending"])
        self.assertIn("clearPending", out)

    def test_jev_without_key_falls_back_to_static(self):
        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            code, data, out = self.run_it({}, jev=True)
        self.assertFalse(data["meta"]["jev_enabled"])
        self.assertEqual({f["function"] for f in data["dead_code"]}, {"closeBundle", "lonely"})
        self.assertIn("TYPESAFE_API_KEY", out)


class TestStubAndTestsOnlyClassifiers(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.git = GitRepo(self.tmp.name)
        files = {
            "app/interfaces.py": (
                "from typing import Protocol\n\n"
                "class StorageProvider(Protocol):\n"
                "    '''Storage abstraction.'''\n"
                "    def save_blob(self, key: str, data: bytes) -> None:\n"
                "        '''Save raw bytes.'''\n"
                "        pass\n"
            ),
            "app/orphans.py": (
                "def forgotten_work():\n"
                "    # TODO: remove me\n"
                "    pass\n\n"
                "def active_seam():\n"
                "    '''Reset global test harness state.'''\n"
                "    return True\n\n"
                "def dead_feature():\n"
                "    return 42\n"
            ),
            "tests/test_orphans.py": (
                "from app.orphans import active_seam, dead_feature\n\n"
                "def test_setup():\n"
                "    assert active_seam() is True\n\n"
                "def test_dead():\n"
                "    assert dead_feature() == 42\n"
            ),
        }
        self.git.commit(files, "init")
        self.index = RepoIndex(self.tmp.name, "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_class_context(self):
        lines = self.index.lines["app/interfaces.py"]
        fn = self.index.find("app/interfaces.py", "StorageProvider.save_blob")
        self.assertIsNotNone(fn)
        ctx = _extract_class_context(lines, fn["start_line"], fn["name"])
        self.assertIn("class StorageProvider(Protocol):", ctx)

    def test_extract_docstring(self):
        fn = self.index.find("app/interfaces.py", "StorageProvider.save_blob")
        lines = self.index.lines["app/interfaces.py"]
        code = "\n".join(lines[fn["start_line"] - 1:fn["end_line"]])
        doc = _extract_docstring(code, lines, fn["start_line"], True)
        self.assertEqual(doc, "Save raw bytes.")

    def test_stub_state_construction(self):
        fn = self.index.find("app/interfaces.py", "StorageProvider.save_blob")
        state = stub_state(self.index, "app/interfaces.py", fn)
        self.assertEqual(state["function"], "StorageProvider.save_blob")
        self.assertIn("StorageProvider(Protocol)", state["class_context"])
        self.assertEqual(state["docstring"], "Save raw bytes.")
        self.assertTrue(state["is_stub"])

    def test_tests_only_state_construction(self):
        fn = self.index.find("app/orphans.py", "active_seam")
        ev = evidence(self.index, "app/orphans.py", fn)
        tstate = tests_only_state(ev, self.index)
        self.assertEqual(tstate["function"], "active_seam")
        self.assertGreaterEqual(tstate["test_references_count"], 1)
        self.assertTrue(any("test_orphans.py" in m for m in tstate["test_mentions"]))

    def test_judge_stub_parses_response(self):
        fake_ans = {
            "answers": {
                "is_intentional_stub": {"noul": 0.92},
                "stub_pattern": {"choice": "protocol_or_abstract"},
            },
            "usage": {"input_tokens": 120},
        }
        with mock.patch("diff_risk_sentinel.jev.ask_jev", return_value=fake_ans):
            res = judge_stub("key", {"function": "test"})
            self.assertEqual(res["is_intentional_stub"], 0.92)
            self.assertEqual(res["stub_pattern"], "protocol_or_abstract")

    def test_judge_tests_only_parses_response(self):
        fake_ans = {
            "answers": {
                "is_testability_seam": {"noul": 0.88},
                "test_usage_role": {"choice": "testability_seam"},
            },
            "usage": {"input_tokens": 140},
        }
        with mock.patch("diff_risk_sentinel.jev.ask_jev", return_value=fake_ans):
            res = judge_tests_only("key", {"function": "test"})
            self.assertEqual(res["is_testability_seam"], 0.88)
            self.assertEqual(res["test_usage_role"], "testability_seam")

    def test_review_with_jev_differentiates_intentional_and_dead_stubs_and_seams(self):
        found = [
            {"file": "app/interfaces.py", "function": "StorageProvider.save_blob", "status": "unreferenced", "stub": True, "lines": "5-7"},
            {"file": "app/orphans.py", "function": "forgotten_work", "status": "unreferenced", "stub": True, "lines": "1-3"},
            {"file": "app/orphans.py", "function": "active_seam", "status": "tests_only", "stub": False, "lines": "5-7"},
            {"file": "app/orphans.py", "function": "dead_feature", "status": "tests_only", "stub": False, "lines": "9-10"},
        ]
        def stub_judge(key, state):
            if "StorageProvider" in state["function"]:
                return {"is_intentional_stub": 0.95, "stub_pattern": "protocol_or_abstract"}
            return {"is_intentional_stub": 0.05, "stub_pattern": "abandoned_stub"}

        def tests_judge(key, state):
            if "active_seam" in state["function"]:
                return {"is_testability_seam": 0.90, "test_usage_role": "testability_seam"}
            return {"is_testability_seam": 0.10, "test_usage_role": "orphaned_feature"}

        res = review_with_jev(
            self.tmp.name,
            "HEAD",
            found,
            "k",
            stub_judge_fn=stub_judge,
            tests_only_judge_fn=tests_judge,
        )

        # StorageProvider.save_blob is intentional stub -> vetoed
        self.assertEqual([f["function"] for f in res["intentional_stubs"]], ["StorageProvider.save_blob"])
        # active_seam is intentional seam -> vetoed
        self.assertEqual([f["function"] for f in res["test_seams"]], ["active_seam"])
        # Both vetoed
        vetoed_names = {f["function"] for f in res["vetoed_by_jev"]}
        self.assertIn("StorageProvider.save_blob", vetoed_names)
        self.assertIn("active_seam", vetoed_names)

        # forgotten_work (dead stub) and dead_feature (orphaned feature) are confirmed in dead_code
        dead_names = {f["function"] for f in res["dead_code"]}
        self.assertIn("forgotten_work", dead_names)
        self.assertIn("dead_feature", dead_names)


if __name__ == "__main__":
    unittest.main()

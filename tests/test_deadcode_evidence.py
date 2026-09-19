import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from diff_risk_sentinel.deadcode import scan_repository
from diff_risk_sentinel.deadcode_evidence import RepoIndex, evidence, judge_state, review_with_jev
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


if __name__ == "__main__":
    unittest.main()

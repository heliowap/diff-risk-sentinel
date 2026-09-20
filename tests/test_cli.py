import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from diff_risk_sentinel.cli import run_sentinel
from tests.gitutil import GitRepo


BASE_PY = """def keep(x):
    if x:
        return 1
    return 0


def neighbour(y):
    return y
"""

HEAD_PY = """def keep(x):
    if x:
        return 1
    if x > 1:
        return 2
    if x > 2:
        return 3
    return 0


def neighbour(y):
    return y


def brand_new(z):
    if z:
        return 1
    return 0
"""


class TestRunSentinel(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.repo = GitRepo(self.root)
        self.repo.commit({"src/mod.py": BASE_PY, "src/dead.py": "def gone(a):\n    if a:\n        return 1\n    return 2\n"}, "base")
        self.repo.commit({"src/mod.py": HEAD_PY, "src/dead.py": None}, "head")
        self.out = os.path.join(self.root, "out.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, **kwargs):
        kwargs.setdefault("base", "HEAD~1")
        kwargs.setdefault("threshold_crap", 0.0)
        kwargs.setdefault("threshold_ccn", 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_sentinel(repo=self.root, output=self.out, **kwargs)
        with open(self.out, encoding="utf-8") as fh:
            return code, json.load(fh), buf.getvalue()

    def targets(self, payload):
        return {t["function"]: t for t in payload["targets"]}

    def test_reads_head_revision_not_dirty_working_tree(self):
        with open(os.path.join(self.root, "src/mod.py"), "w") as fh:
            fh.write("# wiped\n")
        _, payload, _ = self.run_it()
        self.assertEqual(self.targets(payload)["keep"]["ccn"], 4)

    def test_delta_crap_uses_real_ccn_of_base_revision(self):
        _, payload, _ = self.run_it()
        keep = self.targets(payload)["keep"]
        # CCN 2 -> 4 at 0% coverage: CRAP 6 -> 20
        self.assertEqual(keep["crap_before"], 6.0)
        self.assertEqual(keep["delta_crap"], 14.0)

    def test_new_function_delta_is_its_full_crap(self):
        _, payload, _ = self.run_it()
        new = self.targets(payload)["brand_new"]
        self.assertIsNone(new["crap_before"])
        self.assertEqual(new["delta_crap"], new["crap"])

    def test_untouched_neighbour_is_not_flagged(self):
        _, payload, _ = self.run_it()
        self.assertNotIn("neighbour", self.targets(payload))

    def test_summary_counts_removed_functions(self):
        _, payload, _ = self.run_it()
        summary = payload["otterwise_summary"]
        self.assertEqual(summary["removed_methods"], 1)
        self.assertEqual(summary["total_methods"], 2)
        # keep: 6 -> 20 ; brand_new: +6 ; gone: -6
        self.assertEqual(summary["combined_delta_crap"], 14.0)

    def test_summary_is_computed_before_thresholding(self):
        _, payload, _ = self.run_it(threshold_crap=1000.0, threshold_ccn=1000, threshold_delta=1000.0)
        self.assertEqual(payload["targets"], [])
        self.assertEqual(payload["otterwise_summary"]["total_methods"], 2)

    def test_output_file_is_always_rewritten(self):
        with open(self.out, "w") as fh:
            fh.write('{"targets": ["stale"]}')
        _, payload, _ = self.run_it(base="HEAD")
        self.assertEqual(payload["targets"], [])

    def test_jev_is_opt_in_even_when_key_is_set(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}), \
                mock.patch("diff_risk_sentinel.cli.query_jev_function") as q:
            _, payload, _ = self.run_it()
        q.assert_not_called()
        self.assertFalse(payload["meta"]["jev_enabled"])

    @staticmethod
    def fake_jev(risky):
        def answer(api_key, state, **kw):
            hot = 1.0 if state["function"] in risky else 0.0
            return {"introduces_bug": hot, "edge_cases": hot, "behavior_change": 3 * hot,
                    "semantic_risk": 3 * hot, "confidence": 0.9}
        return answer

    def test_jev_ranks_every_production_function_with_crap(self):
        self.repo.commit({"tests/test_mod.py": "def test_keep():\n    assert True\n"}, "a test")
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}), \
                mock.patch("diff_risk_sentinel.cli.query_jev_function", side_effect=self.fake_jev({"brand_new"})) as q:
            _, payload, _ = self.run_it(base="HEAD~2", jev=True, threshold_crap=1000.0, threshold_ccn=1000,
                                        threshold_delta=1000.0)
        sent = {c.args[1]["function"]: c.args[1] for c in q.call_args_list}
        # thresholds do not gate Jev, and test files are not sent
        self.assertEqual(set(sent), {"keep", "brand_new"})
        self.assertIn("if x > 1:", sent["keep"]["new_code"])
        self.assertIn("return 1", sent["keep"]["old_code"])
        self.assertEqual(sent["brand_new"]["old_code"], "")
        order = [t["function"] for t in payload["targets"]]
        # brand_new has the lower CRAP but tops every Jev dimension
        self.assertEqual(order[:2], ["brand_new", "keep"])
        scores = {t["function"]: t["triage_score"] for t in payload["targets"]}
        self.assertGreater(scores["brand_new"], scores["keep"])
        self.assertEqual(payload["targets"][-1]["function"], "test_keep")  # no Jev answer: ranks last
        self.assertTrue(all("_new_code" not in t and "_old_code" not in t for t in payload["targets"]))

    def test_jev_failures_are_reported_and_rank_last(self):
        def fail(api_key, state, **kw):
            return {"error": "boom"}
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}), \
                mock.patch("diff_risk_sentinel.cli.query_jev_function", side_effect=fail):
            _, payload, out = self.run_it(jev=True)
        self.assertEqual(payload["meta"]["jev_failures"], 2)
        self.assertIn("falha", out.lower())
        self.assertTrue(all(t["jev_semantic_risk"] is None for t in payload["targets"]))

    def test_jev_usage_is_aggregated_into_meta(self):
        usage = {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.004}

        def answer(api_key, state, **kw):
            return {"introduces_bug": 0.1, "edge_cases": 0.1, "behavior_change": 1.0,
                    "semantic_risk": 1.0, "confidence": 0.5, "usage": usage}

        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}), \
                mock.patch("diff_risk_sentinel.cli.query_jev_function", side_effect=answer):
            _, payload, _ = self.run_it(jev=True)
        meta = payload["meta"]
        self.assertEqual(meta["jev_usage"], {"input_tokens": 200, "output_tokens": 40, "cost_usd": 0.008})
        self.assertEqual(meta["jev_tokens"], 240)
        self.assertAlmostEqual(meta["jev_cost_usd"], 0.008)

    def test_coverage_report_relative_to_package_is_applied(self):
        xml = ('<?xml version="1.0"?><coverage><sources><source>{src}</source></sources><packages><package>'
               '<classes><class filename="mod.py"><lines>{lines}</lines></class></classes>'
               '</package></packages></coverage>').format(
            src=os.path.join(self.root, "src"),
            lines="".join(f'<line number="{n}" hits="1"/>' for n in range(1, 9)),
        )
        cov = os.path.join(self.root, "src", "coverage.xml")
        with open(cov, "w") as fh:
            fh.write(xml)
        _, payload, _ = self.run_it(coverage=[cov])
        keep = self.targets(payload)["keep"]
        self.assertEqual(keep["coverage"], 1.0)
        self.assertTrue(keep["coverage_known"])
        self.assertEqual(keep["crap"], 4.0)

    def test_empty_coverage_list_disables_autodetection(self):
        with open(os.path.join(self.root, "coverage.xml"), "w") as fh:
            fh.write('<?xml version="1.0"?><coverage><sources><source>.</source></sources><packages/></coverage>')
        _, payload, _ = self.run_it(coverage=[])
        self.assertEqual(payload["meta"]["coverage_reports"], [])
        _, payload, _ = self.run_it(coverage=None)
        self.assertEqual(len(payload["meta"]["coverage_reports"]), 1)

    def test_unparseable_file_is_skipped_not_counted_as_removed(self):
        self.repo.commit({"src/mod.py": "def keep(x:\n    return 1\n"}, "broken")
        _, payload, out = self.run_it()
        self.assertEqual(payload["meta"]["unparsed_files"], ["src/mod.py"])
        self.assertEqual(payload["otterwise_summary"]["removed_methods"], 0)
        self.assertEqual(payload["otterwise_summary"]["total_methods"], 0)
        self.assertIn("src/mod.py", out)

    def test_deleting_last_function_does_not_touch_previous_one(self):
        self.repo.commit({"src/tail.py": "def a(x):\n    if x:\n        return 1\n    return 0\n\n\ndef c():\n    return 2\n"}, "tail")
        self.repo.commit({"src/tail.py": "def a(x):\n    if x:\n        return 1\n    return 0\n"}, "drop c")
        _, payload, _ = self.run_it()
        self.assertNotIn("a", self.targets(payload))
        self.assertEqual(payload["otterwise_summary"]["removed_methods"], 1)

    def test_deleting_a_statement_inside_a_function_touches_it(self):
        self.repo.commit({"src/mid.py": "def a(x):\n    y = 1\n    if x:\n        return y\n    return 0\n"}, "mid")
        self.repo.commit({"src/mid.py": "def a(x):\n    if x:\n        return 1\n    return 0\n"}, "drop stmt")
        _, payload, _ = self.run_it(base="HEAD~1")
        self.assertIn("a", self.targets(payload))

    def test_renaming_a_covered_function_is_not_an_improvement(self):
        body = "    if x == 1:\n        return 1\n    return 0\n"
        self.repo.commit({"src/ren.py": "def old(x):\n" + body}, "old")
        self.repo.commit({"src/ren.py": "def new(x):\n" + body}, "renamed")
        xml = ('<?xml version="1.0"?><coverage><sources><source>{src}</source></sources><packages><package>'
               '<classes><class filename="ren.py"><lines>{lines}</lines></class></classes>'
               '</package></packages></coverage>').format(
            src=os.path.join(self.root, "src"),
            lines="".join(f'<line number="{n}" hits="1"/>' for n in range(1, 5)))
        cov = os.path.join(self.root, "ren.xml")
        with open(cov, "w") as fh:
            fh.write(xml)
        _, payload, _ = self.run_it(coverage=[cov])
        self.assertEqual(payload["otterwise_summary"]["combined_delta_crap"], 0.0)

    def test_consumers_outside_the_diff_are_listed(self):
        self.repo.commit({
            "src/keys.py": "def key_for(unit):\n    return f'quiet_hours_{unit}_v1'\n",
            "src/scope.py": "import re\n\n\ndef scope(key):\n    return re.fullmatch(r'quiet_hours_(north)_v1', key)\n",
        }, "keys")
        self.repo.commit({"src/keys.py": "def key_for(unit, kind):\n    return f'quiet_hours_{kind}_{unit}_v2'\n"}, "keys2")
        _, payload, _ = self.run_it()
        consumers = {(c["file"], c["function"]): c for c in payload["consumers"]}
        self.assertIn(("src/scope.py", "scope"), consumers)
        self.assertIn("quiet_hours_", consumers[("src/scope.py", "scope")]["tokens"])
        self.assertNotIn(("src/keys.py", "key_for"), consumers)

    def test_bad_ref_returns_error_code(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_sentinel(base="nope-ref", repo=self.root, output=self.out)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import os
import unittest

_spec = importlib.util.spec_from_file_location(
    "spec_worksheet", os.path.join(os.path.dirname(__file__), "..", "scripts", "spec_worksheet.py")
)
spec_worksheet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spec_worksheet)


def target(path, fn, **kw):
    t = {"file": path, "function": fn, "lines": "10-20", "ccn": 12, "ccn_before": 8, "coverage": 0.0,
         "coverage_known": False, "crap": 156.0, "delta_crap": 84.0, "composite_risk": 156.0,
         "action": "HIGH_RISK_REFACTOR", "strategy": "Refactor.", "diff_snippet": "+ if x:"}
    t.update(kw)
    return t


class TestSpecWorksheet(unittest.TestCase):

    def test_test_paths_are_detected(self):
        for path in ("pkg/tests/test_a.py", "src/a.test.ts", "e2e/login.spec.ts", "svc/a_test.go", "test_x.py"):
            self.assertTrue(spec_worksheet.is_test_path(path), path)
        for path in ("src/testing_utils.py", "src/contest.py", "src/spectrum.ts"):
            self.assertFalse(spec_worksheet.is_test_path(path), path)

    def test_production_targets_come_first_and_tests_are_listed_last(self):
        report = {"meta": {"base": "main", "old_rev": "a" * 40, "new_rev": "b" * 40},
                  "otterwise_summary": {}, "targets": [
                      target("tests/test_big.py", "test_everything"),
                      target("src/svc.py", "pay"),
                  ]}
        md = spec_worksheet.render(report, max_targets=8, repo=None)
        self.assertIn("## T1 · `src/svc.py::pay`", md)
        self.assertNotIn("## T2", md)
        self.assertIn("deprioritized", md)
        self.assertLess(md.index("src/svc.py::pay"), md.index("tests/test_big.py::test_everything"))
        self.assertIn("| # | Given | When | Then | Exists? |", md)

    def test_consumers_section_lists_untouched_readers(self):
        report = {"meta": {"new_rev": "b" * 40}, "otterwise_summary": {}, "targets": [],
                  "consumers": [{"file": "svc/scope.py", "function": "unit_scope", "lines": "4-6",
                                 "tokens": ["reminder_", "north"], "hits": 2}]}
        md = spec_worksheet.render(report, max_targets=8, repo=None, max_consumers=5)
        self.assertIn("## Consumers outside the diff", md)
        self.assertIn("`svc/scope.py::unit_scope`", md)
        self.assertIn("reminder_", md)

    def test_jev_triage_is_shown_when_present(self):
        t = target("src/svc.py", "pay", triage_score=0.91, jev_introduces_bug=0.62, jev_edge_cases=0.3,
                   jev_behavior_change=2.4, jev_semantic_risk=2.1)
        md = spec_worksheet.render({"meta": {"jev_enabled": True}, "otterwise_summary": {}, "targets": [t]}, 8, None)
        self.assertIn("triage 0.91", md)
        self.assertIn("Jev: bug 62%", md)

    def test_missing_coverage_is_called_out(self):
        report = {"meta": {}, "otterwise_summary": {}, "targets": []}
        self.assertIn("Coverage: **none**", spec_worksheet.render(report, 8, None))

    def test_long_snippets_are_truncated(self):
        long_diff = "\n".join(f"+ line {i}" for i in range(100))
        self.assertIn("40 more diff lines", spec_worksheet.snippet(long_diff))


if __name__ == "__main__":
    unittest.main()

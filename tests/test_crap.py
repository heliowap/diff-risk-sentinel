import unittest
from diff_risk_sentinel.crap import (
    calculate_crap,
    calculate_crap_trend,
    classify_otterwise_risk,
    compute_aggregate_metrics,
)


class TestCrapAndOtterWise(unittest.TestCase):

    def test_calculate_crap_perfect_coverage(self):
        # 100% coverage -> CRAP = ccn
        crap = calculate_crap(ccn=10, coverage=1.0)
        self.assertEqual(crap, 10.0)

    def test_calculate_crap_zero_coverage(self):
        # 0% coverage -> CRAP = ccn^2 + ccn
        crap = calculate_crap(ccn=5, coverage=0.0)
        self.assertEqual(crap, 30.0)

    def test_calculate_crap_trend(self):
        delta = calculate_crap_trend(initial_crap=50.0, final_crap=20.0)
        self.assertEqual(delta, -30.0)

        delta_new = calculate_crap_trend(initial_crap=None, final_crap=15.5)
        self.assertEqual(delta_new, 15.5)

    def test_classify_otterwise_risk(self):
        b1, _ = classify_otterwise_risk(ccn=5, crap=10.0, delta_crap=-15.0, jev_semantic_risk=0.5)
        self.assertEqual(b1, "BENEFICIAL_REFACTOR")

        b2, _ = classify_otterwise_risk(ccn=5, crap=10.0, delta_crap=2.0, jev_semantic_risk=2.4)
        self.assertEqual(b2, "CRITICAL_SEMANTIC_AUDIT")

        b3, _ = classify_otterwise_risk(ccn=25, crap=75.0, delta_crap=5.0, jev_semantic_risk=1.2)
        self.assertEqual(b3, "HIGH_RISK_REFACTOR")

        b4, _ = classify_otterwise_risk(ccn=10, crap=45.0, delta_crap=5.0, jev_semantic_risk=1.0)
        self.assertEqual(b4, "NEEDS_ATTENTION_TESTS")

        b5, _ = classify_otterwise_risk(ccn=3, crap=5.0, delta_crap=0.0, jev_semantic_risk=0.2)
        self.assertEqual(b5, "ACCEPTABLE_LOW_RISK")

    def test_negative_delta_never_fast_passes_a_red_zone_method(self):
        badge, _ = classify_otterwise_risk(ccn=568, crap=323192.0, delta_crap=-1.0, jev_semantic_risk=1.9)
        self.assertEqual(badge, "HIGH_RISK_REFACTOR")

    def test_negative_delta_in_yellow_zone_still_needs_tests(self):
        badge, _ = classify_otterwise_risk(ccn=6, crap=42.0, delta_crap=-8.0, jev_semantic_risk=0.1)
        self.assertEqual(badge, "NEEDS_ATTENTION_TESTS")

    def test_moderate_semantic_risk_is_not_acceptable(self):
        badge, _ = classify_otterwise_risk(ccn=3, crap=5.0, delta_crap=2.0, jev_semantic_risk=1.5)
        self.assertEqual(badge, "SEMANTIC_REVIEW")

    def test_unknown_semantic_risk_falls_back_to_structural_rules(self):
        badge, _ = classify_otterwise_risk(ccn=3, crap=5.0, delta_crap=0.0, jev_semantic_risk=None)
        self.assertEqual(badge, "ACCEPTABLE_LOW_RISK")

    def test_compute_aggregate_metrics(self):
        items = [
            {"crap": 33.0, "crap_before": 28.0},
            {"crap": 10.0, "crap_before": 12.0},
            {"crap": 6.0, "crap_before": None},  # new method
        ]
        removed = [{"crap_before": 20.0}]
        metrics = compute_aggregate_metrics(items, removed)
        self.assertEqual(metrics["total_methods"], 3)
        self.assertEqual(metrics["new_methods"], 1)
        self.assertEqual(metrics["removed_methods"], 1)
        self.assertEqual(metrics["combined_crap"], 49.0)
        self.assertEqual(metrics["combined_crap_before"], 60.0)
        self.assertEqual(metrics["combined_delta_crap"], -11.0)
        self.assertEqual(metrics["average_crap"], 16.3)
        self.assertEqual(metrics["average_crap_before"], 20.0)
        self.assertEqual(metrics["average_delta_crap"], -3.7)

    def test_compute_aggregate_metrics_empty(self):
        metrics = compute_aggregate_metrics([])
        self.assertEqual(metrics["total_methods"], 0)
        self.assertEqual(metrics["average_delta_crap"], 0.0)


if __name__ == "__main__":
    unittest.main()

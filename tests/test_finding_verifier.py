import unittest
from unittest.mock import patch

from diff_risk_sentinel.finding_verifier import (
    finding_state,
    verify_finding_with_jev,
    FINDING_VERIFIER_QUESTIONS,
)
from evals.review_cost.models import Finding


class TestFindingVerifier(unittest.TestCase):

    def test_finding_state_packaging(self):
        f = Finding(
            file="src/billing.py",
            line=45,
            function="calculate_total",
            claim="Empty list passed to min() causes ValueError",
            severity="critical",
        )
        code_excerpt = "def calculate_total(items):\n    return min(items.prices)\n"
        state = finding_state(f, code_excerpt)
        self.assertEqual(state["file"], "src/billing.py")
        self.assertEqual(state["line"], 45)
        self.assertIn("Empty list", state["claim"])
        self.assertIn("min(items.prices)", state["cited_code"])

    @patch("diff_risk_sentinel.finding_verifier.ask_jev")
    def test_verify_valid_finding(self, mock_ask):
        mock_ask.return_value = {
            "answers": {
                "claim_supported_by_code": {"noul": 0.95},
                "severity": {"choice": "critical"},
            }
        }
        f = Finding(
            file="src/calc.py",
            line=10,
            function="div",
            claim="ZeroDivisionError when b == 0",
            severity="critical",
        )
        res = verify_finding_with_jev("fake-key", f, "def div(a, b):\n    return a / b\n")
        self.assertTrue(res["is_valid"])
        self.assertEqual(res["calibrated_severity"], "critical")
        self.assertEqual(res["verdict"], "VALID")

    @patch("diff_risk_sentinel.finding_verifier.ask_jev")
    def test_verify_false_alarm_finding(self, mock_ask):
        mock_ask.return_value = {
            "answers": {
                "claim_supported_by_code": {"noul": 0.12},
                "severity": {"choice": "false_alarm"},
            }
        }
        f = Finding(
            file="src/auth.py",
            line=20,
            function="login",
            claim="Password logged in plaintext",
            severity="critical",
        )
        res = verify_finding_with_jev(
            "fake-key",
            f,
            "def login(user, password):\n    logger.info('User %s logged in', user)\n",
        )
        self.assertFalse(res["is_valid"])
        self.assertEqual(res["calibrated_severity"], "false_alarm")
        self.assertEqual(res["verdict"], "FALSE_ALARM")

    @patch("diff_risk_sentinel.finding_verifier.ask_jev")
    def test_verify_error_is_unknown_not_false_alarm(self, mock_ask):
        mock_ask.return_value = {"error": "HTTP 503", "usage": {"input_tokens": 12}}
        f = Finding(
            file="src/calc.py",
            line=10,
            function="div",
            claim="ZeroDivisionError when b == 0",
            severity="critical",
        )
        res = verify_finding_with_jev("fake-key", f, "def div(a, b):\n    return a / b\n")
        self.assertEqual(res["verdict"], "UNKNOWN")
        self.assertIsNone(res["is_valid"])
        self.assertIsNone(res["support_probability"])
        self.assertEqual(res["error"], "HTTP 503")
        self.assertEqual(res["usage"], {"input_tokens": 12})

    @patch("diff_risk_sentinel.finding_verifier.ask_jev")
    def test_verify_records_real_jev_usage(self, mock_ask):
        mock_ask.return_value = {
            "answers": {
                "claim_supported_by_code": {"noul": 0.9},
                "severity": {"choice": "major"},
            },
            "usage": {"input_tokens": 370, "output_tokens": 60, "cost_usd": 0.0008},
        }
        f = Finding(
            file="src/calc.py",
            line=10,
            function="div",
            claim="ZeroDivisionError when b == 0",
            severity="critical",
        )
        res = verify_finding_with_jev("fake-key", f, "def div(a, b):\n    return a / b\n")
        self.assertEqual(res["usage"],
                         {"input_tokens": 370, "output_tokens": 60, "cost_usd": 0.0008})

import unittest
from unittest.mock import patch

from diff_risk_sentinel.consumer_verifier import (
    consumer_state,
    verify_consumer_with_jev,
    CONSUMER_QUESTIONS,
)


class TestConsumerVerifier(unittest.TestCase):

    def test_consumer_state_packaging(self):
        state = consumer_state(
            token="calc_tax",
            producer_file="src/billing.py",
            producer_diff="-def calc_tax(amount):\n+def calc_tax(amount, currency):",
            consumer_file="src/orders.py",
            consumer_function="checkout",
            consumer_code="def checkout(order):\n    tax = calc_tax(order.amount)\n    return order.amount + tax\n",
        )
        self.assertEqual(state["changed_token"], "calc_tax")
        self.assertIn("calc_tax(amount, currency)", state["producer_diff"])
        self.assertIn("calc_tax(order.amount)", state["consumer_code"])
        self.assertEqual(state["consumer_file"], "src/orders.py")

    @patch("diff_risk_sentinel.consumer_verifier.ask_jev")
    def test_verify_consumer_broken(self, mock_ask):
        # Mock Jev responding with high probability of broken contract
        mock_ask.return_value = {
            "answers": {
                "contract_broken": {"noul": 0.92},
                "explanation": {"score": 2},
            }
        }
        res = verify_consumer_with_jev(
            api_key="fake-key",
            token="calc_tax",
            producer_file="src/billing.py",
            producer_diff="-def calc_tax(amount):\n+def calc_tax(amount, currency):",
            consumer_file="src/orders.py",
            consumer_function="checkout",
            consumer_code="def checkout(order):\n    tax = calc_tax(order.amount)\n",
        )
        self.assertTrue(res["is_broken"])
        self.assertAlmostEqual(res["broken_probability"], 0.92)
        self.assertEqual(res["verdict"], "PROBABLE_CONTRACT_BREAK")

    @patch("diff_risk_sentinel.consumer_verifier.ask_jev")
    def test_verify_consumer_compatible(self, mock_ask):
        mock_ask.return_value = {
            "answers": {
                "contract_broken": {"noul": 0.08},
                "explanation": {"score": 0},
            }
        }
        res = verify_consumer_with_jev(
            api_key="fake-key",
            token="STATUS_OK",
            producer_file="src/constants.py",
            producer_diff="+STATUS_OK = 'OK'",
            consumer_file="src/orders.py",
            consumer_function="is_ready",
            consumer_code="def is_ready(status):\n    return status == STATUS_OK\n",
        )
        self.assertFalse(res["is_broken"])
        self.assertAlmostEqual(res["broken_probability"], 0.08)
        self.assertEqual(res["verdict"], "COMPATIBLE")

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflow"))
from calculate_cost import calculate_cost

RULE = {"coverage_status": "covered", "needs_review": False, "deductible_applies": False,
        "copay_usd": 25, "patient_pays_percentage": 20, "plan_pays_percentage": 80}


class CombinedCopayCoinsurance(unittest.TestCase):
    def test_copay_then_coinsurance_on_rest(self):
        self.assertEqual(calculate_cost(RULE, allowed_amount=200)["patient_cost"], 60.0)  # 25 + 175 * 20%

    def test_amount_below_copay(self):
        self.assertEqual(calculate_cost(RULE, allowed_amount=10)["patient_cost"], 10.0)

    def test_needs_allowed_amount(self):
        self.assertEqual(calculate_cost(RULE)["missing_fields"], ["allowed_amount"])

    def test_deductible_still_needs_review(self):
        self.assertEqual(calculate_cost({**RULE, "deductible_applies": True}, allowed_amount=200)["status"],
                         "needs_review")


if __name__ == "__main__":
    unittest.main()

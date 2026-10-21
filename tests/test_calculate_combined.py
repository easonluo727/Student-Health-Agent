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


COPAY_AFTER_DEDUCTIBLE = {"coverage_status": "covered", "needs_review": False, "deductible_applies": True,
                          "copay_usd": 60, "patient_pays_percentage": None, "plan_pays_percentage": None}


class CopayAfterDeductible(unittest.TestCase):
    def test_part_of_the_deductible_left(self):
        self.assertEqual(calculate_cost(COPAY_AFTER_DEDUCTIBLE, 300, 100)["patient_cost"], 160.0)

    def test_deductible_met(self):
        self.assertEqual(calculate_cost(COPAY_AFTER_DEDUCTIBLE, 300, 0)["patient_cost"], 60.0)

    def test_bill_inside_the_deductible(self):
        self.assertEqual(calculate_cost(COPAY_AFTER_DEDUCTIBLE, 300, 5000)["patient_cost"], 300.0)

    def test_needs_both_amounts(self):
        self.assertEqual(calculate_cost(COPAY_AFTER_DEDUCTIBLE, 300)["missing_fields"], ["remaining_deductible"])
        self.assertEqual(calculate_cost(COPAY_AFTER_DEDUCTIBLE)["missing_fields"],
                         ["allowed_amount", "remaining_deductible"])


FLAT_COPAY = {"coverage_status": "covered", "needs_review": False, "deductible_applies": False,
              "copay_usd": 50, "patient_pays_percentage": None, "plan_pays_percentage": None}


class FlatCopay(unittest.TestCase):
    def test_copay_never_exceeds_the_price(self):  # Covered California endnote 6: the lesser amount
        self.assertEqual(calculate_cost(FLAT_COPAY, 9)["patient_cost"], 9.0)

    def test_copay_below_the_price(self):
        self.assertEqual(calculate_cost(FLAT_COPAY, 300)["patient_cost"], 50.0)

    def test_without_a_price_the_copay_is_the_most(self):
        self.assertEqual(calculate_cost(FLAT_COPAY)["patient_cost"], 50.0)


if __name__ == "__main__":
    unittest.main()

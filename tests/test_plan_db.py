"""plan_db on the committed data (data/plans/policy.sqlite + data/mrf/allowed_amounts.csv)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflow"))
from plan_db import PlanDB  # noqa: E402

UCSHIP = "anthem_ppo_ucship_ucla_2026_27"


class PlanDbOnRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PlanDB()

    def test_coinsurance_on_the_network_rate(self):
        r = self.db.estimate(UCSHIP, "knee_mri", "in", "UC Family")
        self.assertEqual((r["status"], r["patient_cost"]), ("estimated", 104.71))  # 10% of 1,047.10
        self.assertEqual(r["allowed_amount"]["total"], 1047.1)

    def test_components_under_different_benefits(self):
        r = self.db.estimate(UCSHIP, "er_ankle_xray", "in", "UC Family")
        # $250 ER copay (facility + physician) + 20% of the X-ray components (37.26 + 10.63)
        self.assertEqual(r["patient_cost"], 259.58)
        self.assertEqual([line["service"] for line in r["lines"]], ["emergency_room", "xray"])

    def test_tiered_plan_needs_a_tier_and_a_deductible_when_it_applies(self):
        r = self.db.estimate(UCSHIP, "knee_mri", "in", None)
        self.assertEqual((r["status"], r["missing_fields"]), ("needs_information", ["network_tier"]))
        self.assertEqual(self.db.estimate(UCSHIP, "knee_mri", "in", "Network")["missing_fields"],
                         ["remaining_deductible"])

    def test_campus_clinic_tier_has_no_rates_but_flat_copays_work(self):
        self.assertEqual(self.db.estimate(UCSHIP, "office_visit_established", "in", "Ashe Center")["patient_cost"], 0)
        self.assertEqual(self.db.estimate(UCSHIP, "knee_mri", "in", "Ashe Center")["status"], "needs_review")

    def test_copay_plan(self):
        r = self.db.estimate("healthnet_silver70_hmo_2026", "specialist_followup")
        self.assertEqual((r["status"], r["patient_cost"]), ("estimated", 90))

    def test_compare_ranks_by_cost_with_the_deductible_not_met(self):
        r = self.db.compare(["healthnet_silver70_hmo_2026", "healthnet_platinum90_hmo_2026"],
                            ["office_visit_established", "specialist_followup"])
        ranks = {e["plan_id"]: e["rank"] for e in r["plans"]}
        self.assertEqual(ranks, {"healthnet_platinum90_hmo_2026": 1, "healthnet_silver70_hmo_2026": 2})

    def test_unknown_inputs(self):
        self.assertEqual(self.db.estimate("nope", "knee_mri")["status"], "invalid_input")
        self.assertEqual(self.db.estimate(UCSHIP, "nope")["status"], "invalid_input")


if __name__ == "__main__":
    unittest.main()

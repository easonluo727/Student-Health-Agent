"""plan_db on the committed policy database and a small rate store built here (no 3 GB file, no LLM)."""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
sys.path.insert(0, str(ROOT / "scripts" / "mrf"))
import mrf_rate_store  # noqa: E402
from plan_db import PlanDB  # noqa: E402

UCSHIP = "anthem_ppo_ucship_ucla_2026_27"
PPO, HEALTHNET = "anthem_ca_prudent_buyer_ppo", "healthnet_ambetter_ca"

# plan_key -> [(npi, billing_code, billing_class, place_of_service, modifier, rate)]. One provider group per NPI.
RATES = {
    PPO: [
        ("1000000001", "73721", "professional", "11", "", 900.0),   # knee MRI: median of 900, 1000, 1100 = 1000
        ("1000000002", "73721", "professional", "11", "", 1000.0),
        ("1000000003", "73721", "professional", "22", "", 1100.0),
        ("1000000001", "99284", "professional", "23", "", 500.0),   # ER physician
        ("1000000004", "99284", "institutional", "", "", 2000.0),  # ER facility
        ("1000000002", "73610", "professional", "23", "26", 40.0),  # X-ray read (professional component)
        ("1000000004", "73610", "institutional", "", "", 60.0),    # X-ray facility
    ],
    HEALTHNET: [
        ("2000000001", "73721", "professional", "11", "", 400.0),   # knee MRI: median of 400, 450, 500 = 450
        ("2000000002", "73721", "professional", "11", "", 450.0),
        ("2000000003", "73721", "professional", "11", "", 500.0),
    ],
}


def build_rate_store(path):
    con = mrf_rate_store.connect(path)
    for source_id, (plan_key, rows) in enumerate(RATES.items(), 1):
        con.execute("INSERT INTO mrf_sources (source_id, source_url, insurer, plan_key, file_date, status) "
                    "VALUES (?, ?, 'Test', ?, '2026-10-01', 'complete')", (source_id, f"https://test/{plan_key}", plan_key))
        for npi, code, cls, pos, mod, rate in rows:
            con.execute("INSERT INTO negotiated_rates (source_id, billing_code, provider_group_id, negotiated_rate, "
                        "negotiated_type, expiration_date, billing_class, service_codes, modifiers) "
                        "VALUES (?, ?, ?, ?, 'negotiated', '9999-12-31', ?, ?, ?)",
                        (source_id, code, npi, rate, cls, pos, mod))
            con.execute("INSERT OR IGNORE INTO provider_members (source_id, provider_group_id, npi, tier) "
                        "VALUES (?, ?, ?, 'B')", (source_id, npi, npi))
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")  # so the read-only connection needs no -wal file
    con.close()


class PlanDbOnFixtureRates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        rates = os.path.join(cls.tmp, "rates.sqlite")
        build_rate_store(rates)
        cls.db = PlanDB(rates_path=Path(rates))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_coinsurance_on_the_network_median(self):
        r = self.db.estimate(UCSHIP, "knee_mri", "in", "UC Family")
        self.assertEqual((r["status"], r["patient_cost"]), ("estimated", 100.0))  # 10% of 1,000
        prices = r["allowed_amount"]
        self.assertEqual((prices["total"], prices["low"], prices["high"]), (1000.0, 920.0, 1080.0))
        self.assertEqual(prices["basis"], "la_county_median")
        self.assertEqual(prices["parts"][0]["n_providers"], 3)

    def test_components_under_different_benefits(self):
        r = self.db.estimate(UCSHIP, "er_ankle_xray", "in", "UC Family")
        # $250 ER copay (facility + physician) + 20% of the X-ray components (40 read + 60 facility)
        self.assertEqual(r["patient_cost"], 270.0)
        self.assertEqual([line["service"] for line in r["lines"]], ["emergency_room", "xray"])

    def test_deductible_then_coinsurance(self):
        not_met = self.db.estimate("healthnet_bronze60_hmo_2026", "knee_mri", remaining_deductible=5800)
        self.assertEqual(not_met["patient_cost"], 450.0)  # the whole $450 counts toward the deductible
        met = self.db.estimate("healthnet_bronze60_hmo_2026", "knee_mri", remaining_deductible=0)
        self.assertEqual(met["patient_cost"], 180.0)      # 40% of 450

    def test_missing_price_names_the_component(self):
        r = self.db.estimate("lacare_bronze60_hmo_2026", "knee_mri", remaining_deductible=0)
        self.assertEqual(r["status"], "needs_review")
        self.assertIn("mri", r["message"])

    def test_missing_price_does_not_block_a_flat_copay(self):
        r = self.db.estimate("lacare_silver70_hmo_2026", "specialist_followup")
        self.assertEqual((r["status"], r["patient_cost"]), ("estimated", 90))

    def test_unconfirmed_network_link_is_not_priced(self):
        # anthem_silver70_epo_2026 -> CA EPO Pathway is inferred from the plan type only (confirmed = 0).
        prices = self.db.allowed_amounts("anthem_silver70_epo_2026", None, "knee_mri")
        self.assertEqual(prices["status"], "no_rates")
        self.assertIn("not confirmed", prices["message"])

    def test_tiered_plan_needs_a_tier_and_a_deductible_when_it_applies(self):
        r = self.db.estimate(UCSHIP, "knee_mri", "in", None)
        self.assertEqual((r["status"], r["missing_fields"]), ("needs_information", ["network_tier"]))
        self.assertEqual(self.db.estimate(UCSHIP, "knee_mri", "in", "Network")["missing_fields"],
                         ["remaining_deductible"])

    def test_campus_clinic_tier_has_no_rates_but_flat_copays_work(self):
        self.assertEqual(self.db.estimate(UCSHIP, "office_visit_established", "in", "Ashe Center")["patient_cost"], 0)
        self.assertEqual(self.db.estimate(UCSHIP, "knee_mri", "in", "Ashe Center")["status"], "needs_review")

    def test_compare_ranks_by_cost_with_the_deductible_not_met(self):
        r = self.db.compare(["healthnet_silver70_hmo_2026", "healthnet_platinum90_hmo_2026"],
                            ["office_visit_established", "specialist_followup"])
        ranks = {e["plan_id"]: e["rank"] for e in r["plans"]}
        self.assertEqual(ranks, {"healthnet_platinum90_hmo_2026": 1, "healthnet_silver70_hmo_2026": 2})

    def test_unknown_inputs(self):
        self.assertEqual(self.db.estimate("nope", "knee_mri")["status"], "invalid_input")
        self.assertEqual(self.db.estimate(UCSHIP, "nope")["status"], "invalid_input")


class PlanDbWithoutRateStore(unittest.TestCase):
    def test_flat_copays_still_work(self):
        db = PlanDB(rates_path=Path("/nonexistent/rates.sqlite"))
        self.assertEqual(db.estimate("healthnet_silver70_hmo_2026", "specialist_followup")["patient_cost"], 90)
        self.assertEqual(db.allowed_amounts("healthnet_silver70_hmo_2026", None, "knee_mri")["status"], "no_rates")


if __name__ == "__main__":
    unittest.main()

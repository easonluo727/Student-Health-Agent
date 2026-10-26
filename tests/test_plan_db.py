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
        ("2000000001", "99213", "professional", "02", "", 90.0),    # telehealth: no 98005 rate, 99213 at POS 02
        ("2000000002", "87804", "professional", "20", "", 9.0),     # rapid flu test, below the $50 lab copay
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

    def test_thin_price_samples_are_flagged(self):
        prices = self.db.allowed_amounts(UCSHIP, "UC Family", "er_ankle_xray")
        # Every fixture component comes from one or two providers.
        self.assertEqual(len(prices["warnings"]), 4)
        self.assertIn("er_facility", " ".join(prices["warnings"]))

    def test_components_under_different_benefits(self):
        r = self.db.estimate(UCSHIP, "er_ankle_xray", "in", "UC Family")
        # $250 ER copay (facility + physician) + 20% of the X-ray components (40 read + 60 facility)
        self.assertEqual(r["patient_cost"], 270.0)
        self.assertEqual([line["service"] for line in r["lines"]], ["emergency_room", "xray"])

    def test_deductible_then_coinsurance(self):
        # UC SHIP Network tier: imaging is 20% after the $400 deductible; the knee MRI is $1,000.
        not_met = self.db.estimate(UCSHIP, "knee_mri", "in", "Network", remaining_deductible=400)
        self.assertEqual(not_met["patient_cost"], 520.0)  # $400 toward the deductible, then 20% of the other $600
        met = self.db.estimate(UCSHIP, "knee_mri", "in", "Network", remaining_deductible=0)
        self.assertEqual(met["patient_cost"], 200.0)      # 20% of 1,000

    def test_missing_price_names_the_component(self):
        r = self.db.estimate("lacare_bronze60_hmo_2026", "knee_mri", remaining_deductible=0)
        self.assertEqual(r["status"], "needs_review")
        self.assertIn("mri", r["message"])

    def test_missing_price_does_not_block_a_flat_copay(self):
        r = self.db.estimate("lacare_silver70_hmo_2026", "specialist_followup")
        self.assertEqual((r["status"], r["patient_cost"]), ("estimated", 90))

    def test_unconfirmed_network_link_is_not_priced(self):
        policy = os.path.join(self.tmp, "policy.sqlite")
        shutil.copy(ROOT / "data" / "plans" / "policy.sqlite", policy)
        with sqlite3.connect(policy) as con:
            con.execute("UPDATE plan_networks SET confirmed = 0 WHERE plan_id = 'healthnet_silver70_hmo_2026'")
        con.close()
        db = PlanDB(Path(policy), Path(self.tmp) / "rates.sqlite")
        prices = db.allowed_amounts("healthnet_silver70_hmo_2026", None, "knee_mri")
        self.assertEqual(prices["status"], "no_rates")
        self.assertIn("not confirmed", prices["message"])
        self.assertIsNotNone(self.db.allowed_amounts("healthnet_silver70_hmo_2026", None, "knee_mri")["total"])

    def test_telehealth_falls_back_to_the_office_visit_code(self):
        prices = self.db.allowed_amounts("healthnet_silver70_hmo_2026", None, "telehealth_visit")
        self.assertEqual((prices["total"], prices["parts"][0]["billing_code"]), (90.0, "99213"))

    def test_ashe_center_flu_visit_is_a_primary_care_visit(self):
        r = self.db.estimate(UCSHIP, "urgent_care_flu", "in", "Ashe Center")
        self.assertEqual((r["lines"][0]["service"], r["lines"][0]["patient_cost"]), ("primary_care_visit", 0))
        self.assertEqual(r["status"], "needs_review")  # the flu test is 10% of a price the campus clinic doesn't publish

    def test_copay_is_capped_at_the_price(self):
        # Health Net Silver 70: $50 urgent care copay + $50 lab copay, but the $9 flu test costs its price
        # (Covered California endnote 6). The visit has no price here, so its copay is the most.
        r = self.db.estimate("healthnet_silver70_hmo_2026", "urgent_care_flu")
        self.assertEqual([line["patient_cost"] for line in r["lines"]], [50, 9.0])
        self.assertEqual(r["patient_cost"], 59.0)

    def test_tiered_plan_needs_a_tier_and_a_deductible_when_it_applies(self):
        r = self.db.estimate(UCSHIP, "knee_mri", "in", None)
        self.assertEqual((r["status"], r["missing_fields"]), ("needs_information", ["network_tier"]))
        self.assertEqual(self.db.estimate(UCSHIP, "knee_mri", "in", "Network")["missing_fields"],
                         ["remaining_deductible"])

    def test_campus_clinic_tier_has_no_rates_but_flat_copays_work(self):
        self.assertEqual(self.db.estimate(UCSHIP, "office_visit_established", "in", "Ashe Center")["patient_cost"], 0)
        self.assertEqual(self.db.estimate(UCSHIP, "knee_mri", "in", "Ashe Center")["status"], "needs_review")

    def test_a_year_uses_the_deductible_up_once_across_visits(self):
        plan = self.db.plan(UCSHIP)
        year = self.db.year(plan, "Network", {"knee_mri": (3, False)})
        # First MRI: the $400 deductible, then 20% of the other $600; later ones: 20% of $1,000.
        visit = year["visits"][0]
        self.assertEqual(visit["runs"], [{"times": 1, "patient_cost": 520.0}, {"times": 2, "patient_cost": 200.0}])
        self.assertEqual(visit["math"], "1 x $520.00 + 2 x $200.00 = $920.00")
        self.assertEqual(visit["lines"][0]["rule"], "20% coinsurance after the deductible")
        self.assertEqual(year["care"]["total"], 920.0)
        self.assertEqual((year["deductible"]["amount"], year["deductible"]["source_page"]), (400.0, 1))

    def test_a_year_is_capped_at_the_out_of_pocket_max(self):
        year = self.db.year(self.db.plan(UCSHIP), "Network", {"knee_mri": (50, False)})
        self.assertEqual(year["care"]["before_cap"], 520 + 49 * 200)
        self.assertEqual((year["care"]["total"], year["care"]["capped"]), (9000.0, True))
        self.assertIn("out-of-pocket max", year["care"]["message"])

    def test_compare_counts_each_visit_as_often_as_the_user_said(self):
        r = self.db.compare(["healthnet_silver70_hmo_2026"], ["office_visit_established", "preventive_visit"],
                            visits={"office_visit_established": 10, "urgent_care_flu": 2})
        self.assertEqual([(v["scenario"], v["times"], v["assumed"]) for v in r["visits"]],
                         [("office_visit_established", 10, False), ("preventive_visit", 1, True),
                          ("urgent_care_flu", 2, False)])
        visits = {v["scenario"]: v for v in r["plans"][0]["visits"]}
        self.assertEqual(visits["office_visit_established"]["math"], "10 x $50.00 = $500.00")  # SBC p. 2
        self.assertIn("assumed", r["note"])
        # A count of 0 drops the visit even when the model chose it.
        r = self.db.compare(["healthnet_silver70_hmo_2026"], ["office_visit_established"],
                            visits={"office_visit_established": 0})
        self.assertEqual(r["visits"], [])

    def test_a_tiered_plan_is_priced_in_its_cheapest_tier(self):
        r = self.db.compare([UCSHIP], ["knee_mri"], visits={"knee_mri": 2})
        entry = r["plans"][0]
        self.assertEqual((entry["network_tier"], entry["care"]["total"]), ("UC Family", 200.0))
        self.assertIn("cheapest of its priced tiers", entry["tier_note"])

    def test_a_preferred_campus_clinic_prices_the_visits_it_can(self):
        # Someone who keeps going to the Ashe Center: primary care there is $0 (SBC p. 2), not UC Family's $25.
        r = self.db.compare([UCSHIP], ["office_visit_established", "knee_mri"],
                            visits={"office_visit_established": 10, "knee_mri": 1}, preferred_tiers=["Ashe Center"])
        entry = r["plans"][0]
        visits = {v["scenario"]: v for v in entry["visits"]}
        self.assertEqual((visits["office_visit_established"]["network_tier"],
                          visits["office_visit_established"]["total"]), ("Ashe Center", 0.0))
        # An MRI needs a price, which the campus clinic has none of: it falls back to the priced tier, and says so.
        self.assertEqual(visits["knee_mri"]["network_tier"], "UC Family")
        self.assertIn("can't price", visits["knee_mri"]["tier_note"])
        self.assertEqual(entry["care"]["total"], 100.0)
        self.assertIn("Ashe Center", entry["tier_note"])
        # Without the preference, primary care is priced at UC Family.
        plain = self.db.compare([UCSHIP], ["office_visit_established"], visits={"office_visit_established": 10})
        self.assertEqual(plain["plans"][0]["care"]["total"], 250.0)

    def test_compare_without_ratings_ranks_by_the_year_total(self):
        plans, visits = ["healthnet_silver70_hmo_2026", "healthnet_platinum90_hmo_2026"], ["office_visit_established",
                                                                                          "specialist_followup"]
        r = self.db.compare(plans, visits, {"age": 40, "zip": "90024", "student_level": None})
        self.assertEqual((r["ranked_by"], r["missing_fields"]), ("year_total", []))
        by_id = {e["plan_id"]: e for e in r["plans"]}
        silver = by_id["healthnet_silver70_hmo_2026"]
        self.assertEqual(silver["premium"]["annual"], 508 * 12)  # booklet p. 25, region 16, age 40
        self.assertEqual(silver["year_total"], round(silver["premium"]["annual"] + silver["care"]["total"], 2))
        # Platinum is cheaper per visit but $197 a month more: with premiums, Silver comes first.
        self.assertEqual({p: e["rank"] for p, e in by_id.items()},
                         {"healthnet_silver70_hmo_2026": 1, "healthnet_platinum90_hmo_2026": 2})
        self.assertNotIn("score", silver)

    def test_ratings_rank_by_a_weighted_score_that_adds_up(self):
        plans = ["healthnet_silver70_hmo_2026", "healthnet_platinum90_hmo_2026"]
        profile = {"age": 40, "zip": "90024", "student_level": None}
        visits = {"office_visit_established": 12}
        # Someone who only cares about low visit costs: Platinum ($15 copay vs $50) comes first.
        r = self.db.compare(plans, [], profile, visits,
                            {"weights": {"premium": 1, "visit_costs": 5, "worst_case": 1, "keep_doctors": None}})
        self.assertEqual(r["ranked_by"], "weighted_score")
        by_id = {e["plan_id"]: e for e in r["plans"]}
        platinum, silver = by_id["healthnet_platinum90_hmo_2026"], by_id["healthnet_silver70_hmo_2026"]
        self.assertEqual((platinum["rank"], platinum["score"]["total"]), (1, 100.0))
        self.assertEqual((silver["rank"], silver["score"]["total"]), (2, 0.0))
        factors = {f["factor"]: f for f in r["ranking"]["factors"]}
        self.assertEqual((factors["visit_costs"]["weight"], factors["premium"]["used"]), (4, False))
        self.assertFalse(factors["keep_doctors"]["used"])  # no provider-network data yet: said, not guessed
        # Someone who only cares about the premium: Silver comes first.
        r = self.db.compare(plans, [], profile, visits, {"weights": {"premium": 5, "visit_costs": 1, "worst_case": 1}})
        self.assertEqual({e["plan_id"]: e["rank"] for e in r["plans"]},
                         {"healthnet_silver70_hmo_2026": 1, "healthnet_platinum90_hmo_2026": 2})
        # Unrated priorities count as 3; each plan's contributions add up to its score.
        r = self.db.compare(plans, [], profile, visits, {"weights": {"premium": 4}})
        for e in r["plans"]:
            self.assertAlmostEqual(e["score"]["total"], sum(p["contribution"] for p in e["score"]["parts"]), places=1)
        self.assertEqual({f["factor"]: f["weight"] for f in r["ranking"]["factors"]},
                         {"premium": 3, "visit_costs": 2, "worst_case": 2, "keep_doctors": 0})

    def test_plans_over_the_monthly_budget_rank_last(self):
        plans = ["healthnet_silver70_hmo_2026", "healthnet_platinum90_hmo_2026"]
        r = self.db.compare(plans, [], {"age": 40, "zip": "90024", "student_level": None},
                            {"office_visit_established": 12}, {"weights": {"visit_costs": 5}, "monthly_budget": 600})
        by_id = {e["plan_id"]: e for e in r["plans"]}
        self.assertTrue(by_id["healthnet_platinum90_hmo_2026"]["budget"]["over"])  # $705 a month
        self.assertEqual(by_id["healthnet_platinum90_hmo_2026"]["rank"], 2)
        self.assertIn("budget", r["ranking"]["summary"])

    def test_compare_without_the_profile_ranks_visits_only_and_names_what_is_missing(self):
        r = self.db.compare([UCSHIP, "healthnet_silver70_hmo_2026"], ["office_visit_established"])
        self.assertEqual((r["ranked_by"], r["missing_fields"]), ("visits_only", ["age", "student_level", "zip"]))
        self.assertIn("not a recommendation", r["note"])

    def test_the_arena_report_shows_how_each_number_was_computed(self):
        from arena_report import arena_report
        plans = ["healthnet_silver70_hmo_2026", "healthnet_platinum90_hmo_2026"]
        r = self.db.compare(plans, ["preventive_visit"], {"age": 40, "zip": "90024", "student_level": None},
                            {"office_visit_established": 10}, {"weights": {"visit_costs": 5}})
        report = arena_report(r)
        self.assertEqual(report["ranking"]["method"], "weighted_score")
        rows = {row["key"]: row for row in report["comparison"]}
        cell = rows["visit.office_visit_established"]["cells"]["healthnet_silver70_hmo_2026"]
        self.assertEqual(cell["text"], "$500.00")
        self.assertIn("10 x $50.00 = $500.00", cell["detail"])
        self.assertEqual(cell["source"]["kind"], "policy")
        self.assertIn("(assumed once)", rows["visit.preventive_visit"]["label"])
        self.assertIn("score.visit_costs", rows)
        self.assertEqual(rows["oop_max"]["cells"]["healthnet_silver70_hmo_2026"]["source"]["page"], 1)
        self.assertEqual({s["key"] for s in report["scenarios"]}, {"expected", "healthy", "worst"})
        self.assertEqual(report["recommendation"]["plan_id"], next(e["plan_id"] for e in r["plans"] if e["rank"] == 1))

    def test_unknown_inputs(self):
        self.assertEqual(self.db.estimate("nope", "knee_mri")["status"], "invalid_input")
        self.assertEqual(self.db.estimate(UCSHIP, "nope")["status"], "invalid_input")


class PlanDbWithoutRateStore(unittest.TestCase):
    def test_flat_copays_still_work(self):
        db = PlanDB(rates_path=Path("/nonexistent/rates.sqlite"))
        db.warm()  # startup remains available when the optional rate store is absent
        self.assertEqual(db.estimate("healthnet_silver70_hmo_2026", "specialist_followup")["patient_cost"], 90)
        self.assertEqual(db.allowed_amounts("healthnet_silver70_hmo_2026", None, "knee_mri")["status"], "no_rates")


class Premiums(unittest.TestCase):
    """Premiums from the committed policy database (data/plans/premiums.csv, parsed from the official PDFs)."""
    db = PlanDB(rates_path=Path("/nonexistent/rates.sqlite"))

    def test_published_ages_are_used_as_printed(self):
        r = self.db.premium("lacare_bronze60_hmo_2026", 40, "90024")  # region 16 (Westwood)
        self.assertEqual((r["status"], r["amount"], r["annual"], r["scaled_from_age"]), ("priced", 367, 4404, None))
        self.assertEqual(r["source_page"], 25)
        self.assertEqual(self.db.premium("lacare_bronze60_hmo_2026", 25, "90024")["amount"], 289)
        self.assertEqual(self.db.premium("lacare_bronze60_hmo_2026", 40, "91101")["amount"], 347)  # region 15

    def test_other_ages_are_scaled_from_age_40_on_the_federal_curve(self):
        r = self.db.premium("lacare_bronze60_hmo_2026", 22, "90024")
        self.assertEqual((r["amount"], r["annual"], r["scaled_from_age"]), (287.17, 3446.04, 40))  # 367 * 1.000/1.278
        self.assertEqual(self.db.premium("lacare_bronze60_hmo_2026", 70, "90024")["age"], 64)

    def test_scaling_matches_the_published_age_25_rates(self):
        rows = self.db.con.execute("SELECT plan_id, rating_region, amount_usd FROM premiums WHERE age=25").fetchall()
        f = dict(self.db.con.execute("SELECT age, factor FROM age_curve").fetchall())
        for plan_id, region, printed in rows:
            age40 = self.db.con.execute("SELECT amount_usd FROM premiums WHERE plan_id=? AND rating_region=? AND "
                                        "age=40", (plan_id, region)).fetchone()[0]
            self.assertAlmostEqual(age40 * f[25] / f[40], printed, delta=1, msg=(plan_id, region))

    def test_ucship_depends_on_student_level_only(self):
        self.assertEqual(self.db.premium(UCSHIP, None, None, "undergraduate")["annual"], 3886.02)  # 3 x $1,295.34
        self.assertEqual(self.db.premium(UCSHIP, 30, "91101", "graduate")["annual"], 7516.98)     # 3 x $2,505.66
        self.assertEqual(self.db.premium(UCSHIP)["missing_fields"], ["student_level"])
        self.assertEqual(self.db.premium(UCSHIP, enrollee="not_student")["status"], "unavailable")

    def test_missing_and_unavailable(self):
        self.assertEqual(self.db.premium("lacare_gold80_hmo_2026")["missing_fields"], ["zip", "age"])
        self.assertEqual(self.db.premium("lacare_gold80_hmo_2026", 30, "10001")["status"], "unavailable")
        # A plan with no premium rows is unavailable, never a guess.
        self.assertEqual(self.db.premium("no_premium_plan", 30, "90024")["status"], "unavailable")



if __name__ == "__main__":
    unittest.main()

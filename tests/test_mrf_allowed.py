"""End-to-end tests: synthetic MRF -> import_mrf_rates -> allowed-amount rules -> lookup."""
import csv
import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts", "mrf"))

import allowed  # noqa: E402
import import_mrf_rates as imp  # noqa: E402
import lookup  # noqa: E402

NET = "CA PPO TEST"
AS_OF = "2026-10-14"
UCLA_NPI, WESTWOOD_NPI, LA_NPI, LA_NPI2, FAR_NPI = "1000000001", "1000000002", "1000000003", "1000000004", "1999999999"
HOSPITAL_NPI = "1000000005"


def price(rate, ntype="negotiated", cls="professional", pos=("11",), mods=None, exp="9999-12-31"):
    p = {"negotiated_type": ntype, "negotiated_rate": rate, "expiration_date": exp, "billing_class": cls,
         "setting": "outpatient"}
    if pos:
        p["service_code"] = list(pos)
    if mods:
        p["billing_code_modifier"] = list(mods)
    return p


def item(code, rates):
    return {"negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
            "billing_code_type_version": "2026", "billing_code": code, "description": code,
            "negotiated_rates": [{"provider_references": refs, "negotiated_prices": prices} for refs, prices in rates]}


def synthetic_mrf():
    group = lambda gid, npi, tin, name, net=NET: {  # noqa: E731
        "provider_group_id": gid, "network_name": [net],
        "provider_groups": [{"npi": [int(npi)], "tin": {"type": "ein", "value": tin, "business_name": name}}]}
    return {
        "reporting_entity_name": "Synthetic", "reporting_entity_type": "test", "last_updated_on": "2026-10-01",
        "version": "2.0.0",
        "provider_references": [
            group(1, UCLA_NPI, "951111111", "REGENTS OF THE UNIVERSITY OF CALIFORNIA"),
            group(2, WESTWOOD_NPI, "952222222", "WESTWOOD MED GROUP"),
            group(3, LA_NPI, "953333333", "LA CLINIC"),
            group(4, LA_NPI2, "954444444", "LA CLINIC 2"),
            group(5, FAR_NPI, "955555555", "OUT OF AREA"),
            group(6, HOSPITAL_NPI, "956666666", "LA HOSPITAL"),
            group(7, WESTWOOD_NPI, "952222222", "WESTWOOD MED GROUP", net="OTHER NETWORK"),
        ],
        "in_network": [
            item("99213", [
                ([1], [price(100.0), price(130.0, pos=("20",))]),
                ([2], [price(90.0), price(999.0, ntype="percentage")]),
                ([3], [price(80.0, pos=None)]),
                ([4], [price(70.0), price(10.0, exp="2025-12-31")]),
                ([5], [price(1.0)]),
                ([7], [price(5000.0)]),  # other network: must never appear
            ]),
            item("73610", [
                ([2], [price(30.0, pos=None, mods=("TC",)), price(20.0, pos=None, mods=("26",))]),
                ([3], [price(60.0, pos=None)]),
            ]),
            item("99284", [
                ([3], [price(200.0, pos=("23",))]),
                ([6], [price(1500.0, cls="institutional", pos=None)]),
            ]),
            item("87804", [([3], [price(40.0, pos=None)])]),
        ],
    }


class AllowedAmountPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        mrf = os.path.join(cls.tmp, "CA_TEST.json.gz")
        with gzip.open(mrf, "wt") as f:
            json.dump(synthetic_mrf(), f)
        npis = {UCLA_NPI: ("A", "1"), WESTWOOD_NPI: ("A", "0"), LA_NPI: ("B", "0"), LA_NPI2: ("B", "0"),
                HOSPITAL_NPI: ("B", "0")}
        cls.npi_info = {n: ("90024" if t == "A" else "90012", "x", t, int(u)) for n, (t, u) in npis.items()}
        cls.profiles = os.path.join(cls.tmp, "profiles.json")
        with open(cls.profiles, "w") as f:
            json.dump({"plans": [{"plan_key": "test_ppo", "insurer": "Test Insurer", "network_names": [NET], "region": "CA"}]}, f)
        cls.encounters = os.path.join(cls.tmp, "encounters.csv")
        with open(cls.encounters, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["encounter", "label", "component", "codes", "billing_class", "place_of_service", "modifier_rule", "verified_by"])
            w.writerows([
                ["urgent_care_flu", "UC flu", "visit", "99213", "professional", "20", "global", ""],
                ["urgent_care_flu", "UC flu", "flu_test", "87804", "professional", "20", "global", ""],
                ["er_ankle_xray", "ER ankle", "er_physician", "99284", "professional", "23", "global", ""],
                ["er_ankle_xray", "ER ankle", "er_facility", "99284", "institutional", "", "global", ""],
                ["er_ankle_xray", "ER ankle", "ankle_xray", "73610", "professional", "23", "global_or_tc_plus_26", ""],
                ["spirometry", "Spirometry", "test", "94010|94060", "professional", "11", "global", ""],
            ])
        cls.db = os.path.join(cls.tmp, "rates.sqlite")
        profile = imp.load_profile(cls.profiles, "test_ppo")
        cls.result = imp.import_file(cls.db, mrf, profile, ["99213", "73610", "99284", "87804", "94010", "94060"],
                                     cls.npi_info, [NET], source_url="https://example.test/CA_TEST.json.gz",
                                     file_date="2026-10-01")
        cls._saved = (lookup.ENCOUNTERS, lookup.PROFILES)
        lookup.ENCOUNTERS, lookup.PROFILES = cls.encounters, cls.profiles
        cls.con = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        lookup.ENCOUNTERS, lookup.PROFILES = cls._saved
        shutil.rmtree(cls.tmp)

    def test_import_status_and_no_raw_tin(self):
        self.assertEqual(self.result["status"], "complete")
        src = self.con.execute("SELECT status, file_date, source_url, rows_kept FROM mrf_sources").fetchall()
        self.assertEqual(src, [("complete", "2026-10-01", "https://example.test/CA_TEST.json.gz", self.result["rate_rows"])])
        dump = "\n".join(self.con.iterdump())
        for tin in ("951111111", "952222222", "953333333"):
            self.assertNotIn(tin, dump)
        self.assertNotIn(FAR_NPI, dump)          # not in the LA NPI set
        self.assertNotIn("5000.0", dump)         # other network's rate
        orphans = self.con.execute("""SELECT COUNT(*) FROM negotiated_rates r LEFT JOIN mrf_sources s
                                      ON s.source_id = r.source_id WHERE s.source_id IS NULL""").fetchone()[0]
        self.assertEqual(orphans, 0)

    def test_resume_skips_complete_and_redoes_failed(self):
        profile = imp.load_profile(self.profiles, "test_ppo")
        r = imp.import_file(self.db, "/does/not/matter", profile, ["99213"], self.npi_info, [NET],
                            source_url="https://example.test/CA_TEST.json.gz")
        self.assertEqual(r["status"], "skipped_complete")
        bad = imp.import_file(self.db, os.path.join(self.tmp, "missing.json"), profile, ["99213"], self.npi_info, [NET])
        self.assertEqual(bad["status"], "failed")
        st = self.con.execute("SELECT status FROM mrf_sources WHERE source_url LIKE '%missing.json'").fetchone()[0]
        self.assertEqual(st, "failed")

    def test_ucla_rate_wins_with_pos_and_exclusions(self):
        r = allowed.component(self.con, "test_ppo", "99213", "professional", "11", as_of=AS_OF)
        self.assertEqual((r.allowed_amount, r.allowed_amount_basis, r.status), (100.0, "ucla_health_rate", "verified_rate"))
        self.assertEqual(r.westwood_area_median, 95.0)        # UCLA 100, Westwood 90
        self.assertEqual(r.la_county_median, 85.0)           # 100, 90, 80 (no-POS fallback), 70
        self.assertEqual(r.n_rates_excluded, 2)              # one percentage, one expired
        uc = allowed.component(self.con, "test_ppo", "99213", "professional", "20", as_of=AS_OF)
        self.assertEqual(uc.allowed_amount, 130.0)           # POS 20 specific rate preferred

    def test_pos_preference_list(self):
        # 87804 has only no-POS rates; 99213 at "21|20" finds POS 20 for UCLA (130) and falls back
        # to no-POS rates for the rest.
        r = allowed.component(self.con, "test_ppo", "99213", "professional", "21|20", as_of=AS_OF)
        self.assertEqual((r.allowed_amount, r.allowed_amount_basis), (130.0, "ucla_health_rate"))
        r = allowed.component(self.con, "test_ppo", "99213", "professional", "21|11", as_of=AS_OF)
        self.assertEqual(r.allowed_amount, 100.0)

    def test_mod_26_only(self):
        r = allowed.component(self.con, "test_ppo", "73610", "professional", "23", "mod_26", as_of=AS_OF)
        self.assertEqual((r.allowed_amount, r.allowed_amount_basis), (20.0, "westwood_area_median"))

    def test_provider_rate_and_county_fallback(self):
        r = allowed.component(self.con, "test_ppo", "99213", "professional", "11", npi=LA_NPI2, as_of=AS_OF)
        self.assertEqual((r.allowed_amount, r.allowed_amount_basis), (70.0, "provider_rate"))
        flu = allowed.component(self.con, "test_ppo", "87804", "professional", "20", as_of=AS_OF)
        self.assertEqual((flu.allowed_amount, flu.allowed_amount_basis, flu.status), (40.0, "la_county_median", "estimated_rate"))

    def test_tc_plus_26(self):
        r = allowed.component(self.con, "test_ppo", "73610", "professional", "23", "global_or_tc_plus_26", as_of=AS_OF)
        self.assertEqual((r.allowed_amount, r.allowed_amount_basis), (50.0, "westwood_area_median"))  # 30 + 20
        self.assertEqual(r.uses_tc_plus_26, 1)
        g = allowed.component(self.con, "test_ppo", "73610", "professional", "23", "global", as_of=AS_OF)
        self.assertEqual(g.allowed_amount, 60.0)  # without the TC+26 rule only the global rate counts

    def test_encounter_sum_and_worst_basis(self):
        r = lookup.lookup_allowed_amount("Test Insurer", NET, "er_ankle_xray", con=self.con, as_of=AS_OF)
        # 200 (prof, POS 23) + 1500 (facility) + 50 (Westwood TC+26)
        self.assertEqual(r["allowed_amount"], 1750.0)
        self.assertEqual(r["allowed_amount_basis"], "la_county_median")
        self.assertEqual(r["status"], "estimated_rate")
        self.assertEqual([c["billing_class"] for c in r["components"]], ["professional", "institutional", "professional"])
        self.assertEqual(r["source"]["source_urls"], ["https://example.test/CA_TEST.json.gz"])
        flu = lookup.lookup_allowed_amount("test_ppo", None, "urgent_care_flu", con=self.con, as_of=AS_OF)
        self.assertEqual(flu["allowed_amount"], 170.0)  # 130 (UCLA, POS 20) + 40

    def test_unavailable_never_invents(self):
        r = lookup.lookup_allowed_amount("test_ppo", None, "spirometry", con=self.con, as_of=AS_OF)
        self.assertIsNone(r["allowed_amount"])
        self.assertEqual(r["status"], "rate_unavailable")
        code = lookup.lookup_allowed_amount("test_ppo", None, "99213", con=self.con, as_of=AS_OF)
        self.assertEqual(code["allowed_amount"], 100.0)


if __name__ == "__main__":
    unittest.main()

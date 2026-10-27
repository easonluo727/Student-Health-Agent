"""Synthetic NPPES ZIP and read-only rate-store tests."""
import csv
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflow"))

from scripts.providers.build_directory import build
from scripts.providers import build_from_api
from scripts.providers.export_target_npis import export
from scripts.providers.geocode_directory import enrich, parse_census_batch
from scripts.providers.import_zip_centroids import import_centroids
from scripts.providers.validate_directory import validate
from scripts.mrf.mrf_rate_store import SCHEMA as RATE_SCHEMA
from plan_db import PlanDB
from provider_directory import PRIMARY_CARE, ProviderDirectory, doctor_finder, estimate_nearby
from harness.skills.providers.skill import find  # noqa: E402

N1, N2, N3, ABSENT = "1234567893", "1999999984", "1000000004", "1000000012"


def csv_bytes(headers, rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


class DirectoryBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.rates = root / "rates.sqlite"
        with sqlite3.connect(self.rates) as con:
            con.execute("CREATE TABLE provider_members(npi TEXT)")
            con.executemany("INSERT INTO provider_members VALUES (?)", [(x,) for x in
                            (N1, N1, N2, N3, ABSENT, "bad", "", None)])
        self.initial_hash = hashlib.sha256(self.rates.read_bytes()).hexdigest()
        self.targets = root / "targets.csv"
        self.export_report = export(self.rates, self.targets)
        self.archive = root / "NPPES_Data_Dissemination_October_2026_V2.zip"
        main_headers = ["NPI", "Entity Type Code", "Provider First Name", "Provider Last Name (Legal Name)",
                        "Provider Credential Text", "Provider Organization Name (Legal Business Name)",
                        "Provider Business Practice Location Address Line 1",
                        "Provider Business Practice Location Address Line 2",
                        "Provider Business Practice Location Address City Name",
                        "Provider Business Practice Location Address State Name",
                        "Provider Business Practice Location Address Postal Code",
                        "Provider Business Practice Location Address Telephone Number",
                        "Healthcare Provider Taxonomy Code_1", "Healthcare Provider Primary Taxonomy Switch_1",
                        "Healthcare Provider Taxonomy Code_2", "Healthcare Provider Primary Taxonomy Switch_2",
                        "Last Update Date", "NPI Deactivation Date", "NPI Reactivation Date"]
        main = [
            {"NPI": N1, "Entity Type Code": "1", "Provider First Name": "Ada", "Provider Last Name (Legal Name)": "Care",
             "Provider Credential Text": "MD", "Provider Business Practice Location Address Line 1": "10 Main St",
             "Provider Business Practice Location Address City Name": "Los Angeles",
             "Provider Business Practice Location Address State Name": "CA",
             "Provider Business Practice Location Address Postal Code": "90024",
             "Provider Business Practice Location Address Telephone Number": "3105550100",
             "Healthcare Provider Taxonomy Code_1": "207Q00000X", "Healthcare Provider Primary Taxonomy Switch_1": "Y",
             "Healthcare Provider Taxonomy Code_2": "207R00000X", "Last Update Date": "10/01/2026"},
            {"NPI": N2, "Entity Type Code": "2", "Provider Organization Name (Legal Business Name)": "Test Clinic",
             "Provider Business Practice Location Address Line 1": "20 Oak Ave",
             "Provider Business Practice Location Address City Name": "Santa Monica",
             "Provider Business Practice Location Address State Name": "CA",
             "Provider Business Practice Location Address Postal Code": "90401",
             "Healthcare Provider Taxonomy Code_1": "261Q00000X"},
            {"NPI": N3, "Entity Type Code": "1", "NPI Deactivation Date": "09/01/2026",
             "Provider Business Practice Location Address Line 1": "30 Pine Rd",
             "Provider Business Practice Location Address City Name": "Los Angeles",
             "Provider Business Practice Location Address State Name": "CA",
             "Provider Business Practice Location Address Postal Code": "90025"},
            {"NPI": "1000000020", "Entity Type Code": "1", "Provider First Name": "Outside"},
        ]
        secondary_headers = ["NPI", "Provider Secondary Practice Location Address - Address Line 1",
                             "Provider Secondary Practice Location Address - City Name",
                             "Provider Secondary Practice Location Address - State Name",
                             "Provider Secondary Practice Location Address - Postal Code",
                             "Provider Secondary Practice Location Address - Telephone Number"]
        secondary = [
            {"NPI": N1, "Provider Secondary Practice Location Address - Address Line 1": "10 MAIN ST",
             "Provider Secondary Practice Location Address - City Name": "Los Angeles",
             "Provider Secondary Practice Location Address - State Name": "CA",
             "Provider Secondary Practice Location Address - Postal Code": "90024"},
            {"NPI": N1, "Provider Secondary Practice Location Address - Address Line 1": "40 Hill St",
             "Provider Secondary Practice Location Address - City Name": "Beverly Hills",
             "Provider Secondary Practice Location Address - State Name": "CA",
             "Provider Secondary Practice Location Address - Postal Code": "90210"},
            {"NPI": N1, "Provider Secondary Practice Location Address - Address Line 1": "50 Valley St",
             "Provider Secondary Practice Location Address - City Name": "Los Angeles",
             "Provider Secondary Practice Location Address - State Name": "CA",
             "Provider Secondary Practice Location Address - Postal Code": "90025"},
            {"NPI": "1000000020", "Provider Secondary Practice Location Address - Address Line 1": "60 Other St"},
        ]
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("npidata_pfile_20261012.csv", csv_bytes(main_headers, main))
            z.writestr("pl_pfile_20261012.csv", csv_bytes(secondary_headers, secondary))
        self.directory = root / "providers.sqlite"

    def test_export_is_sorted_unique_valid_and_read_only(self):
        self.assertEqual(self.export_report["exported"], 4)
        self.assertEqual(self.export_report["skipped_invalid"], 1)
        self.assertEqual(hashlib.sha256(self.rates.read_bytes()).hexdigest(), self.initial_hash)
        with self.targets.open() as handle:
            self.assertEqual([row["npi"] for row in csv.DictReader(handle)], sorted((N1, N2, N3, ABSENT)))
        with self.assertRaises(ValueError):
            export(self.rates, self.rates)
        with self.assertRaises(ValueError):
            enrich(self.rates, dry_run=True)

    def test_stream_build_preserves_all_practice_locations_and_status(self):
        report = build(self.archive, self.targets, self.directory, "2026-10-12", "2026-10-26")
        self.assertEqual((report["matched_providers"], report["unmatched_targets"], report["locations_imported"]),
                         (3, 1, 5))
        self.assertEqual(hashlib.sha256(self.rates.read_bytes()).hexdigest(), self.initial_hash)
        with sqlite3.connect(self.directory) as con:
            self.assertEqual(con.execute("SELECT entity_type,display_name FROM providers WHERE npi=?", (N2,)).fetchone(),
                             ("2", "Test Clinic"))
            self.assertEqual(con.execute("SELECT nppes_status FROM providers WHERE npi=?", (N3,)).fetchone()[0], "inactive")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_locations WHERE npi=?", (N1,)).fetchone()[0], 3)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_taxonomies WHERE npi=?", (N1,)).fetchone()[0], 2)
        checked = validate(str(self.directory), str(self.targets))
        self.assertTrue(checked["valid"])
        self.assertEqual((checked["multi_location_providers"], checked["unmatched_targets"]), (1, 1))
        with self.assertRaises(FileExistsError):
            build(self.archive, self.targets, self.directory, "2026-10-12", "2026-10-26")

    def test_geocoding_is_cached_resumable_and_keeps_unknown_coordinates_null(self):
        build(self.archive, self.targets, self.directory, "2026-10-12", "2026-10-26")
        calls = []

        def fake(row):
            calls.append(row["address_line_1"])
            if row["address_line_1"] == "20 Oak Ave":
                return {"status": "ambiguous"}
            return {"status": "matched", "latitude": 34.05, "longitude": -118.45,
                    "precision": "street_interpolated", "source": "fixture"}

        self.assertEqual(enrich(self.directory, limit=2, dry_run=True)["selected"], 2)
        self.assertEqual(calls, [])
        enrich(self.directory, limit=2, resume=True, min_interval=0, geocoder=fake)
        enrich(self.directory, limit=20, resume=True, min_interval=0, geocoder=fake)
        self.assertEqual(len(calls), 5)
        self.assertEqual(enrich(self.directory, limit=20, resume=True, min_interval=0, geocoder=fake)["selected"], 0)
        with sqlite3.connect(self.directory) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_locations WHERE geocode_status='matched'").fetchone()[0], 4)
            self.assertIsNone(con.execute("SELECT latitude FROM provider_locations WHERE npi=?", (N2,)).fetchone()[0])

    def test_batch_geocoding_checks_each_match_like_single_requests(self):
        loc = {"address_line_1": "4101 TORRANCE BLVD", "city": "TORRANCE", "state": "CA", "zip5": "90503"}
        text = (
            '"0","4101 TORRANCE BLVD, TORRANCE, CA, 90503","Match","Exact",'
            '"4101 TORRANCE BLVD, TORRANCE, CA, 90503","-118.355802843091,33.837624445372","657168765","R"\n'
            '"1","4101 TORRANCE BLVD, TORRANCE, CA, 90503","Match","Non_Exact",'
            '"4101 TORRANCE BLVD, TORRANCE, CA, 90505","-118.35,33.83","1","R"\n'
            '"2","4101 TORRANCE BLVD, TORRANCE, CA, 90503","Match","Exact",'
            '"4103 TORRANCE BLVD, TORRANCE, CA, 90503","-118.35,33.83","1","R"\n'
            '"3","X","Tie"\n'
            '"4","X","No_Match"\n'
        )
        out = parse_census_batch(text, {str(i): loc for i in range(6)})
        self.assertEqual(out["0"]["status"], "matched")
        self.assertAlmostEqual(out["0"]["latitude"], 33.837624, places=5)
        self.assertEqual(out["1"]["status"], "ambiguous")  # different ZIP
        self.assertEqual(out["2"]["status"], "ambiguous")  # different house number
        self.assertEqual(out["3"]["status"], "ambiguous")  # tie
        self.assertEqual(out["4"]["status"], "failed")
        self.assertEqual(out["5"]["status"], "failed")  # missing from the reply

    def test_batch_geocoding_sends_each_address_once_and_records_every_location(self):
        build(self.archive, self.targets, self.directory, "2026-10-12", "2026-10-26")
        batches = []

        def fake_batch(rows):
            batches.append([row["address_line_1"] for row in rows])
            return {row["address_key"]: ({"status": "ambiguous"} if row["address_line_1"] == "20 Oak Ave" else
                                         {"status": "matched", "latitude": 34.05, "longitude": -118.45,
                                          "precision": "street_interpolated", "source": "fixture"}) for row in rows}

        report = enrich(self.directory, limit=20, resume=True, min_interval=0, batch_size=2, batch_geocoder=fake_batch)
        self.assertEqual(sum(len(b) for b in batches), 5)
        self.assertTrue(all(len(b) <= 2 for b in batches))
        self.assertEqual(report["requests"], len(batches))
        with sqlite3.connect(self.directory) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_locations WHERE geocode_status='matched'").fetchone()[0], 4)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_locations WHERE geocode_status='pending'").fetchone()[0], 0)

    def test_census_zip_reference_is_separate_from_provider_coordinates(self):
        build(self.archive, self.targets, self.directory, "2026-10-12", "2026-10-26")
        gazetteer = Path(self.tmp.name) / "gazetteer.zip"
        with zipfile.ZipFile(gazetteer, "w") as zipped:
            zipped.writestr("2026_Gazetteer_zcta.txt", "GEOID\tINTPTLAT\tINTPTLONG          \n90024\t34.05\t-118.44          \n")
        result = import_centroids(self.directory, gazetteer,
                                  "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/test.zip", "2026")
        self.assertEqual(result["imported"], 1)
        with sqlite3.connect(self.directory) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM zip_centroids").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_locations WHERE latitude IS NOT NULL").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            import_centroids(self.rates, gazetteer,
                             "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/test.zip", "2026")

    def _service(self):
        build(self.archive, self.targets, self.directory, "2026-10-12", "2026-10-26")
        with sqlite3.connect(self.directory) as con:
            con.execute("INSERT INTO zip_centroids VALUES ('90024',34.0500,-118.4400,'2026','https://www2.census.gov/geo/example')")
            con.execute("UPDATE provider_locations SET latitude=34.05,longitude=-118.44,geocode_status='matched' "
                        "WHERE npi=? AND location_type='primary'", (N1,))
            con.execute("UPDATE provider_locations SET latitude=34.06,longitude=-118.44,geocode_status='matched' "
                        "WHERE npi=? AND zip5='90210'", (N1,))
            con.execute("UPDATE provider_locations SET latitude=34.30,longitude=-118.44,geocode_status='matched' "
                        "WHERE npi=? AND zip5='90025'", (N1,))
        root = Path(self.tmp.name)
        policy = root / "policy.sqlite"
        with sqlite3.connect(policy) as con:
            con.executescript("""
            CREATE TABLE plans(plan_id TEXT, plan_name TEXT, insurer TEXT, plan_year TEXT, plan_type TEXT);
            CREATE TABLE plan_networks(plan_id TEXT, network_tier TEXT, mrf_plan_key TEXT, notes TEXT, confirmed INTEGER,
                mrf_network TEXT);
            CREATE TABLE benefits(plan_id TEXT, service TEXT, network TEXT, network_tier TEXT, section TEXT,
                service_as_printed TEXT, component TEXT, calculable INTEGER, coinsurance_pct REAL, covered INTEGER,
                deductible_applies INTEGER, copay_usd REAL, source_url TEXT, source_page INTEGER, quote TEXT,
                benefit_id TEXT, limits_conditions TEXT, review_flags TEXT);
            INSERT INTO plans VALUES ('test','Test PPO','Test','2026','PPO');
            INSERT INTO plan_networks VALUES ('test','','test_key','Confirmed network mapping only',1,'Test');
            INSERT INTO benefits VALUES ('test','primary_care_visit','in',NULL,'Office visits','Primary care','office_visit',
                1,20,1,1,NULL,'https://example.test/policy',2,'20% after deductible','b1',NULL,NULL);
            """)
        price_store = root / "price_store.sqlite"
        with sqlite3.connect(price_store) as con:
            con.executescript(RATE_SCHEMA)
            con.execute("INSERT INTO mrf_sources(source_id,source_url,insurer,plan_key,file_date,status) "
                        "VALUES (1,'https://example.test/mrf','Test','test_key','2026-10-01','complete')")
            con.execute("INSERT INTO provider_members(source_id,provider_group_id,npi,tier) VALUES (1,'group',?,'B')", (N1,))
            con.execute("INSERT INTO negotiated_rates(source_id,billing_code,provider_group_id,negotiated_rate,"
                        "negotiated_type,expiration_date,billing_class,service_codes,modifiers) "
                        "VALUES (1,'99213','group',100,'negotiated','9999-12-31','professional','11','')")
        service = ProviderDirectory(self.directory, policy, price_store)
        return service, policy, price_store

    def test_read_only_search_strict_rates_distance_and_unknowns(self):
        service, policy, price_store = self._service()
        root = Path(self.tmp.name)
        search = dict(zip_code="90024", radius_miles=10, scenario="office_visit_established", plan_ids=["test"])
        result = service.search(**search)
        self.assertEqual(result["total"], 2)
        self.assertEqual([p["distance_miles"] for p in result["providers"]], [0.0, 0.69])
        self.assertEqual(result["providers"][0]["prices"]["test"]["negotiated_rate"], 100)
        self.assertEqual(result["providers"][0]["prices"]["test"]["estimate_status"], "needs_information")
        self.assertEqual(result["providers"][0]["network_verification"], "unverified")
        priced = service.search(**search, remaining_deductible=0)
        self.assertEqual(priced["providers"][0]["prices"]["test"]["patient_cost"], 20)
        absent_rate = service.search(**{**search, "scenario": "office_visit_new"})
        self.assertEqual(absent_rate["providers"][0]["prices"]["test"]["rate_status"], "rate_unavailable")
        self.assertIsNone(absent_rate["providers"][0]["prices"]["test"]["patient_cost"])
        multi = service.search(**{**search, "scenario": "urgent_care_flu"})
        self.assertEqual(multi["providers"][0]["prices"]["test"]["estimate_status"], "billing_provider_mismatch")
        self.assertEqual(ProviderDirectory(root / "absent.sqlite", policy, price_store).search(**search)["status"],
                         "directory_unavailable")

    def test_find_providers_tool_passes_choices_and_keeps_strict_prices(self):
        service, _, _ = self._service()
        args = {"zip_code": "90024", "scenario": "office_visit_established", "specialty": "family_medicine",
                "radius_miles": 10, "plan_ids": ["test"], "plan_tiers": [], "remaining_deductible": 0,
                "sort": "nearest", "limit": 5}
        result = find(service, args)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([p["npi"] for p in result["providers"]], [N1, N1])
        self.assertEqual(result["providers"][0]["prices"]["test"]["patient_cost"], 20)
        self.assertNotIn("provenance", result["providers"][0]["prices"]["test"])
        self.assertEqual(find(service, {**args, "specialty": "dermatology"})["providers"], [])
        absent = find(service, {**args, "scenario": "office_visit_new"})["providers"][0]["prices"]["test"]
        self.assertEqual(absent["rate_status"], "rate_unavailable")
        self.assertNotIn("patient_cost", absent)

    def test_an_unknown_specialty_is_refused(self):
        from provider_directory import check_specialty
        check_specialty({"specialty": None})
        with self.assertRaises(ValueError):
            check_specialty({"specialty": "neurosurgery"})

    def test_an_office_visit_is_priced_at_the_nearest_doctors_own_rate(self):
        service, policy, price_store = self._service()
        db = PlanDB(policy, price_store)
        r = estimate_nearby(db, service, "test", "office_visit_established", "in", None, 0, "90024", PRIMARY_CARE)
        self.assertEqual((r["status"], r["patient_cost"]), ("estimated", 20.0))  # 20% of the doctor's $100
        self.assertEqual(r["doctor"]["npi"], N1)
        self.assertEqual(r["allowed_amount"]["basis"], "provider_rate")
        self.assertEqual([d["npi"] for d in r["doctors"]], [N1])  # one entry per doctor, not per office
        self.assertEqual(r["cost_range"], {"low": 20.0, "high": 20.0})

    def test_no_zip_asks_for_it_and_no_doctor_rate_is_never_filled_with_a_median(self):
        service, policy, price_store = self._service()
        db = PlanDB(policy, price_store)
        asked = estimate_nearby(db, service, "test", "office_visit_established", "in", None, 0, None, PRIMARY_CARE)
        self.assertEqual((asked["status"], asked["missing_fields"]), ("needs_information", ["zip"]))
        none = estimate_nearby(db, service, "test", "office_visit_new", "in", None, 0, "90024", PRIMARY_CARE)
        self.assertEqual(none["status"], "needs_review")  # 20% coinsurance needs a price; there is no doctor's rate
        self.assertIsNone(none["patient_cost"])
        self.assertIn("has a negotiated rate", none["doctor_search"])
        self.assertNotIn("doctor", none)

    def test_a_year_prices_office_visits_at_the_nearest_doctor_with_a_rate(self):
        service, policy, price_store = self._service()
        db = PlanDB(policy, price_store)
        find_doctor = doctor_finder(service, "90024", lambda scenario: PRIMARY_CARE)
        year = db.year(db.plan("test"), None, {"office_visit_established": (3, False)}, find_doctor=find_doctor)
        visit = year["visits"][0]
        self.assertEqual(visit["doctor"]["npi"], N1)
        self.assertEqual(visit["allowed_amount"]["total"], 100)
        self.assertIsNone(year["care"]["total"])  # 20% after a deductible this fixture doesn't state
        far = doctor_finder(service, "90024", lambda scenario: ("207N",))  # no dermatologist in the fixture
        visit = db.year(db.plan("test"), None, {"office_visit_established": (1, False)}, find_doctor=far)["visits"][0]
        self.assertIsNone(visit["doctor"])
        self.assertIn("has a negotiated rate", visit["doctor_search"])


def api_result(npi, etype="NPI-1", **basic):
    return {"number": npi, "enumeration_type": etype,
            "basic": {"first_name": "Ada", "last_name": "Care", "credential": "MD", "status": "A",
                      "last_updated": "2026-10-01", **basic},
            "addresses": [
                {"address_purpose": "MAILING", "address_1": "PO BOX 1", "city": "BEVERLY HILLS", "state": "CA",
                 "postal_code": "902090001", "country_code": "US"},
                {"address_purpose": "LOCATION", "address_1": "10 Main St", "address_2": "Suite 2",
                 "city": "Los Angeles", "state": "CA", "postal_code": "900241234", "country_code": "US",
                 "telephone_number": "310-555-0100"}],
            "practiceLocations": [{"address_1": "40 Hill St", "city": "Beverly Hills", "state": "CA",
                                   "postal_code": "90210", "country_code": "US"}],
            "taxonomies": [{"code": "207R00000X", "primary": False}, {"code": "207Q00000X", "primary": True}]}


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code, self.text = status, json.dumps(payload or {})


class FakeSession:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def get(self, url, params, timeout):
        self.calls.append(params["number"])
        return self.replies.pop(0)


class ApiBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.targets = self.root / "targets.csv"
        self.targets.write_text(f"npi\n{N1}\n{N2}\n{ABSENT}\n")
        self.cache = self.root / "cache.sqlite"

    def test_record_uses_practice_locations_never_mailing(self):
        provider, taxonomies, locations = build_from_api.record_rows(api_result(N1))
        self.assertEqual(provider[:3], (N1, "1", "Ada Care"))
        self.assertEqual(provider[7], "207Q00000X")
        self.assertEqual(dict(taxonomies), {"207R00000X": 0, "207Q00000X": 1})
        self.assertEqual([(l["location_type"], l["address_line_1"], l["zip5"]) for l in locations],
                         [("primary", "10 Main St", "90024"), ("secondary", "40 Hill St", "90210")])

    def test_fetch_backs_off_when_blocked_and_resumes(self):
        sleeps = []
        # Sorted targets: ABSENT, N1, N2. ABSENT is blocked once and left for the next run.
        session = FakeSession([FakeResponse(403), FakeResponse(200, {"results": [api_result(N1)]})])
        report = build_from_api.fetch(self.targets, self.cache, min_interval=0.2, limit=2,
                                      session=session, sleep=sleeps.append)
        self.assertEqual((report["blocked"], report["fetched"], report["remaining"]), (1, 1, 2))
        self.assertIn(60.0, sleeps)
        session = FakeSession([FakeResponse(200, {"results": []}),
                               FakeResponse(200, {"results": [api_result(N2, "NPI-2", organization_name="Clinic")]})])
        again = build_from_api.fetch(self.targets, self.cache, min_interval=0.2, session=session, sleep=sleeps.append)
        self.assertEqual(session.calls, [ABSENT, N2])
        self.assertEqual((again["already_cached"], again["remaining"]), (1, 0))
        with self.assertRaises(ValueError):
            build_from_api.fetch(self.targets, self.cache, min_interval=0.05)

    def test_build_matches_the_bulk_schema_and_validates(self):
        con = sqlite3.connect(self.cache)
        con.execute("CREATE TABLE nppes_raw (npi TEXT PRIMARY KEY, fetched_at TEXT, body TEXT)")
        con.execute("INSERT INTO nppes_raw VALUES (?,?,?)", (N1, "2026-10-26T00:00:00+00:00",
                                                           json.dumps({"results": [api_result(N1)]})))
        con.execute("INSERT INTO nppes_raw VALUES (?,?,?)", (ABSENT, "2026-10-26T00:00:00+00:00",
                                                           json.dumps({"results": []})))
        con.commit()
        con.close()
        out = self.root / "providers.sqlite"
        meta = build_from_api.build(self.targets, out, self.cache)
        self.assertEqual((meta["matched_providers"], meta["not_in_registry"], meta["not_fetched_yet"]), (1, 1, 1))
        report = validate(str(out), str(self.targets))
        self.assertTrue(report["valid"])
        self.assertEqual(report["locations"], 2)
        with self.assertRaises(FileExistsError):
            build_from_api.build(self.targets, out, self.cache)


if __name__ == "__main__":
    unittest.main()

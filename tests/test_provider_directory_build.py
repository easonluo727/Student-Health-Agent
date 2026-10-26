"""Synthetic NPPES ZIP and read-only rate-store tests."""
import csv
import hashlib
import io
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflow"))

from scripts.providers.build_directory import build
from scripts.providers.export_target_npis import export
from scripts.providers.geocode_directory import enrich
from scripts.providers.import_zip_centroids import import_centroids
from scripts.providers.validate_directory import validate
from scripts.mrf.mrf_rate_store import SCHEMA as RATE_SCHEMA
from provider_directory import ProviderDirectory

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

    def test_census_zip_reference_is_separate_from_provider_coordinates(self):
        build(self.archive, self.targets, self.directory, "2026-10-12", "2026-10-26")
        gazetteer = Path(self.tmp.name) / "gazetteer.zip"
        with zipfile.ZipFile(gazetteer, "w") as zipped:
            zipped.writestr("2026_Gazetteer_zcta.txt", "GEOID\tINTPTLAT\tINTPTLONG\n90024\t34.05\t-118.44\n")
        result = import_centroids(self.directory, gazetteer,
                                  "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/test.zip", "2026")
        self.assertEqual(result["imported"], 1)
        with sqlite3.connect(self.directory) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM zip_centroids").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_locations WHERE latitude IS NOT NULL").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            import_centroids(self.rates, gazetteer,
                             "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/test.zip", "2026")

    def test_read_only_search_strict_rates_distance_and_unknowns(self):
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
            CREATE TABLE plans(plan_id TEXT, plan_name TEXT, insurer TEXT, plan_year TEXT);
            CREATE TABLE plan_networks(plan_id TEXT, network_tier TEXT, mrf_plan_key TEXT, notes TEXT, confirmed INTEGER);
            CREATE TABLE benefits(plan_id TEXT, service TEXT, network TEXT, network_tier TEXT, section TEXT,
                service_as_printed TEXT, component TEXT, calculable INTEGER, coinsurance_pct REAL, covered INTEGER,
                deductible_applies INTEGER, copay_usd REAL, source_url TEXT, source_page INTEGER, quote TEXT);
            INSERT INTO plans VALUES ('test','Test PPO','Test','2026');
            INSERT INTO plan_networks VALUES ('test','','test_key','Confirmed network mapping only',1);
            INSERT INTO benefits VALUES ('test','primary_care_visit','in','','Office visits','Primary care','office_visit',
                1,20,1,1,NULL,'https://example.test/policy',2,'20% after deductible');
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
        multi = service.search(**{**search, "scenario": "er_ankle_xray"})
        self.assertEqual(multi["providers"][0]["prices"]["test"]["estimate_status"], "billing_provider_mismatch")
        self.assertEqual(ProviderDirectory(root / "absent.sqlite", policy, price_store).search(**search)["status"],
                         "directory_unavailable")


if __name__ == "__main__":
    unittest.main()

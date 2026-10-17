"""Canonical adapter + rate lookup: the acceptance checks of the A/B/NPI matching spec (no LLM, no network)."""
import json
import shutil
import sqlite3
import statistics
import tempfile
import unittest
from pathlib import Path

import fixtures as fx
import canonical_adapter as ca
from rate_lookup import FallbackConfig
from schema_check import validate


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.paths = fx.write_provider_env(self.tmp)
        self.lk = fx.lookup(self.paths)

    def run_lookup(self, lk=None, **kw):
        out = (lk or self.lk).lookup(fx.request(**kw))
        validate(out, "rate_lookup_result")
        return out

    def records(self):
        return [json.loads(line) for line in (self.paths.log_dir / "rate_lookup.jsonl").read_text().splitlines()]


# 1, 2 --------------------------------------------------------------------------------------------------------------
class Adapter(Env):
    def test_aliased_columns_are_normalized_with_raw_values_and_trace(self):
        db = self.tmp / "aliased.sqlite"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE mrf_sources (source_id INTEGER, source_url TEXT, issuer_name TEXT, plan_key TEXT,
                network_name TEXT, mrf_file_date TEXT, status TEXT);
            CREATE TABLE negotiated_rates (rate_id INTEGER, source_id INTEGER, code TEXT, code_type TEXT,
                billing_class TEXT, pos TEXT, modifiers TEXT, negotiated_rate REAL, negotiated_type TEXT,
                expiration_date TEXT, provider_group_id TEXT);
            CREATE TABLE provider_members (source_id INTEGER, provider_group_id TEXT, NPI TEXT, tin_sha256 TEXT,
                provider_zip TEXT, taxonomy TEXT);
            INSERT INTO mrf_sources VALUES (1, 'u', 'Ins', 'pk', 'NET', '2026-10-01', 'complete');
            INSERT INTO negotiated_rates VALUES (7, 1, '99213', 'CPT', 'prof', '11,CSTM-00', 'TC', 88.5, 'Negotiated',
                '9999-12-31', 'g');
            INSERT INTO provider_members VALUES (1, 'g', '1234567890', 'h', '90024-1234', 'x');""")
        con.commit()
        pr = ca.ProviderRates(sqlite3.connect(f"file:{db}?mode=ro", uri=True))
        self.assertEqual(pr.b["provider_npi"].column, "NPI")
        self.assertEqual(pr.b["billing_code"].column, "code")
        self.assertEqual(pr.b["place_of_service"].column, "pos")
        self.assertIsNone(pr.b["tier"].column)  # optional, absent
        (row,) = pr.rows("pk", "99213")
        self.assertEqual((row["provider_npi"], row["billing_class"], row["place_of_service"], row["modifiers"],
                          row["source_rate_type"], row["expiration_date"], row["provider_zip"]),
                         ("1234567890", "professional", frozenset({"11", "CSTM-00"}), ("TC",), "negotiated", None, "90024"))
        self.assertEqual(row["raw"]["billing_class"], "prof")  # original kept
        self.assertEqual(row["raw"]["service_codes"], "11,CSTM-00")
        report = {(m["canonical"], m["view"]): m for m in ca.mapping_report(pr.b)}
        self.assertEqual(report[("provider_npi", "canonical_provider_rates")]["resolved_column"], "NPI")
        self.assertIn("transform", report[("billing_class", "canonical_provider_rates")])

    def test_missing_or_ambiguous_columns_fail_loudly(self):
        db = self.tmp / "bad.sqlite"
        con = sqlite3.connect(db)
        con.executescript("CREATE TABLE mrf_sources (source_id INTEGER); CREATE TABLE negotiated_rates (rate_id INTEGER);"
                          "CREATE TABLE provider_members (npi TEXT, provider_npi TEXT);")
        with self.assertRaises(ca.AdapterError):
            ca.bind_sqlite(con)
        with self.assertRaises(ca.AdapterError):
            ca.resolve_column(["npi", "provider_npi"], ("npi", "NPI", "provider_npi"))

    def test_semantically_different_fields_are_never_merged(self):
        by = {}
        for f in ca.FIELD_MAP:
            by.setdefault(f.canonical, []).append(f)
        self.assertEqual({a for f in by["source_rate_amount"] for a in f.aliases}, {"negotiated_rate"})
        self.assertNotIn("negotiated_rate", {a for f in by["allowed_amount"] for a in f.aliases})
        self.assertEqual({a for f in by["modifiers"] for a in f.aliases}, {"modifiers"})
        self.assertNotIn("modifiers", {a for f in by["modifier_rule"] for a in f.aliases})
        self.assertNotIn("file_date", {a for f in by["expiration_date"] for a in f.aliases})
        row = self.lk.db.rows("test_ppo", "99213")[0]
        self.assertIn("source_rate_amount", row)
        self.assertNotIn("allowed_amount", row)
        self.assertNotIn("modifier_rule", row)
        self.assertNotEqual(row["mrf_file_date"], row["expiration_date"])

    def test_billing_class_and_pos_transforms(self):
        self.assertEqual([ca.billing_class(v) for v in ("Prof", "inst", "facility", "both", "x")],
                         ["professional", "institutional", "institutional", "both", None])
        self.assertEqual(ca.pos_preference("20|11"), ("20", "11"))
        self.assertEqual(ca.pos_applies(frozenset({"11"}), ("20", "11")), "exact:11")
        self.assertEqual(ca.pos_applies(frozenset({"CSTM-00"}), ("20",)), "all_pos")
        self.assertEqual(ca.pos_applies(frozenset(), ("20",)), "all_pos")
        self.assertIsNone(ca.pos_applies(frozenset({"23"}), ("20", "11")))


# 3, 4 --------------------------------------------------------------------------------------------------------------
class EncounterA(Env):
    def test_stable_key_returns_aggregate(self):
        out = self.run_lookup(encounter_key="office_visit_established")
        self.assertEqual((out["status"], out["allowed_amount"], out["match_path"], out["estimate_scope"]),
                         ("network_estimate", 100.0, "A_encounter", "network_aggregate"))
        self.assertEqual(out["match_keys"], {"plan_key": "test_ppo", "network": fx.NET,
                                             "encounter_key": "office_visit_established"})
        self.assertEqual((out["source_status"], out["allowed_amount_basis"]), ("verified_rate", "ucla_health_rate"))
        self.assertFalse(out["service_date_validated"])  # aggregates carry no per-rate expiration
        self.assertIn("not a specific provider", out["message"])

    def test_label_missing_or_duplicated_fails(self):
        comps = ca.canonical_encounter_components(self.paths.encounters_csv)
        comps.append(dict(comps[0], encounter_key="office_visit_established_copy"))  # same label, second key
        a, _ = ca.canonical_csv_views(self.paths.rates_csv, comps, json.loads(self.paths.plan_profiles.read_text())["plans"], {})
        errs = {r["encounter_label"]: r["errors"] for r in a if r["errors"]}
        self.assertEqual(errs["Primary care visit (established patient)"], ["encounter_key_ambiguous"])
        rows = fx.DEFAULT_ROWS + [fx.a_row("Not in the catalog", 12.0)]
        a, _ = ca.canonical_csv_views(fx.write_env(self.tmp / "x", rows=rows).rates_csv,
                                      ca.canonical_encounter_components(self.paths.encounters_csv),
                                      json.loads(self.paths.plan_profiles.read_text())["plans"], {})
        self.assertEqual([r["errors"] for r in a if r["encounter_label"] == "Not in the catalog"], [["encounter_label_unknown"]])
        out = self.run_lookup(encounter_key="no_such_encounter")
        self.assertEqual((out["status"], out["reason_code"], out["allowed_amount"]),
                         ("rate_unavailable", "encounter_key_unknown", None))

    def test_ambiguous_label_blocks_the_lookup(self):
        from rate_lookup import RateLookup
        comps = ca.canonical_encounter_components(self.paths.encounters_csv)
        comps.append(dict(comps[0], encounter_key="office_visit_established_copy"))
        profiles = json.loads(self.paths.plan_profiles.read_text())["plans"]
        a, b = ca.canonical_csv_views(self.paths.rates_csv, comps, profiles, {})
        lk = RateLookup(profiles, comps, a, b, as_of=fx.AS_OF)
        out = lk.lookup(fx.request(encounter_key="office_visit_established"))
        self.assertEqual((out["status"], out["reason_code"]), ("rate_unavailable", "encounter_key_ambiguous"))

    def test_a_total_is_not_added_to_its_components(self):
        out = self.run_lookup(encounter_key="er_ankle_xray")
        self.assertEqual(out["allowed_amount"], 2000.0)  # the A row alone; B rows 300+1500+40 are not added
        self.assertTrue(all(c["included_in_total"] for c in out["components"]))
        self.assertEqual(len(out["components"]), 3)

    def test_missing_component_gives_no_partial_encounter_total(self):
        # selected NPI has 99213 but nothing for 99203, and no nearby provider bills 99203
        out = self.run_lookup(encounter_key="office_visit_new", selected_provider_npi=fx.SELECTED)
        self.assertEqual((out["status"], out["allowed_amount"], out["reason_code"]),
                         ("rate_unavailable", None, "no_eligible_fallback_provider"))
        ok = self.run_lookup(encounter_key="office_visit_established", selected_provider_npi=fx.SELECTED)
        self.assertEqual((ok["status"], ok["allowed_amount"], ok["estimate_scope"]),
                         ("provider_rate", 95.0, "provider_specific"))


# 5 -----------------------------------------------------------------------------------------------------------------
class ComponentB(Env):
    def test_exact_component_row_and_modifier_rule(self):
        out = self.run_lookup(billing_code="73610", billing_class="professional", place_of_service="23|22|11")
        self.assertEqual((out["status"], out["allowed_amount"]), ("network_estimate", 40.0))
        self.assertEqual(out["match_keys"]["modifier_rule"], "mod_26")  # derived, traceable
        self.assertEqual(out["match_keys"]["billing_code_type"], "CPT")
        bad = self.run_lookup(billing_code="73610", billing_class="professional", place_of_service="23|22|11",
                              modifier_rule="global")
        self.assertEqual((bad["reason_code"], bad["allowed_amount"]), ("modifier_rule_mismatch", None))

    def test_each_key_must_match(self):
        ok = self.run_lookup(billing_code="99213", billing_class="prof", place_of_service="20")
        self.assertEqual(ok["allowed_amount"], 130.0)
        for kw, reason in (({"place_of_service": "22"}, "no_code_rate"), ({"place_of_service": ""}, "pos_missing"),
                           ({"billing_code": "99999"}, "no_code_rate"), ({"network": "OTHER NET"}, "network_not_resolved"),
                           ({"plan_key": "nope"}, "network_not_resolved"), ({"insurer": "Other Co"}, "network_not_resolved")):
            args = {"billing_code": "99213", "billing_class": "professional", "place_of_service": "11", **kw}
            out = self.run_lookup(**args)
            self.assertEqual((out["status"], out["reason_code"], out["allowed_amount"]), ("rate_unavailable", reason, None), kw)

    def test_placeholder_aggregates_are_not_prices(self):
        rows = fx.DEFAULT_ROWS + [fx.b_row("80053", "professional", "81", "99999999999999.0"),
                                  dict(fx.b_row("85025", "professional", "81", "12.5"), p90="99999999999999.0")]
        paths = fx.write_env(self.tmp / "ph", rows=rows)
        lk = fx.lookup(paths)
        bad = self.run_lookup(lk, billing_code="80053", billing_class="professional", place_of_service="81")
        self.assertEqual((bad["reason_code"], bad["allowed_amount"]), ("placeholder_rate", None))
        ok = self.run_lookup(lk, billing_code="85025", billing_class="professional", place_of_service="81")
        self.assertEqual((ok["allowed_amount"], ok["p90"]), (12.5, None))
        self.assertTrue(any("Placeholder" in w for w in ok["warnings"]))

    def test_invalid_requests(self):
        both = self.run_lookup(encounter_key="flu_shot", billing_code="99213", billing_class="professional")
        self.assertEqual(both["reason_code"], "invalid_request")
        bad_date = self.run_lookup(encounter_key="flu_shot", service_date="2026-13-01")
        self.assertEqual(bad_date["reason_code"], "invalid_request")


# 6 -----------------------------------------------------------------------------------------------------------------
class ProviderNpi(Env):
    def test_npi_joins_through_provider_group(self):
        out = self.run_lookup(billing_code="99213", billing_class="professional", place_of_service="11",
                              selected_provider_npi=fx.SELECTED)
        self.assertEqual((out["status"], out["allowed_amount"], out["exact_provider_rate_found"]),
                         ("provider_rate", 95.0, True))
        self.assertEqual(out["source_ids"], [1])
        self.assertTrue(out["service_date_validated"])

    def test_npi_without_rows_is_not_reported_as_found(self):
        out = self.run_lookup(billing_code="99214", billing_class="professional", place_of_service="11",
                              selected_provider_npi=fx.OUTSIDER)
        self.assertEqual(out["exact_reason_code"], "no_provider_npi_rows")
        self.assertFalse(out["exact_provider_rate_found"])
        self.assertEqual(out["status"], "nearby_provider_estimate")  # labelled fallback, not the provider's price
        self.assertIn("NOT a quote for the selected provider", out["message"])

    def test_schema_with_npi_column_but_no_rows(self):
        empty = self.tmp / "empty"
        paths = fx.write_provider_env(empty, empty=True)
        out = self.run_lookup(fx.lookup(paths), billing_code="99214", billing_class="professional", place_of_service="11",
                              selected_provider_npi=fx.SELECTED)
        self.assertEqual((out["status"], out["exact_reason_code"], out["reason_code"], out["allowed_amount"]),
                         ("rate_unavailable", "no_provider_npi_rows", "no_eligible_fallback_provider", None))

    def test_expired_only_and_no_database(self):
        out = self.run_lookup(billing_code="99215", billing_class="professional", place_of_service="11",
                              selected_provider_npi=fx.SELECTED, service_date="2026-12-01")
        self.assertEqual((out["exact_reason_code"], out["allowed_amount"]), ("expired_only", None))
        ok = self.run_lookup(billing_code="99215", billing_class="professional", place_of_service="11",
                             selected_provider_npi=fx.SELECTED, service_date="2026-10-20")
        self.assertEqual((ok["status"], ok["allowed_amount"]), ("provider_rate", 180.0))
        self.lk.db = None
        none = self.run_lookup(billing_code="99213", billing_class="professional", place_of_service="11",
                               selected_provider_npi=fx.SELECTED)
        self.assertEqual((none["reason_code"], none["allowed_amount"]), ("no_data_source", None))


# 7, 8 --------------------------------------------------------------------------------------------------------------
class NearbyFallback(Env):
    def fallback(self, lk=None):
        return self.run_lookup(lk, billing_code="99214", billing_class="professional", place_of_service="11",
                               selected_provider_npi=fx.SELECTED)

    def test_hard_rules_exclude_regardless_of_score(self):
        out = self.fallback()
        npis = {p["npi"] for p in out["fallback"]["providers"]}
        _, excluded = fx.provider_rows()
        self.assertFalse(npis & {r[0] for r in excluded})
        self.assertNotIn(fx.SELECTED, npis)
        trail = {t["stage"]: t for t in out["fallback"]["trail"]}
        for stage in ("billing_class", "dollar_rate_type", "positive_amount", "not_placeholder", "valid_on_service_date",
                      "pos_applicable",
                      "modifier_rule", "location_known", "within_25_miles"):
            self.assertGreaterEqual(trail[stage]["excluded"], 1, stage)

    def test_top_k_distinct_providers_median_and_spread(self):
        out = self.fallback()
        providers = out["fallback"]["providers"]
        self.assertEqual(len({p["npi"] for p in providers}), len(providers))
        self.assertEqual(sorted(p["value"] for p in providers), sorted(fx.EXPECTED_TOP))
        self.assertEqual([p["npi"] for p in providers].count("2000000018"), 1)  # two groups, one provider
        vals = sorted(fx.EXPECTED_TOP)
        q = statistics.quantiles(vals, n=100, method="inclusive")
        self.assertEqual((out["allowed_amount"], out["p10"], out["p90"], out["fallback_provider_count"]),
                         (round(statistics.median(vals), 2), round(q[9], 2), round(q[89], 2), 16))
        shared = [p for p in providers if p["value"] == 500.0]
        self.assertEqual([p["npi"] for p in shared], ["2000000013", "2000000014", "2000000015"])  # TIN cap 3
        self.assertEqual(out["fallback"]["score_max"], max(p["score"] for p in providers))
        self.assertEqual((out["estimate_scope"], out["allowed_amount_basis"]), ("nearby_provider_cohort", "nearby_provider_median"))
        self.assertEqual(out["exact_reason_code"], "no_code_rate")
        self.assertEqual(out["warnings"], [])

    def test_k_limit_takes_highest_scores_and_low_sample_warns(self):
        out = self.fallback(fx.lookup(self.paths, fallback=FallbackConfig(top_k=4)))
        providers = out["fallback"]["providers"]
        self.assertEqual(len(providers), 4)
        full = self.fallback()["fallback"]["providers"]
        self.assertEqual([p["npi"] for p in providers], [p["npi"] for p in full[:4]])
        self.assertEqual(providers, sorted(providers, key=lambda p: (-p["score"], p["distance_miles"], p["npi"])))
        self.assertTrue(any("Low sample" in w for w in out["warnings"]))

    def test_unknown_location_and_zero_candidates(self):
        far = self.run_lookup(billing_code="99214", billing_class="professional", place_of_service="11",
                              selected_provider_npi="5555555555")  # in no directory, no membership
        self.assertEqual((far["reason_code"], far["allowed_amount"]), ("provider_location_unknown", None))
        none = self.run_lookup(billing_code="99214", billing_class="professional", place_of_service="81",
                               selected_provider_npi=fx.SELECTED)
        self.assertEqual((none["status"], none["reason_code"], none["allowed_amount"]),
                         ("rate_unavailable", "no_eligible_fallback_provider", None))
        empty = [r for r in self.records() if r["stage"] == "nearby_filter" and r["request_id"] == none["request_id"]]
        self.assertEqual(empty[-1]["status"], "empty")
        self.assertIsNotNone(empty[-1]["zeroed_at"])


# 10 ----------------------------------------------------------------------------------------------------------------
class Logging(Env):
    def test_every_stage_logged_under_one_request_id_without_free_text(self):
        out = self.run_lookup(billing_code="99214", billing_class="professional", place_of_service="11",
                              selected_provider_npi=fx.SELECTED)
        recs = [r for r in self.records() if r["request_id"] == out["request_id"]]
        stages = [r["stage"] for r in recs]
        for s in ("request", "plan_network", "npi_exact", "nearby_anchor", "nearby_filter", "nearby_topk",
                  "nearby_aggregate", "final"):
            self.assertIn(s, stages)
        self.assertTrue(all(r["run_id"] == out["run_id"] and r["adapter_version"] == ca.ADAPTER_VERSION for r in recs))
        self.assertTrue(all("db_snapshot" in r for r in recs))
        text = (self.paths.log_dir / "rate_lookup.jsonl").read_text()
        self.assertNotIn("Jane", text)  # service_description never logged
        self.assertEqual(out["log_status"], "ok")

    def test_log_write_failure_is_reported(self):
        self.lk.log.path = self.tmp  # a directory: every write fails
        out = self.run_lookup(encounter_key="flu_shot")
        self.assertTrue(out["log_status"].startswith("failed"))
        self.assertTrue(any("trace log write failed" in w for w in out["warnings"]))
        self.assertEqual(out["allowed_amount"], 25.0)

    def test_exceptions_are_logged_with_stack(self):
        self.lk._lookup = lambda req, rid: 1 / 0
        with self.assertRaises(ZeroDivisionError):
            self.lk.lookup(fx.request(encounter_key="flu_shot"))
        err = [r for r in self.records() if r["status"] == "error"]
        self.assertTrue(err and "ZeroDivisionError" in err[-1]["error"] and err[-1]["stack"])


# resolver integration ------------------------------------------------------------------------------------------------
class ResolverIntegration(Env):
    def test_quote_carries_lookup_status_reason_and_scope(self):
        r = fx.resolver(self.paths)
        rule = {"policy_plan_id": "test_plan", "service": "physician office visits", "setting": "outpatient",
                "network": "in-network"}
        q = r.resolve_for_rule(rule, option_id="established")
        self.assertEqual((q["status"], q["lookup_status"], q["estimate_scope"]), ("success", "network_estimate",
                                                                                  "network_aggregate"))
        q = r.resolve_for_rule(rule, option_id="established", provider_npi=fx.SELECTED)
        self.assertEqual((q["status"], q["lookup_status"], q["allowed_amount"], q["provider_specific"]),
                         ("success", "provider_rate", 95.0, True))
        q = r.resolve_for_rule(rule, option_id="new", provider_npi=fx.SELECTED)
        self.assertEqual((q["status"], q["reason_code"], q["allowed_amount"]),
                         ("provider_rate_unavailable", "no_eligible_fallback_provider", None))
        validate(q, "rate_quote")


if __name__ == "__main__":
    unittest.main()

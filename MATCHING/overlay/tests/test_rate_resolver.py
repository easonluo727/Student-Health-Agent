"""A_encounter / B_component resolvers: exact matching, fail-closed statuses, provenance flags."""
import copy
import math
import tempfile
import unittest
from pathlib import Path

import fixtures as fx

RULE = {"policy_plan_id": "test_plan", "plan": "test_plan", "service": "physician office visits",
        "setting": "outpatient", "network": "in-network"}
ER = {**RULE, "service": "emergency room"}
IMM = {**RULE, "service": "immunizations"}
URGENT = {**RULE, "service": "urgent care"}


class Base(unittest.TestCase):
    def env(self, **kw):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        return fx.write_env(tmp, **kw)

    def res(self, **kw):
        return fx.resolver(self.env(**kw), kw.pop("as_of", fx.AS_OF) if "as_of" in kw else fx.AS_OF)


class PlanMapping(Base):
    def test_missing_mapping(self):
        q = self.res(pmap=fx.plan_map([])).resolve_for_rule(RULE, option_id="established")
        self.assertEqual((q["status"], q["usable"], q["allowed_amount"]), ("mapping_missing", False, None))

    def test_pending_mapping_never_resolves(self):
        entry = dict(fx.plan_map()["entries"][0], review_status="pending_review")
        q = self.res(pmap=fx.plan_map([entry])).resolve_for_rule(RULE, option_id="established")
        self.assertEqual(q["status"], "mapping_pending_review")
        self.assertIsNone(q["allowed_amount"])

    def test_two_mappings_are_ambiguous(self):
        e = fx.plan_map()["entries"][0]
        q = self.res(pmap=fx.plan_map([e, dict(e, mrf_plan_key="test_other", mrf_network="OTHER NET")])) \
            .resolve_for_rule(RULE, option_id="established")
        self.assertEqual(q["status"], "ambiguous_match")

    def test_network_must_be_exact(self):
        entry = dict(fx.plan_map()["entries"][0], mrf_network="test ppo")  # case differs
        q = self.res(pmap=fx.plan_map([entry])).resolve_for_rule(RULE, option_id="established")
        self.assertEqual(q["status"], "mapping_invalid")

    def test_no_cross_network_fallback(self):
        entry = dict(fx.plan_map()["entries"][0], mrf_plan_key="test_other", mrf_network="OTHER NET")
        q = self.res(pmap=fx.plan_map([entry])).resolve_for_rule(RULE, option_id="new")
        self.assertEqual(q["status"], "no_match")  # OTHER NET has only the established row; never borrows TEST PPO

    def test_service_mapping_pending(self):
        q = self.res(smap=fx.service_map(review="pending_review")).resolve_for_rule(RULE, option_id="established")
        self.assertEqual(q["status"], "mapping_pending_review")

    def test_out_of_network_rule_never_priced_with_in_network_rates(self):
        q = self.res().resolve_for_rule({**RULE, "network": "out-of-network"}, option_id="established")
        self.assertEqual(q["status"], "mapping_invalid")


class EncounterA(Base):
    def test_unique_match(self):
        q = self.res().resolve_for_rule(RULE, option_id="established")
        self.assertEqual((q["status"], q["usable"], q["allowed_amount"], q["rate_table"]),
                         ("success", True, 100.0, "A_encounter"))
        self.assertEqual((q["mrf_plan_key"], q["mrf_network"], q["insurer"]), ("test_ppo", fx.NET, fx.INSURER))
        self.assertTrue(q["aggregate"])
        self.assertFalse(q["provider_specific"])
        self.assertIsNone(q["provider_npi"])
        self.assertEqual(q["mrf_snapshot_date"], fx.SNAP)
        self.assertEqual(q["mapping"]["encounter_label"], "Primary care visit (established patient)")
        self.assertTrue(q["quote_id"].startswith("q_"))

    def test_estimated_status_kept(self):
        q = self.res().resolve_for_rule(RULE, option_id="new")
        self.assertEqual((q["status"], q["allowed_amount"]), ("estimated", 150.0))

    def test_needs_clarification_does_not_pick_first(self):
        q = self.res().resolve_for_rule(RULE)
        self.assertEqual(q["status"], "needs_clarification")
        self.assertIsNone(q["allowed_amount"])
        self.assertEqual([o["option_id"] for o in q["clarification"]["options"]], ["established", "new"])

    def test_no_match_duplicate_and_unavailable(self):
        smap = fx.service_map()
        smap["entries"][2]["options"] = [
            {"option_id": "dup", "label": "d", "kind": "A_encounter", "encounter_key": "dup_encounter",
             "component_benefit_services": {"visit": "immunizations"}},
            {"option_id": "na", "label": "n", "kind": "A_encounter", "encounter_key": "unavailable_enc",
             "component_benefit_services": {"visit": "immunizations"}}]
        smap["entries"][2]["clarification_question"] = "which?"
        r = self.res(smap=smap)
        self.assertEqual(r.resolve_for_rule(IMM, option_id="dup")["status"], "ambiguous_match")
        self.assertEqual(r.resolve_for_rule(IMM, option_id="na")["status"], "rate_unavailable")
        rows = [x for x in fx.DEFAULT_ROWS if x.get("encounter") != "Flu shot"]
        self.assertEqual(self.res(rows=rows).resolve_for_rule(IMM, option_id="flu")["status"], "no_match")

    def test_bad_amounts_never_become_numbers(self):
        for bad in ("", "0", "-5", "nan", "inf", "-inf", "abc"):
            rows = [fx.a_row("Flu shot", bad)]
            q = self.res(rows=rows).resolve_for_rule(IMM, option_id="flu")
            self.assertEqual((q["usable"], q["allowed_amount"]), (False, None), bad)
            self.assertEqual(q["status"], "invalid_rate", bad)

    def test_a_total_refused_when_components_have_different_benefits(self):
        q = self.res().resolve_for_rule(ER, option_id="er_total")
        self.assertEqual(q["status"], "mapping_invalid")
        self.assertIn("B component", q["message"])

    def test_label_map_must_match_encounters(self):
        from rate_resolver import load_encounters, load_label_map
        p = self.env()
        (p.overlay_data / "encounter_labels.v1.json").write_text(
            '{"entries":[{"encounter_key":"flu_shot","csv_label":"Flu Shot"}]}')
        with self.assertRaises(ValueError):
            load_label_map(p.overlay_data / "encounter_labels.v1.json", load_encounters(p.encounters_csv))


class Snapshot(Base):
    def test_stale_future_and_missing_dates(self):
        for date in ("2026-07-01", "2026-12-01", ""):
            q = self.res(rows=[fx.a_row("Flu shot", 25.0, date=date)]).resolve_for_rule(IMM, option_id="flu")
            self.assertEqual((q["status"], q["allowed_amount"]), ("stale_snapshot", None), date)

    def test_service_date_is_separate_from_snapshot(self):
        q = self.res().resolve_for_rule(IMM, option_id="flu", service_date="2027-03-01")
        self.assertEqual((q["service_date"], q["mrf_snapshot_date"]), ("2027-03-01", fx.SNAP))


class ComponentsB(Base):
    def test_exact_component_match_and_sum(self):
        q = self.res().resolve_for_rule(ER, option_id="er_parts")
        self.assertEqual((q["status"], q["usable"], q["rate_table"]), ("estimated", True, "B_component"))
        self.assertEqual(q["allowed_amount"], 1840.0)
        self.assertEqual([c["allowed_amount"] for c in q["components"]], [300.0, 1500.0, 40.0])
        self.assertEqual([c["billing_class"] for c in q["components"]], ["professional", "institutional", "professional"])
        self.assertTrue(q["aggregate"])
        self.assertFalse(q["provider_specific"])

    def _one(self, **change):
        c = fx.comp("visit", "99213", "professional", "11", "global", "urgent care")
        c.update(change)
        smap = fx.service_map()
        smap["entries"][3]["options"][0]["components"] = [c]
        return self.res(smap=smap).resolve_for_rule(URGENT)

    def test_every_dimension_must_match(self):
        self.assertEqual(self._one()["allowed_amount"], 100.0)
        self.assertEqual(self._one(place_of_service="20")["allowed_amount"], 130.0)  # POS selects a different row
        self.assertEqual(self._one(billing_class="institutional", place_of_service="")["allowed_amount"], 400.0)
        for change in ({"billing_code": "99999"}, {"place_of_service": "22"}, {"place_of_service": "20|11"},
                       {"billing_class": "institutional"}, {"modifier_rule": "global_or_tc_plus_26"}):
            q = self._one(**change)
            self.assertEqual((q["status"], q["allowed_amount"]), ("no_match", None), change)

    def test_missing_required_component_gives_no_partial_total(self):
        q = self.res().resolve_for_rule(URGENT)
        self.assertEqual((q["status"], q["usable"], q["allowed_amount"]), ("no_match", False, None))
        self.assertEqual(q["components"][0]["allowed_amount"], 130.0)  # detail kept, total withheld

    def test_optional_component_may_be_missing(self):
        smap = fx.service_map()
        smap["entries"][3]["options"][0]["components"][1]["required"] = False
        q = self.res(smap=smap).resolve_for_rule(URGENT)
        self.assertEqual((q["status"], q["allowed_amount"]), ("estimated", 130.0))


class ProviderSpecific(Base):
    def test_npi_needs_sqlite(self):
        q = self.res().resolve_for_rule(ER, option_id="er_parts", provider_npi=fx.NPI)
        self.assertEqual((q["status"], q["allowed_amount"]), ("no_data_source", None))

    def test_npi_rates_from_sqlite_are_provider_specific(self):
        p = self.env(sqlite=True)
        q = fx.resolver(p).resolve_for_rule({**RULE, "service": "physician office visits"}, option_id="established",
                                            provider_npi=fx.NPI)
        self.assertEqual((q["status"], q["allowed_amount"], q["provider_specific"], q["aggregate"]),
                         ("success", 95.0, True, False))
        self.assertEqual(q["rate_table"], "A_encounter")
        self.assertEqual(q["source"]["kind"], "sqlite")

    def test_unknown_npi_is_unresolved_not_aggregate(self):
        p = self.env(sqlite=True)
        q = fx.resolver(p).resolve_for_rule(RULE, option_id="established", provider_npi="1111111111")
        self.assertEqual((q["status"], q["allowed_amount"], q["provider_specific"]),
                         ("provider_rate_unavailable", None, False))

    def test_csv_rate_is_never_provider_specific(self):
        q = self.res().resolve_for_rule(RULE, option_id="established")
        self.assertFalse(q["provider_specific"])
        self.assertIsNone(q["provider_npi"])


class NoDataSource(Base):
    def test_missing_csv_and_database(self):
        r = self.res()
        r.lookup.has_aggregates = False
        a = r.resolve_for_rule(RULE, option_id="established")
        b = r.resolve_for_rule(ER, option_id="er_parts")
        self.assertEqual((a["status"], a["allowed_amount"]), ("no_data_source", None))
        self.assertEqual((b["status"], b["allowed_amount"]), ("no_data_source", None))
        r.lookup.db = None
        q = r.resolve_for_rule(ER, option_id="er_parts", provider_npi=fx.NPI)
        self.assertEqual((q["status"], q["provider_specific"]), ("no_data_source", False))


class Store(unittest.TestCase):
    def test_store_hands_out_copies(self):
        from rate_resolver import QuoteStore
        s = QuoteStore()
        q = s.issue({"status": "success", "allowed_amount": 10.0})
        q["allowed_amount"] = 1.0
        self.assertEqual(s.get(q["quote_id"])["allowed_amount"], 10.0)
        got = s.get(q["quote_id"])
        got["allowed_amount"] = 2.0
        self.assertEqual(s.get(q["quote_id"])["allowed_amount"], 10.0)
        self.assertIsNone(s.get("q_forged"))
        self.assertTrue(math.isfinite(s.get(q["quote_id"])["allowed_amount"]))


if __name__ == "__main__":
    unittest.main()

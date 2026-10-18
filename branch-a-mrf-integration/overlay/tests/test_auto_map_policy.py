import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fixtures  # noqa: F401  (sets overlay import paths)
import auto_map_policy as auto


class AutoMappingSafety(unittest.TestCase):
    def test_network_candidate_requires_exact_network_after_payer_prefix(self):
        metadata = {"networks": [{"name_in_pdf": "UnitedHealthcare Choice Plus"}]}
        profiles = [{"plan_key": "uhc_ca_choice_plus", "insurer": "UnitedHealthcare",
                     "network_names": ["Choice-Plus"]},
                    {"plan_key": "other", "insurer": "Other Payer", "network_names": ["Choice-Plus"]}]
        self.assertEqual(auto.exact_network_candidates(metadata, profiles), [{
            "policy_network_label": "UnitedHealthcare Choice Plus", "plan_key": "uhc_ca_choice_plus",
            "insurer": "UnitedHealthcare", "network": "Choice-Plus"}])

    def test_network_candidate_does_not_fuzzy_match(self):
        metadata = {"networks": [{"name_in_pdf": "Choice Network"}]}
        profiles = [{"plan_key": "uhc", "insurer": "UnitedHealthcare", "network_names": ["Choice-Plus"]}]
        self.assertEqual(auto.exact_network_candidates(metadata, profiles), [])

    def test_non_amount_or_wrong_network_is_never_a_candidate(self):
        rates = [{"table": "A_encounter", "insurer": "UnitedHealthcare", "network": "Choice-Plus",
                  "encounter": "Primary care visit", "allowed_amount": "100", "status": "verified_rate"},
                 {"table": "A_encounter", "insurer": "UnitedHealthcare", "network": "Choice-Plus",
                  "encounter": "No rate", "allowed_amount": "", "status": "rate_unavailable"},
                 {"table": "A_encounter", "insurer": "Other", "network": "Choice-Plus",
                  "encounter": "Primary care visit", "allowed_amount": "100", "status": "verified_rate"}]
        self.assertTrue(auto.rate_exists(rates, insurer="UnitedHealthcare", network="Choice-Plus",
                                         encounter_label="Primary care visit"))
        self.assertFalse(auto.rate_exists(rates, insurer="UnitedHealthcare", network="Choice-Plus",
                                          encounter_label="No rate"))
        self.assertFalse(auto.rate_exists(rates, insurer="UnitedHealthcare", network="Choice-Plus",
                                          encounter_label="wrong"))

    def test_generated_candidate_catalog_excludes_unpriced_components(self):
        groups = {"labs": [{"encounter": "labs", "label": "Routine labs", "component": "cbc",
                            "codes": "85025", "billing_class": "professional", "place_of_service": "11",
                            "modifier_rule": "global"}]}
        rates = [{"table": "B_component", "insurer": "UHC", "network": "Choice-Plus",
                  "billing_code": "85025", "billing_class": "professional", "place_of_service": "11",
                  "allowed_amount": "20", "status": "verified_rate"}]
        result, catalog = auto.build_service_candidates(
            [{"service": "lab", "setting": "outpatient", "source_text": ""}],
            groups, {"labs": "Routine labs"}, rates, "UHC", "Choice-Plus")
        self.assertEqual(catalog[0]["components"][0]["billing_codes"], ["85025"])

    def test_ambiguous_high_confidence_mapping_gets_a_clarification_question(self):
        candidates = [{"service": "visit", "setting": "outpatient"}]
        groups = {"pcp": [], "specialist": []}
        labels = {"pcp": "Primary care visit", "specialist": "Specialist follow-up"}
        rates = [{"table": "A_encounter", "insurer": "UHC", "network": "Choice-Plus",
                  "encounter": labels[key], "allowed_amount": "100", "status": "verified_rate"}
                 for key in groups]
        entries = [{"service": "visit", "setting": "outpatient", "confidence": "high", "evidence": ["text"],
                    "clarification_question": None, "options": [
                        {"kind": "A_encounter", "encounter_key": key, "components": [],
                         "label": labels[key], "option_id": key} for key in groups]}]

        accepted, blocked = auto.validate_candidate_maps(
            entries, candidates, groups, labels, rates, "UHC", "Choice-Plus")

        self.assertEqual(len(accepted), 1)
        self.assertIn("Primary care visit", accepted[0]["clarification_question"])
        self.assertIn("Specialist follow-up", accepted[0]["clarification_question"])
        self.assertEqual(blocked, [])

    def test_medium_semantic_match_is_retained_with_user_clarification(self):
        candidates = [{"service": "physician's visits", "setting": "outpatient"}]
        groups = {"vaccine_visit": []}
        labels = {"vaccine_visit": "Doctor visit for vaccine services"}
        rates = [{"table": "A_encounter", "insurer": "UHC", "network": "Choice-Plus",
                  "encounter": labels["vaccine_visit"], "allowed_amount": "100", "status": "verified_rate"}]
        entries = [{"service": "physician's visits", "setting": "outpatient", "confidence": "medium",
                    "evidence": ["The user wants to see a doctor for vaccine services."],
                    "clarification_question": None, "options": [{"kind": "A_encounter",
                        "encounter_key": "vaccine_visit", "components": [], "label": labels["vaccine_visit"],
                        "option_id": "vaccine_visit"}]}]

        accepted, blocked = auto.validate_candidate_maps(
            entries, candidates, groups, labels, rates, "UHC", "Choice-Plus")

        self.assertEqual(len(accepted), 1)
        self.assertIn("Doctor visit for vaccine services", accepted[0]["clarification_question"])
        self.assertEqual(blocked, [])

    def test_omitted_benefit_is_logged_as_unmapped_without_stopping_other_matches(self):
        candidates = [{"service": "office visit", "setting": "outpatient"},
                      {"service": "laboratory procedures", "setting": "outpatient"}]
        groups = {"office_visit": []}
        labels = {"office_visit": "Office visit, established patient"}
        rates = [{"table": "A_encounter", "insurer": "UHC", "network": "Choice-Plus",
                  "encounter": labels["office_visit"], "allowed_amount": "100", "status": "verified_rate"}]
        entries = [{"service": "office visit", "setting": "outpatient", "confidence": "high",
                    "evidence": ["Semantically equivalent service"], "clarification_question": None,
                    "options": [{"kind": "A_encounter", "encounter_key": "office_visit", "components": [],
                                 "label": labels["office_visit"], "option_id": "office_visit"}]}]

        accepted, blocked = auto.validate_candidate_maps(
            entries, candidates, groups, labels, rates, "UHC", "Choice-Plus")

        self.assertEqual(len(accepted), 1)
        self.assertEqual(blocked, [{"service": "laboratory procedures", "setting": "outpatient",
                                    "reason": "LLM omitted this benefit service from its candidate response"}])

    def test_batched_match_retries_only_the_omitted_service(self):
        candidates = [{"service": "office visit", "setting": "outpatient"},
                      {"service": "lab work", "setting": "outpatient"}]
        first = {"service_mappings": [{"service": "office visit", "setting": "outpatient",
                    "confidence": "high", "evidence": ["doctor visit"], "options": [],
                    "clarification_question": None}]}
        retry = {"service_mappings": [{"service": "lab work", "setting": "outpatient",
                    "confidence": "medium", "evidence": ["lab benefit"], "options": [],
                    "clarification_question": None}]}
        args = SimpleNamespace(out_dir=Path("/tmp"), run_id="test-run")
        with patch.object(auto, "call_json_cached", side_effect=[first, retry]) as request:
            result = auto.propose_service_mappings(args, object(), "model", "test-plan",
                                                   candidates, [], batch_size=2)

        self.assertEqual(len(result["service_mappings"]), 2)
        self.assertEqual(request.call_count, 2)
        retry_payload = request.call_args_list[1].args[5]
        self.assertEqual(retry_payload["benefit_services"], [candidates[1]])

    def test_no_candidate_reason_is_preserved_for_user_trace(self):
        candidates = [{"service": "inpatient room and board", "setting": "inpatient"}]
        entry = {"service": "inpatient room and board", "setting": "inpatient", "confidence": "low",
                 "evidence": ["No matching inpatient encounter in supplied catalog"],
                 "clarification_question": None, "options": []}
        _, blocked = auto.validate_candidate_maps([entry], candidates, {}, {}, [], "UHC", "Choice-Plus")
        self.assertIn("No matching inpatient encounter", blocked[0]["reason"])

    def test_leading_dot_component_is_repaired_only_to_exact_catalog_name(self):
        candidates = [{"service": "x-ray", "setting": "outpatient"}]
        groups = {"xray": [{"component": "xray_read", "codes": "73610",
                             "billing_class": "professional", "place_of_service": "23"}]}
        labels = {"xray": "Ankle X-ray"}
        rates = [{"table": "B_component", "insurer": "UHC", "network": "Choice-Plus",
                  "billing_code": "73610", "billing_class": "professional", "place_of_service": "23",
                  "allowed_amount": "100", "status": "verified_rate"}]
        entries = [{"service": "x-ray", "setting": "outpatient", "confidence": "high", "evidence": ["text"],
                    "clarification_question": None, "options": [
                        {"kind": "B_components", "encounter_key": "xray", "components": [".xray_read"],
                         "label": "Ankle X-ray", "option_id": "xray_read"}]}]

        repairs = auto.canonicalize_component_refs(entries, groups)
        accepted, blocked = auto.validate_candidate_maps(
            entries, candidates, groups, labels, rates, "UHC", "Choice-Plus")

        self.assertEqual(entries[0]["options"][0]["components"], ["xray_read"])
        self.assertEqual(repairs[0]["before"], ".xray_read")
        self.assertEqual(repairs[0]["after"], "xray_read")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(blocked, [])

    def test_unknown_component_is_not_repaired(self):
        groups = {"xray": [{"component": "xray_read", "codes": "73610",
                             "billing_class": "professional", "place_of_service": "23"}]}
        candidates = [{"service": "x-ray", "setting": "outpatient"}]
        labels = {"xray": "Ankle X-ray"}
        rates = [{"table": "B_component", "insurer": "UHC", "network": "Choice-Plus",
                  "billing_code": "73610", "billing_class": "professional", "place_of_service": "23",
                  "allowed_amount": "100", "status": "verified_rate"}]
        entries = [{"service": "x-ray", "setting": "outpatient", "confidence": "high", "evidence": ["text"],
                    "clarification_question": None, "options": [
                        {"kind": "B_components", "encounter_key": "xray", "components": [".facility"],
                         "label": "Ankle X-ray", "option_id": "xray_read"}]}]
        self.assertEqual(auto.canonicalize_component_refs(entries, groups), [])
        accepted, blocked = auto.validate_candidate_maps(
            entries, candidates, groups, labels, rates, "UHC", "Choice-Plus")
        self.assertEqual(accepted, [])
        self.assertIn("Unknown component", blocked[0]["reason"])

    def test_completed_model_stages_are_reused_after_a_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(out_dir=Path(tmp), run_id="test-run")
            with patch.object(auto, "call_json", return_value={"result": "cached"}) as request:
                first = auto.call_json_cached(args, object(), "model", "stage", "prompt", {"x": 1}, {})
                second = auto.call_json_cached(args, object(), "model", "stage", "prompt", {"x": 1}, {})
            self.assertEqual(first, second)
            self.assertEqual(request.call_count, 1)
            self.assertTrue((Path(tmp) / "auto_mapping_steps.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

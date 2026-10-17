"""Branch A tool flow: rule -> quote -> calculator, guard provenance, per-component costing."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import fixtures as fx
import branch_a_agent as agent


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = fx.write_env(self.tmp)
        self.confirms = []
        self.tools, self.guard = fx.build_stack(self.paths, confirm=self._confirm)
        self.ids = {(c["service"], c["network"]): c["rule_id"] for c in self.tools.get_catalog()}
        self.inspected = set()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _confirm(self, summary):
        self.confirms.append(summary)
        return True, "yes"

    def call(self, name, **args):
        return agent.execute_tool(name, json.dumps(args), self.tools, self.inspected, self.guard)

    def rid(self, service, network="in-network"):
        return self.ids[(service, network)]

    def user(self, text):
        self.guard.record_user(text)

    def open_rule(self, service):
        rid = self.rid(service)
        self.assertEqual(self.call("get_policy_rule", rule_id=rid)["status"], "found")
        return rid

    def quote(self, rid, option=None, npi=None):
        return self.call("resolve_rate", rule_id=rid, option_id=option, provider_npi=npi, service_date=None)

    # ---------------------------------------------------------------- A flow
    def test_clarify_then_quote_then_calculate(self):
        self.user("How much is a doctor visit?")
        rid = self.open_rule("physician office visits")
        first = self.quote(rid)
        self.assertEqual(first["status"], "needs_clarification")
        # the model may not answer its own question
        self.assertEqual(self.quote(rid, "established")["status"], "clarification_required")
        self.user("I've seen this doctor before")
        q = self.quote(rid, "established")
        self.assertEqual((q["status"], q["allowed_amount"]), ("success", 100.0))
        result = self.call("calculate_with_quote", rule_id=rid, quote_id=q["quote_id"], remaining_deductible=None)
        self.assertEqual((result["status"], result["patient_cost"]), ("estimated", 20.0))
        self.assertEqual(result["allowed_amount_source"], "mrf_quote")
        self.assertEqual(result["rate_quote"]["quote_id"], q["quote_id"])
        self.assertFalse(result["rate_quote"]["provider_specific"])
        self.assertIn("aggregate", self.confirms[-1])
        self.assertIn("not your date of service", self.confirms[-1])
        self.assertIsNone(self.guard.check_reply("Estimated cost: $20.00 (allowed amount $100.00)."))
        self.assertIsNotNone(self.guard.check_reply("Estimated cost: $25.00."))

    def test_model_cannot_pass_or_alter_amounts(self):
        self.user("flu shot price")
        rid = self.open_rule("immunizations")
        q = self.quote(rid)
        self.assertEqual(q["allowed_amount"], 25.0)
        # extra fields (amount, network, code, provider) are rejected outright
        for extra in ({"allowed_amount": 1}, {"mrf_network": "X"}, {"billing_code": "99213"}, {"provider_npi": "1"}):
            bad = self.call("calculate_with_quote", rule_id=rid, quote_id=q["quote_id"], remaining_deductible=None, **extra)
            self.assertEqual(bad["status"], "invalid_arguments", extra)
        self.assertEqual(self.call("calculate_with_quote", rule_id=rid, quote_id="q_made_up",
                                   remaining_deductible=None)["status"], "invalid_quote")
        # copying the quote amount into the manual path is blocked: the user never said 25
        blocked = self.call("calculate_estimate", rule_id=rid, allowed_amount=25.0, remaining_deductible=None)
        self.assertEqual(blocked["status"], "ask_user")

    def test_quote_cannot_be_used_for_another_rule(self):
        self.user("flu shot")
        imm = self.open_rule("immunizations")
        q = self.quote(imm)
        office = self.open_rule("physician office visits")
        r = self.call("calculate_with_quote", rule_id=office, quote_id=q["quote_id"], remaining_deductible=None)
        self.assertEqual(r["status"], "quote_rule_mismatch")

    def test_unusable_quote_cannot_be_calculated(self):
        self.user("urgent care")
        rid = self.open_rule("urgent care")
        q = self.quote(rid)
        self.assertEqual((q["status"], q["usable"]), ("no_match", False))
        r = self.call("calculate_with_quote", rule_id=rid, quote_id=q["quote_id"], remaining_deductible=None)
        self.assertEqual(r["status"], "rate_not_usable")
        self.assertNotIn("patient_cost", {k for k, v in r.items() if v is not None})

    def test_manual_fallback_is_labelled_user_provided(self):
        self.user("The allowed amount is $210")
        rid = self.open_rule("physician office visits")
        r = self.call("calculate_estimate", rule_id=rid, allowed_amount=210, remaining_deductible=None)
        self.assertEqual((r["status"], r["patient_cost"], r["allowed_amount_source"]), ("estimated", 42.0, "user_provided"))

    def test_npi_must_come_from_user(self):
        self.user("doctor visit, I've seen them before")
        rid = self.open_rule("physician office visits")
        self.quote(rid)
        self.user("established")
        r = self.quote(rid, "established", fx.NPI)
        self.assertEqual(r["status"], "ask_user")
        self.user(f"my doctor's NPI is {fx.NPI}")
        r = self.quote(rid, "established", fx.NPI)
        self.assertEqual(r["status"], "no_data_source")  # no SQLite in this environment: explicit, no fallback

    # ---------------------------------------------------------------- B flow
    def test_components_use_their_own_benefit_rules(self):
        self.user("ER visit for my ankle, deductible remaining 30")
        rid = self.open_rule("emergency room")
        self.quote(rid)
        self.user("by component please")
        q = self.quote(rid, "er_parts")
        self.assertEqual((q["status"], q["allowed_amount"]), ("estimated", 1840.0))
        r = self.call("calculate_with_quote", rule_id=rid, quote_id=q["quote_id"], remaining_deductible=30)
        # ER physician 300 and facility 1500 at 10% (no deductible) = 180; X-ray read 40 under "diagnostic x-ray":
        # 30 deductible + 20% of 10 = 32. Total 212.
        self.assertEqual((r["status"], r["patient_cost"]), ("estimated", 212.0))
        self.assertEqual([l["patient_cost"] for l in r["calculation_lines"]], [30.0, 150.0, 32.0])
        self.assertEqual({l["rule_id"] for l in r["calculation_lines"]},
                         {self.rid("emergency room"), self.rid("diagnostic x-ray")})

    def test_missing_component_rule_gives_no_total(self):
        rules = [r for r in fx.rules() if r["service"] != "diagnostic x-ray"]
        shutil.rmtree(self.tmp)
        self.paths = fx.write_env(self.tmp, benefit_rules=rules)
        self.tools, self.guard = fx.build_stack(self.paths, confirm=self._confirm)
        self.ids = {(c["service"], c["network"]): c["rule_id"] for c in self.tools.get_catalog()}
        self.user("ER, deductible remaining 30")
        rid = self.open_rule("emergency room")
        self.quote(rid)
        self.user("by component")
        q = self.quote(rid, "er_parts")
        r = self.call("calculate_with_quote", rule_id=rid, quote_id=q["quote_id"], remaining_deductible=30)
        self.assertEqual((r["status"], r["patient_cost"]), ("needs_review", None))

    def test_deductible_must_come_from_user(self):
        self.user("ER visit")
        rid = self.open_rule("emergency room")
        self.quote(rid)
        self.user("component")
        q = self.quote(rid, "er_parts")
        r = self.call("calculate_with_quote", rule_id=rid, quote_id=q["quote_id"], remaining_deductible=30)
        self.assertEqual(r["status"], "ask_user")
        r = self.call("calculate_with_quote", rule_id=rid, quote_id=q["quote_id"], remaining_deductible=None)
        self.assertEqual((r["status"], r["missing_fields"]), ("needs_information", ["remaining_deductible"]))


class AMDOnly(unittest.TestCase):
    def test_agent_and_importer_refuse_without_amd_config_or_with_openai(self):
        import os
        from unittest import mock
        import policy_import_v2
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                agent.amd_client()
            with self.assertRaises(RuntimeError):
                policy_import_v2.AMDExtractor()
        env = {"AMD_API_BASE_URL": "https://api.openai.com/v1", "AMD_API_KEY": "k", "AMD_MODEL": "m"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                agent.amd_client()
            with self.assertRaises(RuntimeError):
                policy_import_v2.AMDExtractor()


if __name__ == "__main__":
    unittest.main()

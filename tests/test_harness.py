"""Run: python -m unittest discover tests   (no LLM, no network)."""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

from harness import Harness  # noqa: E402
from harness.skills.questionnaire.bank import BY_ID, CALCULATOR_FIELDS, GROUPS, QUESTIONS  # noqa: E402
from harness.skills.questionnaire.skill import answers_message, build_form  # noqa: E402


def harness(search=False, pricing=True):
    return Harness("prime_100", confirm=lambda summary: (True, "yes"),
                   search_worker_url="http://worker" if search else None, pricing=pricing)


def call(h, name, **args):
    """Run one tool call through parse -> pre_tool -> tool -> post_tool, as server.run_call does."""
    parsed, error = h.tools.parse(name, json.dumps(args))
    assert error is None, error
    blocked = h.hooks.pre_tool(name, parsed)
    if blocked is not None:
        return blocked
    return h.hooks.post_tool(name, parsed, h.tools[name].run(parsed))


def form_questions(ids):
    return build_form(ids, "T")["questions"]


def ask(h, ids, prefill=()):
    raw = json.dumps({"question_ids": ids, "title": "T", "intro": "I",
                      "prefill": [{"question_id": q, "value": v} for q, v in prefill]})
    args, error = h.tools.parse("ask_questions", raw)
    assert error is None, error
    return args, h.hooks.pre_tool("ask_questions", args)


class QuestionBank(unittest.TestCase):
    def test_every_question_is_well_formed(self):
        self.assertEqual(len(BY_ID), len(QUESTIONS), "question IDs must be unique")
        for q in QUESTIONS:
            self.assertIn(q["group"], GROUPS)
            self.assertIn(q["kind"], {"single", "multi", "number", "text", "weight"})
            self.assertTrue(q["maps_to"] and q["when"], q["id"])
            if q["kind"] in ("single", "multi"):
                self.assertTrue(q.get("options"), q["id"])
            if q["kind"] == "weight":
                self.assertEqual(len(q["scale_labels"]), 2)

    def test_calculator_fields_have_questions(self):
        for qid in CALCULATOR_FIELDS.values():
            self.assertEqual(BY_ID[qid]["group"], "D")

    def test_form_matches_protocol(self):
        form = build_form(["conditions", "allowed_amount"], "Title", "Why", {"allowed_amount": 180})
        self.assertTrue(form["request_id"].startswith("form_"))
        fields = {q["field"]: q for q in form["questions"]}
        self.assertEqual(fields["conditions"]["kind"], "multi")
        self.assertEqual(fields["allowed_amount"]["prefill"], {"value": 180, "source": "user_message"})
        self.assertNotIn("maps_to", fields["conditions"])  # internal fields stay server-side

    def test_answers_message_marks_unknown(self):
        text = answers_message(form_questions(["er_visits", "remaining_deductible"]), {
            "er_visits": {"value": 1, "unknown": False},
            "remaining_deductible": {"value": None, "unknown": True},
        })
        self.assertIn("source: user_form", text)
        self.assertIn("`er_visits` (", text)
        self.assertIn("unknown", text.splitlines()[2])


class Prompt(unittest.TestCase):
    def test_prompt_is_fully_rendered(self):
        for search in (False, True):
            prompt = harness(search).system_prompt
            self.assertEqual(re.findall(r"\{\{[A-Z_]+\}\}", prompt), [])
            self.assertIn("## questionnaire", prompt)
            self.assertIn("`specialist_visits`", prompt)
        self.assertIn("search_insurer", harness(True).system_prompt)
        self.assertNotIn("search_insurer", harness(False).system_prompt)

    def test_tools(self):
        names = [t["name"] for t in harness(True).tools.definitions()]
        self.assertEqual(names, ["get_policy_rule", "calculate_estimate", "get_definition",
                                 "search_insurer", "ask_questions", "get_allowed_amount", "calculate_with_quote"])
        names = [t["name"] for t in harness(pricing=False).tools.definitions()]
        self.assertNotIn("get_allowed_amount", names)
        self.assertIn("## pricing", harness().system_prompt)

    def test_definitions_lookup(self):
        h = harness()
        found = h.definitions.lookup("allowed amount")
        self.assertEqual(found["status"], "found")
        self.assertTrue(found["text"].startswith("ALLOWED AMOUNT means"))
        self.assertEqual(h.definitions.lookup("no such term")["status"], "not_found")


class QuestionnaireHooks(unittest.TestCase):
    def test_prefill_only_what_the_user_said(self):
        h = harness()
        h.hooks.on_user_message("I have asthma and see a pulmonologist 2 times a year")
        args, blocked = ask(h, ["specialist_visits", "er_visits", "conditions"],
                            [("specialist_visits", "2"), ("er_visits", "1"), ("conditions", "asthma")])
        self.assertIsNone(blocked)
        self.assertEqual(args["checked_prefill"], {"specialist_visits": 2, "conditions": ["asthma"]})

    def test_invalid_option_prefill_is_dropped(self):
        h = harness()
        h.hooks.on_user_message("hi")
        args, _ = ask(h, ["regular_rx"], [("regular_rx", "sometimes")])
        self.assertEqual(args["checked_prefill"], {})

    def test_answered_questions_are_not_asked_again(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["er_visits"]), {"er_visits": {"value": 1, "unknown": False}})
        args, blocked = ask(h, ["er_visits", "urgent_care_visits"])
        self.assertIsNone(blocked)
        self.assertEqual(args["question_ids"], ["urgent_care_visits"])
        _, blocked = ask(h, ["er_visits"])
        self.assertEqual(blocked["status"], "already_answered")

    def test_too_many_questions(self):
        h = harness()
        _, blocked = ask(h, [q["id"] for q in QUESTIONS[:9]])
        self.assertEqual(blocked["status"], "too_many_questions")

    def test_form_numbers_count_as_user_stated(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["allowed_amount", "remaining_deductible"]), {
            "allowed_amount": {"value": 250, "unknown": False},
            "remaining_deductible": {"value": 0, "unknown": False},
        })
        args = {"rule_id": "rule_x", "allowed_amount": 250, "remaining_deductible": 0}
        self.assertIsNone(h.hooks.guard.pre_tool("calculate_estimate", args))
        self.assertEqual(h.hooks.provenance("allowed_amount", 250), "user_form")
        self.assertEqual(h.hooks.provenance("allowed_amount", 300), "user_message")

    def test_weights_do_not_count_as_amounts(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["weight_premium"]), {"weight_premium": {"value": 4, "unknown": False}})
        blocked = h.hooks.guard.pre_tool("calculate_estimate",
                                         {"rule_id": "r", "allowed_amount": 4, "remaining_deductible": None})
        self.assertEqual(blocked["status"], "ask_user")

    def test_calculate_requires_lookup_first(self):
        h = harness()
        args = {"rule_id": "rule_0", "allowed_amount": None, "remaining_deductible": None}
        self.assertEqual(h.hooks.pre_tool("calculate_estimate", args)["status"], "lookup_required")

    def test_missing_calculator_inputs_are_asked_by_python(self):
        h = harness()
        result = {"status": "needs_information", "patient_cost": None,
                  "missing_fields": ["allowed_amount"], "message": "Need the allowed amount."}
        out = h.hooks.post_tool("calculate_estimate",
                                {"rule_id": "rule_0", "allowed_amount": None, "remaining_deductible": None}, result)
        self.assertEqual(out["auto_ask"], ["allowed_amount"])


class UploadMatching(unittest.TestCase):
    """policies/catalog.json matching on text from an uploaded PDF (no LLM)."""

    def test_match_plan(self):
        from harness.reader import load_catalog, match_plan
        catalog = load_catalog()
        sbc = "Anthem Blue Cross: UC SHIP, UC Los Angeles Students and Covered Dependents"
        booklet = "University of California Student Health Insurance Plan UC LOS ANGELES Students"
        self.assertEqual(match_plan(sbc, catalog)["plan_id"], "uc_ship_ucla_2026")
        self.assertEqual(match_plan(booklet, catalog)["plan_id"], "uc_ship_ucla_2026")
        self.assertEqual(match_plan("STUDENT MEDICOVER PRIME 100 2026-2027", catalog)["plan_id"], "prime_100")
        self.assertIsNone(match_plan("STUDENT MEDICOVER PRIME 500 International", catalog))
        self.assertIsNone(match_plan("L.A. Care Covered Silver 70 HMO, Los Angeles", catalog))


class Pricing(unittest.TestCase):
    """The overlay's rate lookup over data/mrf/allowed_amounts.csv (no SQLite, no LLM)."""

    def test_lookup_requires_reading_the_rule(self):
        h = harness()
        self.assertEqual(call(h, "get_allowed_amount", rule_id="rule_28", option_id=None)["status"],
                         "lookup_required")

    def test_clarification_becomes_a_form_then_prices(self):
        h = harness()
        h.hooks.on_user_message("What does a doctor visit cost?")
        call(h, "get_policy_rule", rule_id="rule_28")  # outpatient physician's visits, in-network
        first = call(h, "get_allowed_amount", rule_id="rule_28", option_id=None)
        self.assertEqual(first["status"], "needs_clarification")
        self.assertEqual([o["option_id"] for o in first["clarify"]["options"]], ["office_established", "office_new"])

        # The model may not pick the option itself before the user answers.
        self.assertEqual(call(h, "get_allowed_amount", rule_id="rule_28", option_id="office_established")["status"],
                         "clarification_required")
        field = "rate_option__rule_28"
        h.hooks.on_form_answers([{"field": field, "kind": "single", "label": "?"}],
                                {field: {"value": "office_established", "unknown": False}})
        quote = call(h, "get_allowed_amount", rule_id="rule_28", option_id="office_established")
        self.assertEqual(quote["status"], "success")
        self.assertTrue(quote["usable"])
        self.assertEqual((quote["insurer"], quote["mrf_network"]), ("UnitedHealthcare", "Choice-Plus"))
        self.assertGreater(quote["allowed_amount"], 0)

        result = call(h, "calculate_with_quote", rule_id="rule_28", quote_id=quote["quote_id"],
                      remaining_deductible=None)
        # The prime_100 rule still needs a person to fill in "$25 copay + 80%", so the calculator refuses.
        self.assertEqual(result["status"], "needs_review")

    def test_unknown_quote_is_blocked(self):
        h = harness()
        call(h, "get_policy_rule", rule_id="rule_28")
        blocked = call(h, "calculate_with_quote", rule_id="rule_28", quote_id="q_made_up", remaining_deductible=None)
        self.assertEqual(blocked["status"], "invalid_quote")

    def test_pending_mapping_is_not_priced(self):
        h = harness()
        x_ray = next(r["rule_id"] for r in h.policy.get_catalog()
                     if r["service"] == "diagnostic x-ray services" and r["network"] == "in-network")
        call(h, "get_policy_rule", rule_id=x_ray)
        self.assertEqual(call(h, "get_allowed_amount", rule_id=x_ray, option_id=None)["status"],
                         "mapping_pending_review")


if __name__ == "__main__":
    unittest.main()

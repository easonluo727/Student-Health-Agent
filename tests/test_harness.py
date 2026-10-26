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


def harness():
    return Harness()


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
        prompt = harness().system_prompt
        self.assertEqual(re.findall(r"\{\{[A-Z_]+\}\}", prompt), [])
        self.assertIn("## questionnaire", prompt)
        self.assertIn("## plans", prompt)
        self.assertIn("`specialist_visits`", prompt)

    def test_tools(self):
        names = [t["name"] for t in harness().tools.definitions()]
        self.assertEqual(names, ["ask_questions", "list_plans", "estimate_cost", "compare_plans"])


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

    def test_weights_do_not_count_as_amounts(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["weight_premium"]), {"weight_premium": {"value": 4, "unknown": False}})
        blocked = h.hooks.guard.pre_tool("calculate_estimate",
                                         {"rule_id": "r", "allowed_amount": 4, "remaining_deductible": None})
        self.assertEqual(blocked["status"], "ask_user")

    def test_remaining_deductible_must_be_user_stated(self):
        h = harness()
        h.hooks.on_user_message("What does a specialist visit cost on Silver 70?")
        args = {"plan_id": "p", "scenario": "s", "network": "in", "network_tier": None, "remaining_deductible": 500}
        self.assertEqual(h.hooks.pre_tool("estimate_cost", args)["status"], "ask_user")
        h.hooks.on_user_message("I still have 500 left on my deductible")
        self.assertIsNone(h.hooks.pre_tool("estimate_cost", args))

    def test_a_met_deductible_states_zero(self):
        args = {"plan_id": "p", "scenario": "s", "network": "in", "network_tier": None, "remaining_deductible": 0}
        for said in ("my deductible is already met", "I've met my deductible", "deductible's paid",
                     "I already hit my annual deductible"):
            h = harness()
            h.hooks.on_user_message(f"What does a knee MRI cost? {said}.")
            self.assertIsNone(h.hooks.pre_tool("estimate_cost", args), said)
        for said in ("I haven't met my deductible", "my deductible is not met yet", "deductible isn't met"):
            h = harness()
            h.hooks.on_user_message(f"What does a knee MRI cost? {said}.")
            self.assertEqual(h.hooks.pre_tool("estimate_cost", args)["status"], "ask_user", said)

    def test_missing_calculator_inputs_are_asked_by_python(self):
        h = harness()
        result = {"status": "needs_information", "patient_cost": None,
                  "missing_fields": ["remaining_deductible"], "message": "Need the remaining deductible."}
        out = h.hooks.post_tool("estimate_cost", {"remaining_deductible": None}, result)
        self.assertEqual(out["auto_ask"], ["remaining_deductible"])


    def test_premium_inputs_come_from_the_answers_and_python_asks_for_the_rest(self):
        h = harness()
        h.hooks.on_user_message("Compare UC SHIP and L.A. Care Bronze for me. I'm 22.")
        plans = ["anthem_ppo_ucship_ucla_2026_27", "lacare_bronze60_hmo_2026"]
        out = call(h, "compare_plans", plan_ids=plans, scenarios=["office_visit_established"])
        # Said in chat is not enough: a form, with how often they expect the chosen visit.
        self.assertEqual(out["auto_ask"], ["age", "student_level", "zip", "primary_care_visits"])
        h.hooks.on_form_answers(form_questions(["age", "zip", "student_level", "primary_care_visits"]), {
            "age": {"value": 22, "unknown": False}, "zip": {"value": "90024", "unknown": False},
            "student_level": {"value": "undergraduate", "unknown": False},
            "primary_care_visits": {"value": 10, "unknown": False}})
        out = call(h, "compare_plans", plan_ids=plans, scenarios=["office_visit_established"])
        self.assertNotIn("auto_ask", out)
        self.assertEqual(out["ranked_by"], "year_total")
        self.assertEqual({e["plan_id"]: e["premium"]["annual"] for e in out["plans"]},
                         {"anthem_ppo_ucship_ucla_2026_27": 3886.02, "lacare_bronze60_hmo_2026": 3446.04})
        self.assertEqual(out["visits"][0]["times"], 10)

    def test_visit_counts_ratings_and_budget_come_from_the_answers(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["urgent_care_visits", "weight_premium", "monthly_budget"]), {
            "urgent_care_visits": {"value": 2, "unknown": False}, "weight_premium": {"value": 5, "unknown": False},
            "monthly_budget": {"value": 300, "unknown": False}})
        args = {"plan_ids": ["lacare_bronze60_hmo_2026"], "scenarios": ["office_visit_established"]}
        h.hooks.pre_tool("compare_plans", args)
        self.assertEqual(args["visits"], {"urgent_care_flu": 2})
        self.assertEqual(args["preferences"], {"weights": {"premium": 5, "visit_costs": None, "worst_case": None,
                                                           "keep_doctors": None}, "monthly_budget": 300})

    def test_the_places_the_user_keeps_become_preferred_tiers(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["keep_providers"]), {
            "keep_providers": {"value": ["ucla_health", "ashe"], "unknown": False}})
        args = {"plan_ids": ["anthem_ppo_ucship_ucla_2026_27"], "scenarios": ["office_visit_established"]}
        h.hooks.pre_tool("compare_plans", args)
        self.assertEqual(args["preferred_tiers"], ["Ashe Center"])

    def test_replies_claiming_what_other_plans_lack_are_rejected(self):
        h = harness()
        for text in ["UC SHIP is cheaper. They don't give you the Ashe Center.",
                     "Only UC SHIP gives you the Ashe Center.", "其他保险不能直接用 Ashe。"]:
            self.assertIn("other plans lack", h.hooks.check_reply(text), text)
        self.assertIsNone(h.hooks.check_reply("UC SHIP ranks first: its premium is the lowest."))

    def test_a_closed_form_counts_as_unknown_and_is_not_asked_again(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["primary_care_visits"]), {})
        self.assertIsNone(h.hooks.answers["primary_care_visits"])

    def test_unknown_premium_inputs_are_not_asked_again(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["age"]), {"age": {"value": None, "unknown": True}})
        out = call(h, "compare_plans", plan_ids=["lacare_bronze60_hmo_2026"], scenarios=["office_visit_established"])
        self.assertEqual(out["auto_ask"], ["zip", "primary_care_visits"])


class UploadMatching(unittest.TestCase):
    """Plan database matching on text from an uploaded PDF (no LLM)."""

    def test_match_plan(self):
        from harness.reader import db_catalog, match_plan
        catalog = db_catalog()
        ucla = "Anthem Blue Cross: University of California Student Health Insurance Plan (UC SHIP), UC Los Angeles"
        davis = "Anthem Blue Cross: University of California Student Health Insurance Plan (UC SHIP), UC Davis"
        self.assertEqual(match_plan(ucla, catalog)["plan_id"], "anthem_ppo_ucship_ucla_2026_27")
        self.assertIsNone(match_plan(davis, catalog))
        self.assertEqual(match_plan("L.A. Care Covered Silver 70 HMO", catalog)["plan_id"], "lacare_silver70_hmo_2026")
        self.assertIsNone(match_plan("STUDENT MEDICOVER PRIME 500 International", catalog))


if __name__ == "__main__":
    unittest.main()

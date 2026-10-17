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


def harness(search=False):
    return Harness("prime_100", confirm=lambda summary: (True, "yes"),
                   search_worker_url="http://worker" if search else None)


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
        text = answers_message(["er_visits", "remaining_deductible"], {
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
                                 "search_insurer", "ask_questions"])

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
        h.hooks.on_form_answers(["er_visits"], {"er_visits": {"value": 1, "unknown": False}})
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
        h.hooks.on_form_answers(["allowed_amount", "remaining_deductible"], {
            "allowed_amount": {"value": 250, "unknown": False},
            "remaining_deductible": {"value": 0, "unknown": False},
        })
        args = {"rule_id": "rule_x", "allowed_amount": 250, "remaining_deductible": 0}
        self.assertIsNone(h.hooks.guard.pre_tool("calculate_estimate", args))
        self.assertEqual(h.hooks.provenance("allowed_amount", 250), "user_form")
        self.assertEqual(h.hooks.provenance("allowed_amount", 300), "user_message")

    def test_weights_do_not_count_as_amounts(self):
        h = harness()
        h.hooks.on_form_answers(["weight_premium"], {"weight_premium": {"value": 4, "unknown": False}})
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


if __name__ == "__main__":
    unittest.main()

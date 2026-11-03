"""Run: python -m unittest discover tests   (no LLM, no network)."""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_telehealth_type_maps_to_distinct_scenarios(self):
        q = BY_ID["telehealth_type"]
        self.assertEqual({o["value"] for o in q["options"]}, {"preferred", "other"})
        self.assertIn("preferred_telehealth_visit", q["maps_to"])
        self.assertIn("telehealth_visit", q["maps_to"])

    def test_form_matches_protocol(self):
        form = build_form(["conditions", "allowed_amount"], "Title", "Why", {"allowed_amount": 180})
        self.assertTrue(form["request_id"].startswith("form_"))
        fields = {q["field"]: q for q in form["questions"]}
        self.assertEqual(fields["conditions"]["kind"], "multi")
        self.assertEqual(fields["allowed_amount"]["prefill"], {"value": 180, "source": "user_message"})
        self.assertNotIn("maps_to", fields["conditions"])  # internal fields stay server-side

    def test_answers_message_marks_unknown(self):
        text = answers_message(form_questions(["urgent_care_visits", "remaining_deductible"]), {
            "urgent_care_visits": {"value": 1, "unknown": False},
            "remaining_deductible": {"value": None, "unknown": True},
        })
        self.assertIn("source: user_form", text)
        self.assertIn("`urgent_care_visits` (", text)
        self.assertIn("unknown", text.splitlines()[2])


class Prompt(unittest.TestCase):
    def test_prompt_is_fully_rendered(self):
        prompt = harness().system_prompt
        self.assertEqual(re.findall(r"\{\{[A-Z_]+\}\}", prompt), [])
        self.assertIn("## questionnaire", prompt)
        self.assertIn("## plans", prompt)
        self.assertIn("`specialist_visits`", prompt)
        self.assertIn("`preferred_telehealth_visit`", prompt)
        self.assertIn("ask `telehealth_type`", prompt)

    def test_tools(self):
        names = [t["name"] for t in harness().tools.definitions()]
        self.assertEqual(names, ["ask_questions", "document_overview", "search_document", "read_pages", "view_page",
                                 "find_plan_match", "extract_plan", "list_plans", "estimate_cost", "compare_plans",
                                 "link_network", "set_premium", "find_providers", "read_bill", "audit_bill", "draft_appeal", "show_page"])


class ToolArguments(unittest.TestCase):
    def test_a_written_out_null_is_null_only_where_null_is_allowed(self):
        h = harness()
        args = {"plan_id": "lacare_silver70_hmo_2026", "scenario": "specialist_followup", "network": "in",
                "network_tier": "null", "remaining_deductible": None, "zip_code": "NULL", "specialty": "null"}
        parsed, error = h.tools.parse("estimate_cost", json.dumps(args))
        self.assertIsNone(error)
        self.assertEqual((parsed["network_tier"], parsed["zip_code"], parsed["specialty"]), (None, None, None))
        parsed, error = h.tools.parse("estimate_cost", json.dumps({**args, "scenario": "null"}))
        self.assertEqual(parsed["scenario"], "null")  # not nullable: left as written


class QuestionnaireHooks(unittest.TestCase):
    def test_prefill_only_what_the_user_said(self):
        h = harness()
        h.hooks.on_user_message("I have asthma and see a pulmonologist 2 times a year")
        args, blocked = ask(h, ["specialist_visits", "lab_tests", "conditions"],
                            [("specialist_visits", "2"), ("lab_tests", "1"), ("conditions", "asthma")])
        self.assertIsNone(blocked)
        self.assertEqual(args["checked_prefill"], {"specialist_visits": 2, "conditions": ["asthma"]})

    def test_invalid_option_prefill_is_dropped(self):
        h = harness()
        h.hooks.on_user_message("hi")
        args, _ = ask(h, ["current_coverage"], [("current_coverage", "sometimes")])
        self.assertEqual(args["checked_prefill"], {})

    def test_answered_questions_are_not_asked_again(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["lab_tests"]), {"lab_tests": {"value": 1, "unknown": False}})
        args, blocked = ask(h, ["lab_tests", "urgent_care_visits"])
        self.assertIsNone(blocked)
        self.assertEqual(args["question_ids"], ["urgent_care_visits"])
        _, blocked = ask(h, ["lab_tests"])
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
        out = call(h, "compare_plans", plan_ids=plans, scenarios=["office_visit_established"], specialty=None)
        # Said in chat is not enough: a form, with how often they expect the chosen visit.
        self.assertEqual(out["auto_ask"], ["age", "student_level", "zip", "primary_care_visits"])
        h.hooks.on_form_answers(form_questions(["age", "zip", "student_level", "primary_care_visits"]), {
            "age": {"value": 22, "unknown": False}, "zip": {"value": "90024", "unknown": False},
            "student_level": {"value": "undergraduate", "unknown": False},
            "primary_care_visits": {"value": 10, "unknown": False}})
        out = call(h, "compare_plans", plan_ids=plans, scenarios=["office_visit_established"], specialty=None)
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

    def test_a_premium_the_user_states_is_used_in_this_conversation_only(self):
        import hashlib
        import sessions
        from arena_report import arena_report
        from plan_db import POLICY_DB

        policy_before = hashlib.sha256(Path(POLICY_DB).read_bytes()).hexdigest()
        h = harness()
        h.hooks.on_user_message("Bronze 60 is $139.20 per month for me, and Platinum is $612 a month.")
        bronze, platinum = "lacare_bronze60_hmo_2026", "lacare_platinum90_hmo_2026"

        def premium(**args):
            return h.tools.parse("set_premium", json.dumps({"period": "month", "quote": "Bronze 60 is $139.20 per "
                                                            "month", "file_id": None, **args}))[1]["message"]
        self.assertIn("does not contain", premium(plan_id=bronze, amount=150))           # not what they wrote
        self.assertIn("own words", premium(plan_id=bronze, amount=150, quote="Bronze is $150 per month"))
        self.assertIn("Unknown plan_id", premium(plan_id="nope", amount=139.2))
        self.assertIn("at most", premium(plan_id=bronze, amount=13920))                   # a typo, not a premium
        out = call(h, "set_premium", plan_id=bronze, amount=139.2, period="month", file_id=None,
                   quote="Bronze 60 is $139.20 per month")
        self.assertEqual((out["premium"]["annual"], out["premium"]["basis"]), (1670.4, "user_provided"))
        call(h, "set_premium", plan_id=platinum, amount=612, period="month", file_id=None,
             quote="Platinum is $612 a month")

        out = call(h, "compare_plans", plan_ids=[bronze, platinum], scenarios=["routine_labs"], specialty=None)
        self.assertEqual(out["ranked_by"], "year_total")  # no age or ZIP needed: both premiums are the user's
        entries = {e["plan_id"]: e for e in out["plans"]}
        self.assertEqual(entries[bronze]["premium"]["annual"], 1670.4)
        self.assertEqual(entries[bronze]["year_total"], round(1670.4 + entries[bronze]["care"]["total"], 2))
        self.assertEqual(entries[bronze]["rank"], 1)
        report = {p["plan_id"]: p for p in arena_report(out)["plans"]}
        self.assertEqual(report[bronze]["source"]["kind"], "user_message")
        self.assertEqual(report[bronze]["source"]["label"], "User provided")

        # Kept when the conversation is reopened; absent from a new one; the policy database is untouched.
        session = sessions.Session()
        session.harness = h
        self.assertIn(bronze, sessions.load(sessions.dump(session)).harness.hooks.premium_overrides)
        self.assertEqual(harness().hooks.premium_overrides, {})
        self.assertEqual(hashlib.sha256(Path(POLICY_DB).read_bytes()).hexdigest(), policy_before)

    def test_a_premium_printed_in_an_uploaded_document(self):
        from arena_report import arena_report
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_documents import make_pdf

        tmp = Path(tempfile.mkdtemp())
        make_pdf(tmp / "brochure.pdf", [["Student Plan 2026-2027", "Annual premium: $2,890 per student"]])
        h = harness()
        h.files["f1"] = {"path": tmp / "brochure.pdf", "name": "brochure.pdf", "content_type": "application/pdf",
                         "sha256": "ab" * 32}
        gold = "lacare_gold80_hmo_2026"

        def premium(**args):
            return h.tools.parse("set_premium", json.dumps({"plan_id": gold, "period": "year", "file_id": "f1", **args}))[1]
        self.assertIn("not in the document", premium(amount=2890, quote="Annual premium: $2,990")["message"])
        self.assertIn("Unknown file_id", premium(amount=2890, quote="Annual premium: $2,890", file_id="f9")["message"])
        out = call(h, "set_premium", plan_id=gold, amount=2890, period="year", file_id="f1",
                   quote="Annual premium: $2,890 per student")
        self.assertEqual((out["premium"]["annual"], out["premium"]["basis"]), (2890.0, "uploaded_document"))
        self.assertIn("brochure.pdf", out["message"])
        out = call(h, "compare_plans", plan_ids=[gold], scenarios=["routine_labs"], specialty=None)
        self.assertEqual(out["plans"][0]["premium"]["annual"], 2890.0)
        self.assertEqual(arena_report(out)["plans"][0]["source"]["label"], "brochure.pdf (uploaded, unverified)")

    def test_unknown_premium_inputs_are_not_asked_again(self):
        h = harness()
        h.hooks.on_form_answers(form_questions(["age"]), {"age": {"value": None, "unknown": True}})
        out = call(h, "compare_plans", plan_ids=["lacare_bronze60_hmo_2026"], scenarios=["office_visit_established"], specialty=None)
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

"""Hooks: code that runs at fixed points of every turn. No LLM.

  on_user_message   the user typed a message
  on_form_answers   the user submitted a questionnaire
  pre_tool          before a tool runs; may return a result that replaces the call
  post_tool         after a tool runs; may rewrite the result or pause for confirmation
  check_reply       before a reply is shown; may reject it with feedback

The amount, confirmation and reply checks are guards.ConversationGuard, unchanged. This file
adds the questionnaire rules, Python filling compare_plans' inputs from the answers (premium inputs: age, ZIP,
student level; visit counts; priority ratings and the monthly budget), and Python sending a form when a tool needs
something from the user.
"""

import re
from decimal import Decimal, InvalidOperation

from guards import MAX_REPLY_RETRIES, ConversationGuard, numbers_in  # noqa: F401 (re-exported)

from .skills.questionnaire.bank import (BY_ID, CALCULATOR_FIELDS, PREMIUM_FIELDS, PROVIDER_TIERS, VISIT_FIELDS,
                                       WEIGHT_FIELDS)
from .skills.questionnaire.skill import MAX_QUESTIONS



# "My deductible is met" states a remaining deductible of 0 without saying the number; "haven't met it" doesn't.
DEDUCTIBLE_MET = re.compile(r"deductible(?:\s+is|\s+was|\s+has been|'s)?\s+(?:already\s+|all\s+)?(?:met|paid|reached|"
                            r"done|satisfied)\b|\b(?:met|paid off|reached|hit|satisfied)\s+(?:my|the)\s+(?:\w+\s+)?"
                            r"deductible", re.I)
NEGATION = re.compile(r"\b(?:not|never|yet)\b|n't\b", re.I)
# A claim about what other plans lack or what only one plan gives. The plan database records each plan's own
# benefits, never who else can use a clinic, so such a claim is the model's memory (seen: "They don't give you the
# Ashe Center", when every UCLA student can use it).
EXCLUSIVE_CLAIM = re.compile(
    r"\b(?:they|other plans?|the others?|those plans?|these plans?|none of the others?)\s+(?:don't|do not|doesn't|"
    r"does not|won't|will not|can't|cannot)\s+(?:give|offer|include|have|let you|get you|cover)\b"
    r"|\bonly\s+(?:UC SHIP|this plan|that plan|one plan)\s+(?:gives?|offers?|includes?|has|lets you|covers?)\b"
    r"|\bexclusive (?:to|benefit|access)\b|\byou(?:'d| would| will)? lose (?:access to )?(?:the )?\w+ (?:Center|clinic)\b"
    r"|(?:其他|别的|另外的)(?:保险|计划|plan)[^。！？\n]{0,15}(?:不能|无法|不给|没有|不包括|用不了)"
    r"|只有[^。！？\n]{0,20}(?:才能|才可以|才有)", re.I)


def deductible_met(text):
    return any(not NEGATION.search(text[max(0, m.start() - 20):m.end() + 6]) for m in DEDUCTIBLE_MET.finditer(text))


def _no_confirm(summary):
    # The guard asks for confirmation only on calculate_estimate, which this harness doesn't offer.
    raise RuntimeError("Unexpected confirmation request: " + summary)


class Hooks:
    def __init__(self):
        self.guard = ConversationGuard(confirm=_no_confirm)
        self.user_texts = []
        self.answers = {}  # question-bank id -> submitted value (None when the user doesn't know)

    # Events ---------------------------------------------------------------

    def on_user_message(self, text):
        self.guard.start_turn()
        self.guard.record_user(text)
        if deductible_met(text):
            self.guard.record_user("0")
        self.user_texts.append(text)

    def on_form_answers(self, questions, answers):
        """questions: the form's question dicts. Submitting a form counts as one user turn."""
        numbers = []
        for q in questions:
            answer = answers.get(q["field"]) or {}
            value = None if answer.get("unknown") else answer.get("value")
            if q["field"] in BY_ID:
                self.answers[q["field"]] = value
            if q["kind"] == "number" and value is not None:
                # Questionnaire numbers are user-stated amounts for the calculator guard.
                numbers.append(str(value))
        self.guard.record_user(" ".join(numbers))

    # Tool hooks -----------------------------------------------------------

    def pre_tool(self, name, args):
        if name == "ask_questions":
            return self._check_questions(args)
        if name in ("estimate_cost", "find_providers"):  # the only amount the model passes is the remaining deductible
            if name == "find_providers" and self.answers.get("zip"):  # the ZIP the user gave in the form wins
                args["zip_code"] = str(self.answers["zip"])
            return self.guard.pre_tool("calculate_estimate",
                                       {"allowed_amount": None, "remaining_deductible": args["remaining_deductible"]})
        if name == "compare_plans":  # who the user is and how they use care: only answers they gave, never the model's
            args["profile"] = {field: self.answers.get(qid) for field, qid in PREMIUM_FIELDS.items()}
            args["visits"] = {scenario: self.answers[qid] for qid, scenario in VISIT_FIELDS.items()
                              if self.answers.get(qid) is not None}
            args["preferences"] = {"weights": {f: self.answers.get(qid) for f, qid in WEIGHT_FIELDS.items()},
                                   "monthly_budget": self.answers.get("monthly_budget")}
            args["preferred_tiers"] = [PROVIDER_TIERS[v] for v in self.answers.get("keep_providers") or []
                                       if v in PROVIDER_TIERS]
        return None

    def post_tool(self, name, args, result):
        if name == "ask_questions":
            return result
        if name == "estimate_cost":
            # Inputs are choices from fixed options, so there is nothing to confirm; record the result's
            # numbers so the reply may quote them.
            result = self.guard.post_tool(name, args, result, None)
            if result.get("status") == "needs_information":
                ask = [CALCULATOR_FIELDS[f] for f in result.get("missing_fields") or [] if f in CALCULATOR_FIELDS]
                ask = [qid for qid in ask if qid not in self.answers]
                if ask:
                    result = {**result, "auto_ask": ask}
            return result
        if name == "compare_plans":
            # The premium inputs still missing, and how often the user expects each chosen visit.
            ask = [PREMIUM_FIELDS[f] for f in result.get("missing_fields") or [] if f in PREMIUM_FIELDS]
            ask += [qid for qid, scenario in VISIT_FIELDS.items() if scenario in args["scenarios"]]
            ask = [qid for qid in ask if qid not in self.answers]
            result = self.guard.post_tool(name, args, result, None)
            return {**result, "auto_ask": ask} if ask else result
        return self.guard.post_tool(name, args, result, None)

    def _check_questions(self, args):
        ids = list(dict.fromkeys(args["question_ids"]))
        answered = {qid: self.answers[qid] for qid in ids if qid in self.answers}
        ids = [qid for qid in ids if qid not in answered]
        if not ids:
            return {"status": "already_answered", "answers": answered,
                    "message": "The user already answered all of these. Use the answers instead of asking again."}
        if len(ids) > MAX_QUESTIONS:
            return {"status": "too_many_questions",
                    "message": f"Ask at most {MAX_QUESTIONS} questions in one form. Pick the ones the next step needs."}
        args["question_ids"] = ids
        args["checked_prefill"] = {
            p["question_id"]: value for p in args["prefill"]
            if p["question_id"] in ids and (value := self._stated_value(p["question_id"], p["value"])) is not None
        }
        return None

    def _stated_value(self, qid, raw):
        """The prefill value in its form type, or None unless the user actually said it."""
        q, said = BY_ID[qid], " ".join(self.user_texts)
        raw = raw.strip()
        if q["kind"] in ("number", "weight"):
            try:
                number = Decimal(raw.replace(",", "").lstrip("$"))
            except InvalidOperation:
                return None
            if number.normalize() not in numbers_in(said):
                return None
            return int(number) if number == number.to_integral_value() else float(number)
        if q["kind"] == "single":
            return raw if raw in {o["value"] for o in q["options"]} else None
        if q["kind"] == "multi":
            values = [v.strip() for v in raw.split(",") if v.strip()]
            allowed = {o["value"] for o in q["options"]}
            return values if values and all(v in allowed for v in values) else None
        return raw if raw and raw.lower() in said.lower() else None

    # Reply hook -----------------------------------------------------------

    def check_reply(self, text):
        problems = [self.guard.check_reply(text)]
        if claim := EXCLUSIVE_CLAIM.search(text):
            problems.append(
                f'Your reply claims what other plans lack or what only one plan gives ("{claim.group(0)}"). The plan '
                "database doesn't record that. Remove the claim; compare only what the tool results show for each plan.")
        return " ".join(filter(None, problems)) or None

    def fallback_reply(self):
        return self.guard.fallback_reply()

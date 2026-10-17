"""Hooks: code that runs at fixed points of every turn. No LLM.

  on_user_message   the user typed a message
  on_form_answers   the user submitted a questionnaire
  pre_tool          before a tool runs; may return a result that replaces the call
  post_tool         after a tool runs; may rewrite the result or pause for confirmation
  check_reply       before a reply is shown; may reject it with feedback

The amount, confirmation and reply checks are guards.ConversationGuard, unchanged.
This file adds the lookup-before-calculate rule, the questionnaire rules, and asking
for missing calculator inputs with a form.
"""

from decimal import Decimal, InvalidOperation

from guards import MAX_REPLY_RETRIES, ConversationGuard, numbers_in  # noqa: F401 (re-exported)

from .skills.questionnaire.bank import BY_ID, CALCULATOR_FIELDS
from .skills.questionnaire.skill import MAX_QUESTIONS


class Hooks:
    def __init__(self, policy, confirm):
        self.policy = policy
        self.guard = ConversationGuard(confirm=confirm)
        self.inspected_ids = set()
        self.user_texts = []
        self.answers = {}  # question id -> submitted value (None when the user doesn't know)

    # Events ---------------------------------------------------------------

    def on_user_message(self, text):
        # A fresh lookup per message avoids using an unseen rule after a correction.
        self.inspected_ids = set()
        self.guard.start_turn()
        self.guard.record_user(text)
        self.user_texts.append(text)

    def on_form_answers(self, question_ids, answers):
        for qid in question_ids:
            answer = answers.get(qid) or {}
            value = None if answer.get("unknown") else answer.get("value")
            self.answers[qid] = value
            if BY_ID[qid]["kind"] == "number" and value is not None:
                # Questionnaire numbers are user-stated amounts for the calculator guard.
                self.guard.record_user(str(value))

    def provenance(self, field, value):
        """'user_form' when this value came from the questionnaire, else 'user_message'."""
        if field in self.answers and value is not None and self.answers[field] == value:
            return "user_form"
        return "user_message"

    # Tool hooks -----------------------------------------------------------

    def pre_tool(self, name, args):
        if name == "calculate_estimate":
            if args["rule_id"] not in self.inspected_ids:
                return {"status": "lookup_required", "message": "Call get_policy_rule for this ID before calculating."}
            return self.guard.pre_tool(name, args)
        if name == "ask_questions":
            return self._check_questions(args)
        return None

    def post_tool(self, name, args, result):
        if name == "ask_questions":
            return result
        if name == "get_policy_rule":
            if result.get("status") == "found":
                self.inspected_ids.add(args["rule_id"])
            return self.guard.post_tool(name, args, result, result.get("rule"))
        if name == "calculate_estimate":
            rule = self.policy.get_policy_rule(args["rule_id"]).get("rule")
            result = self.guard.post_tool(name, args, result, rule)  # may pause for confirmation
            if result.get("status") == "needs_information":
                ask = [CALCULATOR_FIELDS[f] for f in result.get("missing_fields") or [] if f in CALCULATOR_FIELDS]
                ask = [qid for qid in ask if qid not in self.answers]
                if ask:
                    result = {**result, "auto_ask": ask}
            return result
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
        return self.guard.check_reply(text)

    def fallback_reply(self):
        return self.guard.fallback_reply()

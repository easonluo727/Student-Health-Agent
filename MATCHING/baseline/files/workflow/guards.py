"""Code-enforced "ask when unsure" hooks for the agent loop. No LLM calls.

Prompts only ask the model to clarify; these hooks make it unavoidable:

1. pre_tool   - every amount passed to calculate_estimate must have been
                stated by the user. Otherwise the call is blocked with ask_user.
2. post_tool  - an estimated result is released to the model only after the
                user confirms the service, network, and amounts in plain text.
3. check_reply - a final reply may not contain dollar amounts that came from
                neither the user nor a tool, and must ask for missing fields
                when the calculator reported needs_information.

The hooks fail closed: when they cannot verify something, the agent asks.
They are independent of the LLM API, so they survive the move to AMD.
"""

import json
import re
from decimal import Decimal, InvalidOperation

AMOUNT_ARGS = ("allowed_amount", "remaining_deductible")
FIELD_LABELS = {
    "allowed_amount": "the allowed amount (the insurer's negotiated price, not the billed price)",
    "remaining_deductible": "how much of your deductible is still remaining this year",
}
MAX_REPLY_RETRIES = 2

_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\w])")
_DOLLAR = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")


def _to_decimal(whole, fraction):
    try:
        return Decimal(whole.replace(",", "") + (fraction or ""))
    except InvalidOperation:
        return None


def numbers_in(text):
    """Every plain or dollar number in text, normalized so 36 == 36.00."""
    found = {_to_decimal(w, f) for w, f in _NUMBER.findall(text or "")}
    found |= {_to_decimal(w, f) for w, f in _DOLLAR.findall(text or "")}
    return {n.normalize() for n in found if n is not None}


def dollar_amounts_in(text):
    found = {_to_decimal(w, f) for w, f in _DOLLAR.findall(text or "")}
    return {n.normalize() for n in found if n is not None}


def _fmt(value):
    return "not provided" if value is None else f"${Decimal(str(value)):,.2f}"


class ConversationGuard:
    """One instance per conversation. Call reset() when the conversation resets."""

    def __init__(self, confirm):
        # confirm(summary_text) -> (accepted: bool, user_reply: str)
        self._confirm = confirm
        self.reset()

    def reset(self):
        self._user_numbers = set()
        self._tool_numbers = set()
        self._confirmed = set()
        self._missing_fields = []

    # Evidence -------------------------------------------------------------

    def record_user(self, text):
        self._user_numbers |= numbers_in(text)

    def start_turn(self):
        self._missing_fields = []

    # Hook 1: before a tool runs -------------------------------------------

    def pre_tool(self, name, args):
        """Return a blocking tool result, or None to let the call run."""
        if name != "calculate_estimate":
            return None
        unstated = [
            field for field in AMOUNT_ARGS
            if args.get(field) is not None
            and Decimal(str(args[field])).normalize() not in self._user_numbers
        ]
        if unstated:
            return {
                "status": "ask_user",
                "missing_fields": unstated,
                "message": (
                    "Blocked: these amounts were never stated by the user: "
                    + ", ".join(unstated)
                    + ". Ask the user for them. Do not estimate, assume, or reuse other numbers."
                ),
            }
        return None

    # Hook 2: after a tool runs, before the model sees the result ----------

    def post_tool(self, name, args, result, rule):
        if result.get("status") == "needs_information":
            self._missing_fields = list(result.get("missing_fields") or [])
        if name == "calculate_estimate" and result.get("status") == "estimated":
            key = (args["rule_id"], args.get("allowed_amount"), args.get("remaining_deductible"))
            if key not in self._confirmed:
                accepted, reply = self._confirm(self._summary(args, rule))
                if not accepted:
                    self.record_user(reply)
                    return {
                        "status": "user_rejected",
                        "user_reply": reply,
                        "message": (
                            "The user did not confirm these details, so no estimate was released. "
                            "Use the user's reply and ask what still needs to change."
                        ),
                    }
                self._confirmed.add(key)
        self._tool_numbers |= numbers_in(json.dumps(result, ensure_ascii=False))
        return result

    @staticmethod
    def _summary(args, rule):
        rule = rule or {}
        return "\n".join([
            "Before I show an estimate, please confirm what I understood:",
            f"  Service:              {rule.get('service', 'unknown')}",
            f"  Network:              {rule.get('network') or 'unknown'}",
            f"  Setting:              {rule.get('setting') or 'unknown'}",
            f"  Allowed amount:       {_fmt(args.get('allowed_amount'))}",
            f"  Remaining deductible: {_fmt(args.get('remaining_deductible'))}",
        ])

    # Hook 3: before a reply reaches the user ------------------------------

    def check_reply(self, text):
        """Return None if the reply may be shown, else feedback for the model."""
        problems = []
        untraced = dollar_amounts_in(text) - self._user_numbers - self._tool_numbers
        if untraced:
            shown = ", ".join(f"${n:,}" for n in sorted(untraced))
            problems.append(
                f"Your reply contains dollar amounts that came from neither the user nor a tool: {shown}. "
                "Remove them, or call calculate_estimate. Never calculate amounts yourself."
            )
        if self._missing_fields and "?" not in text:
            problems.append(
                "The calculator still needs: " + ", ".join(self._missing_fields)
                + ". Ask the user for exactly these fields instead of answering."
            )
        return " ".join(problems) or None

    def fallback_reply(self):
        """Deterministic reply used when the model keeps failing check_reply."""
        if self._missing_fields:
            asks = [FIELD_LABELS.get(field, field) for field in self._missing_fields]
            return "To estimate this, I need " + " and ".join(asks) + ". Could you tell me?"
        return (
            "I couldn't produce an answer I can verify against your policy. "
            "Could you restate the service, whether the provider is in network, and any amounts you know?"
        )

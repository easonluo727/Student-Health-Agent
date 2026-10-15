"""Guard hooks extended for MRF rate quotes (overlay; subclasses the original guards.ConversationGuard).

Trusted amounts now have two provenances:
  user_provided  numbers the user typed (original rule, unchanged for calculate_estimate)
  mrf_quote      amounts inside a quote that Python's RateResolver stored under quote_id

Rules added on top of the original hooks:
- calculate_with_quote runs only for a quote_id issued in this conversation and still usable; the model
  cannot pass an amount, source, plan, network, code or provider (the tool schema has no such fields,
  and execute_tool rejects extra keys). remaining_deductible must still come from the user.
- resolve_rate: provider_npi must be a number the user stated; choosing option_id for a benefit with
  several rate options is allowed only after a needs_clarification result AND a new user message.
- calculate_estimate (manual fallback) keeps the original user-only rule, so a quote amount copied by
  the model into the manual path is blocked.
- Replies may quote dollar amounts from tool results (quotes included), as before.
"""
from __future__ import annotations

import json

from guards import ConversationGuard, numbers_in  # original module, imported read-only


class QuoteGuard(ConversationGuard):
    def __init__(self, confirm, quote_store, rate_options_for_rule=None):
        self._store = quote_store
        # rule_id -> number of rate options (from the service map); used for the clarification gate
        self._rate_options_for_rule = rate_options_for_rule or (lambda rule_id: 1)
        super().__init__(confirm)

    def reset(self):
        super().reset()
        self._user_turns = 0
        self._clarified_at: dict[str, int] = {}

    def record_user(self, text):
        super().record_user(text)
        self._user_turns += 1

    # Hook 1 ------------------------------------------------------------------
    def pre_tool(self, name, args):
        if name == "calculate_estimate":
            return super().pre_tool(name, args)
        if name == "resolve_rate":
            npi = args.get("provider_npi")
            if npi is not None and (not str(npi).isdigit() or
                                    numbers_in(str(npi)).isdisjoint(self._user_numbers)):
                return self._ask(["provider_npi"], "The provider NPI was not stated by the user. Ask for it.")
            if args.get("option_id") is not None and self._rate_options_for_rule(args["rule_id"]) > 1:
                asked = self._clarified_at.get(args["rule_id"])
                if asked is None or self._user_turns <= asked:
                    return {"status": "clarification_required",
                            "message": "Several rate options exist for this benefit. Call resolve_rate without option_id, "
                                       "ask the user the returned question, and choose only from the user's answer."}
            return None
        if name == "calculate_with_quote":
            quote = self._store.get(args.get("quote_id"))
            if quote is None:
                return {"status": "invalid_quote", "message": "Unknown quote_id. Only quotes issued by resolve_rate are accepted."}
            if not quote["usable"]:
                return {"status": "rate_not_usable", "message": f"Quote status {quote['status']!r}; it cannot be used."}
            rd = args.get("remaining_deductible")
            from decimal import Decimal
            if rd is not None and Decimal(str(rd)).normalize() not in self._user_numbers:
                return self._ask(["remaining_deductible"], "The remaining deductible was never stated by the user.")
            return None
        return None

    @staticmethod
    def _ask(fields, message):
        return {"status": "ask_user", "missing_fields": fields,
                "message": message + " Do not estimate, assume, or reuse other numbers."}

    # Hook 2 ------------------------------------------------------------------
    def post_tool(self, name, args, result, rule):
        if name == "resolve_rate":
            if result.get("status") == "needs_clarification":
                self._clarified_at[args["rule_id"]] = self._user_turns
            self._tool_numbers |= numbers_in(json.dumps(result, ensure_ascii=False))
            return result
        if name == "calculate_with_quote":
            if result.get("status") == "needs_information":
                self._missing_fields = list(result.get("missing_fields") or [])
            if result.get("status") == "estimated":
                key = ("quote", args["rule_id"], args["quote_id"], args.get("remaining_deductible"))
                if key not in self._confirmed:
                    accepted, reply = self._confirm(self._quote_summary(args, rule, result["rate_quote"]))
                    if not accepted:
                        self.record_user(reply)
                        return {"status": "user_rejected", "user_reply": reply,
                                "message": "The user did not confirm these details, so no estimate was released. "
                                           "Use the user's reply and ask what still needs to change."}
                    self._confirmed.add(key)
            self._tool_numbers |= numbers_in(json.dumps(result, ensure_ascii=False))
            return result
        result = super().post_tool(name, args, result, rule)
        return result

    @staticmethod
    def _quote_summary(args, rule, q) -> str:
        rule = rule or {}
        what = q["encounter_key"] or ", ".join(f"{c['billing_code']} {c['billing_class']} POS {c['place_of_service'] or 'any'}"
                                               for c in q["components"])
        provider = f"NPI {q['provider_npi']} (provider-specific)" if q["provider_specific"] else "aggregate (no specific provider)"
        return "\n".join([
            "Before I show an estimate, please confirm what I understood:",
            f"  Service:              {rule.get('service', 'unknown')}",
            f"  Network category:     {rule.get('network') or 'unknown'}",
            f"  Setting:              {rule.get('setting') or 'unknown'}",
            f"  Insurer / network:    {q['insurer']} / {q['mrf_network'] or q['network_scope']}",
            f"  Priced as:            {q['rate_table']}: {what}",
            f"  Allowed amount:       ${q['allowed_amount']:,.2f} ({q['allowed_amount_basis']}; {provider})",
            f"  MRF snapshot:         {q['mrf_snapshot_date']} (price list date, not your date of service)",
            f"  Remaining deductible: {'not provided' if args.get('remaining_deductible') is None else args['remaining_deductible']}",
        ])

"""Branch A tools with MRF rate quotes (overlay; wraps the original workflow/agent_tools.PolicyTools).

New tool surface for the agent:
  get_policy_rule(rule_id)                         original behaviour
  resolve_rate(rule_id, option_id?, provider_npi?, service_date?)
                                                   -> rate quote (stored by Python under quote_id)
  calculate_with_quote(rule_id, quote_id, remaining_deductible)
                                                   -> patient cost from the stored quote amount
  calculate_estimate(rule_id, allowed_amount, remaining_deductible)
                                                   manual fallback; result labelled user_provided

The LLM never passes a rate amount, source, plan, network, code or provider into calculate_with_quote:
those come from the stored quote. calculate_cost.calculate_cost stays the pure calculator.
For B_component quotes each component is costed under its own benefit rule (same plan and network
category), the remaining deductible is consumed in component order, and Python sums the costs.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal

from agent_tools import PolicyTools  # original module, imported read-only
from calculate_cost import calculate_cost  # original pure calculator
from rate_resolver import RateResolver, norm_service


def _err(status: str, message: str, **extra) -> dict:
    return {"status": status, "patient_cost": None, "missing_fields": [], "message": message, **extra}


class BranchATools:
    def __init__(self, policy_tools: PolicyTools, resolver: RateResolver):
        self.policy = policy_tools
        self.resolver = resolver
        self.store = resolver.store

    @property
    def plan(self) -> str:
        return self.policy.selected_plan

    def get_catalog(self) -> list[dict]:
        return self.policy.get_catalog()

    def get_policy_rule(self, rule_id):
        return self.policy.get_policy_rule(rule_id)

    def _rule(self, rule_id) -> dict | None:
        found = self.policy.get_policy_rule(rule_id)
        if found["status"] != "found":
            return None
        rule = found["rule"]
        rule.setdefault("policy_plan_id", self.plan)
        return rule

    # ------------------------------------------------------------- rates
    def resolve_rate(self, rule_id, option_id=None, provider_npi=None, service_date=None) -> dict:
        rule = self._rule(rule_id)
        if rule is None:
            return _err("invalid_rule_id", "Choose a rule_id from this conversation's catalog.")
        quote = self.resolver.resolve_for_rule(rule, option_id=option_id, provider_npi=provider_npi,
                                               service_date=service_date)
        quote["rule_id"] = rule_id
        return quote

    def _quote_for_rule(self, rule_id, quote_id):
        rule = self._rule(rule_id)
        if rule is None:
            return None, None, _err("invalid_rule_id", "Choose a rule_id from this conversation's catalog.")
        quote = self.store.get(quote_id)
        if quote is None:
            return None, None, _err("invalid_quote", "Unknown quote_id; call resolve_rate first.")
        if not quote["usable"]:
            return None, None, _err("rate_not_usable", f"Quote status {quote['status']!r}: {quote['message']}",
                                    quote_status=quote["status"])
        m = quote["mapping"]
        if (quote["policy_plan_id"] != self.plan or m.get("benefit_rule_service") != norm_service(rule["service"])
                or m.get("benefit_rule_setting") != rule.get("setting")
                or m.get("policy_network_category") != rule.get("network")):
            return None, None, _err("quote_rule_mismatch", "This quote was resolved for a different plan or benefit rule.")
        return rule, quote, None

    def calculate_with_quote(self, rule_id, quote_id, remaining_deductible=None) -> dict:
        rule, quote, error = self._quote_for_rule(rule_id, quote_id)
        if error:
            return error
        if self._single_benefit(rule, quote):
            # The whole amount is paid under this one rule (an A encounter is only resolvable when every
            # component maps to the rule's own benefit, enforced by the resolver).
            result = self.policy.calculate_estimate(rule_id, quote["allowed_amount"], remaining_deductible)
            return self._annotate(result, quote, [{"component": quote["encounter_key"] or quote["components"][0]["component"],
                                                    "rule_id": rule_id, "allowed_amount": quote["allowed_amount"],
                                                    "status": result.get("status"), "patient_cost": result.get("patient_cost")}])
        return self._per_component(rule_id, rule, quote, remaining_deductible)

    @staticmethod
    def _component_setting(rule: dict, comp: dict):
        return comp["benefit_setting"] if comp.get("benefit_setting") is not None else rule.get("setting")

    def _single_benefit(self, rule: dict, quote: dict) -> bool:
        if quote["rate_table"] == "A_encounter":
            return True
        used = [c for c in quote["components"] if c["status"] in ("success", "estimated")]
        return all(c["benefit_service"] == norm_service(rule["service"])
                   and self._component_setting(rule, c) == rule.get("setting") for c in used)

    def _component_rule_id(self, rule: dict, comp: dict) -> tuple[str | None, str]:
        """Exactly one benefit rule with the component's service, setting and the same network category."""
        setting = self._component_setting(rule, comp)
        ids = [c["rule_id"] for c in self.policy.get_catalog()
               if norm_service(c["service"]) == comp["benefit_service"] and c["setting"] == setting
               and c["network"] == rule.get("network")]
        if len(ids) != 1:
            return None, f"{len(ids)} benefit rules match component {comp['component']!r} ({comp['benefit_service']!r})."
        return ids[0], ""

    def _per_component(self, rule_id, rule, quote, remaining_deductible) -> dict:
        lines, total = [], Decimal(0)
        remaining = None if remaining_deductible is None else Decimal(str(remaining_deductible))
        for comp in quote["components"]:
            if comp["status"] not in ("success", "estimated"):
                continue  # only optional components can be unusable in a usable quote
            cid, msg = self._component_rule_id(rule, comp)
            if cid is None:
                return self._annotate(_err("needs_review", msg), quote, lines)
            crule = self.policy.get_policy_rule(cid)["rule"]
            uses_deductible = crule.get("deductible_applies") is True and crule.get("copay_usd") is None
            if uses_deductible and remaining is None:
                return self._annotate(_err("needs_information", "More information is needed to calculate this benefit.",
                                           missing_fields=["remaining_deductible"]), quote, lines)
            result = self.policy.calculate_estimate(cid, comp["allowed_amount"],
                                                    float(remaining) if uses_deductible else None)
            lines.append({"component": comp["component"], "billing_code": comp["billing_code"], "rule_id": cid,
                          "allowed_amount": comp["allowed_amount"], "status": result.get("status"),
                          "patient_cost": result.get("patient_cost"), "message": result.get("message")})
            if result.get("status") != "estimated":
                return self._annotate(_err(result.get("status", "needs_review"),
                                           f"Component {comp['component']!r}: {result.get('message')}",
                                           missing_fields=result.get("missing_fields") or []), quote, lines)
            total += Decimal(str(result["patient_cost"]))
            if uses_deductible:
                remaining -= min(remaining, Decimal(str(comp["allowed_amount"])))
        out = {"status": "estimated", "patient_cost": float(total.quantize(Decimal("0.01"))), "missing_fields": [],
               "message": "Sum of per-component estimates, each under its own benefit rule; excludes balance billing "
                          "and out-of-pocket maximum adjustments. Not a coverage guarantee."}
        return self._annotate(out, quote, lines)

    @staticmethod
    def _annotate(result: dict, quote: dict, lines: list[dict]) -> dict:
        result = copy.deepcopy(result)
        result["allowed_amount_source"] = "mrf_quote"
        result["rate_quote"] = {k: quote[k] for k in (
            "quote_id", "status", "rate_table", "allowed_amount", "policy_plan_id", "mrf_plan_key", "insurer",
            "mrf_network", "network_scope", "encounter_key", "provider_npi", "provider_specific", "aggregate",
            "allowed_amount_basis", "mrf_snapshot_date", "service_date", "source", "mapping", "lookup_status",
            "reason_code", "estimate_scope")}
        result["rate_quote"]["components"] = [
            {k: c.get(k) for k in ("component", "billing_code", "billing_class", "place_of_service", "modifier_rule",
                                   "allowed_amount", "status")} for c in quote["components"]]
        result["calculation_lines"] = lines
        json.dumps(result, allow_nan=False)
        return result

    # ------------------------------------------------------------- manual fallback
    def calculate_estimate(self, rule_id, allowed_amount=None, remaining_deductible=None) -> dict:
        result = self.policy.calculate_estimate(rule_id, allowed_amount, remaining_deductible)
        result = copy.deepcopy(result)
        result["allowed_amount_source"] = "user_provided" if allowed_amount is not None else None
        return result

"""Policy lookup and calculator tools. No LLM calls or interactive input.

Keep this file beside calculate_cost.py and the normalized policy JSON.
Create one PolicyTools instance per conversation and reuse it throughout.
Tool registration and the LLM conversation loop belong in agent.py.
"""

import json
import math
from collections import defaultdict
from copy import deepcopy
from decimal import DecimalException
from pathlib import Path

from calculate_cost import calculate_cost


DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "normalized_pdf_insurance_rules.json"


def _reject_constant(value):
    raise ValueError(f"Invalid JSON numeric value: {value}")


def _clean(value):
    return " ".join(value.lower().replace("’", "'").split()) if isinstance(value, str) else value


def _error(status, message):
    return {
        "status": status,
        "patient_cost": None,
        "missing_fields": [],
        "message": message,
    }


class PolicyTools:
    """Load a fixed snapshot of one plan; IDs are local to this instance.

These tools cannot establish whether the agent selected the medically correct
service, or whether a supplied amount actually came from the user. The agent
must clarify ambiguous requests and preserve the source of user inputs.
"""

    def __init__(self, selected_plan="prime_100", rules_path=DEFAULT_RULES_PATH):
        if not isinstance(selected_plan, str) or not selected_plan.strip():
            raise ValueError("selected_plan must be a nonempty string.")
        self.selected_plan = selected_plan.strip().lower().replace(" ", "_")
        with Path(rules_path).expanduser().open(encoding="utf-8") as file:
            rules = json.load(file, parse_constant=_reject_constant)
        if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
            raise ValueError("The rules file must contain a list of dictionaries.")

        self._rules = {}
        self._groups = defaultdict(list)
        for index, rule in enumerate(rules):
            if rule.get("plan") != self.selected_plan:
                continue
            if not isinstance(rule.get("service"), str) or not rule["service"].strip():
                raise ValueError(f"Rule at index {index} has no valid service name.")
            if rule.get("network") not in ("uc-family", "in-network", "out-of-network", None):
                raise ValueError(f"Rule at index {index} has an invalid network.")
            if rule.get("setting") not in ("inpatient", "outpatient", "other", None):
                raise ValueError(f"Rule at index {index} has an invalid setting.")
            rule_id = f"rule_{index}"
            self._rules[rule_id] = deepcopy(rule)
            self._groups[self._identity(rule)].append(rule_id)
        if not self._rules:
            raise ValueError(f"No rules found for plan {self.selected_plan!r}.")

    @staticmethod
    def _identity(rule):
        return (_clean(rule.get("service")), rule.get("network"), rule.get("setting"))

    def get_catalog(self):
        """Supply this compact catalog to the agent before it chooses an ID."""
        return [
            {
                "rule_id": rule_id,
                "plan": self.selected_plan,
                "service": rule["service"],
                "network": rule.get("network"),
                "setting": rule.get("setting"),
                "source_page": rule.get("source_page"),
            }
            for rule_id, rule in self._rules.items()
        ]

    def get_policy_rule(self, rule_id):
        """Return the complete stored rule, including original wording.

Unknown IDs are lookup errors, not evidence that a benefit is not covered.
Returned data is a copy, so a caller cannot change the stored rule.
"""
        if not isinstance(rule_id, str) or rule_id not in self._rules:
            return {"status": "invalid_rule_id", "message": "Choose a rule_id from this conversation's catalog.", "rule": None}
        rule = self._rules[rule_id]
        return {
            "status": "found",
            "rule_id": rule_id,
            "rule": deepcopy(rule),
            "same_service_rule_ids": list(self._groups[self._identity(rule)]),
        }

    def calculate_estimate(self, rule_id, allowed_amount=None, remaining_deductible=None):
        """Calculate one occurrence using stored facts; never accept a rule from the LLM.

Missing amounts stay None. All calculator messages, including prototype
assumption disclosures, are preserved unchanged.
"""
        lookup = self.get_policy_rule(rule_id)
        if lookup["status"] != "found":
            return _error("invalid_rule_id", lookup["message"])
        rule = lookup["rule"]
        if len(lookup["same_service_rule_ids"]) > 1:
            return _error("needs_review", "Multiple imported rules share this service, network, and setting. Resolve them before calculating.")
        if rule.get("network") is None or rule.get("setting") is None:
            return _error("needs_review", "The imported rule has an unspecified network or setting.")

        # JSON tool arguments must be numbers or null, never Boolean/string.
        for name, value in (("allowed_amount", allowed_amount), ("remaining_deductible", remaining_deductible)):
            if value is not None:
                if type(value) not in (int, float):
                    return _error("invalid_input", f"{name} must be a number or null.")
                if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
                    return _error("invalid_input", f"{name} must be finite and nonnegative.")
        try:
            result = calculate_cost(
                matched_rule=rule,
                allowed_amount=allowed_amount,
                remaining_deductible=remaining_deductible,
            )
        except (DecimalException, OverflowError, ValueError, TypeError):
            return _error("needs_review", "The calculator could not process these values. Verify the rule and amounts.")

        result = deepcopy(result)
        result.update({
            "rule_id": rule_id,
            "plan": self.selected_plan,
            "service": rule["service"],
            "network": rule.get("network"),
            "setting": rule.get("setting"),
            "source_page": rule.get("source_page"),
            "source_text": rule.get("source_text"),
            "inputs": {"allowed_amount": allowed_amount, "remaining_deductible": remaining_deductible},
        })
        # Ensure even extreme numeric inputs cannot leak NaN/Infinity to the agent.
        try:
            json.dumps(result, allow_nan=False)
        except (ValueError, TypeError):
            return _error("needs_review", "The result is not valid JSON data; verify numeric values.")
        return result

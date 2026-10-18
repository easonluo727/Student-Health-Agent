"""Pricing skill: allowed amounts from the MRF price database, and costs from them.

Wraps the Branch A x MRF overlay (branch-a-mrf-integration/overlay, imported read-only):
  get_allowed_amount    -> BranchATools.resolve_rate: benefit rule -> verified MRF plan/network
                           -> encounter or components -> a quote Python stores under quote_id
  calculate_with_quote  -> BranchATools.calculate_with_quote: the calculator on the stored amount

The model never passes an amount, plan, network or code: they come from the stored quote.
Without data/mrf/rates.sqlite the lookup uses the aggregate rates in data/mrf/allowed_amounts.csv.
Provider (NPI) lookups are not exposed here.
"""

import sys
from pathlib import Path

from .. import Skill, read_skill_md
from ...mcp import Tool, schema

OVERLAY = Path(__file__).resolve().parents[4] / "branch-a-mrf-integration" / "overlay" / "workflow"


def install_overlay():
    """Put the overlay on sys.path and return its configured paths."""
    if str(OVERLAY) not in sys.path:
        sys.path.insert(0, str(OVERLAY))
    import overlay_config

    return overlay_config.install_import_paths()


def build_rates(policy):
    """BranchATools over the shared PolicyTools, and the rate-option counter QuoteGuard needs."""
    paths = install_overlay()
    from branch_a_tools import BranchATools
    from rate_resolver import build_resolver

    resolver = build_resolver(paths)
    rates = BranchATools(policy, resolver)

    def rate_options(rule_id):
        rule = rates._rule(rule_id)
        if rule is None:
            return 1
        entry, _, _ = resolver.m.service_entry(rates.plan, rule["service"], rule.get("setting"), rule.get("network"))
        return len(entry["options"]) if entry else 1

    return rates, rate_options


def _validate(args):
    for key in ("rule_id", "quote_id"):
        if key in args and not isinstance(args[key], str):
            raise ValueError(f"{key} must be a string.")
    if args.get("option_id") is not None and not isinstance(args["option_id"], str):
        raise ValueError("option_id must be a string or null.")
    value = args.get("remaining_deductible")
    if value is not None and (type(value) not in (int, float) or value < 0):
        raise ValueError("remaining_deductible must be a nonnegative number or null.")


def build(rates):
    meta, body = read_skill_md(Path(__file__).with_name("SKILL.md"))
    tools = [
        Tool(
            name="get_allowed_amount",
            description=("Look up the allowed amount for a benefit rule you have read, in the price database. "
                         "Returns a quote with a quote_id; Python keeps the amount. Pass option_id null unless "
                         "the user just chose an option."),
            parameters=schema(rule_id={"type": "string"}, option_id={"type": ["string", "null"]}),
            run=lambda args: rates.resolve_rate(args["rule_id"], option_id=args["option_id"]),
            validate=_validate,
        ),
        Tool(
            name="calculate_with_quote",
            description=("Calculate what the user pays for one occurrence, using a usable quote from "
                         "get_allowed_amount. Pass null for the remaining deductible if the user hasn't given it."),
            parameters=schema(
                rule_id={"type": "string"},
                quote_id={"type": "string"},
                remaining_deductible={"type": ["number", "null"], "minimum": 0},
            ),
            run=lambda args: rates.calculate_with_quote(args["rule_id"], args["quote_id"], args["remaining_deductible"]),
            validate=_validate,
        ),
    ]
    return Skill(name=meta["name"], description=meta["description"], instructions=body, tools=tools)

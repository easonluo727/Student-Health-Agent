"""Plans skill: one-visit cost estimates and the plan Arena, on plan_db.PlanDB (no LLM in the numbers).

  list_plans      the plans in the database, with tiers, deductible and out-of-pocket max
  estimate_cost   PlanDB.estimate: plan + scenario + network (+ tier, remaining deductible) -> patient cost
  compare_plans   PlanDB.compare: several plans x scenarios, deductible not met / met, ranked

The model chooses from fixed options (plan ids, scenario keys); every amount comes from the databases.
"""

from pathlib import Path

from plan_db import PlanDB

from .. import Skill, read_skill_md
from ...mcp import Tool, schema


def _validate(db, args):
    plans = {p["plan_id"] for p in db.plans()}
    for plan_id in args.get("plan_ids") or [args.get("plan_id")]:
        if plan_id not in plans:
            raise ValueError(f"Unknown plan_id {plan_id!r}; use one from list_plans.")
    value = args.get("remaining_deductible")
    if value is not None and (type(value) not in (int, float) or value < 0):
        raise ValueError("remaining_deductible must be a nonnegative number or null.")
    if args.get("network_tier") is not None and not isinstance(args["network_tier"], str):
        raise ValueError("network_tier must be a string or null.")
    if "plan_ids" in args and not 1 <= len(args["plan_ids"]) <= 12:
        raise ValueError("Compare between 1 and 12 plans.")
    if "scenarios" in args and not 1 <= len(args["scenarios"]) <= 10:
        raise ValueError("Use between 1 and 10 scenarios.")


def plan_summary(db):
    return [{k: p[k] for k in ("plan_id", "plan_name", "insurer", "plan_year", "plan_type", "deductible_in_usd",
                               "oop_max_in_usd", "in_network_tiers")} for p in db.plans()]


def build(db: PlanDB):
    scenarios = [s["scenario"] for s in db.scenarios()]
    meta, body = read_skill_md(Path(__file__).with_name("SKILL.md"), SCENARIOS="\n".join(
        f"- `{s['scenario']}`: {s['label']}" for s in db.scenarios()))
    scenario_enum = {"type": "string", "enum": scenarios}
    tools = [
        Tool(
            name="list_plans",
            description="List the plans in the database: id, name, insurer, type, deductible, out-of-pocket max, tiers.",
            parameters=schema(),
            run=lambda args: {"status": "found", "plans": plan_summary(db)},
        ),
        Tool(
            name="estimate_cost",
            description=("What the user pays for one occurrence of a scenario under one plan. Python matches the "
                         "benefit, prices it from the plan's network and runs the calculator."),
            parameters=schema(
                plan_id={"type": "string"},
                scenario=scenario_enum,
                network={"type": "string", "enum": ["in", "out"]},
                network_tier={"type": ["string", "null"]},
                remaining_deductible={"type": ["number", "null"], "minimum": 0},
            ),
            run=lambda args: db.estimate(args["plan_id"], args["scenario"], args["network"], args["network_tier"],
                                         args["remaining_deductible"]),
            validate=lambda args: _validate(db, args),
        ),
        Tool(
            name="compare_plans",
            description=("Arena: compare plans on the same scenarios (one occurrence each). Returns each plan's total "
                         "with the deductible not met and already met, ranked."),
            parameters=schema(
                plan_ids={"type": "array", "items": {"type": "string"}},
                scenarios={"type": "array", "items": scenario_enum},
            ),
            run=lambda args: db.compare(args["plan_ids"], args["scenarios"]),
            validate=lambda args: _validate(db, args),
        ),
    ]
    return Skill(name=meta["name"], description=meta["description"], instructions=body, tools=tools)

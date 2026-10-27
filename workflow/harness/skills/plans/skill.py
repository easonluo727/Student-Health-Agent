"""Plans skill: one-visit cost estimates and the plan Arena, on plan_db.PlanDB (no LLM in the numbers).

  list_plans      the plans in the database, with tiers, deductible and out-of-pocket max
  estimate_cost   PlanDB.estimate: plan + scenario + network (+ tier, remaining deductible) -> patient cost. An office
                  or specialist visit is priced at the nearest doctors with their own rate (estimate_nearby)
  compare_plans   PlanDB.compare: a year of care under several plans (each visit priced as often as the user expects
                  it, the deductible used up across the year, capped at the out-of-pocket max) plus a year's premium,
                  ranked by the user's rated priorities (plan_db.rank_plans). Office and specialist visits are priced
                  at the nearest doctor with a rate in each plan's network. The premium inputs, ZIP, visit counts,
                  priorities and budget come from the questionnaire answers (hooks.py), never from the model

The model chooses from fixed options (plan ids, scenario keys, specialty); every amount comes from the databases.
"""

from pathlib import Path

from plan_db import DOCTOR_SCENARIOS, PlanDB, visit_counts
from provider_directory import (SPECIALTIES, ProviderDirectory, check_specialty, doctor_finder, estimate_nearby,
                                visit_taxonomies)

from .. import Skill, read_skill_md
from ...mcp import Tool, schema

NEEDS_SPECIALTY = ("Pass `specialty`: which kind of specialist the user will see (for example `pulmonology` for "
                   "asthma). A specialist visit is priced at nearby doctors of that specialty.")


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
    if "specialty" in args:
        check_specialty(args)
    zip_code = args.get("zip_code")
    if zip_code is not None and not (len(zip_code) == 5 and zip_code.isdigit()):
        raise ValueError("zip_code must be the user's five-digit ZIP, or null.")


def estimate(db, directory, args):
    if (args["scenario"] == "specialist_followup" and not args["specialty"] and args["network"] == "in"
            and db.priced_network(args["plan_id"], args["network_tier"])):
        return {"status": "invalid_input", "patient_cost": None, "message": NEEDS_SPECIALTY}
    return estimate_nearby(db, directory, args["plan_id"], args["scenario"], args["network"], args["network_tier"],
                           args["remaining_deductible"], args["zip_code"],
                           visit_taxonomies(args["scenario"], args["specialty"]))


def compare(db, directory, args):
    """The Arena. Office and specialist visits are priced at the nearest doctor with a rate in each plan's network,
    so they need the user's ZIP (asked for like the premium inputs) and, for a specialist, the specialty."""
    profile = args.get("profile") or {}
    counts = visit_counts(args["scenarios"], args.get("visits") or {})
    doctor_visits = DOCTOR_SCENARIOS & set(counts)
    if doctor_visits and not profile.get("zip"):
        # One form: the ZIP together with the premium inputs still missing.
        missing = {"zip"} | {f for plan_id in args["plan_ids"] for f in db.premium(
            plan_id, profile.get("age"), None, profile.get("student_level")).get("missing_fields", [])}
        return {"status": "needs_information", "missing_fields": sorted(missing),
                "message": "Office and specialist visits are priced at doctors near the user, so this needs their ZIP."}
    if "specialist_followup" in doctor_visits and not args["specialty"]:
        return {"status": "invalid_input", "message": NEEDS_SPECIALTY}
    find = None
    if doctor_visits:
        def taxonomies(scenario):
            return visit_taxonomies(scenario, args["specialty"] if scenario == "specialist_followup" else None)
        find = doctor_finder(directory, str(profile["zip"]), taxonomies)
    return db.compare(args["plan_ids"], args["scenarios"], profile, args.get("visits"), args.get("preferences"),
                      args.get("preferred_tiers"), find_doctor=find)


def plan_summary(db):
    out = []
    for p in db.plans():
        summary = {k: p[k] for k in ("plan_id", "plan_name", "insurer", "plan_year", "plan_type", "deductible_in_usd",
                                     "oop_max_in_usd", "in_network_tiers")}
        if rules := db.rules(p["plan_id"]):
            summary["rules"] = rules
        out.append(summary)
    return out


def build(db: PlanDB, directory: ProviderDirectory | None = None):
    directory = directory or ProviderDirectory()
    scenarios = [s["scenario"] for s in db.scenarios()]
    meta, body = read_skill_md(Path(__file__).with_name("SKILL.md"), SCENARIOS="\n".join(
        f"- `{s['scenario']}`: {s['label']}" for s in db.scenarios()))
    scenario_enum = {"type": "string", "enum": scenarios}
    specialty = {"type": ["string", "null"], "enum": [*SPECIALTIES, None]}
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
                         "benefit, prices it from the plan's network and runs the calculator. Office and specialist "
                         "visits are priced at the nearest doctors with their own rate, so they need the user's ZIP."),
            parameters=schema(
                plan_id={"type": "string"},
                scenario=scenario_enum,
                network={"type": "string", "enum": ["in", "out"]},
                network_tier={"type": ["string", "null"]},
                remaining_deductible={"type": ["number", "null"], "minimum": 0},
                zip_code={"type": ["string", "null"]},
                specialty=specialty,
            ),
            run=lambda args: estimate(db, directory, args),
            validate=lambda args: _validate(db, args),
        ),
        Tool(
            name="compare_plans",
            description=("Arena: compare a year under several plans. Pick the scenarios for the care the user expects; "
                         "Python counts each as often as the user said (once if they gave no count), and returns each "
                         "plan's care cost, premium, year total, rank and how the rank was computed. The user's "
                         "answers (age, ZIP, student level, visit counts, priorities, budget) are filled in by Python."),
            parameters=schema(
                plan_ids={"type": "array", "items": {"type": "string"}},
                scenarios={"type": "array", "items": scenario_enum},
                specialty=specialty,
            ),
            run=lambda args: compare(db, directory, args),
            validate=lambda args: _validate(db, args),
        ),
    ]
    return Skill(name=meta["name"], description=meta["description"], instructions=body, tools=tools)

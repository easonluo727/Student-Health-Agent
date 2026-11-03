"""Plans skill: one-visit cost estimates and the plan Arena, on plan_db.PlanDB (no LLM in the numbers).

  list_plans      the plans in the database, with tiers, deductible and out-of-pocket max
  estimate_cost   PlanDB.estimate: plan + scenario + network (+ tier, remaining deductible) -> patient cost. An office
                  or specialist visit is priced at the nearest doctors with their own rate (estimate_nearby)
  compare_plans   PlanDB.compare: a year of care under several plans (each visit priced as often as the user expects
                  it, the deductible used up across the year, capped at the out-of-pocket max) plus a year's premium,
                  ranked by the user's rated priorities (plan_db.rank_plans). Office and specialist visits are priced
                  at the nearest doctor with a rate in each plan's network. The premium inputs, ZIP, visit counts,
                  priorities and budget come from the questionnaire answers (hooks.py), never from the model
  link_network    links an uploaded plan to a network in the rate store, only with a quote (from the document or the
                  user's own message) that names the network (plan_db.PRINTED_NETWORKS); Python checks both
  set_premium     a premium for one plan that the user stated or their uploaded plan prints, for this conversation
                  only (Hooks.premium_overrides, saved with the session, never in the policy database); compare_plans
                  uses it, labelled "User provided" or with the document's name

The model chooses from fixed options (plan ids, scenario keys, specialty); every amount comes from the databases.
"""

from pathlib import Path

import re
from decimal import Decimal

from guards import numbers_in
from plan_db import DOCTOR_SCENARIOS, PRINTED_NETWORKS, PlanDB, visit_counts
from provider_directory import (SPECIALTIES, ProviderDirectory, check_specialty, doctor_finder, estimate_nearby,
                                visit_taxonomies)

from .. import Skill, read_skill_md
from ...mcp import Tool, schema
from ..documents.skill import document

MAX_QUOTE = 300
MAX_PREMIUM = {"month": 5000, "year": 60000}  # USD; more is a typo or a family's premium, not one person's

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
    result = db.compare(args["plan_ids"], args["scenarios"], profile, args.get("visits"), args.get("preferences"),
                        args.get("preferred_tiers"), find_doctor=find,
                        premium_overrides=args.get("premium_overrides"))
    if unpriced := [e["plan_id"] for e in result["plans"]
                    if e.get("premium", {}).get("status") == "unavailable" and e["plan_id"].startswith("upload_")]:
        result["premium_next_step"] = (
            f"No premium is on file for {', '.join(unpriced)}. If the user uploaded that plan, search_document for "
            "its premium (premium, rate, cost per year/month); when printed, call set_premium with the file_id and "
            "the exact wording, then compare_plans again. Otherwise ask the user what they pay.")
    return result


def _said(texts, quote):
    """Whether the user typed this wording (any spacing, any case) in one of their messages."""
    pattern = re.compile(r"\s+".join(map(re.escape, quote.split())), re.I)
    return any(pattern.search(text) for text in texts)


def _validate_link(db, files, hooks, args):
    plan = db.plan(args["plan_id"])
    if plan is None or not args["plan_id"].startswith("upload_"):
        raise ValueError("link_network is for uploaded plans (plan_id upload_...); database plans are already linked.")
    tiers, tier = plan["in_network_tiers"], args["network_tier"]
    if tiers and tier not in tiers:
        raise ValueError(f"network_tier must be one of this plan's in-network tiers: {', '.join(tiers)}.")
    if not tiers and tier is not None:
        raise ValueError("This plan has one in-network level; pass network_tier null.")
    if link := db.network(args["plan_id"], tier):
        raise ValueError(f"This plan is already linked to {link['mrf_plan_key']}.")
    available = db.rate_networks()
    if args["network"] not in available:
        raise ValueError("The price database has no rates for that network"
                         + (f"; it has: {', '.join(available)}." if available else " (no rate store here)."))
    quote = args["quote"].strip()
    if not quote or len(quote) > MAX_QUOTE:
        raise ValueError(f"quote must be the exact wording that names the network, at most {MAX_QUOTE} characters.")
    pattern, name = PRINTED_NETWORKS[args["network"]]
    if not pattern.search(quote):
        raise ValueError(f"The quote does not name {name}. Quote the words that name the network, or ask the user "
                         "which network is on their insurance card.")
    if args["file_id"] is not None:
        if not document(files, args["file_id"]).contains(quote):
            raise ValueError("That quote is not in the document; copy it exactly as printed.")
    elif not _said(hooks.user_texts, quote):
        raise ValueError("The user did not write that; quote their message exactly, or pass the document's file_id.")


def link(db, files, args):
    quote = " ".join(args["quote"].split())
    source = f"uploaded document {files[args['file_id']]['name']}" if args["file_id"] else "the user's message"
    db.link_uploaded_network(args["plan_id"], args["network_tier"], args["network"],
                             f'Linked by the agent from {source}: "{quote}"')
    name = PRINTED_NETWORKS[args["network"]][1]
    return {"status": "linked", "plan_id": args["plan_id"], "network_tier": args["network_tier"], "network": name,
            "message": (f"Visits under this plan are now priced from {name}'s negotiated rates. Run estimate_cost or "
                        "compare_plans again. Tell the user the prices come from that network's published rates "
                        "and the plan itself is still unverified."),
            "_step": f"Linked the plan to {name} prices"}


def _validate_premium(db, files, hooks, args):
    if db.plan(args["plan_id"]) is None:
        raise ValueError(f"Unknown plan_id {args['plan_id']!r}; use one from list_plans.")
    amount = args["amount"]
    if type(amount) not in (int, float) or not 0 < amount <= MAX_PREMIUM[args["period"]]:
        raise ValueError(f"amount must be one person's premium per {args['period']}, above 0 and at most "
                         f"${MAX_PREMIUM[args['period']]:,}.")
    quote = args["quote"].strip()
    if not quote or len(quote) > MAX_QUOTE:
        raise ValueError(f"quote must be the exact wording that states this premium, at most {MAX_QUOTE} characters.")
    if args["file_id"] is not None:
        if not document(files, args["file_id"]).contains(quote):
            raise ValueError("That quote is not in the document; copy the premium's wording exactly as printed.")
    elif not _said(hooks.user_texts, quote):
        raise ValueError("quote must be the user's own words that state this premium, copied from their message, "
                         "or the document's wording with its file_id.")
    if Decimal(str(amount)).normalize() not in {n.normalize() for n in numbers_in(quote)}:
        raise ValueError("The quote does not contain that amount; pass the amount exactly as the user wrote it.")


def set_premium(db, files, hooks, args):
    name = files[args["file_id"]]["name"] if args["file_id"] else None
    premium = db.user_premium(args["amount"], args["period"], " ".join(args["quote"].split()), name)
    hooks.premium_overrides[args["plan_id"]] = premium
    return {"status": "saved", "plan_id": args["plan_id"], "premium": premium,
            "message": ("Saved for this conversation only; compare_plans now uses it for this plan, labelled as "
                        + (f"printed in {name} (unverified)" if name else "provided by the user")
                        + ". Run compare_plans again to update the ranking.")}


def plan_summary(db):
    out = []
    for p in db.plans():
        summary = {k: p[k] for k in ("plan_id", "plan_name", "insurer", "plan_year", "plan_type", "deductible_in_usd",
                                     "oop_max_in_usd", "in_network_tiers", "verified")}
        summary["uploaded"] = p["plan_id"].startswith("upload_")
        if summary["uploaded"]:
            summary["price_network_linked"] = bool(p["priced_tiers"])
        if rules := db.rules(p["plan_id"]):
            summary["rules"] = rules
        out.append(summary)
    return out


def build(db: PlanDB, directory: ProviderDirectory | None = None, files: dict | None = None, hooks=None):
    directory = directory or ProviderDirectory()
    files = {} if files is None else files
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
        Tool(
            name="link_network",
            description=("Link an uploaded plan to its provider network in the price database, so deductible and "
                         "coinsurance visits can be priced. Needs a quote that names the network: from the document "
                         "(file_id) or from the user's own message (file_id null). Python checks the quote."),
            parameters=schema(
                plan_id={"type": "string"},
                network_tier={"type": ["string", "null"]},
                network={"type": "string", "enum": list(PRINTED_NETWORKS)},
                file_id={"type": ["string", "null"]},
                quote={"type": "string"},
            ),
            run=lambda args: link(db, files, args),
            validate=lambda args: _validate_link(db, files, hooks, args),
        ),
        Tool(
            name="set_premium",
            description=("Use a premium for one plan in compare_plans, for this conversation only: one the user stated "
                         "(for example after a subsidy; file_id null, their exact words), or one printed in an "
                         "uploaded plan document (its file_id, the exact printed wording). Pass the amount and period "
                         "as written; Python works out the year."),
            parameters=schema(
                plan_id={"type": "string"},
                amount={"type": "number"},
                period={"type": "string", "enum": ["month", "year"]},
                file_id={"type": ["string", "null"]},
                quote={"type": "string"},
            ),
            run=lambda args: set_premium(db, files, hooks, args),
            validate=lambda args: _validate_premium(db, files, hooks, args),
        ),
    ]
    return Skill(name=meta["name"], description=meta["description"], instructions=body, tools=tools)

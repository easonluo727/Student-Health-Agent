"""Providers skill: specific doctors near the user, each priced under the user's plans (no LLM in the numbers).

  find_providers   provider_directory.ProviderDirectory.search: NPPES practice locations within a radius of the
                   user's ZIP, individuals only, each priced with that NPI's own negotiated rate (strict_npi; never
                   a network median) and the plan's benefit through the calculator.

The model chooses from fixed options (scenario, specialty, plan ids) and passes the user's ZIP as stated.
"""

from pathlib import Path

from plan_db import PlanDB
from provider_directory import ProviderDirectory

from .. import Skill, read_skill_md
from ...mcp import Tool, schema

# NUCC Health Care Provider Taxonomy codes (nucc.org); a code ending in X is exact, a shorter one is a prefix that
# includes the subspecialties.
SPECIALTIES = {
    "family_medicine": "207Q",
    "internal_medicine": "207R00000X",
    "pediatrics": "2080",
    "obstetrics_gynecology": "207V",
    "dermatology": "207N",
    "psychiatry": "2084P0800X",
    "allergy_immunology": "207K",
    "pulmonology": "207RP1001X",
    "cardiology": "207RC0000X",
    "orthopaedic_surgery": "207X",
    "psychologist": "103T",
    "clinical_social_worker": "1041C0700X",
    "physical_therapist": "2251",
}

KEEP = ("npi", "name", "primary_taxonomy", "address", "phone", "distance_miles", "latitude", "longitude",
        "geocode_precision", "map_destination")
KEEP_PRICE = ("estimate_status", "rate_status", "negotiated_rate", "patient_cost", "coverage_status",
              "missing_fields", "billing_code", "benefit_source")


def _validate(db, args):
    plans = {p["plan_id"] for p in db.plans()}
    if not (len(args["zip_code"]) == 5 and args["zip_code"].isdigit()):
        raise ValueError("zip_code must be the user's five-digit ZIP.")
    if len(args["plan_ids"]) > 4:
        raise ValueError("Price at most 4 plans at once.")
    for plan_id in args["plan_ids"] + [t["plan_id"] for t in args["plan_tiers"]]:
        if plan_id not in plans:
            raise ValueError(f"Unknown plan_id {plan_id!r}; use one from list_plans.")
    if not 1 <= args["radius_miles"] <= 50:
        raise ValueError("radius_miles must be between 1 and 50.")
    value = args["remaining_deductible"]
    if value is not None and value < 0:
        raise ValueError("remaining_deductible must be a nonnegative number or null.")


def compact(result: dict) -> dict:
    """Only what the model needs to recommend and explain; the full record stays in the directory."""
    if result.get("status") != "ok":
        return result
    return {
        "status": "ok",
        "total": result["total"],
        "user_position": result["user_position"],
        "plans": [{k: p[k] for k in ("plan_id", "name", "network_tier", "plan_network_status", "available_tiers")}
                  for p in result["plans"]],
        "providers": [{**{k: item.get(k) for k in KEEP},
                       "prices": {plan_id: {k: cell.get(k) for k in KEEP_PRICE if cell.get(k) is not None}
                                  for plan_id, cell in (item.get("prices") or {}).items()}}
                      for item in result["providers"]],
        "message": result["message"],
    }


def find(directory: ProviderDirectory, args: dict) -> dict:
    return compact(directory.search(
        zip_code=args["zip_code"], radius_miles=args["radius_miles"], scenario=args["scenario"],
        taxonomy=SPECIALTIES[args["specialty"]] if args["specialty"] else None, plan_ids=args["plan_ids"],
        plan_tiers={t["plan_id"]: t["network_tier"] for t in args["plan_tiers"]},
        remaining_deductible=args["remaining_deductible"], limit=args["limit"], sort=args["sort"]))


def build(db: PlanDB, directory: ProviderDirectory | None = None):
    directory = directory or ProviderDirectory()
    meta, body = read_skill_md(Path(__file__).with_name("SKILL.md"))
    tool = Tool(
        name="find_providers",
        description=("Specific doctors near the user's ZIP, nearest first (or cheapest), each with their own "
                     "negotiated rate and what the user pays under each chosen plan. Individuals only."),
        parameters=schema(
            zip_code={"type": "string"},
            scenario={"type": "string", "enum": [s["scenario"] for s in db.scenarios()]},
            specialty={"type": ["string", "null"], "enum": [*SPECIALTIES, None]},
            radius_miles={"type": "number"},
            plan_ids={"type": "array", "items": {"type": "string"}},
            plan_tiers={"type": "array", "items": schema(plan_id={"type": "string"}, network_tier={"type": "string"})},
            remaining_deductible={"type": ["number", "null"]},
            sort={"type": "string", "enum": ["nearest", "lowest_cost"]},
            limit={"type": "integer", "minimum": 1, "maximum": 20},
        ),
        run=lambda args: find(directory, args),
        validate=lambda args: _validate(db, args),
    )
    return Skill(name=meta["name"], description=meta["description"], instructions=body, tools=[tool])

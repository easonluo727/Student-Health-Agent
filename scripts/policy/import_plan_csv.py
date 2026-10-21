"""Turn human-checked plan tables (e.g. from a Perplexity search) into the policy database. No LLM.

    python scripts/policy/import_plan_csv.py plans.csv benefits.csv

Writes policies/plans.json (plan-level facts) and policies/rules.json (one rule per plan x service x network,
in the rule schema calculate_cost reads). build_seed.py reuses its CSV helpers (read, num, yes).

A row is usable for calculation only when its `verified` column is "yes" (a person checked the number against
the official source). Every other row is stored with needs_review = true. Bad rows are listed and skipped.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "policies"
SERVICES = {"primary_care_visit", "specialist_visit", "urgent_care", "emergency_room", "ambulance", "lab_tests",
            "xray", "advanced_imaging", "generic_drugs", "preferred_brand_drugs", "mental_health_outpatient",
            "preventive_care"}
SETTING = {"emergency_room": "outpatient", "ambulance": "other", "generic_drugs": "outpatient",
           "preferred_brand_drugs": "outpatient"}
PLAN_NUMBERS = ("premium_monthly_usd", "deductible_in_usd", "deductible_out_usd", "oop_max_in_usd",
                "oop_max_out_usd", "coinsurance_in_pct")


def num(v, field):
    v = (v or "").strip().replace("$", "").replace(",", "").replace("%", "")
    if v.upper() in ("", "NOT FOUND", "N/A", "NONE"):
        return None
    x = float(v)
    if x < 0 or (field.endswith("pct") and x > 100):
        raise ValueError(f"{field}={v}")
    return int(x) if x.is_integer() else x


def yes(v):
    v = (v or "").strip().lower()
    return True if v in ("yes", "y", "true") else False if v in ("no", "n", "false") else None


def read(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def main(plans_csv, benefits_csv):
    problems, plans = [], {}
    for i, r in enumerate(read(plans_csv), 2):
        try:
            plans[r["plan_id"]] = {**{k: r.get(k) or None for k in ("plan_id", "insurer", "plan_name", "plan_year",
                                                                   "plan_type", "network_name", "eligibility",
                                                                   "premium_assumption", "source_url")},
                                   **{k: num(r.get(k), k) for k in PLAN_NUMBERS},
                                   "verified": yes(r.get("verified")) is True}
        except (KeyError, ValueError) as e:
            problems.append(f"plans.csv line {i}: {e}")
    rules = []
    for i, r in enumerate(read(benefits_csv), 2):
        try:
            if r["plan_id"] not in plans:
                raise ValueError(f"unknown plan_id {r['plan_id']!r}")
            if r["service"] not in SERVICES:
                raise ValueError(f"unknown service {r['service']!r}")
            if r["network"] not in ("in", "out"):
                raise ValueError("network must be in or out")
            pct = num(r.get("coinsurance_pct"), "coinsurance_pct")
            covered = yes(r.get("covered"))
            rules.append({
                "plan": r["plan_id"], "service": r["service"], "setting": SETTING.get(r["service"], "outpatient"),
                "network": "in-network" if r["network"] == "in" else "out-of-network",
                "copay_usd": num(r.get("copay_usd"), "copay_usd"),
                "patient_pays_percentage": pct, "plan_pays_percentage": None if pct is None else 100 - pct,
                "deductible_applies": yes(r.get("deductible_applies")),
                "prior_authorization": {True: "required", False: "not_required"}.get(yes(r.get("prior_auth")),
                                                                                     "not_stated"),
                "coverage_status": {True: "covered", False: "not_covered"}.get(covered, "unknown"),
                "limits_conditions": r.get("limits_conditions") or None,
                "source_text": r.get("quote") or None, "source_url": r.get("source_url") or None,
                "source_page": num(r.get("source_page"), "source_page"),
                "needs_review": not (yes(r.get("verified")) is True and plans[r["plan_id"]]["verified"]),
            })
        except (KeyError, ValueError) as e:
            problems.append(f"benefits.csv line {i}: {e}")
    OUT.mkdir(exist_ok=True)
    (OUT / "plans.json").write_text(json.dumps(list(plans.values()), indent=2, ensure_ascii=False) + "\n")
    (OUT / "rules.json").write_text(json.dumps(rules, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(plans)} plans, {len(rules)} rules "
          f"({sum(not r['needs_review'] for r in rules)} verified) -> {OUT}")
    for p in problems:
        print("SKIPPED", p)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])

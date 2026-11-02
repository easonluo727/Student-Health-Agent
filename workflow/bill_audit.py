"""Check a read bill against the provider's own negotiated rates and the user's plan. No LLM.

For each line: which benefit pays it (development-time table below), this NPI's own negotiated rate for the code in
the plan's network (never a median), and the user's share through calculate_cost, with the remaining deductible used
in line order. A line is flagged when the bill asks for more than that share, when it repeats an earlier line, and
the bill when its lines don't add up to its total. Emergency care and drugs are outside the product (2026-10-27) and
are not reviewed; a code without a benefit mapping or a rate is "no data", never guessed.

audit(db, directory, bill, plan_id, tier, remaining_deductible) -> result dict (see _result).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from calculate_cost import calculate_cost
from plan_db import SCENARIOS, PlanDB, _encounters, rule_text
from provider_directory import PRIMARY_CARE, ProviderDirectory, _ro

CENT = Decimal("0.01")
EMERGENCY = {"99281", "99282", "99283", "99284", "99285"}
OFFICE_NEW = {"99202", "99203", "99204", "99205"}
OFFICE_ESTABLISHED = {"99212", "99213", "99214", "99215"}
# Billing codes outside encounters.csv, priced like an encounter component of the same kind and paid under its
# benefit. Reviewed 2026-11-01 against data/mrf/billing_codes.csv; every other code is "no data".
EXTRA_CODES = {
    "87502": ("urgent_care_flu", "flu_test"),   # influenza PCR, a lab test like the rapid flu test
    "98006": ("telehealth_visit", "visit"),
    "90715": ("flu_shot", "vaccine"),           # Tdap: a preventive vaccine
    "70450": ("knee_mri", "mri"),               # head CT: advanced imaging, global or TC + 26
    "73600": ("chest_xray", "xray"),            # ankle X-rays: plain X-rays, global or TC + 26
    "73610": ("chest_xray", "xray"),
}


def _code_table() -> dict[str, tuple[str, str]]:
    """billing code -> (scenario, component) whose benefit pays it and whose billing class / POS prices it."""
    table: dict[str, tuple[str, str]] = {}
    encounters = _encounters()
    for scenario in SCENARIOS:  # dict order: office visits come before urgent care and the UTI visit
        for row in encounters.get(scenario, ("", []))[1]:
            if row["billing_class"] == "professional":
                for code in row["codes"].split("|"):
                    table.setdefault(code, (scenario, row["component"]))
    return {**table, **EXTRA_CODES}


def provider_record(directory: ProviderDirectory, npi: str | None) -> dict | None:
    """Name and specialty on record (NPPES) for this NPI, or None when unknown or the directory is missing."""
    if not npi or not directory.providers_path.exists():
        return None
    with closing(_ro(directory.providers_path)) as con:
        row = con.execute("SELECT npi, display_name, primary_taxonomy, entity_type FROM providers WHERE npi=?",
                          (npi,)).fetchone()
        if row is None:
            return None
        location = con.execute("SELECT address_line_1, city, state, postal_code, phone FROM provider_locations "
                               "WHERE npi=? ORDER BY location_type='primary' DESC LIMIT 1", (npi,)).fetchone()
    address = ", ".join(filter(None, [location["address_line_1"], location["city"], location["state"],
                                      location["postal_code"]])) if location else None
    return {"npi": row["npi"], "name": row["display_name"], "primary_taxonomy": row["primary_taxonomy"],
            "organization": row["entity_type"] == "2", "address": address,
            "phone": location["phone"] if location else None}


def _visit_scenario(code: str, taxonomy: str | None) -> str:
    """An office visit is a specialist visit when the doctor's NPPES specialty is not primary care."""
    if taxonomy and not taxonomy.startswith(PRIMARY_CARE):
        return "specialist_followup"
    return "office_visit_new" if code in OFFICE_NEW else "office_visit_established"


def _usd(amount) -> str:
    return f"${amount:,.2f}"


def audit(db: PlanDB, directory: ProviderDirectory, bill: dict, plan_id: str, tier: str | None,
          remaining_deductible: float | None) -> dict:
    plan = db.plan(plan_id)
    if plan is None:
        return {"status": "invalid_input", "message": f"Unknown plan {plan_id!r}; use one from list_plans."}
    if tier is None and plan["in_network_tiers"]:
        return {"status": "needs_information", "missing_fields": ["network_tier"], "options": plan["in_network_tiers"],
                "message": "This plan has several in-network tiers; ask which one the provider is in."}
    link = db.network(plan_id, tier)
    npi = bill.get("provider_npi")
    provider = provider_record(directory, npi)
    unpriced = None
    if not npi:
        unpriced = "The bill shows no NPI, so this provider's own negotiated rates can't be looked up."
    elif link is None or not link["confirmed"]:
        unpriced = "This plan tier has no confirmed price network, so negotiated rates can't be looked up."
    elif db.rates_path is None:
        unpriced = "The rate store is not available on this server."

    codes = _code_table()
    encounters = _encounters()
    remaining = None if remaining_deductible is None else Decimal(str(remaining_deductible))
    seen: dict[tuple, int] = {}
    lines, findings = [], []
    for number, item in enumerate(bill["lines"], start=1):
        code, billed = item.get("code"), item.get("billed")
        line = {"line": number, "service_date": item.get("service_date"), "code": code,
                "description": item.get("description"), "units": item.get("units"), "billed": billed,
                "check": None, "negotiated_rate": None, "benefit": None, "rule": None, "you_owe": None,
                "possible_overcharge": None, "note": None, "source_page": None, "quote": None}
        lines.append(line)
        key = (item.get("service_date"), code, billed)
        if code and key in seen:
            line.update(check="possible_duplicate", you_owe=0.0, possible_overcharge=billed,
                        note=f"Same code, date and amount as line {seen[key]}.")
            findings.append({"kind": "possible_duplicate", "line": number, "amount": billed,
                             "message": f"Line {number} repeats line {seen[key]} ({code}, {item.get('service_date')})."})
            continue
        seen[key] = number
        if code is None or billed is None:
            line.update(check="unreadable", note="The code or amount could not be read from the photo.")
            continue
        if code in EMERGENCY or code.startswith("J"):
            line.update(check="not_reviewed", note="Emergency care and drugs are not reviewed.")
            continue
        if code in OFFICE_NEW | OFFICE_ESTABLISHED:
            scenario, component = _visit_scenario(code, (provider or {}).get("primary_taxonomy")), "visit"
        elif code in codes:
            scenario, component = codes[code]
        else:
            line.update(check="no_data", note="This code is not in the price database.")
            continue
        part = SCENARIOS[scenario][component]
        row = db.benefit(plan_id, part.service, "in", tier, part.component, scenario)
        if row is None and part.fallback:
            row = db.benefit(plan_id, part.fallback, "in", tier, part.component, scenario)
        if row is None:
            line.update(check="no_benefit", note=f"The plan has no {part.service.replace('_', ' ')} benefit here.")
            continue
        line.update(benefit=row["service_as_printed"], rule=rule_text(row),
                    source_page=row["source_page"], quote=(row["quote"] or "")[:300] or None)
        if unpriced:
            line.update(check="no_rate", note=unpriced)
            continue
        encounter_row = next(r for r in encounters[scenario][1] if r["component"] == component)
        rate = db._price(link["mrf_plan_key"], {**encounter_row, "codes": code}, npi)
        if rate is None or rate.allowed_amount is None:
            line.update(check="no_rate", note="This provider has no negotiated rate for this code in the plan's "
                                              "network (they may be out of network).")
            continue
        units = item.get("units") or 1
        allowed = Decimal(str(rate.allowed_amount)) * Decimal(str(units))
        rule = db._rule(row)
        uses_deductible = rule["deductible_applies"] is True
        cost = calculate_cost(rule, float(allowed),
                              float(remaining) if (uses_deductible and remaining is not None) else None)
        line["negotiated_rate"] = float(allowed.quantize(CENT))
        line["mrf_file_dates"] = rate.mrf_file_dates
        if cost["status"] != "estimated":
            line.update(check=cost["status"], note=cost["message"], missing_fields=cost["missing_fields"])
            continue
        owe = Decimal(str(cost["patient_cost"]))
        if uses_deductible and remaining is not None:
            remaining -= min(remaining, allowed)
        line.update(check="ok", you_owe=float(owe))
        over = Decimal(str(billed)) - owe
        if over > CENT / 2:
            line.update(check="billed_above_your_share", possible_overcharge=float(over.quantize(CENT)),
                        note=f"Billed {_usd(billed)}; negotiated rate {_usd(allowed)}; your share {_usd(owe)}.")
            findings.append({"kind": "billed_above_your_share", "line": number, "amount": float(over.quantize(CENT)),
                             "message": f"Line {number} ({code}) bills {_usd(billed)}, but under your plan you owe "
                                        f"{_usd(owe)} at this provider's negotiated rate of {_usd(allowed)}."})

    billed_sum = sum((Decimal(str(l["billed"])) for l in lines if l["billed"] is not None), Decimal(0))
    total = bill.get("total_billed")
    if total is not None and abs(Decimal(str(total)) - billed_sum) > CENT / 2:
        findings.append({"kind": "total_mismatch", "line": None, "amount": float(Decimal(str(total)) - billed_sum),
                         "message": f"The lines add up to {_usd(billed_sum)}, but the bill's total is {_usd(total)}."})

    missing = sorted({f for l in lines for f in l.get("missing_fields") or []})
    reviewed = [l for l in lines if l["check"] in ("ok", "billed_above_your_share", "possible_duplicate")]
    has_rate = any(l["negotiated_rate"] is not None for l in lines)
    if missing:
        status, message = "needs_information", "Ask for: " + ", ".join(missing) + "."
    elif not reviewed:
        status, message = "unavailable", unpriced or ("None of the lines could be checked: no negotiated rate or "
                                                      "benefit was found for any of them.")
    else:
        status, message = "audited", ("Checked against this provider's own negotiated rates; assumes the billed "
                                      "amounts are what you are asked to pay. Not a coverage guarantee.")
    if npi and not unpriced and not has_rate and not missing:
        message += " This provider has no negotiated rates in this network for these codes; they may be out of network."
    owed = [Decimal(str(l["you_owe"])) for l in reviewed]
    overcharge = sum((Decimal(str(l["possible_overcharge"])) for l in lines if l["possible_overcharge"]), Decimal(0))
    return {
        "status": status, "message": message, "missing_fields": missing,
        "plan_id": plan_id, "plan_name": plan["plan_name"], "network_tier": tier,
        "network": (link or {}).get("mrf_network") or (link or {}).get("mrf_plan_key"),
        "provider": {"name_on_bill": bill.get("provider_name"), "npi": npi,
                     "name_on_record": (provider or {}).get("name"), "address": (provider or {}).get("address"),
                     "phone": (provider or {}).get("phone"), "in_directory": provider is not None},
        "statement_date": bill.get("statement_date"),
        "lines": lines, "findings": findings,
        "totals": {"billed": float(billed_sum), "lines_checked": len(reviewed), "lines": len(lines),
                   "you_owe_on_checked_lines": float(sum(owed, Decimal(0))) if reviewed else None,
                   "possible_overcharge": float(overcharge)},
    }

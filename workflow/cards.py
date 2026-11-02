"""Pages for the canvas, built by Python from a tool result the agent chose to show (harness/skills/pages).

The agent picks the result, which items to recommend and a sentence on each; everything else here, the layout and
every number, comes from the stored tool result. No LLM.

  doctors    find_providers   map, the agent's picks with their reasons, then everyone else (event: page)
  compare    compare_plans    the Arena report (event: report, arena_report.py)
  estimate   estimate_cost    one visit's cost, receipt-style (event: estimate)
  bill       audit_bill       the bill line by line (event: estimate)
"""

import re

from arena_report import arena_report

VERIFY_NOTE = ("Locations come from the national provider registry (NPPES). A price means the doctor has a negotiated "
               "rate in the plan's network; it doesn't prove they take the plan today. Call the office before booking.")


def render(kind, page_id, stored, highlight=(), notes=None, title=None, plan_names=None):
    """(SSE event, data) for one page. page_id is the result id, so showing a result again replaces its page."""
    result = stored["result"]
    if kind == "doctors":
        return "page", doctors_page(page_id, stored, list(highlight), notes or {}, title)
    if kind == "compare":
        return "report", {"report": arena_report(result)}
    if kind == "estimate":
        return "estimate", estimate_card(page_id, result, plan_names or {})
    if kind == "bill":
        return "estimate", bill_card(page_id, result)
    raise ValueError(f"No page for {kind!r}.")


CREDENTIALS = {"MD", "DO", "NP", "PA", "PHD", "PSYD", "DPM", "DDS", "RN", "LCSW", "MFT", "DPT", "II", "III", "IV"}


def display_name(name):
    """NPPES names are often all capitals ("ABIGAIL MALLER"); show them the way people write them."""
    if not name or not name.isupper():
        return name
    return re.sub(r"[A-Z]+", lambda m: m.group(0) if m.group(0) in CREDENTIALS else m.group(0).capitalize(), name)


STATES = {"CA", "NY", "TX", "WA", "OR", "NV", "AZ"}


def display_address(address):
    """"10833 LE CONTE AVE, LOS ANGELES, CA, 900953075" -> "10833 Le Conte Ave, Los Angeles, CA 90095-3075"."""
    if not address or not address.isupper():
        return address
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 2 and re.fullmatch(r"\d{5}(\d{4})?", parts[-1]):
        zip9 = parts.pop()
        parts[-1] = f"{parts[-1]} {zip9[:5]}" + (f"-{zip9[5:]}" if len(zip9) == 9 else "")
    def words(m):  # keep states and vowel-less abbreviations ("CHS") in capitals
        w = m.group(0)
        return w if w in STATES or (len(w) <= 4 and not re.search(r"[AEIOUY]", w)) else w.capitalize()
    return ", ".join(re.sub(r"[A-Z]+", words, part) for part in parts)


def _specialty(args):
    return (args.get("specialty") or "").replace("_", " ").capitalize() or None


def doctors_page(page_id, stored, highlight, notes, title):
    """The agent's picks first, in its order, then the rest nearest first; one card per doctor (nearest location)."""
    result, args = stored["result"], stored["args"]
    picks = {npi: rank for rank, npi in enumerate(highlight, start=1)}
    specialty = _specialty(args)
    doctors, seen = [], set()
    for item in result["providers"]:
        if item["npi"] in seen:
            continue
        seen.add(item["npi"])
        doctors.append({
            "npi": item["npi"], "name": display_name(item["name"]), "specialty": specialty, "address": display_address(item["address"]),
            "phone": item.get("phone"), "distance_miles": item.get("distance_miles"),
            "lat": item.get("latitude"), "lng": item.get("longitude"), "directions_url": item.get("map_destination"),
            "pick": picks.get(item["npi"]), "note": notes.get(item["npi"]),
            "prices": {plan_id: {"patient_cost": cell.get("patient_cost"), "negotiated_rate": cell.get("negotiated_rate"),
                                 "status": cell.get("estimate_status")}
                       for plan_id, cell in (item.get("prices") or {}).items()},
        })
    doctors.sort(key=lambda d: d["pick"] or len(picks) + 1)  # stable: the rest keep their distance order
    position = result.get("user_position") or {}
    return {
        "page_id": page_id, "kind": "doctors",
        "title": title or f"{specialty or 'Doctors'} near {args['zip_code']}",
        "scenario": args.get("scenario"),
        "user_position": ({"lat": position["latitude"], "lng": position["longitude"], "label": f"ZIP {args['zip_code']}"}
                          if position.get("latitude") is not None else None),
        "plans": [{"plan_id": p["plan_id"], "name": p["name"]} for p in result["plans"]],
        "doctors": doctors,
        "note": VERIFY_NOTE,
    }


def estimate_card(page_id, result, plan_names):
    """One row per benefit the visit is paid under."""
    prices = result.get("allowed_amount") or {}
    plan_name = plan_names.get(result.get("plan_id"), result.get("plan_id"))
    rows = []
    doctor = result.get("doctor")
    if prices.get("total") is not None:
        spread = (f" (most between ${prices['low']:,.2f} and ${prices['high']:,.2f})"
                  if prices.get("low") is not None and prices.get("high") is not None else "")
        note = (f"{doctor['name']}'s own negotiated rate, {doctor['distance_miles']} mi away" if doctor else
                "Median negotiated rate across Los Angeles providers in the network" + spread)
        rows.append({"label": "Allowed amount", "amount": prices["total"], "note": note,
                     "source": {"kind": "mrf", "label": f"{prices.get('network')} price list, {prices.get('mrf_file_date')}"}})
    for line in result.get("lines") or []:
        rows.append({"label": line["benefit"], "amount": line["patient_cost"],
                     "note": (line.get("quote") or "")[:120] or line["status"],
                     "source": {"kind": "policy", "label": plan_name, "page": line.get("source_page"),
                                "quote": (line.get("quote") or "")[:300] or None}})
    cost = result.get("patient_cost")
    tier = result.get("network_tier")
    return {
        "estimate_id": page_id,
        "title": result.get("scenario_label") or "Estimate",
        "plan_name": plan_name,
        "status": result["status"],
        "patient_cost": {"mid": cost} if result["status"] == "estimated" and cost is not None else None,
        "rows": rows,
        "assumptions": [f"Network: {result.get('network')}" + (f" ({tier})" if tier else ""),
                        *([f"Priced at {doctor['name']}, {doctor['address']}"] if doctor else []),
                        *([f"Nearby doctors: ${spread['low']:,.2f} to ${spread['high']:,.2f}"]
                          if (spread := result.get("cost_range")) and spread["low"] != spread["high"] else []),
                        *([result["doctor_search"]] if result.get("doctor_search") and not doctor else [])],
        "message": result.get("message"),
    }


def bill_card(page_id, result):
    """The bill review card: one row per line, then the findings; amounts are what the user owes."""
    rows = []
    for line in result["lines"]:
        label = " · ".join(filter(None, [line["code"], line["description"]])) or f"Line {line['line']}"
        note = line["note"] or (f"Billed ${line['billed']:,.2f}; negotiated rate ${line['negotiated_rate']:,.2f}"
                                if line["negotiated_rate"] is not None else None)
        source = ({"kind": "mrf", "label": f"{result['network']} negotiated rate"} if line["negotiated_rate"] is not None
                  else {"kind": "user_message", "label": "Your bill photo"})
        rows.append({"label": label, "amount": line["you_owe"], **({"note": note} if note else {}), "source": source})
    for finding in result["findings"]:
        if finding["kind"] != "total_mismatch":
            rows.append({"label": f"Possible overcharge, line {finding['line']}", "amount": finding["amount"],
                         "note": finding["message"]})
    totals, provider = result["totals"], result["provider"]
    return {
        "estimate_id": page_id,
        "title": "Bill review",
        "plan_name": result["plan_name"],
        "status": "estimated" if result["status"] == "audited" else "needs_review",
        "patient_cost": {"mid": totals["you_owe_on_checked_lines"]}
        if result["status"] == "audited" and totals["you_owe_on_checked_lines"] is not None else None,
        "rows": rows,
        "assumptions": [
            f"Provider: {provider['name_on_record'] or provider['name_on_bill']}"
            + (f" (NPI {provider['npi']})" if provider["npi"] else ""),
            f"Network: {result['network']}" + (f" ({result['network_tier']})" if result["network_tier"] else ""),
            f"{totals['lines_checked']} of {totals['lines']} lines checked; you owe "
            f"${totals['you_owe_on_checked_lines'] or 0:,.2f} on them, "
            f"${totals['possible_overcharge']:,.2f} possibly overcharged",
            *[f["message"] for f in result["findings"] if f["kind"] == "total_mismatch"],
        ],
        "message": result["message"],
    }

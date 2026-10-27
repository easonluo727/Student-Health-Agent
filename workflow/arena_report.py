"""The Arena dashboard report (docs/frontend-protocol.md, Report) from a compare_plans result. No LLM and no new
numbers: every amount is copied from plan_db.compare, with the policy page and quote it came from, and every row
says how it was computed."""

import uuid


def _usd(amount):
    return f"${amount:,.2f}"


def _source(label, page, quote, url):
    if page is None and not quote and not url:
        return None
    return {"kind": "policy", "label": label, "page": page, "quote": (quote or "")[:300] or None, "url": url}


def _visit_cell(entry, visit):
    if visit["total"] is None:
        return {"text": visit["status"].replace("_", " "), "detail": visit["message"]}
    detail = [visit["math"]]
    if visit["network_tier"]:
        detail.append(visit["tier_note"] or f"Priced at {visit['network_tier']}")
    detail += [f"{line['benefit']}: {line['rule']}" for line in visit["lines"]]
    prices = visit["allowed_amount"] or {}
    doctor = visit.get("doctor")
    if prices.get("total") is not None and doctor:
        detail.append(f"Allowed amount {_usd(prices['total'])}: {doctor['name']}'s own negotiated rate in "
                      f"{prices['network']} ({prices['mrf_file_date']}), the nearest doctor with one, "
                      f"{doctor['distance_miles']} mi away")
    elif prices.get("total") is not None:
        detail.append(f"Allowed amount {_usd(prices['total'])}: median negotiated rate across Los Angeles providers in "
                      f"{prices['network']} ({prices['mrf_file_date']})")
    if visit.get("doctor_search"):
        detail.append(visit["doctor_search"])
    first = visit["lines"][0] if visit["lines"] else {}
    return {"text": _usd(visit["total"]), "detail": ". ".join(detail),
            "source": _source(entry["plan_name"], first.get("source_page"), first.get("quote"), first.get("source_url"))}


def arena_report(result):
    entries = sorted((e for e in result["plans"] if "care" in e),
                     key=lambda e: (e.get("rank") is None, e.get("rank") or 0))
    ranking = result["ranking"]
    by_premium = result["ranked_by"] != "visits_only"
    weighted = ranking["method"] == "weighted_score"

    plans = []
    for e in entries:
        premium = e["premium"]
        plans.append({
            "plan_id": e["plan_id"], "name": e["plan_name"], "insurer": e["insurer"],
            "network": " · ".join(filter(None, (e["plan_type"], e["network_tier"]))),
            "annual_premium": premium.get("annual"),
            "source": _source(f"{e['plan_name']} premium", premium.get("source_page"), premium.get("quote"),
                              premium.get("source_url")) if premium.get("annual") is not None else None,
        })

    # Chart tabs: a whole year under each plan.
    visits = ", ".join(f"{v['times']} x {v['label']}" + (" (assumed)" if v["assumed"] else "")
                       for v in result["visits"]) or "no visits"
    if by_premium:
        scenarios = [
            {"key": "expected", "label": "Your expected year",
             "description": f"Premium + your care ({visits}), capped at the out-of-pocket max.",
             "costs": {e["plan_id"]: {"mid": e["year_total"]} for e in entries if e["year_total"] is not None}},
            {"key": "healthy", "label": "A healthy year", "description": "Premium only, no visits.",
             "costs": {e["plan_id"]: {"mid": e["premium"]["annual"]} for e in entries
                       if e["premium"].get("annual") is not None}},
            {"key": "worst", "label": "A bad year", "description": "Premium + the out-of-pocket max.",
             "costs": {e["plan_id"]: {"mid": round(e["premium"]["annual"] + e["oop_max"]["amount"], 2)}
                       for e in entries
                       if e["premium"].get("annual") is not None and e["oop_max"]["amount"] is not None}},
        ]
    else:
        scenarios = [{"key": "expected", "label": "Your care (premiums missing)",
                      "description": f"Your care ({visits}), capped at the out-of-pocket max. Premiums are not "
                                     "included yet.",
                      "costs": {e["plan_id"]: {"mid": e["care"]["total"]} for e in entries
                                if e["care"]["total"] is not None}}]

    rows = []

    def row(key, label, cells, highlight=False):
        rows.append({"key": key, "label": label, "highlight": highlight, "cells": cells})

    rank_cells = {}
    for e in entries:
        if e.get("rank") is None:
            rank_cells[e["plan_id"]] = {"text": "Not ranked", "detail": e.get("not_ranked")}
            continue
        text = f"#{e['rank']}" + (f" · {e['score']['total']:g} / 100" if weighted else "")
        detail = None
        if (e.get("budget") or {}).get("over"):
            detail = (f"Premium {_usd(e['budget']['monthly_premium'])} a month is over your budget of "
                      f"{_usd(e['budget']['monthly_budget'])}.")
        rank_cells[e["plan_id"]] = {"text": text, "detail": detail}
    row("rank", "Rank", rank_cells, highlight=True)

    if weighted:
        for factor in (f for f in ranking["factors"] if f["used"]):
            cells = {}
            for e in entries:
                part = next((p for p in (e.get("score") or {}).get("parts", []) if p["factor"] == factor["factor"]),
                            None)
                if part:
                    cells[e["plan_id"]] = {"text": f"{part['points']:g} pts",
                                           "detail": f"{_usd(part['value'])}; adds {part['contribution']:g} to the score"}
            row(f"score.{factor['factor']}", f"{factor['label']} (weight {factor['weight']})", cells)

    for v in result["visits"]:
        cells = {}
        for e in entries:
            visit = next((x for x in e["visits"] if x["scenario"] == v["scenario"]), None)
            if visit:
                cells[e["plan_id"]] = _visit_cell(e, visit)
        row(f"visit.{v['scenario']}", f"{v['times']} x {v['label']}" + (" (assumed once)" if v["assumed"] else ""),
            cells, highlight=not v["assumed"])

    row("care", "Your care this year", {
        e["plan_id"]: {"text": _usd(e["care"]["total"]) if e["care"]["total"] is not None else "—",
                       "detail": e["care"]["message"]} for e in entries})
    row("deductible", "Deductible", {
        e["plan_id"]: {"text": _usd(e["deductible"]["amount"]) if e["deductible"]["amount"] is not None else "Not stated",
                       "source": _source(e["plan_name"], e["deductible"]["source_page"], e["deductible"]["quote"],
                                         e["deductible"]["source_url"])} for e in entries})
    row("oop_max", "Out-of-pocket max", {
        e["plan_id"]: {"text": _usd(e["oop_max"]["amount"]) if e["oop_max"]["amount"] is not None else "Not stated",
                       "source": _source(e["plan_name"], e["oop_max"]["source_page"], e["oop_max"]["quote"],
                                         e["oop_max"]["source_url"])} for e in entries})
    if any(e["network_tier"] for e in entries):
        row("tier", "Priced at", {e["plan_id"]: {"text": " + ".join(dict.fromkeys(
            v["network_tier"] for v in e["visits"] if v["network_tier"])) or e["network_tier"] or "—",
            "detail": e.get("tier_note")} for e in entries})

    recommendation = None
    ranked = [e for e in entries if e.get("rank") is not None]
    if by_premium and ranked:
        top = ranked[0]
        if weighted:
            headline = f"{top['plan_name']} ranks first for your priorities ({top['score']['total']:g} / 100)."
            parts = sorted(top["score"]["parts"], key=lambda p: -p["contribution"])
            reasons = [f"{p['label']}: {p['points']:g} points ({_usd(p['value'])})" for p in parts]
        else:
            headline = f"{top['plan_name']} has the lowest year total: {_usd(top['year_total'])}."
            reasons = []
        # A weighted ranking can skip the premium (e.g. UC SHIP without the student level), so the year total may be unknown.
        if None not in (top.get("year_total"), top["premium"].get("annual"), top["care"].get("total")):
            reasons.append(f"Year total {_usd(top['year_total'])} = premium {_usd(top['premium']['annual'])} + your care "
                           f"{_usd(top['care']['total'])}.")
        if (top.get("budget") or {}).get("over"):
            reasons.append("Every plan's premium is over your monthly budget.")
        recommendation = {
            "plan_id": top["plan_id"], "headline": headline, "reasons": reasons,
            "excluded": [{"plan_id": e["plan_id"], "reason": e.get("not_ranked") or "Not ranked."}
                         for e in entries if e.get("rank") is None],
        }

    return {"report_id": f"arena_{uuid.uuid4().hex[:8]}", "title": "Plan Arena", "recommendation": recommendation,
            "ranking": {k: ranking[k] for k in ("method", "summary", "factors")}, "note": result["note"],
            "plans": plans, "scenarios": scenarios, "comparison": rows}

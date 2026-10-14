"""Return the single allowed_amount for (insurer, network, encounter or code, optional NPI).

    from lookup import lookup_allowed_amount
    lookup_allowed_amount("Anthem Blue Cross (California)", "CA PPO PRUDENT BUYER", "er_ankle_xray")

    python scripts/mrf/lookup.py anthem_ca_prudent_buyer_ppo urgent_care_flu [--npi 1234567890]

Returns {allowed_amount, allowed_amount_basis, status, components, source}. An encounter is the
sum of its components (data/mrf/encounters.csv); if any component has no rate the encounter is
rate_unavailable. The calculator reads only `allowed_amount`.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import allowed  # noqa: E402

DB = os.path.join(ROOT, "data", "mrf", "rates.sqlite")
ENCOUNTERS = os.path.join(ROOT, "data", "mrf", "encounters.csv")
PROFILES = os.path.join(ROOT, "data", "plan_profiles.v1.json")
POS_FOR_CODE_LOOKUP = "11"  # a bare code lookup defaults to office, professional


def load_encounters(path: str | None = None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with open(path or ENCOUNTERS, newline="") as f:
        for row in csv.DictReader(f):
            out.setdefault(row["encounter"], []).append(row)
    return out


def resolve_plan_key(insurer: str, network: str | None, profiles_path: str | None = None) -> str:
    with open(profiles_path or PROFILES) as f:
        plans = json.load(f)["plans"]
    for p in plans:
        if insurer == p["plan_key"]:
            return p["plan_key"]
    for p in plans:
        names = [n.upper() for n in p.get("network_names") or []]
        if p["insurer"].lower() == insurer.lower() and (network is None or network.upper() in names):
            return p["plan_key"]
    raise KeyError(f"no plan profile for insurer={insurer!r} network={network!r}")


def _component(con, plan_key, row, npi, as_of):
    """Alternatives in `codes` (a|b) are tried in order; the first with a rate wins."""
    best = None
    for code in row["codes"].split("|"):
        r = allowed.component(con, plan_key, code, row["billing_class"], row["place_of_service"] or None,
                              row["modifier_rule"], npi, as_of)
        if r.allowed_amount is not None:
            return r
        best = best or r
    return best


def lookup_allowed_amount(insurer: str, network: str | None, encounter_or_code: str, npi: str | None = None,
                          db: str = DB, as_of: str | None = None, con: sqlite3.Connection | None = None) -> dict:
    plan_key = resolve_plan_key(insurer, network)
    encounters = load_encounters()
    own = con is None
    con = con or sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = encounters.get(encounter_or_code) or [{
            "component": encounter_or_code, "codes": encounter_or_code, "billing_class": "professional",
            "place_of_service": POS_FOR_CODE_LOOKUP, "modifier_rule": "global_or_tc_plus_26"}]
        comps = [(row, _component(con, plan_key, row, npi, as_of)) for row in rows]
    finally:
        if own:
            con.close()
    parts = [{"component": row["component"], "billing_code": r.billing_code, "billing_class": r.billing_class,
              "place_of_service": r.place_of_service, "allowed_amount": r.allowed_amount,
              "allowed_amount_basis": r.allowed_amount_basis, "status": r.status, "n_providers": r.n_providers,
              "p10": r.p10, "p90": r.p90, "ucla_health_rate": r.ucla_health_rate}
             for row, r in comps]
    sources = sorted({u for _, r in comps for u in r.source_urls})
    dates = sorted({d for _, r in comps for d in r.mrf_file_dates if d})
    missing = [p["component"] for p in parts if p["allowed_amount"] is None]
    if missing:
        return {"allowed_amount": None, "allowed_amount_basis": None, "status": "rate_unavailable",
                "components": parts, "missing_components": missing,
                "source": {"plan_key": plan_key, "mrf_file_dates": dates, "source_urls": sources}}
    basis = allowed.worst_basis([p["allowed_amount_basis"] for p in parts])
    return {"allowed_amount": round(sum(p["allowed_amount"] for p in parts), 2), "allowed_amount_basis": basis,
            "status": allowed.STATUS[basis], "components": parts,
            "source": {"plan_key": plan_key, "mrf_file_dates": dates, "source_urls": sources}}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Look up one allowed_amount.")
    ap.add_argument("insurer", help="plan_key or insurer name")
    ap.add_argument("encounter_or_code")
    ap.add_argument("--network")
    ap.add_argument("--npi")
    ap.add_argument("--db", default=DB)
    a = ap.parse_args(argv)
    print(json.dumps(lookup_allowed_amount(a.insurer, a.network, a.encounter_or_code, a.npi, a.db), indent=1))


if __name__ == "__main__":
    main()

"""Turn extracted plan tables into the policy database, a SQLite file with the tables in db/schema.sql. No LLM.

    python scripts/policy/build_seed.py [--src data/plans] [--out data/plans/policy.sqlite]

Input, under <src>:
  extracted/<plan_id>/plans.csv + benefits.csv   in the format of the SBC extraction prompt, kept as extracted
  sources.csv                                    one row per source document; a non-empty verified_by marks the
                                                 whole plan verified (rows can still opt out with verified=no)
  plan_networks.csv                              the bridge to the MRF rates; rows for plans not extracted yet wait
  premiums.csv, age_curve.csv, rating_regions.csv  written by scripts/policy/extract_premiums.py from official PDFs
Output: <out>, rebuilt from scratch each run (small, committed, deployed with the code).

Normalizing: NOT FOUND / blank / "none" -> NULL; yes/no -> 1/0; a component other than
office_visit/facility/provider (e.g. "asc") becomes the row's setting. Each benefit row gets a category from its
SBC section and review_flags for what the calculator cannot handle; calculable = verified and no flags.
Bad rows are listed and skipped. A duplicate natural key skips the whole plan, since the rows can't be told apart.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICES = {"primary_care_visit", "specialist_visit", "urgent_care", "emergency_room", "ambulance", "lab_tests",
            "xray", "advanced_imaging", "generic_drugs", "preferred_brand_drugs", "mental_health_outpatient",
            "preventive_care", "plan_level", "other"}
COMPONENTS = {"office_visit", "facility", "provider"}
PLAN_TYPES = {"HMO", "PPO", "EPO", "POS"}
# SBC "Common Medical Event" wording is fixed by the federal template; first match wins.
CATEGORIES = [("important questions", "plan_level"), ("excluded", "excluded"), ("office or clinic", "office_visits"),
              ("have a test", "tests"), ("drugs", "drugs"), ("outpatient surgery", "outpatient_surgery"),
              ("immediate medical attention", "emergency"), ("hospital stay", "hospital_stay"),
              ("mental health", "mental_health"), ("pregnant", "pregnancy"), ("recovering", "recovery"),
              ("child", "pediatric")]
# limits_conditions/quote text the calculator does not model yet
FLAG_PATTERNS = {"cap": r"maximum of \$|up to \$|allowance", "penalty": r"penalty|reduced by \d+%",
                 "balance_billing": r"(over|above) the allowed amount"}
OUT_OF_NETWORK_NOTE = r"out-of-network|non-contracting"
PLAN_COLUMNS = ["plan_id", "insurer", "plan_name", "plan_year", "plan_type", "network_name", "deductible_in_usd",
                "deductible_out_usd", "oop_max_in_usd", "oop_max_out_usd", "coinsurance_in_pct", "source_url",
                "verified"]
BENEFIT_COLUMNS = ["benefit_id", "plan_id", "category", "section", "service", "service_as_printed", "component",
                   "setting", "network", "network_tier", "copay_usd", "coinsurance_pct", "deductible_applies",
                   "prior_auth", "covered", "limits_conditions", "quote", "source_url", "source_page", "verified",
                   "review_flags", "calculable"]
NETWORK_COLUMNS = ["plan_id", "network_tier", "mrf_plan_key", "mrf_network", "notes", "confirmed"]
PREMIUM_COLUMNS = ["plan_id", "rating_region", "age", "enrollee", "period", "amount_usd", "annual_usd", "basis",
                   "source_url", "source_page", "quote", "notes"]
AGE_COLUMNS = ["age", "factor", "source_url", "source_page", "quote"]
REGION_COLUMNS = ["zip", "county", "rating_region", "source_url", "source_page"]


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


def text(v):
    v = (v or "").strip()
    return None if v.upper() in ("", "NOT FOUND", "N/A", "NONE") else v


def category(section, service):
    if service == "plan_level":
        return "plan_level"
    s = (section or "").lower()
    return next((c for key, c in CATEGORIES if key in s), "other")


def flags(row, verified):
    out = [] if verified else ["unverified"]
    if row["service"] == "plan_level" or row["covered"] is False:
        return out
    if row["covered"] is None:
        out.append("coverage_unknown")
    if row["copay_usd"] is None and row["coinsurance_pct"] is None:
        out.append("missing_cost")
    if row["deductible_applies"] is None:
        out.append("missing_deductible")
    # A limits note about out-of-network / non-contracting providers ("25% penalty", "$30 allowance") only
    # changes the price of out-of-network rows.
    notes, limits = row["quote"] or "", row["limits_conditions"] or ""
    if row["network"] == "out" or not re.search(OUT_OF_NETWORK_NOTE, limits, re.I):
        notes += " " + limits
    # A cap ("30% up to $250") limits coinsurance; a copay-only row is unaffected even when its cell (quote) also
    # describes another component's cap.
    out += [f for f, pat in FLAG_PATTERNS.items() if re.search(pat, notes, re.I)
            and not (f == "cap" and row["coinsurance_pct"] is None)]
    return out


def build_plan(folder, blanket_verified=False):
    """Return (plan, benefits, problems) for one extracted plan folder; plan is None if it can't be used."""
    problems = []
    plan_rows = read(folder / "plans.csv")
    if len(plan_rows) != 1 or plan_rows[0].get("plan_id") != folder.name:
        return None, [], [f"{folder.name}/plans.csv: need exactly one row with plan_id {folder.name}"]
    r = plan_rows[0]
    try:
        plan_type = (text(r.get("plan_type")) or "").upper() or None
        if plan_type and plan_type not in PLAN_TYPES:
            raise ValueError(f"plan_type={plan_type}")
        plan = {**{k: text(r.get(k)) for k in ("plan_id", "insurer", "plan_name", "plan_year", "network_name",
                                               "source_url")},
                **{k: num(r.get(k), k) for k in ("deductible_in_usd", "deductible_out_usd", "oop_max_in_usd",
                                                 "oop_max_out_usd", "coinsurance_in_pct")},
                "plan_type": plan_type, "verified": blanket_verified or yes(r.get("verified")) is True}
        if not plan["insurer"] or not plan["plan_name"]:
            raise ValueError("insurer and plan_name are required")
    except ValueError as e:
        return None, [], [f"{folder.name}/plans.csv: {e}"]

    benefits, keys = [], Counter()
    for i, b in enumerate(read(folder / "benefits.csv"), 2):
        try:
            if b.get("plan_id") != folder.name:
                raise ValueError(f"plan_id {b.get('plan_id')!r}")
            if b.get("service") not in SERVICES:
                raise ValueError(f"unknown service {b.get('service')!r}")
            if b.get("network") not in ("in", "out"):
                raise ValueError("network must be in or out")
            if not text(b.get("service_as_printed")):
                raise ValueError("service_as_printed is required")
            component = (text(b.get("component")) or "").lower() or None
            setting = None
            if component and component not in COMPONENTS:
                component, setting = None, component.upper()
            row = {"plan_id": folder.name, "section": text(b.get("section")), "service": b["service"],
                   "service_as_printed": text(b["service_as_printed"]), "component": component, "setting": setting,
                   "network": b["network"], "network_tier": text(b.get("network_tier")),
                   "copay_usd": num(b.get("copay_usd"), "copay_usd"),
                   "coinsurance_pct": num(b.get("coinsurance_pct"), "coinsurance_pct"),
                   "deductible_applies": yes(b.get("deductible_applies")), "prior_auth": yes(b.get("prior_auth")),
                   "covered": yes(b.get("covered")), "limits_conditions": text(b.get("limits_conditions")),
                   "quote": text(b.get("quote")), "source_url": text(b.get("source_url")),
                   "source_page": num(b.get("source_page"), "source_page")}
        except (KeyError, ValueError) as e:
            problems.append(f"{folder.name}/benefits.csv line {i}: {e}")
            continue
        key = tuple(row[k] or "" for k in ("section", "service", "service_as_printed", "component", "setting",
                                           "network", "network_tier"))
        keys[key] += 1
        row_verified = yes(b.get("verified"))
        verified = plan["verified"] and (row_verified is True or (blanket_verified and row_verified is not False))
        row_flags = flags(row, verified)
        row.update(benefit_id=hashlib.sha1("|".join((folder.name, *key)).encode()).hexdigest()[:16],
                   category=category(row["section"], row["service"]), verified=verified,
                   review_flags=row_flags, calculable=not row_flags)
        benefits.append(row)
    dupes = [k for k, n in keys.items() if n > 1]
    if dupes:
        return None, [], problems + [f"{folder.name}/benefits.csv: duplicate rows for {d}" for d in dupes]
    return plan, benefits, problems


def write(con, table, columns, rows):
    con.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                    [[",".join(r[c]) if isinstance(r[c], list) else r[c] for c in columns] for r in rows])


def bridge(path, plans, benefits):
    """plan_networks rows for built plans; each tier must exist as an in-network tier in the plan's benefits."""
    rows, problems, waiting = [], [], []
    tiers = {(b["plan_id"], b["network_tier"] or "") for b in benefits if b["network"] == "in"}
    for i, r in enumerate(read(path) if path.exists() else [], 2):
        row = {"plan_id": r.get("plan_id"), "network_tier": r.get("network_tier") or "",
               "mrf_plan_key": text(r.get("mrf_plan_key")), "mrf_network": text(r.get("mrf_network")),
               "notes": text(r.get("notes")), "confirmed": 1 if yes(r.get("confirmed")) else 0}
        if row["plan_id"] not in plans:
            waiting.append(row["plan_id"])
        elif not row["mrf_plan_key"]:
            problems.append(f"plan_networks.csv line {i}: mrf_plan_key is required")
        elif (row["plan_id"], row["network_tier"]) not in tiers:
            problems.append(f"plan_networks.csv line {i}: {row['plan_id']} has no in-network tier "
                            f"{row['network_tier']!r}")
        else:
            rows.append(row)
    return rows, problems, waiting


def premiums(src, plans):
    """Premium, age curve and rating region rows; a premium needs a built plan and its source."""
    rows, problems, waiting = [], [], []
    path = src / "premiums.csv"
    for i, r in enumerate(read(path) if path.exists() else [], 2):
        try:
            row = {**{k: text(r.get(k)) for k in ("plan_id", "enrollee", "period", "basis", "source_url", "quote",
                                                   "notes")},
                   **{k: num(r.get(k), k) for k in ("rating_region", "age", "amount_usd", "annual_usd",
                                                    "source_page")}}
            if row["period"] not in ("month", "quarter"):
                raise ValueError(f"period={row['period']}")
            if row["amount_usd"] is None or row["annual_usd"] is None:
                raise ValueError("amount_usd and annual_usd are required")
            if not (row["source_url"] and row["quote"] and row["basis"]):
                raise ValueError("source_url, quote and basis are required")
        except ValueError as e:
            problems.append(f"premiums.csv line {i}: {e}")
            continue
        if row["plan_id"] in plans:
            rows.append(row)
        else:
            waiting.append(row["plan_id"])
    ages = [{"age": num(r["age"], "age"), "factor": num(r["factor"], "factor"), "source_url": r["source_url"],
             "source_page": num(r.get("source_page"), "source_page"), "quote": r["quote"]}
            for r in (read(src / "age_curve.csv") if (src / "age_curve.csv").exists() else [])]
    if ages and sorted(a["age"] for a in ages) != list(range(65)):
        problems.append("age_curve.csv: need one factor for every age 0-64")
        ages = []
    zips = [{"zip": r["zip"], "county": r["county"], "rating_region": num(r["rating_region"], "rating_region"),
             "source_url": r["source_url"], "source_page": num(r.get("source_page"), "source_page")}
            for r in (read(src / "rating_regions.csv") if (src / "rating_regions.csv").exists() else [])]
    return rows, ages, zips, problems, waiting


def main(src, out):
    src, out = Path(src), Path(out)
    sources = {r["plan_id"]: r for r in read(src / "sources.csv")} if (src / "sources.csv").exists() else {}
    plans, benefits, problems = [], [], []
    for folder in sorted(p for p in (src / "extracted").iterdir() if p.is_dir()):
        plan, rows, errs = build_plan(folder, bool(text(sources.get(folder.name, {}).get("verified_by"))))
        problems += errs
        if plan:
            plans.append(plan)
            benefits += rows
            counts = Counter(f for r in rows for f in r["review_flags"])
            print(f"{plan['plan_id']}: {len(rows)} rows, {sum(r['calculable'] for r in rows)} calculable"
                  + (f"; flags {dict(counts)}" if counts else ""))
    networks, errs, waiting = bridge(src / "plan_networks.csv", {p["plan_id"] for p in plans}, benefits)
    problems += errs
    premium_rows, ages, zips, errs, premiums_waiting = premiums(src, {p["plan_id"] for p in plans})
    problems += errs
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    with sqlite3.connect(out) as con:
        con.executescript((ROOT / "db" / "schema.sql").read_text())
        write(con, "plans", PLAN_COLUMNS, plans)
        write(con, "benefits", BENEFIT_COLUMNS, benefits)
        write(con, "plan_networks", NETWORK_COLUMNS, networks)
        write(con, "premiums", PREMIUM_COLUMNS, premium_rows)
        write(con, "age_curve", AGE_COLUMNS, ages)
        write(con, "rating_regions", REGION_COLUMNS, zips)
    con.close()
    print(f"{len(plans)} plans, {len(benefits)} benefit rows, {len(networks)} network links, "
          f"{len(premium_rows)} premiums, {len(zips)} ZIP codes -> {out}")
    if waiting:
        print("plan_networks waiting for extraction:", ", ".join(sorted(set(waiting))))
    if premiums_waiting:
        print("premiums waiting for extraction:", ", ".join(sorted(set(premiums_waiting))))
    for p in problems:
        print("SKIPPED", p)
    return 1 if problems else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=ROOT / "data" / "plans")
    ap.add_argument("--out", default=ROOT / "data" / "plans" / "policy.sqlite")
    args = ap.parse_args()
    sys.exit(main(args.src, args.out))

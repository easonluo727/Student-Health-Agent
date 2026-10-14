"""Build rate_summary + data-quality checks and write data/mrf/allowed_amounts.{md,csv}.

    python scripts/mrf/report.py [--db data/mrf/rates.sqlite] [--as-of YYYY-MM-DD]

Pure Python over the SQLite produced by import_mrf_rates.py. No LLM, no network.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sqlite3
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import allowed  # noqa: E402
from lookup import load_encounters, lookup_allowed_amount  # noqa: E402

DATA = os.path.join(ROOT, "data", "mrf")
ER_CODES = {"99281", "99282", "99283", "99284", "99285"}
POS_LABEL = {"11": "office", "20": "urgent care", "22": "outpatient hospital", "23": "ER", "81": "lab", None: "any"}
CLASS_SHORT = {"professional": "prof", "institutional": "inst"}

A_COLS = ["insurer", "network", "encounter", "allowed_amount", "allowed_amount_basis", "status", "components",
          "n_providers", "p10", "p90", "ucla_health_rate", "mrf_file_date"]
B_COLS = ["insurer", "network", "billing_code", "billing_class", "place_of_service", "allowed_amount",
          "allowed_amount_basis", "status", "n_providers", "p10", "p90", "ucla_health_rate", "mrf_file_date",
          "negotiated_type_mix", "n_rates_excluded"]


def money(x) -> str:
    return "" if x is None else f"${x:,.2f}"


def load_profiles() -> list[dict]:
    with open(os.path.join(ROOT, "data", "plan_profiles.v1.json")) as f:
        return json.load(f)["plans"]


def load_codes() -> list[dict]:
    with open(os.path.join(DATA, "billing_codes.csv"), newline="") as f:
        return list(csv.DictReader(f))


REPORT_POS = ("11", "20", "22", "23", "81")


def component_combos(con, codes: list[dict], encounters: dict[str, list[dict]]) -> list[tuple[str, str, str | None, str]]:
    """Encounter components plus, per code x billing class, every reported POS setting that occurs
    in the data (office 11, urgent care 20, outpatient hospital 22, ER 23, lab 81, or none listed)."""
    combos: dict[tuple[str, str, str | None], str] = {}
    for rows in encounters.values():
        for r in rows:
            for code in r["codes"].split("|"):
                combos[(code, r["billing_class"], r["place_of_service"] or None)] = r["modifier_rule"]
    for c in codes:
        code = c["code"]
        rule = "global_or_tc_plus_26" if code.startswith("7") else "global"
        for bclass, sc in con.execute("SELECT DISTINCT billing_class, service_codes FROM negotiated_rates "
                                      "WHERE billing_code=? AND billing_class IN ('professional','institutional')", (code,)):
            listed = set(sc.split(",")) if sc else set()
            any_pos = not listed or "CSTM-00" in listed
            for pos in [p for p in REPORT_POS if p in listed] or ([None] if any_pos else []):
                combos.setdefault((code, bclass, pos), rule)
    return [(k[0], k[1], k[2], v) for k, v in sorted(combos.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or ""))]


def build_ucla_tins(con, npi_csv: str) -> int:
    """UCLA Health TINs = SHA-256 of UCLA organization NPIs (NPPES name rule in build_npi_set.py).
    Insurers that report TINs as organization NPIs (Anthem) then match without any raw TIN."""
    import hashlib
    con.execute("DROP TABLE IF EXISTS ucla_health_tins")
    con.execute("CREATE TABLE ucla_health_tins (tin_sha256 TEXT PRIMARY KEY, npi TEXT)")
    with open(npi_csv, newline="") as f:
        rows = [(hashlib.sha256(r["npi"].encode()).hexdigest(), r["npi"]) for r in csv.DictReader(f) if r["ucla_org"] == "1"]
    con.executemany("INSERT OR IGNORE INTO ucla_health_tins VALUES (?,?)", rows)
    con.commit()
    allowed.fetch_candidates.cache_clear()
    return len(rows)


def build_rate_summary(con, plans, combos, as_of) -> None:
    con.execute("DROP TABLE IF EXISTS rate_summary")
    con.execute("""CREATE TABLE rate_summary (plan_key TEXT, network TEXT, billing_code TEXT, billing_class TEXT,
                   place_of_service TEXT, tier TEXT, n INTEGER, p10 REAL, median REAL, p90 REAL)""")
    for p in plans:
        for code, bclass, pos, rule in combos:
            prov, _, _ = allowed.per_provider(allowed.fetch_candidates(con, p["plan_key"], code, bclass), pos, rule, as_of)
            groups = {"UCLA": [v[0] for v in prov.values() if v[2]], "A": [v[0] for v in prov.values() if v[1] == "A"],
                      "LA": [v[0] for v in prov.values()]}
            for tier, vals in groups.items():
                vals.sort()
                con.execute("INSERT INTO rate_summary VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (p["plan_key"], "|".join(p.get("network_names") or []), code, bclass, pos or "", tier,
                             len(vals), allowed._r(allowed._pct(vals, .1)), allowed._r(statistics.median(vals)) if vals else None,
                             allowed._r(allowed._pct(vals, .9))))
    con.commit()


def data_quality(con, plans, codes) -> dict:
    out = {"negative_rates": con.execute("SELECT COUNT(*) FROM negotiated_rates WHERE negotiated_rate < 0").fetchone()[0],
           "percentage_rates": con.execute("SELECT COUNT(*) FROM negotiated_rates WHERE negotiated_type='percentage'").fetchone()[0],
           "per_diem_rates": con.execute("SELECT COUNT(*) FROM negotiated_rates WHERE negotiated_type='per diem'").fetchone()[0],
           "outliers": 0, "codes_zero_matches": {}}
    rows = con.execute("""SELECT billing_code, negotiated_rate FROM negotiated_rates
                          WHERE negotiated_type IN ('negotiated','derived','fee schedule')""").fetchall()
    by_code: dict[str, list[float]] = {}
    for code, v in rows:
        by_code.setdefault(code, []).append(v)
    flagged = {}
    for code, vals in by_code.items():
        if len(vals) >= 20:
            q = statistics.quantiles(vals, n=100, method="inclusive")
            n = sum(1 for v in vals if v < q[0] or v > q[98])
            flagged[code] = n
            out["outliers"] += n
    out["outliers_by_code"] = flagged
    for p in plans:
        present = {r[0] for r in con.execute("""SELECT DISTINCT r.billing_code FROM negotiated_rates r
                    JOIN mrf_sources s ON s.source_id=r.source_id WHERE s.plan_key=? AND s.status='complete'""", (p["plan_key"],))}
        missing = [c["code"] for c in codes if c["code"] not in present]
        out["codes_zero_matches"][p["plan_key"]] = {"missing": missing, "share": round(len(missing) / len(codes), 3)}
    return out


def build(db: str, as_of: str, npi_csv: str | None = None) -> dict:
    con = sqlite3.connect(db)
    if npi_csv and os.path.exists(npi_csv):
        build_ucla_tins(con, npi_csv)
    profiles = load_profiles()
    loaded = {r[0] for r in con.execute("SELECT DISTINCT plan_key FROM mrf_sources WHERE status='complete'")}
    plans = [p for p in profiles if p["plan_key"] in loaded]
    codes, encounters = load_codes(), load_encounters()
    combos = component_combos(con, codes, encounters)
    build_rate_summary(con, plans, combos, as_of)
    dq = data_quality(con, plans, codes)

    table_a, table_b = [], []
    for p in plans:
        net = " / ".join(p.get("network_names") or [])
        for enc, rows in encounters.items():
            r = lookup_allowed_amount(p["plan_key"], None, enc, con=con, as_of=as_of)
            comps = " + ".join(f'{c["billing_code"]} {CLASS_SHORT.get(c["billing_class"], c["billing_class"])}'
                               f'{" POS " + c["place_of_service"] if c["place_of_service"] else ""}'
                               f'{" " + money(c["allowed_amount"]) if c["allowed_amount"] is not None else " (no rate)"}'
                               for c in r["components"])
            have = [c for c in r["components"] if c["allowed_amount"] is not None]
            def s(key):
                vals = [c[key] for c in r["components"]]
                return None if any(v is None for v in vals) else round(sum(vals), 2)
            table_a.append({"insurer": p["insurer"], "network": net, "encounter": rows[0]["label"],
                            "allowed_amount": r["allowed_amount"], "allowed_amount_basis": r["allowed_amount_basis"] or "",
                            "status": r["status"], "components": comps,
                            "n_providers": min((c["n_providers"] for c in have), default=0),
                            "p10": s("p10"), "p90": s("p90"), "ucla_health_rate": s("ucla_health_rate"),
                            "mrf_file_date": ", ".join(r["source"]["mrf_file_dates"])})
        for code, bclass, pos, rule in combos:
            c = allowed.component(con, p["plan_key"], code, bclass, pos, rule, as_of=as_of)
            table_b.append({"insurer": p["insurer"], "network": net, "billing_code": code, "billing_class": bclass,
                            "place_of_service": pos or "", "allowed_amount": c.allowed_amount,
                            "allowed_amount_basis": c.allowed_amount_basis or "", "status": c.status,
                            "n_providers": c.n_providers, "p10": c.p10, "p90": c.p90,
                            "ucla_health_rate": c.ucla_health_rate, "mrf_file_date": ", ".join(c.mrf_file_dates),
                            "negotiated_type_mix": c.negotiated_type_mix, "n_rates_excluded": c.n_rates_excluded})

    sources = con.execute("""SELECT plan_key, source_url, file_date, reporting_entity, schema_version, rows_kept
                             FROM mrf_sources WHERE status='complete' ORDER BY plan_key, source_url""").fetchall()
    src = con.execute("""SELECT plan_key, COUNT(*), SUM(compressed_bytes), SUM(decompressed_bytes), SUM(rows_scanned),
                         SUM(rows_kept), SUM(wall_seconds), MIN(file_date), MAX(file_date)
                         FROM mrf_sources WHERE status='complete' GROUP BY plan_key""").fetchall()
    con.close()
    write_csv(table_a, table_b)
    write_md(plans, profiles, table_a, table_b, src, dq, as_of, encounters, sources)
    return {"plans": [p["plan_key"] for p in plans], "table_a": len(table_a), "table_b": len(table_b), "dq": dq}


def write_csv(table_a, table_b) -> None:
    cols = ["table"] + A_COLS + [c for c in B_COLS if c not in A_COLS]
    with open(os.path.join(DATA, "allowed_amounts.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in table_a:
            w.writerow({"table": "A_encounter", **r})
        for r in table_b:
            w.writerow({"table": "B_component", **r})


def _md_table(cols, rows) -> list[str]:
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in r) + " |")
    return out


def write_md(plans, profiles, table_a, table_b, src, dq, as_of, encounters, sources=()) -> None:
    gb = sum(r[2] or 0 for r in src) / 1e9
    gb_raw = sum(r[3] or 0 for r in src) / 1e9
    scanned = sum(r[4] or 0 for r in src)
    kept = sum(r[5] or 0 for r in src)
    secs = sum(r[6] or 0 for r in src)
    months = sorted({r[7] for r in src if r[7]} | {r[8] for r in src if r[8]})
    cells = len(table_a)
    covered = sum(1 for r in table_a if r["allowed_amount"] is not None)
    db_mb = os.path.getsize(os.path.join(DATA, "rates.sqlite")) / 1e6 if os.path.exists(os.path.join(DATA, "rates.sqlite")) else 0
    L = ["# Allowed amounts from insurer MRFs (Los Angeles)", "",
         "_Generated by `scripts/mrf/report.py` from `data/mrf/rates.sqlite`. Do not edit by hand._", "",
         "## Summary", "",
         f"- **Insurers covered:** {', '.join(p['insurer'] + ' (' + ' / '.join(p['network_names']) + ')' for p in plans)}",
         f"- **Not covered:** {', '.join(p['insurer'] + ' — ' + (p.get('notes') or '') for p in profiles if p not in plans)}",
         f"- **MRF file date(s):** {', '.join(months)}; rates expiring before {as_of} are dropped",
         f"- **Data scanned:** {gb:,.1f} GB compressed ({gb_raw:,.1f} GB decompressed) in {len(src) and sum(r[1] for r in src)} files",
         f"- **Records scanned → rate rows kept:** {scanned:,} → {kept:,} (SQLite {db_mb:.1f} MB)",
         f"- **Import wall time:** {secs/60:,.1f} min (sum over files)",
         f"- **Coverage:** {covered}/{cells} encounter × insurer cells have an `allowed_amount` ({100*covered/max(cells,1):.0f}%)",
         "- **Billing codes and encounter templates are candidates awaiting human verification** "
         "(`verified_by` is empty in `billing_codes.csv` and `encounters.csv`).", "",
         "## Table A — Encounter allowed amounts (the demo uses this)", ""]
    L += _md_table(A_COLS, [[r["insurer"], r["network"], r["encounter"], f'**{money(r["allowed_amount"])}**' if r["allowed_amount"] is not None else "—",
                             r["allowed_amount_basis"], r["status"], r["components"], r["n_providers"], money(r["p10"]),
                             money(r["p90"]), money(r["ucla_health_rate"]), r["mrf_file_date"]] for r in table_a])
    L += ["", "`p10`/`p90`/`ucla_health_rate` for an encounter are sums of the component values (blank if any component lacks one).", "",
          "## Table B — Component allowed amounts", ""]
    L += _md_table(B_COLS, [[r["insurer"], r["network"], r["billing_code"], r["billing_class"],
                             " → ".join(f'{p} ({POS_LABEL.get(p, "")})' for p in r["place_of_service"].split("|")) if r["place_of_service"] else "any",
                             money(r["allowed_amount"]) or "—", r["allowed_amount_basis"], r["status"], r["n_providers"],
                             money(r["p10"]), money(r["p90"]), money(r["ucla_health_rate"]), r["mrf_file_date"],
                             r["negotiated_type_mix"], r["n_rates_excluded"]] for r in table_b])
    L += ["", "## Table C — Insurer comparison (`allowed_amount` per encounter; cheapest in bold)", ""]
    insurers = [p["insurer"] for p in plans]
    rows = []
    for enc, erows in encounters.items():
        vals = {r["insurer"]: r["allowed_amount"] for r in table_a if r["encounter"] == erows[0]["label"]}
        present = [v for v in vals.values() if v is not None]
        low = min(present) if present else None
        rows.append([erows[0]["label"]] + [("**" + money(vals.get(i)) + "**" if vals.get(i) is not None and vals.get(i) == low and len(present) > 1
                                            else (money(vals.get(i)) or "—")) for i in insurers])
    L += _md_table(["encounter"] + insurers, rows)
    L += ["", "## Data notes", "",
          "- **Negotiated rate ≠ final allowed amount.** MRF rates are contract prices before claim edits, bundling, "
          "multiple-procedure reductions, outlier payments, and member cost sharing. Treat them as the best public "
          "estimate of the allowed amount, not a quote.",
          "- **Basis order:** `provider_rate` (NPI requested) → `ucla_health_rate` (UCLA Health NPIs/TINs in Tier A) → "
          "`westwood_area_median` (Tier A ZIPs 90024, 90025, 90095, 90049, 90064, 90067) → `la_county_median` (Tier B: "
          "ZCTAs ≥50% inside LA County) → `rate_unavailable`. Medians are over per-provider values.",
          "- **Per-provider value:** dollar types only (`negotiated`, `derived`, `fee schedule`); `percentage` and "
          "`per diem` are excluded and counted in `n_rates_excluded` together with expired rates; POS-specific rate "
          "preferred, else a rate with no POS (or CMS `CSTM-00` = all POS); latest expiration date; median if a provider still has several values.",
          "- **Imaging:** global rate, else TC + 26 summed per provider.",
          "- **UC SHIP** is self-funded and uses Anthem Blue Cross Prudent Buyer PPO network rates; the Anthem rows are the demo's prices.",
          "- **Medicare benchmarks are not used** anywhere in this table.",
          "- **Snapshot:** one monthly MRF release; rates change monthly.",
          "", "### Data-quality checks", "",
          f"- Negative rates: {dq['negative_rates']}",
          f"- Rates outside the 1st–99th percentile of their code (flagged, kept): {dq['outliers']}",
          f"- `percentage` rates (not dollar amounts; excluded): {dq['percentage_rates']}; `per diem` (excluded): {dq['per_diem_rates']}",
          "- Billing codes with zero matches per insurer: " + "; ".join(
              f"{k}: {v['share']*100:.0f}%" + (f" ({', '.join(v['missing'])})" if v["missing"] else "") for k, v in dq["codes_zero_matches"].items()),
          "", "### Sources (every rate row traces to one of these files)", ""]
    L += _md_table(["plan_key", "source_url", "file_date", "reporting_entity", "schema", "rate rows"],
                   [[pk, url, fd, ent, ver, f"{n:,}"] for pk, url, fd, ent, ver, n in sources])
    L += [""]
    with open(os.path.join(DATA, "allowed_amounts.md"), "w") as f:
        f.write("\n".join(L))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(DATA, "rates.sqlite"))
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--npi-csv", default=os.path.join(DATA, "la_npis.csv"), help="for the UCLA Health TIN table")
    a = ap.parse_args(argv)
    print(json.dumps(build(a.db, a.as_of, a.npi_csv), indent=1))


if __name__ == "__main__":
    main()

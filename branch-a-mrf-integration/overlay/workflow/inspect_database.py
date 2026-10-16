"""Read-only inspection of the MRF SQLite database before any lookup relies on it.

    python inspect_database.py [--db data/mrf/rates.sqlite] [--out report.json] [--distinct]

Reports path, size, snapshot dates, tables/columns/indexes/row counts, the canonical column binding,
and per-plan join checks: a sampled rate row -> its provider group -> member NPIs, and back from one of
those NPIs -> groups -> rate rows. A schema with an `npi` column proves nothing until these joins return
rows. Prints counts and short masked NPIs only (never raw MRF content). --distinct also counts distinct
NPIs per plan (slow on the full database).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

import overlay_config

overlay_config.install_import_paths()

import canonical_adapter as ca  # noqa: E402

SAMPLE_CODES = ("99213", "99203", "80053", "73610", "99284")


def mask(npi: str | None) -> str | None:
    return None if not npi else f"{npi[:3]}*****{npi[-2:]}"


def _timed(con, sql, args=()):
    t = time.perf_counter()
    rows = con.execute(sql, args).fetchall()
    return rows, round(time.perf_counter() - t, 3)


def inspect(db: Path, distinct: bool = False) -> dict:
    db = Path(db)
    report: dict = {"path": str(db), "exists": db.exists(), "adapter_version": ca.ADAPTER_VERSION,
                    "inspected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if not db.exists():
        report["error"] = "database file not found; NPI lookups and nearby-provider estimates are unavailable"
        return report
    st = db.stat()
    report.update(size_bytes=st.st_size, modified=dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc)
                  .isoformat(timespec="seconds"), sqlite_version=sqlite3.sqlite_version)
    con = ca.connect_ro(db)
    report["user_version"] = con.execute("PRAGMA user_version").fetchone()[0]
    tables = {}
    for name, cols in ca.table_columns(con).items():
        idx = [{"name": r[1], "columns": [c[2] for c in con.execute(f'PRAGMA index_info("{r[1]}")')]}
               for r in con.execute(f'PRAGMA index_list("{name}")')]
        tables[name] = {"columns": cols, "indexes": idx,
                        "rows": con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]}
    report["tables"] = tables
    try:
        bindings = ca.bind_sqlite(con)
        report["binding_errors"] = []
    except ca.AdapterError as e:
        report["binding_errors"] = [str(e)]
        report["canonical_mapping"] = ca.mapping_report()
        return report
    report["canonical_mapping"] = [m for m in ca.mapping_report(bindings) if m["view"] == "canonical_provider_rates"]
    report["summary_tables"] = {
        "rate_summary": "rate_summary" in tables,
        "csv_A_B": "allowed_amounts.csv holds tables A and B (see canonical_csv_views)"}
    plans = []
    for plan_key, n_src, fmin, fmax in con.execute(
            "SELECT plan_key, COUNT(*), MIN(file_date), MAX(file_date) FROM mrf_sources WHERE status='complete' "
            "GROUP BY plan_key ORDER BY plan_key"):
        p = {"plan_key": plan_key, "complete_sources": n_src, "file_dates": sorted({fmin, fmax})}
        rows, secs = _timed(con, "SELECT COUNT(*) FROM negotiated_rates r JOIN mrf_sources s ON s.source_id=r.source_id "
                                 "WHERE s.plan_key=? AND s.status='complete'", (plan_key,))
        p["rate_rows"], p["rate_count_seconds"] = rows[0][0], secs
        p["join_checks"] = []
        for code in SAMPLE_CODES:
            rows, secs = _timed(con, "SELECT r.source_id, r.provider_group_id FROM negotiated_rates r "
                                     "JOIN mrf_sources s ON s.source_id=r.source_id AND s.status='complete' "
                                     "WHERE s.plan_key=? AND r.billing_code=? LIMIT 1", (plan_key, code))
            if not rows:
                p["join_checks"].append({"code": code, "rate_rows": 0})
                continue
            sid, gid = rows[0]
            members, _ = _timed(con, "SELECT npi, zip, taxonomy FROM provider_members WHERE source_id=? AND "
                                     "provider_group_id=? LIMIT 50", (sid, gid))
            check = {"code": code, "sample_group_members": len(members), "sample_npi": mask(members[0][0]) if members else None}
            if members:
                npi = members[0][0]
                back, secs = _timed(con, "SELECT COUNT(*) FROM provider_members m JOIN negotiated_rates r ON "
                                         "r.source_id=m.source_id AND r.provider_group_id=m.provider_group_id "
                                         "JOIN mrf_sources s ON s.source_id=m.source_id AND s.status='complete' "
                                         "WHERE m.npi=? AND s.plan_key=? AND r.billing_code=?", (npi, plan_key, code))
                check.update(npi_rate_rows_for_code=back[0][0], npi_lookup_seconds=secs,
                             sample_member_has_zip=bool(members[0][1]), sample_member_has_taxonomy=bool(members[0][2]))
            p["join_checks"].append(check)
        if distinct:
            rows, secs = _timed(con, "SELECT COUNT(DISTINCT m.npi) FROM provider_members m JOIN mrf_sources s ON "
                                     "s.source_id=m.source_id AND s.status='complete' WHERE s.plan_key=?", (plan_key,))
            p["distinct_npis"], p["distinct_seconds"] = rows[0][0], secs
        plans.append(p)
    report["plans"] = plans
    report["ok"] = all(any(c.get("npi_rate_rows_for_code") for c in p["join_checks"]) for p in plans) and bool(plans)
    return report


def main() -> None:
    paths = overlay_config.load_paths()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=paths.rates_sqlite)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--distinct", action="store_true")
    a = ap.parse_args()
    text = json.dumps(inspect(a.db, a.distinct), indent=2)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

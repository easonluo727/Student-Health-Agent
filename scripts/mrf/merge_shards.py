"""Merge per-file SQLite shards (run_all.py) into one rates.sqlite. Only `complete` sources are merged.

    python scripts/mrf/merge_shards.py --shards data/mrf/work/shards --out data/mrf/rates.sqlite
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mrf_rate_store as store  # noqa: E402

SRC_COLS = [c for c in (
    "source_url insurer plan_key network region file_date toc_url reporting_entity schema_version compressed_bytes "
    "decompressed_bytes rows_scanned rows_kept groups_kept status error started_at finished_at wall_seconds "
    "peak_rss_mb parser_stats").split()]
RATE_COLS = ("billing_code billing_code_type negotiation_arrangement provider_group_id negotiated_rate negotiated_type "
             "expiration_date billing_class setting service_codes modifiers").split()
MEMBER_COLS = ("provider_group_id npi tin_sha256 tin_last4 tin_type tin_business_name network_names zip taxonomy "
               "tier ucla_health").split()


def merge(shard_paths: list[str], out: str) -> dict:
    con = store.connect(out)
    merged = skipped = 0
    for path in shard_paths:
        con.execute("ATTACH DATABASE ? AS s", (path,))
        for (sid, url, pk) in con.execute(
                "SELECT source_id, source_url, plan_key FROM s.mrf_sources WHERE status='complete'").fetchall():
            if con.execute("SELECT 1 FROM main.mrf_sources WHERE source_url=? AND plan_key=? AND status='complete'",
                           (url, pk)).fetchone():
                skipped += 1
                continue
            con.execute("DELETE FROM main.mrf_sources WHERE source_url=? AND plan_key=?", (url, pk))
            new = con.execute(f"INSERT INTO main.mrf_sources ({','.join(SRC_COLS)}) SELECT {','.join(SRC_COLS)} "
                              "FROM s.mrf_sources WHERE source_id=?", (sid,)).lastrowid
            con.execute(f"INSERT INTO main.negotiated_rates (source_id,{','.join(RATE_COLS)}) "
                        f"SELECT ?,{','.join(RATE_COLS)} FROM s.negotiated_rates WHERE source_id=?", (new, sid))
            con.execute(f"INSERT INTO main.provider_members (source_id,{','.join(MEMBER_COLS)}) "
                        f"SELECT ?,{','.join(MEMBER_COLS)} FROM s.provider_members WHERE source_id=?", (new, sid))
            merged += 1
        con.commit()
        con.execute("DETACH DATABASE s")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("VACUUM")
    con.close()
    return {"shards": len(shard_paths), "sources_merged": merged, "already_present": skipped}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    print(merge(sorted(glob.glob(os.path.join(a.shards, "*.sqlite"))), a.out))


if __name__ == "__main__":
    main()

"""Import one in-network MRF into SQLite .

    python scripts/mrf/import_mrf_rates.py \
      --input https://.../CA_XXXX.json.gz \
      --plan-key anthem_ca_prudent_buyer_ppo \
      --code 99213 --code 99214            # repeatable; default: data/mrf/billing_codes.csv

Behavior:
- `--network` is a filter on provider_references[].network_name (default: the plan profile's).
- Providers are filtered to the LA NPI set (`--npi-csv`, `--tier A|B`) while streaming.
- `--input` may be a URL; it is streamed, never downloaded. `--source-url` defaults to it.
- Imports commit periodically and are resumable per file (complete sources are skipped).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import resource
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import mrf_rate_store as store  # noqa: E402
from parse import InNetworkParser  # noqa: E402

DEFAULT_PROFILES = os.path.join(ROOT, "data", "plan_profiles.v1.json")
DEFAULT_CODES = os.path.join(ROOT, "data", "mrf", "billing_codes.csv")
DEFAULT_NPIS = os.path.join(ROOT, "data", "mrf", "la_npis.csv")
DEFAULT_DB = os.path.join(ROOT, "data", "mrf", "rates.sqlite")


def load_profile(path: str, plan_key: str) -> dict:
    with open(path) as f:
        profiles = json.load(f)["plans"]
    for p in profiles:
        if p["plan_key"] == plan_key:
            return p
    raise SystemExit(f"plan_key {plan_key!r} not in {path}")


def load_codes(path: str) -> list[str]:
    with open(path, newline="") as f:
        return [r["code"].strip() for r in csv.DictReader(f) if r.get("code", "").strip()]


def load_npis(path: str, tier: str) -> dict[str, tuple]:
    """npi -> (zip, taxonomy, tier, ucla_org). tier 'A' keeps Tier A only; 'B' keeps all of LA
    County (Tier A is a subset). Tuples keep 350k providers at ~100 MB instead of ~1 GB of dicts."""
    out: dict[str, tuple] = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if tier == "B" or r["tier"] == "A":
                out[r["npi"]] = (r["zip"], sys.intern(r["taxonomy"]), sys.intern(r["tier"]), int(r["ucla_org"]))
    return out


def _progress_printer(label: str):
    t0 = time.time()

    def report(parser) -> None:
        st, live = parser.stats, parser.stream.stats
        el = time.time() - t0
        print(f"[{label}] {el:.0f}s pass {parser.pass_name} {live.compressed_bytes/1e9:.2f} GB this pass "
              f"refs {st.provider_refs_seen} kept {st.provider_refs_kept} items {st.in_network_items_seen} "
              f"matched {st.in_network_items_matched} rows {st.rate_rows_emitted} "
              f"rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MB", file=sys.stderr, flush=True)
    return report


def import_file(db: str, src: str, profile: dict, codes: list[str], npi_info: dict[str, dict] | None,
                network: list[str] | None, source_url: str | None = None, file_date: str | None = None,
                force: bool = False, spill_dir: str | None = None, progress_every: float = 0) -> dict:
    con = store.connect(db)
    url = source_url or src
    if not force and store.source_status(con, url, profile["plan_key"]) == "complete":
        return {"source_url": url, "status": "skipped_complete"}
    sid = store.begin_source(con, url, insurer=profile["insurer"], plan_key=profile["plan_key"],
                             network="|".join(network or []) or None, region=profile.get("region"),
                             file_date=file_date, toc_url=profile.get("toc_url"))
    parser = InNetworkParser(codes, network, set(npi_info) if npi_info is not None else None,
                             code_types=profile.get("code_types", ["CPT", "HCPCS"]), spill_dir=spill_dir,
                             progress=_progress_printer(url.rsplit("/", 1)[-1].split("?")[0]) if progress_every else None,
                             progress_every=progress_every or 60)
    n_rates = n_groups = 0
    try:
        n_rates, n_groups = store.write_stream(con, sid, parser.parse(src), npi_info)
        store.finish_source(con, sid, parser, n_rates, n_groups)
        status, err = "complete", None
    except BaseException as e:  # record the failure (incl. Ctrl-C) so the next run redoes this file
        con.rollback()
        err = f"{type(e).__name__}: {e}"
        store.finish_source(con, sid, parser, n_rates, n_groups, status="failed", error=err[:500])
        if isinstance(e, KeyboardInterrupt):
            raise
        traceback.print_exc()
        status = "failed"
    finally:
        con.close()
    st = parser.stats
    return {"source_url": url, "status": status, "error": err, "rate_rows": n_rates, "groups": n_groups,
            "compressed_mb": round(st.compressed_bytes / 1e6, 1), "decompressed_mb": round(st.decompressed_bytes / 1e6, 1),
            "wall_s": st.wall_seconds, "mb_per_s": round(st.compressed_bytes / 1e6 / st.wall_seconds, 2) if st.wall_seconds else None,
            "peak_rss_mb": st.peak_rss_mb, "provider_refs_seen": st.provider_refs_seen,
            "provider_refs_needed": st.provider_refs_needed, "provider_refs_network_match": st.provider_refs_network_match,
            "provider_refs_kept": st.provider_refs_kept, "member_rows": st.member_rows,
            "in_network_items_seen": st.in_network_items_seen, "in_network_items_matched": st.in_network_items_matched,
            "groups_referenced": st.groups_referenced, "first_section": st.first_section,
            "pass1_s": st.pass1_seconds, "pass2_s": st.pass2_seconds, "pass2_stopped_early": st.pass2_stopped_early}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Stream-import one in-network MRF into SQLite.")
    ap.add_argument("--input", required=True, help="path or URL (.json / .json.gz)")
    ap.add_argument("--source-url", help="official URL to record (default: --input)")
    ap.add_argument("--plan-key", required=True)
    ap.add_argument("--profiles", default=DEFAULT_PROFILES)
    ap.add_argument("--network", action="append", help="override profile network_names (exact match)")
    ap.add_argument("--no-network-filter", action="store_true", help="for per-network files without network_name")
    ap.add_argument("--code", action="append", help="billing code; repeatable (default: billing_codes.csv)")
    ap.add_argument("--codes-csv", default=DEFAULT_CODES)
    ap.add_argument("--npi-csv", default=DEFAULT_NPIS)
    ap.add_argument("--tier", choices=["A", "B"], default="B")
    ap.add_argument("--file-date")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--force", action="store_true", help="re-import even if complete")
    ap.add_argument("--progress", type=float, default=60, help="seconds between progress lines (0 = off)")
    a = ap.parse_args(argv)
    profile = load_profile(a.profiles, a.plan_key)
    network = None if a.no_network_filter else (a.network or profile.get("network_names"))
    codes = a.code or load_codes(a.codes_csv)
    npis = load_npis(a.npi_csv, a.tier)
    print(json.dumps(import_file(a.db, a.input, profile, codes, npis, network, a.source_url, a.file_date, a.force,
                                 progress_every=a.progress)))


if __name__ == "__main__":
    main()

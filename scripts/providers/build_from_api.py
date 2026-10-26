"""Build the provider directory from the NPPES registry API when the monthly bulk file is unavailable.

Same output as build_directory.py (schema.sql: providers, taxonomies, primary and secondary practice locations),
so validate_directory.py, geocode_directory.py and the search work unchanged. The bulk download page was down for
maintenance on 2026-10-26; prefer build_directory.py whenever the monthly ZIP can be downloaded.

Two steps, both resumable:
  fetch   one API call per target NPI into a cache (data/providers/nppes_cache.sqlite), slowly: the API returned
          HTTP 403 after about 3,000 calls at ~48 calls/s on 2026-10-26. Default 2 calls/s (68k NPIs: ~9.5 h);
          on 403/429 it waits, doubling the pause, and stops after five blocks in a row.
  build   cache -> a new providers.sqlite (refuses to replace an existing file)

    python scripts/providers/export_target_npis.py --rates-db data/mrf/rates.sqlite --out data/providers/target_npis.csv
    python scripts/providers/build_from_api.py fetch --target-npis data/providers/target_npis.csv
    python scripts/providers/build_from_api.py build --target-npis data/providers/target_npis.csv \
        --out data/providers/providers.sqlite
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from .build_directory import SCHEMA, _insert_location
    from .common import normalized_address, read_target_npis, zip5
except ImportError:
    from build_directory import SCHEMA, _insert_location
    from common import normalized_address, read_target_npis, zip5

API = "https://npiregistry.cms.hhs.gov/api/"
API_LABEL = "NPPES NPI Registry API v2.1 (one call per NPI)"
CACHE = Path("data/providers/nppes_cache.sqlite")
SCRIPT_VERSION = "1"


# ── API record -> directory rows (pure, tested) ─────────────────────────────────────────────────

def _location(address: dict, secondary: bool) -> dict:
    line1, line2 = (address.get("address_1") or "").strip(), (address.get("address_2") or "").strip()
    city, state = (address.get("city") or "").strip(), (address.get("state") or "").strip().upper()
    postal, country = (address.get("postal_code") or "").strip(), (address.get("country_code") or "US").upper()
    return {"location_type": "secondary" if secondary else "primary", "address_line_1": line1,
            "address_line_2": line2, "city": city, "state": state, "zip5": zip5(postal), "postal_code": postal,
            "country": country, "phone": address.get("telephone_number") or "", "fax": address.get("fax_number") or "",
            "address_key": normalized_address(line1, line2, city, state, postal, country)}


def record_rows(result: dict) -> tuple[tuple, list[tuple[str, int]], list[dict]]:
    """One API result -> (providers row, [(taxonomy, is_primary)], [locations]). Never the MAILING address."""
    basic = result.get("basic") or {}
    npi = str(result["number"])
    etype = "1" if result.get("enumeration_type") == "NPI-1" else "2"
    first, last = (basic.get("first_name") or "").strip(), (basic.get("last_name") or "").strip()
    org = (basic.get("organization_name") or "").strip()
    name = (" ".join(filter(None, (first, last))) if etype == "1" else org) or None
    taxonomies: dict[str, int] = {}
    for tax in result.get("taxonomies") or []:
        if tax.get("code"):
            taxonomies[tax["code"]] = max(taxonomies.get(tax["code"], 0), int(bool(tax.get("primary"))))
    primary = next((code for code, flag in taxonomies.items() if flag), next(iter(taxonomies), None))
    provider = (npi, etype, name, first or None, last or None,
                (basic.get("credential") or None) if etype == "1" else None, org or None, primary,
                basic.get("last_updated") or None, "active" if basic.get("status", "A") == "A" else "inactive")
    locations = [_location(a, False) for a in result.get("addresses") or [] if a.get("address_purpose") == "LOCATION"]
    locations += [_location(a, True) for a in result.get("practiceLocations") or []]
    return provider, list(taxonomies.items()), locations


# ── steps ───────────────────────────────────────────────────────────────────────────────────────

def _cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS nppes_raw (npi TEXT PRIMARY KEY, fetched_at TEXT, body TEXT)")
    return con


def fetch(target_npis: str | Path, cache: Path = CACHE, min_interval: float = 0.5, limit: int | None = None,
          session=None, sleep=time.sleep) -> dict:
    if min_interval < 0.2:
        raise ValueError("min_interval below 0.2 s gets the API to block us (HTTP 403)")
    targets, _ = read_target_npis(target_npis)
    con = _cache(cache)
    done = {row[0] for row in con.execute("SELECT npi FROM nppes_raw")}
    todo = sorted(targets - done)[:limit]
    session = session or requests.Session()
    report = {"targets": len(targets), "already_cached": len(done & targets), "fetched": 0, "failed": 0,
              "blocked": 0, "stopped_early": False}
    pause_on_block, blocks_in_a_row, last = 60.0, 0, 0.0
    for i, npi in enumerate(todo, 1):
        wait = min_interval - (time.monotonic() - last)
        if wait > 0:
            sleep(wait)
        last = time.monotonic()
        try:
            response = session.get(API, params={"version": "2.1", "number": npi}, timeout=30)
        except requests.RequestException:
            report["failed"] += 1
            continue
        if response.status_code in (403, 429):
            report["blocked"] += 1
            blocks_in_a_row += 1
            if blocks_in_a_row >= 5:
                report["stopped_early"] = True
                break
            print(f"  HTTP {response.status_code} at {npi}; waiting {pause_on_block:.0f}s", file=sys.stderr)
            sleep(pause_on_block)
            pause_on_block *= 2
            continue
        blocks_in_a_row, pause_on_block = 0, 60.0
        if response.status_code != 200:
            report["failed"] += 1
            continue
        con.execute("INSERT OR REPLACE INTO nppes_raw VALUES (?, ?, ?)",
                    (npi, datetime.now(timezone.utc).replace(microsecond=0).isoformat(), response.text))
        report["fetched"] += 1
        if i % 500 == 0:
            con.commit()
            print(f"  fetched {i}/{len(todo)}", file=sys.stderr)
    con.commit()
    con.close()
    report["remaining"] = len(targets) - len(done & targets) - report["fetched"]
    return report


def build(target_npis: str | Path, out: str | Path, cache: Path = CACHE) -> dict:
    """Cache -> a new directory database, published only after integrity checks (as build_directory.build)."""
    destination = Path(out)
    if destination.name in {"rates.sqlite", "policy.sqlite"}:
        raise ValueError("Provider directory output cannot be a rate or policy database")
    if destination.exists():
        raise FileExistsError(f"Refusing to replace existing directory: {destination}")
    targets, invalid_target_rows = read_target_npis(target_npis)
    source = sqlite3.connect(f"file:{Path(cache).resolve(strict=True)}?mode=ro", uri=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="providers-", suffix=".sqlite", dir=destination.parent, delete=False) as t:
        staged = Path(t.name)
    try:
        con = sqlite3.connect(staged)
        con.executescript(SCHEMA.read_text(encoding="utf-8"))
        con.execute("PRAGMA foreign_keys=ON")
        matched, not_in_registry, fetched = set(), 0, []
        for npi, fetched_at, body in source.execute("SELECT npi, fetched_at, body FROM nppes_raw ORDER BY npi"):
            if npi not in targets:
                continue
            results = json.loads(body).get("results") or []
            if not results:
                not_in_registry += 1
                continue
            provider, taxonomies, locations = record_rows(results[0])
            con.execute("INSERT OR REPLACE INTO providers VALUES (?,?,?,?,?,?,?,?,?,?)", provider)
            con.executemany("INSERT OR REPLACE INTO provider_taxonomies VALUES (?,?,?)",
                            [(npi, code, flag) for code, flag in taxonomies])
            for loc in locations:
                _insert_location(con, npi, loc)
            matched.add(npi)
            fetched.append(fetched_at)
        metadata = {
            "cms_source_index": API, "cms_source_url": API, "nppes_archive_name": API_LABEL,
            "nppes_release_date": None, "fetched_from": min(fetched, default=None),
            "fetched_to": max(fetched, default=None),
            "imported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "script_version": f"api-{SCRIPT_VERSION}", "target_npis": len(targets),
            "invalid_target_rows": invalid_target_rows, "matched_providers": len(matched),
            "not_in_registry": not_in_registry, "not_fetched_yet": len(targets) - len(matched) - not_in_registry,
            "locations_imported": con.execute("SELECT COUNT(*) FROM provider_locations").fetchone()[0],
            "geocoded_locations": 0,
        }
        con.executemany("INSERT INTO import_metadata VALUES (?,?)",
                        [(key, str(value)) for key, value in metadata.items() if value is not None])
        con.commit()
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        con.close()
        if integrity != "ok" or foreign:
            raise ValueError(f"Directory integrity failed: {integrity}, foreign keys={foreign[:3]}")
        staged.replace(destination)
        return metadata
    finally:
        source.close()
        if staged.exists():
            staged.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("step", choices=["fetch", "build"])
    parser.add_argument("--target-npis", required=True)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--out", help="new providers.sqlite (build)")
    parser.add_argument("--min-interval", type=float, default=0.5, help="seconds between API calls (fetch)")
    parser.add_argument("--limit", type=int, help="fetch at most this many NPIs")
    args = parser.parse_args()
    if args.step == "fetch":
        print(json.dumps(fetch(args.target_npis, args.cache, args.min_interval, args.limit), indent=2))
    else:
        if not args.out:
            parser.error("build needs --out")
        print(json.dumps(build(args.target_npis, args.out, args.cache), indent=2))


if __name__ == "__main__":
    main()

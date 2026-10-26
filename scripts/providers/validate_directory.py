"""Validate a built providers database against its target NPI CSV."""
from __future__ import annotations

import argparse
import json
from contextlib import closing

try:
    from .common import read_only, read_target_npis, valid_npi
    from .geocode_directory import plausible_coordinates
except ImportError:
    from common import read_only, read_target_npis, valid_npi
    from geocode_directory import plausible_coordinates


def validate(db: str, target_npis: str) -> dict:
    targets, invalid_target_rows = read_target_npis(target_npis)
    with closing(read_only(db)) as con:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        npis = {row[0] for row in con.execute("SELECT npi FROM providers")}
        locations = con.execute("SELECT npi, address_line_1, city, state, latitude, longitude, geocode_status "
                                "FROM provider_locations").fetchall()
        missing_names = con.execute("SELECT COUNT(*) FROM providers WHERE display_name IS NULL OR display_name='' ").fetchone()[0]
        active = con.execute("SELECT COUNT(*) FROM providers WHERE nppes_status='active'").fetchone()[0]
        inactive = len(npis) - active
        type_counts = dict(con.execute("SELECT entity_type, COUNT(*) FROM providers GROUP BY entity_type").fetchall())
        primary_address = {r[0] for r in con.execute("SELECT npi FROM provider_locations WHERE location_type='primary' "
                                                     "AND address_line_1 <> '' AND city <> '' AND state <> ''")}
        multis = con.execute("SELECT COUNT(*) FROM (SELECT npi FROM provider_locations GROUP BY npi HAVING COUNT(*) > 1)").fetchone()[0]
        duplicates = con.execute("SELECT COUNT(*) FROM (SELECT npi,address_key FROM provider_locations "
                                 "GROUP BY npi,address_key HAVING COUNT(*)>1)").fetchone()[0]
        metadata = dict(con.execute("SELECT key,value FROM import_metadata").fetchall())
    invalid_coords = sum(1 for row in locations if (row[4] is None) != (row[5] is None) or
                         (row[4] is not None and not plausible_coordinates(row[4], row[5])) or
                         (row[6] == "matched" and row[4] is None))
    counts = {"target_npis": len(targets), "matched_providers": len(npis & targets),
              "match_rate": round(len(npis & targets) / len(targets), 4) if targets else None,
              "unmatched_targets": len(targets - npis), "unmatched_examples": sorted(targets - npis)[:10],
              "unexpected_providers": len(npis - targets), "invalid_target_rows": invalid_target_rows,
              "invalid_provider_npis": sum(not valid_npi(npi) for npi in npis),
              "missing_names": missing_names, "missing_primary_practice_address": len(npis - primary_address),
              "entity_type_1": type_counts.get("1", 0), "entity_type_2": type_counts.get("2", 0),
              "active": active, "inactive": inactive, "multi_location_providers": multis,
              "locations": len(locations), "geocoded": sum(row[6] == "matched" for row in locations),
              "ungeocoded": sum(row[6] != "matched" for row in locations),
              "duplicate_locations": duplicates, "invalid_coordinates": invalid_coords,
              "integrity_check": integrity, "foreign_key_errors": foreign_keys,
              "nppes_archive_name": metadata.get("nppes_archive_name"),
              "nppes_release_date": metadata.get("nppes_release_date"),
              "archive_sha256": metadata.get("archive_sha256")}
    counts["valid"] = (integrity == "ok" and foreign_keys == 0 and invalid_coords == 0 and
                       duplicates == 0 and counts["invalid_provider_npis"] == 0 and counts["unexpected_providers"] == 0)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--target-npis", required=True)
    args = parser.parse_args()
    result = validate(args.db, args.target_npis)
    print(json.dumps(result, indent=2))
    if not result["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

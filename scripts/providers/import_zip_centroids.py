"""Import Census ZCTA representative points as approximate user ZIP positions."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path

try:
    from .geocode_directory import plausible_coordinates
    from .common import require_provider_db_for_write
except ImportError:
    from geocode_directory import plausible_coordinates
    from common import require_provider_db_for_write


def import_centroids(db: str | Path, gazetteer_zip: str | Path, source_url: str, source_year: str) -> dict:
    db = require_provider_db_for_write(db)
    if not source_url.startswith("https://www2.census.gov/geo/"):
        raise ValueError("Source must be an official Census Gazetteer URL")
    if not re.fullmatch(r"20\d{2}", source_year):
        raise ValueError("source_year must be YYYY")
    count = skipped = 0
    with zipfile.ZipFile(gazetteer_zip) as archive, sqlite3.connect(db) as con:
        member = next((name for name in archive.namelist() if name.lower().endswith(".txt")), None)
        if member is None:
            raise ValueError("No Gazetteer text member found")
        with archive.open(member) as handle:
            rows = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig", newline=""), delimiter="\t")
            # The real Census file pads the last heading (and each row's last field) with spaces.
            rows.fieldnames = [name.strip() for name in rows.fieldnames or []]
            if not {"GEOID", "INTPTLAT", "INTPTLONG"}.issubset(rows.fieldnames or []):
                raise ValueError("Gazetteer headings GEOID, INTPTLAT, INTPTLONG are required")
            for row in rows:
                geoid = (row["GEOID"] or "").strip()
                try:
                    latitude, longitude = float(row["INTPTLAT"]), float(row["INTPTLONG"])
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                if not re.fullmatch(r"\d{5}", geoid) or not plausible_coordinates(latitude, longitude):
                    skipped += 1
                    continue
                con.execute("INSERT OR REPLACE INTO zip_centroids VALUES (?,?,?,?,?)",
                            (geoid, latitude, longitude, source_year, source_url))
                count += 1
        con.execute("INSERT OR REPLACE INTO import_metadata VALUES ('zip_centroid_source_url', ?)", (source_url,))
        con.execute("INSERT OR REPLACE INTO import_metadata VALUES ('zip_centroid_source_year', ?)", (source_year,))
    return {"imported": count, "skipped": skipped, "source_url": source_url, "source_year": source_year}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--gazetteer-zip", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-year", required=True)
    args = parser.parse_args()
    print(json.dumps(import_centroids(args.db, args.gazetteer_zip, args.source_url, args.source_year), indent=2))


if __name__ == "__main__":
    main()

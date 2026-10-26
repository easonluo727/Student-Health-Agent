"""Resumable Census address geocoding for the separate providers database.

By default addresses go to the Census batch geocoder, 1,000 per request; --batch-size 0 sends one request per
address. Both apply the same checks: one match, same state, ZIP and house number, plausible coordinates.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from .common import read_only, require_provider_db_for_write
except ImportError:
    from common import read_only, require_provider_db_for_write

SOURCE = "https://geocoding.geo.census.gov/geocoder/locations/address"
BATCH_SOURCE = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
SOURCE_LABEL = "US Census Public_AR_Current"


def plausible_coordinates(latitude: float, longitude: float) -> bool:
    return -15 <= latitude <= 72 and -180 <= longitude <= -60


def census_geocode(location: dict) -> dict:
    response = requests.get(SOURCE, params={"street": location["address_line_1"], "city": location["city"],
                                            "state": location["state"], "zip": location["zip5"],
                                            "benchmark": "Public_AR_Current", "format": "json"}, timeout=(10, 30))
    response.raise_for_status()
    matches = response.json().get("result", {}).get("addressMatches", [])
    if len(matches) != 1:
        return {"status": "ambiguous" if matches else "failed"}
    match = matches[0]
    parts = match.get("addressComponents") or {}
    if parts.get("state", "").upper() != location["state"].upper():
        return {"status": "ambiguous"}
    if location["zip5"] and parts.get("zip") != location["zip5"]:
        return {"status": "ambiguous"}
    input_number = location["address_line_1"].split(" ", 1)[0]
    output_number = (match.get("matchedAddress") or "").split(" ", 1)[0]
    if input_number.isdigit() and input_number != output_number:
        return {"status": "ambiguous"}
    coords = match.get("coordinates") or {}
    try:
        latitude, longitude = float(coords["y"]), float(coords["x"])
    except (KeyError, TypeError, ValueError):
        return {"status": "failed"}
    if not plausible_coordinates(latitude, longitude):
        return {"status": "ambiguous"}
    return {"status": "matched", "latitude": latitude, "longitude": longitude,
            "precision": "street_interpolated", "source": SOURCE_LABEL}


def _checked(location: dict, matched_address: str, latitude: float, longitude: float) -> dict:
    """The single-address checks, on a batch match: same state, ZIP and house number, plausible point."""
    parts = [part.strip() for part in matched_address.split(",")]
    if len(parts) < 4 or parts[2].upper() != (location["state"] or "").upper():
        return {"status": "ambiguous"}
    if location["zip5"] and parts[3][:5] != location["zip5"]:
        return {"status": "ambiguous"}
    input_number = location["address_line_1"].split(" ", 1)[0]
    if input_number.isdigit() and input_number != parts[0].split(" ", 1)[0]:
        return {"status": "ambiguous"}
    if not plausible_coordinates(latitude, longitude):
        return {"status": "ambiguous"}
    return {"status": "matched", "latitude": latitude, "longitude": longitude,
            "precision": "street_interpolated", "source": SOURCE_LABEL}


def parse_census_batch(text: str, locations: dict[str, dict]) -> dict[str, dict]:
    """Census batch CSV -> {id: match}. 'Tie' is ambiguous, 'No_Match' failed; ids missing from the reply failed."""
    out = {key: {"status": "failed"} for key in locations}
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) < 3 or rec[0] not in locations:
            continue
        if rec[2] == "Tie":
            out[rec[0]] = {"status": "ambiguous"}
        elif rec[2] == "Match" and len(rec) >= 6 and rec[5]:
            try:
                longitude, latitude = (float(x) for x in rec[5].split(","))
            except ValueError:
                continue
            out[rec[0]] = _checked(locations[rec[0]], rec[4], latitude, longitude)
    return out


def census_batch(locations: list[dict]) -> dict[str, dict]:
    """One Census batch request (up to 10,000 addresses) -> {address_key: match}."""
    by_id = {str(i): location for i, location in enumerate(locations)}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for i, location in by_id.items():
        writer.writerow([i, location["address_line_1"], location["city"], location["state"], location["zip5"]])
    response = requests.post(BATCH_SOURCE, files={"addressFile": ("addresses.csv", buffer.getvalue())},
                             data={"benchmark": "Public_AR_Current"}, timeout=(10, 900))
    response.raise_for_status()
    return {by_id[i]["address_key"]: match for i, match in parse_census_batch(response.text, by_id).items()}


def enrich(db: str | Path, limit: int = 100, dry_run: bool = False, resume: bool = False,
           min_interval: float = 1.0, geocoder=None, retry_failed: bool = False,
           batch_size: int = 0, batch_geocoder=None) -> dict:
    if limit < 1 or min_interval < 0 or not 0 <= batch_size <= 10000:
        raise ValueError("limit must be positive, min_interval nonnegative, batch_size 0-10000")
    db = require_provider_db_for_write(db)
    connection = read_only(db) if dry_run else sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    if not dry_run:
        connection.execute("PRAGMA foreign_keys=ON")
    if not dry_run and not resume and not retry_failed and connection.execute(
            "SELECT EXISTS(SELECT 1 FROM provider_locations WHERE geocoded_at IS NOT NULL)").fetchone()[0]:
        connection.close()
        raise ValueError("Geocoding has already started; pass --resume to continue pending locations")
    statuses = ("pending", "failed") if retry_failed else ("pending",)
    rows = [dict(row) for row in connection.execute(
        f"SELECT * FROM provider_locations WHERE geocode_status IN ({','.join('?' for _ in statuses)}) "
        "ORDER BY location_id LIMIT ?", (*statuses, limit))]
    result = {"selected": len(rows), "matched": 0, "ambiguous": 0, "failed": 0,
              "cached": 0, "requests": 0, "dry_run": dry_run, "resume": resume}
    if dry_run:
        connection.close()
        return result
    geocoder = geocoder or census_geocode
    last_request = 0.0
    batched: dict[str, dict] = {}
    try:
        if batch_size:
            # Look up every uncached address of this run in batches first; the loop below then records them.
            pending: dict[str, dict] = {}
            for row in rows:
                cached = connection.execute("SELECT status FROM geocode_cache WHERE address_key=?",
                                            (row["address_key"],)).fetchone()
                if ((not cached or (cached["status"] == "failed" and retry_failed)) and row["address_line_1"]
                        and row["state"] and row["country"] == "US"):
                    pending.setdefault(row["address_key"], row)
            todo = list(pending.values())
            for start in range(0, len(todo), batch_size):
                pause = min_interval - (time.monotonic() - last_request)
                if pause > 0:
                    time.sleep(pause)
                last_request = time.monotonic()
                result["requests"] += 1
                chunk = todo[start:start + batch_size]
                try:
                    batched.update((batch_geocoder or census_batch)(chunk))
                except (requests.RequestException, ValueError, KeyError):
                    batched.update((row["address_key"], {"status": "failed"}) for row in chunk)
        for row in rows:
            key = row["address_key"]
            cached = connection.execute("SELECT * FROM geocode_cache WHERE address_key=?", (key,)).fetchone()
            if cached and (cached["status"] != "failed" or not retry_failed):
                match = dict(cached)
                result["cached"] += 1
            elif not row["address_line_1"] or not row["state"] or row["country"] != "US":
                match = {"status": "failed"}
            elif key in batched:
                match = batched[key]
            else:
                pause = min_interval - (time.monotonic() - last_request)
                if pause > 0:
                    time.sleep(pause)
                last_request = time.monotonic()
                result["requests"] += 1
                try:
                    match = geocoder(row)
                except (requests.RequestException, ValueError, KeyError):
                    match = {"status": "failed"}
            status = match.get("status", "failed")
            latitude, longitude = match.get("latitude"), match.get("longitude")
            if (status != "matched" or latitude is None or longitude is None or
                    not plausible_coordinates(float(latitude), float(longitude))):
                latitude = longitude = None
                status = status if status in {"ambiguous", "failed"} else "failed"
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            source = match.get("source", SOURCE_LABEL) if status == "matched" else None
            precision = match.get("precision") if status == "matched" else None
            connection.execute("INSERT OR REPLACE INTO geocode_cache VALUES (?,?,?,?,?,?,?)",
                               (key, latitude, longitude, status, precision, source, now))
            connection.execute("UPDATE provider_locations SET latitude=?, longitude=?, geocode_status=?, "
                               "geocode_precision=?, geocode_source=?, geocoded_at=? WHERE location_id=?",
                               (latitude, longitude, status, precision, source, now, row["location_id"]))
            result[status] += 1
            connection.commit()
        count = connection.execute("SELECT COUNT(*) FROM provider_locations WHERE geocode_status='matched'").fetchone()[0]
        connection.execute("INSERT OR REPLACE INTO import_metadata VALUES ('geocoded_locations', ?)", (str(count),))
        connection.commit()
    finally:
        connection.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Continue pending locations in location_id order")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--min-interval", type=float, default=1.0, help="Seconds between external requests")
    parser.add_argument("--batch-size", type=int, default=1000,
                        help="Addresses per Census batch request (max 10000); 0 sends one request per address")
    args = parser.parse_args()
    print(json.dumps(enrich(args.db, args.limit, args.dry_run, args.resume, args.min_interval,
                            retry_failed=args.retry_failed, batch_size=args.batch_size), indent=2))


if __name__ == "__main__":
    main()

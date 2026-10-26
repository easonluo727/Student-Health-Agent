"""Build a small provider directory from an official NPPES V2 monthly ZIP and target NPIs."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mrf"))
from build_npi_set import _member, _primary_taxonomy, _rows  # noqa: E402

try:
    from .common import normalized_address, read_target_npis, zip5
except ImportError:
    from common import normalized_address, read_target_npis, zip5

SCHEMA = Path(__file__).with_name("schema.sql")
SOURCE_INDEX = "https://download.cms.gov/nppes/NPI_Files.html"
SCRIPT_VERSION = "1"


def _text(row: dict, *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value:
            return value.strip()
    return ""


def _taxonomy_rows(row: dict) -> list[tuple[str, int]]:
    found: dict[str, int] = {}
    for heading, value in row.items():
        match = re.fullmatch(r"Healthcare Provider Taxonomy Code_(\d+)", heading or "")
        if not match or not value:
            continue
        code = value.strip()
        switch = row.get(f"Healthcare Provider Primary Taxonomy Switch_{match.group(1)}", "")
        found[code] = max(found.get(code, 0), int(switch.strip().upper() == "Y"))
    return list(found.items())


def _location(row: dict, secondary: bool) -> dict:
    stem = "Provider Secondary Practice Location Address" if secondary else "Provider Business Practice Location Address"
    line1 = _text(row, stem + " - Address Line 1", stem + " Line 1")
    line2 = _text(row, stem + " - Address Line 2", stem + " Line 2")
    city = _text(row, stem + " - City Name", stem + " City Name")
    state = _text(row, stem + " - State Name", stem + " State Name").upper()
    postal = _text(row, stem + " - Postal Code", stem + " Postal Code")
    country = _text(row, stem + " - Country Code (If outside U.S.)", stem + " Country Code (If outside U.S.)") or "US"
    phone = _text(row, stem + " - Telephone Number", stem + " Telephone Number")
    fax = _text(row, stem + " - Fax Number", stem + " Fax Number")
    return {"location_type": "secondary" if secondary else "primary", "address_line_1": line1,
            "address_line_2": line2, "city": city, "state": state, "zip5": zip5(postal),
            "postal_code": postal, "country": country.upper(), "phone": phone, "fax": fax,
            "address_key": normalized_address(line1, line2, city, state, postal, country.upper())}


def _insert_location(con: sqlite3.Connection, npi: str, loc: dict) -> None:
    if not any((loc["address_line_1"], loc["city"], loc["postal_code"])):
        return
    fields = ("npi", "location_type", "address_key", "address_line_1", "address_line_2", "city", "state",
              "zip5", "postal_code", "country", "phone", "fax")
    values = {"npi": npi, **loc}
    con.execute(f"INSERT OR IGNORE INTO provider_locations ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                tuple(values[field] for field in fields))


def _archive_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_and_rest(rows, required: set[str], member: str):
    first = next(rows, None)
    if first is None or not required.issubset(first):
        raise ValueError(f"{member} lacks required NPPES headings: {sorted(required)}")
    return itertools.chain((first,), rows)


def build(nppes_zip: str | Path, target_npis: str | Path, out: str | Path,
          release_date: str, downloaded_at: str, source_url: str = SOURCE_INDEX) -> dict:
    """Build into a temporary sibling and publish only after successful integrity checks."""
    datetime.strptime(release_date, "%Y-%m-%d")
    datetime.strptime(downloaded_at, "%Y-%m-%d")
    if not source_url.startswith("https://download.cms.gov/nppes/"):
        raise ValueError("--source-url must identify an official CMS NPPES download or index")
    archive, destination = Path(nppes_zip), Path(out)
    if destination.name in {"rates.sqlite", "policy.sqlite"}:
        raise ValueError("Provider directory output cannot be a rate or policy database")
    if destination.exists():
        raise FileExistsError(f"Refusing to replace existing directory: {destination}")
    targets, invalid_target_rows = read_target_npis(target_npis)
    if not targets:
        raise ValueError("Target NPI CSV contains no valid NPIs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sha256 = _archive_sha256(archive)
    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    start = time.monotonic()
    with tempfile.NamedTemporaryFile(prefix="providers-", suffix=".sqlite", dir=destination.parent, delete=False) as temp:
        staged = Path(temp.name)
    try:
        con = sqlite3.connect(staged)
        con.executescript(SCHEMA.read_text(encoding="utf-8"))
        con.execute("PRAGMA foreign_keys=ON")
        matched: set[str] = set()
        scanned = invalid_nppes = 0
        with zipfile.ZipFile(archive) as zipped:
            main_info = _member(zipped, "npidata_pfile")
            secondary_info = _member(zipped, "pl_pfile")
            main_rows = _first_and_rest(_rows(zipped, main_info),
                                        {"NPI", "Entity Type Code", "Provider Business Practice Location Address Line 1"},
                                        main_info.filename)
            for row in main_rows:
                scanned += 1
                npi = (row.get("NPI") or "").strip()
                if npi not in targets:
                    continue
                etype = _text(row, "Entity Type Code")
                if etype not in {"1", "2"}:
                    invalid_nppes += 1
                    continue
                inactive = bool(_text(row, "NPI Deactivation Date") and not _text(row, "NPI Reactivation Date"))
                first = _text(row, "Provider First Name")
                last = _text(row, "Provider Last Name (Legal Name)")
                org = _text(row, "Provider Organization Name (Legal Business Name)")
                name = (" ".join(filter(None, (first, last))) if etype == "1" else org) or None
                taxonomies = _taxonomy_rows(row)
                primary = next((code for code, switch in taxonomies if switch), None) or _primary_taxonomy(row) or None
                con.execute("INSERT OR REPLACE INTO providers VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (npi, etype, name, first or None, last or None,
                             _text(row, "Provider Credential Text") or None, org or None, primary,
                             _text(row, "Last Update Date") or None, "inactive" if inactive else "active"))
                for code, is_primary in taxonomies:
                    con.execute("INSERT OR REPLACE INTO provider_taxonomies VALUES (?,?,?)", (npi, code, is_primary))
                _insert_location(con, npi, _location(row, False))
                matched.add(npi)
                if len(matched) % 1000 == 0:
                    con.commit()
            secondary_rows = _first_and_rest(_rows(zipped, secondary_info), {"NPI"}, secondary_info.filename)
            secondary_seen = 0
            for row in secondary_rows:
                secondary_seen += 1
                npi = (row.get("NPI") or "").strip()
                if npi in matched:
                    _insert_location(con, npi, _location(row, True))
                if secondary_seen % 10000 == 0:
                    con.commit()
        counts = {
            "target_npis": len(targets), "invalid_target_rows": invalid_target_rows,
            "nppes_rows_scanned": scanned, "matched_providers": len(matched),
            "unmatched_targets": len(targets - matched), "invalid_target_nppes_rows": invalid_nppes,
            "active_providers": con.execute("SELECT COUNT(*) FROM providers WHERE nppes_status='active'").fetchone()[0],
            "inactive_providers": con.execute("SELECT COUNT(*) FROM providers WHERE nppes_status='inactive'").fetchone()[0],
            "locations_imported": con.execute("SELECT COUNT(*) FROM provider_locations").fetchone()[0],
            "geocoded_locations": 0,
        }
        metadata = {"cms_source_index": SOURCE_INDEX, "cms_source_url": source_url,
                    "nppes_archive_name": archive.name, "nppes_release_date": release_date,
                    "downloaded_at": downloaded_at, "imported_at": imported_at, "archive_sha256": sha256,
                    "script_version": SCRIPT_VERSION, "build_seconds": round(time.monotonic() - start, 1),
                    **counts}
        con.executemany("INSERT INTO import_metadata VALUES (?,?)", [(key, str(value)) for key, value in metadata.items()])
        con.commit()
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        con.close()
        if integrity != "ok" or foreign:
            raise ValueError(f"Directory integrity failed: {integrity}, foreign keys={foreign[:3]}")
        staged.replace(destination)
        return metadata
    finally:
        if staged.exists():
            staged.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nppes-zip", required=True)
    parser.add_argument("--target-npis", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--release-date", required=True, help="YYYY-MM-DD, verified on the CMS release index")
    parser.add_argument("--downloaded-at", required=True, help="YYYY-MM-DD when this archive was downloaded")
    parser.add_argument("--source-url", default=SOURCE_INDEX, help="Official CMS archive URL, if known")
    args = parser.parse_args()
    print(json.dumps(build(args.nppes_zip, args.target_npis, args.out, args.release_date,
                           args.downloaded_at, args.source_url), indent=2))


if __name__ == "__main__":
    main()

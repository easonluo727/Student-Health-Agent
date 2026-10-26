"""Shared validation and read-only SQLite helpers for provider directory tools."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


def valid_npi(value: str) -> bool:
    """NPI check digit uses Luhn on the 80840 prefix and ten NPI digits."""
    if not re.fullmatch(r"[12]\d{9}", value):
        return False
    digits = [int(c) for c in "80840" + value]
    total = 0
    for offset, digit in enumerate(reversed(digits)):
        if offset % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def read_target_npis(path: str | Path) -> tuple[set[str], int]:
    import csv

    valid: set[str] = set()
    invalid = 0
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "npi" not in reader.fieldnames:
            raise ValueError("Target CSV needs a 'npi' column")
        for row in reader:
            value = (row.get("npi") or "").strip()
            if valid_npi(value):
                valid.add(value)
            else:
                invalid += 1
    return valid, invalid


def read_only(path: str | Path) -> sqlite3.Connection:
    location = Path(path).resolve(strict=True)
    con = sqlite3.connect(f"file:{quote(str(location))}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def require_provider_db_for_write(path: str | Path) -> Path:
    """Refuse to create a DB or enrich a rate/policy store by accident."""
    location = Path(path)
    if location.name in {"rates.sqlite", "policy.sqlite"} or not location.is_file():
        raise ValueError("A pre-existing providers.sqlite is required for enrichment")
    with closing(read_only(location)) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"providers", "provider_locations", "import_metadata"}.issubset(tables):
        raise ValueError("This file is not a provider directory database")
    return location


def normalized_address(*parts: str) -> str:
    return "|".join(re.sub(r"[^A-Z0-9]+", " ", (part or "").upper()).strip() for part in parts)


def zip5(value: str) -> str | None:
    match = re.match(r"^(\d{5})", (value or "").strip())
    return match.group(1) if match else None

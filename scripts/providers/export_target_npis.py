"""Export the distinct valid target NPIs from the existing rates database, read only."""
from __future__ import annotations

import argparse
import csv
import json
from contextlib import closing
from pathlib import Path

try:
    from .common import read_only, valid_npi
except ImportError:
    from common import read_only, valid_npi


def export(rates_db: str | Path, out: str | Path) -> dict:
    destination = Path(out)
    if destination.suffix.lower() in {".sqlite", ".db"} or destination.resolve() == Path(rates_db).resolve():
        raise ValueError("Target NPI output must be a CSV, never a database file")
    with closing(read_only(rates_db)) as con:
        values = [row[0] for row in con.execute(
            "SELECT DISTINCT npi FROM provider_members WHERE npi IS NOT NULL AND npi <> ''")]
    npis = sorted(value for value in values if isinstance(value, str) and valid_npi(value))
    skipped = sorted(str(value) for value in values if not isinstance(value, str) or not valid_npi(value))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["npi"])
        writer.writerows((npi,) for npi in npis)
    return {"distinct_nonempty": len(values), "exported": len(npis), "skipped_invalid": len(skipped),
            "invalid_examples": skipped[:10], "out": str(destination)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rates-db", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.rates_db, args.out), indent=2))


if __name__ == "__main__":
    main()

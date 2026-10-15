"""Build overlay/data/encounter_labels.v1.json: the controlled encounter_key -> CSV label map.

    python build_encounter_labels.py [--check]

Deterministic: reads data/mrf/encounters.csv (stable keys + display labels) and checks that every label
appears in table A of data/mrf/allowed_amounts.csv. The resolver uses only this file to translate keys
to the CSV's display labels; there is no fuzzy matching at runtime. --check fails if the file is stale.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys

import overlay_config

overlay_config.install_import_paths()
from rate_resolver import load_encounters  # noqa: E402


def build(paths: overlay_config.OverlayPaths) -> dict:
    encounters = load_encounters(paths.encounters_csv)
    labels = [e.label for e in encounters.values()]
    if len(set(labels)) != len(labels):
        raise SystemExit("encounters.csv: two encounter keys share a label; labels must be unique")
    with open(paths.rates_csv, newline="", encoding="utf-8") as f:
        csv_labels = {r["encounter"] for r in csv.DictReader(f) if r["table"] == "A_encounter"}
    with open(paths.encounters_csv, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return {"schema_version": "encounter_labels.v1",
            "generated_from": {"encounters_csv": "data/mrf/encounters.csv", "sha256": digest},
            "entries": [{"encounter_key": k, "csv_label": e.label, "in_rates_csv": e.label in csv_labels}
                        for k, e in sorted(encounters.items())]}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    paths = overlay_config.load_paths()
    doc = build(paths)
    out = paths.overlay_data / "encounter_labels.v1.json"
    if a.check:
        current = json.loads(out.read_text())
        sys.exit(0 if current == doc else "encounter_labels.v1.json is stale; rebuild it")
    out.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"{len(doc['entries'])} encounters, {sum(e['in_rates_csv'] for e in doc['entries'])} present in table A")


if __name__ == "__main__":
    main()

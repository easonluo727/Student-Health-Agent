"""Human review of AMD-extracted policy rules. No LLM.

1. Merge import batches and write a review sheet:
     python scripts/policy/review_sheet.py build OUT_RULES.json REVIEW.csv BATCH.json [BATCH.json ...]
         [--columns "UC Family=uc-family,Network=in-network,Out-of-Network=out-of-network"]
   The sheet has one row per rule: the extracted values next to the PDF cell they came from,
   and an empty `verified` column.

2. A person checks each row against the PDF, writes `yes` in `verified` (or fixes nothing and
   leaves it blank), then applies the sheet:
     python scripts/policy/review_sheet.py apply OUT_RULES.json REVIEW.csv
   Only rows marked `yes` get needs_review = false; every other rule is set to needs_review = true,
   so the calculator refuses it. Values are never edited here; fix extraction errors by re-importing.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

FIELDS = ["copay_usd", "plan_pays_percentage", "patient_pays_percentage", "deductible_applies",
          "coverage_status", "prior_authorization"]
SHEET = ["rule_index", "source_page", "service", "network", *FIELDS, "model_needs_review",
         "pdf_cell_text", "verified", "reviewer_note"]


def column_keys(spec):
    """Network -> the row key import_policy.py used for that column."""
    items = [item.partition("=") for item in spec.split(",")]
    if spec == "Preferred Provider=in-network,Out-of-Network=out-of-network":
        return {"in-network": "preferred", "out-of-network": "out_of_network"}
    return {network.strip(): f"column_{i}" for i, (_, _, network) in enumerate(items, start=1)}


def build(args):
    rules = []
    for batch in args.batches:
        rules.extend(json.loads(Path(batch).read_text(encoding="utf-8")))
    rules.sort(key=lambda r: r.get("source_page") or 0)  # stable: keeps row order within a page
    keys = column_keys(args.columns)
    pages = sorted({r.get("source_page") for r in rules})
    Path(args.rules).parent.mkdir(parents=True, exist_ok=True)
    Path(args.rules).write_text(json.dumps(rules, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with open(args.sheet, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=SHEET)
        writer.writeheader()
        for index, rule in enumerate(rules):
            row = json.loads(rule.get("source_text") or "{}")
            writer.writerow({
                "rule_index": index, "source_page": rule.get("source_page"),
                "service": rule.get("service"), "network": rule.get("network"),
                **{field: rule.get(field) for field in FIELDS},
                "model_needs_review": rule.get("needs_review"),
                "pdf_cell_text": " ".join(str(row.get(keys.get(rule.get("network"), ""), "")).split()),
                "verified": "", "reviewer_note": "",
            })
    print(f"{len(rules)} rules from pages {pages[0]}-{pages[-1]} ({len(pages)} pages) -> {args.rules}")
    print(f"Review sheet -> {args.sheet}")


def apply(args):
    rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
    with open(args.sheet, newline="", encoding="utf-8") as file:
        sheet = list(csv.DictReader(file))
    if len(sheet) != len(rules):
        sys.exit(f"Sheet has {len(sheet)} rows but the rules file has {len(rules)}; rebuild the sheet.")
    verified = 0
    for row, rule in zip(sheet, rules):
        if int(row["rule_index"]) != rules.index(rule) or row["service"] != rule.get("service"):
            sys.exit(f"Sheet row {row['rule_index']} doesn't match the rules file; rebuild the sheet.")
        ok = row["verified"].strip().lower() == "yes"
        rule["needs_review"] = not ok
        verified += ok
    Path(args.rules).write_text(json.dumps(rules, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{verified} of {len(rules)} rules verified; the rest are needs_review = true.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("rules"), b.add_argument("sheet"), b.add_argument("batches", nargs="+")
    b.add_argument("--columns", default="Preferred Provider=in-network,Out-of-Network=out-of-network")
    a = sub.add_parser("apply")
    a.add_argument("rules"), a.add_argument("sheet")
    args = parser.parse_args()
    build(args) if args.command == "build" else apply(args)


if __name__ == "__main__":
    main()

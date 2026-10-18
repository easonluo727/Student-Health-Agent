"""Run: python -m unittest discover tests   (no LLM, no network)."""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "policy" / "review_sheet.py"
COLUMNS = "UC Family=uc-family,Network=in-network,Out-of-Network=out-of-network"


def rule(page, service, network, needs_review):
    row = {"service": service, "column_1": "10% Coinsurance", "column_2": "20% Coinsurance after Deductible",
           "column_3": "50% Coinsurance after Deductible"}
    return {"plan": "p", "service": service, "network": network, "copay_usd": None,
            "plan_pays_percentage": 80, "patient_pays_percentage": 20, "deductible_applies": True,
            "coverage_status": "covered", "prior_authorization": "not_stated",
            "source_text": json.dumps(row), "source_page": page, "needs_review": needs_review}


class ReviewSheet(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=True)

    def test_build_then_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "b2.json").write_text(json.dumps([rule(31, "x-ray", "out-of-network", False)]))
            (tmp / "b1.json").write_text(json.dumps([rule(30, "allergy", "uc-family", False),
                                                     rule(30, "allergy", "in-network", True)]))
            rules, sheet = tmp / "rules.json", tmp / "review.csv"
            self.run_script("build", str(rules), str(sheet), str(tmp / "b2.json"), str(tmp / "b1.json"),
                            "--columns", COLUMNS)

            rows = list(csv.DictReader(sheet.open()))
            self.assertEqual([r["source_page"] for r in rows], ["30", "30", "31"])  # merged, page order
            self.assertEqual(rows[0]["pdf_cell_text"], "10% Coinsurance")  # the cell for that network
            self.assertEqual(rows[2]["pdf_cell_text"], "50% Coinsurance after Deductible")

            rows[1]["verified"] = "yes"  # only the human-checked row
            with sheet.open("w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            self.run_script("apply", str(rules), str(sheet))
            # The model said needs_review=False for rows 0 and 2; without a human "yes" they stay blocked.
            self.assertEqual([r["needs_review"] for r in json.loads(rules.read_text())], [True, False, True])


if __name__ == "__main__":
    unittest.main()

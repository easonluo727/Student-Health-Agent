import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "policy"))
import import_plan_csv as imp  # noqa: E402


class ImportPlanCsv(unittest.TestCase):
    def test_only_verified_rows_are_calculable_and_bad_rows_skipped(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "p.csv").write_text("plan_id,insurer,network_name,deductible_in_usd,verified\nx,Co,Net,\"$1,000\",yes\n")
        (tmp / "b.csv").write_text("plan_id,service,network,copay_usd,coinsurance_pct,deductible_applies,verified\n"
                                   "x,lab_tests,in,,20%,yes,yes\nx,urgent_care,in,$40,,no,no\nx,dental,in,,,,yes\n")
        imp.OUT = tmp / "out"
        imp.main(tmp / "p.csv", tmp / "b.csv")
        plans = json.loads((tmp / "out" / "plans.json").read_text())
        rules = json.loads((tmp / "out" / "rules.json").read_text())
        self.assertEqual(plans[0]["deductible_in_usd"], 1000)
        self.assertEqual([(r["service"], r["needs_review"]) for r in rules], [("lab_tests", False), ("urgent_care", True)])
        self.assertEqual((rules[0]["patient_pays_percentage"], rules[0]["plan_pays_percentage"]), (20, 80))


if __name__ == "__main__":
    unittest.main()

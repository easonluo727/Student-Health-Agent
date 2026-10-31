import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "policy"))
import extract_sbc as ex  # noqa: E402


class ExtractSbc(unittest.TestCase):
    def test_find_pages_uses_the_sbc_template_wording(self):
        texts = ["Important Questions  What is the overall deductible?", "Common Medical Event  Services You May Need",
                 "Services Your Plan Generally Does NOT Cover", "Your Grievance and Appeals Rights", ""]
        self.assertEqual(ex.find_pages(texts), (1, [2, 3]))

    def test_parse_json_strips_thinking_and_fences(self):
        self.assertEqual(ex.parse_json('<think>hmm</think>\n```json\n{"rows": []}\n```'), {"rows": []})
        self.assertEqual(ex.parse_json('Here you go: {"rows": [1]} done'), {"rows": [1]})

    def test_assemble_fills_plan_fields_and_drops_rows_repeated_across_pages(self):
        row = {"section": "If you have a test", "service": "xray", "service_as_printed": "Diagnostic test",
               "component": None, "network": "in", "network_tier": "UC Family", "copay_usd": None,
               "coinsurance_pct": 20, "deductible_applies": False, "prior_auth": None, "covered": True,
               "limits_conditions": None, "quote": "20% coinsurance"}
        summary = {"plan": {"insurer": "Anthem", "plan_name": "UC SHIP", "deductible_in_usd": 0}, "rows": []}
        plan, rows, dropped = ex.assemble("p", "https://x", summary, [(2, [row]), (3, [dict(row)])])
        self.assertEqual((plan["plan_id"], plan["deductible_in_usd"], plan["oop_max_in_usd"]), ("p", 0, "NOT FOUND"))
        self.assertEqual((len(rows), dropped), (1, 1))
        r = rows[0]
        self.assertEqual((r["copay_usd"], r["coinsurance_pct"], r["deductible_applies"], r["covered"]),
                         ("NOT FOUND", 20, "no", "yes"))
        self.assertEqual((r["component"], r["limits_conditions"], r["source_page"], r["verified"]), ("", "", 2, ""))

    def test_services_covered_before_the_deductible_unless_the_cell_says_otherwise(self):
        rows = [{"network": "in", "service": "primary_care_visit", "deductible_applies": "yes", "quote": "$50 copay"},
                {"network": "in", "service": "specialist_visit", "deductible_applies": "yes",
                 "quote": "$90 copay after deductible"},
                {"network": "in", "service": "xray", "deductible_applies": "yes", "quote": "$95 copay"},
                {"network": "out", "service": "primary_care_visit", "deductible_applies": "yes", "quote": "50%"}]
        changed = ex.apply_before_deductible(rows, ["primary_care_visit", "specialist_visit"])
        self.assertEqual(changed, 1)
        self.assertEqual([r["deductible_applies"] for r in rows], ["no", "yes", "yes", "yes"])

    def test_parse_pages(self):
        self.assertEqual(ex.parse_pages("2-4,6"), [2, 3, 4, 6])

    def test_importable_extractor_writes_session_csvs(self):
        summary = {"plan": {"insurer": None, "plan_name": None, "plan_year": "2026", "plan_type": "PPO"},
                   "rows": []}
        benefit = {"rows": [{"section": "If you visit a health care provider's office or clinic",
                              "service": "primary_care_visit", "service_as_printed": "Primary care visit",
                              "component": "office_visit", "network": "in", "network_tier": None,
                              "copay_usd": 25, "coinsurance_pct": None, "deductible_applies": False,
                              "prior_auth": None, "covered": True, "limits_conditions": None,
                              "quote": "$25 copay"}]}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ex, "page_texts", return_value=["Important Questions", "Common Medical Event"]), \
                patch.object(ex, "render", return_value="image"), \
                patch.object(ex, "before_deductible", return_value=[]), \
                patch.object(ex, "ask", side_effect=lambda _c, _m, prompt, _pages, _ctx="":
                             summary if "overall deductible" in prompt else benefit):
            out = Path(tmp) / "extracted" / "session_x"
            result = ex.extract(object(), "model", Path(tmp) / "plan.pdf", "session_x", out=out,
                                identity={"plan_name": "Uploaded Plan", "insurer": "Uploaded Insurer"}, workers=1)
            self.assertTrue((out / "plans.csv").exists())
            self.assertTrue((out / "benefits.csv").exists())
            self.assertEqual(result["plan"]["plan_name"], "Uploaded Plan")
            self.assertEqual((len(result["benefits"]), result["benefits"][0]["source_page"]), (1, 2))


if __name__ == "__main__":
    unittest.main()

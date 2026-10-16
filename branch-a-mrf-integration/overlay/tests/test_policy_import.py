"""Policy import: metadata and Schedule of Benefits extracted separately, with source pages; no LLM."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import fixtures as fx  # noqa: F401  (sets import paths)
import policy_import_v2 as imp
from schema_check import SchemaError, validate


class FakeExtractor:
    def __init__(self, metadata, rows, normalized):
        self.metadata, self.rows, self.normalized = metadata, rows, normalized
        self.calls = []

    def extract_metadata(self, pages):
        self.calls.append(("metadata", sorted(pages)))
        return self.metadata

    def extract_rows(self, page, png):
        self.calls.append(("rows", page))
        return self.rows.get(page, [])

    def normalize(self, entries):
        self.calls.append(("normalize", [e["entry_id"] for e in entries]))
        return [dict(self.normalized[e["entry_id"]], entry_id=e["entry_id"]) for e in entries]


META = {
    "issuer": {"value": "Anthem Blue Cross", "source_page": 1},
    "plan_name": {"value": "Student Health PPO", "source_page": 1},
    "product_ids": [{"id_type": "ein", "value": "943067788", "source_page": 2}],
    "region": {"value": "California", "source_page": 1},
    "coverage_year": {"value": "2026-2027", "source_page": 2},
    "effective_start": {"value": "2026-08-01", "source_page": 2},
    "effective_end": {"value": "July 31, 2027", "source_page": 2},  # not ISO -> dropped, flagged
    "networks": [{"name_in_pdf": "Prudent Buyer PPO", "policy_network_category": "in-network", "source_page": 2},
                 {"name_in_pdf": "Ghost Network", "policy_network_category": "in-network", "source_page": 9}],
}
ROWS = {12: [{"setting": "outpatient", "service": "Physician Office Visits\nper visit",
              "preferred": "$25 Copay", "out_of_network": "40% Coinsurance after Deductible"}],
        13: [{"setting": "outpatient", "service": "Laboratory Procedures", "preferred": "20% Coinsurance",
              "out_of_network": None}]}
NORM = {
    1: {"copay_usd": 25, "plan_pays_percentage": None, "patient_pays_percentage": None, "deductible_applies": False,
        "prior_authorization": "not_stated", "coverage_status": "covered", "needs_review": False},
    2: {"copay_usd": None, "plan_pays_percentage": 60, "patient_pays_percentage": 40, "deductible_applies": True,
        "prior_authorization": "not_stated", "coverage_status": "covered", "needs_review": False},
    3: {"copay_usd": None, "plan_pays_percentage": 70, "patient_pays_percentage": 20, "deductible_applies": False,
        "prior_authorization": "not_stated", "coverage_status": "covered", "needs_review": False},  # 70+20 != 100
}


class Import(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = self.tmp / "policy.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake")
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_import(self, meta=META):
        ext = FakeExtractor(meta, ROWS, NORM)
        render = lambda pdf, pages, d: {p: b"png" for p in pages}  # noqa: E731
        res = imp.run_import("student_ppo_2026", self.pdf, [1, 2], self.pdf, [12, 13], self.out, ext, render=render,
                             plan_profiles_path=fx.PATHS.plan_profiles)
        return res, ext

    def load(self, name):
        return json.loads((self.out / name).read_text())

    def test_metadata_and_benefits_are_separate_outputs_with_pages(self):
        res, ext = self.run_import()
        self.assertEqual([c[0] for c in ext.calls][:3], ["metadata", "rows", "rows"])
        meta, rules = self.load("policy_metadata.json"), self.load("benefit_rules.json")
        validate(meta, "policy_metadata")
        self.assertEqual(meta["issuer"], {"value": "Anthem Blue Cross", "source_page": 1})
        self.assertEqual(meta["plan"]["product_ids"][0]["source_page"], 2)
        self.assertEqual(meta["sources"]["pages"], [1, 2])
        self.assertEqual(len(meta["sources"]["sha256"]), 64)
        self.assertEqual(meta["networks"], [{"name_in_pdf": "Prudent Buyer PPO", "policy_network_category": "in-network",
                                             "source_page": 2}])
        self.assertIsNone(meta["coverage"]["effective_end"])
        self.assertEqual((meta["extraction_status"], meta["review_status"]), ("partial", "pending_review"))
        self.assertTrue(any("Ghost Network" in p for p in meta["extraction"]["problems"]))
        # Schedule of Benefits: one rule per network column, with page, file and review state; no rates
        self.assertEqual([(r["service"], r["network"], r["source_page"]) for r in rules],
                         [("physician office visits", "in-network", 12), ("physician office visits", "out-of-network", 12),
                          ("laboratory procedures", "in-network", 13)])
        for r in rules:
            validate(r, "benefit_rule")
            self.assertEqual((r["source_file"], r["review_status"], r["policy_plan_id"]),
                             (str(self.pdf), "pending_review", "student_ppo_2026"))
            self.assertNotIn("allowed_amount", r)
        self.assertEqual(rules[0]["rule_key"], "student_ppo_2026:p12:r1:in-network")
        self.assertTrue(rules[2]["needs_review"])  # percentages that do not sum to 100 are forced to review
        self.assertEqual(len(res["rule_problems"]), 1)

    def test_mrf_mapping_is_pending_never_guessed(self):
        self.run_import()
        mapping = self.load("policy_mrf_map.pending.json")
        validate(mapping, "policy_mrf_map")
        self.assertEqual([e["policy_network_category"] for e in mapping["entries"]], ["in-network", "out-of-network"])
        for e in mapping["entries"]:
            self.assertEqual((e["mrf_plan_key"], e["mrf_network"], e["review_status"]), (None, None, "pending_review"))
            self.assertIn("Student Health PPO", e["question_for_human"])
        self.assertIn("Prudent Buyer PPO (PDF p.2)", mapping["entries"][0]["evidence"])
        cands = {c["mrf_plan_key"] for c in mapping["entries"][0]["candidates"]}
        self.assertIn("anthem_ca_prudent_buyer_ppo", cands)  # suggestions only; never selected automatically
        self.assertNotIn("cigna_national_ppo", cands)

    def test_missing_issuer_marks_extraction_failed(self):
        self.run_import(dict(META, issuer=None))
        self.assertEqual(self.load("policy_metadata.json")["extraction_status"], "failed")

    def test_normalizer_changing_ids_aborts(self):
        ext = FakeExtractor(META, ROWS, NORM)
        ext.normalize = lambda entries: [dict(NORM[1], entry_id=99)]
        with self.assertRaises(RuntimeError):
            imp.run_import("p", self.pdf, [1], self.pdf, [12], self.out, ext, render=lambda *a: {12: b"x", 1: b"x"},
                           plan_profiles_path=fx.PATHS.plan_profiles)

    def test_benefit_rule_schema_rejects_embedded_rates(self):
        rule = {"schema_version": "benefit_rule.v2", "rule_key": "k", "policy_plan_id": "p", "plan": "p", "service": "s",
                "setting": None, "network": "in-network", "copay_usd": None, "plan_pays_percentage": 80,
                "patient_pays_percentage": 20, "deductible_applies": False, "prior_authorization": "not_stated",
                "coverage_status": "covered", "source_text": "t", "source_file": "f", "source_page": 1,
                "needs_review": False, "review_status": "pending_review"}
        validate(rule, "benefit_rule")
        with self.assertRaises(SchemaError):
            validate(dict(rule, allowed_amount=100), "benefit_rule")

    def test_page_ranges(self):
        self.assertEqual(imp.parse_pages("1-3, 5"), [1, 2, 3, 5])
        for bad in ("0", "3-1", ""):
            with self.assertRaises(ValueError):
                imp.parse_pages(bad)


if __name__ == "__main__":
    unittest.main()

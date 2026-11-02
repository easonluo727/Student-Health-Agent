"""Bill photos: reading (fake model clients) and checking against a small rate store built here. No LLM, no network."""

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
sys.path.insert(0, str(ROOT / "scripts" / "mrf"))
import mrf_rate_store  # noqa: E402
from bill_audit import audit  # noqa: E402
from bill_reader import normalize, parse_reply, read_bill  # noqa: E402
from harness.skills.bills.skill import run_audit, run_draft  # noqa: E402
from plan_db import PlanDB  # noqa: E402
from provider_directory import ProviderDirectory  # noqa: E402

PLAN = "healthnet_gold80_hmo_2026"  # one in-network tier, confirmed network healthnet_ambetter_ca
NPI = "2000000001"
UCSHIP = "anthem_ppo_ucship_ucla_2026_27"  # its "Network" tier MRI is 20% coinsurance after the deductible
RATES = {"healthnet_ambetter_ca": [(NPI, "73721", "professional", "11", "", 400.0),
                                   ("2000000002", "87804", "professional", "20", "", 9.0)],
         "anthem_ca_prudent_buyer_ppo": [(NPI, "73721", "professional", "11", "", 900.0)]}


def build_rate_store(path):
    con = mrf_rate_store.connect(path)
    for source_id, (plan_key, rows) in enumerate(RATES.items(), 1):
        con.execute("INSERT INTO mrf_sources (source_id, source_url, insurer, plan_key, file_date, status) "
                    "VALUES (?, ?, 'Test', ?, '2026-10-01', 'complete')", (source_id, f"https://test/{plan_key}", plan_key))
        for npi, code, cls, pos, mod, rate in rows:
            con.execute("INSERT INTO negotiated_rates (source_id, billing_code, provider_group_id, negotiated_rate, "
                        "negotiated_type, expiration_date, billing_class, service_codes, modifiers) "
                        "VALUES (?, ?, ?, ?, 'negotiated', '9999-12-31', ?, ?, ?)",
                        (source_id, code, npi, rate, cls, pos, mod))
            con.execute("INSERT OR IGNORE INTO provider_members (source_id, provider_group_id, npi, tier) "
                        "VALUES (?, ?, ?, 'B')", (source_id, npi, npi))
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()


BILL = {
    "provider_name": "WESTSIDE IMAGING", "provider_npi": NPI, "statement_date": "2026-10-20",
    "lines": [
        {"service_date": "2026-10-01", "code": "73721", "description": "MRI KNEE", "units": None, "billed": 1200.0},
        {"service_date": "2026-10-01", "code": "73721", "description": "MRI KNEE", "units": None, "billed": 1200.0},
        {"service_date": "2026-10-01", "code": "99284", "description": "ER VISIT", "units": None, "billed": 800.0},
        {"service_date": "2026-10-01", "code": "29540", "description": "STRAPPING", "units": None, "billed": 50.0},
        {"service_date": "2026-10-01", "code": "87804", "description": "FLU TEST", "units": None, "billed": 40.0},
    ],
    "total_billed": 3500.0,
}


class AuditOnFixtureRates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        rates = os.path.join(cls.tmp, "rates.sqlite")
        build_rate_store(rates)
        cls.db = PlanDB(rates_path=Path(rates))
        cls.directory = ProviderDirectory(providers_path=Path(cls.tmp) / "missing.sqlite")
        cls.result = audit(cls.db, cls.directory, BILL, PLAN, None, 0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def checks(self):
        return [line["check"] for line in self.result["lines"]]

    def test_each_line_gets_the_right_check(self):
        self.assertEqual(self.checks(), ["billed_above_your_share", "possible_duplicate", "not_reviewed", "no_data",
                                         "no_rate"])

    def test_share_is_the_calculators_at_this_providers_own_rate(self):
        line = self.result["lines"][0]
        expected = self.db.estimate(PLAN, "knee_mri", "in", None, remaining_deductible=0, doctor={"npi": NPI})
        self.assertEqual(line["negotiated_rate"], 400.0)
        self.assertEqual(line["you_owe"], expected["patient_cost"])
        self.assertAlmostEqual(line["possible_overcharge"], 1200.0 - expected["patient_cost"])

    def test_another_providers_rate_is_never_used(self):
        self.assertIsNone(self.result["lines"][4]["negotiated_rate"])  # 87804 has a rate, but only at another NPI

    def test_duplicate_and_total_mismatch_are_findings(self):
        kinds = [f["kind"] for f in self.result["findings"]]
        self.assertIn("possible_duplicate", kinds)
        self.assertIn("total_mismatch", kinds)
        self.assertEqual(self.result["totals"]["billed"], 3290.0)

    def test_unknown_deductible_asks_instead_of_guessing(self):
        result = audit(self.db, self.directory, BILL, UCSHIP, "Network", None)
        self.assertEqual((result["status"], result["missing_fields"]), ("needs_information", ["remaining_deductible"]))

    def test_deductible_is_used_up_in_line_order(self):
        result = audit(self.db, self.directory, BILL, UCSHIP, "Network", 0)
        expected = self.db.estimate(UCSHIP, "knee_mri", "in", "Network", remaining_deductible=0, doctor={"npi": NPI})
        self.assertEqual(result["lines"][0]["you_owe"], expected["patient_cost"])

    def test_a_plan_with_tiers_needs_one(self):
        self.assertEqual(audit(self.db, self.directory, BILL, UCSHIP, None, 0)["missing_fields"], ["network_tier"])

    def test_no_npi_means_no_rates(self):
        result = audit(self.db, self.directory, {**BILL, "provider_npi": None}, PLAN, None, 0)
        self.assertTrue(all(line["negotiated_rate"] is None for line in result["lines"]))

    def test_letter_quotes_only_audited_numbers(self):
        bills = {"file_1": {"bill": BILL, "name": "bill.jpg", "file_id": "file_1", "audit": None}}
        run_audit(self.db, self.directory, bills, {"bill_id": "file_1", "plan_id": PLAN, "network_tier": None,
                                                   "remaining_deductible": 0})
        draft = run_draft(self.db, bills, {"bill_id": "file_1"})["draft"]
        audited = {f"{n:,.2f}" for line in bills["file_1"]["audit"]["lines"]
                   for n in (line["billed"], line["negotiated_rate"], line["you_owe"], line["possible_overcharge"])
                   if n is not None}
        audited |= {f"{f['amount']:,.2f}" for f in bills["file_1"]["audit"]["findings"]}
        audited |= {f"{bills['file_1']['audit']['totals'][k]:,.2f}" for k in ("billed", "possible_overcharge")}
        audited.add(f"{BILL['total_billed']:,.2f}")
        self.assertTrue(set(re.findall(r"\$([\d,]+\.\d\d)", draft["body"])) <= audited)
        self.assertEqual(draft["to"], [])
        self.assertIn("[Your name]", draft["body"])

    def test_no_letter_before_an_audit(self):
        bills = {"b": {"bill": BILL, "name": "bill.jpg", "audit": None}}
        self.assertEqual(run_draft(self.db, bills, {"bill_id": "b"})["status"], "needs_audit")


class Reader(unittest.TestCase):
    def test_reply_after_glimmers_message_marker(self):
        text = ' to=user<|message|>{"provider_name": "A", "lines": []}'
        self.assertEqual(parse_reply(text)["provider_name"], "A")

    def test_normalize_amounts_dates_and_npi(self):
        bill = normalize({"provider_name": "  DR  A ", "provider_npi": "12345", "statement_date": "10/20/2026",
                          "lines": [{"service_date": "2026-10-01", "code": " 99213 ", "billed": "$1,234.50",
                                     "units": "2"}, "junk"], "total_billed": None})
        self.assertEqual(bill["provider_name"], "DR A")
        self.assertIsNone(bill["provider_npi"])
        self.assertIsNone(bill["statement_date"])
        self.assertEqual(bill["lines"], [{"service_date": "2026-10-01", "code": "99213", "description": None,
                                          "units": 2.0, "billed": 1234.5}])

    def test_no_lines_is_unreadable(self):
        self.assertIsNone(normalize({"lines": []}))

    def fake(self, reply=None, error=None):
        def create(**kwargs):
            if error:
                raise error
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def test_falls_back_to_the_next_model(self):
        photo = Path(tempfile.mkdtemp()) / "bill.png"
        photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        good = '{"provider_name": "A", "provider_npi": "1234567890", "lines": [{"code": "99213", "billed": 100}]}'
        result = read_bill(photo, "image/png", readers=[(self.fake(error=TimeoutError()), "glimmer"),
                                                        (self.fake(good), "main")])
        self.assertEqual((result["status"], result["model"]), ("read", "main"))
        self.assertEqual(result["bill"]["provider_npi"], "1234567890")

    def test_heic_is_not_sent_to_a_model(self):
        result = read_bill(Path("/nonexistent.heic"), "image/heic", readers=[])
        self.assertEqual(result["status"], "unreadable")


if __name__ == "__main__":
    unittest.main()

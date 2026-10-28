import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "bills"))

from score import paired, parse_prediction, same, score_bill, summarize  # noqa: E402

GOLD = {
    "provider_name": "BENJAMIN EASTER, MD",
    "provider_npi": None,
    "statement_date": "2026-10-23",
    "lines": [
        {"service_date": "2026-09-22", "code": "99213", "description": "OFFICE VISIT", "units": 1, "billed": 153.38},
        {"service_date": "2026-09-22", "code": "29515", "description": "SHORT LEG SPLINT", "units": 1, "billed": 166.69},
    ],
    "total_billed": 320.07,
}


class ParseTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(parse_prediction('{"a": 1}'), {"a": 1})

    def test_takes_final_message_after_reasoning(self):
        text = '<|start|>assistant to=self<|message|>think {"x": 0}<|eot|><|start|>assistant to=user<|message|>{"a": 2}<|eot|>'
        self.assertEqual(parse_prediction(text), {"a": 2})

    def test_code_fence_and_garbage(self):
        self.assertEqual(parse_prediction('Here:\n```json\n{"a": 3}\n```'), {"a": 3})
        self.assertIsNone(parse_prediction("no json here {broken"))
        self.assertIsNone(parse_prediction(""))


class SameTest(unittest.TestCase):
    def test_numbers_and_strings(self):
        self.assertTrue(same("153.38", 153.38))
        self.assertTrue(same(153.384, 153.38))
        self.assertFalse(same(153.39, 153.38))
        self.assertTrue(same("OFFICE  VISIT ", "OFFICE VISIT"))
        self.assertFalse(same("office visit", "OFFICE VISIT"))

    def test_null_only_matches_null(self):
        self.assertTrue(same(None, None))
        self.assertFalse(same("", None))
        self.assertFalse(same(None, 0))


class ScoreBillTest(unittest.TestCase):
    def test_perfect(self):
        s = score_bill(json.dumps(GOLD), GOLD)
        self.assertTrue(s["json_ok"] and s["line_count_ok"] and s["all_ok"])

    def test_missing_line_counts_as_wrong(self):
        pred = dict(GOLD, lines=GOLD["lines"][:1])
        s = score_bill(json.dumps(pred), GOLD)
        self.assertFalse(s["line_count_ok"])
        self.assertEqual(s["fields"]["line.code"], [True, False])
        self.assertFalse(s["all_ok"])

    def test_unparseable_scores_zero(self):
        s = score_bill("sorry", GOLD)
        self.assertFalse(s["json_ok"])
        self.assertEqual(s["fields"]["provider_name"], [False])
        # the answer's null NPI is not matched by a reply that never stated it
        self.assertEqual(s["fields"]["provider_npi"], [False])

    def test_missing_key_is_not_null(self):
        pred = {k: v for k, v in GOLD.items() if k != "provider_npi"}
        s = score_bill(json.dumps(pred), GOLD)
        self.assertEqual(s["fields"]["provider_npi"], [False])

    def test_summary_and_pairing(self):
        good = score_bill(json.dumps(GOLD), GOLD)
        bad = score_bill(json.dumps(dict(GOLD, total_billed=1)), GOLD)
        summary = summarize([good, bad], [1.0, 3.0])
        self.assertEqual(summary["all_ok"], 0.5)
        self.assertEqual(summary["total_billed"], 0.5)
        self.assertEqual(summary["seconds_per_bill"], 2.0)
        self.assertEqual(paired([good, good], [good, bad]), {"only_first": 1, "only_second": 0, "p_value": 1.0})


if __name__ == "__main__":
    unittest.main()

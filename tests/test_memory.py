"""The user's long-term memory: checked facts, the markdown the agent reads, and how it fills the questionnaire
(no LLM, no network)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

from accounts import AccountStore  # noqa: E402
from harness.hooks import Hooks  # noqa: E402
from memory import MemoryStore, check, question_answers, render  # noqa: E402


class CheckTest(unittest.TestCase):
    def test_valid_values_are_kept_in_their_type(self):
        self.assertEqual(check("zip", " 90024 "), "90024")
        self.assertEqual(check("age", 22.0), 22)
        self.assertEqual(check("plan_id", "p1", plan_ids={"p1"}), "p1")

    def test_invalid_values_are_refused(self):
        for field, value in [("zip", "9002"), ("zip", "abcde"), ("age", 17), ("age", 65), ("age", 22.5),
                             ("age", True), ("age", "22"), ("student_level", "graduate"), ("plan_id", "nope"),
                             ("remaining_deductible", 500)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                check(field, value, plan_ids={"p1"})


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "app.sqlite"
        self.user, _ = AccountStore(path).sign_in({"sub": "1", "email": "a@example.com", "name": "Alice"})
        self.store = MemoryStore(path)

    def test_remember_update_and_forget(self):
        uid = self.user["user_id"]
        self.store.remember(uid, {"zip": "90024", "age": 22}, "settings")
        self.store.remember(uid, {"zip": "94720"}, "form")
        facts = self.store.facts(uid)
        self.assertEqual((facts["zip"]["value"], facts["zip"]["source"]), ("94720", "form"))
        self.assertEqual(question_answers(facts), {"zip": "94720", "age": 22})
        self.store.remember(uid, {"age": None}, "settings")
        self.assertNotIn("age", self.store.facts(uid))

    def test_one_bad_value_saves_nothing(self):
        with self.assertRaises(ValueError):
            self.store.remember(self.user["user_id"], {"zip": "90024", "age": 300}, "settings")
        self.assertEqual(self.store.facts(self.user["user_id"]), {})

    def test_no_memory_renders_nothing(self):
        self.assertEqual(self.store.memory({**self.user, "name": None}, {}), "")


class RenderTest(unittest.TestCase):
    def test_three_sections_with_dates_and_sources(self):
        facts = {"zip": {"value": "90024", "source": "settings", "updated_at": 1793500000},
                 "age": {"value": 22, "source": "form", "updated_at": 1793500000},
                 "plan_id": {"value": "p1", "source": "settings", "updated_at": 1793500000}}
        reviews = [{"provider": "UCLA Medical Center", "statement_date": "2026-10-15", "plan_name": "UC SHIP",
                    "billed": 1200.0, "you_owe": 150.0, "possible_overcharge": 80.0, "findings": 1,
                    "appeal_drafted": 1, "status": "audited", "updated_at": 1793500000}]
        plans = [{"plan_id": "upload_ab", "plan_name": "Anthem Silver", "uploaded_at": 1793500000}]
        text = render({"name": "Alice"}, facts, reviews, plans, {"p1": "UC SHIP 2026-27"})
        for expected in ["## 1. How to use this memory", "## 2. About the user", "## 3. Past activity",
                         "- Name: Alice", "Current plan: UC SHIP 2026-27 (plan_id p1)", "ZIP: 90024 · set in Settings",
                         "Age: 22 · answered in a questionnaire", "should owe $150.00",
                         "appeal letter drafted", "Uploaded plan 2026-11-01: Anthem Silver (plan_id upload_ab)"]:
            self.assertIn(expected, text)


class UseMemoryTest(unittest.TestCase):
    def test_memory_fills_answers_but_this_chat_wins(self):
        hooks = Hooks()
        hooks.use_memory({"zip": "90024", "age": 22})
        self.assertEqual(hooks.answers, {"zip": "90024", "age": 22})
        hooks.on_form_answers([{"field": "zip", "kind": "text"}], {"zip": {"value": "94720", "unknown": False}})
        hooks.use_memory({"zip": "90024", "age": 23})  # the user changed their age in Settings
        self.assertEqual(hooks.answers, {"zip": "94720", "age": 23})
        hooks.use_memory({"zip": "90024"})  # and then removed it
        self.assertEqual(hooks.answers, {"zip": "94720"})
        # So the questionnaire doesn't ask for what memory already answered.
        hooks.use_memory({"zip": "90024", "age": 23})
        result = hooks.pre_tool("ask_questions", {"question_ids": ["zip", "age"], "prefill": []})
        self.assertEqual(result["status"], "already_answered")


if __name__ == "__main__":
    unittest.main()

"""The user's long-term memory: everything they said in their other chats and in Settings, and how the hooks count
it as stated (no LLM, no network)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

from accounts import AccountStore  # noqa: E402
from appdb import connect  # noqa: E402
from harness.hooks import Hooks  # noqa: E402
from memory import MemoryStore, check  # noqa: E402
from sessions import Session, SessionStore  # noqa: E402

DAY = 86400


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


class MemoryTest(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "app.sqlite"
        accounts = AccountStore(path)
        self.user, _ = accounts.sign_in({"sub": "1", "email": "a@example.com", "name": "Alice"})
        self.bob, _ = accounts.sign_in({"sub": "2", "email": "b@example.com"})
        self.sessions = SessionStore(path)
        self.store = MemoryStore(path)
        self.path = path

    def chat(self, session_id, user, inputs, days_ago):
        self.sessions.save(session_id, Session(), user["user_id"], [{"event": "input", "data": d} for d in inputs])
        with connect(self.path) as con:
            con.execute("UPDATE session_events SET created_at=created_at-? WHERE session_id=?",
                        (days_ago * DAY, session_id))
        con.close()

    def test_settings_remember_and_forget(self):
        uid = self.user["user_id"]
        self.store.remember(uid, {"zip": "90024", "age": 22})
        self.assertEqual(self.store.facts(uid)["zip"]["value"], "90024")
        self.store.remember(uid, {"age": None})
        self.assertNotIn("age", self.store.facts(uid))
        with self.assertRaises(ValueError):  # one bad value saves nothing
            self.store.remember(uid, {"zip": "94720", "age": 300})
        self.assertEqual(self.store.facts(uid)["zip"]["value"], "90024")

    def test_nothing_said_renders_nothing(self):
        recall = self.store.recall({**self.user, "name": None}, "now", {})
        self.assertEqual((recall.texts, recall.answers, recall.prompt), ([], {}, ""))

    def test_everything_said_elsewhere_is_recalled_newest_winning(self):
        self.chat("old", self.user, [
            {"type": "message", "text": "My remaining deductible is $400 and I see a therapist."},
            {"type": "form_response", "request_id": "f1",
             "answers": {"age": {"value": 22, "unknown": False}, "zip": {"value": "90024", "unknown": False},
                         "monthly_budget": {"value": None, "unknown": True}}},
        ], days_ago=3)
        self.chat("now", self.user, [{"type": "message", "text": "This chat's own words are in its history."}], 0)
        self.chat("bobs", self.bob, [{"type": "message", "text": "Bob's deductible is $999."}], 1)
        self.store.remember(self.user["user_id"], {"age": 23, "plan_id": "p1"}, plan_ids={"p1"})  # later, Settings

        recall = self.store.recall(self.user, "now", {"p1": "UC SHIP 2026-27"})
        self.assertEqual(recall.answers, {"age": 23, "zip": "90024"})  # Settings is newer; "don't know" isn't kept
        self.assertIn("My remaining deductible is $400 and I see a therapist.", recall.texts)
        self.assertFalse(any("own words" in t or "Bob" in t for t in recall.texts))
        for expected in ["Their name: Alice", "· chat: My remaining deductible is $400",
                         "· questionnaire: How old are you? 22", "· Settings: My age is 23",
                         "My current plan is UC SHIP 2026-27 (plan_id p1)"]:
            self.assertIn(expected, recall.prompt)

        self.sessions.delete("old", self.user["user_id"])  # deleting a chat forgets what was said in it
        recall = self.store.recall(self.user, "now", {})
        self.assertEqual(recall.answers, {"age": 23})
        self.assertFalse(any("$400" in t for t in recall.texts))

    def test_long_memory_keeps_the_newest(self):
        self.chat("old", self.user, [{"type": "message", "text": f"message {i} " + "x" * 500} for i in range(40)], 1)
        prompt = self.store.recall(self.user, "now", {}).prompt
        self.assertIn("message 39 ", prompt)
        self.assertNotIn("message 0 ", prompt)


class HooksRememberTest(unittest.TestCase):
    def setUp(self):
        self.hooks = Hooks()
        self.hooks.remember(["My remaining deductible is $400.", "What's your ZIP code? 90024"], {"zip": "90024"})

    def test_an_amount_said_in_an_earlier_chat_counts_as_stated(self):
        self.assertIsNone(self.hooks.pre_tool("estimate_cost", {"remaining_deductible": 400, "zip_code": None}))
        blocked = self.hooks.pre_tool("estimate_cost", {"remaining_deductible": 250, "zip_code": None})
        self.assertEqual(blocked["status"], "ask_user")
        self.assertIsNone(self.hooks.check_reply("You told me your remaining deductible is $400."))

    def test_remembered_answers_are_not_asked_again_and_this_chat_wins(self):
        result = self.hooks.pre_tool("ask_questions", {"question_ids": ["zip"], "prefill": []})
        self.assertEqual(result["status"], "already_answered")
        self.hooks.on_form_answers([{"field": "zip", "kind": "text"}], {"zip": {"value": "94720", "unknown": False}})
        self.assertEqual(self.hooks.known["zip"], "94720")
        self.assertEqual(self.hooks.answers, {"zip": "94720"})  # only this chat's answers are saved with it

    def test_prefill_may_use_what_was_said_before(self):
        args = {"question_ids": ["remaining_deductible"], "prefill": [
            {"question_id": "remaining_deductible", "value": "400"}]}
        self.assertIsNone(self.hooks.pre_tool("ask_questions", args))
        self.assertEqual(args["checked_prefill"], {"remaining_deductible": 400})


if __name__ == "__main__":
    unittest.main()

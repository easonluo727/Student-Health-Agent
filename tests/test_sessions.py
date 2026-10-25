"""Conversation state survives a save and load through SQLite (no LLM, no network)."""

import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

from sessions import Session, SessionStore, upload_folder  # noqa: E402


class FakeOutputItem:
    """Stands in for an SDK output item (has model_dump, like openai's pydantic models)."""

    def model_dump(self, mode, exclude_none):
        return {"type": "function_call", "call_id": "c1", "name": "list_plans", "arguments": "{}"}


class SessionStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = SessionStore(self.tmp / "sessions.sqlite", ttl=3600, upload_dir=self.tmp / "uploads")

    def test_round_trip_keeps_history_forms_answers_and_guard_numbers(self):
        s = Session()
        s.harness.hooks.on_user_message("I have $500 left on my deductible")
        s.harness.hooks.answers["age"] = 22
        s.harness.hooks.guard.post_tool("compare_plans", {}, {"premium": {"annual": 3886.02}}, None)
        s.history = [{"role": "user", "content": "hi"}, FakeOutputItem()]
        s.open_forms = {"form_1": [{"field": "zip", "kind": "text"}]}
        s.uploads = {"file_1": {"path": self.tmp / "a.pdf", "name": "a.pdf"}}
        s.context = {"mode": "review", "plan_ids": ["p"]}
        self.store.save("abc", s)

        t = self.store.get("abc")
        self.assertEqual(t.history[1]["name"], "list_plans")
        self.assertEqual(t.open_forms, s.open_forms)
        self.assertEqual(t.uploads["file_1"]["path"], self.tmp / "a.pdf")
        self.assertEqual((t.context, t.harness.hooks.answers), (s.context, {"age": 22}))
        self.assertIn(Decimal("500"), t.harness.hooks.guard._user_numbers)
        self.assertIn(Decimal("3886.02"), t.harness.hooks.guard._tool_numbers)
        # The restored guard still accepts a reply quoting a tool amount and rejects an invented one.
        self.assertIsNone(t.harness.hooks.check_reply("UC SHIP costs $3,886.02 a year."))
        self.assertIsNotNone(t.harness.hooks.check_reply("UC SHIP costs $4,100 a year."))

    def test_unknown_or_expired_sessions_start_fresh(self):
        self.assertEqual(self.store.get("nobody").history, [])
        s = Session()
        s.history = [{"role": "user", "content": "hi"}]
        self.store.save("old", s)
        with self.store._connect() as con:
            con.execute("UPDATE sessions SET updated_at=?", (time.time() - 7200,))
        con.close()
        self.assertEqual(self.store.get("old").history, [])

    def test_purge_deletes_expired_sessions_and_their_uploads(self):
        folder = upload_folder(self.tmp / "uploads", "old")
        folder.mkdir(parents=True)
        (folder / "plan.pdf").write_bytes(b"%PDF")
        self.store.save("old", Session())
        self.store.save("new", Session())
        with self.store._connect() as con:
            con.execute("UPDATE sessions SET updated_at=? WHERE session_id='old'", (time.time() - 7200,))
        con.close()
        self.assertEqual(self.store.purge(), 1)
        self.assertFalse(folder.exists())
        self.assertEqual(self.store.get("new").history, [])


if __name__ == "__main__":
    unittest.main()

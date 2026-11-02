"""Conversations in the app database: state round trip, who may use them, transcripts, uploads and deletion
(no LLM, no network)."""

import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

from accounts import AccountStore  # noqa: E402
from appdb import MIGRATIONS, connect  # noqa: E402
from sessions import (NotYours, Session, SessionStore, appeal_drafted, bill_review, compact, file_kind,  # noqa: E402
                      upload_folder)


class FakeOutputItem:
    """Stands in for an SDK output item (has model_dump, like openai's pydantic models)."""

    def model_dump(self, mode, exclude_none):
        return {"type": "function_call", "call_id": "c1", "name": "list_plans", "arguments": "{}"}


class SessionStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = SessionStore(self.tmp / "app.sqlite", ttl=3600, upload_dir=self.tmp / "uploads")
        accounts = AccountStore(self.tmp / "app.sqlite")
        self.alice = accounts.sign_in({"sub": "1", "email": "alice@example.com"})[0]["user_id"]
        self.bob = accounts.sign_in({"sub": "2", "email": "bob@example.com"})[0]["user_id"]
        self.accounts = accounts

    def upload(self, session_id, user_id=None, file_id="file_1"):
        folder = upload_folder(self.tmp / "uploads", session_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{file_id}.pdf"
        path.write_bytes(b"%PDF")
        self.store.add_upload(session_id, user_id, file_id, path, "plan.pdf", "application/pdf", 4, "ab")
        return path

    def age(self, session_id, seconds=7200):
        with connect(self.store.path) as con:
            con.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (time.time() - seconds, session_id))
        con.close()

    def test_round_trip_keeps_history_forms_answers_and_guard_numbers(self):
        s = Session()
        s.harness.hooks.on_user_message("I have $500 left on my deductible")
        s.harness.hooks.answers["age"] = 22
        s.harness.hooks.guard.post_tool("compare_plans", {}, {"premium": {"annual": 3886.02}}, None)
        s.history = [{"role": "user", "content": "hi"}, FakeOutputItem()]
        s.open_forms = {"form_1": [{"field": "zip", "kind": "text"}]}
        s.context = {"mode": "review", "plan_ids": ["p"]}
        self.store.save("abc", s)
        path = self.upload("abc")

        t = self.store.get("abc")
        self.assertEqual(t.history[1]["name"], "list_plans")
        self.assertEqual(t.open_forms, s.open_forms)
        self.assertEqual(t.uploads["file_1"], {"path": path, "name": "plan.pdf", "content_type": "application/pdf",
                                               "sha256": "ab"})
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
        self.store.save("old", s, events=[{"event": "input", "data": {"type": "message", "text": "hi"}}])
        self.age("old")
        self.assertEqual(self.store.get("old").history, [])
        # The expired one is gone with its transcript, so a new chat under the same id starts empty.
        self.store.save("old", Session(), self.alice)
        self.assertEqual(self.store.transcript("old", self.alice), [])

    def test_purge_deletes_expired_anonymous_sessions_and_their_uploads(self):
        path = self.upload("old")
        self.store.save("new", Session())
        self.store.save("mine", Session(), self.alice)
        self.age("old")
        self.age("mine", seconds=10 * 24 * 3600)
        self.assertEqual(self.store.purge(), 1)
        self.assertFalse(path.parent.exists())
        self.assertEqual(self.store.get("new").history, [])
        self.assertEqual(len(self.store.sessions_of(self.alice)), 1)  # a signed-in user's chat never expires
        self.assertEqual(self.store.get("mine", self.alice).history, [])

    def test_an_owned_session_is_only_the_owners(self):
        s = Session()
        s.history = [{"role": "user", "content": "my deductible"}]
        self.store.save("abc", s, self.alice)
        self.assertEqual(self.store.get("abc", self.alice).history, s.history)
        for intruder in (self.bob, None):  # another user, or anyone signed out
            with self.assertRaises(NotYours):
                self.store.get("abc", intruder)
            with self.assertRaises(NotYours):
                self.store.save("abc", Session(), intruder)
            with self.assertRaises(NotYours):
                self.upload("abc", intruder, file_id="file_x")
            self.assertIsNone(self.store.transcript("abc", intruder or "nobody"))
            self.assertFalse(self.store.delete("abc", intruder or "nobody"))
        # Nothing the intruders tried changed it.
        self.assertEqual(self.store.get("abc", self.alice).history, s.history)
        self.assertEqual(self.store.uploads_of(self.alice), [])
        self.assertIsNone(self.store.upload_of("file_x", self.bob))

    def test_signing_in_claims_the_anonymous_session_and_its_uploads(self):
        self.upload("abc")
        self.store.save("abc", Session(), events=[{"event": "input", "data": {"type": "message", "text": "  hi \n there"}}])
        self.assertEqual(self.store.sessions_of(self.alice), [])
        self.store.save("abc", Session(), self.alice, events=[{"event": "input", "data": {"type": "message", "text": "x"}}])
        [mine] = self.store.sessions_of(self.alice)
        self.assertEqual((mine["session_id"], mine["title"], mine["uploads"]), ("abc", "hi there", 1))
        self.assertEqual([u["file_id"] for u in self.store.uploads_of(self.alice)], ["file_1"])
        with self.assertRaises(NotYours):  # and now it is hers alone
            self.store.get("abc")

    def test_transcript_keeps_inputs_and_events_in_order_with_text_joined(self):
        turn1 = [{"event": "input", "data": {"type": "message", "text": "hi"}},
                 {"event": "message_start", "data": {"message_id": "m1"}},
                 {"event": "message_delta", "data": {"message_id": "m1", "text": "Hel"}},
                 {"event": "message_delta", "data": {"message_id": "m1", "text": "lo"}},
                 {"event": "message_end", "data": {"message_id": "m1"}},
                 {"event": "done", "data": {}}]
        turn2 = [{"event": "input", "data": {"type": "form_response", "request_id": "f1", "answers": {}}},
                 {"event": "done", "data": {}}]
        self.store.save("abc", Session(), self.alice, turn1)
        self.store.save("abc", Session(), self.alice, turn2)
        events = self.store.transcript("abc", self.alice)
        self.assertEqual([e["event"] for e in events],
                         ["input", "message_start", "message_delta", "message_end", "done", "input", "done"])
        self.assertEqual(events[2]["data"]["text"], "Hello")
        self.assertEqual(compact(turn1)[2], events[2])

    def test_deleting_a_session_or_a_user_removes_rows_and_files(self):
        one = self.upload("one", self.alice)
        two = self.upload("two", self.alice, file_id="file_2")
        bobs = self.upload("bobs", self.bob, file_id="file_3")
        self.store.save("one", Session(), self.alice, [{"event": "done", "data": {}}])
        self.assertTrue(self.store.delete("one", self.alice))
        self.assertFalse(one.exists())
        self.assertEqual([s["session_id"] for s in self.store.sessions_of(self.alice)], ["two"])
        with connect(self.store.path) as con:  # the transcript and upload rows went with it
            self.assertEqual(con.execute("SELECT COUNT(*) FROM session_events").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM uploads WHERE session_id='one'").fetchone()[0], 0)
        con.close()

        self.assertEqual(self.store.delete_user(self.alice), 1)
        self.accounts.delete_user(self.alice)
        self.assertFalse(two.exists())
        self.assertTrue(bobs.exists())  # nobody else's
        with connect(self.store.path) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM logins WHERE user_id=?", (self.alice,)).fetchone()[0], 0)
        con.close()


    def test_tool_records_tag_files_and_keep_bill_reviews(self):
        self.upload("abc", self.alice, file_id="file_plan")
        self.upload("abc", self.alice, file_id="file_bill")
        audit = {"status": "audited", "plan_id": "p1", "plan_name": "UC SHIP", "statement_date": "2026-10-15",
                 "provider": {"name_on_record": "UCLA Medical Center", "name_on_bill": "UCLA"},
                 "totals": {"billed": 1200.0, "you_owe_on_checked_lines": 150.0, "possible_overcharge": 80.0},
                 "findings": [{"kind": "billed_above_your_share"}]}
        s = Session()
        s.records = [file_kind("file_plan", "plan", "p1", "UC SHIP"), file_kind("file_bill", "bill"),
                     bill_review("file_bill", "file_bill", audit)]
        self.store.save("abc", s, self.alice)
        s.records = [appeal_drafted("file_bill")]
        self.store.save("abc", s, self.alice)
        files = {u["file_id"]: u for u in self.store.uploads_of(self.alice)}
        self.assertEqual((files["file_plan"]["kind"], files["file_plan"]["plan_name"]), ("plan", "UC SHIP"))
        [review] = files["file_bill"]["bill_reviews"]
        self.assertEqual((review["provider"], review["you_owe"], review["findings"], review["appeal_drafted"]),
                         ("UCLA Medical Center", 150.0, 1, True))
        # Reviewing the same bill again replaces the review (and the appeal belongs to the old one).
        s.records = [bill_review("file_bill", "file_bill", {**audit, "findings": []})]
        self.store.save("abc", s, self.alice)
        [review] = {u["file_id"]: u for u in self.store.uploads_of(self.alice)}["file_bill"]["bill_reviews"]
        self.assertEqual((review["findings"], review["appeal_drafted"]), (0, False))
        # A record can't tag someone else's file.
        self.upload("bobs", self.bob, file_id="file_bob")
        s.records = [file_kind("file_bob", "bill")]
        self.store.save("abc", s, self.alice)
        self.assertIsNone(self.store.uploads_of(self.bob)[0]["kind"])


class MigrationTest(unittest.TestCase):
    def test_a_version_1_database_upgrades_and_keeps_its_data(self):
        path = Path(tempfile.mkdtemp()) / "app.sqlite"
        con = connect(path)
        con.executescript(f"BEGIN; {MIGRATIONS[0]}; PRAGMA user_version = 1; COMMIT;")
        con.execute("INSERT INTO users VALUES ('user_1', 'sub1', 'a@example.com', 'A', NULL, 1, 1)")
        con.execute("INSERT INTO sessions VALUES ('s1', 'user_1', 'hi', '{}', 1, 1)")
        con.execute("INSERT INTO uploads VALUES ('f1', 's1', 'bill.png', 'image/png', 4, 'ab', '/x', 1)")
        con.commit()
        con.close()
        store = SessionStore(path)
        [upload] = store.uploads_of("user_1")
        self.assertEqual((upload["name"], upload["kind"], upload["bill_reviews"]), ("bill.png", None, []))


    def test_accounts_move_from_users_sqlite_once(self):
        tmp = Path(tempfile.mkdtemp())
        old = sqlite3.connect(tmp / "users.sqlite")  # the 2026-11-01 accounts file
        old.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, google_sub TEXT NOT NULL UNIQUE, email TEXT NOT "
                    "NULL, name TEXT, picture TEXT, created_at REAL NOT NULL, last_login_at REAL NOT NULL)")
        old.execute("CREATE TABLE logins (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT "
                    "NULL, expires_at REAL NOT NULL)")
        old.execute("INSERT INTO users VALUES ('user_1', 'sub1', 'a@example.com', 'A', NULL, 1, 1)")
        old.execute("INSERT INTO logins VALUES ('hash1', 'user_1', 1, ?)", (time.time() + 3600,))
        old.commit()
        old.close()
        store = SessionStore(tmp / "app.sqlite")
        AccountStore(tmp / "app.sqlite")  # a second start doesn't import again or fail
        with connect(store.path) as con:
            self.assertEqual(con.execute("SELECT email FROM users").fetchall(), [("a@example.com",)])
            self.assertEqual(con.execute("SELECT user_id FROM logins").fetchall(), [("user_1",)])
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
        con.close()


if __name__ == "__main__":
    unittest.main()

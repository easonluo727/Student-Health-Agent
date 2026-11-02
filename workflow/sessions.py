"""Conversations in the app database (appdb.py), so a redeploy doesn't lose them and a signed-in user can reopen them.

  SESSION_TTL   seconds an anonymous conversation is kept after its last turn (default one day); its uploads go with it

A session is loaded at the start of every request and saved at the end. Its state is plain data: the model history
(as dicts), open forms, the home-screen mode, the hooks' state (questionnaire answers, user text, and the guard's
numbers), the bills read from photos and the tool results the agent may show as pages. The harness itself (prompt, skills, tools) is rebuilt from code each time.
Uploads are rows of their own, so an upload during a running turn isn't lost when the turn saves.

Who may use a conversation: an anonymous one, anyone who has its id (the frontend's random uuid); a signed-in user
who uses it claims it. An owned one, only its owner; to everyone else it doesn't exist (NotYours). Owned
conversations never expire; the owner deletes them.

Each save also appends what the user saw that turn (their input and the SSE events), so a chat can be shown again,
and writes the turn's records: what an uploaded file turned out to be (a plan or a bill) and each bill review.
"""

import json
import os
import shutil
import time
from decimal import Decimal
from pathlib import Path

from appdb import APP_DB, migrate, transaction
from harness import Harness

SESSION_TTL = int(os.environ.get("SESSION_TTL", 24 * 3600))
TITLE_CHARS = 80


class NotYours(LookupError):
    """The conversation belongs to another user (or the caller is signed out). Report it as not found."""


class Session:
    def __init__(self):
        self.harness = Harness()
        self.history = []
        self.open_forms = {}  # request_id -> the form's question dicts
        self.uploads = self.harness.files  # file_id -> {"path", "name", "content_type", "sha256"}; tools read it
        self.attachment_notes = []
        self.context = None  # the last home-screen mode applied
        # Per request, not saved in the state: the user's memory for the prompt (memory.py), and facts from this
        # turn's tools for the database (see SessionStore._write).
        self.memory = ""
        self.records = []
        self.turn_results = []  # result ids from this request's tools, for the page check in server.agent_loop
        self.turn_shown = []  # result ids shown as pages in this request


def file_kind(file_id, kind, plan_id=None, plan_name=None):
    """Record: the agent found an upload to be a plan (with the plan it is) or a bill."""
    return {"type": "file_kind", "file_id": file_id, "kind": kind, "plan_id": plan_id, "plan_name": plan_name}


def bill_review(bill_id, file_id, result):
    """Record: an audit_bill result, summarized for the user's file list and memory."""
    provider, totals = result["provider"], result["totals"]
    return {"type": "bill_review", "bill_id": bill_id, "file_id": file_id, "status": result["status"],
            "plan_id": result["plan_id"], "plan_name": result["plan_name"],
            "provider": provider.get("name_on_record") or provider.get("name_on_bill"),
            "statement_date": result.get("statement_date"), "billed": totals["billed"],
            "you_owe": totals["you_owe_on_checked_lines"], "possible_overcharge": totals["possible_overcharge"],
            "findings": len(result["findings"])}


def appeal_drafted(bill_id):
    return {"type": "appeal", "bill_id": bill_id}


def _plain(item):
    """A history item as JSON data (the model's output items are SDK objects)."""
    return item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else item


def dump(session):
    hooks, guard = session.harness.hooks, session.harness.hooks.guard
    return {
        "history": [_plain(item) for item in session.history],
        "open_forms": session.open_forms,
        "attachment_notes": session.attachment_notes,
        "context": session.context,
        "bills": session.harness.bills,
        "results": session.harness.results,
        "hooks": {"user_texts": hooks.user_texts, "answers": hooks.answers,
                  "from_memory": sorted(hooks.from_memory)},
        "guard": {"user_numbers": sorted(map(str, guard._user_numbers)),
                  "tool_numbers": sorted(map(str, guard._tool_numbers)),
                  "confirmed": [list(k) for k in guard._confirmed], "missing_fields": guard._missing_fields},
    }


def load(state):
    session = Session()
    if not state:  # the row was made by an upload before the first turn
        return session
    session.history = state["history"]
    session.open_forms = state["open_forms"]
    session.attachment_notes = state["attachment_notes"]
    session.context = state["context"]
    session.harness.bills.update(state.get("bills") or {})  # the bills skill holds this same dict
    session.harness.results.update(state.get("results") or {})  # and the pages skill this one
    hooks, guard = session.harness.hooks, session.harness.hooks.guard
    hooks.user_texts, hooks.answers = state["hooks"]["user_texts"], state["hooks"]["answers"]
    hooks.from_memory = set(state["hooks"].get("from_memory") or [])
    g = state["guard"]
    guard._user_numbers = {Decimal(n) for n in g["user_numbers"]}
    guard._tool_numbers = {Decimal(n) for n in g["tool_numbers"]}
    guard._confirmed = {tuple(k) for k in g["confirmed"]}
    guard._missing_fields = g["missing_fields"]
    return session


def compact(events):
    """The turn's events with each message's text deltas joined into one, which is how they are stored."""
    out = []
    for e in events:
        last = out[-1] if out else None
        if (e["event"] == "message_delta" and last and last["event"] == "message_delta"
                and last["data"]["message_id"] == e["data"]["message_id"]):
            out[-1] = {**last, "data": {**last["data"], "text": last["data"]["text"] + e["data"]["text"]}}
        else:
            out.append(e)
    return out


def title_of(events):
    """The conversation's title: the user's first message, or the first file they attached."""
    for e in events:
        if e["event"] == "input" and e["data"].get("type") == "message":
            text = " ".join((e["data"].get("text") or "").split())
            names = [a.get("name") for a in e["data"].get("attachments") or [] if a.get("name")]
            if text or names:
                return (text or names[0])[:TITLE_CHARS]
    return None


# Claim an anonymous conversation for the user, keep an owned one with its owner, refuse anyone else: when the WHERE
# fails the upsert changes no row, and the caller raises NotYours.
_OWNER_MAY_WRITE = "WHERE sessions.user_id IS NULL OR sessions.user_id IS excluded.user_id"


class SessionStore:
    def __init__(self, path: Path = APP_DB, ttl: int = SESSION_TTL, upload_dir: Path | None = None):
        migrate(path)
        self.path, self.ttl, self.upload_dir = path, ttl, upload_dir

    def get(self, session_id: str, user_id: str | None = None) -> Session:
        """The saved session with its uploads; a new one when there is none or an anonymous one has expired.
        Raises NotYours when it belongs to someone other than user_id."""
        with transaction(self.path) as con:
            row = con.execute("SELECT user_id, state, updated_at FROM sessions WHERE session_id=?",
                              (session_id,)).fetchone()
            uploads = con.execute("SELECT file_id, path, name, content_type, sha256 FROM uploads WHERE session_id=? "
                                  "ORDER BY created_at", (session_id,)).fetchall()
        if row is None:
            return Session()
        owner, state, updated_at = row
        if owner is not None and owner != user_id:
            raise NotYours(session_id)
        if owner is None and time.time() - updated_at > self.ttl:
            self._delete([session_id])  # so its old transcript and files don't come back with the new chat
            return Session()
        session = load(json.loads(state))
        session.uploads.update({fid: {"path": Path(path), "name": name, "content_type": ctype, "sha256": sha}
                                for fid, path, name, ctype, sha in uploads})
        return session

    def save(self, session_id: str, session: Session, user_id: str | None = None, events=()) -> None:
        """Save the state and append the turn's events; a signed-in user claims an anonymous conversation."""
        now = time.time()
        events = compact(list(events))
        state = json.dumps(dump(session), ensure_ascii=False, allow_nan=False)
        with transaction(self.path) as con:
            changed = con.execute(
                "INSERT INTO sessions (session_id, user_id, title, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "user_id=COALESCE(sessions.user_id, excluded.user_id), title=COALESCE(sessions.title, excluded.title), "
                "state=excluded.state, updated_at=excluded.updated_at " + _OWNER_MAY_WRITE,
                (session_id, user_id, title_of(events), state, now, now)).rowcount
            if not changed:
                raise NotYours(session_id)
            seq = con.execute("SELECT COALESCE(MAX(seq), 0) FROM session_events WHERE session_id=?",
                              (session_id,)).fetchone()[0]
            con.executemany("INSERT INTO session_events (session_id, seq, event, data, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            [(session_id, seq + i, e["event"], json.dumps(e["data"], ensure_ascii=False), now)
                             for i, e in enumerate(events, start=1)])
            self._write(con, session_id, session.records, now)

    @staticmethod
    def _write(con, session_id, records, now):
        """The turn's records (file_kind, bill_review, appeal), in the save's transaction."""
        for r in records:
            if r["type"] == "file_kind":
                con.execute("UPDATE uploads SET kind=?, plan_id=?, plan_name=? WHERE file_id=? AND session_id=?",
                            (r["kind"], r["plan_id"], r["plan_name"], r["file_id"], session_id))
            elif r["type"] == "bill_review":
                file_id = r["file_id"] if con.execute("SELECT 1 FROM uploads WHERE file_id=? AND session_id=?",
                                                      (r["file_id"], session_id)).fetchone() else None
                con.execute(
                    "INSERT INTO bill_audits (session_id, bill_id, file_id, status, plan_id, plan_name, provider, "
                    "statement_date, billed, you_owe, possible_overcharge, findings, created_at, updated_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, bill_id) DO UPDATE SET "
                    "file_id=excluded.file_id, status=excluded.status, plan_id=excluded.plan_id, "
                    "plan_name=excluded.plan_name, provider=excluded.provider, statement_date=excluded.statement_date, "
                    "billed=excluded.billed, you_owe=excluded.you_owe, possible_overcharge=excluded.possible_overcharge, "
                    "findings=excluded.findings, appeal_drafted=0, updated_at=excluded.updated_at",
                    (session_id, r["bill_id"], file_id, r["status"], r["plan_id"], r["plan_name"], r["provider"],
                     r["statement_date"], r["billed"], r["you_owe"], r["possible_overcharge"], r["findings"], now, now))
            elif r["type"] == "appeal":
                con.execute("UPDATE bill_audits SET appeal_drafted=1 WHERE session_id=? AND bill_id=?",
                            (session_id, r["bill_id"]))

    def add_upload(self, session_id: str, user_id: str | None, file_id: str, path: Path, name: str,
                   content_type: str, size: int, sha256: str) -> None:
        """Record a file already written to path, creating the conversation if this is its first request."""
        now = time.time()
        with transaction(self.path) as con:
            changed = con.execute(
                "INSERT INTO sessions (session_id, user_id, title, state, created_at, updated_at) "
                "VALUES (?, ?, NULL, '{}', ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "user_id=COALESCE(sessions.user_id, excluded.user_id), updated_at=excluded.updated_at "
                + _OWNER_MAY_WRITE, (session_id, user_id, now, now)).rowcount
            if not changed:
                raise NotYours(session_id)
            con.execute("INSERT INTO uploads (file_id, session_id, name, content_type, size, sha256, path, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (file_id, session_id, name, content_type, size, sha256, str(path), now))

    # The signed-in user's own data. Anything not theirs is None / False, never an error that says it exists.

    def sessions_of(self, user_id: str) -> list[dict]:
        """The user's conversations, newest first."""
        with transaction(self.path) as con:
            rows = con.execute("SELECT s.session_id, s.title, s.created_at, s.updated_at, "
                               "(SELECT COUNT(*) FROM uploads u WHERE u.session_id = s.session_id) "
                               "FROM sessions s WHERE s.user_id=? ORDER BY s.updated_at DESC", (user_id,)).fetchall()
        return [{"session_id": sid, "title": title, "created_at": created, "updated_at": updated, "uploads": n}
                for sid, title, created, updated, n in rows]

    def transcript(self, session_id: str, user_id: str) -> list[dict] | None:
        """What the user saw in their conversation, in order: [{"event", "data"}]; None if it isn't theirs."""
        with transaction(self.path) as con:
            if not con.execute("SELECT 1 FROM sessions WHERE session_id=? AND user_id=?",
                               (session_id, user_id)).fetchone():
                return None
            rows = con.execute("SELECT event, data FROM session_events WHERE session_id=? ORDER BY seq",
                               (session_id,)).fetchall()
        return [{"event": event, "data": json.loads(data)} for event, data in rows]

    def uploads_of(self, user_id: str, session_id: str | None = None) -> list[dict]:
        """The user's uploaded files, newest first, each with what it is (kind: "plan", "bill" or None until the
        agent reads it) and its bill reviews; only one conversation's when session_id is given."""
        sql = ("SELECT u.file_id, u.session_id, u.name, u.content_type, u.size, u.sha256, u.created_at, u.path, "
               "u.kind, u.plan_id, u.plan_name FROM uploads u JOIN sessions s ON s.session_id = u.session_id "
               "WHERE s.user_id=?")
        params = [user_id]
        if session_id is not None:
            sql, params = sql + " AND u.session_id=?", params + [session_id]
        review_keys = ("bill_id", "status", "plan_id", "plan_name", "provider", "statement_date", "billed", "you_owe",
                       "possible_overcharge", "findings", "appeal_drafted", "updated_at")
        with transaction(self.path) as con:
            rows = con.execute(sql + " ORDER BY u.created_at DESC", params).fetchall()
            reviews = {}
            for file_id, *review in con.execute(
                    "SELECT b.file_id, " + ", ".join(f"b.{k}" for k in review_keys) + " FROM bill_audits b JOIN "
                    "sessions s ON s.session_id = b.session_id WHERE s.user_id=? AND b.file_id IS NOT NULL "
                    "ORDER BY b.bill_id", (user_id,)):
                reviews.setdefault(file_id, []).append({**dict(zip(review_keys, review)),
                                                        "appeal_drafted": bool(review[10])})
        return [{"file_id": fid, "session_id": sid, "name": name, "content_type": ctype, "size": size,
                 "sha256": sha, "created_at": created, "path": Path(path), "kind": kind, "plan_id": plan_id,
                 "plan_name": plan_name, "bill_reviews": reviews.get(fid, [])}
                for fid, sid, name, ctype, size, sha, created, path, kind, plan_id, plan_name in rows]

    def upload_of(self, file_id: str, user_id: str) -> dict | None:
        return next((u for u in self.uploads_of(user_id) if u["file_id"] == file_id), None)

    def delete(self, session_id: str, user_id: str) -> bool:
        """Delete one of the user's conversations with its transcript and files; False if it isn't theirs."""
        with transaction(self.path) as con:
            mine = con.execute("SELECT 1 FROM sessions WHERE session_id=? AND user_id=?",
                               (session_id, user_id)).fetchone()
        if mine:
            self._delete([session_id])
        return bool(mine)

    def delete_user(self, user_id: str) -> int:
        """Delete all the user's conversations and their files (before AccountStore.delete_user); return how many."""
        with transaction(self.path) as con:
            ids = [r[0] for r in con.execute("SELECT session_id FROM sessions WHERE user_id=?", (user_id,))]
        self._delete(ids)
        return len(ids)

    def purge(self) -> int:
        """Delete expired anonymous conversations and their uploaded files; return how many."""
        cutoff = time.time() - self.ttl
        with transaction(self.path) as con:
            ids = [r[0] for r in con.execute("SELECT session_id FROM sessions WHERE user_id IS NULL AND "
                                             "updated_at < ?", (cutoff,))]
        self._delete(ids)
        return len(ids)

    def _delete(self, session_ids) -> None:
        """Rows first (events and upload records cascade), then the files, so no row ever points at a missing file."""
        with transaction(self.path) as con:
            con.executemany("DELETE FROM sessions WHERE session_id=?", [(sid,) for sid in session_ids])
        if self.upload_dir is not None:
            for sid in session_ids:
                shutil.rmtree(upload_folder(self.upload_dir, sid), ignore_errors=True)


def upload_folder(upload_dir: Path, session_id: str) -> Path:
    return upload_dir / "".join(c for c in session_id if c.isalnum() or c == "-")[:64]

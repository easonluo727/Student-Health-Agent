"""Conversation state in SQLite, so a redeploy doesn't lose a conversation and several instances can share them.

  SESSIONS_DB   default data/sessions.sqlite; /data/sessions.sqlite on Railway (the volume)
  SESSION_TTL   seconds a conversation is kept after its last turn (default one day); its uploads go with it

A session is loaded at the start of every request and saved at the end. What is saved is plain data: the model
history (as dicts), open forms, uploads, the home-screen mode, and the hooks' state (questionnaire answers, user
text, and the guard's numbers). The harness itself (prompt, skills, tools) is rebuilt from code each time.
"""

import json
import os
import shutil
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from harness import Harness

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_DB = Path(os.environ.get("SESSIONS_DB", ROOT / "data" / "sessions.sqlite"))
SESSION_TTL = int(os.environ.get("SESSION_TTL", 24 * 3600))


class Session:
    def __init__(self):
        self.harness = Harness()
        self.history = []
        self.open_forms = {}  # request_id -> the form's question dicts
        self.uploads = {}  # file_id -> {"path", "name", "content_type"}
        self.attachment_notes = []
        self.context = None  # the last home-screen mode applied


def _plain(item):
    """A history item as JSON data (the model's output items are SDK objects)."""
    return item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else item


def dump(session):
    hooks, guard = session.harness.hooks, session.harness.hooks.guard
    return {
        "history": [_plain(item) for item in session.history],
        "open_forms": session.open_forms,
        "uploads": {fid: {**u, "path": str(u["path"])} for fid, u in session.uploads.items()},
        "attachment_notes": session.attachment_notes,
        "context": session.context,
        "hooks": {"user_texts": hooks.user_texts, "answers": hooks.answers},
        "guard": {"user_numbers": sorted(map(str, guard._user_numbers)),
                  "tool_numbers": sorted(map(str, guard._tool_numbers)),
                  "confirmed": [list(k) for k in guard._confirmed], "missing_fields": guard._missing_fields},
    }


def load(state):
    session = Session()
    session.history = state["history"]
    session.open_forms = state["open_forms"]
    session.uploads = {fid: {**u, "path": Path(u["path"])} for fid, u in state["uploads"].items()}
    session.attachment_notes = state["attachment_notes"]
    session.context = state["context"]
    hooks, guard = session.harness.hooks, session.harness.hooks.guard
    hooks.user_texts, hooks.answers = state["hooks"]["user_texts"], state["hooks"]["answers"]
    g = state["guard"]
    guard._user_numbers = {Decimal(n) for n in g["user_numbers"]}
    guard._tool_numbers = {Decimal(n) for n in g["tool_numbers"]}
    guard._confirmed = {tuple(k) for k in g["confirmed"]}
    guard._missing_fields = g["missing_fields"]
    return session


class SessionStore:
    def __init__(self, path: Path = SESSIONS_DB, ttl: int = SESSION_TTL, upload_dir: Path | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.ttl, self.upload_dir = path, ttl, upload_dir
        with self._connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                        "updated_at REAL NOT NULL)")

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def get(self, session_id: str) -> Session:
        """The saved session, or a new one when there is none or it has expired."""
        with self._connect() as con:
            row = con.execute("SELECT state, updated_at FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        con.close()
        if row is None or time.time() - row[1] > self.ttl:
            return Session()
        return load(json.loads(row[0]))

    def save(self, session_id: str, session: Session) -> None:
        state = json.dumps(dump(session), ensure_ascii=False, allow_nan=False)
        with self._connect() as con:
            con.execute("INSERT INTO sessions (session_id, state, updated_at) VALUES (?, ?, ?) ON CONFLICT"
                        "(session_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                        (session_id, state, time.time()))
        con.close()

    def purge(self) -> int:
        """Delete expired sessions and their uploaded files; return how many."""
        cutoff = time.time() - self.ttl
        with self._connect() as con:
            expired = [r[0] for r in con.execute("SELECT session_id FROM sessions WHERE updated_at < ?", (cutoff,))]
            con.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        con.close()
        if self.upload_dir is not None:
            for session_id in expired:
                shutil.rmtree(upload_folder(self.upload_dir, session_id), ignore_errors=True)
        return len(expired)


def upload_folder(upload_dir: Path, session_id: str) -> Path:
    return upload_dir / "".join(c for c in session_id if c.isalnum() or c == "-")[:64]

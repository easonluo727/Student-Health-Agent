"""The app database: accounts, conversations, what the user saw, and uploads, in one SQLite file.

  APP_DB   default data/app.sqlite; /data/app.sqlite on Railway (the volume)

One file so the tables can reference each other: deleting a user deletes their logins and conversations, and
deleting a conversation deletes its transcript and upload records (ON DELETE CASCADE; files on disk are removed by
sessions.py). A conversation with no user is anonymous and expires (sessions.py); one with a user is kept until
they delete it. The plan and rate databases are separate: they hold shared data, not anyone's.

  users           one row per Google account
  logins          a signed-in browser: the sha256 of its cookie token
  sessions        one conversation: owner (NULL = anonymous), title, the model's state as JSON
  session_events  what the user saw, in order: their inputs and the server's SSE events, for reopening a chat
  uploads         one uploaded file; the file itself is in UPLOAD_DIR/<session_id>/; kind once the agent has read it
  user_facts      what the user told us that lasts: settings and some questionnaire answers (memory.py)
  bill_audits     the last review of each bill in a conversation, for the user's file list and the agent's memory

The schema version is SQLite's user_version; migrate() brings a file up to date and is safe to call every start.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DB = Path(os.environ.get("APP_DB", ROOT / "data" / "app.sqlite"))

MIGRATIONS = [
    # 1: accounts (moved from users.sqlite), conversations owned by users, transcripts and uploads.
    """
    CREATE TABLE users (
        user_id TEXT PRIMARY KEY,
        google_sub TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL,
        name TEXT,
        picture TEXT,
        created_at REAL NOT NULL,
        last_login_at REAL NOT NULL
    );
    CREATE TABLE logins (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    CREATE INDEX logins_by_user ON logins(user_id);
    CREATE INDEX logins_by_expiry ON logins(expires_at);
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT REFERENCES users(user_id) ON DELETE CASCADE,
        title TEXT,
        state TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX sessions_by_user ON sessions(user_id, updated_at);
    CREATE TABLE session_events (
        session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        event TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (session_id, seq)
    ) WITHOUT ROWID;
    CREATE TABLE uploads (
        file_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        content_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE INDEX uploads_by_session ON uploads(session_id);
    """,
    # 2: the user's settings and long-term facts, what each upload turned out to be, and bill reviews.
    """
    ALTER TABLE users ADD COLUMN display_name TEXT;
    ALTER TABLE uploads ADD COLUMN kind TEXT CHECK (kind IN ('plan', 'bill'));
    ALTER TABLE uploads ADD COLUMN plan_id TEXT;
    ALTER TABLE uploads ADD COLUMN plan_name TEXT;
    CREATE TABLE user_facts (
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        field TEXT NOT NULL,
        value TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('settings', 'form')),
        updated_at REAL NOT NULL,
        PRIMARY KEY (user_id, field)
    ) WITHOUT ROWID;
    CREATE TABLE bill_audits (
        session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        bill_id TEXT NOT NULL,
        file_id TEXT REFERENCES uploads(file_id) ON DELETE SET NULL,
        status TEXT NOT NULL,
        plan_id TEXT,
        plan_name TEXT,
        provider TEXT,
        statement_date TEXT,
        billed REAL,
        you_owe REAL,
        possible_overcharge REAL,
        findings INTEGER NOT NULL,
        appeal_drafted INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (session_id, bill_id)
    );
    CREATE INDEX bill_audits_by_file ON bill_audits(file_id);
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA foreign_keys=ON")  # per connection in SQLite; the cascades depend on it
    con.execute("PRAGMA journal_mode=WAL")
    return con


@contextmanager
def transaction(path: Path):
    """A connection whose work is committed at the end of the block, or rolled back if it raises."""
    con = connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def migrate(path: Path) -> None:
    """Create or upgrade the schema. The first time, import accounts from the old users.sqlite next to it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # executescript commits first, so the version bump is in the same script as the change.
            con.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")
            if number == 1:
                _import_legacy_users(con, path.with_name("users.sqlite"))
    finally:
        con.close()


def _import_legacy_users(con, legacy: Path) -> None:
    """Accounts and logins from users.sqlite (2026-11-01, before this file), so nobody is signed out."""
    if not legacy.is_file():
        return
    con.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
    try:
        with con:
            con.execute("INSERT OR IGNORE INTO users SELECT user_id, google_sub, email, name, picture, created_at, "
                        "last_login_at FROM legacy.users")
            con.execute("INSERT OR IGNORE INTO logins SELECT token_hash, user_id, created_at, expires_at "
                        "FROM legacy.logins WHERE user_id IN (SELECT user_id FROM users)")
    finally:
        con.execute("DETACH DATABASE legacy")

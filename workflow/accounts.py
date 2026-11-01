"""Accounts: sign in with Google. Users and their login sessions in SQLite; signing in is optional.

  GOOGLE_CLIENT_ID   the OAuth web client id (Google Cloud console); unset → sign-in is off and the button is hidden
  USERS_DB           default data/users.sqlite; /data/users.sqlite on Railway (the volume)
  LOGIN_TTL          seconds a login lasts (default 30 days)

The frontend shows Google's own button (Google Identity Services), which hands it an ID token. The frontend posts the
token here; Python checks Google's signature, audience and expiry, saves the user, and sets an httpOnly cookie holding
a random token. Only the token's sha256 is stored, so a copy of the database can't be used to log in.
"""

import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USERS_DB = Path(os.environ.get("USERS_DB", ROOT / "data" / "users.sqlite"))
LOGIN_TTL = int(os.environ.get("LOGIN_TTL", 30 * 24 * 3600))
COOKIE_NAME = "th_login"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


def google_client_id() -> str | None:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip() or None


def verify_google_token(credential: str, client_id: str) -> dict:
    """The ID token's claims if Google signed it for this client and it hasn't expired; ValueError otherwise."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise ValueError("The token was not issued by Google.")
    if not claims.get("email_verified"):
        raise ValueError("This Google account's email address is not verified.")
    return claims


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(row) -> dict:
    return {"user_id": row[0], "email": row[1], "name": row[2], "picture": row[3]}


class AccountStore:
    def __init__(self, path: Path = USERS_DB, ttl: int = LOGIN_TTL):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.ttl = path, ttl
        with self._connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, google_sub TEXT NOT NULL UNIQUE, "
                        "email TEXT NOT NULL, name TEXT, picture TEXT, created_at REAL NOT NULL, "
                        "last_login_at REAL NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS logins (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL "
                        "REFERENCES users(user_id), created_at REAL NOT NULL, expires_at REAL NOT NULL)")
        con.close()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def sign_in(self, claims: dict) -> tuple[dict, str]:
        """Create or update the user from verified Google claims; return (user, login token for the cookie)."""
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self._connect() as con:
            con.execute("INSERT INTO users (user_id, google_sub, email, name, picture, created_at, last_login_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email, "
                        "name=excluded.name, picture=excluded.picture, last_login_at=excluded.last_login_at",
                        (f"user_{secrets.token_hex(8)}", claims["sub"], claims["email"], claims.get("name"),
                         claims.get("picture"), now, now))
            row = con.execute("SELECT user_id, email, name, picture FROM users WHERE google_sub=?",
                              (claims["sub"],)).fetchone()
            con.execute("INSERT INTO logins (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                        (_hash(token), row[0], now, now + self.ttl))
        con.close()
        return public_user(row), token

    def user_for(self, token: str | None) -> dict | None:
        """The signed-in user for a cookie token, or None when it is missing, unknown or expired."""
        if not token:
            return None
        with self._connect() as con:
            row = con.execute("SELECT u.user_id, u.email, u.name, u.picture FROM logins l JOIN users u "
                              "ON u.user_id = l.user_id WHERE l.token_hash=? AND l.expires_at > ?",
                              (_hash(token), time.time())).fetchone()
        con.close()
        return public_user(row) if row else None

    def sign_out(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as con:
            con.execute("DELETE FROM logins WHERE token_hash=?", (_hash(token),))
        con.close()

    def purge(self) -> int:
        """Delete expired logins; return how many."""
        with self._connect() as con:
            count = con.execute("DELETE FROM logins WHERE expires_at < ?", (time.time(),)).rowcount
        con.close()
        return count

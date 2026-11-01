"""Google sign-in: users and logins in SQLite, and the checks on Google's ID token (no network)."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

import accounts  # noqa: E402
from accounts import AccountStore, verify_google_token  # noqa: E402

CLAIMS = {"iss": "https://accounts.google.com", "sub": "1234567890", "email": "bruin@g.ucla.edu",
          "email_verified": True, "name": "Joe Bruin", "picture": "https://example.com/joe.png"}


class AccountStoreTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "users.sqlite"
        self.store = AccountStore(self.path, ttl=3600)

    def test_sign_in_then_cookie_token_finds_the_user(self):
        user, token = self.store.sign_in(CLAIMS)
        self.assertEqual(user["email"], "bruin@g.ucla.edu")
        self.assertEqual(user["name"], "Joe Bruin")
        self.assertEqual(self.store.user_for(token), user)

    def test_signing_in_again_keeps_the_user_id_and_updates_the_profile(self):
        first, _ = self.store.sign_in(CLAIMS)
        second, token = self.store.sign_in({**CLAIMS, "name": "Josephine Bruin"})
        self.assertEqual(first["user_id"], second["user_id"])
        self.assertEqual(self.store.user_for(token)["name"], "Josephine Bruin")

    def test_unknown_missing_and_signed_out_tokens_find_nobody(self):
        _, token = self.store.sign_in(CLAIMS)
        self.assertIsNone(self.store.user_for(None))
        self.assertIsNone(self.store.user_for("not-a-token"))
        self.store.sign_out(token)
        self.assertIsNone(self.store.user_for(token))

    def test_expired_login_finds_nobody_and_is_purged(self):
        store = AccountStore(self.path, ttl=-1)
        _, token = store.sign_in(CLAIMS)
        self.assertIsNone(store.user_for(token))
        self.assertEqual(store.purge(), 1)

    def test_only_the_token_hash_is_stored(self):
        _, token = self.store.sign_in(CLAIMS)
        con = sqlite3.connect(self.path)
        stored = [r[0] for r in con.execute("SELECT token_hash FROM logins")]
        con.close()
        self.assertEqual(len(stored), 1)
        self.assertNotEqual(stored[0], token)


class VerifyGoogleTokenTest(unittest.TestCase):
    def verify(self, claims):
        with mock.patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims) as patched:
            result = verify_google_token("id-token", "client-123")
        self.assertEqual(patched.call_args.args[0], "id-token")
        self.assertEqual(patched.call_args.args[2], "client-123")
        return result

    def test_valid_claims_pass(self):
        self.assertEqual(self.verify(CLAIMS)["sub"], "1234567890")

    def test_other_issuer_is_rejected(self):
        with self.assertRaises(ValueError):
            self.verify({**CLAIMS, "iss": "https://evil.example.com"})

    def test_unverified_email_is_rejected(self):
        with self.assertRaises(ValueError):
            self.verify({**CLAIMS, "email_verified": False})


class ClientIdTest(unittest.TestCase):
    def test_blank_client_id_means_sign_in_is_off(self):
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "  "}):
            self.assertIsNone(accounts.google_client_id())
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "abc.apps.googleusercontent.com"}):
            self.assertEqual(accounts.google_client_id(), "abc.apps.googleusercontent.com")


if __name__ == "__main__":
    unittest.main()

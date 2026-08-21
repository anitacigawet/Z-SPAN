"""Invitation-gated local account, credential, reset, and route tests."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_CORE_PROJECT_DIR = Path(__file__).resolve().parents[3]
_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_COUNCIL_NAVIGATOR_DIR, _CORE_PROJECT_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import account_system, database, password_auth

sys.modules["database"] = database

import slack_listener

with tempfile.TemporaryDirectory() as _import_temp_dir:
    with (
        mock.patch.object(
            database,
            "DB_PATH",
            str(Path(_import_temp_dir) / "import.db"),
        ),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server


TOKEN_A = "A" * 32
TOKEN_B = "B" * 32
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "another memorable password phrase"


class PasswordAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "password-auth.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.scrypt_patch = mock.patch.multiple(
            password_auth,
            SCRYPT_N=2**12,
            SCRYPT_MAXMEM_BYTES=32 * 1024 * 1024,
        )
        self.scrypt_patch.start()
        self.addCleanup(self.scrypt_patch.stop)
        database.init_db()
        api_server.app.config.update(TESTING=True)
        api_server._reset_public_rate_limits_for_tests()
        self.client = api_server.app.test_client()

    @staticmethod
    def _import_invitation(token: str) -> None:
        database.import_invitation_batch(
            "password-auth-test",
            [{
                "serial_number": 1 if token == TOKEN_A else 2,
                "token_hash": hashlib.sha256(token.encode("ascii")).hexdigest(),
            }],
        )

    def _register(self, token: str = TOKEN_A):
        return password_auth.register_invited_user(
            email="Reader@Example.Test",
            display_name="Reader One",
            password=PASSWORD,
            invitation_token=token,
        )

    def test_registration_is_invitation_gated_and_atomic(self) -> None:
        with mock.patch.object(password_auth, "_new_credential") as derive:
            status, user = self._register(TOKEN_A)
        self.assertEqual(status, "invitation_unavailable")
        self.assertIsNone(user)
        derive.assert_not_called()
        self.assertEqual(database.count_users(), 0)

        self._import_invitation(TOKEN_A)
        status, user = self._register()
        self.assertEqual(status, "registered")
        self.assertIsNotNone(user)
        self.assertIsNone(user.google_sub)
        self.assertEqual(user.email, "reader@example.test")
        self.assertEqual(database.get_invitation_status(TOKEN_A), "redeemed")
        self.assertEqual(
            database.get_user_librarian_access(user.id),
            "granted",
        )

        conn = database.get_connection()
        try:
            credential = conn.execute(
                "SELECT * FROM password_credentials WHERE user_id = ?",
                (user.id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertNotEqual(credential["password_hash"], PASSWORD)
        self.assertNotIn(PASSWORD, tuple(credential))

    def test_correct_password_authenticates_and_wrong_password_locks(self) -> None:
        self._import_invitation(TOKEN_A)
        _, user = self._register()

        status, authenticated = password_auth.authenticate_password(
            email="READER@example.test",
            password=PASSWORD,
        )
        self.assertEqual(status, "authenticated")
        self.assertEqual(authenticated.id, user.id)

        with mock.patch.object(password_auth, "FAILED_ATTEMPT_LIMIT", 2):
            first, _ = password_auth.authenticate_password(
                email=user.email,
                password="this is the wrong passphrase",
            )
            second, _ = password_auth.authenticate_password(
                email=user.email,
                password="this is still not the password",
            )
            correct_while_locked, _ = password_auth.authenticate_password(
                email=user.email,
                password=PASSWORD,
            )
        self.assertEqual(first, "invalid")
        self.assertEqual(second, "locked")
        self.assertEqual(correct_while_locked, "locked")

    def test_existing_email_does_not_consume_another_invitation(self) -> None:
        self._import_invitation(TOKEN_A)
        self._register()
        self._import_invitation(TOKEN_B)
        status, user = password_auth.register_invited_user(
            email="reader@example.test",
            display_name="A Different Person",
            password=PASSWORD,
            invitation_token=TOKEN_B,
        )
        self.assertEqual(status, "email_unavailable")
        self.assertIsNone(user)
        self.assertEqual(database.get_invitation_status(TOKEN_B), "active")

        client = api_server.app.test_client()
        existing_email = client.post(
            "/api/auth/password/register",
            json={
                "email": "reader@example.test",
                "display_name": "Reader Again",
                "password": PASSWORD,
                "invitation_token": TOKEN_B,
            },
        )
        unknown_card = client.post(
            "/api/auth/password/register",
            json={
                "email": "different@example.test",
                "display_name": "Different Reader",
                "password": PASSWORD,
                "invitation_token": "C" * 32,
            },
        )
        self.assertEqual(existing_email.status_code, 409)
        self.assertEqual(existing_email.get_json(), unknown_card.get_json())

    def test_verified_google_identity_links_to_local_account(self) -> None:
        self._import_invitation(TOKEN_A)
        _, local_user = self._register()
        linked = account_system.upsert_user_from_google(
            google_sub="google-subject-1",
            email="Reader@Example.Test",
            display_name="Reader From Google",
            avatar_url="https://images.example.test/reader.png",
        )
        self.assertEqual(linked.id, local_user.id)
        self.assertEqual(linked.google_sub, "google-subject-1")
        self.assertEqual(database.count_users(), 1)

    def test_password_reset_is_one_time_and_replaces_the_verifier(self) -> None:
        self._import_invitation(TOKEN_A)
        _, user = self._register()
        raw_token, recipient = password_auth.create_password_reset_token(user.email)
        self.assertEqual(recipient, user.email)
        self.assertIsNotNone(raw_token)

        status, reset_user = password_auth.reset_password(
            token=raw_token,
            password=NEW_PASSWORD,
        )
        self.assertEqual(status, "reset")
        self.assertEqual(reset_user.id, user.id)
        repeated, _ = password_auth.reset_password(
            token=raw_token,
            password=PASSWORD,
        )
        self.assertEqual(repeated, "invalid")
        old_status, _ = password_auth.authenticate_password(
            email=user.email,
            password=PASSWORD,
        )
        new_status, _ = password_auth.authenticate_password(
            email=user.email,
            password=NEW_PASSWORD,
        )
        self.assertEqual(old_status, "invalid")
        self.assertEqual(new_status, "authenticated")

    def test_unknown_reset_bearer_does_not_invoke_scrypt(self) -> None:
        with mock.patch.object(password_auth, "_new_credential") as derive:
            status, user = password_auth.reset_password(
                token="Z" * 43,
                password=NEW_PASSWORD,
            )
        self.assertEqual(status, "invalid")
        self.assertIsNone(user)
        derive.assert_not_called()

    def test_reset_email_keeps_bearer_out_of_the_http_request_url(self) -> None:
        response = mock.Mock()
        response.raise_for_status.return_value = None
        with (
            mock.patch.dict(
                os.environ,
                {
                    "RESEND_API_KEY": "test-resend-key",
                    "ZSPAN_PUBLIC_ORIGIN": "https://zspan.org",
                },
            ),
            mock.patch.object(
                password_auth.requests,
                "post",
                return_value=response,
            ) as post,
        ):
            sent = password_auth.send_password_reset_email(
                "reader@example.test",
                "Z" * 43,
            )

        self.assertTrue(sent)
        payload = post.call_args.kwargs["json"]
        self.assertIn("https://zspan.org/login#reset=", payload["text"])
        self.assertNotIn("/login?reset=", payload["text"])

    def test_routes_set_session_and_keep_forgot_response_generic(self) -> None:
        self._import_invitation(TOKEN_A)
        registered = self.client.post(
            "/api/auth/password/register",
            json={
                "email": "reader@example.test",
                "display_name": "Reader One",
                "password": PASSWORD,
                "invitation_token": TOKEN_A,
            },
        )
        self.assertEqual(registered.status_code, 201)
        self.assertIn(api_server.SESSION_COOKIE_NAME, registered.headers["Set-Cookie"])

        with mock.patch.object(
            api_server,
            "send_password_reset_email",
        ) as send:
            known = self.client.post(
                "/api/auth/password/forgot",
                json={"email": "reader@example.test"},
            )
            unknown = self.client.post(
                "/api/auth/password/forgot",
                json={"email": "nobody@example.test"},
            )
        self.assertEqual(known.get_json(), unknown.get_json())
        send.assert_called_once()

    def test_registration_route_rejects_cross_origin_and_oversized_bodies(self) -> None:
        self._import_invitation(TOKEN_A)
        payload = {
            "email": "reader@example.test",
            "display_name": "Reader One",
            "password": PASSWORD,
            "invitation_token": TOKEN_A,
        }
        cross_origin = self.client.post(
            "/api/auth/password/register",
            json=payload,
            headers={"Origin": "https://attacker.example"},
        )
        oversized = self.client.post(
            "/api/auth/password/register",
            data=b"{" + b" " * (api_server._PASSWORD_AUTH_BODY_MAX_BYTES + 1),
            content_type="application/json",
        )
        self.assertEqual(cross_origin.status_code, 403)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(database.get_invitation_status(TOKEN_A), "active")
        self.assertEqual(database.count_users(), 0)

    def test_local_registration_cannot_claim_an_owner_email(self) -> None:
        self._import_invitation(TOKEN_A)
        with mock.patch.object(
            api_server,
            "get_owner_emails",
            return_value={"owner@example.test"},
        ):
            response = self.client.post(
                "/api/auth/password/register",
                json={
                    "email": "owner@example.test",
                    "display_name": "Not The Owner",
                    "password": PASSWORD,
                    "invitation_token": TOKEN_A,
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(database.get_invitation_status(TOKEN_A), "active")
        self.assertEqual(database.count_users(), 0)


class UsersMigrationTests(unittest.TestCase):
    def test_not_null_google_subject_migrates_without_breaking_child_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "legacy-users.db")
            with mock.patch.object(database, "DB_PATH", path):
                conn = database.get_connection()
                try:
                    conn.execute("""
                        CREATE TABLE users (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            google_sub TEXT UNIQUE NOT NULL,
                            email TEXT UNIQUE NOT NULL,
                            display_name TEXT,
                            avatar_url TEXT,
                            role TEXT NOT NULL DEFAULT 'light',
                            librarian_access TEXT NOT NULL DEFAULT 'none',
                            librarian_enforcement_epoch INTEGER NOT NULL DEFAULT 0,
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE child_row (
                            user_id INTEGER NOT NULL,
                            FOREIGN KEY (user_id) REFERENCES users(id)
                        )
                    """)
                    conn.execute(
                        "INSERT INTO users (google_sub, email) VALUES (?, ?)",
                        ("legacy-sub", "legacy@example.test"),
                    )
                    conn.execute("INSERT INTO child_row (user_id) VALUES (1)")
                    conn.commit()

                    database._ensure_users_support_local_auth(conn)
                    google_sub = next(
                        row
                        for row in conn.execute("PRAGMA table_info(users)")
                        if row[1] == "google_sub"
                    )
                    self.assertEqual(google_sub[3], 0)
                    self.assertEqual(
                        conn.execute("SELECT user_id FROM child_row").fetchone()[0],
                        1,
                    )
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()

"""Slice 3B public unsubscribe endpoint tests."""

from __future__ import annotations

import html
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

from parsers import database

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

from parsers import account_system, unsubscribe_tokens


class UnsubscribeEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = mock.patch.object(
            database,
            "DB_PATH",
            str(Path(self.temp_dir.name) / "unsubscribe.db"),
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.secret_patch = mock.patch.dict(
            os.environ,
            {"ZSPAN_SESSION_SECRET": "unsubscribe-endpoint-test-secret"},
            clear=False,
        )
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)
        database.init_db()

        self.user = account_system.upsert_user_from_google(
            google_sub="unsubscribe-user",
            email="unsubscribe@example.com",
        )
        account_system.set_notification_prefs(
            self.user.id,
            digest_cadence="daily",
            email_enabled=True,
        )
        self.token = unsubscribe_tokens.ensure_token_for_user(self.user.id)

        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def test_get_valid_token_is_html_and_does_not_mutate_prefs(self):
        response = self.client.get(
            "/api/unsubscribe",
            query_string={"token": self.token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith("text/html"))
        self.assertIn('method="post"', response.get_data(as_text=True))
        prefs = account_system.get_notification_prefs(self.user.id)
        self.assertTrue(prefs["email_enabled"])
        self.assertEqual(prefs["digest_cadence"], "daily")

    def test_get_invalid_token_returns_generic_400_html(self):
        response = self.client.get(
            "/api/unsubscribe",
            query_string={"token": "unknown.bad-signature"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.content_type.startswith("text/html"))
        self.assertIn("Invalid unsubscribe link", response.get_data(as_text=True))
        self.assertNotIn("unknown.bad-signature", response.get_data(as_text=True))

    def test_post_valid_json_disables_email_and_marks_token_used(self):
        response = self.client.post(
            "/api/unsubscribe",
            json={"token": self.token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith("text/html"))
        prefs = account_system.get_notification_prefs(self.user.id)
        self.assertFalse(prefs["email_enabled"])
        self.assertEqual(prefs["digest_cadence"], "daily")

        token_id = self.token.split(".", 1)[0]
        conn = database.get_connection()
        try:
            used_at = conn.execute(
                """
                SELECT used_at FROM unsubscribe_tokens
                WHERE token_id = ?
                """,
                (token_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(used_at)

    def test_post_invalid_token_returns_400_json(self):
        response = self.client.post(
            "/api/unsubscribe",
            json={"token": "unknown.bad-signature"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {
            "success": False,
            "error": "invalid_or_expired_token",
        })

    def test_post_replay_is_rejected(self):
        first = self.client.post(
            "/api/unsubscribe",
            data={"token": self.token},
        )
        second = self.client.post(
            "/api/unsubscribe",
            query_string={"token": self.token},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.get_json(), {
            "success": False,
            "error": "invalid_or_expired_token",
        })
        self.assertFalse(
            account_system.get_notification_prefs(self.user.id)[
                "email_enabled"
            ]
        )

    def test_preference_failure_rolls_back_token_claim(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """
                CREATE TRIGGER reject_unsubscribe_pref
                BEFORE UPDATE OF email_enabled ON notification_prefs
                WHEN NEW.email_enabled = 0
                BEGIN
                    SELECT RAISE(ABORT, 'simulated preference failure');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            self.client.post(
                "/api/unsubscribe",
                json={"token": self.token},
            )

        self.assertEqual(
            unsubscribe_tokens.verify_unsubscribe_token(self.token),
            self.user.id,
        )
        self.assertTrue(
            account_system.get_notification_prefs(self.user.id)[
                "email_enabled"
            ]
        )

    def test_get_escapes_reflected_token_value(self):
        raw = 'id"><script>alert("xss")</script>.signature'
        with mock.patch.object(
            api_server,
            "verify_unsubscribe_token",
            return_value=self.user.id,
        ):
            response = self.client.get(
                "/api/unsubscribe",
                query_string={"token": raw},
            )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(raw, body)
        self.assertNotIn("<script>", body)
        self.assertIn(html.escape(raw, quote=True), body)


if __name__ == "__main__":
    unittest.main(verbosity=2)

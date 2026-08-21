"""Librarian request-access schema, route, and fresh-row gate tests."""
from __future__ import annotations

import sys
import sqlite3
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

import env_config
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


class LibrarianAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "librarian.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()

        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()
        self.owner_id = self._seed_user("owner@example.test")
        self.user_id = self._seed_user("reader@example.test")

    @staticmethod
    def _seed_user(email: str) -> int:
        conn = database.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (
                    google_sub, email, display_name, avatar_url
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    f"google-{email}",
                    email,
                    email.split("@", 1)[0].title(),
                    f"https://images.example.test/{email}.png",
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    @staticmethod
    def _user(user_id: int):
        user = api_server.get_user(user_id)
        if user is None:
            raise AssertionError(f"test user {user_id} disappeared")
        return user

    def _gate(self, user_id: int | None, *, owner=False):
        user = self._user(user_id) if user_id is not None else None
        with (
            api_server.app.test_request_context("/api/rag-search"),
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=user,
            ),
            mock.patch.object(
                api_server,
                "is_owner_email",
                return_value=owner,
            ),
        ):
            return api_server._byok_public_query_allowed()

    def _post_decision(self, action: str, *, user_id: int | None = None):
        owner = self._user(self.owner_id)
        with (
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=owner,
            ),
            mock.patch.object(
                api_server,
                "is_owner_email",
                return_value=True,
            ),
        ):
            return self.client.post(
                (
                    "/api/librarian/access-requests/"
                    f"{user_id if user_id is not None else self.user_id}/decide"
                ),
                json={"action": action},
            )

    def _epoch(self, user_id: int | None = None) -> int:
        conn = database.get_connection()
        try:
            return int(conn.execute(
                """
                SELECT librarian_enforcement_epoch
                FROM users
                WHERE id = ?
                """,
                (self.user_id if user_id is None else user_id,),
            ).fetchone()[0])
        finally:
            conn.close()

    def test_gate_allows_signed_in_accounts_without_manual_approval(self):
        for status in ("requested", "none"):
            with self.subTest(status=status):
                database.set_librarian_access(self.user_id, status)
                self.assertEqual(
                    self._gate(self.user_id),
                    (True, "signed-account"),
                )

        database.set_librarian_access(self.user_id, "granted")
        self.assertEqual(
            self._gate(self.user_id),
            (True, "signed-account"),
        )
        self.assertEqual(
            self._gate(self.owner_id, owner=True),
            (True, "owner"),
        )
        self.assertEqual(
            self._gate(None),
            (False, "sign-in-required"),
        )

    def test_manual_ban_remains_an_enforceable_abuse_boundary(self):
        database.set_librarian_access(self.user_id, "banned")
        self.assertEqual(
            self._gate(self.user_id),
            (False, "account-blocked"),
        )

    def test_truthy_retired_setting_cannot_unlock_anonymous_live_query(self):
        with (
            mock.patch.object(
                env_config,
                "load_user_settings",
                return_value={"byok_public_query_enabled": True},
            ) as load_settings,
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=None,
            ),
            mock.patch.object(
                api_server,
                "is_owner_email",
                return_value=False,
            ),
        ):
            response = self.client.post(
                "/api/rag-search/101",
                json={"query": "What happened?"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["status"], "sign_in_required")
        load_settings.assert_not_called()

    def test_request_access_transitions_and_ban(self):
        with mock.patch.object(
            api_server,
            "_current_user_from_cookie",
            return_value=None,
        ):
            response = self.client.post("/api/librarian/request-access")
        self.assertEqual(response.status_code, 401)

        user = self._user(self.user_id)
        with mock.patch.object(
            api_server,
            "_current_user_from_cookie",
            return_value=user,
        ):
            first = self.client.post("/api/librarian/request-access")
            repeat = self.client.post("/api/librarian/request-access")
        self.assertEqual(first.get_json(), {
            "success": True,
            "status": "requested",
        })
        self.assertEqual(repeat.get_json(), first.get_json())
        self.assertEqual(
            database.get_user_librarian_access(self.user_id),
            "requested",
        )

        database.set_librarian_access(self.user_id, "banned")
        with mock.patch.object(
            api_server,
            "_current_user_from_cookie",
            return_value=user,
        ):
            banned = self.client.post("/api/librarian/request-access")
        self.assertEqual(banned.status_code, 403)
        self.assertEqual(banned.get_json()["success"], False)
        self.assertEqual(banned.get_json()["status"], "banned")
        self.assertEqual(
            banned.get_json()["message"],
            "Librarian access is unavailable for this account.",
        )

    def test_generic_access_epoch_advances_only_on_stored_change(self):
        self.assertEqual(self._epoch(), 0)
        self.assertTrue(
            database.set_librarian_access(self.user_id, "requested")
        )
        self.assertEqual(self._epoch(), 1)
        self.assertTrue(
            database.set_librarian_access(self.user_id, "requested")
        )
        self.assertEqual(self._epoch(), 1)
        database.set_librarian_access(self.user_id, "granted")
        self.assertEqual(self._epoch(), 2)

    def test_decisions_require_owner_and_update_database(self):
        user = self._user(self.user_id)
        with (
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=user,
            ),
            mock.patch.object(
                api_server,
                "is_owner_email",
                return_value=False,
            ),
        ):
            blocked = self.client.post(
                f"/api/librarian/access-requests/{self.user_id}/decide",
                json={"action": "grant"},
            )
        self.assertEqual(blocked.status_code, 403)

        for action, expected in (
            ("grant", "granted"),
            ("deny", "none"),
            ("ban", "banned"),
        ):
            with self.subTest(action=action):
                response = self._post_decision(action)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json(), {
                    "success": True,
                    "user_id": self.user_id,
                    "status": expected,
                })
                self.assertEqual(
                    database.get_user_librarian_access(self.user_id),
                    expected,
                )

        self.assertEqual(self._post_decision("unknown").status_code, 400)
        self.assertEqual(
            self._post_decision("grant", user_id=999_999).status_code,
            404,
        )

    def test_each_operator_decision_advances_epoch_once(self):
        before = self._epoch()
        for offset, action in enumerate(("grant", "grant", "deny", "ban"), 1):
            self.assertEqual(self._post_decision(action).status_code, 200)
            self.assertEqual(self._epoch(), before + offset)

    def test_grant_clears_residuals_without_active_auto_ban(self):
        database.set_librarian_access(self.user_id, "granted")
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO librarian_abuse_state (
                    user_id,
                    recent_rejects_json,
                    recent_cooldowns_json,
                    cooldown_until,
                    cooldown_blocked_count,
                    duplicate_suppressed_count,
                    active_auto_ban
                ) VALUES (?, '[{"ts":"x","fp":"y"}]', '["x"]',
                          '2999-01-01 00:00:00', 4, 5, 0)
                """,
                (self.user_id,),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(self._post_decision("grant").status_code, 200)
        conn = database.get_connection()
        try:
            state = conn.execute(
                """
                SELECT *
                FROM librarian_abuse_state
                WHERE user_id = ?
                """,
                (self.user_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(state["recent_rejects_json"], "[]")
        self.assertEqual(state["recent_cooldowns_json"], "[]")
        self.assertIsNone(state["cooldown_until"])
        self.assertEqual(state["cooldown_blocked_count"], 0)
        self.assertEqual(state["duplicate_suppressed_count"], 0)
        self.assertEqual(state["active_auto_ban"], 0)
        self.assertIsNotNone(state["last_restored_at"])

    def test_schema_migration_is_idempotent_and_preserves_epoch(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE users
                SET librarian_enforcement_epoch = 7
                WHERE id = ?
                """,
                (self.user_id,),
            )
            conn.commit()
        finally:
            conn.close()
        database.init_db()
        database.init_db()
        self.assertEqual(self._epoch(), 7)

    def test_old_user_migrates_to_zero_once_then_keeps_new_epoch(self):
        with tempfile.TemporaryDirectory() as migration_dir:
            old_path = str(Path(migration_dir) / "old-users.db")
            old = sqlite3.connect(old_path)
            try:
                old.execute(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        google_sub TEXT UNIQUE NOT NULL,
                        email TEXT UNIQUE NOT NULL,
                        display_name TEXT,
                        avatar_url TEXT,
                        role TEXT NOT NULL DEFAULT 'light',
                        librarian_access TEXT NOT NULL DEFAULT 'granted',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                old.execute(
                    """
                    INSERT INTO users (google_sub, email)
                    VALUES ('old-sub', 'old@example.test')
                    """
                )
                old.commit()
            finally:
                old.close()
            with mock.patch.object(database, "DB_PATH", old_path):
                database.init_db()
                migrated = database.get_connection()
                try:
                    epoch = migrated.execute(
                        """
                        SELECT librarian_enforcement_epoch
                        FROM users
                        WHERE id = 1
                        """
                    ).fetchone()[0]
                    self.assertEqual(epoch, 0)
                    migrated.execute(
                        """
                        UPDATE users
                        SET librarian_enforcement_epoch = 9
                        WHERE id = 1
                        """
                    )
                    migrated.commit()
                finally:
                    migrated.close()
                database.init_db()
                database.init_db()
                migrated = database.get_connection()
                try:
                    self.assertEqual(
                        migrated.execute(
                            """
                            SELECT librarian_enforcement_epoch
                            FROM users
                            WHERE id = 1
                            """
                        ).fetchone()[0],
                        9,
                    )
                finally:
                    migrated.close()

    def test_owner_can_list_non_default_access_rows(self):
        database.set_librarian_access(self.user_id, "requested")
        owner = self._user(self.owner_id)
        with (
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=owner,
            ),
            mock.patch.object(
                api_server,
                "is_owner_email",
                return_value=True,
            ),
        ):
            response = self.client.get("/api/librarian/access-requests")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["requests"]), 1)
        self.assertEqual(payload["requests"][0]["id"], self.user_id)
        self.assertEqual(
            payload["requests"][0]["librarian_access"],
            "requested",
        )

    def test_account_gate_needs_no_approval_but_preserves_manual_bans(self):
        database.set_librarian_access(self.user_id, "granted")
        self.assertEqual(
            self._gate(self.user_id),
            (True, "signed-account"),
        )

        decision = self._post_decision("ban")
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(
            database.get_user_librarian_access(self.user_id),
            "banned",
        )
        self.assertEqual(
            self._gate(self.user_id),
            (False, "account-blocked"),
        )

    def test_validate_key_carries_the_same_gate_as_the_relays(self):
        # Sibling-branch check (§ 5a): the public-plane edge admits
        # /api/byok/validate-key, so it must carry the same D-145 gate as
        # relay/relay-stream — otherwise it is an anonymous key-validation
        # oracle. Anonymous → locked; signed-in → gate passes and the request
        # proceeds to body validation (400 on the short key, no provider
        # network touched).
        with (
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=None,
            ),
            mock.patch.object(api_server, "is_owner_email", return_value=False),
        ):
            anon = self.client.post(
                "/api/byok/validate-key",
                json={"provider": "google-gemini-2.5-flash", "api_key": "x" * 20},
            )
        self.assertEqual(anon.status_code, 403)
        self.assertEqual(anon.get_json().get("status"), "sign_in_required")

        database.set_librarian_access(self.user_id, "granted")
        with (
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=self._user(self.user_id),
            ),
            mock.patch.object(api_server, "is_owner_email", return_value=False),
        ):
            granted = self.client.post(
                "/api/byok/validate-key",
                json={"provider": "google-gemini-2.5-flash", "api_key": "short"},
            )
        self.assertEqual(granted.status_code, 400)


if __name__ == "__main__":
    unittest.main()

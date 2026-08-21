"""Single-use printed invitation storage, redemption, and route tests."""
from __future__ import annotations

import hashlib
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


TOKEN_A = "A" * 32
TOKEN_B = "B" * 32
TOKEN_C = "C" * 32
TOKEN_D = "D" * 32
BATCH = "chamber-2026-01"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class InvitationAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "invitations.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        api_server.app.config.update(TESTING=True)
        api_server._reset_public_rate_limits_for_tests()
        self.client = api_server.app.test_client()
        self.owner_id = self._seed_user("owner@example.test")
        self.user_id = self._seed_user("reader@example.test")
        self.other_user_id = self._seed_user("other@example.test")

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

    def _import(self, *tokens: str) -> dict[str, int]:
        return database.import_invitation_batch(
            BATCH,
            [
                {"serial_number": index, "token_hash": _digest(token)}
                for index, token in enumerate(tokens, 1)
            ],
            actor_user_id=self.owner_id,
        )

    def test_database_keeps_only_a_one_way_digest(self) -> None:
        self.assertEqual(self._import(TOKEN_A), {"inserted": 1, "unchanged": 0})
        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM invitation_codes"
            ).fetchone()
            columns = {
                item[1]
                for item in conn.execute("PRAGMA table_info(invitation_codes)")
            }
        finally:
            conn.close()

        self.assertNotIn("token", columns)
        self.assertEqual(row["token_hash"], _digest(TOKEN_A))
        self.assertNotEqual(row["token_hash"], TOKEN_A)
        self.assertNotIn("token_hash", database.list_invitation_codes()[0])

    def test_batch_import_is_idempotent_and_collision_safe(self) -> None:
        self.assertEqual(
            self._import(TOKEN_A, TOKEN_B),
            {"inserted": 2, "unchanged": 0},
        )
        self.assertEqual(
            self._import(TOKEN_A, TOKEN_B),
            {"inserted": 0, "unchanged": 2},
        )

        with self.assertRaisesRegex(ValueError, "serial already belongs"):
            database.import_invitation_batch(
                BATCH,
                [
                    {"serial_number": 1, "token_hash": _digest(TOKEN_C)},
                    {"serial_number": 3, "token_hash": _digest(TOKEN_D)},
                ],
                actor_user_id=self.owner_id,
            )
        self.assertEqual(len(database.list_invitation_codes()), 2)

        with self.assertRaisesRegex(ValueError, "already belongs to another card"):
            database.import_invitation_batch(
                "another-batch",
                [{"serial_number": 1, "token_hash": _digest(TOKEN_A)}],
                actor_user_id=self.owner_id,
            )

    def test_redeem_is_atomic_single_use_and_same_user_idempotent(self) -> None:
        self._import(TOKEN_A)
        database.set_librarian_access(self.user_id, "requested")
        epoch_before = self._epoch(self.user_id)

        self.assertEqual(
            database.redeem_invitation_token(self.user_id, TOKEN_A),
            "redeemed",
        )
        self.assertEqual(
            database.get_user_librarian_access(self.user_id),
            "granted",
        )
        self.assertEqual(self._epoch(self.user_id), epoch_before + 1)
        self.assertEqual(database.get_invitation_status(TOKEN_A), "redeemed")
        self.assertEqual(
            database.redeem_invitation_token(self.user_id, TOKEN_A),
            "redeemed",
        )
        self.assertEqual(
            database.redeem_invitation_token(self.other_user_id, TOKEN_A),
            "unavailable",
        )

        conn = database.get_connection()
        try:
            redeemed_events = conn.execute(
                """
                SELECT COUNT(*)
                FROM invitation_events
                WHERE event_type = 'redeemed'
                """
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(redeemed_events, 1)

    def test_banned_or_already_granted_account_does_not_consume_card(self) -> None:
        self._import(TOKEN_A, TOKEN_B)
        database.set_librarian_access(self.user_id, "banned")
        self.assertEqual(
            database.redeem_invitation_token(self.user_id, TOKEN_A),
            "banned",
        )
        self.assertEqual(database.get_invitation_status(TOKEN_A), "active")

        database.set_librarian_access(self.other_user_id, "granted")
        self.assertEqual(
            database.redeem_invitation_token(self.other_user_id, TOKEN_B),
            "already_granted",
        )
        self.assertEqual(database.get_invitation_status(TOKEN_B), "active")

    def test_unused_card_can_be_revoked_but_redeemed_card_cannot(self) -> None:
        self._import(TOKEN_A, TOKEN_B)
        invitation_ids = [row["id"] for row in database.list_invitation_codes()]
        self.assertEqual(
            database.revoke_invitation_code(
                invitation_ids[0], actor_user_id=self.owner_id
            ),
            "revoked",
        )
        self.assertEqual(database.get_invitation_status(TOKEN_A), "revoked")
        self.assertEqual(
            database.redeem_invitation_token(self.user_id, TOKEN_A),
            "revoked",
        )

        self.assertEqual(
            database.redeem_invitation_token(self.user_id, TOKEN_B),
            "redeemed",
        )
        self.assertEqual(
            database.revoke_invitation_code(
                invitation_ids[1], actor_user_id=self.owner_id
            ),
            "already_redeemed",
        )

    def test_public_status_collapses_all_non_active_states(self) -> None:
        self._import(TOKEN_A, TOKEN_B)
        invitations = database.list_invitation_codes()
        database.revoke_invitation_code(
            invitations[1]["id"], actor_user_id=self.owner_id
        )

        active = self.client.post(
            "/api/invitations/status", json={"token": TOKEN_A}
        )
        revoked = self.client.post(
            "/api/invitations/status", json={"token": TOKEN_B}
        )
        invalid = self.client.post(
            "/api/invitations/status", json={"token": TOKEN_C}
        )
        self.assertEqual(active.get_json()["status"], "active")
        self.assertEqual(revoked.get_json()["status"], "unavailable")
        self.assertEqual(invalid.get_json(), revoked.get_json())

    def test_redemption_route_requires_sign_in_and_grants_access(self) -> None:
        self._import(TOKEN_A)
        anonymous = self.client.post(
            "/api/invitations/redeem", json={"token": TOKEN_A}
        )
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(database.get_invitation_status(TOKEN_A), "active")

        with mock.patch.object(
            api_server,
            "_current_user_from_cookie",
            return_value=self._user(self.user_id),
        ):
            redeemed = self.client.post(
                "/api/invitations/redeem", json={"token": TOKEN_A}
            )
        self.assertEqual(redeemed.status_code, 200)
        self.assertEqual(redeemed.get_json()["status"], "granted")
        self.assertEqual(
            database.get_user_librarian_access(self.user_id), "granted"
        )

    def test_mutations_reject_untrusted_browser_origins(self) -> None:
        self._import(TOKEN_A)
        response = self.client.post(
            "/api/invitations/redeem",
            json={"token": TOKEN_A},
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "untrusted_origin")
        self.assertEqual(database.get_invitation_status(TOKEN_A), "active")

    def test_owner_can_import_list_and_revoke_without_hash_disclosure(self) -> None:
        owner = self._user(self.owner_id)
        payload = {
            "batch_name": BATCH,
            "invitations": [
                {"serial_number": 1, "token_hash": _digest(TOKEN_A)}
            ],
        }
        with (
            mock.patch.object(
                api_server, "_current_user_from_cookie", return_value=owner
            ),
            mock.patch.object(api_server, "is_owner_email", return_value=True),
        ):
            imported = self.client.post("/api/invitations/import", json=payload)
            listed = self.client.get("/api/invitations")
            invitation_id = listed.get_json()["invitations"][0]["id"]
            revoked = self.client.post(
                f"/api/invitations/{invitation_id}/revoke"
            )

        self.assertEqual(imported.get_json()["inserted"], 1)
        self.assertNotIn("token_hash", listed.get_data(as_text=True))
        self.assertEqual(revoked.get_json()["status"], "revoked")

    def test_owner_routes_reject_non_owner_and_bad_shapes(self) -> None:
        reader = self._user(self.user_id)
        with (
            mock.patch.object(
                api_server, "_current_user_from_cookie", return_value=reader
            ),
            mock.patch.object(api_server, "is_owner_email", return_value=False),
        ):
            self.assertEqual(self.client.get("/api/invitations").status_code, 403)
            self.assertEqual(
                self.client.post(
                    "/api/invitations/import", json={"batch_name": BATCH}
                ).status_code,
                403,
            )

        owner = self._user(self.owner_id)
        with (
            mock.patch.object(
                api_server, "_current_user_from_cookie", return_value=owner
            ),
            mock.patch.object(api_server, "is_owner_email", return_value=True),
        ):
            bad = self.client.post("/api/invitations/import", json=[])
        self.assertEqual(bad.status_code, 400)

    @staticmethod
    def _epoch(user_id: int) -> int:
        conn = database.get_connection()
        try:
            return int(
                conn.execute(
                    """
                    SELECT librarian_enforcement_epoch
                    FROM users
                    WHERE id = ?
                    """,
                    (user_id,),
                ).fetchone()[0]
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

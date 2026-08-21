"""Publication identity coercion and private owner-attribution tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database, google_oauth, operator_identity

sys.modules["database"] = database

import slack_listener

with tempfile.TemporaryDirectory() as _import_temp_dir:
    with (
        mock.patch.object(
            database, "DB_PATH", str(Path(_import_temp_dir) / "import.db")
        ),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server


class OperatorIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = str(Path(self.temp_dir.name) / "identity.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", self.database_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        operator_identity.clear_legacy_token_cache()
        self.addCleanup(operator_identity.clear_legacy_token_cache)
        database.init_db()

        conn = database.get_connection()
        try:
            city_id = conn.execute(
                "INSERT INTO cities (name, county, state) VALUES (?, ?, ?)",
                ("Identity City", "Test County", "Arizona"),
            ).lastrowid
            for meeting_id in (701, 702):
                conn.execute(
                    """
                    INSERT INTO meetings (
                        id, city_id, city_name, county, state, meeting_title,
                        meeting_date, meeting_status, published_by
                    ) VALUES (?, ?, 'Identity City', 'Test County', 'Arizona',
                              ?, '2026-07-22', 'Scheduled', ?)
                    """,
                    (
                        meeting_id,
                        city_id,
                        f"Identity Meeting {meeting_id}",
                        "Legacy Alias" if meeting_id == 702 else None,
                    ),
                )
            self.work_order_id = conn.execute(
                "INSERT INTO work_orders (meeting_id, state) VALUES (701, 'completed')"
            ).lastrowid
            self.owner_email = "owner.identity@example.test"
            self.owner_name = "Owner Identity"
            self.user_id = conn.execute(
                """
                INSERT INTO users (google_sub, email, display_name)
                VALUES ('identity-owner', ?, ?)
                """,
                (self.owner_email, self.owner_name),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()
        self.owner = SimpleNamespace(
            id=self.user_id,
            email=self.owner_email,
            display_name=self.owner_name,
        )
        self.owner_gate = mock.patch.object(
            api_server, "_require_owner", return_value=(self.owner, None)
        )
        self.owner_gate.start()
        self.addCleanup(self.owner_gate.stop)
        self.owner_emails = mock.patch.object(
            google_oauth, "get_owner_emails", return_value={self.owner_email}
        )
        self.owner_emails.start()
        self.addCleanup(self.owner_emails.stop)

    def _role_rows(self):
        conn = database.get_connection()
        try:
            meeting = conn.execute(
                "SELECT published_by, publish_notes FROM meetings WHERE id = 701"
            ).fetchone()
            work_order = conn.execute(
                "SELECT approved_by FROM work_orders WHERE id = ?",
                (self.work_order_id,),
            ).fetchone()
            verifications = conn.execute(
                "SELECT verified_by FROM quote_verifications ORDER BY id"
            ).fetchall()
            events = conn.execute(
                """
                SELECT action, actor_user_id
                FROM operator_review_events ORDER BY id
                """
            ).fetchall()
            return meeting, work_order, verifications, events
        finally:
            conn.close()

    def test_write_boundaries_coerce_every_actor_shape_and_audit_user(self):
        actor_values = (
            "person@example.test",
            "A Full Name",
            "arbitrary actor",
            None,
            "Z-SPAN",
        )
        for actor in actor_values:
            publish_payload = {"force": True}
            approve_payload = {"verified_quote_ids": []}
            unpublish_payload = {}
            if actor is not None:
                publish_payload["published_by"] = actor
                approve_payload["approved_by"] = actor
                unpublish_payload["unpublished_by"] = actor

            published = self.client.post(
                "/api/meetings/701/publish", json=publish_payload
            )
            approved = self.client.post(
                f"/api/work-orders/{self.work_order_id}/approve",
                json=approve_payload,
            )
            unpublished = self.client.post(
                "/api/meetings/701/unpublish", json=unpublish_payload
            )
            self.assertEqual(published.status_code, 200, published.get_data(as_text=True))
            self.assertEqual(approved.status_code, 200, approved.get_data(as_text=True))
            self.assertEqual(unpublished.status_code, 200, unpublished.get_data(as_text=True))

        meeting, work_order, _, events = self._role_rows()
        self.assertEqual(meeting["published_by"], "Z-SPAN")
        self.assertIn("by Z-SPAN", meeting["publish_notes"])
        self.assertEqual(work_order["approved_by"], "Z-SPAN")
        self.assertEqual(len(events), len(actor_values) * 3)
        self.assertEqual(
            [row["action"] for row in events],
            [action for _ in actor_values for action in ("publish", "approve", "unpublish")],
        )
        self.assertTrue(all(row["actor_user_id"] == self.user_id for row in events))

    def test_publish_and_unpublish_reject_unsafe_prose(self):
        cases = (
            ("/api/meetings/701/publish", {"force": True, "publish_notes": "mail x@y.test"}),
            ("/api/meetings/701/publish", {"force": True, "publish_notes": self.owner_name}),
            ("/api/meetings/701/publish", {"force": True, "publish_notes": "Owner approved"}),
            ("/api/meetings/701/publish", {"force": True, "publish_notes": "Legacy Alias reviewed"}),
            ("/api/meetings/701/unpublish", {"reason": "mail x@y.test"}),
            ("/api/meetings/701/unpublish", {"reason": self.owner_name}),
        )
        for path, payload in cases:
            with self.subTest(path=path, payload=payload):
                response = self.client.post(path, json=payload)
                self.assertEqual(response.status_code, 400)

        _, _, _, events = self._role_rows()
        self.assertEqual(events, [])

    def test_role_identity_display_name_is_safe_in_publication_prose(self):
        conn = database.get_connection()
        try:
            conn.execute(
                "UPDATE users SET display_name = ? WHERE id = ?",
                (operator_identity.ROLE_IDENTITY, self.user_id),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertIsNone(
            database.publication_text_violation("Published by Z-SPAN")
        )

    def test_direct_database_calls_still_coerce_without_private_attribution(self):
        published = database.publish_meeting(
            701,
            "poison@example.test",
            publisher_user_id=self.user_id,
            force=True,
        )
        approved = database.approve_work_order(
            self.work_order_id,
            "Poisoned Name",
            verified_quote_ids=["quote-1"],
        )
        unpublished = database.unpublish_meeting(
            701,
            unpublished_by="arbitrary",
            reason="policy revision",
        )

        self.assertEqual(published["published_by"], "Z-SPAN")
        self.assertEqual(approved["approved_by"], "Z-SPAN")
        self.assertIn("by Z-SPAN", unpublished["publish_notes"])
        meeting, work_order, verifications, events = self._role_rows()
        self.assertEqual(meeting["published_by"], "Z-SPAN")
        self.assertEqual(work_order["approved_by"], "Z-SPAN")
        self.assertEqual([row["verified_by"] for row in verifications], ["Z-SPAN"])
        self.assertEqual(events, [])

    def test_operator_citation_joins_private_event_to_user(self):
        response = self.client.post(
            "/api/meetings/701/publish",
            json={"force": True, "published_by": "discarded"},
        )
        self.assertEqual(response.status_code, 200)

        citation = api_server._build_citation_tree(701, anonymize=False)
        event = citation["operator_review_events"][0]
        self.assertEqual(event["actor_user_id"], self.user_id)
        self.assertEqual(event["actor_display_name"], self.owner_name)
        self.assertEqual(event["actor_email"], self.owner_email)
        self.assertIn("clicked publish", event["description"])

        public_tree = api_server._build_citation_tree(701, anonymize=True)
        self.assertNotIn("operator_review_events", public_tree)


if __name__ == "__main__":
    unittest.main()

"""BRA-6 approval integrity tests for the per-meeting flagship payload."""
from __future__ import annotations

import sys
import tempfile
import unittest
import copy
from pathlib import Path
from unittest import mock

_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database

sys.modules["database"] = database

from parsers import flagship_sync


class FlagshipSyncApprovalTests(unittest.TestCase):
    APPROVED_AT = "2026-07-17 14:25:00"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.source_db = str(root / "source.db")
        self.receiver_db = str(root / "receiver.db")
        self._init_db(self.source_db)
        self._init_db(self.receiver_db)

    @staticmethod
    def _init_db(path: str) -> None:
        with mock.patch.object(database, "DB_PATH", path):
            database.init_db()

    def _seed_source(self, *, is_published: bool, approved_at: str | None) -> None:
        with mock.patch.object(database, "DB_PATH", self.source_db):
            conn = database.get_connection()
            try:
                city_id = conn.execute(
                    "INSERT INTO cities (name, county, state) VALUES (?, ?, ?)",
                    ("Testville", "Test County", "Arizona"),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO meetings (
                        id, city_id, city_name, county, state, meeting_title,
                        meeting_date, meeting_status, is_published,
                        published_by, publish_notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        501,
                        city_id,
                        "Testville",
                        "Test County",
                        "Arizona",
                        "Regular Council Meeting",
                        "2026-07-17",
                        "Minutes Available",
                        int(is_published),
                        "poisoned.sender@example.test",
                        "Institutional review complete",
                    ),
                )
                member_id = conn.execute(
                    """
                    INSERT INTO council_members (
                        city_name, name, seat_id, role
                    ) VALUES ('Testville', 'Public Member', 'seat-1', 'Member')
                    """
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO quotes (
                        meeting_id, member_id, speaker_name, quote_text,
                        verified_status, verified_by, content_hash
                    ) VALUES (501, ?, 'Public Member', 'A verified quote.',
                              'verified', 'poisoned quote actor', 'hash-501')
                    """,
                    (member_id,),
                )
                conn.execute(
                    """
                    INSERT INTO member_quotes (
                        member_id, meeting_id, quote_text, verified_status,
                        verified_by
                    ) VALUES (?, 501, 'A legacy quote.', 'verified',
                              'poisoned legacy actor')
                    """,
                    (member_id,),
                )
                if approved_at is not None:
                    conn.execute(
                        """
                        INSERT INTO work_orders (meeting_id, state, approved_at)
                        VALUES (?, ?, ?)
                        """,
                        (501, "completed", approved_at),
                    )
                conn.commit()
            finally:
                conn.close()

    def _gather(self) -> dict:
        with (
            mock.patch.object(database, "DB_PATH", self.source_db),
            mock.patch.object(flagship_sync, "_gather_preview_sidecars", return_value={}),
        ):
            return flagship_sync.gather_meeting_payload(501)

    def _apply(self, payload: dict) -> dict:
        with mock.patch.object(database, "DB_PATH", self.receiver_db):
            return flagship_sync.apply_meeting_payload(payload)

    def _receiver_rows(self):
        with mock.patch.object(database, "DB_PATH", self.receiver_db):
            conn = database.get_connection()
            try:
                meeting = conn.execute(
                    "SELECT is_published, published_by FROM meetings WHERE id = 501"
                ).fetchone()
                approvals = conn.execute(
                    "SELECT approved_at FROM work_orders WHERE meeting_id = 501"
                ).fetchall()
                return meeting, approvals
            finally:
                conn.close()

    def test_approved_publication_round_trips(self):
        self._seed_source(is_published=True, approved_at=self.APPROVED_AT)

        payload = self._gather()
        result = self._apply(payload)
        meeting, approvals = self._receiver_rows()

        self.assertEqual(payload["approval"], {"approved_at": self.APPROVED_AT})
        self.assertEqual(payload["meeting"]["published_by"], "Z-SPAN")
        self.assertEqual(payload["quotes"][0]["verified_by"], "Z-SPAN")
        self.assertEqual(payload["member_quotes_legacy"][0]["verified_by"], "Z-SPAN")
        self.assertTrue(result["approval_copied"])
        self.assertEqual(meeting["is_published"], 1)
        self.assertEqual([row["approved_at"] for row in approvals], [self.APPROVED_AT])

    def test_draft_publication_round_trips_without_approval(self):
        self._seed_source(is_published=False, approved_at=None)

        payload = self._gather()
        result = self._apply(payload)
        meeting, approvals = self._receiver_rows()

        self.assertEqual(payload["approval"], {"approved_at": None})
        self.assertFalse(result["approval_copied"])
        self.assertEqual(meeting["is_published"], 0)
        self.assertEqual(approvals, [])

    def test_sender_rejects_published_meeting_without_approval(self):
        self._seed_source(is_published=True, approved_at=None)

        with self.assertRaisesRegex(
            ValueError,
            "is_published is true but work-order approved_at is absent",
        ):
            self._gather()

    def test_repush_is_idempotent(self):
        self._seed_source(is_published=True, approved_at=self.APPROVED_AT)
        payload = self._gather()

        self._apply(payload)
        self._apply(payload)
        meeting, approvals = self._receiver_rows()

        self.assertEqual(meeting["is_published"], 1)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["approved_at"], self.APPROVED_AT)

    def test_notification_failures_do_not_fail_committed_sync(self):
        self._seed_source(is_published=True, approved_at=self.APPROVED_AT)
        payload = self._gather()

        with (
            mock.patch.object(
                flagship_sync.notification_pipeline,
                "recompute_meeting_topic_tags",
                side_effect=RuntimeError("classification unavailable"),
            ),
            mock.patch.object(
                flagship_sync.resend_adapter,
                "drain_notification_outbox",
                side_effect=RuntimeError("resend unavailable"),
            ),
            self.assertLogs(flagship_sync.logger, level="ERROR"),
        ):
            result = self._apply(payload)

        meeting, approvals = self._receiver_rows()
        self.assertEqual(meeting["is_published"], 1)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(
            result["notify_skipped_reason"],
            "notification_pipeline_error",
        )
        self.assertEqual(result["notify_drain"]["error"], "drain_failed")

    def test_void_state_round_trips_without_old_payload_resurrection(self):
        self._seed_source(is_published=False, approved_at=None)
        with mock.patch.object(database, "DB_PATH", self.source_db):
            conn = database.get_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO notebook_outputs (
                        meeting_id, notebook_id, output_type, content,
                        voided_at, voided_by
                    ) VALUES (
                        501, 'source-notebook', 'synopsis', 'Stored synopsis',
                        '2026-07-24 08:15:00', 'owner@example.test'
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

        payload = self._gather()
        synopsis = next(
            output
            for output in payload["outputs"]
            if output["output_type"] == "synopsis"
        )
        self.assertEqual(synopsis["voided_at"], "2026-07-24 08:15:00")
        self.assertEqual(synopsis["voided_by"], "owner@example.test")
        self._apply(payload)

        legacy_payload = copy.deepcopy(payload)
        legacy_synopsis = next(
            output
            for output in legacy_payload["outputs"]
            if output["output_type"] == "synopsis"
        )
        legacy_synopsis.pop("voided_at")
        legacy_synopsis.pop("voided_by")
        legacy_synopsis["content"] = "Updated by an older sender"
        self._apply(legacy_payload)

        with mock.patch.object(database, "DB_PATH", self.receiver_db):
            conn = database.get_connection()
            try:
                receiver_output = conn.execute(
                    """
                    SELECT content, voided_at, voided_by
                    FROM notebook_outputs
                    WHERE meeting_id = 501 AND output_type = 'synopsis'
                    """
                ).fetchone()
            finally:
                conn.close()
        self.assertEqual(receiver_output["content"], "Updated by an older sender")
        self.assertEqual(receiver_output["voided_at"], "2026-07-24 08:15:00")
        self.assertEqual(receiver_output["voided_by"], "owner@example.test")

        restored_payload = copy.deepcopy(payload)
        restored_synopsis = next(
            output
            for output in restored_payload["outputs"]
            if output["output_type"] == "synopsis"
        )
        restored_synopsis["voided_at"] = None
        restored_synopsis["voided_by"] = None
        self._apply(restored_payload)
        with mock.patch.object(database, "DB_PATH", self.receiver_db):
            conn = database.get_connection()
            try:
                restored = conn.execute(
                    """
                    SELECT voided_at, voided_by FROM notebook_outputs
                    WHERE meeting_id = 501 AND output_type = 'synopsis'
                    """
                ).fetchone()
            finally:
                conn.close()
        self.assertIsNone(restored["voided_at"])
        self.assertIsNone(restored["voided_by"])

    def test_receiver_coerces_poisoned_role_identities(self):
        self._seed_source(is_published=True, approved_at=self.APPROVED_AT)
        payload = self._gather()
        poisoned = copy.deepcopy(payload)
        poisoned["meeting"]["published_by"] = "receiver@example.test"
        poisoned["quotes"][0]["verified_by"] = "poisoned receiver quote"
        poisoned["member_quotes_legacy"][0]["verified_by"] = "poisoned receiver legacy"

        self._apply(poisoned)
        with mock.patch.object(database, "DB_PATH", self.receiver_db):
            conn = database.get_connection()
            try:
                meeting_actor = conn.execute(
                    "SELECT published_by FROM meetings WHERE id = 501"
                ).fetchone()["published_by"]
                quote_actor = conn.execute(
                    "SELECT verified_by FROM quotes WHERE meeting_id = 501"
                ).fetchone()["verified_by"]
                legacy_actor = conn.execute(
                    "SELECT verified_by FROM member_quotes WHERE meeting_id = 501"
                ).fetchone()["verified_by"]
            finally:
                conn.close()
        self.assertEqual(meeting_actor, "Z-SPAN")
        self.assertEqual(quote_actor, "Z-SPAN")
        self.assertEqual(legacy_actor, "Z-SPAN")

    def test_sender_and_receiver_reject_unsafe_notes(self):
        self._seed_source(is_published=True, approved_at=self.APPROVED_AT)
        with mock.patch.object(database, "DB_PATH", self.source_db):
            conn = database.get_connection()
            try:
                conn.execute(
                    "UPDATE meetings SET publish_notes = ? WHERE id = 501",
                    ("contact unsafe@example.test",),
                )
                conn.commit()
            finally:
                conn.close()
        with self.assertRaisesRegex(ValueError, "publish_notes contains an email"):
            self._gather()

        with mock.patch.object(database, "DB_PATH", self.source_db):
            conn = database.get_connection()
            try:
                conn.execute(
                    "UPDATE meetings SET publish_notes = 'safe again' WHERE id = 501"
                )
                conn.commit()
            finally:
                conn.close()
        payload = self._gather()
        payload["meeting"]["publish_notes"] = "contact unsafe@example.test"
        with self.assertRaisesRegex(ValueError, "publish_notes contains an email"):
            self._apply(payload)


class TestPublishStateGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "receiver.db")
        with mock.patch.object(database, "DB_PATH", self.db_path):
            database.init_db()

    def _seed_meeting(
        self,
        *,
        is_published: int,
        published_at: str | None = None,
        published_by: str | None = None,
        publish_notes: str | None = None,
    ) -> None:
        with mock.patch.object(database, "DB_PATH", self.db_path):
            conn = database.get_connection()
            try:
                city_id = conn.execute(
                    "INSERT INTO cities (name, county, state) VALUES (?, ?, ?)",
                    ("Testville", "Test County", "Arizona"),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO meetings (
                        id, city_id, city_name, county, state, meeting_title,
                        meeting_date, meeting_status, summary, is_published,
                        published_at, published_by, publish_notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        501,
                        city_id,
                        "Testville",
                        "Test County",
                        "Arizona",
                        "Regular Council Meeting",
                        "2026-07-27",
                        "Scheduled",
                        "Old summary",
                        is_published,
                        published_at,
                        published_by,
                        publish_notes,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def _upsert_and_fetch(self, **publish_fields):
        payload = {
            "id": 501,
            "city_name": "Testville",
            "county": "Test County",
            "state": "Arizona",
            "meeting_title": "Regular Council Meeting",
            "meeting_date": "2026-07-27",
            "summary": "Updated summary",
            **publish_fields,
        }
        with mock.patch.object(database, "DB_PATH", self.db_path):
            database.upsert_meeting_from_flagship_payload(payload)
            conn = database.get_connection()
            try:
                return conn.execute(
                    """
                    SELECT summary, is_published, published_at, published_by,
                           publish_notes
                    FROM meetings WHERE id = 501
                    """
                ).fetchone()
            finally:
                conn.close()

    def test_sync_never_downgrades_published_row(self):
        self._seed_meeting(
            is_published=1,
            published_at="2026-07-23 00:00:00",
            published_by="Z-SPAN",
            publish_notes="prod publish",
        )

        row = self._upsert_and_fetch(
            is_published=0,
            published_at=None,
            published_by=None,
            publish_notes=None,
        )

        self.assertEqual(row["summary"], "Updated summary")
        self.assertEqual(row["is_published"], 1)
        self.assertEqual(row["published_at"], "2026-07-23 00:00:00")
        self.assertEqual(row["published_by"], "Z-SPAN")
        self.assertEqual(row["publish_notes"], "prod publish")

    def test_sync_updates_unpublished_row_normally(self):
        self._seed_meeting(is_published=0, publish_notes="old note")

        row = self._upsert_and_fetch(
            is_published=0,
            published_at=None,
            published_by=None,
            publish_notes=None,
        )

        self.assertEqual(row["summary"], "Updated summary")
        self.assertEqual(row["is_published"], 0)
        self.assertIsNone(row["publish_notes"])

    def test_sync_promotion_path_still_works(self):
        self._seed_meeting(is_published=0)

        row = self._upsert_and_fetch(
            is_published=1,
            published_at="2026-07-27 01:00:00",
            published_by="Z-SPAN",
            publish_notes="ok",
        )

        self.assertEqual(row["is_published"], 1)
        self.assertEqual(row["published_at"], "2026-07-27 01:00:00")
        self.assertEqual(row["published_by"], "Z-SPAN")
        self.assertEqual(row["publish_notes"], "ok")


if __name__ == "__main__":
    unittest.main()

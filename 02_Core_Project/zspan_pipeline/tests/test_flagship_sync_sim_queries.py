"""Flagship sync contract for atomic signed-out sim-query generations."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_CORE_PROJECT_DIR = Path(__file__).resolve().parents[2]
_COUNCIL_NAVIGATOR_DIR = _CORE_PROJECT_DIR / "council_navigator"
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_CORE_PROJECT_DIR, _COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database

sys.modules["database"] = database

from parsers import flagship_sync


class FlagshipSyncSimQueryTests(unittest.TestCase):
    SOURCE_MEETING_ID = 501
    RECEIVER_MEETING_ID = 900

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.source_db = str(root / "source.db")
        self.receiver_db = str(root / "receiver.db")
        self._init_db(self.source_db)
        self._init_db(self.receiver_db)
        self._seed_meeting(self.source_db, self.SOURCE_MEETING_ID)
        self._seed_meeting(self.receiver_db, self.RECEIVER_MEETING_ID)
        self._insert_sim_queries(self.source_db, self.SOURCE_MEETING_ID)

    @staticmethod
    def _init_db(path: str) -> None:
        with mock.patch.object(database, "DB_PATH", path):
            database.init_db()

    @staticmethod
    def _seed_meeting(path: str, meeting_id: int) -> None:
        with mock.patch.object(database, "DB_PATH", path):
            conn = database.get_connection()
            try:
                city_id = conn.execute(
                    """
                    INSERT INTO cities (name, county, state)
                    VALUES ('Syncville', 'Test County', 'Arizona')
                    """
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO meetings (
                        id, city_id, city_name, county, state, meeting_title,
                        meeting_date, meeting_status, is_published
                    ) VALUES (?, ?, 'Syncville', 'Test County', 'Arizona',
                              'Regular Council Meeting', '2026-07-31',
                              'Minutes Available', 0)
                    """,
                    (meeting_id, city_id),
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _wire_rows(prefix: str = "Original") -> list[dict]:
        rows = []
        for slot in range(3):
            question = f"{prefix} question {slot + 1}?"
            answer = f"{prefix} answer {slot + 1}. [00:12]"
            rows.append({
                "query_slot": slot,
                "question_text": question,
                "answer_text": answer,
                "prompt_name": "sim_query_answer",
                "prompt_version": "v1-2026-07-31",
                "prompt_hash": "a" * 64,
                "vocab_version": "v1-2026-07-31",
                "query_hash": hashlib.sha256(
                    question.encode("utf-8")
                ).hexdigest(),
                "answer_digest": hashlib.sha256(
                    answer.encode("utf-8")
                ).hexdigest(),
                "model_id": "claude-sonnet-4-6",
                "retrieved_chunk_ids": json.dumps([slot + 20]),
                "run_id": "00000000-0000-4000-8000-000000000501",
                "generated_at": "2026-07-31T20:00:00Z",
            })
        return rows

    def _insert_sim_queries(self, path: str, meeting_id: int) -> None:
        rows = self._wire_rows()
        with mock.patch.object(database, "DB_PATH", path):
            conn = database.get_connection()
            try:
                conn.executemany(
                    f"""
                    INSERT INTO episode_sim_queries (
                        meeting_id, {', '.join(flagship_sync.SIM_QUERY_WIRE_FIELDS)}
                    ) VALUES (
                        {', '.join('?' for _ in range(len(flagship_sync.SIM_QUERY_WIRE_FIELDS) + 1))}
                    )
                    """,
                    [
                        (
                            meeting_id,
                            *(row[field] for field in flagship_sync.SIM_QUERY_WIRE_FIELDS),
                        )
                        for row in rows
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def _gather(self) -> dict:
        with (
            mock.patch.object(database, "DB_PATH", self.source_db),
            mock.patch.object(
                flagship_sync,
                "_gather_preview_sidecars",
                return_value={},
            ),
        ):
            return flagship_sync.gather_meeting_payload(self.SOURCE_MEETING_ID)

    def _apply(self, payload: dict) -> dict:
        with (
            mock.patch.object(database, "DB_PATH", self.receiver_db),
            mock.patch.object(
                flagship_sync.notification_pipeline,
                "recompute_meeting_topic_tags",
                return_value=[],
            ),
            mock.patch.object(
                flagship_sync.notification_pipeline,
                "enqueue_published_meeting_notifications",
                return_value={
                    "enqueued": False,
                    "recipient_count": 0,
                    "skipped_reason": "not_public",
                },
            ),
            mock.patch.object(
                flagship_sync.resend_adapter,
                "drain_notification_outbox",
                return_value={
                    "attempted": 0,
                    "sent": 0,
                    "failed": 0,
                    "skipped_no_api_key": True,
                },
            ),
        ):
            return flagship_sync.apply_meeting_payload(payload)

    def _receiver_rows(self) -> list[dict]:
        with mock.patch.object(database, "DB_PATH", self.receiver_db):
            conn = database.get_connection()
            try:
                return [dict(row) for row in conn.execute(
                    f"""
                    SELECT meeting_id, {', '.join(flagship_sync.SIM_QUERY_WIRE_FIELDS)}
                    FROM episode_sim_queries
                    WHERE meeting_id = ? ORDER BY query_slot
                    """,
                    (self.RECEIVER_MEETING_ID,),
                ).fetchall()]
            finally:
                conn.close()

    def test_sender_includes_complete_triplet_and_provenance(self) -> None:
        payload = self._gather()

        self.assertEqual(flagship_sync.PAYLOAD_SCHEMA_VERSION, 1)
        self.assertEqual(len(payload["sim_queries"]), 3)
        self.assertEqual(
            [item["query_slot"] for item in payload["sim_queries"]],
            [0, 1, 2],
        )
        for item in payload["sim_queries"]:
            self.assertEqual(set(item), set(flagship_sync.SIM_QUERY_WIRE_FIELDS))
            self.assertNotIn("meeting_id", item)
            for field in flagship_sync.SIM_QUERY_WIRE_FIELDS:
                self.assertIsNotNone(item[field])

    def test_sender_omits_incomplete_triplet_loudly(self) -> None:
        with mock.patch.object(database, "DB_PATH", self.source_db):
            conn = database.get_connection()
            try:
                conn.execute(
                    """
                    DELETE FROM episode_sim_queries
                    WHERE meeting_id = ? AND query_slot = 2
                    """,
                    (self.SOURCE_MEETING_ID,),
                )
                conn.commit()
            finally:
                conn.close()

        with self.assertLogs(flagship_sync.logger, level="ERROR") as captured:
            payload = self._gather()

        self.assertNotIn("sim_queries", payload)
        self.assertIn("expected 3 sim-query rows", "\n".join(captured.output))

    def test_receiver_uses_resolved_id_and_replay_is_idempotent(self) -> None:
        payload = self._gather()

        first = self._apply(payload)
        second = self._apply(payload)
        rows = self._receiver_rows()

        self.assertEqual(first["meeting_id"], self.RECEIVER_MEETING_ID)
        self.assertEqual(first["sim_queries_upserted"], 3)
        self.assertEqual(second["sim_queries_upserted"], 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            {row["meeting_id"] for row in rows},
            {self.RECEIVER_MEETING_ID},
        )
        self.assertEqual(
            [row["question_text"] for row in rows],
            [row["question_text"] for row in self._wire_rows()],
        )

    def test_absent_key_preserves_receiver_rows(self) -> None:
        payload = self._gather()
        self._apply(payload)
        old_sender_payload = copy.deepcopy(payload)
        old_sender_payload.pop("sim_queries")

        result = self._apply(old_sender_payload)

        self.assertIsNone(result["sim_queries_upserted"])
        self.assertEqual(len(self._receiver_rows()), 3)

    def test_explicit_empty_list_clears_receiver_rows(self) -> None:
        payload = self._gather()
        self._apply(payload)
        clear_payload = copy.deepcopy(payload)
        clear_payload["sim_queries"] = []

        result = self._apply(clear_payload)

        self.assertEqual(result["sim_queries_upserted"], 0)
        self.assertEqual(self._receiver_rows(), [])

    def test_invalid_incoming_triplet_preserves_complete_generation(self) -> None:
        payload = self._gather()
        self._apply(payload)
        before = self._receiver_rows()
        invalid = copy.deepcopy(payload)
        invalid["sim_queries"].pop()

        with self.assertRaisesRegex(ValueError, "expected 3 sim-query rows"):
            self._apply(invalid)

        self.assertEqual(self._receiver_rows(), before)

    def test_date_only_generated_at_is_rejected(self) -> None:
        rows = self._wire_rows()
        for row in rows:
            row["generated_at"] = "2026-07-31Z"

        with self.assertRaisesRegex(ValueError, "invalid generated_at"):
            flagship_sync._normalize_sim_query_payload(rows)

    def test_replace_transaction_rolls_back_on_insert_failure(self) -> None:
        payload = self._gather()
        self._apply(payload)
        before = self._receiver_rows()
        replacement = self._wire_rows("Replacement")

        with mock.patch.object(database, "DB_PATH", self.receiver_db):
            conn = database.get_connection()
            try:
                conn.execute(
                    """
                    CREATE TRIGGER reject_replacement_sim_query
                    BEFORE INSERT ON episode_sim_queries
                    WHEN NEW.answer_text LIKE 'Replacement%'
                    BEGIN
                        SELECT RAISE(ABORT, 'injected sim-query failure');
                    END
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaises(sqlite3.IntegrityError):
                flagship_sync._replace_sim_queries_for_meeting(
                    self.RECEIVER_MEETING_ID,
                    replacement,
                )

        self.assertEqual(self._receiver_rows(), before)


if __name__ == "__main__":
    unittest.main()

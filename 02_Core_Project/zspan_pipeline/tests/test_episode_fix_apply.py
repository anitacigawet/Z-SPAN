"""Offline tests for operator-gated episode audit fix application."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_IMPORT_TEMP_DIR = tempfile.TemporaryDirectory()
atexit.register(_IMPORT_TEMP_DIR.cleanup)
_IMPORT_DB_PATH = Path(_IMPORT_TEMP_DIR.name) / "import-isolation.db"
with mock.patch.dict(os.environ, {"ZSPAN_DB_PATH": str(_IMPORT_DB_PATH)}):
    from zspan_pipeline import episode_auditor as auditor
    import database
    from zspan_pipeline import episode_fix_apply as fixer


class EpisodeFixApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "fix-apply.db"
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE meetings (
                    id INTEGER PRIMARY KEY,
                    city_name TEXT NOT NULL,
                    is_published INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE notebook_outputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id INTEGER NOT NULL,
                    output_type TEXT NOT NULL,
                    content TEXT NOT NULL
                );
                INSERT INTO meetings (id, city_name, is_published)
                VALUES (7, 'Mesa', 1);
                """
            )

        self.database_connection_patch = mock.patch.object(
            database,
            "get_connection",
            side_effect=self._connect,
        )
        self.fixer_connection_patch = mock.patch.object(
            fixer,
            "get_connection",
            side_effect=self._connect,
        )
        self.database_connection_patch.start()
        self.fixer_connection_patch.start()
        database.init_episode_audit_runs_schema()
        database.init_episode_audit_fix_events_schema()

        self.load_patch = mock.patch.object(
            fixer,
            "load_audit_inputs",
            side_effect=self._load_inputs,
        )
        self.roster_patch = mock.patch.object(
            auditor,
            "_load_roster",
            return_value=[],
        )
        self.load_patch.start()
        self.roster_patch.start()

    def tearDown(self):
        self.roster_patch.stop()
        self.load_patch.stop()
        self.fixer_connection_patch.stop()
        self.database_connection_patch.stop()
        self.temp_dir.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_inputs(self, meeting_id: int) -> auditor.AuditInputs:
        with self._connect() as conn:
            meeting_row = conn.execute(
                "SELECT * FROM meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
            output_rows = conn.execute(
                """
                SELECT id, output_type, content
                FROM notebook_outputs
                WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchall()
        meeting = dict(meeting_row)
        outputs = {
            str(row["output_type"]): str(row["content"])
            for row in output_rows
        }
        row_ids = {
            str(row["output_type"]): row["id"]
            for row in output_rows
        }
        words = (
            {"word": "water", "start": 0.0, "end": 0.5},
            {"word": "contract", "start": 1.0, "end": 1.5},
            {"word": "adopted", "start": 2.0, "end": 3.0},
        )
        return auditor.AuditInputs(
            meeting_id=meeting_id,
            meeting=meeting,
            outputs=outputs,
            output_row_ids=row_ids,
            missing_outputs=(),
            transcript_words=words,
            outputs_snapshot_hash=auditor.compute_outputs_snapshot_hash(
                outputs
            ),
        )

    @staticmethod
    def _proposal(
        *,
        proposal_id: str = "p1.1",
        target_output: str = "synopsis",
        before: str = (
            "Council approved the water contract [at 0:00:01]."
        ),
        after: str = (
            "Council adopted the water contract [at 0:00:01]."
        ),
    ) -> dict:
        return {
            "id": proposal_id,
            "finding_number": 1,
            "target_output": target_output,
            "before": before,
            "after": after,
            "fix_rationale": "Evidence-supported correction.",
            "delimiters_ok": True,
            "parse_ok": True,
        }

    def _save_run(self, proposal: dict, run_id: str = "run-1") -> None:
        database.save_episode_audit_run(
            run_id=run_id,
            meeting_id=7,
            outputs_snapshot_hash="snapshot",
            auditor_version=auditor.AUDITOR_VERSION,
            prompt_sha256="prompt",
            model=auditor.AUDIT_MODEL_ID,
            effort="max",
            run_status="complete",
            verdict="catches",
            findings_count=1,
            open_findings_count=1,
            suggestions_count=1,
            deterministic_flags_count=0,
            report_json=json.dumps(
                {"llm": {"proposals": [proposal]}}
            ),
            started_at_utc="2026-07-28T00:00:00Z",
            duration_seconds=1.0,
        )

    def _insert_output(self, output_type: str, content: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notebook_outputs (
                    meeting_id, output_type, content
                ) VALUES (7, ?, ?)
                """,
                (output_type, content),
            )

    def _content(self, output_type: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT content
                FROM notebook_outputs
                WHERE meeting_id = 7 AND output_type = ?
                """,
                (output_type,),
            ).fetchone()
        return str(row["content"])

    def test_happy_apply_changes_content_and_records_hashes(self):
        proposal = self._proposal()
        self._save_run(proposal)
        self._insert_output("synopsis", proposal["before"])

        result = fixer.apply_fix(7, "run-1", "p1.1", "operator:james")

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["superseded_run_id"], "run-1")
        self.assertEqual(self._content("synopsis"), proposal["after"])
        expected_hash = hashlib.sha256(
            proposal["after"].encode("utf-8")
        ).hexdigest()
        self.assertEqual(result["post_content_sha256"], expected_hash)
        events = database.get_episode_audit_fix_events(7)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["disposition"], "applied")
        self.assertEqual(events[0]["post_content_sha256"], expected_hash)
        self.assertEqual(events[0]["was_published"], 1)
        self.assertTrue(events[0]["validation"]["validated"])

    def test_already_applied_is_idempotent(self):
        proposal = self._proposal()
        self._save_run(proposal)
        self._insert_output("synopsis", proposal["before"])
        first = fixer.apply_fix(7, "run-1", "p1.1", "operator:james")

        second = fixer.apply_fix(7, "run-1", "p1.1", "operator:james")

        self.assertEqual(first["status"], "applied")
        self.assertEqual(second, {"status": "already_applied"})
        self.assertEqual(
            len(database.get_episode_audit_fix_events(7)),
            1,
        )

    def test_structured_output_is_adapter_deferred_without_touching_rows(self):
        proposal = self._proposal(
            target_output="key_decisions",
            before="motion passed",
            after="motion was adopted",
        )
        self._save_run(proposal)
        self._insert_output("key_decisions", "motion passed")

        result = fixer.apply_fix(7, "run-1", "p1.1", "operator:james")

        self.assertEqual(result, {"status": "adapter_deferred"})
        self.assertEqual(self._content("key_decisions"), "motion passed")
        self.assertEqual(database.get_episode_audit_fix_events(7), [])

    def test_validation_failure_records_event_and_preserves_content(self):
        proposal = self._proposal(before="same", after="corrected")
        self._save_run(proposal)
        self._insert_output("synopsis", "same and same")

        result = fixer.apply_fix(7, "run-1", "p1.1", "operator:james")

        self.assertEqual(result["status"], "validation_failed")
        self.assertFalse(result["checks"]["before_unique"])
        self.assertEqual(self._content("synopsis"), "same and same")
        event = database.get_episode_audit_fix_events(7)[0]
        self.assertEqual(event["disposition"], "apply_failed")
        self.assertIn("before_not_unique", event["reason"])
        self.assertFalse(event["validation"]["validated"])

    def test_cas_conflict_records_failure_after_midflow_change(self):
        proposal = self._proposal()
        self._save_run(proposal)
        self._insert_output("synopsis", proposal["before"])
        real_validate = fixer.validate_single_proposal

        def mutate_after_validation(*args, **kwargs):
            validation = real_validate(*args, **kwargs)
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE notebook_outputs
                    SET content = 'rival content'
                    WHERE meeting_id = 7 AND output_type = 'synopsis'
                    """
                )
            return validation

        with mock.patch.object(
            fixer,
            "validate_single_proposal",
            side_effect=mutate_after_validation,
        ):
            result = fixer.apply_fix(
                7,
                "run-1",
                "p1.1",
                "operator:james",
            )

        self.assertEqual(result, {"status": "cas_conflict"})
        self.assertEqual(self._content("synopsis"), "rival content")
        event = database.get_episode_audit_fix_events(7)[0]
        self.assertEqual(event["disposition"], "apply_failed")
        self.assertEqual(event["reason"], "cas_conflict")

    def test_rejected_requires_reason(self):
        proposal = self._proposal()
        self._save_run(proposal)

        with self.assertRaisesRegex(ValueError, "requires a reason"):
            fixer.record_disposition(
                7,
                "run-1",
                "p1.1",
                "rejected",
                "operator:james",
            )
        self.assertEqual(database.get_episode_audit_fix_events(7), [])

    def test_deferred_event_lands_without_touching_content(self):
        proposal = self._proposal()
        self._save_run(proposal)
        self._insert_output("synopsis", proposal["before"])

        result = fixer.record_disposition(
            7,
            "run-1",
            "p1.1",
            "deferred",
            "operator:james",
        )

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(self._content("synopsis"), proposal["before"])
        event = database.get_episode_audit_fix_events(7)[0]
        self.assertEqual(event["event_id"], result["event_id"])
        self.assertEqual(event["disposition"], "deferred")

    def test_event_getter_orders_newest_and_tolerates_bad_json(self):
        base = {
            "meeting_id": 7,
            "run_id": "run-1",
            "proposal_id": "p1.1",
            "reason": None,
            "actor": "operator:james",
            "target_output": "synopsis",
            "before_text": "before",
            "after_text": "after",
            "pre_content_sha256": None,
            "post_content_sha256": None,
            "was_published": 0,
        }
        database.save_episode_audit_fix_event(
            **base,
            event_id="event-1",
            disposition="deferred",
            validation_json=json.dumps({"valid": True}),
        )
        database.save_episode_audit_fix_event(
            **base,
            event_id="event-2",
            disposition="rejected",
            validation_json="{not-json",
        )

        events = database.get_episode_audit_fix_events(7)

        self.assertEqual(
            [event["event_id"] for event in events],
            ["event-2", "event-1"],
        )
        self.assertIsNone(events[0]["validation"])
        self.assertEqual(events[0]["validation_json_raw"], "{not-json")
        self.assertEqual(events[1]["validation"], {"valid": True})


if __name__ == "__main__":
    unittest.main()

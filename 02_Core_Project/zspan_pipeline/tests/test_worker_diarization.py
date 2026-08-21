"""Worker ordering and durable-state tests for optional diarization."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zspan_pipeline import diarize_orchestrator, sidecar_pipeline, worker

import database


def _work_order() -> dict:
    return {
        "id": 42,
        "meeting_id": 100,
        "meeting_title": "Regular Meeting",
        "city_name": "Kingman",
        "meeting_date": "2026-07-19",
        "youtube_video_url": "https://www.youtube.com/watch?v=test",
        "requested_outputs": "synopsis",
    }


class TestWorkerDiarizationOrdering(unittest.IsolatedAsyncioTestCase):
    async def test_default_deferred_worker_completes_sidecars(self):
        status_calls: list[tuple[int, str, str | None]] = []
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("env_config.load_user_settings", return_value={}),
            mock.patch.object(worker, "is_transcription_ready_url", return_value=True),
            mock.patch.object(
                worker,
                "fetch_all_outputs",
                new=mock.AsyncMock(return_value=[{"status": "ok"}]),
            ),
            mock.patch.object(worker, "update_work_order_state"),
            mock.patch.object(
                worker,
                "update_meeting_diarization_status",
                side_effect=lambda meeting_id, status, detail: (
                    status_calls.append((meeting_id, status, detail)) or True
                ),
            ),
            mock.patch.object(sidecar_pipeline, "run_pipeline", return_value={}) as sidecar,
            mock.patch.object(
                diarize_orchestrator, "run_full_diarize_step"
            ) as diarize,
            mock.patch.object(worker, "_finalize_work_order", return_value="completed"),
        ):
            result = await worker._process_one(_work_order())

        self.assertEqual(result, "completed")
        sidecar.assert_called_once_with(100, "Kingman")
        diarize.assert_not_called()
        self.assertEqual(status_calls[0][:2], (100, "deferred"))
        self.assertIn("backfill", status_calls[0][2] or "")

    async def test_env_switch_runs_diarization_before_sidecars(self):
        order: list[str] = []
        status_calls: list[str] = []
        summary = {
            "diarize_skipped": False,
            "diarize_result": {
                "ok": True,
                "diarized_word_count": 12,
                "speaker_count": 2,
            },
        }

        def run_diarization(_meeting_id: int, _city: str) -> dict:
            order.append("diarization")
            return summary

        def run_sidecars(_meeting_id: int, _city: str) -> dict:
            order.append("sidecars")
            return {}

        with (
            mock.patch.dict(
                os.environ, {"ZSPAN_WORKER_DIARIZATION_ENABLED": "1"}, clear=False,
            ),
            mock.patch.object(worker, "is_transcription_ready_url", return_value=True),
            mock.patch.object(
                worker,
                "fetch_all_outputs",
                new=mock.AsyncMock(return_value=[{"status": "ok"}]),
            ),
            mock.patch.object(worker, "update_work_order_state"),
            mock.patch.object(
                worker,
                "update_meeting_diarization_status",
                side_effect=lambda _mid, status, _detail: (
                    status_calls.append(status) or True
                ),
            ),
            mock.patch.object(
                diarize_orchestrator,
                "run_full_diarize_step",
                side_effect=run_diarization,
            ) as diarize,
            mock.patch.object(sidecar_pipeline, "run_pipeline", side_effect=run_sidecars),
            mock.patch.object(worker, "_finalize_work_order", return_value="completed"),
        ):
            result = await worker._process_one(_work_order())

        self.assertEqual(result, "completed")
        diarize.assert_called_once_with(100, "Kingman")
        self.assertEqual(order, ["diarization", "sidecars"])
        self.assertEqual(status_calls, ["running", "succeeded"])

    async def test_enabled_diarization_failure_is_recorded_and_sidecars_continue(self):
        status_calls: list[str] = []
        failed_summary = {
            "diarize_skipped": False,
            "diarize_result": {
                "ok": False,
                "skipped_reason": "/diarize call failed: unavailable",
            },
        }
        with (
            mock.patch.dict(
                os.environ, {"ZSPAN_WORKER_DIARIZATION_ENABLED": "true"},
                clear=False,
            ),
            mock.patch.object(worker, "is_transcription_ready_url", return_value=True),
            mock.patch.object(
                worker,
                "fetch_all_outputs",
                new=mock.AsyncMock(return_value=[{"status": "ok"}]),
            ),
            mock.patch.object(worker, "update_work_order_state"),
            mock.patch.object(
                worker,
                "update_meeting_diarization_status",
                side_effect=lambda _mid, status, _detail: (
                    status_calls.append(status) or True
                ),
            ),
            mock.patch.object(
                diarize_orchestrator,
                "run_full_diarize_step",
                return_value=failed_summary,
            ),
            mock.patch.object(sidecar_pipeline, "run_pipeline", return_value={}) as sidecar,
            mock.patch.object(worker, "_finalize_work_order", return_value="completed"),
        ):
            result = await worker._process_one(_work_order())

        self.assertEqual(result, "completed")
        self.assertEqual(status_calls, ["running", "failed"])
        sidecar.assert_called_once_with(100, "Kingman")


class TestDiarizationDurability(unittest.TestCase):
    def test_deferral_is_persisted_without_failing_work_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE work_orders (
                    meeting_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    last_error TEXT,
                    diarization_status TEXT,
                    diarization_detail TEXT,
                    diarization_updated_at TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO work_orders (meeting_id, state) VALUES (100, 'completed')"
            )
            conn.commit()
            conn.close()

            def connect() -> sqlite3.Connection:
                return sqlite3.connect(db_path)

            with mock.patch.object(database, "get_connection", side_effect=connect):
                updated = database.update_meeting_diarization_status(
                    100, "deferred", "queued for backfill",
                )

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                """
                SELECT state, last_error, diarization_status, diarization_detail,
                       diarization_updated_at
                FROM work_orders WHERE meeting_id = 100
                """
            ).fetchone()
            conn.close()

        self.assertTrue(updated)
        self.assertEqual(row[:4], ("completed", None, "deferred", "queued for backfill"))
        self.assertIsNotNone(row[4])


class TestDiarizationIdempotency(unittest.TestCase):
    def test_already_diarized_meeting_is_not_rediarized(self):
        with (
            mock.patch.object(
                diarize_orchestrator, "_load_transcript_words_row",
                return_value={"provider": "mac_node"},
            ),
            mock.patch.object(
                diarize_orchestrator, "is_meeting_diarized", return_value=True,
            ),
            mock.patch.object(
                diarize_orchestrator, "diarize_and_save_transcript_words",
            ) as diarize,
            mock.patch.object(
                diarize_orchestrator, "_trigger_local_reindex",
            ) as reindex,
            mock.patch(
                "zspan_pipeline.cluster_roster_mapper.map_clusters_for_meeting",
                return_value={"mapped": 0},
            ),
        ):
            summary = diarize_orchestrator.run_full_diarize_step(100, "Kingman")

        diarize.assert_not_called()
        reindex.assert_not_called()
        self.assertTrue(summary["diarize_skipped"])
        self.assertEqual(
            diarize_orchestrator.classify_diarization_summary(summary)[0],
            "succeeded",
        )

    def test_assemblyai_provider_bypasses_cluster_name_mapper(self):
        with (
            mock.patch.object(
                diarize_orchestrator, "_load_transcript_words_row",
                return_value={
                    "provider": "assemblyai",
                    "words": [{
                        "word": "hello", "start": 0.0, "end": 0.5,
                        "speaker_id": "A",
                    }],
                },
            ),
            mock.patch.object(
                diarize_orchestrator, "is_meeting_diarized",
            ) as is_diarized,
            mock.patch.object(
                diarize_orchestrator, "diarize_and_save_transcript_words",
            ) as diarize,
            mock.patch.object(
                diarize_orchestrator, "_trigger_local_reindex",
            ) as reindex,
            mock.patch(
                "zspan_pipeline.cluster_roster_mapper.map_clusters_for_meeting",
            ) as mapper,
        ):
            summary = diarize_orchestrator.run_full_diarize_step(100, "Kingman")

        is_diarized.assert_not_called()
        diarize.assert_not_called()
        reindex.assert_not_called()
        mapper.assert_not_called()
        self.assertTrue(summary["diarize_skipped"])
        self.assertEqual(
            summary["mapper_summary"],
            {
                "skipped": True,
                "reason": "assemblyai anonymous speaker clusters are not name-resolved",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

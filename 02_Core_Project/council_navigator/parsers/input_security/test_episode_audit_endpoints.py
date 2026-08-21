"""Owner-gate and input-hardening tests for private episode-audit endpoints."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
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


class EpisodeAuditEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = mock.patch.object(
            database,
            "DB_PATH",
            str(Path(self.temp_dir.name) / "episode-audit.db"),
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def test_anonymous_requests_are_rejected_before_handler_work(self) -> None:
        with (
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=None,
            ),
            mock.patch.object(
                database,
                "get_latest_episode_audit_run",
            ) as get_latest,
        ):
            responses = (
                self.client.get("/api/episode-audit/123"),
                self.client.get(
                    "/api/episode-audit/summary?meeting_ids=123"
                ),
                self.client.post(
                    "/api/episode-audit/123/apply-fix",
                    json={"run_id": "run-1", "proposal_id": "proposal-1"},
                ),
                self.client.post(
                    "/api/episode-audit/123/disposition",
                    json={
                        "run_id": "run-1",
                        "proposal_id": "proposal-1",
                        "disposition": "deferred",
                    },
                ),
            )

        self.assertEqual(
            [response.status_code for response in responses],
            [401, 401, 401, 401],
        )
        get_latest.assert_not_called()

    def test_mutations_reject_missing_run_id(self) -> None:
        owner = mock.Mock(email="owner@example.com")
        with mock.patch.object(
            api_server,
            "_require_owner",
            return_value=(owner, None),
        ):
            responses = (
                self.client.post(
                    "/api/episode-audit/123/apply-fix",
                    json={"proposal_id": "proposal-1"},
                ),
                self.client.post(
                    "/api/episode-audit/123/disposition",
                    json={
                        "proposal_id": "proposal-1",
                        "disposition": "deferred",
                    },
                ),
            )

        self.assertEqual(
            [response.status_code for response in responses],
            [400, 400],
        )

    def test_disposition_rejects_applied(self) -> None:
        owner = mock.Mock(email="owner@example.com")
        with mock.patch.object(
            api_server,
            "_require_owner",
            return_value=(owner, None),
        ):
            response = self.client.post(
                "/api/episode-audit/123/disposition",
                json={
                    "run_id": "run-1",
                    "proposal_id": "proposal-1",
                    "disposition": "applied",
                },
            )

        self.assertEqual(response.status_code, 400)

    def test_rejected_disposition_requires_non_empty_reason(self) -> None:
        owner = mock.Mock(email="owner@example.com")
        with mock.patch.object(
            api_server,
            "_require_owner",
            return_value=(owner, None),
        ):
            response = self.client.post(
                "/api/episode-audit/123/disposition",
                json={
                    "run_id": "run-1",
                    "proposal_id": "proposal-1",
                    "disposition": "rejected",
                    "reason": "  ",
                },
            )

        self.assertEqual(response.status_code, 400)

    def test_mutations_reject_oversize_ids(self) -> None:
        owner = mock.Mock(email="owner@example.com")
        oversize = "x" * 201
        cases = (
            (
                "/api/episode-audit/123/apply-fix",
                {"run_id": oversize, "proposal_id": "proposal-1"},
            ),
            (
                "/api/episode-audit/123/apply-fix",
                {"run_id": "run-1", "proposal_id": oversize},
            ),
            (
                "/api/episode-audit/123/disposition",
                {
                    "run_id": oversize,
                    "proposal_id": "proposal-1",
                    "disposition": "deferred",
                },
            ),
            (
                "/api/episode-audit/123/disposition",
                {
                    "run_id": "run-1",
                    "proposal_id": oversize,
                    "disposition": "deferred",
                },
            ),
        )
        with mock.patch.object(
            api_server,
            "_require_owner",
            return_value=(owner, None),
        ):
            responses = [
                self.client.post(path, json=payload)
                for path, payload in cases
            ]

        self.assertEqual(
            [response.status_code for response in responses],
            [400, 400, 400, 400],
        )

    def test_apply_fix_maps_modeled_outcomes_and_forwards_actor(self) -> None:
        owner = mock.Mock(email="owner@example.com")
        cases = (
            (
                {
                    "status": "applied",
                    "event_id": "event-1",
                    "post_content_sha256": "abc123",
                    "superseded_run_id": "run-1",
                },
                200,
                {
                    "status": "applied",
                    "event_id": "event-1",
                    "post_content_sha256": "abc123",
                },
            ),
            (
                {"status": "already_applied"},
                200,
                {"status": "already_applied"},
            ),
            (
                {"status": "adapter_deferred"},
                409,
                {"status": "adapter_deferred"},
            ),
            (
                {"status": "validation_failed", "checks": {"exact": False}},
                422,
                {
                    "status": "validation_failed",
                    "checks": {"exact": False},
                },
            ),
            (
                {"status": "cas_conflict"},
                409,
                {"status": "cas_conflict"},
            ),
            (
                {"status": "not_found"},
                404,
                {"status": "not_found"},
            ),
        )
        for result, expected_status, expected_payload in cases:
            with (
                self.subTest(status=result["status"]),
                mock.patch.object(
                    api_server,
                    "_require_owner",
                    return_value=(owner, None),
                ),
                mock.patch(
                    "zspan_pipeline.episode_fix_apply.apply_fix",
                    return_value=result,
                ) as apply_fix,
            ):
                response = self.client.post(
                    "/api/episode-audit/123/apply-fix",
                    json={"run_id": "run-1", "proposal_id": "proposal-1"},
                )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.get_json(), expected_payload)
                apply_fix.assert_called_once_with(
                    123,
                    "run-1",
                    "proposal-1",
                    actor="owner@example.com",
                )

    def test_disposition_maps_outcomes_and_forwards_actor(self) -> None:
        owner = mock.Mock(email="owner@example.com")
        cases = (
            (
                {"status": "rejected", "event_id": "event-1"},
                200,
                {"status": "rejected", "event_id": "event-1"},
            ),
            (
                {"status": "not_found"},
                404,
                {"status": "not_found"},
            ),
        )
        for result, expected_status, expected_payload in cases:
            with (
                self.subTest(status=result["status"]),
                mock.patch.object(
                    api_server,
                    "_require_owner",
                    return_value=(owner, None),
                ),
                mock.patch(
                    "zspan_pipeline.episode_fix_apply.record_disposition",
                    return_value=result,
                ) as record_disposition,
            ):
                response = self.client.post(
                    "/api/episode-audit/123/disposition",
                    json={
                        "run_id": "run-1",
                        "proposal_id": "proposal-1",
                        "disposition": "rejected",
                        "reason": "operator reason",
                    },
                )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.get_json(), expected_payload)
                record_disposition.assert_called_once_with(
                    123,
                    "run-1",
                    "proposal-1",
                    "rejected",
                    actor="owner@example.com",
                    reason="operator reason",
                )

    def test_summary_rejects_non_integer_id(self) -> None:
        with mock.patch.object(
            api_server,
            "_require_owner",
            return_value=(object(), None),
        ):
            response = self.client.get(
                "/api/episode-audit/summary?meeting_ids=abc"
            )

        self.assertEqual(response.status_code, 400)

    def test_summary_rejects_more_than_200_ids(self) -> None:
        meeting_ids = ",".join(str(value) for value in range(201))
        with mock.patch.object(
            api_server,
            "_require_owner",
            return_value=(object(), None),
        ):
            response = self.client.get(
                f"/api/episode-audit/summary?meeting_ids={meeting_ids}"
            )

        self.assertEqual(response.status_code, 400)

    def test_unaudited_meeting_is_a_normal_empty_state(self) -> None:
        with (
            mock.patch.object(
                api_server,
                "_require_owner",
                return_value=(object(), None),
            ),
            mock.patch.object(
                database,
                "get_latest_episode_audit_run",
                return_value=None,
            ) as get_latest,
        ):
            response = self.client.get("/api/episode-audit/456")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"status": "none", "meeting_id": 456},
        )
        get_latest.assert_called_once_with(456)

    def test_full_read_drops_only_raw_report_json(self) -> None:
        run = {
            "id": "audit-1",
            "meeting_id": 789,
            "verdict": "review",
            "report_json": '{"private": "raw"}',
            "report": {"private": "parsed"},
            "report_json_raw": "visible corruption evidence",
        }
        with (
            mock.patch.object(
                api_server,
                "_require_owner",
                return_value=(object(), None),
            ),
            mock.patch.object(
                database,
                "get_latest_episode_audit_run",
                return_value=run,
            ),
        ):
            response = self.client.get("/api/episode-audit/789")

        self.assertEqual(response.status_code, 200)
        response_run = response.get_json()["run"]
        self.assertNotIn("report_json", response_run)
        self.assertEqual(response_run["report"], {"private": "parsed"})
        self.assertEqual(
            response_run["report_json_raw"],
            "visible corruption evidence",
        )

    def test_summary_returns_only_badge_fields_and_omits_missing_rows(self) -> None:
        run = {
            "verdict": "pass",
            "run_status": "completed",
            "findings_count": 4,
            "open_findings_count": 1,
            "suggestions_count": 2,
            "deterministic_flags_count": 3,
            "created_at": "2026-07-28T12:00:00Z",
            "report": {"private": "must not leak"},
        }
        with (
            mock.patch.object(
                api_server,
                "_require_owner",
                return_value=(object(), None),
            ),
            mock.patch.object(
                database,
                "get_latest_episode_audit_run",
                side_effect=[run, None],
            ),
        ):
            response = self.client.get(
                "/api/episode-audit/summary?meeting_ids=789,790"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("790", payload["audits"])
        self.assertEqual(
            payload["audits"]["789"],
            {
                "verdict": "pass",
                "run_status": "completed",
                "findings_count": 4,
                "open_findings_count": 1,
                "suggestions_count": 2,
                "deterministic_flags_count": 3,
                "created_at": "2026-07-28T12:00:00Z",
            },
        )


if __name__ == "__main__":
    unittest.main()

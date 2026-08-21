"""RR-8 Flask-backstop gating — the operator-read endpoints the hardening
pass found ungated must reject the anonymous caller (401) once the perimeter
narrows. In-process via app.test_client() — no live Flask, no network."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        mock.patch.object(database, "DB_PATH", str(Path(_import_temp_dir) / "import.db")),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server


# Newly-gated operator-read endpoints (RR-8 backstop sweep). Each must 401 the
# anonymous caller; the gate fires before any handler logic, so no DB seed is
# needed for the anonymous assertion.
_GATED_GET_ENDPOINTS = [
    "/api/vocabulary-inbox?city=Kingman",
    "/api/v1-launch/progress",
    "/api/orchestrator/autonomy",
    "/api/speaker-roster/pending-review",
    "/api/speaker-roster/meeting/1",
    "/api/speaker-roster/1/cluster-samples",
    "/api/llm-health",
    "/api/work-orders/1/flagship-sync-status",
]

_TEST_AGENT_TOKEN = "test-agent-token-abc123-def456"
_AGENT_PROPOSAL_ENDPOINTS = (
    (
        "/api/vocabulary-inbox/41/agent-propose",
        {"proposed_right": "Councilmember Smith"},
        "record_agent_counter_proposal",
        "correction",
    ),
    (
        "/api/disputed-quotes/42/agent-propose",
        {"proposed_quote_text": "A corrected quote."},
        "record_agent_quote_counter_proposal",
        "quote",
    ),
)


class BackstopGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = str(Path(self.temp_dir.name) / "gates.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def test_anonymous_is_401_on_every_gated_read(self):
        with mock.patch.object(api_server, "_current_user_from_cookie", return_value=None):
            for path in _GATED_GET_ENDPOINTS:
                with self.subTest(path=path):
                    resp = self.client.get(path)
                    self.assertEqual(
                        resp.status_code, 401,
                        f"{path} must reject the anonymous caller, got {resp.status_code}",
                    )

    def test_prompt_body_endpoint_refuses_anonymous(self):
        with mock.patch.object(api_server, "_current_user_from_cookie", return_value=None):
            response = self.client.get("/api/prompts/synopsis")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "sign-in required")

    def test_owner_is_not_401(self):
        """The gate lets the owner through — a non-401 (200/404/500 from the
        handler is fine; what matters is the gate doesn't block the owner)."""
        owner = SimpleNamespace(email="owner@example.com")
        with (
            mock.patch.object(api_server, "_current_user_from_cookie", return_value=owner),
            mock.patch.object(api_server, "is_owner_email", return_value=True),
        ):
            for path in _GATED_GET_ENDPOINTS:
                with self.subTest(path=path):
                    resp = self.client.get(path)
                    self.assertNotEqual(
                        resp.status_code, 401,
                        f"{path} must not 401 the owner",
                    )


class AgentProposalRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = str(Path(self.temp_dir.name) / "proposal-roles.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

        self.saved_agent_token = os.environ.get("ZSPAN_AGENT_STATE_TOKEN")
        os.environ["ZSPAN_AGENT_STATE_TOKEN"] = _TEST_AGENT_TOKEN
        self.addCleanup(self._restore_agent_token)

    def _restore_agent_token(self):
        if self.saved_agent_token is None:
            os.environ.pop("ZSPAN_AGENT_STATE_TOKEN", None)
        else:
            os.environ["ZSPAN_AGENT_STATE_TOKEN"] = self.saved_agent_token

    @staticmethod
    def _bearer_headers(role: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {_TEST_AGENT_TOKEN}"}
        if role is not None:
            headers["X-Zspan-Agent-Role"] = role
        return headers

    def test_unknown_bearer_role_is_400_without_mutation(self):
        with mock.patch.object(api_server, "_current_user_from_cookie", return_value=None):
            for path, base_payload, writer_name, _response_key in _AGENT_PROPOSAL_ENDPOINTS:
                for source in ("body", "header"):
                    with self.subTest(path=path, source=source):
                        payload = dict(base_payload)
                        headers = self._bearer_headers()
                        if source == "body":
                            payload["agent_role"] = "garbage-role"
                        else:
                            headers["X-Zspan-Agent-Role"] = "garbage-role"
                        with mock.patch.object(database, writer_name) as writer:
                            response = self.client.post(
                                path, json=payload, headers=headers
                            )
                        self.assertEqual(response.status_code, 400)
                        self.assertIn(
                            "agent_audit.KNOWN_ROLES",
                            response.get_json()["error"],
                        )
                        writer.assert_not_called()

    def test_bearer_claiming_operator_is_400_without_mutation(self):
        with mock.patch.object(api_server, "_current_user_from_cookie", return_value=None):
            for path, base_payload, writer_name, _response_key in _AGENT_PROPOSAL_ENDPOINTS:
                for source in ("body", "header"):
                    with self.subTest(path=path, source=source):
                        payload = dict(base_payload)
                        headers = self._bearer_headers()
                        if source == "body":
                            payload["agent_role"] = "operator"
                        else:
                            headers["X-Zspan-Agent-Role"] = "operator"
                        with mock.patch.object(database, writer_name) as writer:
                            response = self.client.post(
                                path, json=payload, headers=headers
                            )
                        self.assertEqual(response.status_code, 400)
                        writer.assert_not_called()

    def test_owner_is_stored_as_operator_regardless_of_body(self):
        owner = SimpleNamespace(email="owner@example.com")
        with (
            mock.patch.object(api_server, "_current_user_from_cookie", return_value=owner),
            mock.patch.object(api_server, "is_owner_email", return_value=True),
        ):
            for path, base_payload, writer_name, response_key in _AGENT_PROPOSAL_ENDPOINTS:
                with self.subTest(path=path):
                    payload = {**base_payload, "agent_role": "garbage-role"}
                    stored = {"id": 1, "agent_proposed_by": "operator"}
                    with mock.patch.object(
                        database, writer_name, return_value=stored
                    ) as writer:
                        response = self.client.post(path, json=payload)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json()[response_key], stored)
                    self.assertEqual(writer.call_args.kwargs["agent_role"], "operator")

    def test_known_role_end_to_end_still_works(self):
        correction = database.upsert_vocabulary_correction(
            "Kingman", "Counselor Smith", "Councilmember Smith"
        )
        path = f"/api/vocabulary-inbox/{correction['id']}/agent-propose"
        with mock.patch.object(api_server, "_current_user_from_cookie", return_value=None):
            response = self.client.post(
                path,
                json={
                    "proposed_right": "Council Member Smith",
                    "agent_role": "vocabulary-curator",
                },
                headers=self._bearer_headers("vocabulary-curator"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["correction"]["agent_proposed_by"],
            "vocabulary-curator",
        )

        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT agent_proposed_right, agent_proposed_by "
                "FROM city_vocabulary_corrections WHERE id = ?",
                (correction["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["agent_proposed_right"], "Council Member Smith")
        self.assertEqual(row["agent_proposed_by"], "vocabulary-curator")

    def test_known_header_role_is_used_when_body_role_is_absent(self):
        with (
            mock.patch.object(api_server, "_current_user_from_cookie", return_value=None),
            mock.patch.object(
                database,
                "record_agent_counter_proposal",
                return_value={"id": 41, "agent_proposed_by": "vocabulary-curator"},
            ) as writer,
        ):
            response = self.client.post(
                "/api/vocabulary-inbox/41/agent-propose",
                json={"proposed_right": "Councilmember Smith"},
                headers=self._bearer_headers("vocabulary-curator"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(writer.call_args.kwargs["agent_role"], "vocabulary-curator")

class PublishStatusGateTests(unittest.TestCase):
    """RR-8 draft-content gate on /api/meetings/<id>/publish-status: a
    publicly-visible meeting's status is public (BroadcastPage reads it); a
    draft's status + readiness internals are owner-only."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = str(Path(self.temp_dir.name) / "ps.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        conn = database.get_connection()
        try:
            city_id = conn.execute(
                "INSERT INTO cities (name, county, state) "
                "VALUES ('Kingman', 'Mohave', 'Arizona')"
            ).lastrowid
            # 201: published + approved work order → publicly visible.
            conn.execute(
                "INSERT INTO meetings (id, public_id, city_id, city_name, county, "
                "state, meeting_title, meeting_date, is_published) VALUES "
                "(201, ?, ?, 'Kingman', 'Mohave', 'Arizona', 'Visible', "
                "'2026-07-01', 1)",
                ("m_" + "P" * 22, city_id),
            )
            conn.execute(
                "INSERT INTO work_orders (meeting_id, state, approved_at) "
                "VALUES (201, 'completed', '2026-07-02 10:00:00')"
            )
            # 202: not published → draft.
            conn.execute(
                "INSERT INTO meetings (id, public_id, city_id, city_name, county, "
                "state, meeting_title, meeting_date, is_published) VALUES "
                "(202, ?, ?, 'Kingman', 'Mohave', 'Arizona', 'Draft', "
                "'2026-07-03', 0)",
                ("m_" + "Q" * 22, city_id),
            )
            conn.commit()
        finally:
            conn.close()
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def test_published_status_is_public(self):
        with mock.patch.object(api_server, "_current_user_from_cookie", return_value=None):
            resp = self.client.get("/api/meetings/201/publish-status")
        self.assertEqual(resp.status_code, 200)

    def test_draft_status_is_owner_only(self):
        with mock.patch.object(api_server, "_current_user_from_cookie", return_value=None):
            resp = self.client.get("/api/meetings/202/publish-status")
        self.assertEqual(resp.status_code, 401)

    def test_nonexistent_id_is_401_not_404_for_anon(self):
        # Gate-before-data: an anonymous caller must not be able to distinguish
        # a nonexistent meeting (would-be 404) from a protected draft (401).
        with mock.patch.object(api_server, "_current_user_from_cookie", return_value=None):
            resp = self.client.get("/api/meetings/999999/publish-status")
        self.assertEqual(resp.status_code, 401)

    def test_owner_sees_draft_status(self):
        owner = SimpleNamespace(email="owner@example.com")
        with (
            mock.patch.object(api_server, "_current_user_from_cookie", return_value=owner),
            mock.patch.object(api_server, "is_owner_email", return_value=True),
        ):
            resp = self.client.get("/api/meetings/202/publish-status")
        self.assertEqual(resp.status_code, 200)


# PipelineBlueprintGateTests retired with the scrape daemon per D-169 —
# the /api/pipeline/* blueprint that class tested no longer exists.


if __name__ == "__main__":
    unittest.main(verbosity=2)

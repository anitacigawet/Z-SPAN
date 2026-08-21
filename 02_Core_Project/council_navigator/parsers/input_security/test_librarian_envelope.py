"""Relay-envelope binding, migration, and concurrency tests."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
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
        mock.patch.object(
            database,
            "DB_PATH",
            str(Path(_import_temp_dir) / "import.db"),
        ),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server

from parsers import librarian_envelope
from zspan_pipeline import byok_relay, qdrant_synthesizer, rag_search


SYSTEM_FIXTURE = "System π\r\nline"
QUERY_FIXTURE = "What changed — exactly?\r\nNext"
EXPECTED_USER_MESSAGE = (
    "CURRENT QUESTION: What changed — exactly?\r\nNext\n\n"
    "RETRIEVED CONTEXT — chunks from meeting_id=42:\n---\n"
    "[chunk_index=7 timecode=00:01 start_seconds=1.2]\n"
    "Café line\r\n--- embedded delimiter\n\n"
    "[chunk_index=8 timecode=01:01 start_seconds=61.3]\n"
    "Second — chunk\n---"
)
EXPECTED_HASH = (
    "6824d45f403e724b714c9b98cccd11939dd389d177c4f97c15d37abb82ae4191"
)
EXPECTED_SYSTEM_BYTES = (
    b"System \xcf\x80\r\nline"
)
EXPECTED_USER_BYTES_HEX = (
    "43555252454e54205155455354494f4e3a2057686174206368616e67656420"
    "e280942065786163746c793f0d0a4e6578740a0a5245545249455645442043"
    "4f4e5445585420e28094206368756e6b732066726f6d206d656574696e675f"
    "69643d34323a0a2d2d2d0a5b6368756e6b5f696e6465783d372074696d65"
    "636f64653d30303a30312073746172745f7365636f6e64733d312e325d0a"
    "436166c3a9206c696e650d0a2d2d2d20656d6265646465642064656c696d"
    "697465720a0a5b6368756e6b5f696e6465783d382074696d65636f64653d"
    "30313a30312073746172745f7365636f6e64733d36312e335d0a5365636f"
    "6e6420e28094206368756e6b0a2d2d2d"
)


def _chunk(
    meeting_id: int,
    *,
    index: int = 7,
    start_seconds: float = 1.25,
    body: str = "Café line\r\n--- embedded delimiter",
) -> qdrant_synthesizer.RetrievedChunk:
    return qdrant_synthesizer.RetrievedChunk(
        score=0.9,
        body=body,
        chunk_index=index,
        start_seconds=start_seconds,
        end_seconds=start_seconds + 20,
        meeting_id=meeting_id,
        city="Test",
        county="Test",
        state="AZ",
    )


class LibrarianEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = mock.patch.object(
            database,
            "DB_PATH",
            str(Path(self.temp_dir.name) / "librarian-envelope.db"),
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        database.init_byok_audit_runs_schema()
        database.init_librarian_gate_events_schema()
        database.init_librarian_abuse_state_schema()
        database.update_librarian_policy(daily_query_cap=100)

        api_server.app.config.update(TESTING=True)
        api_server._reset_public_rate_limits_for_tests()
        self.client = api_server.app.test_client()
        self.reader_id = self._seed_user("reader@example.test")
        self.other_id = self._seed_user("other@example.test")
        self.owner_id = self._seed_user("owner@example.test")
        database.set_librarian_access(self.reader_id, "granted")
        database.set_librarian_access(self.other_id, "granted")
        self.next_meeting_id = 1000

    def tearDown(self) -> None:
        api_server._reset_public_rate_limits_for_tests()

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
                    email.split("@", 1)[0],
                    "",
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

    @contextmanager
    def _principal(
        self,
        user_id: int | None,
        *,
        owner: bool = False,
        chunks: list | None = None,
    ):
        user = self._user(user_id) if user_id is not None else None
        effective_chunks = chunks
        if effective_chunks is None:
            effective_chunks = [_chunk(self.next_meeting_id)]
        settings = {"librarian_daily_query_cap": 100}
        with (
            mock.patch.object(
                api_server,
                "load_user_settings",
                return_value=settings,
            ),
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
            mock.patch.object(
                database,
                "is_meeting_rag_indexed",
                return_value=True,
            ),
            mock.patch.object(
                rag_search,
                "load_prompt_template",
                return_value=SYSTEM_FIXTURE,
            ),
            mock.patch.object(
                qdrant_synthesizer,
                "retrieve_chunks",
                return_value=effective_chunks,
            ),
        ):
            yield

    def _new_envelope(
        self,
        *,
        user_id: int | None = None,
        owner: bool = False,
    ) -> dict:
        meeting_id = self.next_meeting_id
        self.next_meeting_id += 1
        chunks = [_chunk(meeting_id)]
        principal_id = (
            self.owner_id
            if owner
            else self.reader_id if user_id is None else user_id
        )
        with self._principal(
            principal_id,
            owner=owner,
            chunks=chunks,
        ):
            response = self.client.post(
                f"/api/rag-search/{meeting_id}",
                json={"query": "What changed?"},
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertIn("synthesis_envelope", payload)
        return payload["synthesis_envelope"]

    @staticmethod
    def _relay_body(
        envelope: dict,
        *,
        provider: str = "openai-gpt-4o-mini",
    ) -> dict:
        return {
            "provider": provider,
            "api_key": "sk-test-not-real",
            "model": "test-model",
            "system_prompt": envelope["system_prompt"],
            "user_message": envelope["user_message"],
            "envelope_version": envelope["envelope_version"],
            "run_id": envelope["run_id"],
            "max_tokens": 256,
            "temperature": 0.2,
        }

    def _attempts(self, run_id: str, user_id: int | None = None) -> int:
        conn = database.get_connection()
        try:
            row = conn.execute(
                """
                SELECT relay_attempt_count
                FROM librarian_gate_events
                WHERE user_id = ?
                  AND retrieval_run_id = ?
                  AND stencil_result = 'accepted'
                """,
                (
                    self.reader_id if user_id is None else user_id,
                    run_id,
                ),
            ).fetchone()
            if row is None:
                raise AssertionError(f"missing gate event for {run_id}")
            return int(row["relay_attempt_count"])
        finally:
            conn.close()

    def test_envelope_bytes_rounding_and_domain_hash_match_fixture(self):
        chunks = [
            {
                "chunk_index": 7,
                "start_seconds": 1.25,
                "body": "Café line\r\n--- embedded delimiter",
            },
            {
                "chunk_index": 8,
                "start_seconds": 61.26,
                "body": "Second — chunk",
            },
        ]
        with mock.patch.object(
            rag_search,
            "load_prompt_template",
            return_value=SYSTEM_FIXTURE,
        ):
            envelope = librarian_envelope.build_synthesis_envelope(
                42,
                QUERY_FIXTURE,
                chunks,
            )

        self.assertEqual(envelope["system_prompt"], SYSTEM_FIXTURE)
        self.assertEqual(envelope["user_message"], EXPECTED_USER_MESSAGE)
        self.assertEqual(
            envelope["system_prompt"].encode("utf-8"),
            EXPECTED_SYSTEM_BYTES,
        )
        self.assertEqual(
            envelope["user_message"].encode("utf-8").hex(),
            EXPECTED_USER_BYTES_HEX,
        )
        self.assertEqual(len(envelope["user_message"].encode("utf-8")), 259)
        self.assertEqual(envelope["envelope_hash"], EXPECTED_HASH)
        self.assertEqual(
            librarian_envelope.compute_envelope_hash(
                SYSTEM_FIXTURE,
                EXPECTED_USER_MESSAGE,
                "envelope-v1",
            ),
            EXPECTED_HASH,
        )
        self.assertNotEqual(
            librarian_envelope.compute_envelope_hash(
                SYSTEM_FIXTURE,
                EXPECTED_USER_MESSAGE,
                "envelope-v2",
            ),
            EXPECTED_HASH,
        )

    def test_nonfinite_or_negative_start_seconds_rejects_build(self):
        for value in (float("nan"), float("inf"), -0.1):
            with (
                self.subTest(value=value),
                mock.patch.object(
                    rag_search,
                    "load_prompt_template",
                    return_value=SYSTEM_FIXTURE,
                ),
            ):
                with self.assertRaises(ValueError):
                    librarian_envelope.build_synthesis_envelope(
                        42,
                        "What changed?",
                        [{
                            "chunk_index": 1,
                            "start_seconds": value,
                            "body": "body",
                        }],
                    )

    def test_run_ids_are_unique_and_partial_index_accepts_both(self):
        now = datetime(2026, 7, 29, 8, 30, 0, 123456, timezone.utc)
        query_digest = rag_search.query_hash("What changed?")
        first = rag_search.make_run_id(42, query_digest, now)
        second = rag_search.make_run_id(42, query_digest, now)
        self.assertNotEqual(first, second)
        for run_id in (first, second):
            lease = database.preflight_librarian_abuse_state(
                self.reader_id
            )
            conn = database.get_connection()
            try:
                admitted = database.evaluate_and_record_librarian_query(
                    conn,
                    user_id=self.reader_id,
                    meeting_id=42,
                    raw_query="What changed?",
                    expected_epoch=lease["expected_epoch"],
                    thresholds=database.get_librarian_policy_snapshot(),
                    stencil_verdict=api_server.evaluate_librarian_query(
                        "What changed?"
                    ),
                )
            finally:
                conn.close()
            self.assertIsInstance(admitted, database.AdmittedResult)
            claimed, reason = database.claim_librarian_retrieval(
                event_id=admitted.event_id,
                retrieval_run_id=run_id,
            )
            self.assertTrue(claimed, reason)

    def test_one_shot_then_stream_claims_two_attempts_and_third_denied(self):
        envelope = self._new_envelope()
        body = self._relay_body(envelope)
        with (
            self._principal(self.reader_id),
            mock.patch.object(
                byok_relay,
                "relay",
                return_value=(200, {"choices": []}),
            ) as one_shot,
            mock.patch.object(
                byok_relay,
                "relay_stream",
                return_value=iter([b"data: [DONE]\n\n"]),
            ) as stream,
        ):
            first = self.client.post("/api/byok/relay", json=body)
            second = self.client.post(
                "/api/byok/relay-stream",
                json=body,
            )
            second.get_data()
            third = self.client.post("/api/byok/relay", json=body)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 403)
        self.assertEqual(
            third.get_json()["error"]["type"],
            "attempts_exhausted",
        )
        self.assertEqual(self._attempts(envelope["run_id"]), 2)
        one_shot.assert_called_once()
        stream.assert_called_once()

    def test_tamper_rejected_without_attempt_and_internal_reason_is_specific(self):
        envelope = self._new_envelope()
        body = self._relay_body(envelope)
        body["user_message"] += "x"
        with self._principal(self.reader_id):
            response = self.client.post("/api/byok/relay", json=body)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["type"], "envelope_invalid")
        self.assertEqual(self._attempts(envelope["run_id"]), 0)

        fresh = self._new_envelope()
        ok, reason = librarian_envelope.consume_envelope_claim(
            self.reader_id,
            fresh["run_id"],
            fresh["system_prompt"],
            fresh["user_message"] + "x",
            fresh["envelope_version"],
            "openai-gpt-4o-mini",
        )
        self.assertFalse(ok)
        self.assertEqual(reason["reason"], "envelope_hash_mismatch")
        self.assertEqual(reason["type"], "envelope_invalid")

    def test_expired_unknown_and_cross_user_are_indistinguishable_as_required(self):
        expired = self._new_envelope()
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE librarian_gate_events
                SET envelope_expires_at = '2000-01-01 00:00:00'
                WHERE retrieval_run_id = ?
                """,
                (expired["run_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        with self._principal(self.reader_id):
            expired_response = self.client.post(
                "/api/byok/relay",
                json=self._relay_body(expired),
            )
        self.assertEqual(expired_response.status_code, 403)
        self.assertEqual(
            expired_response.get_json()["error"]["type"],
            "envelope_expired",
        )
        self.assertEqual(self._attempts(expired["run_id"]), 0)

        unknown = self._new_envelope()
        unknown_body = self._relay_body(unknown)
        unknown_body["run_id"] = "zspan-rag-unknown"
        with self._principal(self.reader_id):
            unknown_response = self.client.post(
                "/api/byok/relay",
                json=unknown_body,
            )
        self.assertEqual(unknown_response.status_code, 403)
        self.assertEqual(
            unknown_response.get_json()["error"]["type"],
            "envelope_invalid",
        )
        self.assertEqual(self._attempts(unknown["run_id"]), 0)

        other = self._new_envelope(user_id=self.other_id)
        with self._principal(self.reader_id):
            cross_user = self.client.post(
                "/api/byok/relay",
                json=self._relay_body(other),
            )
        self.assertEqual(cross_user.status_code, 403)
        self.assertEqual(cross_user.get_json()["error"]["type"], "envelope_invalid")
        self.assertEqual(self._attempts(other["run_id"], self.other_id), 0)

    def test_missing_version_and_unsupported_provider_do_not_claim(self):
        missing_run = self._new_envelope()
        missing_body = self._relay_body(missing_run)
        missing_body.pop("run_id")
        with self._principal(self.reader_id):
            response = self.client.post("/api/byok/relay", json=missing_body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._attempts(missing_run["run_id"]), 0)

        for malformed in ("", None, "envelope-v0"):
            envelope = self._new_envelope()
            body = self._relay_body(envelope)
            body["envelope_version"] = malformed
            with self._principal(self.reader_id):
                response = self.client.post("/api/byok/relay", json=body)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(
                response.get_json()["error"]["type"],
                "envelope_invalid",
            )
            self.assertEqual(self._attempts(envelope["run_id"]), 0)

        unsupported = self._new_envelope()
        with self._principal(self.reader_id):
            response = self.client.post(
                "/api/byok/relay",
                json=self._relay_body(
                    unsupported,
                    provider="google-gemini-2.5-flash",
                ),
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"]["type"],
            "unsupported_provider",
        )
        self.assertEqual(self._attempts(unsupported["run_id"]), 0)

    def test_rate_limit_denial_leaves_attempt_untouched(self):
        envelope = self._new_envelope()
        invalid = self._relay_body(envelope)
        invalid.pop("run_id")
        limit = api_server._PUBLIC_RATE_LIMITS["byok_relay"]
        with (
            self._principal(self.reader_id),
            mock.patch.object(
                api_server,
                "_public_rate_limit_now",
                return_value=100.0,
            ),
        ):
            for _ in range(limit):
                response = self.client.post("/api/byok/relay", json=invalid)
                self.assertEqual(response.status_code, 400)
            limited = self.client.post(
                "/api/byok/relay",
                json=self._relay_body(envelope),
            )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(self._attempts(envelope["run_id"]), 0)

    def test_provider_4xx_and_5xx_after_claim_each_consume_attempt(self):
        for status in (400, 500):
            envelope = self._new_envelope()
            with (
                self._principal(self.reader_id),
                mock.patch.object(
                    byok_relay,
                    "relay",
                    return_value=(
                        status,
                        {"error": {"message": "provider failed"}},
                    ),
                ),
            ):
                response = self.client.post(
                    "/api/byok/relay",
                    json=self._relay_body(envelope),
                )
            self.assertEqual(response.status_code, status)
            self.assertEqual(self._attempts(envelope["run_id"]), 1)

    def test_owner_gets_envelope_but_relay_remains_exempt(self):
        before = self._gate_event_count()
        envelope = self._new_envelope(owner=True)
        self.assertEqual(self._gate_event_count(), before)
        arbitrary = {
            "provider": "openai-gpt-4o-mini",
            "api_key": "sk-test-not-real",
            "model": "test-model",
            "system_prompt": "owner system",
            "user_message": "owner arbitrary body",
        }
        with (
            self._principal(self.owner_id, owner=True),
            mock.patch.object(
                byok_relay,
                "relay",
                return_value=(200, {"choices": []}),
            ) as dispatch,
        ):
            response = self.client.post("/api/byok/relay", json=arbitrary)
        self.assertEqual(response.status_code, 200)
        dispatch.assert_called_once()
        self.assertTrue(envelope["run_id"])

    def test_ban_and_cooldown_boundaries_revoke_older_envelopes(self):
        banned = self._new_envelope()
        database.set_librarian_access(self.reader_id, "banned")
        with self._principal(self.reader_id):
            response = self.client.post(
                "/api/byok/relay",
                json=self._relay_body(banned),
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["status"],
            "account_blocked",
        )
        self.assertEqual(self._attempts(banned["run_id"]), 0)

        database.set_librarian_access(self.reader_id, "granted")
        cooldown = self._new_envelope()
        policy = database.update_librarian_policy(
            reject_burst_threshold=4,
            reject_burst_window_seconds=600,
            reject_cooldown_seconds=600,
            reject_autoban_strike_threshold=3,
            reject_autoban_window_seconds=3600,
        )
        for index in range(4):
            lease = database.preflight_librarian_abuse_state(
                self.reader_id
            )
            conn = database.get_connection()
            try:
                database.evaluate_and_record_librarian_query(
                    conn,
                    user_id=self.reader_id,
                    meeting_id=900 + index,
                    raw_query=f"bad-{index}",
                    expected_epoch=lease["expected_epoch"],
                    thresholds=policy,
                    stencil_verdict=SimpleNamespace(
                        ok=False,
                        canonical_query=None,
                        reason_code="not_a_question",
                        message="rejected",
                        matched_rule_id=None,
                        gate_version="grammar-v2+stencil-v2",
                    ),
                )
            finally:
                conn.close()
        with self._principal(self.reader_id):
            response = self.client.post(
                "/api/byok/relay",
                json=self._relay_body(cooldown),
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["error"]["type"],
            "admission_state_changed",
        )
        self.assertEqual(self._attempts(cooldown["run_id"]), 0)

    def test_anonymous_relays_are_hard_locked(self):
        endpoints = (
            "/api/byok/relay",
            "/api/byok/relay-stream",
        )
        with self._principal(None):
            responses = [
                self.client.post(
                    endpoint,
                    json={
                        "provider": "openai-gpt-4o-mini",
                        "api_key": "sk-test-not-real",
                        "user_message": "arbitrary",
                    },
                )
                for endpoint in endpoints
            ]
        for endpoint, response in zip(endpoints, responses):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.get_json()["status"],
                    "sign_in_required",
                )

    def test_persistence_failure_preserves_quota_and_terminal_state(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """
                CREATE TRIGGER fail_envelope_update
                BEFORE UPDATE OF synthesis_envelope_hash
                ON librarian_gate_events
                BEGIN
                    SELECT RAISE(FAIL, 'simulated envelope failure');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

        meeting_id = self.next_meeting_id
        with self._principal(
            self.reader_id,
            chunks=[_chunk(meeting_id)],
        ):
            response = self.client.post(
                f"/api/rag-search/{meeting_id}",
                json={"query": "What changed?"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["error"]["type"],
            "envelope_persist_failed",
        )
        self.assertEqual(self._gate_event_count(), 1)
        conn = database.get_connection()
        try:
            row = conn.execute(
                """
                SELECT stencil_result, terminal_failure_reason,
                       terminal_failed_at
                FROM librarian_gate_events
                """
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["stencil_result"], "accepted")
        self.assertEqual(
            row["terminal_failure_reason"],
            "envelope_persist_failed",
        )
        self.assertIsNotNone(row["terminal_failed_at"])

    def test_retrieval_failure_preserves_accepted_quota_row(self):
        meeting_id = self.next_meeting_id
        with (
            self._principal(self.reader_id, chunks=[_chunk(meeting_id)]),
            mock.patch.object(
                qdrant_synthesizer,
                "retrieve_chunks",
                side_effect=RuntimeError("simulated retrieval failure"),
            ),
        ):
            response = self.client.post(
                f"/api/rag-search/{meeting_id}",
                json={"query": "What changed?"},
            )
        self.assertEqual(response.status_code, 502)
        conn = database.get_connection()
        try:
            row = conn.execute(
                """
                SELECT stencil_result, terminal_failure_reason
                FROM librarian_gate_events
                WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["stencil_result"], "accepted")
        self.assertEqual(
            row["terminal_failure_reason"],
            "retrieval_failed",
        )

    def test_three_concurrent_claims_admit_exactly_two(self):
        envelope = self._new_envelope()
        body = self._relay_body(envelope)
        ready = threading.Barrier(3)
        statuses: list[int] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def submit() -> None:
            try:
                ready.wait(timeout=5)
                with api_server.app.test_client() as client:
                    response = client.post("/api/byok/relay", json=body)
                with result_lock:
                    statuses.append(response.status_code)
            except BaseException as exc:
                with result_lock:
                    errors.append(exc)

        with (
            self._principal(self.reader_id),
            mock.patch.object(
                byok_relay,
                "relay",
                return_value=(200, {"choices": []}),
            ),
        ):
            threads = [threading.Thread(target=submit) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(statuses), [200, 200, 403])
        self.assertEqual(self._attempts(envelope["run_id"]), 2)

    def test_retrieval_result_is_suppressed_when_ban_wins_release(self):
        meeting_id = self.next_meeting_id

        def revoke_during_retrieval(*_args, **_kwargs):
            database.set_librarian_access(self.reader_id, "banned")
            return [_chunk(meeting_id)]

        with (
            self._principal(self.reader_id, chunks=[_chunk(meeting_id)]),
            mock.patch.object(
                qdrant_synthesizer,
                "retrieve_chunks",
                side_effect=revoke_during_retrieval,
            ),
        ):
            response = self.client.post(
                f"/api/rag-search/{meeting_id}",
                json={"query": "What changed?"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["status"],
            "admission_state_changed",
        )
        conn = database.get_connection()
        try:
            row = conn.execute(
                """
                SELECT terminal_failure_reason
                FROM librarian_gate_events
                WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            row["terminal_failure_reason"],
            "revoked_during_retrieval",
        )

    def test_one_shot_result_is_suppressed_when_ban_wins_release(self):
        envelope = self._new_envelope()

        def revoke_during_provider(**_kwargs):
            database.set_librarian_access(self.reader_id, "banned")
            return 200, {"choices": [{"message": {"content": "secret"}}]}

        with (
            self._principal(self.reader_id),
            mock.patch.object(
                byok_relay,
                "relay",
                side_effect=revoke_during_provider,
            ),
        ):
            response = self.client.post(
                "/api/byok/relay",
                json=self._relay_body(envelope),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["error"]["type"],
            "admission_state_changed",
        )
        self.assertNotIn("secret", response.get_data(as_text=True))

    def test_stream_claim_occurs_inside_generator_and_stale_claim_fails(
        self,
    ):
        envelope = self._new_envelope()
        body = self._relay_body(envelope)
        with (
            self._principal(self.reader_id),
            mock.patch.object(
                byok_relay,
                "relay_stream",
                return_value=iter([b"data: [DONE]\n\n"]),
            ) as provider_stream,
            api_server.app.test_request_context(
                "/api/byok/relay-stream",
                method="POST",
                json=body,
            ),
        ):
            response = api_server.api_byok_relay_stream()
            self.assertEqual(self._attempts(envelope["run_id"]), 0)
            database.set_librarian_access(self.reader_id, "banned")
            output = b"".join(response.response)

        self.assertIn(b"admission_state_changed", output)
        self.assertEqual(self._attempts(envelope["run_id"]), 0)
        provider_stream.assert_not_called()

    def test_already_claimed_stream_finishes_after_epoch_change(self):
        envelope = self._new_envelope()

        def stream_then_revoke(**_kwargs):
            yield b'data: {"delta":"first"}\n\n'
            database.set_librarian_access(self.reader_id, "banned")
            yield b'data: {"delta":"second"}\n\n'
            yield b"data: [DONE]\n\n"

        with (
            self._principal(self.reader_id),
            mock.patch.object(
                byok_relay,
                "relay_stream",
                side_effect=stream_then_revoke,
            ),
        ):
            response = self.client.post(
                "/api/byok/relay-stream",
                json=self._relay_body(envelope),
            )
            output = response.get_data()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"delta":"first"', output)
        self.assertIn(b'"delta":"second"', output)
        self.assertEqual(self._attempts(envelope["run_id"]), 1)

    def test_schema_init_is_idempotent_and_old_shape_migrates(self):
        database.init_librarian_gate_events_schema()
        database.init_librarian_gate_events_schema()
        conn = database.get_connection()
        try:
            names = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(librarian_gate_events)"
                )
            }
            indexes = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA index_list(librarian_gate_events)"
                )
            }
        finally:
            conn.close()
        self.assertTrue({
            "synthesis_envelope_hash",
            "envelope_version",
            "envelope_expires_at",
            "relay_attempt_count",
            "relay_started_at",
            "relay_provider",
            "enforcement_epoch_at_decision",
            "policy_revision",
            "terminal_failure_reason",
            "terminal_failed_at",
        }.issubset(names))
        self.assertIn("idx_lge_accepted_run", indexes)
        self.assertIn("idx_lge_terminal_failure", indexes)

        with tempfile.TemporaryDirectory() as migration_dir:
            old_path = str(Path(migration_dir) / "old.db")
            old = sqlite3.connect(old_path)
            try:
                old.execute(
                    """
                    CREATE TABLE librarian_gate_events (
                        event_id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        meeting_id INTEGER,
                        query_hash TEXT NOT NULL,
                        gate_version TEXT NOT NULL,
                        stencil_result TEXT NOT NULL,
                        reason_code TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                old.execute(
                    """
                    INSERT INTO librarian_gate_events (
                        event_id, user_id, meeting_id, query_hash,
                        gate_version, stencil_result
                    ) VALUES ('historical', 1, 42, 'hash', 'gate',
                              'accepted')
                    """
                )
                old.commit()
            finally:
                old.close()
            with mock.patch.object(database, "DB_PATH", old_path):
                database.init_librarian_gate_events_schema()
                database.init_librarian_gate_events_schema()
                migrated = database.get_connection()
                try:
                    migrated_names = {
                        row["name"]
                        for row in migrated.execute(
                            "PRAGMA table_info(librarian_gate_events)"
                        )
                    }
                    migrated_indexes = {
                        row["name"]
                        for row in migrated.execute(
                            "PRAGMA index_list(librarian_gate_events)"
                        )
                    }
                    relay_column = next(
                        row
                        for row in migrated.execute(
                            "PRAGMA table_info(librarian_gate_events)"
                        )
                        if row["name"] == "relay_attempt_count"
                    )
                    historical = migrated.execute(
                        """
                        SELECT enforcement_epoch_at_decision,
                               policy_revision
                        FROM librarian_gate_events
                        WHERE event_id = 'historical'
                        """
                    ).fetchone()
                finally:
                    migrated.close()
        self.assertTrue({
            "synthesis_envelope_hash",
            "envelope_version",
            "envelope_expires_at",
            "relay_attempt_count",
            "relay_started_at",
            "relay_provider",
            "enforcement_epoch_at_decision",
            "policy_revision",
            "terminal_failure_reason",
            "terminal_failed_at",
        }.issubset(migrated_names))
        self.assertEqual(relay_column["notnull"], 1)
        self.assertEqual(relay_column["dflt_value"], "0")
        self.assertIn("idx_lge_accepted_run", migrated_indexes)
        self.assertIn("idx_lge_terminal_failure", migrated_indexes)
        self.assertIsNone(historical["enforcement_epoch_at_decision"])
        self.assertIsNone(historical["policy_revision"])

    @staticmethod
    def _gate_event_count() -> int:
        conn = database.get_connection()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM librarian_gate_events"
                ).fetchone()[0]
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

"""Bounded reject-burst cooldown and Librarian auto-ban tests."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
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

from zspan_pipeline import rag_search


class LibrarianAbuseControlTests(unittest.TestCase):
    CONFIG = {
        "burst_threshold": 4,
        "burst_window_seconds": 600,
        "cooldown_seconds": 600,
        "strike_threshold": 3,
        "autoban_window_seconds": 3600,
    }

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = mock.patch.object(
            database,
            "DB_PATH",
            str(Path(self.temp_dir.name) / "librarian-abuse.db"),
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        database.init_librarian_gate_events_schema()
        database.init_librarian_abuse_state_schema()
        database.init_byok_audit_runs_schema()

        api_server.app.config.update(TESTING=True)
        with api_server._public_rate_limit_lock:
            api_server._public_rate_limit_buckets.clear()
        self.client = api_server.app.test_client()
        self.reader_id = self._seed_user("reader@example.test")
        database.set_librarian_access(self.reader_id, "granted")
        self._active_config = None

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

    @staticmethod
    def _rows(table: str):
        conn = database.get_connection()
        try:
            return conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
        finally:
            conn.close()

    def _state(self, user_id: int | None = None):
        conn = database.get_connection()
        try:
            return conn.execute(
                """
                SELECT *
                FROM librarian_abuse_state
                WHERE user_id = ?
                """,
                (self.reader_id if user_id is None else user_id,),
            ).fetchone()
        finally:
            conn.close()

    def _record(
        self,
        query: str,
        *,
        user_id: int | None = None,
        reason_code: str = "not_a_question",
        matched_rule_id: str | None = None,
        config: dict | None = None,
    ):
        target_user = self.reader_id if user_id is None else user_id
        effective = self.CONFIG if config is None else config
        self._set_policy(effective)
        lease = database.preflight_librarian_abuse_state(target_user)
        if lease["status"] != "clear":
            return {
                "status": lease["status"],
                "event_id": None,
                **{
                    "retry_after_seconds": lease["retry_after_seconds"]
                    for _ in (0,)
                    if "retry_after_seconds" in lease
                },
            }
        conn = database.get_connection()
        try:
            result = database.evaluate_and_record_librarian_query(
                conn,
                user_id=target_user,
                meeting_id=101,
                raw_query=query,
                expected_epoch=lease["expected_epoch"],
                thresholds=database.get_librarian_policy_snapshot(),
                stencil_verdict=SimpleNamespace(
                    ok=False,
                    canonical_query=None,
                    reason_code=reason_code,
                    message="rejected",
                    matched_rule_id=matched_rule_id,
                    gate_version="grammar-v2+stencil-v2",
                ),
            )
        finally:
            conn.close()
        if not isinstance(result, database.RejectedResult):
            return {"status": result.status, "event_id": None}
        return {
            "status": result.rejection_status,
            "event_id": result.event_id,
            "duplicate": result.duplicate,
            "strike_recorded": (
                result.rejection_status == "cooldown_started"
                and bool(json.loads(
                    self._state()["recent_cooldowns_json"]
                ))
            ),
            "retry_after_seconds": result.retry_after_seconds,
        }

    def _set_policy(self, config: dict) -> None:
        if self._active_config == config:
            return
        database.update_librarian_policy(
            daily_query_cap=100,
            reject_burst_threshold=config["burst_threshold"],
            reject_burst_window_seconds=config["burst_window_seconds"],
            reject_cooldown_seconds=config["cooldown_seconds"],
            reject_autoban_strike_threshold=config["strike_threshold"],
            reject_autoban_window_seconds=config["autoban_window_seconds"],
        )
        self._active_config = dict(config)

    def _age_cooldown(self) -> None:
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE librarian_abuse_state
                SET cooldown_until = '2000-01-01 00:00:00'
                WHERE user_id = ?
                """,
                (self.reader_id,),
            )
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _principal(self, config: dict | None = None):
        settings = {"librarian_daily_query_cap": 100}
        effective = self.CONFIG if config is None else config
        self._set_policy(effective)
        with (
            mock.patch.object(
                api_server,
                "load_user_settings",
                return_value=settings,
            ),
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=self._user(self.reader_id),
            ),
            mock.patch.object(
                api_server,
                "is_owner_email",
                return_value=False,
            ),
            mock.patch.object(
                database,
                "is_meeting_rag_indexed",
                return_value=False,
            ),
            mock.patch.object(
                rag_search,
                "load_prompt_template",
                return_value="Test Librarian system prompt.",
            ),
        ):
            yield

    def test_state_row_and_rings_stay_bounded_under_one_thousand_rejects(
        self,
    ) -> None:
        config = {
            **self.CONFIG,
            "burst_threshold": 64,
            "strike_threshold": 32,
            "autoban_window_seconds": 86400,
        }
        for index in range(1000):
            self._record(f"malformed-{index}", config=config)

        self.assertEqual(len(self._rows("librarian_abuse_state")), 1)
        state = self._state()
        self.assertLessEqual(
            len(json.loads(state["recent_rejects_json"])),
            config["burst_threshold"],
        )
        self.assertLessEqual(
            len(json.loads(state["recent_cooldowns_json"])),
            config["strike_threshold"],
        )
        self.assertLessEqual(
            state["cooldown_blocked_count"],
            1_000_000,
        )

    def test_duplicate_suppression_records_one_event_for_ten_attempts(
        self,
    ) -> None:
        config = {**self.CONFIG, "burst_threshold": 12}
        for _ in range(10):
            self._record("same malformed question", config=config)

        self.assertEqual(len(self._rows("librarian_gate_events")), 1)
        state = self._state()
        self.assertEqual(state["duplicate_suppressed_count"], 9)
        self.assertEqual(len(json.loads(state["recent_rejects_json"])), 10)

    def test_duplicate_only_burst_starts_cooldown_without_strike(self) -> None:
        results = [
            self._record("stuck client question")
            for _ in range(self.CONFIG["burst_threshold"])
        ]

        self.assertEqual(results[-1]["status"], "cooldown_started")
        self.assertFalse(results[-1]["strike_recorded"])
        state = self._state()
        self.assertIsNotNone(state["cooldown_until"])
        self.assertEqual(json.loads(state["recent_cooldowns_json"]), [])
        self.assertEqual(len(self._rows("librarian_gate_events")), 1)

    def test_natural_expiry_ages_timestamp_and_production_rearms(self):
        for index in range(self.CONFIG["burst_threshold"]):
            self._record(f"expiry-{index}")
        before_epoch = database.preflight_librarian_abuse_state(
            self.reader_id
        )["expected_epoch"]
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE librarian_abuse_state
                SET recent_rejects_json =
                    '[{"ts":"2999-01-01 00:00:00","fp":"stale"}]'
                WHERE user_id = ?
                """,
                (self.reader_id,),
            )
            conn.commit()
        finally:
            conn.close()
        self._age_cooldown()

        observed = database.preflight_librarian_abuse_state(
            self.reader_id
        )
        self.assertEqual(observed["status"], "clear")
        self.assertEqual(observed["expected_epoch"], before_epoch + 1)
        state = self._state()
        self.assertIsNone(state["cooldown_until"])
        self.assertEqual(state["recent_rejects_json"], "[]")

    def test_three_distinct_bursts_auto_ban_without_query_text(self) -> None:
        database.set_librarian_access(self.reader_id, "granted")
        secret_query_fragment = "never-store-this-question"
        final_result = None
        for burst in range(3):
            for offset in range(self.CONFIG["burst_threshold"]):
                final_result = self._record(
                    f"{secret_query_fragment}-{burst}-{offset}",
                    reason_code=(
                        "artifact_pattern" if offset % 2 else "not_a_question"
                    ),
                    matched_rule_id=(
                        "deny.artifact_bigram.v2" if offset % 2 else None
                    ),
                )
            if burst < 2:
                self._age_cooldown()

        self.assertEqual(final_result["status"], "auto_banned")
        self.assertEqual(
            database.get_user_librarian_access(self.reader_id),
            "banned",
        )
        state = dict(self._state())
        self.assertEqual(state["active_auto_ban"], 1)
        self.assertIsNotNone(state["auto_banned_at"])
        evidence = json.loads(state["evidence_json"])
        self.assertEqual(evidence["refused_count"], 12)
        self.assertEqual(evidence["burst_count"], 3)
        self.assertEqual(
            evidence["thresholds"]["burst_threshold"],
            self.CONFIG["burst_threshold"],
        )
        self.assertNotIn(
            secret_query_fragment,
            json.dumps(state, sort_keys=True),
        )
        self.assertNotIn("fp", state["evidence_json"])

    def test_cooldown_and_auto_ban_write_no_additional_gate_events(
        self,
    ) -> None:
        database.set_librarian_access(self.reader_id, "granted")
        for offset in range(self.CONFIG["burst_threshold"]):
            self._record(f"cooldown-{offset}")
        before_cooldown = len(self._rows("librarian_gate_events"))
        before_blocked_count = self._state()["cooldown_blocked_count"]
        with self._principal():
            blocked = self.client.post(
                "/api/rag-search/202",
                json={"query": "Tell me what happened?"},
            )
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.get_json()["status"], "cooldown_active")
        self.assertTrue(blocked.headers["Retry-After"].isdigit())
        self.assertEqual(
            len(self._rows("librarian_gate_events")),
            before_cooldown,
        )
        self.assertEqual(
            self._state()["cooldown_blocked_count"],
            before_blocked_count + 1,
        )

        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE librarian_abuse_state
                SET active_auto_ban = 1,
                    cooldown_until = NULL
                WHERE user_id = ?
                """,
                (self.reader_id,),
            )
            conn.execute(
                """
                UPDATE users
                SET librarian_access = 'banned'
                WHERE id = ?
                """,
                (self.reader_id,),
            )
            conn.commit()
        finally:
            conn.close()
        with self._principal():
            blocked_by_ban = self.client.post(
                "/api/rag-search/203",
                json={"query": "Tell me what happened?"},
            )
        self.assertEqual(blocked_by_ban.status_code, 403)
        self.assertEqual(
            len(self._rows("librarian_gate_events")),
            before_cooldown,
        )

    def test_restore_is_atomic_and_retains_provenance(self) -> None:
        database.set_librarian_access(self.reader_id, "granted")
        for burst in range(3):
            for offset in range(self.CONFIG["burst_threshold"]):
                self._record(f"restore-{burst}-{offset}")
            if burst < 2:
                self._age_cooldown()

        banned_state = self._state()
        auto_banned_at = banned_state["auto_banned_at"]
        evidence_json = banned_state["evidence_json"]
        self.assertTrue(
            database.decide_librarian_access(self.reader_id, "granted")
        )

        self.assertEqual(
            database.get_user_librarian_access(self.reader_id),
            "granted",
        )
        restored = self._state()
        self.assertEqual(restored["recent_rejects_json"], "[]")
        self.assertEqual(restored["recent_cooldowns_json"], "[]")
        self.assertIsNone(restored["cooldown_until"])
        self.assertEqual(restored["active_auto_ban"], 0)
        self.assertIsNotNone(restored["last_restored_at"])
        self.assertEqual(restored["auto_banned_at"], auto_banned_at)
        self.assertEqual(restored["evidence_json"], evidence_json)

    def test_manual_and_auto_bans_have_distinct_queue_markers(self) -> None:
        manual_id = self._seed_user("manual@example.test")
        database.set_librarian_access(manual_id, "banned")
        database.set_librarian_access(self.reader_id, "granted")
        for burst in range(3):
            for offset in range(self.CONFIG["burst_threshold"]):
                self._record(f"label-{burst}-{offset}")
            if burst < 2:
                self._age_cooldown()

        queue = {
            item["id"]: item
            for item in database.list_librarian_access_requests()
        }
        self.assertFalse(queue[manual_id]["active_auto_ban"])
        self.assertIsNone(queue[manual_id]["abuse_evidence"])
        self.assertTrue(queue[self.reader_id]["active_auto_ban"])
        self.assertIsNotNone(queue[self.reader_id]["abuse_evidence"])
        self.assertNotIn(
            "fp",
            json.dumps(queue[self.reader_id]["abuse_evidence"]),
        )

        database.decide_librarian_access(self.reader_id, "banned")
        queue = {
            item["id"]: item
            for item in database.list_librarian_access_requests()
        }
        self.assertFalse(queue[self.reader_id]["active_auto_ban"])

    def test_evaluation_error_never_updates_abuse_state(self) -> None:
        database.set_librarian_access(self.reader_id, "granted")
        with (
            self._principal(),
            mock.patch.object(
                api_server,
                "evaluate_librarian_query",
                side_effect=RuntimeError("stencil failed"),
            ),
        ):
            response = self.client.post(
                "/api/rag-search/404",
                json={"query": "What happened?"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertIsNone(self._state())
        events = self._rows("librarian_gate_events")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason_code"], "evaluation_error")

    def test_accepted_query_clears_rejects_but_not_strikes(self) -> None:
        database.set_librarian_access(self.reader_id, "granted")
        self._record("first malformed")
        self._record("second malformed")
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE librarian_abuse_state
                SET recent_cooldowns_json = ?
                WHERE user_id = ?
                """,
                ('["2026-07-29 00:00:00"]', self.reader_id),
            )
            conn.commit()
        finally:
            conn.close()

        lease = database.preflight_librarian_abuse_state(self.reader_id)
        decision_conn = database.get_connection()
        try:
            admitted = database.evaluate_and_record_librarian_query(
                decision_conn,
                user_id=self.reader_id,
                meeting_id=505,
                raw_query="What happened?",
                expected_epoch=lease["expected_epoch"],
                thresholds=database.get_librarian_policy_snapshot(),
                stencil_verdict=api_server.evaluate_librarian_query(
                    "What happened?"
                ),
            )
        finally:
            decision_conn.close()

        self.assertIsInstance(admitted, database.AdmittedResult)
        state = self._state()
        self.assertEqual(state["recent_rejects_json"], "[]")
        self.assertEqual(
            json.loads(state["recent_cooldowns_json"]),
            ["2026-07-29 00:00:00"],
        )

    def test_sqlite_policy_group_is_atomic_and_reachable(self) -> None:
        updated = database.update_librarian_policy(
            reject_burst_threshold=12,
            reject_burst_window_seconds=900,
            reject_cooldown_seconds=1200,
            reject_autoban_strike_threshold=5,
            reject_autoban_window_seconds=7200,
        )
        self.assertEqual(updated.reject_burst_threshold, 12)
        before = database.get_librarian_policy_snapshot()
        invalid_groups = (
            {"reject_burst_threshold": 65},
            {"reject_cooldown_seconds": 800},
            {"reject_autoban_strike_threshold": True},
            {
                "reject_cooldown_seconds": 1800,
                "reject_autoban_strike_threshold": 5,
                "reject_autoban_window_seconds": 7200,
            },
        )
        for invalid in invalid_groups:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    database.update_librarian_policy(**invalid)
                self.assertEqual(
                    database.get_librarian_policy_snapshot(),
                    before,
                )

    def _run_reject_boundary(self, *, expect_auto_ban: bool) -> None:
        config = {**self.CONFIG, "burst_threshold": 8}
        database.set_librarian_access(self.reader_id, "granted")
        for offset in range(7):
            self._record(f"preseed-{offset}", config=config)
        if expect_auto_ban:
            conn = database.get_connection()
            try:
                now_text = conn.execute(
                    "SELECT CURRENT_TIMESTAMP"
                ).fetchone()[0]
                conn.execute(
                    """
                    UPDATE librarian_abuse_state
                    SET recent_cooldowns_json = ?
                    WHERE user_id = ?
                    """,
                    (
                        json.dumps([now_text, now_text]),
                        self.reader_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        original = database.evaluate_and_record_librarian_query
        ready = threading.Barrier(2)
        statuses: list[int] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def synchronized_record(conn, **kwargs):
            ready.wait(timeout=5)
            return original(conn, **kwargs)

        def submit(offset: int) -> None:
            try:
                with api_server.app.test_client() as client:
                    response = client.post(
                        f"/api/rag-search/{700 + offset}",
                        json={"query": f"Tell me boundary {offset}?"},
                    )
                with lock:
                    statuses.append(response.status_code)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        with (
            self._principal(config),
            mock.patch.object(
                api_server,
                "evaluate_and_record_librarian_query",
                side_effect=synchronized_record,
            ),
        ):
            threads = [
                threading.Thread(target=submit, args=(offset,))
                for offset in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(self._rows("librarian_gate_events")), 8)
        if expect_auto_ban:
            self.assertEqual(statuses, [403, 403])
            self.assertEqual(
                database.get_user_librarian_access(self.reader_id),
                "banned",
            )
            self.assertEqual(self._state()["active_auto_ban"], 1)
        else:
            self.assertEqual(sorted(statuses), [400, 429])
            self.assertIsNotNone(self._state()["cooldown_until"])

    def test_concurrent_requests_at_reject_count_seven(self) -> None:
        self._run_reject_boundary(expect_auto_ban=False)

    def test_concurrent_requests_at_strike_count_two(self) -> None:
        self._run_reject_boundary(expect_auto_ban=True)


if __name__ == "__main__":
    unittest.main()

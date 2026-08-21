"""Owner-only Librarian tuning endpoint tests."""

from __future__ import annotations

import sys
import tempfile
import threading
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

import env_config
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


class LibrarianTuningEndpointTests(unittest.TestCase):
    DEFAULT_SETTINGS = {
        "librarian_daily_query_cap": 3,
        "librarian_reject_burst_threshold": 8,
        "librarian_reject_burst_window_seconds": 600,
        "librarian_reject_cooldown_seconds": 1800,
        "librarian_reject_autoban_strike_threshold": 3,
        "librarian_reject_autoban_window_seconds": 86400,
    }

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = mock.patch.object(
            database,
            "DB_PATH",
            str(Path(self.temp_dir.name) / "librarian-tuning.db"),
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.settings_patch = mock.patch.object(
            env_config,
            "SETTINGS_PATH",
            str(Path(self.temp_dir.name) / "user_settings.json"),
        )
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

        database.init_db()
        database.init_librarian_gate_events_schema()
        database.init_librarian_abuse_state_schema()
        env_config.save_user_settings(dict(self.DEFAULT_SETTINGS))

        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()
        self.owner_gate = mock.patch.object(
            api_server,
            "_require_owner",
            return_value=(object(), None),
        )

    def _get_as_owner(self):
        with self.owner_gate:
            return self.client.get("/api/librarian/tuning")

    def _patch_as_owner(self, body: object):
        with self.owner_gate:
            return self.client.patch("/api/librarian/tuning", json=body)

    @staticmethod
    def _seed_granted_user() -> int:
        conn = database.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (google_sub, email, librarian_access)
                VALUES ('policy-race', 'policy-race@example.test', 'granted')
                """
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def test_owner_get_returns_full_effective_shape(self) -> None:
        response = self._get_as_owner()

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(
            body["cross_field_rule"],
            "burst_window_seconds <= cooldown_seconds <= "
            "autoban_window_seconds; auto-ban must be reachable",
        )
        self.assertFalse(body["group_fallback_active"])
        self.assertEqual(body["revision"], 1)
        self.assertEqual(
            body["settings"],
            {
                "librarian_daily_query_cap": {
                    "value": 3,
                    "default": 3,
                    "min": 1,
                    "max": None,
                    "unit": "queries",
                },
                "librarian_reject_burst_threshold": {
                    "value": 8,
                    "default": 8,
                    "min": 4,
                    "max": 64,
                    "unit": "rejects",
                },
                "librarian_reject_burst_window_seconds": {
                    "value": 600,
                    "default": 600,
                    "min": 60,
                    "max": None,
                    "unit": "seconds",
                },
                "librarian_reject_cooldown_seconds": {
                    "value": 1800,
                    "default": 1800,
                    "min": 300,
                    "max": None,
                    "unit": "seconds",
                },
                "librarian_reject_autoban_strike_threshold": {
                    "value": 3,
                    "default": 3,
                    "min": 2,
                    "max": 32,
                    "unit": "cooldowns",
                },
                "librarian_reject_autoban_window_seconds": {
                    "value": 86400,
                    "default": 86400,
                    "min": 3600,
                    "max": None,
                    "unit": "seconds",
                },
            },
        )
        self.assertEqual(
            set(body["stats"]),
            {
                "granted_accounts",
                "requested_pending",
                "cooldowns_active",
                "auto_bans_last_7d",
                "accepted_queries_last_24h",
            },
        )

    def test_anonymous_caller_is_rejected_with_401(self) -> None:
        response = self.client.get("/api/librarian/tuning")

        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()["success"])

    def test_owner_patch_single_key_persists_in_sqlite(self) -> None:
        response = self._patch_as_owner({"librarian_daily_query_cap": 5})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["settings"]["librarian_daily_query_cap"][
                "value"
            ],
            5,
        )
        self.assertEqual(
            database.get_librarian_policy_snapshot().daily_query_cap,
            5,
        )

    def test_malformed_patches_are_rejected_without_any_write(self) -> None:
        cases = (
            ("bool", {"librarian_daily_query_cap": True}),
            ("string", {"librarian_daily_query_cap": "5"}),
            ("negative", {"librarian_daily_query_cap": -1}),
            ("zero", {"librarian_daily_query_cap": 0}),
            ("out-of-range", {"librarian_reject_burst_threshold": 65}),
            ("unknown", {"unrelated_setting": 9}),
            (
                "cross-field",
                {"librarian_reject_cooldown_seconds": 90000},
            ),
        )
        for label, patch in cases:
            with self.subTest(label=label):
                before = database.get_librarian_policy_snapshot()
                response = self._patch_as_owner(patch)

                self.assertEqual(response.status_code, 400)
                body = response.get_json()
                self.assertFalse(body["success"])
                self.assertIsInstance(body["error"], str)
                self.assertTrue(body["error"])
                self.assertIn("invalid_key", body)
                self.assertEqual(
                    database.get_librarian_policy_snapshot(),
                    before,
                )

    def test_valid_patch_is_reflected_in_the_next_get(self) -> None:
        patched = self._patch_as_owner({
            "librarian_reject_burst_threshold": 12,
            "librarian_reject_burst_window_seconds": 900,
        })
        self.assertEqual(patched.status_code, 200)

        fetched = self._get_as_owner()
        self.assertEqual(fetched.status_code, 200)
        settings = fetched.get_json()["settings"]
        self.assertEqual(
            settings["librarian_reject_burst_threshold"]["value"],
            12,
        )
        self.assertEqual(
            settings["librarian_reject_burst_window_seconds"]["value"],
            900,
        )

    def test_file_backed_threshold_changes_do_not_affect_enforcement(
        self,
    ) -> None:
        invalid = dict(self.DEFAULT_SETTINGS)
        invalid["librarian_reject_burst_threshold"] = 2
        invalid["librarian_reject_cooldown_seconds"] = 7200
        env_config.save_user_settings(invalid)

        response = self._get_as_owner()

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body["group_fallback_active"])
        self.assertEqual(
            {
                key: item["value"]
                for key, item in body["settings"].items()
                if key != "librarian_daily_query_cap"
            },
            {
                key: value
                for key, value in self.DEFAULT_SETTINGS.items()
                if key != "librarian_daily_query_cap"
            },
        )

    def test_patch_persists_only_supplied_sqlite_fields(
        self,
    ) -> None:
        before = database.get_librarian_policy_snapshot()

        response = self._patch_as_owner({"librarian_daily_query_cap": 5})

        self.assertEqual(response.status_code, 200)
        persisted = database.get_librarian_policy_snapshot()
        self.assertEqual(persisted.daily_query_cap, 5)
        self.assertEqual(
            persisted.reject_burst_threshold,
            before.reject_burst_threshold,
        )
        self.assertFalse(response.get_json()["group_fallback_active"])

    def test_stats_are_integers_and_never_null(self) -> None:
        response = self._get_as_owner()

        self.assertEqual(response.status_code, 200)
        for value in response.get_json()["stats"].values():
            self.assertIsInstance(value, int)
            self.assertIsNotNone(value)

    def test_concurrent_patch_and_admission_use_complete_revision(self):
        user_id = self._seed_granted_user()
        old_policy = database.get_librarian_policy_snapshot()
        lease = database.preflight_librarian_abuse_state(user_id)
        ready = threading.Barrier(2)
        results: list[object] = []
        errors: list[BaseException] = []

        def patch_policy():
            try:
                ready.wait(timeout=5)
                results.append(database.update_librarian_policy(
                    reject_burst_threshold=12,
                    reject_burst_window_seconds=900,
                    reject_cooldown_seconds=1200,
                    reject_autoban_strike_threshold=5,
                    reject_autoban_window_seconds=7200,
                ))
            except BaseException as exc:
                errors.append(exc)

        def admit():
            conn = database.get_connection()
            try:
                ready.wait(timeout=5)
                results.append(
                    database.evaluate_and_record_librarian_query(
                        conn,
                        user_id=user_id,
                        meeting_id=991,
                        raw_query="What changed?",
                        expected_epoch=lease["expected_epoch"],
                        thresholds=old_policy,
                        stencil_verdict=SimpleNamespace(
                            ok=True,
                            canonical_query="What changed?",
                            reason_code=None,
                            message=None,
                            matched_rule_id=None,
                            gate_version="grammar-v2+stencil-v2",
                        ),
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=patch_policy),
            threading.Thread(target=admit),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        admitted = next(
            result
            for result in results
            if isinstance(result, database.AdmittedResult)
        )
        new_policy = database.get_librarian_policy_snapshot()
        self.assertIn(
            admitted.policy_revision,
            {old_policy.revision, new_policy.revision},
        )
        self.assertEqual(new_policy.reject_burst_threshold, 12)
        self.assertEqual(new_policy.reject_burst_window_seconds, 900)
        self.assertEqual(new_policy.reject_cooldown_seconds, 1200)
        self.assertEqual(new_policy.reject_autoban_strike_threshold, 5)
        self.assertEqual(new_policy.reject_autoban_window_seconds, 7200)


if __name__ == "__main__":
    unittest.main()

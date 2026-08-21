"""CLI-1 unit tests — config round-trip, provider matrix invariants,
validation dispatch (network stubbed), and the init command end-to-end
against a temp ZSPAN_HOME.

Run directly (pytest is not in the project venv):

    python -m zspan_cli.tests.test_zspan_cli

No test touches the network or the real ~/.zspan.
"""
from __future__ import annotations

import json
import io
import os
import plistlib
import shlex
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import nullcontext, redirect_stdout
from unittest import mock

import requests

from zspan_cli import (
    auth,
    cli,
    config,
    contribution,
    flagship,
    media,
    protocol,
    providers,
    resolver,
    validate,
    workspace,
)


class TestPrivateContributionPackage(unittest.TestCase):
    def _meeting(self):
        return {
            "id": 1,
            "public_id": "m_" + "A" * 22,
            "video_url": "https://www.youtube.com/watch?v=test",
        }

    def _transcript(self):
        return {
            "source_url": "https://www.youtube.com/watch?v=test",
            "duration_seconds": 2.0,
            "language": "en",
            "transcriber": "local",
            "model": "small.en",
            "words": [
                {"word": "Council", "start": 0.0, "end": 0.8},
                {"word": "opened", "start": 0.8, "end": 1.5},
            ],
        }

    def _outputs(self):
        return {
            output_type: {
                "content": f"content:{output_type}",
                "provider": "openai",
                "model": "gpt-test",
                "gate_status": "observed_clean",
                "gate_log": '{"status":"observed_clean"}',
            }
            for output_type in contribution.OUTPUT_TYPES
        }

    def test_package_is_deterministic_and_contains_no_secret_or_media(self):
        first = contribution.build_core(
            self._meeting(), self._transcript(), self._outputs()
        )
        second = contribution.build_core(
            self._meeting(), self._transcript(), self._outputs()
        )
        self.assertEqual(first, second)
        payload = contribution.finish(first, "I" * 24)
        self.assertEqual(payload["payload_sha256"], contribution.sha256_json(first))
        encoded = json.dumps(payload)
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("media_path", encoded)

    def test_package_requires_all_outputs_and_rejects_secret_leak(self):
        outputs = self._outputs()
        outputs.pop("episode_tagline")
        with self.assertRaises(contribution.ContributionError):
            contribution.build_core(self._meeting(), self._transcript(), outputs)
        core = contribution.build_core(
            self._meeting(), self._transcript(), self._outputs()
        )
        core["outputs"][0]["content"] = "credential sk-test-very-secret-value"
        with self.assertRaises(contribution.ContributionError):
            contribution.assert_secrets_absent(
                core, ["sk-test-very-secret-value"]
            )


class _TempHome(unittest.TestCase):
    """Base: every test runs against a throwaway ZSPAN_HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "ZSPAN_HOME": self._tmp.name,
                "ZSPAN_SKIP_APPROVALS": "1",
            },
            clear=False,
        )
        self._env.start()
        # Belt-and-suspenders: no accidental flagship override leaking in.
        os.environ.pop("ZSPAN_FLAGSHIP_URL", None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


def _accepted_private_contribution(_base_url, payload, _bearer):
    return {
        "submission_public_id": "c_1234567890AbCdEfGhIjKl",
        "payload_sha256": payload["payload_sha256"],
        "status": "received_unverified",
        "replayed": False,
        "published": False,
    }


class TestConfig(_TempHome):
    def test_absent_config_is_none(self):
        self.assertIsNone(config.load_config())

    def test_round_trip_and_field_preservation(self):
        config.save_config({
            "synthesis_provider": "openai",
            "api_keys": {"openai": "sk-test-abcdefgh1234"},
            "future_field_from_cli2": {"kept": True},
        })
        loaded = config.load_config()
        self.assertEqual(loaded["synthesis_provider"], "openai")
        self.assertEqual(loaded["api_keys"]["openai"], "sk-test-abcdefgh1234")
        self.assertEqual(loaded["future_field_from_cli2"], {"kept": True})
        self.assertEqual(loaded["version"], config.CONFIG_VERSION)
        self.assertIn("created_at", loaded)
        self.assertIn("updated_at", loaded)

    def test_zspan_home_override_respected(self):
        self.assertEqual(config.zspan_home(), Path(self._tmp.name))
        self.assertEqual(config.config_path().parent, Path(self._tmp.name))

    def test_corrupt_config_is_loud_not_silent(self):
        config.config_path().parent.mkdir(parents=True, exist_ok=True)
        config.config_path().write_text("{not json", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load_config()

    def test_non_object_config_is_loud(self):
        config.config_path().parent.mkdir(parents=True, exist_ok=True)
        config.config_path().write_text('["a", "list"]', encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load_config()

    @unittest.skipIf(sys.platform == "win32", "POSIX permission check")
    def test_config_file_is_owner_only(self):
        path = config.save_config({"api_keys": {"openai": "sk-test-abcdefgh1234"}})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_fingerprint_shapes(self):
        self.assertEqual(config.key_fingerprint("sk-abcdefghijklmnop"), "sk-a...mnop")
        self.assertEqual(config.key_fingerprint("short"), "(too short)")
        self.assertEqual(config.key_fingerprint(""), "(too short)")

    def test_home_jurisdiction_new_legacy_and_absent(self):
        self.assertIsNone(config.home_jurisdiction(None))
        self.assertEqual(
            config.home_jurisdiction({
                "picked_city": {"city": "Old", "county": "C", "state": "AZ",
                                "status": "covered"},
            }),
            {"city": "Old", "county": "C", "state": "AZ"},
        )
        updated = config.save_home_jurisdiction({}, "Arizona", "Mohave County", "Kingman")
        self.assertEqual(
            config.home_jurisdiction(updated),
            {"state": "Arizona", "county": "Mohave County", "city": "Kingman"},
        )

    def test_processing_ack_record_and_version_gate(self):
        self.assertFalse(config.has_processing_ack({}))
        accepted = config.record_processing_ack({})
        self.assertTrue(config.has_processing_ack(accepted))
        self.assertIn("accepted_at", accepted["local_processing_ack"])
        with mock.patch.object(
            config, "PROCESSING_ACK_VERSION", config.PROCESSING_ACK_VERSION + 1
        ):
            self.assertFalse(config.has_processing_ack(accepted))


class TestProviders(unittest.TestCase):
    def test_matrix_invariants(self):
        self.assertIn(providers.DEFAULT_PROVIDER, providers.PROVIDERS)
        for pid, p in providers.PROVIDERS.items():
            for field in ("label", "key_url", "synthesis", "cloud_transcription", "cost_note"):
                self.assertIn(field, p, f"{pid} missing {field}")
        # Transcription is local-by-default (operator redirect 2026-07-09);
        # the cloud path is a speed OPT-IN, offered by at least one provider.
        self.assertTrue(providers.cloud_transcription_providers())

    def test_matrix_lines_carry_the_transcription_note(self):
        text = "\n".join(providers.matrix_lines())
        # The note must state the local-first floor AND the optional cloud path.
        self.assertIn("locally", text)
        self.assertIn("whisper-1", text)
        for p in providers.PROVIDERS.values():
            self.assertIn(p["label"], text)

    @unittest.skipUnless(os.name == "posix", "POSIX fallback locations")
    def test_codex_resolution_survives_finder_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = (
                Path(tmp) / ".nvm" / "versions" / "node" /
                "v22.22.1" / "bin" / "codex"
            )
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(
                os.environ,
                {"HOME": tmp, "PATH": "/usr/bin:/bin"},
                clear=False,
            ):
                self.assertIsNone(shutil.which("codex"))
                self.assertEqual(
                    providers.resolve_codex_binary({}),
                    str(binary.absolute()),
                )
                self.assertTrue(providers.codex_available({}))

    def test_codex_config_override_resolves_cross_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            suffix = ".cmd" if os.name == "nt" else ""
            binary = Path(tmp) / f"codex{suffix}"
            body = "@exit /b 0\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n"
            binary.write_text(body, encoding="utf-8")
            if os.name == "posix":
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(
                providers.resolve_codex_binary({"codex_binary": str(binary)}),
                str(binary.absolute()),
            )


def _resp(status_code: int, body):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = body
    return r


class TestValidate(unittest.TestCase):
    KEY = "sk-test-abcdefghijklmnopqrstuv"

    def test_unknown_provider_is_honest(self):
        result = validate.validate_key("nonsense", self.KEY)
        self.assertFalse(result["valid"])
        self.assertIn("not supported", result["error"])

    def test_valid_key_openai_shape(self):
        with mock.patch.object(validate.requests, "get", return_value=_resp(200, {"data": [1, 2, 3]})):
            result = validate.validate_key("openai", self.KEY)
        self.assertTrue(result["valid"])
        self.assertEqual(result["model_count"], 3)

    def test_valid_key_gemini_shape(self):
        with mock.patch.object(validate.requests, "get", return_value=_resp(200, {"models": [1]})):
            result = validate.validate_key("gemini", self.KEY)
        self.assertTrue(result["valid"])
        self.assertEqual(result["model_count"], 1)

    def test_rejected_key_surfaces_provider_message(self):
        body = {"error": {"message": "Incorrect API key provided"}}
        with mock.patch.object(validate.requests, "get", return_value=_resp(401, body)):
            result = validate.validate_key("openai", self.KEY)
        self.assertFalse(result["valid"])
        self.assertIn("Incorrect API key", result["error"])

    def test_network_error_reports_type_only_never_key(self):
        exc = requests.exceptions.ConnectionError(f"boom https://x?key={self.KEY}")
        with mock.patch.object(validate.requests, "get", side_effect=exc):
            result = validate.validate_key("gemini", self.KEY)
        self.assertFalse(result["valid"])
        self.assertEqual(result["error"], "network error: ConnectionError")
        # Custody: the raw key appears in NO returned value.
        for v in result.values():
            self.assertNotIn(self.KEY, str(v))


class TestInitCommand(_TempHome):
    KEY = "sk-test-abcdefghijklmnopqrstuv"

    def _init(self, *extra, key_in_env=True):
        env = {"FAKE_ZSPAN_KEY": self.KEY} if key_in_env else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if not key_in_env:
                os.environ.pop("FAKE_ZSPAN_KEY", None)
            return cli.main(["init", "--provider", "openai", "--key-env", "FAKE_ZSPAN_KEY", "--yes", *extra])

    def test_non_interactive_init_skip_validate(self):
        rc = self._init("--skip-validate")
        self.assertEqual(rc, 0)
        cfg = config.load_config()
        self.assertEqual(cfg["synthesis_provider"], "openai")
        self.assertEqual(cfg["api_keys"]["openai"], self.KEY)
        self.assertEqual(cfg["flagship_url"], config.DEFAULT_FLAGSHIP_URL)

    def test_missing_key_env_writes_nothing(self):
        rc = self._init("--skip-validate", key_in_env=False)
        self.assertEqual(rc, 1)
        self.assertIsNone(config.load_config())

    def test_validated_init(self):
        with mock.patch.object(cli, "validate_key", return_value={
            "valid": True, "provider": "openai", "fingerprint": "sk-t...stuv", "model_count": 5,
        }):
            rc = self._init()
        self.assertEqual(rc, 0)
        self.assertEqual(config.load_config()["api_keys"]["openai"], self.KEY)

    def test_invalid_key_non_interactive_writes_nothing(self):
        with mock.patch.object(cli, "validate_key", return_value={
            "valid": False, "provider": "openai", "fingerprint": "sk-t...stuv", "error": "nope",
        }):
            rc = self._init()
        self.assertEqual(rc, 1)
        self.assertIsNone(config.load_config())

    def test_reinit_preserves_other_provider_keys(self):
        config.save_config({
            "synthesis_provider": "gemini",
            "api_keys": {"gemini": "AIzaFakeFakeFakeFake"},
        })
        rc = self._init("--skip-validate")
        self.assertEqual(rc, 0)
        cfg = config.load_config()
        self.assertEqual(cfg["synthesis_provider"], "openai")
        self.assertEqual(cfg["api_keys"]["gemini"], "AIzaFakeFakeFakeFake")
        self.assertEqual(cfg["api_keys"]["openai"], self.KEY)

    def test_flagship_url_override_lands(self):
        rc = self._init("--skip-validate", "--flagship-url", "http://127.0.0.1:5001")
        self.assertEqual(rc, 0)
        self.assertEqual(config.load_config()["flagship_url"], "http://127.0.0.1:5001")

    def test_corrupt_config_fails_loud_before_prompting(self):
        config.config_path().parent.mkdir(parents=True, exist_ok=True)
        config.config_path().write_text("{broken", encoding="utf-8")
        rc = self._init("--skip-validate")
        self.assertEqual(rc, 1)


class TestCommandSurface(_TempHome):
    def test_open_with_empty_workspace_is_honest_and_nonzero(self):
        # Every V0 command is real as of CLI-4; `open` on a workspace with
        # no synthesized outputs fails with the pointer at `zspan process`.
        self.assertEqual(cli.main(["open"]), 1)

    def test_providers_command(self):
        self.assertEqual(cli.main(["providers"]), 0)

    def test_bare_invocation_prints_help(self):
        self.assertEqual(cli.main([]), 0)


_COVERAGE = [
    {"city": "Kingman", "county": "Mohave County", "state": "AZ",
     "status": "covered", "published_count": 5, "latest_published_date": "2026-06-10"},
    {"city": "Chinle", "county": "Apache County", "state": "AZ",
     "status": "assessment pending", "published_count": 0, "latest_published_date": None},
    {"city": "Elko", "county": "Elko County", "state": "NV",
     "status": "needs repair", "published_count": 0, "latest_published_date": None},
]


def _event(flagship_id: int, title: str, date: str, video: str = "") -> dict:
    return {
        "id": flagship_id, "city_name": "Kingman", "county": "Mohave County",
        "state": "AZ", "meeting_title": title, "meeting_date": date,
        "public_id": f"m_{flagship_id:022d}",
        "meeting_time": "", "meeting_location": "", "meeting_status": "Scheduled",
        "agenda_url": "", "minutes_url": "", "agenda_packet_url": "",
        "video_url": video,
    }


class TestWorkspace(_TempHome):
    def test_connect_is_idempotent(self):
        for _ in range(2):
            conn = workspace.connect()
            conn.close()
        self.assertTrue(workspace.workspace_path().exists())

    def test_upsert_new_then_update_preserves_process_state(self):
        conn = workspace.connect()
        self.assertEqual(workspace.upsert_meeting(conn, _event(1, "Council", "2026-07-01")), "new")
        # CLI-3 stamps process state; a later re-pull must not clobber it.
        conn.execute("UPDATE meetings SET processed_at = 'X', transcript_path = 'T' WHERE id = 1")
        self.assertEqual(
            workspace.upsert_meeting(conn, _event(1, "Council (amended)", "2026-07-01", video="https://v")),
            "updated",
        )
        row = conn.execute("SELECT * FROM meetings WHERE id = 1").fetchone()
        self.assertEqual(row["title"], "Council (amended)")
        self.assertEqual(row["video_url"], "https://v")
        self.assertEqual(row["processed_at"], "X")
        self.assertEqual(row["transcript_path"], "T")
        n, latest = workspace.pull_stats(conn, "Kingman")
        self.assertEqual((n, latest), (1, "2026-07-01"))
        conn.close()

    def test_upsert_without_id_is_loud(self):
        conn = workspace.connect()
        with self.assertRaises(ValueError):
            workspace.upsert_meeting(conn, {"meeting_title": "no id"})
        conn.close()

    def test_pre_pi6_migration_backfills_legacy_identity(self):
        path = workspace.workspace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.executescript(workspace._SCHEMA)
        conn.execute(
            """INSERT INTO meetings
               (id, city, source_row_json, pulled_at) VALUES (41, 'Kingman', '{}', 'X')"""
        )
        conn.commit()
        conn.close()

        conn = workspace.connect()
        row = workspace.get_meeting(conn, 41)
        conn.close()
        self.assertEqual(row["flagship_row_id"], 41)
        self.assertEqual(row["import_source"], "pull")
        self.assertIsNone(row["public_id"])

    def test_public_identity_collapses_pull_and_handoff_in_either_order(self):
        public_id = "m_1234567890AbCdEfGhIjKl"
        for handoff_first in (False, True):
            with self.subTest(handoff_first=handoff_first):
                conn = workspace.connect()
                conn.execute("DELETE FROM meetings")
                imported = {
                    "public_id": public_id, "city_name": "Kingman",
                    "meeting_title": "Original", "meeting_date": "2026-07-01",
                }
                pulled = {**_event(77, "Refreshed", "2026-07-01"),
                          "public_id": public_id}
                ordered = ((imported, "handoff"), (pulled, "pull"))
                if not handoff_first:
                    ordered = tuple(reversed(ordered))
                for row, source in ordered:
                    workspace.upsert_meeting(conn, row, import_source=source)
                rows = conn.execute("SELECT * FROM meetings").fetchall()
                conn.close()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["flagship_row_id"], 77)
                self.assertEqual(rows[0]["import_source"], "pull")

    def test_handoff_autoassigns_local_id_and_reimport_preserves_processing(self):
        public_id = "m_1234567890AbCdEfGhIjKl"
        row = {"public_id": public_id, "city_name": "Kingman",
               "meeting_title": "Original", "meeting_date": "2026-07-01"}
        conn = workspace.connect()
        workspace.upsert_meeting(conn, row, import_source="handoff")
        first = workspace.get_meeting_by_public_id(conn, public_id)
        self.assertIsInstance(first["id"], int)
        self.assertIsNone(first["flagship_row_id"])
        conn.execute(
            "UPDATE meetings SET processed_at = 'X' WHERE public_id = ?", (public_id,)
        )
        workspace.upsert_meeting(
            conn, {**row, "meeting_title": "Refreshed"}, import_source="handoff"
        )
        refreshed = workspace.get_meeting_by_public_id(conn, public_id)
        conn.close()
        self.assertEqual(refreshed["processed_at"], "X")
        self.assertEqual(refreshed["title"], "Refreshed")

    def test_contribution_retry_key_is_stable_and_new_bytes_revoke_completion(self):
        conn = workspace.connect()
        workspace.upsert_meeting(conn, _event(91, "Council", "2026-07-01"))
        meeting_id = conn.execute(
            "SELECT id FROM meetings WHERE flagship_row_id = 91"
        ).fetchone()[0]
        first_key = workspace.prepare_contribution(conn, meeting_id, "a" * 64)
        retry_key = workspace.prepare_contribution(conn, meeting_id, "a" * 64)
        self.assertEqual(first_key, retry_key)
        workspace.mark_contribution_submitted(
            conn,
            meeting_id,
            payload_sha256="a" * 64,
            submission_public_id="c_first",
        )
        workspace.mark_processed(conn, meeting_id)
        replacement_key = workspace.prepare_contribution(conn, meeting_id, "b" * 64)
        row = workspace.contribution_submission(conn, meeting_id)
        meeting = workspace.get_meeting(conn, meeting_id)
        conn.close()
        self.assertNotEqual(replacement_key, first_key)
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["submission_public_id"])
        self.assertIsNone(meeting["processed_at"])


class TestPickCommand(_TempHome):
    def test_direct_city_pick_writes_config(self):
        with mock.patch.object(cli, "fetch_coverage", return_value=_COVERAGE):
            rc = cli.main(["pick", "--city", "kingman"])
        self.assertEqual(rc, 0)
        picked = config.load_config()["home_jurisdiction"]
        self.assertEqual(picked["city"], "Kingman")
        self.assertEqual(picked["state"], "AZ")

    def test_unknown_city_fails_honestly(self):
        with mock.patch.object(cli, "fetch_coverage", return_value=_COVERAGE):
            rc = cli.main(["pick", "--city", "Atlantis"])
        self.assertEqual(rc, 1)
        cfg = config.load_config()
        self.assertIsNone((cfg or {}).get("home_jurisdiction"))

    def test_list_renders_all_rows(self):
        with mock.patch.object(cli, "fetch_coverage", return_value=_COVERAGE):
            rc = cli.main(["pick", "--list"])
        self.assertEqual(rc, 0)

    def test_empty_coverage_is_a_plain_failure(self):
        with mock.patch.object(cli, "fetch_coverage", return_value=[]):
            rc = cli.main(["pick", "--city", "Kingman"])
        self.assertEqual(rc, 1)


class TestPullCommand(_TempHome):
    def _payload(self, events):
        return {"events": events, "count": len(events), "is_stale": False,
                "last_scraped": "2026-07-09 12:00:00", "source": "cache", "success": True}

    def test_pull_writes_workspace_rows(self):
        events = [_event(1, "Council", "2026-07-01", video="https://v"),
                  _event(2, "Planning", "2026-07-03")]
        with mock.patch.object(cli, "fetch_meetings", return_value=self._payload(events)):
            rc = cli.main(["pull", "Kingman"])
        self.assertEqual(rc, 0)
        conn = workspace.connect()
        n, latest = workspace.pull_stats(conn, "Kingman")
        conn.close()
        self.assertEqual((n, latest), (2, "2026-07-03"))

    def test_pull_uses_picked_city_when_no_arg(self):
        config.save_config({"picked_city": {"city": "Kingman", "county": "Mohave County",
                                            "state": "AZ", "status": "covered"}})
        with mock.patch.object(cli, "fetch_meetings", return_value=self._payload([_event(3, "Golf", "2026-06-10")])) as fm:
            rc = cli.main(["pull"])
        self.assertEqual(rc, 0)
        self.assertEqual(fm.call_args[0][1], "Kingman")

    def test_pull_without_city_or_pick_fails(self):
        self.assertEqual(cli.main(["pull"]), 1)

    def test_pull_state_sweeps_published_cities_only(self):
        """--state az mirrors the az cities that carry PUBLISHED
        meetings — the public catalog serves published-only (operator
        2026-07-10 evening, superseding the same-day all-rows form), so
        unpublished Chinle is skipped upfront rather than fetched into
        a guaranteed empty; the NV row is never touched."""
        def per_city(_base, city, year=None):
            if city == "Kingman":
                return self._payload([
                    _event(1, "Council", "2026-07-01", video="https://v")])
            raise AssertionError(
                f"unexpected fetch for {city} — unpublished cities are "
                "skipped upfront under the published-only contract")

        with mock.patch.object(cli, "fetch_coverage", return_value=_COVERAGE), \
             mock.patch.object(cli, "fetch_meetings", side_effect=per_city) as fm:
            rc = cli.main(["pull", "--state", "az"])
        self.assertEqual(rc, 0)
        pulled = sorted(c.args[1] for c in fm.call_args_list)
        self.assertEqual(pulled, ["Kingman"])  # Chinle unpublished; Elko NV
        conn = workspace.connect()
        n, _ = workspace.pull_stats(conn, "Kingman")
        conn.close()
        self.assertEqual(n, 1)

        # Unknown state → honest fail naming what the coverage knows.
        with mock.patch.object(cli, "fetch_coverage", return_value=_COVERAGE):
            self.assertEqual(cli.main(["pull", "--state", "zz"]), 1)
        # City + --state together is ambiguous → refuse honestly.
        self.assertEqual(cli.main(["pull", "Kingman", "--state", "az"]), 1)

    def test_succeeded_empty_is_exit_zero_and_writes_nothing(self):
        # Known-city empty (Kingman IS in coverage) → the honest
        # parser-stale message, exit 0 (succeeded-empty ≠ failure, F8).
        with mock.patch.object(cli, "fetch_meetings", return_value=self._payload([])), \
             mock.patch.object(cli, "fetch_coverage", return_value=_COVERAGE):
            rc = cli.main(["pull", "Kingman"])
        self.assertEqual(rc, 0)
        conn = workspace.connect()
        n, _ = workspace.pull_stats(conn, "Kingman")
        conn.close()
        self.assertEqual(n, 0)

    def test_pull_unpublished_city_names_the_publish_wall(self):
        # Chinle IS covered but carries no published meetings — the
        # empty is the publish wall, said plainly (exit 0, and never
        # the parser-repair diagnosis).
        said = []
        with mock.patch.object(cli, "fetch_meetings", return_value=self._payload([])), \
             mock.patch.object(cli, "fetch_coverage", return_value=_COVERAGE), \
             mock.patch.object(cli, "_say", side_effect=lambda m="": said.append(str(m))):
            rc = cli.main(["pull", "Chinle"])
        self.assertEqual(rc, 0)
        self.assertTrue(any("published" in line for line in said))
        self.assertFalse(any("repair" in line.lower() for line in said))

    def test_unknown_city_empty_is_named_not_blamed_on_parsers(self):
        # A city that isn't in coverage at all → "isn't in the live
        # coverage list", exit 1 — never the parser-repair diagnosis.
        with mock.patch.object(cli, "fetch_meetings", return_value=self._payload([])), \
             mock.patch.object(cli, "fetch_coverage", return_value=_COVERAGE):
            rc = cli.main(["pull", "Atlantis"])
        self.assertEqual(rc, 1)

    def test_year_flag_accepts_all(self):
        events = [_event(9, "Old Council", "2024-03-01")]
        with mock.patch.object(cli, "fetch_meetings", return_value=self._payload(events)) as fm:
            rc = cli.main(["pull", "Kingman", "--year", "all"])
        self.assertEqual(rc, 0)
        self.assertEqual(fm.call_args.kwargs.get("year"), "all")

    def test_repull_is_idempotent(self):
        events = [_event(1, "Council", "2026-07-01")]
        with mock.patch.object(cli, "fetch_meetings", return_value=self._payload(events)):
            cli.main(["pull", "Kingman"])
            cli.main(["pull", "Kingman"])
        conn = workspace.connect()
        n, _ = workspace.pull_stats(conn, "Kingman")
        conn.close()
        self.assertEqual(n, 1)


class TestFlagshipClient(_TempHome):
    def test_network_error_is_a_plain_flagship_error(self):
        from zspan_cli import flagship
        exc = requests.exceptions.ConnectionError("refused")
        with mock.patch.object(flagship.requests, "get", side_effect=exc):
            with self.assertRaises(flagship.FlagshipError) as ctx:
                flagship.fetch_coverage("http://127.0.0.1:1")
        self.assertIn("could not reach", str(ctx.exception))

    def test_client_identifies_itself_to_our_own_server(self):
        from zspan_cli import flagship
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"states": []}
        with mock.patch.object(flagship.requests, "get", return_value=resp) as g:
            flagship.fetch_coverage("http://x")
        self.assertTrue(g.call_args.kwargs["headers"]["User-Agent"].startswith("zspan-cli/"))
        self.assertTrue(g.call_args.args[0].endswith("/v1/catalog/jurisdictions"))

    def test_coverage_flattens_the_public_jurisdiction_contract(self):
        from zspan_cli import flagship

        payload = {
            "states": [{
                "state": "AZ",
                "counties": [{
                    "county": "Mohave County",
                    "cities": [
                        {"city": "Kingman", "meeting_count": 2, "covered": True},
                        {"city": "Yucca", "meeting_count": 0, "covered": False},
                    ],
                }],
            }],
        }
        with mock.patch.object(
            flagship.requests, "get", return_value=_resp(200, payload)
        ):
            self.assertEqual(flagship.fetch_coverage("http://x"), [
                {
                    "city": "Kingman",
                    "county": "Mohave County",
                    "state": "AZ",
                    "status": "covered",
                    "published_count": 2,
                },
                {
                    "city": "Yucca",
                    "county": "Mohave County",
                    "state": "AZ",
                    "status": "not covered yet",
                    "published_count": 0,
                },
            ])

    def test_coverage_rejects_malformed_nested_rows(self):
        from zspan_cli import flagship

        malformed = {
            "states": [{
                "state": "AZ",
                "counties": [{
                    "county": "Mohave County",
                    "cities": [{
                        "city": "Kingman",
                        "meeting_count": 2,
                        "covered": "yes",
                    }],
                }],
            }],
        }
        with mock.patch.object(
            flagship.requests, "get", return_value=_resp(200, malformed)
        ):
            with self.assertRaises(flagship.FlagshipError):
                flagship.fetch_coverage("http://x")

    def test_meeting_catalog_paginates_and_enriches_public_details(self):
        from zspan_cli import flagship

        first_id = "m_" + "A" * 22
        second_id = "m_" + "B" * 22
        calls = []

        def request_get(url, *, params, headers, timeout):
            calls.append((url, dict(params)))
            if url.endswith("/v1/catalog/meetings"):
                if params.get("cursor") == "next-page":
                    return _resp(200, {
                        "meetings": [{"public_id": second_id}],
                        "next_cursor": "",
                    })
                return _resp(200, {
                    "meetings": [{"public_id": first_id}],
                    "next_cursor": "next-page",
                })
            public_id = url.rsplit("/", 1)[-1]
            return _resp(200, {
                "public_id": public_id,
                "state": "AZ",
                "county": "Mohave County",
                "city": "Kingman",
                "title": "City Council",
                "date": "2026-08-01",
                "time": "17:00",
                "location": "Council Chambers",
                "meeting_status": "Scheduled",
                "availability": "coming_soon",
                "video_url": "https://www.youtube.com/watch?v=test",
                "documents": {
                    "agenda_url": "https://example.gov/agenda",
                    "minutes_url": "",
                    "packet_url": "",
                },
                "local_processing": {
                    "status": "ready",
                    "source_kind": "youtube",
                },
            })

        with mock.patch.object(flagship.requests, "get", side_effect=request_get):
            payload = flagship.fetch_meetings("http://x", "Kingman", year=2026)

        self.assertEqual(payload["count"], 2)
        self.assertEqual([row["public_id"] for row in payload["events"]], [
            first_id, second_id,
        ])
        self.assertEqual(
            payload["events"][0]["agenda_url"], "https://example.gov/agenda"
        )
        list_calls = [params for url, params in calls if url.endswith("/meetings")]
        self.assertEqual(list_calls, [
            {"city": "Kingman", "year": "2026"},
            {"city": "Kingman", "year": "2026", "cursor": "next-page"},
        ])

    def test_meeting_catalog_all_years_omits_year_and_rejects_cursor_loop(self):
        from zspan_cli import flagship

        responses = [
            _resp(200, {"meetings": [], "next_cursor": "same"}),
            _resp(200, {"meetings": [], "next_cursor": "same"}),
        ]
        with mock.patch.object(flagship.requests, "get", side_effect=responses) as get:
            with self.assertRaises(flagship.FlagshipError):
                flagship.fetch_meetings("http://x", "Kingman", year="all")
        self.assertNotIn("year", get.call_args_list[0].kwargs["params"])

    def test_catalog_detail_404_and_jurisdictions_shape(self):
        from zspan_cli import flagship

        with mock.patch.object(flagship.requests, "get", return_value=_resp(404, {})):
            self.assertIsNone(flagship.fetch_catalog_detail("http://x", "m_" + "A" * 22))
        with mock.patch.object(
            flagship.requests, "get", return_value=_resp(200, {"states": []})
        ):
            self.assertEqual(flagship.fetch_jurisdictions("http://x"), [])
        with mock.patch.object(
            flagship.requests, "get", return_value=_resp(200, {"states": {}})
        ):
            with self.assertRaises(flagship.FlagshipError):
                flagship.fetch_jurisdictions("http://x")

    def test_http_status_and_bearer_are_preserved_without_echoing_token(self):
        response = _resp(401, {"error": "cli auth required"})
        with mock.patch.object(flagship.requests, "get", return_value=response) as get:
            with self.assertRaises(flagship.FlagshipError) as ctx:
                flagship.fetch_cli_me("https://zspan.org", "secret-cli-token")
        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn("secret-cli-token", str(ctx.exception))
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"],
            "Bearer secret-cli-token",
        )


class TestCliAuth(_TempHome):
    STATE = "S" * 43
    VERIFIER = "V" * 86
    CODE = "C" * 43

    @staticmethod
    def _server():
        server = mock.Mock()
        server.server_address = ("127.0.0.1", 43123)
        return server

    def _login_patches(self, callback, exchange=None):
        return (
            mock.patch.object(auth, "HTTPServer", return_value=self._server()),
            mock.patch.object(
                auth.secrets,
                "token_urlsafe",
                side_effect=[self.STATE, self.VERIFIER],
            ),
            mock.patch.object(auth, "_wait_for_callback", return_value=callback),
            mock.patch.object(auth.webbrowser, "open"),
            mock.patch.object(
                auth,
                "exchange_cli_code",
                return_value=exchange or {
                    "token": "opaque-cli-token",
                    "expires_at": "2026-10-01T00:00:00Z",
                    "account": {
                        "email": "person@example.com",
                        "display_name": "Test Person",
                    },
                },
            ),
        )

    def test_state_mismatch_refuses_without_exchange(self):
        patches = self._login_patches({"state": "wrong", "code": self.CODE})
        with patches[0], patches[1], patches[2], patches[3], patches[4] as exchange:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertFalse(auth.login({}))
        exchange.assert_not_called()
        self.assertIn("state did not match", output.getvalue())
        self.assertIsNone(config.load_config())

    def test_cancel_path_is_plain_and_writes_nothing(self):
        patches = self._login_patches({"state": self.STATE, "error": "cancelled"})
        with patches[0], patches[1], patches[2], patches[3], patches[4] as exchange:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertFalse(auth.login({}))
        exchange.assert_not_called()
        self.assertIn("sign-in cancelled from the browser", output.getvalue())

    def test_exchange_failure_redacts_callback_secrets(self):
        patches = self._login_patches({"state": self.STATE, "code": self.CODE})
        failure = flagship.FlagshipError(
            f"exchange failed for {self.CODE} with {self.VERIFIER}", status=400
        )
        patches = patches[:-1] + (
            mock.patch.object(auth, "exchange_cli_code", side_effect=failure),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertFalse(auth.login({}))
        shown = output.getvalue()
        self.assertNotIn(self.CODE, shown)
        self.assertNotIn(self.VERIFIER, shown)

    def test_success_writes_only_the_auth_shape_through_save_config(self):
        patches = self._login_patches({"state": self.STATE, "code": self.CODE})
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with redirect_stdout(io.StringIO()):
                self.assertTrue(auth.login({"future": "preserved"}))
        loaded = config.load_config()
        self.assertEqual(loaded["future"], "preserved")
        self.assertEqual(loaded["auth"], {
            "token": "opaque-cli-token",
            "email": "person@example.com",
            "display_name": "Test Person",
            "expires_at": "2026-10-01T00:00:00Z",
        })


def _detail(public_id="m_1234567890AbCdEfGhIjKl", *, title="Council"):
    return {
        "public_id": public_id,
        "state": "Arizona",
        "county": "Mohave County",
        "city": "Kingman",
        "title": title,
        "date": "2026-06-15",
        "time": "",
        "location": "Council Chambers",
        "meeting_status": "Scheduled",
        "availability": "coming_soon",
        "video_url": "https://www.youtube.com/watch?v=abc",
        "documents": {
            "agenda_url": "https://example.org/agenda",
            "minutes_url": "https://example.org/minutes",
            "packet_url": "https://example.org/packet",
        },
        "local_processing": {"status": "ready", "source_kind": "youtube"},
    }


class TestResolver(_TempHome):
    PUBLIC_ID = "m_1234567890AbCdEfGhIjKl"

    def test_syntax_rejections_do_not_fetch(self):
        bad = ("x_" + "A" * 22, "m_short", "m_" + "-" * 22)
        with mock.patch.object(resolver, "fetch_catalog_detail") as fetch:
            for value in bad:
                with self.subTest(value=value), self.assertRaises(resolver.ResolveError):
                    resolver.resolve_and_import(value, {}, say=lambda _m: None)
        fetch.assert_not_called()

    def test_unknown_public_id_is_honest(self):
        with mock.patch.object(resolver, "fetch_catalog_detail", return_value=None):
            with self.assertRaises(resolver.ResolveError) as ctx:
                resolver.resolve_and_import(self.PUBLIC_ID, {}, say=lambda _m: None)
        self.assertIn("isn't in the public catalog", str(ctx.exception))

    def test_alias_is_adopted_and_fields_map_to_workspace_names(self):
        canonical = "m_ZYXWVUTSRQPONMLKJIHGFE"
        said = []
        with mock.patch.object(
            resolver, "fetch_catalog_detail", return_value=_detail(canonical)
        ), mock.patch.object(resolver.media, "assert_safe_media_url"):
            row = resolver.resolve_and_import(
                self.PUBLIC_ID, {}, say=lambda line: said.append(line)
            )
        self.assertEqual(row["public_id"], canonical)
        self.assertEqual(row["city"], "Kingman")
        self.assertEqual(row["title"], "Council")
        self.assertEqual(row["agenda_packet_url"], "https://example.org/packet")
        source = json.loads(row["source_row_json"])
        self.assertNotIn("id", source)
        self.assertEqual(source["meeting_location"], "Council Chambers")
        self.assertTrue(any("merged" in line and canonical in line for line in said))

    def test_unsafe_video_is_dropped_without_dropping_the_record(self):
        from zspan_cli import media

        detail = _detail(self.PUBLIC_ID)
        detail["video_url"] = "http://127.0.0.1/secret"
        said = []
        with mock.patch.object(resolver, "fetch_catalog_detail", return_value=detail), \
             mock.patch.object(
                 resolver.media, "assert_safe_media_url",
                 side_effect=media.MediaError("refusing localhost"),
             ):
            row = resolver.resolve_and_import(
                self.PUBLIC_ID, {}, say=lambda line: said.append(line)
            )
        self.assertEqual(row["video_url"], "")
        self.assertTrue(any("refused" in line.lower() for line in said))

    def test_reimport_is_idempotent_and_refreshes(self):
        details = [_detail(self.PUBLIC_ID, title="First"),
                   _detail(self.PUBLIC_ID, title="Refreshed")]
        with mock.patch.object(resolver, "fetch_catalog_detail", side_effect=details), \
             mock.patch.object(resolver.media, "assert_safe_media_url"):
            resolver.resolve_and_import(self.PUBLIC_ID, {}, say=lambda _m: None)
            row = resolver.resolve_and_import(self.PUBLIC_ID, {}, say=lambda _m: None)
        conn = workspace.connect()
        count = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(row["title"], "Refreshed")


# ════════════════════════════════════════════════════════════ CLI-3 ═════

from zspan_cli import gate as gate_mod
from zspan_cli import grounding, media, pipeline, synthesize, transcribe


class TestPI6Commands(_TempHome):
    PUBLIC_ID = "m_1234567890AbCdEfGhIjKl"

    def test_target_parser_accepts_both_forms_and_rejects_garbage(self):
        self.assertEqual(cli._parse_meeting_target("42"), ("local", 42))
        self.assertEqual(
            cli._parse_meeting_target(self.PUBLIC_ID), ("public", self.PUBLIC_ID)
        )
        with self.assertRaises(ValueError) as ctx:
            cli._parse_meeting_target("meeting-42")
        self.assertIn("numeric local id", str(ctx.exception))
        self.assertIn("public id", str(ctx.exception))

    def test_open_empty_and_process_share_the_public_resolver(self):
        from zspan_cli import boot, processing, serve

        accepted = config.record_processing_ack({
            "home_jurisdiction": {
                "state": "Arizona", "county": "Mohave County", "city": "Kingman",
            },
        })

        def resolve(public_id, _config, *, say):
            conn = workspace.connect()
            workspace.upsert_meeting(conn, {
                "public_id": public_id,
                "city_name": "Kingman",
                "county": "Mohave County",
                "state": "Arizona",
                "meeting_title": "Council",
                "meeting_date": "2026-07-01",
                "video_url": "https://www.youtube.com/watch?v=abc",
                "local_processing": {"status": "ready", "source_kind": "youtube"},
            }, import_source="handoff")
            conn.commit()
            row = workspace.get_meeting_by_public_id(conn, public_id)
            conn.close()
            return row

        class FakeBoot:
            def say(self, _line):
                pass

            def step(self, _label, action):
                return action()

            def finish(self, _title, _lines):
                pass

        server = mock.Mock()
        with mock.patch.object(
            resolver, "resolve_and_import", side_effect=resolve
        ) as shared, mock.patch.object(
            processing, "run_pipeline", return_value={"ok": True}
        ) as pipeline_run:
            self.assertEqual(cli.main(["process", self.PUBLIC_ID]), 0)
            pipeline_run.assert_called_once()
            with mock.patch.object(serve, "resolve_webapp_dir", return_value=Path(self._tmp.name)), \
                 mock.patch.object(
                     serve, "open_workspace", return_value=(server, "http://127.0.0.1:1/")
                 ), mock.patch.object(boot, "TerminalBoot", return_value=FakeBoot()), \
                 mock.patch("threading.Event.wait", side_effect=KeyboardInterrupt):
                self.assertEqual(
                    cli.main(["open", self.PUBLIC_ID, "--no-browser"]), 0
                )
        self.assertEqual(shared.call_count, 2)
        conn = workspace.connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0], 1)
        conn.close()
        self.assertTrue(config.has_processing_ack(accepted))

    def test_home_direct_uses_public_jurisdictions(self):
        states = [{
            "state": "Arizona",
            "counties": [{
                "county": "Mohave County",
                "cities": [{"city": "Kingman", "meeting_count": 8, "covered": True}],
            }],
        }]
        with mock.patch.object(cli, "fetch_jurisdictions", return_value=states):
            self.assertEqual(cli.main(["home", "--city", "kingman"]), 0)
        self.assertEqual(config.home_jurisdiction(config.load_config())["city"], "Kingman")


class TestOpenBootInterrupts(_TempHome):
    def _seed_workspace(self):
        config.save_config(config.record_processing_ack({}))
        conn = workspace.connect()
        workspace.upsert_meeting(conn, _event(1, "Council", "2026-07-01"))
        conn.commit()
        conn.close()

    def test_open_exits_cleanly_when_boot_step_is_interrupted(self):
        from zspan_cli import boot, serve

        self._seed_workspace()

        class InterruptedBoot:
            def say(self, _message):
                pass

            def step(self, _label, _action):
                raise KeyboardInterrupt

        with mock.patch.object(serve, "resolve_webapp_dir",
                               return_value=Path(self._tmp.name)), \
             mock.patch.object(boot, "TerminalBoot",
                               return_value=InterruptedBoot()), \
             mock.patch.object(cli, "_say") as say:
            self.assertEqual(cli.main(["open", "--no-browser"]), 0)
        say.assert_any_call(
            "Stopped. Your workspace is untouched — `zspan open` brings it back."
        )

    def test_open_shuts_server_down_when_finish_is_interrupted(self):
        from zspan_cli import boot, serve

        self._seed_workspace()
        server = mock.Mock()

        class InterruptedBoot:
            def say(self, _message):
                pass

            def step(self, _label, action):
                return action()

            def finish(self, _header, _lines):
                raise KeyboardInterrupt

        with mock.patch.object(serve, "resolve_webapp_dir",
                               return_value=Path(self._tmp.name)), \
             mock.patch.object(
                 serve, "open_workspace",
                 return_value=(server, "http://127.0.0.1:8741/"),
             ), mock.patch.object(boot, "TerminalBoot",
                                  return_value=InterruptedBoot()), \
             mock.patch.object(cli, "_say") as say:
            self.assertEqual(cli.main(["open", "--no-browser"]), 0)
        server.shutdown.assert_called_once_with()
        say.assert_any_call(
            "Stopped. Your workspace is untouched — `zspan open` brings it back."
        )


class TestProtocolLinks(_TempHome):
    PUBLIC_ID = "m_1234567890AbCdEfGhIjKl"

    def test_parse_scheme_url_accepts_meeting_and_ignores_other_targets(self):
        self.assertEqual(
            protocol.parse_scheme_url(f"zspan://meeting/{self.PUBLIC_ID}"),
            self.PUBLIC_ID,
        )
        self.assertEqual(
            protocol.parse_scheme_url(f"zspan://meeting/{self.PUBLIC_ID}/"),
            self.PUBLIC_ID,
        )
        for value in (self.PUBLIC_ID, "7", "https://zspan.org/meeting/example"):
            with self.subTest(value=value):
                self.assertIsNone(protocol.parse_scheme_url(value))

    def test_parse_scheme_url_names_unsupported_route_and_invalid_ids(self):
        with self.assertRaises(protocol.ProtocolError) as ctx:
            protocol.parse_scheme_url("zspan://contribute/x")
        self.assertEqual(
            str(ctx.exception),
            "this build understands zspan://meeting/… links only.",
        )
        for value in ("zspan://meeting/not-an-id", "zspan://meeting/"):
            with self.subTest(value=value), self.assertRaises(
                protocol.ProtocolError
            ) as ctx:
                protocol.parse_scheme_url(value)
            self.assertIn("must contain a public id", str(ctx.exception))

    def test_cli_target_parser_routes_links_and_keeps_protocol_failure(self):
        self.assertEqual(
            cli._parse_meeting_target(f"zspan://meeting/{self.PUBLIC_ID}"),
            ("public", self.PUBLIC_ID),
        )
        with self.assertRaises(ValueError) as ctx:
            cli._parse_meeting_target("zspan://contribute/task_1")
        self.assertEqual(
            str(ctx.exception),
            "this build understands zspan://meeting/… links only.",
        )

    def test_invocation_prefers_console_script_and_has_module_fallback(self):
        with mock.patch.object(protocol.shutil, "which", return_value="/opt/bin/zspan"):
            self.assertEqual(protocol._zspan_invocation(), ["/opt/bin/zspan"])
        with mock.patch.object(protocol.shutil, "which", return_value=None), \
             mock.patch.object(protocol.sys, "executable", "/opt/Python/bin/python"):
            self.assertEqual(
                protocol._zspan_invocation(),
                ["/opt/Python/bin/python", "-m", "zspan_cli"],
            )

    def test_applescript_source_quotes_invocation_and_url(self):
        invocation = ["/Applications/Z SPAN/bin/zspan"]
        source = protocol._applescript_source(invocation)
        self.assertIn(shlex.quote(invocation[0]), source)
        self.assertIn(' & " open " & quoted form of theURL', source)
        self.assertIn("on open location theURL", source)

    def test_plist_injection_sets_bundle_and_url_types(self):
        path = Path(self._tmp.name) / "Info.plist"
        with path.open("wb") as handle:
            plistlib.dump({"CFBundleName": "Z-SPAN Handler"}, handle)
        protocol._inject_url_types(path)
        with path.open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info["CFBundleIdentifier"], "org.zspan.handler")
        self.assertEqual(info["CFBundleURLTypes"], [{
            "CFBundleURLName": "org.zspan.meeting",
            "CFBundleURLSchemes": ["zspan"],
        }])
        self.assertEqual(info["CFBundleName"], "Z-SPAN Handler")

    def test_windows_command_quotes_executable_and_url_argument(self):
        command = protocol._windows_command([
            r"C:\Program Files\Z-SPAN\zspan.exe"
        ])
        self.assertEqual(
            command,
            '"C:\\Program Files\\Z-SPAN\\zspan.exe" open "%1"',
        )

    def test_desktop_entry_carries_scheme_and_open_invocation(self):
        entry = protocol._desktop_entry(["/opt/Z SPAN/zspan"])
        self.assertIn("MimeType=x-scheme-handler/zspan;", entry)
        self.assertIn("Exec='/opt/Z SPAN/zspan' open %u", entry)
        self.assertIn("Terminal=true", entry)

    def test_register_rejects_an_unsupported_platform_without_side_effects(self):
        with mock.patch.object(protocol.sys, "platform", "plan9"), \
             mock.patch.object(protocol, "_zspan_invocation", return_value=["zspan"]):
            with self.assertRaises(protocol.ProtocolError) as ctx:
                protocol.register()
        self.assertIn("not supported", str(ctx.exception))
        self.assertIn("plan9", str(ctx.exception))

    def test_register_protocol_remove_calls_only_unregister(self):
        summary = "Nothing was registered in this test."
        with mock.patch.object(protocol, "unregister", return_value=summary) as remove, \
             mock.patch.object(protocol, "register") as register, \
             mock.patch.object(cli, "_say") as say:
            self.assertEqual(cli.main(["register-protocol", "--remove"]), 0)
        remove.assert_called_once_with()
        register.assert_not_called()
        say.assert_called_once_with(summary)


class TestMediaClassify(unittest.TestCase):
    def test_youtube_forms(self):
        for url in (
            "https://www.youtube.com/watch?v=abc123",
            "https://youtu.be/abc123",
            "https://m.youtube.com/watch?v=abc123",
        ):
            self.assertEqual(media.classify_video_url(url), media.KIND_YOUTUBE, url)

    def test_direct_media_with_query_string(self):
        self.assertEqual(
            media.classify_video_url("https://city.gov/archive/council.mp4?t=99"),
            media.KIND_DIRECT_MEDIA,
        )
        self.assertEqual(
            media.classify_video_url("https://cdn.example.com/audio/m.m4a"),
            media.KIND_DIRECT_MEDIA,
        )

    def test_vendor_player_pages_named_honestly(self):
        for url in (
            "https://city.granicus.com/MediaPlayer.php?view_id=2&clip_id=99",
            "https://city.granicus.com/player/clip/1234",
            "https://media.city.gov/stream.asx",
        ):
            kind = media.classify_video_url(url)
            self.assertEqual(kind, media.KIND_VENDOR_PAGE, url)
            self.assertIn("vendor player page", media.unsupported_reason(kind, url))

    def test_unknown_is_unknown_not_guessed(self):
        self.assertEqual(
            media.classify_video_url("https://city.gov/meetings/video-portal"),
            media.KIND_UNKNOWN,
        )
        self.assertEqual(media.classify_video_url(""), media.KIND_UNKNOWN)


class TestTranscribeMerge(_TempHome):
    def test_merge_offsets_and_word_normalization(self):
        chunk_results = [
            {"words": [{"word": " Hello ", "start": 0.0, "end": 0.5},
                       {"word": "", "start": 0.5, "end": 0.6}],
             "duration_seconds": 300.0, "language": "en"},
            {"words": [{"word": "council", "start": 1.0, "end": 1.5}],
             "duration_seconds": 120.0, "language": "en"},
        ]
        merged = transcribe.merge_chunk_words(chunk_results, chunk_seconds=300)
        self.assertEqual([w["word"] for w in merged["words"]], ["Hello", "council"])
        # Second chunk's words carry offset = 1 × 300s (the flagship's
        # copy-codec fixed-offset merge).
        self.assertAlmostEqual(merged["words"][1]["start"], 301.0)
        self.assertAlmostEqual(merged["duration_seconds"], 420.0)

    def test_load_transcript_f8_semantics(self):
        path = Path(self._tmp.name) / "t.json"
        self.assertIsNone(transcribe.load_transcript(path))          # absent = normal
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(transcribe.TranscribeError):          # corrupt = loud
            transcribe.load_transcript(path)
        path.write_text('{"no_words": true}', encoding="utf-8")
        with self.assertRaises(transcribe.TranscribeError):          # wrong shape = loud
            transcribe.load_transcript(path)
        path.write_text('{"words": [{"word": "hi", "start": 0, "end": 1}]}', encoding="utf-8")
        self.assertEqual(len(transcribe.load_transcript(path)["words"]), 1)


def _words(*tokens, spacing=1.0):
    return [
        {"word": t, "start": i * spacing, "end": i * spacing + 0.5}
        for i, t in enumerate(tokens)
    ]


class TestChunking(unittest.TestCase):
    ONE_TOKEN_PER_WORD = staticmethod(lambda word: 1)

    def test_boundaries_overlap_and_timecodes(self):
        words = _words(*[f"w{i}" for i in range(25)])
        chunks = pipeline.chunk_transcript(
            words, token_counter=self.ONE_TOKEN_PER_WORD, exact=True,
            target_tokens=10, overlap_tokens=3,
        )
        self.assertGreaterEqual(len(chunks), 3)
        self.assertEqual(chunks[0].text.split()[0], "w0")
        # Overlap: chunk 1 starts ~3 tokens before chunk 0's end (word 10).
        self.assertEqual(chunks[1].text.split()[0], "w7")
        # Timecodes come from the word timings.
        self.assertAlmostEqual(chunks[0].start_seconds, 0.0)
        self.assertAlmostEqual(chunks[0].end_seconds, 9.5)
        # Every chunk advances (the anti-oscillation guard).
        starts = [c.start_seconds for c in chunks]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(starts)), len(starts))

    def test_pathological_overlap_still_advances(self):
        words = _words(*[f"w{i}" for i in range(6)])
        chunks = pipeline.chunk_transcript(
            words, token_counter=self.ONE_TOKEN_PER_WORD, exact=True,
            target_tokens=2, overlap_tokens=10,   # overlap > target
        )
        self.assertTrue(all(
            b.chunk_index == a.chunk_index + 1 for a, b in zip(chunks, chunks[1:])
        ))
        self.assertLessEqual(len(chunks), len(words))

    def test_approximate_mode_scales_budgets(self):
        words = _words(*[f"w{i}" for i in range(40)])
        exact_chunks = pipeline.chunk_transcript(
            words, token_counter=self.ONE_TOKEN_PER_WORD, exact=True,
            target_tokens=20, overlap_tokens=0,
        )
        approx_chunks = pipeline.chunk_transcript(
            words, token_counter=self.ONE_TOKEN_PER_WORD, exact=False,
            target_tokens=20, overlap_tokens=0,
        )
        # exact=False divides the budget by the tokens-per-word estimate
        # (20 → 15 words/chunk here), so chunks get SMALLER — safety under
        # the model's 512-token ceiling.
        self.assertEqual(len(exact_chunks), 2)
        self.assertEqual(len(approx_chunks), 3)


class TestSynthesisEnvelope(unittest.TestCase):
    def _chunk(self, i=0, start=754.0):
        return pipeline.RetrievedChunk(
            chunk_index=i, text="the council discussed the budget",
            start_seconds=start, end_seconds=start + 30.0, score=0.9,
        )

    def test_envelope_carries_the_flagship_sections(self):
        prompt = synthesize.build_synthesis_prompt(
            output_type="synopsis", canonical_prompt="CANON BODY",
            meeting_id=42, chunks=[self._chunk()],
        )
        self.assertIn("You are extracting structured output", prompt)
        self.assertIn("The output type is `synopsis`", prompt)
        self.assertIn("RETRIEVED CONTEXT — top-1 chunks", prompt)
        self.assertIn("(meeting_id=42)", prompt)
        self.assertIn("[chunk_index=0 timecode=12:34 start_seconds=754.0]", prompt)
        self.assertIn("CANONICAL PROMPT:\nCANON BODY", prompt)
        self.assertIn("FINAL INSTRUCTION", prompt)
        self.assertIn("rather than fabricating content", prompt)

    def test_canonical_prompt_loader_strips_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "synopsis.md"
            p.write_text("---\nstatus: canonical\n---\nTHE PROMPT BODY\n", encoding="utf-8")
            body = synthesize.load_canonical_prompt("synopsis", Path(d))
        self.assertEqual(body, "THE PROMPT BODY")

    def test_missing_prompt_is_loud(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(synthesize.SynthesisError):
                synthesize.load_canonical_prompt("synopsis", Path(d))

    def test_prompts_dir_env_override_wins(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"ZSPAN_PROMPTS_DIR": d}):
                self.assertEqual(synthesize.resolve_prompts_dir(None), Path(d))

    def test_rendered_set_is_the_broadcastpage_render_list(self):
        # The operator's Q3 answer, verified against BroadcastPage.tsx at
        # CLI-3 open: these four render; newsletter + whats_next generate
        # flagship-side but do NOT render (D-157 hide-not-delete).
        self.assertEqual(
            synthesize.RENDERED_OUTPUT_TYPES,
            ["synopsis", "key_decisions", "community_calls_to_action", "episode_tagline"],
        )
        for t in synthesize.RENDERED_OUTPUT_TYPES:
            self.assertIn(t, synthesize.OUTPUT_QUERIES)


class TestProviderHTTP(unittest.TestCase):
    KEY = "AIzaFakeKeyForTests000000"

    def test_gemini_payload_and_parse(self):
        body = {"candidates": [{"content": {"parts": [{"text": " out "}]}}]}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(200, body)) as p:
            out = synthesize.synthesize("gemini", self.KEY, "gemini-x", "PROMPT")
        self.assertEqual(out, "out")
        call = p.call_args
        self.assertIn("gemini-x:generateContent", call.args[0])
        self.assertEqual(call.kwargs["headers"]["X-Goog-Api-Key"], self.KEY)
        self.assertEqual(
            call.kwargs["json"]["contents"][0]["parts"][0]["text"], "PROMPT"
        )

    def test_gemini_key_is_never_put_in_request_url(self):
        body = {"candidates": [{"content": {"parts": [{"text": "out"}]}}]}
        with mock.patch.object(
            synthesize.requests, "post", return_value=_resp(200, body)
        ) as post:
            synthesize.synthesize("gemini", self.KEY, "gemini-x", "PROMPT")

        call = post.call_args
        self.assertNotIn(self.KEY, call.args[0])
        self.assertNotIn("params", call.kwargs)
        self.assertEqual(call.kwargs["headers"]["X-Goog-Api-Key"], self.KEY)

    def test_openai_payload_and_parse(self):
        body = {"choices": [{"message": {"content": "out"}}]}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(200, body)) as p:
            out = synthesize.synthesize("openai", "sk-x", "gpt-4o-mini", "PROMPT")
        self.assertEqual(out, "out")
        call = p.call_args
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer sk-x")
        self.assertEqual(call.kwargs["json"]["model"], "gpt-4o-mini")

    def test_anthropic_payload_and_parse(self):
        body = {"content": [{"type": "text", "text": "out"}]}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(200, body)) as p:
            out = synthesize.synthesize("anthropic", "sk-ant-x", "claude-x", "PROMPT")
        self.assertEqual(out, "out")
        call = p.call_args
        self.assertEqual(call.kwargs["headers"]["x-api-key"], "sk-ant-x")
        self.assertIn("anthropic-version", call.kwargs["headers"])
        self.assertIn("max_tokens", call.kwargs["json"])

    def test_empty_completion_is_an_error_never_a_blank_output(self):
        body = {"choices": [{"message": {"content": "   "}}]}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(200, body)):
            with self.assertRaises(synthesize.SynthesisError):
                synthesize.synthesize("openai", "sk-x", "m", "PROMPT")

    def test_provider_error_message_surfaces(self):
        body = {"error": {"message": "quota exhausted"}}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(429, body)):
            with self.assertRaises(synthesize.SynthesisError) as ctx:
                synthesize.synthesize("gemini", self.KEY, "m", "PROMPT")
        self.assertIn("quota exhausted", str(ctx.exception))

    def test_gemini_error_cannot_echo_header_key(self):
        body = {"error": {"message": f"X-Goog-Api-Key: {self.KEY} is invalid"}}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(400, body)):
            with self.assertRaises(synthesize.SynthesisError) as ctx:
                synthesize.synthesize("gemini", self.KEY, "m", "PROMPT")
        self.assertNotIn(self.KEY, str(ctx.exception))

    def test_network_error_reports_type_only_never_key(self):
        exc = requests.exceptions.ConnectionError(f"boom https://x?key={self.KEY}")
        with mock.patch.object(synthesize.requests, "post", side_effect=exc):
            with self.assertRaises(synthesize.SynthesisError) as ctx:
                synthesize.synthesize("gemini", self.KEY, "m", "PROMPT")
        self.assertNotIn(self.KEY, str(ctx.exception))
        self.assertIn("ConnectionError", str(ctx.exception))

    def test_unknown_provider_is_honest(self):
        with self.assertRaises(synthesize.SynthesisError):
            synthesize.synthesize("nonsense", "k", "m", "PROMPT")

    def test_chat_envelope_carries_two_roles_and_generation_config(self):
        """The Librarian chat path keeps the flagship's two-role envelope
        (system prompt separate from the user message) + the panel's
        generation knobs — per provider wire shape."""
        body = {"choices": [{"message": {"content": "out"}},],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(200, body)) as p:
            result = synthesize.synthesize_chat(
                "openai", "sk-x", "gpt-4o-mini",
                system_prompt="SYS", user_message="USER",
                max_tokens=512, temperature=0.3,
            )
        sent = p.call_args.kwargs["json"]
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(sent["messages"][1], {"role": "user", "content": "USER"})
        self.assertEqual(sent["max_tokens"], 512)
        self.assertEqual(sent["temperature"], 0.3)
        self.assertEqual(result, {"answer": "out", "input_tokens": 11, "output_tokens": 7})

        gem_body = {"candidates": [{"content": {"parts": [{"text": "g"}]}}],
                    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(200, gem_body)) as p:
            result = synthesize.synthesize_chat(
                "gemini", self.KEY, "gemini-x",
                system_prompt="SYS", user_message="USER",
            )
        self.assertNotIn(self.KEY, p.call_args.args[0])
        self.assertNotIn("params", p.call_args.kwargs)
        self.assertEqual(p.call_args.kwargs["headers"]["X-Goog-Api-Key"], self.KEY)
        sent = p.call_args.kwargs["json"]
        self.assertEqual(sent["systemInstruction"]["parts"][0]["text"], "SYS")
        self.assertEqual(sent["generationConfig"]["maxOutputTokens"], 1024)
        self.assertEqual(result["input_tokens"], 3)

        ant_body = {"content": [{"type": "text", "text": "a"}],
                    "usage": {"input_tokens": 5, "output_tokens": 4}}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(200, ant_body)) as p:
            result = synthesize.synthesize_chat(
                "anthropic", "sk-ant-x", "claude-x",
                system_prompt="SYS", user_message="USER",
            )
        sent = p.call_args.kwargs["json"]
        self.assertEqual(sent["system"], "SYS")
        self.assertEqual(result["output_tokens"], 4)

    def test_chat_empty_completion_and_key_hygiene(self):
        empty = {"choices": [{"message": {"content": ""}}]}
        with mock.patch.object(synthesize.requests, "post", return_value=_resp(200, empty)):
            with self.assertRaises(synthesize.SynthesisError):
                synthesize.synthesize_chat(
                    "openai", "sk-x", "m", system_prompt="s", user_message="u"
                )
        exc = requests.exceptions.ConnectionError(f"boom https://x?key={self.KEY}")
        with mock.patch.object(synthesize.requests, "post", side_effect=exc):
            with self.assertRaises(synthesize.SynthesisError) as ctx:
                synthesize.synthesize_chat(
                    "gemini", self.KEY, "m", system_prompt="s", user_message="u"
                )
        self.assertNotIn(self.KEY, str(ctx.exception))


# A transcript with one real vote moment ("all in favor ... motion
# carries" = strong founders), a resolution ref in spoken form, and a
# verbatim sentence to quote.
_VOTE_TRANSCRIPT = _words(
    "we", "will", "now", "consider", "resolution", "number", "r", "-15",
    "regarding", "the", "water", "contract", "all", "those", "in", "favor",
    "say", "aye", "the", "motion", "carries", "unanimously", "thank", "you",
    "the", "library", "will", "open", "on", "saturday", "morning",
)

# A transcript with talk but ZERO vote moments (the m105310 class).
_NO_VOTE_TRANSCRIPT = _words(
    "staff", "presented", "an", "information", "report", "about", "the",
    "airport", "master", "plan", "no", "action", "was", "taken", "today",
)


class TestGrounding(unittest.TestCase):
    def test_ref_extraction_two_part_suppression(self):
        refs = grounding.extract_refs("Ordinance 2026-6 and Resolution R-15")
        self.assertIn("ord-2026-6", refs)
        self.assertIn("r-15", refs)
        self.assertNotIn("ord-2026", refs)   # span suppression

    def test_ref_grounds_against_spoken_form(self):
        t = grounding.build_transcript_index(_VOTE_TRANSCRIPT)
        pattern = grounding.ref_grounding_pattern("r-15")
        self.assertIsNotNone(__import__("re").search(pattern, t.low))

    def test_municipal_case_id_extraction(self):
        cases = {
            "Approved CUP 26 0001 to construct": {"CUP 26 0001"},
            "Recommended approval of Z026 0002, a text amendment": {"Z026 0002"},
            "Continued item AB 25 0005 off calendar": {"AB 25 0005"},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(grounding.extract_refs(text), expected)

        negatives = (
            "a 115,200 square foot facility",
            "on Apr 21, 2026",
            "$19.99",
            "the commission discussed landscaping requirements",
        )
        for text in negatives:
            with self.subTest(text=text):
                self.assertEqual(grounding.extract_refs(text), set())

    def test_municipal_case_id_grounding_renderings(self):
        pattern = grounding.ref_grounding_pattern("CUP 26 0001")
        for rendering in ("cup 26 0001", "cup 26-0001", "cup 26 1"):
            with self.subTest(rendering=rendering):
                self.assertIsNotNone(__import__("re").search(pattern, rendering))
        for incomplete in ("cup", "26 0001"):
            with self.subTest(incomplete=incomplete):
                self.assertIsNone(__import__("re").search(pattern, incomplete))

    def test_municipal_case_id_negative_grammar_lockdown(self):
        negatives = (
            "FY 2026", "US 101", "SR 260", "SB 1487", "HB 2721",
            "COVID 19", "Item 3", "Phase 2", "District 1",
            "Apr 21, 2026", "$115,200", "AB 25-0005-12-7",
        )
        for text in negatives:
            with self.subTest(text=text):
                self.assertEqual(grounding.extract_refs(text), set())
        self.assertEqual(grounding.extract_refs("Resolution R-1487"), {"r-1487"})

    def test_dollar_haystack_strips_whisper_separators(self):
        words = _words("the", "budget", "is", "$199", ",750", ",036", "total")
        t = grounding.build_transcript_index(words)
        self.assertIn("199750036", t.digits)
        self.assertEqual(grounding.extract_dollars("costs $199,750,036 now"),
                         {"199750036"})

    def test_quoted_span_extraction_thresholds(self):
        spans = grounding.extract_quoted_spans(
            'He said "the library will open on saturday morning" and "hi".'
        )
        self.assertEqual(len(spans), 1)   # "hi" is below the check floor

    def test_vote_moment_needs_a_strong_founder(self):
        t = grounding.build_transcript_index(_VOTE_TRANSCRIPT)
        self.assertGreaterEqual(t.vote_moment_count, 1)
        # A bare numeric tally is a weak anchor — weak-only clusters are
        # dropped (the probe-3 false-positive fix).
        weak_only = _words("the", "score", "was", "5", "to", "0", "yesterday")
        t2 = grounding.build_transcript_index(weak_only)
        self.assertEqual(t2.vote_moment_count, 0)
        t3 = grounding.build_transcript_index(_NO_VOTE_TRANSCRIPT)
        self.assertEqual(t3.vote_moment_count, 0)


class TestGate(unittest.TestCase):
    def setUp(self):
        self.t = grounding.build_transcript_index(_VOTE_TRANSCRIPT)
        self.t_no_vote = grounding.build_transcript_index(_NO_VOTE_TRANSCRIPT)

    def test_clean_output_passes_untouched(self):
        content = "1. The council approved the water contract in Resolution R-15.\n\n" \
                  "2. The library will open on Saturday."
        final, report = gate_mod.gate_and_retry(
            "key_decisions", content, self.t, progress=lambda _m: None,
        )
        self.assertEqual(final, content)
        self.assertEqual(report.status, "observed_clean")
        self.assertFalse(report.retried)

    def test_key_decisions_audit_block_is_stripped_before_gate(self):
        prose = "1. The council approved the water contract in Resolution R-15."
        content = (
            prose
            + '\n\n<!-- audit\n[{"index": 1, "rationale": "record evidence"}]'
            + "\naudit -->"
        )
        final, report = gate_mod.gate_and_retry(
            "key_decisions", content, self.t, progress=lambda _m: None,
        )
        self.assertEqual(final, prose)
        self.assertEqual(report.status, "observed_clean")
        self.assertNotIn("audit", final)

    def test_audit_only_key_decisions_is_honest_empty(self):
        content = "<!-- audit\n[]\naudit -->"
        retry = mock.Mock(side_effect=AssertionError("empty is not a gate failure"))
        final, report = gate_mod.gate_and_retry(
            "key_decisions", content, self.t,
            resynthesize=retry, progress=lambda _m: None,
        )
        self.assertEqual(final, "")
        self.assertEqual(report.status, "observed_empty")
        retry.assert_not_called()

    def test_fabricated_ref_is_observed_without_retry_or_rewrite(self):
        bad = "1. The council adopted Resolution R-99 for the new stadium."
        resynth = mock.Mock(side_effect=AssertionError("audit mode must not retry"))

        final, report = gate_mod.gate_and_retry(
            "key_decisions", bad, self.t, resynthesize=resynth,
            progress=lambda _m: None,
        )
        self.assertEqual(final, bad)
        self.assertEqual(report.status, "observed_findings")
        self.assertFalse(report.retried)
        self.assertTrue(any("r-99" in f for f in report.determinate_failures))
        self.assertEqual(report.stripped_units, [])
        resynth.assert_not_called()

    def test_multiple_units_survive_findings_without_renumbering(self):
        bad = ("1. The council adopted Resolution R-99 for the new stadium.\n\n"
               "2. The council approved the water contract in Resolution R-15.")
        final, report = gate_mod.gate_and_retry(
            "key_decisions", bad, self.t, resynthesize=lambda _f: bad,
            progress=lambda _m: None,
        )
        self.assertEqual(report.status, "observed_findings")
        self.assertEqual(final, bad)
        self.assertIn("1. The council adopted Resolution R-99", final)
        self.assertIn("2. The council approved", final)
        self.assertEqual(report.stripped_units, [])

    def test_spoken_instrument_number_finding_never_strips_decision(self):
        # Flagship 103753-class regression: Whisper can render an instrument
        # number in words while the synthesis prints its digits. The textual
        # detector may observe a missing reference, but it cannot rewrite the
        # private civic output.
        spoken = grounding.build_transcript_index([
            {"word": word, "start": float(i), "end": float(i + 1)}
            for i, word in enumerate(
                "the council adopted resolution number two thousand two hundred "
                "twenty four without objection".split()
            )
        ])
        content = "1. The council adopted Resolution 2224 without objection."
        final, report = gate_mod.gate_and_retry(
            "key_decisions", content, spoken, progress=lambda _m: None,
        )
        self.assertEqual(final, content)
        self.assertEqual(report.status, "observed_findings")
        self.assertTrue(any("r-2224" in f for f in report.determinate_failures))
        self.assertEqual(report.stripped_units, [])
        self.assertFalse(report.retried)

    def test_uncheckable_dollar_never_refutes(self):
        content = "1. The council approved a $84,000,000 septic to sewer conversion."
        final, report = gate_mod.gate_and_retry(
            "key_decisions", content, self.t, progress=lambda _m: None,
        )
        self.assertEqual(report.status, "observed_clean")
        self.assertEqual(final, content)
        self.assertTrue(any("spoken-number gap" in n for n in report.uncheckable_notes))

    def test_absent_case_id_is_uncheckable_and_does_not_change_verdict(self):
        content = "1. Continued item CUP 26 0001 off calendar."
        verdict = gate_mod.run_gate("key_decisions", content, self.t)[0]
        self.assertEqual(verdict.raw, "Continued item CUP 26 0001 off calendar.")
        self.assertEqual(verdict.failures, [])
        self.assertEqual(verdict.uncheckable, [
            "case id CUP 26 0001 not verbatim-locatable (spoken-rendering gap)"
        ])

        final, report = gate_mod.gate_and_retry(
            "key_decisions", content, self.t, progress=lambda _m: None,
        )
        self.assertEqual(final, content)
        self.assertEqual(report.status, "observed_clean")
        self.assertFalse(report.retried)

    def test_missing_ordinance_ref_remains_determinate(self):
        verdict = gate_mod.run_gate(
            "synopsis", "Introduced Ordinance 2026-14.", self.t,
        )[0]
        self.assertEqual(verdict.uncheckable, [])
        self.assertEqual(
            verdict.failures,
            ["reference ord-2026-14 does not appear in the record"],
        )

    def test_quote_must_be_verbatim(self):
        content = 'The mayor said "we will absolutely never fund this project again".'
        final, report = gate_mod.gate_and_retry(
            "synopsis", content, self.t, progress=lambda _m: None,
        )
        self.assertEqual(report.status, "observed_findings")
        self.assertEqual(final, content)
        content_ok = 'The record notes "the library will open on saturday morning".'
        final2, report2 = gate_mod.gate_and_retry(
            "synopsis", content_ok, self.t, progress=lambda _m: None,
        )
        self.assertEqual(report2.status, "observed_clean")
        self.assertEqual(final2, content_ok)

    def test_vote_claim_in_voteless_record_is_refuted(self):
        content = "1. The council approved the airport master plan."
        final, report = gate_mod.gate_and_retry(
            "key_decisions", content, self.t_no_vote, progress=lambda _m: None,
        )
        self.assertEqual(final, content)
        self.assertEqual(report.status, "observed_findings")
        self.assertTrue(any("zero deterministic vote moments" in f
                            for f in report.determinate_failures))
        # The same claim against a record WITH vote moments passes through
        # (per-claim proximity stays uncheckable at v0 — conservative).
        _f2, report2 = gate_mod.gate_and_retry(
            "key_decisions", content, self.t, progress=lambda _m: None,
        )
        self.assertEqual(report2.status, "observed_clean")

    def test_ccta_json_findings_preserve_every_element(self):
        # The real element shape from the CCTA prompt — quote_text is the
        # claimed-verbatim field (the category's own contract).
        content = json.dumps([
            {"speaker_name": "Tami Ring", "speaker_role": "invited org leader",
             "quote_text": "the library will open on saturday morning",
             "ask_kind": "volunteer"},
            {"speaker_name": "Unknown", "speaker_role": "official",
             "quote_text": "we demand every resident pay double taxes",
             "ask_kind": "donation"},
        ])
        final, report = gate_mod.gate_and_retry(
            "community_calls_to_action", content, self.t,
            resynthesize=lambda _f: content, progress=lambda _m: None,
        )
        self.assertEqual(report.status, "observed_findings")
        self.assertEqual(final, content)
        survivors = json.loads(final)
        self.assertEqual(len(survivors), 2)
        self.assertEqual(report.stripped_units, [])

    def test_ccta_json_field_names_never_read_as_quotes(self):
        # A clean element must pass even though its JSON encoding is full
        # of quote marks — the gate checks values, never the encoding.
        content = json.dumps([{
            "speaker_name": "Tami Ring", "speaker_role": "invited org leader",
            "quote_text": "the library will open on saturday morning",
            "ask_kind": "volunteer", "actionable_hook": "come help shelve",
            "deadline": None, "contact": None,
            "video_timestamp_seconds": 1847.3, "chunk_index": 12,
        }])
        final, report = gate_mod.gate_and_retry(
            "community_calls_to_action", content, self.t, progress=lambda _m: None,
        )
        self.assertEqual(report.status, "observed_clean")
        self.assertEqual(json.loads(final)[0]["chunk_index"], 12)

    def test_resynthesize_callback_is_never_invoked(self):
        bad = "1. The council adopted Resolution R-99."
        broken = mock.Mock(side_effect=synthesize.SynthesisError("provider down"))

        final, report = gate_mod.gate_and_retry(
            "key_decisions", bad, self.t, resynthesize=broken,
            progress=lambda _m: None,
        )
        self.assertEqual(report.status, "observed_findings")
        self.assertEqual(final, bad)
        self.assertFalse(report.retried)
        broken.assert_not_called()


class TestProcessCommand(_TempHome):
    MEETING_ID = 7

    def _seed_workspace(self, video="https://www.youtube.com/watch?v=abc"):
        config.save_config({
            "synthesis_provider": "gemini",
            "api_keys": {"gemini": "AIzaFakeKeyForTests000000"},
            "picked_city": {"city": "Kingman", "county": "Mohave County",
                            "state": "AZ", "status": "covered"},
            "local_processing_ack": {"version": config.PROCESSING_ACK_VERSION,
                                     "accepted_at": "2026-07-13T00:00:00Z"},
            "auth": {
                "token": "cli-test-token",
                "email": "person@example.com",
                "display_name": "Test Person",
                "expires_at": "2026-10-01T00:00:00Z",
            },
        })
        conn = workspace.connect()
        workspace.upsert_meeting(conn, _event(self.MEETING_ID, "Council", "2026-07-01", video=video))
        self.MEETING_ID = conn.execute(
            "SELECT id FROM meetings WHERE flagship_row_id = ?", (self.MEETING_ID,)
        ).fetchone()["id"]
        conn.commit()
        conn.close()

    def _seed_transcript(self):
        tdir = Path(self._tmp.name) / "transcripts"
        tdir.mkdir(parents=True, exist_ok=True)
        payload = {
            "words": _VOTE_TRANSCRIPT,
            "duration_seconds": 31.0,
            "language": "en",
            "source_url": "https://www.youtube.com/watch?v=abc",
        }
        (tdir / f"{self.MEETING_ID}.json").write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _fake_embeddings():
        import numpy as np

        def fake_embed_texts(texts, progress=lambda _m: None):
            out = np.zeros((len(texts), pipeline.VECTOR_DIM), dtype=np.float32)
            for i in range(len(texts)):
                out[i, i % pipeline.VECTOR_DIM] = 1.0
            return out

        def fake_embed_query(_query):
            v = np.zeros(pipeline.VECTOR_DIM, dtype=np.float32)
            v[0] = 1.0
            return v

        return fake_embed_texts, fake_embed_query

    def _run_process(
        self, *extra, synth_side_effect=None, contribution_side_effect=None
    ):
        from zspan_cli import processing

        fake_texts, fake_query = self._fake_embeddings()
        synth = mock.Mock(side_effect=synth_side_effect) if synth_side_effect \
            else mock.Mock(return_value="The council discussed the water contract.")
        submit_effect = contribution_side_effect or _accepted_private_contribution
        # The embed-disabled video rescue probes the network — inert in
        # the pipeline tests; it has its own dedicated test.
        with mock.patch.object(pipeline, "load_token_counter",
                               return_value=(lambda _w: 1, True)), \
             mock.patch.object(pipeline, "embed_texts", side_effect=fake_texts), \
             mock.patch.object(pipeline, "embed_query", side_effect=fake_query), \
             mock.patch.object(processing, "ensure_watchable_video"), \
             mock.patch.object(processing, "fetch_cli_me", return_value={
                 "ok": True,
                 "account": {"email": "person@example.com", "display_name": "Test Person"},
                 "expires_at": "2026-10-01T00:00:00Z",
             }), \
             mock.patch.object(processing, "register_generation", return_value={
                 "generation_public_id": "g_1234567890AbCdEfGhIjKl",
                 "ribbon_token": "ABCDEFG2",
                 "status": "registered",
             }), \
             mock.patch.object(
                 processing,
                 "submit_private_contribution",
                 side_effect=submit_effect,
             ), \
             mock.patch.object(synthesize, "synthesize", synth):
            rc = cli.main(["process", *extra])
        return rc, synth

    def test_no_config_fails_honestly(self):
        self.assertEqual(cli.main(["process"]), 1)

    def test_end_to_end_with_transcript_on_disk(self):
        self._seed_workspace()
        self._seed_transcript()
        rc, synth = self._run_process()
        self.assertEqual(rc, 0)
        # One synthesis per rendered output type, no retries needed.
        self.assertEqual(synth.call_count, len(synthesize.RENDERED_OUTPUT_TYPES))
        conn = workspace.connect()
        done = workspace.existing_outputs(conn, self.MEETING_ID)
        row = workspace.get_meeting(conn, self.MEETING_ID)
        conn.close()
        self.assertEqual(set(done), set(synthesize.RENDERED_OUTPUT_TYPES))
        self.assertTrue(all(v == "observed_clean" for v in done.values()))
        self.assertIsNotNone(row["processed_at"])
        self.assertTrue(
            (Path(self._tmp.name) / "transcripts" / f"{self.MEETING_ID}.json").exists()
        )

    def test_rerun_skips_existing_outputs(self):
        self._seed_workspace()
        self._seed_transcript()
        self._run_process()
        # A processed meeting leaves the no-arg default queue (the next
        # unprocessed one is the natural target); re-touching it takes an
        # explicit id — and then every cached output is skipped.
        rc_noarg, _ = self._run_process()
        self.assertEqual(rc_noarg, 1)
        rc, synth = self._run_process(str(self.MEETING_ID))
        self.assertEqual(rc, 0)
        self.assertEqual(synth.call_count, 0)   # everything already cached

    def test_private_submission_failure_stays_pending_and_retry_completes(self):
        self._seed_workspace()
        self._seed_transcript()
        rc, _ = self._run_process(
            contribution_side_effect=flagship.FlagshipError("offline")
        )
        self.assertEqual(rc, 1)
        conn = workspace.connect()
        pending = workspace.contribution_submission(conn, self.MEETING_ID)
        meeting = workspace.get_meeting(conn, self.MEETING_ID)
        conn.close()
        self.assertEqual(pending["state"], "pending")
        self.assertIsNone(meeting["processed_at"])

        rc, synth = self._run_process(str(self.MEETING_ID))
        self.assertEqual(rc, 0)
        self.assertEqual(synth.call_count, 0)
        conn = workspace.connect()
        submitted = workspace.contribution_submission(conn, self.MEETING_ID)
        meeting = workspace.get_meeting(conn, self.MEETING_ID)
        conn.close()
        self.assertEqual(submitted["state"], "submitted")
        self.assertEqual(
            submitted["idempotency_key"], pending["idempotency_key"]
        )
        self.assertIsNotNone(meeting["processed_at"])

    def test_force_regenerates(self):
        self._seed_workspace()
        self._seed_transcript()
        self._run_process()
        rc, synth = self._run_process(str(self.MEETING_ID), "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(synth.call_count, len(synthesize.RENDERED_OUTPUT_TYPES))

    def test_provider_failure_isolates_per_output_and_exits_nonzero(self):
        self._seed_workspace()
        self._seed_transcript()

        calls = {"n": 0}

        def flaky(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise synthesize.SynthesisError("quota exhausted")
            return "The council discussed the water contract."

        rc, _synth = self._run_process(synth_side_effect=flaky)
        self.assertEqual(rc, 1)   # partial failure is honest at the exit code
        conn = workspace.connect()
        done = workspace.existing_outputs(conn, self.MEETING_ID)
        row = workspace.get_meeting(conn, self.MEETING_ID)
        conn.close()
        self.assertEqual(len(done), len(synthesize.RENDERED_OUTPUT_TYPES) - 1)
        self.assertIsNone(row["processed_at"])   # not fully processed

    def test_gate_verdict_travels_with_the_row(self):
        self._seed_workspace()
        self._seed_transcript()
        # key_decisions carries one deterministic finding; audit-only mode
        # preserves it and records the evidence without a second model call.
        def synth(_provider, _key, _model, prompt):
            if "`key_decisions`" in prompt:
                return ("1. The council adopted Resolution R-99.\n\n"
                        "2. The council approved the water contract in Resolution R-15.")
            return "The council discussed the water contract."

        rc, _ = self._run_process(synth_side_effect=synth)
        self.assertEqual(rc, 0)
        conn = workspace.connect()
        r = conn.execute(
            "SELECT content, gate_status, gate_log FROM outputs "
            "WHERE meeting_id = ? AND output_type = 'key_decisions'",
            (self.MEETING_ID,),
        ).fetchone()
        conn.close()
        self.assertEqual(r["gate_status"], "observed_findings")
        self.assertIn("R-99", r["content"])
        log = json.loads(r["gate_log"])
        self.assertFalse(log["retried"])
        self.assertTrue(log["determinate_failures"])
        self.assertEqual(log["stripped_units"], [])

    def test_no_eligible_meeting_reasons_are_specific(self):
        # No pull at all:
        config.save_config({
            "synthesis_provider": "gemini",
            "api_keys": {"gemini": "AIzaFakeKeyForTests000000"},
            "picked_city": {"city": "Kingman"},
        })
        self.assertEqual(cli.main(["process"]), 1)
        # Meetings exist but none carry video:
        conn = workspace.connect()
        workspace.upsert_meeting(conn, _event(1, "Council", "2026-07-01"))
        conn.commit()
        conn.close()
        self.assertEqual(cli.main(["process"]), 1)
        # Explicit id not in workspace:
        self.assertEqual(cli.main(["process", "999"]), 1)

    def test_unsupported_video_source_is_named(self):
        self._seed_workspace(video="https://city.granicus.com/MediaPlayer.php?clip_id=9")
        rc, _ = self._run_process()
        self.assertEqual(rc, 1)


class TestOpenAndServe(_TempHome):
    MEETING_ID = 7

    def _seed_processed(self, *, outputs=True):
        conn = workspace.connect()
        workspace.upsert_meeting(
            conn, _event(self.MEETING_ID, "Council", "2026-07-01", video="https://v")
        )
        self.MEETING_ID = conn.execute(
            "SELECT id FROM meetings WHERE flagship_row_id = ?", (self.MEETING_ID,)
        ).fetchone()["id"]
        if outputs:
            for output_type, content in (
                ("synopsis", "A meeting about <water> & sewers."),
                ("key_decisions",
                 "1. <core>Approved the water contract</core> by a vote of **5-2**."),
                ("community_calls_to_action",
                 '[{"speaker_name": "Tami Ring", "speaker_role": "org leader", '
                 '"quote_text": "come volunteer", "ask_kind": "volunteer"}]'),
                ("episode_tagline", "Water contract approved 5-2"),
            ):
                workspace.save_output(
                    conn, self.MEETING_ID, output_type,
                    content=content, provider="openai", model="gpt-4o-mini",
                    gate_status="ok", gate_log="{}",
                )
        conn.commit()
        conn.close()

    def _seed_transcript_file(self):
        tdir = Path(self._tmp.name) / "transcripts"
        tdir.mkdir(parents=True, exist_ok=True)
        conn = workspace.connect()
        tpath = tdir / f"{self.MEETING_ID}.json"
        tpath.write_text(json.dumps({
            "words": _VOTE_TRANSCRIPT, "duration_seconds": 31.0, "language": "en",
        }), encoding="utf-8")
        workspace.set_transcript_path(conn, self.MEETING_ID, str(tpath))
        conn.close()

    def test_workspace_helpers(self):
        self._seed_processed()
        conn = workspace.connect()
        rows = workspace.processed_meetings(conn)
        self.assertEqual([int(r["id"]) for r in rows], [self.MEETING_ID])
        self.assertEqual(int(rows[0]["output_count"]), 4)
        outputs = workspace.load_outputs(conn, self.MEETING_ID)
        conn.close()
        self.assertEqual(set(outputs), {
            "synopsis", "key_decisions", "community_calls_to_action", "episode_tagline",
        })
        self.assertEqual(outputs["synopsis"]["gate_status"], "ok")

    def test_legacy_audit_only_decisions_heal_on_read_without_db_rewrite(self):
        from zspan_cli import serve

        self._seed_processed(outputs=False)
        raw = "<!-- audit\n[]\naudit -->"
        conn = workspace.connect()
        workspace.save_output(
            conn, self.MEETING_ID, "key_decisions",
            content=raw, provider="openai", model="gpt-4o-mini",
            gate_status="ok", gate_log='{"status": "ok"}',
        )

        displayed = workspace.load_outputs(conn, self.MEETING_ID)["key_decisions"]
        notebook, status = serve._notebook(conn, self.MEETING_ID)
        stored = conn.execute(
            "SELECT content, gate_status FROM outputs "
            "WHERE meeting_id = ? AND output_type = 'key_decisions'",
            (self.MEETING_ID,),
        ).fetchone()
        conn.close()

        self.assertEqual(displayed["content"], "")
        self.assertEqual(displayed["gate_status"], "empty")
        self.assertEqual(status, 200)
        self.assertEqual(notebook["outputs"]["key_decisions"]["content"], "")
        self.assertEqual(
            notebook["outputs"]["key_decisions"]["gate_status"], "empty"
        )
        self.assertEqual(stored["content"], raw)
        self.assertEqual(stored["gate_status"], "ok")

    def test_server_routes_escaping_and_asbuilt_placement(self):
        from urllib.request import urlopen
        from zspan_cli import serve

        self._seed_processed()
        self._seed_transcript_file()
        server = serve.start_server(port=0)
        try:
            port = server.server_address[1]
            # The meeting page: the as-built show flow — decisions + CCTA +
            # processed pill, and NO synopsis paragraph or tagline hero
            # (the site's show page deliberately dropped them).
            with urlopen(f"http://127.0.0.1:{port}/meeting/{self.MEETING_ID}") as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
            self.assertIn("Approved the water contract", body)
            self.assertIn("Tami Ring", body)
            self.assertIn("Processed ·", body)
            self.assertNotIn("Water contract approved 5-2", body)
            self.assertNotIn("&lt;water&gt;", body)
            # Key decisions <core> renders as the site's marker wash.
            self.assertIn("<mark class=\"core\">", body)

            # The index: no redirect even with one meeting; the page is
            # just the list (the hologram boot plays in the TERMINAL now
            # — no second boot in the browser); tagline + synopsis
            # (escaped) describe the rows here.
            with urlopen(f"http://127.0.0.1:{port}/") as resp:
                index_body = resp.read().decode("utf-8")
                self.assertNotIn("/meeting/", resp.url)   # stayed on /
            self.assertNotIn("class=\"boot\"", index_body)
            self.assertNotIn("class=\"wave\"", index_body)
            self.assertIn("class=\"index-row\"", index_body)
            self.assertIn("Water contract approved 5-2", index_body)
            self.assertIn("&lt;water&gt;", index_body)
            self.assertNotIn("<water>", index_body)

            # 404s are pages, not tracebacks.
            import urllib.error
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urlopen(f"http://127.0.0.1:{port}/meeting/999")
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()

    def test_open_with_unknown_meeting_is_honest(self):
        self._seed_processed()
        self.assertEqual(cli.main(["open", "999"]), 1)

    def test_unprocessed_meeting_renders_as_a_factual_record(self):
        from zspan_cli import render

        self._seed_processed(outputs=False)
        conn = workspace.connect()
        row = workspace.get_meeting(conn, self.MEETING_ID)
        html = render.meeting_page(row, {})
        index = render.index_page(workspace.all_meetings(conn))
        conn.close()
        self.assertIn("Not processed yet", html)
        self.assertNotIn("Synthesized on your machine", html)
        self.assertIn("not processed yet", index)

    def test_spa_mode_serves_client_and_workspace_shims(self):
        """SPA mode (the 1ca7e8c pivot): the real client serves from a
        webapp dir; the shims answer in the flagship's shapes with LOCAL
        process-state; operator endpoints answer honest 404s."""
        import urllib.error
        from urllib.request import Request, urlopen
        from zspan_cli import serve

        self._seed_processed()
        self._seed_transcript_file()
        conn = workspace.connect()
        conn.execute("UPDATE meetings SET processed_at = '2026-07-10T00:00:00Z' WHERE id = ?",
                     (self.MEETING_ID,))
        workspace.upsert_meeting(
            conn, _event(9, "Planning", "2026-07-03", video="https://v")
        )
        planning_id = conn.execute(
            "SELECT id FROM meetings WHERE flagship_row_id = 9"
        ).fetchone()["id"]
        workspace.upsert_meeting(
            conn,
            _event(
                10, "Processable", "2026-07-04",
                video="https://www.youtube.com/watch?v=processable",
            ),
        )
        processable_id = conn.execute(
            "SELECT id FROM meetings WHERE flagship_row_id = 10"
        ).fetchone()["id"]
        conn.commit()
        conn.close()

        webapp = Path(self._tmp.name) / "webapp"
        webapp.mkdir()
        (webapp / "index.html").write_text("<!doctype html>FAKE CLIENT", encoding="utf-8")
        server = serve.start_server(port=0, webapp_dir=webapp)
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urlopen(f"{base}/?view=channels") as r:      # SPA fallback
                self.assertIn("FAKE CLIENT", r.read().decode())
            with urlopen(f"{base}/api/auth/me") as r:          # visitor state
                self.assertEqual(json.load(r), {
                    "authenticated": False,
                    "user": None,
                })
            with urlopen(f"{base}/api/channels/tree") as r:    # tree contract
                tree = json.load(r)
            city = tree["states"][0]["counties"][0]["cities"][0]
            self.assertEqual(tree["states"][0]["counties"][0]["county"], "Mohave")
            self.assertEqual(city["status"], "live")           # processed → live
            self.assertEqual(city["meeting_count"], 2)
            self.assertEqual(city["broadcast_count"], 1)
            with urlopen(f"{base}/api/cities/Kingman/meetings") as r:
                events = json.load(r)["events"]
            by_id = {e["id"]: e for e in events}
            self.assertTrue(by_id[self.MEETING_ID]["is_published"])   # local overlay
            self.assertFalse(by_id[processable_id]["is_published"])
            self.assertNotIn(planning_id, by_id)  # unusable + unprocessed
            # Filtering the channel list does not delete the factual record:
            # a direct show-page lookup remains an honest empty notebook.
            with urlopen(f"{base}/api/notebook/{planning_id}") as r:
                filtered_nb = json.load(r)
            self.assertTrue(filtered_nb["success"])
            self.assertIsNone(filtered_nb["approved_at"])
            with urlopen(f"{base}/api/notebook/{self.MEETING_ID}") as r:
                nb = json.load(r)
            self.assertTrue(nb["success"])
            self.assertIn("key_decisions", nb["outputs"])
            self.assertIsNotNone(nb["approved_at"])
            with urlopen(f"{base}/api/local/process/setup") as r:      # presence only
                self.assertIn("cloud_ready", json.load(r))
            # A second kick while one "runs" is refused — single-flight.
            serve._PROCESS_STATE.update(meeting_id=9, running=True, done=False)
            req = Request(f"{base}/api/local/process/{self.MEETING_ID}",
                          data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urlopen(req)
            self.assertEqual(ctx.exception.code, 409)
            serve._PROCESS_STATE.update(meeting_id=None, running=False)
            # Operator surface → honest JSON 404.
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urlopen(f"{base}/api/work-orders/stats")
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()

    def test_spa_mode_mirrors_cli_auth_and_refuses_local_follows(self):
        from urllib.request import Request, urlopen
        from zspan_cli import serve

        webapp = Path(self._tmp.name) / "webapp"
        webapp.mkdir()
        (webapp / "index.html").write_text(
            "<!doctype html>FAKE CLIENT", encoding="utf-8"
        )
        server = serve.start_server(port=0, webapp_dir=webapp)
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"

            # Point-of-use config loading: a login after server startup is
            # reflected by the next request, without restarting the server.
            config.save_config({"auth": {
                "token": "opaque-cli-token",
                "email": "person+local@example.com",
                "display_name": "Local Person",
                "expires_at": "2026-10-01T00:00:00Z",
            }})
            with urlopen(f"{base}/api/auth/me") as r:
                self.assertEqual(json.load(r), {
                    "authenticated": True,
                    "user": {
                        "user_id": 0,
                        "email": "person+local@example.com",
                        "display_name": "Local Person",
                        "avatar_url": None,
                        "role": "light",
                        "is_owner": False,
                        "is_operator_search_principal": False,
                        "follows": [],
                    },
                })

            logout = Request(
                f"{base}/api/auth/logout", data=b"{}", method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(logout) as r:
                self.assertEqual(json.load(r), {
                    "ok": True,
                    "note": "local sign-in is managed from the terminal: zspan logout",
                })
            with urlopen(f"{base}/api/auth/me") as r:
                self.assertTrue(json.load(r)["authenticated"])

            with urlopen(f"{base}/api/auth/google/login") as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers.get_content_type(), "text/html")
                login_html = r.read().decode("utf-8")
            self.assertIn("already signed in as", login_html)
            self.assertIn("person+local@example.com", login_html)

            with urlopen(f"{base}/api/follows") as r:
                self.assertEqual(json.load(r), {"success": True, "follows": []})
            expected_refusal = {
                "ok": False,
                "error": (
                    "following is a flagship-account feature; "
                    "the local workspace doesn't store follows"
                ),
            }
            for method in ("POST", "DELETE"):
                request = Request(
                    f"{base}/api/follows", data=b"{}", method=method,
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(json.load(r), expected_refusal)

            config.save_config({})
            with urlopen(f"{base}/api/auth/me") as r:
                self.assertEqual(json.load(r), {
                    "authenticated": False,
                    "user": None,
                })
            with urlopen(f"{base}/api/auth/google/login") as r:
                signed_out_html = r.read().decode("utf-8")
            self.assertIn("Sign in from the terminal", signed_out_html)
            self.assertIn("zspan login", signed_out_html)
            self.assertNotIn("already signed in as", signed_out_html)
        finally:
            server.shutdown()

    def test_local_librarian_shims(self):
        """The Librarian trio: presence-only setup, the flagship-shaped
        rag-search over workspace chunks, and the loopback synthesis on
        the stored key (S-131 — no relay semantics on loopback)."""
        import urllib.error
        from urllib.request import Request, urlopen

        import numpy as np

        from zspan_cli import config as config_mod
        from zspan_cli import pipeline as pl
        from zspan_cli import serve

        self._seed_processed()
        # Three one-hot "embeddings" — a stubbed query vector picks the
        # winner deterministically without loading the real model.
        chunks = [
            pl.Chunk(chunk_index=i, text=t, start_seconds=10.0 * i,
                     end_seconds=10.0 * i + 8.0)
            for i, t in enumerate(
                ["opening remarks", "the water contract vote", "adjournment"]
            )
        ]
        vectors = np.zeros((3, pl.VECTOR_DIM), dtype=np.float32)
        for i in range(3):
            vectors[i, i] = 1.0
        conn = workspace.connect()
        workspace.replace_chunks(conn, self.MEETING_ID, chunks, vectors)
        conn.close()

        query_vec = np.zeros(pl.VECTOR_DIM, dtype=np.float32)
        query_vec[1] = 1.0

        def post_json(base, path, payload):
            req = Request(
                base + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as r:
                return json.load(r)

        server = serve.start_server(port=0)
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"

            # Setup: no config AND no codex binary → not ready (and
            # nothing else leaks). codex stubbed off — on a machine with
            # the CLI installed, keyless setup legitimately arms via it.
            with mock.patch("zspan_cli.providers.codex_available",
                            return_value=False):
                with urlopen(f"{base}/api/local/librarian/setup") as r:
                    self.assertEqual(json.load(r), {"ready": False})

            # Arm a stored key the way `zspan init` would.
            config_mod.save_config({
                "synthesis_provider": "openai",
                "api_keys": {"openai": "sk-test-abcdefgh1234"},
            })
            with urlopen(f"{base}/api/local/librarian/setup") as r:
                setup = json.load(r)
            self.assertTrue(setup["ready"])
            self.assertEqual(setup["provider"], "openai")
            self.assertEqual(setup["engine"], "key")
            self.assertNotIn("sk-test-abcdefgh1234", json.dumps(setup))
            self.assertEqual(setup["fingerprint"], "sk-t...1234")

            # rag-search: flagship shape over the workspace, stubbed query
            # embedding, honest local provenance (empty run_id).
            with mock.patch.object(pl, "embed_query", return_value=query_vec):
                rag = post_json(base, f"/api/rag-search/{self.MEETING_ID}",
                                {"query": "what happened with the water contract?",
                                 "top_k": 2})
            self.assertTrue(rag["success"])
            self.assertEqual(rag["chunks"][0]["body"], "the water contract vote")
            self.assertEqual(rag["chunks"][0]["start_seconds"], 10.0)
            self.assertAlmostEqual(rag["chunks"][0]["score"], 1.0, places=5)
            self.assertEqual(len(rag["chunks"]), 2)
            self.assertEqual(rag["provenance"]["run_id"], "")
            self.assertIn("[at MM:SS]", rag["recommended_system_prompt"])

            # Validation + honest empties.
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post_json(base, f"/api/rag-search/{self.MEETING_ID}", {"query": ""})
            self.assertEqual(ctx.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post_json(base, "/api/rag-search/999", {"query": "hm"})
            self.assertEqual(ctx.exception.code, 404)
            conn = workspace.connect()
            workspace.upsert_meeting(conn, _event(12, "Special", "2026-07-04"))
            special_id = conn.execute(
                "SELECT id FROM meetings WHERE flagship_row_id = 12"
            ).fetchone()["id"]
            conn.commit()
            conn.close()
            unindexed = post_json(
                base, f"/api/rag-search/{special_id}", {"query": "hm"}
            )
            self.assertEqual(unindexed["interpreted_as"], "not_indexed")
            self.assertEqual(unindexed["chunks"], [])

            # Loopback synthesis: stored key + stubbed provider call; the
            # response carries the flagship-shaped provider id + usage.
            with mock.patch(
                "zspan_cli.synthesize.synthesize_chat",
                return_value={"answer": "The vote passed [at 00:10].",
                              "input_tokens": 21, "output_tokens": 9},
            ) as chat:
                out = post_json(base, "/api/local/librarian/synthesize",
                                {"system_prompt": "SYS", "user_message": "USER",
                                 "max_tokens": 512, "temperature": 0.2})
            self.assertTrue(out["success"])
            self.assertEqual(out["provider_id"], "openai-gpt-4.1")
            self.assertEqual(out["input_tokens"], 21)
            self.assertEqual(chat.call_args.args[:3],
                             ("openai", "sk-test-abcdefgh1234", "gpt-4.1"))

            # Provider failure → 502 with the provider's message; missing
            # user_message → 400.
            with mock.patch(
                "zspan_cli.synthesize.synthesize_chat",
                side_effect=synthesize.SynthesisError("quota"),
            ):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    post_json(base, "/api/local/librarian/synthesize",
                              {"system_prompt": "s", "user_message": "u"})
            self.assertEqual(ctx.exception.code, 502)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post_json(base, "/api/local/librarian/synthesize", {})
            self.assertEqual(ctx.exception.code, 400)

            # No stored key AND no codex → the honest engine pointer
            # (codex stubbed off so a machine with the CLI installed
            # doesn't arm — and doesn't spend a real subscription call).
            config_mod.config_path().unlink()
            with mock.patch("zspan_cli.providers.codex_available",
                            return_value=False):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    post_json(base, "/api/local/librarian/synthesize",
                              {"system_prompt": "s", "user_message": "u"})
            self.assertEqual(ctx.exception.code, 400)
            body = json.loads(ctx.exception.read().decode("utf-8"))
            self.assertIn("Codex", body["error"])
            self.assertIn("launch context", body["error"])
            self.assertIn("checked", body["error"])
            self.assertNotIn("zspan init", body["error"])

            # Keyless + Codex CLI installed → the panel arms via codex
            # (operator ask 2026-07-10 evening: wean the local demo off
            # the key) and synthesis routes the codex chat path — no key
            # material anywhere in either payload.
            with mock.patch("zspan_cli.providers.codex_available",
                            return_value=True):
                with urlopen(f"{base}/api/local/librarian/setup") as r:
                    setup = json.load(r)
                self.assertTrue(setup["ready"])
                self.assertEqual(setup["provider"], "codex")
                self.assertEqual(setup["engine"], "codex")
                self.assertEqual(setup["fingerprint"], "")
                with mock.patch(
                    "zspan_cli.synthesize.synthesize_chat",
                    return_value={"answer": "Keyless answer [at 00:10].",
                                  "input_tokens": 0, "output_tokens": 0},
                ) as chat:
                    out = post_json(base, "/api/local/librarian/synthesize",
                                    {"system_prompt": "SYS",
                                     "user_message": "USER"})
                self.assertTrue(out["success"])
                self.assertEqual(chat.call_args.args[0], "codex")
                self.assertEqual(chat.call_args.args[1], "")  # keyless
                self.assertEqual(
                    out["provider_id"], f"codex-{chat.call_args.args[2]}")
        finally:
            server.shutdown()

    def test_local_video_range_serving_and_overlay(self):
        """The embed-disabled rescue's playback path: Range-capable
        serving (seeks need partial reads), traversal-safe, and the
        notebook shim's video_url overlay routing the player to the
        html5 adapter."""
        import urllib.error
        from urllib.request import Request, urlopen

        from zspan_cli import serve
        from zspan_cli.config import videos_dir

        self._seed_processed()
        conn = workspace.connect()
        conn.execute("UPDATE meetings SET processed_at='2026-07-10T00:00:00Z' WHERE id=?",
                     (self.MEETING_ID,))
        conn.commit()
        conn.close()
        vdir = videos_dir()
        vdir.mkdir(parents=True, exist_ok=True)
        payload = bytes(range(256)) * 40  # 10240 bytes, position-checkable
        (vdir / f"{self.MEETING_ID}.mp4").write_bytes(payload)

        server = serve.start_server(port=0)
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urlopen(f"{base}/media/video/{self.MEETING_ID}.mp4") as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers["Accept-Ranges"], "bytes")
                self.assertEqual(r.read(), payload)

            req = Request(f"{base}/media/video/{self.MEETING_ID}.mp4",
                          headers={"Range": "bytes=100-199"})
            with urlopen(req) as r:
                self.assertEqual(r.status, 206)
                self.assertEqual(r.headers["Content-Range"],
                                 f"bytes 100-199/{len(payload)}")
                self.assertEqual(r.read(), payload[100:200])

            req = Request(f"{base}/media/video/{self.MEETING_ID}.mp4",
                          headers={"Range": "bytes=-16"})
            with urlopen(req) as r:
                self.assertEqual(r.status, 206)
                self.assertEqual(r.read(), payload[-16:])

            req = Request(f"{base}/media/video/{self.MEETING_ID}.mp4",
                          headers={"Range": f"bytes={len(payload) + 5}-"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urlopen(req)
            self.assertEqual(ctx.exception.code, 416)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urlopen(f"{base}/media/video/..%2F..%2Fconfig.json")
            self.assertEqual(ctx.exception.code, 404)

            # The notebook payload routes the player at the local copy.
            with urlopen(f"{base}/api/notebook/{self.MEETING_ID}") as r:
                nb = json.load(r)
            self.assertEqual(nb["video_url"],
                             f"/media/video/{self.MEETING_ID}.mp4")
        finally:
            server.shutdown()

    def test_watchable_video_rescue_fires_only_on_embed_disabled(self):
        from zspan_cli import media as media_mod
        from zspan_cli import processing

        self._seed_processed()
        conn = workspace.connect()
        row = workspace.get_meeting(conn, self.MEETING_ID)
        conn.close()
        # Meeting 7's seeded video is "https://v" (unknown class) → the
        # rescue skips before any probe fires.
        with mock.patch.object(media_mod, "download_video") as dl:
            processing.ensure_watchable_video(row, progress=lambda _m: None)
        dl.assert_not_called()

        yt_row = dict(row)
        yt_row["video_url"] = "https://www.youtube.com/watch?v=abc"
        # Embeddable (oEmbed 200) → no fetch.
        with mock.patch.object(processing, "requests", create=True), \
             mock.patch("requests.get", return_value=_resp(200, {})), \
             mock.patch.object(media_mod, "download_video") as dl:
            processing.ensure_watchable_video(yt_row, progress=lambda _m: None)
        dl.assert_not_called()
        # Embed-disabled (oEmbed 401) → fetch fires into videos_dir.
        events = []
        with mock.patch("requests.get", return_value=_resp(401, {})), \
             mock.patch.object(media_mod, "download_video") as dl:
            dl.return_value = mock.Mock(bytes=1048576, path=Path("x/7.mp4"))
            processing.ensure_watchable_video(
                yt_row, progress=lambda _m: None,
                activity=lambda k, l, d="", s=200: events.append((k, l, s)),
            )
        dl.assert_called_once()
        self.assertTrue(any("watchable" in l for _k, l, _s in events))

    def test_boot_svg_is_deterministic(self):
        # Retired with the web hologram (operator-directed 2026-07-10):
        # the boot is the terminal's now — see TestTerminalBoot.
        self.skipTest("web hologram replaced by the terminal boot")


class TestTerminalBoot(unittest.TestCase):
    """The hologram boot, terminal form (boot.py rewrite 2026-07-10)."""

    def _spec(self, **kw):
        from zspan_cli import boot
        return boot.BootSpec(**kw)

    def test_detect_capabilities_art_floor_boundaries(self):
        from zspan_cli import boot

        class FakeTTY:
            encoding = "utf-8"

            def isatty(self):
                return True

        out = FakeTTY()
        with mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=False):
            for size, expected_art in (
                ((75, 23), False),
                ((75, 24), False),
                ((76, 23), False),
                ((76, 24), True),
            ):
                with self.subTest(size=size), mock.patch.object(
                    boot.shutil, "get_terminal_size",
                    return_value=os.terminal_size(size),
                ), mock.patch.object(
                    boot, "_enable_windows_vt", return_value=True,
                ):
                    spec = boot.detect_capabilities(out)
                    self.assertEqual(spec is not None, expected_art)

        with mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=False), \
             mock.patch.object(
                 boot.shutil, "get_terminal_size",
                 return_value=os.terminal_size((200, 50)),
             ), mock.patch.object(
                 boot, "_enable_windows_vt", return_value=True,
             ):
            self.assertEqual(boot.detect_capabilities(out).width, 200)

    def test_detect_capabilities_marks_ascii_glyph_fallback(self):
        from zspan_cli import boot

        class FakeAsciiTTY:
            encoding = "ascii"

            def isatty(self):
                return True

        with mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=False), \
             mock.patch.object(
                 boot.shutil, "get_terminal_size",
                 return_value=os.terminal_size((76, 24)),
             ), mock.patch.object(
                 boot, "_enable_windows_vt", return_value=True,
             ):
            spec = boot.detect_capabilities(FakeAsciiTTY())
        self.assertIsNotNone(spec)
        self.assertTrue(spec.ascii_only)
        self.assertEqual(spec.glyphs, boot._GLYPHS_ASCII)

    def test_frames_are_deterministic(self):
        from zspan_cli import boot
        for width, rows in ((76, 24), (100, 32), (132, 40), (200, 50)):
            with self.subTest(size=(width, rows)):
                spec = self._spec(width=width, rows=rows)
                a = boot.render_frame(spec, "columns", 7)
                b = boot.render_frame(spec, "columns", 7)
                self.assertEqual(a, b)         # a drawing, not dice
                c = boot.render_frame(spec, "columns", 8)
                self.assertNotEqual(a, c)      # ...that still animates

    def test_ninth_shaft_row_gets_hot_beat_then_settles(self):
        from zspan_cli import boot
        spec = self._spec(width=80)
        first_full_tick = (spec.layout.shaft_rows - 1) * boot._TICKS_PER_ROW
        top_row = 2 + spec.layout.capital_rows
        hot = spec.color(boot._WHITE_HOT, bold=True)
        self.assertIn(
            hot, boot.render_frame(spec, "columns", first_full_tick)[top_row]
        )
        self.assertNotIn(
            hot,
            boot.render_frame(spec, "columns", first_full_tick + 2)[top_row],
        )

    def test_wave_count_contract(self):
        from zspan_cli import boot
        self.assertEqual(boot.wave_count(0), 8)   # full-tide floor
        self.assertEqual(boot.wave_count(5), 8)
        self.assertEqual(boot.wave_count(8), 8)
        self.assertEqual(boot.wave_count(40), 9)  # optional ninth home row

    def test_width_discipline_every_phase(self):
        from zspan_cli import boot
        lines = [("teal", "u"), ("indigo", "g"), ("grey", "h")]
        for width, rows in ((76, 24), (80, 24), (100, 32), (132, 40),
                            (200, 50)):
            spec = self._spec(width=width, rows=rows)
            for phase, splashed in (("bases", 0), ("columns", 0),
                                    ("capitals", 0), ("ocean", 1),
                                    ("resolved", 3)):
                with self.subTest(size=(width, rows), phase=phase):
                    for line in boot.render_frame(
                            spec, phase, 5, status="Loading: x",
                            ocean_lines=lines, splashed=splashed):
                        self.assertLessEqual(boot.visible_len(line), spec.width)

    def test_adaptive_layout_table_and_floor_form(self):
        from zspan_cli import boot
        expected = {
            (76, 24): (76, 9, 13, 46, 13),
            (80, 24): (80, 9, 13, 50, 13),
            (100, 32): (100, 13, 17, 62, 19),
            (132, 40): (132, 17, 21, 86, 25),
            (200, 50): (132, 17, 21, 86, 25),
        }
        for size, values in expected.items():
            with self.subTest(size=size):
                layout = self._spec(width=size[0], rows=size[1]).layout
                self.assertEqual(
                    (layout.canvas_width, layout.shaft_w, layout.col_w,
                     layout.ocean_w, layout.art_rows), values)
        floor = self._spec(width=76, rows=24).layout
        self.assertEqual(
            (floor.shaft_w, floor.col_w, floor.shaft_rows, floor.capital_rows,
             floor.base_rows, floor.art_rows),
            (boot._SHAFT_W, boot._COL_W, boot._SHAFT_ROWS, 2, 2,
             boot._ART_ROWS),
        )

    def test_odd_shafts_are_exactly_centered_and_grooves_are_vertical(self):
        from zspan_cli import boot
        for width, rows in ((76, 24), (100, 32), (132, 40)):
            with self.subTest(size=(width, rows)):
                spec = self._spec(width=width, rows=rows, color_depth=0)
                shaft = boot._shaft_row(
                    spec.glyphs, spec.layout.shaft_w, spec.layout.col_w)
                occupied = [i for i, (ch, _) in enumerate(shaft) if ch != " "]
                self.assertEqual(occupied[0], 2)
                self.assertEqual(occupied[-1], spec.layout.col_w - 3)
                self.assertEqual(len(occupied), spec.layout.shaft_w)
                groove_x = [i for i, (ch, _) in enumerate(shaft)
                            if ch == spec.glyphs["groove"]]
                self.assertTrue(groove_x)
                second = boot._shaft_row(
                    spec.glyphs, spec.layout.shaft_w, spec.layout.col_w)
                self.assertEqual(groove_x, [i for i, (ch, _) in enumerate(second)
                                            if ch == spec.glyphs["groove"]])

    def test_wave_slots_are_unique_monotonic_and_inside_the_shaft(self):
        from zspan_cli import boot
        for width, rows in ((76, 24), (100, 32), (132, 40), (200, 50)):
            with self.subTest(size=(width, rows)):
                layout = self._spec(width=width, rows=rows).layout
                slots = [boot._slot_row(layout, i) for i in range(8)]
                self.assertEqual(slots, sorted(set(slots)))
                self.assertGreaterEqual(slots[0], layout.capital_rows)
                self.assertLess(slots[-1],
                                layout.capital_rows + layout.shaft_rows)

    def test_stage_is_centered_on_a_200_column_terminal(self):
        from zspan_cli import boot
        spec = self._spec(width=200, rows=50, color_depth=0)
        frame = boot.render_frame(spec, "resolved", 0, status="CENTER")
        self.assertEqual(spec.layout.left_margin, 34)
        self.assertTrue(all(line.startswith(" " * 34) for line in frame))
        self.assertEqual(frame[0].index("CENTER"),
                         34 + (132 - len("CENTER")) // 2)

    def test_resolved_frame_carries_the_home_lines(self):
        from zspan_cli import boot
        spec = self._spec(width=90)
        url = "http://127.0.0.1:8741/"
        frame = boot.render_frame(
            spec, "resolved", 30, status="Z-SPAN: connected to local workspace",
            ocean_lines=[("teal", f"Your workspace → {url}"),
                         ("indigo", "The framework → github")])
        flat = "\n".join(frame)
        self.assertIn(url, flat)
        self.assertIn("The framework", flat)
        self.assertIn("connected to local workspace", flat)

    def test_resolved_frame_renders_no_waves_even_with_blank_spacers(self):
        # Conductor catch (2026-07-13 pixel render): blank home spacers fell
        # through to the wave branch in "resolved" (splashed defaults 0), so
        # the settled home showed leftover water forever. Waves are ocean-
        # phase-only; the resolved home's spacers are air.
        from zspan_cli import boot
        spec = self._spec(width=100, rows=32)
        g = spec.glyphs
        home = [("white", "d1"), ("white", "d2"), ("blank", ""),
                ("teal", "url"), ("indigo", "gh"), ("indigo", "kofi"),
                ("blank", ""), ("grey", "status"), ("grey", "ctrl-c")]
        frame = boot.render_frame(spec, "resolved", 200,
                                  status="hdr", ocean_lines=home)
        band = "\n".join(frame[2:2 + spec.layout.art_rows])
        # the columns' own glyphs never include the wave glyphs
        self.assertNotIn(g["wave_hi"], band)
        self.assertNotIn(g["wave_lo"] * 3, band)  # runs of wave ink

    def test_splash_lands_top_down(self):
        from zspan_cli import boot
        spec = self._spec(width=80)
        lines = [("teal", "FIRST-LINE"), ("indigo", "SECOND-LINE")]
        mid = "\n".join(boot.render_frame(spec, "ocean", 9,
                                          ocean_lines=lines, splashed=1))
        self.assertIn("FIRST-LINE", mid)       # splashed
        self.assertNotIn("SECOND-LINE", mid)   # still a wave

    def test_landing_row_atomically_replaces_wave_with_disclaimer_text(self):
        from zspan_cli import boot
        spec = self._spec(width=132, color_depth=0)
        spans = next(
            row for row in boot.wrap_spans(
                boot.DISCLAIMER_SPANS, spec.ocean_width
            ) if "may not be 100% accurate" in "".join(t for _, t in row)
        )
        text = "".join(t for _, t in spans)
        lines = [("white", spans), ("indigo", "NEXT-LINE")]
        ocean_start = spec.layout.left_margin + 1 + spec.layout.col_w + 1
        row_number = 2 + boot._slot_row(spec.layout, 0)

        before = boot.render_frame(
            spec, "ocean", 200, ocean_lines=lines, ocean_slots=8,
        )[row_number][ocean_start:ocean_start + spec.ocean_width]
        self.assertTrue(spec.glyphs["wave_hi"] in before
                        or spec.glyphs["wave_lo"] in before)

        row = boot.render_frame(
            spec, "ocean", 200, ocean_lines=lines, splashed=1,
            ocean_slots=8,
        )[row_number][ocean_start:ocean_start + spec.ocean_width]
        self.assertEqual(row, text.ljust(spec.ocean_width))
        self.assertNotIn(spec.glyphs["wave_hi"], row)
        self.assertNotIn(spec.glyphs["wave_lo"], row)

        long_text = "X" * (spec.ocean_width + 20)
        truncated = boot.render_frame(
            spec, "ocean", 200, ocean_lines=[("teal", long_text)],
            splashed=1, ocean_slots=8,
        )[row_number][ocean_start:ocean_start + spec.ocean_width]
        self.assertEqual(truncated, "X" * (spec.ocean_width - 1) + "…")

        blank_lines = [("teal", "FIRST"), ("blank", ""),
                       ("indigo", "NEXT")]
        blank_row_number = 2 + boot._slot_row(spec.layout, 1)
        before_blank = boot.render_frame(
            spec, "ocean", 200, ocean_lines=blank_lines, splashed=1,
            ocean_slots=8,
        )[blank_row_number][ocean_start:ocean_start + spec.ocean_width]
        self.assertTrue(spec.glyphs["wave_hi"] in before_blank
                        or spec.glyphs["wave_lo"] in before_blank)
        landed_blank = boot.render_frame(
            spec, "ocean", 200, ocean_lines=blank_lines, splashed=2,
            ocean_slots=8,
        )[blank_row_number][ocean_start:ocean_start + spec.ocean_width]
        self.assertEqual(landed_blank, " " * spec.ocean_width)

    def test_splash_suppresses_waves_at_and_above_landing_seam(self):
        from zspan_cli import boot
        lines = [("grey", f"HOME-{i}") for i in range(8)]
        for width, rows in ((100, 32), (132, 40)):
            with self.subTest(size=(width, rows)):
                spec = self._spec(width=width, rows=rows, color_depth=0)
                loading = boot.render_frame(
                    spec, "ocean", 200, status="HEADER", ocean_slots=8)
                splash = boot.render_frame(
                    spec, "ocean", 200, status="HEADER", ocean_lines=lines,
                    splashed=4, ocean_slots=8,
                )
                for idx in range(4):
                    row = splash[2 + boot._slot_row(spec.layout, idx)]
                    self.assertNotIn(spec.glyphs["wave_hi"], row)
                    self.assertNotIn(spec.glyphs["wave_lo"], row)
                for idx in range(4, 8):
                    row_number = 2 + boot._slot_row(spec.layout, idx)
                    self.assertEqual(splash[row_number], loading[row_number])

    def test_lines_draw_downward_then_hold(self):
        from zspan_cli import boot
        spec = self._spec(width=80, color_depth=0)
        g = spec.glyphs

        def wave_rows(frame):
            return sum(1 for l in frame
                       if g["wave_hi"] in l or g["wave_lo"] in l)

        self.assertEqual(
            boot._TIDE_LANDING_TICKS, (0, 7, 13, 18, 22, 25, 27, 29)
        )
        self.assertEqual(wave_rows(boot.render_frame(
            spec, "ocean", 0, ocean_slots=3)), 1)
        self.assertEqual(wave_rows(boot.render_frame(
            spec, "ocean", 6, ocean_slots=3)), 1)
        self.assertEqual(wave_rows(boot.render_frame(
            spec, "ocean", 7, ocean_slots=3)), 2)
        self.assertEqual(wave_rows(boot.render_frame(
            spec, "ocean", 17, ocean_slots=3)), 3)
        self.assertEqual(wave_rows(boot.render_frame(
            spec, "ocean", 18, ocean_slots=3)), 4)
        self.assertEqual(wave_rows(boot.render_frame(
            spec, "ocean", 29, ocean_slots=3)), 8)

        # Every row is blank before its beat and complete from its beat on.
        # Checking every tick also locks out any partial-extension path.
        for tick in range(boot._TIDE_LANDING_TICKS[-1] + 2):
            frame = boot.render_frame(spec, "ocean", tick, ocean_slots=3)
            for identity in range(8):
                row = frame[2 + boot._slot_row(spec.layout, identity)]
                ink = row.count(g["wave_hi"]) + row.count(g["wave_lo"])
                _indent, full_length = boot._fan(
                    spec.ocean_width, identity, 8)
                expected = (full_length
                            if tick >= boot._TIDE_LANDING_TICKS[identity] else 0)
                with self.subTest(tick=tick, identity=identity):
                    self.assertEqual(ink, expected)

        # once every line is drawn, the drawing HOLDS — dead still
        # ("I'm trying to draw lines", not an animation)
        late_a = boot.render_frame(spec, "ocean", 200, ocean_slots=8,
                                   status="Loading: x")
        late_b = boot.render_frame(spec, "ocean", 201, ocean_slots=8,
                                   status="Loading: x")
        self.assertEqual(late_a, late_b)
        # Sketch 7.png is a centered sonar cone: narrow at the top, then
        # monotonically wider downward to a near-full-width bottom.
        for width in (48, 60, 80):
            envelope = [boot._fan(width, row, 8) for row in range(8)]
            widths = [length for _indent, length in envelope]
            self.assertTrue(all(a <= b for a, b in zip(widths, widths[1:])))
            self.assertLessEqual(widths[0], width * 0.50)
            self.assertGreaterEqual(widths[-1], width * 0.90)
            for indent, length in envelope:
                right_inset = width - indent - length
                self.assertLessEqual(abs(indent - right_inset), 1)

    def test_wave_ink_is_continuous_between_varied_endpoints(self):
        from zspan_cli import boot
        g = boot._GLYPHS_FULL
        endpoints = []
        for row in range(8):
            indent, length = boot._fan(80, row, 8)
            line = boot._wave_line(g, 80, row, 8)
            self.assertTrue(all(ch in (g["wave_hi"], g["wave_lo"])
                                for ch in line[indent:indent + length]))
            endpoints.append((indent, length))
        self.assertGreater(len(set(endpoints)), 4)

    def test_unsplashed_rows_below_seam_keep_held_wave_identities(self):
        from zspan_cli import boot
        spec = self._spec(width=100, color_depth=0)
        held_tick = 200
        status = "Z-SPAN: connected to local workspace"
        loading = boot.render_frame(
            spec, "ocean", held_tick, status=status, ocean_slots=8
        )
        # The production home shape carries blank spacers (rows 2 and 7).
        # Rows below the active seam keep their waves until their own splash.
        home_rows = [("grey", f"HOME-{i}") for i in range(9)]
        home_rows[2] = ("blank", "")
        home_rows[7] = ("blank", "")
        first_finish = boot.render_frame(
            spec, "ocean", held_tick, status=status,
            ocean_lines=home_rows, splashed=2, ocean_slots=9,
        )
        for idx in range(2, 8):
            frame_row = 2 + boot._slot_row(spec.layout, idx)
            with self.subTest(wave_identity=idx):
                self.assertEqual(loading[frame_row], first_finish[frame_row])

    def test_early_finish_waits_for_full_tide_crash_and_hold(self):
        from zspan_cli import boot
        import io

        events = []

        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self._target = target

            def start(self):
                self._target()

            def is_alive(self):
                return False

            def join(self):
                return None

        def record_frame(_spec, phase, tick, status="", **kwargs):
            events.append((phase, tick, status, kwargs))
            return []

        spec = self._spec(width=100, color_depth=0)
        with mock.patch.object(boot, "detect_capabilities", return_value=spec), \
             mock.patch.object(boot.threading, "Thread", ImmediateThread), \
             mock.patch.object(boot, "render_frame", side_effect=record_frame):
            tb = boot.TerminalBoot(out=io.StringIO())
            tb._intro_done = True
            tb._draw = lambda _frame: None
            tb._frame_sleep = lambda: None
            self.assertEqual(tb.step("instant server", lambda: 42), 42)
            tb.finish("RESOLVED", [("grey", "FIRST-TEXT")])

        loading = [event for event in events if event[2] == "Loading: instant server"]
        self.assertEqual(
            [tick for _phase, tick, _status, _kwargs in loading],
            list(range(boot._TIDE_MIN_TICKS)),
        )
        self.assertEqual(
            [tick for _phase, tick, _status, _kwargs in loading[-4:]],
            [30, 31, 32, 33],
        )
        splash_frames = [
            event for event in events
            if event[2] == "RESOLVED" and event[0] == "ocean"
        ]
        # Only the real destination row splashes. The seven waves beneath a
        # one-line home remain held until the resolved frame.
        self.assertEqual(len(splash_frames), 1)
        first_splash = splash_frames[0]
        self.assertEqual(first_splash[1], boot._TIDE_MIN_TICKS)
        self.assertEqual(first_splash[3]["splashed"], 1)
        self.assertNotIn("landing", first_splash[3])
        self.assertGreaterEqual(
            first_splash[1],
            boot._TIDE_LANDING_TICKS[-1] + 1 + boot._CRASH_HOLD_TICKS,
        )

    def test_final_resolved_frame_is_unchanged_after_atomic_splash(self):
        from zspan_cli import boot
        spec = self._spec(width=100, color_depth=0)
        g = spec.glyphs
        lines = [("grey", "LANDED"), ("blank", ""),
                 ("teal", "WORKSPACE"), ("indigo", "FRAMEWORK")]
        expected = boot.render_frame(
            spec, "resolved", 200, status="HEADER", ocean_lines=lines,
        )
        after_splash = boot.render_frame(
            spec, "resolved", 200, status="HEADER", ocean_lines=lines,
            splashed=3,
        )
        self.assertEqual(after_splash, expected)
        band = "\n".join(after_splash[2:2 + spec.layout.art_rows])
        self.assertNotIn(g["wave_hi"], band)
        self.assertNotIn(g["wave_lo"], band)

    def test_finish_draws_each_row_once_with_three_skippable_beats_between(self):
        from zspan_cli import boot
        import io

        events = []
        sleeps = []

        def record_frame(_spec, phase, tick, status="", **kwargs):
            events.append((phase, tick, status, kwargs))
            return []

        spec = self._spec(width=100, color_depth=0)
        with mock.patch.object(boot, "detect_capabilities", return_value=spec), \
             mock.patch.object(boot, "render_frame", side_effect=record_frame):
            tb = boot.TerminalBoot(out=io.StringIO())
            tb._intro_done = True
            tb._ocean_ticks = 73
            tb._draw = lambda _frame: None
            tb._frame_sleep = lambda: sleeps.append("beat")
            tb.finish("RESOLVED", [
                ("teal", "FIRST"), ("blank", ""), ("indigo", "THIRD"),
            ])

        landing_frames = [event for event in events if event[0] == "ocean"]
        self.assertEqual(len(landing_frames), 3)
        self.assertEqual([event[1] for event in landing_frames], [73, 73, 73])
        self.assertEqual(
            [event[3]["splashed"] for event in landing_frames], [1, 2, 3]
        )
        self.assertEqual(landing_frames[1][3]["ocean_lines"][1],
                         ("blank", ""))
        self.assertEqual(boot._INTER_ROW_BEAT_TICKS, 3)
        self.assertEqual(len(sleeps), 2 * boot._INTER_ROW_BEAT_TICKS)
        self.assertEqual(events[-1][0], "resolved")

    def test_finish_skip_during_inter_row_beat_goes_straight_to_resolved(self):
        from zspan_cli import boot
        import io

        events = []

        def record_frame(_spec, phase, tick, status="", **kwargs):
            events.append((phase, tick, status, kwargs))
            return []

        poll = mock.Mock()
        poll.available = True
        poll.pressed.side_effect = [False, True]
        spec = self._spec(width=100, color_depth=0)
        with mock.patch.object(boot, "detect_capabilities", return_value=spec), \
             mock.patch.object(boot, "render_frame", side_effect=record_frame), \
             mock.patch.object(boot.time, "sleep"):
            tb = boot.TerminalBoot(out=io.StringIO())
            tb._intro_done = True
            tb._ocean_ticks = 91
            tb._poll = poll
            tb._draw = lambda _frame: None
            tb.finish("RESOLVED", [
                ("teal", "FIRST"), ("indigo", "SECOND"),
                ("grey", "THIRD"),
            ])

        landing_frames = [event for event in events if event[0] == "ocean"]
        self.assertEqual(len(landing_frames), 1)
        self.assertEqual(landing_frames[0][1], 91)
        self.assertEqual(landing_frames[0][3]["splashed"], 1)
        self.assertEqual(events[-1][0], "resolved")
        self.assertTrue(tb._skip)
        self.assertEqual(tb._tick, 2)
        self.assertEqual(poll.pressed.call_count, 2)
        poll.close.assert_called_once_with()

    def test_disclaimer_spans_wrap_and_keep_red_core(self):
        from zspan_cli import boot
        rows = boot.wrap_spans(boot.DISCLAIMER_SPANS, 48)
        self.assertIn(len(rows), (2, 3))        # "a one-liner or two-liner"
        flat_kinds = [k for row in rows for k, _ in row]
        self.assertIn("red", flat_kinds)
        joined = " ".join(t for row in rows for _, t in row)
        self.assertIn("may not be 100% accurate", joined)
        self.assertIn("I accept the consequence", joined)

    def test_home_line_fitting_ocean_width_is_never_truncated(self):
        from zspan_cli import boot
        spec = self._spec(width=78)          # ocean width = 48
        text = "The framework → github.com/anitacigawet/Z-SPAN"  # 46 chars
        flat = "\n".join(boot.render_frame(spec, "resolved", 5,
                                           ocean_lines=[("indigo", text)]))
        self.assertIn(text, flat)
        self.assertNotIn("…", flat)

    def test_ascii_real_home_frame_encodes_and_truncates_safely(self):
        from zspan_cli import boot
        spec = self._spec(width=76, glyphs=dict(boot._GLYPHS_ASCII),
                          color_depth=0, ascii_only=True)
        home_rows = [
            ("white", row)
            for row in boot.wrap_spans(boot.DISCLAIMER_SPANS, spec.ocean_width)
        ] + [
            ("teal", "Your workspace → http://127.0.0.1:8741/"),
            ("indigo", "The framework → github.com/anitacigawet/Z-SPAN"),
            ("indigo", "Support the work → ko-fi.com/zspan"),
            ("grey", "5 processed meetings ready · private intake complete"),
            ("grey", "Ctrl-C stops the server"),
        ]
        flat = "\n".join(boot.render_frame(
            spec, "resolved", 3,
            status="Z-SPAN: connected to local workspace",
            ocean_lines=home_rows,
        ))
        flat.encode("ascii")                   # all production copy is ASCII
        self.assertIn("...", flat)             # three-cell truncation marker
        self.assertNotIn("→", flat)
        self.assertNotIn("·", flat)
        self.assertNotIn("…", flat)

    def test_mono_depth_emits_no_escapes(self):
        from zspan_cli import boot
        spec = self._spec(width=80, color_depth=0)
        flat = "\n".join(boot.render_frame(
            spec, "resolved", 4,
            ocean_lines=[("spans", boot.DISCLAIMER_SPANS)],
        ))
        self.assertNotIn("\x1b", flat)

    def test_plain_tier_prints_the_same_facts(self):
        from zspan_cli import boot
        import io
        out = io.StringIO()               # not a tty → plain tier
        tb = boot.TerminalBoot(out=out)
        self.assertFalse(tb.art)
        result = tb.step("the local server", lambda: 42)
        self.assertEqual(result, 42)
        tb.say("captured? no — immediate in plain tier")
        tb.finish("Z-SPAN: connected to local workspace",
                  [("teal", "http://127.0.0.1:1/"), ("grey", "Ctrl-C stops")])
        text = out.getvalue()
        self.assertIn("→ the local server", text)
        self.assertIn("✓", text)
        self.assertIn("immediate in plain tier", text)
        self.assertIn("connected to local workspace", text)
        self.assertIn("http://127.0.0.1:1/", text)
        self.assertNotIn("\x1b[", text)        # zero escapes in plain tier

    def test_plain_tier_transliterates_for_ascii_stream(self):
        from zspan_cli import boot
        import io

        class AsciiStream(io.StringIO):
            encoding = "ascii"

            def write(self, text):
                text.encode("ascii")
                return super().write(text)

        out = AsciiStream()
        tb = boot.TerminalBoot(out=out)
        tb.say("one → two · three …")
        self.assertEqual(tb.step("the local server", lambda: 42), 42)
        tb.finish("Resolved · local", [("grey", "Ctrl-C stops — safely")])
        tb.fail("Done ✓ · no ellipsis …")
        text = out.getvalue()
        text.encode("ascii")
        self.assertIn("one -> two - three ...", text)
        self.assertIn("Done OK - no ellipsis ...", text)

    def test_keyboard_interrupt_mid_intro_restores_terminal_once(self):
        from zspan_cli import boot
        import io

        out = io.StringIO()
        poll = mock.Mock()
        poll.available = False
        with mock.patch.object(boot, "detect_capabilities",
                               return_value=self._spec(width=80)), \
             mock.patch.object(boot, "_SkipPoll", return_value=poll), \
             mock.patch.object(boot.time, "sleep",
                               side_effect=KeyboardInterrupt):
            tb = boot.TerminalBoot(out=out)
            with self.assertRaises(KeyboardInterrupt):
                tb._play_intro()
            tb._cleanup_terminal()  # idempotence is part of the contract
        self.assertEqual(out.getvalue().count("\x1b[?25h"), 1)
        self.assertNotIn("\x1b[3J", out.getvalue())
        poll.close.assert_called_once_with()

    def test_keyboard_interrupt_mid_ocean_restores_terminal_once(self):
        from zspan_cli import boot
        import io

        out = io.StringIO()
        poll = mock.Mock()
        poll.available = False
        with mock.patch.object(boot, "detect_capabilities",
                               return_value=self._spec(width=80)), \
             mock.patch.object(boot.time, "sleep",
                               side_effect=KeyboardInterrupt):
            tb = boot.TerminalBoot(out=out)
            tb._intro_done = True
            tb._poll = poll
            with self.assertRaises(KeyboardInterrupt):
                tb.step("the local server", lambda: 42)
            tb._cleanup_terminal()
        self.assertEqual(out.getvalue().count("\x1b[?25h"), 1)
        poll.close.assert_called_once_with()

    def test_step_failure_surfaces_plainly(self):
        from zspan_cli import boot
        import io
        out = io.StringIO()
        tb = boot.TerminalBoot(out=out)

        def boom():
            raise RuntimeError("port taken")

        with self.assertRaises(RuntimeError):
            tb.step("the local server", boom)
        self.assertIn("→ the local server", out.getvalue())


class TestModelResolution(_TempHome):
    """Codex preference plus the enforced civic-synthesis model floor."""

    def test_strongest_reachable_ranks_the_keys_own_list(self):
        from zspan_cli import providers as prov

        ids = ["gpt-4o-mini", "gpt-4o", "whisper-1", "text-embedding-3-small",
               "gpt-4.1", "dall-e-3", "gpt-4o-realtime-preview"]
        # gpt-4.1 outranks gpt-4o (numeric version beats the bare 4o);
        # both outrank the rejected mini tier; non-synthesis ids never surface.
        self.assertEqual(prov.strongest_reachable("openai", ids), "gpt-4.1")
        ids.append("gpt-5.2")
        self.assertEqual(prov.strongest_reachable("openai", ids), "gpt-5.2")
        # A 4o-and-mini-only key resolves to gpt-4o, never the mini.
        self.assertEqual(
            prov.strongest_reachable("openai", ["gpt-4o-mini", "gpt-4o"]),
            "gpt-4o")
        # Mini-only key has no approved civic-synthesis model.
        self.assertEqual(
            prov.strongest_reachable("openai", ["gpt-4o-mini", "whisper-1"]),
            "")
        # No list → the provider's static default.
        self.assertEqual(prov.strongest_reachable("openai", None),
                         prov.PROVIDERS["openai"]["default_model"])
        self.assertEqual(
            prov.strongest_reachable("gemini", ["gemini-2.5-flash", "gemini-2.5-pro",
                                                "gemini-embedding-001"]),
            "gemini-2.5-pro")
        self.assertEqual(
            prov.strongest_reachable("anthropic",
                                     ["claude-haiku-4-5", "claude-sonnet-4-6"]),
            "claude-sonnet-4-6")
        self.assertFalse(
            prov.is_approved_synthesis_model("anthropic", "claude-haiku-4-5")
        )
        self.assertFalse(
            prov.is_approved_synthesis_model("gemini", "gemini-2.5-flash")
        )

    def test_resolution_uses_strongest_approved_and_rejects_opt_down(self):
        from zspan_cli import processing
        from zspan_cli import providers as prov

        cfg = {"synthesis_provider": "openai",
               "api_keys": {"openai": "sk-x"},
               "available_models": {"openai": ["gpt-4o-mini", "gpt-4o"]}}
        with mock.patch.object(prov, "codex_available", return_value=False):
            _p, _k, model = processing.resolve_synthesis_setup(cfg)
        self.assertEqual(model, "gpt-4o")
        with mock.patch.object(prov, "codex_available", return_value=False), \
             self.assertRaises(processing.PipelineSetupError) as override_ctx:
            processing.resolve_synthesis_setup(cfg, model_override="gpt-4o-mini")
        self.assertIn("mini, nano, flash, and haiku", str(override_ctx.exception))
        self.assertIn("gpt-5.6-sol", str(override_ctx.exception))
        cfg["synthesis_model"] = "gpt-4o-mini"
        with mock.patch.object(prov, "codex_available", return_value=False), \
             self.assertRaises(processing.PipelineSetupError):
            processing.resolve_synthesis_setup(cfg)

    def test_economy_only_key_fails_with_actionable_process_guidance(self):
        from zspan_cli import processing
        from zspan_cli import providers as prov

        cfg = {
            "synthesis_provider": "gemini",
            "api_keys": {"gemini": "AIza-x"},
            "available_models": {
                "gemini": ["gemini-2.5-flash", "gemini-2.0-flash-lite"]
            },
        }
        with mock.patch.object(prov, "codex_available", return_value=False), \
             self.assertRaises(processing.PipelineSetupError) as ctx:
            processing.resolve_synthesis_setup(cfg)
        message = str(ctx.exception)
        self.assertIn("does not meet Z-SPAN's civic-synthesis model floor", message)
        self.assertIn("Gemini Pro", message)
        self.assertIn("Install the Codex CLI", message)
        self.assertIn("gemini-2.5-flash", message)

    def test_explicit_byok_provider_wins_over_installed_codex(self):
        from zspan_cli import processing
        from zspan_cli import providers as prov
        from zspan_cli import serve

        cases = {
            "openai": ("sk-x", "gpt-4.1"),
            "anthropic": ("sk-ant-x", "claude-sonnet-4-6"),
            "gemini": ("AIza-x", "gemini-2.5-pro"),
        }
        with mock.patch.object(prov, "codex_available", return_value=True):
            for provider, (key, default_model) in cases.items():
                with self.subTest(provider=provider):
                    cfg = {
                        "synthesis_provider": provider,
                        "api_keys": {provider: key},
                    }
                    expected = (provider, key, default_model)
                    self.assertEqual(
                        processing.resolve_synthesis_setup(cfg), expected
                    )
                    self.assertEqual(
                        serve._resolve_librarian_engine(cfg), expected
                    )

    def test_stored_key_without_provider_marker_is_a_byok_selection(self):
        from zspan_cli import processing
        from zspan_cli import providers as prov

        cfg = {"api_keys": {"openai": "sk-x"}}
        with mock.patch.object(prov, "codex_available", return_value=True):
            self.assertEqual(
                processing.resolve_synthesis_setup(cfg),
                ("openai", "sk-x", "gpt-4.1"),
            )

    def test_codex_engine_is_keyless_and_honest_when_absent(self):
        from zspan_cli import processing
        from zspan_cli import providers as prov

        with mock.patch.object(prov, "codex_available", return_value=True):
            p, key, model = processing.resolve_synthesis_setup(
                {}, provider_override="codex")
        self.assertEqual((p, key, model), ("codex", "", prov.CODEX_DEFAULT_MODEL))
        with mock.patch.object(prov, "codex_available", return_value=False):
            with self.assertRaises(processing.PipelineSetupError):
                processing.resolve_synthesis_setup({}, provider_override="codex")

    def test_keyless_config_defaults_to_codex_when_available(self):
        # Zero config (no `zspan init`) + codex installed -> keyless codex with
        # NO override needed. Regression guard: cmd_process's hard init gate +
        # the missing codex-when-keyless default in the resolver blocked exactly
        # this (operator-caught 2026-07-11 — `zspan process` demanded init).
        from zspan_cli import processing
        from zspan_cli import providers as prov
        with mock.patch.object(prov, "codex_available", return_value=True):
            p, key, _model = processing.resolve_synthesis_setup({})
        self.assertEqual((p, key), ("codex", ""))
        # No codex AND no key must still raise (never a silent pass).
        with mock.patch.object(prov, "codex_available", return_value=False):
            with self.assertRaises(processing.PipelineSetupError) as ctx:
                processing.resolve_synthesis_setup({})
        message = str(ctx.exception)
        self.assertIn("Codex CLI isn't reachable from this launch context", message)
        self.assertIn("checked", message)
        self.assertNotIn("zspan init", message)

    def test_codex_call_shape_and_extraction(self):
        completed = mock.Mock(returncode=0, stdout=b"", stderr=b"")

        def fake_run(cmd, **kwargs):
            # The invocation discipline: explicit model + reasoning flags.
            self.assertEqual(cmd[0], "/fake/bin/codex")
            self.assertEqual(kwargs["env"]["PATH"].split(os.pathsep)[0],
                             str(Path("/fake/bin")))
            self.assertIn("--model", cmd)
            self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-5.6-sol")
            self.assertIn("model_reasoning_effort=high", cmd)
            out_path = cmd[cmd.index("-o") + 1]
            Path(out_path).write_text("[]", encoding="utf-8")
            return completed

        with mock.patch(
            "zspan_cli.providers.resolve_codex_binary",
            return_value="/fake/bin/codex",
        ), mock.patch("subprocess.run", side_effect=fake_run):
            out = synthesize.synthesize("codex", "", "gpt-5.6-sol", "PROMPT")
        self.assertEqual(out, "[]")


class TestDiscussion(_TempHome):
    """The deterministic decision-Discussion locator + its preview shims
    — anchors locate the moment, the vote extends it, absence stays
    honest, and the payloads are exactly the client's sidecar shapes."""

    KD = ("1. <core>Approved Resolution R-15</core> for the **water "
          "contract** by a vote of 5-2.\n\n"
          "2. Directed staff to prepare the annual report.")
    VIDEO = "https://www.youtube.com/watch?v=discussion-test"

    def setUp(self):
        super().setUp()
        from zspan_cli import serve
        serve._DISCUSSION_CACHE.clear()
        serve._FLAGSHIP_MEETING_QUOTES_CACHE.clear()

    def tearDown(self):
        from zspan_cli import serve
        serve._DISCUSSION_CACHE.clear()
        serve._FLAGSHIP_MEETING_QUOTES_CACHE.clear()
        super().tearDown()

    def _seed_discussion(self, *, flagship_id=700, video=None, source_url=None):
        video = self.VIDEO if video is None else video
        source_url = self.VIDEO if source_url is None else source_url
        conn = workspace.connect()
        row = _event(flagship_id or 700, "Council", "2026-07-01", video=video)
        if flagship_id is None:
            row.pop("id")
            row["public_id"] = "m_HANDOFFDISCUSSION00001"
        workspace.upsert_meeting(conn, row)
        meeting = conn.execute(
            "SELECT * FROM meetings WHERE public_id = ?", (row["public_id"],)
        ).fetchone()
        meeting_id = int(meeting["id"])
        workspace.save_output(
            conn, meeting_id, "key_decisions", content=self.KD,
            provider="openai", model="gpt-4o-mini",
            gate_status="ok", gate_log="{}",
        )
        tdir = Path(self._tmp.name) / "discussion-transcripts"
        tdir.mkdir(parents=True, exist_ok=True)
        transcript_path = tdir / f"{meeting_id}.json"
        transcript_path.write_text(json.dumps({
            "words": _VOTE_TRANSCRIPT,
            "duration_seconds": 31.0,
            "language": "en",
            "source_url": source_url,
        }), encoding="utf-8")
        workspace.set_transcript_path(conn, meeting_id, str(transcript_path))
        conn.commit()
        return conn, meeting_id

    @staticmethod
    def _record_payload(*spans):
        quotes = []
        routing = []
        for index, (start_ms, end_ms) in enumerate(spans):
            quotes.append({
                "speaker_name": "From the meeting record",
                "speaker_role": None,
                "speaker_class": "record",
                "quote_text": f"record window {index + 1}",
                "video_timestamp_seconds": start_ms / 1000,
                "selection_rationale": f"record rationale {index + 1}",
                "word_timings": [
                    {"word": "first", "start_ms": start_ms,
                     "end_ms": start_ms + 100},
                    {"word": "last", "start_ms": end_ms - 100,
                     "end_ms": end_ms},
                ],
            })
            routing.append({
                "quote_index": index,
                "bucket": "decision_bound",
                "decision_index": index + 1,
            })
        return {
            "quotes": quotes,
            "routing": routing,
            "summary": {
                "standalone_count": 0,
                "decision_bound_count": len(routing),
                "drop_count": 0,
            },
        }

    @staticmethod
    def _named_quote(name, start_ms, end_ms, *, status="verified"):
        return {
            "speaker_name": name,
            "speaker_role": "Council Member",
            "speaker_class": "council_member",
            "quote_text": f"{name} spoke",
            "video_timestamp_seconds": start_ms / 1000,
            "word_timings": [
                {"word": name, "start_ms": start_ms, "end_ms": start_ms + 100},
                {"word": "spoke", "start_ms": end_ms - 100, "end_ms": end_ms},
            ],
            "verified_status": status,
        }

    def test_flagship_quote_replaces_only_its_owned_record_window(self):
        from zspan_cli import discussion, serve

        built = self._record_payload((1000, 5000), (10000, 14000))
        named = self._named_quote("Alice Example", 2000, 3000)
        conn, meeting_id = self._seed_discussion()
        try:
            with mock.patch.object(discussion, "build_discussion", return_value=built), \
                 mock.patch.object(serve, "_flagship_meeting_quotes",
                                   return_value=[named]):
                payload = serve._discussion_payload(conn, meeting_id)
        finally:
            conn.close()

        self.assertEqual(payload["quotes"][0]["speaker_name"], "Alice Example")
        self.assertNotEqual(payload["quotes"][0].get("speaker_class"), "record")
        self.assertIs(payload["quotes"][0]["word_timings"], named["word_timings"])
        self.assertEqual(payload["quotes"][0]["speaker_name"],
                         named["speaker_name"])
        self.assertNotIn("verified_status", payload["quotes"][0])
        self.assertIs(payload["quotes"][1], built["quotes"][1])
        self.assertEqual(payload["quotes"][1]["speaker_class"], "record")
        self.assertEqual(payload["routing"], [
            {"quote_index": 0, "bucket": "decision_bound", "decision_index": 1},
            {"quote_index": 1, "bucket": "decision_bound", "decision_index": 2},
        ])

    def test_two_speakers_in_one_window_stay_separate(self):
        from zspan_cli import discussion, serve

        built = self._record_payload((1000, 5000))
        named = [
            self._named_quote("Alice Example", 1500, 2500),
            self._named_quote("Bob Example", 2600, 3600),
        ]
        conn, meeting_id = self._seed_discussion(flagship_id=701)
        try:
            with mock.patch.object(discussion, "build_discussion", return_value=built), \
                 mock.patch.object(serve, "_flagship_meeting_quotes",
                                   return_value=named):
                payload = serve._discussion_payload(conn, meeting_id)
        finally:
            conn.close()

        self.assertEqual(
            [quote["speaker_name"] for quote in payload["quotes"]],
            ["Alice Example", "Bob Example"],
        )
        self.assertEqual([route["decision_index"] for route in payload["routing"]],
                         [1, 1])
        self.assertEqual(payload["summary"]["decision_bound_count"], 2)

    def test_gated_flagship_quotes_leave_record_excerpt(self):
        from zspan_cli import discussion, serve

        built = self._record_payload((1000, 5000))
        conn, meeting_id = self._seed_discussion(flagship_id=702)
        response = _resp(200, {
            "success": True, "source": "gated", "quotes": [], "count": 0,
        })
        try:
            with mock.patch.object(discussion, "build_discussion", return_value=built), \
                 mock.patch("requests.get", return_value=response) as fetch:
                payload = serve._discussion_payload(conn, meeting_id)
        finally:
            conn.close()

        self.assertIs(payload, built)
        self.assertEqual(payload["quotes"][0]["speaker_class"], "record")
        fetch.assert_called_once()

    def test_flagship_fetch_failure_leaves_record_excerpt(self):
        from zspan_cli import discussion, serve

        built = self._record_payload((1000, 5000))
        conn, meeting_id = self._seed_discussion(flagship_id=703)
        try:
            with mock.patch.object(discussion, "build_discussion", return_value=built), \
                 mock.patch("requests.get", side_effect=requests.ConnectionError("offline")):
                payload = serve._discussion_payload(conn, meeting_id)
        finally:
            conn.close()

        self.assertIs(payload, built)
        self.assertEqual(payload["quotes"][0]["speaker_class"], "record")

    def test_no_flagship_id_or_recording_mismatch_never_fetches(self):
        from zspan_cli import discussion, serve

        for flagship_id, video, source in (
            (None, self.VIDEO, self.VIDEO),
            (704, self.VIDEO, "https://www.youtube.com/watch?v=older-recording"),
        ):
            serve._DISCUSSION_CACHE.clear()
            built = self._record_payload((1000, 5000))
            conn, meeting_id = self._seed_discussion(
                flagship_id=flagship_id, video=video, source_url=source,
            )
            try:
                with mock.patch.object(discussion, "build_discussion",
                                       return_value=built), \
                     mock.patch.object(serve, "_flagship_meeting_quotes") as fetch:
                    payload = serve._discussion_payload(conn, meeting_id)
            finally:
                conn.close()
            self.assertIs(payload, built)
            self.assertEqual(payload["quotes"][0]["speaker_class"], "record")
            fetch.assert_not_called()

    def test_disputed_and_malformed_timing_rows_never_merge(self):
        from zspan_cli import discussion, serve

        built = self._record_payload((1000, 5000))
        disputed = self._named_quote("Disputed", 1500, 2500,
                                     status="disputed")
        malformed = self._named_quote("Malformed", 1500, 2500)
        malformed["word_timings"] = "not-json"
        point_only = self._named_quote("Point only", 1500, 2500)
        point_only.pop("word_timings")
        point_only["video_timestamp_seconds"] = 2.0
        conn, meeting_id = self._seed_discussion(flagship_id=705)
        try:
            with mock.patch.object(discussion, "build_discussion", return_value=built), \
                 mock.patch.object(serve, "_flagship_meeting_quotes", return_value=[
                     disputed, malformed, point_only,
                 ]):
                payload = serve._discussion_payload(conn, meeting_id)
        finally:
            conn.close()

        self.assertIs(payload, built)
        self.assertEqual(payload["quotes"][0]["speaker_class"], "record")

    def test_cross_window_quote_has_one_greatest_overlap_owner(self):
        from zspan_cli import discussion, serve

        built = self._record_payload((0, 10000), (6000, 14000))
        named = self._named_quote("Second Window", 7000, 13000)
        conn, meeting_id = self._seed_discussion(flagship_id=706)
        try:
            with mock.patch.object(discussion, "build_discussion", return_value=built), \
                 mock.patch.object(serve, "_flagship_meeting_quotes",
                                   return_value=[named]):
                payload = serve._discussion_payload(conn, meeting_id)
        finally:
            conn.close()

        self.assertEqual(len(payload["quotes"]), 2)
        self.assertEqual(payload["quotes"][0]["speaker_class"], "record")
        self.assertEqual(payload["quotes"][1]["speaker_name"], "Second Window")
        self.assertEqual(
            [route for route in payload["routing"] if route["decision_index"] == 2],
            [{"quote_index": 1, "bucket": "decision_bound", "decision_index": 2}],
        )

    def test_unchanged_fingerprint_reuses_one_payload_and_flagship_generation(self):
        from zspan_cli import discussion, serve

        built = self._record_payload((1000, 5000))
        named = self._named_quote("Cached Name", 1500, 2500)
        response = _resp(200, {
            "success": True, "source": "quotes_table", "quotes": [named], "count": 1,
        })
        conn, meeting_id = self._seed_discussion(flagship_id=707)
        try:
            with mock.patch.object(discussion, "build_discussion", return_value=built) \
                    as locate, mock.patch("requests.get", return_value=response) as fetch:
                first = serve._discussion_payload(conn, meeting_id)
                second = serve._discussion_payload(conn, meeting_id)
        finally:
            conn.close()

        self.assertIs(first, second)
        self.assertEqual(first["quotes"][0]["speaker_name"], "Cached Name")
        locate.assert_called_once()
        fetch.assert_called_once()

    def test_cast_subpaths_proxy_and_keep_members_shaped_offline(self):
        from zspan_cli import serve

        handler = object.__new__(serve._Handler)
        handler._send_json = mock.Mock()
        with mock.patch.object(
            serve, "_flagship_proxy", return_value=(200, {"members": [{"id": 1}]})
        ) as proxy:
            handler._route_api("/api/cast/Kingman/seat-1")
        proxy.assert_called_once_with("/api/cast/Kingman/seat-1")
        handler._send_json.assert_called_once_with(200, {"members": [{"id": 1}]})

        handler._send_json.reset_mock()
        offline = {"ok": False, "success": False, "offline": True}
        with mock.patch.object(serve, "_flagship_proxy",
                               return_value=(200, offline.copy())):
            handler._route_api("/api/cast/Kingman")
        payload = handler._send_json.call_args.args[1]
        self.assertEqual(payload["members"], [])
        self.assertTrue(payload["offline"])

    def test_locator_finds_ref_extends_through_vote(self):
        from zspan_cli import discussion

        payload = discussion.build_discussion(self.KD, _VOTE_TRANSCRIPT)
        # Item 1 locates by its resolution ref; item 2 has no anchors —
        # honest absence, no guessed window.
        self.assertEqual(len(payload["quotes"]), 1)
        self.assertEqual(payload["routing"], [{
            "quote_index": 0, "bucket": "decision_bound", "decision_index": 1,
        }])
        q = payload["quotes"][0]
        self.assertIn("r -15", q["quote_text"])
        self.assertIn("carries", q["quote_text"])      # vote extension
        self.assertEqual(q["speaker_class"], "record")
        self.assertIn("resolution 15", q["selection_rationale"])
        self.assertIn("through the vote moment", q["selection_rationale"])
        self.assertIn("no AI selection", q["selection_rationale"])
        # SyncedQuote's contract: integer milliseconds.
        first = q["word_timings"][0]
        self.assertIsInstance(first["start_ms"], int)
        self.assertEqual(first["start_ms"], 0)
        self.assertEqual(q["word_timings"][1]["start_ms"], 1000)

    def test_synthetic_core_phrases_bind_all_five_decisions(self):
        from zspan_cli import discussion

        fixture_path = (
            Path(__file__).with_name("data")
            / "synthetic_five_decision_discussion.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        words = [
            {"word": word, "start": start, "end": end}
            for word, start, end in fixture["words"]
        ]

        payload = discussion.build_discussion(fixture["key_decisions"], words)

        self.assertEqual(
            [route["decision_index"] for route in payload["routing"]],
            [1, 2, 3, 4, 5],
        )
        timestamps = {
            str(route["decision_index"]): payload["quotes"][route["quote_index"]][
                "video_timestamp_seconds"
            ]
            for route in payload["routing"]
        }
        for decision_index, expected in fixture["expected_timestamps"].items():
            self.assertAlmostEqual(timestamps[decision_index], expected, delta=3.0)
        self.assertEqual(
            payload["summary"]["citation_coverage"],
            {
                "state": "valid",
                "complete": True,
                "required_decision_indices": [1, 2, 3, 4, 5],
                "produced_decision_indices": [1, 2, 3, 4, 5],
                "missing_decision_indices": [],
            },
        )

    def test_core_phrase_ambiguity_and_short_common_core_stay_uncited(self):
        from zspan_cli import discussion

        ambiguous = _words(
            "regional", "transit", "agreement", "then", "later",
            "regional", "transit", "agreement",
        )
        payload = discussion.build_discussion(
            "1. <core>Approved the regional transit agreement</core>.", ambiguous,
        )
        self.assertEqual(payload["quotes"], [])
        self.assertEqual(
            payload["summary"]["citation_coverage"]["state"],
            "citation_incomplete",
        )

        payload = discussion.build_discussion(
            "1. <core>Approved the park</core>.", _words("the", "park", "opened"),
        )
        self.assertEqual(payload["quotes"], [])
        self.assertEqual(
            payload["summary"]["citation_coverage"]["missing_decision_indices"],
            [1],
        )

    def test_zero_decisions_is_valid_empty_not_citation_incomplete(self):
        from zspan_cli import discussion

        payload = discussion.build_discussion("", _VOTE_TRANSCRIPT)
        self.assertEqual(payload["quotes"], [])
        self.assertEqual(payload["routing"], [])
        self.assertEqual(
            payload["summary"]["citation_coverage"],
            {
                "state": "no_decisions_pending_classification",
                "complete": True,
                "required_decision_indices": [],
                "produced_decision_indices": [],
                "missing_decision_indices": [],
            },
        )

    def test_citation_incomplete_cached_output_is_not_finished_on_read(self):
        from zspan_cli import serve

        conn, meeting_id = self._seed_discussion(
            flagship_id=708,
            source_url="https://www.youtube.com/watch?v=older-recording",
        )
        try:
            notebook, status = serve._notebook(conn, meeting_id)
            publish_status = serve._publish_status(conn, str(meeting_id))
            city_payload = serve._city_meetings(conn, "Kingman")
            tree_city = serve._channels_tree(conn)["states"][0]["counties"][0][
                "cities"
            ][0]
        finally:
            conn.close()

        self.assertEqual(status, 200)
        self.assertIsNone(notebook["approved_at"])
        self.assertFalse(notebook["completeness"]["complete"])
        coverage = notebook["completeness"]["citation_coverage"]
        self.assertEqual(coverage["required_decision_indices"], [1, 2])
        self.assertEqual(coverage["produced_decision_indices"], [1])
        self.assertEqual(coverage["missing_decision_indices"], [2])
        self.assertFalse(publish_status["meeting"]["is_published"])
        self.assertIsNone(publish_status["meeting"]["published_at"])
        self.assertEqual(
            publish_status["meeting"]["citation_coverage"]["state"],
            "citation_incomplete",
        )
        self.assertFalse(city_payload["events"][0]["is_published"])
        self.assertEqual(tree_city["status"], "cached")
        self.assertEqual(tree_city["broadcast_count"], 0)

    def test_case_id_locator_stays_out_of_welsh_garbage(self):
        from zspan_cli import discussion

        welsh = ["cymraeg"] * 65
        # More than LEAD_WORDS of English precedes the anchor, keeping the
        # Welsh hallucination prefix outside the selected quote window.
        english = (["english"] * 45 + [
            "item", "AB", "25", "0005", "consideration", "for", "a",
            "request", "to", "abandon", "right", "of", "way", "continued",
        ])
        self.assertEqual(grounding.extract_refs(" ".join(welsh)), set())
        words = _words(*(welsh + english))
        kd = "1. <core>Continued item AB 25 0005 off calendar</core>"

        payload = discussion.build_discussion(kd, words)

        self.assertGreaterEqual(len(payload["quotes"]), 1)
        quote = payload["quotes"][0]
        self.assertIn("item AB 25 0005 consideration", quote["quote_text"])
        self.assertNotIn("cymraeg", quote["quote_text"])
        self.assertGreaterEqual(
            quote["word_timings"][0]["start_ms"], len(welsh) * 1000,
        )
        self.assertLessEqual(
            quote["word_timings"][-1]["end_ms"], len(words) * 1000,
        )

    def test_dollar_and_quote_anchors_locate(self):
        from zspan_cli import discussion

        words = _words(
            "the", "budget", "totals", "$199", ",750", ",036", "for",
            "the", "coming", "year", "and", "council", "moved", "on",
        )
        kd = "1. Adopted the $199,750,036 budget."
        payload = discussion.build_discussion(kd, words)
        self.assertEqual(len(payload["quotes"]), 1)
        self.assertIn("$199", payload["quotes"][0]["quote_text"])
        self.assertIn("$199,750,036", payload["quotes"][0]["selection_rationale"])

        kd_quote = '1. Announced that "the library will open on saturday".'
        payload = discussion.build_discussion(kd_quote, _VOTE_TRANSCRIPT)
        self.assertEqual(len(payload["quotes"]), 1)
        self.assertIn("library", payload["quotes"][0]["quote_text"])
        self.assertIn("verbatim quote", payload["quotes"][0]["selection_rationale"])

    def test_verbatim_quote_alignment_uses_timestamp_hint(self):
        from zspan_cli import discussion

        words = _words(
            "Please,", "volunteer", "today!", "gap",
            "Please,", "volunteer", "today!", spacing=10.0,
        )
        timings = discussion.align_verbatim_quote(
            "Please, volunteer today!", words, 42.0,
        )
        self.assertEqual(
            [timing["word"] for timing in timings],
            ["Please,", "volunteer", "today!"],
        )
        self.assertEqual(timings[0]["start_ms"], 40000)
        self.assertEqual(timings[-1]["end_ms"], 60500)
        self.assertEqual(
            discussion.align_verbatim_quote("invented civic ask", words, 0.0),
            [],
        )

    def test_notebook_adds_ccta_karaoke_without_rewriting_content(self):
        from zspan_cli import serve

        conn, meeting_id = self._seed_discussion(flagship_id=709)
        ccta = json.dumps([{
            "speaker_name": "City Clerk",
            "speaker_role": "Clerk",
            "quote_text": "the library will open on saturday morning",
            "video_timestamp_seconds": 25.0,
        }])
        workspace.save_output(
            conn, meeting_id, "community_calls_to_action", content=ccta,
            provider="openai", model="gpt-4o-mini",
            gate_status="ok", gate_log="{}",
        )
        try:
            notebook, status = serve._notebook(conn, meeting_id)
        finally:
            conn.close()

        self.assertEqual(status, 200)
        output = notebook["outputs"]["community_calls_to_action"]
        self.assertEqual(output["content"], ccta)
        timings = output["karaoke_word_timings"][0]
        self.assertEqual(
            [timing["word"] for timing in timings],
            ["the", "library", "will", "open", "on", "saturday", "morning"],
        )
        self.assertEqual(timings[0]["start_ms"], 24000)
        self.assertEqual(timings[-1]["end_ms"], 30500)
        self.assertNotIn(
            "karaoke_word_timings", notebook["outputs"]["key_decisions"],
        )

    def test_window_cap_keeps_moments_listenable(self):
        from zspan_cli import discussion

        words = _words(*(["filler"] * 600), "resolution", "r", "-15",
                       "passes", *(["tail"] * 100))
        payload = discussion.build_discussion("1. Approved R-15.", words)
        self.assertEqual(len(payload["quotes"]), 1)
        n = len(payload["quotes"][0]["word_timings"])
        self.assertLessEqual(n, discussion.WINDOW_CAP_WORDS)
        self.assertGreater(n, 10)

    def test_preview_shims_serve_the_sidecar_shapes(self):
        from urllib.request import urlopen

        from zspan_cli import serve

        config.save_config({"synthesis_provider": "openai",
                            "api_keys": {"openai": "sk-test-abcdefgh1234"}})
        conn = workspace.connect()
        workspace.upsert_meeting(
            conn, _event(7, "Council", "2026-07-01", video="https://v"))
        meeting_id = conn.execute(
            "SELECT id FROM meetings WHERE flagship_row_id = 7"
        ).fetchone()["id"]
        workspace.save_output(
            conn, meeting_id, "key_decisions", content=self.KD,
            provider="openai", model="gpt-4o-mini",
            gate_status="ok", gate_log="{}",
        )
        conn.commit()
        conn.close()
        tdir = Path(self._tmp.name) / "transcripts"
        tdir.mkdir(parents=True, exist_ok=True)
        transcript_path = tdir / f"{meeting_id}.json"
        transcript_path.write_text(json.dumps({
            "words": _VOTE_TRANSCRIPT, "duration_seconds": 31.0, "language": "en",
        }), encoding="utf-8")
        conn = workspace.connect()
        workspace.set_transcript_path(conn, meeting_id, str(transcript_path))
        conn.close()

        server = serve.start_server(port=0)
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urlopen(f"{base}/api/preview/decisions/{meeting_id}") as r:
                self.assertEqual(json.load(r)["prose_output"], self.KD)
            with urlopen(f"{base}/api/preview/quotes/{meeting_id}") as r:
                quotes = json.load(r)
            self.assertEqual(quotes["quote_count"], 1)
            self.assertEqual(quotes["quotes"][0]["word_timings"][1]["start_ms"], 1000)
            with urlopen(f"{base}/api/preview/routing/{meeting_id}") as r:
                routing = json.load(r)
            self.assertEqual(routing["routing"][0]["decision_index"], 1)
            self.assertEqual(routing["summary"]["decision_bound_count"], 1)
            with urlopen(f"{base}/api/preview/recusals/{meeting_id}") as r:
                self.assertEqual(json.load(r), {"recusal_count": 0, "recusals": []})
            # Un-processed meeting → no sidecar → the client's fallback.
            conn = workspace.connect()
            workspace.upsert_meeting(conn, _event(9, "Planning", "2026-07-03"))
            planning_id = conn.execute(
                "SELECT id FROM meetings WHERE flagship_row_id = 9"
            ).fetchone()["id"]
            conn.commit()
            conn.close()
            with urlopen(f"{base}/api/preview/decisions/{planning_id}") as r:
                self.assertEqual(json.load(r), {})
            with urlopen(f"{base}/api/preview/quotes/{planning_id}") as r:
                self.assertEqual(json.load(r)["quote_count"], 0)
        finally:
            server.shutdown()
            serve._DISCUSSION_CACHE.clear()


class TestPI6ServeSeams(_TempHome):
    def setUp(self):
        super().setUp()
        from zspan_cli import serve

        serve._PROCESS_STATE.update(
            meeting_id=None, lines=[], running=False, done=False, ok=None, error=None,
            engine=None, run_started_monotonic=None,
            pending_approval=None, approval_waiter=None,
        )

    def test_process_post_requires_ack_and_body_flag_records_it(self):
        from zspan_cli import processing, serve

        config.save_config({
            "api_keys": {"openai": "sk-test-abcdefghijkl"},
            "synthesis_provider": "openai",
        })
        status, payload = serve._kick_process(1, {})
        self.assertEqual(status, 428)
        self.assertTrue(payload["ack_required"])

        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(
            processing, "run_pipeline", return_value={"ok": True}
        ) as run, mock.patch.object(serve.threading, "Thread", ImmediateThread):
            status, payload = serve._kick_process(
                1, {"acknowledge_local_processing": True}
            )
        self.assertEqual((status, payload), (200, {"started": True}))
        self.assertTrue(config.has_processing_ack(config.load_config()))
        run.assert_called_once()

    def test_keyless_primary_process_uses_codex(self):
        from zspan_cli import processing, serve

        config.save_config(config.record_processing_ack({}))

        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with mock.patch(
            "zspan_cli.providers.codex_available", return_value=True,
        ), mock.patch.object(
            processing, "run_pipeline", return_value={"ok": True},
        ) as run, mock.patch.object(serve.threading, "Thread", ImmediateThread):
            status, payload = serve._kick_process(1, {})

        self.assertEqual((status, payload), (200, {"started": True}))
        self.assertEqual(run.call_args.kwargs["provider_override"], "codex")

    def test_keyless_process_error_names_unreachable_engine(self):
        from zspan_cli import serve

        config.save_config(config.record_processing_ack({}))
        with mock.patch(
            "zspan_cli.providers.codex_available", return_value=False,
        ):
            status, payload = serve._kick_process(1, {})

        self.assertEqual(status, 400)
        self.assertFalse(payload["started"])
        self.assertIn("Codex CLI isn't reachable from this launch context",
                      payload["error"])
        self.assertIn("checked", payload["error"])
        self.assertNotIn("zspan init", payload["error"])

    def test_pipeline_seam_refuses_unacknowledged_callers(self):
        from zspan_cli import processing

        with self.assertRaises(processing.PipelineSetupError) as ctx:
            processing.run_pipeline(1, config={}, progress=lambda _m: None)
        self.assertEqual(str(ctx.exception), config.PROCESSING_ACK_TEXT)

    def _post_approval_route(self, serve, meeting_id, body):
        encoded = json.dumps(body).encode("utf-8")
        captured = {}
        handler = serve._Handler.__new__(serve._Handler)
        handler.path = f"/api/local/process/{meeting_id}/approval"
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        handler._guard_ok = lambda *, mutating: True
        handler._send_json = lambda status, payload: captured.update(
            status=status, payload=payload,
        )
        handler._publish_request = lambda _path: None
        handler.do_POST()
        return captured["status"], captured["payload"]

    def test_web_approval_route_drives_each_decision_and_is_idempotent(self):
        import threading
        import time
        from types import SimpleNamespace

        from zspan_cli import approval, serve

        review = {
            "output_type": "synopsis",
            "chunk_index": 1,
            "chunk_total": 4,
            "retrieval_query": "What happened?",
            "retrieved_chunks": [SimpleNamespace(
                start_seconds=12.0,
                chunk_index=3,
                score=0.875,
                text="Council discussed the water contract.",
            )],
            "canonical_prompt": "Summarize the meeting.",
            "full_envelope": "RAW\nENVELOPE\nVERBATIM",
            "provider": "gemini",
            "model": "gemini-test",
            "key_fingerprint_str": "AIza...0000",
        }
        expected = {
            "proceed": approval.ApprovalDecision.PROCEED,
            "skip": approval.ApprovalDecision.SKIP,
            "abort": approval.ApprovalDecision.ABORT_ALL,
        }

        try:
            for raw, expected_decision in expected.items():
                with self.subTest(decision=raw):
                    with serve._PROCESS_LOCK:
                        serve._PROCESS_STATE.update(
                            meeting_id=1, running=True, done=False,
                            pending_approval=None, approval_waiter=None,
                        )
                    result = []
                    thread = threading.Thread(
                        target=lambda: result.append(
                            serve._web_approve_chunk(**review)
                        ),
                    )
                    thread.start()
                    for _ in range(100):
                        with serve._PROCESS_LOCK:
                            pending = serve._PROCESS_STATE.get("pending_approval")
                        if pending is not None:
                            break
                        time.sleep(0.01)
                    self.assertIsNotNone(pending)
                    self.assertEqual(pending["full_envelope"], review["full_envelope"])
                    self.assertIn("[0:12]", pending["retrieved_chunks"][0]["display_text"])

                    self.assertEqual(
                        self._post_approval_route(
                            serve, 1, {"decision": raw}
                        ),
                        (200, {
                        "accepted": True, "pending": False,
                        }),
                    )
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(result, [expected_decision])

                    second_status, second_payload = self._post_approval_route(
                        serve, 1, {"decision": raw}
                    )
                    self.assertEqual(second_status, 409)
                    self.assertFalse(second_payload["pending"])
        finally:
            with serve._PROCESS_LOCK:
                serve._PROCESS_STATE.update(running=False)

    def test_web_approval_route_rejects_when_nothing_is_pending(self):
        from zspan_cli import serve

        with serve._PROCESS_LOCK:
            serve._PROCESS_STATE.update(
                meeting_id=1, running=True,
                pending_approval=None, approval_waiter=None,
            )
        status, payload = self._post_approval_route(
            serve, 1, {"decision": "proceed"}
        )
        self.assertEqual(status, 409)
        self.assertFalse(payload["accepted"])
        self.assertFalse(payload["pending"])

    def test_server_shutdown_aborts_a_pending_web_approval(self):
        import threading
        import time
        from types import SimpleNamespace

        from zspan_cli import approval, serve

        with serve._PROCESS_LOCK:
            serve._PROCESS_STATE.update(
                meeting_id=1, running=True,
                pending_approval=None, approval_waiter=None,
            )
        result = []
        thread = threading.Thread(target=lambda: result.append(
            serve._web_approve_chunk(
                output_type="synopsis", chunk_index=1, chunk_total=4,
                retrieval_query="query",
                retrieved_chunks=[SimpleNamespace(
                    start_seconds=0.0, chunk_index=0, score=1.0, text="text",
                )],
                canonical_prompt="prompt", full_envelope="envelope",
                provider="gemini", model="test",
                key_fingerprint_str="AIza...0000",
            )
        ))
        try:
            thread.start()
            for _ in range(100):
                with serve._PROCESS_LOCK:
                    pending = serve._PROCESS_STATE.get("pending_approval")
                if pending is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(pending)
            server = serve._LocalThreadingHTTPServer.__new__(
                serve._LocalThreadingHTTPServer
            )
            with mock.patch.object(
                serve.ThreadingHTTPServer, "shutdown"
            ) as parent_shutdown:
                server.shutdown()
            parent_shutdown.assert_called_once_with()
            thread.join(timeout=2)
            self.assertEqual(result, [approval.ApprovalDecision.ABORT_ALL])
        finally:
            if thread.is_alive():
                serve._abort_pending_web_approval()
                thread.join(timeout=2)
            with serve._PROCESS_LOCK:
                serve._PROCESS_STATE.update(running=False)

    def test_home_tree_filters_only_foreign_handoffs(self):
        from zspan_cli import serve

        config.save_home_jurisdiction({}, "Arizona", "Mohave County", "Kingman")
        conn = workspace.connect()
        rows = [
            ({"public_id": "m_AAAAAAAAAAAAAAAAAAAAAA", "city_name": "Kingman",
              "county": "Mohave County", "state": "Arizona",
              "meeting_title": "Home", "meeting_date": "2026-07-01",
              "video_url": "https://youtu.be/home"}, "handoff"),
            ({"public_id": "m_BBBBBBBBBBBBBBBBBBBBBB", "city_name": "Phoenix",
              "county": "Maricopa County", "state": "Arizona",
              "meeting_title": "Foreign handoff", "meeting_date": "2026-07-02",
              "video_url": "https://youtu.be/foreign"}, "handoff"),
            ({**_event(303, "Foreign pull", "2026-07-03"),
              "city_name": "Tucson", "county": "Pima County",
              "public_id": "m_CCCCCCCCCCCCCCCCCCCCCC",
              "video_url": "https://youtu.be/pull"}, "pull"),
        ]
        for row, source in rows:
            workspace.upsert_meeting(conn, row, import_source=source)
        conn.commit()

        tree = serve._channels_tree(conn)
        names = {
            city["name"]
            for state in tree["states"]
            for county in state["counties"]
            for city in county["cities"]
        }
        self.assertEqual(names, {"Kingman", "Tucson"})
        self.assertEqual(serve._city_meetings(conn, "Phoenix")["count"], 1)

        config.save_config({})
        unscoped = serve._channels_tree(conn)
        conn.close()
        unscoped_names = {
            city["name"]
            for state in unscoped["states"]
            for county in state["counties"]
            for city in county["cities"]
        }
        self.assertEqual(unscoped_names, {"Kingman", "Phoenix", "Tucson"})

    def test_city_meetings_serve_only_processable_or_cached_rows(self):
        from zspan_cli import serve

        conn = workspace.connect()
        rows = [
            _event(401, "YouTube", "2026-07-01",
                   video="https://www.youtube.com/watch?v=abc"),
            _event(402, "Vendor", "2026-07-02",
                   video="https://kingman.granicus.com/MediaPlayer.php?clip_id=1"),
            _event(403, "Direct", "2026-07-03",
                   video="https://example.com/meeting.mp4"),
            _event(404, "No video", "2026-07-04"),
            _event(405, "Processed", "2026-07-05",
                   video="https://kingman.granicus.com/MediaPlayer.php?clip_id=2"),
        ]
        handoff = {
            **_event(406, "Handoff", "2026-07-06"),
            "availability": "coming_soon",
        }
        for row in rows:
            workspace.upsert_meeting(conn, row)
        workspace.upsert_meeting(conn, handoff, import_source="handoff")
        handoff["video_url"] = {"unexpected": "non-string"}
        conn.execute(
            "UPDATE meetings SET source_row_json = ? WHERE flagship_row_id = 406",
            (json.dumps(handoff),),
        )
        conn.execute(
            "UPDATE meetings SET processed_at = '2026-07-10T00:00:00Z' "
            "WHERE flagship_row_id = 405"
        )
        handoff_id = conn.execute(
            "SELECT id FROM meetings WHERE flagship_row_id = 406"
        ).fetchone()["id"]
        workspace.save_output(
            conn, handoff_id, "episode_tagline",
            content="Cached handoff", provider="codex", model="test",
            gate_status="ok", gate_log="{}",
        )
        conn.commit()

        payload = serve._city_meetings(conn, "Kingman")
        events = {
            row["meeting_title"]: row
            for row in payload["events"]
        }
        tree_city = serve._channels_tree(conn)["states"][0]["counties"][0]["cities"][0]
        cached_notebook, cached_status = serve._notebook(conn, handoff_id)
        no_video_id = conn.execute(
            "SELECT id FROM meetings WHERE flagship_row_id = 404"
        ).fetchone()["id"]
        filtered_notebook, filtered_status = serve._notebook(conn, no_video_id)
        conn.close()

        self.assertEqual(payload["count"], 4)
        self.assertEqual(
            set(events), {"YouTube", "Direct", "Processed", "Handoff"}
        )
        self.assertNotIn("Vendor", events)
        self.assertNotIn("No video", events)
        expected = {
            "YouTube": ("youtube", True),
            "Direct": ("direct_media", True),
        }
        for title, classification in expected.items():
            self.assertEqual(
                (events[title]["local_video_class"],
                 events[title]["local_processable"]),
                classification,
            )
        self.assertEqual(
            (events["Processed"]["local_video_class"],
             events["Processed"]["local_processable"]),
            ("vendor_page", False),
        )
        self.assertTrue(events["Processed"]["is_published"])
        self.assertEqual(events["Handoff"]["local_video_class"], "none")
        self.assertFalse(events["Handoff"]["local_processable"])
        self.assertTrue(events["Handoff"]["is_published"])
        self.assertEqual(events["Handoff"]["episode_tagline"], "Cached handoff")
        self.assertNotIn("availability", events["Handoff"])
        self.assertEqual(tree_city["meeting_count"], payload["count"])
        self.assertEqual(tree_city["broadcast_count"], 2)
        self.assertEqual(cached_status, 200)
        self.assertIsNotNone(cached_notebook["approved_at"])
        self.assertIn("episode_tagline", cached_notebook["outputs"])
        self.assertEqual(filtered_status, 200)
        self.assertTrue(filtered_notebook["success"])
        self.assertIsNone(filtered_notebook["approved_at"])


class TestLocalHQStatus(_TempHome):
    def setUp(self):
        super().setUp()
        from zspan_cli import serve

        with serve._PROCESS_LOCK:
            self._saved_process_state = dict(serve._PROCESS_STATE)
            serve._PROCESS_STATE.clear()
            serve._PROCESS_STATE.update(
                meeting_id=None, lines=[], running=False, done=False,
                ok=None, error=None, engine=None,
                run_started_monotonic=None,
            )
        with serve._ACTIVITY_LOCK:
            self._saved_activity = list(serve._RECENT_ACTIVITY)
            serve._RECENT_ACTIVITY.clear()
        self._saved_flagship_cache = serve._FLAGSHIP_LINK_CACHE

    def tearDown(self):
        from zspan_cli import serve

        with serve._PROCESS_LOCK:
            serve._PROCESS_STATE.clear()
            serve._PROCESS_STATE.update(self._saved_process_state)
        with serve._ACTIVITY_LOCK:
            serve._RECENT_ACTIVITY.clear()
            serve._RECENT_ACTIVITY.extend(self._saved_activity)
        serve._FLAGSHIP_LINK_CACHE = self._saved_flagship_cache
        super().tearDown()

    def _activity(self, monotonic_ts, kind, label, detail=""):
        from zspan_cli import serve

        event = {
            "ts": f"2026-07-16T00:00:0{int(monotonic_ts) % 10}Z",
            "kind": kind,
            "label": label,
            "detail": detail,
        }
        with serve._ACTIVITY_LOCK:
            serve._RECENT_ACTIVITY.append((monotonic_ts, event))

    def _payload(self, *, flagship="up", codex=False):
        from zspan_cli import serve

        conn = workspace.connect()
        try:
            with mock.patch.object(
                serve, "_flagship_link_status", return_value=flagship,
            ), mock.patch(
                "zspan_cli.providers.codex_available", return_value=codex,
            ):
                return serve._hq_status(conn)
        finally:
            conn.close()

    def test_hq_route_carries_the_local_contract_only(self):
        from zspan_cli import serve

        captured = {}
        handler = serve._Handler.__new__(serve._Handler)
        handler._send_json = lambda status, body: captured.update(
            status=status, payload=body,
        )
        with mock.patch.object(
            serve, "_flagship_link_status", return_value="up",
        ), mock.patch(
            "zspan_cli.providers.codex_available", return_value=False,
        ):
            handler._route_api("/api/hq/status")

        self.assertEqual(captured["status"], 200)
        payload = captured["payload"]
        self.assertTrue({"building", "departments", "infrastructure", "funding"}
                        <= set(payload))
        expected = {
            "pipeline-operator": ("Pipeline Operator", "PIPELINE OPS"),
            "ingestion": ("Ingestion / Media", "INGEST"),
            "transcription": ("Whisper Transcription", "WHISPER"),
            "synthesis": ("Synthesis / RAG", "RAG"),
            "verification": ("Grounding Gate", "VERIFY"),
        }
        departments = {department["id"]: department
                       for department in payload["departments"]}
        self.assertEqual(set(departments), set(expected))
        for department_id, (name, short) in expected.items():
            self.assertEqual(
                (departments[department_id]["name"],
                 departments[department_id]["short"]),
                (name, short),
            )
        self.assertTrue({
            "vocabulary-curator", "disputed-quotes-reviewer",
            "content-scout", "parser-custodian",
        }.isdisjoint(departments))
        self.assertIsNone(payload["funding"]["lastUpdated"])
        self.assertNotIn("billboards", payload)

    def test_newest_run_stage_wins_and_idle_clears_agents(self):
        from zspan_cli import serve

        with serve._PROCESS_LOCK:
            serve._PROCESS_STATE.update(
                running=True, run_started_monotonic=100.0, engine="gemini",
            )
        self._activity(101.0, "transcription", "heard 00:00–00:30", "words")
        self._activity(102.0, "synthesis", "synopsis via gemini", "chunks 1")

        payload = self._payload()
        departments = {department["id"]: department
                       for department in payload["departments"]}
        self.assertEqual(departments["synthesis"]["state"], "running")
        self.assertEqual(departments["transcription"]["state"], "idle")
        self.assertEqual(
            departments["transcription"]["recentSummary"],
            "Last: heard 00:00–00:30",
        )
        self.assertEqual(departments["pipeline-operator"]["state"], "running")

        with serve._PROCESS_LOCK:
            serve._PROCESS_STATE["running"] = False
        idle_payload = self._payload()
        for department in idle_payload["departments"]:
            self.assertEqual(department["state"], "idle")
            self.assertEqual(department["agents"], [])

    def test_pre_run_activity_never_becomes_current(self):
        from zspan_cli import serve

        self._activity(90.0, "transcription", "old transcription", "old words")
        with serve._PROCESS_LOCK:
            serve._PROCESS_STATE.update(
                running=True, run_started_monotonic=100.0, engine="gemini",
            )
        payload = self._payload()
        transcription = next(
            department for department in payload["departments"]
            if department["id"] == "transcription"
        )
        self.assertEqual(transcription["state"], "idle")
        self.assertIsNone(transcription["currentObjective"])
        self.assertEqual(transcription["agents"], [])
        self.assertNotIn(
            "old transcription",
            [department["currentObjective"]
             for department in payload["departments"]],
        )

    def test_synthesis_agent_uses_the_stored_run_engine(self):
        from zspan_cli import serve

        self._activity(101.0, "synthesis", "building synopsis", "chunks 1")
        with serve._PROCESS_LOCK:
            serve._PROCESS_STATE.update(
                running=True, run_started_monotonic=100.0, engine="Codex",
            )
        payload = self._payload()
        synthesis = next(department for department in payload["departments"]
                         if department["id"] == "synthesis")
        self.assertEqual(synthesis["agents"][0]["model"], "Codex")

        with serve._PROCESS_LOCK:
            serve._PROCESS_STATE["engine"] = None
        payload = self._payload()
        synthesis = next(department for department in payload["departments"]
                         if department["id"] == "synthesis")
        self.assertEqual(synthesis["agents"][0]["model"], "Engine")

    def test_infrastructure_and_overall_status_follow_real_capabilities(self):
        down_payload = self._payload(flagship="down", codex=False)
        down_services = {service["id"]: service
                         for service in down_payload["infrastructure"]["services"]}
        self.assertEqual(down_services["flagship"]["status"], "down")
        self.assertEqual(down_payload["building"]["overallStatus"], "degraded")

        config.save_config({"api_keys": {"gemini": "gemini-test-key"}})
        up_payload = self._payload(flagship="up", codex=False)
        up_services = {service["id"]: service
                       for service in up_payload["infrastructure"]["services"]}
        self.assertEqual(up_services["engine"]["status"], "up")
        self.assertEqual(up_payload["building"]["overallStatus"], "operational")

    def test_hq_status_poll_is_activity_exempt(self):
        from zspan_cli import serve

        self.assertTrue(serve._is_activity_exempt("/api/hq/status"))


class TestActivityFeed(_TempHome):
    """The HQ skybox's local feed: pipeline steps + requests become
    fiber-optic star events; the SSE endpoint streams them; watcher
    heartbeats stay out of the sky."""

    MEETING_ID = 7

    def _seed(self):
        config.save_config({
            "synthesis_provider": "gemini",
            "api_keys": {"gemini": "AIzaFakeKeyForTests000000"},
            "local_processing_ack": {"version": config.PROCESSING_ACK_VERSION,
                                     "accepted_at": "2026-07-13T00:00:00Z"},
            "auth": {
                "token": "cli-test-token",
                "email": "person@example.com",
                "display_name": "Test Person",
                "expires_at": "2026-10-01T00:00:00Z",
            },
        })
        conn = workspace.connect()
        workspace.upsert_meeting(
            conn, _event(self.MEETING_ID, "Council", "2026-07-01",
                         video="https://www.youtube.com/watch?v=abc")
        )
        self.MEETING_ID = conn.execute(
            "SELECT id FROM meetings WHERE flagship_row_id = ?", (self.MEETING_ID,)
        ).fetchone()["id"]
        conn.commit()
        conn.close()
        tdir = Path(self._tmp.name) / "transcripts"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{self.MEETING_ID}.json").write_text(json.dumps({
            "words": _VOTE_TRANSCRIPT, "duration_seconds": 31.0, "language": "en",
        }), encoding="utf-8")

    def _fake_embed_patches(self):
        import numpy as np

        def fake_texts(texts, progress=lambda _m: None):
            out = np.zeros((len(texts), pipeline.VECTOR_DIM), dtype=np.float32)
            for i in range(len(texts)):
                out[i, i % pipeline.VECTOR_DIM] = 1.0
            return out

        def fake_query(_query):
            v = np.zeros(pipeline.VECTOR_DIM, dtype=np.float32)
            v[0] = 1.0
            return v

        return (
            mock.patch.object(pipeline, "load_token_counter",
                              return_value=(lambda _w: 1, True)),
            mock.patch.object(pipeline, "embed_texts", side_effect=fake_texts),
            mock.patch.object(pipeline, "embed_query", side_effect=fake_query),
        )

    def _run_pipeline_with_approval(self, approval_fn, synth):
        from zspan_cli import processing

        p1, p2, p3 = self._fake_embed_patches()
        with p1, p2, p3, mock.patch.object(
            synthesize, "synthesize", synth,
        ), mock.patch.object(processing, "ensure_watchable_video"), \
             mock.patch.object(processing, "fetch_cli_me", return_value={
                 "account": {"email": "person@example.com"}
             }), mock.patch.object(processing, "register_generation", return_value={
                 "generation_public_id": "g_1234567890AbCdEfGhIjKl",
                 "ribbon_token": "ABCDEFG2",
             }), mock.patch.object(
                 processing, "submit_private_contribution",
                 side_effect=_accepted_private_contribution,
             ):
            return processing.run_pipeline(
                self.MEETING_ID,
                config=config.load_config(),
                progress=lambda _m: None,
                approval_fn=approval_fn,
            )

    def test_pipeline_default_requires_injected_approval(self):
        from zspan_cli import approval

        self._seed()
        approve = mock.Mock(return_value=approval.ApprovalDecision.ABORT_ALL)
        synth = mock.Mock(return_value="must not be sent")
        with mock.patch.dict(
            os.environ, {approval.YES_TO_ALL_ENV_VAR: ""}, clear=False,
        ):
            result = self._run_pipeline_with_approval(approve, synth)

        self.assertTrue(result["aborted_by_operator"])
        approve.assert_called_once()
        synth.assert_not_called()

    def test_pipeline_env_opt_out_bypasses_injected_approval(self):
        from zspan_cli import approval

        self._seed()
        approve = mock.Mock(side_effect=AssertionError("approval must be bypassed"))
        synth = mock.Mock(
            return_value="The council discussed the water contract."
        )
        with mock.patch.dict(
            os.environ, {approval.YES_TO_ALL_ENV_VAR: "1"}, clear=False,
        ):
            result = self._run_pipeline_with_approval(approve, synth)

        self.assertTrue(result["ok"])
        approve.assert_not_called()
        self.assertEqual(synth.call_count, len(synthesize.RENDERED_OUTPUT_TYPES))

    def test_pipeline_publishes_structured_activity(self):
        from zspan_cli import processing

        self._seed()
        events: list = []

        def collect(kind, label, detail="", status=200):
            events.append((kind, label, detail, status))

        p1, p2, p3 = self._fake_embed_patches()
        with p1, p2, p3, mock.patch.object(
            synthesize, "synthesize",
            return_value="The council discussed the water contract.",
        ), mock.patch.object(processing, "ensure_watchable_video"), \
             mock.patch.object(processing, "fetch_cli_me", return_value={
                 "account": {"email": "person@example.com"}
             }), mock.patch.object(processing, "register_generation", return_value={
                 "generation_public_id": "g_1234567890AbCdEfGhIjKl",
                 "ribbon_token": "ABCDEFG2",
             }), mock.patch.object(
                 processing, "submit_private_contribution",
                 side_effect=_accepted_private_contribution,
             ):
            result = processing.run_pipeline(
                self.MEETING_ID,
                config=config.load_config(),
                progress=lambda _m: None,
                activity=collect,
            )
        self.assertTrue(result["ok"])
        kinds = [e[0] for e in events]
        # The arc: pipeline open → transcript reuse → index → per-output
        # retrieval+synthesis+gate → pipeline close. Clean run = all 200.
        self.assertEqual(kinds[0], "pipeline")
        self.assertIn("transcription", kinds)
        self.assertIn("index", kinds)
        self.assertEqual(kinds.count("retrieval"),
                         len(synthesize.RENDERED_OUTPUT_TYPES))
        self.assertEqual(kinds.count("synthesis"),
                         len(synthesize.RENDERED_OUTPUT_TYPES))
        self.assertEqual(kinds.count("gate"),
                         len(synthesize.RENDERED_OUTPUT_TYPES))
        self.assertEqual(kinds[-1], "pipeline")
        self.assertTrue(all(e[3] == 200 for e in events))
        # The synthesis star carries the retrieved-chunk receipts.
        synth_evt = next(e for e in events if e[0] == "synthesis")
        self.assertIn("chunks", synth_evt[2])

    def test_no_public_id_skips_registration_once_and_keeps_legacy_pending(self):
        from zspan_cli import processing

        self._seed()
        conn = workspace.connect()
        conn.execute(
            "UPDATE meetings SET public_id = NULL WHERE id = ?",
            (self.MEETING_ID,),
        )
        workspace.save_output(
            conn,
            self.MEETING_ID,
            "synopsis",
            content="legacy synopsis",
            provider="gemini",
            model="gemini-test",
            gate_status="ok",
            gate_log="{}",
        )
        conn.execute(
            """UPDATE outputs
               SET registration_state = NULL,
                   registration_idempotency_key = NULL,
                   registered_account = NULL
               WHERE meeting_id = ? AND output_type = 'synopsis'""",
            (self.MEETING_ID,),
        )
        conn.commit()
        conn.close()

        lines: list[str] = []
        p1, p2, p3 = self._fake_embed_patches()
        with p1, p2, p3, mock.patch.object(
            synthesize,
            "synthesize",
            return_value="The council discussed the water contract.",
        ), mock.patch.object(processing, "ensure_watchable_video"), \
             mock.patch.object(processing, "fetch_cli_me", return_value={
                 "account": {"email": "person@example.com"}
             }), mock.patch.object(processing, "register_generation") as register, \
             mock.patch.object(
                 processing, "submit_private_contribution",
                 side_effect=_accepted_private_contribution,
             ) as submit:
            result = processing.run_pipeline(
                self.MEETING_ID,
                config=config.load_config(),
                progress=lines.append,
            )

        notice = (
            "this meeting has no catalog id in your workspace (pulled before "
            "the catalog contract) — registration will engage once the "
            "workspace row carries one."
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["contribution_state"], "pending")
        self.assertEqual(lines.count(notice), 1)
        register.assert_not_called()
        submit.assert_not_called()

        conn = workspace.connect()
        legacy = conn.execute(
            """SELECT registration_state, registration_idempotency_key,
                      registered_account
               FROM outputs
               WHERE meeting_id = ? AND output_type = 'synopsis'""",
            (self.MEETING_ID,),
        ).fetchone()
        conn.close()
        self.assertEqual(legacy["registration_state"], "pending")
        self.assertTrue(legacy["registration_idempotency_key"])
        self.assertEqual(legacy["registered_account"], "person@example.com")

    def test_gate_findings_publish_normal_observation_events(self):
        from zspan_cli import processing

        self._seed()
        events: list = []

        def collect(kind, label, detail="", status=200):
            events.append((kind, label, detail, status))

        p1, p2, p3 = self._fake_embed_patches()
        with p1, p2, p3, mock.patch.object(
            synthesize, "synthesize",
            return_value="1. Approved resolution R-99 with a vote of 5-2.",
        ), mock.patch.object(processing, "ensure_watchable_video"), \
             mock.patch.object(processing, "fetch_cli_me", return_value={
                 "account": {"email": "person@example.com"}
             }), mock.patch.object(processing, "register_generation", return_value={
                 "generation_public_id": "g_1234567890AbCdEfGhIjKl",
                 "ribbon_token": "ABCDEFG2",
             }), mock.patch.object(
                 processing, "submit_private_contribution",
                 side_effect=_accepted_private_contribution,
             ):
            processing.run_pipeline(
                self.MEETING_ID,
                config=config.load_config(),
                progress=lambda _m: None,
                activity=collect,
                force=True,
            )
        observed = [
            e for e in events
            if e[0] == "gate" and "observed_findings" in e[1]
        ]
        self.assertTrue(observed, "determinate findings must stay in the audit trail")
        self.assertTrue(all(e[3] == 200 for e in observed))
        self.assertTrue(any("R-99" in e[2] or "r-99" in e[2]
                            for e in observed))
        conn = workspace.connect()
        rows = conn.execute(
            "SELECT content FROM outputs WHERE meeting_id = ?",
            (self.MEETING_ID,),
        ).fetchall()
        conn.close()
        self.assertTrue(rows)
        self.assertTrue(all("R-99" in row["content"] for row in rows))

    def test_sse_stream_request_stars_and_exemptions(self):
        import socket
        import time
        from urllib.request import Request, urlopen

        import numpy as np

        from zspan_cli import pipeline as pl
        from zspan_cli import serve

        self._seed()
        conn = workspace.connect()
        chunks = [pl.Chunk(chunk_index=0, text="the water contract vote",
                           start_seconds=0.0, end_seconds=8.0)]
        vec = np.zeros((1, pl.VECTOR_DIM), dtype=np.float32)
        vec[0, 0] = 1.0
        workspace.replace_chunks(conn, self.MEETING_ID, chunks, vec)
        conn.close()

        server = serve.start_server(port=0)
        port = server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            # Loopback Host — the local-server guard (serve._guard_ok)
            # rejects any non-loopback Host as a DNS-rebinding attempt, so
            # the test client must send what a real browser sends.
            sock.sendall(
                f"GET /api/hq/traffic-events HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Accept: text/event-stream\r\n\r\n".encode()
            )
            time.sleep(0.3)

            base = f"http://127.0.0.1:{port}"
            urlopen(f"{base}/api/channels/tree").read()          # → star
            urlopen(f"{base}/api/local/process/7/status").read()  # exempt
            urlopen(f"{base}/api/local/process/active").read()    # exempt
            qvec = np.zeros(pl.VECTOR_DIM, dtype=np.float32)
            qvec[0] = 1.0
            with mock.patch.object(pl, "embed_query", return_value=qvec):
                req = Request(
                    f"{base}/api/rag-search/{self.MEETING_ID}",
                    data=json.dumps({"query": "what about the ponds?"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urlopen(req).read()                              # → librarian star

            time.sleep(0.3)
            sock.settimeout(2)
            buf = b""
            try:
                while b"Librarian searched" not in buf:
                    buf += sock.recv(65536)
            except socket.timeout:
                pass
            lines = buf.decode("utf-8", "replace").splitlines()
            events = [json.loads(l[5:]) for l in lines if l.startswith("data:")]

            kinds = [(e["kind"], e["label"]) for e in events]
            self.assertIn(("watcher", "a watcher connected to the sky"), kinds)
            self.assertIn(("request", "the channels tree"), kinds)
            self.assertIn(("librarian", "Librarian searched the record"), kinds)
            # The poll endpoints never reach the sky.
            self.assertFalse(any("process" in e.get("detail", "")
                                 and e["kind"] == "request" for e in events))
            librarian = next(e for e in events
                             if e["kind"] == "librarian")
            self.assertEqual(librarian["detail"], "what about the ponds?")
            self.assertEqual(librarian["source"], "local")

            # The active-process poll answers honestly while idle.
            with urlopen(f"{base}/api/local/process/active") as r:
                active = json.load(r)
            self.assertFalse(active["active"])
        finally:
            sock.close()
            server.shutdown()

    def test_loopback_guard_blocks_rebinding_host_and_cross_origin_post(self):
        """The local-server-plus-browser defense: a DNS-rebinding GET
        (foreign Host) and a cross-origin mutating POST (foreign Origin)
        are both refused with 403, while a legit loopback request still
        works. This is the wall that stops a malicious web page from
        spending the user's key or reading the workspace."""
        import http.client

        from zspan_cli import serve

        self._seed()
        server = serve.start_server(port=0)
        port = server.server_address[1]
        try:
            # DNS-rebinding: browser still sends the attacker's hostname.
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/channels/tree", headers={"Host": "evil.com"})
            self.assertEqual(c.getresponse().status, 403)
            c.close()

            # Cross-origin mutating POST → refused before it can spend a key.
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(
                "POST", f"/api/local/process/{self.MEETING_ID}",
                body=json.dumps({"mode": "local"}),
                headers={"Host": f"127.0.0.1:{port}",
                         "Origin": "https://evil.com",
                         "Content-Type": "application/json"},
            )
            self.assertEqual(c.getresponse().status, 403)
            c.close()

            # A legit loopback GET (Host set by the client) still works.
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/system/status")
            self.assertEqual(c.getresponse().status, 200)
            c.close()
        finally:
            server.shutdown()

    def test_process_active_reports_running_meeting(self):
        from urllib.request import urlopen

        from zspan_cli import serve

        self._seed()
        server = serve.start_server(port=0)
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            serve._PROCESS_STATE.update(
                meeting_id=self.MEETING_ID, running=True, done=False, ok=None,
            )
            with urlopen(f"{base}/api/local/process/active") as r:
                active = json.load(r)
            self.assertTrue(active["active"])
            self.assertEqual(active["meeting_id"], self.MEETING_ID)
            self.assertEqual(active["city"], "Kingman")
        finally:
            serve._PROCESS_STATE.update(
                meeting_id=None, running=False, done=False, ok=None,
            )
            server.shutdown()


class TestBundle(_TempHome):
    """The release-bundle fetcher (CLI-5, Q4=pip): pinned-hash verify,
    zip-traversal guard, honest 404 for the not-yet-public release, and
    the webapp resolution order gaining ~/.zspan/webapp."""

    def _fake_response(self, payload: bytes, status: int = 200):
        class _Resp:
            status_code = status

            def iter_content(self, chunk_size=1):
                for i in range(0, len(payload), chunk_size):
                    yield payload[i:i + chunk_size]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    def _zip_bytes(self, files: dict) -> bytes:
        import io
        import zipfile as zf_mod
        buf = io.BytesIO()
        with zf_mod.ZipFile(buf, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return buf.getvalue()

    def test_fetch_verifies_hash_and_unpacks(self):
        import hashlib

        from zspan_cli import bundle

        payload = self._zip_bytes({"index.html": "<!doctype html>ok",
                                   "assets/app.js": "1"})
        good = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(bundle, "BUNDLE_SHA256", good), \
             mock.patch.object(bundle.requests, "get",
                               return_value=self._fake_response(payload)):
            dest = bundle.fetch_bundle(say=lambda _m: None)
        self.assertTrue((dest / "index.html").is_file())
        self.assertEqual(dest, bundle.webapp_install_dir())

        # ...and the serve resolution now finds it (last candidate).
        from zspan_cli import serve
        resolved = serve.resolve_webapp_dir()
        self.assertIsNotNone(resolved)

    def test_hash_mismatch_refuses_loudly(self):
        from zspan_cli import bundle

        payload = self._zip_bytes({"index.html": "x"})
        with mock.patch.object(bundle, "BUNDLE_SHA256", "0" * 64), \
             mock.patch.object(bundle.requests, "get",
                               return_value=self._fake_response(payload)):
            with self.assertRaises(bundle.BundleError) as ctx:
                bundle.fetch_bundle(say=lambda _m: None)
        self.assertIn("SHA256", str(ctx.exception))
        self.assertFalse(bundle.webapp_install_dir().exists())

    def test_traversal_member_refuses(self):
        import hashlib

        from zspan_cli import bundle

        payload = self._zip_bytes({"../escape.html": "nope",
                                   "index.html": "x"})
        good = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(bundle, "BUNDLE_SHA256", good), \
             mock.patch.object(bundle.requests, "get",
                               return_value=self._fake_response(payload)):
            with self.assertRaises(bundle.BundleError) as ctx:
                bundle.fetch_bundle(say=lambda _m: None)
        self.assertIn("unsafe path", str(ctx.exception))

    def test_private_release_404_is_named_plainly(self):
        from zspan_cli import bundle

        with mock.patch.object(bundle.requests, "get",
                               return_value=self._fake_response(b"", 404)):
            with self.assertRaises(bundle.BundleError) as ctx:
                bundle.fetch_bundle(say=lambda _m: None)
        self.assertIn("isn't public yet", str(ctx.exception))

    def test_pinned_constants_shape(self):
        import re

        from zspan_cli import bundle

        self.assertTrue(bundle.BUNDLE_URL.startswith(
            "https://github.com/anitacigawet/Z-SPAN/releases/download/"))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", bundle.BUNDLE_SHA256))

    def test_sibling_prefix_traversal_refuses(self):
        """The str-prefix guard let `unpacked-evil/x` masquerade as inside
        `unpacked/`. is_relative_to catches it (sol pen-test Finding #13)."""
        import hashlib
        import io
        import os
        import zipfile as zf_mod

        from zspan_cli import bundle

        # Build a payload whose SOLE member resolves outside `unpack/`
        # via a sibling-prefix directory. We can't construct that with
        # writestr's default relative names — construct the ZipInfo
        # manually so its resolved path lands in tmp_root/unpacked-evil.
        buf = io.BytesIO()
        with zf_mod.ZipFile(buf, "w") as zf:
            # A relative path that, when joined to unpack/, resolves via
            # ../ to a sibling — the exact shape the old str-prefix check
            # accepted because `unpacked-evil` starts with `unpacked`.
            zf.writestr("../unpacked-evil/pwn.html", "x")
        payload = buf.getvalue()
        good = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(bundle, "BUNDLE_SHA256", good), \
             mock.patch.object(bundle.requests, "get",
                               return_value=self._fake_response(payload)):
            with self.assertRaises(bundle.BundleError) as ctx:
                bundle.fetch_bundle(say=lambda _m: None)
        self.assertIn("unsafe path", str(ctx.exception))

    def test_member_count_cap_refuses(self):
        """Too many members refuses before extract (zip-bomb / DoS)."""
        import hashlib

        from zspan_cli import bundle

        # Craft a payload just over the cap; use tiny members to keep the
        # test cheap. Patch MAX_MEMBERS to 5 for the assertion.
        payload = self._zip_bytes({
            f"file_{i}.txt": "x" for i in range(10)
        })
        good = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(bundle, "BUNDLE_SHA256", good), \
             mock.patch.object(bundle, "MAX_MEMBERS", 5), \
             mock.patch.object(bundle.requests, "get",
                               return_value=self._fake_response(payload)):
            with self.assertRaises(bundle.BundleError) as ctx:
                bundle.fetch_bundle(say=lambda _m: None)
        self.assertIn("more than the", str(ctx.exception))

    def test_compression_ratio_cap_refuses(self):
        """A file that compresses too aggressively is a zip-bomb signal."""
        import hashlib
        import io
        import zipfile as zf_mod

        from zspan_cli import bundle

        # 200KB of zeros with DEFLATE — compresses to a few hundred bytes,
        # ratio ~1000:1.
        buf = io.BytesIO()
        with zf_mod.ZipFile(buf, "w", zf_mod.ZIP_DEFLATED) as zf:
            zf.writestr("index.html", "<!doctype html>ok")
            zf.writestr("bomb.bin", "\0" * (200 * 1024))
        payload = buf.getvalue()
        good = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(bundle, "BUNDLE_SHA256", good), \
             mock.patch.object(bundle.requests, "get",
                               return_value=self._fake_response(payload)):
            with self.assertRaises(bundle.BundleError) as ctx:
                bundle.fetch_bundle(say=lambda _m: None)
        self.assertIn("ratio", str(ctx.exception).lower())

    def test_symlink_member_refuses(self):
        """A zip member with S_IFLNK in external_attr is rejected."""
        import hashlib
        import io
        import stat as _stat
        import zipfile as zf_mod

        from zspan_cli import bundle

        buf = io.BytesIO()
        with zf_mod.ZipFile(buf, "w") as zf:
            info = zf_mod.ZipInfo("link.txt")
            # Symlink mode in external_attr (upper 16 bits).
            info.external_attr = (_stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
            zf.writestr("index.html", "x")
        payload = buf.getvalue()
        good = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(bundle, "BUNDLE_SHA256", good), \
             mock.patch.object(bundle.requests, "get",
                               return_value=self._fake_response(payload)):
            with self.assertRaises(bundle.BundleError) as ctx:
                bundle.fetch_bundle(say=lambda _m: None)
        self.assertIn("non-regular entry", str(ctx.exception))


class TestMediaSSRFGuard(unittest.TestCase):
    """SEC-INPUT-2 / RR-8 — assert_safe_media_url rejects non-web schemes,
    embedded credentials, and hosts resolving to internal addresses, before
    any URL reaches yt-dlp. Network stubbed via a socket.getaddrinfo mock."""

    @staticmethod
    def _addrinfo(*ips):
        # getaddrinfo 5-tuple: (family, type, proto, canonname, sockaddr)
        return [(0, 0, 0, "", (ip, 0)) for ip in ips]

    def test_rejects_file_and_non_http_schemes(self):
        from zspan_cli import media
        for u in ("file:///etc/passwd", "data:text/plain,hi",
                  "ftp://example.com/x.mp4", "gopher://x/"):
            with self.assertRaises(media.MediaError):
                media.assert_safe_media_url(u)

    def test_rejects_embedded_credentials(self):
        from zspan_cli import media
        with mock.patch("socket.getaddrinfo",
                        return_value=self._addrinfo("93.184.216.34")):
            with self.assertRaises(media.MediaError):
                media.assert_safe_media_url("https://user:pass@example.com/x.mp4")

    def test_rejects_internal_targets(self):
        from zspan_cli import media
        for ip, u in (
            ("127.0.0.1", "http://127.0.0.1/x.mp4"),
            ("169.254.169.254", "http://169.254.169.254/latest/meta-data/"),
            ("10.0.0.5", "https://looks-public.example.com/x.mp4"),
            ("192.168.1.10", "https://lan.example.com/x.mp4"),
            ("::1", "http://[::1]/x.mp4"),
        ):
            with mock.patch("socket.getaddrinfo",
                            return_value=self._addrinfo(ip)):
                with self.assertRaises(media.MediaError):
                    media.assert_safe_media_url(u)

    def test_rejects_mixed_public_and_private(self):
        from zspan_cli import media
        with mock.patch("socket.getaddrinfo",
                        return_value=self._addrinfo("93.184.216.34", "192.168.1.10")):
            with self.assertRaises(media.MediaError):
                media.assert_safe_media_url("https://mixed.example.com/x.mp4")

    def test_unresolvable_host_refused(self):
        import socket as _socket
        from zspan_cli import media
        with mock.patch("socket.getaddrinfo",
                        side_effect=_socket.gaierror("no such host")):
            with self.assertRaises(media.MediaError):
                media.assert_safe_media_url("https://nonexistent.invalid/x.mp4")

    def test_allows_public_https_and_youtube(self):
        from zspan_cli import media
        with mock.patch("socket.getaddrinfo",
                        return_value=self._addrinfo("93.184.216.34")):
            media.assert_safe_media_url("https://example.com/video.mp4")
        with mock.patch("socket.getaddrinfo",
                        return_value=self._addrinfo("142.250.72.14")):
            media.assert_safe_media_url("https://www.youtube.com/watch?v=abc123")

    def test_download_context_pins_first_dns_answer(self):
        import socket
        from zspan_cli import media

        calls = 0

        def resolver(host, *args, **kwargs):
            nonlocal calls
            self.assertEqual(host, "www.youtube.com")
            calls += 1
            ip = "142.250.72.14" if calls == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

        with mock.patch("socket.getaddrinfo", side_effect=resolver):
            with media.pinned_media_dns(
                "https://www.youtube.com/watch?v=abc123"
            ):
                first = socket.getaddrinfo("www.youtube.com", 443)
                second = socket.getaddrinfo("www.youtube.com", 443)

        self.assertEqual(calls, 1)
        self.assertEqual(first[0][4][0], "142.250.72.14")
        self.assertEqual(second[0][4][0], "142.250.72.14")


class TestMediaRedirectResolver(unittest.TestCase):
    """DIV-011 — resolve_media_redirects_safely re-applies the SSRF guard to
    EVERY redirect hop before yt-dlp sees a URL, so a public catalog URL that
    30x-redirects to a private target (localhost / LAN / cloud metadata) is
    refused. Network stubbed via socket.getaddrinfo + requests.request mocks."""

    @staticmethod
    def _gai(mapping):
        import socket

        def _fn(host, *a, **k):
            ip = mapping.get(host)
            if ip is None:
                raise socket.gaierror("no such host")
            return [(0, 0, 0, "", (ip, 0))]
        return _fn

    class _Resp:
        def __init__(self, status, location=None):
            self.status_code = status
            self.is_redirect = status in (301, 302, 303, 307, 308) and bool(location)
            self.headers = {"Location": location} if location else {}

        def close(self):
            pass

    @classmethod
    def _req(cls, hops):
        def _fn(method, url, **k):
            status, location = hops[url]
            return cls._Resp(status, location)
        return _fn

    def test_redirect_to_private_target_refused(self):
        from zspan_cli import media
        gai = self._gai({
            "pub.example.com": "93.184.216.34",
            "internal.example.com": "10.0.0.5",
        })
        hops = {
            "https://pub.example.com/x.mp4":
                (302, "https://internal.example.com/x.mp4"),
        }
        with mock.patch("socket.getaddrinfo", side_effect=gai), \
             mock.patch("requests.request", side_effect=self._req(hops)):
            with self.assertRaises(media.MediaError):
                media.resolve_media_redirects_safely("https://pub.example.com/x.mp4")

    def test_redirect_to_credentialed_target_refused(self):
        from zspan_cli import media
        gai = self._gai({"pub.example.com": "93.184.216.34"})
        hops = {
            "https://pub.example.com/x.mp4":
                (302, "https://user:pass@evil.example.com/x.mp4"),
        }
        with mock.patch("socket.getaddrinfo", side_effect=gai), \
             mock.patch("requests.request", side_effect=self._req(hops)):
            with self.assertRaises(media.MediaError):
                media.resolve_media_redirects_safely("https://pub.example.com/x.mp4")

    def test_too_many_redirects_refused(self):
        from zspan_cli import media
        gai = self._gai({f"h{i}.example.com": "93.184.216.34" for i in range(12)})
        hops = {
            f"https://h{i}.example.com/x.mp4":
                (302, f"https://h{i + 1}.example.com/x.mp4")
            for i in range(12)
        }
        with mock.patch("socket.getaddrinfo", side_effect=gai), \
             mock.patch("requests.request", side_effect=self._req(hops)):
            with self.assertRaises(media.MediaError):
                media.resolve_media_redirects_safely("https://h0.example.com/x.mp4")

    def test_public_redirect_resolves_to_terminal(self):
        from zspan_cli import media
        gai = self._gai({
            "pub.example.com": "93.184.216.34",
            "cdn.example.com": "151.101.0.1",
        })
        hops = {
            "https://pub.example.com/x.mp4":
                (302, "https://cdn.example.com/final.mp4"),
            "https://cdn.example.com/final.mp4": (200, None),
        }
        with mock.patch("socket.getaddrinfo", side_effect=gai), \
             mock.patch("requests.request", side_effect=self._req(hops)):
            terminal = media.resolve_media_redirects_safely(
                "https://pub.example.com/x.mp4")
        self.assertEqual(terminal, "https://cdn.example.com/final.mp4")

    def test_no_redirect_passes_through(self):
        from zspan_cli import media
        gai = self._gai({"pub.example.com": "93.184.216.34"})
        hops = {"https://pub.example.com/x.mp4": (200, None)}
        with mock.patch("socket.getaddrinfo", side_effect=gai), \
             mock.patch("requests.request", side_effect=self._req(hops)):
            terminal = media.resolve_media_redirects_safely(
                "https://pub.example.com/x.mp4")
        self.assertEqual(terminal, "https://pub.example.com/x.mp4")

    def test_safe_source_url_youtube_skips_redirect_resolution(self):
        from zspan_cli import media
        # YouTube: guard the URL but never follow redirects (trusted host;
        # its resolution isn't a plain redirect chain). requests.request must
        # not be touched — the AssertionError side-effect proves it.
        with mock.patch("socket.getaddrinfo",
                        side_effect=self._gai({"www.youtube.com": "142.250.72.14"})), \
             mock.patch("requests.request",
                        side_effect=AssertionError("youtube must not redirect-resolve")):
            out = media._safe_source_url(
                "https://www.youtube.com/watch?v=abc123", media.KIND_YOUTUBE)
        self.assertEqual(out, "https://www.youtube.com/watch?v=abc123")

    def test_safe_source_url_direct_media_resolves(self):
        from zspan_cli import media
        gai = self._gai({
            "pub.example.com": "93.184.216.34",
            "cdn.example.com": "151.101.0.1",
        })
        hops = {
            "https://pub.example.com/x.mp4": (302, "https://cdn.example.com/f.mp4"),
            "https://cdn.example.com/f.mp4": (200, None),
        }
        with mock.patch("socket.getaddrinfo", side_effect=gai), \
             mock.patch("requests.request", side_effect=self._req(hops)):
            out = media._safe_source_url(
                "https://pub.example.com/x.mp4", media.KIND_DIRECT_MEDIA)
        self.assertEqual(out, "https://cdn.example.com/f.mp4")


class TestMediaYoutubeClientRetry(unittest.TestCase):
    """YouTube gets singleton player-client attempts; direct media does not."""

    class _DownloadError(Exception):
        pass

    @staticmethod
    def _downloaders():
        return (media.download_audio, media.download_video)

    def _run_download(
        self,
        downloader,
        url,
        actions,
        before_download=None,
        dns_resolver=None,
    ):
        from types import SimpleNamespace

        attempts = []
        download_error = self._DownloadError

        class FakeYoutubeDL:
            def __init__(self, opts):
                self.opts = opts
                self.index = len(attempts)
                attempts.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def download(self, urls):
                if before_download is not None:
                    before_download(self.index, self.opts, urls)
                action = actions[self.index]
                if action == "403":
                    raise download_error("HTTP Error 403: Forbidden")
                if action == "error":
                    raise download_error("upstream source disappeared")
                output = Path(
                    self.opts["outtmpl"].replace("%(ext)s", "mp4")
                )
                output.write_bytes(b"media")

        fake_yt_dlp = SimpleNamespace(
            YoutubeDL=FakeYoutubeDL,
            utils=SimpleNamespace(DownloadError=download_error),
            version=SimpleNamespace(__version__="2099.1-test"),
        )
        dns_patch = (
            mock.patch("socket.getaddrinfo", side_effect=dns_resolver)
            if dns_resolver is not None
            else mock.patch.object(
                media, "pinned_media_dns", return_value=nullcontext()
            )
        )
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}), \
             mock.patch.object(media, "assert_safe_media_url"), \
             mock.patch.object(media, "_safe_source_url", return_value=url), \
             dns_patch, \
             mock.patch("shutil.which", return_value=None):
            dest_dir = Path(tmp)
            try:
                result = downloader(
                    url, dest_dir, 42, progress=lambda _message: None
                )
                return result, attempts, dest_dir
            except Exception as error:
                error.attempts = attempts
                error.dest_dir = dest_dir
                raise

    def test_extractor_args_are_youtube_only(self):
        youtube_url = "https://www.youtube.com/watch?v=abc123"
        direct_url = "https://media.example.com/meeting.mp4"
        for downloader in self._downloaders():
            with self.subTest(downloader=downloader.__name__, kind="youtube"):
                _result, attempts, _dest = self._run_download(
                    downloader, youtube_url, ["success"]
                )
                self.assertEqual(
                    attempts[0]["extractor_args"],
                    {"youtube": {"player_client": ["android_vr"]}},
                )
            with self.subTest(downloader=downloader.__name__, kind="direct"):
                _result, attempts, _dest = self._run_download(
                    downloader, direct_url, ["success"]
                )
                self.assertEqual(len(attempts), 1)
                self.assertNotIn("extractor_args", attempts[0])

    def test_youtube_403_retries_once_with_single_alternate_client(self):
        url = "https://youtu.be/abc123"
        for downloader in self._downloaders():
            with self.subTest(downloader=downloader.__name__):
                _result, attempts, _dest = self._run_download(
                    downloader, url, ["403", "success"]
                )
                self.assertEqual(len(attempts), 2)
                self.assertEqual(
                    attempts[0]["extractor_args"],
                    {"youtube": {"player_client": ["android_vr"]}},
                )
                self.assertEqual(
                    attempts[1]["extractor_args"],
                    {"youtube": {"player_client": ["web_safari"]}},
                )
                self.assertNotIn("continuedl", attempts[0])
                self.assertIs(attempts[1]["continuedl"], False)

    def test_selector_is_unchanged_across_youtube_attempts(self):
        url = "https://www.youtube.com/watch?v=abc123"
        for downloader in self._downloaders():
            with self.subTest(downloader=downloader.__name__):
                _result, attempts, _dest = self._run_download(
                    downloader, url, ["403", "success"]
                )
                self.assertEqual(attempts[0]["format"], attempts[1]["format"])
                if downloader is media.download_audio:
                    self.assertEqual(attempts[0]["format"], "worstaudio/worst")

    def test_non_403_does_not_retry(self):
        url = "https://www.youtube.com/watch?v=abc123"
        for downloader in self._downloaders():
            with self.subTest(downloader=downloader.__name__):
                with self.assertRaises(media.MediaError) as ctx:
                    self._run_download(downloader, url, ["error"])
                self.assertEqual(len(ctx.exception.attempts), 1)
                self.assertNotIn("alternate player-client", str(ctx.exception))

    def test_direct_media_403_does_not_retry_or_get_youtube_args(self):
        url = "https://media.example.com/meeting.mp4"
        for downloader in self._downloaders():
            with self.subTest(downloader=downloader.__name__):
                with self.assertRaises(media.MediaError) as ctx:
                    self._run_download(downloader, url, ["403"])
                self.assertEqual(len(ctx.exception.attempts), 1)
                self.assertNotIn("extractor_args", ctx.exception.attempts[0])

    def test_retry_exhaustion_names_three_causes_and_installed_version(self):
        url = "https://www.youtube.com/watch?v=abc123"
        for downloader in self._downloaders():
            with self.subTest(downloader=downloader.__name__):
                with self.assertRaises(media.MediaError) as ctx:
                    self._run_download(downloader, url, ["403", "403"])
                message = str(ctx.exception)
                self.assertEqual(len(ctx.exception.attempts), 2)
                self.assertIn("VPN or datacenter IP", message)
                self.assertIn("try without the VPN", message)
                self.assertIn("2099.1-test", message)
                self.assertIn("pip install -U yt-dlp", message)
                self.assertIn("JavaScript runtime", message)
                self.assertIn("Deno or Node", message)

    def test_partial_files_are_cleared_before_retry(self):
        url = "https://www.youtube.com/watch?v=abc123"
        for downloader in self._downloaders():
            observed = []

            def before_download(index, opts, _urls):
                output = Path(opts["outtmpl"].replace("%(ext)s", "webm"))
                partials = (
                    Path(str(output) + ".part"),
                    Path(str(output) + ".ytdl"),
                    Path(str(output) + ".part-Frag1"),
                )
                if index == 0:
                    for partial in partials:
                        partial.write_bytes(b"partial")
                else:
                    observed.append(
                        (all(not partial.exists() for partial in partials),
                         opts.get("continuedl"))
                    )

            with self.subTest(downloader=downloader.__name__):
                _result, attempts, _dest = self._run_download(
                    downloader, url, ["403", "success"], before_download
                )
                self.assertEqual(len(attempts), 2)
                self.assertEqual(observed, [(True, False)])

    def test_all_downloads_set_the_500_mib_declared_size_cap(self):
        url = "https://www.youtube.com/watch?v=abc123"
        for downloader in self._downloaders():
            with self.subTest(downloader=downloader.__name__):
                _result, attempts, _dest = self._run_download(
                    downloader, url, ["success"]
                )
                self.assertEqual(
                    attempts[0]["max_filesize"],
                    media.MEDIA_DOWNLOAD_MAX_BYTES,
                )

    def test_all_downloads_pin_dns_around_ytdlp(self):
        import socket

        url = "https://www.youtube.com/watch?v=abc123"
        for downloader in self._downloaders():
            calls = 0
            observed: list[str] = []

            def resolver(host, *args, **kwargs):
                nonlocal calls
                self.assertEqual(host, "www.youtube.com")
                calls += 1
                ip = "142.250.72.14" if calls == 1 else "127.0.0.1"
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

            def before_download(_index, _opts, _urls):
                for _ in range(2):
                    infos = socket.getaddrinfo("www.youtube.com", 443)
                    observed.append(infos[0][4][0])

            with self.subTest(downloader=downloader.__name__):
                self._run_download(
                    downloader,
                    url,
                    ["success"],
                    before_download,
                    dns_resolver=resolver,
                )
                self.assertEqual(calls, 1)
                self.assertEqual(
                    observed, ["142.250.72.14", "142.250.72.14"]
                )

    def test_progress_hook_aborts_and_cleans_unknown_length_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            current = dest_dir / "42.webm"
            partial = dest_dir / "42.webm.part"
            unrelated = dest_dir / "42.notes"
            current.write_bytes(b"oversized-current")
            partial.write_bytes(b"oversized-partial")
            unrelated.write_bytes(b"keep")

            hook = media._download_progress_hook(
                dest_dir, 42, lambda _message: None
            )
            with self.assertRaisesRegex(media.MediaError, "500 MiB"):
                hook({
                    "status": "downloading",
                    "downloaded_bytes": media.MEDIA_DOWNLOAD_MAX_BYTES + 1,
                    "filename": str(current),
                    "tmpfilename": str(partial),
                })

            self.assertFalse(current.exists())
            self.assertFalse(partial.exists())
            self.assertTrue(unrelated.exists())


class TestAuthGenerationPreflight(_TempHome):
    ACK = {
        "local_processing_ack": {
            "version": config.PROCESSING_ACK_VERSION,
            "accepted_at": "2026-07-13T00:00:00Z",
        }
    }

    def test_signed_out_sentence(self):
        from zspan_cli import processing

        with self.assertRaises(processing.PipelineSetupError) as ctx:
            processing.run_pipeline(1, config=self.ACK, progress=lambda _m: None)
        self.assertEqual(
            str(ctx.exception),
            "generation requires Google sign-in — run `zspan login` first.",
        )

    def test_expired_and_unreachable_sentences(self):
        from zspan_cli import processing

        cfg = {**self.ACK, "auth": {"token": "token", "email": "a@example.com"}}
        with mock.patch.object(
            processing,
            "fetch_cli_me",
            side_effect=flagship.FlagshipError("no", status=401),
        ):
            with self.assertRaises(processing.PipelineSetupError) as ctx:
                processing.run_pipeline(1, config=cfg, progress=lambda _m: None)
        self.assertEqual(
            str(ctx.exception), "your sign-in expired — run `zspan login` again."
        )

        lines = []
        with mock.patch.object(
            processing,
            "fetch_cli_me",
            side_effect=flagship.FlagshipError("offline"),
        ):
            with self.assertRaises(processing.PipelineSetupError) as ctx:
                processing.run_pipeline(1, config=cfg, progress=lines.append)
        self.assertIn("isn't in your workspace", str(ctx.exception))
        self.assertTrue(any("temporarily unreachable" in line for line in lines))


class TestGenerationRegistration(_TempHome):
    ACCOUNT = "person@example.com"

    def _seed(self):
        conn = workspace.connect()
        workspace.upsert_meeting(
            conn, _event(71, "Council", "2026-07-01", video="https://v")
        )
        meeting = conn.execute(
            "SELECT * FROM meetings WHERE flagship_row_id = 71"
        ).fetchone()
        workspace.save_output(
            conn,
            int(meeting["id"]),
            "synopsis",
            content="exact UTF-8 content ✓",
            provider="openai",
            model="gpt-test",
            gate_status="ok",
            gate_log="{}",
            registration_idempotency_key="idem_1234567890abcdef",
            registered_account=self.ACCOUNT,
        )
        output = conn.execute(
            "SELECT * FROM outputs WHERE meeting_id = ? AND output_type = 'synopsis'",
            (meeting["id"],),
        ).fetchone()
        return conn, meeting, output

    @staticmethod
    def _registered_response():
        return {
            "generation_public_id": "g_1234567890AbCdEfGhIjKl",
            "ribbon_token": "ABCDEFG2",
        }

    def test_success_updates_row_and_hashes_exact_saved_bytes(self):
        from zspan_cli import processing

        conn, meeting, output = self._seed()
        with mock.patch.object(
            processing, "register_generation", return_value=self._registered_response()
        ) as register:
            self.assertTrue(processing._register_cached_output(
                conn, meeting, output, base_url="https://zspan.org", bearer="token",
                account_email=self.ACCOUNT, progress=lambda _m: None,
            ))
        payload = register.call_args.args[1]
        import hashlib
        self.assertEqual(
            payload["content_sha256"],
            hashlib.sha256("exact UTF-8 content ✓".encode("utf-8")).hexdigest(),
        )
        row = conn.execute("SELECT * FROM outputs").fetchone()
        conn.close()
        self.assertEqual(row["registration_state"], "registered")
        self.assertEqual(row["ribbon_token"], "ABCDEFG2")

    def test_failure_stays_pending_then_retry_registers_same_key(self):
        from zspan_cli import processing

        conn, meeting, output = self._seed()
        lines = []
        with mock.patch.object(
            processing,
            "register_generation",
            side_effect=flagship.FlagshipError("offline"),
        ):
            self.assertFalse(processing._register_cached_output(
                conn, meeting, output, base_url="https://zspan.org", bearer="token",
                account_email=self.ACCOUNT, progress=lines.append,
            ))
        pending = conn.execute("SELECT * FROM outputs").fetchone()
        self.assertEqual(pending["registration_state"], "pending")
        self.assertEqual(pending["registration_idempotency_key"], "idem_1234567890abcdef")
        self.assertIn("cached but UNREGISTERED", "\n".join(lines))
        with mock.patch.object(
            processing, "register_generation", return_value=self._registered_response()
        ) as retry:
            self.assertTrue(processing._register_cached_output(
                conn, meeting, pending, base_url="https://zspan.org", bearer="token",
                account_email=self.ACCOUNT, progress=lambda _m: None,
            ))
        self.assertEqual(
            retry.call_args.args[1]["idempotency_key"], "idem_1234567890abcdef"
        )
        self.assertEqual(
            conn.execute("SELECT registration_state FROM outputs").fetchone()[0],
            "registered",
        )
        conn.close()

    def test_different_account_pending_is_skipped_without_reassignment(self):
        from zspan_cli import processing

        conn, meeting, output = self._seed()
        conn.execute(
            "UPDATE outputs SET registered_account = 'other@example.com'"
        )
        conn.commit()
        output = conn.execute("SELECT * FROM outputs").fetchone()
        lines = []
        with mock.patch.object(processing, "register_generation") as register:
            self.assertFalse(processing._register_cached_output(
                conn, meeting, output, base_url="https://zspan.org", bearer="token",
                account_email=self.ACCOUNT, progress=lines.append,
            ))
        register.assert_not_called()
        self.assertIn("was not reassigned", "\n".join(lines))
        self.assertEqual(
            conn.execute("SELECT registered_account FROM outputs").fetchone()[0],
            "other@example.com",
        )
        conn.close()

    def test_legacy_null_state_is_bound_and_backfilled(self):
        from zspan_cli import processing

        conn, meeting, _output = self._seed()
        conn.execute(
            "UPDATE outputs SET registration_state = NULL, "
            "registration_idempotency_key = NULL, registered_account = NULL"
        )
        conn.commit()
        legacy = workspace.rows_needing_registration(conn, int(meeting["id"]))[0]
        with mock.patch.object(
            processing, "register_generation", return_value=self._registered_response()
        ):
            self.assertTrue(processing._register_cached_output(
                conn, meeting, legacy, base_url="https://zspan.org", bearer="token",
                account_email=self.ACCOUNT, progress=lambda _m: None,
            ))
        row = conn.execute("SELECT * FROM outputs").fetchone()
        conn.close()
        self.assertEqual(row["registration_state"], "registered")
        self.assertEqual(row["registered_account"], self.ACCOUNT)
        self.assertTrue(row["registration_idempotency_key"])

    def test_fresh_save_clears_old_ribbon_and_mints_new_pending_key(self):
        conn, meeting, _output = self._seed()
        workspace.update_registration(
            conn,
            int(meeting["id"]),
            "synopsis",
            ribbon_token="ABCDEFG2",
            generation_public_id="g_1234567890AbCdEfGhIjKl",
            state="registered",
        )
        workspace.save_output(
            conn,
            int(meeting["id"]),
            "synopsis",
            content="forced replacement",
            provider="openai",
            model="gpt-test",
            gate_status="ok",
            gate_log="{}",
            registration_idempotency_key="fresh_1234567890abcdef",
            registered_account=self.ACCOUNT,
        )
        row = conn.execute("SELECT * FROM outputs").fetchone()
        conn.close()
        self.assertEqual(row["registration_state"], "pending")
        self.assertEqual(row["registration_idempotency_key"], "fresh_1234567890abcdef")
        self.assertIsNone(row["ribbon_token"])
        self.assertIsNone(row["generation_public_id"])


class TestNotebookRegistrationFields(_TempHome):
    def test_notebook_carries_ribbon_and_registration_state(self):
        from zspan_cli import serve

        conn = workspace.connect()
        workspace.upsert_meeting(conn, _event(81, "Council", "2026-07-01"))
        meeting = conn.execute(
            "SELECT * FROM meetings WHERE flagship_row_id = 81"
        ).fetchone()
        workspace.save_output(
            conn, int(meeting["id"]), "synopsis", content="content",
            provider="openai", model="gpt-test", gate_status="ok", gate_log="{}",
            registered_account="person@example.com",
        )
        workspace.update_registration(
            conn, int(meeting["id"]), "synopsis", ribbon_token="ABCDEFG2",
            generation_public_id="g_1234567890AbCdEfGhIjKl", state="registered",
        )
        payload, status = serve._notebook(conn, int(meeting["id"]))
        conn.close()
        self.assertEqual(status, 200)
        self.assertIs(payload["local_workspace"], True)
        self.assertEqual(payload["outputs"]["synopsis"]["gate_status"], "ok")
        self.assertEqual(payload["outputs"]["synopsis"]["ribbon_token"], "ABCDEFG2")
        self.assertEqual(
            payload["outputs"]["synopsis"]["registration_state"], "registered"
        )


class TestDecisionTranscriptEvidenceRender(unittest.TestCase):
    @staticmethod
    def _evidence(text="item introduced", **span_fields):
        return {
            "index": 1,
            "verbatim_spans": [{
                "text": text,
                "start_seconds": 1.0,
                "end_seconds": 2.0,
                "source": "item_quote_to_action_quote",
                "label": "Verbatim transcript excerpt — complete",
                "structure": "contiguous",
                **span_fields,
            }],
        }

    def test_key_decision_prose_is_non_seekable_and_locator_free(self):
        from zspan_cli import render

        html = render._key_decisions_html(
            "1. <core>Approved it</core> [at 0:05:10].\n\n"
            "2. **Denied it** [at 12:30]."
        )
        self.assertNotIn("[at", html)
        self.assertNotIn("class=\"chip\"", html)
        self.assertNotIn("data-seek", html)
        self.assertIn("<mark class=\"core\">", html)
        self.assertIn("<strong>Denied it</strong>", html)

    def test_cli_pause_threshold_and_round_trip_fallback(self):
        from zspan_cli import render

        no_break = [
            {"word": "one", "start": 0.0, "end": 0.5},
            {"word": "two", "start": 1.999, "end": 2.2},
        ]
        breaks = [
            {"word": "one", "start": 0.0, "end": 0.5},
            {"word": "two", "start": 2.0, "end": 2.2},
        ]
        self.assertEqual(
            render._paragraphize_verbatim_words(no_break, "one two", 1.5),
            ["one two"],
        )
        self.assertEqual(
            render._paragraphize_verbatim_words(breaks, "one two", 1.5),
            ["one", "two"],
        )
        self.assertIsNone(
            render._paragraphize_verbatim_words(breaks, "changed text", 1.5)
        )
        malformed = [
            {"word": "one", "start": 2.0, "end": 3.0},
            {"word": "two", "start": 1.0, "end": 4.0},
        ]
        self.assertIsNone(
            render._paragraphize_verbatim_words(malformed, "one two", 1.5)
        )

    def test_cli_threads_transcript_words_and_preserves_exact_token_order(self):
        from zspan_cli import render

        transcript_words = [
            {"word": "these", "start": 0.0, "end": 0.2},
            {"word": "Words", "start": 0.3, "end": 0.5},
            {"word": "stay", "start": 2.0, "end": 2.2},
            {"word": "odd", "start": 3.7, "end": 3.9},
        ]
        evidence = self._evidence(
            "these Words stay odd", start_word_index=0, end_word_index=3,
        )
        html = render._decision_evidence_html([evidence], transcript_words)
        self.assertIn(
            '<p class="decision-evidence-paragraph">these Words</p>'
            '<p class="decision-evidence-paragraph">stay</p>'
            '<p class="decision-evidence-paragraph">odd</p>',
            html,
        )
        self.assertNotIn("These Words", html)
        self.assertNotIn("odd.", html)

    def test_cli_round_trip_mismatch_falls_back_to_one_escaped_paragraph(self):
        from zspan_cli import render

        evidence = self._evidence(
            "original <raw> text",
            word_timings=[
                {"word": "changed", "start": 0.0, "end": 0.2},
                {"word": "text", "start": 2.0, "end": 2.2},
            ],
        )
        html = render._decision_evidence_html([evidence])
        self.assertIn(
            '<p class="decision-evidence-paragraph">original &lt;raw&gt; text</p>',
            html,
        )
        self.assertEqual(html.count('class="decision-evidence-paragraph"'), 1)
        self.assertNotIn("<raw>", html)

    def test_cli_short_long_and_elided_shapes(self):
        from zspan_cli import render

        short = " ".join(f"word{i}" for i in range(30))
        short_html = render._decision_evidence_html([self._evidence(short)])
        self.assertEqual(short_html.count("<blockquote"), 1)
        self.assertNotIn("decision-evidence-divider", short_html)
        self.assertNotIn("Collapse transcript source", short_html)

        long = " ".join(f"word{i}" for i in range(181))
        self.assertIn(
            "Collapse transcript source",
            render._decision_evidence_html([self._evidence(long)]),
        )

        elided = self._evidence(
            "item introduced",
            structure="elided",
            omission_marker="[WRONG 999 words 123ms]",
        )
        elided["verbatim_spans"].append({
            "text": "motion carried",
            "start_seconds": 1322.0,
            "end_seconds": 1323.0,
            "source": "item_quote_to_action_quote",
            "label": "Verbatim transcript excerpts — middle omitted",
            "structure": "elided",
            "omission_marker": "[WRONG 999 words 123ms]",
        })
        elided_html = render._decision_evidence_html([elided])
        self.assertIn("Verbatim transcript resumes about 22 minutes later", elided_html)
        self.assertNotIn("WRONG", elided_html)
        self.assertNotIn("999 words", elided_html)
        self.assertNotIn("123ms", elided_html)
        self.assertNotIn("mono", elided_html)
        self.assertNotIn("amber", elided_html)
        self.assertEqual(elided_html.count("<blockquote"), 2)
        self.assertIn("Collapse transcript source", elided_html)

    def test_cli_places_only_matching_excerpt_at_each_decision_end(self):
        from zspan_cli import render

        def evidence(index, text):
            return {
                "index": index,
                "verbatim_spans": [{
                    "text": text,
                    "source": "item_quote_to_action_quote",
                    "label": "Verbatim transcript excerpt — complete",
                    "structure": "contiguous",
                }],
            }

        content = "1. Approved it.\n\n2. Denied it."
        decision_evidence = [
            evidence(1, "first decision excerpt"),
            evidence(2, "second decision excerpt"),
        ]
        html = render._key_decisions_html(content, decision_evidence)
        first_start = html.index("Approved it.")
        second_start = html.index("Denied it.")
        first_decision = html[first_start:second_start]
        second_decision = html[second_start:]

        self.assertIn("first decision excerpt", first_decision)
        self.assertNotIn("second decision excerpt", first_decision)
        self.assertIn("second decision excerpt", second_decision)
        self.assertNotIn("first decision excerpt", second_decision)
        self.assertEqual(html.count("Verbatim transcript source"), 2)
        self.assertEqual(
            html.count(
                'class="decision-copy decision-evidence-host" data-state="closed"'
            ),
            2,
        )
        self.assertLess(
            first_decision.index("Approved it."),
            first_decision.index("decision-evidence-disclosure"),
        )

        page = render.meeting_page(
            {
                "title": "Council Meeting",
                "city": "Mesa",
                "meeting_date": "2026-07-21",
                "video_url": "",
                "processed_at": "2026-07-21T00:00:00",
            },
            {"key_decisions": {"content": content, "gate_status": "ok"}},
            decision_evidence=decision_evidence,
        )
        self.assertIn("<h3>Key Decisions</h3>", page)
        self.assertIn("background: var(--surface-2);", page)
        self.assertIn("border-left: 3px solid var(--highway-sign-blue);", page)
        self.assertIn("font-size: 16px;", page)
        self.assertIn("line-height: 1.75;", page)
        self.assertNotIn(".evidence-pop", page)

    def test_cli_uses_reduced_two_state_aria_js(self):
        from zspan_cli import render

        css = render._CSS
        js = render._EVIDENCE_JS

        evidence_css = css[css.index(".decision-evidence-host"):]
        self.assertIn(
            ".decision-evidence-disclosure[hidden] { display: none; }", css
        )
        self.assertNotIn("position: absolute", evidence_css)
        self.assertNotIn("max-height", evidence_css)
        self.assertNotIn("setTimeout", js)
        self.assertNotIn("pointerenter", js)
        self.assertNotIn("pointerleave", js)
        self.assertNotIn("document.addEventListener", js)
        self.assertIn("trigger.addEventListener('click'", js)
        self.assertIn("host.addEventListener('keydown'", js)
        self.assertIn("host.contains(document.activeElement)", js)
        self.assertIn("'Hide' : 'Show'", js)
        html = render._decision_evidence_html([self._evidence()])
        self.assertIn('aria-expanded="false"', html)
        self.assertIn(
            'aria-label="Show verbatim transcript source for this decision"', html
        )
        self.assertIn('aria-controls="decision-evidence-1"', html)
        self.assertNotIn('role="dialog"', html)
        self.assertNotIn('title=', html)
        self.assertNotIn('aria-haspopup', html)

    def test_cli_filters_to_item_to_action_source(self):
        from zspan_cli import render

        evidence = [{
            "index": 1,
            "verbatim_spans": [
                {
                    "text": "kept excerpt",
                    "source": "item_quote_to_action_quote",
                    "label": "Verbatim transcript excerpt — complete",
                    "structure": "contiguous",
                },
                {
                    "text": "wrong source",
                    "source": "legacy",
                    "label": "Legacy",
                    "structure": "contiguous",
                },
            ],
        }]
        html = render._decision_evidence_html(evidence)

        self.assertIn("kept excerpt", html)
        self.assertNotIn("wrong source", html)
        self.assertIn("Words unchanged", html)
        self.assertIn("Verbatim transcript source", html)

    def test_cli_renders_no_evidence_shape_without_materialized_spans(self):
        from zspan_cli import render

        self.assertEqual(render._decision_evidence_html([]), "")
        self.assertEqual(
            render._decision_evidence_html([
                {"index": 1, "verbatim_spans": [{"text": "legacy"}]}
            ]),
            "",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

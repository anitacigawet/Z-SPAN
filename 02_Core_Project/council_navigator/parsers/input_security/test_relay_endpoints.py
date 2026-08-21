"""S-008 V0 / surface S-9 — cross-machine HTTP endpoint tests.

Covers:
- Mac claude relay Pydantic schemas + bind-address pre-flight.
- PC agent relay Pydantic schemas + bind-address pre-flight.

Each schema is tested with valid payloads, over-length-rejection,
negative-integer-rejection, and missing-required-field rejection. The
bind-address pre-flight is tested against env-var combinations.

The relays themselves are not started — these tests exercise the
validation surface only. End-to-end auth + audit-log testing is
operationally verified per `INPUT_SECURITY_TESTS.md`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import ValidationError


def _load_module_from_path(module_name: str, file_path: Path) -> ModuleType:
    if not file_path.is_file():
        # The relay implementations are private runtime artifacts and are
        # intentionally absent from a clean public/workshop worktree. Keep the
        # validation live whenever that private source is present, while
        # reporting an honest skip instead of turning its absence into a gate
        # error.
        raise unittest.SkipTest(
            f"private relay source unavailable in this checkout: {file_path.name}"
        )
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build import spec for {file_path}")
    module = importlib.util.module_from_spec(spec)
    # Make the module discoverable to other importers (eg pickle); harmless.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as e:
        # The relay servers (pc_agent_relay / mac_claude_relay) are FastAPI
        # apps that run on their own nodes with their own venvs. When their
        # optional deps (fastapi, etc.) aren't installed in THIS environment,
        # SKIP the relay input-validation tests rather than erroring the whole
        # S-008 gate — these tests exercise the relay's Pydantic schemas, not
        # anything in the parser/worker venv.
        raise unittest.SkipTest(f"relay server dependency unavailable: {e}")
    return module


# Resolve the two relay server.py files relative to this test.
# Layout: 02_Core_Project/council_navigator/parsers/input_security/test_relay_endpoints.py
# parents[0] = input_security
# parents[1] = parsers
# parents[2] = council_navigator
# parents[3] = 02_Core_Project
_TWO_CORE = Path(__file__).resolve().parents[3]
_PC_RELAY_PATH = _TWO_CORE / "pc_agent_relay" / "server.py"
_MAC_RELAY_PATH = _TWO_CORE / "mac_claude_relay" / "server.py"


# ── PC agent relay tests ─────────────────────────────────────────────


class PCRelayValidationTests(unittest.TestCase):
    """Pydantic schemas on the PC agent relay refuse malformed payloads."""

    @classmethod
    def setUpClass(cls) -> None:
        # Token / LAN auto-detection are side effects at import time. The
        # token write is idempotent against parsers/user_settings.json
        # (already populated in any real Z-SPAN dev environment); the LAN
        # detect is a single UDP probe. Both safe.
        cls.relay = _load_module_from_path(
            "_zspan_pc_agent_relay_under_test", _PC_RELAY_PATH
        )

    def test_state_write_valid(self) -> None:
        m = self.relay.StateWriteRequest(
            city="Kingman", status="ok", meeting_count=4
        )
        self.assertEqual(m.city, "Kingman")
        self.assertEqual(m.meeting_count, 4)

    def test_state_write_missing_required_field_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.StateWriteRequest(city="", status="ok", meeting_count=4)
        with self.assertRaises(ValidationError):
            self.relay.StateWriteRequest(  # type: ignore[call-arg]
                city="Kingman", status="ok"
            )

    def test_state_write_negative_meeting_count_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.StateWriteRequest(
                city="Kingman", status="ok", meeting_count=-1
            )

    def test_state_write_over_length_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.StateWriteRequest(
                city="x" * 1000, status="ok", meeting_count=0
            )
        with self.assertRaises(ValidationError):
            self.relay.StateWriteRequest(
                city="Kingman",
                status="ok",
                meeting_count=0,
                last_error="x" * 10_000,
            )

    def test_escalate_valid_minimal(self) -> None:
        m = self.relay.EscalateRequest(severity="info", summary="all clear")
        self.assertEqual(m.severity, "info")

    def test_escalate_over_length_summary_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.EscalateRequest(
                severity="info", summary="x" * 50_000
            )

    def test_escalate_over_count_bullets_rejected(self) -> None:
        too_many = ["bullet"] * 1000
        with self.assertRaises(ValidationError):
            self.relay.EscalateRequest(
                severity="info", summary="x", see=too_many
            )

    def test_memory_write_valid_set(self) -> None:
        m = self.relay.MemoryWriteRequest(
            cmd="set", slug="x", type="observation", description="ok",
            body="body",
        )
        self.assertEqual(m.cmd, "set")

    def test_memory_write_over_length_body_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.MemoryWriteRequest(
                cmd="set", body="x" * 100_000,
            )

    def test_vocab_action_negative_correction_id_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.VocabActionRequest(cmd="promote", correction_id=-1)

    def test_dqr_action_negative_quote_id_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.DQRActionRequest(cmd="verify", quote_id=-1)

    def test_dqr_action_over_length_quote_text_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.DQRActionRequest(
                cmd="verify", quote_id=5, quote_text="x" * 100_000,
            )

    def test_trigger_agent_over_length_role_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.TriggerAgentRequest(role="x" * 1000)


class PCRelayPreflightTests(unittest.TestCase):
    """The PC relay bind-address pre-flight refuses 0.0.0.0 by default."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.relay = sys.modules.get(
            "_zspan_pc_agent_relay_under_test"
        ) or _load_module_from_path(
            "_zspan_pc_agent_relay_under_test", _PC_RELAY_PATH
        )

    def _with_env(self, **overrides: str | None) -> dict[str, str | None]:
        saved = {
            k: os.environ.get(k) for k in (
                "ZSPAN_AGENT_RELAY_HOST", "ZSPAN_AGENT_RELAY_BIND_ANY",
            )
        }
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return saved

    def _restore_env(self, saved: dict[str, str | None]) -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_binds_lan_ip(self) -> None:
        saved = self._with_env(
            ZSPAN_AGENT_RELAY_HOST=None,
            ZSPAN_AGENT_RELAY_BIND_ANY=None,
        )
        try:
            host = self.relay._resolve_host_with_preflight()
            # Either the detected LAN IP or 127.0.0.1 if detection failed.
            self.assertNotEqual(host, "0.0.0.0")
        finally:
            self._restore_env(saved)

    def test_explicit_0_0_0_0_rejected_without_override(self) -> None:
        saved = self._with_env(
            ZSPAN_AGENT_RELAY_HOST="0.0.0.0",
            ZSPAN_AGENT_RELAY_BIND_ANY=None,
        )
        try:
            with self.assertRaises(SystemExit):
                self.relay._resolve_host_with_preflight()
        finally:
            self._restore_env(saved)

    def test_explicit_0_0_0_0_allowed_with_override(self) -> None:
        saved = self._with_env(
            ZSPAN_AGENT_RELAY_HOST="0.0.0.0",
            ZSPAN_AGENT_RELAY_BIND_ANY="true",
        )
        try:
            self.assertEqual(
                self.relay._resolve_host_with_preflight(), "0.0.0.0",
            )
        finally:
            self._restore_env(saved)

    def test_explicit_other_host_honored(self) -> None:
        saved = self._with_env(
            ZSPAN_AGENT_RELAY_HOST="192.0.2.5",
            ZSPAN_AGENT_RELAY_BIND_ANY=None,
        )
        try:
            self.assertEqual(
                self.relay._resolve_host_with_preflight(), "192.0.2.5",
            )
        finally:
            self._restore_env(saved)


# ── Mac claude relay tests ───────────────────────────────────────────


class MacRelayValidationTests(unittest.TestCase):
    """Pydantic schemas on the Mac claude relay refuse malformed payloads."""

    @classmethod
    def setUpClass(cls) -> None:
        # The Mac relay reads ZSPAN_MAC_RELAY_TOKEN at module import; the
        # value affects only the require_token dependency (not exercised
        # here). Set a placeholder so a future module-level assertion can
        # rely on it.
        os.environ.setdefault("ZSPAN_MAC_RELAY_TOKEN", "test-import-only")
        cls.relay = _load_module_from_path(
            "_zspan_mac_relay_under_test", _MAC_RELAY_PATH
        )

    def test_invoke_valid_minimal(self) -> None:
        m = self.relay.InvokeRequest(prompt="hello")
        self.assertEqual(m.prompt, "hello")

    def test_invoke_empty_prompt_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.InvokeRequest(prompt="")

    def test_invoke_over_length_prompt_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.InvokeRequest(prompt="x" * 500_000)

    def test_invoke_over_count_allowed_tools_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.InvokeRequest(
                prompt="hi",
                allowed_tools=["Read"] * 1000,
            )

    def test_invoke_over_length_working_dir_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.InvokeRequest(prompt="hi", working_dir="/" * 2000)

    def test_shell_empty_cmd_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.ShellRequest(cmd="")

    def test_shell_over_length_cmd_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.ShellRequest(cmd="x" * 100_000)


class MacRelayPreflightTests(unittest.TestCase):
    """The Mac relay bind-address + token pre-flights work as advertised."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("ZSPAN_MAC_RELAY_TOKEN", "test-import-only")
        cls.relay = sys.modules.get(
            "_zspan_mac_relay_under_test"
        ) or _load_module_from_path(
            "_zspan_mac_relay_under_test", _MAC_RELAY_PATH
        )

    def _with_env(self, **overrides: str | None) -> dict[str, str | None]:
        saved = {
            k: os.environ.get(k) for k in (
                "HOST", "ZSPAN_MAC_RELAY_BIND_ANY",
            )
        }
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return saved

    def _restore_env(self, saved: dict[str, str | None]) -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_resolves_to_non_wildcard(self) -> None:
        saved = self._with_env(HOST=None, ZSPAN_MAC_RELAY_BIND_ANY=None)
        try:
            host = self.relay._resolve_host_with_preflight()
            self.assertNotEqual(host, "0.0.0.0")
        finally:
            self._restore_env(saved)

    def test_explicit_0_0_0_0_rejected_without_override(self) -> None:
        saved = self._with_env(HOST="0.0.0.0", ZSPAN_MAC_RELAY_BIND_ANY=None)
        try:
            with self.assertRaises(SystemExit):
                self.relay._resolve_host_with_preflight()
        finally:
            self._restore_env(saved)

    def test_explicit_0_0_0_0_allowed_with_override(self) -> None:
        saved = self._with_env(HOST="0.0.0.0", ZSPAN_MAC_RELAY_BIND_ANY="true")
        try:
            self.assertEqual(
                self.relay._resolve_host_with_preflight(), "0.0.0.0",
            )
        finally:
            self._restore_env(saved)

    def test_explicit_other_host_honored(self) -> None:
        saved = self._with_env(HOST="192.0.2.5", ZSPAN_MAC_RELAY_BIND_ANY=None)
        try:
            self.assertEqual(
                self.relay._resolve_host_with_preflight(), "192.0.2.5",
            )
        finally:
            self._restore_env(saved)

    def test_token_missing_exits(self) -> None:
        # The pre-flight reads the module-level EXPECTED_TOKEN, not the env,
        # because the token is captured at import. Simulate the missing-
        # token case by monkey-patching the module constant for one call.
        saved_token = self.relay.EXPECTED_TOKEN
        saved_env = self._with_env(
            HOST=None, ZSPAN_MAC_RELAY_BIND_ANY=None
        )
        self.relay.EXPECTED_TOKEN = ""
        try:
            with self.assertRaises(SystemExit):
                self.relay._resolve_host_with_preflight()
        finally:
            self.relay.EXPECTED_TOKEN = saved_token
            self._restore_env(saved_env)


if __name__ == "__main__":
    unittest.main()

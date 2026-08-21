"""S-008 V0 / surface S-8 — orchestrator rung-gate + curation tests.

Static + behavioral checks on `orchestrator_trigger_agent.py`:
- Rung 1 allows ONLY content-scout + parser-custodian autonomously.
- Rung 1 rejects DQR + vocab-curator + pipeline-operator autonomously.
- Mode B (instructed) bypasses the rung gate.
- Argparse choices enforce the role enum at parse time.

Per S-008 V0 / surface S-8 in
`01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType


_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_trigger_module() -> ModuleType:
    path = _SCRIPTS_DIR / "orchestrator_trigger_agent.py"
    if not path.exists():
        raise unittest.SkipTest(f"{path} not found")
    spec = importlib.util.spec_from_file_location(
        "orchestrator_trigger_agent", path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("spec build failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator_trigger_agent"] = module
    spec.loader.exec_module(module)
    return module


class RungGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_trigger_module()

    def _with_env(self, **overrides: str | None) -> dict[str, str | None]:
        keys = list(overrides.keys())
        saved = {k: os.environ.get(k) for k in keys}
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

    def test_current_rung_is_one(self):
        self.assertEqual(self.mod.CURRENT_RUNG, 1)

    def test_rung_1_allows_read_only_watchers(self):
        allowed = self.mod._current_rung_allowed()
        self.assertIn("content-scout", allowed)
        self.assertIn("parser-custodian", allowed)

    def test_rung_1_rejects_judgment_agents(self):
        allowed = self.mod._current_rung_allowed()
        self.assertNotIn("disputed-quotes-reviewer", allowed)
        self.assertNotIn("vocabulary-curator", allowed)
        self.assertNotIn("pipeline-operator", allowed)

    def test_known_roles_matches_rung_unions(self):
        # ALL_KNOWN_ROLES is the union of the rung-N additions; any role
        # passed to --role must be in this set per argparse choices.
        self.assertEqual(
            self.mod.ALL_KNOWN_ROLES,
            self.mod.RUNG_1_AUTO_ROLES
            | self.mod.RUNG_2_ADDITIONS
            | self.mod.RUNG_3_ADDITIONS,
        )

    def test_instructed_mode_detection_off(self):
        saved = self._with_env(ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS=None)
        try:
            self.assertFalse(self.mod.is_instructed_mode())
        finally:
            self._restore_env(saved)

    def test_instructed_mode_detection_on(self):
        saved = self._with_env(
            ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS="123.456",
        )
        try:
            self.assertTrue(self.mod.is_instructed_mode())
        finally:
            self._restore_env(saved)

    def test_instructed_mode_empty_string_off(self):
        saved = self._with_env(
            ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS="   ",  # whitespace-only
        )
        try:
            self.assertFalse(self.mod.is_instructed_mode())
        finally:
            self._restore_env(saved)


class RoleEnumTests(unittest.TestCase):
    """The argparse `choices` validation rejects roles outside ALL_KNOWN_ROLES."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_trigger_module()

    def test_argv_with_unknown_role_rejected(self):
        # Invoke main() via direct argv manipulation. argparse exits the
        # process with code 2 on unknown choice; capture SystemExit.
        saved_argv = sys.argv
        sys.argv = [
            "orchestrator_trigger_agent.py",
            "--role", "definitely-not-a-real-role",
            "--dry-run",
        ]
        try:
            with self.assertRaises(SystemExit) as cm:
                self.mod.main()
            # argparse exits 2 for unknown choice.
            self.assertEqual(cm.exception.code, 2)
        finally:
            sys.argv = saved_argv

    def test_dry_run_rejects_rung_2_role_at_rung_1(self):
        # Disable instructed mode so the rung gate fires.
        saved_env = os.environ.pop(
            "ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS", None,
        )
        saved_argv = sys.argv
        sys.argv = [
            "orchestrator_trigger_agent.py",
            "--role", "disputed-quotes-reviewer",
            "--dry-run",
        ]
        try:
            rc = self.mod.main()
            self.assertEqual(rc, 3)  # rung-rejected
        finally:
            sys.argv = saved_argv
            if saved_env is not None:
                os.environ["ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS"] = saved_env

    def test_dry_run_allows_rung_1_role(self):
        saved_env = os.environ.pop(
            "ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS", None,
        )
        saved_argv = sys.argv
        sys.argv = [
            "orchestrator_trigger_agent.py",
            "--role", "content-scout",
            "--dry-run",
        ]
        try:
            rc = self.mod.main()
            # 0 = dry-run-allowed; 4 = heartbeat script missing
            # (the latter is still expected if running outside the live
            # ops/ tree). Both are non-rung rejections, so they prove the
            # rung gate let content-scout through.
            self.assertIn(rc, (0, 4))
        finally:
            sys.argv = saved_argv
            if saved_env is not None:
                os.environ["ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS"] = saved_env

    def test_instructed_mode_bypasses_rung_for_dqr_dry_run(self):
        saved_env = os.environ.get("ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS")
        os.environ["ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS"] = "test-thread.0"
        saved_argv = sys.argv
        sys.argv = [
            "orchestrator_trigger_agent.py",
            "--role", "disputed-quotes-reviewer",
            "--dry-run",
        ]
        try:
            rc = self.mod.main()
            # 0 = dry-run-allowed; 4 = heartbeat script missing. Either
            # signals the rung wall passed (instructed bypassed it).
            self.assertIn(rc, (0, 4))
        finally:
            sys.argv = saved_argv
            if saved_env is None:
                os.environ.pop("ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS", None)
            else:
                os.environ["ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS"] = saved_env


if __name__ == "__main__":
    unittest.main()

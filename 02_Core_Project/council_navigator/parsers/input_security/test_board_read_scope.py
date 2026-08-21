"""S-008 V0 / surface S-14 — per-agent board_read scope tests.

Each agent's board_read wrapper enforces an ALLOWED_PATH_PREFIXES + (where
applicable) DENIED_PATH_SUBSTRINGS allowlist over the Flask path it will
forward to. This test loads each wrapper as a module and confirms the
allowlist matches what the agent manual scope says it should be.

These are STATIC checks against the wrapper's literal allow/deny lists.
The tests catch the failure mode where someone accidentally widens an
allowlist without updating the manual + the per-agent scope walls — a
real risk per S-7 / S-14.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1] / "scripts"
)


def _load_script_module(name: str) -> ModuleType:
    path = _SCRIPTS_DIR / f"{name}.py"
    if not path.exists():
        raise unittest.SkipTest(f"{path} not found")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DisputedQuotesReviewerBoardReadScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_script_module("disputed_quotes_reviewer_board_read")

    def test_role_is_self(self):
        self.assertEqual(self.mod.ROLE, "disputed-quotes-reviewer")

    def test_allowlist_matches_manual(self):
        expected = {
            "/api/disputed-quotes",
            "/api/operator/pending-escalations",
        }
        self.assertEqual(set(self.mod.ALLOWED_PATH_PREFIXES), expected)

    def test_denylist_includes_action_substrings(self):
        for needle in ("/resolve", "/agent-propose"):
            self.assertIn(needle, self.mod.DENIED_PATH_SUBSTRINGS)

    def test_is_allowed_accepts_in_scope(self):
        ok, _ = self.mod._is_allowed("/api/disputed-quotes")
        self.assertTrue(ok)
        ok, _ = self.mod._is_allowed(
            "/api/operator/pending-escalations"
        )
        self.assertTrue(ok)

    def test_is_allowed_rejects_out_of_scope(self):
        ok, reason = self.mod._is_allowed("/api/work-orders")
        self.assertFalse(ok)
        self.assertIn("allowlist", reason)

    def test_is_allowed_rejects_action_substrings(self):
        ok, reason = self.mod._is_allowed(
            "/api/disputed-quotes/42/resolve"
        )
        self.assertFalse(ok)
        self.assertIn("denied action substring", reason)

    def test_is_allowed_rejects_relative_paths(self):
        ok, _ = self.mod._is_allowed("api/disputed-quotes")
        self.assertFalse(ok)


class VocabularyCuratorBoardReadScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_script_module("vocabulary_curator_board_read")

    def test_role_is_self(self):
        self.assertEqual(self.mod.ROLE, "vocabulary-curator")

    def test_is_allowed_in_scope(self):
        # The vocabulary inbox is the canonical lane.
        for path in (
            "/api/vocabulary-inbox",
            "/api/vocabulary-inbox?city=Kingman",
        ):
            ok, reason = self.mod._is_allowed(path)
            self.assertTrue(ok, f"{path} unexpectedly rejected: {reason}")

    def test_is_allowed_rejects_out_of_scope(self):
        for path in (
            "/api/disputed-quotes",
            "/api/work-orders",
            "/api/settings",
        ):
            ok, _ = self.mod._is_allowed(path)
            self.assertFalse(ok, f"{path} unexpectedly allowed")


class ContentScoutBoardReadScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_script_module("content_scout_board_read")

    def test_role_is_self(self):
        self.assertEqual(self.mod.ROLE, "content-scout")

    def test_is_allowed_rejects_settings(self):
        ok, _ = self.mod._is_allowed("/api/settings")
        self.assertFalse(ok)

    def test_is_allowed_rejects_refresh_query(self):
        # The content scout's allowlist explicitly excludes ?refresh=true on
        # /scrape/* (per its manual).
        ok, reason = self.mod._is_allowed("/scrape/Kingman?refresh=true")
        self.assertFalse(ok)


class OrchestratorBoardReadScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_script_module("orchestrator_board_read")

    def test_role_is_self(self):
        self.assertEqual(self.mod.ROLE, "orchestrator")

    def test_is_allowed_rejects_action_substrings(self):
        # Orchestrator can read board state but must not call mutating
        # endpoints via board_read.
        for path in (
            "/api/disputed-quotes/42/resolve",
            "/api/vocabulary-inbox/promote",
            "/api/work-orders/1/process",
        ):
            ok, reason = self.mod._is_allowed(path)
            self.assertFalse(
                ok, f"orchestrator board_read unexpectedly allowed {path}"
            )


if __name__ == "__main__":
    unittest.main()

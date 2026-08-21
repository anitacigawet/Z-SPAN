"""S-008 V0 / pillar 3b — user-input moderation primitive tests.

Exercises `parsers.input_moderation`:
- Clean input passes the deterministic rules.
- Per-surface caps fire correctly (creator_feedback vs suggestion_query
  vs creator_signup).
- Rate-limit storage works against the user_input_attempts table.
- Failures land in the table with the rejection reason.

Per [D-100](../../../../01_Project_Overview/DECISIONS.md#d-100): defensive
negative-test cases.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from parsers.input_moderation import (
    SURFACE_DEFAULTS,
    SurfaceConfig,
    ModerationResult,
    moderate_user_input,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SurfaceDefaultsTests(unittest.TestCase):
    def test_creator_feedback_caps(self):
        cfg = SURFACE_DEFAULTS["creator_feedback"]
        self.assertEqual(cfg.max_length, 2_000)
        self.assertEqual(cfg.max_urls, 3)

    def test_suggestion_query_caps(self):
        cfg = SURFACE_DEFAULTS["suggestion_query"]
        self.assertEqual(cfg.max_length, 500)
        self.assertEqual(cfg.max_urls, 0)

    def test_creator_signup_caps(self):
        cfg = SURFACE_DEFAULTS["creator_signup"]
        self.assertEqual(cfg.max_length, 500)
        self.assertEqual(cfg.max_urls, 0)


class ModerationContentTests(unittest.TestCase):
    """Without DB writes — record_attempt=False short-circuits the table."""

    def test_clean_feedback_accepted(self):
        out = moderate_user_input(
            "I used the Bullhead 5/28 highlights in a piece about parks funding.",
            surface="creator_feedback",
            user_id=1,
            record_attempt=False,
        )
        self.assertTrue(out.accept)
        self.assertEqual(out.reason, "clean")
        self.assertIsNotNone(out.normalized_text)

    def test_over_length_feedback_rejected(self):
        out = moderate_user_input(
            "x" * 5_000,
            surface="creator_feedback",
            user_id=1,
            record_attempt=False,
        )
        self.assertFalse(out.accept)
        self.assertEqual(out.reason, "too_long")

    def test_over_length_suggestion_rejected(self):
        # suggestion_query has a tighter cap (500).
        out = moderate_user_input(
            "x" * 1_000,
            surface="suggestion_query",
            user_id=1,
            record_attempt=False,
        )
        self.assertFalse(out.accept)
        self.assertEqual(out.reason, "too_long")

    def test_suggestion_query_no_urls_allowed(self):
        # creator_feedback allows 3 URLs; suggestion_query allows 0.
        out = moderate_user_input(
            "see https://example.com",
            surface="suggestion_query",
            user_id=1,
            record_attempt=False,
        )
        self.assertFalse(out.accept)
        self.assertEqual(out.reason, "too_many_urls")

    def test_creator_feedback_url_budget(self):
        out = moderate_user_input(
            "see https://a.example and https://b.example",
            surface="creator_feedback",
            user_id=1,
            record_attempt=False,
        )
        self.assertTrue(out.accept)

    def test_fence_marker_rejected(self):
        out = moderate_user_input(
            "<zspan-content-begin nonce=\"x\">",
            surface="creator_feedback",
            user_id=1,
            record_attempt=False,
        )
        self.assertFalse(out.accept)

    def test_unknown_surface_raises(self):
        with self.assertRaises(ValueError):
            moderate_user_input(
                "x",
                surface="nonexistent_surface",  # type: ignore[arg-type]
                user_id=1,
                record_attempt=False,
            )

    def test_config_override(self):
        # Override lets a tighter ad-hoc surface fire.
        out = moderate_user_input(
            "x" * 100,
            surface="creator_feedback",
            user_id=1,
            config_override=SurfaceConfig(
                max_length=10, max_urls=0, per_user_per_day_cap=100,
            ),
            record_attempt=False,
        )
        self.assertFalse(out.accept)
        self.assertEqual(out.reason, "too_long")


class RateLimitTests(unittest.TestCase):
    """Exercise the DB-backed rate-limit storage via an isolated sqlite."""

    def setUp(self):
        self.tmp_db_path = _PROJECT_ROOT / "parsers" / (
            f"_test_input_moderation_{id(self)}.db"
        )
        if self.tmp_db_path.exists():
            self.tmp_db_path.unlink()
        conn = sqlite3.connect(self.tmp_db_path)
        conn.execute(
            """
            CREATE TABLE user_input_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                surface TEXT NOT NULL,
                accept INTEGER NOT NULL,
                reason TEXT,
                submitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
        conn.close()

        self._patches = []
        patcher = mock.patch(
            "parsers.database.get_connection",
            side_effect=lambda: sqlite3.connect(self.tmp_db_path),
        )
        patcher.start()
        self._patches.append(patcher)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        try:
            if self.tmp_db_path.exists():
                self.tmp_db_path.unlink()
        except PermissionError:
            pass

    def _count(self) -> int:
        conn = sqlite3.connect(self.tmp_db_path)
        row = conn.execute(
            "SELECT COUNT(*) FROM user_input_attempts"
        ).fetchone()
        conn.close()
        return row[0]

    def test_clean_input_records_attempt(self):
        moderate_user_input(
            "Clean note.",
            surface="creator_feedback",
            user_id=42,
        )
        self.assertEqual(self._count(), 1)

    def test_rejected_input_still_records(self):
        moderate_user_input(
            "x" * 5_000,
            surface="creator_feedback",
            user_id=42,
        )
        # We want the rejection in the audit log too.
        self.assertEqual(self._count(), 1)

    def test_rate_limit_fires(self):
        cfg = SurfaceConfig(
            max_length=100, max_urls=0, per_user_per_day_cap=3,
        )
        for _ in range(3):
            out = moderate_user_input(
                "note",
                surface="suggestion_query",
                user_id=99,
                config_override=cfg,
            )
            self.assertTrue(out.accept)
        # Fourth attempt should hit the rate limit.
        out = moderate_user_input(
            "note",
            surface="suggestion_query",
            user_id=99,
            config_override=cfg,
        )
        self.assertFalse(out.accept)
        self.assertIn("rate_limited", out.reason)

    def test_rate_limit_per_user_independent(self):
        cfg = SurfaceConfig(
            max_length=100, max_urls=0, per_user_per_day_cap=2,
        )
        for _ in range(2):
            moderate_user_input(
                "note", surface="suggestion_query", user_id=1,
                config_override=cfg,
            )
        # User 1 is at cap; user 2 should still be fine.
        out = moderate_user_input(
            "note", surface="suggestion_query", user_id=2,
            config_override=cfg,
        )
        self.assertTrue(out.accept)


if __name__ == "__main__":
    unittest.main()

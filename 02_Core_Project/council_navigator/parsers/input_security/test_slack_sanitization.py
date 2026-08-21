"""S-008 V0 / surface S-14 — slack_notifier sanitization tests.

Exercises `parsers.slack_notifier._sanitize_escalation_text` +
`_sanitize_escalation_bullets` against:
- bidi controls (stripped)
- structural fence markers (replaced with redaction tag)
- benign text (passes through unchanged)
- existing `_strip_bold` semantics (preserved — not regressed)

These functions execute on the agent-emit path before insert_pending_escalation
+ before the Slack POST. The deeper defenses (Pydantic length caps in the
relay; agent_audit.validate_agent_text in action wrappers) catch most of
these upstream; the slack_notifier sanitizer is the off-path safety net.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# slack_notifier resolves its sibling helpers (env_config) via a bare
# `from env_config import ...`, expecting parsers/ to be on sys.path. The
# test runner adds the project root; this insertion adds the parsers dir
# specifically so the bare import resolves regardless of caller cwd.
_PARSERS_DIR = Path(__file__).resolve().parents[1]
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from parsers.slack_notifier import (  # noqa: E402  (path setup above)
    _sanitize_escalation_bullets,
    _sanitize_escalation_text,
    _strip_bold,
)


class StripBoldTests(unittest.TestCase):
    """The pre-existing helper still does what it did. No regression."""

    def test_strips_asterisks(self):
        self.assertEqual(_strip_bold("hello *world*"), "hello world")

    def test_none_safe(self):
        self.assertEqual(_strip_bold(None), "")  # type: ignore[arg-type]


class SanitizeEscalationTextTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(_sanitize_escalation_text(None))

    def test_clean_text_unchanged(self):
        self.assertEqual(
            _sanitize_escalation_text("Council member Stehly motion passed."),
            "Council member Stehly motion passed.",
        )

    def test_bidi_controls_stripped(self):
        # Insert an LRO (U+202D) and a PDF (U+202C).
        bidi_text = "ok ‭looks reversed‬"
        self.assertEqual(
            _sanitize_escalation_text(bidi_text),
            "ok looks reversed",
        )

    def test_fence_begin_marker_replaced(self):
        text = "summary <zspan-content-begin nonce=\"abc\"> body"
        out = _sanitize_escalation_text(text)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("[fence-marker-stripped]", out)
        self.assertNotIn("<zspan-content-begin", out.lower())

    def test_fence_end_marker_replaced(self):
        text = "tail <zspan-content-end nonce=\"abc\">"
        out = _sanitize_escalation_text(text)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("[fence-marker-stripped]", out)

    def test_case_insensitive_marker_detection(self):
        out = _sanitize_escalation_text(
            "<ZSPAN-CONTENT-BEGIN nonce=\"x\">"
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("[fence-marker-stripped]", out)

    def test_multiple_markers_all_replaced(self):
        text = (
            "<zspan-content-begin nonce=\"a\"> body "
            "<zspan-content-end nonce=\"a\">"
        )
        out = _sanitize_escalation_text(text)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.count("[fence-marker-stripped]"), 2)

    def test_non_string_coerced(self):
        out = _sanitize_escalation_text(123)  # type: ignore[arg-type]
        self.assertEqual(out, "123")


class SanitizeEscalationBulletsTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(_sanitize_escalation_bullets(None))

    def test_empty_list(self):
        self.assertEqual(_sanitize_escalation_bullets([]), [])

    def test_per_bullet_sanitization(self):
        bullets = [
            "Stehly motioned to approve.",
            "‭reversed‬ statement",
            "<zspan-content-begin nonce=\"x\"> bad",
        ]
        out = _sanitize_escalation_bullets(bullets)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out[0], "Stehly motioned to approve.")
        self.assertEqual(out[1], "reversed statement")
        self.assertIn("[fence-marker-stripped]", out[2])


if __name__ == "__main__":
    unittest.main()

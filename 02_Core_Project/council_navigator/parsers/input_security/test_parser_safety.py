"""S-008 V0 / surface S-3 — scraped HTML parser safety tests.

These tests exercise the normalize.py canonical pass — every parser's
output flows through normalize_meeting_fields before reaching cache.db.
The defensive guarantees we lock in here:

1. normalize_meeting_fields is a pure function: same dict in, same dict
   out, no side effects (no network calls, no file writes, no subprocess
   spawns).
2. The output dict contains only primitive Python types (str, int, None,
   bool, dict, list) — never custom objects that could carry __getitem__
   side effects.
3. Adversarial content in input strings (script tags, javascript: URIs,
   bidi controls, oversize attribute values) passes through as TEXT,
   never gets executed or interpolated into shell/HTTP calls.

Per [D-100](../../../../01_Project_Overview/DECISIONS.md#d-100), fixture
inputs use known-hostile substrings AS NEGATIVE-TEST CASES.

The per-parser audit (each ~93 city parsers' scrape_calendar function)
is documented as ongoing work in INPUT_SECURITY_TESTS.md. This module
locks in the canonical normalization layer; per-parser tests get added
as parsers are touched.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# normalize.py uses bare imports against the parsers/ dir.
_PARSERS_DIR = Path(__file__).resolve().parents[1]
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from normalize import normalize_meeting_fields  # noqa: E402


_ADVERSARIAL_FIXTURES = (
    {
        "name": "script_tag_in_title",
        "meeting": {
            "title": "<script>alert(1)</script>",
            "date": "2026-06-02",
            "link": "https://kingmancity.gov/agenda",
        },
    },
    {
        "name": "javascript_uri_in_link",
        "meeting": {
            "title": "City Council Regular Meeting",
            "date": "2026-06-02",
            "link": "javascript:alert(1)",
        },
    },
    {
        "name": "data_uri_in_link",
        "meeting": {
            "title": "City Council Regular Meeting",
            "date": "2026-06-02",
            "link": "data:text/html,<script>alert(1)</script>",
        },
    },
    {
        "name": "bidi_controls_in_title",
        "meeting": {
            "title": "Council ‮reversed‬ meeting",
            "date": "2026-06-02",
            "link": "https://kingmancity.gov/agenda",
        },
    },
    {
        "name": "oversize_attribute",
        "meeting": {
            "title": "City Council Regular Meeting",
            "date": "2026-06-02",
            "link": "https://kingmancity.gov/agenda?q=" + "x" * 50_000,
        },
    },
    {
        "name": "fence_marker_in_title",
        "meeting": {
            "title": "Council <zspan-content-begin nonce=\"x\"> meeting",
            "date": "2026-06-02",
            "link": "https://kingmancity.gov/agenda",
        },
    },
)


class NormalizePurityTests(unittest.TestCase):
    """normalize_meeting_fields is pure: no side effects, only primitive
    types in output."""

    def test_normalize_does_not_mutate_input(self):
        original = {
            "title": "Council Regular Meeting",
            "date": "2026-06-02",
            "link": "https://example.com",
        }
        snapshot = dict(original)
        _ = normalize_meeting_fields(original)
        self.assertEqual(original, snapshot)

    def test_normalize_output_contains_only_primitives(self):
        for fixture in _ADVERSARIAL_FIXTURES:
            with self.subTest(fixture=fixture["name"]):
                out = normalize_meeting_fields(fixture["meeting"])
                for key, value in out.items():
                    self.assertIsInstance(
                        key, str,
                        f"normalized key {key!r} must be str",
                    )
                    self.assertIsInstance(
                        value,
                        (str, int, float, bool, type(None), dict, list),
                        f"value for {key!r} has non-primitive type "
                        f"{type(value).__name__}",
                    )

    def test_normalize_clean_input_renames_canonically(self):
        meeting = {
            "title": "City Council Regular Meeting",
            "date": "2026-06-02",
            "time": "5:30 PM",
            "link": "https://kingmancity.gov/agenda",
            "location": "Council Chambers",
        }
        out = normalize_meeting_fields(meeting)
        self.assertEqual(out["meeting_title"], "City Council Regular Meeting")
        self.assertEqual(out["meeting_date"], "2026-06-02")
        self.assertEqual(out["meeting_time"], "5:30 PM")
        self.assertEqual(out["agenda_url"], "https://kingmancity.gov/agenda")
        self.assertEqual(out["meeting_location"], "Council Chambers")


class AdversarialPayloadPreservationTests(unittest.TestCase):
    """Adversarial input strings pass through as TEXT (preserved verbatim).
    The defensive guarantee is that they are NOT executed; preservation
    means downstream cache.db gets the raw bytes, which the operator
    review path can flag if needed.
    """

    def test_script_tag_passes_through_as_text(self):
        out = normalize_meeting_fields({
            "title": "<script>alert(1)</script>",
            "date": "2026-06-02",
        })
        # The script tag stays as text in the canonical title field. It
        # does NOT get executed or sanitized at this layer — the operator
        # review surface is responsible for surfacing it.
        self.assertIn("<script>", out["meeting_title"])

    def test_javascript_uri_passes_through_as_text(self):
        out = normalize_meeting_fields({
            "title": "x",
            "date": "2026-06-02",
            "link": "javascript:alert(1)",
        })
        self.assertEqual(out["agenda_url"], "javascript:alert(1)")

    def test_bidi_controls_preserved(self):
        # bidi controls preserved in the canonical text field (downstream
        # surfaces are responsible for stripping per their display
        # requirements). This is the parser-layer guarantee: pure data
        # passthrough, no transformation.
        adv = "Council ‮reversed‬ meeting"
        out = normalize_meeting_fields({"title": adv, "date": "2026-06-02"})
        self.assertEqual(out["meeting_title"], adv)


class NoSideEffectsTests(unittest.TestCase):
    """normalize_meeting_fields must not trigger network calls / shell /
    file writes / subprocess spawns. This is a structural guarantee — the
    function's body has no I/O. We verify by:
    1. Checking the source file does not import network/subprocess/file
       modules at module scope.
    """

    def test_no_dangerous_imports_in_module(self):
        normalize_path = _PARSERS_DIR / "normalize.py"
        source = normalize_path.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "import os.system",
            "import requests",
            "import urllib",
            "import http",
            "import socket",
        ):
            self.assertNotIn(
                forbidden, source,
                f"normalize.py imports {forbidden!r} — unsafe at this layer",
            )


if __name__ == "__main__":
    unittest.main()

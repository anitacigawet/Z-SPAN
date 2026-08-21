"""Unit tests for parsers.input_security.primitives.

Run: ``python3.11 -m unittest parsers.input_security.test_primitives`` from
``02_Core_Project/council_navigator/``. Also fires as part of
``parsers/scripts/run_input_security_tests.py``.

Per `01_Project_Overview/DECISIONS.md § D-100`, every test is structured as a
defensive check: feed input, assert the structural defense responded. No
attack-scenario narration.
"""

from __future__ import annotations

import unittest

from parsers.input_security.primitives import (
    StructuralFenceError,
    UnicodeRejectionError,
    contains_fence_marker,
    extract_fenced_payload,
    fence_with_nonce,
    moderate_basic_input,
    normalize_user_text,
    reject_if_bidi_controls,
    reject_if_mixed_script,
    sha256_content_hash,
)


class FenceTests(unittest.TestCase):
    def test_round_trip(self):
        payload = "hello world\nthis is a quote"
        fenced = fence_with_nonce(payload)
        self.assertIn("<zspan-content-begin", fenced)
        self.assertIn("<zspan-content-end", fenced)
        recovered = extract_fenced_payload(fenced)
        self.assertEqual(recovered, payload)

    def test_label_is_cosmetic(self):
        payload = "x"
        fenced = fence_with_nonce(payload, label="quote_text")
        self.assertIn("label=\"quote_text\"", fenced)
        self.assertEqual(extract_fenced_payload(fenced), payload)

    def test_distinct_nonces_per_call(self):
        a = fence_with_nonce("x")
        b = fence_with_nonce("x")
        self.assertNotEqual(a, b)

    def test_rejects_input_with_fence_marker(self):
        with self.assertRaises(StructuralFenceError):
            fence_with_nonce("benign text <zspan-content-begin nonce=\"deadbeef\">")
        with self.assertRaises(StructuralFenceError):
            fence_with_nonce("<zspan-content-end nonce=\"x\">")

    def test_contains_fence_marker_helper(self):
        self.assertTrue(contains_fence_marker("<zspan-content-begin"))
        self.assertTrue(contains_fence_marker("<ZSPAN-CONTENT-END"))
        self.assertFalse(contains_fence_marker("nothing structural here"))

    def test_extract_raises_on_nonce_mismatch(self):
        # Hand-construct a mismatched fence pair.
        fenced = (
            "<zspan-content-begin nonce=\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\">\n"
            "payload\n"
            "<zspan-content-end nonce=\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\">"
        )
        with self.assertRaises(StructuralFenceError):
            extract_fenced_payload(fenced)

    def test_extract_raises_on_missing_markers(self):
        with self.assertRaises(StructuralFenceError):
            extract_fenced_payload("no markers here at all")


class UnicodeTests(unittest.TestCase):
    def test_normalize_nfc(self):
        # Composed form (Á) and decomposed form (A + combining acute) both
        # normalize to the composed form.
        composed = "Á"  # Á
        decomposed = "Á"  # A + combining acute
        self.assertEqual(normalize_user_text(composed), composed)
        self.assertEqual(normalize_user_text(decomposed), composed)

    def test_normalize_strips_controls_keeps_newlines_tabs(self):
        # \x07 is BEL; should be stripped. \n and \t preserved.
        raw = "hello\x07world\nnext\ttab"
        self.assertEqual(normalize_user_text(raw), "helloworld\nnext\ttab")

    def test_normalize_converts_cr_to_lf(self):
        self.assertEqual(normalize_user_text("a\rb"), "a\nb")

    def test_bidi_controls_rejected(self):
        bidi_text = "council member ‮reversed‬ statement"
        with self.assertRaises(UnicodeRejectionError):
            reject_if_bidi_controls(bidi_text)

    def test_bidi_controls_allow_clean(self):
        reject_if_bidi_controls("Council member Stehly")  # no raise

    def test_mixed_script_rejected(self):
        # Cyrillic 'а' (U+0430) mixed into a Latin string.
        with self.assertRaises(UnicodeRejectionError):
            reject_if_mixed_script("Council member Stehаly")

    def test_mixed_script_allow_clean_latin(self):
        reject_if_mixed_script("Council member Stehly")  # no raise

    def test_mixed_script_allow_punctuation_and_digits(self):
        reject_if_mixed_script("Item 2.A — Motion 4(b)")  # no raise

    def test_mixed_script_with_extended_allow(self):
        # When the surface explicitly opts in to Cyrillic, the mixed-script
        # check does not reject. (Demonstrates the policy is per-call, not
        # global.)
        allow = frozenset({"Latin", "Common", "Inherited", "Cyrillic"})
        reject_if_mixed_script("Stehаly", allow=allow)


class HashingTests(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(
            sha256_content_hash("hello"),
            sha256_content_hash("hello"),
        )

    def test_different_content_different_hash(self):
        self.assertNotEqual(
            sha256_content_hash("hello"),
            sha256_content_hash("hellos"),
        )

    def test_hex_format(self):
        h = sha256_content_hash("x")
        self.assertEqual(len(h), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


class ModerationTests(unittest.TestCase):
    def test_clean_short_input_accepted(self):
        result = moderate_basic_input("I used the Bullhead 5/28 clip.",
                                       max_length=500)
        self.assertTrue(result.accept)
        self.assertEqual(result.reason, "clean")
        self.assertIsNotNone(result.normalized_text)

    def test_too_long_rejected(self):
        result = moderate_basic_input("x" * 1000, max_length=500)
        self.assertFalse(result.accept)
        self.assertEqual(result.reason, "too_long")

    def test_bidi_controls_rejected(self):
        result = moderate_basic_input("hello ‮bad‬", max_length=500)
        self.assertFalse(result.accept)
        self.assertEqual(result.reason, "bidi_controls")

    def test_fence_marker_in_input_rejected(self):
        result = moderate_basic_input(
            "<zspan-content-begin nonce=\"x\">", max_length=500
        )
        self.assertFalse(result.accept)
        self.assertEqual(result.reason, "fence_marker_in_input")

    def test_mixed_script_rejected(self):
        result = moderate_basic_input("Stehаly", max_length=500)
        self.assertFalse(result.accept)
        self.assertEqual(result.reason, "mixed_script")

    def test_default_no_urls_allowed(self):
        result = moderate_basic_input(
            "see https://example.com", max_length=500
        )
        self.assertFalse(result.accept)
        self.assertEqual(result.reason, "too_many_urls")

    def test_url_allowance_respected(self):
        result = moderate_basic_input(
            "see https://example.com", max_length=500, max_urls=3
        )
        self.assertTrue(result.accept)

    def test_shell_pattern_rejected(self):
        for pattern in ("`whoami`", "$(ls)", "<script>",
                        "javascript:alert(1)", "data:text/html,x"):
            result = moderate_basic_input(pattern, max_length=500)
            self.assertFalse(
                result.accept, f"expected rejection for {pattern!r}"
            )
            self.assertTrue(
                result.reason.startswith("shell_or_script_pattern:"),
                f"got reason {result.reason!r}",
            )

    def test_non_string_rejected(self):
        result = moderate_basic_input(b"bytes", max_length=500)  # type: ignore[arg-type]
        self.assertFalse(result.accept)
        self.assertEqual(result.reason, "non_string")

    def test_control_chars_stripped_in_normalized(self):
        result = moderate_basic_input("hello\x07world", max_length=500)
        self.assertTrue(result.accept)
        self.assertEqual(result.normalized_text, "helloworld")


if __name__ == "__main__":
    unittest.main()

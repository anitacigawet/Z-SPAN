"""S-036 V1-complete — tests for the field-sanity output gate.

Covers URL HEAD-checks (mocked to avoid real network), ISO date validation,
plausible time validation, the per-invocation HEAD-check budget cap, and
the no-mutation guarantee on the input dict.

Run via:
    python3.11 test_haiku_field_sanity.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import haiku_field_sanity as fs  # type: ignore


def _make_response(meetings):
    """Build a minimal Haiku response dict with the given meetings."""
    return {
        "scrape_success": True,
        "scrape_method": "static_html",
        "meetings_found": len(meetings),
        "meetings": meetings,
        "caveats": [],
        "raw_observations": "test fixture",
    }


class NoOpAndEmptyCases(unittest.TestCase):
    def test_no_meetings_returns_clean_report(self):
        resp = _make_response([])
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        self.assertEqual(sanitized["meetings"], [])
        self.assertEqual(report.head_checks_attempted, 0)
        self.assertEqual(report.urls_cleared, [])
        self.assertEqual(report.dates_cleared, [])
        self.assertEqual(report.times_cleared, [])

    def test_meetings_field_missing_returns_input_unchanged(self):
        resp = {"scrape_success": True}  # no meetings key
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        self.assertEqual(sanitized, resp)
        self.assertEqual(report.head_checks_attempted, 0)

    def test_empty_url_fields_skipped(self):
        meeting = {
            "meeting_title": "Council", "meeting_date": "2026-06-14",
            "meeting_time": "7:00 PM", "agenda_url": "", "minutes_url": "",
            "video_url": "", "ecomment_url": "",
        }
        resp = _make_response([meeting])
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=False)
        self.assertEqual(report.head_checks_attempted, 0)


class DateSanityTests(unittest.TestCase):
    def test_strict_iso_date_passes(self):
        meeting = {"meeting_title": "X", "meeting_date": "2026-06-14"}
        resp = _make_response([meeting])
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        self.assertEqual(sanitized["meetings"][0]["meeting_date"], "2026-06-14")
        self.assertEqual(report.dates_cleared, [])

    def test_us_format_date_cleared(self):
        meeting = {"meeting_title": "X", "meeting_date": "6/14/2026"}
        resp = _make_response([meeting])
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        self.assertEqual(sanitized["meetings"][0]["meeting_date"], "")
        self.assertEqual(len(report.dates_cleared), 1)
        self.assertEqual(report.dates_cleared[0]["original_value"], "6/14/2026")

    def test_natural_language_date_cleared(self):
        meeting = {"meeting_title": "X", "meeting_date": "June 14, 2026"}
        resp = _make_response([meeting])
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        self.assertEqual(sanitized["meetings"][0]["meeting_date"], "")
        self.assertEqual(len(report.dates_cleared), 1)

    def test_empty_date_passes(self):
        meeting = {"meeting_title": "X", "meeting_date": ""}
        resp = _make_response([meeting])
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        self.assertEqual(report.dates_cleared, [])


class TimeSanityTests(unittest.TestCase):
    def test_12hr_format_passes(self):
        for valid in ["7:00 PM", "12:30 am", "11:00 PM"]:
            meeting = {"meeting_title": "X", "meeting_time": valid}
            resp = _make_response([meeting])
            _, report = fs.apply_field_sanity(resp, skip_head_checks=True)
            self.assertEqual(
                report.times_cleared, [],
                f"valid time {valid!r} should pass"
            )

    def test_24hr_format_passes(self):
        meeting = {"meeting_title": "X", "meeting_time": "19:00"}
        resp = _make_response([meeting])
        _, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        self.assertEqual(report.times_cleared, [])

    def test_junk_time_cleared(self):
        meeting = {"meeting_title": "X", "meeting_time": "afternoon-ish"}
        resp = _make_response([meeting])
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        self.assertEqual(sanitized["meetings"][0]["meeting_time"], "")
        self.assertEqual(len(report.times_cleared), 1)


class UrlHeadCheckTests(unittest.TestCase):
    """HEAD-check behavior with mocked requests."""

    def test_2xx_url_passes(self):
        meeting = {
            "meeting_title": "X", "meeting_date": "2026-06-14",
            "agenda_url": "https://example.gov/agenda.pdf",
        }
        resp = _make_response([meeting])
        mock_resp = type("R", (), {"status_code": 200})()
        with patch("haiku_field_sanity.requests.head", return_value=mock_resp):
            sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=False)
        self.assertEqual(sanitized["meetings"][0]["agenda_url"], "https://example.gov/agenda.pdf")
        self.assertEqual(report.head_checks_passed, 1)
        self.assertEqual(report.urls_cleared, [])

    def test_404_url_cleared(self):
        meeting = {
            "meeting_title": "X", "meeting_date": "2026-06-14",
            "video_url": "https://example.gov/fake-video.mp4",
        }
        resp = _make_response([meeting])
        mock_resp = type("R", (), {"status_code": 404})()
        with patch("haiku_field_sanity.requests.head", return_value=mock_resp):
            sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=False)
        self.assertEqual(sanitized["meetings"][0]["video_url"], "")
        self.assertEqual(report.head_checks_failed, 1)
        self.assertEqual(len(report.urls_cleared), 1)
        self.assertIn("HTTP 404", report.urls_cleared[0]["reason"])

    def test_timeout_url_kept_as_unverified(self):
        # F-1: Timeout means the server didn't speak HEAD. It is NOT a
        # verdict on URL validity. Keep the URL; record as unverified.
        import requests as real_requests
        meeting = {
            "meeting_title": "X", "meeting_date": "2026-06-14",
            "agenda_url": "https://slowsite.gov/x.pdf",
        }
        resp = _make_response([meeting])
        with patch(
            "haiku_field_sanity.requests.head",
            side_effect=real_requests.Timeout("timed out"),
        ):
            sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=False)
        # URL is KEPT, not cleared.
        self.assertEqual(
            sanitized["meetings"][0]["agenda_url"], "https://slowsite.gov/x.pdf"
        )
        self.assertEqual(report.head_checks_unverified, 1)
        self.assertEqual(report.head_checks_failed, 0)
        self.assertEqual(report.urls_cleared, [])
        self.assertEqual(len(report.urls_unverified), 1)
        self.assertIn("timeout", report.urls_unverified[0]["reason"])

    def test_connection_error_url_kept_as_unverified(self):
        # F-1: ConnectionError means the server didn't speak HEAD (YouTube's
        # edge rejects HEAD from non-browser User-Agents; was the trigger
        # case). Keep the URL; record as unverified.
        import requests as real_requests
        meeting = {
            "meeting_title": "X", "meeting_date": "2026-06-14",
            "video_url": "https://www.youtube.com/watch?v=abc123",
        }
        resp = _make_response([meeting])
        with patch(
            "haiku_field_sanity.requests.head",
            side_effect=real_requests.ConnectionError("youtube blocked our UA"),
        ):
            sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=False)
        self.assertEqual(
            sanitized["meetings"][0]["video_url"],
            "https://www.youtube.com/watch?v=abc123",
        )
        self.assertEqual(report.head_checks_unverified, 1)
        self.assertEqual(report.urls_cleared, [])
        self.assertIn("connection error", report.urls_unverified[0]["reason"])

    def test_budget_cap_caps_check_count(self):
        # Build enough meetings to exceed the per-invocation cap. Each
        # meeting has 1 URL, so MAX+5 meetings = MAX+5 URLs.
        cap = fs.MAX_HEAD_CHECKS_PER_INVOCATION
        meetings = [
            {"meeting_title": f"M{i}", "agenda_url": f"https://x.gov/{i}.pdf"}
            for i in range(cap + 5)
        ]
        resp = _make_response(meetings)
        mock_resp = type("R", (), {"status_code": 200})()
        # Also patch time.sleep to make the test fast (inter-request delay).
        with patch("haiku_field_sanity.requests.head", return_value=mock_resp), \
             patch("haiku_field_sanity.time.sleep"):
            sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=False)
        self.assertEqual(report.head_checks_attempted, cap)
        self.assertEqual(report.head_checks_skipped_over_budget, 5)


class CaveatsAndImmutabilityTests(unittest.TestCase):
    def test_caveats_appended_when_things_are_cleared(self):
        meeting = {
            "meeting_title": "X", "meeting_date": "6/14/2026",  # bad date
            "meeting_time": "lunchtime",                          # bad time
        }
        resp = _make_response([meeting])
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        caveats_text = " ".join(sanitized["caveats"])
        self.assertIn("non-ISO date", caveats_text)
        self.assertIn("implausible time", caveats_text)

    def test_input_dict_not_mutated(self):
        meeting = {"meeting_title": "X", "meeting_date": "6/14/2026"}
        resp = _make_response([meeting])
        snapshot_meeting_date = resp["meetings"][0]["meeting_date"]
        fs.apply_field_sanity(resp, skip_head_checks=True)
        # Original dict unchanged.
        self.assertEqual(
            resp["meetings"][0]["meeting_date"], snapshot_meeting_date
        )

    def test_no_caveats_appended_when_nothing_cleared(self):
        meeting = {
            "meeting_title": "X", "meeting_date": "2026-06-14",
            "meeting_time": "7:00 PM",
        }
        resp = _make_response([meeting])
        sanitized, report = fs.apply_field_sanity(resp, skip_head_checks=True)
        self.assertEqual(sanitized["caveats"], [])


if __name__ == "__main__":
    unittest.main()

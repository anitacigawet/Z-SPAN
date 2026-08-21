"""S-036 V1-complete — tests for the calendar-class pre-classifier.

Covers the canonical Class A / Class B / unknown verdicts against synthetic
HTML representing each signal combination, plus the vendor-not-supported and
fetch-failure paths. Real-world validation lives in the Maricopa smoke run
(Phoenix / Mesa = Class A; Glendale = Class B), not here — these tests pin
the function's CONTRACT, not its real-world fit.

Run via:
    python3.11 scripts/test_calendar_class_detector.py
or:
    python3.11 -m unittest scripts.test_calendar_class_detector
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import calendar_class_detector as ccd


# Minimal synthetic HTML samples — each isolates one shape the classifier
# should handle, not full Legistar pages (those are too noisy and would
# couple these tests to incidental site structure).

_CLASS_B_HTML = """
<html><body>
<form action="/Calendar.aspx" id="aspnetForm" method="post">
  <input type="hidden" name="__VIEWSTATE" value="..."/>
  <script>function __doPostBack(eventTarget, eventArgument){}</script>
  <table>
    <thead><tr><th>Name</th><th>Date</th></tr></thead>
    <tbody></tbody>
  </table>
  <div class="rgNoRecords">No records were found.</div>
</form>
</body></html>
"""

_CLASS_A_HTML = """
<html><body>
<form action="/Calendar.aspx" id="aspnetForm" method="post">
  <input type="hidden" name="__VIEWSTATE" value="..."/>
  <script>function __doPostBack(eventTarget, eventArgument){}</script>
  <table>
    <thead><tr><th>Name</th><th>Date</th></tr></thead>
    <tbody>
      <tr><td>City Council</td><td>6/2/2026</td><td>7:00 PM</td></tr>
      <tr><td>Planning &amp; Zoning</td><td>6/5/2026</td><td>5:30 PM</td></tr>
      <tr><td>Parks Commission</td><td>6/8/2026</td><td>6:00 PM</td></tr>
      <tr><td>Heritage Commission</td><td>6/10/2026</td><td>4:00 PM</td></tr>
    </tbody>
  </table>
</form>
</body></html>
"""

_NEITHER_HTML = """
<html><body>
<h1>Static city page</h1>
<p>No calendar here, no ASP.NET, no anything to classify.</p>
</body></html>
"""


class ClassificationContractTests(unittest.TestCase):
    """Pin the verdict + signals contract for each canonical shape."""

    def test_class_b_signals_all_present(self):
        result = ccd.detect_calendar_class(_CLASS_B_HTML, is_html=True)
        self.assertEqual(result.calendar_class, "class_b")
        self.assertTrue(result.signals["no_records_text"])
        self.assertTrue(result.signals["postback_controls"])
        self.assertFalse(result.signals["populated_calendar_table"])
        self.assertIn("Class B", result.reasoning)

    def test_class_a_postback_plus_populated_calendar_table(self):
        result = ccd.detect_calendar_class(_CLASS_A_HTML, is_html=True)
        self.assertEqual(result.calendar_class, "class_a")
        self.assertTrue(result.signals["postback_controls"])
        self.assertTrue(result.signals["populated_calendar_table"])
        self.assertFalse(result.signals["no_records_text"])
        self.assertIn("Class A", result.reasoning)

    def test_unknown_when_no_legistar_signals(self):
        result = ccd.detect_calendar_class(_NEITHER_HTML, is_html=True)
        self.assertEqual(result.calendar_class, "unknown")
        self.assertFalse(result.signals["postback_controls"])
        self.assertFalse(result.signals["populated_calendar_table"])
        self.assertFalse(result.signals["no_records_text"])

    def test_unknown_when_vendor_not_supported(self):
        result = ccd.detect_calendar_class(
            "https://example.gov/calendar",
            vendor="granicus",
        )
        self.assertEqual(result.calendar_class, "unknown")
        self.assertEqual(result.vendor, "granicus")
        self.assertFalse(result.signals.get("vendor_supported", True))
        self.assertIn("legistar", result.reasoning.lower())


class FetchFailurePathTests(unittest.TestCase):
    """The classifier shouldn't crash or false-dispatch on network errors."""

    def test_fetch_failure_returns_unknown(self):
        with patch.object(ccd, "fetch_html", return_value=None):
            result = ccd.detect_calendar_class("https://example.gov/calendar")
        self.assertEqual(result.calendar_class, "unknown")
        self.assertTrue(result.signals.get("fetch_failed"))
        self.assertIn("could not fetch", result.reasoning)


class SignalDetectionUnitTests(unittest.TestCase):
    """Direct coverage of the three signal-check helpers."""

    def test_no_records_text_variants(self):
        self.assertTrue(ccd._has_no_records_text("No records were found."))
        self.assertTrue(ccd._has_no_records_text("NO RECORDS WERE FOUND"))
        self.assertTrue(ccd._has_no_records_text("there are no items found here"))
        self.assertFalse(ccd._has_no_records_text("Showing 14 records"))

    def test_postback_controls_requires_both_markers(self):
        self.assertTrue(ccd._has_postback_controls(
            'name="__VIEWSTATE" value="x" __doPostBack()'
        ))
        # Just one of the two is not enough.
        self.assertFalse(ccd._has_postback_controls('__doPostBack()'))
        self.assertFalse(ccd._has_postback_controls('name="__VIEWSTATE"'))
        self.assertFalse(ccd._has_postback_controls("plain HTML"))

    def test_populated_calendar_table_legistar_master_table(self):
        populated_master = """
            <table class="rgMasterTable"><tbody>
              <tr><td>Meeting A</td></tr>
              <tr><td>Meeting B</td></tr>
            </tbody></table>
        """
        self.assertTrue(ccd._has_populated_calendar_table(populated_master))

    def test_populated_calendar_table_legistar_master_table_empty_state(self):
        empty_state_master = """
            <table class="rgMasterTable"><tbody>
              <tr><td>No records were found.</td></tr>
            </tbody></table>
        """
        self.assertFalse(ccd._has_populated_calendar_table(empty_state_master))

    def test_populated_calendar_table_fallback_for_non_legistar(self):
        few_rows = "<table><tbody><tr><td>1</td></tr></tbody></table>"
        many_rows = "<table><tbody>" + "<tr><td>x</td></tr>" * 5 + "</tbody></table>"
        empty_rows = "<table><tbody><tr></tr><tr></tr><tr></tr></tbody></table>"
        self.assertFalse(ccd._has_populated_calendar_table(few_rows))
        self.assertTrue(ccd._has_populated_calendar_table(many_rows))
        self.assertFalse(ccd._has_populated_calendar_table(empty_rows))
        self.assertFalse(ccd._has_populated_calendar_table("<p>no tbody</p>"))


class ArchiveOnlyCandidateTests(unittest.TestCase):
    """F-4 (2026-06-14): latest_meeting_year signal + archive_only_candidate
    flag. The classifier surfaces the signal; downstream consumers decide
    whether to skip extraction."""

    def test_latest_meeting_year_extracted_from_legistar_dates(self):
        # rgMasterTable with two meeting dates; max year should be 2017.
        html = """
        <table class="rgMasterTable">
          <tbody>
            <tr><td>City Council</td><td>6/27/2017</td></tr>
            <tr><td>City Council Workshop</td><td>6/22/2017</td></tr>
            <tr><td>Old Meeting</td><td>1/15/2016</td></tr>
          </tbody>
        </table>
        """
        year = ccd._extract_latest_meeting_year(html)
        self.assertEqual(year, 2017)

    def test_latest_meeting_year_none_when_no_rgmastertable(self):
        html = "<html><body><p>No table here.</p></body></html>"
        year = ccd._extract_latest_meeting_year(html)
        self.assertIsNone(year)

    def test_latest_meeting_year_ignores_out_of_range_numbers(self):
        # 4-digit-looking IDs / building numbers / ZIP codes inside cells
        # shouldn't be confused with date years.
        html = """
        <table class="rgMasterTable">
          <tbody>
            <tr><td>Council #4567</td><td>6/12/2026</td><td>ZIP 85001</td></tr>
          </tbody>
        </table>
        """
        year = ccd._extract_latest_meeting_year(html)
        self.assertEqual(year, 2026)

    def test_archive_only_candidate_set_when_threshold_passed(self):
        # Glendale shape: max year 2017, threshold 2024 (= 2026 - 2).
        html = _CLASS_A_HTML.replace(
            "6/2/2026", "6/2/2017"
        ).replace(
            "6/5/2026", "6/5/2017"
        ).replace(
            "6/8/2026", "6/8/2017"
        ).replace(
            "6/10/2026", "6/10/2017"
        ).replace(
            "<table>",
            "<table class='rgMasterTable'>"
        )
        result = ccd.detect_calendar_class(
            html, is_html=True, archive_threshold_year=2024
        )
        self.assertTrue(result.archive_only_candidate)
        self.assertEqual(result.latest_meeting_year, 2017)
        self.assertIn("ARCHIVE-ONLY CANDIDATE", result.reasoning)
        self.assertIn("2017", result.reasoning)

    def test_archive_only_candidate_NOT_set_when_threshold_omitted(self):
        # Default behavior: no threshold passed -> never flagged as archive.
        html = _CLASS_A_HTML.replace("<table>", "<table class='rgMasterTable'>")
        result = ccd.detect_calendar_class(html, is_html=True)
        self.assertFalse(result.archive_only_candidate)
        # latest_meeting_year still surfaces for downstream use.
        self.assertEqual(result.latest_meeting_year, 2026)

    def test_archive_only_candidate_not_set_when_current_year(self):
        # Threshold passed but current meetings present -> not flagged.
        html = _CLASS_A_HTML.replace("<table>", "<table class='rgMasterTable'>")
        result = ccd.detect_calendar_class(
            html, is_html=True, archive_threshold_year=2024
        )
        self.assertFalse(result.archive_only_candidate)
        self.assertEqual(result.latest_meeting_year, 2026)


if __name__ == "__main__":
    unittest.main()

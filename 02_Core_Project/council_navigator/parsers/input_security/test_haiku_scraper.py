"""S-008 V0 / surface S-4 — Haiku-class HTML scraper URL safety + tagging.

Exercises:
- `assert_haiku_url_safe` rejects javascript:, data:, file:, ftp:,
  vbscript: schemes; rejects empty URLs; rejects URLs with structural
  fence markers or bidi controls; rejects oversize URLs.
- `assert_haiku_url_safe` accepts canonical https:// + http:// URLs.
- `tag_meetings_haiku_fallback` stamps scraper_source on every meeting
  dict but preserves caller-provided scraper_source if already set.

Per [D-100](../../../../01_Project_Overview/DECISIONS.md#d-100), the test
inputs use known-bad URL shapes AS NEGATIVE-TEST CASES.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_haiku_module() -> ModuleType:
    path = _SCRIPTS_DIR / "haiku_html_scrape.py"
    if not path.exists():
        raise unittest.SkipTest(f"{path} not found")
    spec = importlib.util.spec_from_file_location(
        "_zspan_haiku_html_scrape_under_test", path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("spec build failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_zspan_haiku_html_scrape_under_test"] = module
    spec.loader.exec_module(module)
    return module


class UrlSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_haiku_module()

    def test_https_url_accepted(self):
        self.mod.assert_haiku_url_safe(
            "https://kingmancity.gov/agenda"
        )

    def test_http_url_accepted(self):
        # http allowed for legacy municipal sites; the test confirms we
        # don't accidentally hard-fail on http.
        self.mod.assert_haiku_url_safe(
            "http://oldcity.gov/calendar"
        )

    def test_empty_url_rejected(self):
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe("")
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe("   ")

    def test_javascript_uri_rejected(self):
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe("javascript:alert(1)")

    def test_data_uri_rejected(self):
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe(
                "data:text/html,<script>alert(1)</script>"
            )

    def test_file_uri_rejected(self):
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe("file:///etc/passwd")

    def test_ftp_uri_rejected(self):
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe("ftp://kingmancity.gov/file")

    def test_no_scheme_rejected(self):
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe("kingmancity.gov/agenda")

    def test_oversize_url_rejected(self):
        oversize = "https://kingmancity.gov/" + "x" * 10_000
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe(oversize)

    def test_fence_marker_in_url_rejected(self):
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe(
                "https://kingmancity.gov/<zspan-content-begin nonce=\"x\">"
            )

    def test_bidi_control_in_url_rejected(self):
        with self.assertRaises(self.mod.HaikuUrlSafetyError):
            self.mod.assert_haiku_url_safe(
                "https://kingmancity.gov/path‮reversed"
            )


class TaggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_haiku_module()

    def test_tag_added_when_missing(self):
        meetings = [
            {"meeting_title": "Council Meeting", "meeting_date": "2026-06-02"},
            {"meeting_title": "P&Z", "meeting_date": "2026-06-05"},
        ]
        out = self.mod.tag_meetings_haiku_fallback(meetings)
        self.assertEqual(out[0]["scraper_source"], "haiku_fallback")
        self.assertEqual(out[1]["scraper_source"], "haiku_fallback")

    def test_existing_scraper_source_preserved(self):
        meetings = [
            {
                "meeting_title": "Council Meeting",
                "scraper_source": "deterministic_parser",
            },
        ]
        out = self.mod.tag_meetings_haiku_fallback(meetings)
        # setdefault preserves prior value.
        self.assertEqual(out[0]["scraper_source"], "deterministic_parser")

    def test_does_not_mutate_input(self):
        meetings = [{"meeting_title": "X"}]
        snapshot = [dict(m) for m in meetings]
        self.mod.tag_meetings_haiku_fallback(meetings)
        self.assertEqual(meetings, snapshot)

    def test_non_dict_items_pass_through(self):
        meetings = [{"meeting_title": "X"}, "not a dict", None]
        out = self.mod.tag_meetings_haiku_fallback(meetings)
        self.assertEqual(out[0]["scraper_source"], "haiku_fallback")
        self.assertEqual(out[1], "not a dict")
        self.assertIsNone(out[2])

    def test_empty_list_safe(self):
        self.assertEqual(
            self.mod.tag_meetings_haiku_fallback([]),
            [],
        )


class PreRenderedPromptTests(unittest.TestCase):
    """S-036 V1-complete Class-B (pre-rendered HTML) prompt construction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_haiku_module()

    def test_pre_rendered_prompt_embeds_html_and_metadata(self):
        html = "<table class='rgMasterTable'><tbody><tr><td>X</td></tr></tbody></table>"
        url = "https://glendale-az.legistar.com/Calendar.aspx"
        prompt = self.mod.build_prompt_pre_rendered("Glendale", url, html)
        self.assertIn("Glendale", prompt)
        self.assertIn(url, prompt)
        self.assertIn(html, prompt)
        self.assertIn("PRE-RENDERED HTML BEGINS", prompt)
        self.assertIn("PRE-RENDERED HTML ENDS", prompt)
        # Tells the agent NOT to WebFetch on this path.
        self.assertIn("Do NOT WebFetch", prompt)

    def test_webfetch_prompt_does_not_include_pre_rendered_markers(self):
        prompt = self.mod.build_prompt(
            "Phoenix", "https://phoenix.legistar.com/Calendar.aspx"
        )
        # The Class-A prompt should NOT carry the pre-rendered framing.
        self.assertNotIn("PRE-RENDERED", prompt)
        self.assertIn("WebFetch the URL", prompt)


class LoadPreRenderedHtmlTests(unittest.TestCase):
    """Sanity gates on the --html-file path."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_haiku_module()

    def test_missing_file_raises(self):
        from pathlib import Path
        with self.assertRaises(ValueError) as ctx:
            self.mod.load_pre_rendered_html(Path("/tmp/zspan_definitely_does_not_exist.html"))
        self.assertIn("not found", str(ctx.exception))

    def test_empty_file_raises(self):
        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as f:
            f.write("")
            path = Path(f.name)
        try:
            with self.assertRaises(ValueError) as ctx:
                self.mod.load_pre_rendered_html(path)
            self.assertIn("empty", str(ctx.exception))
        finally:
            path.unlink(missing_ok=True)

    def test_oversized_file_raises(self):
        import tempfile
        from pathlib import Path
        cap = self.mod.MAX_HTML_FILE_BYTES
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write("x" * (cap + 1024))
            path = Path(f.name)
        try:
            with self.assertRaises(ValueError) as ctx:
                self.mod.load_pre_rendered_html(path)
            self.assertIn("exceeds", str(ctx.exception))
        finally:
            path.unlink(missing_ok=True)

    def test_valid_file_returns_content(self):
        import tempfile
        from pathlib import Path
        content = "<html><body><h1>Test</h1></body></html>"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            path = Path(f.name)
        try:
            self.assertEqual(self.mod.load_pre_rendered_html(path), content)
        finally:
            path.unlink(missing_ok=True)


class ExtractMeetingTableSubtreeTests(unittest.TestCase):
    """F-2 (2026-06-14): pre-extract the meeting-table subtree from rendered
    HTML so the embedded prompt fits Haiku's 200K-token context window and
    the Mac relay's prompt cap."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_haiku_module()

    def test_legistar_rgmastertable_matches_and_shrinks(self):
        # Simulate a Legistar page with a rgMasterTable surrounded by a lot of
        # Telerik UI markup. Pre-extraction should pull just the table.
        outer_noise = "<div class='telerik-junk'>" + ("x" * 5000) + "</div>"
        master = """<table class="rgMasterTable">
<tbody>
<tr><td>Meeting A</td><td>2026-06-01</td></tr>
<tr><td>Meeting B</td><td>2026-06-08</td></tr>
</tbody>
</table>"""
        html = f"<html><head>{outer_noise}</head><body>{outer_noise}{master}{outer_noise}</body></html>"
        extracted, vendor = self.mod.extract_meeting_table_subtree(html)
        self.assertIn("rgMasterTable", vendor)
        self.assertIn("Meeting A", extracted)
        self.assertIn("Meeting B", extracted)
        # Substantially smaller than the input.
        self.assertLess(len(extracted), len(html) // 2)

    def test_no_known_selector_falls_back_to_raw_html(self):
        # An HTML page with no rgMasterTable (or any known meeting-table
        # signature) should return the original HTML untouched with
        # vendor=None.
        html = "<html><body><div>just a homepage</div></body></html>"
        extracted, vendor = self.mod.extract_meeting_table_subtree(html)
        self.assertEqual(extracted, html)
        self.assertIsNone(vendor)

    def test_handles_malformed_html(self):
        # BeautifulSoup is lenient; ensure no crash on broken markup.
        broken = "<html><body><table class='rgMasterTable'><tbody><tr>"
        extracted, vendor = self.mod.extract_meeting_table_subtree(broken)
        # Either the partial table was extracted (vendor set) OR fell back
        # to raw HTML (vendor None) — both are acceptable; the contract is
        # "don't crash."
        self.assertIsNotNone(extracted)


class ArchiveOnlyCandidateTests(unittest.TestCase):
    """F-4b (2026-06-14): wrapper-side `compute_archive_only_candidate` that
    runs the same archive-threshold check as the F-4a classifier against
    Haiku's extracted `meeting_date` list. The wrapper-side check exists
    specifically because Class-B pages (Glendale-class) have an empty static
    default view — the classifier can't read years from a blank page, so
    F-4b runs post-extraction where the rendered dates surface."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_haiku_module()

    def test_latest_year_extracted_from_iso_meeting_dates(self):
        meetings = [
            {"meeting_title": "Council", "meeting_date": "2017-06-27"},
            {"meeting_title": "Workshop", "meeting_date": "2017-06-22"},
            {"meeting_title": "Old", "meeting_date": "2016-01-15"},
        ]
        latest, archive = self.mod.compute_archive_only_candidate(meetings)
        self.assertEqual(latest, 2017)
        # No threshold supplied -> never flagged.
        self.assertFalse(archive)

    def test_latest_year_none_when_no_parseable_dates(self):
        meetings = [
            {"meeting_title": "Council", "meeting_date": ""},
            {"meeting_title": "Workshop", "meeting_date": "TBD"},
            {"meeting_title": "Bad", "meeting_date": "6/15/2017"},  # non-ISO
        ]
        latest, archive = self.mod.compute_archive_only_candidate(meetings)
        self.assertIsNone(latest)
        self.assertFalse(archive)

    def test_out_of_range_years_ignored(self):
        meetings = [
            {"meeting_title": "Modern", "meeting_date": "2026-06-12"},
            # Date-shaped junk that would otherwise pull the max year up/down.
            {"meeting_title": "Future", "meeting_date": "3001-01-01"},
            {"meeting_title": "Ancient", "meeting_date": "1066-10-14"},
        ]
        latest, _ = self.mod.compute_archive_only_candidate(meetings)
        self.assertEqual(latest, 2026)

    def test_archive_only_flag_set_when_latest_below_threshold(self):
        # Glendale shape: extracted dates are all 2017; operator threshold
        # current_year - ARCHIVE_AGE_THRESHOLD_YEARS = 2024.
        meetings = [
            {"meeting_title": "Council", "meeting_date": "2017-06-27"},
            {"meeting_title": "Workshop", "meeting_date": "2017-06-22"},
        ]
        latest, archive = self.mod.compute_archive_only_candidate(
            meetings, threshold_year=2024,
        )
        self.assertEqual(latest, 2017)
        self.assertTrue(archive)

    def test_archive_only_flag_NOT_set_when_threshold_omitted(self):
        # Even with old dates, no threshold = no flag (caller owns the call).
        meetings = [
            {"meeting_title": "Council", "meeting_date": "2017-06-27"},
        ]
        latest, archive = self.mod.compute_archive_only_candidate(meetings)
        self.assertEqual(latest, 2017)
        self.assertFalse(archive)

    def test_archive_only_flag_not_set_when_current_meetings_present(self):
        # Live city shape: a single recent meeting keeps the flag off even
        # if archival dates are also present — the threshold checks max, not min.
        meetings = [
            {"meeting_title": "Council", "meeting_date": "2026-06-12"},
            {"meeting_title": "Archive", "meeting_date": "2017-06-27"},
        ]
        latest, archive = self.mod.compute_archive_only_candidate(
            meetings, threshold_year=2024,
        )
        self.assertEqual(latest, 2026)
        self.assertFalse(archive)


if __name__ == "__main__":
    unittest.main()

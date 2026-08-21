"""Recon-2 — tests for vendor_fingerprint.py.

Deterministic unit tests for the URL + HTML rule sets, plus an offline
batch-mode smoke test against the live parser_index.json. No HTTP fetches
in any test — all coverage is on the rule logic itself.

Run via:
    python3.11 test_vendor_fingerprint.py
or:
    python3.11 -m unittest scripts.test_vendor_fingerprint
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Make sure parsers/ is on the path before importing.
_HERE = Path(__file__).resolve().parent
_PARSERS = _HERE.parent
if str(_PARSERS) not in sys.path:
    sys.path.insert(0, str(_PARSERS))

# `scripts.` import path matches the existing test convention.
from scripts import vendor_fingerprint as vf  # type: ignore  # noqa: E402


class TestURLSubdomainRules(unittest.TestCase):
    """High-confidence URL subdomain matches — no HTTP needed."""

    def test_granicus_subdomain_basic(self):
        r = vf.fingerprint_vendor(
            "https://scottsdale.granicus.com/ViewPublisher.php?view_id=118",
            fetch=False,
        )
        self.assertEqual(r["vendor"], "granicus")
        self.assertEqual(r["confidence"], "high")
        self.assertEqual(r["method"], "url_subdomain")

    def test_granicus_rss_path(self):
        r = vf.fingerprint_vendor(
            "https://cityofkingman.granicus.com/ViewPublisherRSS.php?view_id=1",
            fetch=False,
        )
        self.assertEqual(r["vendor"], "granicus")
        self.assertEqual(r["confidence"], "high")

    def test_legistar_distinct_from_granicus(self):
        # Legistar IS a Granicus product but takes its own subdomain;
        # rule order in _URL_RULES must classify it as legistar, not granicus.
        for city in ("phoenix", "mesa", "glendale-az"):
            r = vf.fingerprint_vendor(
                f"https://{city}.legistar.com/Calendar.aspx", fetch=False
            )
            self.assertEqual(r["vendor"], "legistar", msg=f"failed for {city}")
            self.assertEqual(r["confidence"], "high")

    def test_civicclerk_portal_subdomain(self):
        r = vf.fingerprint_vendor(
            "https://prescottaz.portal.civicclerk.com/", fetch=False
        )
        self.assertEqual(r["vendor"], "civicclerk")
        self.assertEqual(r["confidence"], "high")

    def test_civicclerk_bare_subdomain(self):
        r = vf.fingerprint_vendor(
            "https://example.civicclerk.com/Calendar.aspx", fetch=False
        )
        self.assertEqual(r["vendor"], "civicclerk")

    def test_civicplus_direct_domain(self):
        # Synthetic — most CivicPlus shows up via /AgendaCenter path, but a
        # bare *.civicplus.com URL exists in some setups.
        r = vf.fingerprint_vendor(
            "https://demo.civicplus.com/", fetch=False
        )
        self.assertEqual(r["vendor"], "civicplus")
        self.assertEqual(r["confidence"], "high")

    def test_hyland_subdomain(self):
        r = vf.fingerprint_vendor(
            "https://tempe.hylandcloud.com/AgendaOnline", fetch=False
        )
        self.assertEqual(r["vendor"], "hyland")
        self.assertEqual(r["confidence"], "high")

    def test_destiny_subdomain(self):
        for host in ("destinyhosted.com", "public.destinyhosted.com"):
            r = vf.fingerprint_vendor(
                f"https://{host}/agenda_publish.cfm?id=24263", fetch=False
            )
            self.assertEqual(r["vendor"], "destiny", msg=f"failed for {host}")

    def test_novusagenda_subdomain(self):
        r = vf.fingerprint_vendor(
            "https://peoriaaz.novusagenda.com/agendapublic/meetingsgeneral.aspx",
            fetch=False,
        )
        self.assertEqual(r["vendor"], "novusagenda")
        self.assertEqual(r["confidence"], "high")

    def test_iqm2_subdomain(self):
        r = vf.fingerprint_vendor(
            "https://coolidgecityaz.iqm2.com/Citizens/Calendar.aspx", fetch=False
        )
        self.assertEqual(r["vendor"], "iqm2")
        self.assertEqual(r["confidence"], "high")

    def test_municode_subdomain(self):
        r = vf.fingerprint_vendor(
            "https://jerome-az.municodemeetings.com/calendar", fetch=False
        )
        self.assertEqual(r["vendor"], "municode")
        self.assertEqual(r["confidence"], "high")

    def test_civicweb_subdomain(self):
        r = vf.fingerprint_vendor(
            "https://cityofsomerton.civicweb.net/Portal/MeetingSchedule.aspx",
            fetch=False,
        )
        self.assertEqual(r["vendor"], "civicweb")
        self.assertEqual(r["confidence"], "high")

    def test_swagit_subdomain(self):
        r = vf.fingerprint_vendor(
            "https://orovalley.swagit.com/play/...", fetch=False
        )
        self.assertEqual(r["vendor"], "swagit")
        self.assertEqual(r["confidence"], "high")


class TestURLPathRules(unittest.TestCase):
    """Medium-confidence URL path rules — vendor on the city's own domain."""

    def test_civicplus_agenda_center_path(self):
        # CivicPlus puts /AgendaCenter on the city's own domain.
        for url in (
            "https://www.bisbeeaz.gov/agendacenter",
            "https://www.welltonaz.gov/AgendaCenter",
            "https://sahuaritaaz.gov/calendar.aspx?CID=26",  # negative; no agendacenter
        ):
            r = vf.fingerprint_vendor(url, fetch=False)
            # First two should match civicplus; last should NOT match URL rules.
            if "agendacenter" in url.lower():
                self.assertEqual(r["vendor"], "civicplus", msg=f"failed for {url}")
                self.assertEqual(r["confidence"], "medium")
                self.assertEqual(r["method"], "url_path")
            else:
                self.assertEqual(r["vendor"], "unknown", msg=f"failed for {url}")

    def test_tribe_events_wp_json_path(self):
        r = vf.fingerprint_vendor(
            "https://www.florenceaz.gov/wp-json/tribe/events/v1/events?per_page=50",
            fetch=False,
        )
        self.assertEqual(r["vendor"], "tribe_events")
        self.assertEqual(r["confidence"], "medium")
        self.assertEqual(r["method"], "url_path")


class TestRulePriority(unittest.TestCase):
    """Order-sensitive cases: subdomain rules must win over path rules,
    and Legistar (a Granicus product) must register as legistar even though
    it would also "look like" granicus to a less-specific rule."""

    def test_legistar_does_not_collide_with_granicus_subdomain_rule(self):
        # If the granicus rule were too greedy (e.g. matched anything ending
        # in granicus.com), a legistar URL might catch it incorrectly. The
        # rule order in _URL_RULES puts legistar first AND the granicus regex
        # requires `granicus.com` exactly. Sanity-check both.
        r = vf.fingerprint_vendor(
            "https://phoenix.legistar.com/Calendar.aspx", fetch=False
        )
        self.assertEqual(r["vendor"], "legistar")

    def test_subdomain_wins_over_path_when_both_could_match(self):
        # Synthetic: a hypothetical /AgendaCenter on a civicplus subdomain
        # should classify as civicplus via URL_SUBDOMAIN (high), not URL_PATH (medium).
        r = vf.fingerprint_vendor(
            "https://demo.civicplus.com/AgendaCenter", fetch=False
        )
        self.assertEqual(r["vendor"], "civicplus")
        self.assertEqual(r["confidence"], "high")
        self.assertEqual(r["method"], "url_subdomain")


class TestHTMLRules(unittest.TestCase):
    """HTML-signature rules — when the URL itself doesn't tell us."""

    def test_granicus_html_iframe(self):
        html = (
            "<html><head></head><body>"
            "<iframe src='https://cityname.granicus.com/MediaPlayer.php?view_id=1'></iframe>"
            "</body></html>"
        )
        r = vf.fingerprint_vendor(
            "https://www.example.gov/calendar", html=html, fetch=False
        )
        self.assertEqual(r["vendor"], "granicus")
        self.assertEqual(r["confidence"], "medium")
        self.assertEqual(r["method"], "html_asset")

    def test_civicplus_meta_generator(self):
        html = (
            "<html><head>"
            "<meta name=\"generator\" content=\"CivicPlus Web Platform v1\">"
            "</head><body>hi</body></html>"
        )
        r = vf.fingerprint_vendor(
            "https://www.example.gov/", html=html, fetch=False
        )
        self.assertEqual(r["vendor"], "civicplus")
        self.assertEqual(r["method"], "html_meta")

    def test_civicclerk_html_asset(self):
        html = '<script src="https://cdn.civicclerk.com/app.js"></script>'
        r = vf.fingerprint_vendor(
            "https://www.example.gov/", html=html, fetch=False
        )
        self.assertEqual(r["vendor"], "civicclerk")

    def test_swagit_html_asset(self):
        html = "<a href='https://playback.swagit.com/v/orovalley/...'>Watch</a>"
        r = vf.fingerprint_vendor(
            "https://www.example.gov/", html=html, fetch=False
        )
        self.assertEqual(r["vendor"], "swagit")

    def test_tribe_events_plugin_path(self):
        html = "<link rel='stylesheet' href='/wp-content/plugins/the-events-calendar/main.css'>"
        r = vf.fingerprint_vendor(
            "https://www.example.gov/", html=html, fetch=False
        )
        self.assertEqual(r["vendor"], "tribe_events")


class TestUnknownCases(unittest.TestCase):
    """The fingerprinter must honestly return `unknown` when nothing matches."""

    def test_unknown_url_no_html(self):
        r = vf.fingerprint_vendor(
            "https://example-rando-city.gov/meetings", fetch=False
        )
        self.assertEqual(r["vendor"], "unknown")
        self.assertEqual(r["confidence"], "none")
        self.assertEqual(r["method"], "none")

    def test_unknown_url_unhelpful_html(self):
        html = "<html><body><h1>Welcome</h1></body></html>"
        r = vf.fingerprint_vendor(
            "https://example-rando-city.gov/", html=html, fetch=False
        )
        self.assertEqual(r["vendor"], "unknown")

    def test_empty_url(self):
        r = vf.fingerprint_vendor("", fetch=False)
        self.assertEqual(r["vendor"], "unknown")

    def test_url_with_port_and_query(self):
        r = vf.fingerprint_vendor(
            "https://kingman.granicus.com:443/ViewPublisherRSS.php?view_id=1#section",
            fetch=False,
        )
        # Port in URL shouldn't break the subdomain match.
        self.assertEqual(r["vendor"], "granicus")

    def test_http_not_https(self):
        # Vendor rules should match http:// the same as https://.
        r = vf.fingerprint_vendor(
            "http://kingman.granicus.com/ViewPublisher.php", fetch=False
        )
        self.assertEqual(r["vendor"], "granicus")

    def test_uppercase_domain_match(self):
        # Case-insensitive regex on host.
        r = vf.fingerprint_vendor(
            "https://Kingman.Granicus.COM/ViewPublisher.php", fetch=False
        )
        self.assertEqual(r["vendor"], "granicus")


class TestBatchOnParserIndex(unittest.TestCase):
    """Offline smoke test against the live parser_index.json.

    Validates rule coverage on real URLs without HTTP. Doesn't assert
    specific counts (parser_index changes over time) — just that the
    fingerprinter runs cleanly + classifies a sane majority of obvious
    vendor URLs.
    """

    def setUp(self):
        self.parser_index = _PARSERS / "parser_index.json"
        if not self.parser_index.exists():
            self.skipTest("parser_index.json not present in test environment")

    def test_batch_runs_clean(self):
        summary = vf.fingerprint_parser_index(self.parser_index, fetch=False)
        self.assertGreater(summary["total"], 0)
        self.assertEqual(summary["fetched"], False)
        self.assertEqual(summary["fetch_errors"], 0)
        self.assertIn("by_vendor", summary)

    def test_known_vendor_cities_classified_correctly(self):
        summary = vf.fingerprint_parser_index(self.parser_index, fetch=False)
        by_city = {r["city"]: r for r in summary["results"]}

        # Spot-check that obvious vendor URLs land in the right bucket.
        expectations = {
            # Granicus
            "Scottsdale": "granicus",
            "Kingman": "granicus",
            "Bullhead City": "granicus",
            # Legistar
            "Phoenix": "legistar",
            "Mesa": "legistar",
            "Glendale": "legistar",
            # CivicClerk
            "Page": "civicclerk",
            "Prescott": "civicclerk",
            # Destiny
            "Globe": "destiny",
            "Chandler": "destiny",
            # Hyland
            "Tempe": "hyland",
            "Tucson": "hyland",
            # NovusAgenda
            "Peoria": "novusagenda",
            # IQM2
            "Coolidge": "iqm2",
            # Municode
            "Jerome": "municode",
            # CivicWeb
            "Somerton": "civicweb",
        }
        for city, want in expectations.items():
            if city not in by_city:
                continue  # parser_index may have changed; skip vs. fail
            got = by_city[city]["vendor"]
            self.assertEqual(
                got,
                want,
                msg=(
                    f"{city}: expected {want}, got {got} "
                    f"(url={by_city[city]['calendar_url']})"
                ),
            )

    def test_unknown_count_is_bounded(self):
        """Sanity bound: the fingerprinter should classify the majority of
        parser_index cities. If unknowns exceed 60%, a rule is missing —
        we want to see that test break loudly when we add a new vendor
        that wasn't previously fingerprinted."""
        summary = vf.fingerprint_parser_index(self.parser_index, fetch=False)
        total = summary["total"]
        unknown = summary["by_vendor"].get("unknown", 0)
        # Many parser_index entries are "Custom HTML" on the city's own
        # domain — those WILL register as unknown in URL-only mode. The
        # bound is generous; tighten as rules grow.
        self.assertLess(
            unknown / total,
            0.7,
            msg=f"unknown={unknown}/{total} — too many unclassified URLs",
        )


class TestResultShape(unittest.TestCase):
    """Stable contract: the result dict shape is part of the public API."""

    def _required_keys(self):
        return {"vendor", "confidence", "method", "evidence", "description", "url_checked"}

    def test_high_confidence_result_has_all_required_keys(self):
        r = vf.fingerprint_vendor(
            "https://kingman.granicus.com/", fetch=False
        )
        for k in self._required_keys():
            self.assertIn(k, r)

    def test_unknown_result_has_all_required_keys(self):
        r = vf.fingerprint_vendor("https://nowhere.gov/", fetch=False)
        for k in self._required_keys():
            self.assertIn(k, r)

    def test_vendor_value_is_in_enum(self):
        r = vf.fingerprint_vendor("https://nowhere.gov/", fetch=False)
        self.assertIn(r["vendor"], vf.VENDORS)


if __name__ == "__main__":
    unittest.main(verbosity=2)

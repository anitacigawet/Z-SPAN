"""Session-103 (product-slice3a) — deterministic topic-matcher tests.

Sol Round-2 verdict: use case-fold whole-token matching over
title/tagline/key_decisions ONLY. A passing "data center" mention
in a broader budget synopsis must NOT admit the tag; a title like
"Hyperscaler Zoning Special Session" MUST admit it.

These tests pin the precision contract that decides whether topic
notifications are trustworthy in v1.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
if str(_COUNCIL_NAVIGATOR_DIR) not in sys.path:
    sys.path.insert(0, str(_COUNCIL_NAVIGATOR_DIR))

from parsers.topic_matcher import (
    FIELD_KEY_DECISION,
    FIELD_TAGLINE,
    FIELD_TITLE,
    match_meeting,
)
from parsers.topic_tags import MATCHER_VERSION


def _tags_only(matches):
    return {m.tag_id for m in matches}


class DataCentersPrecision(unittest.TestCase):
    """The sol Round-1 worked distinction: hyperscaler title fires,
    passing budget-synopsis mention does not (because synopsis isn't
    scanned)."""

    def test_hyperscaler_zoning_title_admits(self):
        matches = match_meeting(
            meeting_title="Special Meeting on Hyperscaler Zoning",
            episode_tagline=None,
            key_decisions=[],
        )
        self.assertEqual(_tags_only(matches), {"data_centers"})
        self.assertEqual(matches[0].evidence_field, FIELD_TITLE)
        self.assertEqual(matches[0].trigger_phrase, "hyperscaler")

    def test_data_center_in_key_decision_admits(self):
        matches = match_meeting(
            meeting_title="Regular Meeting",
            episode_tagline=None,
            key_decisions=[
                "Approved rezoning of parcel 43 for a data center campus",
            ],
        )
        self.assertEqual(_tags_only(matches), {"data_centers"})
        self.assertEqual(matches[0].evidence_field, FIELD_KEY_DECISION)

    def test_synopsis_only_mention_does_NOT_fire(self):
        # Synopsis is not a scanned field. This is the load-bearing
        # sol Round-2 cold-start precision rule.
        matches = match_meeting(
            meeting_title="Regular Council Meeting",
            episode_tagline=None,
            key_decisions=[
                "Adopted the fiscal-year budget with amendments.",
                "Confirmed the interim police chief appointment.",
            ],
        )
        # Neither decision mentions data-center terminology.
        self.assertEqual(_tags_only(matches), set())

    def test_field_priority_title_beats_decision(self):
        matches = match_meeting(
            meeting_title="Hyperscaler Zoning Special Session",
            episode_tagline=None,
            key_decisions=[
                "Approved the data center incentive package.",
            ],
        )
        # Two fields both match — title wins per priority order.
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].evidence_field, FIELD_TITLE)


class WholeTokenMatching(unittest.TestCase):
    def test_bare_word_water_does_NOT_match_water_rights(self):
        matches = match_meeting(
            meeting_title="Water Utility Rate Review",
            episode_tagline=None,
            key_decisions=[],
        )
        # "water rights" is the trigger; "water utility" doesn't hit.
        self.assertEqual(_tags_only(matches), set())

    def test_lgbtq_recognition_admits(self):
        matches = match_meeting(
            meeting_title="Regular Meeting",
            episode_tagline="Council votes on LGBTQ recognition proclamation",
            key_decisions=[],
        )
        self.assertEqual(_tags_only(matches), {"lgbtq"})
        self.assertEqual(matches[0].evidence_field, FIELD_TAGLINE)

    def test_case_and_hyphen_normalization(self):
        matches = match_meeting(
            meeting_title="Data-Center Zoning Amendments",
            episode_tagline=None,
            key_decisions=[],
        )
        self.assertEqual(_tags_only(matches), {"data_centers"})

    def test_terminal_plural_admits(self):
        # "data center" trigger should match "data centers" title.
        matches = match_meeting(
            meeting_title="Regulation of Data Centers",
            episode_tagline=None,
            key_decisions=[],
        )
        self.assertEqual(_tags_only(matches), {"data_centers"})

    def test_substring_hit_does_NOT_fire(self):
        # "tax" as a substring inside "contact" must not fire (nothing
        # in the vocab uses this shape, but the guarantee matters).
        matches = match_meeting(
            meeting_title="Contact Report — Community Outreach",
            episode_tagline=None,
            key_decisions=["Public comment: budget concerns"],
        )
        self.assertEqual(_tags_only(matches), set())


class EmptyAndColdStart(unittest.TestCase):
    def test_empty_inputs_yield_empty_matches(self):
        self.assertEqual(
            match_meeting(
                meeting_title=None, episode_tagline=None, key_decisions=None
            ),
            [],
        )

    def test_generic_meeting_yields_empty_NOT_other(self):
        # sol Round-2 cold-start rule: unmatched meeting has ZERO
        # topic-match rows, not a fallback `other` tag. Sending a
        # "we're not sure why" email erodes trust.
        matches = match_meeting(
            meeting_title="Regular Council Meeting",
            episode_tagline="Council approves budget amendments and hears reports.",
            key_decisions=[
                "Adopted the fiscal-year budget.",
                "Confirmed the interim police chief.",
                "Accepted the quarterly financial report.",
            ],
        )
        self.assertEqual(matches, [])


class MultiTagAdmission(unittest.TestCase):
    def test_meeting_can_carry_multiple_tags(self):
        matches = match_meeting(
            meeting_title="Special Session: Hyperscaler Zoning + LGBTQ Proclamation",
            episode_tagline=None,
            key_decisions=[],
        )
        self.assertEqual(_tags_only(matches), {"data_centers", "lgbtq"})


class EvidenceIntegrity(unittest.TestCase):
    def test_matcher_version_stamped_on_every_match(self):
        matches = match_meeting(
            meeting_title="Hyperscaler Zoning",
            episode_tagline=None,
            key_decisions=[],
        )
        self.assertEqual(matches[0].matcher_version, MATCHER_VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)

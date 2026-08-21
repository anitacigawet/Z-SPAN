"""
topic_tags — controlled vocabulary for Cast-page quote filtering.

Per James 2026-05-11 (T-007 V1), the Cast page surfaces ONLY these five
topic categories as filter buttons. Extracted quotes can
still carry richer tags in the `member_quotes.topic_tags` column; the
list here defines what shows up on the user-facing UI.

MUST stay in sync with `client/src/utils/topicTags.ts`. When updating
this list, update the TS file too — `pnpm check` won't catch drift
because they're independent literal lists.
"""

from __future__ import annotations

# (tag_id, display_label, hint_for_classifier)
TOPIC_TAGS: tuple[tuple[str, str, str], ...] = (
    (
        "data_centers",
        "Data Centers",
        "Quotes about data-center proposals, hyperscaler expansion, the "
        "water/power demands of such facilities, related zoning or "
        "incentive votes.",
    ),
    (
        "water_rights",
        "Water Rights",
        "Quotes about Colorado River allocations, well permits, "
        "groundwater conservation, drought response, water-supply "
        "infrastructure.",
    ),
    (
        "diversity_inclusion",
        "Diversity & Inclusion",
        "Quotes about DEI policy, civic-access programs, language "
        "services, accessibility accommodations, equity in city services.",
    ),
    (
        "lgbtq",
        "LGBTQ",
        "Quotes about LGBTQ-related ordinances, official recognition or "
        "proclamations, public-comment exchanges on LGBTQ topics.",
    ),
    (
        "education",
        "Education",
        "Quotes about school-board liaison items, library funding or "
        "policy, civic-education partnerships, after-school programs.",
    ),
)

TOPIC_TAG_IDS: tuple[str, ...] = tuple(t[0] for t in TOPIC_TAGS)

# Convenience lookup — {tag_id: display_label} — so consumers stop
# rebuilding the (tag_id, label) tuple every place they need the
# human-readable name (see database.py:_TRUTH_BOOK_FEATURED_LANES,
# which had drifted to a THIRD literal copy of the vocab before sol's
# 2026-07-30 audit caught it; that consumer now derives its lane list
# from TOPIC_TAGS instead of hand-copying).
TOPIC_LABELS: dict[str, str] = {tag_id: label for tag_id, label, _ in TOPIC_TAGS}

# Session-103 (product-slice3a) — deterministic keyword-matcher trigger
# vocabulary per sol Round-2 draft. Every phrase is a whole-token or
# whole-phrase trigger (the matcher normalizes case and hyphen/slash to
# space + collapses whitespace + supports ordinary terminal plurals).
# Deliberately conservative: single-word generics like "water", "power",
# "zoning", "equity", "school", "library", "proclamation", "public
# comment" are excluded because they fire on non-topical meetings
# (email trust is precision-over-recall for tonight's v1 loop).
#
# The matcher scans meeting_title, episode_tagline, key_decisions — NOT
# synopsis — so a passing "data center" mention in a broader budget
# synopsis does NOT admit the tag; a title like "Hyperscaler Zoning
# Special Session" does. See parsers/topic_matcher.py.
TOPIC_TRIGGERS: dict[str, tuple[str, ...]] = {
    "data_centers": (
        "data center",
        "data center campus",
        "data center incentive",
        "hyperscaler",
        "server farm",
        "gpu cluster",
    ),
    "water_rights": (
        "water rights",
        "colorado river allocation",
        "well permit",
        "groundwater conservation",
        "drought response",
        "water supply infrastructure",
    ),
    "diversity_inclusion": (
        "diversity and inclusion",
        "dei policy",
        "civic access program",
        "language service",
        "accessibility accommodation",
        "equity in city services",
    ),
    "lgbtq": (
        "lgbtq",
        "lgbtq ordinance",
        "lgbtq proclamation",
        "lgbtq recognition",
        "lgbtq public comment",
        "pride proclamation",
    ),
    "education": (
        "school board liaison",
        "library funding",
        "library policy",
        "civic education partnership",
        "after school program",
        "education partnership",
    ),
}

# Bumped whenever TOPIC_TRIGGERS changes. Stored on every
# `meeting_topic_tags` row so a future re-tuning + backfill can
# distinguish rows tagged under old vs new trigger sets. Semver-shape;
# don't parse it — it's a lineage label, not a comparison key.
MATCHER_VERSION: str = "v1-2026-07-30"

# Used as the fallback tag when extraction yields a quote whose topic
# doesn't match any of the five surfaced categories. Stored in the
# database but not surfaced as a Cast-page filter. Meetings do NOT
# receive the "other" tag — an unmatched meeting has zero topic-match
# rows (see topic_matcher.match_meeting).
OTHER_TAG_ID: str = "other"


def is_valid_tag(tag: str) -> bool:
    return tag == OTHER_TAG_ID or tag in TOPIC_TAG_IDS


def normalize_tags(tags: list[str]) -> list[str]:
    """Coerce a list of arbitrary tag strings to the canonical set.

    Any tag not in TOPIC_TAG_IDS or OTHER_TAG_ID is dropped. If the
    result is empty, returns [OTHER_TAG_ID] so every quote has at
    least one tag.

    NOTE: this helper is for the QUOTE side (member_quotes, unified
    quotes.topic_tags) where "unclassified" needs to be captured as
    the `other` lane. Do NOT use it on the MEETING side — an unmatched
    meeting must have zero topic-match rows so it doesn't email topic
    followers spuriously (sol Round-2 cold-start rule).
    """
    if not tags:
        return [OTHER_TAG_ID]
    cleaned = [t for t in tags if is_valid_tag(t)]
    return cleaned if cleaned else [OTHER_TAG_ID]

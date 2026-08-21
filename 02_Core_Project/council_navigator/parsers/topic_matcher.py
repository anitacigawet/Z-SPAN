"""topic_matcher — deterministic keyword matcher over the meeting text fields.

Session-103 (product-slice3a) — the notification-fanout path needs to
know which of the controlled 5-tag topics (data_centers, water_rights,
diversity_inclusion, lgbtq, education) a newly-published meeting is
"about," so a signed-in follower of a topic gets exactly the emails
they asked for.

Design constraints (sol Round-2 verdict + rationale):

  1. Deterministic. Same inputs → same tags every time. Replay-safe.
  2. Zero LLM cost. `apply_meeting_payload` runs synchronously inside
     the flagship-sync HTTP request; an LLM call would add seconds +
     nondeterminism to a path with a 120 s timeout.
  3. Field-gated. Scans the HIGH-SIGNAL fields ONLY: `meeting_title`,
     `episode_tagline`, and each item of the `key_decisions` list.
     Deliberately excludes `synopsis` — a passing "data center"
     mention in a broader budget synopsis must NOT admit the tag;
     "Special Meeting on Hyperscaler Zoning" must.
  4. Whole-token / whole-phrase. Case-fold + normalize hyphens/slashes
     to space + collapse whitespace. Support ordinary terminal plurals
     ("data center" matches "data center", "data centers", not
     "data-centering-adjacent"). Never substring-match — `tax` MUST
     NOT match `contact`.
  5. Cold-start honesty. An unmatched meeting yields an empty list,
     NOT the `other` fallback tag. Sending a "we're not sure why"
     email erodes trust; an honest false negative doesn't.
  6. Evidence preserved. Every match carries the FIELD it hit and the
     TRIGGER PHRASE that matched, so the outbox row's `reasons_json`
     lets the email say truthfully "Sent because you follow Data
     Centers (matched 'hyperscaler' in the meeting title)."

The trigger vocabulary lives at `topic_tags.TOPIC_TRIGGERS`; the
`MATCHER_VERSION` string is stamped on every stored match row so a
future re-tuning of the triggers is distinguishable in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

try:
    from parsers.topic_tags import (
        MATCHER_VERSION,
        TOPIC_TAG_IDS,
        TOPIC_TRIGGERS,
    )
except ImportError:
    from topic_tags import (  # type: ignore[no-redef]
        MATCHER_VERSION,
        TOPIC_TAG_IDS,
        TOPIC_TRIGGERS,
    )


# Fields the matcher scans, in priority order. When a tag matches in
# multiple fields for the same meeting, the first-listed field wins
# so the stored evidence is stable across runs. See `match_meeting()`.
FIELD_TITLE = "meeting_title"
FIELD_TAGLINE = "episode_tagline"
FIELD_KEY_DECISION = "key_decision"

_FIELD_PRIORITY = (FIELD_TITLE, FIELD_TAGLINE, FIELD_KEY_DECISION)


@dataclass(frozen=True)
class TopicMatch:
    """One tag match for a meeting.

    Multiple `TopicMatch` instances for the same (meeting, tag_id) pair
    should never be persisted — `match_meeting` collapses them to a
    single winner per tag before returning.
    """
    tag_id: str
    evidence_field: str
    trigger_phrase: str
    matcher_version: str


# Whitespace collapser + hyphen/slash normalizer. Applied to BOTH the
# scan text and the trigger phrases so "data-center" in text matches
# "data center" in triggers and vice versa. Punctuation other than
# hyphen/slash stays (periods, commas, quotes) — the whole-token
# regex-boundary check below tolerates them fine.
_NORMALIZE_RE = re.compile(r"[-/–—]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Case-fold + swap dashes/slashes for spaces + collapse whitespace.

    Case-fold (not lower) so non-ASCII text ("straße") normalizes
    reliably; downstream comparisons stay ASCII-cheap because the
    trigger vocabulary is ASCII by construction.
    """
    if not text:
        return ""
    text = _NORMALIZE_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.casefold()


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    """Whole-token/phrase matcher with optional terminal plural.

    `data center` → r'\\bdata center s?\\b' (space + optional 's' at end).
    Word boundaries prevent `tax` matching inside `contact`. Terminal
    plural is a single trailing `s` — deliberately narrow (not `es`,
    not `ies`) so we don't overreach into false-positive territory.
    """
    normalized = _normalize(phrase)
    escaped = re.escape(normalized)
    # `\bWORD s?\b` matches the phrase itself + a bare pluralized last
    # token. The trailing `\b` binds against a token boundary so
    # `hyperscaler campus` still matches even with the pluralization
    # allowance.
    return re.compile(r"\b" + escaped + r"s?\b")


# Precompile every trigger once at import time.
# _TRIGGERS[tag_id] = tuple of (phrase, compiled_regex) pairs.
_TRIGGERS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    tag_id: tuple(
        (phrase, _phrase_regex(phrase)) for phrase in TOPIC_TRIGGERS.get(tag_id, ())
    )
    for tag_id in TOPIC_TAG_IDS
}


def _scan_field(text: str, tag_id: str) -> str | None:
    """Return the FIRST trigger phrase from `tag_id` that hits `text`.

    Preserves the trigger vocabulary's declared order — the first
    listed phrase per tag is the "canonical" one, so a title matching
    both "data center" and "hyperscaler" reports the former.
    """
    normalized = _normalize(text)
    if not normalized:
        return None
    for phrase, pattern in _TRIGGERS.get(tag_id, ()):
        if pattern.search(normalized):
            return phrase
    return None


def match_meeting(
    *,
    meeting_title: str | None,
    episode_tagline: str | None,
    key_decisions: Sequence[str] | None,
) -> list[TopicMatch]:
    """Return the deduped, deterministic list of topic tags this meeting hits.

    Priority resolution: for each tag, we scan the fields in
    `_FIELD_PRIORITY` order (title first, tagline second, key
    decisions third). The FIRST field to hit wins — that field name +
    the first hitting phrase feed the stored `evidence_field` and
    `trigger_phrase`. This keeps the evidence stable across runs: if
    a title mentions "hyperscaler" and a decision also does, the
    stored evidence is the title, not the (later) decision.

    Callers pass `key_decisions` as the parsed list already stripped
    of citations/markup. Passing raw prompt output would produce
    false positives from citation phrasing.

    An empty return means NO topic-follow email fires for this
    meeting. Do NOT fall back to `other` — see the cold-start rule
    at the module docstring.
    """
    normalized_title = meeting_title or ""
    normalized_tagline = episode_tagline or ""
    decisions = tuple(d for d in (key_decisions or ()) if d and d.strip())

    matches: list[TopicMatch] = []
    for tag_id in TOPIC_TAG_IDS:
        winner_field: str | None = None
        winner_phrase: str | None = None

        # FIELD_TITLE — single string.
        hit = _scan_field(normalized_title, tag_id)
        if hit is not None:
            winner_field = FIELD_TITLE
            winner_phrase = hit

        if winner_field is None:
            # FIELD_TAGLINE — single string.
            hit = _scan_field(normalized_tagline, tag_id)
            if hit is not None:
                winner_field = FIELD_TAGLINE
                winner_phrase = hit

        if winner_field is None:
            # FIELD_KEY_DECISION — list of strings; first decision that
            # matches wins. The evidence_field is stored as the generic
            # "key_decision" so the storage key doesn't hardcode an
            # ordinal index that later re-generations might invalidate.
            for decision in decisions:
                hit = _scan_field(decision, tag_id)
                if hit is not None:
                    winner_field = FIELD_KEY_DECISION
                    winner_phrase = hit
                    break

        if winner_field is not None and winner_phrase is not None:
            matches.append(
                TopicMatch(
                    tag_id=tag_id,
                    evidence_field=winner_field,
                    trigger_phrase=winner_phrase,
                    matcher_version=MATCHER_VERSION,
                )
            )

    return matches

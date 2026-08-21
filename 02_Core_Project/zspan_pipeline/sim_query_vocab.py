"""Python mirror of the signed-out Librarian's fixed question vocabulary.

``client/src/lib/suggestedQuestions.ts`` remains the product source of truth.
The cross-language golden test reads both source files so wording or ordering
drift fails loudly instead of changing which cached questions are generated.
"""

from __future__ import annotations

import re
from typing import Literal


MeetingQuestionBucket = Literal[
    "regular",
    "work_study",
    "special",
    "fallback",
]

# The TypeScript vocabulary was authored on 2026-07-08. Bump this constant
# whenever the mirrored wording or order changes.
SIM_QUERY_VOCAB_VERSION = "v1-2026-07-08"

SUGGESTED_QUESTIONS_BY_TYPE: dict[MeetingQuestionBucket, tuple[str, ...]] = {
    "regular": (
        "What did the council vote on, and how did each member vote?",
        "What money was approved, and for what?",
        "Were any items tabled, postponed, or sent back to staff?",
        "What did residents raise during the public-comment period?",
        "What is scheduled to come back to the council next?",
    ),
    "work_study": (
        "What topics did the council discuss without taking a formal vote?",
        "What direction did the council give to staff?",
        "What questions or concerns did council members raise about these topics?",
        "What did residents raise during the public-comment period?",
        "Which items are expected to return for a formal decision later?",
    ),
    "special": (
        "Why was this special meeting called, and what items were on the agenda?",
        "What did the council decide on those items?",
        "Were any items tabled or continued to a later date?",
        "What did residents raise during the public-comment period?",
        "What happens next on these items?",
    ),
    "fallback": (
        "What were the main items this body considered?",
        "What did it decide, and how did the members vote?",
        "Were any items tabled, postponed, or sent back to staff?",
        "What did members of the public raise during the comment period?",
        "What is scheduled to come next?",
    ),
}

_WORK_STUDY_RE = re.compile(r"work session|study session|workshop")
_SPECIAL_RE = re.compile(r"\bspecial\b")
_FALLBACK_RE = re.compile(
    r"board|commission|committee|planning|zoning|authority"
)


def bucket_for_title(title: str | None) -> MeetingQuestionBucket:
    """Mirror ``bucketForTitle`` from the TypeScript source exactly."""
    normalized = (title or "").lower()
    if _WORK_STUDY_RE.search(normalized):
        return "work_study"
    if _SPECIAL_RE.search(normalized):
        return "special"
    if _FALLBACK_RE.search(normalized):
        return "fallback"
    return "regular"


def suggested_questions_for_title(title: str | None) -> tuple[str, ...]:
    """Return the full five-question display vocabulary for ``title``."""
    return SUGGESTED_QUESTIONS_BY_TYPE[bucket_for_title(title)]


def sim_questions_for_title(title: str | None) -> tuple[str, str, str]:
    """Return the three receipts-only questions eligible for cached answers."""
    questions = suggested_questions_for_title(title)
    return questions[0], questions[1], questions[2]


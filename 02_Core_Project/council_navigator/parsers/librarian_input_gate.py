"""Deterministic grammar-v2 gate for Librarian queries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


GATE_VERSION = "grammar-v2"
QUERY_CHAR_CAP = 200

MESSAGES: dict[str, str] = {
    "not_a_string": "Write one question in the box.",
    "empty": "Write a question before sending.",
    "too_long": "Keep it under 200 characters — one focused question.",
    "control_characters": (
        "Use spaces between words instead of tabs, line breaks, or other "
        "control characters."
    ),
    "non_ascii": (
        "Use plain English letters only — write names without accents and "
        "spell numbers out as words."
    ),
    "digits": (
        "Spell numbers out as words — write 'sixteen million', not '16M'."
    ),
    "no_terminal_question_mark": (
        "End your question with one question mark."
    ),
    "multiple_question_marks": (
        "Use one question mark, at the end."
    ),
    "multiple_sentences": (
        "Ask one focused question without periods, exclamation marks, "
        "semicolons, or colons."
    ),
    "no_words": "Write at least one word before the question mark.",
    "bad_word": (
        "Use words made from letters, apostrophes, or hyphens; a comma sits "
        "right after a word — like 'approved, and' — not {token}."
    ),
}

_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*'?,?")


@dataclass(frozen=True)
class GateResult:
    ok: bool
    canonical_query: Optional[str]
    reason_code: Optional[str]
    message: Optional[str]


def _reject(reason_code: str, *, token: str | None = None) -> GateResult:
    message = MESSAGES[reason_code]
    if token is not None:
        message = message.format(token=json.dumps(token))
    return GateResult(
        ok=False,
        canonical_query=None,
        reason_code=reason_code,
        message=message,
    )


def validate_librarian_query(raw: object) -> GateResult:
    """Validate and canonicalize one Librarian question."""

    if not isinstance(raw, str):
        return _reject("not_a_string")

    if len(raw) > QUERY_CHAR_CAP:
        return _reject("too_long")

    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        return _reject("control_characters")

    if any(ord(char) > 0x7E for char in raw):
        return _reject("non_ascii")

    if any("0" <= char <= "9" for char in raw):
        return _reject("digits")

    canonical = " ".join(raw.strip().split())
    assert len(canonical) <= QUERY_CHAR_CAP
    if not canonical:
        return _reject("empty")

    if not canonical.endswith("?"):
        return _reject("no_terminal_question_mark")

    if canonical.count("?") != 1:
        return _reject("multiple_question_marks")

    if any(mark in canonical for mark in ".!;:"):
        return _reject("multiple_sentences")

    body = canonical[:-1].strip()
    if not body:
        return _reject("no_words")

    tokens = body.split(" ")
    for index, token in enumerate(tokens):
        if _WORD_RE.fullmatch(token) is None:
            return _reject("bad_word", token=token)
        if index == len(tokens) - 1 and token.endswith(","):
            return _reject("bad_word", token=token)

    return GateResult(
        ok=True,
        canonical_query=canonical,
        reason_code=None,
        message=None,
    )

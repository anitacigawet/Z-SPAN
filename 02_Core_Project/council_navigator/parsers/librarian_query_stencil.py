"""Deterministic contextual stencil for grammar-valid Librarian queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    from .librarian_input_gate import (
        GATE_VERSION,
        MESSAGES as GRAMMAR_MESSAGES,
        validate_librarian_query,
    )
except ImportError:  # Support direct execution from the parsers directory.
    from librarian_input_gate import (  # type: ignore[no-redef]
        GATE_VERSION,
        MESSAGES as GRAMMAR_MESSAGES,
        validate_librarian_query,
    )


STENCIL_VERSION = "stencil-v2"
COMPOSED_GATE_VERSION = f"{GATE_VERSION}+{STENCIL_VERSION}"

MESSAGES: dict[str, str] = {
    **GRAMMAR_MESSAGES,
    "not_a_question": (
        "Start with a question word — like What, Why, How, or Did."
    ),
    "artifact_pattern": (
        "That doesn't look like a question about the meeting — ask about "
        "what happened in the record."
    ),
}

_INTERROGATIVE_LEADS = frozenset(
    {
        "what",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "which",
        "did",
        "does",
        "do",
        "is",
        "are",
        "was",
        "were",
        "can",
        "could",
        "will",
        "would",
        "should",
        "shall",
        "has",
        "have",
        "had",
        "may",
        "might",
        "must",
        "am",
        "isn't",
        "aren't",
        "wasn't",
        "weren't",
        "don't",
        "doesn't",
        "didn't",
        "can't",
        "couldn't",
        "won't",
        "wouldn't",
        "shouldn't",
        "hasn't",
        "haven't",
        "hadn't",
        "mightn't",
        "mustn't",
    }
)
_ARTIFACT_BIGRAMS = frozenset(
    {
        ("system", "prompt"),
        ("developer", "prompt"),
        ("system", "instructions"),
        ("developer", "instructions"),
    }
)
_ARTIFACT_NOUNS = frozenset(
    {"instructions", "instruction", "prompt", "prompts", "rules", "guidelines"}
)
_DISCARD_VERBS = frozenset(
    {"ignore", "disregard", "override", "forget", "discard", "abandon", "reset"}
)
_NOFOLLOW_PHRASES = (
    ("without", "following"),
    ("without", "obeying"),
    ("do", "not", "follow"),
    ("not", "follow"),
)
_EXTRACTION_VERBS = frozenset(
    {"reveal", "repeat", "show", "expose", "disclose", "dump", "print"}
)
_EXTRACTION_NOUNS = frozenset(
    {"prompt", "prompts", "instructions", "instruction"}
)
_ROLE_PHRASES = (
    ("act", "as"),
    ("pose", "as"),
    ("behave", "as"),
    ("function", "as"),
    ("operate", "as"),
    ("role", "play"),
    ("roleplay",),
    ("pretend", "you", "are"),
    ("pretend", "to", "be"),
)
_SECOND_PERSON = frozenset({"you", "your"})
_ROLE_ARTIFACT_CONTEXT = frozenset(
    {
        "system",
        "override",
        "prompt",
        "instructions",
        "unrestricted",
        "unfiltered",
        "developer",
        "assistant",
    }
)
_EVASION_SINGLE_TOKENS = frozenset(
    {"bypass", "evade", "circumvent", "disable"}
)
_EVASION_NOUNS = frozenset(
    {"gate", "filter", "restrictions", "restriction", "rules", "safeguards"}
)
_ARTICLES = frozenset({"the", "a", "an"})
_POSSESSIVE_NOUNS = frozenset(
    {"rules", "instructions", "prompt", "prompts", "guidelines", "filter"}
)


@dataclass(frozen=True)
class StencilResult:
    ok: bool
    canonical_query: Optional[str]
    reason_code: Optional[str]
    message: Optional[str]
    matched_rule_id: Optional[str]
    gate_version: str


def _canonical_tokens(canonical_query: str) -> tuple[str, ...]:
    """Return comma-free surface tokens for the interrogative-lead check."""

    body = canonical_query[:-1].strip()
    return tuple(token.rstrip(",").lower() for token in body.split(" "))


def _artifact_tokens(surface_tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Return a stencil-only view split on grammar-approved separators."""

    artifact_tokens: list[str] = []
    for surface_token in surface_tokens:
        token = surface_token
        if token.endswith("'"):
            token = token[:-1]
        token = token.replace("-", " ").replace("'", " ")
        artifact_tokens.extend(
            part for part in token.split(" ") if part
        )
    return tuple(artifact_tokens)


def _contains_phrase(
    tokens: tuple[str, ...],
    phrase: tuple[str, ...],
) -> bool:
    width = len(phrase)
    return any(
        tokens[index : index + width] == phrase
        for index in range(len(tokens) - width + 1)
    )


def _has_any_phrase(
    tokens: tuple[str, ...],
    phrases: tuple[tuple[str, ...], ...],
) -> bool:
    return any(_contains_phrase(tokens, phrase) for phrase in phrases)


def _has_nonattributive_evasion(tokens: tuple[str, ...]) -> bool:
    for index, token in enumerate(tokens):
        is_evasion = token in _EVASION_SINGLE_TOKENS
        if not is_evasion and token == "get":
            is_evasion = (
                index + 1 < len(tokens) and tokens[index + 1] == "around"
            )
        if not is_evasion:
            continue
        if index > 0 and tokens[index - 1] in _ARTICLES:
            continue
        return True
    return False


def _reject(
    reason_code: str,
    *,
    matched_rule_id: str | None = None,
    message: str | None = None,
) -> StencilResult:
    return StencilResult(
        ok=False,
        canonical_query=None,
        reason_code=reason_code,
        message=message if message is not None else MESSAGES[reason_code],
        matched_rule_id=matched_rule_id,
        gate_version=COMPOSED_GATE_VERSION,
    )


def _accept(canonical_query: str) -> StencilResult:
    return StencilResult(
        ok=True,
        canonical_query=canonical_query,
        reason_code=None,
        message=None,
        matched_rule_id=None,
        gate_version=COMPOSED_GATE_VERSION,
    )


def evaluate_librarian_query(raw: object) -> StencilResult:
    """Apply grammar-v2, then stencil-v2, to one Librarian query."""

    grammar_result = validate_librarian_query(raw)
    if not grammar_result.ok:
        return _reject(
            grammar_result.reason_code or "not_a_string",
            message=grammar_result.message,
        )

    canonical_query = grammar_result.canonical_query
    assert canonical_query is not None
    surface_tokens = _canonical_tokens(canonical_query)

    if surface_tokens[0] not in _INTERROGATIVE_LEADS:
        return _reject("not_a_question")

    tokens = _artifact_tokens(surface_tokens)
    token_set = set(tokens)

    if any(
        pair in _ARTIFACT_BIGRAMS
        for pair in zip(tokens, tokens[1:])
    ):
        return _reject(
            "artifact_pattern",
            matched_rule_id="deny.artifact_bigram.v2",
        )

    if (
        "jailbreak" in token_set
        or _contains_phrase(tokens, ("jail", "break"))
    ):
        return _reject(
            "artifact_pattern",
            matched_rule_id="deny.jailbreak.v2",
        )

    if token_set & _DISCARD_VERBS and token_set & _ARTIFACT_NOUNS:
        return _reject(
            "artifact_pattern",
            matched_rule_id="deny.discard.v2",
        )

    if (
        _has_any_phrase(tokens, _NOFOLLOW_PHRASES)
        and token_set & _ARTIFACT_NOUNS
    ):
        return _reject(
            "artifact_pattern",
            matched_rule_id="deny.nofollow.v2",
        )

    if token_set & _EXTRACTION_VERBS and token_set & _EXTRACTION_NOUNS:
        return _reject(
            "artifact_pattern",
            matched_rule_id="deny.extraction.v2",
        )

    if (
        _has_any_phrase(tokens, _ROLE_PHRASES)
        and (
            token_set & _SECOND_PERSON
            or token_set & _ROLE_ARTIFACT_CONTEXT
        )
    ):
        return _reject(
            "artifact_pattern",
            matched_rule_id="deny.role.v2",
        )

    if (
        _has_nonattributive_evasion(tokens)
        and token_set & _EVASION_NOUNS
    ):
        return _reject(
            "artifact_pattern",
            matched_rule_id="deny.evasion.v2",
        )

    if "your" in token_set and token_set & _POSSESSIVE_NOUNS:
        return _reject(
            "artifact_pattern",
            matched_rule_id="deny.possessive.v2",
        )

    return _accept(canonical_query)

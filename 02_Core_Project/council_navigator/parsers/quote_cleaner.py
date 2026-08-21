"""
quote_cleaner — strip spoken disfluencies from extracted council quotes.
==========================================================================

Per James 2026-05-11: extracted quotes from the synthesis pipeline (and downstream
verification sources) often carry filler words and verbal tics that make
them read poorly on the Cast page. This module runs a *verbatim-preserving*
cleanup pass before quotes land in `member_quotes`.

This is the ONLY use of OpenAI in the project — quote cleaning is mechanical
cleanup of existing content, NOT content generation. The V1-RAG-3 pipeline remains the
sole brain for civic content per CLAUDE.md. The doctrinal split:

  * V1-RAG-3 pipeline -> generates the broadcast output set,
                       extracts quotes.
  * OpenAI 4o-mini  -> removes "uh"s, "um"s, false starts, stuttered
                       repetitions from those extracted quotes. Never adds,
                       rephrases, or interprets.

Model: gpt-4o-mini (cheap, fast, predictable). ~$0.0001/quote at typical
length; affordable even at full state scale.

Key resolution: tries `OPENAI_API_KEY` env var first, then the
`openai_api_key` field in `parsers/user_settings.json` (set on the Settings
page). Cleaning is locked to gpt-4o-mini, so this resolves the key directly
rather than going through any provider dispatcher. (The `active_provider`
toggle it once sat beside was retired with the legacy Navigator AI subsystem.)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import requests

from env_config import load_user_settings

logger = logging.getLogger(__name__)

OPENAI_MODEL = "gpt-4o-mini"
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_TIMEOUT_SECONDS = 30

# The cleaning prompt is locked. Changing it would change the quote
# corpus's character. If you genuinely need to update it, also add a
# `cleaner_version` field to `member_quotes` and re-clean affected rows.
CLEANER_SYSTEM_PROMPT = """You are a verbatim transcript cleaner for civic council meeting quotes. Your job is to remove ONLY spoken disfluencies from a quote — never to paraphrase, summarize, or alter meaning in any way.

REMOVE:
- Filler sounds used as pauses: "uh", "um", "uhh", "er", "ah", "mm"
- Verbal tics when clearly used as filler (NOT when carrying meaning): "like", "you know", "I mean", "sort of", "kind of"
- Repeated false starts: "the the the" becomes "the"
- Stuttered partial words at the start of phrases: "I— I think" becomes "I think", "we— we voted" becomes "we voted"
- Single-word self-corrections where the speaker immediately replaces with a different word: "the budget — the appropriation" stays as said, but "the bud— the budget" becomes "the budget"

DO NOT:
- Change any actual content word
- Rephrase, paraphrase, or modernize language
- Fix grammar (unless it was clearly a stutter or false start)
- Translate, expand, or contract contractions
- Add ANY words that were not in the original
- Remove words that carry meaning even if awkward
- Remove "like", "you know", "I mean", etc. when they're being used meaningfully (e.g., comparison, citation, or actual reference)
- Add or change punctuation beyond what reflects natural pauses

PRESERVE EXACTLY:
- All proper nouns (people, places, organizations) as spoken
- All numbers, dates, dollar amounts as spoken
- Sentence structure, tone, register, and word choice
- Punctuation that reflects natural pauses (commas, ellipses)
- Capitalization

If the quote contains NO disfluencies, return it EXACTLY UNCHANGED.

If you are uncertain whether a word is a filler or carries meaning, KEEP IT. Bias toward preserving the original. Citizens will read these quotes alongside the source video — they must match what was actually said.

Return ONLY the cleaned quote as a single JSON object with this exact shape:
{"cleaned": "<the cleaned quote text>", "changed": <true if any change was made, false if returned unchanged>, "notes": "<optional short note on what was removed, max 50 chars>"}

No preamble, no surrounding text, no markdown fences. Just the JSON object."""


@dataclass
class CleanedQuote:
    original: str
    cleaned: str
    changed: bool
    notes: str
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "cleaned": self.cleaned,
            "changed": self.changed,
            "notes": self.notes,
            "error": self.error,
        }


def _resolve_openai_key() -> str:
    """Resolve OPENAI_API_KEY env var -> user_settings.json -> empty."""
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key.strip()
    settings = load_user_settings()
    return (settings.get("openai_api_key") or "").strip()


def is_configured() -> bool:
    """Return True iff the LLM backing clean_quote/polish_for_display is
    callable. Post-migration (2026-06-21) the backing LLM is gpt-5.5 via
    Codex CLI, NOT OpenAI's REST API — so the gate checks whether the
    codex binary is locatable, not whether OPENAI_API_KEY is set. The
    OpenAI key resolver above stays for any callers that genuinely
    need it (e.g. whisper-1 fallback) but does not gate THIS module's
    public functions anymore."""
    try:
        from codex_cli_client import _resolve_codex_binary
        _resolve_codex_binary()
        return True
    except Exception:
        return False


def clean_quote(quote_text: str, context: Optional[str] = None) -> CleanedQuote:
    """Run the cleaning prompt against a single quote.

    Args:
        quote_text: the raw extracted quote string.
        context: optional surrounding context (the agenda item being
            discussed, or the previous/next sentence). Helps the cleaner
            judge whether ambiguous words like "like" carry meaning.
            Context is for the model's understanding only and is NOT
            included in the output.

    Returns a CleanedQuote with the cleaned text, a changed flag, and
    optional notes. On failure (no key, HTTP error, malformed JSON),
    returns `cleaned == original` with the error message attached and
    `changed=False`.

    Also records ok/fail with llm_health for observability.
    """
    # Lazy import — llm_health is a sibling module and importing at module
    # load would create a tight coupling; importing here keeps the dependency
    # one-directional and lets llm_health be optional.
    try:
        from llm_health import record_ok, record_fail
    except Exception:
        record_ok = lambda *_a, **_k: None  # noqa: E731
        record_fail = lambda *_a, **_k: None  # noqa: E731

    if not quote_text or not quote_text.strip():
        return CleanedQuote(
            original=quote_text or "",
            cleaned=quote_text or "",
            changed=False,
            notes="empty input",
        )

    # Migrated 2026-06-21: per-call gpt-4o-mini → gpt-5.5 via Codex CLI
    # (subscription-backed; no per-call paid-API key required). The system
    # prompt + user payload shape is unchanged so the contract holds.
    from codex_cli_client import invoke_codex_json
    user_payload = {"quote": quote_text}
    if context:
        user_payload["context"] = context

    parsed, codex_result = invoke_codex_json(
        CLEANER_SYSTEM_PROMPT,
        json.dumps(user_payload, ensure_ascii=False),
    )
    if not codex_result.ok or parsed is None:
        record_fail("clean_quote", codex_result.error or "codex invoke failed")
        return CleanedQuote(
            original=quote_text,
            cleaned=quote_text,
            changed=False,
            notes="",
            error=codex_result.error or "codex invoke failed",
        )
    cleaned = parsed.get("cleaned", quote_text)
    changed = bool(parsed.get("changed", cleaned != quote_text))
    notes = (parsed.get("notes") or "")[:80]

    # Sanity check: the cleaner must not have invented new content.
    # We allow shrinkage (disfluency removal) but reject expansion that
    # adds substantially new tokens — that would be a model hallucination.
    # Threshold: cleaned must be <= 1.1x the input length in characters.
    if len(cleaned) > len(quote_text) * 1.1 + 10:
        logger.warning(
            "Cleaner output suspiciously longer than input; rejecting. "
            "original=%r cleaned=%r",
            quote_text,
            cleaned,
        )
        record_fail("clean_quote", "cleaner output longer than input (rejected)")
        return CleanedQuote(
            original=quote_text,
            cleaned=quote_text,
            changed=False,
            notes="",
            error="cleaner output longer than input (rejected as suspicious)",
        )

    record_ok("clean_quote")
    return CleanedQuote(
        original=quote_text,
        cleaned=cleaned,
        changed=changed,
        notes=notes,
    )


# ── polish_for_display — second-pass readability polish ─────────────────────
#
# The disfluency-stripped form above preserves spoken-transcript styling
# (often lowercase, sparse punctuation) for verbatim fidelity to the audio
# — that lets karaoke alignment + word_timings stay accurate. But for
# operator review surfaces like DisputedQuotesPage, the same verbatim form
# is fatiguing to scan. This second pass adds capitalization and light
# punctuation ONLY, without changing wording.
#
# The polished form becomes the new `quotes.quote_text` once the operator
# verifies the quote on DisputedQuotesPage; the verbatim original is
# preserved in `quote_text_original` for forensic comparison (see
# `update_quote_verification` in `database.py`).
#
# Locked prompt — same discipline as the disfluency cleaner: changing it
# would change the polished corpus character. If you need to update it,
# also recompute every existing `quote_text_display` value.

POLISH_SYSTEM_PROMPT = """You are a readability polisher for civic council meeting quotes that have already been verbatim-cleaned (disfluencies removed). Your ONLY job is to make the text easier to scan visually for a reviewer.

YOU MAY:
- Capitalize the first letter of the quote
- Capitalize clearly-identifiable proper nouns (names of people, cities, organizations, recognizable institutions, official titles)
- Add commas at natural pauses where the speaker's cadence implies them
- Add a period (or appropriate terminal punctuation) at the end of the quote if missing
- Add ONE additional mid-quote period ONLY when there is an unmistakable sentence boundary — a complete subject-verb-object clause followed by a fresh independent clause that introduces a new subject or topic

YOU MUST NOT:
- Change any word
- Add any word
- Remove any word
- Rephrase, paraphrase, or summarize
- Translate
- Expand or contract contractions
- Fix grammatical errors that are not punctuation/capitalization
- Add quotation marks around or inside the text
- Add ellipses unless the source already had them
- Add semicolons or em-dashes
- Reorder or restructure

PREFER COMMAS OVER PERIODS. Civic speakers often produce long run-on cadences that read as one continuous thought even when they cross a clause boundary. Adding a period there breaks the speaker's voice. When in doubt between a comma and a period, choose the comma. The reviewer prefers one long sentence with commas over two short choppy ones.

If the quote is ALREADY properly capitalized and punctuated, return it UNCHANGED.

If you are uncertain whether a word is a proper noun, KEEP IT LOWERCASE. Bias toward preserving the original. The reviewer will see this polished form in an editable textarea and can correct any misjudgment.

Return ONLY a single JSON object with this exact shape:
{"polished": "<the polished quote text>", "changed": <true if any change was made, false if returned unchanged>}

No preamble, no surrounding text, no markdown fences. Just the JSON object."""


@dataclass
class PolishedQuote:
    original: str
    polished: str
    changed: bool
    error: Optional[str] = None
    # V1-Consensus-1 C4: when polish_for_display rejects because the
    # polisher reworded (the word-level safety check at line 357), this
    # field carries the polisher's actual proposed text so the caller
    # can route it to the consensus pipeline. Null on success and on
    # the longer-than-input rejection (that signal is "polisher added
    # content" not "polisher proposed a word substitution").
    rejected_polish_proposal: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "polished": self.polished,
            "changed": self.changed,
            "error": self.error,
            "rejected_polish_proposal": self.rejected_polish_proposal,
        }


def polish_for_display(quote_text: str) -> PolishedQuote:
    """Run the polish prompt against a single quote.

    Adds capitalization + light punctuation for readability, without
    changing wording. On any failure (no key, HTTP error, malformed JSON,
    suspicious length expansion) returns `polished == original` with the
    error attached and `changed=False` — the caller can fall back to the
    verbatim form for display.

    Also records ok/fail with llm_health for observability.
    """
    try:
        from llm_health import record_ok, record_fail
    except Exception:
        record_ok = lambda *_a, **_k: None  # noqa: E731
        record_fail = lambda *_a, **_k: None  # noqa: E731

    if not quote_text or not quote_text.strip():
        return PolishedQuote(
            original=quote_text or "",
            polished=quote_text or "",
            changed=False,
        )

    # Migrated 2026-06-21: per-call gpt-4o-mini → gpt-5.5 via Codex CLI
    # (subscription-backed). Same POLISH_SYSTEM_PROMPT, same safety checks
    # below — only the LLM call mechanism changes. Word-level changes the
    # polisher proposes (which the safety check rejects) are the input
    # signal for the consensus-vocab-promotion pipeline (next chunk piece).
    from codex_cli_client import invoke_codex_json
    parsed, codex_result = invoke_codex_json(
        POLISH_SYSTEM_PROMPT,
        json.dumps({"quote": quote_text}, ensure_ascii=False),
    )
    if not codex_result.ok or parsed is None:
        record_fail("polish_for_display", codex_result.error or "codex invoke failed")
        return PolishedQuote(
            original=quote_text,
            polished=quote_text,
            changed=False,
            error=codex_result.error or "codex invoke failed",
        )
    polished = parsed.get("polished", quote_text)
    changed = bool(parsed.get("changed", polished != quote_text))

    # Reject suspicious expansion. Punctuation + capitalization should
    # add only a handful of characters; if the polished form is meaningfully
    # longer, the model probably added content. Threshold: +15% chars +10.
    if len(polished) > len(quote_text) * 1.15 + 10:
        logger.warning(
            "polish_for_display output suspiciously longer than input; rejecting. "
            "original=%r polished=%r",
            quote_text,
            polished,
        )
        record_fail("polish_for_display", "output longer than input (rejected)")
        return PolishedQuote(
            original=quote_text,
            polished=quote_text,
            changed=False,
            error="polish output longer than input (rejected as suspicious)",
        )

    # Sanity check: the lowercase-stripped-punctuation form must be the same.
    # Catches any word-level edits that slipped past the prompt.
    def _normalize_for_comparison(s: str) -> str:
        keep = "".join(c.lower() if c.isalnum() else " " for c in s)
        return " ".join(keep.split())

    if _normalize_for_comparison(polished) != _normalize_for_comparison(quote_text):
        logger.warning(
            "polish_for_display changed word-level content; rejecting. "
            "original=%r polished=%r",
            quote_text,
            polished,
        )
        record_fail("polish_for_display", "output changed wording (rejected)")
        # V1-Consensus-1 C4: carry the rejected polish proposal back to the
        # caller so the polish-rejection event can route to the consensus
        # queue. The caller decides whether to enqueue (it needs city_name
        # + meeting_id which polish_for_display doesn't have). See
        # consensus_vocab.route_polish_rejection_to_consensus.
        return PolishedQuote(
            original=quote_text,
            polished=quote_text,
            changed=False,
            error="polish output changed wording (rejected)",
            rejected_polish_proposal=polished,
        )

    record_ok("polish_for_display")
    return PolishedQuote(
        original=quote_text,
        polished=polished,
        changed=changed,
    )


if __name__ == "__main__":
    # Quick smoke test runnable as: python3.11 quote_cleaner.py
    test_quote = (
        "Uh, I, I think the the budget you know is, um, going to need "
        "a lot of work before we can like really approve it."
    )
    result = clean_quote(test_quote, context="2026 General Fund discussion")
    print("ORIGINAL:", result.original)
    print("CLEANED :", result.cleaned)
    print("CHANGED :", result.changed)
    print("NOTES   :", result.notes)
    if result.error:
        print("ERROR   :", result.error)

    print()
    polish_input = (
        "the Capitol Police has this program through contract they will "
        "reimburse for overtime to not endure the cost of overtime for "
        "officers for the city so it's whenever the dignitaries are coming "
        "in and it's going to require some type of security for officers "
        "we then will get reimbursed"
    )
    polish_result = polish_for_display(polish_input)
    print("POLISH INPUT :", polish_input)
    print("POLISH OUTPUT:", polish_result.polished)
    print("CHANGED      :", polish_result.changed)
    if polish_result.error:
        print("ERROR        :", polish_result.error)

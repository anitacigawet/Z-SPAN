"""verdict_emphasis — extract short substrings worth red-highlighting in the disputed-quote heads-up
=======================================================================================================

When a quote is disputed by the T-013 V3 verification pass, Gemini's structured
verdict (text_differences + clip_integrity + speaker_attribution + other_concerns)
gets collapsed by the frontend's `humanizeVerdict()` into a single plain-language
note above the editable textarea on DisputedQuotesPage.

For scanning speed, James asked 2026-05-26 that the substantive bits inside that
note (specific differing words, brief integrity descriptors) be wrapped in red so
the reviewer's eye catches them at a glance. This module asks gpt-4o-mini for a
short list of substrings to emphasize.

Doctrinal split (mirrors quote_cleaner.py's rationale):
  - V1-RAG-3 pipeline -> generates the broadcast output set, extracts quotes
  - Gemini Pro       -> verifies extracted quotes against the source recording (T-013 V2)
  - OpenAI 4o-mini   -> mechanical UX helpers: strips fillers (quote_cleaner),
                        polishes for display (quote_cleaner.polish_for_display),
                        extracts emphasis tokens (THIS MODULE).

OpenAI never generates civic content or produces verdicts. It only does
mechanical post-processing of artifacts the other models produced.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from env_config import load_user_settings

logger = logging.getLogger(__name__)

OPENAI_MODEL = "gpt-4o-mini"
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_TIMEOUT_SECONDS = 30


EMPHASIS_SYSTEM_PROMPT = """You are helping a reviewer scan disputed-quote verdicts quickly.

The reviewer sees a plain-language note about what the verification pass flagged. For example:

  "Text misses \"so\" at the beginning, \"um\" before \"so it's whenever\", and \"for the overtime\" at the very end. The clip cuts mid word."

Your job: identify 2-5 SHORT substrings from that note that should be highlighted in red so the reviewer's eye catches the substantive bits at a glance.

GOOD emphasis substrings (return these):
- Words/phrases the verdict says are missing, added, or wrong: \"so\", \"um\", \"for the overtime\"
- Brief integrity descriptors: cuts mid word, starts mid sentence, audio garbled
- Brief speaker mismatches: wrong speaker, uncertain speaker
- Specific noun corrections: \"Beale Street\", \"POS systems\"

Include surrounding quotation marks IF the source note has them around the substring — e.g., if the note says
\"misses \"so\" at the beginning\", return \"so\" with the quote marks (so the highlighted unit reads as a quoted token).

BAD emphasis substrings (DO NOT return these):
- Full sentences or long phrases
- Generic connective words (\"the\", \"and\", \"at\", \"to\", \"in\")
- Surrounding context that explains where/why the issue is (\"at the beginning\", \"at the very end\", \"before\")
- Quoted context that's just describing WHERE the issue is, not the issue itself (e.g., \"so it's whenever\" when used to anchor where another word is missing)
- The whole note (defeats the purpose)

Each returned substring MUST be a verbatim case-sensitive match against the source note (a substring-search will be used to wrap it). If you can't find an exact match, omit it.

Return ONLY a JSON object with this exact shape:
{"emphasis": ["substring1", "substring2", ...]}

If the note doesn't have anything worth emphasizing (e.g., it's already short and clear), return {"emphasis": []}.

No preamble, no surrounding text, no markdown fences."""


@dataclass
class EmphasisResult:
    humanized_text: str
    emphasis_tokens: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "humanized_text": self.humanized_text,
            "emphasis_tokens": self.emphasis_tokens,
            "error": self.error,
        }


def humanize_verdict(verdict: Optional[dict]) -> Optional[str]:
    """Python port of `humanizeVerdict()` in `client/src/pages/DisputedQuotesPage.tsx`.

    Collapses Gemini's structured verdict (text_differences, clip_integrity,
    speaker_attribution / speaker_attribution_notes, other_concerns) into a
    single plain-language sentence — exactly the form the operator UI
    renders above the editable textarea on the disputed-quote review row.

    Returning the rendered form here lets the backend hand the LLM the
    exact string the operator will see, so emphasis tokens are guaranteed
    to substring-match what's on screen.

    Mirror this function 1:1 with the TypeScript original — if one moves,
    move the other.
    """
    if not verdict or not isinstance(verdict, dict):
        return None
    parts: List[str] = []

    text_diff = verdict.get("text_differences")
    if isinstance(text_diff, str) and text_diff.strip().lower() != "none" and text_diff.strip():
        parts.append(text_diff.strip())

    clip_integrity = verdict.get("clip_integrity")
    if (
        isinstance(clip_integrity, str)
        and clip_integrity.strip().lower() not in ("ok", "none", "")
    ):
        human = clip_integrity.replace("-", " ").strip()
        parts.append(f"The clip {human}.")

    speaker_attr = verdict.get("speaker_attribution")
    speaker_notes = verdict.get("speaker_attribution_notes")
    if isinstance(speaker_attr, str):
        sa = speaker_attr.strip().lower()
        if sa == "uncertain":
            if isinstance(speaker_notes, str) and speaker_notes and speaker_notes != "ok":
                parts.append(f"Speaker uncertain — {speaker_notes.strip()}")
            else:
                parts.append("Speaker uncertain.")
        elif sa == "no":
            if isinstance(speaker_notes, str) and speaker_notes and speaker_notes != "ok":
                parts.append(f"Wrong speaker — {speaker_notes.strip()}")
            else:
                parts.append("Wrong speaker.")

    other = verdict.get("other_concerns")
    if isinstance(other, str) and other.strip().lower() != "none" and other.strip():
        parts.append(other.strip())

    if not parts:
        return None
    return " ".join(parts)


def _resolve_openai_key() -> str:
    """Resolve OPENAI_API_KEY env var -> user_settings.json -> empty."""
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key.strip()
    settings = load_user_settings()
    return (settings.get("openai_api_key") or "").strip()


def is_configured() -> bool:
    """Post-migration (2026-06-21) extract_verdict_emphasis runs on
    gpt-5.5 via Codex CLI, NOT OpenAI's REST API — so the gate checks
    whether the codex binary is locatable, not whether OPENAI_API_KEY
    is set. Without this fix, removing the OpenAI key would silently
    no-op the emphasis-token extraction even though the LLM call itself
    no longer needs that key."""
    try:
        from codex_cli_client import _resolve_codex_binary
        _resolve_codex_binary()
        return True
    except Exception:
        return False


def extract_verdict_emphasis(verdict: Optional[dict]) -> EmphasisResult:
    """Compute the humanized verdict text + a list of short emphasis substrings.

    Returns an EmphasisResult with:
      - humanized_text: the same string the operator UI renders (via the
        TS twin of humanize_verdict). Stored so the backend serves a
        canonical pre-rendered form.
      - emphasis_tokens: 0-5 short substrings that substring-match within
        humanized_text. The frontend wraps each match in a red span.
      - error: optional message if anything failed; falls back gracefully
        (humanized_text always populated, emphasis_tokens may be []).

    Also records ok/fail with llm_health for observability.
    """
    try:
        from llm_health import record_ok, record_fail
    except Exception:
        record_ok = lambda *_a, **_k: None  # noqa: E731
        record_fail = lambda *_a, **_k: None  # noqa: E731

    humanized = humanize_verdict(verdict)
    if not humanized:
        return EmphasisResult(humanized_text="", emphasis_tokens=[])

    # Migrated 2026-06-21: per-call gpt-4o-mini → gpt-5.5 via Codex CLI
    # (subscription-backed). Same EMPHASIS_SYSTEM_PROMPT, same defensive
    # token filter below — only the LLM call mechanism changes.
    from codex_cli_client import invoke_codex_json

    # Pass the LLM both the rendered note + the raw structured verdict.
    # The note is what it must substring-match against; the structured
    # form helps it distinguish "missing word" from "context describing
    # where it's missing".
    user_payload = {
        "rendered_note": humanized,
        "structured_verdict": verdict,
    }

    parsed, codex_result = invoke_codex_json(
        EMPHASIS_SYSTEM_PROMPT,
        json.dumps(user_payload, ensure_ascii=False),
    )
    if not codex_result.ok or parsed is None:
        record_fail("extract_verdict_emphasis", codex_result.error or "codex invoke failed")
        return EmphasisResult(
            humanized_text=humanized,
            emphasis_tokens=[],
            error=codex_result.error or "codex invoke failed",
        )
    raw_tokens = parsed.get("emphasis", [])
    if not isinstance(raw_tokens, list):
        raw_tokens = []

    # Defensive filter: keep only short, verbatim-matching substrings.
    # If the LLM hallucinates a token that isn't in the rendered note,
    # drop it — the substring-search wouldn't find anything to wrap and
    # the operator just wouldn't see the highlight. Don't surface that
    # as an error.
    cleaned: List[str] = []
    for tok in raw_tokens:
        if not isinstance(tok, str):
            continue
        t = tok.strip()
        if not t:
            continue
        # Length sanity: emphasis chunks shouldn't be paragraph-length.
        if len(t) > 80:
            continue
        if t not in humanized:
            logger.debug(
                "verdict-emphasis token %r not found in rendered note; dropping",
                t,
            )
            continue
        cleaned.append(t)
        if len(cleaned) >= 5:
            break

    record_ok("extract_verdict_emphasis")
    return EmphasisResult(humanized_text=humanized, emphasis_tokens=cleaned)


if __name__ == "__main__":
    # Smoke test mirroring the screenshot James shared 2026-05-26.
    sample = {
        "speaker_attribution": "yes",
        "text_accuracy": "minor differences",
        "text_differences": (
            "Text misses \"so\" at the beginning, \"um\" before \"so it's "
            "whenever\", and \"for the overtime\" at the very end"
        ),
        "clip_integrity": "cuts-mid-word",
        "other_concerns": "none",
    }
    result = extract_verdict_emphasis(sample)
    print("HUMANIZED:", result.humanized_text)
    print("EMPHASIS :", result.emphasis_tokens)
    if result.error:
        print("ERROR    :", result.error)

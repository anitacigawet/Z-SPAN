"""consensus_vocab — vocabulary-correction consensus pipeline helpers.

V1-Consensus-1 component (see [01_Project_Overview/V1_CONSENSUS_1_SPEC.md]).
When the deterministic polish-rejection path in `quote_cleaner.py` rejects
a proposed Sonnet correction, the rejected (wrong → right) pair routes
through this module's independent-verification helpers. The Opus
vocabulary-curator agent (`agents/vocabulary-curator.md`) and this
Codex-side helper each independently propose a canonical form; when they
agree exact-string AND a two-prong safety gate passes (Prong 1
authoritative-source verification, Prong 2 wrong-form specificity), the
correction auto-promotes to `city_vocabulary_corrections.auto_apply=1`.
Disagreement OR prong-fail routes to the operator review queue.

This file carries the Codex-side query helper (C1) + the deterministic
Prong-1 / Prong-2 safety-gate checks (C2 code half). The Opus-curator
DOCTRINE side lives in the curator's role doctrine at
`agents/vocabulary-curator.md` (the Opus agent layers Wikipedia /
agenda-PDF / contextual judgment on top of these deterministic checks).
The curator's action wrapper at `parsers/scripts/vocabulary_curator_action.py`
remains unchanged — it just routes the curator's eventual promote /
reject / counter-propose decisions to the existing Flask endpoints.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from codex_cli_client import (
    CodexResult,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    invoke_codex_json,
)

logger = logging.getLogger(__name__)


VALID_CONFIDENCE_LEVELS = ("high", "medium", "low")


# --- Prong 2 specificity blocklists ---------------------------------------

# Common English words known to over-match if used as the `wrong` form of
# a global substitution. The spec's "dick → Dick Smith" pattern + the
# 2026-06-22 Staley/David-Staley citizen-speaker collision motivate this.
# Not exhaustive; the discipline is "single-token common English word" —
# the blocklist catches the worst-cases.
COMMON_WORD_BLOCKLIST = frozenset(
    {
        # The spec's worked example (operator's framing)
        "dick",
        # Standard high-collision common words
        "the", "and", "but", "for", "with", "from", "into", "onto", "upon",
        "well", "yes", "no", "ok", "okay", "sure", "right", "left",
        "their", "there", "they", "them", "this", "that", "these", "those",
        "scale", "balance", "weight", "measure", "count",
        "council", "member", "chair", "speaker", "mayor",
        # Hostile-vocabulary risk (over-match would corrupt civic text widely)
        "vote", "voted", "approve", "approved", "reject", "rejected",
        "motion", "second", "discussion", "comment", "comments",
    }
)


# Pairs whose substitution would change MEANING, not just transcription.
# The vocabulary-curator role doctrine already forbids these — this is
# the deterministic backstop for the consensus pipeline so the Opus
# agent can never accidentally route a meaning-shift pair through to
# auto-promote even if Codex agrees.
MEANING_SHIFT_PAIRS = frozenset(
    {
        ("no", "yes"), ("yes", "no"),
        ("approve", "reject"), ("approved", "rejected"),
        ("reject", "approve"), ("rejected", "approved"),
        ("for", "against"), ("against", "for"),
        ("aye", "nay"), ("nay", "aye"),
        ("passed", "failed"), ("failed", "passed"),
    }
)


CODEX_VERIFY_SYSTEM_PROMPT = """You are a vocabulary-correction verifier for civic-meeting transcripts.

A speech-to-text transcript captured a civic council meeting. A downstream
process flagged a word or phrase as potentially incorrect (the WRONG form)
and proposed a replacement (the RIGHT form). Your job: independently judge
whether the RIGHT form is the canonical form for this city/jurisdiction.

You will receive:
  - wrong_form: the form the transcript currently contains
  - proposed_right: the form a separate process proposed as canonical
  - city, state: the jurisdiction
  - surrounding_text: the sentence/quote the wrong form appears in (if available)
  - meeting_type: the kind of meeting (e.g. "City Council", "Planning Commission")

Respond with a JSON object containing exactly these three keys:
  - proposed_right: string. YOUR independent take on the canonical form.
      * If you agree with the input proposed_right, return it verbatim.
      * If you have a better alternative, return that instead.
      * If you believe the wrong_form is actually correct as-is (no fix
        needed), return the wrong_form verbatim.
  - reasoning: string. ONE sentence explaining your reasoning.
  - confidence: string. Exactly one of "high" / "medium" / "low".
      * "high" = clear proper noun + city-specific knowledge confirms
      * "medium" = plausible but not independently verifiable from public sources
      * "low" = uncertain; recommend operator review

Conduct rules:
  - Form your verdict INDEPENDENTLY. Don't rubber-stamp the proposed_right.
  - Lean conservative: when uncertain, return "low" confidence + reasoning.
  - Never propose meaning-changing corrections (e.g. "no"→"yes", "approved"→"rejected").
  - If the wrong_form is a common English word that would over-match in
    arbitrary text ("dick", "well", "their", "scale"), flag with "low"
    confidence and reasoning that a global substitution would over-match.
"""


@dataclass
class CodexProposal:
    """Outcome of a single Codex consensus-vocab query."""

    proposed_right: Optional[str]
    reasoning: str
    confidence: str  # "high" | "medium" | "low"
    error: Optional[str] = None
    raw_result: Optional[CodexResult] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.proposed_right)


def propose_via_codex(
    wrong: str,
    right: str,
    context: Optional[dict] = None,
    *,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    timeout_seconds: int = 180,
) -> CodexProposal:
    """Query Codex CLI for an independent verdict on a wrong→right candidate.

    Parameters
    ----------
    wrong : str
        The form the transcript currently contains.
    right : str
        The form a separate process (typically the polish-rejection diff)
        proposed as canonical.
    context : dict, optional
        Optional surrounding evidence. Recognized keys:
          - city: str (e.g. "Kingman")
          - state: str (e.g. "AZ")
          - surrounding_text: str (the sentence the wrong appears in)
          - meeting_type: str (e.g. "City Council")
          - meeting_id: str (optional, surfaced for traceback only)
    model, reasoning_effort, timeout_seconds : pass-through to codex_cli_client.

    Returns
    -------
    CodexProposal
        Codex's INDEPENDENT take. `.proposed_right` may agree with the
        input `right`, may differ, or may match `wrong` (meaning Codex
        believes no correction is needed). Never raises; failures
        populate `.error` and leave `.proposed_right` as None.
    """
    if not wrong or not wrong.strip():
        return CodexProposal(
            proposed_right=None,
            reasoning="empty wrong_form; nothing to verify",
            confidence="low",
            error="wrong_form is empty",
        )
    if not right or not right.strip():
        return CodexProposal(
            proposed_right=None,
            reasoning="empty proposed_right; nothing to verify",
            confidence="low",
            error="proposed_right is empty",
        )

    ctx = context or {}
    user_payload = {
        "wrong_form": wrong,
        "proposed_right": right,
        "city": ctx.get("city"),
        "state": ctx.get("state"),
        "surrounding_text": ctx.get("surrounding_text"),
        "meeting_type": ctx.get("meeting_type"),
    }

    parsed, codex_result = invoke_codex_json(
        CODEX_VERIFY_SYSTEM_PROMPT,
        json.dumps(user_payload, ensure_ascii=False),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
    )

    if not codex_result.ok or parsed is None:
        return CodexProposal(
            proposed_right=None,
            reasoning="codex invoke failed",
            confidence="low",
            error=codex_result.error or "codex invoke failed",
            raw_result=codex_result,
        )

    proposed = parsed.get("proposed_right")
    reasoning_text = parsed.get("reasoning", "")
    confidence = parsed.get("confidence", "low")

    if not isinstance(proposed, str) or not proposed.strip():
        return CodexProposal(
            proposed_right=None,
            reasoning=str(reasoning_text) or "codex response missing proposed_right",
            confidence="low",
            error="codex response missing valid proposed_right",
            raw_result=codex_result,
        )

    if confidence not in VALID_CONFIDENCE_LEVELS:
        logger.warning(
            "codex returned unexpected confidence %r; coercing to 'low'",
            confidence,
        )
        confidence = "low"

    return CodexProposal(
        proposed_right=proposed.strip(),
        reasoning=str(reasoning_text)[:1000],
        confidence=confidence,
        raw_result=codex_result,
    )


# --- Prong 1 / Prong 2 deterministic safety-gate checks (C2 code half) ---


@dataclass
class ProngVerdict:
    """Outcome of a Prong 1 or Prong 2 deterministic check.

    `passed=True` means this prong's deterministic gate is satisfied.
    `passed=False` means it's failed and the candidate must NOT auto-promote;
    the curator routes to operator review with `reasoning` as the cited cause.
    `requires_opus_judgment=True` means none of the deterministic checks
    resolved the question — the Opus curator layers Wikipedia / agenda-PDF /
    contextual judgment on top to decide. The curator's verdict is the
    tie-breaker; deterministic helpers stop short of asserting in this case.
    """

    passed: bool
    reasoning: str
    evidence_links: List[str] = field(default_factory=list)
    requires_opus_judgment: bool = False


def check_prong_1_authoritative_source(
    right: str,
    context: Optional[dict] = None,
) -> ProngVerdict:
    """Prong 1 — deterministic authoritative-source verification.

    Checks the proposed `right` against the city's roster
    (`city_intelligence/<slug>.json § current_members`), existing
    `whisper_vocabulary_hints`, and the `city_vocabulary_corrections`
    table. If any of these contain the proposed `right` verbatim or as
    a substring of a canonical entry, the deterministic check passes
    with the matching source as evidence.

    If no deterministic source matches, `requires_opus_judgment=True`
    is set so the Opus curator agent can layer Wikipedia / agenda-PDF
    grep (per S-075 shallow scope) / contextual judgment on top. This
    helper deliberately does NOT call out to Wikipedia or PDF parsers
    — those are slower and live in the curator's judgment layer.

    Context keys read: `city` (str, required), `meeting_id` (optional).
    """
    if not right or not right.strip():
        return ProngVerdict(
            passed=False,
            reasoning="empty proposed_right; cannot verify",
        )

    ctx = context or {}
    city = ctx.get("city")
    if not city:
        return ProngVerdict(
            passed=False,
            reasoning="missing city in context; cannot run Prong 1 deterministic check",
            requires_opus_judgment=True,
        )

    right_stripped = right.strip()
    evidence: List[str] = []

    # Roster check — does the city's current_members list contain the right?
    try:
        from database import load_city_intelligence  # noqa: PLC0415
    except Exception as e:
        logger.warning("database.load_city_intelligence unavailable: %s", e)
        return ProngVerdict(
            passed=False,
            reasoning=f"database.load_city_intelligence unavailable ({e})",
            requires_opus_judgment=True,
        )

    intel = load_city_intelligence(city)
    if intel is None:
        return ProngVerdict(
            passed=False,
            reasoning=f"no city_intelligence file found for {city!r}",
            requires_opus_judgment=True,
        )

    # 1) Roster (canonical member names — strongest authoritative source)
    # Token-set match (not substring) to avoid over-matching short rights:
    # right="Sam" must NOT match name="Samantha Smith" via substring
    # ("Sam" in "Samantha") — that's an over-match. Token-set match
    # requires "Sam" to appear as a full whitespace-separated word.
    # Still catches the V1-Repair-1 cases: "Stehly" tokenizes to {Stehly}
    # which is a subset of {Jamie, Scott, Stehly} (good); "Councilmember
    # Stehly" tokenizes to {Councilmember, Stehly} and {Jamie, Scott,
    # Stehly} is a subset of THAT (matches via the reverse direction).
    right_tokens = set(right_stripped.split())
    for member in intel.get("current_members") or []:
        name = (member.get("name") or "").strip()
        if not name:
            continue
        name_tokens = set(name.split())
        if not name_tokens or not right_tokens:
            continue
        exact_match = right_stripped == name
        right_subset = right_tokens.issubset(name_tokens)
        name_subset = name_tokens.issubset(right_tokens)
        if exact_match or right_subset or name_subset:
            src = member.get("source_url") or "city_intelligence roster"
            evidence.append(f"roster:{name} ({src})")
            return ProngVerdict(
                passed=True,
                reasoning=f"matches roster entry '{name}' in {city} city_intelligence",
                evidence_links=evidence,
            )

    # 2) Existing whisper_vocabulary_hints — already-promoted canonical forms
    for hint in intel.get("whisper_vocabulary_hints") or []:
        term = (hint.get("term") or "").strip()
        if not term:
            continue
        if right_stripped == term or right_stripped in term or term in right_stripped:
            evidence.append(
                f"whisper_vocabulary_hints:{term} (promoted_by={hint.get('promoted_by', 'unknown')})"
            )
            return ProngVerdict(
                passed=True,
                reasoning=f"matches existing whisper_vocabulary_hints entry '{term}' for {city}",
                evidence_links=evidence,
            )

    # 3) Existing city_vocabulary_corrections (auto_apply=1) — already-promoted
    #    substitutions where the same `right` form has been canonically vouched.
    try:
        from database import load_vocabulary_corrections  # noqa: PLC0415

        existing_rows = load_vocabulary_corrections(city) or []
        for row in existing_rows:
            row_right = (row.get("right") or "").strip()
            row_wrong = (row.get("wrong") or "").strip()
            if not row_right:
                continue
            if (
                right_stripped == row_right
                or right_stripped in row_right
                or row_right in right_stripped
            ):
                evidence.append(
                    f"city_vocabulary_corrections:{row_wrong}→{row_right}"
                )
                return ProngVerdict(
                    passed=True,
                    reasoning=(
                        f"matches existing city_vocabulary_corrections row "
                        f"({row_wrong}→{row_right}) for {city}"
                    ),
                    evidence_links=evidence,
                )
    except Exception as e:
        logger.warning("load_vocabulary_corrections lookup failed: %s", e)
        # Non-fatal — continue to Opus-judgment fallback.

    # No deterministic source matched. The curator falls back to Opus judgment
    # (Wikipedia confirmation for landmarks; agenda-PDF grep per S-075 shallow
    # scope for individual names with roles; .gov bio page lookup for council).
    return ProngVerdict(
        passed=False,
        reasoning=(
            f"no deterministic Prong-1 source matched {right_stripped!r} for {city}; "
            "requires Opus-judgment fallback (Wikipedia / agenda-PDF / .gov bio)"
        ),
        requires_opus_judgment=True,
    )


def check_prong_2_specificity(
    wrong: str,
    right: str,
    context: Optional[dict] = None,
) -> ProngVerdict:
    """Prong 2 — deterministic wrong-form specificity check.

    Rules (all deterministic, no LLM):
      1. Common-word blocklist (the "dick" / "well" / "their" pattern).
      2. Meaning-shift detection ("no"↔"yes", "approve"↔"reject").
      3. Single-token wrong-form is suspect; multi-word phrase preferred.
      4. WHISPER_PHONETIC_VARIANT flag triggers stricter check (passed
         only if multi-token; single-token phonetic variants need
         operator confirmation).

    Context keys read: `is_phonetic_variant` (bool, default False).
    """
    if not wrong or not wrong.strip():
        return ProngVerdict(
            passed=False,
            reasoning="empty wrong_form; cannot check specificity",
        )
    if not right or not right.strip():
        return ProngVerdict(
            passed=False,
            reasoning="empty proposed_right; cannot check specificity",
        )

    ctx = context or {}
    wrong_stripped = wrong.strip()
    right_stripped = right.strip()
    wrong_lower = wrong_stripped.lower()
    wrong_tokens = wrong_stripped.split()

    # Rule order: most-specific architectural concern first. Meaning-shift
    # (D-100 neutrality) outranks common-word (specificity) — when both
    # would fire, surface the meaning-shift reasoning so the operator sees
    # the load-bearing concern rather than the less-specific one.

    # Rule 1 — meaning-shift detection (D-100 neutrality)
    if (wrong_lower, right_stripped.lower()) in MEANING_SHIFT_PAIRS:
        return ProngVerdict(
            passed=False,
            reasoning=(
                f"({wrong_stripped!r} → {right_stripped!r}) is a meaning-shift pair, "
                "not a transcription correction. Categorical reject per neutrality doctrine."
            ),
        )

    # Rule 2 — common-word blocklist (single-token check)
    if len(wrong_tokens) == 1 and wrong_lower in COMMON_WORD_BLOCKLIST:
        return ProngVerdict(
            passed=False,
            reasoning=(
                f"wrong_form {wrong_stripped!r} is a common English word; "
                "global substitution would over-match in unrelated text. "
                "Multi-word scoped substitution required (e.g. "
                f"'Councilman {wrong_stripped} → Councilman {right_stripped}')."
            ),
        )

    # Rule 4 — WHISPER_PHONETIC_VARIANT flag stricter check
    is_phonetic = bool(ctx.get("is_phonetic_variant"))
    if is_phonetic and len(wrong_tokens) == 1:
        return ProngVerdict(
            passed=False,
            reasoning=(
                f"WHISPER_PHONETIC_VARIANT flag set + wrong_form {wrong_stripped!r} "
                "is single-token. Phonetic variants are more likely to over-generalize "
                "than verbatim corpus errors — multi-word context or explicit operator "
                "confirmation required."
            ),
        )

    # Rule 3 — single-token wrong-form is suspect but not categorically rejected.
    # Pass with a caveat in reasoning so the curator can layer judgment.
    if len(wrong_tokens) == 1:
        return ProngVerdict(
            passed=True,
            reasoning=(
                f"wrong_form {wrong_stripped!r} is single-token but not on the "
                "common-word blocklist; specificity check passes. Curator should "
                "still consider citizen-speaker collision per agents/vocabulary-curator.md."
            ),
        )

    # Multi-word phrase — strongest case
    return ProngVerdict(
        passed=True,
        reasoning=(
            f"wrong_form {wrong_stripped!r} is multi-word ({len(wrong_tokens)} tokens); "
            "low collision risk with unrelated text."
        ),
    )


# --- C4: polish-rejection → consensus queue routing ---------------------


def _extract_first_token_diff(
    original: str,
    polished: str,
) -> Optional[tuple[str, str]]:
    """Return the (wrong, right) single-token diff between original + polished.

    Tokenizes both on whitespace, walks them in parallel, returns the first
    token where alphanumeric content differs (ignoring case + surrounding
    punctuation). Returns None when:
      - Token counts differ by more than 2 (the diff is multi-word or
        has a different shape; let the curator compute it at processing
        time with full context).
      - No alphanumeric difference is found (shouldn't happen — caller
        only calls this for word-level rejection cases).
    """
    if not original or not polished:
        return None
    orig_tokens = original.split()
    poli_tokens = polished.split()
    if abs(len(orig_tokens) - len(poli_tokens)) > 2:
        return None

    def _norm(tok: str) -> str:
        return "".join(c for c in tok.lower() if c.isalnum())

    for i in range(min(len(orig_tokens), len(poli_tokens))):
        if _norm(orig_tokens[i]) != _norm(poli_tokens[i]):
            return (orig_tokens[i], poli_tokens[i])
    return None


def route_polish_rejection_to_consensus(
    polished_quote,
    city_name: str,
    *,
    meeting_id: Optional[int] = None,
    quote_id: Optional[int] = None,
) -> Optional[Dict]:
    """Route a word-level-rejected PolishedQuote to the consensus queue.

    Called by quote-precompute / polish callers after `polish_for_display`
    returns. No-op when:
      - The PolishedQuote has no rejected_polish_proposal (success case
        or longer-than-input rejection — neither is a vocab candidate).
      - city_name is missing (can't enqueue without a city).

    Returns:
      - None when no enqueue happened (no-op cases)
      - dict {id, created, status, wrong_token, right_token,
              is_phonetic_variant, sibling_count} on enqueue
    """
    if polished_quote is None:
        return None
    rejected = getattr(polished_quote, "rejected_polish_proposal", None)
    if not rejected:
        return None
    if not city_name:
        return None

    original = getattr(polished_quote, "original", "") or ""
    if not original:
        return None

    # Extract a lightweight diff at queue-time so the operator review queue
    # can show (wrong → right) at a glance. The curator will recompute /
    # refine at processing time with full context.
    diff = _extract_first_token_diff(original, rejected)
    wrong_token, right_token = (diff if diff else (None, None))

    is_phonetic = False
    sibling_wrongs: List[str] = []
    sibling_count = 0
    if wrong_token and right_token:
        try:
            from database import detect_phonetic_variant_for_right  # noqa: PLC0415

            det = detect_phonetic_variant_for_right(
                city_name, wrong_token, right_token,
            )
            is_phonetic = bool(det.get("is_phonetic_variant"))
            sibling_wrongs = det.get("sibling_wrongs") or []
            sibling_count = det.get("sibling_count") or 0
        except Exception as e:
            logger.warning(
                "detect_phonetic_variant_for_right lookup failed at queue-time: %s", e
            )

    try:
        from database import enqueue_polish_rejection_candidate  # noqa: PLC0415

        result = enqueue_polish_rejection_candidate(
            city_name=city_name,
            original_text=original,
            polished_proposal=rejected,
            meeting_id=meeting_id,
            quote_id=quote_id,
            wrong_token=wrong_token,
            right_token=right_token,
            is_phonetic_variant=is_phonetic,
            sibling_wrongs=sibling_wrongs,
        )
        result["wrong_token"] = wrong_token
        result["right_token"] = right_token
        result["is_phonetic_variant"] = is_phonetic
        result["sibling_count"] = sibling_count
        return result
    except Exception as e:
        logger.warning("enqueue_polish_rejection_candidate failed: %s", e)
        return None


# --- C5: pending-review row processing (consensus check + decision routing) ---


def process_pending_consensus_row(
    row_id: int,
    curator_proposed_right: str,
    curator_reasoning: str,
    *,
    curator_proposed_wrong: Optional[str] = None,
    skip_codex: bool = False,
    codex_proposal_override: Optional[CodexProposal] = None,
) -> Dict:
    """V1-Consensus-1 C5 — process a single pending-review row.

    The Opus vocabulary-curator agent calls this with its own verdict at
    heartbeat-time.

    `curator_proposed_wrong` (added post-brainstorm-audit 2026-06-22) is
    the curator's optional override of the queue row's `wrong_token`.
    The C4 first-token diff extractor records only the first single-
    token difference between original_text + polished_proposal (e.g.
    "Antivine Avenue" → "Andy Devine Avenue" extracts ("Antivine",
    "Andy") — single tokens). When the curator examines the row's
    original_text + polished_proposal and recognizes the substantive
    diff is multi-word (e.g. the canonical substitution should be
    "Antivine Avenue → Andy Devine Avenue"), the curator passes the
    widened wrong-form here. The upsert + Prong 2 then use the wider
    pair. Defaults to the row's wrong_token when curator doesn't
    explicitly override.

    The orchestrator:

      1. Loads the pending row (city, meeting, wrong_token, right_token,
         is_phonetic_variant, original_text, polished_proposal).
      2. Calls propose_via_codex with the row's wrong/right tokens + a
         context dict carrying city/state/surrounding_text. Skipped when
         `skip_codex=True` (smoke testing) or when an override is passed.
      3. Runs check_prong_1_authoritative_source on the curator's
         proposed_right (if Prong 1 doesn't pass deterministically and
         requires_opus_judgment=True, the curator's reasoning + this
         function's decision routing takes the conservative default —
         route to review).
      4. Runs check_prong_2_specificity on (effective_wrong, agreed_right)
         with the is_phonetic_variant flag from the row.
      5. Compares Codex's proposed_right to curator's exact-string.
      6. Decides:
         - consensus exact-match AND prong_1.passed AND prong_2.passed
           → consensus_match_promoted (auto-promote to
             city_vocabulary_corrections using `effective_wrong` +
             `curator_proposed_right` as the substitution pair, with
             auto_apply=1 + promoted_by='codex-opus-consensus')
         - consensus mismatch → consensus_disagreement_review
         - prong fail → prong_fail_review
      7. Calls resolve_pending_review to stamp the verdict.

    `skip_codex=True` + `codex_proposal_override` together let smoke
    tests run deterministically without live Codex invocations.

    Returns dict with the final row state + decision metadata.
    """
    # Lazy imports keep the module light when the curator path isn't being
    # walked.
    from database import (  # noqa: PLC0415
        get_pending_review_row,
        resolve_pending_review,
        upsert_vocabulary_correction,
        mark_correction_promoted,
    )

    row = get_pending_review_row(row_id)
    if row is None:
        return {"error": f"pending_review row {row_id} not found"}
    if row.get("status") != "pending":
        return {
            "error": f"row {row_id} already resolved as {row.get('status')!r}",
            "row": row,
        }

    wrong_token = (row.get("wrong_token") or "").strip()
    right_token_existing = (row.get("right_token") or "").strip()
    city_name = (row.get("city_name") or "").strip()
    is_phonetic = bool(row.get("is_phonetic_variant"))
    curator_proposed_right = (curator_proposed_right or "").strip()

    # Effective wrong-form: curator's widened multi-word version when
    # provided, else the C4 first-token diff from the row. This is the
    # form that gets persisted to city_vocabulary_corrections + Prong 2'd.
    effective_wrong = (
        (curator_proposed_wrong or "").strip() or wrong_token
    )

    if not effective_wrong or not curator_proposed_right or not city_name:
        return {
            "error": "row missing wrong_token (or curator override), "
                     "city_name, or curator_proposed_right is empty",
            "row": row,
        }

    # 1) Codex independent verdict — query on the effective_wrong so Codex
    # sees the curator's widened scope (or the single-token C4 diff when
    # not widened). Both LLMs propose against the same wrong-form.
    if codex_proposal_override is not None:
        codex_proposal = codex_proposal_override
    elif skip_codex:
        codex_proposal = CodexProposal(
            proposed_right=None,
            reasoning="codex skipped (smoke mode)",
            confidence="low",
            error="codex_skipped",
        )
    else:
        codex_proposal = propose_via_codex(
            wrong=effective_wrong,
            right=right_token_existing or curator_proposed_right,
            context={
                "city": city_name,
                "state": "AZ",  # V1 scope is AZ-only per D-124
                "surrounding_text": row.get("original_text"),
                "meeting_type": "City Council",
            },
        )

    codex_right = (codex_proposal.proposed_right or "").strip()

    # 2) Prong 1 — authoritative-source verification (on curator's proposal)
    prong_1 = check_prong_1_authoritative_source(
        right=curator_proposed_right,
        context={"city": city_name, "meeting_id": row.get("meeting_id")},
    )

    # 3) Prong 2 — wrong-form specificity (on the effective_wrong so a
    # widened multi-word wrong passes Rule 3's "multi-word phrase" leg
    # instead of being treated as a single-token candidate).
    prong_2 = check_prong_2_specificity(
        wrong=effective_wrong,
        right=curator_proposed_right,
        context={"is_phonetic_variant": is_phonetic},
    )

    # 4) Consensus check (case-sensitive whole-string compare)
    consensus_match = (
        codex_proposal.ok
        and codex_right == curator_proposed_right
    )

    # 5) Decision routing
    if consensus_match and prong_1.passed and prong_2.passed:
        # Auto-promote using the effective_wrong (curator-widened when
        # provided, else C4's single-token diff). The substitution pair
        # persisted to city_vocabulary_corrections is the pair both LLMs
        # agreed on + both prongs cleared.
        upsert_result = upsert_vocabulary_correction(
            city_name=city_name,
            wrong=effective_wrong,
            right=curator_proposed_right,
            source_response_file=f"V1-Consensus-1:pending_review_id={row_id}",
        )
        correction_id = upsert_result.get("id")
        if correction_id is not None:
            mark_correction_promoted(
                correction_id=correction_id,
                promoted_by="codex-opus-consensus",
            )
        status = "consensus_match_promoted"
        resolution_action = (
            f"promoted to city_vocabulary_corrections id={correction_id} "
            f"(auto_apply=1, promoted_by=codex-opus-consensus)"
        )
    elif not consensus_match:
        status = "consensus_disagreement_review"
        resolution_action = (
            f"codex proposed {codex_right!r} ({codex_proposal.confidence}); "
            f"curator proposed {curator_proposed_right!r}. exact-string mismatch."
        )
    else:
        # Consensus match but prong failed
        status = "prong_fail_review"
        prong_reason = []
        if not prong_1.passed:
            prong_reason.append(f"Prong 1: {prong_1.reasoning}")
        if not prong_2.passed:
            prong_reason.append(f"Prong 2: {prong_2.reasoning}")
        resolution_action = (
            f"consensus exact-match on {curator_proposed_right!r} but "
            f"prong fail: {'; '.join(prong_reason)}"
        )

    # 6) Persist resolution
    updated_row = resolve_pending_review(
        row_id=row_id,
        status=status,
        codex_proposed_right=codex_right or None,
        codex_confidence=codex_proposal.confidence,
        codex_reasoning=codex_proposal.reasoning,
        curator_proposed_right=curator_proposed_right,
        curator_reasoning=curator_reasoning,
        prong_1_passed=prong_1.passed,
        prong_1_reasoning=prong_1.reasoning,
        prong_1_evidence=prong_1.evidence_links,
        prong_2_passed=prong_2.passed,
        prong_2_reasoning=prong_2.reasoning,
        resolution_action=resolution_action,
        resolved_by="codex-opus-consensus",
    )

    return {
        "row_id": row_id,
        "status": status,
        "consensus_match": consensus_match,
        "codex_proposed_right": codex_right or None,
        "codex_confidence": codex_proposal.confidence,
        "curator_proposed_right": curator_proposed_right,
        "curator_proposed_wrong": curator_proposed_wrong,
        "effective_wrong": effective_wrong,
        "prong_1_passed": prong_1.passed,
        "prong_2_passed": prong_2.passed,
        "resolution_action": resolution_action,
        "updated_row": updated_row,
    }


# --- Smoke + main entry ---------------------------------------------------


def smoke() -> dict:
    """Smoke entry: cover the V1-Repair-1 + V1-Consensus-1 worked examples.

    Codex query: Mojave County → Mohave County (Kingman, AZ).
    Prong-1 checks: Mohave County (no roster match but no city), Stehly
      (should match roster), Antivine→Andy Devine (Andy Devine in hints).
    Prong-2 checks: Antivine (multi-token pass), Staley (single-token
      pass with caveat), dick (common-word block), no→yes (meaning shift).
    """
    proposal = propose_via_codex(
        wrong="Mojave County",
        right="Mohave County",
        context={
            "city": "Kingman",
            "state": "AZ",
            "surrounding_text": (
                "We're recommending the Mojave County board approve this "
                "resolution at their next meeting."
            ),
            "meeting_type": "City Council",
        },
    )

    prong_1_cases = [
        # Should HIT roster ("Stehly" is substring of "Jamie Scott Stehly")
        ("Stehly", {"city": "Kingman"}),
        # Should HIT whisper_vocabulary_hints ("Andy Devine" is canonical)
        ("Andy Devine", {"city": "Kingman"}),
        # Should MISS deterministically — Wikipedia/PDF-grade landmark
        ("Mohave County", {"city": "Kingman"}),
        # Missing city — should require Opus judgment
        ("Stehly", {}),
    ]
    prong_1_results = [
        {
            "input_right": r,
            "city": (c.get("city") or "<unset>"),
            "verdict": check_prong_1_authoritative_source(r, c).__dict__,
        }
        for r, c in prong_1_cases
    ]

    prong_2_cases = [
        # Multi-token pass (V1-Repair-1 canonical case)
        ("Antivine Avenue", "Andy Devine Avenue", {}),
        # Single-token pass with caveat (curator should layer judgment)
        ("Antivine", "Andy Devine", {}),
        # Common-word block (spec worked example)
        ("dick", "Dick Smith", {}),
        # Meaning-shift block
        ("no", "yes", {}),
        # WHISPER_PHONETIC_VARIANT flag + single-token = block
        ("Dikins", "Dykens", {"is_phonetic_variant": True}),
        # WHISPER_PHONETIC_VARIANT flag + multi-token = pass
        ("Councilmember Dikins", "Councilmember Dykens", {"is_phonetic_variant": True}),
    ]
    prong_2_results = [
        {
            "wrong": w,
            "right": r,
            "is_phonetic_variant": c.get("is_phonetic_variant", False),
            "verdict": check_prong_2_specificity(w, r, c).__dict__,
        }
        for w, r, c in prong_2_cases
    ]

    return {
        "codex_query": {
            "ok": proposal.ok,
            "proposed_right": proposal.proposed_right,
            "reasoning": proposal.reasoning,
            "confidence": proposal.confidence,
            "error": proposal.error,
            "duration_seconds": (
                round(proposal.raw_result.duration_seconds, 2)
                if proposal.raw_result
                else None
            ),
        },
        "prong_1_deterministic_checks": prong_1_results,
        "prong_2_specificity_checks": prong_2_results,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(smoke(), indent=2, default=str))

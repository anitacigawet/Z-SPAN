"""
Z-SPAN output fetcher — orchestrates the V1-RAG-3 outputs for a single
work order. Invoked by the worker daemon, not directly.

For each requested output type:
  1. Loads the matching prompt file from ../prompts/ (front-matter + body)
  2. Dispatches to the strategy handler (Qdrant retrieve + `claude -p`
     Sonnet synthesize, the quote-extraction linker pass, or the Whisper
     transcript path)
  3. Persists the result to meetings_cache.db via save_notebook_output()

The registry below (OUTPUT_TYPE_REGISTRY) is the authoritative
output_type → (prompt file, strategy) map.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

# Make `parsers/` importable so we can use database.save_notebook_output
_PARSERS_DIR = Path(__file__).resolve().parent.parent / "council_navigator" / "parsers"
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

# D-099 Phase 2 C5: swap to HTTP backend when ZSPAN_DB_BACKEND=http.
from zspan_pipeline.db_backend import install_db_backend  # noqa: E402
install_db_backend()

from database import (  # noqa: E402
    save_notebook_output,
    is_output_already_present,
    load_city_intelligence,
    save_member_attendance_batch,
    save_member_quotes_batch,
    get_meeting_city,
    get_resolved_video_url,
)

from .prompt_loader import load_prompt_with_meta  # noqa: E402
from .truth_packet import gate_truth_packet, TruthPacketResult  # noqa: E402
from . import citation_validator  # noqa: E402 — synopsis verbatim anchors
from . import qdrant_synthesizer  # noqa: E402 — V1-RAG-3 backend
from . import qdrant_quote_extractor  # noqa: E402 — V1-RAG-3 quote linker (D-NNN post-C5)

logger = logging.getLogger(__name__)


_SYNOPSIS_VERBATIM_ANCHOR_RE = re.compile(r'\[at "(?P<quote>[^\r\n]+?)"\]')


# ─── S-009 chunk 3 (2026-06-19): truth-packet auto-run-first gate ───
# Per S-009 spec § 7.1, when truth_packet is in a WO's requested_outputs the
# fetcher runs it FIRST and gates the rest of the WO on the verdict. The
# class hierarchy below propagates the verdict + reason from the gate
# decision up to the worker, which transitions the WO state accordingly:
#   halt      → state='failed_truth_packet'           (operator re-pastes URL)
#   ambiguous → state='awaiting_truth_packet_review'  (operator confirms)
#   pass      → continues normally (no exception raised)
#
# Both exception classes carry the TruthPacketResult so the worker can
# persist the gate's observations + reason for operator review without
# re-parsing the raw gate response.
#
# DORMANT ACTIVATION NOTE: truth_packet is NOT in default requested_outputs
# until the operator's truth_packet.md prompt review clears (see OUTPUT_TYPE_REGISTRY
# comment block). The wire-in below is the code surface; activation is the
# downstream default-list flip + a per-WO opt-in toggle if needed. Per James
# 2026-06-19: build the code, defer testing + activation.
class TruthPacketHaltError(RuntimeError):
    """Truth-packet gate returned a halt verdict — WO must not proceed.

    Carries the `TruthPacketResult` so the worker can persist the gate's
    reason + observations into the WO state writeback. The worker
    transitions WO state to 'failed_truth_packet' (operator re-pastes the
    video URL after investigating the gate's reason).
    """

    def __init__(self, result: TruthPacketResult) -> None:
        super().__init__(result.reason)
        self.result = result


class TruthPacketAmbiguousError(RuntimeError):
    """Truth-packet gate returned ambiguous — needs operator review.

    Carries the `TruthPacketResult`. The worker transitions WO state to
    'awaiting_truth_packet_review' (operator inspects observations +
    decides whether to proceed / re-paste / abandon). Unlike halt, this
    is recoverable without a URL change — the operator may simply confirm
    the source is correct (e.g. a non-Council government meeting that
    the operator deliberately wants processed).
    """

    def __init__(self, result: TruthPacketResult) -> None:
        super().__init__(result.reason)
        self.result = result


# ── Kill-survivability / smart retry (added 2026-05-12) ──────────────
# When a previous run wrote a successful row to notebook_outputs for a
# given (meeting, output_type), the default behavior is to SKIP that
# output on subsequent retries — only re-generate the truly-missing ones.
# This makes [RETRY] kill-survivable: if the worker died after producing
# 9 of 12 outputs, retrying regenerates only the 3
# missing ones, not all 12.
#
# Operator override: set ZSPAN_FORCE_REGENERATE=1 to bypass the skip and
# re-generate everything from scratch (use case: prompt iteration, where
# the operator wants the new prompt's output to replace the old).
FORCE_REGENERATE = os.environ.get("ZSPAN_FORCE_REGENERATE", "").strip() in (
    "1", "true", "yes", "on"
)


def _transcript_overwrite_block(
    meeting_id: int, output_type: str,
) -> Optional[dict]:
    """Return a skip result when a successful transcript is already frozen."""
    if output_type != "transcript_words":
        return None
    if os.environ.get("ZSPAN_FORCE_TRANSCRIPT_OVERWRITE", "").strip() == "1":
        return None
    existing = is_output_already_present(meeting_id, output_type)
    if not existing:
        return None
    logger.warning(
        "output[transcript_words] meeting=%s → skipped_existing; transcript "
        "overwrite requires ZSPAN_FORCE_TRANSCRIPT_OVERWRITE=1",
        meeting_id,
    )
    return {
        "output_type": output_type,
        "status": "skipped_existing",
        "generated_at": existing.get("generated_at"),
    }

# ── T-009 Phase 0a Whisper pipeline gate ──────────────────────────
# Whisper word-level transcripts are cost-bearing (~$0.006/min, so ~$0.72-
# 1.44 per typical meeting). Default ON; set ZSPAN_WHISPER_ENABLED=0 to
# skip when iterating on other parts of the pipeline or running offline.
WHISPER_ENABLED = os.environ.get(
    "ZSPAN_WHISPER_ENABLED", "1"
).strip().lower() not in ("0", "false", "off", "no")


# ── Media output configuration ────────────────────────────────────
# Default: 02_Core_Project/council_navigator/media/<meeting_id>/
# Override with ZSPAN_MEDIA_ROOT.
DEFAULT_MEDIA_ROOT = (
    Path(__file__).resolve().parent.parent
    / "council_navigator"
    / "media"
)
MEDIA_ROOT = Path(os.environ.get("ZSPAN_MEDIA_ROOT", str(DEFAULT_MEDIA_ROOT)))
# URL prefix that the Express static handler serves at.
MEDIA_URL_PREFIX = os.environ.get("ZSPAN_MEDIA_URL_PREFIX", "/media")


def _meeting_media_dir(meeting_id: int) -> Path:
    p = MEDIA_ROOT / str(meeting_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _media_url_for(meeting_id: int, filename: str) -> str:
    return f"{MEDIA_URL_PREFIX}/{meeting_id}/{filename}"


# ── Output type registry ──────────────────────────────────────────
# Each output_type maps to (prompt_filename, strategy). This is the
# authoritative list of producible outputs; every strategy here is a
# live V1-RAG-3 handler (qdrant_synthesize / qdrant_synthesize_multi /
# qdrant_extract_quotes / transcript_words).
#
# Insertion order matters: Python dicts preserve order since 3.7, and
# fetch_all_outputs runs requested_outputs in registry order (when no
# explicit order is given).
#
# ── Dormant output types (registered NOWHERE below on purpose) ─────
# Six output types have working persistence machinery but no live
# fetch strategy; they re-enter this registry when their extraction
# migrates to a V1-RAG-3 strategy AND the operator's prompt review clears
# (prompts/PROMPT_REVIEW_LEDGER.md):
#   truth_packet          — S-009 attestation gate. The gate wiring in
#                           fetch_all_outputs + truth_packet.py stays
#                           live-dormant; requesting it per-WO today
#                           yields an ambiguous verdict (fetch errors
#                           as unknown type), which fails safe.
#   motions / votes / agenda_transitions / seconds
#                         — Conversational Compiler Track B (D-087/
#                           D-088). transcript_nodes persist paths in
#                           _maybe_persist_member_output are the
#                           landing pads.
#   video_explainer_kawaii — V2 studio-rebuild variant (T-010).
# The retired pre-D-143 output types (studio media, council_quotes,
# member_attendance, member_quotes_topic, episode_tags, quotes) were
# removed outright — git history is their record.
OUTPUT_TYPE_REGISTRY = {
    "episode_tagline":         ("episode_tagline.md",         "qdrant_synthesize"),
    # V1-RAG-3 backend per D-126
    "synopsis":                ("synopsis.md",                "qdrant_synthesize"),
    # V1-RAG-3 backend per D-126
    "newsletter":              ("newsletter.md",              "qdrant_synthesize"),
    # V1-RAG-3 backend per D-126
    "key_decisions":           ("key_decisions.md",           "qdrant_synthesize"),
    # V1-CommunityCallsToAction-1 (2026-06-29) — verbatim civic asks from
    # officials directed at the public; the platform-amplifier surface
    # that flips Z-SPAN from "audits decisions" to "amplifies civic
    # invitations." Tami Ring food-bank quote (m103753 Kingman 2026-06-02)
    # is the canonical first published call. Output is a JSON array
    # parsed by BroadcastPage's Community Calls to Action accordion.
    # Honest-empty result (`[]`) is valid + expected for procedural-only
    # meetings. Composes with S-096 Portal as substrate.
    "community_calls_to_action": ("community_calls_to_action.md", "qdrant_synthesize"),
    # V1-RAG-3 backend per D-126
    "whats_next":              ("whats_next.md",              "qdrant_synthesize"),
    # V1-RAG-3 backend per D-126
    "council_sentiment":       ("council_sentiment.md",       "qdrant_synthesize"),
    # Per-question Q&A pairs, each retrieved + synthesized independently
    # against the meeting's Qdrant index.
    "suggested_questions":     ("suggested_questions.md",     "qdrant_synthesize_multi"),
    # T-012 Tracked Claims — forward-looking statements (assurances,
    # commitments, predictions, promises) that constitute long-term
    # accountability data. Sidecar persistence into the structured
    # `tracked_claims` table runs via _maybe_persist_member_output
    # post-synthesis; the persona preamble is applied in _fetch_qdrant
    # for speaker_name resolution.
    "tracked_claims":          ("tracked_claims.md",          "qdrant_synthesize"),
    # Quote-extraction linker pass (post-C5 finding 2026-06-20) —
    # Sonnet-on-Qdrant attributed-quote extraction over Whisper transcript
    # chunks + the [SYMBOLS] block + canonical roster. Persists into the
    # canonical `quotes` table via save_quotes_batch (preserves
    # verification state across re-extractions). Single unified quote
    # stream covering council members + staff + outside experts, with
    # broadcast_hero_ordinals flagging the 5-8 hero subset for
    # BroadcastPage. See 01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md.
    # Operator-only TruthBook surface at V1 per the C1 owner gate.
    "quote_extraction":        ("quote_extraction.md",        "qdrant_extract_quotes"),
    # T-009 Phase 0a — Whisper word-level transcripts. No prompt file;
    # the strategy dispatcher in fetch_one_output recognizes
    # prompt_filename is None and routes to _fetch_transcript_words.
    "transcript_words":        (None,                         "transcript_words"),
}

async def fetch_one_output(
    meeting_id: int,
    notebook_id: str,
    output_type: str,
) -> dict:
    """Process a single output type for a meeting; persist + return summary.

    Kill-survivability: if a prior run already wrote a successful row to
    notebook_outputs for this (meeting, output_type), SKIP this call and
    return status='skipped_existing'. This makes retries idempotent —
    regenerating only the genuinely-missing outputs after
    an interrupted run. Override via ZSPAN_FORCE_REGENERATE=1 (intended for
    prompt iteration, where the operator wants new prompt output to
    replace old).
    """
    if output_type not in OUTPUT_TYPE_REGISTRY:
        msg = f"Unknown output_type: {output_type}"
        save_notebook_output(meeting_id=meeting_id, notebook_id=notebook_id,
                             output_type=output_type, error=msg)
        return {"output_type": output_type, "status": "error", "error": msg}

    transcript_block = _transcript_overwrite_block(meeting_id, output_type)
    if transcript_block is not None:
        return transcript_block

    # Skip-if-already-present check (kill-survivability). The DB helper only
    # treats a row as "present" when content/url is non-empty AND error is
    # blank — mid-flight and errored rows will fall through to a real fetch.
    if output_type != "transcript_words" and not FORCE_REGENERATE:
        existing = is_output_already_present(meeting_id, output_type)
        if existing:
            logger.info(
                "output[%s] meeting=%s → skipped_existing "
                "(prior run already produced it; set ZSPAN_FORCE_REGENERATE=1 to override)",
                output_type, meeting_id,
            )
            return {
                "output_type": output_type,
                "status": "skipped_existing",
                "generated_at": existing.get("generated_at"),
            }

    prompt_filename, strategy = OUTPUT_TYPE_REGISTRY[output_type]

    # transcript_words loads no prompt file — it runs Whisper against
    # the meeting's audio entirely outside the prompt pipeline.
    # Early-dispatch here so the prompt loader / preamble plumbing
    # below stays focused on prompt-backed outputs.
    if strategy == "transcript_words":
        return await _fetch_transcript_words(meeting_id, notebook_id, output_type)

    try:
        meta, instructions = load_prompt_with_meta(prompt_filename)
    except FileNotFoundError as e:
        save_notebook_output(meeting_id=meeting_id, notebook_id=notebook_id,
                             output_type=output_type, prompt_filename=prompt_filename,
                             error=f"Prompt file missing: {e}")
        return {"output_type": output_type, "status": "error", "error": str(e)}

    # T-017 Layer 2 — prepend the city's known spelling corrections so
    # EVERY prompt honors them. Goes FIRST in the final instructions
    # (before the persona preamble / task) so the model internalizes
    # the corrections before reading anything that would render them.
    instructions = _maybe_prepend_correction_directives(meeting_id, instructions)

    if strategy == "qdrant_synthesize":
        return await _fetch_qdrant(
            meeting_id, notebook_id, output_type,
            prompt_filename, instructions,
        )
    if strategy == "qdrant_synthesize_multi":
        # Same front-matter shape as text_multi — `questions:` list.
        questions = (meta or {}).get("questions") or []
        if not isinstance(questions, list) or not questions:
            save_notebook_output(
                meeting_id=meeting_id, notebook_id=notebook_id,
                output_type=output_type, prompt_filename=prompt_filename,
                error="no `questions:` list found in prompt front-matter",
            )
            return {"output_type": output_type, "status": "error",
                    "error": "front-matter missing questions list"}
        return await _fetch_qdrant_multi(
            meeting_id, notebook_id, output_type,
            prompt_filename, questions,
        )
    if strategy == "qdrant_extract_quotes":
        return await _fetch_qdrant_extract_quotes(
            meeting_id, notebook_id, output_type,
            prompt_filename,
        )

    msg = f"Unknown retrieval strategy: {strategy}"
    save_notebook_output(meeting_id=meeting_id, notebook_id=notebook_id,
                         output_type=output_type, prompt_filename=prompt_filename, error=msg)
    return {"output_type": output_type, "status": "error", "error": msg}



# T-013 V4 — when alignment lands for a meeting (member_quotes,
# transcript_words BOTH present + word_timings populated), auto-spawn
# `build_review_queue.py` so the operator never has to click [BUILD]
# for the common case. Fire-and-forget subprocess; the existing
# operator-terminal [BUILD] button stays available for manual runs +
# rebuilds. Disable with `ZSPAN_AUTO_BUILD_REVIEW_QUEUE=0` if you'd
# rather curate which meetings get clip-extracted (e.g., during a
# Phase 1 stabilization window where the source-cache disk pressure
# is more of a concern than convenience).
_AUTO_BUILD_REVIEW_QUEUE_ENABLED = os.environ.get(
    "ZSPAN_AUTO_BUILD_REVIEW_QUEUE", "1"
).strip().lower() not in ("0", "false", "off", "no")


def _maybe_auto_build_review_queue(meeting_id: int) -> None:
    """Spawn `build_review_queue.py --meeting-id N` in the background
    once alignment has produced word_timings for a meeting's quotes.

    Idempotent at the callsite layer: skips when a BATCH_MANIFEST.json
    already exists for the meeting (the operator already built — let
    them re-run manually via [BUILD] if they want to regenerate).
    Skips when the env var is off.

    Fire-and-forget: spawns subprocess.Popen without waiting. The build
    takes ~10 min on cold cache (yt-dlp source download), <5 sec when
    cached. Blocking the worker on that would stall the next WO; the
    operator-terminal cache badge + the [CLIPS] availability surface
    the eventual result.
    """
    if not _AUTO_BUILD_REVIEW_QUEUE_ENABLED:
        return
    try:
        # Skip if already built — the CLI script regenerates idempotently
        # but spawning a subprocess just to no-op is wasteful.
        from local_fs import find_meeting_folder  # noqa: E402
        existing_dir = find_meeting_folder(meeting_id)
        if existing_dir is not None:
            manifest = existing_dir / "BATCH_MANIFEST.json"
            if manifest.exists():
                logger.info(
                    "auto-build skipped for meeting=%s: BATCH_MANIFEST.json "
                    "already present at %s",
                    meeting_id, existing_dir,
                )
                return
    except Exception as e:
        logger.warning(
            "auto-build pre-check raised (%s) — proceeding anyway", e,
        )

    import subprocess
    from pathlib import Path as _Path
    # The script lives in zspan_pipeline/scripts/ — running it as a
    # -m module requires 02_Core_Project as the import root. Resolve
    # from this file (zspan_pipeline/fetcher.py).
    cwd = str(_Path(__file__).resolve().parent.parent)
    try:
        proc = subprocess.Popen(
            ["py", "-3.11", "-m", "zspan_pipeline.scripts.build_review_queue",
             "--meeting-id", str(meeting_id)],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            # CREATE_NEW_PROCESS_GROUP keeps the child alive after the
            # parent (worker / Flask) exits — useful for a 10-min build
            # that we don't want killed if the parent restarts.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        logger.info(
            "auto-build spawned for meeting=%s: pid=%s "
            "(fire-and-forget — operator-terminal CACHE badge will surface result)",
            meeting_id, proc.pid,
        )
    except FileNotFoundError:
        # `py` launcher missing — try `python` fallback.
        try:
            proc = subprocess.Popen(
                ["python", "-m", "zspan_pipeline.scripts.build_review_queue",
                 "--meeting-id", str(meeting_id)],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            logger.info(
                "auto-build spawned (python fallback) for meeting=%s: pid=%s",
                meeting_id, proc.pid,
            )
        except FileNotFoundError as e:
            logger.warning(
                "auto-build skipped for meeting=%s: no Python launcher on PATH (%s)",
                meeting_id, e,
            )
    except Exception as e:
        logger.warning(
            "auto-build spawn failed for meeting=%s: %s "
            "(operator can click [BUILD] manually)", meeting_id, e,
        )


# Output types that need the city's persona preamble (canonical-name list)
# prepended to the prompt so the synthesis model uses exact name spellings. Per
# T-006/T-007: the Cast-page pipeline depends on this; name drift in the
# extraction breaks the (city_name, name) → member_id lookup. T-012
# tracked_claims is in this set for the same reason — speaker_name must
# resolve to a canonical roster member or the row is dropped.
_PERSONA_PREAMBLE_OUTPUTS = {
    "member_attendance", "member_quotes_topic", "tracked_claims",
    # Quotes Unification Refactor — needs canonical names for council_member
    # speaker_class rows (staff / external speakers use the names as given).
    "quotes",
    # Conversational Compiler Track B (D-087, 2026-06-05) — every Track-B
    # extraction that emits canonical speaker / member / chair names binds
    # to the [SYMBOLS] linker contract block. NOTE: this set also gates
    # the structured persistence path in `_maybe_persist_member_output`
    # below — being absent here means motions/votes wouldn't have been
    # persisted via the bridge persist branch (the data was inserted via
    # ad-hoc invocations of save_*_batch). Adding them aligns the
    # contract-prepend AND the persist-dispatch paths.
    "motions", "votes",
    "agenda_transitions", "seconds",
}


def _meeting_city(meeting_id: int) -> str | None:
    """Lookup the city_name for a meeting_id. Thin wrapper that swallows
    DB errors (caller falls through to a None-tolerant code path).
    Backend-agnostic since database.get_meeting_city handles SQLite vs
    HTTP-shim dispatch (D-099 Phase 2 C4)."""
    try:
        return get_meeting_city(meeting_id)
    except Exception as e:
        logger.warning("city lookup failed for meeting_id=%s: %s", meeting_id, e)
        return None


def _maybe_prepend_persona_preamble(
    output_type: str, meeting_id: int, instructions: str
) -> str:
    """Prepend the city's [SYMBOLS] linker contract block to the
    instructions when the output type is one that depends on canonical
    names (motions, votes, tracked_claims, member_quotes_topic, quotes,
    member_attendance, etc.).

    Replaces the older prose persona preamble (T-006, James 2026-06-05):
    the prose version was framed as an informational hint ("here are the
    names, use these spellings") and empirically failed — models still
    emitted role-prefixed forms like "Mayor Watkins" / "Counselor
    Stehly" / "Councilmember Dykins" because the soft directive didn't
    bind output. The [SYMBOLS] block is framed as a HARD LINKER CONTRACT
    (you MUST link every reference through this table; NEVER output a
    name not listed) with the canonical / alias table generated from
    `council_members` + `whisper_vocabulary_hints` +
    `city_vocabulary_corrections`. See zspan_pipeline/symbols.py for
    the builder + CONVERSATIONAL_COMPILER_SPEC.md § the linker
    contract.

    Returns the instructions unchanged when:
      - the output type doesn't need a preamble (most outputs)
      - the meeting can't be resolved to a city
      - the symbols block is empty (no council_members + no city_intel
        for the city — falls through to no-preamble path)
    """
    if output_type not in _PERSONA_PREAMBLE_OUTPUTS:
        return instructions
    city = _meeting_city(meeting_id)
    if not city:
        return instructions
    try:
        from .symbols import build_symbols_block
    except Exception as e:
        logger.warning("symbols builder import failed (%s); skipping preamble", e)
        return instructions
    block = build_symbols_block(city)
    if not block:
        logger.info(
            "symbols block empty for city=%s output=%s; skipping preamble",
            city, output_type,
        )
        return instructions
    return f"{block}\n\n---\n\n{instructions}"


# T-017 Layer 2 — output_types whose post-extraction text gets the
# city's vocabulary corrections applied. Excludes:
#   - member_quotes_topic / member_attendance: those go through V3
#     verification which corrects via gemini_correction_notes, and
#     re-running auto-corrections here would race the audited values.
#   - audio_overview / video_explainer / infographic: binary media,
#     not text. Correction for those rides on the prompt-level
#     directive prepend (`_maybe_prepend_correction_directives`).
_CITY_CORRECTION_PROSE_OUTPUTS = {
    "episode_tagline", "episode_tags", "synopsis", "newsletter",
    "key_decisions", "whats_next", "council_sentiment",
    # Conversational Compiler Track B (2026-06-05) — whole-string find/
    # replace on the JSON payload is safe for these because the only
    # text fields that hold proper nouns are speaker / motion_text /
    # per_member_votes.member / motion_reference, all of which benefit
    # from canonical-spelling substitution. Keeping the simple-text
    # treatment rather than per-field JSON correction because: (a) the
    # corrections list is short proper nouns (Dykens, Beale Street,
    # etc.) — accidental substring matches would have to be character-
    # for-character identical, which is vanishingly unlikely for
    # multi-syllable proper nouns; (b) deferring the per-field JSON
    # variant until a real false-positive surfaces.
    "motions", "votes",
    # B-3 + B-4 (2026-06-04) — same canonical-name proper-noun substitution
    # benefits apply: chair_speaker / speaker / motion_reference fields all
    # carry council names that benefit from city_vocabulary_corrections
    # substitution post-synthesis.
    "agenda_transitions", "seconds",
}


def _maybe_prepend_correction_directives(
    meeting_id: int, instructions: str
) -> str:
    """Prepend the city's known proper-noun corrections to a synthesis
    prompt as an explicit spelling directive (T-017 Layer 2). Applies to
    EVERY prompt.

    The block looks like:

        ## SPELLING CORRECTIONS
        Use these spellings exactly. If you see a different form in the
        source, the spellings below are correct:
        - "Andy Devine" (NOT "Annie Divine")
        - "POS systems" (NOT "POSOS systems")
        ...

    No-op when the meeting has no city or the city has no auto-apply
    corrections on file. Block sits BEFORE everything else (including
    the persona preamble) so the model internalizes the spellings
    before reading the task.
    """
    if not instructions:
        return instructions
    city = _meeting_city(meeting_id)
    if not city:
        return instructions
    try:
        from database import load_vocabulary_corrections
        corrections = load_vocabulary_corrections(city, auto_apply_only=True)
    except Exception as e:
        logger.warning("city corrections lookup failed for %s: %s", city, e)
        return instructions
    if not corrections:
        return instructions
    lines = [
        "## SPELLING CORRECTIONS",
        "Use these spellings exactly. If you see a different form in the source, the spellings below are correct:",
    ]
    for c in corrections:
        lines.append(f"- \"{c['right']}\" (NOT \"{c['wrong']}\")")
    block = "\n".join(lines)
    return f"{block}\n\n---\n\n{instructions.lstrip()}"


def _maybe_apply_city_corrections(
    output_type: str, meeting_id: int, content: str
) -> str:
    """Apply the city's known proper-noun corrections to a synthesized
    text output (T-017 Layer 2). Self-improving across meetings: every
    correction in city_vocabulary_corrections (auto_apply=1) gets applied
    here on every text Studio output before persistence.

    Dispatches by output_type:
      - free-form prose (synopsis, newsletter, key_decisions, etc.)
                        → apply to the whole string.
      - everything else → skip (defensive: only apply where we know
        the shape).

    No-op when the meeting has no city or no auto-apply corrections.
    """
    if not content:
        return content
    city = _meeting_city(meeting_id)
    if not city:
        return content
    try:
        from database import apply_city_corrections
    except Exception as e:
        logger.warning("apply_city_corrections import failed: %s", e)
        return content

    if output_type in _CITY_CORRECTION_PROSE_OUTPUTS:
        new_content, log = apply_city_corrections(city, content)
        applied = [e for e in (log or []) if e.get("count", 0) > 0]
        if applied:
            logger.info(
                "city corrections applied to %s meeting=%s city=%s: %s",
                output_type, meeting_id, city,
                ", ".join(
                    f"{e['from']!r}->{e['to']!r}(x{e['count']})"
                    for e in applied
                ),
            )
        return new_content

    return content



def _maybe_persist_member_output(
    output_type: str, meeting_id: int, content: str
) -> None:
    """Parse the JSON `content` produced by member_attendance or
    member_quotes_topic and persist into the structured `member_attendance`
    / `member_quotes` tables. The raw text already lives in
    `notebook_outputs` (kept for auditability); this is a sidecar write
    so the Cast-page API can JOIN by member_id efficiently.

    Defensive on every failure mode:
      - Not the right output_type → no-op.
      - No city resolvable → log + skip.
      - JSON parse fails → log + skip (raw row in notebook_outputs is
        still the source of truth; operator review surface flags this).
      - Member name doesn't resolve to canonical roster → row skipped in
        the batch helper, counts logged.
    """
    if output_type not in _PERSONA_PREAMBLE_OUTPUTS:
        return
    if not content:
        return
    city = _meeting_city(meeting_id)
    if not city:
        logger.warning(
            "cannot persist %s — meeting %s has no city_name",
            output_type, meeting_id,
        )
        return

    # Strip markdown fence if the model ignored the "no fence" rule.
    import re
    fence = re.match(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", content, re.DOTALL)
    json_str = fence.group(1) if fence else content
    try:
        import json as _json
        data = _json.loads(json_str)
    except Exception as e:
        logger.warning(
            "%s JSON parse failed for meeting=%s (%s) — raw stays in notebook_outputs",
            output_type, meeting_id, e,
        )
        return

    if not isinstance(data, dict):
        logger.warning("%s for meeting=%s is not a JSON object", output_type, meeting_id)
        return

    if output_type == "member_attendance":
        items = data.get("attendance")
        if not isinstance(items, list):
            logger.warning(
                "member_attendance for meeting=%s missing `attendance` array",
                meeting_id,
            )
            return
        result = save_member_attendance_batch(meeting_id, city, items)
        logger.info(
            "member_attendance persisted for meeting=%s: %s",
            meeting_id, result,
        )
    elif output_type == "member_quotes_topic":
        items = data.get("quotes")
        if not isinstance(items, list):
            logger.warning(
                "member_quotes_topic for meeting=%s missing `quotes` array",
                meeting_id,
            )
            return
        result = save_member_quotes_batch(meeting_id, city, items)
        logger.info(
            "member_quotes persisted for meeting=%s: %s", meeting_id, result,
        )
        # T-009 Phase 0b auto-align: if transcript_words is already in
        # notebook_outputs for this meeting (e.g., this WO is being
        # reprocessed and transcript_words landed first OR a prior WO
        # populated it), kick off alignment for the newly-saved quotes.
        # If transcript_words isn't there yet, align_meeting_quotes
        # returns no_transcript=True and is a no-op — the alignment
        # will run when transcript_words eventually lands (the
        # symmetric trigger in _fetch_transcript_words).
        try:
            from quote_align import align_meeting_quotes  # noqa: E402
            align_stats = align_meeting_quotes(meeting_id)
            if align_stats.get("aligned") or align_stats.get("failed"):
                logger.info(
                    "member_quotes_topic meeting=%s → align: %s",
                    meeting_id, align_stats,
                )
            # T-013 V4 — if alignment actually produced word_timings,
            # fire the auto-build so clips are ready when the operator
            # checks back. Skipped if a manifest already exists.
            if align_stats.get("aligned"):
                _maybe_auto_build_review_queue(meeting_id)
        except Exception as e:
            logger.warning(
                "member_quotes_topic meeting=%s: post-save quote alignment "
                "raised (%s); not blocking", meeting_id, e,
            )
    elif output_type == "tracked_claims":
        # T-012 — same shape as member_quotes_topic with the JSON array
        # under `tracked_claims` rather than `quotes`. Sidecar persist
        # writes structured rows into the `tracked_claims` table; raw
        # row in notebook_outputs stays as audit. Karaoke alignment
        # runs symmetrically with member_quotes_topic.
        items = data.get("tracked_claims")
        if not isinstance(items, list):
            logger.warning(
                "tracked_claims for meeting=%s missing `tracked_claims` array",
                meeting_id,
            )
            return
        from database import save_tracked_claims_batch
        result = save_tracked_claims_batch(meeting_id, city, items)
        logger.info(
            "tracked_claims persisted for meeting=%s: %s", meeting_id, result,
        )
        try:
            from quote_align import align_tracked_claims_for_meeting  # noqa: E402
            align_stats = align_tracked_claims_for_meeting(meeting_id)
            if align_stats.get("aligned") or align_stats.get("failed"):
                logger.info(
                    "tracked_claims meeting=%s → align: %s",
                    meeting_id, align_stats,
                )
        except Exception as e:
            logger.warning(
                "tracked_claims meeting=%s: post-save alignment "
                "raised (%s); not blocking", meeting_id, e,
            )
    elif output_type == "quotes":
        # Quotes Unification Refactor (2026-05-26). Unified extraction —
        # `data` carries TWO top-level keys: `quotes` (list of quote dicts)
        # AND `broadcast_hero_ordinals` (list of quote_ordinal_id strings
        # the prompt picked as the 5-8 hero subset). save_quotes_batch
        # uses content_hash + UNIQUE(meeting_id, content_hash) to preserve
        # verification state across re-extractions (the V3-wipe bug is
        # structurally fixed; see DECISIONS.md § D-052 once landed).
        items = data.get("quotes")
        if not isinstance(items, list):
            logger.warning(
                "quotes for meeting=%s missing `quotes` array",
                meeting_id,
            )
            return
        hero_ordinals = data.get("broadcast_hero_ordinals")
        if not isinstance(hero_ordinals, list):
            # Soft-fallback: prompt may have returned a different name
            # (e.g., `hero_ordinals`) or omitted it entirely. Log + treat
            # as "no hero subset for this meeting" rather than failing.
            logger.warning(
                "quotes for meeting=%s missing `broadcast_hero_ordinals` "
                "array; defaulting to empty hero set", meeting_id,
            )
            hero_ordinals = []
        from database import save_quotes_batch
        result = save_quotes_batch(
            meeting_id=meeting_id,
            items=items,
            broadcast_hero_ordinals=hero_ordinals,
            city_name=city,
        )
        logger.info(
            "quotes persisted for meeting=%s: %s (hero_subset_size=%d)",
            meeting_id, result, len(hero_ordinals),
        )
        # Symmetric alignment trigger (mirrors the member_quotes_topic
        # branch above). If transcript_words is already on disk, align
        # now; otherwise the transcript-side trigger picks it up later.
        try:
            from quote_align import align_quotes_for_meeting  # noqa: E402
            align_stats = align_quotes_for_meeting(meeting_id)
            if align_stats.get("aligned") or align_stats.get("failed"):
                logger.info(
                    "quotes meeting=%s → align: %s",
                    meeting_id, align_stats,
                )
            # Auto-build review queue if alignment produced word_timings.
            # Same trigger semantics as member_quotes_topic — skipped if
            # a manifest already exists.
            if align_stats.get("aligned"):
                _maybe_auto_build_review_queue(meeting_id)
        except Exception as e:
            logger.warning(
                "quotes meeting=%s: post-save alignment raised (%s); "
                "not blocking", meeting_id, e,
            )
    elif output_type == "motions":
        # Conversational Compiler Track B Chunk B-1 (2026-06-05). Same
        # extraction pattern as tracked_claims, but the JSON array lands
        # under `motions` and persists to transcript_nodes
        # (node_type='Motion') rather than tracked_claims. Per Decision
        # #8a — the model does the extraction; we just persist.
        items = data.get("motions")
        if not isinstance(items, list):
            logger.warning(
                "motions for meeting=%s missing `motions` array",
                meeting_id,
            )
            return
        from database import save_motions_batch
        result = save_motions_batch(meeting_id, city, items)
        logger.info(
            "motions persisted for meeting=%s: %s", meeting_id, result,
        )
        # No karaoke alignment for motions — they're procedural events,
        # not quoted speech the karaoke UI displays. Skip the alignment
        # hook entirely (unlike tracked_claims / quotes).
    elif output_type == "votes":
        # Conversational Compiler Track B Chunk B-2 (2026-06-05). Vote
        # nodes — the body's response to motions. Persisted as
        # transcript_nodes with node_type='Vote'. The per_member_votes
        # JSON array goes into typed_fields verbatim (the bridge resolves
        # member names → member_ids lazily when the frontend reads).
        items = data.get("votes")
        if not isinstance(items, list):
            logger.warning(
                "votes for meeting=%s missing `votes` array",
                meeting_id,
            )
            return
        from database import save_votes_batch
        result = save_votes_batch(meeting_id, city, items)
        logger.info(
            "votes persisted for meeting=%s: %s", meeting_id, result,
        )
        # No karaoke alignment for votes — procedural body action, not
        # quoted speech. Same skip as motions.
    elif output_type == "agenda_transitions":
        # Conversational Compiler Track B Chunk B-3 (2026-06-04).
        # AgendaTransition nodes anchor logical-block parents for the
        # downstream Motion / Vote / Commit_P nodes per SPEC Decision #2.
        items = data.get("agenda_transitions")
        if not isinstance(items, list):
            logger.warning(
                "agenda_transitions for meeting=%s missing array",
                meeting_id,
            )
            return
        from database import save_agenda_transitions_batch
        result = save_agenda_transitions_batch(meeting_id, city, items)
        logger.info(
            "agenda_transitions persisted for meeting=%s: %s",
            meeting_id, result,
        )
        # No karaoke alignment for transitions — structural events,
        # not quoted speech.
    elif output_type == "seconds":
        # Conversational Compiler Track B Chunk B-4 (2026-06-04). Second
        # nodes complete the Robert's Rules procedural triad. The
        # constraint-checker pass will eventually extend to Second →
        # Motion responds_to edges (V0 fires on Vote → Motion only).
        items = data.get("seconds")
        if not isinstance(items, list):
            logger.warning(
                "seconds for meeting=%s missing `seconds` array",
                meeting_id,
            )
            return
        from database import save_seconds_batch
        result = save_seconds_batch(meeting_id, city, items)
        logger.info(
            "seconds persisted for meeting=%s: %s", meeting_id, result,
        )
        # No karaoke alignment for seconds — single-word procedural
        # affirmation, not quoted substance.



# ── Strategy: qdrant_synthesize (complete-evidence flagship path) ─────
#
# Every whole-meeting text artifact receives all hash-verified indexed chunks
# in chronological order. Query-shaped Librarian/search paths keep retrieval.

def _load_synopsis_anchor_transcript_words(meeting_id: int) -> list[dict]:
    """Load canonical transcript words through the active DB backend."""
    row = is_output_already_present(meeting_id, "transcript_words")
    if row is None:
        raise ValueError(
            f"meeting {meeting_id} has no successful transcript_words cache row"
        )

    raw = row.get("content")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"meeting {meeting_id} transcript_words content is invalid JSON"
        ) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("words"), list):
        raise ValueError(
            f"meeting {meeting_id} transcript_words content has no words list"
        )
    if not parsed["words"]:
        raise ValueError(f"meeting {meeting_id} transcript_words list is empty")
    return parsed["words"]


def _uncheckable_synopsis_anchor_resolution(
    text: str,
) -> citation_validator.VerbatimAnchorResolution:
    """Build durable audit evidence when the resolver unexpectedly fails."""
    quote_by_start = {
        match.start(): match
        for match in _SYNOPSIS_VERBATIM_ANCHOR_RE.finditer(text)
    }
    markers = list(re.finditer(r"\[at\b", text, flags=re.IGNORECASE))
    failure_rows: list[dict] = []
    for ordinal, marker in enumerate(markers, start=1):
        match = quote_by_start.get(marker.start())
        if match is not None:
            start, end = match.span()
            raw_anchor = match.group(0)
            quote = match.group("quote")
        else:
            start = marker.start()
            closing = text.find("]", start)
            end = closing + 1 if closing >= 0 else len(text)
            raw_anchor = text[start:end]
            quote = ""
        failure_rows.append(
            {
                "ordinal": ordinal,
                "source_span": [start, end],
                "raw_anchor": raw_anchor,
                "quote": quote,
                "reason": "resolver_internal_error",
            }
        )
    return citation_validator.VerbatimAnchorResolution(
        text=text,
        state="uncheckable",
        anchors_total=len(markers),
        aligned=(),
        failures=tuple(failure_rows),
    )


def _append_synopsis_anchor_audit(
    content: str,
    resolution: citation_validator.VerbatimAnchorResolution,
) -> str:
    """Append the versioned, code-generated synopsis anchor audit block."""
    payload = citation_validator.serialize_verbatim_anchor_resolution(resolution)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    # Keep arbitrary model-emitted quote text from terminating or invalidating
    # the surrounding HTML comment. JSON decoding restores the hyphens.
    encoded = encoded.replace("--", "\\u002d\\u002d")
    return (
        f"{content}\n\n<!-- synopsis_anchor_audit v1\n"
        f"{encoded}\n"
        "audit -->"
    )


async def _fetch_qdrant(
    meeting_id, notebook_id, output_type, prompt_filename, instructions,
) -> dict:
    """Run complete-transcript flagship generation for one text output.

    ``notebook_id`` is preserved for cache-row provenance.

    The `instructions` arg arrives already decorated with the spelling-
    correction directives `_maybe_prepend_correction_directives`
    applied in `fetch_one_output`. We additionally apply the persona
    preamble here so outputs in `_PERSONA_PREAMBLE_OUTPUTS` (e.g.
    tracked_claims) get the [SYMBOLS] linker contract block before
    being embedded inside the flagship synthesis envelope.
    """
    import asyncio

    # Persona preamble: scoped to outputs in `_PERSONA_PREAMBLE_OUTPUTS`
    # (tracked_claims is the one V1-RAG-3 output that needs it).
    # Spelling-correction directives are NOT re-applied here — they
    # were prepended into `instructions` upstream in fetch_one_output
    # and would double up if re-prepended.
    decorated_canonical = _maybe_prepend_persona_preamble(
        output_type, meeting_id, instructions,
    )

    try:
        chunks = await asyncio.to_thread(
            qdrant_synthesizer.load_complete_meeting_chunks,
            meeting_id,
        )
    except Exception as e:
        msg = f"complete transcript evidence load failed: {e}"
        logger.exception(
            "complete transcript evidence load failed meeting=%s output=%s",
            meeting_id,
            output_type,
        )
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, prompt_filename=prompt_filename,
            error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    full_prompt = qdrant_synthesizer.build_synthesis_prompt(
        output_type=output_type,
        canonical_prompt=decorated_canonical,
        meeting_id=meeting_id,
        chunks=chunks,
    )

    try:
        generation = await asyncio.to_thread(
            qdrant_synthesizer.generate_with_fallback,
            full_prompt,
        )
        answer = generation.content
    except qdrant_synthesizer.GenerationPausedError:
        logger.exception(
            "flagship generation paused meeting=%s output=%s",
            meeting_id,
            output_type,
        )
        raise
    except Exception as e:
        msg = f"flagship generation failed: {e}"
        logger.exception(
            "flagship generation failed meeting=%s output=%s",
            meeting_id,
            output_type,
        )
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, prompt_filename=prompt_filename,
            error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    synopsis_anchor_resolution = None
    if output_type == "synopsis":
        raw_answer = answer
        try:
            transcript_words = _load_synopsis_anchor_transcript_words(meeting_id)
        except Exception:
            # Let the resolver classify syntax-only cases (zero anchors or a
            # direct timestamp bypass) even when canonical timing evidence is
            # unavailable. Quote anchors become ``uncheckable`` against the
            # empty transcript rather than raising or blocking persistence.
            logger.warning(
                "synopsis anchor transcript unavailable meeting=%s; "
                "continuing with uncheckable timing evidence",
                meeting_id,
                exc_info=True,
            )
            transcript_words = ()
        try:
            synopsis_anchor_resolution = (
                citation_validator.resolve_inline_verbatim_anchors(
                    raw_answer,
                    chunks,
                    transcript_words,
                )
            )
        except Exception:
            logger.warning(
                "synopsis anchor resolution failed unexpectedly meeting=%s; "
                "persisting raw synopsis as uncheckable",
                meeting_id,
                exc_info=True,
            )
            synopsis_anchor_resolution = _uncheckable_synopsis_anchor_resolution(
                raw_answer
            )
        answer = (
            synopsis_anchor_resolution.text
            if synopsis_anchor_resolution.state == "resolved"
            else raw_answer
        )

    try:
        qdrant_synthesizer.record_synthesis_provenance(
            meeting_id=meeting_id,
            output_type=output_type,
            prompt=full_prompt,
            model_id=generation.model_id,
            retrieved_chunk_ids=[chunk.chunk_index for chunk in chunks],
            evidence_mode="complete_transcript",
            attempts=generation.attempts,
        )
    except Exception as e:
        logger.warning(
            "Could not record synthesis provenance for meeting=%s "
            "output_type=%s: %s",
            meeting_id,
            output_type,
            e,
            exc_info=True,
        )

    # Post-extraction city corrections — identical discipline to the
    # legacy text path. Free-form prose outputs in
    # `_CITY_CORRECTION_PROSE_OUTPUTS` get whole-string find-and-replace
    # against the city's auto-apply correction rows.
    try:
        answer = _maybe_apply_city_corrections(output_type, meeting_id, answer)
    except Exception as e:
        logger.warning(
            "%s city-correction step raised (%s) — persisting uncorrected",
            output_type, e,
        )

    if synopsis_anchor_resolution is not None:
        answer = _append_synopsis_anchor_audit(answer, synopsis_anchor_resolution)

    prompt_version = f"v1-rag-3-{generation.model_id}"
    save_notebook_output(
        meeting_id=meeting_id, notebook_id=notebook_id,
        output_type=output_type, content=answer,
        prompt_filename=prompt_filename, prompt_version=prompt_version,
    )

    # Sidecar persist — tracked_claims writes structured rows into the
    # tracked_claims table (the only output in `_PERSONA_PREAMBLE_OUTPUTS`
    # currently routed through qdrant). The legacy text path calls the
    # same helper; preserve identical sidecar discipline.
    try:
        _maybe_persist_member_output(output_type, meeting_id, answer)
    except Exception as e:
        logger.warning(
            "sidecar persist for %s meeting=%s raised (%s) — raw row still saved",
            output_type, meeting_id, e,
        )

    return {
        "output_type": output_type,
        "status": "ok",
        "content_chars": len(answer or ""),
        "chunks_evidenced": len(chunks),
        "backend": "complete_transcript_flagship",
        "model_id": generation.model_id,
    }


async def _fetch_qdrant_multi(
    meeting_id, notebook_id, output_type, prompt_filename, questions,
) -> dict:
    """V1-RAG-3 second slice — per-question Q&A pairs via Qdrant.

    5 questions in, JSON array of {question, answer} out — each
    question is retrieved + synthesized independently against the
    meeting's Qdrant index. F5 fix from the 2026-06-20 brainstorm-audit:
    legacy cached chips were source-less meta-responses ("That's a
    great question! … I'll need you to add some sources first…"), so
    the Q&A pairs must be generated via this path instead.

    Each question is its OWN retrieval query — the question text
    itself drives the embedding search, no central OUTPUT_QUERIES
    lookup. The synthesis prompt asks Sonnet to answer that specific
    question using ONLY the retrieved chunks.
    """
    import asyncio
    import json

    if not questions:
        msg = "no questions provided in prompt front-matter"
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, prompt_filename=prompt_filename,
            error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    pairs: list[dict] = []
    for q in questions:
        question_text = str(q).strip()
        if not question_text:
            continue

        # The question itself is the retrieval query for its own answer.
        try:
            chunks = await asyncio.to_thread(
                qdrant_synthesizer.retrieve_chunks,
                meeting_id,
                question_text,
            )
        except Exception as e:
            logger.exception(
                "qdrant_multi: retrieve failed for q=%r", question_text[:80],
            )
            pairs.append({
                "question": question_text,
                "answer": None,
                "error": f"retrieve failed: {e}",
            })
            continue

        if not chunks:
            pairs.append({
                "question": question_text,
                "answer": None,
                "error": "no chunks retrieved (meeting may not be indexed)",
            })
            continue

        # Compose a Q-and-A-shaped synthesis prompt. Different shape
        # than _fetch_qdrant's canonical-prompt-embedded form because
        # the suggested_questions.md prompt body is documentation about
        # the cached-chip pattern, not an instruction TO the model.
        chunks_block = "\n\n".join(
            qdrant_synthesizer._format_chunk_for_prompt(c) for c in chunks
        )
        qa_prompt = (
            f"You are answering a citizen's question about a U.S. municipal "
            f"city council meeting. The answer will be cached and replayed "
            f"as a clickable chip on the meeting's broadcast page — no live "
            f"API call happens when a citizen clicks the chip.\n\n"
            f"QUESTION: {question_text}\n\n"
            f"RETRIEVED CONTEXT — top-{len(chunks)} chunks from the meeting "
            f"transcript (meeting_id={meeting_id}). Each chunk is tagged "
            f"with its karaoke-timecode metadata. Do NOT use information "
            f"that isn't in these chunks.\n\n"
            f"---\n"
            f"{chunks_block}\n"
            f"---\n\n"
            f"TASK — write a 2-4 sentence answer to the question above using "
            f"ONLY the retrieved context. Be concrete: cite specific facts, "
            f"dollar amounts, vote counts, named members, project names when "
            f"present in the chunks. Use neutral civic-news register — "
            f"never \"controversial,\" \"wisely,\" \"narrowly.\" State facts; "
            f"don't characterize. If the chunks don't contain enough "
            f"information for a confident answer, say so plainly in one "
            f"sentence rather than fabricating content.\n\n"
            f"Output ONLY the answer text — no preamble, no \"Answer:\" "
            f"label, no closing line."
        )

        try:
            answer = await asyncio.to_thread(
                qdrant_synthesizer.synthesize_via_claude_p,
                qa_prompt,
            )
            pairs.append({"question": question_text, "answer": answer})
            logger.info(
                "qdrant_multi[%s] q=%s → %d chars",
                output_type, question_text[:50], len(answer or ""),
            )
        except Exception as e:
            logger.exception(
                "qdrant_multi: synthesize failed for q=%r", question_text[:80],
            )
            pairs.append({
                "question": question_text,
                "answer": None,
                "error": f"synthesize failed: {e}",
            })

    if not pairs:
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, prompt_filename=prompt_filename,
            error="no question/answer pairs produced",
        )
        return {"output_type": output_type, "status": "error",
                "error": "no Q&A pairs"}

    # T-017 Layer 2 — apply city corrections to each pair's question +
    # answer (mirrors the legacy text_multi path).
    try:
        city = _meeting_city(meeting_id)
        if city:
            from database import apply_city_corrections
            for p in pairs:
                for field in ("question", "answer"):
                    v = p.get(field)
                    if isinstance(v, str) and v:
                        new_v, _ = apply_city_corrections(city, v)
                        if new_v != v:
                            p[field] = new_v
    except Exception as e:
        logger.warning(
            "%s city-correction step raised (%s) — persisting uncorrected",
            output_type, e,
        )

    prompt_version = f"v1-rag-3-{qdrant_synthesizer.SONNET_MODEL_ID}"
    save_notebook_output(
        meeting_id=meeting_id, notebook_id=notebook_id,
        output_type=output_type,
        content=json.dumps(pairs),
        prompt_filename=prompt_filename, prompt_version=prompt_version,
    )
    err_count = sum(1 for p in pairs if p.get("error"))
    return {
        "output_type": output_type,
        "status": "ok" if err_count == 0 else "partial",
        "pairs_total": len(pairs),
        "pairs_errored": err_count,
        "backend": "qdrant_sonnet",
    }


# ── Strategy: qdrant_extract_quotes (V1-RAG-3 linker pass, post-C5) ──
#
# The linker pass (post-C5 finding): the retired quote extraction had
# been acting as the linker — it consumed the [SYMBOLS] block
# (canonical-name-to-aliases table) and used LLM-reasoning to attribute
# imperfectly-transcribed surnames to canonical council_members rows.
# Dropping that pass left V1-RAG-3 meetings with populated rosters but
# zero attributed quotes.
#
# This strategy does that work in chronological batches over the complete
# indexed transcript. The qdrant_quote_extractor module (D3 + D4) owns the
# load → batch → extract → persist →
# align pipeline; this function is the thin fetcher-side wrapper that
# resolves city_name from meeting_id, fires the extractor, and writes
# a notebook_outputs cache row with the run stats for traceability.

async def _fetch_qdrant_extract_quotes(
    meeting_id, notebook_id, output_type, prompt_filename,
) -> dict:
    """Run the V1-RAG-3 attributed-quote linker pass for one meeting.

    V1-RAG-3 talks to the Surface Pro RAG node + claude -p subprocess.
    `notebook_id` is preserved for the cache-row provenance.
    """
    import asyncio

    city = _meeting_city(meeting_id)
    if not city:
        msg = f"cannot extract quotes — meeting {meeting_id} has no city_name"
        logger.error(msg)
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, prompt_filename=prompt_filename,
            error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    try:
        stats = await asyncio.to_thread(
            qdrant_quote_extractor.extract_and_persist,
            meeting_id=meeting_id,
            city_name=city,
        )
    except qdrant_synthesizer.GenerationPausedError:
        logger.exception(
            "quote extraction generation paused meeting=%s city=%s",
            meeting_id,
            city,
        )
        raise
    except Exception as e:
        msg = f"qdrant_extract_quotes failed: {e}"
        logger.exception(
            "qdrant_extract_quotes failed meeting=%s city=%s", meeting_id, city
        )
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, prompt_filename=prompt_filename,
            error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    # Cache row stores the per-run stats as JSON content so the operator
    # terminal + work-order detail view surface the attributed/skipped
    # count without a fresh DB query. The actual quote rows live in the
    # canonical `quotes` table; this row is provenance, not data.
    model_ids = list(dict.fromkeys(stats.get("model_ids") or []))
    model_label = "+".join(model_ids) or qdrant_synthesizer.FLAGSHIP_MODEL_ID
    prompt_version = f"v1-rag-3-{model_label}"
    save_notebook_output(
        meeting_id=meeting_id, notebook_id=notebook_id,
        output_type=output_type, content=json.dumps(stats),
        prompt_filename=prompt_filename, prompt_version=prompt_version,
    )

    return {
        "output_type": output_type,
        "status": "ok",
        "extracted_count": stats.get("extracted_count", 0),
        "saved": stats.get("saved", 0),
        "updated": stats.get("updated", 0),
        "member_lookup_misses": stats.get("member_lookup_misses", 0),
        "backend": "complete_transcript_flagship_extract",
        "model_ids": model_ids,
    }


# ── Strategy: transcript_words (T-009 Phase 0a) ───────────────────


async def _fetch_transcript_words(
    meeting_id: int,
    notebook_id: str,
    output_type: str,
) -> dict:
    """Generate word-level transcripts via OpenAI Whisper.

    Resolves the meeting's source video URL (work_orders.youtube_video_url
    preferred, meetings.video_url fallback), downloads audio with yt-dlp,
    transcribes via Whisper, persists `{words, duration_seconds, language,
    source_url}` JSON into notebook_outputs.content.

    Gated by ZSPAN_WHISPER_ENABLED (default on). When disabled, returns
    `status=skipped_disabled` and writes no row — so re-enabling later
    will cleanly generate.

    Designed to be safely callable inside `fetch_all_outputs` alongside
    the synthesis outputs: it holds no shared rate-limit budget and runs
    the blocking download + upload in a thread via asyncio.to_thread.
    """
    import asyncio
    import json

    transcript_block = _transcript_overwrite_block(meeting_id, output_type)
    if transcript_block is not None:
        return transcript_block

    if not WHISPER_ENABLED:
        msg = "ZSPAN_WHISPER_ENABLED=0 — transcript_words skipped"
        logger.info("transcript_words meeting=%s: %s", meeting_id, msg)
        return {
            "output_type": output_type,
            "status": "skipped_disabled",
            "note": msg,
        }

    # Look up the canonical YouTube URL for this meeting. Prefer the
    # matcher-set work_orders.youtube_video_url (T-004); fall back to
    # the unreliable meetings.video_url field. Lifted into
    # database.get_resolved_video_url so the D-099 Phase 2 C4 HTTP shim
    # can mirror it without forking the join logic.
    try:
        youtube_url = get_resolved_video_url(meeting_id) or None
    except Exception as e:
        msg = f"DB error resolving video URL: {e}"
        logger.exception("transcript_words meeting=%s: %s", meeting_id, msg)
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}
    if not youtube_url:
        msg = "no YouTube URL available for this meeting (set via T-004 matcher or manual [SET URL])"
        logger.warning("transcript_words meeting=%s: %s", meeting_id, msg)
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    # Lazy import: keeps yt-dlp / whisper_client out of the cold-start
    # path for fetchers that don't request transcript_words.
    try:
        from whisper_client import (  # noqa: E402
            transcribe_youtube,
            build_whisper_prompt_for_city,
            WhisperError,
            is_configured,
        )
    except Exception as e:
        msg = f"whisper_client import failed: {e}"
        logger.exception("transcript_words meeting=%s: %s", meeting_id, msg)
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    # T-017 Layer 1: build a vocabulary-hints prompt from the city's
    # city_intelligence (canonical council names + civic vocab + optional
    # per-city `whisper_vocabulary_hints` field). Reduces proper-noun ASR
    # errors at the source. Empty string is fine — transcribe_youtube
    # tolerates it.
    city_for_prompt = _meeting_city(meeting_id)
    whisper_prompt = build_whisper_prompt_for_city(city_for_prompt) if city_for_prompt else ""
    if whisper_prompt:
        logger.info(
            "transcript_words meeting=%s: priming Whisper with %d-char prompt for %s",
            meeting_id, len(whisper_prompt), city_for_prompt,
        )

    if not is_configured():
        msg = (
            "active transcription provider is not configured; check its "
            "credentials and settings"
        )
        logger.warning("transcript_words meeting=%s: %s", meeting_id, msg)
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    work_dir = _meeting_media_dir(meeting_id) / "whisper"

    try:
        # asyncio.to_thread doesn't accept kwargs by default; use a
        # functools.partial so we can pass `prompt=` cleanly.
        from functools import partial
        result = await asyncio.to_thread(
            partial(
                transcribe_youtube,
                youtube_url,
                work_dir,
                False,
                prompt=whisper_prompt or None,
            )
        )
    except WhisperError as e:
        msg = f"{type(e).__name__}: {e}"
        logger.exception("transcript_words meeting=%s failed", meeting_id)
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}
    except Exception as e:
        msg = f"unexpected: {e}"
        logger.exception("transcript_words meeting=%s crashed", meeting_id)
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    provider = result.get("provider")
    model = result.get("model")
    if not (
        isinstance(provider, str) and provider.strip()
        and isinstance(model, str) and model.strip()
    ):
        msg = "transcription result omitted provider/model provenance"
        logger.error("transcript_words meeting=%s: %s", meeting_id, msg)
        save_notebook_output(
            meeting_id=meeting_id, notebook_id=notebook_id,
            output_type=output_type, error=msg,
        )
        return {"output_type": output_type, "status": "error", "error": msg}

    from zspan_pipeline.transcript_quarantine import (
        apply_degenerate_span_quarantine,
        log_quarantine_result,
    )

    quarantine = apply_degenerate_span_quarantine(result)
    log_quarantine_result(meeting_id, quarantine)
    payload = json.dumps(result, ensure_ascii=False)
    save_notebook_output(
        meeting_id=meeting_id, notebook_id=notebook_id,
        output_type=output_type, content=payload,
        prompt_filename=None,
        prompt_version=f"{provider.strip()}/{model.strip()}",
    )
    word_count = len(result.get("words") or [])
    logger.info(
        "transcript_words meeting=%s → ok (%d words, %.1fs audio, "
        "%d quarantined words; detector_ran=%s)",
        meeting_id, word_count, result.get("duration_seconds") or 0.0,
        quarantine.quarantined_word_count, quarantine.detector_ran,
    )

    # T-009 Phase 0b auto-align: now that transcript_words is on disk
    # for this meeting, run alignment on any unaligned member_quotes,
    # tracked_claims, and unified quotes for this meeting. Idempotent
    # — skips rows where word_timings is already set. Failures don't
    # propagate: alignment is best-effort polish on top of the canonical
    # extraction.
    try:
        from quote_align import (  # noqa: E402
            align_meeting_quotes,
            align_tracked_claims_for_meeting,
            align_quotes_for_meeting,
        )
        member_stats = align_meeting_quotes(meeting_id)
        if member_stats.get("aligned") or member_stats.get("failed"):
            logger.info(
                "transcript_words meeting=%s → align_meeting_quotes: %s",
                meeting_id, member_stats,
            )
        claims_stats = align_tracked_claims_for_meeting(meeting_id)
        if claims_stats.get("aligned") or claims_stats.get("failed"):
            logger.info(
                "transcript_words meeting=%s → align_tracked_claims: %s",
                meeting_id, claims_stats,
            )
        # Quotes Unification Refactor — the new canonical `quotes` table.
        # Sibling trigger fires here so transcript_words landing AFTER
        # the unified quotes extraction unblocks alignment for the
        # new table. The same align_quotes_for_meeting is also called
        # from _maybe_persist_member_output's `quotes` branch for the
        # opposite ordering (quotes landed first, transcript second).
        quotes_stats = align_quotes_for_meeting(meeting_id)
        if quotes_stats.get("aligned") or quotes_stats.get("failed"):
            logger.info(
                "transcript_words meeting=%s → align_quotes: %s",
                meeting_id, quotes_stats,
            )
        # T-013 V4 — if member_quotes alignment produced timings, fire
        # auto-build. Sibling trigger in _maybe_persist_member_output
        # handles the case when member_quotes_topic lands AFTER the
        # transcript; whichever side lands second fires (the other call
        # is a no-op because the manifest now exists).
        if member_stats.get("aligned"):
            _maybe_auto_build_review_queue(meeting_id)
    except Exception as e:
        logger.warning(
            "transcript_words meeting=%s: post-success quote alignment "
            "raised (%s); not blocking — quote_align can be re-run "
            "manually via align_meeting_quotes() / "
            "align_tracked_claims_for_meeting()",
            meeting_id, e,
        )

    return {
        "output_type": output_type,
        "status": "ok",
        "word_count": word_count,
        "duration_seconds": result.get("duration_seconds"),
    }


# ── Fetch all (used by worker) ────────────────────────────────────


async def fetch_all_outputs(
    meeting_id: int,
    notebook_id: str,
    output_types: Iterable[str],
) -> list[dict]:
    """Fetch every requested output type for one meeting.

    Every registry strategy (qdrant_synthesize / qdrant_synthesize_multi
    / qdrant_extract_quotes / transcript_words) dispatches to real work;
    an output_type absent from the registry returns an "Unknown
    output_type" error row from fetch_one_output.
    """
    output_list = list(output_types)

    # ─── S-009 chunk 3 auto-run-first gate ───
    # When the WO opted into truth_packet (NOT default — see
    # OUTPUT_TYPE_REGISTRY truth_packet entry), run it FIRST, gate verdict,
    # raise on halt/ambiguous so the worker transitions state cleanly. The
    # result row IS persisted regardless of verdict (for operator audit).
    results: list[dict] = []
    if "truth_packet" in output_list:
        output_list.remove("truth_packet")
        logger.info(
            "S-009 ch3 gate: running truth_packet FIRST for meeting=%s (gates rest of WO)",
            meeting_id,
        )
        tp_result = await fetch_one_output(
            meeting_id, notebook_id, "truth_packet",
        )
        results.append(tp_result)

        if tp_result.get("status") == "error":
            # The truth_packet itself failed to fetch (upstream error or
            # dormant/unregistered type), NOT a gate verdict. Treat as ambiguous
            # so the operator can decide whether to retry. (The error itself
            # is captured in tp_result; the gate is the structural verdict.)
            err_msg = tp_result.get("error", "(no detail)")
            logger.warning(
                "truth_packet fetch errored for meeting=%s: %s → ambiguous",
                meeting_id, err_msg,
            )
            raise TruthPacketAmbiguousError(
                TruthPacketResult(
                    verdict="ambiguous",
                    reason=f"truth_packet fetch failed upstream: {err_msg}",
                    observations={},
                )
            )

        # Raw response is in tp_result["content"] — gate it.
        raw = tp_result.get("content", "") or ""
        # Pass expected_jurisdiction from the meeting if available — the gate
        # uses it for cross-check; safe to skip if not resolvable.
        expected_jurisdiction = None
        try:
            expected_jurisdiction = get_meeting_city(meeting_id)
        except Exception:  # noqa: BLE001 — non-fatal, just skip the check
            pass
        verdict = gate_truth_packet(raw, expected_jurisdiction=expected_jurisdiction)
        if verdict.verdict == "halt":
            logger.warning(
                "truth_packet HALT meeting=%s reason=%s",
                meeting_id, verdict.reason,
            )
            raise TruthPacketHaltError(verdict)
        if verdict.verdict == "ambiguous":
            logger.warning(
                "truth_packet AMBIGUOUS meeting=%s reason=%s",
                meeting_id, verdict.reason,
            )
            raise TruthPacketAmbiguousError(verdict)
        logger.info(
            "truth_packet PASS meeting=%s reason=%s",
            meeting_id, verdict.reason,
        )

    rest = await _run_serial(meeting_id, notebook_id, output_list)
    return results + rest


async def _run_serial(
    meeting_id: int,
    notebook_id: str,
    output_types: list[str],
) -> list[dict]:
    """Strict one-at-a-time. Every fetcher must complete before the next starts."""
    results = []
    for ot in output_types:
        result = await fetch_one_output(meeting_id, notebook_id, ot)
        logger.info("output[%s] meeting=%s → %s (serial)",
                    ot, meeting_id, result.get("status"))
        results.append(result)
    return results

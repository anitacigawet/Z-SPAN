#!/usr/bin/env python3.11
"""Z-SPAN single-shot worker for operator-triggered work orders.

Invoke with ``--once`` to process the next pending work order or with
``--work-order-id N`` to process a specific work order.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Make `parsers/` and `parsers/scripts/` importable. parsers/ exposes the
# core helpers (database, whisper_client, etc.); parsers/scripts/ exposes
# the S-037 V0 non-YouTube source resolver (`transcribe_non_youtube`).
_PARSERS_DIR = Path(__file__).resolve().parent.parent / "council_navigator" / "parsers"
_PARSERS_SCRIPTS_DIR = _PARSERS_DIR / "scripts"
for _p in (_PARSERS_DIR, _PARSERS_SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Make the bridge package importable when invoked as a script
_BRIDGE_PARENT = Path(__file__).resolve().parent.parent
if str(_BRIDGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_PARENT))

# D-099 Phase 2 C5: swap to HTTP backend when ZSPAN_DB_BACKEND=http.
# MUST run before any `from database import ...`.
from zspan_pipeline.db_backend import install_db_backend  # noqa: E402
install_db_backend()

from database import (  # noqa: E402
    get_work_order,
    next_pending_work_order,
    recover_stale_work_orders,
    update_meeting_diarization_status,
    update_work_order_state,
)
from zspan_pipeline.fetcher import (  # noqa: E402
    fetch_all_outputs,
    TruthPacketHaltError,
    TruthPacketAmbiguousError,
)
from zspan_pipeline.output_contracts import (  # noqa: E402
    FLAGSHIP_PRODUCTION_CONTRACT,
)
from zspan_pipeline.qdrant_synthesizer import (  # noqa: E402
    GenerationPausedError,
    work_order_generation_scope,
)
from transcribe_non_youtube import (  # noqa: E402 (S-037 V0 worker integration)
    is_transcription_ready_url,
    resolve_source,
    wo_to_meeting_row,
)

# How long a work order can stay 'processing' before we consider it stuck
# (likely a worker crash or claude -p canary hang) and reset to
# 'pending' on next worker startup. Default: 2 hours.
STALE_PROCESSING_HOURS = float(os.environ.get("ZSPAN_STALE_PROCESSING_HOURS", "2"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("zspan.worker")

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off", ""})


def _worker_diarization_enabled() -> bool:
    """Resolve the synchronous diarization switch; default is off.

    ``ZSPAN_WORKER_DIARIZATION_ENABLED`` has precedence over the persistent
    ``zspan_worker_diarization_enabled`` user setting.
    """
    from env_config import load_user_settings

    if "ZSPAN_WORKER_DIARIZATION_ENABLED" in os.environ:
        raw: Any = os.environ.get("ZSPAN_WORKER_DIARIZATION_ENABLED")
        source = "ZSPAN_WORKER_DIARIZATION_ENABLED"
    else:
        raw = load_user_settings().get("zspan_worker_diarization_enabled", False)
        source = "zspan_worker_diarization_enabled"

    if isinstance(raw, bool):
        return raw
    normalized = str(raw).strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized not in _FALSEY:
        logger.warning("Invalid %s=%r; synchronous diarization stays disabled", source, raw)
    return False


def _record_diarization_status(
    meeting_id: int,
    status: str,
    detail: Optional[str],
) -> None:
    """Require a durable work-order row for every worker diarization decision."""
    if not update_meeting_diarization_status(meeting_id, status, detail):
        raise RuntimeError(
            f"meeting {meeting_id} has no work order for diarization status"
        )


async def _run_worker_diarization(
    wo_id: int,
    meeting_id: int,
    city: str,
) -> str:
    """Run or deliberately defer diarization without affecting sidecars."""
    if not _worker_diarization_enabled():
        detail = "synchronous worker diarization disabled; queued for backfill"
        _record_diarization_status(meeting_id, "deferred", detail)
        logger.info(
            "WO %d (meeting=%s): diarization deferred; sidecars continue now",
            wo_id, meeting_id,
        )
        return "deferred"

    from . import diarize_orchestrator

    _record_diarization_status(
        meeting_id, "running", "synchronous worker diarization enabled",
    )
    try:
        summary = await asyncio.to_thread(
            diarize_orchestrator.run_full_diarize_step,
            int(meeting_id), str(city or ""),
        )
        status, detail = diarize_orchestrator.classify_diarization_summary(summary)
    except Exception as diarize_exc:
        status, detail = "failed", str(diarize_exc)
        logger.warning(
            "WO %d (meeting=%s): diarize_orchestrator failed (non-fatal; "
            "sidecar pipeline still runs): %s",
            wo_id, meeting_id, diarize_exc,
        )
    _record_diarization_status(meeting_id, status, detail)
    logger.info(
        "WO %d (meeting=%s): diarization status=%s detail=%s",
        wo_id, meeting_id, status, detail,
    )
    return status


# ── V1-RAG-3 supported output types (per D-126 + D-143) ──────────────
# The worker dispatches only the registry's flagship contract plus
# transcript_words (special-cased in the filter). Anything else in a
# WO's requested_outputs is filtered out before fetcher dispatch —
# dormant types (motions / votes / seconds / agenda_transitions /
# truth_packet) re-enter when their extraction migrates to a live
# strategy per fetcher.OUTPUT_TYPE_REGISTRY's dormant-set note.
#
# COUPLING: output_contracts.py is the single home for this set; keep its
# members aligned with the live qdrant_* flagship strategies registered in
# fetcher.OUTPUT_TYPE_REGISTRY.
V1_RAG3_OUTPUT_TYPES = FLAGSHIP_PRODUCTION_CONTRACT


def _shared_retrieval_core():
    """Import the CLI-owned shared core from either repo or installed shape."""
    try:
        from zspan_cli import local_retrieval
    except ImportError:
        from zspan_cli.zspan_cli import local_retrieval
    return local_retrieval


def _collapse_speaker_turns(
    words: list[dict[str, Any]],
) -> Optional[list[dict[str, Any]]]:
    """Match the retired flagship indexer's contiguous speaker-run payload."""
    if not any(word.get("speaker_id") for word in words):
        return None
    turns: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for word in words:
        text = str(word.get("word") or "").strip()
        if not text:
            continue
        speaker = str(word.get("speaker_id") or "UNKNOWN")
        start = round(float(word.get("start", 0.0)), 3)
        end = round(float(word.get("end", 0.0)), 3)
        if current is None or current["speaker_label"] != speaker:
            if current is not None:
                turns.append(current)
            current = {
                "speaker_label": speaker,
                "start": start,
                "end": end,
                "text": text,
            }
        else:
            current["end"] = end
            current["text"] = f"{current['text']} {text}"
    if current is not None:
        turns.append(current)
    return turns


def _chunk_retrievable_segments(
    core: Any,
    words: list[dict[str, Any]],
    *,
    token_counter: Optional[Callable[[str], int]],
    exact_tokenizer: Optional[bool],
) -> list[Any]:
    """Chunk non-quarantined runs without joining across a removed span."""
    from zspan_pipeline.transcript_quarantine import is_quarantined_word

    chunks: list[Any] = []
    cursor = 0
    while cursor < len(words):
        while cursor < len(words) and is_quarantined_word(words[cursor]):
            cursor += 1
        segment_start = cursor
        while cursor < len(words) and not is_quarantined_word(words[cursor]):
            cursor += 1
        if segment_start == cursor:
            continue

        segment_chunks = core.chunk_transcript(
            words[segment_start:cursor],
            token_counter=token_counter,
            exact=exact_tokenizer,
        )
        for chunk in segment_chunks:
            chunk.chunk_index = len(chunks)
            chunk.word_start_index += segment_start
            chunk.word_end_index += segment_start
            chunks.append(chunk)
    return chunks


def index_meeting_locally(
    meeting_id: int,
    *,
    db_path: Optional[Path | str] = None,
    token_counter: Optional[Callable[[str], int]] = None,
    exact_tokenizer: Optional[bool] = None,
    embedding_fn: Optional[Callable[..., Any]] = None,
) -> int:
    """Chunk, embed, and atomically index cached transcript words in SQLite.

    Optional seams are for offline deterministic tests; production callers use
    only ``meeting_id`` and therefore the shared fastembed/tokenizer path.
    """
    from zspan_pipeline import local_vector_store
    from zspan_pipeline.transcript_quarantine import (
        apply_degenerate_span_quarantine,
        log_quarantine_result,
    )

    core = _shared_retrieval_core()
    transcript = local_vector_store.load_transcript_words(
        meeting_id, db_path=db_path,
    )
    quarantine = apply_degenerate_span_quarantine(transcript)
    log_quarantine_result(meeting_id, quarantine)
    if quarantine.changed:
        local_vector_store.save_transcript_words(
            meeting_id, transcript, db_path=db_path,
        )
    words = transcript["words"]
    chunks = _chunk_retrievable_segments(
        core,
        words,
        token_counter=token_counter,
        exact_tokenizer=exact_tokenizer,
    )
    if not chunks:
        raise ValueError(
            f"meeting {meeting_id} transcript produced no retrievable chunks "
            f"after quarantining {quarantine.quarantined_word_count} words"
        )
    embed = embedding_fn or core.embed_texts
    vectors = embed([chunk.text for chunk in chunks], progress=lambda _msg: None)
    turns = [
        _collapse_speaker_turns(
            words[chunk.word_start_index:chunk.word_end_index]
        )
        for chunk in chunks
    ]
    local_vector_store.replace_meeting_index(
        meeting_id,
        chunks,
        vectors,
        turns,
        transcript_sha256=local_vector_store.transcript_hash(transcript),
        embed_model=core.EMBED_MODEL_NAME,
        vector_dim=core.VECTOR_DIM,
        chunk_token_target=core.CHUNK_TOKEN_TARGET,
        chunk_token_overlap=core.CHUNK_TOKEN_OVERLAP,
        chunker_version=core.CHUNKER_VERSION,
        db_path=db_path,
    )
    logger.info(
        "V1-mode: locally indexed meeting=%d chunks=%d model=%s "
        "quarantined_words=%d detector_ran=%s",
        meeting_id, len(chunks), core.EMBED_MODEL_NAME,
        quarantine.quarantined_word_count, quarantine.detector_ran,
    )
    return len(chunks)


# ── Legacy direct-Claude auth fingerprint ────────────────────────────
# Query-shaped callers that deliberately remain outside the flagship fallback
# chain can still return this historical empty-stderr 401 shape.

# Fingerprint of the empty-stderr 401 path raised by qdrant_synthesizer.
# Both strings must appear in str(exc) for the match — they bracket the
# specific "claude -p returned non-zero but said nothing" shape.
_CLAUDE_P_AUTH_FAIL_RE = re.compile(
    r"claude -p (?:failed with returncode|returned empty stdout)"
)
_CLAUDE_P_EMPTY_STDERR_RE = re.compile(r"stderr:\s*['\"]\s*['\"]")


def _looks_like_claude_p_auth_failure(exc_or_msg: BaseException | str) -> bool:
    """Match the qdrant_synthesizer-raised RuntimeError fingerprint that
    signals expired CLI auth — `claude -p` returned rc!=0 (or empty
    stdout) and stderr is the empty string. The two patterns guard
    against false positives from real engineering errors that happen to
    use the same call path.

    Accepts either an exception (when the caller has one in hand) or a
    raw error string (when the caller is reading from a results-list
    dict that already swallowed the exception — the fetcher.py
    qdrant_synthesize strategy does this so per-output errors don't
    abort the whole WO)."""
    msg = str(exc_or_msg)
    if not _CLAUDE_P_AUTH_FAIL_RE.search(msg):
        return False
    return bool(_CLAUDE_P_EMPTY_STDERR_RE.search(msg))


class ClaudePAuthFingerprintError(RuntimeError):
    """Sentinel raised by `_process_one` when the per-output results list
    carries the `claude -p` empty-stderr 401 fingerprint. It is surfaced
    to the single-shot caller through `_run_once`'s exception handling."""


def _recover_stale_work_orders(hours: float = STALE_PROCESSING_HOURS) -> int:
    """Thin wrapper around database.recover_stale_work_orders that adds
    the per-WO log lines. Backend-agnostic — the actual SQL lives in
    database.py so the Mac-side HTTP shim (D-099 Phase 2 C4b) can mirror
    it without forking the recovery logic."""
    stale = recover_stale_work_orders(hours)
    for row in stale:
        logger.warning(
            "Recovery: reset WO %d (meeting %s) from stale 'processing' to 'pending'",
            row["id"], row["meeting_id"],
        )
    return len(stale)


# ── Per-work-order processing ─────────────────────────────────────────

def _finalize_work_order(
    wo_id: int,
    meeting_id: int,
    *,
    sidecar_failure_reason: Optional[str],
    output_count: int,
) -> str:
    """Run the publish-readiness gate and set the WO's terminal state,
    returning "completed" or "failed".

    Extracted from _process_one so the DIV-010 fail-closed path is unit
    testable: a readiness-service CRASH must not mint a `completed` WO (which
    would assert a completeness the check never confirmed). Outputs + sidecars
    are already persisted before this runs, so a crash fails the WO retryably
    rather than freezing a false completion. Publication stays the independent
    two-field owner gate, so this was never an auto-publish path.
    """
    try:
        from database import check_publish_readiness
        verdict = check_publish_readiness(int(meeting_id))
    except Exception as readiness_exc:
        # DIV-010 — fail CLOSED on readiness uncertainty (was
        # `verdict = {"ready": True}`, which let a readiness-service fault
        # complete a WO that never verified its own completeness).
        error_parts = [f"readiness_check_error: {readiness_exc}"]
        if sidecar_failure_reason:
            error_parts.append(sidecar_failure_reason)
        update_work_order_state(
            wo_id, "failed",
            error=" · ".join(error_parts),
            increment_retry=True,
        )
        logger.warning(
            "WO %d: readiness check crashed (%s) — failing closed (retryable); "
            "produced outputs preserved for the re-check",
            wo_id, readiness_exc,
        )
        return "failed"

    if not verdict.get("ready"):
        reasons = verdict.get("reasons") or ["incomplete outputs"]
        error_parts = [f"incomplete_outputs: {'; '.join(reasons)}"]
        if sidecar_failure_reason:
            error_parts.append(sidecar_failure_reason)
        update_work_order_state(
            wo_id, "failed",
            error=" · ".join(error_parts),
            increment_retry=True,
        )
        logger.warning(
            "WO %d: post-completion readiness FAILED — %s",
            wo_id, " · ".join(error_parts),
        )
        return "failed"

    update_work_order_state(wo_id, "completed", error=None)
    logger.info("WO %d: completed (%d outputs)", wo_id, output_count)
    return "completed"


async def _process_one(work_order: dict) -> str:
    """Process one work order inside one shared generation quota circuit."""
    with work_order_generation_scope(work_order["id"]):
        return await _process_one_scoped(work_order)


async def _process_one_scoped(work_order: dict) -> str:
    """Process a single work order. Returns the resulting state string.

    Updates the work_orders row + writes per-output results to
    notebook_outputs. Every generation from the serial output pass through
    the later sidecar pass shares the wrapper's work-order quota circuit.
    """
    wo_id = work_order["id"]
    meeting_id = work_order["meeting_id"]
    title = work_order.get("meeting_title") or f"meeting {meeting_id}"
    city = work_order.get("city_name", "")
    date = work_order.get("meeting_date", "")

    logger.info("WO %d: starting [%s · %s · %s]", wo_id, city, date, title)
    update_work_order_state(wo_id, "processing")

    # Source URL: use a pre-populated candidate only when it is directly
    # transcribable. Wrapper/vendor landing pages remain resolver evidence.
    candidate = work_order.get("youtube_video_url") or work_order.get("meeting_video_url")
    video_url = candidate if is_transcription_ready_url(candidate) else None
    if video_url is None:
        if candidate:
            logger.warning(
                "WO %d: rejecting wrapper/non-direct source candidate: %s",
                wo_id,
                candidate,
            )
        meeting_row = wo_to_meeting_row(work_order)
        resolved = resolve_source(meeting_row)
        logger.info(
            "WO %d: S-037 resolver returned source_kind=%s (%s)",
            wo_id, resolved.source_kind, resolved.notes,
        )
        if resolved.source_kind == "no_video_source":
            update_work_order_state(wo_id, "no_video_source", error=resolved.notes)
            return "no_video_source"
        if resolved.source_kind == "unsupported_city" or resolved.source_url is None:
            update_work_order_state(
                wo_id, "awaiting_video",
                error=f"no video URL and no S-037 strategy: {resolved.notes}",
            )
            return "awaiting_video"
        # Resolver found a URL — persist it to the WO so retries skip
        # re-resolution + the dashboard reflects the discovered URL.
        video_url = resolved.source_url
        update_work_order_state(
            wo_id, "processing",
            youtube_video_url=video_url,
        )
        logger.info(
            "WO %d: S-037 resolver set video_url=%s (kind=%s)",
            wo_id, video_url, resolved.source_kind,
        )

    # notebook_id is a legacy provenance column — historical cache rows
    # still bind by (meeting_id, notebook_id); empty for modern WOs.
    notebook_id = work_order.get("notebook_id") or ""

    # Pull outputs
    requested = (work_order.get("requested_outputs") or "").split(",")
    requested = [o.strip() for o in requested if o.strip()]
    if not requested:
        requested = [
            "episode_tagline",
            "synopsis", "newsletter",
            "key_decisions", "community_calls_to_action",
            "whats_next", "council_sentiment",
            # suggested_questions retired per D-157 (see V1_RAG3_OUTPUT_TYPES note)
            "transcript_words",
            "tracked_claims",
        ]

    # Filter to the V1-RAG-3 output types + transcript_words. Dormant
    # types (motions / votes / seconds / agenda_transitions /
    # truth_packet) re-enter when their extraction migrates to a live
    # strategy; retired types in stale WO lists drop here silently.
    before = list(requested)
    requested = [
        o for o in requested
        if o in V1_RAG3_OUTPUT_TYPES or o == "transcript_words"
    ]
    dropped = [o for o in before if o not in requested]
    if dropped:
        logger.info(
            "WO %d: filtered output_types, kept=%s dropped=%s",
            wo_id, requested, dropped,
        )

    # Pipeline order: transcript_words first (Whisper → cache row), then
    # in-process local indexing, THEN the existing qdrant-named synthesis
    # strategies against the freshly-indexed chunks. Run
    # transcript_words as its own fetch_one_output so we can branch
    # cleanly: if transcription failed (no video URL, Whisper unreachable,
    # yt-dlp couldn't ingest the source), there's nothing to index and
    # retrieval outputs would return honest-empty anyway — skip indexing.
    if "transcript_words" in requested:
        from zspan_pipeline.fetcher import fetch_one_output  # noqa: E402
        requested.remove("transcript_words")
        logger.info("WO %d: running transcript_words first", wo_id)
        tw_result = await fetch_one_output(
            meeting_id, notebook_id, "transcript_words",
        )
        tw_status = tw_result.get("status")
        logger.info(
            "WO %d: transcript_words → status=%s", wo_id, tw_status,
        )
        if tw_status in ("ok", "skipped_existing"):
            try:
                index_meeting_locally(meeting_id)
            except Exception:
                logger.exception(
                    "WO %d: local transcript indexing failed; retrieval "
                    "outputs below will probably return honest-empty",
                    wo_id,
                )
        else:
            logger.warning(
                "WO %d: skipping local index — no usable "
                "transcript_words (status=%s)",
                wo_id, tw_status,
            )

    try:
        results = await fetch_all_outputs(
            meeting_id=meeting_id,
            notebook_id=notebook_id,
            output_types=requested,
        )
    except TruthPacketHaltError as e:
        # S-009 ch3 — gate decisively halted the WO. Operator must
        # investigate (likely re-paste the video URL or abandon the WO).
        # The truth_packet row is already persisted by fetch_all_outputs
        # (it ran BEFORE raising). State writeback carries the gate's
        # reason for the operator review queue.
        logger.warning(
            "WO %d: truth_packet HALT — reason=%s",
            wo_id, e.result.reason,
        )
        update_work_order_state(
            wo_id, "failed_truth_packet",
            error=f"truth_packet halt: {e.result.reason}",
            increment_retry=False,  # operator action required, not a retry-eligible failure
        )
        return "failed_truth_packet"
    except TruthPacketAmbiguousError as e:
        # S-009 ch3 — gate couldn't make a structural decision. Park WO
        # in awaiting_truth_packet_review; operator inspects observations
        # and chooses to proceed / re-paste / abandon. NOT counted as
        # a retry — it's a structural review checkpoint.
        logger.warning(
            "WO %d: truth_packet AMBIGUOUS — reason=%s",
            wo_id, e.result.reason,
        )
        update_work_order_state(
            wo_id, "awaiting_truth_packet_review",
            error=f"truth_packet ambiguous: {e.result.reason}",
            increment_retry=False,
        )
        return "awaiting_truth_packet_review"
    except GenerationPausedError as e:
        logger.warning(
            "WO %d: generation paused failure_class=%s; completed outputs preserved",
            wo_id,
            e.failure_class,
        )
        update_work_order_state(
            wo_id,
            "failed",
            error=f"generation_paused:{e.failure_class}: {e}",
            increment_retry=False,
        )
        return "failed"
    except Exception as e:
        logger.exception("WO %d: fetch_all_outputs crashed", wo_id)
        update_work_order_state(
            wo_id, "failed",
            error=f"fetch_all_outputs crashed: {e}",
            increment_retry=True,
        )
        return "failed"

    # Did all outputs succeed (text + studio_pending count as success — error is failure)
    err_count = sum(1 for r in results if r.get("status") == "error")
    if err_count:
        err_msgs = [r.get("error") or "" for r in results if r.get("status") == "error"]
        update_work_order_state(
            wo_id, "failed",
            error=f"{err_count}/{len(results)} outputs errored",
            increment_retry=True,
        )
        # worker-headless-resilience-V0: the qdrant_synthesize strategy
        # catches the synthesizer's RuntimeError and packs it into the
        # results dict, so the per-output error strings are the only
        # surviving carrier of the 401 fingerprint. Raise a sentinel so
        # the single-shot caller receives the auth-specific failure without
        # us re-string-matching in two places.
        if any(_looks_like_claude_p_auth_failure(m) for m in err_msgs):
            raise ClaudePAuthFingerprintError(
                f"{err_count} output(s) failed with `claude -p` empty-stderr "
                f"401 fingerprint; first: {err_msgs[0][:300]}"
            )
        return "failed"

    # Diarization feeds only the owner roster-review workflow, so it is
    # default-deferred and cannot hold the public sidecars behind pyannote.
    # Operators can temporarily restore the synchronous path with the
    # explicit switch resolved by _worker_diarization_enabled().
    await _run_worker_diarization(wo_id, int(meeting_id), str(city or ""))

    # Generate the .preview/m<id>*.json sidecars that BroadcastPage's
    # new-shape rendering reads from (quote+decision selection-discipline
    # accordion + DISCUSSION-nested-under-decisions).
    #
    # Session-32 (2026-07-04) — sidecar_pipeline failure is no longer
    # silently non-fatal. If the pipeline crashes and the meeting ends
    # up without the quotes it needs to be publishable, the readiness
    # check below catches it and the WO transitions to `failed` with a
    # specific `incomplete_outputs` error. Previous behavior swept
    # sidecar crashes under the rug because "canonical outputs still
    # cached" — but that comment predated the sidecar-pipeline being
    # the sole quote-extraction path. m104615 (Kingman Golf Commission)
    # is the worked example: everything else succeeded, sidecar produced
    # quotes into `.preview/m104615.json` but nothing imported them to
    # the DB, WO marked "completed", broadcast page shipped without
    # highlights.
    sidecar_failure_reason: Optional[str] = None
    try:
        import asyncio as _asyncio
        from . import sidecar_pipeline
        await _asyncio.to_thread(
            sidecar_pipeline.run_pipeline, int(meeting_id), str(city or ""),
        )
    except GenerationPausedError as sidecar_exc:
        logger.warning(
            "WO %d: sidecar generation paused failure_class=%s; "
            "completed outputs and sidecars preserved",
            wo_id,
            sidecar_exc.failure_class,
        )
        update_work_order_state(
            wo_id,
            "failed",
            error=(
                f"generation_paused:{sidecar_exc.failure_class}: "
                f"{sidecar_exc}"
            ),
            increment_retry=False,
        )
        return "failed"
    except Exception as sidecar_exc:
        sidecar_failure_reason = f"sidecar_pipeline crashed: {sidecar_exc}"
        logger.warning(
            "WO %d (meeting=%s): sidecar_pipeline failed: %s "
            "(will run readiness check to decide if this is fatal)",
            wo_id, meeting_id, sidecar_exc,
        )

    # Session-32 (2026-07-04) — publish-readiness gate at WO completion.
    # Stale requested_outputs lists (pre-migration WOs) can complete without
    # producing everything a publishable episode needs; the readiness check
    # asks the meeting-level question and fails the WO (retryably) if not
    # ready. Extracted to _finalize_work_order so the DIV-010 fail-closed
    # path (a crashed readiness check must NOT mint a completed WO) is unit
    # testable.
    return _finalize_work_order(
        wo_id, meeting_id,
        sidecar_failure_reason=sidecar_failure_reason,
        output_count=len(results),
    )


# ── Single-shot modes ─────────────────────────────────────────────────
# These are used by the Flask "process one" endpoints so the user can step
# through work orders one click at a time.

async def _run_once(work_order_id: int | None = None) -> dict:
    """Process exactly one work order and exit.

    If work_order_id is provided, process that specific order (validating
    it's in a processable state). Otherwise, pull the next pending work
    order from the queue.

    Returns: { found: bool, work_order_id: int|None, final_state: str|None }
    """
    # Recover stale work orders BEFORE picking the next one — otherwise
    # next_pending_work_order skips those still marked 'processing'.
    _recover_stale_work_orders()

    if work_order_id is not None:
        wo = get_work_order(work_order_id)
        if wo is None:
            return {"found": False, "work_order_id": work_order_id,
                    "final_state": None, "reason": "not_found"}
        if wo["state"] not in ("pending", "awaiting_video", "failed"):
            return {"found": True, "work_order_id": work_order_id,
                    "final_state": wo["state"],
                    "reason": f"work order is in state '{wo['state']}', "
                              f"not processable. Use /retry to reset to pending."}
    else:
        wo = next_pending_work_order()
        if wo is None:
            return {"found": False, "work_order_id": None,
                    "final_state": None, "reason": "queue_empty"}

    try:
        final_state = await _process_one(wo)
    except Exception as e:
        logger.exception("WO %d: unexpected error in single-shot run", wo["id"])
        update_work_order_state(
            wo["id"], "failed",
            error=f"single-shot crashed: {e}",
            increment_retry=True,
        )
        final_state = "failed"

    return {
        "found": True,
        "work_order_id": wo["id"],
        "final_state": final_state,
    }


def run_once(work_order_id: int | None = None) -> dict:
    """Sync entry point for `python -m zspan_pipeline.worker --once`."""
    return asyncio.run(_run_once(work_order_id))


# ── CLI entry ─────────────────────────────────────────────────────────

def _parse_args(argv: list[str]):
    import argparse
    p = argparse.ArgumentParser(
        description="Z-SPAN pipeline worker"
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true",
        help="Process one work order (next pending) and exit. "
             "Used by the Flask 'process one' button."
    )
    mode.add_argument(
        "--work-order-id", type=int, default=None,
        help="Process this specific work order and exit."
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.once or args.work_order_id is not None:
        wo_id = args.work_order_id  # may be None for --once
        result = run_once(work_order_id=wo_id)
        logger.info("Single-shot result: %s", result)
        return 0

    logger.error(
        "The autonomous worker daemon was retired (operator-triggered only). "
        "Use --once or --work-order-id N."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Phase 2 D7 — orchestrate the diarize → merge → re-index → cluster-map sequence.

Called synchronously only when the worker's operator switch is enabled, or by
the manual/background backfill runner. The sequence:

  1. Check if the meeting's transcript_words already carries speaker_id
     per word. If yes, skip diarization (idempotent).
  2. Call mac_diarizer /diarize for the meeting's video URL.
  3. Merge pyannote turns with the existing whisper word list via the
     majority-of-duration rule (speaker_merger).
  4. Re-save the transcript_words notebook_output row with speaker_id
     injected per word.
  5. Re-index the meeting locally so speaker_turns lands in chunk storage.
  6. Run cluster_roster_mapper to populate meeting_speaker_roster.

Non-fatal: if any step fails, log + return; the sidecar_pipeline will
still run against undiarized data (D5 prompts fall back to proximity
inference per the D5 prompt update).

Composes with:
  - parsers.diarize_client  (HTTP wrapper for /diarize on :8767)
  - parsers.speaker_merger  (majority-of-duration + OVERLAP rule)
  - parsers.database        (load + save transcript_words via notebook_outputs)
  - zspan_pipeline.cluster_roster_mapper (the Sonnet pass + prongs)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
_PARSERS_DIR = (
    _THIS_DIR.parent / "council_navigator" / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))


def _load_transcript_words_row(meeting_id: int) -> Optional[Dict[str, Any]]:
    """Load the transcript_words notebook_outputs content for a meeting.

    Returns the parsed dict {words, duration_seconds, language, ...} or
    None if the row doesn't exist OR has an error.
    """
    import database  # type: ignore
    conn = database.get_connection()
    try:
        row = conn.execute(
            """
            SELECT content, notebook_id, error
            FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'transcript_words'
            """,
            (meeting_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or row["error"]:
        return None
    raw = row["content"]
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        logger.warning("transcript_words content parse failed: %s", exc)
        return None
    if not isinstance(parsed, dict):
        return None
    parsed["_notebook_id"] = row["notebook_id"]
    return parsed


def _save_transcript_words_content(
    meeting_id: int, notebook_id: str, content: Dict[str, Any],
) -> None:
    """Persist updated transcript_words content (with speaker_id per word)
    back to notebook_outputs. The notebook_id stays the same; we only
    update the content blob + bump generated_at."""
    import database  # type: ignore
    serialized = json.dumps(content, ensure_ascii=False)
    database.save_notebook_output(
        meeting_id=meeting_id,
        notebook_id=notebook_id,
        output_type="transcript_words",
        content=serialized,
    )


def is_meeting_diarized(meeting_id: int) -> bool:
    """True when the cached transcript_words has speaker_id in its words.

    Idempotency guard — the orchestrator skips diarization when this
    returns True, so worker re-runs over an already-diarized meeting
    don't re-do the (expensive) diarization step.

    Subtle edge case (named 2026-06-24 brainstorm-audit): the diarize
    step persists the merged transcript_words BEFORE re-indexing. If
    the orchestrator crashes BETWEEN diarize-save and reindex, on
    retry this check returns True, the reindex step is skipped, and
    cluster_roster_mapper runs against Qdrant chunks that lack the
    speaker_turns payload — producing empty mappings. No infinite-fail
    loop (the meeting just sits in a half-state until manual cleanup),
    but the cluster_roster_mapper output for that meeting is silently
    incorrect. If we ever observe meetings with diarized transcript_words
    but ALL cluster mappings empty, this is the failure mode to check;
    fix is to force-re-index via `index_meeting_to_qdrant.py --meeting-id N`
    then re-run cluster_roster_mapper. A future hardening could check
    the latest Qdrant payload schema version + force re-index if stale.
    """
    parsed = _load_transcript_words_row(meeting_id)
    if not parsed:
        return False
    words = parsed.get("words") or []
    if not isinstance(words, list) or not words:
        return False
    # Sample the first word — if it has speaker_id, the whole transcript
    # was diarized (we save them all at once).
    first = words[0]
    return isinstance(first, dict) and bool(first.get("speaker_id"))


def _resolve_video_url_for_meeting(meeting_id: int) -> Optional[str]:
    """Resolve the canonical video URL for a meeting, mirroring fetcher.py."""
    import database  # type: ignore
    try:
        url = database.get_resolved_video_url(meeting_id)
    except Exception as exc:
        logger.warning(
            "get_resolved_video_url(%s) raised: %s", meeting_id, exc,
        )
        return None
    return (url or "").strip() or None


def diarize_and_save_transcript_words(meeting_id: int) -> Dict[str, Any]:
    """Run /diarize on the meeting's video, merge with whisper words, persist.

    Returns a summary dict:
        {"ok": bool, "diarized_word_count": int, "speaker_count": int,
         "skipped_reason": str or None}
    """
    existing = _load_transcript_words_row(meeting_id)
    if not existing:
        return {
            "ok": False, "skipped_reason": "no transcript_words cache",
        }
    words = existing.get("words") or []
    if not words:
        return {"ok": False, "skipped_reason": "empty whisper words list"}

    video_url = _resolve_video_url_for_meeting(meeting_id)
    if not video_url:
        return {"ok": False, "skipped_reason": "no resolvable video URL"}

    import diarize_client  # type: ignore
    import speaker_merger  # type: ignore

    if not diarize_client.is_configured():
        return {
            "ok": False,
            "skipped_reason": "diarize_client not configured "
            "(zspan_diarizer_node_token / STATUS.json missing)",
        }

    # Dispatch — pick the right param based on URL shape. YouTube goes
    # via youtube_url; Granicus direct-MP4 + other audio_url via audio_url.
    is_youtube = "youtube.com" in video_url or "youtu.be" in video_url
    try:
        if is_youtube:
            result = diarize_client.diarize_via_mac_node(youtube_url=video_url)
        else:
            result = diarize_client.diarize_via_mac_node(audio_url=video_url)
    except Exception as exc:
        return {
            "ok": False,
            "skipped_reason": f"/diarize call failed: {exc}",
        }

    turns = result.get("turns") or []
    if not turns:
        return {
            "ok": False,
            "skipped_reason": f"/diarize returned 0 turns (pyannote saw no speech?)",
        }

    # Merge — produce a new word list with speaker_id per word
    merged = speaker_merger.merge_words_with_speakers(words, turns)

    # Save back. Preserve existing top-level keys (duration_seconds,
    # language, source_url, etc.) — only the words array changes shape.
    #
    # D-147 ephemeral-biometrics policy (2026-07-01): voice embeddings are
    # PROCESS-THEN-DESTROY. mac_diarizer's per-cluster embeddings exist only
    # in this response object and are deliberately NOT persisted anywhere —
    # not in diarization_meta, not in any library. What persists is the
    # non-biometric residue: SPEAKER_NN labels + timings + counts. The
    # prior V-Op-1 stash (which fed the scrapped voice library) is removed;
    # historical rows are purged by ops/purge_diarization_embeddings.py.
    updated_content = dict(existing)
    updated_content.pop("_notebook_id", None)
    updated_content["words"] = merged
    updated_content["diarized"] = True
    diarization_meta: Dict[str, Any] = {
        "model": "pyannote/speaker-diarization-3.1",
        "turn_count": len(turns),
        "speaker_count": len(result.get("speaker_summary") or {}),
        "diarization_seconds": result.get("diarization_seconds"),
        "speaker_summary": result.get("speaker_summary"),
    }
    # (speaker_embeddings from the diarizer response are intentionally
    # dropped here — see the D-147 policy comment above.)
    updated_content["diarization_meta"] = diarization_meta
    _save_transcript_words_content(
        meeting_id, existing["_notebook_id"], updated_content,
    )

    distinct_speakers = {w.get("speaker_id") for w in merged if w.get("speaker_id")}
    return {
        "ok": True,
        "diarized_word_count": len(merged),
        "speaker_count": len(distinct_speakers
                              - {"OVERLAP", "UNKNOWN"}),
        "turn_count": len(turns),
        "skipped_reason": None,
    }


def _trigger_local_reindex(meeting_id: int) -> bool:
    """Rebuild this meeting's local index after diarization.

    The refreshed transcript is already persisted before this call, so the
    atomic local replacement carries chunk-level ``speaker_turns`` into every
    downstream ``RetrievedChunk`` consumer.

    Returns True on success, False on any error (caller logs + continues).
    """
    try:
        from .worker import index_meeting_locally  # local import avoids cycle
    except Exception as exc:
        logger.warning("could not import index_meeting_locally: %s", exc)
        return False
    try:
        index_meeting_locally(meeting_id)
        return True
    except Exception as exc:
        logger.warning(
            "re-index failed for meeting=%d (non-fatal): %s", meeting_id, exc,
        )
        return False


def run_full_diarize_step(meeting_id: int, city_name: str) -> Dict[str, Any]:
    """Top-level orchestrator for synchronous or backfill diarization.

    Stages:
      0. Skip if already diarized (idempotency).
      1. Diarize + merge + persist transcript_words.
      2. Re-index locally so stored chunks carry speaker_turns.
      3. Run cluster_roster_mapper (Sonnet pass + prongs + persist).

    Returns a summary dict for the worker log. Non-fatal — any stage
    failure logs + falls through; sidecar_pipeline will still run.
    """
    summary: Dict[str, Any] = {
        "meeting_id": meeting_id,
        "city": city_name,
        "diarize_skipped": False,
        "diarize_result": None,
        "reindex_ok": False,
        "mapper_summary": None,
    }

    transcript = _load_transcript_words_row(meeting_id)
    provider = transcript.get("provider") if transcript else ""
    if isinstance(provider, str) and provider.strip().lower() == "assemblyai":
        logger.info(
            "diarize_orchestrator: meeting=%d uses AssemblyAI anonymous "
            "speaker clusters; skipping diarization and cluster-roster mapping",
            meeting_id,
        )
        summary["diarize_skipped"] = True
        summary["mapper_summary"] = {
            "skipped": True,
            "reason": "assemblyai anonymous speaker clusters are not name-resolved",
        }
        return summary

    if is_meeting_diarized(meeting_id):
        logger.info(
            "diarize_orchestrator: meeting=%d already diarized; "
            "skipping diarize+reindex; running cluster_roster_mapper for "
            "freshness", meeting_id,
        )
        summary["diarize_skipped"] = True
    else:
        logger.info(
            "diarize_orchestrator: meeting=%d — diarize + merge + save",
            meeting_id,
        )
        res = diarize_and_save_transcript_words(meeting_id)
        summary["diarize_result"] = res
        if not res["ok"]:
            logger.warning(
                "diarize_orchestrator: meeting=%d diarize step skipped (%s); "
                "remaining stages will run against undiarized transcript",
                meeting_id, res.get("skipped_reason"),
            )
            # Even when diarization fails, attempt the cluster mapper so any
            # previously-confirmed mappings continue to apply. Skip the reindex.
            try:
                from . import cluster_roster_mapper
                summary["mapper_summary"] = cluster_roster_mapper.map_clusters_for_meeting(
                    meeting_id, city_name,
                )
            except Exception as exc:
                logger.warning(
                    "cluster_roster_mapper failed (non-fatal): %s", exc,
                )
            return summary

        logger.info(
            "diarize_orchestrator: meeting=%d — rebuilding local index",
            meeting_id,
        )
        summary["reindex_ok"] = _trigger_local_reindex(meeting_id)

    logger.info(
        "diarize_orchestrator: meeting=%d — cluster_roster_mapper", meeting_id,
    )
    try:
        from . import cluster_roster_mapper
        summary["mapper_summary"] = cluster_roster_mapper.map_clusters_for_meeting(
            meeting_id, city_name,
        )
    except Exception as exc:
        logger.warning(
            "cluster_roster_mapper failed (non-fatal): %s", exc,
        )
        summary["mapper_summary"] = {"error": str(exc)}

    return summary


def classify_diarization_summary(summary: Dict[str, Any]) -> tuple[str, str]:
    """Convert the orchestrator result into a durable terminal substatus."""
    if summary.get("diarize_skipped"):
        return "succeeded", "already diarized; expensive diarization was not re-run"

    result = summary.get("diarize_result")
    if isinstance(result, dict) and result.get("ok") is True:
        return (
            "succeeded",
            "diarized "
            f"{int(result.get('diarized_word_count') or 0)} words across "
            f"{int(result.get('speaker_count') or 0)} speakers",
        )

    reason = "orchestrator returned no confirmed diarization result"
    if isinstance(result, dict) and result.get("skipped_reason"):
        reason = str(result["skipped_reason"])
    return "failed", reason

"""sidecar_pipeline — produce the new-discipline .preview/* JSON sidecars.

After V1-RAG-3 processing completes for a meeting (the qdrant_synthesize
strategies have populated notebook_outputs + qdrant_extract_quotes has
populated the SQL quotes table), this module produces the 4 .preview/
sidecar JSON files that BroadcastPage's new-shape rendering reads from:

  - m<id>.json           — extracted quotes with selection-discipline metadata
                           (news_values, selection_rationale per quote)
  - m<id>_decisions.json — Round 4 decisions prose + citation evidence
  - m<id>_routing.json   — quote_router classification (standalone /
                           decision_bound / drop)
  - m<id>_recusals.json  — regex-detected recusal events

The orchestrator also writes m<id>_pipeline_state.json after every stage
transition. That state is an operator audit trail; resume decisions are made
from validated artifacts so an interrupted state write cannot cause expensive
completed synthesis to be repeated.

The 4 in-repo downstream scripts (quote_router_runner, recusal_detector,
align_preview_quotes, rationale_rewriter) handle the post-extraction
stages. This module provides the missing front-half:
  - Stage 1: quote extraction preserving the full selection-discipline
             fields (qdrant_quote_extractor's ExtractedQuote dataclass
             drops news_values + selection_rationale; we re-implement
             the batch loop here to keep the raw generator dicts intact).
  - Stage 2: decisions synthesis with the Round 4 two-part citation anchor +
             audit-json parsing from the trailing comment block.

Closes the F5 + F6 brainstorm-audit findings from 2026-06-24:
"orchestrator was missing; only m103753 had the sidecars because they
were hand-produced during the design session."

Composes with:
  - zspan_pipeline.qdrant_synthesizer (complete evidence + generation)
  - zspan_pipeline.qdrant_quote_extractor (prompt-building helpers
    + canonical-roster formatter)
  - zspan_pipeline.symbols (symbols-block builder)
  - zspan_pipeline.quote_router_runner (Stage 3 — subprocess-called)
  - zspan_pipeline.recusal_detector (Stage 4)
  - zspan_pipeline.align_preview_quotes (Stage 5)
  - zspan_pipeline.rationale_rewriter (Stage 6)
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import (
    citation_validator,
    qdrant_quote_extractor,
    qdrant_synthesizer,
    symbols,
)
from council_navigator.parsers import quote_align

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
PREVIEW_DIR = _REPO_ROOT / ".preview"

DECISIONS_TIMEOUT_ENV = "ZSPAN_DECISIONS_TIMEOUT_SECONDS"
# Full-meeting decisions prompts observed in production are roughly
# 119k-139k characters and have already completed close to the former 300s
# ceiling.  Three times that observed edge gives normal throughput variance a
# useful buffer without allowing a wedged subprocess to run indefinitely.
DEFAULT_DECISIONS_TIMEOUT_SECONDS = 900.0
PIPELINE_STATE_VERSION = 1

# Audit block delimiter — the Round 3 key_decisions prompt emits this
# trailing HTML-comment block carrying the per-decision news_values +
# rationale array. The renderer strips it before display; we parse it
# here for the operator-debug surface.
_AUDIT_BLOCK_RE = re.compile(
    r"<!--\s*audit\s*(\[.*?\])\s*audit\s*-->", re.DOTALL
)
_CITATION_RE = re.compile(
    r"\s*(?:\[\d+(?:[-,\s\d]*)\]|\[at\s+(?:\d+:)?\d{1,3}:\d{2}\])",
    re.IGNORECASE,
)


# ── helpers ───────────────────────────────────────────────────────────


def _strip_json_fence(raw: str) -> str:
    """Strip optional ```json fences (mirrors qdrant_quote_extractor's helper)."""
    body = raw.strip()
    if body.startswith("```"):
        lines = body.split("\n")
        if len(lines) >= 2:
            body = "\n".join(lines[1:])
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3].rstrip()
    return body.strip()


def _load_transcript_words(meeting_id: int) -> dict[str, Any]:
    """Load canonical timed words without importing NumPy at module import."""
    from .local_vector_store import load_transcript_words

    return load_transcript_words(meeting_id)


def _get_canonical_roster(city_name: str) -> str:
    """Build the canonical roster block from the council_members table."""
    parsers_path = _REPO_ROOT / "02_Core_Project" / "council_navigator" / "parsers"
    if str(parsers_path) not in sys.path:
        sys.path.insert(0, str(parsers_path))
    try:
        from database import get_council_members
        members = get_council_members(city_name) or []
    except Exception as exc:
        logger.warning("get_council_members(%s) failed: %s", city_name, exc)
        members = []
    return qdrant_quote_extractor._format_canonical_roster(members)


def _apply_corrections(city_name: str, text: str) -> str:
    """Run the deterministic post-Whisper substitutions from
    city_vocabulary_corrections against `text`. Returns corrected text
    (or original if anything fails — corrections are best-effort).

    Mirrors the discipline fetcher.py applies to V1-RAG-3 batch text
    outputs (synopsis / key_decisions / etc.) — extends it to the new
    sidecar pipeline so quotes + decisions sidecars get the same
    Whisper-error cleanup. Wired 2026-06-24 after James spotted
    "Lake Kavasu" / "Lake Kavisw" passing through to the broadcast UI.
    """
    if not text or not city_name:
        return text
    parsers_path = _REPO_ROOT / "02_Core_Project" / "council_navigator" / "parsers"
    if str(parsers_path) not in sys.path:
        sys.path.insert(0, str(parsers_path))
    try:
        from database import apply_city_corrections
        corrected, _log = apply_city_corrections(city_name, text)
        return corrected
    except Exception as exc:
        logger.warning(
            "apply_city_corrections failed for city=%s (returning original): %s",
            city_name, exc,
        )
        return text


def _decisions_timeout_seconds() -> float:
    """Resolve the full-meeting decisions timeout from the operator env."""
    raw = os.environ.get(DECISIONS_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_DECISIONS_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{DECISIONS_TIMEOUT_ENV} must be a positive number; got {raw!r}"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            f"{DECISIONS_TIMEOUT_ENV} must be a positive finite number; got {raw!r}"
        )
    return timeout


# ── Stage 1: quotes sidecar ────────────────────────────────────────────


def produce_quotes_sidecar(meeting_id: int, city_name: str) -> Path:
    """Extract quotes from every chronological chunk (preserving news_values +
    selection_rationale) and write .preview/m<id>.json.

    Phase 2 D7: when the meeting has confirmed cluster→roster mappings
    (auto-promoted or operator-confirmed via meeting_speaker_roster),
    inject a CLUSTER_ROSTER block into the extraction prompt so Sonnet
    uses cluster labels as the authoritative attribution signal instead
    of proximity inference. Builds to empty string + transparently falls
    back when no mappings exist.
    """
    sidecar_path = PREVIEW_DIR / f"m{meeting_id}.json"

    symbols_block = symbols.build_symbols_block(city_name) or ""
    canonical_roster = _get_canonical_roster(city_name)
    extraction_instructions = qdrant_quote_extractor._load_extraction_prompt()

    # Phase 2 D7 — pull the meeting's confirmed cluster→roster mappings.
    try:
        from . import cluster_roster_mapper
        cluster_roster_block = cluster_roster_mapper.build_cluster_roster_block(meeting_id)
    except Exception as exc:
        logger.warning(
            "build_cluster_roster_block failed for meeting=%d (proceeding "
            "without CLUSTER_ROSTER): %s", meeting_id, exc,
        )
        cluster_roster_block = ""

    chunks = qdrant_synthesizer.load_complete_meeting_chunks(meeting_id)

    batches_per = qdrant_quote_extractor.DEFAULT_CHUNKS_PER_BATCH
    batches = [chunks[i:i + batches_per] for i in range(0, len(chunks), batches_per)]

    started = time.monotonic()
    all_quotes_raw: list[dict] = []
    generation_results: list[qdrant_synthesizer.GenerationResult] = []

    for i, batch in enumerate(batches):
        prompt = qdrant_quote_extractor.build_extraction_prompt(
            extraction_instructions=extraction_instructions,
            symbols_block=symbols_block,
            canonical_roster=canonical_roster,
            chunks=batch,
            meeting_id=meeting_id,
            batch_index=i,
            batch_total=len(batches),
            cluster_roster_block=cluster_roster_block,
        )
        generation = qdrant_synthesizer.generate_with_fallback(
            prompt,
            timeout_seconds=qdrant_quote_extractor.DEFAULT_PER_BATCH_TIMEOUT,
        )
        generation_results.append(generation)
        raw = _strip_json_fence(generation.content)
        parsed = json.loads(raw)
        if not isinstance(parsed.get("quotes"), list):
            raise ValueError(
                f"quote sidecar batch {i} has no quotes list for meeting={meeting_id}"
            )
        for q in parsed["quotes"]:
            if isinstance(q, dict) and q.get("speaker_name") and q.get("quote_text"):
                all_quotes_raw.append(q)
        logger.info(
            "  quote batch %d/%d → %d quotes (running %d)",
            i + 1, len(batches),
            len(parsed["quotes"]), len(all_quotes_raw),
        )

    # Apply post-extraction city corrections to each quote's quote_text +
    # selection_rationale. Mirrors fetcher.py:1597 _maybe_apply_city_corrections
    # discipline on V1-RAG-3 batch outputs. Without this, Whisper transcription
    # errors (Mojave→Mohave, Kavasu→Havasu, etc.) flow straight through to
    # the broadcast UI in the quote body — gap closed 2026-06-24.
    for q in all_quotes_raw:
        if isinstance(q.get("quote_text"), str):
            q["quote_text"] = _apply_corrections(city_name, q["quote_text"])
        if isinstance(q.get("selection_rationale"), str):
            q["selection_rationale"] = _apply_corrections(
                city_name, q["selection_rationale"],
            )

    sidecar = {
        "meeting_id": meeting_id,
        "city": city_name,
        "prompt_path": "02_Core_Project/prompts/quote_extraction.md",
        "extraction_started": "complete",
        "evidence_mode": "complete_transcript",
        "batches_total": len(batches),
        "batches_completed": len(batches),
        "batches_failed": [],
        "chunks_total": len(chunks),
        "model_ids": [result.model_id for result in generation_results],
        "generation_attempts": [
            [attempt.as_dict() for attempt in result.attempts]
            for result in generation_results
        ],
        "quote_count": len(all_quotes_raw),
        "elapsed_seconds": time.monotonic() - started,
        "quotes": all_quotes_raw,
    }

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(sidecar, indent=2))
    return sidecar_path


# ── Stage 2: decisions sidecar ─────────────────────────────────────────


def produce_decisions_sidecar(meeting_id: int, city_name: str) -> Path:
    """Run key_decisions Round 4 synthesis + parse prose + audit_json,
    write .preview/m<id>_decisions.json."""
    sidecar_path = PREVIEW_DIR / f"m{meeting_id}_decisions.json"

    canonical_prompt = qdrant_synthesizer.load_canonical_prompt("key_decisions")
    started = time.monotonic()
    chunks = qdrant_synthesizer.load_complete_meeting_chunks(meeting_id)

    prompt = qdrant_synthesizer.build_synthesis_prompt(
        output_type="key_decisions",
        canonical_prompt=canonical_prompt,
        meeting_id=meeting_id,
        chunks=chunks,
    )
    timeout_seconds = _decisions_timeout_seconds()
    logger.info(
        "key_decisions meeting=%d: prompt_chars=%d timeout=%.0fs (%s)",
        meeting_id,
        len(prompt),
        timeout_seconds,
        DECISIONS_TIMEOUT_ENV if DECISIONS_TIMEOUT_ENV in os.environ else "default",
    )
    generation = qdrant_synthesizer.generate_with_fallback(
        prompt,
        timeout_seconds=timeout_seconds,
    )
    raw = generation.content

    # Split prose from <!-- audit ... audit --> block
    m = _AUDIT_BLOCK_RE.search(raw)
    if m:
        prose = raw[:m.start()].strip()
        try:
            audit_json = json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            logger.warning("audit JSON parse failed: %s", exc)
            audit_json = None
    else:
        prose = raw.strip()
        audit_json = None
        logger.warning(
            "No <!-- audit ... audit --> block in key_decisions output "
            "for meeting=%d (audit chips will be missing)", meeting_id,
        )

    # Apply post-extraction city corrections to the prose_output (same
    # discipline fetcher.py applies to V1-RAG-3 batch key_decisions
    # output). Whisper errors that survive into decision sentences get
    # cleaned up here before the sidecar lands.
    prose = _apply_corrections(city_name, prose)

    # The model's locator identifies a retrieved neighborhood, but chunk starts
    # are not word-precise enough for "click and hear it immediately."  Anchor
    # the distinctive item introduction first, then align the later action
    # quote inside that forward-only agenda boundary.  Fail closed per decision
    # on weak or ambiguous evidence rather than publishing a guessed seek.
    transcript = _load_transcript_words(meeting_id)
    alignment = citation_validator.align_decision_citations(
        prose,
        transcript["words"],
        chunks,
        anchors=audit_json if isinstance(audit_json, list) else None,
    )
    if not alignment.aligned_indices:
        raise RuntimeError(
            f"key_decisions citation alignment failed for every decision "
            f"meeting={meeting_id}: "
            f"{alignment.failures}"
        )
    prose = alignment.text
    quote_observation_reasons = {
        entry["output_index"]: "quote_anchored_outside_retrieved_chunks"
        for entry in alignment.per_decision
        if entry.get("source") == "two_part_quote"
        and isinstance(entry.get("output_index"), int)
    }
    citation_report = citation_validator.validate_inline_citations(
        prose,
        chunks,
        membership_observation_reasons=quote_observation_reasons,
    )
    if citation_report.state != "valid":
        raise RuntimeError(
            f"key_decisions citation validation failed for meeting={meeting_id}: "
            f"state={citation_report.state} "
            f"uncovered={citation_report.uncovered_indices} "
            f"unknown={citation_report.unknown_citations}"
        )
    if citation_report.nonmember_observations:
        logger.warning(
            "key_decisions meeting=%d: quote-anchored citations outside "
            "retrieved chunks recorded as observations=%s",
            meeting_id,
            citation_report.nonmember_observations,
        )
    logger.info(
        "key_decisions meeting=%d: emitted %d/%d decision citations; omitted=%d",
        meeting_id,
        len(alignment.aligned_indices),
        alignment.decisions_total,
        len(alignment.failures),
    )

    if alignment.failures:
        logger.warning(
            "key_decisions meeting=%d: omitted decisions=%s; survivors were "
            "renumbered contiguously using index_map=%s",
            meeting_id,
            alignment.failures,
            alignment.index_map,
        )

    # Keep operator-audit entries synchronized with the surviving prose.  An
    # audit list was also the source of quote anchors, so a surviving decision
    # necessarily has a same-index entry; legacy outputs keep audit_json=None.
    if isinstance(audit_json, list):
        audit_by_index = {
            entry.get("index"): entry
            for entry in audit_json
            if isinstance(entry, dict)
            and isinstance(entry.get("index"), int)
            and not isinstance(entry.get("index"), bool)
        }
        synchronized_audit: list[dict[str, Any]] = []
        for mapping in alignment.index_map:
            entry = audit_by_index.get(mapping["source_index"])
            if entry is None:
                # Defensive: the aligner should already have dropped this row.
                raise RuntimeError(
                    "key_decisions audit synchronization invariant failed "
                    f"meeting={meeting_id} source_index={mapping['source_index']}"
                )
            synchronized = dict(entry)
            synchronized["index"] = mapping["output_index"]
            synchronized_audit.append(synchronized)
        audit_json = synchronized_audit

    # Also run corrections through each audit-json rationale string —
    # operator-debug surface but still benefits from clean text.
    if isinstance(audit_json, list):
        for entry in audit_json:
            if isinstance(entry, dict) and isinstance(entry.get("rationale"), str):
                entry["rationale"] = _apply_corrections(city_name, entry["rationale"])

    # Materialize presentation evidence only after omission/renumbering has
    # settled. Every surviving decision must retain both verified anchors;
    # signature fallbacks are insufficient for transcript_excerpt_v1.
    decision_spans: list[dict[str, Any]] = []
    for entry in alignment.per_decision:
        output_index = entry.get("output_index")
        if output_index is None:
            continue
        item_evidence = entry.get("item_evidence")
        action_evidence = entry.get("action_evidence")
        if (
            entry.get("source") != "two_part_quote"
            or not isinstance(item_evidence, dict)
            or not isinstance(action_evidence, dict)
        ):
            raise RuntimeError(
                "key_decisions transcript excerpt requires two-part anchors "
                f"meeting={meeting_id} output_index={output_index}"
            )
        try:
            spans = quote_align.materialize_transcript_excerpt(
                transcript["words"], item_evidence, action_evidence,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "key_decisions transcript excerpt materialization failed "
                f"meeting={meeting_id} output_index={output_index}: {exc}"
            ) from exc
        span_errors = quote_align.validate_transcript_excerpt_spans(
            transcript["words"], spans, item_evidence, action_evidence,
        )
        if span_errors:
            raise RuntimeError(
                "key_decisions transcript excerpt validation failed "
                f"meeting={meeting_id} output_index={output_index}: "
                f"{span_errors}"
            )
        decision_spans.append(
            {"index": output_index, "verbatim_spans": spans}
        )

    if len(decision_spans) != len(alignment.aligned_indices):
        raise RuntimeError(
            "key_decisions transcript excerpt coverage invariant failed "
            f"meeting={meeting_id} spans={len(decision_spans)} "
            f"survivors={len(alignment.aligned_indices)}"
        )

    # Stored display prose is non-seekable. Locator stripping is deliberately
    # last: the anchors, span materialization, and inline-citation verifier all
    # consume the aligned locators before this presentation-only cleanup.
    prose = _CITATION_RE.sub("", prose)
    prose_list_count = len(re.findall(r"^\s*\d+\.\s", prose, re.MULTILINE))

    sidecar = {
        "meeting_id": meeting_id,
        "city": city_name,
        "prompt_path": "02_Core_Project/prompts/key_decisions.md",
        "prompt_round": "Round 4 — two-part item/action citation anchors",
        "citation_modality": quote_align.TRANSCRIPT_EXCERPT_MODALITY,
        "extraction_started": "complete",
        "evidence_mode": "complete_transcript",
        "model_id": generation.model_id,
        "generation_attempts": [
            attempt.as_dict() for attempt in generation.attempts
        ],
        "elapsed_seconds": time.monotonic() - started,
        "chunks_total": len(chunks),
        "prose_output": prose,
        "prose_list_count": prose_list_count,
        "audit_json": audit_json,
        "citation_alignment": [
            entry for entry in alignment.per_decision if "output_index" in entry
        ],
        "citation_observations": citation_report.nonmember_observations,
        "citation_omissions": alignment.failures,
        "decisions": decision_spans,
    }

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(sidecar, indent=2))
    return sidecar_path


# ── Stages 3-6: subprocess-call the 4 in-repo downstream scripts ──────


def _run_downstream(module: str, meeting_id: int) -> None:
    """Subprocess-call one of the 4 read-only downstream scripts."""
    cmd = [sys.executable, "-m", module, "--meeting-id", str(meeting_id)]
    logger.info("  → %s", " ".join(cmd))
    cwd = _REPO_ROOT / "02_Core_Project"
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(cwd), timeout=300,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-400:]
        raise RuntimeError(f"{module} failed (rc={result.returncode}): {tail!r}")


# ── Top-level orchestrator ─────────────────────────────────────────────


class PipelineIncompleteError(RuntimeError):
    """A publication-required stage is still missing after an attempted run."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(meeting_id: int) -> Path:
    return PREVIEW_DIR / f"m{meeting_id}_pipeline_state.json"


def _read_json_artifact(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unreadable pipeline artifact %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("Pipeline artifact %s is not a JSON object", path)
        return None
    return payload


def _quotes_artifact_complete(meeting_id: int) -> bool:
    payload = _read_json_artifact(PREVIEW_DIR / f"m{meeting_id}.json")
    return bool(
        payload
        and payload.get("meeting_id") == meeting_id
        and payload.get("extraction_started") == "complete"
        and isinstance(payload.get("quotes"), list)
    )


def _decisions_artifact_complete(meeting_id: int) -> bool:
    payload = _read_json_artifact(PREVIEW_DIR / f"m{meeting_id}_decisions.json")
    return bool(
        payload
        and payload.get("meeting_id") == meeting_id
        and payload.get("extraction_started") == "complete"
        and isinstance(payload.get("prose_output"), str)
    )


def _routing_artifact_complete(meeting_id: int) -> bool:
    payload = _read_json_artifact(PREVIEW_DIR / f"m{meeting_id}_routing.json")
    return bool(
        payload
        and payload.get("meeting_id") == meeting_id
        and payload.get("router_started") == "complete"
        and isinstance(payload.get("routing"), list)
    )


def _recusals_artifact_complete(meeting_id: int) -> bool:
    payload = _read_json_artifact(PREVIEW_DIR / f"m{meeting_id}_recusals.json")
    return bool(
        payload
        and payload.get("meeting_id") == meeting_id
        and isinstance(payload.get("recusals"), list)
    )


def _alignment_artifact_complete(meeting_id: int) -> bool:
    payload = _read_json_artifact(PREVIEW_DIR / f"m{meeting_id}.json")
    if not payload or payload.get("meeting_id") != meeting_id:
        return False
    quotes = payload.get("quotes")
    if not isinstance(quotes, list):
        return False
    if not quotes:
        return True
    return bool(
        "align_elapsed_seconds" in payload
        and "align_aligned_count" in payload
        and "align_failed_count" in payload
        and all(
            not isinstance(quote, dict)
            or not str(quote.get("quote_text") or "").strip()
            or "word_timings" in quote
            for quote in quotes
        )
    )


def _rationale_artifact_complete(meeting_id: int) -> bool:
    payload = _read_json_artifact(PREVIEW_DIR / f"m{meeting_id}.json")
    if not payload or payload.get("meeting_id") != meeting_id:
        return False
    quotes = payload.get("quotes")
    if not isinstance(quotes, list):
        return False
    if not quotes:
        return True
    return bool(
        "rationale_rewrite_elapsed_seconds" in payload
        and "rationale_rewritten_count" in payload
    )


def _write_pipeline_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically persist enough state to resume after process failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _utc_now()
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _new_pipeline_state(
    path: Path,
    meeting_id: int,
    city_name: str,
    stages: list[dict[str, Any]],
) -> dict[str, Any]:
    previous = _read_json_artifact(path) or {}
    previous_stages = previous.get("stages")
    if not isinstance(previous_stages, dict):
        previous_stages = {}

    stage_state: dict[str, dict[str, Any]] = {}
    for stage in stages:
        old_stage = previous_stages.get(stage["name"], {})
        history = list(old_stage.get("history") or []) if isinstance(old_stage, dict) else []
        if isinstance(old_stage, dict) and old_stage.get("status") == "running":
            history.append({
                "status": "failed",
                "finished_at": _utc_now(),
                "reason": "previous run ended while this stage was running",
            })
        stage_state[stage["name"]] = {
            "index": stage["index"],
            "label": stage["label"],
            "required_for_publication": stage["required"],
            "artifact": str(stage["artifact"]),
            "status": "not_reached",
            "reason": "stage has not been evaluated in this run",
            "started_at": None,
            "finished_at": None,
            "history": history,
        }

    return {
        "version": PIPELINE_STATE_VERSION,
        "meeting_id": meeting_id,
        "city": city_name,
        "run_number": int(previous.get("run_number") or 0) + 1,
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "finished_at": None,
        "outcome": "running",
        "publication_required_sidecars_complete": False,
        "required_stages": [stage["name"] for stage in stages if stage["required"]],
        "best_effort_stages": [stage["name"] for stage in stages if not stage["required"]],
        "stages": stage_state,
    }


def _record_stage_status(
    path: Path,
    state: dict[str, Any],
    stage_name: str,
    status: str,
    reason: str,
) -> None:
    stage = state["stages"][stage_name]
    now = _utc_now()
    stage["status"] = status
    stage["reason"] = reason
    if status == "running":
        stage["started_at"] = now
        stage["finished_at"] = None
    else:
        stage["finished_at"] = now
        stage["history"].append({
            "status": status,
            "started_at": stage.get("started_at"),
            "finished_at": now,
            "reason": reason,
        })
    _write_pipeline_state(path, state)


def _dependencies_available(
    stage_name: str,
    artifact_checks: dict[str, Callable[[], bool]],
) -> tuple[bool, str]:
    quotes = artifact_checks["quotes"]()
    decisions = artifact_checks["decisions"]()
    if stage_name == "routing" and not (quotes and decisions):
        return False, "requires completed quotes and decisions artifacts"
    if stage_name == "recusals" and not (quotes or decisions):
        return False, "requires at least one completed quotes or decisions artifact"
    if stage_name in {"alignment", "rationale_rewrite"} and not quotes:
        return False, "requires a completed quotes artifact"
    return True, ""


def run_pipeline(meeting_id: int, city_name: str) -> dict:
    """Resume or run all sidecar stages for one meeting.

    Stages:
      1. extract quotes → m<id>.json
      2. extract decisions → m<id>_decisions.json
      3. quote_router_runner → m<id>_routing.json
      4. recusal_detector → m<id>_recusals.json
      5. align_preview_quotes → populates word_timings IN PLACE in m<id>.json
      6. rationale_rewriter → adds rewritten rationale IN PLACE in m<id>.json

    The cited decisions sidecar is the only publication-required artifact.
    Quotes, routing, recusals, alignment, and rationale rewriting enrich the
    public page but are best-effort under the current publication contract.
    A required-stage failure is raised only after every still-runnable stage
    has been attempted. Best-effort failures remain loud in logs, state, and
    the returned summary.
    """
    logger.info(
        "sidecar_pipeline: meeting=%d city=%s — starting", meeting_id, city_name,
    )
    started = time.monotonic()
    quotes_path = PREVIEW_DIR / f"m{meeting_id}.json"
    decisions_path = PREVIEW_DIR / f"m{meeting_id}_decisions.json"

    stages: list[dict[str, Any]] = [
        {
            "index": 1,
            "name": "quotes",
            "label": "extracting quotes",
            "required": False,
            "artifact": quotes_path,
            "check": lambda: _quotes_artifact_complete(meeting_id),
            "run": lambda: produce_quotes_sidecar(meeting_id, city_name),
        },
        {
            "index": 2,
            "name": "decisions",
            "label": "extracting decisions",
            "required": True,
            "artifact": decisions_path,
            "check": lambda: _decisions_artifact_complete(meeting_id),
            "run": lambda: produce_decisions_sidecar(meeting_id, city_name),
        },
        {
            "index": 3,
            "name": "routing",
            "label": "quote_router_runner",
            "required": False,
            "artifact": PREVIEW_DIR / f"m{meeting_id}_routing.json",
            "check": lambda: _routing_artifact_complete(meeting_id),
            "run": lambda: _run_downstream(
                "zspan_pipeline.quote_router_runner", meeting_id,
            ),
        },
        {
            "index": 4,
            "name": "recusals",
            "label": "recusal_detector",
            "required": False,
            "artifact": PREVIEW_DIR / f"m{meeting_id}_recusals.json",
            "check": lambda: _recusals_artifact_complete(meeting_id),
            "run": lambda: _run_downstream(
                "zspan_pipeline.recusal_detector", meeting_id,
            ),
        },
        {
            "index": 5,
            "name": "alignment",
            "label": "align_preview_quotes",
            "required": False,
            "artifact": quotes_path,
            "check": lambda: _alignment_artifact_complete(meeting_id),
            "run": lambda: _run_downstream(
                "zspan_pipeline.align_preview_quotes", meeting_id,
            ),
        },
        {
            "index": 6,
            "name": "rationale_rewrite",
            "label": "rationale_rewriter",
            "required": False,
            "artifact": quotes_path,
            "check": lambda: _rationale_artifact_complete(meeting_id),
            "run": lambda: _run_downstream(
                "zspan_pipeline.rationale_rewriter", meeting_id,
            ),
        },
    ]
    artifact_checks = {stage["name"]: stage["check"] for stage in stages}
    state_path = _state_path(meeting_id)
    state = _new_pipeline_state(state_path, meeting_id, city_name, stages)
    _write_pipeline_state(state_path, state)

    for stage in stages:
        name = stage["name"]
        prefix = f"sidecar_pipeline [{stage['index']}/6]"
        if stage["check"]():
            reason = "completed artifact already exists"
            logger.info("%s ⏭ SKIPPED: %s (%s)", prefix, stage["label"], reason)
            _record_stage_status(state_path, state, name, "skipped", reason)
            continue

        available, unavailable_reason = _dependencies_available(name, artifact_checks)
        if not available:
            logger.warning(
                "%s ⏸ NOT REACHED: %s (%s)",
                prefix,
                stage["label"],
                unavailable_reason,
            )
            stage_state = state["stages"][name]
            stage_state["reason"] = unavailable_reason
            _write_pipeline_state(state_path, state)
            continue

        logger.info("%s: %s", prefix, stage["label"])
        _record_stage_status(state_path, state, name, "running", "stage started")
        try:
            stage["run"]()
            if not stage["check"]():
                raise RuntimeError(
                    f"stage returned without producing a complete artifact: "
                    f"{stage['artifact']}"
                )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.exception("%s ❌ FAILED: %s", prefix, reason)
            _record_stage_status(state_path, state, name, "failed", reason)
            continue

        logger.info("%s ✅ completed: %s", prefix, stage["artifact"])
        _record_stage_status(
            state_path,
            state,
            name,
            "completed",
            "artifact produced and validated",
        )

    elapsed = time.monotonic() - started
    required_complete = _decisions_artifact_complete(meeting_id)
    all_complete = all(stage["check"]() for stage in stages)
    if all_complete:
        outcome = "complete"
    elif required_complete:
        outcome = "required_stages_complete_with_best_effort_gaps"
    else:
        outcome = "failed_required_stage"

    state["outcome"] = outcome
    state["publication_required_sidecars_complete"] = required_complete
    state["elapsed_seconds"] = elapsed
    state["finished_at"] = _utc_now()
    _write_pipeline_state(state_path, state)

    summary = {
        "meeting_id": meeting_id,
        "city": city_name,
        "quotes_path": str(quotes_path) if quotes_path.exists() else "",
        "decisions_path": str(decisions_path) if decisions_path.exists() else "",
        "state_path": str(state_path),
        "outcome": outcome,
        "publication_required_sidecars_complete": required_complete,
        "stages": {
            name: stage_state["status"]
            for name, stage_state in state["stages"].items()
        },
        "elapsed_seconds": elapsed,
    }

    if not required_complete:
        decisions_reason = state["stages"]["decisions"]["reason"]
        logger.error(
            "sidecar_pipeline: meeting=%d ❌ required decisions stage missing; "
            "resume state=%s reason=%s",
            meeting_id,
            state_path,
            decisions_reason,
        )
        raise PipelineIncompleteError(
            f"meeting={meeting_id} lacks its publication-required decisions sidecar "
            f"({decisions_reason}); resume state: {state_path}"
        )

    if all_complete:
        logger.info(
            "sidecar_pipeline: meeting=%d ✅ complete in %.1fs", meeting_id, elapsed,
        )
    else:
        logger.warning(
            "sidecar_pipeline: meeting=%d publication-required sidecars complete "
            "with best-effort gaps in %.1fs; state=%s",
            meeting_id,
            elapsed,
            state_path,
        )
    return summary

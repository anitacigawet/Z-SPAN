"""V1-RAG-3 quote extractor — complete transcript + attributed-quote extract.

Restores the linker pass that V1 mode (ZSPAN_V1_RAG3_ONLY=1)
disabled per [D-126](../../01_Project_Overview/DECISIONS.md#d-126). The
retired extraction was acting as a linker: it consumed the [SYMBOLS]
block (canonical-name-to-aliases table from `current_members` +
`whisper_vocabulary_hints`) and used LLM reasoning to attribute
imperfectly-transcribed surnames to canonical council_members rows.

Bypassing it left V1-RAG-3 meetings (Bullhead trio + CC) with populated
`council_members` rosters but zero member-attributed `quotes` rows,
breaking the existing TruthBook surface past Kingman. This module
The current linker processes every indexed transcript chunk in chronological
batches through the bounded flagship generation chain.

Pure extraction — no DB writes happen here. The caller (D4 persistence +
D5 worker integration) handles persistence:

    extracted = extract_quotes_for_meeting(meeting_id=103225, city_name="Bullhead City")
    # extracted is list[ExtractedQuote]; D4's save path resolves
    # speaker_name → member_id + runs quote_align + INSERTs into quotes table.

Composes with:
  - zspan_pipeline.symbols.build_symbols_block (the existing linker
    contract generator)
  - zspan_pipeline.qdrant_synthesizer (complete chunk loading + bounded
    flagship generation)
  - prompts/quote_extraction.md (D2 — the canonical extraction prompt)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import qdrant_synthesizer
from . import symbols
from .prompt_loader import strip_explicit_model_boundaries
from council_navigator.parsers.topic_tags import normalize_tags

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPTS_DIR = _THIS_DIR.parent / "prompts"
QUOTE_EXTRACTION_PROMPT = DEFAULT_PROMPTS_DIR / "quote_extraction.md"

# Chunks per generation call. Each chunk averages ~400 tokens; 5 × 400 = 2000
# tokens of context (+ ~3000 tokens of symbols / roster / instructions) =
# ~5000-token prompts. D6 smoke (2026-06-20) empirics:
#   - 4 chunks completed in 115s (1076 chars output)
#   - 8 chunks timed out at 400s (5 of 7 batches landed; 2 lost)
# 5 chunks scales linearly to ~150s typical wall-clock with the 500s
# timeout below — well clear of the cliff. More batches per meeting (~16
# vs 10) but each one reliably completes, so coverage isn't lost. Total
# wall-clock is roughly equivalent to the larger-batch ideal once the
# timeout-and-retry overhead of the larger size is amortized.
DEFAULT_CHUNKS_PER_BATCH = 5

# Per-batch wall-clock budget. 500s gives ~3x buffer over expected
# wall-clock for the 5-chunk size, well clear of variance. Per the
# observed Sonnet output rate (~10s per ~100 output chars), a 5-chunk
# batch producing ~1500 chars of JSON takes ~150s; raise this if Sonnet
# throughput degrades or the prompt grows.
DEFAULT_PER_BATCH_TIMEOUT = 500.0

@dataclass
class ExtractedQuote:
    """One attributed quote candidate from a per-batch generation."""

    speaker_name: str  # canonical name (per roster) for council members
    speaker_role: str  # Mayor / Vice Mayor / Council Member / Staff / Expert
    speaker_class: str  # council_member / staff / expert
    quote_text: str  # verbatim from a chunk
    topic_tags: list[str]  # from the controlled vocab
    video_timestamp_seconds: int  # chunk start_seconds
    chunk_index: int  # source chunk identifier
    speaker_cluster_label: Optional[str] = None  # Phase 2 D5 — diarized cluster label

    @classmethod
    def from_dict(cls, d: dict) -> "ExtractedQuote":
        raw_cluster = d.get("speaker_cluster_label")
        return cls(
            speaker_name=str(d.get("speaker_name", "")).strip(),
            speaker_role=str(d.get("speaker_role", "")).strip(),
            speaker_class=str(d.get("speaker_class", "")).strip(),
            quote_text=str(d.get("quote_text", "")).strip(),
            topic_tags=normalize_tags(
                [t for t in (d.get("topic_tags") or []) if isinstance(t, str)]
            ),
            video_timestamp_seconds=int(d.get("video_timestamp_seconds", 0) or 0),
            chunk_index=int(d.get("chunk_index", 0) or 0),
            speaker_cluster_label=(
                str(raw_cluster).strip() if raw_cluster else None
            ),
        )

    def is_valid(self) -> bool:
        return bool(self.speaker_name and self.quote_text and self.speaker_class)


def _load_extraction_prompt() -> str:
    """Load the prompt body, stripping the YAML frontmatter (mirrors
    qdrant_synthesizer.load_canonical_prompt's frontmatter strip)."""
    if not QUOTE_EXTRACTION_PROMPT.exists():
        raise FileNotFoundError(
            f"quote_extraction prompt not found at {QUOTE_EXTRACTION_PROMPT}. "
            f"D2 ships the prompt; verify it landed cleanly."
        )
    text = QUOTE_EXTRACTION_PROMPT.read_text()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
    return strip_explicit_model_boundaries(text)


def _format_canonical_roster(members: list[dict]) -> str:
    """Render the council_members roster as a CANONICAL_ROSTER block for
    the extraction prompt. Sonnet reads this as the ground-truth list for
    speaker_name attribution."""
    lines = ["CANONICAL_ROSTER:"]
    seen: set[str] = set()
    for m in members:
        name = (m.get("name") or "").strip()
        role = (m.get("role") or "Council Member").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        lines.append(f"  - {name} ({role})")
    if len(lines) == 1:
        lines.append("  (no council_members rows in DB for this city)")
    return "\n".join(lines)


def _format_chunk_for_extraction(chunk) -> str:
    """One chunk rendered as a labeled block for inclusion in the
    extraction prompt. The chunk_index + start_seconds are required so
    Sonnet can attach them to extracted quotes for downstream alignment.

    Phase 2 D5 (2026-06-24): when the chunk carries speaker_turns
    (diarized meeting), the body renders as `SPEAKER_NN: "<text>"` blocks
    so Sonnet directly cites the cluster label that ships with each line
    instead of inferring from textual proximity. The cluster→roster
    mapping happens downstream via D6's cluster_roster_mapper; for the
    extraction itself, Sonnet quotes the cluster label verbatim (e.g.,
    `speaker_name: "SPEAKER_03"`) and the persistence layer resolves
    the canonical roster name via `get_canonical_for_cluster`.
    """
    start_min = int(chunk.start_seconds // 60)
    start_sec = int(chunk.start_seconds % 60)
    header = (
        f"[chunk_index={chunk.chunk_index} "
        f"start_seconds={int(chunk.start_seconds)} "
        f"timecode={start_min:02d}:{start_sec:02d}]"
    )
    speaker_turns = getattr(chunk, "speaker_turns", None)
    if speaker_turns:
        body_lines = [
            f"  {_format_speaker_label(t['speaker_label'])}: \"{t['text']}\""
            for t in speaker_turns
        ]
        return header + "\n" + "\n".join(body_lines)
    return f"{header}\n{chunk.body}"


def _format_speaker_label(label: object) -> str:
    """Render stored diarization labels in the prompt's SPEAKER_* form.

    AssemblyAI stores bare labels such as ``A`` and ``B`` while older
    pyannote-backed prompt examples use ``SPEAKER_NN``.  Normalizing only at
    this formatter boundary keeps stored evidence labels untouched while
    making every diarized chunk use one coherent prompt vocabulary.
    """

    normalized = str(label or "UNKNOWN").strip() or "UNKNOWN"
    if normalized.startswith("SPEAKER_") or normalized in {"OVERLAP", "UNKNOWN"}:
        return normalized
    return f"SPEAKER_{normalized}"


def build_extraction_prompt(
    *,
    extraction_instructions: str,
    symbols_block: str,
    canonical_roster: str,
    chunks: list,
    meeting_id: int,
    batch_index: int,
    batch_total: int,
    cluster_roster_block: str = "",
) -> str:
    """Compose one batch's full Sonnet prompt: symbols + roster + cluster
    mapping (when diarized) + chunks + extraction instructions (in that
    order so Sonnet sees the ground-truth references BEFORE the chunks
    they apply to).

    Phase 2 D5: when the meeting was diarized AND the cluster→roster
    mapper (D6) produced confirmed mappings, the caller injects
    `cluster_roster_block` (a CLUSTER_ROSTER section). When empty,
    Sonnet falls back to proximity-inference against the canonical_roster.
    """
    chunks_block = "\n\n".join(_format_chunk_for_extraction(c) for c in chunks)
    blocks: list[str] = [symbols_block, canonical_roster]
    if cluster_roster_block:
        blocks.append(cluster_roster_block)
    blocks.append(
        f"CHUNKS — batch {batch_index + 1} of {batch_total} for meeting_id={meeting_id}:\n"
        f"---\n"
        f"{chunks_block}\n"
        f"---"
    )
    blocks.append(extraction_instructions)
    return "\n\n".join(blocks)


def _strip_json_fence(text: str) -> str:
    """Sonnet occasionally wraps JSON output in a ```json ``` fence despite
    the prompt's "no markdown code fence" instruction. Tolerate both."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def extract_quotes_from_batch(
    *,
    extraction_instructions: str,
    symbols_block: str,
    canonical_roster: str,
    chunks: list,
    meeting_id: int,
    batch_index: int,
    batch_total: int,
    cluster_roster_block: str = "",
    timeout_seconds: float = 180.0,
) -> tuple[list[ExtractedQuote], qdrant_synthesizer.GenerationResult]:
    """Generate one chronological batch and fail closed on invalid shape."""
    prompt = build_extraction_prompt(
        extraction_instructions=extraction_instructions,
        symbols_block=symbols_block,
        canonical_roster=canonical_roster,
        chunks=chunks,
        meeting_id=meeting_id,
        batch_index=batch_index,
        batch_total=batch_total,
        cluster_roster_block=cluster_roster_block,
    )
    generation = qdrant_synthesizer.generate_with_fallback(
        prompt,
        timeout_seconds=timeout_seconds,
    )
    raw = _strip_json_fence(generation.content)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"quote extraction JSON invalid meeting={meeting_id} "
            f"batch={batch_index}: {exc}"
        ) from exc

    quotes = parsed.get("quotes")
    if not isinstance(quotes, list):
        raise ValueError(
            "quote extraction response has no quotes list "
            f"meeting={meeting_id} batch={batch_index}"
        )

    out: list[ExtractedQuote] = []
    for q in quotes:
        if not isinstance(q, dict):
            continue
        try:
            eq = ExtractedQuote.from_dict(q)
        except Exception as exc:
            logger.warning("Skipping malformed quote dict: %s", exc)
            continue
        if not eq.is_valid():
            logger.debug("Skipping empty quote: %s", eq)
            continue
        out.append(eq)
    return out, generation


def extract_quotes_for_meeting(
    *,
    meeting_id: int,
    city_name: str,
    symbols_block: Optional[str] = None,
    canonical_roster: Optional[str] = None,
    cluster_roster_block: str = "",
    chunks_per_batch: int = DEFAULT_CHUNKS_PER_BATCH,
    per_batch_timeout: float = DEFAULT_PER_BATCH_TIMEOUT,
    generation_results: Optional[
        list[qdrant_synthesizer.GenerationResult]
    ] = None,
) -> list[ExtractedQuote]:
    """Load every chunk, batch in order, and return attributed quotes.

    The caller (D4 persistence + D5 worker) handles DB writes; this
    function is import-side-effect-free and idempotent against the same
    Qdrant state.
    """
    # Build canonical references if caller didn't provide them (D5 worker
    # may pre-build these once for a meeting that gets multiple extraction
    # invocations and avoid the DB hit per call).
    if symbols_block is None:
        symbols_block = symbols.build_symbols_block(city_name) or ""

    if canonical_roster is None:
        # Same shim pattern the bridge uses — make parsers/ importable from
        # the bridge package without requiring an editable install.
        import sys
        bridge_root = _THIS_DIR.parent
        parsers_path = bridge_root / "council_navigator" / "parsers"
        if str(parsers_path) not in sys.path:
            sys.path.insert(0, str(parsers_path))
        try:
            from database import get_council_members
            members = get_council_members(city_name) or []
        except Exception as exc:
            logger.warning(
                "extract_quotes_for_meeting: get_council_members(%s) failed: %s",
                city_name, exc,
            )
            members = []
        canonical_roster = _format_canonical_roster(members)

    extraction_instructions = _load_extraction_prompt()

    chunks = qdrant_synthesizer.load_complete_meeting_chunks(meeting_id)

    # Sort by chunk_index so each batch covers a contiguous slice of the
    # meeting — preserves narrative continuity within a batch so Sonnet can
    # resolve cross-chunk references (e.g., a speaker introduced in chunk 5
    # and continuing in chunk 6 sits in the same batch).
    chunks.sort(key=lambda c: c.chunk_index)

    batches = [
        chunks[i : i + chunks_per_batch]
        for i in range(0, len(chunks), chunks_per_batch)
    ]
    batch_total = len(batches)
    logger.info(
        "extract_quotes_for_meeting: meeting=%d chunks=%d batches=%d "
        "(~%d/batch)",
        meeting_id, len(chunks), batch_total, chunks_per_batch,
    )

    all_quotes: list[ExtractedQuote] = []
    for i, batch in enumerate(batches):
        quotes, generation = extract_quotes_from_batch(
            extraction_instructions=extraction_instructions,
            symbols_block=symbols_block,
            canonical_roster=canonical_roster,
            chunks=batch,
            meeting_id=meeting_id,
            batch_index=i,
            batch_total=batch_total,
            cluster_roster_block=cluster_roster_block,
            timeout_seconds=per_batch_timeout,
        )
        if generation_results is not None:
            generation_results.append(generation)
        all_quotes.extend(quotes)
        logger.info(
            "  batch %d/%d → %d quotes (running total %d)",
            i + 1, batch_total, len(quotes), len(all_quotes),
        )

    logger.info(
        "extract_quotes_for_meeting: meeting=%d done — %d quotes extracted",
        meeting_id, len(all_quotes),
    )
    return all_quotes


# ── D4: persistence adapter (extract → save_quotes_batch → align_word_timings) ──


# Speaker-class enum mapping. The D2 prompt emits {council_member, staff,
# expert}; the canonical quotes table accepts {council_member, staff,
# external} (per save_quotes_batch's defensive validation). Map at the
# adapter boundary so the prompt stays human-readable while the DB row
# stays schema-clean.
_SPEAKER_CLASS_MAP = {
    "council_member": "council_member",
    "staff": "staff",
    "expert": "external",
    "external": "external",
}


def _to_save_quotes_item(eq: ExtractedQuote) -> dict:
    """Adapt one ExtractedQuote to the dict shape save_quotes_batch expects.

    Maps speaker_class enum, lifts video_timestamp_seconds into the field
    save_quotes_batch reads (`approximate_timestamp_seconds`), and stashes
    chunk_index in `context` so operator review can trace each quote back
    to its source chunk without a new schema column."""
    cls = (eq.speaker_class or "council_member").strip().lower()
    cls = _SPEAKER_CLASS_MAP.get(cls, "council_member")
    return {
        "speaker_name": eq.speaker_name,
        "speaker_role": eq.speaker_role or None,
        "speaker_class": cls,
        "quote_text": eq.quote_text,
        "topic_tags": eq.topic_tags,
        "approximate_timestamp_seconds": eq.video_timestamp_seconds or None,
        "context": f"v1-rag-3 chunk_index={eq.chunk_index}",
    }


def persist_extracted_quotes(
    *,
    meeting_id: int,
    city_name: str,
    quotes: list[ExtractedQuote],
    align_word_timings: bool = True,
) -> dict:
    """Persist a list of extracted quotes to the canonical `quotes` table.

    Adapts ExtractedQuote → save_quotes_batch dict shape, calls the existing
    idempotent UPSERT (which already resolves speaker_name → member_id via
    `_lookup_member_id_via_cursor` + preserves verification state on
    conflict), then optionally fires the word-timings alignment pass over
    the meeting's transcript so karaoke renders work for the new rows.

    Returns a combined stats dict:
        {
          "saved": int,            # new INSERTs
          "updated": int,          # idempotent UPSERTs (verification preserved)
          "skipped_invalid": int,  # malformed dicts
          "member_lookup_misses": int,  # speaker_name didn't resolve to roster
          "alignment": <align_quotes_for_meeting stats or None>,
        }
    """
    # Same parsers/ import shim qdrant_synthesizer + the extractor itself
    # use — makes `from database import ...` work whether the caller runs
    # from the parsers dir or from the bridge dir.
    import sys
    bridge_root = _THIS_DIR.parent
    parsers_path = bridge_root / "council_navigator" / "parsers"
    if str(parsers_path) not in sys.path:
        sys.path.insert(0, str(parsers_path))

    if not quotes:
        return {
            "saved": 0,
            "updated": 0,
            "skipped_invalid": 0,
            "member_lookup_misses": 0,
            "alignment": None,
        }

    items = [_to_save_quotes_item(eq) for eq in quotes if eq.is_valid()]
    from database import save_quotes_batch
    save_stats = save_quotes_batch(
        meeting_id=meeting_id,
        items=items,
        broadcast_hero_ordinals=None,  # hero curation deferred for V1-preview
        city_name=city_name,
    )
    logger.info(
        "persist_extracted_quotes: meeting=%d city=%s "
        "saved=%d updated=%d skipped=%d misses=%d",
        meeting_id, city_name,
        save_stats.get("saved", 0),
        save_stats.get("updated", 0),
        save_stats.get("skipped_invalid", 0),
        save_stats.get("member_lookup_misses", 0),
    )

    alignment_stats = None
    if align_word_timings:
        try:
            from quote_align import align_quotes_for_meeting
            alignment_stats = align_quotes_for_meeting(meeting_id)
            logger.info(
                "  alignment: %s", alignment_stats,
            )
        except Exception as exc:
            logger.warning(
                "persist_extracted_quotes: align_quotes_for_meeting failed "
                "for meeting=%d: %s",
                meeting_id, exc,
            )

    return {**save_stats, "alignment": alignment_stats}


def extract_and_persist(
    *,
    meeting_id: int,
    city_name: str,
    chunks_per_batch: int = DEFAULT_CHUNKS_PER_BATCH,
    align_word_timings: bool = True,
) -> dict:
    """End-to-end convenience: retrieve → extract → persist → align.

    D5 worker integration calls this from the qdrant_extract_quotes
    strategy on every V1-mode meeting pass. Returns the persist_extracted_quotes
    stats dict plus the raw extracted count.
    """
    generation_results: list[qdrant_synthesizer.GenerationResult] = []
    extracted = extract_quotes_for_meeting(
        meeting_id=meeting_id,
        city_name=city_name,
        chunks_per_batch=chunks_per_batch,
        generation_results=generation_results,
    )
    stats = persist_extracted_quotes(
        meeting_id=meeting_id,
        city_name=city_name,
        quotes=extracted,
        align_word_timings=align_word_timings,
    )
    stats["extracted_count"] = len(extracted)
    stats["evidence_mode"] = "complete_transcript"
    stats["model_ids"] = [result.model_id for result in generation_results]
    stats["generation_attempts"] = [
        [attempt.as_dict() for attempt in result.attempts]
        for result in generation_results
    ]
    return stats

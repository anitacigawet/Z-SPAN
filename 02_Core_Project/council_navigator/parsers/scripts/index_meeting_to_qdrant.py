"""
V1-RAG-2 — per-meeting Qdrant indexing pipeline.

Reads a meeting's `transcript_words` cache from the Mac's Flask API (the same
`/api/notebook/<meeting_id>` endpoint the broadcast page consumes), chunks
the transcript by sentence-transformer-tokenized windows, embeds each chunk
via BGE-small-en-v1.5 (the 384-dim ARM-friendly model pinned in V1-RAG-1),
and upserts the chunks into the Qdrant `zspan_meetings` collection with
deterministic point IDs derived from `(meeting_id, chunk_index)` — so
re-indexing a meeting overwrites the existing chunks in place and external
citations to chunk IDs stay valid across re-index operations.

Designed to run on the Surface Pro substrate per [D-126](../../01_Project_Overview/DECISIONS.md)
and [S-033](../../01_Project_Overview/FUTURE_THOUGHTS.md); the embedding
model + qdrant-client live in `zspan-embed-venv` on the Surface Pro. The
script is host-agnostic for the transcript SOURCE — set
`ZSPAN_TRANSCRIPT_SOURCE_HOST=<mac-lan-ip>` (Mac LAN IP) when running on
Surface Pro, or leave it as `localhost` for in-Mac smoke testing.

Idempotency: deterministic point IDs (UUID5 from a project-namespace + the
"meeting_id:chunk_index" string) mean re-running this script for the same
meeting overwrites the existing chunks; no orphaned points accumulate.

CLI usage examples:

    # Single meeting (Bullhead 5/19 example)
    python index_meeting_to_qdrant.py --meeting-id 103225

    # All Mohave past-2-weeks meetings (V1-Mohave-1 batch ingestion)
    python index_meeting_to_qdrant.py --all-mohave

    # Override hosts (Surface-Pro-to-Mac cross-machine)
    ZSPAN_TRANSCRIPT_SOURCE_HOST=<mac-lan-ip> \\
        python index_meeting_to_qdrant.py --meeting-id 103225

    # Dry-run (chunk + log but do NOT upsert)
    python index_meeting_to_qdrant.py --meeting-id 103225 --dry-run

Composes:
- [D-126](../../01_Project_Overview/DECISIONS.md) V1 flag-pole
  architecture (Qdrant + Sonnet RAG per D-126).
- [S-033](../../01_Project_Overview/FUTURE_THOUGHTS.md) Option C
  self-hosted RAG migration — this script is the indexing-pipeline
  realization on Surface Pro.
- [V1_RAG1_SURFACE_PRO_HANDOFF.md](../../01_Project_Overview/V1_RAG1_SURFACE_PRO_HANDOFF.md)
  — the Surface Pro Qdrant + embedding model install this script
  depends on.

V1-RAG-3 (the next chunk) consumes the indexed corpus this script produces
to swap fetcher.py's text-output handlers to
Qdrant retrieve + Sonnet synthesize-with-karaoke-citations.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import requests

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────

# V1-RAG-1 pinned BGE-small-en-v1.5 as the ARM-friendly default. This is a
# LOAD-BEARING constant: the Qdrant collection's vectors_config.size is
# hard-coded to this value at collection-create time, and any later attempt
# to upsert a different-dimension vector will fail loudly. Changing the
# embedding model requires re-indexing every meeting AND dropping the
# collection (or migrating to a new collection name). See D-126 + the
# V1-RAG-1 handoff doc.
VECTOR_DIM = 384
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Standard RAG chunking defaults. 400 tokens is well under BGE-small's 512
# max sequence length, leaving headroom for the model's internal special
# tokens. The 50-token overlap stitches semantic continuity across chunk
# boundaries (e.g., a sentence that spans the boundary still appears in
# both adjacent chunks).
CHUNK_TOKEN_TARGET = 400
CHUNK_TOKEN_OVERLAP = 50

# Qdrant collection name. ASCII-only, lowercase, snake_case for Qdrant
# compatibility. Project-scoped to make accidental cross-project pollution
# obvious if Qdrant is ever shared.
QDRANT_COLLECTION = "zspan_meetings"

# UUID5 namespace for deterministic point IDs. Generated once with
# uuid.uuid4() and pinned here so the SAME (meeting_id, chunk_index) always
# produces the SAME UUID across script runs + machines. Don't change this
# constant — doing so would orphan every existing chunk.
ZSPAN_QDRANT_NAMESPACE = uuid.UUID("3a76e8c5-2c40-4f1d-b3ad-c1b9e9d8b2a4")

# V1 Mohave-county past-2-weeks scope. V1-Mohave-1 batch ingestion targets
# these four cities specifically; everything outside this set is V1
# "Coming soon" scaffold per D-124.
V1_MOHAVE_CITIES = ("Kingman", "Bullhead City", "Lake Havasu City", "Colorado City")

# Default config from env; CLI flags override.
DEFAULT_QDRANT_HOST = os.environ.get("ZSPAN_QDRANT_HOST", "localhost")
DEFAULT_QDRANT_PORT = int(os.environ.get("ZSPAN_QDRANT_PORT", "6333"))
DEFAULT_TRANSCRIPT_SOURCE_HOST = os.environ.get(
    "ZSPAN_TRANSCRIPT_SOURCE_HOST", "localhost"
)
DEFAULT_TRANSCRIPT_SOURCE_PORT = int(
    os.environ.get("ZSPAN_TRANSCRIPT_SOURCE_PORT", "5001")
)


# ── Data shapes ────────────────────────────────────────────────────────


@dataclass
class WordEntry:
    """One Whisper-emitted word with timing. Matches the existing cache shape.

    Phase 2 D4 (2026-06-24): optional `speaker_id` populated when the meeting
    was transcribed AND diarized (D7 worker integration). Pre-diarization
    meetings have speaker_id=None; the payload then omits speaker_turns.
    """

    word: str
    start: float
    end: float
    speaker_id: Optional[str] = None


@dataclass
class MeetingChunk:
    """One indexable chunk of a meeting transcript.

    Phase 2 D4: `speaker_turns` populated when transcript_words carries
    speaker_id per word. Each turn run is `{speaker_label, start, end, text}`
    representing one contiguous run within this chunk's time window.
    """

    meeting_id: int
    chunk_index: int
    body: str
    start_seconds: float
    end_seconds: float
    city: str
    county: str
    state: str  # e.g., "AZ"
    speaker_turns: Optional[list[dict[str, Any]]] = None

    def point_id(self) -> str:
        """Deterministic UUID5 from (meeting_id, chunk_index) — see ZSPAN_QDRANT_NAMESPACE."""
        return str(uuid.uuid5(ZSPAN_QDRANT_NAMESPACE, f"{self.meeting_id}:{self.chunk_index}"))

    def payload(self) -> dict[str, Any]:
        """Payload stored alongside the vector in Qdrant. Filter-indexed fields
        (state / county / city / meeting_id) are also indexed at collection-
        create time for fast filtered retrieval.

        Phase 2 D4: emits `speaker_turns` only when the chunk has them — keeps
        pre-diarization meetings' payloads unchanged.
        """
        payload: dict[str, Any] = {
            "state": self.state,
            "county": self.county,
            "city": self.city,
            "meeting_id": self.meeting_id,
            "chunk_index": self.chunk_index,
            "body": self.body,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
        }
        if self.speaker_turns:
            payload["speaker_turns"] = self.speaker_turns
        return payload


# ── Transcript fetch ───────────────────────────────────────────────────


def fetch_meeting_outputs(
    meeting_id: int, source_host: str, source_port: int
) -> dict[str, Any]:
    """Pull the cached outputs for a meeting from the Mac-side Flask API.

    Returns the parsed JSON from `/api/notebook/<meeting_id>` including the
    top-level meeting metadata (city, county, meeting_title, etc.) AND the
    nested outputs dict (which contains `transcript_words.content` as a
    JSON-encoded string).

    Raises requests.HTTPError on non-2xx; returns None-equivalent (empty
    dict) on successful response that the API marked unsuccessful — the
    caller decides what to do with that.
    """
    url = f"http://{source_host}:{source_port}/api/notebook/{meeting_id}"
    logger.info("Fetching meeting outputs from %s", url)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True):
        logger.warning("Source API returned success=false for meeting=%s", meeting_id)
        return {}
    return data


def parse_transcript_words(outputs: dict[str, Any]) -> tuple[list[WordEntry], float]:
    """Extract the words list from outputs.transcript_words.content.

    Returns (words, duration_seconds). Raises ValueError if the transcript
    is missing or empty — the caller decides whether to skip the meeting
    or fail.
    """
    raw = outputs.get("transcript_words", {}).get("content")
    if not raw:
        raise ValueError("transcript_words output is missing or empty")
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    words_data = parsed.get("words") if isinstance(parsed, dict) else None
    if not words_data or not isinstance(words_data, list):
        raise ValueError("transcript_words content has no 'words' list")
    words = [
        WordEntry(
            word=w["word"],
            start=float(w["start"]),
            end=float(w["end"]),
            speaker_id=w.get("speaker_id"),
        )
        for w in words_data
        if isinstance(w, dict) and "word" in w and "start" in w and "end" in w
    ]
    duration = float(parsed.get("duration_seconds", 0.0))
    return words, duration


def _collapse_speaker_turns_for_chunk(chunk_words: list[WordEntry]) -> Optional[list[dict[str, Any]]]:
    """Collapse a chunk's worth of words into per-turn speaker runs.

    Returns None when no word in the chunk carries a speaker_id (pre-
    diarization meeting). Otherwise returns a compact list of
    `{speaker_label, start, end, text}` runs — one entry per contiguous
    same-speaker stretch within the chunk's time window. Used by D5 to
    render `SPEAKER_03: "..."` blocks in the extractor prompt.
    """
    if not any(w.speaker_id for w in chunk_words):
        return None

    runs: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for w in chunk_words:
        spk = w.speaker_id or "UNKNOWN"
        text = (w.word or "").strip()
        if not text:
            continue
        if current is None or current["speaker_label"] != spk:
            if current is not None:
                runs.append(current)
            current = {
                "speaker_label": spk,
                "start": round(w.start, 3),
                "end": round(w.end, 3),
                "text": text,
            }
        else:
            current["end"] = round(w.end, 3)
            current["text"] = f"{current['text']} {text}"
    if current is not None:
        runs.append(current)
    return runs


# ── Chunking ───────────────────────────────────────────────────────────


def chunk_transcript(
    words: list[WordEntry],
    *,
    meeting_id: int,
    city: str,
    county: str,
    state: str,
    tokenizer: Any,
    target_tokens: int = CHUNK_TOKEN_TARGET,
    overlap_tokens: int = CHUNK_TOKEN_OVERLAP,
) -> Iterator[MeetingChunk]:
    """Chunk word-level transcripts into token-bounded windows with overlap.

    Uses the embedding model's tokenizer to count tokens accurately rather
    than estimating from word count — that way we know each chunk fits
    within the model's max_seq_length and we don't lose content to
    truncation. The overlap stitches semantic continuity across chunk
    boundaries.

    Yields MeetingChunk objects with chunk_index 0, 1, 2, ... in order.
    start_seconds + end_seconds derive from the first/last word in each
    chunk, so karaoke-style timecode citations from the V1-RAG-3 RAG
    layer link directly back to the playable portion of the source video.
    """
    if not words:
        return

    # Walk forward through words, accumulating into the current chunk until
    # the token count crosses target_tokens. Tokenizing the running text on
    # every word is expensive for a 20k-word transcript; we batch-tokenize
    # in N-word increments and refine at the boundary.
    chunk_index = 0
    cursor = 0
    total_words = len(words)

    while cursor < total_words:
        # Greedy-grow the chunk until tokens hit target. Start with an
        # estimate (~1.3 words/token for English transcripts) and refine.
        est_words = int(target_tokens * 1.4)
        end_cursor = min(cursor + est_words, total_words)

        # Refine: if the estimate undershoots, grow until we hit target;
        # if it overshoots, shrink until we're at or below target.
        has_shrunk = False
        while True:
            text = " ".join(w.word for w in words[cursor:end_cursor])
            token_count = len(tokenizer.encode(text, add_special_tokens=False))
            if token_count <= target_tokens or end_cursor >= total_words:
                # Try growing by a small step if we have headroom. But never
                # grow back AFTER shrinking in this iteration — the shrink
                # step (~10% of span, floored at 5) and the grow step (30)
                # can coincidentally cancel, producing a stable two-state
                # oscillator. Real-world trigger: m103224 cursor=4692 spun
                # between end_cursor=4992 (405 tokens, shrink-eligible)
                # and end_cursor=4962 (359 tokens, grow-eligible) forever.
                if (
                    not has_shrunk
                    and token_count < target_tokens - 30
                    and end_cursor < total_words
                ):
                    end_cursor = min(end_cursor + 30, total_words)
                    continue
                break
            # Shrink by ~10% of current span.
            shrink_by = max(5, (end_cursor - cursor) // 10)
            new_end_cursor = max(cursor + 1, end_cursor - shrink_by)
            if new_end_cursor == end_cursor:
                # Edge case (infinite-loop fix 2026-06-20): we tried to
                # shrink but `max(cursor + 1, ...)` clamped us to the same
                # end_cursor we already had. That means we're at the
                # minimum 1-word chunk AND that single word tokenizes to
                # more than `target_tokens` tokens (Whisper run-on, long
                # URL, pathological unicode, etc.). Accept the oversized
                # chunk and emit it rather than spinning forever calling
                # tokenizer.encode on the same string. Log the violator so
                # operators can see which meetings + words triggered it.
                logger.warning(
                    "Indexer chunking accepted oversized single-word "
                    "chunk for meeting=%d at cursor=%d (token_count=%d > "
                    "target=%d). Word: %r",
                    meeting_id, cursor, token_count, target_tokens,
                    words[cursor].word[:120],
                )
                break
            end_cursor = new_end_cursor
            has_shrunk = True

        chunk_words = words[cursor:end_cursor]
        body = " ".join(w.word for w in chunk_words).strip()
        if not body:
            cursor = end_cursor
            continue

        yield MeetingChunk(
            meeting_id=meeting_id,
            chunk_index=chunk_index,
            body=body,
            start_seconds=chunk_words[0].start,
            end_seconds=chunk_words[-1].end,
            city=city,
            county=county,
            state=state,
            speaker_turns=_collapse_speaker_turns_for_chunk(chunk_words),
        )
        chunk_index += 1

        # Advance cursor; back up by overlap_tokens worth of words for the
        # next chunk's prefix (semantic stitching across boundaries).
        if end_cursor >= total_words:
            break
        overlap_words = int(overlap_tokens * 1.4)
        next_cursor = max(cursor + 1, end_cursor - overlap_words)
        cursor = next_cursor


# ── Qdrant operations ──────────────────────────────────────────────────


def ensure_collection(client: Any) -> None:
    """Create the zspan_meetings collection if it doesn't exist.

    Idempotent: if the collection already exists with the right shape,
    this is a no-op. Adds payload indexes on state / county / city /
    meeting_id so filtered retrieval (e.g., "find chunks from this
    specific meeting") is fast even at national scale.
    """
    from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

    existing = {c.name for c in client.get_collections().collections}
    if QDRANT_COLLECTION in existing:
        # Verify the existing collection has the right vector dimension —
        # if a prior run created it with a different embedding model, we
        # need to fail loud rather than silently mix dimensions.
        info = client.get_collection(QDRANT_COLLECTION)
        existing_size = info.config.params.vectors.size
        if existing_size != VECTOR_DIM:
            raise RuntimeError(
                f"Collection {QDRANT_COLLECTION!r} exists with vector dim "
                f"{existing_size}, but this script's VECTOR_DIM is {VECTOR_DIM}. "
                f"Drop the collection (`client.delete_collection({QDRANT_COLLECTION!r})`) "
                f"and re-run, OR pin the embedding model to one matching the "
                f"existing dimension. Cross-dimension upserts will fail."
            )
        logger.info(
            "Collection %s already exists with correct dim %d",
            QDRANT_COLLECTION,
            VECTOR_DIM,
        )
        return

    logger.info(
        "Creating collection %s with vector dim %d (Cosine distance)",
        QDRANT_COLLECTION,
        VECTOR_DIM,
    )
    client.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )

    # Payload indexes for filtered retrieval. Without these, payload
    # filters do a full scan; with them, queries like "find chunks where
    # meeting_id=103225" land in O(log n) on the filter axis.
    for field, schema in [
        ("state", PayloadSchemaType.KEYWORD),
        ("county", PayloadSchemaType.KEYWORD),
        ("city", PayloadSchemaType.KEYWORD),
        ("meeting_id", PayloadSchemaType.INTEGER),
    ]:
        client.create_payload_index(
            collection_name=QDRANT_COLLECTION,
            field_name=field,
            field_schema=schema,
        )
        logger.info("Created payload index on %s (%s)", field, schema)


def upsert_chunks(client: Any, chunks: list[MeetingChunk], vectors: list[list[float]]) -> None:
    """Upsert a list of chunks (with their pre-computed vectors) into Qdrant.

    Deterministic point IDs (UUID5 from meeting_id:chunk_index) mean this is
    a true upsert — re-running indexing for the same meeting overwrites the
    existing points in place rather than accumulating orphans.
    """
    from qdrant_client.models import PointStruct

    if not chunks:
        logger.info("No chunks to upsert (empty input)")
        return
    if len(chunks) != len(vectors):
        raise ValueError(
            f"chunks ({len(chunks)}) and vectors ({len(vectors)}) length mismatch"
        )

    points = [
        PointStruct(id=c.point_id(), vector=v, payload=c.payload())
        for c, v in zip(chunks, vectors)
    ]
    client.upsert(collection_name=QDRANT_COLLECTION, points=points)
    logger.info("Upserted %d chunks into %s", len(points), QDRANT_COLLECTION)


# ── Index orchestration ────────────────────────────────────────────────


def index_meeting(
    meeting_id: int,
    *,
    source_host: str,
    source_port: int,
    qdrant_client: Any,
    embed_model: Any,
    state_fallback: str = "AZ",
    dry_run: bool = False,
) -> int:
    """Top-level index one meeting end-to-end.

    Returns the number of chunks indexed. Raises on hard errors (transcript
    missing, embedding dimension mismatch); logs and returns 0 on soft
    skips (meeting outputs empty, no words to chunk).
    """
    logger.info("─" * 60)
    logger.info("Indexing meeting %d", meeting_id)
    outputs_response = fetch_meeting_outputs(meeting_id, source_host, source_port)
    if not outputs_response:
        logger.warning("Empty response for meeting %d; skipping", meeting_id)
        return 0

    outputs = outputs_response.get("outputs", {})
    city = outputs_response.get("city") or "Unknown"
    county = outputs_response.get("county") or "Unknown"
    # State isn't in /api/notebook today; default to AZ for V1 Mohave scope.
    # V2 contributors-from-other-states will need to thread state through.
    state = state_fallback

    try:
        words, duration = parse_transcript_words(outputs)
    except ValueError as e:
        logger.warning(
            "Meeting %d has no usable transcript_words (%s); skipping. "
            "(For V1 this is expected for meetings without an ingested "
            "video — Colorado City no_video_source WOs etc.)",
            meeting_id,
            e,
        )
        return 0

    logger.info(
        "Meeting %d transcript: %d words, %.1f minutes audio (%s · %s · %s)",
        meeting_id,
        len(words),
        duration / 60,
        city,
        county,
        state,
    )

    tokenizer = embed_model.tokenizer
    chunks = list(
        chunk_transcript(
            words,
            meeting_id=meeting_id,
            city=city,
            county=county,
            state=state,
            tokenizer=tokenizer,
        )
    )
    logger.info("Produced %d chunks (target=%d tokens, overlap=%d tokens)",
                len(chunks), CHUNK_TOKEN_TARGET, CHUNK_TOKEN_OVERLAP)

    if not chunks:
        logger.warning("Meeting %d produced 0 chunks; skipping upsert", meeting_id)
        return 0

    # Embed in one batch (sentence-transformers handles batching internally).
    bodies = [c.body for c in chunks]
    logger.info("Embedding %d chunks via %s ...", len(bodies), EMBED_MODEL_NAME)
    vectors = embed_model.encode(
        bodies, normalize_embeddings=True, show_progress_bar=False
    )
    # Defensive: hard assertion on dimension. Catches the case where the
    # loaded model has a different dim than the collection (e.g., someone
    # accidentally swapped the model constant + collection survived from
    # a prior run with the old dim).
    actual_dim = vectors.shape[1] if hasattr(vectors, "shape") else len(vectors[0])
    if actual_dim != VECTOR_DIM:
        raise RuntimeError(
            f"Embedding model emitted dim {actual_dim} but VECTOR_DIM is "
            f"{VECTOR_DIM}. Either the model constant changed without the "
            f"VECTOR_DIM constant being updated, or the loaded model isn't "
            f"the one we think it is. Aborting before any upsert."
        )

    vectors_list = [v.tolist() for v in vectors]

    if dry_run:
        logger.info("DRY RUN: would upsert %d chunks; first chunk preview:", len(chunks))
        first = chunks[0]
        logger.info("  id=%s start=%.1fs end=%.1fs body=%r",
                    first.point_id(),
                    first.start_seconds,
                    first.end_seconds,
                    first.body[:120] + "..." if len(first.body) > 120 else first.body)
        return len(chunks)

    upsert_chunks(qdrant_client, chunks, vectors_list)
    return len(chunks)


def list_v1_mohave_meeting_ids(source_host: str, source_port: int) -> list[int]:
    """Enumerate the V1-Mohave-1 batch ingestion target meetings.

    Returns the meeting_ids of all past-2-weeks meetings across the four
    V1 Mohave cities (Kingman / Bullhead City / Lake Havasu City /
    Colorado City). Uses the channels-tree endpoint to find each city's
    cached meetings.
    """
    url = f"http://{source_host}:{source_port}/api/channels/tree"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    tree = response.json()

    meeting_ids: list[int] = []
    # Channels tree structure: tree.states → counties → cities → meetings
    for state_entry in tree.get("states", []):
        for county_entry in state_entry.get("counties", []):
            for city_entry in county_entry.get("cities", []):
                if city_entry.get("name") not in V1_MOHAVE_CITIES:
                    continue
                for meeting in city_entry.get("meetings", []):
                    mid = meeting.get("meeting_id")
                    if isinstance(mid, int):
                        meeting_ids.append(mid)

    logger.info(
        "Found %d V1-Mohave meetings to index across %s",
        len(meeting_ids),
        ", ".join(V1_MOHAVE_CITIES),
    )
    return meeting_ids


# ── CLI ────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Index a Z-SPAN meeting's transcript into Qdrant (V1-RAG-2).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--meeting-id",
        type=int,
        help="Index a single meeting by ID (e.g., 103225 for Bullhead 5/19).",
    )
    parser.add_argument(
        "--all-mohave",
        action="store_true",
        help="Index every past-2-weeks meeting across the 4 V1 Mohave cities.",
    )
    parser.add_argument(
        "--source-host",
        default=DEFAULT_TRANSCRIPT_SOURCE_HOST,
        help=f"Mac-side Flask host (default: {DEFAULT_TRANSCRIPT_SOURCE_HOST}; "
        f"set ZSPAN_TRANSCRIPT_SOURCE_HOST=<mac-lan-ip> on Surface Pro).",
    )
    parser.add_argument(
        "--source-port",
        type=int,
        default=DEFAULT_TRANSCRIPT_SOURCE_PORT,
        help=f"Mac-side Flask port (default: {DEFAULT_TRANSCRIPT_SOURCE_PORT}).",
    )
    parser.add_argument(
        "--qdrant-host",
        default=DEFAULT_QDRANT_HOST,
        help=f"Qdrant host (default: {DEFAULT_QDRANT_HOST}; usually localhost "
        f"on the Surface Pro substrate per D-126 / V1-RAG-1).",
    )
    parser.add_argument(
        "--qdrant-port",
        type=int,
        default=DEFAULT_QDRANT_PORT,
        help=f"Qdrant REST port (default: {DEFAULT_QDRANT_PORT}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + chunk + embed but do NOT upsert. Useful for smoke "
        "testing the pipeline end-to-end against a meeting without "
        "polluting the collection.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG-level logging.",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.meeting_id and not args.all_mohave:
        parser.error("Provide either --meeting-id <int> or --all-mohave.")

    # Heavy imports gated behind arg parsing — quick --help responses.
    from qdrant_client import QdrantClient
    from sentence_transformers import SentenceTransformer

    logger.info("Loading embedding model %s ...", EMBED_MODEL_NAME)
    embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    loaded_dim = embed_model.get_sentence_embedding_dimension()
    if loaded_dim != VECTOR_DIM:
        logger.error(
            "Loaded model %s has embedding dim %d, but VECTOR_DIM constant is "
            "%d. Either the model constant in this script needs updating OR "
            "you're loading a different model than expected. Aborting.",
            EMBED_MODEL_NAME,
            loaded_dim,
            VECTOR_DIM,
        )
        return 2
    logger.info(
        "Loaded %s (dim=%d, max_seq_len=%d)",
        EMBED_MODEL_NAME,
        loaded_dim,
        embed_model.max_seq_length,
    )

    logger.info("Connecting to Qdrant at %s:%d", args.qdrant_host, args.qdrant_port)
    qdrant_client = QdrantClient(host=args.qdrant_host, port=args.qdrant_port)
    ensure_collection(qdrant_client)

    if args.all_mohave:
        meeting_ids = list_v1_mohave_meeting_ids(args.source_host, args.source_port)
    else:
        meeting_ids = [args.meeting_id]

    total_chunks = 0
    failed_meetings: list[tuple[int, str]] = []
    for mid in meeting_ids:
        try:
            n = index_meeting(
                mid,
                source_host=args.source_host,
                source_port=args.source_port,
                qdrant_client=qdrant_client,
                embed_model=embed_model,
                dry_run=args.dry_run,
            )
            total_chunks += n
        except Exception as exc:  # pragma: no cover — defensive logging only
            logger.exception("Indexing failed for meeting %d", mid)
            failed_meetings.append((mid, str(exc)))

    logger.info("─" * 60)
    logger.info("Index run complete: %d chunks across %d meeting(s)",
                total_chunks, len(meeting_ids) - len(failed_meetings))
    if failed_meetings:
        logger.warning("Failed meetings:")
        for mid, err in failed_meetings:
            logger.warning("  %d → %s", mid, err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

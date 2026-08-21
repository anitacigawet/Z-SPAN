"""Operator-only cross-meeting search helpers (V1.5-OperatorSearch-1).

Lives alongside rag_search.py and qdrant_synthesizer.py as the V1.5+
retrieval-and-synthesis infrastructure. The intent-interpret step uses
the same `claude -p` Sonnet subprocess pattern as the V1-RAG-3 text
output synthesis (the MAX-cap-absorbed Mac-side path).

Per the V1.5-OperatorSearch-1 spec (handoff 2026-06-25):
- Phase 1 intent-interpret = Sonnet via `claude -p` (DP-3 strict: never BYOK-swapped).
- Phase 3 cross-meeting synthesis = Sonnet via `claude -p` for the
  test-loop, swappable to BYOK relay later.

Both phases use prompts assembled here so the prompt versions are
auditable in a single file.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional


INTERPRET_PROMPT_VERSION = "v1.5-operator-search-interpret-2026-06-25"
SYNTHESIS_PROMPT_VERSION = "v1.5-operator-search-synthesis-2026-06-25"

logger = logging.getLogger(__name__)

# Strip ```json fences if Sonnet wraps its output. Defensive — the
# prompt asks for raw JSON but instruction-following isn't perfect.
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def build_interpret_prompt(query: str, *, today: str) -> str:
    """Build the Sonnet prompt for natural-language scope extraction.

    Output JSON shape:
        {
            "state": "Arizona" | null,
            "county": "<canonical county>" | null,
            "city": "<canonical city>" | null,
            "keywords": ["<noun phrase>", ...],
            "date_range": {"after": "YYYY-MM-DD" | null,
                           "before": "YYYY-MM-DD" | null} | null,
            "confidence": "high" | "medium" | "low"
        }
    """
    return f"""You are extracting a scope specification from a natural-language
operator query for the Z-SPAN civic-data network. Z-SPAN covers U.S.
city council meetings, primarily Arizona at V1.

Operator query: "{query}"

Today's date: {today}

Extract the scope as JSON. Return ONLY the JSON object — no markdown
fences, no prose, no explanation.

Schema:
{{
  "state": "Arizona" | null,
  "county": "<canonical county name with 'County' suffix>" | null,
  "city": "<canonical city name>" | null,
  "keywords": ["<noun phrase>", ...],
  "date_range": {{"after": "YYYY-MM-DD" | null, "before": "YYYY-MM-DD" | null}} | null,
  "confidence": "high" | "medium" | "low"
}}

Rules:
- Default scope: if the operator says "across Arizona" or doesn't name
  a specific city/county, default state="Arizona", county=null, city=null.
- County names use the "Mohave County" form (with "County" suffix),
  e.g. "Mohave County", "Maricopa County", "Pima County".
- Canonical AZ city names include "Kingman", "Bullhead City",
  "Lake Havasu City", "Colorado City" (Mohave County); "Phoenix",
  "Tempe", "Mesa", "Scottsdale" (Maricopa County); "Tucson" (Pima).
- If a city is named, set both city AND the containing county.
- keywords: 2-5 distinct concept noun phrases. Drop stopwords ("the",
  "what", "did", "about", "tell me", "anything", "across"). Prefer
  specific phrases ("Chinese solar farms", "Route 66 trail funding")
  over generic ones ("decisions", "things").
- date_range: convert relative dates to absolute YYYY-MM-DD using
  today's date above. "last month" / "in 2024" / "this year". If no
  date scope is mentioned, set date_range=null (NOT an object with nulls).
- confidence: "high" if scope is unambiguous; "medium" if partly
  inferred; "low" if you're guessing.

Return ONLY the JSON object."""


def parse_interpret_output(raw: str) -> Optional[dict]:
    """Parse Sonnet's interpret output. Strips ```json fences if present.

    Returns the parsed dict, or None if parsing failed. Fills in missing
    required keys with None / [] defaults so callers can index without
    KeyError defensively.
    """
    cleaned = raw.strip()
    fence_match = _FENCE_RE.match(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict):
        return None
    for required in ("state", "county", "city", "confidence"):
        if required not in result:
            result[required] = None
    if not isinstance(result.get("keywords"), list):
        result["keywords"] = []
    if "date_range" not in result:
        result["date_range"] = None
    return result


# ── Phase 3 — fan-out execution + cross-meeting synthesis ────────────

DEFAULT_TOP_K_PER_MEETING = 8
DEFAULT_FANOUT_CONCURRENCY = 10
DEFAULT_MAX_UNION_CHUNKS = 50


@dataclass
class MeetingScope:
    """Per-meeting context the fan-out needs for tagging + citation."""

    meeting_id: int
    city_name: str
    meeting_date: str  # YYYY-MM-DD


@dataclass
class LegResult:
    """One per-meeting retrieval leg's outcome."""

    meeting_id: int
    city_name: str
    meeting_date: str
    interpreted_as: str  # "ok" | "indexed_no_match" | "not_indexed" | "qdrant_down"
    chunks: list = field(default_factory=list)  # list[RetrievedChunk]
    retrieval_run_id: Optional[str] = None
    error: Optional[str] = None


def fan_out_retrieve(
    *,
    query: str,
    scopes: list[MeetingScope],
    top_k_per_meeting: int = DEFAULT_TOP_K_PER_MEETING,
    concurrency: int = DEFAULT_FANOUT_CONCURRENCY,
    rag_host: Optional[str] = None,
    rag_port: Optional[int] = None,
    rag_token: Optional[str] = None,
) -> list[LegResult]:
    """Parallel-retrieve top-K chunks from each meeting in scopes.

    Concurrency-capped to `concurrency` (default 10) — Surface Pro's
    embed model is serialized at the Rust tokenizer level per the
    surface-pro-as-server memory entry, so spraying 20+ concurrent
    /query calls would just queue without speeding anything up.

    Each leg's interpreted_as field follows the same F8 taxonomy as
    /api/rag-search:
        "ok"               - chunks returned successfully
        "indexed_no_match" - retrieve succeeded but 0 chunks matched
                              (rare with cosine similarity; possible
                              if the meeting has 0 indexed points)
        "qdrant_down"      - Surface Pro unreachable / timeout / 5xx

    The "not_indexed" branch isn't hit here because the caller filters
    to indexed meetings upstream via /interpret's meeting_ids; if a
    meeting somehow lost its index between interpret and execute, the
    leg falls through as indexed_no_match rather than not_indexed.
    """
    # Local import to avoid bridge_root path gymnastics at module import time.
    from zspan_pipeline import qdrant_synthesizer
    import requests

    def _retrieve_one(scope: MeetingScope) -> LegResult:
        kwargs: dict[str, Any] = {"top_k": top_k_per_meeting}
        if rag_host is not None:
            kwargs["host"] = rag_host
        if rag_port is not None:
            kwargs["port"] = rag_port
        if rag_token is not None:
            kwargs["token"] = rag_token
        try:
            chunks = qdrant_synthesizer.retrieve_chunks(
                scope.meeting_id, query, **kwargs,
            )
            return LegResult(
                meeting_id=scope.meeting_id,
                city_name=scope.city_name,
                meeting_date=scope.meeting_date,
                interpreted_as="ok" if chunks else "indexed_no_match",
                chunks=chunks,
            )
        except requests.exceptions.RequestException as e:
            logger.warning(
                "fan_out_retrieve: qdrant_down for meeting=%s: %s",
                scope.meeting_id, e,
            )
            return LegResult(
                meeting_id=scope.meeting_id,
                city_name=scope.city_name,
                meeting_date=scope.meeting_date,
                interpreted_as="qdrant_down",
                error=str(e),
            )
        except Exception as e:
            logger.exception(
                "fan_out_retrieve: unexpected error for meeting=%s",
                scope.meeting_id,
            )
            return LegResult(
                meeting_id=scope.meeting_id,
                city_name=scope.city_name,
                meeting_date=scope.meeting_date,
                interpreted_as="qdrant_down",
                error=str(e),
            )

    results: list[LegResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        for leg in ex.map(_retrieve_one, scopes):
            results.append(leg)
    return results


def dedup_and_rerank_chunks(
    legs: list[LegResult],
    *,
    max_union: int = DEFAULT_MAX_UNION_CHUNKS,
) -> list[dict]:
    """Flatten + dedup + re-rank chunks across all legs by score.

    Returns a list of {leg, chunk} pairs, ordered by chunk.score desc,
    capped at max_union. Dedup key is (meeting_id, chunk_index) — same
    chunk retrieved by two queries within one fan-out shouldn't happen
    (each leg only retrieves once), but cheap-defensive.
    """
    flat: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for leg in legs:
        if leg.interpreted_as != "ok":
            continue
        for c in leg.chunks:
            key = (leg.meeting_id, c.chunk_index)
            if key in seen:
                continue
            seen.add(key)
            flat.append({"leg": leg, "chunk": c})
    flat.sort(key=lambda p: p["chunk"].score, reverse=True)
    return flat[:max_union]


def build_synthesis_prompt(
    *,
    query: str,
    interpretation: dict,
    ranked: list[dict],
) -> str:
    """Build the cross-meeting Sonnet synthesis prompt.

    Each chunk is rendered with its source tag so Sonnet can produce
    citations like [Bullhead City · 2026-05-19] that the frontend can
    later parse into linkable chips. The instructions explicitly forbid
    fabrication and require citing the meeting tag for every load-bearing
    factual claim.
    """
    scope_bits = []
    if interpretation.get("state"):
        scope_bits.append(interpretation["state"])
    if interpretation.get("county"):
        scope_bits.append(interpretation["county"])
    if interpretation.get("city"):
        scope_bits.append(interpretation["city"])
    scope_label = " · ".join(scope_bits) if scope_bits else "all locations"

    chunk_blocks: list[str] = []
    for i, pair in enumerate(ranked, start=1):
        leg = pair["leg"]
        c = pair["chunk"]
        tag = f"{leg.city_name} · {leg.meeting_date}"
        # Render the chunk body verbatim; the speaker_turns + timecode
        # are surfaced in the citation chips, not the prompt (Sonnet
        # doesn't need to read them — it just needs to cite the source).
        body = c.body if isinstance(c.body, str) else str(c.body)
        chunk_blocks.append(
            f"[CHUNK {i}] [{tag}] [meeting_id={leg.meeting_id}]\n{body}"
        )
    chunks_text = "\n\n".join(chunk_blocks)

    return f"""You are answering an operator's natural-language question by
synthesizing across multiple Z-SPAN civic-data sources (Arizona city
council meeting transcripts).

Operator query: "{query}"

Scope: {scope_label}

Sources retrieved ({len(ranked)} chunks across {len({pair["leg"].meeting_id for pair in ranked})} meeting{"s" if len({pair["leg"].meeting_id for pair in ranked}) != 1 else ""}):

{chunks_text}

Instructions:

1. Answer the operator's query using ONLY the source chunks above. If
   the chunks don't contain the answer, say so plainly — DO NOT
   fabricate, infer beyond the text, or fill in from general knowledge.
2. Every load-bearing factual claim MUST cite its source meeting using
   the tag format [City · YYYY-MM-DD]. Example:
   "Bullhead City approved $20,000 for the Route 66 trail project
   [Bullhead City · 2026-05-19]."
   IMPORTANT: do NOT include the literal `meeting_id` token from the
   chunk metadata in your output. Cite via the [City · YYYY-MM-DD]
   tag format ONLY — the integer meeting_id shown to you is internal
   plumbing the renderer does not display.
3. Multiple meetings may have relevant information — synthesize across
   them when appropriate. Cite each meeting individually.
4. If chunks contradict each other across meetings, surface the
   contradiction honestly rather than picking one.
5. Use Markdown for structure (bold for key facts, bullet lists where
   appropriate). Keep the answer focused — 2-6 paragraphs typical.
6. Do NOT prefix the answer with "Based on the provided chunks..." or
   similar throat-clearing. Lead with the substance.

Return the answer as Markdown. No JSON envelope, no code fences."""


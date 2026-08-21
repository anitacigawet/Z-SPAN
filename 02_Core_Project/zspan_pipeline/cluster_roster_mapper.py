"""Phase 2 D6 — cluster→roster mapping with two-prong safety gate.

After a meeting is diarized (D3) and indexed (D4), pyannote has tagged
each transcript word with an anonymous cluster label (SPEAKER_00,
SPEAKER_01, ...). This module maps those cluster labels to canonical
`council_members` rows via a Sonnet pass over the meeting's opening
minutes — typically the section where speakers introduce themselves
("Mayor Watkins recognized me", "Councilmember Stehly speaking", etc.).

Architecture mirrors V1-Consensus-1 at `parsers/consensus_vocab.py`:
  - Sonnet proposes a mapping per cluster_label + evidence_text
  - Two-prong safety gate evaluates the proposal:
      Prong 1 — anchor evidence (explicit introduction or role-tagged
                reference present in evidence_text)
      Prong 2 — specificity (proposed canonical's last name is unique
                in the city's roster — no ambiguity)
  - When BOTH prongs pass → auto-promote (status='auto_promoted',
    confirmed_canonical=proposed_canonical)
  - When EITHER fails → pending_review for the Speaker Roster Review
    queue (D-Build-B)

The mapping persists to `meeting_speaker_roster` (D2 table). The D5
extractor prompt receives the confirmed mappings as a CLUSTER_ROSTER
block; D7 worker integration wires the call sequence end-to-end.

Composes with:
  - zspan_pipeline.qdrant_synthesizer (retrieve_chunks +
    synthesize_via_claude_p — same primitives V1-RAG-3 uses)
  - parsers.database (upsert_speaker_roster_row,
    get_speaker_roster_for_meeting, get_canonical_for_cluster)
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import qdrant_synthesizer

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent

# How many of a meeting's earliest chunks to feed Sonnet for the mapping
# pass. The opening 5-10 minutes of a council meeting typically include
# the gavel, roll call, and pledge — natural moments where speakers are
# named explicitly. 8 chunks at ~400 tokens each ~= 3200 tokens of
# context, well within Sonnet's prompt budget.
DEFAULT_OPENING_CHUNK_COUNT = 8

# Retrieval query biased toward the introduction/roll-call section.
OPENING_RETRIEVAL_QUERY = (
    "Roll call mayor councilmember introductions opening gavel "
    "pledge of allegiance call to order welcome attendance."
)

# How sensitive Prong 1 is — these patterns + the proposed canonical's
# last name should both appear near each other in evidence_text for the
# anchor to count. Compiled lazily per call so the canonical can be
# spliced in.
_PRONG_1_ANCHOR_PATTERNS = (
    # "Mayor Watkins", "Vice Mayor Watkins", "Councilmember Watkins"
    r"\b(Mayor|Vice\s*Mayor|Council\s*member|Councilman|Councilwoman|Councilor|Counselor)\s+{last}\b",
    # "Watkins, Mayor"
    r"\b{last}\b\s*,?\s*(Mayor|Vice\s*Mayor|Council\s*member|Councilman|Councilwoman|Councilor|Counselor)\b",
    # "I am Mayor Watkins", "I'm Councilmember Stehly"
    r"\bI\s*('?m|\s+am)\s+(Mayor|Vice\s*Mayor|Council\s*member|Councilman|Councilwoman|Councilor|Counselor)\s+{last}\b",
    # "Mayor Watkins recognized" / "Mayor Watkins moves" — role + last + verb
    r"\b(Mayor|Vice\s*Mayor|Council\s*member|Councilman|Councilwoman|Councilor|Counselor)\s+{last}\s+(recognized|moves|seconds|votes|yields)\b",
    # "the Mayor", "the Vice Mayor" — accept only when accompanied by surname
    # within ~30 chars (handled via lookahead window in code, not here)
)


@dataclass
class ProposedMapping:
    """One Sonnet-proposed cluster→canonical mapping with provenance."""

    cluster_label: str
    proposed_canonical: Optional[str]  # None means "Sonnet could not infer"
    evidence_text: str
    evidence_chunk_indices: List[int]


@dataclass
class ProngVerdict:
    """Outcome of evaluating a single prong against a proposal."""

    passed: bool
    reasoning: str


# Sentinel labels we never auto-promote — they're cross-talk + skipped
# audio, not real speakers.
SENTINEL_CLUSTER_LABELS = {"OVERLAP", "UNKNOWN"}


def _load_roster(city_name: str) -> List[Dict[str, Any]]:
    """Pull council_members rows for the city."""
    parsers_path = (
        _THIS_DIR.parent / "council_navigator" / "parsers"
    )
    if str(parsers_path) not in sys.path:
        sys.path.insert(0, str(parsers_path))
    from database import get_council_members  # type: ignore
    return list(get_council_members(city_name) or [])


def _format_roster_for_prompt(members: List[Dict[str, Any]]) -> str:
    """Render the council_members rows as a labeled CANONICAL_ROSTER block."""
    lines = ["CANONICAL_ROSTER:"]
    for m in members:
        name = (m.get("name") or "").strip()
        role = (m.get("role") or "Council Member").strip()
        if name:
            lines.append(f"  - {name} ({role})")
    if len(lines) == 1:
        lines.append("  (no council_members rows in DB for this city)")
    return "\n".join(lines)


# ── Stage 1: Sonnet proposes mappings ─────────────────────────────────


_DEFAULT_PROPOSE_TIMEOUT_S = 600.0  # bumped 180 → 600 after m104714 timed
# out 2026-06-30 with 18 clusters + ~3min real synthesis time. The old
# 180s was too tight for meetings with many speaker clusters; 600s gives
# Sonnet headroom without unbounded grinding. Composes with the
# worker-headless-resilience-V0 chunk (TEMPORARY_THOUGHTS 2026-06-30).


def propose_mapping_via_sonnet(
    meeting_id: int,
    city_name: str,
    *,
    opening_chunk_count: int = DEFAULT_OPENING_CHUNK_COUNT,
    model: str = qdrant_synthesizer.SONNET_MODEL_ID,
    timeout_seconds: float = _DEFAULT_PROPOSE_TIMEOUT_S,
) -> List[ProposedMapping]:
    """Sonnet pass over the meeting's opening chunks → proposed mappings.

    Returns one ProposedMapping per cluster label observed in the
    opening chunks. proposed_canonical is None when Sonnet declines
    to attribute (no clear introduction evidence found).
    """
    chunks = qdrant_synthesizer.retrieve_chunks(
        meeting_id, OPENING_RETRIEVAL_QUERY, top_k=opening_chunk_count,
    )
    if not chunks:
        logger.warning(
            "propose_mapping_via_sonnet: no chunks retrieved for meeting=%d",
            meeting_id,
        )
        return []

    # Sort by chunk_index so Sonnet sees them in narrative order.
    chunks.sort(key=lambda c: c.chunk_index)

    # Detect cluster labels present in the retrieved chunks.
    observed_labels: set[str] = set()
    for c in chunks:
        if c.speaker_turns:
            for t in c.speaker_turns:
                label = t.get("speaker_label")
                if label and label not in SENTINEL_CLUSTER_LABELS:
                    observed_labels.add(label)

    if not observed_labels:
        logger.warning(
            "propose_mapping_via_sonnet: no cluster labels found in opening "
            "chunks of meeting=%d (was the meeting diarized?)", meeting_id,
        )
        return []

    members = _load_roster(city_name)
    roster_block = _format_roster_for_prompt(members)
    chunks_block = "\n\n".join(
        qdrant_synthesizer._format_chunk_for_prompt(c) for c in chunks
    )

    observed_list = ", ".join(sorted(observed_labels))
    prompt = (
        f"You are mapping anonymous speaker-diarization cluster labels to "
        f"canonical council_members for one municipal council meeting. "
        f"pyannote.audio produced cluster labels (SPEAKER_00, SPEAKER_01, "
        f"etc.) by clustering acoustic features; the labels are local to "
        f"this meeting (SPEAKER_00 here has no relationship to SPEAKER_00 "
        f"in any other meeting).\n\n"
        f"Your job: for each cluster label observed in the opening chunks, "
        f"infer the canonical roster member who owns that cluster, OR "
        f"return null if no clear introduction evidence is present.\n\n"
        f"OBSERVED CLUSTER LABELS in opening chunks: {observed_list}\n\n"
        f"{roster_block}\n\n"
        f"CITY: {city_name}\n"
        f"MEETING_ID: {meeting_id}\n\n"
        f"OPENING CHUNKS — first {len(chunks)} chunks of the meeting "
        f"(roll call, gavel, pledge, opening business; speakers typically "
        f"introduce themselves or are addressed by name + role here):\n"
        f"---\n"
        f"{chunks_block}\n"
        f"---\n\n"
        f"For each cluster label, look for explicit introduction or "
        f"address evidence in the chunks above:\n"
        f"  - Speaker says 'I'm Mayor X' / 'Councilmember Y'\n"
        f"  - Another speaker addresses them: 'Mayor X, would you like to...'\n"
        f"  - The procedural cue 'Mayor X recognized', 'Councilmember Y moves'\n"
        f"  - Roll call: 'Mayor X present', 'Councilmember Y here'\n\n"
        f"Return strict JSON with NO preamble or trailing commentary:\n\n"
        f"```\n"
        f"{{\n"
        f"  \"mappings\": [\n"
        f"    {{\n"
        f"      \"cluster_label\": \"SPEAKER_00\",\n"
        f"      \"proposed_canonical\": \"Ken Watkins\" or null,\n"
        f"      \"evidence_text\": \"verbatim snippet from the chunks above showing the attribution\",\n"
        f"      \"evidence_chunk_indices\": [12, 14]\n"
        f"    }},\n"
        f"    ...\n"
        f"  ]\n"
        f"}}\n"
        f"```\n\n"
        f"Rules:\n"
        f"  - `proposed_canonical` MUST be the EXACT name from CANONICAL_ROSTER "
        f"or null. Never invent a name. Never paraphrase.\n"
        f"  - `evidence_text` MUST be a verbatim snippet from the opening "
        f"chunks. If you can't find a verbatim anchor, return null for "
        f"proposed_canonical AND set evidence_text to a 1-line note "
        f"explaining what you observed (e.g., 'cluster speaks only after "
        f"the chair, no name reference').\n"
        f"  - `evidence_chunk_indices` MUST list the chunk_index of every "
        f"chunk you cited evidence from. Empty list if proposed_canonical "
        f"is null.\n"
        f"  - Output one mapping per OBSERVED cluster label. Do NOT include "
        f"OVERLAP or UNKNOWN — those are sentinels, not real speakers.\n"
        f"  - HONEST-EMPTY > FABRICATED. When the evidence is weak, return "
        f"null. The downstream safety gate will require both an anchor and "
        f"specificity before auto-promoting.\n"
    )

    raw = qdrant_synthesizer.synthesize_via_claude_p(
        prompt, model=model, timeout_seconds=timeout_seconds,
    )

    # Strip optional ```json fences.
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "propose_mapping_via_sonnet: JSON parse failed (meeting=%d): %s; "
            "raw[:300]=%r", meeting_id, exc, raw[:300],
        )
        return []

    out: List[ProposedMapping] = []
    for m in parsed.get("mappings", []):
        if not isinstance(m, dict):
            continue
        cl = (m.get("cluster_label") or "").strip()
        if not cl or cl in SENTINEL_CLUSTER_LABELS:
            continue
        proposed = m.get("proposed_canonical")
        proposed_str = (
            str(proposed).strip() if proposed else None
        )
        evidence = str(m.get("evidence_text") or "").strip()
        idxs_raw = m.get("evidence_chunk_indices") or []
        idxs: List[int] = []
        for i in idxs_raw:
            try:
                idxs.append(int(i))
            except (TypeError, ValueError):
                continue
        out.append(
            ProposedMapping(
                cluster_label=cl,
                proposed_canonical=proposed_str,
                evidence_text=evidence,
                evidence_chunk_indices=idxs,
            )
        )
    return out


# ── Stage 2: Two-prong safety gate ─────────────────────────────────────


def evaluate_prong_1_anchor_evidence(
    proposed_canonical: str, evidence_text: str,
) -> ProngVerdict:
    """Prong 1 — explicit anchor evidence in evidence_text.

    Looks for a verbatim phrase combining the proposed canonical's last
    name with a role indicator (Mayor, Vice Mayor, Council Member, etc.)
    OR a procedural cue ("recognized", "moves", "seconds").
    """
    if not proposed_canonical or not evidence_text:
        return ProngVerdict(False, "missing proposed_canonical or evidence_text")

    last = proposed_canonical.split()[-1]
    last_escaped = re.escape(last)

    for pattern_template in _PRONG_1_ANCHOR_PATTERNS:
        pattern = pattern_template.format(last=last_escaped)
        if re.search(pattern, evidence_text, re.IGNORECASE):
            return ProngVerdict(
                True,
                f"anchor pattern matched in evidence_text: "
                f"role-prefix + last name '{last}'",
            )

    return ProngVerdict(
        False,
        f"no role+last-name anchor for '{last}' found in evidence_text "
        f"(checked Mayor/Vice Mayor/Councilmember/etc. variants)",
    )


def evaluate_prong_2_specificity(
    proposed_canonical: str, roster: List[Dict[str, Any]],
) -> ProngVerdict:
    """Prong 2 — last-name uniqueness in the city's roster.

    The proposed canonical's last name must match exactly ONE member in
    the roster. If two members share a last name, an introduction
    citing only "Watkins" is ambiguous and the mapping needs operator
    confirmation, not auto-promote.
    """
    if not proposed_canonical:
        return ProngVerdict(False, "missing proposed_canonical")
    if not roster:
        return ProngVerdict(False, "empty roster — cannot verify specificity")

    proposed_last = proposed_canonical.split()[-1].lower()
    matching = [
        m for m in roster
        if (m.get("name") or "").split() and
           (m.get("name") or "").split()[-1].lower() == proposed_last
    ]
    if not matching:
        return ProngVerdict(
            False,
            f"proposed canonical '{proposed_canonical}' last-name "
            f"'{proposed_last}' does not match any roster member",
        )
    if len(matching) > 1:
        names = ", ".join((m.get("name") or "") for m in matching)
        return ProngVerdict(
            False,
            f"last-name '{proposed_last}' is ambiguous — matches multiple "
            f"roster members: {names}",
        )
    # Exact match for canonical (case-sensitive) — confirms Sonnet didn't
    # echo back a near-canonical variant.
    matched = matching[0]
    if (matched.get("name") or "") != proposed_canonical:
        return ProngVerdict(
            False,
            f"proposed canonical '{proposed_canonical}' does not exactly "
            f"match roster entry '{matched.get('name')}' — possible echoing "
            f"of role-prefixed form",
        )
    return ProngVerdict(
        True,
        f"unique last-name match against roster: '{proposed_canonical}'",
    )


# ── Stage 3: Apply mapping ─────────────────────────────────────────────


def apply_mapping(
    meeting_id: int,
    proposals: List[ProposedMapping],
    *,
    city_name: str,
    model_id: str = qdrant_synthesizer.SONNET_MODEL_ID,
) -> Dict[str, Any]:
    """Persist each proposed mapping, evaluating both prongs.

    Returns a summary dict:
        {
          "auto_promoted": int, "pending_review": int, "rejected": int,
          "voice_samples_captured": int,
          "rows": [{cluster_label, status, prong_1_passed, prong_2_passed,
                    proposed_canonical}]
        }
    """
    parsers_path = (
        _THIS_DIR.parent / "council_navigator" / "parsers"
    )
    if str(parsers_path) not in sys.path:
        sys.path.insert(0, str(parsers_path))
    from database import upsert_speaker_roster_row  # type: ignore

    roster = _load_roster(city_name)

    auto = 0
    pending = 0
    rejected = 0
    rows: List[Dict[str, Any]] = []

    for prop in proposals:
        if not prop.proposed_canonical:
            # Sonnet couldn't infer — log as pending_review with the note,
            # so operator can later resolve via the UI.
            res = upsert_speaker_roster_row(
                meeting_id, prop.cluster_label,
                proposed_canonical=None,
                evidence_chunk_indices=prop.evidence_chunk_indices,
                evidence_text=prop.evidence_text,
                prong_1_passed=False,
                prong_1_reasoning="no proposed_canonical from Sonnet",
                prong_2_passed=False,
                prong_2_reasoning="N/A (no canonical to verify)",
                status="pending_review",
                model_id=model_id,
            )
            pending += 1
            rows.append({
                "cluster_label": prop.cluster_label,
                "status": "pending_review",
                "proposed_canonical": None,
                "prong_1_passed": False,
                "prong_2_passed": False,
                "id": res["id"],
            })
            continue

        p1 = evaluate_prong_1_anchor_evidence(
            prop.proposed_canonical, prop.evidence_text,
        )
        p2 = evaluate_prong_2_specificity(prop.proposed_canonical, roster)
        both_pass = p1.passed and p2.passed
        status = "auto_promoted" if both_pass else "pending_review"

        res = upsert_speaker_roster_row(
            meeting_id, prop.cluster_label,
            proposed_canonical=prop.proposed_canonical,
            evidence_chunk_indices=prop.evidence_chunk_indices,
            evidence_text=prop.evidence_text,
            prong_1_passed=p1.passed, prong_1_reasoning=p1.reasoning,
            prong_2_passed=p2.passed, prong_2_reasoning=p2.reasoning,
            status=status, model_id=model_id,
        )
        if both_pass:
            auto += 1
        else:
            pending += 1
        rows.append({
            "cluster_label": prop.cluster_label,
            "status": status,
            "proposed_canonical": prop.proposed_canonical,
            "prong_1_passed": p1.passed,
            "prong_2_passed": p2.passed,
            "id": res["id"],
        })

    return {
        "auto_promoted": auto,
        "pending_review": pending,
        "rejected": rejected,
        "rows": rows,
    }
# ── Stage 4: Build CLUSTER_ROSTER block for the extractor prompt ──────


def build_cluster_roster_block(meeting_id: int) -> str:
    """Render the meeting's confirmed cluster→canonical mappings as a
    CLUSTER_ROSTER block ready to splice into the D5 extractor prompt.

    Only includes rows where a canonical name is resolved (auto-promoted
    or operator-confirmed); pending_review rows are omitted so the
    extractor falls back to proximity inference for those clusters
    rather than acting on a low-confidence guess.

    Returns "" when no rows resolve — the caller then omits the block
    from the prompt.
    """
    parsers_path = (
        _THIS_DIR.parent / "council_navigator" / "parsers"
    )
    if str(parsers_path) not in sys.path:
        sys.path.insert(0, str(parsers_path))
    from database import get_speaker_roster_for_meeting  # type: ignore

    rows = get_speaker_roster_for_meeting(meeting_id)
    resolved = [
        r for r in rows
        if r.get("confirmed_canonical")
        and r.get("status") in ("auto_promoted", "operator_confirmed", "operator_overridden")
    ]
    if not resolved:
        return ""

    lines = [
        "CLUSTER_ROSTER — confirmed mappings for this meeting (use these "
        "directly when a chunk's SPEAKER_NN block matches a cluster below; "
        "the cluster label is the authoritative attribution signal, not "
        "textual proximity):",
    ]
    for r in sorted(resolved, key=lambda x: x["cluster_label"]):
        lines.append(
            f"  {r['cluster_label']} -> {r['confirmed_canonical']}"
            + (f"  (auto-promoted; both prongs passed)"
               if r["status"] == "auto_promoted"
               else f"  (operator-confirmed)")
        )
    return "\n".join(lines)


# ── Top-level orchestrator ─────────────────────────────────────────────


def map_clusters_for_meeting(
    meeting_id: int,
    city_name: str,
    *,
    opening_chunk_count: int = DEFAULT_OPENING_CHUNK_COUNT,
    model: str = qdrant_synthesizer.SONNET_MODEL_ID,
) -> Dict[str, Any]:
    """End-to-end: propose → evaluate prongs → persist.

    Idempotent against re-runs on the same meeting: existing
    operator-touched rows preserve their confirmed_canonical even when
    Sonnet's proposal changes (see database.upsert_speaker_roster_row's
    CASE clause).

    Returns the apply_mapping summary dict (auto / pending / rejected
    counts + per-row outcomes).
    """
    logger.info(
        "map_clusters_for_meeting: meeting=%d city=%s — Sonnet propose",
        meeting_id, city_name,
    )
    proposals = propose_mapping_via_sonnet(
        meeting_id, city_name,
        opening_chunk_count=opening_chunk_count, model=model,
    )
    if not proposals:
        logger.warning(
            "map_clusters_for_meeting: no proposals returned for meeting=%d "
            "— diarization may not have run OR opening chunks have no "
            "introductions", meeting_id,
        )
        return {
            "auto_promoted": 0, "pending_review": 0, "rejected": 0,
            "rows": [],
        }

    logger.info(
        "map_clusters_for_meeting: %d Sonnet proposals; evaluating prongs",
        len(proposals),
    )

    # The Sonnet propose step only sees opening chunks (where intros
    # concentrate), so speakers who introduce themselves mid-meeting
    # (e.g., the food-bank operator at minute 43 of Bullhead m104714)
    # never get a proposal. The operator-confirm UI needs every speaker
    # in the queue regardless. Enumerate all clusters present in any
    # Qdrant chunk for this meeting + add unmapped ones as
    # `proposed_canonical=None` rows so the operator can identify them.
    proposed_labels = {p.cluster_label for p in proposals}
    all_labels = _enumerate_all_cluster_labels(meeting_id)
    missing = all_labels - proposed_labels
    if missing:
        logger.info(
            "map_clusters_for_meeting: %d cluster(s) absent from Sonnet "
            "proposals — adding as pending_review with operator-identify "
            "fallback: %s",
            len(missing), sorted(missing),
        )
        for label in sorted(missing):
            evidence = _longest_turn_evidence(meeting_id, label) or ""
            proposals.append(ProposedMapping(
                cluster_label=label,
                proposed_canonical=None,
                evidence_text=evidence,
                evidence_chunk_indices=[],
            ))

    summary = apply_mapping(
        meeting_id, proposals, city_name=city_name, model_id=model,
    )
    logger.info(
        "map_clusters_for_meeting: meeting=%d done — "
        "%d auto-promoted, %d pending_review",
        meeting_id, summary["auto_promoted"], summary["pending_review"],
    )
    return summary


def _enumerate_all_cluster_labels(meeting_id: int) -> set[str]:
    """Walk Qdrant chunks for the meeting + return every distinct
    SPEAKER_NN cluster_label that appears in any chunk's speaker_turns.
    Skips OVERLAP / UNKNOWN sentinels."""
    try:
        chunks_by_index = _sample_meeting_chunks(meeting_id)
        labels: set[str] = set()
        for chunk in chunks_by_index.values():
            for t in chunk.speaker_turns or []:
                lab = (t.get("speaker_label") or "").strip()
                if lab and lab not in ("OVERLAP", "UNKNOWN", "?"):
                    labels.add(lab)
        return labels
    except Exception:
        logger.exception("enumerate_all_cluster_labels: unexpected error")
        return set()


def top_n_turn_excerpts(
    meeting_id: int,
    cluster_label: str,
    *,
    n: int = 3,
    min_duration_seconds: float = 2.0,
) -> List[Dict[str, Any]]:
    """Return the cluster's top-N longest speech turns (by duration) as a
    list of operator-readable excerpts. Each entry carries `start_seconds`,
    `end_seconds`, `duration_seconds`, `text`, `chunk_index` — enough for
    the SpeakerRosterReviewPage to render a quote with a timestamp + a
    "Listen in context" deep-link that opens BroadcastPage at
    `start_seconds`.

    The page calls this through `/api/speaker-roster/<row_id>/cluster-samples`;
    keeping it as a sibling of `_longest_turn_evidence` so both share
    `_sample_meeting_chunks`. Returns `[]` honestly when no indexed
    chunks carry turns for this cluster (e.g., meeting was diarized but
    not yet indexed, or every turn ran shorter than the min-duration
    floor).
    """
    try:
        chunks_by_index = _sample_meeting_chunks(meeting_id)
    except Exception:
        logger.exception("top_n_turn_excerpts: _sample_meeting_chunks failed")
        return []

    # Collect every matching turn across all sampled chunks. The same
    # transcript turn can appear in multiple Qdrant chunks (chunking
    # overlap), so dedupe by (start, end) before sorting — otherwise the
    # top-N list can end up with three near-identical excerpts.
    seen: set[tuple[float, float]] = set()
    candidates: list[tuple[float, float, float, str, int]] = []  # (start, end, duration, text, chunk_index)
    for chunk in chunks_by_index.values():
        for t in chunk.speaker_turns or []:
            if (t.get("speaker_label") or "").strip() != cluster_label:
                continue
            try:
                start = float(t.get("start", 0))
                end = float(t.get("end", 0))
            except (TypeError, ValueError):
                continue
            duration = end - start
            text = (t.get("text") or "").strip()
            if not text or duration < min_duration_seconds:
                continue
            key = (round(start, 2), round(end, 2))
            if key in seen:
                continue
            seen.add(key)
            candidates.append((start, end, duration, text, chunk.chunk_index))

    candidates.sort(key=lambda r: r[2], reverse=True)
    excerpts: list[Dict[str, Any]] = []
    for start, end, duration, text, chunk_index in candidates[:max(1, n)]:
        excerpts.append({
            "start_seconds": round(start, 2),
            "end_seconds": round(end, 2),
            "duration_seconds": round(duration, 2),
            "text": text[:400] + ("…" if len(text) > 400 else ""),
            "chunk_index": chunk_index,
        })
    return excerpts


def _longest_turn_evidence(meeting_id: int, cluster_label: str) -> str | None:
    """Find this cluster's longest speech turn (by duration) from the
    sampled Qdrant chunks + return a trimmed excerpt as evidence_text
    so the operator-review row has something readable instead of a bare
    cluster label."""
    try:
        chunks_by_index = _sample_meeting_chunks(meeting_id)
        best: tuple[float, str] | None = None  # (duration, text)
        for chunk in chunks_by_index.values():
            for t in chunk.speaker_turns or []:
                if (t.get("speaker_label") or "").strip() != cluster_label:
                    continue
                dur = float(t.get("end", 0)) - float(t.get("start", 0))
                text = (t.get("text") or "").strip()
                if not text or dur < 2.0:
                    continue
                if best is None or dur > best[0]:
                    best = (dur, text)
        if best is None:
            return None
        text = best[1]
        return text[:300] + ("…" if len(text) > 300 else "")
    except Exception:
        logger.exception("_longest_turn_evidence: unexpected error")
        return None


_SAMPLE_QUERIES = (
    "city council meeting motion vote",
    "public comment item discussion",
    "thank you mayor councilmember",
)


def _sample_meeting_chunks(meeting_id: int):
    """Run a few broad Qdrant queries + return a deduped dict of
    `RetrievedChunk` keyed by chunk_index. Used by the all-clusters
    enumeration so we don't re-query for every cluster_label."""
    chunks_by_index = {}
    for q in _SAMPLE_QUERIES:
        try:
            for c in qdrant_synthesizer.retrieve_chunks(
                meeting_id=meeting_id, query=q, top_k=50,
            ):
                if c.chunk_index not in chunks_by_index:
                    chunks_by_index[c.chunk_index] = c
        except Exception:
            logger.exception("_sample_meeting_chunks: query failed q=%r", q)
    return chunks_by_index

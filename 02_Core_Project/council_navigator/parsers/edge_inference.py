"""
parsers.edge_inference — Conversational Compiler constraint-checker pass.

Reads `transcript_nodes` for a meeting and emits `transcript_edges` per
CONVERSATIONAL_COMPILER_SPEC § Edge types. V0 fires two edge kinds:

  * `responds_to` (Vote → Motion) — procedural response. Per SPEC §
    Resolved design decisions row 6, the constraint checker matches
    `Vote.typed_fields.motion_reference` (and the Vote's `agenda_item`)
    against Motion nodes in the same meeting via:
      1) agenda-item key prefix match (Item 2E, Item 4A, etc.) — the
         strong civic-procedure signal; confidence 0.95
      2) token-set Jaccard similarity on motion_reference/summary
         vs Motion.summary_sentence/motion_text — fallback when
         agenda keys don't match cleanly; confidence = score

  * `satisfies` (Vote-passed → Commit_P) — the "Heap-allocation-freed"
    edge from the memory-leak metaphor. Fires when a Vote with
    `vote_result='passed'` matches a Commit_P node's claim_text or
    context strongly enough that the vote operationalizes the commitment.

Per SPEC § Relationship to existing tracked_claims, Commit_P canonically
lives in `transcript_nodes`; `tracked_claims` is the projection. Hand-
seeded m101091 claims pre-date `transcript_nodes` and have
`source_node_id=NULL`. This module backfills them into transcript_nodes
Commit_P rows first (idempotent — runs once per claim, then skipped),
linking via `tracked_claims.source_node_id`, so `satisfies` edges can
target them.

EMPIRICAL CAVEAT (per [[llm-directives-as-soft-hints]] + D-087): the
The [SYMBOLS] linker contract bounds drift but doesn't eliminate it. The
matchers tolerate role-prefixed speaker forms ("Counselor Stehly") and
mild ASR variance because those upstream-drift signals leak past the
contract. The agenda-key signal is strong precisely because it's NOT
emitted by the LLM as canonical prose — it's a parliamentary-procedure
identifier the council uses verbatim.

CLI:
    python3.11 -m parsers.edge_inference --meeting-id 101091 --city Kingman
    python3.11 -m parsers.edge_inference --meeting-id 101091 --dry-run --verbose

Idempotent: each invocation wipes existing responds_to + satisfies edges
for the meeting's nodes before inserting new ones. Other edge types
(references / entails / contradicts) are preserved.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Make parsers/ importable for module-mode invocation from any cwd
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from database import get_connection  # noqa: E402


# Confidence thresholds. Tuned against m101091 ground truth:
#   - agenda-key prefix match: 5/5 Motions ↔ 5/6 Votes (Vote #1 is the
#     procedural consent-agenda roll-up with no single Motion)
#   - token-jaccard fallback: catches paraphrase drift when one side
#     uses civic vocabulary ("ordinance amending zoning code") while
#     the other uses summary prose ("zoning code text amendments")
_AGENDA_KEY_CONFIDENCE = 0.95
_RESPONDS_MIN_CONFIDENCE = 0.35
# Satisfies uses VOTE-SIDE CONTAINMENT (|∩| / |vote_tokens|) rather than
# symmetric Jaccard. A Vote is the short, focused procedural artifact;
# a Commit_P (backfilled or LLM-extracted) carries much longer
# claim_text + context + transcript_span. Jaccard's union-denominator
# is dominated by the Commit_P's verbosity even when the Vote's content
# is structurally inside the commitment — which produces low scores
# (0.19 for the V5/cp6 ADA-barrier pair on m101091) that look like
# noise but aren't. Containment-from-vote-side captures the directional
# "is this vote's content inside this commitment?" question that
# satisfies actually asks. Empirical against m101091:
#   V5/cp6 (ADA barrier — should fire):           0.50  ✓
#   V2/cp5 (Capitol Police staffing — semantic): 0.083 ✗ (correct miss)
#   All other pairs:                              < 0.05
_SATISFIES_MIN_CONFIDENCE = 0.30
_COMPILER_PARSER_MODEL_COMMIT_P_BACKFILL = "compiler:backfill_from_tracked_claims@v1"

# Match strictness — agenda-item identifiers are short (e.g. "2E", "4A",
# "10B"). Two acceptable shapes:
#   - "Item 2E", "ITEM 2E:", "item 2E - title..." (the canonical form
#     councils + the agenda_transitions prompt use)
#   - bare "4B - Z26-00001..." (the form extraction sometimes returns
#     for seconds/votes when the speaker just says the item number
#     without saying "Item")
# The regex tries the "Item NNNN" form first; the bare-leading-number
# helper below catches the second form.
_AGENDA_KEY_RE = re.compile(
    r"\bitem\s+(\d+[A-Za-z]?)\b",
    re.IGNORECASE,
)
_BARE_LEADING_AGENDA_KEY_RE = re.compile(
    r"^\s*(\d+[A-Za-z]?)\b",
)

# Stopwords for the token-jaccard similarity. Civic-meeting vocabulary
# that adds noise without semantic signal ("approve", "council", "the")
# joins the standard stopword list. Kept short — over-pruning hurts more
# than under-pruning at this scale.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "for", "with", "in", "on",
    "at", "by", "as", "is", "are", "was", "were", "be", "been", "being",
    "from", "that", "this", "these", "those",
    # Civic-meeting noise
    "council", "councilmember", "approve", "accept", "motion", "second",
    "vote", "passed", "approval", "approving", "meeting",
})


def _normalize_for_tokens(text: Optional[str]) -> List[str]:
    """Lowercase + strip punctuation + drop stopwords. Returns token list."""
    if not text:
        return []
    # Replace non-alphanumeric with whitespace; collapse runs.
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", text.lower())
    tokens = [t for t in cleaned.split() if t and t not in _STOPWORDS]
    return tokens


def _token_jaccard(a: Optional[str], b: Optional[str]) -> float:
    """Jaccard similarity over the normalized token sets."""
    sa, sb = set(_normalize_for_tokens(a)), set(_normalize_for_tokens(b))
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def _vote_side_containment(vote_text: Optional[str], target_text: Optional[str]) -> float:
    """Directional containment: what fraction of the vote's token set
    appears in the target's? Biases toward the shorter / more focused
    side (the vote), which is the right asymmetric question for
    satisfies — "is this vote's substance inside this commitment?".

    Use jaccard for symmetric questions (Vote ↔ Motion responds_to) and
    this for asymmetric (Vote → Commit_P satisfies).
    """
    sv = set(_normalize_for_tokens(vote_text))
    st = set(_normalize_for_tokens(target_text))
    if not sv or not st:
        return 0.0
    return len(sv & st) / len(sv)


def _extract_agenda_key(agenda_item: Optional[str]) -> Optional[str]:
    """Pull the "Item NNNN" identifier from an agenda_item string.

    Examples:
      "Item 2E - MOU with United States Capitol Police" → "2E"
      "Item 2J Kingman Little League scoreboards donation" → "2J"
      "Item 4A FY 2026 CDBG regional account fund project" → "4A"
      "Item 2 Consent Agenda" → "2"
      "4B - Z26-00001 and Ordinance No. 1993" → "4B" (bare form;
        Extraction sometimes drops the "Item" prefix in seconds /
        votes when the speaker just says the item number)
      "" / None → None
    """
    if not agenda_item:
        return None
    m = _AGENDA_KEY_RE.search(agenda_item)
    if m:
        return m.group(1).upper()
    # Fallback: bare leading number at start of string ("4B - foo")
    m = _BARE_LEADING_AGENDA_KEY_RE.match(agenda_item)
    if m:
        return m.group(1).upper()
    return None


def _agenda_key_parent_fallback(key: str) -> Optional[str]:
    """When no exact agenda-transition exists for a child's key, fall
    back to the digit-only prefix — a sub-item conceptually lives
    under the broader item. Example: "4A" (CDBG sub-item) hangs under
    a "4" transition (the public-hearing block). Returns None when the
    key has no digit-only prefix to try, or when the key IS the digit-
    only form already."""
    if not key:
        return None
    # Strip trailing letter to get the digit-only prefix
    m = re.match(r"^(\d+)[A-Za-z]+$", key)
    if not m:
        return None
    return m.group(1)


def _motion_text_for_match(motion_typed_fields: Dict) -> str:
    """Compose the Motion's text for matching: summary + motion_text +
    agenda_item context. Used as the right-hand side of the token-jaccard
    similarity against a Vote's motion_reference."""
    parts: List[str] = []
    for k in ("summary_sentence", "motion_text", "agenda_item"):
        v = motion_typed_fields.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " ".join(parts)


def _vote_text_for_match(vote_typed_fields: Dict) -> str:
    """Compose the Vote's text for matching: motion_reference + summary
    + agenda_item. Used as the left-hand side against either a Motion or
    a Commit_P."""
    parts: List[str] = []
    for k in ("motion_reference", "summary_sentence", "agenda_item"):
        v = vote_typed_fields.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " ".join(parts)


def _commit_text_for_match(node_row: Dict) -> str:
    """Compose a Commit_P node's text for matching against a Vote.
    Reads both the typed_fields projection (claim_text + context + summary)
    AND the transcript_span_text (which often holds the motion phrasing
    that the backfill copied over from tracked_claims.context)."""
    parts: List[str] = []
    tf = node_row.get("typed_fields") or {}
    for k in ("summary_sentence", "claim_text", "context", "expected_outcome"):
        v = tf.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    span = node_row.get("transcript_span_text")
    if isinstance(span, str) and span.strip():
        parts.append(span.strip())
    return " ".join(parts)


# ── Backfill: hand-seeded tracked_claims → transcript_nodes Commit_P ──


def backfill_commit_p_nodes(meeting_id: int, *, dry_run: bool = False) -> Dict:
    """For each tracked_claims row in this meeting with `source_node_id IS
    NULL`, create a corresponding `transcript_nodes` Commit_P row and
    link it back via `tracked_claims.source_node_id`.

    Per SPEC § Relationship to existing tracked_claims, transcript_nodes
    is canonical; tracked_claims is the projection. The m101091 hand-
    seeded claims (and any other legacy rows) pre-date the
    transcript_nodes table, so the backfill aligns them with the
    canonical model. Without this, satisfies edges can't fire — the
    edges table FKs to transcript_nodes.id.

    Idempotent: claims with non-NULL source_node_id are skipped. Safe to
    re-run after fresh seeds.

    Returns: {created, skipped_already_linked, skipped_no_claims}
    """
    conn = get_connection()
    cursor = conn.cursor()

    claim_rows = cursor.execute(
        """
        SELECT id, member_id, claim_type, claim_text, expected_outcome,
               time_horizon_months, topic_tags, confidence, context,
               source_node_id
        FROM tracked_claims
        WHERE meeting_id = ?
        ORDER BY id
        """,
        (meeting_id,),
    ).fetchall()

    if not claim_rows:
        conn.close()
        return {"created": 0, "skipped_already_linked": 0, "skipped_no_claims": 1}

    # Compute the starting ordinal for the new Commit_P rows so they
    # don't collide with existing Motion/Vote ordinals. Per SPEC § Node
    # types, ordinal is "monotonic within meeting" — we sit AFTER the
    # highest existing ordinal across all node_types so the IR list
    # mode's per-kind ordinaling stays consistent.
    max_ord_row = cursor.execute(
        "SELECT COALESCE(MAX(ordinal), 0) AS max_ord "
        "FROM transcript_nodes "
        "WHERE meeting_id = ? AND node_type = 'Commit_P'",
        (meeting_id,),
    ).fetchone()
    next_ord = (max_ord_row["max_ord"] if max_ord_row else 0) + 1

    created = 0
    skipped_already_linked = 0

    for claim in claim_rows:
        if claim["source_node_id"] is not None:
            skipped_already_linked += 1
            continue

        # Build the Commit_P typed_fields per SPEC § Node types row 5.
        try:
            topic_tags = json.loads(claim["topic_tags"]) if claim["topic_tags"] else []
        except (json.JSONDecodeError, TypeError):
            topic_tags = []

        # summary_sentence is the IR-only "one-line gist" field. For
        # backfilled rows, derive it from the claim_text (first sentence
        # / first 120 chars) — operator can edit later when the IR-edit
        # menu lands per SPEC § Future-option note.
        claim_text = (claim["claim_text"] or "").strip()
        summary = _derive_summary_sentence(claim_text)

        typed_fields = json.dumps({
            "summary_sentence": summary,
            "claim_text": claim_text,
            "claim_type": claim["claim_type"],
            "topic_tags": topic_tags,
            "time_horizon_months": claim["time_horizon_months"],
            "context": claim["context"],
            "expected_outcome": claim["expected_outcome"],
            "confidence": claim["confidence"],
            "tracked_claim_id": claim["id"],
        }, ensure_ascii=False)

        # transcript_span_text: use the claim_text itself (per the IR
        # contract — span is the textual extent of the node).
        span_text = claim_text or summary or "(unstated)"

        if dry_run:
            logger.info(
                "backfill (dry-run): would create Commit_P for tracked_claim %s "
                "(speaker_id=%s, summary=%r)",
                claim["id"], claim["member_id"], summary[:60],
            )
            created += 1
            continue

        cursor.execute(
            """
            INSERT INTO transcript_nodes (
                meeting_id, ordinal,
                audio_offset_seconds, audio_duration_seconds,
                speaker_id, speaker_name,
                transcript_span_text, word_timings,
                node_type, typed_fields,
                parser_model, parser_confidence,
                parser_ran_at, parent_node_id
            ) VALUES (
                ?, ?,
                NULL, NULL,
                ?, NULL,
                ?, NULL,
                'Commit_P', ?,
                ?, NULL,
                CURRENT_TIMESTAMP, NULL
            )
            """,
            (
                meeting_id, next_ord,
                claim["member_id"],
                span_text,
                typed_fields,
                _COMPILER_PARSER_MODEL_COMMIT_P_BACKFILL,
            ),
        )
        new_node_id = cursor.lastrowid
        next_ord += 1

        cursor.execute(
            "UPDATE tracked_claims SET source_node_id = ? WHERE id = ?",
            (new_node_id, claim["id"]),
        )
        created += 1
        logger.info(
            "backfill: created Commit_P node id=%s from tracked_claim %s "
            "(summary=%r)",
            new_node_id, claim["id"], summary[:60],
        )

    if not dry_run:
        conn.commit()
    conn.close()

    return {
        "created": created,
        "skipped_already_linked": skipped_already_linked,
        "skipped_no_claims": 0,
    }


def _derive_summary_sentence(claim_text: str) -> str:
    """Cheap one-line summary derivation for backfilled Commit_P nodes.
    Trims to ~120 chars at the first sentence boundary if available."""
    s = (claim_text or "").strip()
    if not s:
        return ""
    # First sentence break
    for sep in (". ", "; ", " — ", " - "):
        idx = s.find(sep)
        if 20 <= idx <= 120:
            return s[:idx].strip()
    if len(s) <= 120:
        return s
    return s[:117].rstrip() + "…"


# ── Vote → Motion responds_to inference ───────────────────────────────


def _match_vote_to_motion(
    vote: Dict,
    motions: List[Dict],
) -> Optional[Tuple[Dict, float, str]]:
    """For a Vote node, find the best-matching Motion node in the same
    meeting. Returns (motion, confidence, match_reason) or None.

    Primary signal: agenda-item key prefix (Item 2E ↔ Item 2E).
    Fallback: token-set Jaccard similarity on combined text.
    """
    vote_tf = vote.get("typed_fields") or {}
    vote_agenda_key = _extract_agenda_key(vote_tf.get("agenda_item"))

    # Strong primary match: agenda-key prefix. Council uses "Item NNN"
    # identifiers verbatim, so this is the highest-confidence signal we
    # have. If multiple Motions share an agenda key, prefer the one
    # whose token-jaccard against the Vote is highest.
    if vote_agenda_key:
        agenda_matches = []
        for m in motions:
            m_tf = m.get("typed_fields") or {}
            m_key = _extract_agenda_key(m_tf.get("agenda_item"))
            if m_key == vote_agenda_key:
                jacc = _token_jaccard(
                    _vote_text_for_match(vote_tf),
                    _motion_text_for_match(m_tf),
                )
                agenda_matches.append((m, jacc))
        if agenda_matches:
            # Pick the highest-jaccard tiebreaker; confidence stays at
            # _AGENDA_KEY_CONFIDENCE (the agenda match is the load-
            # bearing signal, not the jaccard).
            best = max(agenda_matches, key=lambda x: x[1])
            return (best[0], _AGENDA_KEY_CONFIDENCE, f"agenda_key={vote_agenda_key}")

    # Fallback: token-jaccard against all motions; require best score
    # above threshold.
    best_motion: Optional[Dict] = None
    best_score = 0.0
    vote_text = _vote_text_for_match(vote_tf)
    for m in motions:
        m_tf = m.get("typed_fields") or {}
        score = _token_jaccard(vote_text, _motion_text_for_match(m_tf))
        if score > best_score:
            best_score = score
            best_motion = m

    if best_motion is not None and best_score >= _RESPONDS_MIN_CONFIDENCE:
        return (best_motion, best_score, f"jaccard={best_score:.2f}")
    return None


def infer_responds_to_edges(
    meeting_id: int, *, dry_run: bool = False
) -> Dict:
    """Infer Vote → Motion `responds_to` edges for the meeting.

    Idempotent: deletes existing responds_to edges sourced from this
    meeting's Vote nodes before inserting new ones. Other edge_types
    are preserved.

    Returns: {created, skipped_no_match, edges: [{source_id, target_id,
    confidence, reason}, ...]}
    """
    conn = get_connection()
    cursor = conn.cursor()

    motions, votes, _ = _load_nodes_by_type(cursor, meeting_id)

    if not votes or not motions:
        conn.close()
        return {
            "created": 0,
            "skipped_no_match": len(votes),
            "edges": [],
        }

    # Wipe existing responds_to edges whose source is a Vote in this
    # meeting (idempotent re-run; preserves edges from future Second →
    # Motion responds_to once those land).
    vote_ids = [v["id"] for v in votes]
    if vote_ids:
        placeholders = ",".join("?" * len(vote_ids))
        cursor.execute(
            f"DELETE FROM transcript_edges "
            f"WHERE edge_type = 'responds_to' AND source_node_id IN ({placeholders})",
            vote_ids,
        )

    created = 0
    skipped = 0
    edges: List[Dict] = []

    for vote in votes:
        match = _match_vote_to_motion(vote, motions)
        if not match:
            skipped += 1
            continue
        motion, confidence, reason = match
        edges.append({
            "source_node_id": vote["id"],
            "target_node_id": motion["id"],
            "confidence": round(confidence, 3),
            "reason": reason,
        })
        if dry_run:
            logger.info(
                "responds_to (dry-run): Vote#%s → Motion#%s (%s)",
                vote["ordinal"], motion["ordinal"], reason,
            )
            created += 1
            continue
        cursor.execute(
            """
            INSERT INTO transcript_edges (
                source_node_id, target_node_id, edge_type,
                parser_confidence, parser_ran_at
            ) VALUES (?, ?, 'responds_to', ?, CURRENT_TIMESTAMP)
            """,
            (vote["id"], motion["id"], confidence),
        )
        created += 1
        logger.info(
            "responds_to: Vote id=%s → Motion id=%s (%s)",
            vote["id"], motion["id"], reason,
        )

    if not dry_run:
        conn.commit()
    conn.close()
    return {"created": created, "skipped_no_match": skipped, "edges": edges}


# ── Vote → Commit_P satisfies inference ───────────────────────────────


def _match_vote_to_commit_p(
    vote: Dict,
    commit_ps: List[Dict],
) -> List[Tuple[Dict, float, str]]:
    """For a Vote node with vote_result='passed', find Commit_P nodes
    the vote operationalizes. Returns a list of (commit_p, confidence,
    reason) — a single vote may satisfy multiple commitments (e.g., a
    motion that combines two reassurances)."""
    vote_tf = vote.get("typed_fields") or {}
    if (vote_tf.get("vote_result") or "").lower() != "passed":
        return []

    vote_agenda_key = _extract_agenda_key(vote_tf.get("agenda_item"))
    vote_text = _vote_text_for_match(vote_tf)

    matches: List[Tuple[Dict, float, str]] = []
    for cp in commit_ps:
        cp_tf = cp.get("typed_fields") or {}
        # Agenda-key match isn't typically available for Commit_P (the
        # commit isn't tied to a specific agenda Item in the typed
        # fields), but if it ever is, prefer it.
        cp_agenda_key = _extract_agenda_key(cp_tf.get("agenda_item"))
        if vote_agenda_key and cp_agenda_key and vote_agenda_key == cp_agenda_key:
            matches.append((cp, _AGENDA_KEY_CONFIDENCE, f"agenda_key={vote_agenda_key}"))
            continue

        cp_text = _commit_text_for_match(cp)
        # Vote-side containment: directional "is this vote's content
        # inside this commitment?". See _SATISFIES_MIN_CONFIDENCE comment
        # for the rationale (Jaccard's union-denominator buries the
        # signal when Commit_P text is verbose; containment preserves
        # the directional structure satisfies actually asks about).
        score = _vote_side_containment(vote_text, cp_text)
        if score >= _SATISFIES_MIN_CONFIDENCE:
            matches.append((cp, score, f"vote_containment={score:.2f}"))

    return matches


def infer_satisfies_edges(
    meeting_id: int, *, dry_run: bool = False
) -> Dict:
    """Infer Vote → Commit_P `satisfies` edges for the meeting.

    Only Vote nodes with vote_result='passed' are sources (the
    Heap-allocation-freed semantic). Each Vote may satisfy 0+ Commit_P
    nodes; we don't pick a single "best" because a combined motion can
    legitimately satisfy multiple separately-tracked commitments.

    Idempotent: wipes existing satisfies edges whose source is a Vote in
    this meeting before re-inserting.

    Returns: {created, skipped_no_passed_votes, skipped_no_match, edges}
    """
    conn = get_connection()
    cursor = conn.cursor()

    _motions, votes, commit_ps = _load_nodes_by_type(cursor, meeting_id)

    if not votes:
        conn.close()
        return {
            "created": 0,
            "skipped_no_passed_votes": 0,
            "skipped_no_match": 0,
            "edges": [],
        }

    vote_ids = [v["id"] for v in votes]
    if vote_ids:
        placeholders = ",".join("?" * len(vote_ids))
        cursor.execute(
            f"DELETE FROM transcript_edges "
            f"WHERE edge_type = 'satisfies' AND source_node_id IN ({placeholders})",
            vote_ids,
        )

    if not commit_ps:
        conn.commit()
        conn.close()
        return {
            "created": 0,
            "skipped_no_passed_votes": sum(
                1 for v in votes
                if ((v.get("typed_fields") or {}).get("vote_result") or "").lower() != "passed"
            ),
            "skipped_no_match": sum(
                1 for v in votes
                if ((v.get("typed_fields") or {}).get("vote_result") or "").lower() == "passed"
            ),
            "edges": [],
        }

    created = 0
    skipped_not_passed = 0
    skipped_no_match = 0
    edges: List[Dict] = []

    for vote in votes:
        vote_tf = vote.get("typed_fields") or {}
        if (vote_tf.get("vote_result") or "").lower() != "passed":
            skipped_not_passed += 1
            continue

        matches = _match_vote_to_commit_p(vote, commit_ps)
        if not matches:
            skipped_no_match += 1
            continue

        for commit_p, confidence, reason in matches:
            edges.append({
                "source_node_id": vote["id"],
                "target_node_id": commit_p["id"],
                "confidence": round(confidence, 3),
                "reason": reason,
            })
            if dry_run:
                logger.info(
                    "satisfies (dry-run): Vote#%s → Commit_P#%s (%s)",
                    vote["ordinal"], commit_p["ordinal"], reason,
                )
                created += 1
                continue
            cursor.execute(
                """
                INSERT INTO transcript_edges (
                    source_node_id, target_node_id, edge_type,
                    parser_confidence, parser_ran_at
                ) VALUES (?, ?, 'satisfies', ?, CURRENT_TIMESTAMP)
                """,
                (vote["id"], commit_p["id"], confidence),
            )
            created += 1
            logger.info(
                "satisfies: Vote id=%s → Commit_P id=%s (%s)",
                vote["id"], commit_p["id"], reason,
            )

    if not dry_run:
        conn.commit()
    conn.close()
    return {
        "created": created,
        "skipped_no_passed_votes": skipped_not_passed,
        "skipped_no_match": skipped_no_match,
        "edges": edges,
    }


def _load_nodes_by_type(
    cursor, meeting_id: int
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Load this meeting's Motion, Vote, and Commit_P nodes with
    typed_fields already parsed. Returns (motions, votes, commit_ps).

    Use `_load_all_compiler_nodes` for the broader set that includes
    AgendaTransition + Second (needed for parent_node_id backfill and
    Second→Motion responds_to inference)."""
    rows = cursor.execute(
        """
        SELECT id, ordinal, node_type, typed_fields, transcript_span_text,
               speaker_id, speaker_name
        FROM transcript_nodes
        WHERE meeting_id = ?
        AND node_type IN ('Motion', 'Vote', 'Commit_P')
        ORDER BY node_type, ordinal
        """,
        (meeting_id,),
    ).fetchall()
    motions: List[Dict] = []
    votes: List[Dict] = []
    commit_ps: List[Dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["typed_fields"] = json.loads(d["typed_fields"]) if d["typed_fields"] else {}
        except (json.JSONDecodeError, TypeError):
            d["typed_fields"] = {}
        if d["node_type"] == "Motion":
            motions.append(d)
        elif d["node_type"] == "Vote":
            votes.append(d)
        elif d["node_type"] == "Commit_P":
            commit_ps.append(d)
    return motions, votes, commit_ps


def _load_seconds_and_transitions(
    cursor, meeting_id: int
) -> Tuple[List[Dict], List[Dict]]:
    """Load this meeting's Second + AgendaTransition nodes with
    typed_fields parsed. Returns (seconds, agenda_transitions)."""
    rows = cursor.execute(
        """
        SELECT id, ordinal, node_type, typed_fields, transcript_span_text,
               speaker_id, speaker_name
        FROM transcript_nodes
        WHERE meeting_id = ?
        AND node_type IN ('Second', 'AgendaTransition')
        ORDER BY node_type, ordinal
        """,
        (meeting_id,),
    ).fetchall()
    seconds: List[Dict] = []
    transitions: List[Dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["typed_fields"] = json.loads(d["typed_fields"]) if d["typed_fields"] else {}
        except (json.JSONDecodeError, TypeError):
            d["typed_fields"] = {}
        if d["node_type"] == "Second":
            seconds.append(d)
        elif d["node_type"] == "AgendaTransition":
            transitions.append(d)
    return seconds, transitions


# ── Second → Motion responds_to inference ─────────────────────────────


def _second_text_for_match(second_typed_fields: Dict) -> str:
    """Compose the Second's text for matching against a Motion. Mirrors
    `_vote_text_for_match` shape — same field surface, different node
    type, so the matcher's behavior is symmetric for both procedural
    responders (Vote and Second)."""
    parts: List[str] = []
    for k in ("motion_reference", "summary_sentence", "agenda_item"):
        v = second_typed_fields.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " ".join(parts)


def _match_second_to_motion(
    second: Dict,
    motions: List[Dict],
) -> Optional[Tuple[Dict, float, str]]:
    """For a Second node, find the best-matching Motion node in the
    same meeting. Same matcher shape as `_match_vote_to_motion` —
    agenda-key prefix is the primary signal; token-Jaccard fallback
    catches paraphrase drift when one side lacks an agenda key."""
    s_tf = second.get("typed_fields") or {}
    s_agenda_key = _extract_agenda_key(s_tf.get("agenda_item"))

    if s_agenda_key:
        agenda_matches = []
        for m in motions:
            m_tf = m.get("typed_fields") or {}
            m_key = _extract_agenda_key(m_tf.get("agenda_item"))
            if m_key == s_agenda_key:
                jacc = _token_jaccard(
                    _second_text_for_match(s_tf),
                    _motion_text_for_match(m_tf),
                )
                agenda_matches.append((m, jacc))
        if agenda_matches:
            best = max(agenda_matches, key=lambda x: x[1])
            return (best[0], _AGENDA_KEY_CONFIDENCE, f"agenda_key={s_agenda_key}")

    # Fallback: token-jaccard against all motions
    best_motion: Optional[Dict] = None
    best_score = 0.0
    second_text = _second_text_for_match(s_tf)
    for m in motions:
        m_tf = m.get("typed_fields") or {}
        score = _token_jaccard(second_text, _motion_text_for_match(m_tf))
        if score > best_score:
            best_score = score
            best_motion = m

    if best_motion is not None and best_score >= _RESPONDS_MIN_CONFIDENCE:
        return (best_motion, best_score, f"jaccard={best_score:.2f}")
    return None


def infer_responds_to_seconds_edges(
    meeting_id: int, *, dry_run: bool = False
) -> Dict:
    """Infer Second → Motion `responds_to` edges for the meeting.

    Idempotent: deletes existing responds_to edges sourced from this
    meeting's Second nodes before inserting new ones. Other edge_types
    are preserved (in particular, Vote → Motion responds_to created by
    infer_responds_to_edges stays untouched — both functions wipe only
    edges from their own source-node set).
    """
    conn = get_connection()
    cursor = conn.cursor()

    seconds, _transitions = _load_seconds_and_transitions(cursor, meeting_id)
    motions, _votes, _commit_ps = _load_nodes_by_type(cursor, meeting_id)

    if not seconds or not motions:
        conn.close()
        return {
            "created": 0,
            "skipped_no_match": len(seconds),
            "edges": [],
        }

    second_ids = [s["id"] for s in seconds]
    if second_ids:
        placeholders = ",".join("?" * len(second_ids))
        cursor.execute(
            f"DELETE FROM transcript_edges "
            f"WHERE edge_type = 'responds_to' AND source_node_id IN ({placeholders})",
            second_ids,
        )

    created = 0
    skipped = 0
    edges: List[Dict] = []

    for second in seconds:
        match = _match_second_to_motion(second, motions)
        if not match:
            skipped += 1
            continue
        motion, confidence, reason = match
        edges.append({
            "source_node_id": second["id"],
            "target_node_id": motion["id"],
            "confidence": round(confidence, 3),
            "reason": reason,
        })
        if dry_run:
            logger.info(
                "responds_to (dry-run): Second#%s → Motion#%s (%s)",
                second["ordinal"], motion["ordinal"], reason,
            )
            created += 1
            continue
        cursor.execute(
            """
            INSERT INTO transcript_edges (
                source_node_id, target_node_id, edge_type,
                parser_confidence, parser_ran_at
            ) VALUES (?, ?, 'responds_to', ?, CURRENT_TIMESTAMP)
            """,
            (second["id"], motion["id"], confidence),
        )
        created += 1
        logger.info(
            "responds_to: Second id=%s → Motion id=%s (%s)",
            second["id"], motion["id"], reason,
        )

    if not dry_run:
        conn.commit()
    conn.close()
    return {"created": created, "skipped_no_match": skipped, "edges": edges}


# ── AgendaTransition parent_node_id backfill ──────────────────────────


def backfill_parent_node_ids(
    meeting_id: int, *, dry_run: bool = False
) -> Dict:
    """Assign `parent_node_id` to Motion / Vote / Commit_P / Second
    nodes based on the AgendaTransition with the matching agenda-item
    key. Implements SPEC § Decision #2 (layered abstraction): each
    procedural node hangs under the AgendaTransition that opened its
    agenda item.

    Matching strategy:
      - Extract agenda_key from each procedural node's typed_fields
        (Item 2E / 4A / etc.)
      - For each child node, find the LATEST AgendaTransition (by
        ordinal) whose agenda_key matches. "Latest" because public
        hearings may have multiple Open/Take-Up/Close transitions for
        the same item; the most-recent one BEFORE or AT the child's
        ordinal is the right parent.

    Idempotent: re-runs can update parent_node_id (the SQL is UPDATE,
    not INSERT-then-SET).

    Returns: {assigned, skipped_no_key, skipped_no_parent, by_type}
    """
    conn = get_connection()
    cursor = conn.cursor()

    motions, votes, commit_ps = _load_nodes_by_type(cursor, meeting_id)
    seconds, transitions = _load_seconds_and_transitions(cursor, meeting_id)

    if not transitions:
        conn.close()
        return {
            "assigned": 0,
            "skipped_no_key": 0,
            "skipped_no_parent": 0,
            "by_type": {},
        }

    # Index transitions by agenda_key for cheap lookup. For each key,
    # keep the LIST of transitions sorted by ordinal — multiple transitions
    # per key happen when public hearings have separate Open/Take-Up/Close
    # transitions.
    transitions_by_key: Dict[str, List[Dict]] = {}
    for t in transitions:
        tf = t.get("typed_fields") or {}
        key = _extract_agenda_key(tf.get("agenda_item")) or _extract_agenda_key(
            tf.get("agenda_item_number")
        )
        if not key:
            # An AgendaTransition without a key (e.g., adjournment) can
            # still be a parent for orphan nodes via fallback — but for
            # now skip indexing it.
            continue
        transitions_by_key.setdefault(key, []).append(t)
    for key in transitions_by_key:
        transitions_by_key[key].sort(key=lambda x: x["ordinal"])

    assigned = 0
    skipped_no_key = 0
    skipped_no_parent = 0
    by_type: Dict[str, int] = {}

    def _assign(child: Dict) -> None:
        nonlocal assigned, skipped_no_key, skipped_no_parent
        c_tf = child.get("typed_fields") or {}
        c_key = _extract_agenda_key(c_tf.get("agenda_item"))
        if not c_key:
            skipped_no_key += 1
            return
        candidates = transitions_by_key.get(c_key)
        if not candidates:
            # Sub-item fallback — "4A" hangs under the broader "4"
            # transition when no "4A" transition exists.
            fallback_key = _agenda_key_parent_fallback(c_key)
            if fallback_key:
                candidates = transitions_by_key.get(fallback_key)
        if not candidates:
            skipped_no_parent += 1
            return
        # Pick the FIRST transition for this key — the "Take up X" or
        # "Open X" transition that introduces the agenda item. Subsequent
        # transitions for the same item (Close-the-public-hearing) are
        # for closing, not introducing, the deliberation.
        parent = candidates[0]
        parent_id = parent["id"]
        if dry_run:
            logger.info(
                "parent_node_id (dry-run): %s id=%s → AgendaTransition id=%s "
                "(agenda_key=%s)",
                child["node_type"], child["id"], parent_id, c_key,
            )
            assigned += 1
            by_type[child["node_type"]] = by_type.get(child["node_type"], 0) + 1
            return
        # Read prior parent_node_id so we can count "newly assigned" vs
        # "already set" deterministically — don't trust cursor.rowcount,
        # which SQLite's Python driver can return as -1 on UPDATEs with
        # composite WHERE clauses (silent miscount).
        row = cursor.execute(
            "SELECT parent_node_id FROM transcript_nodes WHERE id = ?",
            (child["id"],),
        ).fetchone()
        prior_parent = row["parent_node_id"] if row else None
        cursor.execute(
            "UPDATE transcript_nodes SET parent_node_id = ? WHERE id = ?",
            (parent_id, child["id"]),
        )
        if prior_parent != parent_id:
            assigned += 1
            by_type[child["node_type"]] = by_type.get(child["node_type"], 0) + 1

    for child in (*motions, *votes, *commit_ps, *seconds):
        _assign(child)

    if not dry_run:
        conn.commit()
    conn.close()
    return {
        "assigned": assigned,
        "skipped_no_key": skipped_no_key,
        "skipped_no_parent": skipped_no_parent,
        "by_type": by_type,
    }


# ── Top-level orchestrator ────────────────────────────────────────────


def infer_all_edges(
    meeting_id: int, *, dry_run: bool = False, do_backfill: bool = True
) -> Dict:
    """Run the full edge-inference sequence for a meeting:
      1. Backfill hand-seeded tracked_claims → transcript_nodes Commit_P
         (per SPEC § Relationship to existing tracked_claims projection)
      2. Backfill parent_node_id from AgendaTransition matches (SPEC
         Decision #2 layered abstraction)
      3. Vote → Motion responds_to edges
      4. Second → Motion responds_to edges
      5. Vote-passed → Commit_P satisfies edges

    Returns a dict combining each step's output.
    """
    result: Dict = {"meeting_id": meeting_id, "dry_run": dry_run}

    if do_backfill:
        result["backfill"] = backfill_commit_p_nodes(meeting_id, dry_run=dry_run)
        result["parent_backfill"] = backfill_parent_node_ids(meeting_id, dry_run=dry_run)

    result["responds_to_votes"] = infer_responds_to_edges(meeting_id, dry_run=dry_run)
    result["responds_to_seconds"] = infer_responds_to_seconds_edges(meeting_id, dry_run=dry_run)
    result["satisfies"] = infer_satisfies_edges(meeting_id, dry_run=dry_run)
    return result


# ── CLI ───────────────────────────────────────────────────────────────


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Conversational Compiler edge-inference pass for one meeting.",
    )
    parser.add_argument("--meeting-id", type=int, required=True,
                        help="Meeting id to infer edges for (e.g. 101091)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute edges without persisting; log what would be created.")
    parser.add_argument("--no-backfill", action="store_true",
                        help="Skip the tracked_claims → transcript_nodes Commit_P backfill.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose logging at INFO level.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = infer_all_edges(
        args.meeting_id,
        dry_run=args.dry_run,
        do_backfill=not args.no_backfill,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

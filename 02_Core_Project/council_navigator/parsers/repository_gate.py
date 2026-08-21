"""repository_gate — D-095 / D-006 repository deposit gate data access.

Per [D-095](../../01_Project_Overview/DECISIONS.md#d-095) Decentralized
Creator Network + [D-006](../../01_Project_Overview/DECISIONS.md#d-006)
publication gate (extending to repository deposits): every asset
destined for the static-asset repository carries a repository_status
value (draft / pending_owner_review / approved / withdrawn). Approval
is owner-only. Only approved assets become available to creators.

The repository_assets table is a polymorphic registry: each row
references its source via (source_type, source_id) so a single operator
queue + a single faucet-decision log span Studio outputs
(notebook_outputs), member quotes (member_quotes), and future asset
classes without per-class status columns.

Helpers:
  - enqueue_repository_asset(source_type, source_id, source_meeting_id,
        asset_type, asset_metadata, status=pending_owner_review)
  - approve_repository_asset(asset_id, approved_by)
  - reject_repository_asset(asset_id, rejected_by, reason)
  - withdraw_repository_asset(asset_id, withdrawn_by, reason)
  - get_repository_asset(asset_id)
  - list_pending_review_assets(limit=200)
  - list_recently_filtered(limit=50)
  - count_pending_review_assets()

State transitions enforced by the helpers (the DB CHECK constraint is
the structural floor; the helpers add the policy floor of legal
transitions):
  - approve_repository_asset: pending_owner_review -> approved
  - reject_repository_asset:  pending_owner_review -> draft
  - withdraw_repository_asset: approved -> withdrawn

Per [D-100](../../01_Project_Overview/DECISIONS.md#d-100): defensive
data-access primitives. No LLM calls.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

# Dual-import shim. See parsers/account_system.py + the
# [[parsers-dual-import-shim]] memory entry — pytest runs from
# Navigator/ cwd (`from parsers import database`); Flask runs from
# parsers/ cwd (`import database`).
try:
    from parsers import database  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — Flask sibling-import fallback
    import database  # type: ignore[no-redef]


SourceType = Literal["notebook_output", "member_quote", "clip_file", "other"]
AssetType = Literal["clip", "summary", "infographic", "audio", "video", "other"]
RepoStatus = Literal["draft", "pending_owner_review", "approved", "withdrawn"]
FilterAction = Literal["reject", "withdraw"]


_LEGAL_SOURCE_TYPES = ("notebook_output", "member_quote", "clip_file", "other")
_LEGAL_ASSET_TYPES = ("clip", "summary", "infographic", "audio", "video", "other")
_LEGAL_INITIAL_STATUS = ("draft", "pending_owner_review")


# Canonical map from notebook_outputs.output_type to repository_assets.
# asset_type. Source of truth for both the seed script
# (parsers/scripts/enqueue_repository_candidates.py) and the worker.py
# auto-enqueue hook called from save_notebook_output. Output types not
# in this mapping (episode_tagline, episode_tags, council_sentiment,
# suggested_questions, member_attendance, transcript_words,
# tracked_claims, quotes) are either internal display strings or raw
# structured data and do not become repository deposits.
NOTEBOOK_OUTPUT_TO_ASSET_TYPE: dict[str, AssetType] = {
    "synopsis": "summary",
    "newsletter": "summary",
    "key_decisions": "summary",
    "whats_next": "summary",
    "audio_overview": "audio",
    "video_explainer": "video",
    "infographic": "infographic",
}


class AssetNotFoundError(Exception):
    """Raised when a repository_assets row does not exist."""


class IllegalTransitionError(Exception):
    """Raised when an approve/reject/withdraw call targets a row in a
    status that the requested action does not legally move out of.
    The Flask endpoint translates this to HTTP 409."""

    def __init__(self, asset_id: int, current_status: str, action: str) -> None:
        super().__init__(
            f"asset {asset_id} is in status {current_status!r}; cannot {action}"
        )
        self.asset_id = asset_id
        self.current_status = current_status
        self.action = action


# ── Dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RepositoryAsset:
    id: int
    source_type: str
    source_id: int
    source_meeting_id: int
    asset_type: str
    asset_metadata: Optional[dict[str, Any]]
    repository_status: str
    queued_at: str
    approved_at: Optional[str]
    approved_by: Optional[str]
    withdrawn_at: Optional[str]
    withdrawn_reason: Optional[str]
    filter_reason: Optional[str]


@dataclass(frozen=True)
class RepositoryFilterLogEntry:
    id: int
    asset_id: int
    filter_action: str
    filter_reason: str
    filtered_at: str
    filtered_by: Optional[str]


def _row_to_asset(row: Any) -> RepositoryAsset:
    metadata_raw = row["asset_metadata"]
    metadata: Optional[dict[str, Any]] = None
    if metadata_raw:
        try:
            metadata = json.loads(metadata_raw)
        except (TypeError, ValueError):
            logger.warning(
                "repository_assets row %s has non-JSON asset_metadata; "
                "returning raw string under {'raw': ...}",
                row["id"],
            )
            metadata = {"raw": metadata_raw}
    return RepositoryAsset(
        id=row["id"],
        source_type=row["source_type"],
        source_id=row["source_id"],
        source_meeting_id=row["source_meeting_id"],
        asset_type=row["asset_type"],
        asset_metadata=metadata,
        repository_status=row["repository_status"],
        queued_at=row["queued_at"],
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
        withdrawn_at=row["withdrawn_at"],
        withdrawn_reason=row["withdrawn_reason"],
        filter_reason=row["filter_reason"],
    )


# ── Writes ────────────────────────────────────────────────────────────


def enqueue_repository_asset(
    source_type: SourceType,
    source_id: int,
    source_meeting_id: int,
    asset_type: AssetType,
    asset_metadata: Optional[dict[str, Any]] = None,
    *,
    initial_status: RepoStatus = "pending_owner_review",
) -> RepositoryAsset:
    """Insert a repository_assets row. Idempotent against the UNIQUE
    (source_type, source_id, asset_type) constraint — re-enqueueing the
    same (source, asset_type) returns the existing row unchanged.

    initial_status defaults to 'pending_owner_review' (the V0 seed path
    + the future worker.py auto-deposit path both land directly in
    review). 'draft' is allowed when the production pipeline wants a
    holding state before review.
    """
    if source_type not in _LEGAL_SOURCE_TYPES:
        raise ValueError(f"illegal source_type: {source_type!r}")
    if asset_type not in _LEGAL_ASSET_TYPES:
        raise ValueError(f"illegal asset_type: {asset_type!r}")
    if initial_status not in _LEGAL_INITIAL_STATUS:
        raise ValueError(
            f"illegal initial_status: {initial_status!r}; "
            f"only {_LEGAL_INITIAL_STATUS} permitted at enqueue time"
        )

    metadata_text = json.dumps(asset_metadata) if asset_metadata is not None else None

    conn = database.get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT * FROM repository_assets
         WHERE source_type = ? AND source_id = ? AND asset_type = ?
        """,
        (source_type, source_id, asset_type),
    )
    existing = cursor.fetchone()
    if existing is not None:
        asset = _row_to_asset(existing)
        conn.close()
        return asset

    cursor.execute(
        """
        INSERT INTO repository_assets (
            source_type, source_id, source_meeting_id,
            asset_type, asset_metadata, repository_status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source_type,
            source_id,
            source_meeting_id,
            asset_type,
            metadata_text,
            initial_status,
        ),
    )
    asset_id = cursor.lastrowid
    conn.commit()

    cursor.execute("SELECT * FROM repository_assets WHERE id = ?", (asset_id,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        raise RuntimeError(
            f"enqueue_repository_asset({asset_id}): row vanished after insert"
        )
    return _row_to_asset(row)


def approve_repository_asset(asset_id: int, approved_by: str) -> RepositoryAsset:
    """pending_owner_review -> approved. Stamps approved_at + approved_by.
    Idempotent: re-calling on an already-approved row is treated as a
    409 (IllegalTransitionError) — the caller should observe the
    existing approval rather than silently re-stamp.
    """
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM repository_assets WHERE id = ?", (asset_id,)
    )
    row = cursor.fetchone()
    if row is None:
        conn.close()
        raise AssetNotFoundError(f"repository_assets {asset_id} not found")
    if row["repository_status"] != "pending_owner_review":
        conn.close()
        raise IllegalTransitionError(asset_id, row["repository_status"], "approve")

    cursor.execute(
        """
        UPDATE repository_assets
           SET repository_status = 'approved',
               approved_at = CURRENT_TIMESTAMP,
               approved_by = ?
         WHERE id = ?
        """,
        (approved_by, asset_id),
    )
    conn.commit()
    cursor.execute("SELECT * FROM repository_assets WHERE id = ?", (asset_id,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_asset(row)


def reject_repository_asset(
    asset_id: int, rejected_by: str, reason: str
) -> RepositoryAsset:
    """pending_owner_review -> draft. Writes a repository_filter_log row
    with filter_action='reject'. The asset stays in the system at draft
    so a future re-submission can flip it back to pending_owner_review.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reject_repository_asset requires a non-empty reason")

    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM repository_assets WHERE id = ?", (asset_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        raise AssetNotFoundError(f"repository_assets {asset_id} not found")
    if row["repository_status"] != "pending_owner_review":
        conn.close()
        raise IllegalTransitionError(asset_id, row["repository_status"], "reject")

    cursor.execute(
        """
        UPDATE repository_assets
           SET repository_status = 'draft',
               filter_reason = ?
         WHERE id = ?
        """,
        (reason, asset_id),
    )
    cursor.execute(
        """
        INSERT INTO repository_filter_log (
            asset_id, filter_action, filter_reason, filtered_by
        ) VALUES (?, 'reject', ?, ?)
        """,
        (asset_id, reason, rejected_by),
    )
    conn.commit()
    cursor.execute("SELECT * FROM repository_assets WHERE id = ?", (asset_id,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_asset(row)


def withdraw_repository_asset(
    asset_id: int, withdrawn_by: str, reason: str
) -> RepositoryAsset:
    """approved -> withdrawn. Writes a repository_filter_log row with
    filter_action='withdraw'. The asset stays in the system at withdrawn
    so the audit trail is preserved + the filter log explains why a
    previously-public asset disappeared.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("withdraw_repository_asset requires a non-empty reason")

    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM repository_assets WHERE id = ?", (asset_id,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        raise AssetNotFoundError(f"repository_assets {asset_id} not found")
    if row["repository_status"] != "approved":
        conn.close()
        raise IllegalTransitionError(asset_id, row["repository_status"], "withdraw")

    cursor.execute(
        """
        UPDATE repository_assets
           SET repository_status = 'withdrawn',
               withdrawn_at = CURRENT_TIMESTAMP,
               withdrawn_reason = ?
         WHERE id = ?
        """,
        (reason, asset_id),
    )
    cursor.execute(
        """
        INSERT INTO repository_filter_log (
            asset_id, filter_action, filter_reason, filtered_by
        ) VALUES (?, 'withdraw', ?, ?)
        """,
        (asset_id, reason, withdrawn_by),
    )
    conn.commit()
    cursor.execute("SELECT * FROM repository_assets WHERE id = ?", (asset_id,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_asset(row)


# ── Reads ─────────────────────────────────────────────────────────────


def get_repository_asset(asset_id: int) -> Optional[RepositoryAsset]:
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM repository_assets WHERE id = ?", (asset_id,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_asset(row) if row is not None else None


# Sentinel patterns that identify legacy NotebookLM meta-responses —
# the empty-notebook / "It looks like you're interested in..." boilerplate
# the unofficial wrapper used to return when the source set wasn't ingested
# yet OR when the session was in a degraded state. Ported from the
# BroadcastPage.tsx `looksLikeLegacyNotebookLMArtifact` so the review queue
# applies the same gate the broadcast renderer already does. These outputs
# were generated under the pre-D-126 NotebookLM architecture that's been
# retired from V1 — they have no place in the operator review queue.
_LEGACY_NOTEBOOKLM_ARTIFACT_PATTERNS = [
    re.compile(r"notebooklm query failed", re.IGNORECASE),
    re.compile(r"chat request was (rate limited|rejected)", re.IGNORECASE),
    re.compile(r"^\s*(welcome[!.,:]|hi[!.,:]|hello[!.,:])", re.IGNORECASE),
    # The conversational openers NotebookLM produces when the source set
    # isn't ingested. Relaxed to match all variants observed in pre-D-126
    # cached rows: "you're interested/working/ready/getting", "you've
    # started/created/begun", or just bare "I see you" / "It looks like you".
    re.compile(r"(it looks like|i see) you(['’]ve)? (started|created|begun) a notebook", re.IGNORECASE),
    re.compile(r"(it looks like|i see) you(['’]?re)? ?(interested|working|ready|getting|exploring)", re.IGNORECASE),
    re.compile(r"to your notebook", re.IGNORECASE),
    re.compile(r"source panel on the left", re.IGNORECASE),
    re.compile(r"polished artifacts", re.IGNORECASE),
    re.compile(r"briefing doc.*study guide", re.IGNORECASE),
    re.compile(r"(would you like me to|i can help find them)", re.IGNORECASE),
    re.compile(r"upload(ing)? the (meeting transcript|agenda)", re.IGNORECASE),
    re.compile(r"first need to add", re.IGNORECASE),
    re.compile(r"your notebook is empty", re.IGNORECASE),
    re.compile(r"to generate (a high-quality output|the insights)", re.IGNORECASE),
]


def is_legacy_notebooklm_artifact(text: Optional[str]) -> bool:
    """True iff `text` matches a known legacy NotebookLM meta-response
    pattern (empty-notebook boilerplate, rate-limit messages, etc.).
    Returns False for None / empty strings.

    Used by `list_pending_review_assets` to gate legacy NotebookLM
    outputs that were enqueued before D-126 retired NotebookLM from
    V1 — they're factually noise in the operator queue and the
    BroadcastPage already gates them via the TS sibling sentinel.
    """
    if not text:
        return False
    sample = text[:500]
    return any(p.search(sample) for p in _LEGACY_NOTEBOOKLM_ARTIFACT_PATTERNS)


def list_pending_review_assets(limit: int = 200) -> list[dict[str, Any]]:
    """Return rows ready for the operator's repository queue. Joins the
    source meeting so the UI can render the city + meeting date without
    a follow-up fetch. Newest queued first.

    The return shape is a plain dict (not RepositoryAsset) so the joined
    meeting cols come along — the Flask endpoint forwards it verbatim.

    Legacy NotebookLM meta-response rows (pre-D-126 architecture) are
    filtered out via `is_legacy_notebooklm_artifact` on the
    `asset_metadata.preview` field — they remain in the DB at status
    `pending_owner_review` (no destructive change) but don't surface in
    the queue. Count of filtered rows is logged so the suppression
    isn't silent.
    """
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            ra.id,
            ra.source_type,
            ra.source_id,
            ra.source_meeting_id,
            ra.asset_type,
            ra.asset_metadata,
            ra.repository_status,
            ra.queued_at,
            ra.filter_reason,
            m.city_name,
            m.meeting_date,
            m.meeting_title
          FROM repository_assets ra
          JOIN meetings m ON m.id = ra.source_meeting_id
         WHERE ra.repository_status = 'pending_owner_review'
         ORDER BY ra.queued_at DESC
         LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()

    out: list[dict[str, Any]] = []
    suppressed_legacy = 0
    for row in rows:
        metadata_raw = row["asset_metadata"]
        metadata: Optional[dict[str, Any]] = None
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw)
            except (TypeError, ValueError):
                metadata = {"raw": metadata_raw}
        if metadata and is_legacy_notebooklm_artifact(metadata.get("preview")):
            suppressed_legacy += 1
            continue
        out.append(
            {
                "id": row["id"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "source_meeting_id": row["source_meeting_id"],
                "asset_type": row["asset_type"],
                "asset_metadata": metadata,
                "repository_status": row["repository_status"],
                "queued_at": row["queued_at"],
                "filter_reason": row["filter_reason"],
                "city_name": row["city_name"],
                "meeting_date": row["meeting_date"],
                "meeting_title": row["meeting_title"],
            }
        )
    if suppressed_legacy:
        logger.info(
            "list_pending_review_assets: hid %s pre-D-126 legacy "
            "NotebookLM artifacts from the queue (rows remain in DB)",
            suppressed_legacy,
        )
    return out


def list_recently_filtered(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent rejection / withdrawal log entries, joined
    to the asset + meeting so the operator surface can show context
    without per-row roundtrips.
    """
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            f.id AS log_id,
            f.asset_id,
            f.filter_action,
            f.filter_reason,
            f.filtered_at,
            f.filtered_by,
            ra.asset_type,
            ra.source_type,
            ra.repository_status,
            m.city_name,
            m.meeting_date,
            m.meeting_title
          FROM repository_filter_log f
          JOIN repository_assets ra ON ra.id = f.asset_id
          JOIN meetings m ON m.id = ra.source_meeting_id
         ORDER BY f.filtered_at DESC
         LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "log_id": row["log_id"],
            "asset_id": row["asset_id"],
            "filter_action": row["filter_action"],
            "filter_reason": row["filter_reason"],
            "filtered_at": row["filtered_at"],
            "filtered_by": row["filtered_by"],
            "asset_type": row["asset_type"],
            "source_type": row["source_type"],
            "current_status": row["repository_status"],
            "city_name": row["city_name"],
            "meeting_date": row["meeting_date"],
            "meeting_title": row["meeting_title"],
        }
        for row in rows
    ]


def count_pending_review_assets() -> int:
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS n FROM repository_assets "
        "WHERE repository_status = 'pending_owner_review'"
    )
    n = cursor.fetchone()["n"]
    conn.close()
    return int(n)


# ── Worker-side auto-enqueue hook ─────────────────────────────────────


def auto_enqueue_from_notebook_output(
    notebook_output_id: int,
) -> Optional[RepositoryAsset]:
    """After save_notebook_output writes a fresh row, automatically
    deposit it into the repository deposit gate at pending_owner_review
    (if its output_type maps to a repository asset class + the row has
    no error). Returns the resulting RepositoryAsset, or None if the
    row was not eligible.

    Best-effort: every call site should swallow exceptions and let the
    upstream write complete — the seed script
    (parsers/scripts/enqueue_repository_candidates.py) is the fallback
    for any output that slipped past this hook.
    """
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, meeting_id, notebook_id, output_type, content,
               generated_at, error
          FROM notebook_outputs
         WHERE id = ?
        """,
        (notebook_output_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    if row["error"]:
        return None
    asset_type = NOTEBOOK_OUTPUT_TO_ASSET_TYPE.get(row["output_type"])
    if asset_type is None:
        return None

    content: Optional[str] = row["content"]
    preview: Optional[str] = None
    if content:
        flat = " ".join(content.split())
        preview = flat if len(flat) <= 200 else flat[:199].rstrip() + "…"

    metadata: dict[str, Any] = {
        "output_type": row["output_type"],
        "notebook_id": row["notebook_id"],
        "generated_at": row["generated_at"],
        "preview": preview,
    }

    return enqueue_repository_asset(
        source_type="notebook_output",
        source_id=row["id"],
        source_meeting_id=row["meeting_id"],
        asset_type=asset_type,
        asset_metadata=metadata,
        initial_status="pending_owner_review",
    )

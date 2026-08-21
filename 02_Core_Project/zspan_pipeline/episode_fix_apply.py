#!/usr/bin/env python3.11
"""Operator-gated, event-sourced application of episode audit proposals."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping


_PROJECT_DIR = Path(__file__).resolve().parent.parent
_PARSERS_DIR = _PROJECT_DIR / "council_navigator" / "parsers"
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from zspan_pipeline.db_backend import install_db_backend  # noqa: E402

install_db_backend()

from database import (  # noqa: E402
    get_connection,
    get_episode_audit_fix_events,
    get_episode_audit_run,
    save_episode_audit_fix_event,
)
from zspan_pipeline.episode_auditor import (  # noqa: E402
    load_audit_inputs,
    validate_single_proposal,
)


APPLY_ALLOWLIST = frozenset({
    "episode_tagline",
    "synopsis",
    "newsletter",
    "whats_next",
    "council_sentiment",
})

_FIX_EVENT_COLUMNS = (
    "event_id",
    "meeting_id",
    "run_id",
    "proposal_id",
    "disposition",
    "reason",
    "actor",
    "target_output",
    "before_text",
    "after_text",
    "pre_content_sha256",
    "post_content_sha256",
    "validation_json",
    "was_published",
)


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _find_proposal(
    meeting_id: int,
    run_id: str,
    proposal_id: str,
) -> Mapping[str, Any] | None:
    run = get_episode_audit_run(run_id)
    if run is None or run.get("meeting_id") != meeting_id:
        return None
    report = run.get("report")
    if not isinstance(report, Mapping):
        return None
    llm = report.get("llm")
    if not isinstance(llm, Mapping):
        return None
    proposals = llm.get("proposals")
    if not isinstance(proposals, list):
        return None
    for proposal in proposals:
        if (
            isinstance(proposal, Mapping)
            and proposal.get("id") == proposal_id
        ):
            return proposal
    return None


def _event_fields(
    *,
    meeting_id: int,
    run_id: str,
    proposal_id: str,
    disposition: str,
    reason: str | None,
    actor: str,
    proposal: Mapping[str, Any],
    pre_content_sha256: str | None,
    post_content_sha256: str | None,
    validation: Mapping[str, Any] | None,
    was_published: bool | int,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "meeting_id": meeting_id,
        "run_id": run_id,
        "proposal_id": proposal_id,
        "disposition": disposition,
        "reason": reason,
        "actor": actor,
        "target_output": str(proposal.get("target_output") or ""),
        "before_text": str(proposal.get("before") or ""),
        "after_text": str(proposal.get("after") or ""),
        "pre_content_sha256": pre_content_sha256,
        "post_content_sha256": post_content_sha256,
        "validation_json": (
            json.dumps(validation, ensure_ascii=False, sort_keys=True)
            if validation is not None
            else None
        ),
        "was_published": int(bool(was_published)),
    }


def _insert_event(
    conn: sqlite3.Connection,
    event: Mapping[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO episode_audit_fix_events (
            event_id, meeting_id, run_id, proposal_id, disposition,
            reason, actor, target_output, before_text, after_text,
            pre_content_sha256, post_content_sha256, validation_json,
            was_published
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(event[column] for column in _FIX_EVENT_COLUMNS),
    )


def _already_applied(
    meeting_id: int,
    run_id: str,
    proposal_id: str,
) -> bool:
    return any(
        event.get("run_id") == run_id
        and event.get("proposal_id") == proposal_id
        and event.get("disposition") == "applied"
        for event in get_episode_audit_fix_events(meeting_id)
    )


def _current_content(meeting_id: int, target_output: str) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT content
            FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = ?
            """,
            (meeting_id, target_output),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return str(row["content"])


def apply_fix(
    meeting_id: int,
    run_id: str,
    proposal_id: str,
    actor: str,
) -> dict[str, Any]:
    """Validate and atomically apply one direct-row text proposal."""
    proposal = _find_proposal(meeting_id, run_id, proposal_id)
    if proposal is None:
        return {"status": "not_found"}

    if _already_applied(meeting_id, run_id, proposal_id):
        return {"status": "already_applied"}

    target_output = str(proposal.get("target_output") or "")
    if target_output not in APPLY_ALLOWLIST:
        return {"status": "adapter_deferred"}

    inputs = load_audit_inputs(meeting_id)
    city = str(
        inputs.meeting.get("city_name")
        or inputs.meeting.get("city")
        or inputs.meeting.get("municipality")
        or ""
    )
    validation = validate_single_proposal(
        proposal,
        inputs.outputs,
        inputs.transcript_words,
        city,
    )
    original = inputs.outputs.get(target_output, "")
    before = proposal.get("before")
    after = proposal.get("after")
    was_published = inputs.meeting.get("is_published", 0)
    pre_hash = _content_sha256(original)

    if not validation.get("validated"):
        reason = "; ".join(validation.get("validation_errors") or [])
        event = _event_fields(
            meeting_id=meeting_id,
            run_id=run_id,
            proposal_id=proposal_id,
            disposition="apply_failed",
            reason=reason or "validation_failed",
            actor=actor,
            proposal=proposal,
            pre_content_sha256=pre_hash,
            post_content_sha256=pre_hash,
            validation=validation,
            was_published=was_published,
        )
        save_episode_audit_fix_event(**event)
        return {
            "status": "validation_failed",
            "checks": validation.get("checks", {}),
        }

    candidate = original.replace(str(before), str(after), 1)
    post_hash = _content_sha256(candidate)
    applied_event = _event_fields(
        meeting_id=meeting_id,
        run_id=run_id,
        proposal_id=proposal_id,
        disposition="applied",
        reason=None,
        actor=actor,
        proposal=proposal,
        pre_content_sha256=pre_hash,
        post_content_sha256=post_hash,
        validation=validation,
        was_published=was_published,
    )

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT 1
            FROM episode_audit_fix_events
            WHERE run_id = ? AND proposal_id = ? AND disposition = 'applied'
            LIMIT 1
            """,
            (run_id, proposal_id),
        ).fetchone()
        if existing is not None:
            conn.rollback()
            return {"status": "already_applied"}
        cursor = conn.execute(
            """
            UPDATE notebook_outputs
            SET content = ?
            WHERE meeting_id = ?
              AND output_type = ?
              AND content = ?
            """,
            (candidate, meeting_id, target_output, original),
        )
        if cursor.rowcount != 1:
            conn.rollback()
        else:
            _insert_event(conn, applied_event)
            conn.commit()
            return {
                "status": "applied",
                "event_id": applied_event["event_id"],
                "post_content_sha256": post_hash,
                "superseded_run_id": run_id,
            }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    conflicting_content = _current_content(meeting_id, target_output)
    conflict_event = _event_fields(
        meeting_id=meeting_id,
        run_id=run_id,
        proposal_id=proposal_id,
        disposition="apply_failed",
        reason="cas_conflict",
        actor=actor,
        proposal=proposal,
        pre_content_sha256=pre_hash,
        post_content_sha256=(
            _content_sha256(conflicting_content)
            if conflicting_content is not None
            else None
        ),
        validation=validation,
        was_published=was_published,
    )
    save_episode_audit_fix_event(**conflict_event)
    return {"status": "cas_conflict"}


def record_disposition(
    meeting_id: int,
    run_id: str,
    proposal_id: str,
    disposition: str,
    actor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Append an operator rejection or deferral without touching content."""
    if disposition not in {"rejected", "deferred"}:
        raise ValueError("disposition must be rejected or deferred")
    if disposition == "rejected" and not str(reason or "").strip():
        raise ValueError("rejected disposition requires a reason")

    proposal = _find_proposal(meeting_id, run_id, proposal_id)
    if proposal is None:
        return {"status": "not_found"}

    event = _event_fields(
        meeting_id=meeting_id,
        run_id=run_id,
        proposal_id=proposal_id,
        disposition=disposition,
        reason=reason,
        actor=actor,
        proposal=proposal,
        pre_content_sha256=None,
        post_content_sha256=None,
        validation=None,
        was_published=0,
    )
    save_episode_audit_fix_event(**event)
    return {"status": disposition, "event_id": event["event_id"]}

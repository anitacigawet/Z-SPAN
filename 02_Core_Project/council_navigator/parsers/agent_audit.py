"""agent_audit — centralized audit log for the S-004 agent fleet.

Backs the `agent_actions` SQLite table introduced in C2.0 of
[`01_Project_Overview/S008_INPUT_SECURITY_SPEC.md`](../../01_Project_Overview/S008_INPUT_SECURITY_SPEC.md).

Every agent action wrapper calls `record_agent_action` immediately after a
successful POST to Flask so the durable audit trail captures:
- WHO acted (role)
- WHAT action enum was used
- WHICH row was mutated (table + id)
- A SHA-256 hash of the action body — `action_argument_origin` — that
  proves the audit retains the structural shape the agent submitted, so
  later forensics can confirm the wrapper validated the same payload that
  Flask saw.
- Optional reasoning string (caller's brief explanation).
- Optional rung context for orchestrator-class actions (rung_attempted +
  rung_outcome).

Per [D-100](../../01_Project_Overview/DECISIONS.md#d-100), this module is
defensive — it adds an audit row, it does not run any LLM, and it has no
side effects beyond the DB write.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Allowed agent_role values; centralized so adding a new agent requires
# explicit code surgery rather than ad-hoc string mismatches. Public because
# API attribution gates must enforce the exact same vocabulary as audit writes.
KNOWN_ROLES = frozenset({
    "disputed-quotes-reviewer",
    "vocabulary-curator",
    "parser-custodian",
    "content-scout",
    "orchestrator",
    "balance-auditor",
    "pipeline-operator",
    "haiku-html-scraper",
})

# Backward-compatible alias for any existing private-name consumers.
_KNOWN_ROLES = KNOWN_ROLES


def _hash_action_body(body: Any) -> str:
    """SHA-256 hex of the canonical-serialized action body.

    Canonical = JSON dump with sort_keys=True + ensure_ascii=False so the
    same logical body always hashes the same regardless of dict order.
    Strings are NFC-normalized via input_security.primitives.normalize_user_text
    before serialization so equivalent unicode forms collapse to a single
    hash.
    """
    from parsers.input_security.primitives import (  # local import to avoid
        normalize_user_text,                        # circulars at module load
        sha256_content_hash,
    )

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            return normalize_user_text(node)
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    canon = json.dumps(_walk(body), sort_keys=True, ensure_ascii=False)
    return sha256_content_hash(canon)


def record_agent_action(
    *,
    agent_role: str,
    action_name: str,
    action_argument_table: Optional[str] = None,
    action_argument_id: Optional[int] = None,
    action_body: Any = None,
    reasoning: Optional[str] = None,
    rung_attempted: Optional[str] = None,
    rung_outcome: Optional[str] = None,
) -> Optional[int]:
    """Append one row to `agent_actions`. Returns the new row id, or None
    on failure (logged; never raises into the caller's hot path).

    Failure to write the audit row MUST NOT block the caller's primary
    action. The audit log is best-effort observability; the primary
    actions remain the load-bearing path.
    """
    if agent_role not in KNOWN_ROLES:
        logger.warning(
            "agent_audit.record_agent_action: unknown agent_role %r; "
            "audit row not inserted; expected a role from "
            "agent_audit.KNOWN_ROLES", agent_role,
        )
        return None

    try:
        # Local imports keep this helper importable even if database.py is
        # being reloaded mid-process or if a downstream consumer wants to
        # vendor agent_audit without pulling the rest of parsers/.
        from parsers import database  # noqa: PLC0415

        conn = database.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO agent_actions (
                agent_role, action_name,
                action_argument_table, action_argument_id,
                action_argument_origin,
                reasoning,
                rung_attempted, rung_outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_role,
                action_name,
                action_argument_table,
                action_argument_id,
                _hash_action_body(action_body) if action_body is not None else None,
                reasoning,
                rung_attempted,
                rung_outcome,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id
    except Exception as e:  # broad on purpose — audit must never crash callers
        logger.warning(
            "agent_audit.record_agent_action(role=%s, action=%s) failed: %s",
            agent_role, action_name, e,
        )
        return None


def validate_agent_text(
    text: Optional[str],
    *,
    field_name: str,
    max_length: int,
) -> Optional[str]:
    """Defensive validation for free-text fields agents emit.

    Returns the NFC-normalized text on success, raises ValueError on:
    - length cap exceeded
    - bidi controls present
    - fence-marker substring present

    Per the S-008 V0 threat-model surface S-7 acceptance tests, agent-emitted
    text with structural markers is treated as anomalous — a well-behaved
    agent will never emit a fence marker, so its presence is a signal worth
    halting on.

    None input passes through unchanged (callers use this on optional fields).
    """
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError(f"{field_name} must be a string, got {type(text).__name__}")

    from parsers.input_security.primitives import (  # local to avoid circulars
        contains_fence_marker,
        normalize_user_text,
        reject_if_bidi_controls,
    )

    if len(text) > max_length:
        raise ValueError(
            f"{field_name} exceeds max length: {len(text)} > {max_length}"
        )

    reject_if_bidi_controls(text)  # raises UnicodeRejectionError → caller's except

    if contains_fence_marker(text):
        raise ValueError(
            f"{field_name} contains a structural fence marker — agents must "
            f"not emit fence markers in action payloads"
        )

    return normalize_user_text(text)

"""Server-authoritative Librarian synthesis envelopes and relay claims."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
from typing import Any

try:
    from .database import (
        _mark_librarian_event_terminal_failure_in_tx,
        _materialize_librarian_cooldown_expiry,
        get_connection,
    )
except ImportError:  # Direct ``python`` imports from parsers/.
    from database import (
        _mark_librarian_event_terminal_failure_in_tx,
        _materialize_librarian_cooldown_expiry,
        get_connection,
    )


ENVELOPE_VERSION = "envelope-v1"
ENVELOPE_TTL_SECONDS = 600
ENVELOPE_MAX_ATTEMPTS = 2

_DOMAIN = b"zspan:librarian-synthesis-envelope"
_RELAY_TEXT_MAX_BYTES = 1_000_000
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_INVALID_MESSAGE = (
    "The request doesn't match a Librarian question you asked."
)


def _length_prefix(value: bytes) -> bytes:
    return len(value).to_bytes(8, byteorder="big", signed=False)


def compute_envelope_hash(
    system_prompt: str,
    user_message: str,
    envelope_version: str,
) -> str:
    """Hash a domain-separated, length-prefixed envelope byte sequence."""
    if not all(
        isinstance(value, str)
        for value in (system_prompt, user_message, envelope_version)
    ):
        raise TypeError("envelope hash inputs must be strings")
    version_bytes = envelope_version.encode("utf-8")
    system_bytes = system_prompt.encode("utf-8")
    user_bytes = user_message.encode("utf-8")
    material = b"".join((
        _DOMAIN,
        _length_prefix(version_bytes),
        version_bytes,
        _length_prefix(system_bytes),
        system_bytes,
        _length_prefix(user_bytes),
        user_bytes,
    ))
    return hashlib.sha256(material).hexdigest()


def verify_envelope_hash(computed: str, stored: str) -> bool:
    """Compare envelope digests without content-dependent timing."""
    if not isinstance(computed, str) or not isinstance(stored, str):
        return False
    return hmac.compare_digest(computed, stored)


def _chunk_value(chunk: Any, name: str) -> Any:
    if isinstance(chunk, dict):
        return chunk.get(name)
    return getattr(chunk, name)


def _format_timecode(start_seconds: float) -> str:
    whole_seconds = math.floor(start_seconds)
    minutes, seconds = divmod(whole_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def build_synthesis_envelope(
    meeting_id: int,
    canonical_query: str,
    chunks: list[Any],
) -> dict[str, str]:
    """Build the exact prompt strings that OpenAI/Anthropic may receive.

    ``start_seconds`` is formatted here with Python's authoritative
    ``:.1f`` behavior. Clients consume the resulting strings verbatim.
    """
    if isinstance(meeting_id, bool) or not isinstance(meeting_id, int):
        raise TypeError("meeting_id must be an integer")
    if not isinstance(canonical_query, str):
        raise TypeError("canonical_query must be a string")
    if not isinstance(chunks, list):
        raise TypeError("chunks must be a list")

    chunk_blocks: list[str] = []
    for chunk in chunks:
        chunk_index = _chunk_value(chunk, "chunk_index")
        body = _chunk_value(chunk, "body")
        raw_start = _chunk_value(chunk, "start_seconds")
        if isinstance(raw_start, bool) or not isinstance(
            raw_start,
            (int, float),
        ):
            raise ValueError("chunk start_seconds must be a finite number")
        start_seconds = float(raw_start)
        if not math.isfinite(start_seconds) or start_seconds < 0:
            raise ValueError(
                "chunk start_seconds must be finite and nonnegative"
            )
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
            raise TypeError("chunk_index must be an integer")
        if not isinstance(body, str):
            raise TypeError("chunk body must be a string")
        chunk_blocks.append(
            f"[chunk_index={chunk_index} "
            f"timecode={_format_timecode(start_seconds)} "
            f"start_seconds={start_seconds:.1f}]\n{body}"
        )

    chunks_block = "\n\n".join(chunk_blocks)
    user_message = (
        f"CURRENT QUESTION: {canonical_query}\n\n"
        "RETRIEVED CONTEXT — chunks from "
        f"meeting_id={meeting_id}:\n---\n{chunks_block}\n---"
    )

    # The RAG prompt loader remains the single source of truth for both
    # ``recommended_system_prompt`` and the bound synthesis envelope.
    from zspan_pipeline import rag_search

    system_prompt = rag_search.load_prompt_template()
    envelope_hash = compute_envelope_hash(
        system_prompt,
        user_message,
        ENVELOPE_VERSION,
    )
    return {
        "system_prompt": system_prompt,
        "user_message": user_message,
        "envelope_hash": envelope_hash,
        "envelope_version": ENVELOPE_VERSION,
    }


def _failure(
    *,
    reason: str,
    http: int,
    message: str,
    error_type: str,
) -> tuple[bool, dict[str, Any]]:
    return False, {
        "reason": reason,
        "http": http,
        "message": message,
        "type": error_type,
    }


def _invalid(reason: str = "envelope_invalid") -> tuple[bool, dict[str, Any]]:
    return _failure(
        reason=reason,
        http=403,
        message=_INVALID_MESSAGE,
        error_type="envelope_invalid",
    )


def consume_envelope_claim(
    user_id: int,
    run_id: str,
    system_prompt: str,
    user_message: str,
    envelope_version: str,
    provider: str,
) -> tuple[bool, dict[str, Any]]:
    """Atomically claim one of the accepted envelope's two relay attempts.

    ``relay_started_at`` records dispatch claim time, not proof that a
    provider socket opened. A disconnect after this commit consumes a slot.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        return _failure(
            reason="invalid_user_id",
            http=400,
            message="Invalid relay request.",
            error_type="bad_request",
        )
    if not isinstance(run_id, str) or not run_id:
        return _failure(
            reason="missing_run_id",
            http=400,
            message="run_id is required",
            error_type="bad_request",
        )
    if not isinstance(system_prompt, str) or not isinstance(
        user_message,
        str,
    ):
        return _failure(
            reason="invalid_message_type",
            http=400,
            message="system_prompt and user_message must be strings",
            error_type="bad_request",
        )
    if (
        len(system_prompt.encode("utf-8")) > _RELAY_TEXT_MAX_BYTES
        or len(user_message.encode("utf-8")) > _RELAY_TEXT_MAX_BYTES
    ):
        return _failure(
            reason="message_too_large",
            http=400,
            message="Relay message exceeds the 1 MB limit.",
            error_type="bad_request",
        )
    if not isinstance(provider, str) or not provider.startswith(
        ("openai-", "anthropic-"),
    ):
        return _failure(
            reason="unsupported_provider",
            http=400,
            message=(
                "provider is not routable via the Librarian relay; "
                "supported: openai, anthropic"
            ),
            error_type="unsupported_provider",
        )
    if (
        not isinstance(envelope_version, str)
        or envelope_version != ENVELOPE_VERSION
    ):
        return _invalid("envelope_version_mismatch")

    computed_hash = compute_envelope_hash(
        system_prompt,
        user_message,
        envelope_version,
    )

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        _materialize_librarian_cooldown_expiry(
            conn,
            user_id=user_id,
            now_text=now,
        )
        row = conn.execute(
            """
            SELECT gle.event_id,
                   gle.enforcement_epoch_at_decision,
                   gle.envelope_expires_at,
                   gle.synthesis_envelope_hash,
                   gle.envelope_version,
                   COALESCE(gle.relay_attempt_count, 0) AS attempts,
                   gle.terminal_failure_reason,
                   u.librarian_access,
                   u.librarian_enforcement_epoch,
                   COALESCE(las.active_auto_ban, 0) AS active_auto_ban,
                   las.cooldown_until
            FROM librarian_gate_events gle
            JOIN users u ON u.id = gle.user_id
            LEFT JOIN librarian_abuse_state las
                   ON las.user_id = gle.user_id
            WHERE gle.user_id = ?
              AND gle.retrieval_run_id = ?
              AND gle.stencil_result = 'accepted'
            """,
            (user_id, run_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            return _invalid("envelope_not_found")
        if row["terminal_failure_reason"] is not None:
            conn.rollback()
            return _invalid("envelope_terminal")
        epoch_current = (
            row["enforcement_epoch_at_decision"] is not None
            and int(row["enforcement_epoch_at_decision"])
            == int(row["librarian_enforcement_epoch"])
        )
        if (
            row["librarian_access"] != "granted"
            or row["active_auto_ban"]
            or row["cooldown_until"]
            or not epoch_current
        ):
            _mark_librarian_event_terminal_failure_in_tx(
                conn,
                event_id=row["event_id"],
                reason="revoked_before_dispatch",
                now_text=now,
            )
            conn.commit()
            return _failure(
                reason="access_revoked",
                http=409,
                message=(
                    "Librarian access changed before provider dispatch."
                ),
                error_type="admission_state_changed",
            )
        if (
            row["envelope_version"] != ENVELOPE_VERSION
            or row["envelope_version"] != envelope_version
            or not isinstance(row["synthesis_envelope_hash"], str)
            or _SHA256_HEX_RE.fullmatch(
                row["synthesis_envelope_hash"]
            ) is None
        ):
            conn.rollback()
            return _invalid("stored_envelope_invalid")

        expires_at = row["envelope_expires_at"]
        if not isinstance(expires_at, str):
            conn.rollback()
            return _invalid("stored_envelope_invalid")
        if expires_at <= now:
            if int(row["attempts"]) == 0:
                _mark_librarian_event_terminal_failure_in_tx(
                    conn,
                    event_id=row["event_id"],
                    reason="envelope_expired",
                    now_text=now,
                )
                conn.commit()
            else:
                conn.rollback()
            return _failure(
                reason="envelope_expired",
                http=403,
                message="That question's session expired — ask again.",
                error_type="envelope_expired",
            )
        if int(row["attempts"]) >= ENVELOPE_MAX_ATTEMPTS:
            conn.rollback()
            return _failure(
                reason="attempts_exhausted",
                http=403,
                message=(
                    "That question has already been sent to your "
                    "provider — ask a new one."
                ),
                error_type="attempts_exhausted",
            )
        if not verify_envelope_hash(
            computed_hash,
            row["synthesis_envelope_hash"],
        ):
            conn.rollback()
            return _invalid("envelope_hash_mismatch")

        cursor = conn.execute(
            """
            UPDATE librarian_gate_events
            SET relay_attempt_count = COALESCE(relay_attempt_count, 0) + 1,
                relay_started_at = ?,
                relay_provider = ?
            WHERE event_id = ?
              AND terminal_failure_reason IS NULL
              AND relay_attempt_count < ?
            """,
            (
                now,
                provider,
                row["event_id"],
                ENVELOPE_MAX_ATTEMPTS,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise RuntimeError("Librarian envelope claim row disappeared")
        conn.commit()
        return True, {
            "event_id": row["event_id"],
            "run_id": run_id,
            "enforcement_epoch": int(
                row["enforcement_epoch_at_decision"]
            ),
            "attempt": int(row["attempts"]) + 1,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

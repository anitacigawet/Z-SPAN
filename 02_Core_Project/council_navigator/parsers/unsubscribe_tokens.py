"""Opaque, HMAC-authenticated unsubscribe tokens for notification email."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

try:
    from parsers import database
    from parsers.google_oauth import get_or_create_jwt_secret
except ImportError:  # Direct imports from parsers/ at runtime.
    import database  # type: ignore[no-redef]
    from google_oauth import get_or_create_jwt_secret  # type: ignore[no-redef]


def _signature(token_id: str, user_id: int) -> str:
    message = f"{token_id}:{user_id}".encode("ascii")
    return hmac.new(
        get_or_create_jwt_secret(),
        message,
        hashlib.sha256,
    ).hexdigest()


def ensure_token_for_user(
    user_id: int,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Return a signed unused token, reusing the user's existing row."""
    own_connection = conn is None
    if conn is None:
        conn = database.get_connection()
    failed = False
    try:
        row = conn.execute(
            """
            SELECT token_id
            FROM unsubscribe_tokens
            WHERE user_id = ?
              AND used_at IS NULL
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            ORDER BY created_at ASC, token_id ASC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            token_id = secrets.token_urlsafe(24)
            conn.execute(
                """
                INSERT INTO unsubscribe_tokens (
                    token_id, user_id, expires_at
                ) VALUES (?, ?, datetime('now', '+30 days'))
                """,
                (token_id, user_id),
            )
        else:
            token_id = str(row[0])
        return f"{token_id}.{_signature(token_id, user_id)}"
    except Exception:
        failed = True
        raise
    finally:
        if own_connection:
            try:
                if failed:
                    conn.rollback()
                else:
                    conn.commit()
            finally:
                conn.close()


def verify_unsubscribe_token(
    raw: object,
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """Return the token's user id, failing closed for every error shape."""
    own_connection = conn is None
    try:
        if conn is None:
            conn = database.get_connection()
        if not isinstance(raw, str) or raw.count(".") != 1:
            return None
        token_id, supplied_signature = raw.split(".", 1)
        if not token_id or not supplied_signature:
            return None
        row = conn.execute(
            """
            SELECT user_id
            FROM unsubscribe_tokens
            WHERE token_id = ?
              AND used_at IS NULL
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """,
            (token_id,),
        ).fetchone()
        if row is None:
            return None
        user_id = int(row[0])
        expected_signature = _signature(token_id, user_id)
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        return user_id
    except Exception:
        return None
    finally:
        if own_connection and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def mark_token_used(
    token_id: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Stamp a token used once while preserving the first-use timestamp."""
    own_connection = conn is None
    if conn is None:
        conn = database.get_connection()
    failed = False
    try:
        conn.execute(
            """
            UPDATE unsubscribe_tokens
            SET used_at = COALESCE(used_at, CURRENT_TIMESTAMP)
            WHERE token_id = ? AND used_at IS NULL
            """,
            (token_id,),
        )
    except Exception:
        failed = True
        raise
    finally:
        if own_connection:
            try:
                if failed:
                    conn.rollback()
                else:
                    conn.commit()
            finally:
                conn.close()

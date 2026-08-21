"""Institutional identity policy for publication and review audit fields."""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable, Optional


ROLE_IDENTITY = "Z-SPAN"
EMAIL_PATTERN = re.compile(r"\S+@\S+\.\S+", re.IGNORECASE)

OPERATOR_REVIEW_EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS operator_review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL
        CHECK (action IN ('publish', 'approve', 'unpublish', 'void', 'restore')),
    meeting_id INTEGER,
    work_order_id INTEGER,
    output_type TEXT,
    actor_user_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    FOREIGN KEY (work_order_id) REFERENCES work_orders(id) ON DELETE CASCADE,
    FOREIGN KEY (actor_user_id) REFERENCES users(id)
)
"""


def ensure_operator_review_events_schema(cursor: sqlite3.Cursor) -> None:
    """Create or idempotently widen the private operator-action audit table."""
    cursor.execute(OPERATOR_REVIEW_EVENTS_SCHEMA_SQL)
    table_sql_row = cursor.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'operator_review_events'
        """
    ).fetchone()
    table_sql = (table_sql_row[0] or "") if table_sql_row else ""
    columns = {
        row[1] for row in cursor.execute(
            "PRAGMA table_info(operator_review_events)"
        ).fetchall()
    }

    # Existing databases carry a CHECK constraint that predates per-output
    # void/restore. SQLite cannot widen a CHECK in place, so preserve every
    # audit row through a focused table rebuild.
    if "'void'" not in table_sql or "'restore'" not in table_sql:
        legacy_table = "operator_review_events_pre_void"
        legacy_exists = cursor.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (legacy_table,),
        ).fetchone()
        if legacy_exists:
            raise RuntimeError(
                "incomplete operator_review_events void migration detected"
            )
        cursor.execute(
            f"ALTER TABLE operator_review_events RENAME TO {legacy_table}"
        )
        cursor.execute(OPERATOR_REVIEW_EVENTS_SCHEMA_SQL)
        output_type_expr = "output_type" if "output_type" in columns else "NULL"
        cursor.execute(
            f"""
            INSERT INTO operator_review_events (
                id, event_key, action, meeting_id, work_order_id, output_type,
                actor_user_id, occurred_at, created_at
            )
            SELECT id, event_key, action, meeting_id, work_order_id,
                   {output_type_expr}, actor_user_id, occurred_at, created_at
            FROM {legacy_table}
            ORDER BY id
            """
        )
        cursor.execute(f"DROP TABLE {legacy_table}")
        return

    if "output_type" not in columns:
        cursor.execute(
            "ALTER TABLE operator_review_events ADD COLUMN output_type TEXT"
        )

# This is shared by the one-shot migration and the runtime boundary. Keeping
# one query prevents the migration's definition of a legacy identity from
# drifting away from the write/sync guard's definition.
DISTINCT_LEGACY_IDENTITY_QUERY = """
SELECT DISTINCT trim(identity) AS identity
FROM (
    SELECT published_by AS identity FROM meetings
    UNION ALL
    SELECT approved_by AS identity FROM work_orders
    UNION ALL
    SELECT verified_by AS identity FROM quote_verifications
)
WHERE identity IS NOT NULL
  AND trim(identity) != ''
  AND upper(trim(identity)) != 'Z-SPAN'
ORDER BY identity
"""

_legacy_tokens_by_database: dict[str, frozenset[str]] = {}


def coerce_role_identity(_value: object = None) -> str:
    """Return the only identity allowed on public-adjacent role columns."""
    return ROLE_IDENTITY


def coerce_optional_role_identity(value: object) -> Optional[str]:
    """Preserve the absence of verification while anonymizing a real stamp."""
    if value is None or not str(value).strip():
        return None
    return ROLE_IDENTITY


def distinct_legacy_tokens(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Read the migration/runtime legacy identity vocabulary from ``conn``."""
    return tuple(row[0] for row in conn.execute(DISTINCT_LEGACY_IDENTITY_QUERY))


def cached_legacy_tokens(
    conn: sqlite3.Connection,
    database_key: str,
) -> frozenset[str]:
    """Capture legacy identities once per database path for this process."""
    cached = _legacy_tokens_by_database.get(database_key)
    if cached is None:
        cached = frozenset(distinct_legacy_tokens(conn))
        _legacy_tokens_by_database[database_key] = cached
    return cached


def clear_legacy_token_cache(database_key: Optional[str] = None) -> None:
    """Test/migration hook; production callers rely on process-lifetime cache."""
    if database_key is None:
        _legacy_tokens_by_database.clear()
    else:
        _legacy_tokens_by_database.pop(database_key, None)


def owner_display_names(
    conn: sqlite3.Connection,
    owner_emails: Iterable[str],
) -> tuple[str, ...]:
    """Query current display names for configured, authenticated owners."""
    normalized = sorted({email.strip().casefold() for email in owner_emails if email.strip()})
    if not normalized:
        return ()
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"""
        SELECT DISTINCT trim(display_name)
        FROM users
        WHERE lower(trim(email)) IN ({placeholders})
          AND display_name IS NOT NULL
          AND trim(display_name) != ''
          AND lower(trim(display_name)) != lower(?)
        ORDER BY trim(display_name)
        """,
        (*normalized, ROLE_IDENTITY),
    ).fetchall()
    return tuple(row[0] for row in rows)


def publication_text_violation(
    value: object,
    *,
    conn: sqlite3.Connection,
    database_key: str,
    owner_emails: Iterable[str],
) -> Optional[str]:
    """Return a public-safe rejection reason, or ``None`` when text is safe."""
    legacy_tokens = cached_legacy_tokens(conn, database_key)
    if value is None:
        return None
    if not isinstance(value, str):
        return "must be a string"
    if EMAIL_PATTERN.search(value):
        return "contains an email address"

    folded = value.casefold()
    for display_name in owner_display_names(conn, owner_emails):
        owner_tokens = {display_name.casefold()}
        owner_tokens.update(
            part.casefold() for part in display_name.split() if len(part) >= 3
        )
        if any(
            re.search(rf"(?<!\w){re.escape(token)}(?!\w)", folded)
            for token in owner_tokens
        ):
            return "contains an authenticated owner display name"
    for token in legacy_tokens:
        if token.casefold() in folded:
            return "contains a legacy operator identity token"
    return None

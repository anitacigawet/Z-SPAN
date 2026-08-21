#!/usr/bin/env python3.11
"""One-shot, repeat-safe migration to institutional publication identity."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any


_PARSERS_DIR = Path(__file__).resolve().parents[2]
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

import operator_identity


def migrate(database_path: str | Path) -> dict[str, Any]:
    """Apply the role-identity migration in one SQLite transaction."""
    path = str(Path(database_path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    report: dict[str, Any] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(operator_identity.OPERATOR_REVIEW_EVENTS_SCHEMA_SQL)

        before_events = conn.execute(
            "SELECT COUNT(*) FROM operator_review_events"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT OR IGNORE INTO operator_review_events (
                event_key, action, meeting_id, actor_user_id, occurred_at
            )
            SELECT
                'legacy:publish:meeting:' || m.id || ':user:' || u.id,
                'publish', m.id, u.id,
                COALESCE(m.published_at, m.updated_at, m.created_at)
            FROM meetings m
            JOIN users u
              ON lower(trim(u.email)) = lower(trim(m.published_by))
            WHERE m.published_by IS NOT NULL
              AND trim(m.published_by) != ''
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO operator_review_events (
                event_key, action, meeting_id, work_order_id,
                actor_user_id, occurred_at
            )
            SELECT
                'legacy:approve:work-order:' || wo.id || ':user:' || u.id,
                'approve', wo.meeting_id, wo.id, u.id,
                COALESCE(wo.approved_at, wo.updated_at, wo.created_at)
            FROM work_orders wo
            JOIN users u
              ON lower(trim(u.email)) = lower(trim(wo.approved_by))
            WHERE wo.approved_by IS NOT NULL
              AND trim(wo.approved_by) != ''
            """
        )

        legacy_tokens = operator_identity.distinct_legacy_tokens(conn)
        for token in sorted(legacy_tokens, key=len, reverse=True):
            conn.execute(
                """
                UPDATE meetings
                SET publish_notes = replace(publish_notes, ?, ?)
                WHERE publish_notes IS NOT NULL
                  AND instr(publish_notes, ?) > 0
                """,
                (token, operator_identity.ROLE_IDENTITY, token),
            )

        report["meetings_normalized"] = conn.execute(
            """
            UPDATE meetings
            SET published_by = ?
            WHERE published_by IS NOT NULL
              AND published_by != ?
            """,
            (operator_identity.ROLE_IDENTITY, operator_identity.ROLE_IDENTITY),
        ).rowcount
        report["work_orders_normalized"] = conn.execute(
            """
            UPDATE work_orders
            SET approved_by = ?
            WHERE approved_by IS NOT NULL
              AND approved_by != ?
            """,
            (operator_identity.ROLE_IDENTITY, operator_identity.ROLE_IDENTITY),
        ).rowcount
        report["quote_verifications_normalized"] = conn.execute(
            """
            UPDATE quote_verifications
            SET verified_by = ?
            WHERE verified_by IS NOT NULL
              AND verified_by != ?
            """,
            (operator_identity.ROLE_IDENTITY, operator_identity.ROLE_IDENTITY),
        ).rowcount

        after_events = conn.execute(
            "SELECT COUNT(*) FROM operator_review_events"
        ).fetchone()[0]
        report["events_inserted"] = after_events - before_events
        report["legacy_tokens_captured"] = len(legacy_tokens)
        conn.commit()
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database_path", type=Path)
    args = parser.parse_args()
    report = migrate(args.database_path)
    for key, value in sorted(report.items()):
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

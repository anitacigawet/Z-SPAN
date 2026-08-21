"""Deterministic meeting-topic classification and notification fan-out.

Slice 3B keeps generation/sync and email delivery loosely coupled:

* ``recompute_meeting_topic_tags`` rebuilds the five-tag relationship from
  the meeting's current high-signal text.
* ``enqueue_published_meeting_notifications`` creates at most one durable
  event per publicly-visible meeting and one outbox row per matching user.

Both helpers accept a caller-owned SQLite connection. When no connection is
provided they commit their own transaction; a supplied connection is never
committed, rolled back, or closed here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Any

try:
    from parsers import database
    from parsers.topic_matcher import TopicMatch, match_meeting
    from parsers.topic_tags import TOPIC_LABELS
except ImportError:  # Direct imports from parsers/ at runtime.
    import database  # type: ignore[no-redef]
    from topic_matcher import TopicMatch, match_meeting  # type: ignore[no-redef]
    from topic_tags import TOPIC_LABELS  # type: ignore[no-redef]


_CITATION_MARKUP_RE = re.compile(r"\[[^\]]*\]|\{[^}]*\}|<[^>]*>")
_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+(.+)$", re.MULTILINE)
_MAX_KEY_DECISIONS = 5


def _parse_key_decisions(raw: str | None) -> list[str]:
    """Mirror BroadcastPage's numbered-list extraction for matcher input."""
    if not raw:
        return []
    stripped = _CITATION_MARKUP_RE.sub("", raw)
    return [
        match.strip()
        for match in _NUMBERED_ITEM_RE.findall(stripped)
        if match.strip()
    ][:_MAX_KEY_DECISIONS]


def _finish_owned_connection(
    conn: sqlite3.Connection,
    *,
    own_connection: bool,
    error: bool,
) -> None:
    if not own_connection:
        return
    try:
        if error:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()


def recompute_meeting_topic_tags(
    meeting_id: int,
    conn: sqlite3.Connection | None = None,
) -> list[TopicMatch]:
    """Replace a meeting's stored topic matches with its current matches.

    Missing meetings are a no-op. Only non-voided ``episode_tagline`` and
    ``key_decisions`` outputs participate; a missing/empty output is honest
    empty input to the deterministic matcher.
    """
    own_connection = conn is None
    if conn is None:
        conn = database.get_connection()

    failed = False
    try:
        meeting = conn.execute(
            "SELECT meeting_title FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if meeting is None:
            return []

        output_rows = conn.execute(
            """
            SELECT output_type, content
            FROM notebook_outputs
            WHERE meeting_id = ?
              AND output_type IN ('episode_tagline', 'key_decisions')
              AND voided_at IS NULL
            """,
            (meeting_id,),
        ).fetchall()
        outputs = {
            str(row[0]): (str(row[1]) if row[1] is not None else "")
            for row in output_rows
        }

        matches = match_meeting(
            meeting_title=str(meeting[0] or ""),
            episode_tagline=outputs.get("episode_tagline", ""),
            key_decisions=_parse_key_decisions(outputs.get("key_decisions")),
        )

        conn.execute(
            "DELETE FROM meeting_topic_tags WHERE meeting_id = ?",
            (meeting_id,),
        )
        conn.executemany(
            """
            INSERT INTO meeting_topic_tags (
                meeting_id, tag_id, evidence_field, trigger_phrase,
                matcher_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    meeting_id,
                    match.tag_id,
                    match.evidence_field,
                    match.trigger_phrase,
                    match.matcher_version,
                )
                for match in matches
            ],
        )
        return matches
    except Exception:
        failed = True
        raise
    finally:
        _finish_owned_connection(
            conn,
            own_connection=own_connection,
            error=failed,
        )


def _result(
    meeting_id: int,
    *,
    enqueued: bool,
    recipient_count: int = 0,
    skipped_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "enqueued": enqueued,
        "meeting_id": meeting_id,
        "recipient_count": recipient_count,
        "skipped_reason": skipped_reason,
    }


def _matched_topic_tags(
    conn: sqlite3.Connection,
    user_id: int,
    city_key: str,
    meeting_id: int,
) -> list[str]:
    """Return enabled city topics that matched this meeting."""
    enabled_tags = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT tag_id
            FROM follow_city_topics
            WHERE user_id = ? AND city_key = ?
            """,
            (user_id, city_key),
        ).fetchall()
    }
    if not enabled_tags:
        return []

    meeting_tags = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT tag_id
            FROM meeting_topic_tags
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        ).fetchall()
    }
    return sorted(enabled_tags & meeting_tags)


def _reason_for_row(
    row: sqlite3.Row | tuple[Any, ...],
    matched_topic_tags: list[str] | None = None,
) -> dict[str, Any]:
    target_type = str(row[1])
    target_key = str(row[2])
    if target_type == "city":
        reason: dict[str, Any] = {
            "target_type": "city",
            "target_key": target_key,
            "label": target_key,
        }
        if matched_topic_tags:
            reason["matched_topic_tags"] = matched_topic_tags
        return reason
    # COMMENTED_OUT_SESSION_104 - global topic-follow deferred per operator direction; only city follows fire emails now
    # if target_type == "topic":
    #     return {
    #         "target_type": "topic",
    #         "target_key": target_key,
    #         "label": TOPIC_LABELS.get(target_key, target_key),
    #         "evidence_field": str(row[3] or ""),
    #         "trigger_phrase": str(row[4] or ""),
    #     }
    raise ValueError(f"Unsupported notification reason type: {target_type}")


def enqueue_published_meeting_notifications(
    meeting_id: int,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Fan a newly-public meeting out to matching, email-enabled followers.

    City-follows fire the email; enabled per-city topics decorate matched
    meetings via ``matched_topic_tags`` inside each city reason.

    ``notification_events.meeting_id`` is the serialization gate. If this
    call does not create that row it returns ``already_enqueued`` before
    reading followers or touching the outbox.
    """
    own_connection = conn is None
    if conn is None:
        conn = database.get_connection()

    failed = False
    try:
        meeting = conn.execute(
            """
            SELECT COALESCE(c.name, m.city_name) AS city_name
            FROM meetings AS m
            LEFT JOIN cities AS c ON c.id = m.city_id
            WHERE m.id = ?
            """,
            (meeting_id,),
        ).fetchone()
        if meeting is None:
            return _result(
                meeting_id,
                enqueued=False,
                skipped_reason="meeting_not_found",
            )
        if not database.is_meeting_publicly_visible(meeting_id, conn=conn):
            return _result(
                meeting_id,
                enqueued=False,
                skipped_reason="not_publicly_visible",
            )

        event_cursor = conn.execute(
            "INSERT OR IGNORE INTO notification_events (meeting_id) VALUES (?)",
            (meeting_id,),
        )
        if event_cursor.rowcount != 1:
            return _result(
                meeting_id,
                enqueued=False,
                skipped_reason="already_enqueued",
            )

        # The CTE yields one row per matched follow. The outer LEFT JOIN makes
        # "no prefs row" mean the default email-enabled state, while an
        # explicit email_enabled=0 suppresses the user before aggregation.
        reason_rows = conn.execute(
            """
            WITH matching_reasons AS (
                SELECT
                    f.user_id,
                    'city' AS target_type,
                    f.target_key,
                    NULL AS evidence_field,
                    NULL AS trigger_phrase
                FROM follows AS f
                WHERE f.target_type = 'city'
                  AND LOWER(f.target_key) = LOWER(:city_name)

                /* # COMMENTED_OUT_SESSION_104 - global topic-follow deferred per operator direction; only city follows fire emails now
                UNION ALL

                SELECT
                    f.user_id,
                    'topic' AS target_type,
                    f.target_key,
                    mt.evidence_field,
                    mt.trigger_phrase
                FROM follows AS f
                JOIN meeting_topic_tags AS mt
                  ON mt.tag_id = f.target_key
                WHERE f.target_type = 'topic'
                  AND mt.meeting_id = :meeting_id
                */
            )
            SELECT
                mr.user_id,
                mr.target_type,
                mr.target_key,
                mr.evidence_field,
                mr.trigger_phrase
            FROM matching_reasons AS mr
            LEFT JOIN notification_prefs AS np
              ON np.user_id = mr.user_id
            WHERE np.email_enabled IS NULL OR np.email_enabled = 1
            ORDER BY
                mr.user_id ASC,
                CASE mr.target_type WHEN 'city' THEN 0 ELSE 1 END ASC,
                mr.target_key ASC
            """,
            {
                "city_name": str(meeting[0] or ""),
                "meeting_id": meeting_id,
            },
        ).fetchall()

        reasons_by_user: dict[int, list[dict[str, Any]]] = defaultdict(list)
        canonical_city_key = str(meeting[0] or "")
        for row in reason_rows:
            user_id = int(row[0])
            target_type = str(row[1])
            matched_topic_tags = (
                _matched_topic_tags(
                    conn,
                    user_id,
                    canonical_city_key,
                    meeting_id,
                )
                if target_type == "city"
                else None
            )
            reasons_by_user[user_id].append(
                _reason_for_row(row, matched_topic_tags)
            )

        recipient_count = 0
        for user_id, reasons in reasons_by_user.items():
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO notification_outbox (
                    user_id, meeting_id, reasons_json
                ) VALUES (?, ?, ?)
                """,
                (
                    user_id,
                    meeting_id,
                    json.dumps(
                        reasons,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            if cursor.rowcount == 1:
                recipient_count += 1

        conn.execute(
            """
            UPDATE notification_events
            SET recipient_count = ?
            WHERE meeting_id = ?
            """,
            (recipient_count, meeting_id),
        )
        return _result(
            meeting_id,
            enqueued=True,
            recipient_count=recipient_count,
        )
    except Exception:
        failed = True
        raise
    finally:
        _finish_owned_connection(
            conn,
            own_connection=own_connection,
            error=failed,
        )

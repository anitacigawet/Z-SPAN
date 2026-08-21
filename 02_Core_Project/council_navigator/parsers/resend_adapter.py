"""Resend delivery adapter for the durable notification outbox."""

from __future__ import annotations

import html
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests

try:
    from parsers import database, unsubscribe_tokens
    from parsers.topic_tags import TOPIC_LABELS
except ImportError:  # Direct imports from parsers/ at runtime.
    import database  # type: ignore[no-redef]
    import unsubscribe_tokens  # type: ignore[no-redef]
    from topic_tags import TOPIC_LABELS  # type: ignore[no-redef]


logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
MAX_ATTEMPTS = 5
DRAIN_BATCH_SIZE = 20
HTTP_TIMEOUT_SECONDS = 8
LAST_ERROR_MAX_CHARS = 1000

DEFAULT_SENDER_ADDRESS = "Z-SPAN <notifications@zspan.org>"
DEFAULT_PUBLIC_ORIGIN = "https://zspan.org"

_BACKOFF_BY_ATTEMPT = {
    1: timedelta(minutes=1),
    2: timedelta(minutes=5),
    3: timedelta(minutes=30),
    4: timedelta(hours=4),
    5: timedelta(hours=24),
}

_EVIDENCE_LABELS = {
    "meeting_title": "meeting title",
    "episode_tagline": "episode headline",
    "key_decision": "key decision",
}


def _public_origin() -> str:
    origin = (
        os.environ.get("ZSPAN_PUBLIC_ORIGIN", DEFAULT_PUBLIC_ORIGIN).strip()
        or DEFAULT_PUBLIC_ORIGIN
    ).rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(char in origin for char in "\r\n<>")
    ):
        raise ValueError(
            "ZSPAN_PUBLIC_ORIGIN must be a bare absolute http(s) origin"
        )
    return origin


def _inline(value: object) -> str:
    return " ".join(str(value or "").split())


def _plain(value: object) -> str:
    # Angle brackets in even the text/plain alternative can confuse link and
    # abuse scanners. Entity-escape them at the insertion boundary.
    return _inline(value).replace("<", "&lt;").replace(">", "&gt;")


def _html(value: object) -> str:
    return html.escape(_inline(value), quote=True)


def _reason_summary(raw: str) -> str:
    decoded = json.loads(raw)
    if not isinstance(decoded, list) or not decoded:
        raise ValueError("reasons_json must be a non-empty list")

    summaries: list[str] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise ValueError("reasons_json entries must be objects")
        target_type = item.get("target_type")
        target_key = _inline(item.get("target_key"))
        label = _inline(item.get("label"))
        if target_type == "city" and target_key:
            matched = item.get("matched_topic_tags") or []
            if matched:
                tag_labels = ", ".join(
                    TOPIC_LABELS.get(str(tag_id), str(tag_id))
                    for tag_id in matched
                )
                summaries.append(
                    f"you follow {label or target_key} · tagged: {tag_labels}"
                )
            else:
                summaries.append(f"you follow {label or target_key}")
            continue
        if target_type == "topic" and target_key:
            topic_label = label or TOPIC_LABELS.get(target_key, target_key)
            trigger = _inline(item.get("trigger_phrase"))
            evidence = _EVIDENCE_LABELS.get(
                _inline(item.get("evidence_field")),
                _inline(item.get("evidence_field")) or "meeting",
            )
            detail = f" (matched “{trigger}” in the {evidence})" if trigger else ""
            summaries.append(f"you follow {topic_label}{detail}")
            continue
        raise ValueError("reasons_json contains an unknown reason")
    return "Sent because " + "; ".join(summaries) + "."


def _message_payload(row: tuple[Any, ...], unsubscribe_url: str) -> dict[str, Any]:
    (
        _outbox_id,
        _user_id,
        _meeting_id,
        reasons_json,
        _attempt_count,
        email,
        public_id,
        meeting_title,
        city_name,
    ) = row
    origin = _public_origin()
    broadcast_url = f"{origin}/?{urlencode({'view': 'broadcast', 'publicId': public_id})}"
    reason_summary = _reason_summary(str(reasons_json))

    subject = _plain(f"New {city_name} meeting: {meeting_title}")
    text_body = "\n\n".join(
        (
            subject,
            _plain(reason_summary),
            f"Watch the broadcast: {_plain(broadcast_url)}",
            f"Unsubscribe from Z-SPAN meeting emails: {_plain(unsubscribe_url)}",
        )
    )

    safe_title = _html(meeting_title)
    safe_city = _html(city_name)
    safe_reasons = _html(reason_summary)
    safe_broadcast_url = _html(broadcast_url)
    safe_unsubscribe_url = _html(unsubscribe_url)
    decoded_reasons = json.loads(str(reasons_json))
    matched_tag_ids = {
        str(tag_id)
        for reason in decoded_reasons
        if isinstance(reason, dict)
        for tag_id in (reason.get("matched_topic_tags") or [])
        if str(tag_id) in TOPIC_LABELS
    }
    ordered_tag_ids = [
        tag_id for tag_id in TOPIC_LABELS if tag_id in matched_tag_ids
    ]
    tag_pills = "".join(
        (
            '<span style="display:inline-block;padding:2px 8px;'
            'margin-right:4px;background:#eef2ff;border-radius:9999px;'
            f'font-size:12px;">{_html(TOPIC_LABELS[tag_id])}</span>'
        )
        for tag_id in ordered_tag_ids
    )
    tagged_html = (
        '<p style="margin:8px 0 12px 0;font-size:13px;color:#555;">'
        f"Tagged: {tag_pills}</p>"
        if tag_pills
        else ""
    )
    html_body = (
        "<!doctype html><html><body>"
        f"<p>A new <strong>{safe_city}</strong> meeting is available.</p>"
        f"<h1>{safe_title}</h1>"
        f"<p>{safe_reasons}</p>"
        f"{tagged_html}"
        f'<p><a href="{safe_broadcast_url}">Watch the broadcast</a></p>'
        f'<p><a href="{safe_unsubscribe_url}">Unsubscribe from meeting emails</a></p>'
        "</body></html>"
    )

    return {
        "from": _inline(
            os.environ.get(
                "ZSPAN_SENDER_ADDRESS",
                DEFAULT_SENDER_ADDRESS,
            ).strip()
            or DEFAULT_SENDER_ADDRESS
        ),
        "to": [_inline(email)],
        "subject": subject,
        "text": text_body,
        "html": html_body,
        "headers": {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def _sqlite_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _record_failure(
    conn: sqlite3.Connection,
    *,
    outbox_id: int,
    prior_attempt_count: int,
    error: Exception,
) -> None:
    attempt_count = prior_attempt_count + 1
    delay = _BACKOFF_BY_ATTEMPT[min(attempt_count, MAX_ATTEMPTS)]
    next_attempt_at = _sqlite_timestamp(datetime.now(timezone.utc) + delay)
    error_text = f"{type(error).__name__}: {error}"[:LAST_ERROR_MAX_CHARS]
    conn.execute(
        """
        UPDATE notification_outbox
        SET attempt_count = ?,
            next_attempt_at = ?,
            last_error = ?
        WHERE id = ? AND sent_at IS NULL
        """,
        (attempt_count, next_attempt_at, error_text, outbox_id),
    )


def drain_notification_outbox(
    limit: int = DRAIN_BATCH_SIZE,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Attempt a bounded pending batch without letting one row abort another."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    result: dict[str, Any] = {
        "attempted": 0,
        "sent": 0,
        "failed": 0,
        "skipped_no_api_key": not bool(api_key),
    }
    if not api_key:
        return result

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")

    own_connection = conn is None
    if conn is None:
        conn = database.get_connection()

    try:
        rows = conn.execute(
            """
            SELECT
                o.id,
                o.user_id,
                o.meeting_id,
                o.reasons_json,
                o.attempt_count,
                u.email,
                m.public_id,
                m.meeting_title,
                m.city_name
            FROM notification_outbox AS o
            JOIN users AS u ON u.id = o.user_id
            JOIN meetings AS m ON m.id = o.meeting_id
            WHERE o.sent_at IS NULL
              AND o.attempt_count < ?
              AND o.next_attempt_at <= CURRENT_TIMESTAMP
            ORDER BY o.next_attempt_at ASC, o.id ASC
            LIMIT ?
            """,
            (MAX_ATTEMPTS, limit),
        ).fetchall()

        for raw_row in rows:
            row = tuple(raw_row)
            outbox_id = int(row[0])
            user_id = int(row[1])
            prior_attempt_count = int(row[4])
            result["attempted"] += 1
            try:
                raw_token = unsubscribe_tokens.ensure_token_for_user(
                    user_id,
                    conn=conn,
                )
                origin = _public_origin()
                unsubscribe_url = (
                    f"{origin}/api/unsubscribe?{urlencode({'token': raw_token})}"
                )
                payload = _message_payload(row, unsubscribe_url)

                # Release an owned SQLite write lock before the network call.
                # A caller-supplied connection remains entirely caller-owned.
                if own_connection:
                    conn.commit()

                response = requests.post(
                    RESEND_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": f"zspan-outbox-{outbox_id}",
                    },
                    json=payload,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                response_body = response.json()
                provider_message_id = (
                    response_body.get("id")
                    if isinstance(response_body, dict)
                    else None
                )
                if not provider_message_id:
                    raise ValueError("Resend response omitted message id")

                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET sent_at = CURRENT_TIMESTAMP,
                        provider_message_id = ?,
                        last_error = NULL
                    WHERE id = ? AND sent_at IS NULL
                    """,
                    (str(provider_message_id), outbox_id),
                )
                if own_connection:
                    conn.commit()
                result["sent"] += 1
            except Exception as exc:
                logger.exception(
                    "notification outbox row %s failed; continuing batch",
                    outbox_id,
                )
                try:
                    _record_failure(
                        conn,
                        outbox_id=outbox_id,
                        prior_attempt_count=prior_attempt_count,
                        error=exc,
                    )
                    if own_connection:
                        conn.commit()
                except Exception:
                    logger.exception(
                        "failed to persist notification outbox error for row %s",
                        outbox_id,
                    )
                    if own_connection:
                        conn.rollback()
                result["failed"] += 1
        return result
    finally:
        if own_connection:
            conn.close()

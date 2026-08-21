"""video_match_helpers — shared infrastructure for video-match scripts.

Extracted from the original `match_videos.py` (T-004 deterministic matcher,
retired 2026-06-10) so the Haiku-based replacement (`scripts/haiku_match_videos.py`)
can reuse the DB-write + meeting-list machinery without depending on the
deprecated heuristic-classification code.

Pure infrastructure — no matching logic lives here. Callers decide the
match; this module just persists it correctly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional

from database import get_connection


def parse_meeting_date(s: Optional[str]) -> Optional[date]:
    """Parse a meeting_date string into a date. Handles common formats."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def meetings_for_city(
    city: str, within_days: int = 21, include_future: bool = False,
) -> List[dict]:
    """Return recent meetings from the DB for a given city, newest first.

    Filters to meetings with a parseable meeting_date in the window
    [today - within_days, today]. Future-dated scheduled meetings are
    EXCLUDED by default: a meeting that hasn't happened yet can't have a
    recording, so matching it wastes a Haiku call and inflates any
    "recent" count. Pass include_future=True to include them.

    Returned dicts include id, city, date (date object), title,
    video_url, match_confidence, match_method.
    """
    today = date.today()
    cutoff = today - timedelta(days=within_days)
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT m.id, m.city_name, m.meeting_date, m.meeting_title, m.video_url,
               m.video_url_match_confidence, m.video_url_match_method
        FROM meetings m
        WHERE m.city_name = ?
        ORDER BY m.meeting_date DESC
        LIMIT 200
        """,
        (city,),
    ).fetchall()
    conn.close()
    out: List[dict] = []
    for r in rows:
        d = parse_meeting_date(r["meeting_date"])
        if d and d >= cutoff and (include_future or d <= today):
            out.append({
                "id": r["id"],
                "city": r["city_name"],
                "date": d,
                "title": r["meeting_title"] or "",
                "video_url": r["video_url"],
                "match_confidence": r["video_url_match_confidence"],
                "match_method": r["video_url_match_method"],
            })
    return out


def apply_match(
    meeting_id: int, video_url: str, confidence: str, method: str,
) -> None:
    """Write a match back to meetings.video_url + match metadata.

    Also propagates the URL + match info to any existing work order for
    the meeting so the operator terminal can see the match without
    joining tables. For high-confidence matches, flips the WO state from
    `awaiting_video` to `pending` automatically (no manual [SET URL]
    needed); medium / needs_review WOs keep their state and surface a
    [CONFIRM URL] button instead.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Update meetings table
    cursor.execute(
        """
        UPDATE meetings
        SET video_url = ?,
            video_url_match_confidence = ?,
            video_url_match_method = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (video_url, confidence, method, meeting_id),
    )

    # Mirror to work_orders if a WO exists for this meeting. Behavior depends
    # on confidence to keep the state machine clean:
    #   - high:         set wo.youtube_video_url AND flip awaiting_video →
    #                   pending (no operator click needed; [PROCESS] is ready)
    #   - medium/
    #     needs_review: write match metadata only; leave youtube_video_url
    #                   null. The operator sees a confidence pill + a
    #                   [CONFIRM URL] button.
    # Never overwrite a manually-set wo.youtube_video_url regardless of
    # confidence — operator paste is authoritative.
    cursor.execute(
        "SELECT id, state, youtube_video_url FROM work_orders WHERE meeting_id = ?",
        (meeting_id,),
    )
    wo_row = cursor.fetchone()
    if wo_row is not None:
        wo_id, wo_state, wo_url = wo_row["id"], wo_row["state"], wo_row["youtube_video_url"]
        if wo_url:
            cursor.execute(
                """
                UPDATE work_orders
                SET video_url_match_confidence = ?,
                    video_url_match_method = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (confidence, method, wo_id),
            )
        elif confidence == "high":
            new_state = "pending" if wo_state == "awaiting_video" else wo_state
            cursor.execute(
                """
                UPDATE work_orders
                SET youtube_video_url = ?,
                    state = ?,
                    video_url_match_confidence = ?,
                    video_url_match_method = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (video_url, new_state, confidence, method, wo_id),
            )
        else:
            cursor.execute(
                """
                UPDATE work_orders
                SET video_url_match_confidence = ?,
                    video_url_match_method = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (confidence, method, wo_id),
            )

    conn.commit()
    conn.close()


# Confidence rank for --min-confidence gating.
CONFIDENCE_RANK = {"high": 3, "medium": 2, "needs_review": 1, "none": 0}

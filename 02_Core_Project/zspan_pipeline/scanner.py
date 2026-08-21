"""
Z-SPAN scanner — walks recent meetings and enqueues work orders.

Runs periodically (manually or via cron / the worker's idle tick). For each
target city, the scanner:
  1. Pulls cached meetings from the SQLite cache (no live re-scrape).
  2. Filters to meetings within the last MEETING_AGE_LIMIT_DAYS.
  3. For each meeting, creates a work_orders row in `pending` if missing.
  4. Marks meetings without a video URL as `awaiting_video`.

Older meetings (older than the age limit) are skipped — Z-SPAN does not
spend compute on processing months-old meetings; they remain visible on the
public site but no Studio outputs are generated for them.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List

# Make `parsers/` importable
_PARSERS_DIR = Path(__file__).resolve().parent.parent / "council_navigator" / "parsers"
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

# D-099 Phase 2 C5: swap to HTTP backend when ZSPAN_DB_BACKEND=http.
from zspan_pipeline.db_backend import install_db_backend  # noqa: E402
install_db_backend()

from database import (  # noqa: E402
    get_connection,
    enqueue_work_order,
    update_work_order_state,
)

logger = logging.getLogger(__name__)

# How far back we'll process meetings. Older meetings are not worth the compute.
MEETING_AGE_LIMIT_DAYS = int(os.environ.get("ZSPAN_MEETING_AGE_LIMIT_DAYS", "30"))

# Default coverage area for the pilot — expand later via env or DB config.
DEFAULT_TARGET_CITIES = (
    os.environ.get("ZSPAN_TARGET_CITIES")
    or "Kingman,Bullhead City,Lake Havasu City,Colorado City"
).split(",")
DEFAULT_TARGET_CITIES = [c.strip() for c in DEFAULT_TARGET_CITIES if c.strip()]


def _parse_meeting_date(s: str | None) -> datetime | None:
    """Parse YYYY-MM-DD or fail-safe variants. Returns None if unparseable."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _is_youtube_url(url: str | None) -> bool:
    if not url:
        return False
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def scan_recent_meetings(
    cities: Iterable[str] | None = None,
    age_limit_days: int = MEETING_AGE_LIMIT_DAYS,
) -> dict:
    """
    Enqueue work orders for any recent meetings in the target cities that
    don't have one yet. Idempotent — safe to run repeatedly.

    Returns a summary dict:
        {
            "scanned": int,
            "enqueued_pending": int,
            "enqueued_awaiting_video": int,
            "skipped_too_old": int,
            "already_tracked": int,
        }
    """
    # D-099 Phase 2 C5: when running on Mac under HTTP backend, the scan
    # itself runs on PC (where the meetings table lives). The HTTP shim
    # hits /api/worker/scan which calls THIS function in PC's process.
    # Without this guard, Mac would try to run the scan locally + hit
    # database_http_client.get_connection() -> NotImplementedError.
    if os.environ.get("ZSPAN_DB_BACKEND", "").strip().lower() == "http":
        import database_http_client  # noqa: PLC0415 — lazy
        return database_http_client.scan_recent_meetings(
            cities=cities, age_limit_days=age_limit_days
        )

    cities = list(cities) if cities else DEFAULT_TARGET_CITIES
    cutoff = datetime.now() - timedelta(days=age_limit_days)

    summary = {
        "scanned": 0,
        "enqueued_pending": 0,
        "enqueued_awaiting_video": 0,
        "skipped_too_old": 0,
        "already_tracked": 0,
    }

    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in cities)
    cursor.execute(
        f"""
        SELECT
            m.id, m.city_name, m.county,
            m.meeting_title, m.meeting_date, m.video_url,
            (SELECT id FROM work_orders WHERE meeting_id = m.id) AS existing_wo_id
        FROM meetings m
        WHERE m.city_name IN ({placeholders})
        ORDER BY m.meeting_date DESC
        """,
        cities,
    )
    rows = cursor.fetchall()
    conn.close()

    for row in rows:
        summary["scanned"] += 1

        meeting_dt = _parse_meeting_date(row["meeting_date"])
        is_recent = meeting_dt is not None and meeting_dt >= cutoff

        existing_wo = row["existing_wo_id"]

        # Old meetings that haven't been tracked yet → skip with a marker
        if not is_recent:
            if existing_wo is None:
                summary["skipped_too_old"] += 1
                # Store a row so we don't keep scanning it; in 'skipped_too_old' state
                wo_id = enqueue_work_order(meeting_id=row["id"], priority=-1)
                update_work_order_state(wo_id, "skipped_too_old",
                                        error="meeting older than age limit")
            else:
                summary["already_tracked"] += 1
            continue

        # Recent meeting — figure out priority by recency (higher = newer)
        days_ago = (datetime.now() - meeting_dt).days
        priority = max(0, age_limit_days - days_ago)

        video_url = row["video_url"] if _is_youtube_url(row["video_url"]) else None

        if existing_wo is not None:
            summary["already_tracked"] += 1
            # Refresh: maybe the video URL just landed
            if video_url:
                enqueue_work_order(
                    meeting_id=row["id"],
                    youtube_video_url=video_url,
                    priority=priority,
                )
            continue

        wo_id = enqueue_work_order(
            meeting_id=row["id"],
            youtube_video_url=video_url,
            priority=priority,
        )
        if video_url:
            summary["enqueued_pending"] += 1
        else:
            update_work_order_state(wo_id, "awaiting_video",
                                    error="no YouTube video URL on the meeting yet")
            summary["enqueued_awaiting_video"] += 1

    logger.info("Scan complete: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    result = scan_recent_meetings()
    print(result)

#!/usr/bin/env python3.11
"""
Guide live-stream detector (S-015) — the present-tense layer.

Polls the YouTube Data API for each registered city channel to find broadcasts
that are live RIGHT NOW, and records them in the `live_streams` table for the
Guide view to read. Pure data-mapping — no LLM, no generation;
it never touches the bridge or the publish gate. It only mirrors public live
streams (via the official API), so there is no D-005 pacing or D-006 gate here.

Quota discipline (the one real constraint, per FUTURE_THOUGHTS.md § S-015):
  search.list (the live check) costs 100 units against a 10K/day budget. Two
  layers keep that bounded:
    1. Time-window calendar gate — only poll a city whose meeting is scheduled
       within [T - early_hours, T + late_hours] of now (default 2h early / 3h
       late). A today-meeting with an unparseable time is polled conservatively
       (better an extra poll than a missed live meeting).
    2. Per-city throttle (loop mode) — never poll the same channel more often
       than --min-poll-interval, decoupling loop cadence from quota.
  Currently-live cities are also re-polled (throttled) so we detect when a
  stream ends.

Usage (from the parsers/ directory):
  python3.11 guide_detector.py                  # one calendar-gated pass
  python3.11 guide_detector.py --city Kingman   # poll one city (testing)
  python3.11 guide_detector.py --all            # every registered channel (burns quota)
  python3.11 guide_detector.py --loop           # run continuously on a cadence

Scheduling: run `--loop` as a background process (or wire the one-shot to
Windows Task Scheduler / the orchestrator's heartbeat). It consumes YouTube
quota over time, so it's started by James, not auto-spawned.

Timezone note (V1 simplification): meeting_time is assumed to be in the
meeting's local zone, same as the machine running the detector. Arizona has no
DST, so for the Mohave pilot this holds; cross-tz handling is a future refinement.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time as _time
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Tuple

import database
import youtube_data_api
from env_config import get_youtube_data_api_key
from pattern_projection import get_upcoming_meetings_from_patterns

logger = logging.getLogger(__name__)

# meeting_time parsers — tolerant of "5:00 PM", "5 p.m.", "5pm", "17:00".
_TIME_AMPM_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", re.IGNORECASE)
_TIME_24_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def _parse_meeting_dt(date_str: str, meeting_time: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of (YYYY-MM-DD, meeting_time) into a local datetime.

    Returns None when the time is missing or unparseable — the caller treats
    None as "unknown time" and polls conservatively rather than risk missing a
    live meeting. Handles '5:00 PM', '5 p.m.', '5pm', '17:00'.
    """
    if not date_str or not meeting_time:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    s = str(meeting_time).strip()
    m = _TIME_AMPM_RE.search(s)
    if m:
        hour = int(m.group(1)) % 12
        minute = int(m.group(2) or 0)
        if m.group(3).lower() == "p":
            hour += 12
        try:
            return datetime.combine(d, dtime(hour, minute))
        except ValueError:
            return None
    m = _TIME_24_RE.match(s)
    if m:
        try:
            return datetime.combine(d, dtime(int(m.group(1)), int(m.group(2))))
        except ValueError:
            return None
    return None


def cities_in_window(cities: List[Dict], early_hours: float = 2.0,
                     late_hours: float = 3.0,
                     now: Optional[datetime] = None) -> List[Dict]:
    """Filter registered `cities` to those with a meeting whose window holds now.

    Window for a meeting at time T: [T - early_hours, T + late_hours]. A city
    whose today-meeting has an unparseable/missing time is INCLUDED (we never
    want to miss a live meeting because we couldn't read its scheduled time).

    H-5 (Phase H, 2026-06-03): for cities with curated `meeting_patterns[]`
    in their `city_intelligence/<slug>.json`, consult the pattern projection
    FIRST. The projection is authoritative for cadence — it doesn't depend
    on the scrape having captured the meeting yet, which closes the silent
    failure mode where a city's calendar page changes structure and the
    parser starts returning empty, so the detector skips the city, and a
    live meeting goes unnoticed. Cities WITHOUT patterns fall back to the
    existing scrape-instance gate (backward-compat preserved).
    """
    now = now or datetime.now()
    today_str = now.date().isoformat()

    keep: set = set()

    # H-5 primary path: pattern projection for cities that have it. For each
    # registered city, ask pattern_projection for today's projected
    # meetings (days_ahead=0). If any project into the polling window
    # (or have no parseable time → conservative include), keep the city.
    cities_with_patterns: set = set()
    for c in cities:
        city_name = c.get("city")
        if not city_name:
            continue
        projected_today = [
            m for m in get_upcoming_meetings_from_patterns(city_name, days_ahead=0, start_date=now.date())
            if m["date"] == today_str
        ]
        if not projected_today:
            continue
        cities_with_patterns.add((city_name, c.get("state")))
        for mtg in projected_today:
            dt = mtg["datetime"]
            if dt.hour == 0 and dt.minute == 0:
                # The projection couldn't parse the time → conservative include.
                keep.add((city_name, c.get("state")))
                break
            if (dt - timedelta(hours=early_hours)) <= now <= (dt + timedelta(hours=late_hours)):
                keep.add((city_name, c.get("state")))
                break

    # Fallback path: scraped-instance gate for cities WITHOUT patterns
    # (cities_with_patterns set above identifies which to skip — those
    # cities' patterns are the authoritative signal, so the scraped row
    # shouldn't compete with it). Without this skip, a city with a
    # pattern saying "no meeting today" but a stale scrape row showing
    # one would still trigger a poll.
    scheduled = database.get_scheduled_meetings_on(today_str)
    for mtg in scheduled:
        key = (mtg["city_name"], mtg.get("state"))
        if key in cities_with_patterns:
            continue  # pattern projection already had its say
        dt = _parse_meeting_dt(today_str, mtg.get("meeting_time"))
        if dt is None:
            keep.add(key)  # unknown time → poll conservatively
        elif (dt - timedelta(hours=early_hours)) <= now <= (dt + timedelta(hours=late_hours)):
            keep.add(key)

    return [c for c in cities if (c["city"], c.get("state")) in keep]


def _gated_candidates(cities: List[Dict], early_hours: float, late_hours: float,
                      now: Optional[datetime] = None) -> List[Dict]:
    """Cities to poll this pass: those in a meeting window now, PLUS any that are
    currently live (so we notice when they go off air)."""
    candidates = list(cities_in_window(cities, early_hours, late_hours, now))
    seen = {(c["city"], c.get("state")) for c in candidates}
    live_keys = {(s["city_name"], s.get("state")) for s in database.get_live_streams()}
    for c in cities:
        key = (c["city"], c.get("state"))
        if key in live_keys and key not in seen:
            candidates.append(c)
            seen.add(key)
    return candidates


def detect_for_city(api_key: str, city: Dict) -> int:
    """Check one city's channel for a live broadcast; update `live_streams`.

    `city` is a dict from database.get_cities_with_youtube_channel().
    Returns 1 if a live broadcast was found, else 0.
    """
    name = city["city"]
    state = city.get("state")
    county = city.get("county")
    channel_url = city.get("channel_url") or ""
    channel_id = city.get("channel_id") or ""

    try:
        live: Optional[youtube_data_api.LiveBroadcast] = youtube_data_api.find_live_broadcast(
            api_key, channel_url=channel_url, channel_id=channel_id
        )
    except youtube_data_api.YouTubeDataApiError as e:
        logger.warning("live check failed for %s: %s", name, e)
        return 0

    if live is None:
        database.mark_city_live_streams_ended(name, state, keep_video_ids=[])
        logger.info("%s: not live", name)
        return 0

    # Cache the resolved channel_id back on the city so future polls skip the
    # 1-unit channel resolution call.
    if live.channel_id and not channel_id:
        try:
            database.set_city_youtube_channel(
                name, channel_url=channel_url, channel_id=live.channel_id,
                state=state, county=county,
            )
        except Exception as e:  # non-fatal — the detection still succeeded
            logger.debug("could not cache channel_id for %s: %s", name, e)

    database.upsert_live_stream(
        city_name=name, state=state, county=county,
        channel_id=live.channel_id, video_id=live.video_id,
        video_url=live.url, title=live.title, started_at=live.started_at,
    )
    database.mark_city_live_streams_ended(name, state, keep_video_ids=[live.video_id])
    logger.info("%s: LIVE -> %s (%s)", name, (live.title or "")[:60], live.url)
    return 1


def detect_live_streams(only_city: Optional[str] = None, calendar_gated: bool = True,
                        early_hours: float = 2.0,
                        late_hours: float = 3.0) -> Dict[str, int]:
    """Run one detection pass.

    - only_city: restrict to a single city (testing).
    - calendar_gated: poll only cities in their meeting time-window now, PLUS
      any currently-live city (to detect when it ends). When False, poll every
      registered channel (burns quota).
    """
    api_key = get_youtube_data_api_key()
    if not api_key:
        raise youtube_data_api.YouTubeDataApiError(
            "YOUTUBE_DATA_API_KEY not configured (env or user_settings.json)."
        )

    cities = database.get_cities_with_youtube_channel()
    if only_city:
        cities = [c for c in cities if c["city"].lower() == only_city.lower()]
    elif calendar_gated:
        cities = _gated_candidates(cities, early_hours, late_hours)

    live_count = 0
    for c in cities:
        live_count += detect_for_city(api_key, c)

    logger.info("detection pass: polled=%d live=%d", len(cities), live_count)
    return {"polled": len(cities), "live": live_count}


def run_loop(interval: int = 300, min_poll_interval: int = 600,
             early_hours: float = 2.0, late_hours: float = 3.0) -> None:
    """Run the gated detection pass every `interval` seconds, forever.

    A per-city throttle (`min_poll_interval`) bounds how often any one channel
    is hit, decoupling loop cadence from YouTube quota. In-memory throttle
    (resets on restart — at worst one extra poll per city after a restart).
    """
    api_key = get_youtube_data_api_key()
    if not api_key:
        raise youtube_data_api.YouTubeDataApiError(
            "YOUTUBE_DATA_API_KEY not configured (env or user_settings.json)."
        )
    last_polled: Dict[Tuple, datetime] = {}
    logger.info("guide loop starting: interval=%ss, per-city throttle=%ss, window=-%sh/+%sh",
                interval, min_poll_interval, early_hours, late_hours)
    while True:
        now = datetime.now()
        cities = database.get_cities_with_youtube_channel()
        candidates = _gated_candidates(cities, early_hours, late_hours, now)
        polled = 0
        for c in candidates:
            key = (c["city"], c.get("state"))
            last = last_polled.get(key)
            if last and (now - last).total_seconds() < min_poll_interval:
                continue
            detect_for_city(api_key, c)
            last_polled[key] = now
            polled += 1
        logger.info("loop pass: candidates=%d polled=%d throttled=%d",
                    len(candidates), polled, len(candidates) - polled)
        _time.sleep(interval)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console em-dash safety
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Guide live-stream detector (S-015)")
    p.add_argument("--city", help="poll only this city (testing)")
    p.add_argument("--all", action="store_true",
                   help="poll every registered channel, ignoring the calendar "
                        "gate (testing — burns quota)")
    p.add_argument("--loop", action="store_true", help="run continuously on a cadence")
    p.add_argument("--interval", type=int, default=300,
                   help="loop interval in seconds (default 300)")
    p.add_argument("--min-poll-interval", type=int, default=600,
                   help="min seconds between polls of the same channel in loop "
                        "mode (default 600)")
    p.add_argument("--early-hours", type=float, default=2.0,
                   help="poll-window start, hours before a meeting (default 2)")
    p.add_argument("--late-hours", type=float, default=3.0,
                   help="poll-window end, hours after a meeting (default 3)")
    args = p.parse_args(argv)

    database.init_notebook_schema()  # ensure the live_streams table exists

    if args.loop:
        try:
            run_loop(interval=args.interval, min_poll_interval=args.min_poll_interval,
                     early_hours=args.early_hours, late_hours=args.late_hours)
        except KeyboardInterrupt:
            print("\nstopped")
        return 0

    res = detect_live_streams(
        only_city=args.city,
        calendar_gated=not args.all and not args.city,
        early_hours=args.early_hours, late_hours=args.late_hours,
    )
    print(f"polled={res['polled']} live={res['live']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

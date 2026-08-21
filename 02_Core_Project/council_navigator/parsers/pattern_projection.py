#!/usr/bin/env python3.11
"""
Project upcoming meeting dates from a city's curated `meeting_patterns[]`
(Phase H — Guide data engine).

Pure date math against the cadence shape; no DB writes, no network calls,
no LLM. H-5 will switch `guide_detector.py`'s calendar-gate to consult
this helper FIRST + fall back to scraped instances when a city has no
patterns yet. H-3 will compare projection against scrape to fill the
`pattern_health` table.

Public API:

    get_upcoming_meetings_from_patterns(
        city_name: str,
        days_ahead: int = 14,
        start_date: Optional[date] = None,
    ) -> List[Dict]

Returns a list of projected meeting instances, each:

    {
        "pattern_id": str,
        "meeting_type": str,
        "date": "YYYY-MM-DD",
        "time_local": "H:MM AM/PM",     # or whatever the pattern stored
        "datetime": datetime,            # date + parsed time, local zone
        "location": str | None,
        "youtube_channel_url": str | None,
        "source": "pattern",             # discriminator for H-5
    }

Sorted by datetime ascending. Exceptions are removed. Patterns with
`frequency == "adhoc"` contribute nothing (they can't be projected).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# Centralized in this module so callers don't have to know about the
# city_intelligence layout to use the projection helper.
_HERE = Path(__file__).resolve().parent
_CITY_INTELLIGENCE_DIR = _HERE.parent / "city_intelligence"

# 0 = Monday in Python's date.weekday() — match this to schema casing.
DAYS_OF_WEEK_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Time parsing — mirrors guide_detector._parse_meeting_dt's tolerance so
# pattern projection round-trips cleanly against scraped meeting_time
# values.
_TIME_AMPM_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", re.IGNORECASE
)
_TIME_24_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def _parse_time(time_str: Optional[str]) -> Optional[dtime]:
    """Best-effort parse of a time string. Returns None if unparseable."""
    if not time_str:
        return None
    s = time_str.strip()
    m = _TIME_AMPM_RE.search(s)
    if m:
        hour = int(m.group(1)) % 12
        minute = int(m.group(2) or 0)
        if m.group(3).lower() == "p":
            hour += 12
        try:
            return dtime(hour, minute)
        except ValueError:
            return None
    m = _TIME_24_RE.match(s)
    if m:
        try:
            return dtime(int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _city_slug(city_name: str) -> str:
    """Match the city_intelligence/ filename convention from RECIPE.md
    (lowercase + snake_case)."""
    return city_name.strip().lower().replace(" ", "_")


def _load_city_patterns(city_name: str) -> List[Dict]:
    """Load `meeting_patterns[]` from the city's intelligence JSON.
    Returns empty list if the file or field is missing — callers should
    treat that as "no projection available" and fall back to scrape gating."""
    slug = _city_slug(city_name)
    path = _CITY_INTELLIGENCE_DIR / f"{slug}.json"
    if not path.is_file():
        return []
    try:
        city = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    patterns = city.get("meeting_patterns")
    if not isinstance(patterns, list):
        return []
    return patterns


# ─────────────────────────────────────────────────────────────────
# Per-cadence projection helpers
# ─────────────────────────────────────────────────────────────────


def _week_of_month(d: date) -> int:
    """1..5 — which Nth occurrence of (its weekday) `d` is in its month.

    Matches the convention used in H-1's extraction reasoning + the
    schema's `weeks_of_month` semantics. Day 1-7 → week 1, day 8-14 →
    week 2, day 15-21 → week 3, day 22-28 → week 4, day 29-31 → week 5.
    """
    return (d.day - 1) // 7 + 1


def _month_iter(start: date, end: date):
    """Yield (year, month) pairs that intersect [start, end] inclusive."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y += 1
            m = 1


def _dates_for_monthly_weeks(
    cadence: Dict, window_start: date, window_end: date,
) -> List[date]:
    """All in-window dates matching a `monthly_weeks` cadence (weeks_of_month
    × day_of_week, optionally filtered by months_of_year)."""
    weeks = cadence.get("weeks_of_month") or []
    day_name = (cadence.get("day_of_week") or "").lower()
    if not weeks or day_name not in DAYS_OF_WEEK_INDEX:
        return []
    target_weekday = DAYS_OF_WEEK_INDEX[day_name]
    months_filter = cadence.get("months_of_year")  # None → every month

    out: List[date] = []
    for y, m in _month_iter(window_start, window_end):
        if months_filter is not None and m not in months_filter:
            continue
        # Walk the month and pick days that match (weekday + week-of-month).
        day = 1
        while True:
            try:
                d = date(y, m, day)
            except ValueError:
                break  # past end of month
            if d.weekday() == target_weekday and _week_of_month(d) in weeks:
                if window_start <= d <= window_end:
                    out.append(d)
            day += 1
    return out


def _dates_for_weekly(
    cadence: Dict, window_start: date, window_end: date,
) -> List[date]:
    day_name = (cadence.get("day_of_week") or "").lower()
    if day_name not in DAYS_OF_WEEK_INDEX:
        return []
    target_weekday = DAYS_OF_WEEK_INDEX[day_name]
    out: List[date] = []
    d = window_start
    while d <= window_end:
        if d.weekday() == target_weekday:
            out.append(d)
        d += timedelta(days=1)
    return out


def _dates_for_biweekly(
    cadence: Dict, window_start: date, window_end: date,
) -> List[date]:
    day_name = (cadence.get("day_of_week") or "").lower()
    anchor_str = cadence.get("anchor_date")
    if day_name not in DAYS_OF_WEEK_INDEX or not anchor_str:
        return []
    target_weekday = DAYS_OF_WEEK_INDEX[day_name]
    try:
        anchor = datetime.strptime(anchor_str, "%Y-%m-%d").date()
    except ValueError:
        return []
    if anchor.weekday() != target_weekday:
        # Anchor must itself be on day_of_week — otherwise the pattern
        # is internally inconsistent. Skip rather than fudge.
        return []
    out: List[date] = []
    d = window_start
    while d <= window_end:
        if d.weekday() == target_weekday and (d - anchor).days % 14 == 0:
            out.append(d)
        d += timedelta(days=1)
    return out


def _dates_for_monthly_date(
    cadence: Dict, window_start: date, window_end: date,
) -> List[date]:
    dom = cadence.get("date_of_month")
    if not isinstance(dom, int) or not (1 <= dom <= 31):
        return []
    out: List[date] = []
    for y, m in _month_iter(window_start, window_end):
        try:
            d = date(y, m, dom)
        except ValueError:
            continue  # e.g., Feb 30 — skip months where this date doesn't exist
        if window_start <= d <= window_end:
            out.append(d)
    return out


def _dates_for_twice_monthly(
    cadence: Dict, window_start: date, window_end: date,
) -> List[date]:
    doms = cadence.get("days_of_month") or []
    if not (isinstance(doms, list) and len(doms) == 2):
        return []
    if not all(isinstance(d, int) and 1 <= d <= 31 for d in doms):
        return []
    out: List[date] = []
    for y, m in _month_iter(window_start, window_end):
        for dom in doms:
            try:
                d = date(y, m, dom)
            except ValueError:
                continue
            if window_start <= d <= window_end:
                out.append(d)
    return out


_CADENCE_DISPATCH = {
    "weekly": _dates_for_weekly,
    "biweekly": _dates_for_biweekly,
    "monthly_weeks": _dates_for_monthly_weeks,
    "monthly_date": _dates_for_monthly_date,
    "twice_monthly": _dates_for_twice_monthly,
    # "adhoc" → no projection (the dispatch table omits it)
}


def project_pattern(
    pattern: Dict, window_start: date, window_end: date,
) -> List[Dict]:
    """Project a single pattern's recurring instances within [window_start,
    window_end] inclusive. Returns a list of projected-meeting dicts."""
    cadence = pattern.get("cadence") or {}
    freq = cadence.get("frequency")
    dispatcher = _CADENCE_DISPATCH.get(freq)
    if dispatcher is None:
        return []  # adhoc or unknown frequency — no projection

    candidate_dates = dispatcher(cadence, window_start, window_end)

    # Apply per-pattern exceptions: skip any date in the exceptions list.
    exception_dates = {
        ex.get("date")
        for ex in (pattern.get("exceptions") or [])
        if isinstance(ex, dict) and isinstance(ex.get("date"), str)
    }
    if exception_dates:
        candidate_dates = [
            d for d in candidate_dates if d.isoformat() not in exception_dates
        ]

    time_obj = _parse_time(pattern.get("time_local"))
    out: List[Dict] = []
    for d in candidate_dates:
        dt = datetime.combine(d, time_obj) if time_obj else datetime.combine(d, dtime(0, 0))
        out.append({
            "pattern_id": pattern.get("pattern_id"),
            "meeting_type": pattern.get("meeting_type"),
            "date": d.isoformat(),
            "time_local": pattern.get("time_local"),
            "datetime": dt,
            "location": pattern.get("location"),
            "youtube_channel_url": pattern.get("youtube_channel_url"),
            "source": "pattern",
        })
    return out


def get_upcoming_meetings_from_patterns(
    city_name: str,
    days_ahead: int = 14,
    start_date: Optional[date] = None,
) -> List[Dict]:
    """Project the city's curated `meeting_patterns[]` over the next
    `days_ahead` days, starting from `start_date` (defaults to today).

    Returns projected meeting instances sorted by datetime ascending.
    Empty list if the city has no patterns curated yet — callers should
    treat that as "fall back to scrape gating" per H-5.
    """
    window_start = start_date or date.today()
    window_end = window_start + timedelta(days=days_ahead)
    patterns = _load_city_patterns(city_name)
    projected: List[Dict] = []
    for p in patterns:
        projected.extend(project_pattern(p, window_start, window_end))
    projected.sort(key=lambda m: m["datetime"])
    return projected


def project_patterns_for_window(
    patterns: List[Dict], window_start: date, window_end: date,
) -> List[Dict]:
    """Lower-level helper for callers that already have patterns in hand
    (e.g., H-3 reconciliation). Same return shape, no I/O."""
    out: List[Dict] = []
    for p in patterns:
        out.extend(project_pattern(p, window_start, window_end))
    out.sort(key=lambda m: m["datetime"])
    return out


__all__ = [
    "DAYS_OF_WEEK_INDEX",
    "project_pattern",
    "project_patterns_for_window",
    "get_upcoming_meetings_from_patterns",
]

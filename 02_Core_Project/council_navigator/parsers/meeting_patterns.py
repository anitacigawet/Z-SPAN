#!/usr/bin/env python3.11
"""
Meeting-pattern schema validator (Phase H — Guide data engine).

Validates the `meeting_patterns[]` field inside each city's
`city_intelligence/<city>.json`. The field is the canonical, hand-curated
description of a city's recurring meeting types (City Council = 2nd + 4th
Tuesday at 5pm, etc.). H-2 will use the same shape to project upcoming
meeting instances; H-3/H-4 will compare projections against scraped
instances and log to the `pattern_health` table.

The validator is pure-Python with no external deps (no jsonschema package
gets added) — small, fast, and easy to extend as the schema grows.

Usage:
    from meeting_patterns import validate_meeting_patterns
    ok, errors = validate_meeting_patterns(city_json["meeting_patterns"])
    if not ok:
        raise ValueError("\n".join(errors))

Reference shape (see city_intelligence/RECIPE.md § Meeting patterns schema
for the full prose, examples, and curation workflow):

    [
        {
            "pattern_id": "city_council",          # unique within city
            "meeting_type": "City Council",        # human label
            "cadence": {
                "frequency": "monthly_weeks",      # see CADENCE_FREQUENCIES
                "weeks_of_month": [2, 4],          # 2nd + 4th
                "day_of_week": "Tuesday",
                "months_of_year": [1, 4, 7, 10]    # optional — quarterly /
                                                    # bi-monthly bodies
            },
            "time_local": "17:00",                 # 24h or "5:00 PM"
            "location": "City Council Chambers",
            "youtube_channel_url": "https://...",  # optional; per-pattern channel
            "exceptions": [
                {"date": "2026-11-26", "reason": "Thanksgiving week"}
            ],
            "source_url": "https://...",           # where the pattern was verified
            "verified_on": "2026-06-02",
            "notes": "..."                          # free text
        }
    ]
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# The supported cadence frequencies. Each implies a different set of
# required cadence sub-fields:
#   weekly         → day_of_week
#   biweekly       → day_of_week + anchor_date (so projection knows phase)
#   monthly_weeks  → weeks_of_month [1-5] + day_of_week
#   monthly_date   → date_of_month [1-31]
#   twice_monthly  → days_of_month [1-31, 1-31] (e.g. 1st + 15th)
#   adhoc          → no cadence sub-fields required; projection skips it
CADENCE_FREQUENCIES = {
    "weekly",
    "biweekly",
    "monthly_weeks",
    "monthly_date",
    "twice_monthly",
    "adhoc",
}

DAYS_OF_WEEK = {
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
}

REQUIRED_PATTERN_FIELDS = (
    "pattern_id",
    "meeting_type",
    "cadence",
    "time_local",
    "source_url",
    "verified_on",
)

# YYYY-MM-DD (strict — H-1 curation should normalize to this)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 24h "HH:MM" or 12h "H:MM AM/PM" / "H AM" / "H pm" — same tolerance as
# guide_detector._parse_meeting_dt so meeting_time strings round-trip cleanly.
_TIME_24_RE = re.compile(r"^\d{1,2}:\d{2}$")
_TIME_12_RE = re.compile(r"^\d{1,2}(?::\d{2})?\s*[ap]\.?\s*m\.?$", re.IGNORECASE)
# pattern_id: lowercase + snake_case, matches the convention in
# RECIPE.md for council seat_id ("seat_1", "mayor"), keeps SQL-safe.
_PATTERN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _err(errors: List[str], path: str, msg: str) -> None:
    errors.append(f"{path}: {msg}")


def _validate_time(value: Any, path: str, errors: List[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        _err(errors, path, "must be a non-empty string (e.g. '17:00' or '5:00 PM')")
        return
    s = value.strip()
    if not (_TIME_24_RE.match(s) or _TIME_12_RE.match(s)):
        _err(errors, path, f"unrecognized time format: {value!r}")


def _validate_date(value: Any, path: str, errors: List[str]) -> None:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        _err(errors, path, f"must be 'YYYY-MM-DD', got {value!r}")


def _validate_cadence(cadence: Any, path: str, errors: List[str]) -> None:
    if not isinstance(cadence, dict):
        _err(errors, path, "must be an object")
        return

    freq = cadence.get("frequency")
    if freq not in CADENCE_FREQUENCIES:
        _err(errors, f"{path}.frequency",
             f"must be one of {sorted(CADENCE_FREQUENCIES)}, got {freq!r}")
        return  # nothing else makes sense without a valid frequency

    if freq in ("weekly", "biweekly", "monthly_weeks"):
        day = cadence.get("day_of_week")
        if not isinstance(day, str) or day.strip().lower() not in DAYS_OF_WEEK:
            _err(errors, f"{path}.day_of_week",
                 f"must be a weekday name (Monday..Sunday), got {day!r}")

    if freq == "monthly_weeks":
        weeks = cadence.get("weeks_of_month")
        if not isinstance(weeks, list) or not weeks:
            _err(errors, f"{path}.weeks_of_month",
                 "must be a non-empty list of integers 1-5")
        else:
            for i, w in enumerate(weeks):
                if not isinstance(w, int) or not (1 <= w <= 5):
                    _err(errors, f"{path}.weeks_of_month[{i}]",
                         f"must be an int 1-5, got {w!r}")
        # Optional months_of_year filter — express quarterly / bi-monthly
        # cadences (e.g. Parks Commission meets 3rd Wed of Feb/May/Aug/Nov).
        # When omitted, projection assumes the pattern fires every month.
        moy = cadence.get("months_of_year")
        if moy is not None:
            if not isinstance(moy, list) or not moy:
                _err(errors, f"{path}.months_of_year",
                     "must be a non-empty list of ints 1-12, or omitted")
            else:
                for i, m in enumerate(moy):
                    if not isinstance(m, int) or not (1 <= m <= 12):
                        _err(errors, f"{path}.months_of_year[{i}]",
                             f"must be an int 1-12, got {m!r}")

    if freq == "biweekly":
        anchor = cadence.get("anchor_date")
        if anchor is not None:
            _validate_date(anchor, f"{path}.anchor_date", errors)
        else:
            _err(errors, f"{path}.anchor_date",
                 "biweekly cadence needs an anchor_date so projection knows phase")

    if freq == "monthly_date":
        dom = cadence.get("date_of_month")
        if not isinstance(dom, int) or not (1 <= dom <= 31):
            _err(errors, f"{path}.date_of_month",
                 f"must be an int 1-31, got {dom!r}")

    if freq == "twice_monthly":
        doms = cadence.get("days_of_month")
        if not isinstance(doms, list) or len(doms) != 2:
            _err(errors, f"{path}.days_of_month",
                 "must be a 2-element list of ints 1-31")
        else:
            for i, d in enumerate(doms):
                if not isinstance(d, int) or not (1 <= d <= 31):
                    _err(errors, f"{path}.days_of_month[{i}]",
                         f"must be an int 1-31, got {d!r}")


def _validate_exceptions(value: Any, path: str, errors: List[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        _err(errors, path, "must be a list (or omitted)")
        return
    for i, ex in enumerate(value):
        sub = f"{path}[{i}]"
        if not isinstance(ex, dict):
            _err(errors, sub, "each exception must be an object {date, reason}")
            continue
        if "date" not in ex:
            _err(errors, sub, "missing required field 'date'")
        else:
            _validate_date(ex["date"], f"{sub}.date", errors)
        if "reason" in ex and not isinstance(ex["reason"], str):
            _err(errors, f"{sub}.reason", "must be a string when present")


def validate_meeting_pattern(pattern: Any, path: str = "pattern") -> List[str]:
    """Validate a single meeting_pattern dict. Returns a list of error strings
    (empty list = valid)."""
    errors: List[str] = []
    if not isinstance(pattern, dict):
        _err(errors, path, "must be an object")
        return errors

    for field in REQUIRED_PATTERN_FIELDS:
        if field not in pattern:
            _err(errors, path, f"missing required field {field!r}")

    pid = pattern.get("pattern_id")
    if pid is not None:
        if not isinstance(pid, str) or not _PATTERN_ID_RE.match(pid):
            _err(errors, f"{path}.pattern_id",
                 "must be lowercase snake_case starting with a letter "
                 "(e.g. 'city_council', 'p_and_z_board'), got "
                 f"{pid!r}")

    if "meeting_type" in pattern and not isinstance(pattern["meeting_type"], str):
        _err(errors, f"{path}.meeting_type", "must be a string")

    if "cadence" in pattern:
        _validate_cadence(pattern["cadence"], f"{path}.cadence", errors)

    if "time_local" in pattern:
        _validate_time(pattern["time_local"], f"{path}.time_local", errors)

    if "verified_on" in pattern:
        _validate_date(pattern["verified_on"], f"{path}.verified_on", errors)

    if "source_url" in pattern:
        s = pattern["source_url"]
        if not isinstance(s, str) or not s.startswith(("http://", "https://")):
            _err(errors, f"{path}.source_url",
                 f"must be an http(s) URL, got {s!r}")

    # Optional fields — only type-check when present.
    for opt_str in ("location", "youtube_channel_url", "notes"):
        if opt_str in pattern and pattern[opt_str] is not None \
                and not isinstance(pattern[opt_str], str):
            _err(errors, f"{path}.{opt_str}", "must be a string when present")

    _validate_exceptions(pattern.get("exceptions"), f"{path}.exceptions", errors)

    return errors


def validate_meeting_patterns(patterns: Any) -> Tuple[bool, List[str]]:
    """Validate a `meeting_patterns[]` list (the value of the field in
    `city_intelligence/<city>.json`).

    Returns (ok, errors). `ok` is True iff `errors` is empty.

    Also enforces uniqueness of `pattern_id` within the list, since the
    `pattern_health` table keys per-(city, pattern_id) — duplicates would
    silently merge their health rows.
    """
    errors: List[str] = []
    if patterns is None or patterns == []:
        # An empty list is structurally valid — a city with no curated
        # patterns yet falls back to scraped-instance gating (H-5).
        return True, errors
    if not isinstance(patterns, list):
        return False, ["meeting_patterns: must be a list (or omitted)"]

    seen_ids: Dict[str, int] = {}
    for i, p in enumerate(patterns):
        path = f"meeting_patterns[{i}]"
        errors.extend(validate_meeting_pattern(p, path))
        if isinstance(p, dict):
            pid = p.get("pattern_id")
            if isinstance(pid, str):
                if pid in seen_ids:
                    _err(errors, path,
                         f"duplicate pattern_id {pid!r} (first seen at index {seen_ids[pid]})")
                else:
                    seen_ids[pid] = i

    return (not errors), errors


def find_pattern(patterns: List[Dict], pattern_id: str) -> Optional[Dict]:
    """Convenience lookup — first pattern in `patterns` with the given
    pattern_id, or None. H-2/H-3/H-4 use this for per-pattern operations."""
    if not isinstance(patterns, list):
        return None
    for p in patterns:
        if isinstance(p, dict) and p.get("pattern_id") == pattern_id:
            return p
    return None


__all__ = [
    "CADENCE_FREQUENCIES",
    "DAYS_OF_WEEK",
    "REQUIRED_PATTERN_FIELDS",
    "validate_meeting_pattern",
    "validate_meeting_patterns",
    "find_pattern",
]

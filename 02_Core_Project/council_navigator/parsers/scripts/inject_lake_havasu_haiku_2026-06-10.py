#!/usr/bin/env python3.11
"""One-time injection of Lake Havasu City meetings extracted by a Haiku
sub-agent probe against `https://lakehavasucity.legistar.com/Calendar.aspx`
on 2026-06-10.

**Why this script exists**

Lake Havasu's Legistar RSS feed (the source `lake_havasu_city_parser.py`
hits) is structurally limited: it returns only ~30 stale items from older
years, NOT current-year meetings. Confirmed via direct curl 2026-06-10 —
Mode=2025 and Mode=2026 both return the same pre-2026 set, but the public
Calendar.aspx page has all June 2026 meetings.

The Haiku-class sub-agent probe (Agent tool, model=haiku) successfully
extracted 6 June 2026 meetings from Calendar.aspx HTML in ~33 seconds at
~30k tokens. Quality was high (canonical-schema match, identified
cancellations via Status field, captured Legistar meeting IDs). One
caveat noted in the probe's own output: one meeting had its `video_url`
hallucinated as the calendar page URL — corrected here.

This file is a **one-time operator-driven cache injection** to unblock V1
data acquisition for Lake Havasu. The proper production architecture
(a `claude -p`-invoked Haiku scraper agent, schedulable + repeatable)
is scoped under FUTURE_THOUGHTS S-036. Don't re-run this script blindly
in future sessions; re-running would be safe (cache_meetings is UPSERT
per D-038) but the data would be stale relative to whatever Lake Havasu
has published since.

Provenance trail: the probe's full raw output is committed alongside this
script via the operator-ping it surfaced in.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `parsers/` importable
_HERE = Path(__file__).resolve().parent
_PARSERS_DIR = _HERE.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import cache_meetings
from normalize import normalize_meeting_fields

CITY = "Lake Havasu City"
COUNTY = "Mohave County"

# Verbatim from the Haiku probe output 2026-06-10. The one hallucinated
# `video_url` (was the calendar page URL) is cleared here; the cancellation
# status is preserved verbatim.
HAIKU_PROBE_MEETINGS_2026_06_10 = [
    {
        "Meeting Title/Name": "Planning and Zoning Commission",
        "Meeting Date": "2026-06-03",
        "Meeting Time": "9:00 AM",
        "Meeting Location": "Council Chambers",
        "Agenda URL": "https://lakehavasucity.legistar.com/View.ashx?M=A&ID=1403052&GUID=B84BFAC4-8B00-4C5C-A8E6-40EC3C589B54",
        "Minutes URL": "https://lakehavasucity.legistar.com/View.ashx?M=E2&ID=1403052&GUID=B84BFAC4-8B00-4C5C-A8E6-40EC3C589B54",
        "Video URL": "",
        "Agenda Packet URL": "",
        "Meeting Status": "",
        "eComment/Public Comment URL": "",
        "Meeting ID": "1403052",
    },
    {
        "Meeting Title/Name": "City Council",
        "Meeting Date": "2026-06-09",
        "Meeting Time": "5:30 PM",
        "Meeting Location": "Council Chambers",
        "Agenda URL": "https://lakehavasucity.legistar.com/View.ashx?M=A&ID=1402550&GUID=9315B239-1A9C-4F89-B270-F16E8C9C192A",
        "Minutes URL": "",
        "Video URL": "",
        "Agenda Packet URL": "",
        "Meeting Status": "",
        "eComment/Public Comment URL": "",
        "Meeting ID": "1402550",
    },
    {
        "Meeting Title/Name": "Board of Adjustment",
        "Meeting Date": "2026-06-10",
        "Meeting Time": "9:00 AM",
        "Meeting Location": "",
        "Agenda URL": "",
        "Minutes URL": "",
        "Video URL": "",
        "Agenda Packet URL": "",
        "Meeting Status": "Cancelled",
        "eComment/Public Comment URL": "",
        "Meeting ID": "1403054",
    },
    {
        "Meeting Title/Name": "Planning and Zoning Commission",
        "Meeting Date": "2026-06-17",
        "Meeting Time": "9:00 AM",
        "Meeting Location": "Council Chambers",
        "Agenda URL": "",
        "Minutes URL": "",
        "Video URL": "",
        "Agenda Packet URL": "",
        "Meeting Status": "",
        "eComment/Public Comment URL": "",
        "Meeting ID": "1403053",
    },
    {
        "Meeting Title/Name": "City Council",
        "Meeting Date": "2026-06-23",
        "Meeting Time": "5:30 PM",
        "Meeting Location": "Council Chambers",
        "Agenda URL": "",
        "Minutes URL": "",
        "Video URL": "",
        "Agenda Packet URL": "",
        "Meeting Status": "",
        "eComment/Public Comment URL": "",
        "Meeting ID": "1402552",
    },
    {
        "Meeting Title/Name": "Board of Adjustment",
        "Meeting Date": "2026-06-24",
        "Meeting Time": "9:00 AM",
        "Meeting Location": "",
        "Agenda URL": "",
        "Minutes URL": "",
        "Video URL": "",
        "Agenda Packet URL": "",
        "Meeting Status": "Cancelled",
        "eComment/Public Comment URL": "",
        "Meeting ID": "1403055",
    },
]


def main() -> int:
    normalized = [normalize_meeting_fields(m) for m in HAIKU_PROBE_MEETINGS_2026_06_10]
    cache_meetings(CITY, COUNTY, normalized)
    print(f"Injected {len(normalized)} meetings for {CITY} ({COUNTY})")
    for m in normalized:
        cancel = " [CANCELLED]" if (m.get("meeting_status") or "").lower() == "cancelled" else ""
        print(f"  {m.get('meeting_date')} {m.get('meeting_time')} - {m.get('meeting_title')}{cancel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

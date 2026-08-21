#!/usr/bin/env python3.11
"""V1-Batch-1 — City + window scanner (read-only).

Surfaces meeting candidates for the V1 public-release batch tool per the
V1_PUBLIC_RELEASE_SPEC. For each city in the input list, lists all meetings
scraped within a backward-looking date window and cross-references them
against the work_orders table to show current processing state. Pattern
projection is also consulted to surface cadence-vs-scrape drift (meetings
the curated `meeting_patterns[]` says should exist but the scraper didn't
catch).

Read-only — no DB writes, no work-order creation, no synthesis calls.

Foundation for V1-Batch-2 (work-order creator) which acts on this scan.

Usage:
    python3.11 v1_batch_scan.py
    python3.11 v1_batch_scan.py --cities "Kingman,Bullhead City"
    python3.11 v1_batch_scan.py --days-back 7
    python3.11 v1_batch_scan.py --end-date 2026-06-10
    python3.11 v1_batch_scan.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# Bootstrap import path so the script can run from anywhere — parsers/ is the
# importable layer.
_HERE = Path(__file__).resolve().parent
_PARSERS_DIR = _HERE.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import get_connection, cache_meetings, enqueue_work_order
from normalize import normalize_meeting_fields
from parser_loader import scrape_city_calendar, load_parser_index
from pattern_projection import project_patterns_for_window

# Sibling to parsers/, where pattern_projection reads patterns from.
_CITY_INTELLIGENCE_DIR = _PARSERS_DIR.parent / "city_intelligence"

DEFAULT_CITIES = [
    "Kingman",
    "Bullhead City",
    "Lake Havasu City",
    "Colorado City",
]
DEFAULT_DAYS_BACK = 14

# V1 SPEC requested_outputs — drops Studio media (audio_overview /
# video_explainer / infographic) for V1 per the SPEC's "default: no Studio
# media" guidance. Studio media is expensive + James can flip back to the
# full set via --with-studio-media when scoping the actual batch fire.
V1_REQUESTED_OUTPUTS = (
    "episode_tagline,episode_tags,synopsis,newsletter,key_decisions,"
    "whats_next,council_sentiment,suggested_questions,member_attendance,"
    "transcript_words,tracked_claims,quotes"
)
FULL_REQUESTED_OUTPUTS = (
    "episode_tagline,episode_tags,synopsis,newsletter,key_decisions,"
    "whats_next,council_sentiment,suggested_questions,audio_overview,"
    "video_explainer,infographic,member_attendance,transcript_words,"
    "tracked_claims,quotes"
)


def _city_slug(city_name: str) -> str:
    return city_name.strip().lower().replace(" ", "_")


def load_city_patterns(city_name: str) -> List[Dict]:
    """Return the city's curated meeting_patterns[] or an empty list if none.

    Mirrors pattern_projection's loader semantics: empty list means "no
    projection available; fall back to scrape gating only" (per H-5 framing).
    """
    path = _CITY_INTELLIGENCE_DIR / f"{_city_slug(city_name)}.json"
    if not path.is_file():
        return []
    try:
        city = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    patterns = city.get("meeting_patterns")
    return patterns if isinstance(patterns, list) else []


def refresh_city_cache(city_name: str) -> Dict:
    """Re-scrape the city's calendar + upsert the meetings table.

    Returns a small report dict for the operator's view of what happened:
    `{ok: bool, meeting_count: int, county: str, error: Optional[str]}`.
    Failure of one city's refresh doesn't halt the run — the scan still
    reports what's in cache for that city.
    """
    try:
        idx = load_parser_index()
        if city_name not in idx:
            return {
                "ok": False, "meeting_count": 0, "county": None,
                "error": "not in parser_index.json",
            }
        county = idx[city_name].get("county", "Unknown")
        meetings = scrape_city_calendar(city_name)
        normalized = [normalize_meeting_fields(m) for m in meetings]
        cache_meetings(city_name, county, normalized)
        return {
            "ok": True, "meeting_count": len(normalized), "county": county,
            "error": None,
        }
    except Exception as e:
        return {
            "ok": False, "meeting_count": 0, "county": None,
            "error": f"{type(e).__name__}: {e}",
        }


def query_scraped_meetings(
    city_name: str, window_start: date, window_end: date,
) -> List[Dict]:
    """Read the meetings table for the city's in-window meetings."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, meeting_title, meeting_date, meeting_time,
                   meeting_location, meeting_status,
                   video_url, agenda_url, minutes_url
            FROM meetings
            WHERE city_name = ?
              AND meeting_date >= ?
              AND meeting_date <= ?
            ORDER BY meeting_date DESC, id DESC
            """,
            (city_name, window_start.isoformat(), window_end.isoformat()),
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def query_work_orders_by_meeting_id(meeting_ids: List[int]) -> Dict[int, Dict]:
    """Return {meeting_id: work_order_dict} for the given meeting_ids."""
    if not meeting_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    try:
        placeholders = ",".join("?" for _ in meeting_ids)
        cursor.execute(
            f"""
            SELECT id, meeting_id, state, youtube_video_url, notebook_id,
                   created_at, completed_at, last_error
            FROM work_orders
            WHERE meeting_id IN ({placeholders})
            """,
            meeting_ids,
        )
        return {r["meeting_id"]: dict(r) for r in cursor.fetchall()}
    finally:
        conn.close()


def create_work_orders_for_missing(
    meetings: List[Dict],
    work_orders: Dict[int, Dict],
    requested_outputs: str,
    dry_run: bool = False,
) -> List[Dict]:
    """Create work orders for in-window meetings that don't have one.

    Returns a list of per-creation result dicts:
      {meeting_id, title, date, dry_run, wo_id, state, error}

    Honors the `enqueue_work_order` UPSERT contract — if a WO already exists
    for the meeting_id, this function skips it (the existing WO is returned
    by enqueue_work_order's existing-row code path). For in-window meetings
    with NO WO at all (the common case for V1-Batch-2 fresh acquisition),
    a new WO is created in 'pending' or 'awaiting_video' state per the
    inherited video_url logic in enqueue_work_order.
    """
    results: List[Dict] = []
    for m in meetings:
        if m["id"] in work_orders:
            continue  # already has a WO; don't disturb
        record = {
            "meeting_id": m["id"],
            "title": m["meeting_title"],
            "date": m["meeting_date"],
            "dry_run": dry_run,
            "wo_id": None,
            "state": None,
            "error": None,
        }
        if dry_run:
            # enqueue_work_order defaults state to 'pending' (regardless of
            # whether the meeting has a video_url); only the partial-match
            # case (medium / needs_review confidence on a scraped video_url)
            # lands in 'awaiting_video'. The URL-gap surfacing has to look at
            # whether the WO actually has a youtube_video_url set, not at
            # the state name — see URL-GAP BOARD in render_text.
            record["state"] = "pending"
            results.append(record)
            continue
        try:
            wo_id = enqueue_work_order(
                meeting_id=m["id"],
                requested_outputs=requested_outputs,
            )
            record["wo_id"] = wo_id
            # Re-query state — enqueue_work_order may have applied auto-match
            re_query = query_work_orders_by_meeting_id([m["id"]])
            wo = re_query.get(m["id"])
            record["state"] = wo["state"] if wo else "unknown"
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {e}"
        results.append(record)
    return results


def scan_city(
    city_name: str, window_start: date, window_end: date,
    refresh: bool = True,
    create_mode: bool = False,
    dry_run_create: bool = True,
    requested_outputs: str = V1_REQUESTED_OUTPUTS,
) -> Dict:
    """Build a per-city report for the window. Refreshes the scrape cache
    first (unless `refresh=False`) so the scan reads live data.

    If `create_mode=True`, creates work orders for in-window meetings that
    don't already have one. `dry_run_create=True` (default in create mode)
    shows what WOULD be created without doing it; pass `dry_run_create=False`
    to actually fire enqueue_work_order. The `requested_outputs` string
    controls which outputs each new WO will eventually generate (default:
    V1_REQUESTED_OUTPUTS, which drops Studio media).
    """
    refresh_result: Optional[Dict] = None
    if refresh:
        refresh_result = refresh_city_cache(city_name)
    scraped = query_scraped_meetings(city_name, window_start, window_end)
    patterns = load_city_patterns(city_name)
    projected = project_patterns_for_window(patterns, window_start, window_end)
    work_orders = query_work_orders_by_meeting_id([m["id"] for m in scraped])
    creation_results: Optional[List[Dict]] = None
    if create_mode:
        creation_results = create_work_orders_for_missing(
            meetings=scraped,
            work_orders=work_orders,
            requested_outputs=requested_outputs,
            dry_run=dry_run_create,
        )
        # Re-query WOs after creation so the per-meeting display reflects fresh state
        if not dry_run_create and creation_results:
            work_orders = query_work_orders_by_meeting_id([m["id"] for m in scraped])

    meetings_view: List[Dict] = []
    for m in scraped:
        wo = work_orders.get(m["id"])
        meetings_view.append({
            "meeting_id": m["id"],
            "title": m["meeting_title"],
            "date": m["meeting_date"],
            "time": m["meeting_time"],
            "location": m["meeting_location"],
            "status": m["meeting_status"],
            "has_video_url": bool(m["video_url"]),
            "has_agenda_url": bool(m["agenda_url"]),
            "work_order_id": wo["id"] if wo else None,
            "work_order_state": wo["state"] if wo else None,
            "work_order_youtube_url": wo.get("youtube_video_url") if wo else None,
            "work_order_completed_at": wo.get("completed_at") if wo else None,
        })

    wo_state_counts: Dict[str, int] = {}
    no_wo_count = 0
    for m in meetings_view:
        s = m["work_order_state"]
        if s:
            wo_state_counts[s] = wo_state_counts.get(s, 0) + 1
        else:
            no_wo_count += 1

    scraped_dates = {m["meeting_date"] for m in scraped}
    projected_not_scraped = [
        {
            "pattern_id": p["pattern_id"],
            "meeting_type": p["meeting_type"],
            "date": p["date"],
            "time_local": p["time_local"],
            "location": p["location"],
        }
        for p in projected if p["date"] not in scraped_dates
    ]

    return {
        "city": city_name,
        "refresh": refresh_result,
        "has_curated_patterns": bool(patterns),
        "pattern_count": len(patterns),
        "scraped_meeting_count": len(scraped),
        "projected_meeting_count": len(projected),
        "projected_not_scraped_count": len(projected_not_scraped),
        "wo_state_counts": wo_state_counts,
        "meetings_without_wo": no_wo_count,
        "meetings": meetings_view,
        "projected_not_scraped": projected_not_scraped,
        "creation_results": creation_results,
    }


def _humanize_state(state: Optional[str]) -> str:
    """D-054: surface work-order state as prose, not as a schema label."""
    if state is None:
        return "no work order yet"
    return {
        "pending": "ready to process",
        "awaiting_video": "waiting on YouTube URL",
        "in_progress": "processing now",
        "completed": "processed",
        "failed": "failed",
        "paused": "paused",
    }.get(state, state)


def _format_meeting_line(m: Dict) -> List[str]:
    """Render one meeting as two indented lines."""
    title = m["title"] or "(untitled meeting)"
    when = m["date"]
    if m["time"]:
        when += f" at {m['time']}"
    lead = f"  - {when} - {title}"
    detail_parts = [_humanize_state(m["work_order_state"])]
    if m["work_order_id"]:
        detail_parts.append(f"work order #{m['work_order_id']}")
    if m["work_order_state"] == "completed" and m["work_order_completed_at"]:
        detail_parts.append(f"completed {m['work_order_completed_at'][:10]}")
    if (
        m["work_order_state"] in ("pending", "awaiting_video")
        and not m["work_order_youtube_url"]
    ):
        detail_parts.append("YouTube URL not pasted")
    detail = "    " + " - ".join(detail_parts)
    return [lead, detail]


def render_text(report: Dict) -> str:
    """Operator-readable text report. Sentence-case prose per D-054."""
    lines: List[str] = []
    bar = "=" * 70
    lines.append(bar)
    lines.append(f"V1-Batch-1 scan - {report['window_start']} to {report['window_end']}")
    days = (
        date.fromisoformat(report["window_end"])
        - date.fromisoformat(report["window_start"])
    ).days
    lines.append(f"Window: {days} days")
    lines.append(bar)
    lines.append("")

    for city_report in report["cities"]:
        lines.append(city_report["city"])
        refresh_info = city_report.get("refresh")
        if refresh_info is None:
            pass  # --no-refresh path; don't surface
        elif refresh_info["ok"]:
            lines.append(
                f"  Live-scrape refresh: ok ({refresh_info['meeting_count']} meetings)"
            )
        else:
            lines.append(f"  Live-scrape refresh: FAILED - {refresh_info['error']}")
        if city_report["has_curated_patterns"]:
            lines.append(
                f"  Curated meeting patterns: yes ({city_report['pattern_count']} bodies)"
            )
        else:
            lines.append(
                "  Curated meeting patterns: none (city_intelligence file missing)"
            )
        lines.append(
            f"  Scraped {city_report['scraped_meeting_count']} meeting(s) in window"
        )

        if city_report["meetings"]:
            for m in city_report["meetings"]:
                lines.extend(_format_meeting_line(m))
        else:
            lines.append("    (no meetings)")

        if city_report["projected_not_scraped"]:
            lines.append(
                f"  Pattern projection expected {city_report['projected_not_scraped_count']} "
                f"more meeting(s) the scraper didn't catch:"
            )
            for p in city_report["projected_not_scraped"]:
                expected = p["date"]
                if p["time_local"]:
                    expected += f" at {p['time_local']}"
                lines.append(f"    - {expected} - {p['meeting_type']}")

        # Per-city summary line, D-054 sentence-case
        summary_parts: List[str] = []
        for st, n in city_report["wo_state_counts"].items():
            summary_parts.append(f"{n} {_humanize_state(st)}")
        if city_report["meetings_without_wo"]:
            summary_parts.append(
                f"{city_report['meetings_without_wo']} {_humanize_state(None)}"
            )
        if summary_parts:
            lines.append(f"  Summary: {', '.join(summary_parts)}")
        lines.append("")

    # Totals
    total_meetings = sum(c["scraped_meeting_count"] for c in report["cities"])
    total_processed = sum(
        c["wo_state_counts"].get("completed", 0) for c in report["cities"]
    )
    total_no_wo = sum(c["meetings_without_wo"] for c in report["cities"])
    lines.append(bar)
    lines.append("Totals")
    lines.append(f"  {total_meetings} meeting(s) scraped across {len(report['cities'])} cities")
    lines.append(f"  {total_processed} processed")
    lines.append(f"  {total_no_wo} need work order created (V1-Batch-2 input)")

    # Create-mode results + URL-gap board
    if report.get("create_mode"):
        lines.append("")
        if report.get("dry_run_create"):
            lines.append("V1-Batch-2 CREATE (dry-run — no DB writes)")
        else:
            lines.append("V1-Batch-2 CREATE (executed)")
        outputs_label = "V1 (no Studio media)" if report.get("requested_outputs") == V1_REQUESTED_OUTPUTS else "FULL (with Studio media)"
        lines.append(f"  Requested outputs: {outputs_label}")
        total_would_create = 0
        total_created = 0
        total_errors = 0
        for c in report["cities"]:
            cres = c.get("creation_results") or []
            if not cres:
                continue
            lines.append(f"  {c['city']}: {len(cres)} candidate(s)")
            for r in cres:
                if r["error"]:
                    marker = "ERROR"
                    total_errors += 1
                elif r["dry_run"]:
                    marker = f"would create -> {r['state']}"
                    total_would_create += 1
                else:
                    marker = f"work order #{r['wo_id']} -> {r['state']}"
                    total_created += 1
                lines.append(f"    - {r['date']} - {r['title']}  [{marker}]")
        lines.append("")
        if report.get("dry_run_create"):
            lines.append(f"  Would create: {total_would_create} new work order(s). Errors: {total_errors}")
            lines.append("  Re-run with --no-dry-run to actually create them.")
        else:
            lines.append(f"  Created: {total_created} new work order(s). Errors: {total_errors}")

    # URL-gap board — any non-terminal WO without a YouTube URL. Renders
    # whether or not --create was used; on a plain read-only scan this
    # surfaces existing gaps the operator already has work for.
    NON_TERMINAL = {"pending", "awaiting_video", "processing", "awaiting_notebook"}
    url_gap_rows: List[tuple] = []
    for c in report["cities"]:
        for m in c["meetings"]:
            if (
                m["work_order_id"] is not None
                and m["work_order_state"] in NON_TERMINAL
                and not m["work_order_youtube_url"]
            ):
                url_gap_rows.append((c["city"], m))
    if url_gap_rows:
        lines.append("")
        lines.append(f"URL-GAP BOARD - {len(url_gap_rows)} meeting(s) without auto-assigned video URLs")
        lines.append("  Autonomous path (per D-138): python3.11 parsers/scripts/haiku_match_videos.py --city <name> --apply")
        lines.append("  Manual paste REMOVED (D-138 supersedes D-008/D-009). awaiting_video = coverage-gap, not operator action.")
        for city, m in url_gap_rows:
            lines.append(f"  - work order #{m['work_order_id']} | {city} | {m['date']} {m['time']} | {m['title']}")

    lines.append(bar)
    return "\n".join(lines)


def build_report(
    cities: List[str], window_start: date, window_end: date,
    refresh: bool = True,
    create_mode: bool = False,
    dry_run_create: bool = True,
    requested_outputs: str = V1_REQUESTED_OUTPUTS,
) -> Dict:
    return {
        "scan_run_at": datetime.now().isoformat(timespec="seconds"),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "refresh": refresh,
        "create_mode": create_mode,
        "dry_run_create": dry_run_create if create_mode else None,
        "requested_outputs": requested_outputs if create_mode else None,
        "city_count": len(cities),
        "cities": [
            scan_city(
                c, window_start, window_end,
                refresh=refresh,
                create_mode=create_mode,
                dry_run_create=dry_run_create,
                requested_outputs=requested_outputs,
            )
            for c in cities
        ],
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only V1 batch scanner. Lists meetings in a backward-looking "
            "date window per city + their current work-order state."
        ),
    )
    parser.add_argument(
        "--cities",
        default=",".join(DEFAULT_CITIES),
        help=(
            "Comma-separated city list. Default: Kingman, Bullhead City, "
            "Lake Havasu City, Colorado City (V1 SPEC Mohave County target)."
        ),
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=DEFAULT_DAYS_BACK,
        help=f"How many days back to scan from --end-date. Default: {DEFAULT_DAYS_BACK}.",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help=(
            "ISO date (YYYY-MM-DD) for the window's right edge. Default: today. "
            "Window is [end-date minus days-back, end-date] inclusive."
        ),
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help=(
            "Skip the live-scrape refresh before reading the meetings cache. "
            "Default behavior is to re-scrape each city's calendar first so "
            "the scan reads fresh data; --no-refresh reads pure cache (faster, "
            "but may be stale up to the cache TTL window)."
        ),
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help=(
            "V1-Batch-2: create work orders for in-window meetings that don't "
            "already have one. Default is DRY-RUN — pass --no-dry-run to "
            "actually fire enqueue_work_order. Existing WOs are left alone "
            "regardless (terminal states preserved; non-terminal updated only "
            "with the requested_outputs override)."
        ),
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help=(
            "When --create is set, actually create the work orders (default "
            "is dry-run, which shows what WOULD be created without doing it)."
        ),
    )
    parser.add_argument(
        "--with-studio-media",
        action="store_true",
        help=(
            "When --create is set, request the full output set including "
            "Studio audio/video/infographic. Default V1 set drops those three "
            "(per V1_PUBLIC_RELEASE_SPEC)."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of human-readable text.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    if not cities:
        print("No cities supplied.", file=sys.stderr)
        return 2

    if args.end_date:
        try:
            window_end = date.fromisoformat(args.end_date)
        except ValueError:
            print(f"Bad --end-date: {args.end_date!r}", file=sys.stderr)
            return 2
    else:
        window_end = date.today()
    window_start = window_end - timedelta(days=args.days_back)

    requested_outputs = FULL_REQUESTED_OUTPUTS if args.with_studio_media else V1_REQUESTED_OUTPUTS
    report = build_report(
        cities, window_start, window_end,
        refresh=not args.no_refresh,
        create_mode=args.create,
        dry_run_create=not args.no_dry_run,
        requested_outputs=requested_outputs,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

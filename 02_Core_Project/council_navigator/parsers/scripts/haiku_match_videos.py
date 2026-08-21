#!/usr/bin/env python3.11
"""haiku_match_videos — Haiku-based channel-to-video matcher (S-036 V1+).

Replaces the heuristic title-similarity + type-classification logic in
`match_videos.py` with a Haiku 4.5 LLM call that reasons about meeting-
to-video correspondence directly. Per James 2026-06-10: skip the
code-based confidence-matching approach and deploy a Haiku agent for
the match instead. Composes with S-036 (haiku-html-scraper
sibling) and D-099 (Mac-runtime for claude -p calls).

Composes existing infrastructure:
- `youtube_data_api.list_channel_videos` — authoritative video list
  (Google's structured API; D-085 says use the authoritative source)
- `mac_claude_relay_client.invoke_mac_claude` — claude -p on Mac with
  Haiku 4.5 pinned (D-099 Phase 1 model parity)
- `match_videos.apply_match` — DB-write semantics (UPSERT meetings.video_url
  + propagate to work_orders with auto-promote on high-confidence; mirrors
  the deterministic matcher's behavior so downstream pipelines don't care
  which matcher produced the match)

Why Haiku-instead-of-deterministic for matching specifically:
- The YouTube Data API IS the authoritative source for what videos exist
  (D-085 satisfied at the data layer)
- The matching itself is semantic interpretation ("is this meeting the
  same as this video?") — heuristic title scoring + type-bucket classification
  are exactly LLM-shaped tasks
- Self-adapts to new city title conventions without heuristic maintenance
- Cost: ~$0.001-0.005/match via Max-subscription claude -p path (D-078-compliant;
  no separate paid API exposure)

Usage:
    python3.11 haiku_match_videos.py --city Kingman
    python3.11 haiku_match_videos.py --city Kingman --apply
    python3.11 haiku_match_videos.py --city Kingman --apply --min-confidence medium
    python3.11 haiku_match_videos.py --city Kingman --within-days 30
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import List, Optional

# Windows PowerShell default is cp1252; Haiku output can contain Unicode
# (em-dash, minus sign, smart quotes). Force UTF-8 so we don't crash on print.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

_HERE = Path(__file__).resolve().parent
_PARSERS_DIR = _HERE.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import get_city_youtube_channel  # noqa: E402
from env_config import get_youtube_data_api_key  # noqa: E402
from video_match_helpers import (  # noqa: E402
    CONFIDENCE_RANK as _CONFIDENCE_RANK_SHARED,
    apply_match,
    meetings_for_city,
)
from mac_claude_relay_client import invoke_mac_claude  # noqa: E402
from youtube_data_api import Video, list_channel_videos  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"

# Re-export from video_match_helpers so the CLI gating below reads naturally.
_CONFIDENCE_RANK = _CONFIDENCE_RANK_SHARED

PROMPT_TEMPLATE = """You are matching a council meeting to a YouTube video on the city's official channel. Return ONLY a JSON object — no markdown fence, no preamble, no commentary.

CITY: {city}
MEETING:
  title: {title}
  date: {meeting_date} (ISO YYYY-MM-DD)

CANDIDATE VIDEOS (newest first, up to {n_candidates} shown):
{candidates}

Find the video that corresponds to the meeting. Typical patterns:
- Video title includes the body name + the meeting date (e.g., "City Council Meeting - 05/19/2026")
- Video upload date is 0-3 days AFTER the meeting date (recording posted next day or two)
- Body type must match: a "City Council" video does NOT match a "Planning & Zoning Commission" meeting even if dates align
- Some cities use abbreviations or omit "Meeting" — "P&Z 5/19" can match "Planning & Zoning Commission - May 19, 2026"

Return this exact JSON shape (no extra fields, no markdown):
{{
  "best_match_video_id": "<youtube video_id>" or null,
  "confidence": "high" | "medium" | "needs_review" | "none",
  "reasoning": "<one short sentence explaining the match or why none works>"
}}

Confidence rubric:
- high: same body + meeting_date matches a video uploaded 0-2 days after + clear title correspondence
- medium: probable match with one ambiguity (e.g., body abbreviation or upload 3-5 days later)
- needs_review: best of poor options; operator should confirm before processing
- none: no candidate video plausibly corresponds to this meeting

Be conservative — false-positive matches feed bad URLs into the broadcast pipeline. When in doubt, prefer "needs_review" over "high".
"""


def format_candidates(videos: List[Video], max_n: int = 40) -> str:
    """Render the candidate video list as a numbered prompt-friendly block."""
    lines = []
    for i, v in enumerate(videos[:max_n], start=1):
        title = (v.title or "").strip()
        if len(title) > 140:
            title = title[:137] + "..."
        lines.append(
            f"{i}. id={v.video_id} | uploaded={v.upload_date.isoformat()} | title={title}"
        )
    return "\n".join(lines)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def parse_haiku_verdict(text: str) -> dict:
    """Parse Haiku's response into a verdict dict. Strips markdown fences if
    present. Returns a none-verdict on parse failure rather than raising."""
    if not text:
        return {
            "best_match_video_id": None,
            "confidence": "none",
            "reasoning": "haiku returned no text",
        }
    cleaned = text.strip()
    m = _JSON_FENCE_RE.search(cleaned)
    if m:
        cleaned = m.group(1).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return {
            "best_match_video_id": None,
            "confidence": "none",
            "reasoning": f"haiku response not valid JSON ({e}); raw: {cleaned[:200]}",
        }
    if not isinstance(obj, dict):
        return {
            "best_match_video_id": None,
            "confidence": "none",
            "reasoning": f"haiku response not a JSON object; got {type(obj).__name__}",
        }
    # Normalize fields with safe defaults
    return {
        "best_match_video_id": obj.get("best_match_video_id"),
        "confidence": obj.get("confidence", "none"),
        "reasoning": obj.get("reasoning", ""),
    }


def match_one_meeting(
    city: str, meeting: dict, candidates: List[Video], timeout_sec: int = 120,
) -> dict:
    """Dispatch a Haiku call to match ONE meeting against the candidate list."""
    prompt = PROMPT_TEMPLATE.format(
        city=city,
        title=meeting["title"],
        meeting_date=meeting["date"].isoformat(),
        n_candidates=min(len(candidates), 40),
        candidates=format_candidates(candidates),
    )
    try:
        result = invoke_mac_claude(
            prompt,
            allowed_tools=[],  # no tool use needed — pure classification
            model=HAIKU_MODEL_ID,
            timeout_seconds=timeout_sec,
        )
    except Exception as e:
        return {
            "best_match_video_id": None,
            "confidence": "none",
            "reasoning": f"mac relay error: {type(e).__name__}: {e}",
        }
    return parse_haiku_verdict(result.get("text", ""))


def render_match_line(meeting: dict, verdict: dict, url: Optional[str]) -> List[str]:
    """Render one meeting + its Haiku verdict for the text output."""
    conf = verdict.get("confidence", "none")
    title = (meeting["title"] or "")[:70]
    lines = [
        f"meeting #{meeting['id']}  {meeting['date']}  {title}",
        f"  [{conf:<13}] {url or '(no video)'}",
        f"  reasoning: {verdict.get('reasoning', '')}",
    ]
    return lines


def run_match_haiku(
    city: str,
    *,
    apply: bool = False,
    min_confidence: str = "high",
    max_videos: int = 50,
    within_days: int = 21,
    state: Optional[str] = None,
) -> dict:
    """Programmatic entry point (mirror of the retired match_videos.run_match
    contract) so api_server.py can swap callers without other changes. The CLI
    main() also calls this.

    Returns dict shape:
        success: bool
        error: str | None
        city, channel_url, videos_listed, meetings_inspected, matches_applied
        results: [
          {
            meeting_id, meeting_date, meeting_title,
            had_existing_url, existing_confidence,
            top_candidates: [<at most one>: {confidence, video_title,
                            video_url, video_upload_date, method, reasoning}],
            applied: bool,
          }, ...
        ]
    """
    out: dict = {
        "success": False,
        "error": None,
        "city": city,
        "channel_url": None,
        "videos_listed": 0,
        "meetings_inspected": 0,
        "matches_applied": 0,
        "results": [],
    }

    try:
        chan = get_city_youtube_channel(city, state=state)
    except ValueError as e:
        out["error"] = str(e)
        return out
    if not chan or not chan.get("channel_url"):
        out["error"] = f"No YouTube channel registered for {city!r}."
        return out
    channel_url = chan["channel_url"]
    out["channel_url"] = channel_url

    try:
        api_key = get_youtube_data_api_key()
    except Exception as e:
        out["error"] = f"YOUTUBE_DATA_API_KEY unavailable: {e}"
        return out

    try:
        videos = list_channel_videos(
            api_key=api_key, channel_url=channel_url, max_videos=max_videos,
        )
    except Exception as e:
        out["error"] = f"YouTube Data API error: {type(e).__name__}: {e}"
        return out
    out["videos_listed"] = len(videos)

    meetings = meetings_for_city(city, within_days)
    out["meetings_inspected"] = len(meetings)
    threshold = _CONFIDENCE_RANK[min_confidence]

    method_label = f"haiku-{HAIKU_MODEL_ID}"
    for meeting in meetings:
        row: dict = {
            "meeting_id": meeting["id"],
            "meeting_date": meeting["date"].isoformat() if meeting.get("date") else None,
            "meeting_title": meeting["title"],
            "had_existing_url": bool(meeting.get("video_url")),
            "existing_confidence": meeting.get("match_confidence"),
            "top_candidates": [],
            "applied": False,
        }
        # Don't waste a Haiku call on already-matched-high meetings
        if meeting.get("video_url") and meeting.get("match_confidence") == "high":
            out["results"].append(row)
            continue

        verdict = match_one_meeting(city, meeting, videos)
        conf = verdict.get("confidence", "none")
        vid_id = verdict.get("best_match_video_id")
        url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None
        if url and conf in {"high", "medium", "needs_review"}:
            # Look up matching video to surface title + upload_date for the UI
            video = next((v for v in videos if v.video_id == vid_id), None)
            row["top_candidates"].append({
                "confidence": conf,
                "video_id": vid_id,
                "video_url": url,
                "video_title": video.title if video else "",
                "video_upload_date": video.upload_date.isoformat() if video else None,
                "method": method_label,
                "reasoning": verdict.get("reasoning", ""),
            })
        if (
            apply
            and url
            and conf in {"high", "medium", "needs_review"}
            and _CONFIDENCE_RANK.get(conf, 0) >= threshold
        ):
            apply_match(
                meeting_id=meeting["id"], video_url=url,
                confidence=conf, method=method_label,
            )
            row["applied"] = True
            out["matches_applied"] += 1
        out["results"].append(row)

    out["success"] = True
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--city", required=True, help="City name (must match cities.name)")
    parser.add_argument(
        "--within-days", type=int, default=21,
        help="Only match meetings within the past N days (default 21).",
    )
    parser.add_argument(
        "--max-videos", type=int, default=50,
        help="Max YouTube videos to consider as candidates (default 50).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write matches to DB (default: dry-run; print verdicts only).",
    )
    parser.add_argument(
        "--min-confidence", default="high",
        choices=["high", "medium", "needs_review"],
        help="When --apply, only persist matches at or above this confidence.",
    )
    parser.add_argument(
        "--state", default=None,
        help="Disambiguate city name across states (e.g., 'Arizona').",
    )
    args = parser.parse_args(argv)

    # Pre-flight checks
    try:
        chan = get_city_youtube_channel(args.city, state=args.state)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not chan or not chan.get("channel_url"):
        print(
            f"ERROR: No YouTube channel registered for {args.city!r}. "
            f"Register via: POST /api/cities/<city>/youtube-channel "
            f"OR set_city_channel.py",
            file=sys.stderr,
        )
        return 2
    channel_url = chan["channel_url"]

    try:
        api_key = get_youtube_data_api_key()
    except Exception as e:
        print(f"ERROR: YOUTUBE_DATA_API_KEY unavailable: {e}", file=sys.stderr)
        return 2

    # Fetch candidate videos from the YouTube Data API
    try:
        videos = list_channel_videos(
            api_key=api_key,
            channel_url=channel_url,
            max_videos=args.max_videos,
        )
    except Exception as e:
        print(f"ERROR fetching YouTube videos: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    meetings = meetings_for_city(args.city, args.within_days)
    if not meetings:
        print(f"# {args.city}: no meetings in last {args.within_days} days. Nothing to match.")
        return 0
    print(
        f"# {args.city}: {len(meetings)} meeting(s) in window, "
        f"{len(videos)} video(s) on channel. Matcher: haiku-{HAIKU_MODEL_ID}. "
        f"Mode: {'APPLY' if args.apply else 'dry-run'}."
    )

    applied = 0
    skipped_already_matched = 0
    for meeting in meetings:
        # Skip if already has a confident match
        if meeting.get("video_url") and meeting.get("match_confidence") == "high":
            skipped_already_matched += 1
            print()
            print(
                f"meeting #{meeting['id']}  {meeting['date']}  "
                f"{(meeting['title'] or '')[:70]}"
            )
            print(f"  [already-matched ] {meeting['video_url']}")
            continue

        verdict = match_one_meeting(args.city, meeting, videos)
        vid_id = verdict.get("best_match_video_id")
        url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None
        conf = verdict.get("confidence", "none")
        print()
        for line in render_match_line(meeting, verdict, url):
            print(line)

        if (
            args.apply
            and url
            and conf in {"high", "medium", "needs_review"}
            and _CONFIDENCE_RANK.get(conf, 0) >= _CONFIDENCE_RANK[args.min_confidence]
        ):
            apply_match(
                meeting_id=meeting["id"],
                video_url=url,
                confidence=conf,
                method=f"haiku-{HAIKU_MODEL_ID}",
            )
            applied += 1
            print("  -> APPLIED")

    print()
    print(
        f"# Done. {len(meetings)} meeting(s) inspected. "
        f"{applied} applied, {skipped_already_matched} skipped (already matched)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

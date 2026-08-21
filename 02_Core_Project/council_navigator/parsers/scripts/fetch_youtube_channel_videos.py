#!/usr/bin/env python3
"""fetch_youtube_channel_videos.py — Recon-3: deterministic channel-video fetcher.

Pulls the recent N videos (title + description + upload date) from a candidate
YouTube channel and emits JSON suitable for an LLM verification prompt. The
companion to the "adversarial video-channel verification pass" the per-state
recon Sonnet runs against the discovery pass's candidate channel — Action 3
of the 2026-06-14 RECON_SWARM_AUDIT.

The audit's framing: the dangerous edge case is a city's tourism / comms /
single-purpose YouTube channel masquerading as the government channel. AZ
findings log shows real instances (e.g. @cityofbensonaz8592 registered then
cleared; @CityofSedonaAZ was tourism with council on Swagit; Cottonwood
was airport-commission-only). A second-pass classification over the actual
recent video titles + descriptions catches these structurally.

This module is the DETERMINISTIC half — it only fetches. The LLM
verification half is documented as a prompt template in
[CHANNEL_DISCOVERY_PLAYBOOK.md § Verification pass](../../01_Project_Overview/CHANNEL_DISCOVERY_PLAYBOOK.md).
The recon Sonnet pipes this script's output into the prompt and reads
the verdict back.

How it works:
  1. Resolves a YouTube channel URL (handle / channel-id / custom URL)
     via youtube_data_api.resolve_channel.
  2. Pulls recent uploads via youtube_data_api.list_channel_videos.
  3. Truncates descriptions to a sane length so the prompt stays under
     the Sonnet context budget.
  4. Emits a JSON object the verification prompt expects.

Usage:
    python3.11 fetch_youtube_channel_videos.py --channel-url https://www.youtube.com/@CityofKingman
    python3.11 fetch_youtube_channel_videos.py --channel-url ... --max-videos 20 --json
    python3.11 fetch_youtube_channel_videos.py --channel-url ... --max-description-chars 500

References:
    01_Project_Overview/RECON_SWARM_AUDIT_2026-06-14.md (Action 3 / § 4.1)
    01_Project_Overview/CHANNEL_DISCOVERY_PLAYBOOK.md (prompt template lives here)
    01_Project_Overview/DECISIONS.md#d-085 (the deterministic-first discipline)
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_PARSERS = _SCRIPT_DIR.parent
if str(_PARSERS) not in sys.path:
    sys.path.insert(0, str(_PARSERS))

# Imports from parsers/ — both via launchd-style absolute paths and
# directly when run as a script from inside scripts/.
from env_config import get_youtube_data_api_key  # type: ignore  # noqa: E402
from youtube_data_api import (  # type: ignore  # noqa: E402
    YouTubeDataApiError,
    list_channel_videos,
    resolve_channel,
)

DEFAULT_MAX_VIDEOS = 25  # audit: "most recent 10-25 video titles + descriptions"
DEFAULT_MAX_DESC_CHARS = 280  # one tweet's worth; ~70 tokens. Keeps prompt small.

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_for_verification(
    channel_url: str,
    *,
    api_key: Optional[str] = None,
    max_videos: int = DEFAULT_MAX_VIDEOS,
    max_description_chars: int = DEFAULT_MAX_DESC_CHARS,
) -> dict:
    """Fetch the recent videos for a channel + return the JSON the LLM prompt expects.

    Args:
        channel_url: Any YouTube channel URL form
            (https://www.youtube.com/@handle | /channel/UC... | /c/customslug).
        api_key: YouTube Data API key. Defaults to user_settings.json's
            `youtube_data_api_key`.
        max_videos: How many recent videos to include (audit says 10-25).
        max_description_chars: Truncate each description to this many chars
            (the verifier doesn't need full description bodies — keeps the
            prompt under context budget).

    Returns:
        {
          "channel": {"url", "channel_id", "title", "handle"},
          "videos": [
              {"title", "description", "upload_date", "url"},
              ...
          ],
          "count": <int>,
          "channel_url_input": <str>,
        }

    Raises:
        YouTubeDataApiError: channel can't be resolved (deleted, private,
            URL malformed). Caller should treat as needs-human-review.
    """
    key = api_key or get_youtube_data_api_key()
    if not key:
        raise YouTubeDataApiError(
            "youtube_data_api_key not set in user_settings.json; "
            "can't run Recon-3 verification fetch"
        )

    chan = resolve_channel(key, channel_url)
    chan_id = chan.get("id") or ""
    snip = chan.get("snippet") or {}
    chan_title = snip.get("title") or ""
    chan_handle = snip.get("customUrl") or ""  # YouTube returns "@handle" here

    videos = list_channel_videos(key, channel_url, max_videos=max_videos)

    return {
        "channel": {
            "url": channel_url,
            "channel_id": chan_id,
            "title": chan_title,
            "handle": chan_handle,
        },
        "videos": [
            {
                "title": v.title,
                "description": _truncate(v.description, max_description_chars),
                "upload_date": v.upload_date.isoformat(),
                "url": v.url,
            }
            for v in videos
        ],
        "count": len(videos),
        "channel_url_input": channel_url,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(s: str, n: int) -> str:
    """Truncate `s` to `n` chars without breaking mid-word when possible."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= n:
        return s
    # Try to cut at a word boundary near the limit.
    cut = s.rfind(" ", 0, n - 1)
    if cut < n // 2:
        cut = n - 1
    return s[:cut].rstrip() + "…"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Fetch a YouTube channel's recent videos as JSON for "
            "Recon-3 adversarial verification."
        ),
    )
    p.add_argument(
        "--channel-url",
        required=True,
        help="Channel URL (any form: @handle / /channel/UC... / /c/slug).",
    )
    p.add_argument(
        "--max-videos",
        type=int,
        default=DEFAULT_MAX_VIDEOS,
        help=f"How many recent videos to fetch (default: {DEFAULT_MAX_VIDEOS}).",
    )
    p.add_argument(
        "--max-description-chars",
        type=int,
        default=DEFAULT_MAX_DESC_CHARS,
        help=(
            "Truncate each video description to this many chars "
            f"(default: {DEFAULT_MAX_DESC_CHARS}). The verifier doesn't need "
            "full bodies; smaller keeps the prompt under context budget."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit ONLY the JSON object (suitable for piping to a Sonnet prompt).",
    )
    args = p.parse_args(argv)

    try:
        result = fetch_for_verification(
            args.channel_url,
            max_videos=args.max_videos,
            max_description_chars=args.max_description_chars,
        )
    except YouTubeDataApiError as e:
        if args.json:
            print(
                json.dumps(
                    {
                        "channel_url_input": args.channel_url,
                        "fetch_error": str(e),
                        "count": 0,
                        "videos": [],
                    },
                    indent=2,
                )
            )
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        ch = result["channel"]
        print(f"Channel: {ch['title']!r} ({ch['handle'] or ch['channel_id']})")
        print(f"URL: {ch['url']}")
        print(f"Videos fetched: {result['count']}")
        print()
        for i, v in enumerate(result["videos"], 1):
            print(f"  {i:2d}. [{v['upload_date']}] {v['title']}")
            if v["description"]:
                desc1 = v["description"].split("\n")[0]
                print(f"       {desc1[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

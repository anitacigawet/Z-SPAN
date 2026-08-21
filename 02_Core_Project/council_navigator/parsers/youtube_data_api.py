"""
YouTube Data API v3 client — channel video enumeration for T-004.

Used by `video_matcher.py` to list videos on a city's YouTube channel so
the matcher can find the one corresponding to a given meeting. Sanctioned
Google Cloud API; no unofficial scrapers (per `DECISIONS.md § D-029` /
`FUTURE_THOUGHTS.md § T-004` wrapper-safety logic).

Uses plain HTTPS via `requests`. Doesn't pull in google-api-python-client;
the API surface we need is small and the official client adds a lot of
auth machinery we don't need for an unauthenticated API-key call.

Quota cost (rough, per call):
  - channels.list: 1 unit
  - playlistItems.list: 1 unit (≤50 items per call)

For a channel with 200 videos, one full enumeration ≈ 1 + 4 = 5 units.
46 channels × weekly refresh ≈ 230 units / week. Free tier is 10K/day.
Comfortable headroom.

Reference:
  https://developers.google.com/youtube/v3/docs/channels/list
  https://developers.google.com/youtube/v3/docs/playlistItems/list
"""
from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/youtube/v3"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_PAGES_PER_LIST = 20  # safety: cap at 20 pages × 50 items = 1000 videos


# ─────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────


@dataclass
class Video:
    """A single YouTube video, the subset we care about for matching."""
    video_id: str
    url: str
    title: str
    upload_date: date
    description: str = ""
    raw: dict = field(default_factory=dict, repr=False)  # full API response, for debugging


# ─────────────────────────────────────────────────────────────────
# URL parsing — extract handle (@xxx) or channel id (UCxxx) from a
# channel URL the way the cities.youtube_channel_url field stores it.
# ─────────────────────────────────────────────────────────────────


_HANDLE_RE = re.compile(r"@([A-Za-z0-9_.\-]+)")
_CHANNEL_ID_RE = re.compile(r"/channel/(UC[A-Za-z0-9_\-]{20,})")
# Also catch /c/ legacy custom URLs (e.g., youtube.com/c/CityOfKingman) — these
# need a search-by-name resolution which is more expensive (search.list = 100
# units). For now we just pass the slug along and the resolver tries to handle it.
_CUSTOM_URL_RE = re.compile(r"/c/([A-Za-z0-9_.\-]+)")


def parse_channel_url(channel_url: str) -> tuple[str, str]:
    """Parse a YouTube channel URL into (kind, value).

    kind ∈ {"handle", "channel_id", "custom", "unknown"}
    value is the handle (without @), the UC id, or the custom slug.

    Examples:
      https://www.youtube.com/@CityofKingman/videos -> ("handle", "CityofKingman")
      https://www.youtube.com/channel/UCabc123...     -> ("channel_id", "UCabc123...")
      https://www.youtube.com/c/CityOfKingman          -> ("custom", "CityOfKingman")
    """
    if not channel_url:
        return ("unknown", "")
    m = _HANDLE_RE.search(channel_url)
    if m:
        return ("handle", m.group(1))
    m = _CHANNEL_ID_RE.search(channel_url)
    if m:
        return ("channel_id", m.group(1))
    m = _CUSTOM_URL_RE.search(channel_url)
    if m:
        return ("custom", m.group(1))
    return ("unknown", channel_url)


# ─────────────────────────────────────────────────────────────────
# API calls
# ─────────────────────────────────────────────────────────────────


class YouTubeDataApiError(RuntimeError):
    """Wraps any failure from the YouTube Data API."""


def _request(api_key: str, endpoint: str, params: dict) -> dict:
    """Single HTTPS GET against the API. Raises YouTubeDataApiError on failure."""
    if not api_key:
        raise YouTubeDataApiError(
            "YOUTUBE_DATA_API_KEY is not set. Place it in env or in "
            "parsers/user_settings.json under 'youtube_data_api_key'."
        )
    full_params = {**params, "key": api_key}
    url = f"{API_ROOT}/{endpoint}"
    try:
        resp = requests.get(url, params=full_params, timeout=DEFAULT_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise YouTubeDataApiError(f"network error calling {endpoint}: {e}") from e
    if resp.status_code == 403:
        # Quota / key issues land here
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:200]}
        raise YouTubeDataApiError(
            f"403 from {endpoint}: {body.get('error', body)}. "
            f"Check the key is valid and YouTube Data API v3 is enabled in the project."
        )
    if resp.status_code != 200:
        raise YouTubeDataApiError(f"{resp.status_code} from {endpoint}: {resp.text[:200]}")
    try:
        return resp.json()
    except Exception as e:
        raise YouTubeDataApiError(f"non-JSON response from {endpoint}: {e}") from e


def resolve_channel(api_key: str, channel_url: str) -> dict:
    """Resolve a channel URL to its API channel resource.

    Returns the full `channels.list` item dict (includes id, contentDetails,
    snippet). Raises if resolution fails.

    Quota cost: 1 unit per call.
    """
    kind, value = parse_channel_url(channel_url)
    if kind == "channel_id":
        params = {"part": "id,snippet,contentDetails", "id": value}
    elif kind == "handle":
        # forHandle was added in 2023 and accepts the handle without @.
        params = {"part": "id,snippet,contentDetails", "forHandle": value}
    elif kind == "custom":
        # /c/ URLs don't have a direct lookup; forUsername is for the older
        # legacy username scheme. Most c/ URLs map to handles now. Try
        # forHandle as a best-effort.
        params = {"part": "id,snippet,contentDetails", "forHandle": value}
    else:
        raise YouTubeDataApiError(
            f"could not parse channel URL: {channel_url!r}. "
            f"Expected forms: /@handle/, /channel/UC..., or /c/customslug/."
        )
    body = _request(api_key, "channels", params)
    items = body.get("items") or []
    if not items:
        raise YouTubeDataApiError(
            f"no channel found for {kind}={value!r} (URL: {channel_url}). "
            f"Check the channel URL in cities.youtube_channel_url."
        )
    return items[0]


def list_channel_videos(
    api_key: str,
    channel_url: str,
    max_videos: int = 200,
) -> List[Video]:
    """Enumerate videos on a YouTube channel, newest first.

    Resolves the channel → its uploads playlist → paginates playlistItems.
    Returns up to `max_videos`. Default 200 is enough for ~4 years of
    weekly council meetings, more than the 30-day freshness window the
    bridge cares about.

    Quota cost: 1 (channels.list) + ceil(max_videos/50) (playlistItems.list).
    """
    chan = resolve_channel(api_key, channel_url)
    uploads_playlist_id = (
        chan.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    )
    if not uploads_playlist_id:
        raise YouTubeDataApiError(
            f"channel {chan.get('id')} has no uploads playlist (private channel?)"
        )

    videos: List[Video] = []
    page_token: Optional[str] = None
    pages_seen = 0

    while len(videos) < max_videos and pages_seen < MAX_PAGES_PER_LIST:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": min(50, max_videos - len(videos)),
        }
        if page_token:
            params["pageToken"] = page_token
        body = _request(api_key, "playlistItems", params)
        for item in body.get("items", []):
            snip = item.get("snippet") or {}
            cd = item.get("contentDetails") or {}
            video_id = cd.get("videoId") or snip.get("resourceId", {}).get("videoId")
            if not video_id:
                continue
            title = snip.get("title") or ""
            description = snip.get("description") or ""
            published_at = snip.get("publishedAt") or cd.get("videoPublishedAt") or ""
            try:
                upload_date = datetime.fromisoformat(
                    published_at.replace("Z", "+00:00")
                ).date()
            except (ValueError, AttributeError):
                upload_date = date.today()  # last-ditch fallback; matcher will likely down-rank
            videos.append(
                Video(
                    video_id=video_id,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    title=title,
                    upload_date=upload_date,
                    description=description,
                    raw=item,
                )
            )
            if len(videos) >= max_videos:
                break
        page_token = body.get("nextPageToken")
        pages_seen += 1
        if not page_token:
            break
        # Light pacing — YouTube's 10K/day quota is generous but no sense
        # hammering it. 50ms between pages is plenty.
        time.sleep(0.05)

    return videos


# ─────────────────────────────────────────────────────────────────
# Live broadcast detection (S-015 — the Guide)
# ─────────────────────────────────────────────────────────────────


@dataclass
class LiveBroadcast:
    """A currently-live broadcast on a channel (the subset the Guide needs)."""
    video_id: str
    url: str
    title: str
    channel_id: str
    started_at: Optional[str] = None  # ISO 8601 (snippet.publishedAt); best-effort
    raw: dict = field(default_factory=dict, repr=False)


def find_live_broadcast(
    api_key: str,
    channel_url: str = "",
    channel_id: str = "",
) -> Optional[LiveBroadcast]:
    """Return the channel's currently-live broadcast, or None if it isn't live.

    Pass `channel_id` directly when known (cached in cities.youtube_channel_id)
    to skip the channel-resolution call; otherwise `channel_url` is resolved
    first.

    Quota cost: search.list = **100 units** (+ 1 unit for channel resolution if
    channel_id wasn't supplied). This is the expensive call — gate it by the
    calendar (only poll channels with a meeting scheduled in the buffer window),
    per FUTURE_THOUGHTS.md § S-015.
    """
    cid = channel_id or ""
    if not cid:
        if not channel_url:
            raise YouTubeDataApiError(
                "find_live_broadcast needs a channel_id or a channel_url"
            )
        chan = resolve_channel(api_key, channel_url)  # 1 unit
        cid = chan.get("id") or ""
        if not cid:
            raise YouTubeDataApiError(
                f"could not resolve a channel id for {channel_url!r}"
            )
    body = _request(
        api_key,
        "search",
        {
            "part": "snippet",
            "channelId": cid,
            "eventType": "live",
            "type": "video",
            "maxResults": 1,
            "order": "date",
        },
    )  # 100 units
    items = body.get("items") or []
    if not items:
        return None
    item = items[0]
    snip = item.get("snippet") or {}
    video_id = (item.get("id") or {}).get("videoId") or ""
    if not video_id:
        return None
    return LiveBroadcast(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=snip.get("title") or "",
        channel_id=cid,
        started_at=snip.get("publishedAt"),
        raw=item,
    )


# ─────────────────────────────────────────────────────────────────
# Smoke test entry point
# ─────────────────────────────────────────────────────────────────


def _smoke():
    """Quick check: list the first ~10 videos from Kingman's channel.

    Run as: py -3.12 -m youtube_data_api
    Requires YOUTUBE_DATA_API_KEY to be resolvable (env or user_settings.json).
    """
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from env_config import get_youtube_data_api_key  # noqa: E402, PLC0415

    key = get_youtube_data_api_key()
    if not key:
        print("YOUTUBE_DATA_API_KEY not set. See FUTURE_THOUGHTS.md § T-004.")
        return 1

    test_url = "https://www.youtube.com/@CityofKingman/videos"
    print(f"Listing videos from {test_url} (first 10)...")
    try:
        videos = list_channel_videos(key, test_url, max_videos=10)
    except YouTubeDataApiError as e:
        print(f"FAILED: {e}")
        return 2

    print(f"OK — {len(videos)} videos:")
    for v in videos:
        print(f"  {v.upload_date}  {v.title[:80]}")
        print(f"             {v.url}")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke())

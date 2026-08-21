#!/usr/bin/env python3.11
"""transcribe_non_youtube — S-037 V0 non-YouTube video-source resolver.

For a meeting whose video lives on a non-YouTube platform (Granicus,
Legistar, TV4, etc.), this module exposes the resolver primitives the
worker uses to find a playable / transcribable URL from the meeting
record — preferring meetings.video_url if pre-populated, else
constructing per-city from meeting_id or other available fields.

Post-D-143 (subsystem retirement 2026-07-01), the actual
transcription + downstream synthesis is handled by
`_fetch_transcript_words` in zspan_pipeline/fetcher.py + the
V1-RAG-3 qdrant strategies. The CLI mode here is a resolver report
(dry-run style) — it does NOT itself transcribe or upload.

V0 scope (resolved 2026-06-10):
  - Cities supported: Bullhead City (Granicus direct), Lake Havasu City
    (Granicus ASX). Colorado City has NO video source — exits cleanly
    with a no_video_source marker.
  - Whisper substrate: Mac via S-019 mac_transcriber (local Whisper).
  - Engine: faster-whisper distil-large-v3 INT8.

See `01_Project_Overview/S037_NON_YOUTUBE_VIDEO_SOURCES_SPEC.md` for
the full design + per-city findings.

Usage (resolver report):
  python3.11 transcribe_non_youtube.py --work-order-id 47
  python3.11 transcribe_non_youtube.py --meeting-id 101095
  python3.11 transcribe_non_youtube.py --city "Bullhead City" \\
      --days-back 14  # batch resolver report across recent Bullhead WOs
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

# Windows PowerShell default is cp1252; output may contain Unicode (em-dash,
# smart quotes, Granicus titles). Force UTF-8 so we don't crash on print.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

_HERE = Path(__file__).resolve().parent
_PARSERS_DIR = _HERE.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from polite_http import make_session  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


# ── Per-city URL resolution strategies ────────────────────────────────


@dataclass
class ResolvedSource:
    """Outcome of per-meeting source-URL resolution."""
    meeting_id: int
    city: str
    meeting_date: str
    meeting_title: str
    source_url: Optional[str]
    source_kind: str  # "youtube" | "granicus_mediaplayer" | "granicus_asx" | "preset_video_url" | "no_video_source" | "unsupported_city"
    notes: str


_GRANICUS_CLIP_ID_RE = re.compile(r"clip_id=(\d+)")
_DIRECT_MEDIA_SUFFIXES = (
    ".mp4", ".m4a", ".mp3", ".wav", ".webm", ".mov", ".mkv", ".m3u8",
)


def is_transcription_ready_url(url: Optional[str]) -> bool:
    """Return whether *url* can be handed directly to the transcriber."""
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
    except (AttributeError, ValueError):
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False

    hostname = parsed.hostname.lower()
    if hostname == "youtube.com" or hostname.endswith(".youtube.com"):
        return True
    if hostname == "youtu.be":
        return True
    return parsed.path.lower().endswith(_DIRECT_MEDIA_SUFFIXES)


def _fetch_text_bounded(
    url: str,
    *,
    allowed_host: str,
    timeout: int,
    max_bytes: int,
) -> str:
    """Fetch paced text with a body cap and exact redirect-host validation."""
    with make_session() as session:
        with session.get(
            url,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            final_host = (urlparse(response.url).hostname or "").lower()
            if final_host != allowed_host:
                raise ValueError(
                    f"Redirect to disallowed host: {final_host} (started from {url})"
                )

            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError(
                        f"Response from {url} exceeded {max_bytes} bytes"
                    )
            return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _extract_granicus_clip_id(*candidates: Optional[str]) -> Optional[str]:
    """Return the first clip_id found in any of the candidate strings."""
    for c in candidates:
        if not c:
            continue
        m = _GRANICUS_CLIP_ID_RE.search(c)
        if m:
            return m.group(1)
    return None


# ── Bullhead Granicus archive scraper ─────────────────────────────────


_BH_GRANICUS_ARCHIVE_URL = "https://bullheadcity.granicus.com/ViewPublisher.php?view_id=2"
_BH_ARCHIVE_CACHE: Optional[dict[str, str]] = None  # clip_id → archive-video MP4 URL

_BH_MP4_URL_RE = re.compile(
    r"https?://archive-video\.granicus\.com/bullheadcity/bullheadcity_[0-9a-f-]+\.mp4",
    re.IGNORECASE,
)


def _fetch_bullhead_archive_index() -> dict[str, str]:
    """Scrape bullheadcity.granicus.com/ViewPublisher.php?view_id=2,
    return a {clip_id: archive_video_mp4_url} dict. Cached in-process.

    Per S-037 V0 C2.1 verification 2026-06-10: each archive row contains an
    Agenda link with clip_id=N AND a direct MP4 link to
    archive-video.granicus.com/bullheadcity/bullheadcity_<UUID>.mp4. The
    MediaPlayer.php wrapper that C2 originally constructed is NOT handled
    by yt-dlp's extractors (returns HTTP 500); the archive-video URL is a
    plain HTTPS MP4 yt-dlp downloads via its generic extractor.
    """
    global _BH_ARCHIVE_CACHE
    if _BH_ARCHIVE_CACHE is not None:
        return _BH_ARCHIVE_CACHE

    from bs4 import BeautifulSoup

    logger.info("fetching Bullhead Granicus archive index: %s", _BH_GRANICUS_ARCHIVE_URL)
    text = _fetch_text_bounded(
        _BH_GRANICUS_ARCHIVE_URL,
        allowed_host="bullheadcity.granicus.com",
        timeout=30,
        max_bytes=5_000_000,
    )
    soup = BeautifulSoup(text, "html.parser")

    # Bullhead's archive uses the same Granicus <table id="archive"> shape as
    # LH. Fall back to searching the entire document if the id is missing in
    # a future Granicus layout change.
    archive_table = soup.find("table", id="archive") or soup

    index: dict[str, str] = {}
    for row in archive_table.find_all("tr"):
        clip_id: Optional[str] = None
        mp4_url: Optional[str] = None
        for link in row.find_all("a"):
            href = link.get("href", "") or ""
            onclick = link.get("onclick", "") or ""
            if not clip_id:
                cid = _extract_granicus_clip_id(href, onclick)
                if cid:
                    clip_id = cid
            if not mp4_url:
                m = _BH_MP4_URL_RE.search(href) or _BH_MP4_URL_RE.search(onclick)
                if m:
                    mp4_url = m.group(0)
        if clip_id and mp4_url:
            index[clip_id] = mp4_url

    logger.info("Bullhead Granicus archive: %d clips indexed", len(index))
    _BH_ARCHIVE_CACHE = index
    return index


def resolve_bullhead_city(meeting_row: dict) -> ResolvedSource:
    """Bullhead City: Granicus archive lookup → direct MP4 URL.

    Per S-037 V0 C2.1 fix (2026-06-10): yt-dlp does NOT handle Bullhead's
    MediaPlayer.php wrapper (returns HTTP 500 — surfaced during the C4 firing
    attempt). The downloadable MP4 lives at
    archive-video.granicus.com/bullheadcity/bullheadcity_<UUID>.mp4 and is
    exposed directly in the archive page row's MP4/Download cell.

    Resolution order:
      1. A transcription-ready meetings.video_url
      2. clip_id parsed from a meetings.video_url wrapper
      3. An all-digit external meetings.meeting_id
      4. clip_id parsed from meetings.agenda_url
      5. Archive-index lookup → archive-video MP4 URL
    """
    video_url = (meeting_row.get("video_url") or "").strip()
    if is_transcription_ready_url(video_url):
        return ResolvedSource(
            meeting_id=meeting_row["id"],
            city=meeting_row["city_name"],
            meeting_date=meeting_row.get("meeting_date") or "",
            meeting_title=meeting_row.get("meeting_title") or "",
            source_url=video_url,
            source_kind="preset_video_url",
            notes="meetings.video_url is transcription-ready; using as-is",
        )
    if video_url:
        logger.warning(
            "Bullhead meetings.video_url is a wrapper/non-direct source; "
            "continuing resolution: %s",
            video_url,
        )

    clip_id = _extract_granicus_clip_id(video_url)
    external_meeting_id = str(meeting_row.get("meeting_id") or "").strip()
    if not clip_id and external_meeting_id.isdigit():
        clip_id = external_meeting_id
    if not clip_id:
        clip_id = _extract_granicus_clip_id(meeting_row.get("agenda_url"))
    if not clip_id:
        return ResolvedSource(
            meeting_id=meeting_row["id"],
            city=meeting_row["city_name"],
            meeting_date=meeting_row.get("meeting_date") or "",
            meeting_title=meeting_row.get("meeting_title") or "",
            source_url=None,
            source_kind="no_video_source",
            notes=(
                "Bullhead meeting has no clip_id in video_url, numeric external "
                "meeting_id, or agenda_url"
            ),
        )

    try:
        index = _fetch_bullhead_archive_index()
    except Exception as e:
        logger.warning("Bullhead Granicus archive fetch failed: %s", e)
        return ResolvedSource(
            meeting_id=meeting_row["id"],
            city=meeting_row["city_name"],
            meeting_date=meeting_row.get("meeting_date") or "",
            meeting_title=meeting_row.get("meeting_title") or "",
            source_url=None,
            source_kind="no_video_source",
            notes=f"Bullhead Granicus archive unreachable: {e}",
        )

    mp4_url = index.get(clip_id)
    if not mp4_url:
        return ResolvedSource(
            meeting_id=meeting_row["id"],
            city=meeting_row["city_name"],
            meeting_date=meeting_row.get("meeting_date") or "",
            meeting_title=meeting_row.get("meeting_title") or "",
            source_url=None,
            source_kind="no_video_source",
            notes=f"clip_id={clip_id} not in Bullhead Granicus archive (pre-cutoff or not yet posted)",
        )

    return ResolvedSource(
        meeting_id=meeting_row["id"],
        city=meeting_row["city_name"],
        meeting_date=meeting_row.get("meeting_date") or "",
        meeting_title=meeting_row.get("meeting_title") or "",
        source_url=mp4_url,
        source_kind="granicus_direct_mp4",
        notes=f"clip_id={clip_id} resolved to direct MP4 via Bullhead archive scrape",
    )


# ── Lake Havasu Granicus archive scraper ──────────────────────────────


_LH_GRANICUS_HOST = "lakehavasucity.granicus.com"
_LH_GRANICUS_ARCHIVE_URL_TMPL = (
    "https://lakehavasucity.granicus.com/ViewPublisher.php?view_id={view_id}"
)
_LH_ARCHIVE_CACHE: dict[str, list[dict]] = {}


def _extract_lh_view_id(video_url: str) -> Optional[str]:
    """Extract a publisher view only from Lake Havasu's exact wrapper URL."""
    if not video_url:
        return None
    try:
        parsed = urlparse(video_url.strip())
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or (parsed.hostname or "").lower() != _LH_GRANICUS_HOST
        or parsed.path != "/ViewPublisher.php"
    ):
        return None
    view_ids = parse_qs(parsed.query).get("view_id", [])
    if len(view_ids) != 1 or not view_ids[0].isdigit():
        return None
    return view_ids[0]


def _normalize_body_name(name: str) -> str:
    """Loose normalization for body-name matching across LH calendar +
    Granicus archive. "Planning and Zoning Commission" ↔ "Planning &
    Zoning Commission" ↔ "P&Z" should all match."""
    n = (name or "").lower()
    n = n.replace("&", "and")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _parse_lh_archive_date(s: str) -> Optional[str]:
    """Granicus archive dates are typically 'Jun 9, 2026' or '6/9/2026'."""
    s = (s or "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# Granicus archive cell[0] format: "<Body> on YYYY-MM-DD H:MM AM/PM - <subtype>"
# e.g.: "City Council on 2026-06-09 5:30 PM - Regular Meeting (Tentative Budget...)"
_LH_ROW_HEADER_RE = re.compile(
    r"^(?P<body>.+?)\s+on\s+(?P<date>\d{4}-\d{2}-\d{2})\s",
)


def _fetch_lh_archive_index(view_id: str) -> list[dict]:
    """Scrape a Lake Havasu publisher view and return its archive entries.

    Entries contain {date_iso, body, clip_id, raw_header, view_id} and are
    cached in-process.

    Layout (verified 2026-06-10):
      <table class="listingTable" id="archive">
        <tr><td class="listItem">City Council on YYYY-MM-DD H:MM PM - Subtype</td>
            <td>UNIX_TIMESTAMP Mon  D, YYYY</td>
            <td>HHh MMm</td>
            <td><a href=".../AgendaViewer.php?...clip_id=NNNN">Agenda</a></td>
            <td></td>
            <td><a onclick="...MediaPlayer.php?...clip_id=NNNN...">Video</a></td>
        </tr>
        ...
      </table>
    """
    if view_id in _LH_ARCHIVE_CACHE:
        return _LH_ARCHIVE_CACHE[view_id]

    from bs4 import BeautifulSoup

    archive_url = _LH_GRANICUS_ARCHIVE_URL_TMPL.format(view_id=view_id)
    logger.info("fetching LH Granicus archive index: %s", archive_url)
    text = _fetch_text_bounded(
        archive_url,
        allowed_host=_LH_GRANICUS_HOST,
        timeout=30,
        max_bytes=5_000_000,
    )
    soup = BeautifulSoup(text, "html.parser")

    archive_table = soup.find("table", id="archive")
    if archive_table is None:
        logger.warning("no <table id='archive'> on LH Granicus page")
        _LH_ARCHIVE_CACHE[view_id] = []
        return _LH_ARCHIVE_CACHE[view_id]

    entries: list[dict] = []
    for row in archive_table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        header_text = cells[0].get_text(" ", strip=True)
        m = _LH_ROW_HEADER_RE.match(header_text)
        if not m:
            continue
        date_iso = m.group("date")
        body = m.group("body").strip()

        # clip_id can appear in href (Agenda link) or onclick (Video link).
        # Either points to the same meeting — pick whichever we find first.
        clip_id: Optional[str] = None
        for link in row.find_all("a"):
            href = link.get("href", "")
            onclick = link.get("onclick", "")
            cid = _extract_granicus_clip_id(href, onclick)
            if cid:
                clip_id = cid
                break
        if not clip_id:
            continue
        entries.append({
            "date_iso": date_iso,
            "body": body,
            "clip_id": clip_id,
            "raw_header": header_text,
            "view_id": view_id,
        })

    logger.info("LH Granicus archive view_id=%s: %d clips indexed", view_id, len(entries))
    _LH_ARCHIVE_CACHE[view_id] = entries
    return entries


def _lookup_lh_clip_id(
    meeting_date: str,
    meeting_title: str,
    view_id: str,
) -> Optional[dict]:
    """Match a LH meeting to a Granicus archive entry by date (exact) +
    body name (loose). Returns the matching entry dict or None."""
    if not meeting_date or not meeting_title:
        return None
    try:
        entries = _fetch_lh_archive_index(view_id)
    except Exception as e:
        logger.warning("LH Granicus archive fetch failed: %s", e)
        return None

    # Normalize the meeting date — DB stores some entries as 'YYYY-MM-DD',
    # some as 'M/D/YYYY' (Legistar parser variability).
    md = meeting_date.strip()
    if "/" in md:
        try:
            md = datetime.strptime(md, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    title_norm = _normalize_body_name(meeting_title)
    for entry in entries:
        if entry["date_iso"] != md:
            continue
        if _normalize_body_name(entry["body"]) == title_norm:
            return entry
    return None


# ── LH ASX → archive-video MP4 resolver ───────────────────────────────


_LH_ASX_URL_TMPL = (
    "https://lakehavasucity.granicus.com/ASX.php?view_id={view_id}&clip_id={clip_id}"
)
_LH_MP4_FROM_ASX_RE = re.compile(
    r"lakehavasucity_[0-9a-f-]+\.mp4",
    re.IGNORECASE,
)
_LH_ASX_CACHE: dict[tuple[str, str], Optional[str]] = {}


def _resolve_lh_mp4_url_from_asx(clip_id: str, view_id: str) -> Optional[str]:
    """Fetch the LH ASX file for a clip_id, parse the RTMP <REF> for the
    lakehavasucity_<UUID>.mp4 filename, return the HTTPS archive-video URL.
    Cached per publisher view_id + clip_id.

    Per S-037 V0 C2.1 verification 2026-06-10: LH's ASX wraps an RTMP REF of
    shape `rtmp://69.5.90.100/OnDemand/mp4:lakehavasucity/lakehavasucity_<UUID>.mp4?wmcache=0`.
    RTMP support in yt-dlp is unreliable (rtmpdump unmaintained since 2014),
    but the underlying file is ALSO HTTPS-reachable at
    archive-video.granicus.com/lakehavasucity/lakehavasucity_<UUID>.mp4 —
    verified via direct HTTPS GET (200 OK, video/mp4, 1.9 GB content-length).
    yt-dlp downloads HTTPS MP4 URLs via the generic extractor without
    Granicus-specific handling.
    """
    cache_key = (view_id, clip_id)
    if cache_key in _LH_ASX_CACHE:
        return _LH_ASX_CACHE[cache_key]

    asx_url = _LH_ASX_URL_TMPL.format(view_id=view_id, clip_id=clip_id)
    try:
        text = _fetch_text_bounded(
            asx_url,
            allowed_host=_LH_GRANICUS_HOST,
            timeout=15,
            max_bytes=1_000_000,
        )
    except Exception as e:
        logger.warning(
            "LH ASX fetch failed for view_id=%s clip_id=%s: %s",
            view_id,
            clip_id,
            e,
        )
        _LH_ASX_CACHE[cache_key] = None
        return None

    m = _LH_MP4_FROM_ASX_RE.search(text)
    if not m:
        logger.warning(
            "LH ASX for view_id=%s clip_id=%s missing "
            "lakehavasucity_<UUID>.mp4 pattern",
            view_id,
            clip_id,
        )
        _LH_ASX_CACHE[cache_key] = None
        return None

    mp4_url = f"https://archive-video.granicus.com/lakehavasucity/{m.group(0)}"
    _LH_ASX_CACHE[cache_key] = mp4_url
    return mp4_url


def resolve_lake_havasu_city(meeting_row: dict) -> ResolvedSource:
    """Lake Havasu City: Granicus archive → ASX → archive-video MP4 URL.

    Per S-037 V0 C2.1 fix (2026-06-10): yt-dlp's ASX handling is unreliable
    (RTMP-only wrapper in LH's case; rtmpdump is unmaintained). Resolve to
    the underlying HTTPS MP4 the same way Bullhead's archive exposes it.

    The Legistar parser stores Legistar event_id in meetings.meeting_id —
    NOT the Granicus clip_id (different systems). Resolution path:

      1. A transcription-ready meetings.video_url
      2. Parse a publisher view from an exact Lake Havasu ViewPublisher wrapper
      3. Cross-walk publisher view then default view 2 (date + body → clip_id)
      4. Fetch ASX for that clip_id through the matched publisher view
         → parse the lakehavasucity_<UUID>.mp4
         from the RTMP REF → construct archive-video.granicus.com HTTPS URL

    If the archive doesn't have the meeting (canceled, not-yet-posted, or
    pre-cutoff) or the ASX parse fails, returns no_video_source with note.
    """
    video_url = (meeting_row.get("video_url") or "").strip()
    if is_transcription_ready_url(video_url):
        return ResolvedSource(
            meeting_id=meeting_row["id"],
            city=meeting_row["city_name"],
            meeting_date=meeting_row.get("meeting_date") or "",
            meeting_title=meeting_row.get("meeting_title") or "",
            source_url=video_url,
            source_kind="preset_video_url",
            notes="meetings.video_url is transcription-ready; using as-is",
        )
    if video_url:
        logger.warning(
            "Lake Havasu meetings.video_url is a wrapper/non-direct source; "
            "continuing resolution: %s",
            video_url,
        )

    meeting_date = meeting_row.get("meeting_date") or ""
    meeting_title = meeting_row.get("meeting_title") or ""
    wrapper_view_id = _extract_lh_view_id(video_url)
    view_ids = list(dict.fromkeys([wrapper_view_id, "2"]))
    archive_match = None
    for view_id in view_ids:
        if view_id is None:
            continue
        archive_match = _lookup_lh_clip_id(meeting_date, meeting_title, view_id)
        if archive_match:
            break
    if not archive_match:
        return ResolvedSource(
            meeting_id=meeting_row["id"],
            city=meeting_row["city_name"],
            meeting_date=meeting_date,
            meeting_title=meeting_title,
            source_url=None,
            source_kind="no_video_source",
            notes=(
                "no Granicus archive match for date+body (may be canceled, "
                "not-yet-posted, or pre-cutoff)"
            ),
        )

    clip_id = archive_match["clip_id"]
    matched_view_id = archive_match["view_id"]
    mp4_url = _resolve_lh_mp4_url_from_asx(clip_id, matched_view_id)
    if not mp4_url:
        return ResolvedSource(
            meeting_id=meeting_row["id"],
            city=meeting_row["city_name"],
            meeting_date=meeting_date,
            meeting_title=meeting_title,
            source_url=None,
            source_kind="no_video_source",
            notes=(
                f"view_id={matched_view_id} clip_id={clip_id} matched in archive "
                f"but ASX→MP4 resolution "
                f"failed (ASX unreachable or missing lakehavasucity_<UUID>.mp4)"
            ),
        )

    return ResolvedSource(
        meeting_id=meeting_row["id"],
        city=meeting_row["city_name"],
        meeting_date=meeting_date,
        meeting_title=meeting_title,
        source_url=mp4_url,
        source_kind="granicus_direct_mp4",
        notes=(
            f"view_id={matched_view_id} clip_id={clip_id} → ASX → "
            "archive-video MP4 URL"
        ),
    )


def resolve_colorado_city(meeting_row: dict) -> ResolvedSource:
    """Colorado City: refined S-037 V0 C1 finding (dry-run 2026-06-10).

    tocc.us/meetings publishes only Google Drive minutes documents — but
    SOME meetings have a YouTube video_url populated in the DB (e.g., the
    JUNE PLANNING COMMISSION MEETING). For meetings WITH a YouTube
    video_url, the existing YouTube path handles them — S-037 V0 is not
    the unblocker. For meetings WITHOUT, there is genuinely no source.
    """
    video_url = (meeting_row.get("video_url") or "").strip()
    if video_url:
        is_youtube = "youtube.com" in video_url or "youtu.be" in video_url
        if is_youtube:
            return ResolvedSource(
                meeting_id=meeting_row["id"],
                city=meeting_row["city_name"],
                meeting_date=meeting_row.get("meeting_date") or "",
                meeting_title=meeting_row.get("meeting_title") or "",
                source_url=video_url,
                source_kind="existing_youtube_in_video_url",
                notes="meetings.video_url already has YouTube URL; use existing YouTube path (S-037 V0 not the unblocker)",
            )
        return ResolvedSource(
            meeting_id=meeting_row["id"],
            city=meeting_row["city_name"],
            meeting_date=meeting_row.get("meeting_date") or "",
            meeting_title=meeting_row.get("meeting_title") or "",
            source_url=video_url,
            source_kind="preset_video_url",
            notes="meetings.video_url pre-populated (non-YouTube); using as-is",
        )

    return ResolvedSource(
        meeting_id=meeting_row["id"],
        city=meeting_row["city_name"],
        meeting_date=meeting_row.get("meeting_date") or "",
        meeting_title=meeting_row.get("meeting_title") or "",
        source_url=None,
        source_kind="no_video_source",
        notes="Colorado City meeting has no video_url; tocc.us publishes only minutes for this meeting.",
    )


CITY_STRATEGIES = {
    "Bullhead City": resolve_bullhead_city,
    "Lake Havasu City": resolve_lake_havasu_city,
    "Colorado City": resolve_colorado_city,
}


def resolve_source(meeting_row: dict) -> ResolvedSource:
    city = (meeting_row.get("city_name") or "").strip()
    strategy = CITY_STRATEGIES.get(city)
    if strategy is None:
        return ResolvedSource(
            meeting_id=meeting_row["id"],
            city=city,
            meeting_date=meeting_row.get("meeting_date") or "",
            meeting_title=meeting_row.get("meeting_title") or "",
            source_url=None,
            source_kind="unsupported_city",
            notes=f"S-037 V0 has no source-resolution strategy for {city!r}",
        )
    return strategy(meeting_row)


# ── Transcript → text-source body ─────────────────────────────────────



# ── Worker integration helpers (used by zspan_pipeline.worker) ─────
# NOTE: `is_youtube_url` was removed 2026-07-01 session-21 chunk-27 after
# orphan-verification found zero external callers. `scanner.py` has its
# own `_is_youtube_url` (underscore-prefixed) for its own local use.


def wo_to_meeting_row(wo: dict) -> dict:
    """Adapt a work_order dict (from database.get_work_order /
    next_pending_work_order — joined with select meetings fields) to the
    meeting_row shape resolve_source() expects.

    The joined SELECT in database.py exposes meeting_title, meeting_date,
    city_name, agenda_url, meeting.meeting_id as meeting_external_id, and
    meeting.video_url as meeting_video_url.
    """
    return {
        "id": wo.get("meeting_id"),
        "city_name": wo.get("city_name") or "",
        "meeting_date": wo.get("meeting_date") or "",
        "meeting_title": wo.get("meeting_title") or "",
        "meeting_id": wo.get("meeting_external_id") or "",
        "video_url": (
            wo.get("youtube_video_url") or wo.get("meeting_video_url") or ""
        ),
        "agenda_url": wo.get("agenda_url") or "",
    }


# The V1-RAG-3 pipeline transcribes non-YouTube sources via
# `_fetch_transcript_words` in zspan_pipeline/fetcher.py.


# ── DB lookups (Flask-style direct SQL; no model layer) ───────────────


def _get_meeting_row(meeting_id: int) -> Optional[dict]:
    from database import get_connection
    conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, city_name, meeting_date, meeting_time, meeting_title,
               meeting_id, video_url, agenda_url, meeting_status
        FROM meetings
        WHERE id = ?
        """,
        (meeting_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _get_meeting_row_for_work_order(work_order_id: int) -> Optional[dict]:
    from database import get_connection
    conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT m.id AS id, m.city_name, m.meeting_date, m.meeting_time,
               m.meeting_title, m.meeting_id, m.video_url, m.agenda_url, m.meeting_status,
               wo.id AS work_order_id, wo.state AS wo_state,
               wo.youtube_video_url AS wo_youtube_url
        FROM work_orders wo
        JOIN meetings m ON m.id = wo.meeting_id
        WHERE wo.id = ?
        """,
        (work_order_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _get_city_meetings_in_window(city: str, days_back: int) -> list[dict]:
    """Used for batch dry-run scanning."""
    from database import get_connection
    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=days_back)
    conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, city_name, meeting_date, meeting_time, meeting_title,
               meeting_id, video_url, agenda_url, meeting_status
        FROM meetings
        WHERE city_name = ?
          AND meeting_date >= ?
          AND meeting_date <= ?
        ORDER BY meeting_date DESC, meeting_time
        """,
        (city, start.isoformat(), end.isoformat()),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ── Pipeline orchestration ────────────────────────────────────────────


async def transcribe_meeting(
    meeting_row: dict,
    *,
    dry_run: bool,
) -> dict:
    """Resolve the video source for a single meeting.

    This script's role is scoped to the S-037 resolver report — it plans which meetings have
    a Whisper-transcribable source and which don't. The V1-RAG-3
    pipeline in zspan_pipeline/worker.py handles the actual
    transcription end-to-end via `_fetch_transcript_words`.
    """
    resolved = resolve_source(meeting_row)
    plan = {
        "meeting_id": resolved.meeting_id,
        "city": resolved.city,
        "meeting_date": resolved.meeting_date,
        "meeting_title": resolved.meeting_title,
        "source_kind": resolved.source_kind,
        "source_url": resolved.source_url,
        "notes": resolved.notes,
        "dry_run": dry_run,
    }

    if resolved.source_kind in ("no_video_source", "unsupported_city"):
        plan["next_action"] = "skip"
        return plan
    if resolved.source_kind == "existing_youtube_in_video_url":
        plan["next_action"] = "skip — defer to existing YouTube fetcher path"
        return plan
    if resolved.source_url is None:
        plan["next_action"] = "skip"
        plan["notes"] += " | resolved without URL — skipping"
        return plan

    plan["next_action"] = (
        "V1-RAG-3 worker will call mac_transcriber on this URL"
    )
    return plan


# ── CLI ───────────────────────────────────────────────────────────────


def _print_plan(plan: dict) -> None:
    print(
        f"[{plan['source_kind']:<24}] m{plan['meeting_id']} "
        f"{plan['meeting_date']} {plan['city']:<20} {plan['meeting_title'][:60]}"
    )
    if plan.get("source_url"):
        print(f"    URL: {plan['source_url']}")
    print(f"    notes: {plan['notes']}")
    print(f"    next: {plan['next_action']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="S-037 V0 — transcribe non-YouTube council meeting videos via mac_transcriber + add_text_source.",
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--meeting-id", type=int, help="ZSPAN internal meetings.id")
    g.add_argument("--work-order-id", type=int, help="ZSPAN work_orders.id")
    g.add_argument(
        "--city",
        type=str,
        help="Batch dry-run mode: scan recent meetings for this city",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=14,
        help="With --city: scan window in days (default 14, matches V1 past-2-weeks)",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually fire mac_transcriber + add_text_source. Default is dry-run.",
    )
    args = parser.parse_args()
    dry_run = not args.no_dry_run

    if args.work_order_id is not None:
        row = _get_meeting_row_for_work_order(args.work_order_id)
        if not row:
            logger.error("work_order_id=%s not found", args.work_order_id)
            return 2
        rows = [row]
    elif args.meeting_id is not None:
        row = _get_meeting_row(args.meeting_id)
        if not row:
            logger.error("meeting_id=%s not found", args.meeting_id)
            return 2
        rows = [row]
    else:
        rows = _get_city_meetings_in_window(args.city, args.days_back)
        if not rows:
            logger.warning(
                "no meetings found for city=%r in past %d days",
                args.city, args.days_back,
            )
            return 0
        logger.info(
            "scanning %d meetings for %s in past %d days",
            len(rows), args.city, args.days_back,
        )

    plans = []
    for row in rows:
        plan = asyncio.run(transcribe_meeting(row, dry_run=dry_run))
        plans.append(plan)
        _print_plan(plan)
        print()

    # Summary
    by_kind: dict[str, int] = {}
    for p in plans:
        by_kind[p["source_kind"]] = by_kind.get(p["source_kind"], 0) + 1
    print("=" * 60)
    print(f"Summary ({len(plans)} meetings) — dry_run={dry_run}:")
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind:<24} {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
proofs_uploader — Z-SPAN Proofs YouTube channel uploader (T-009 Phase 2).
==========================================================================

For each featured quote with `word_timings`, this module can:

  1. Extract a tight video+audio clip from the source YouTube meeting
     recording using yt-dlp's download-ranges + ffmpeg keyframe-cut.
  2. Compute SHA256 of the clip — tamper-evidence anchor that lives in
     the YouTube description.
  3. Upload to the Z-SPAN Proofs channel via the YouTube Data API v3
     (OAuth-authenticated; see `youtube_oauth.py`).
  4. Persist the resulting public URL into `member_quotes.proof_clip_url`.

Per `DECISIONS.md § D-040`: this is the legal-armor + permanence layer
of T-009's triple-source verification. Per `DECISIONS.md § D-041`: Gemini
multimodal verification (the original Phase 1) is parked indefinitely;
the human-via-karaoke gate suffices for V1 verification. The Proofs
channel still has value (tamper-evidence, public-record permanence, SEO,
brand presence) and that's what this module enables.

Privacy defaults to `unlisted` — uploads are accessible only to people
with the URL until the operator promotes them. This is the safer
default; operator promotes to `public` after the two-gate review
(D-032) confirms the broadcast is publication-ready.

This module is NOT auto-invoked by the worker. The first uploads happen
via `zspan_pipeline/scripts/upload_one_proof.py` so the operator can
spot-check every clip before batch-automating. Once trust is established,
a future chunk wires automatic upload into the same fetcher trigger
flow as the alignment.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Clip parameters ──────────────────────────────────────────────────

# Lead-in / lead-out buffer around the quote. Word-level timings are
# precise to the spoken word; the buffer absorbs minor alignment drift
# AND gives the viewer brief context before/after the verbatim content.
CLIP_LEAD_SECONDS = 2.0
CLIP_TRAIL_SECONDS = 2.0

# Format selector — 720p max keeps upload size reasonable (~30 MB/min
# for typical council-meeting video) and YouTube re-encodes anyway.
# Falls back gracefully if 720p isn't available.
DEFAULT_FORMAT_SELECTOR = "bv*[height<=720]+ba/b[height<=720]/best"

# YouTube category 25 = News & Politics. Sensible default for civic
# meeting clips.
DEFAULT_CATEGORY_ID = "25"

# Privacy ladder. Operator-controlled.
PRIVACY_UNLISTED = "unlisted"
PRIVACY_PUBLIC = "public"
PRIVACY_PRIVATE = "private"


# ── Exceptions ──────────────────────────────────────────────────────


class ProofsError(Exception):
    """Base for Proofs uploader failures."""


class ProofsClipError(ProofsError):
    """Clip extraction (yt-dlp + ffmpeg) failed."""


class ProofsUploadError(ProofsError):
    """YouTube upload failed."""


class ProofsAuthError(ProofsError):
    """OAuth credentials missing or revoked. Run setup_youtube_auth.py."""


# ── Clip data shape ──────────────────────────────────────────────────


@dataclass
class ProofClipMetadata:
    """Everything `upload_one_proof` needs to publish a proof clip.

    The orchestrator (`upload_proof_for_quote`) queries the DB and
    populates this; individual helpers take it as a structured arg so
    the data flow is auditable.
    """
    quote_id: int
    quote_text: str          # the cleaned verbatim text
    speaker_name: str        # canonical name from city_intelligence
    topic_tag: str           # primary topic (e.g., "water_rights")
    city_name: str
    meeting_date: str        # YYYY-MM-DD
    meeting_title: str
    source_video_url: str    # the meeting's YouTube URL
    clip_start_seconds: float
    clip_end_seconds: float


# ── Clip extraction ─────────────────────────────────────────────────


def _ffmpeg_path() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise ProofsClipError(
            "ffmpeg not on PATH. Install via your platform's package manager."
        )
    return p


def _ensure_deno_on_path() -> None:
    """yt-dlp needs deno (or another JS runtime) on PATH to solve YouTube's
    JS challenges. winget installs deno but doesn't update PATH for the
    current shell; we hard-prepend it here so the worker process always
    finds it regardless of how it was launched.

    No-op if deno is already on PATH or the winget install dir doesn't
    exist (which means deno was installed via a different method that
    presumably did update PATH).
    """
    if shutil.which("deno"):
        return
    # Windows-only path (winget's per-user install dir); resolved from
    # LOCALAPPDATA so it works for any user profile. No-op elsewhere.
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if not local_appdata:
        return
    winget_deno = (
        Path(local_appdata)
        / "Microsoft/WinGet/Packages"
        / "DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe"
    )
    if winget_deno.exists():
        current = os.environ.get("PATH", "")
        if str(winget_deno) not in current:
            os.environ["PATH"] = str(winget_deno) + os.pathsep + current


def download_source_video(
    source_video_url: str,
    output_path: Path,
    *,
    format_selector: str = "worst[ext=mp4]/worst",
) -> Path:
    """Download the full source video at the smallest available
    progressive format. Cached per meeting — subsequent clip extractions
    against the same meeting reuse this file.

    Why progressive (not the higher-quality DASH separate-stream
    formats): on the dev environment (2026-05-16) yt-dlp's
    `download_ranges` + DASH-stream combination hung silently, with
    ffmpeg's HTTP range requests against googlevideo CDN URLs writing
    48 bytes and stalling indefinitely. Audio-only Whisper downloads
    (Phase 0a) worked fine, suggesting the CDN issue is specific to
    video-format segments on this network. Switching to a single
    progressive mp4 file (itag 18 / 360p typically) sidesteps the
    streaming-range path entirely — yt-dlp does a normal HTTP download
    of the full file, which works reliably.

    At Mohave-pilot scale (~30 meetings/month × ~150 MB at 360p ≈ 4.5
    GB/month) the storage cost is small. Caller is responsible for
    eventually cleaning up old source.mp4 files (the per-meeting
    media/<meeting_id>/proofs/ dir gets cleaned with the meeting).

    Idempotent: if `output_path` already exists with non-trivial size,
    returns it without re-downloading.

    Raises ProofsClipError on yt-dlp failure.
    """
    if output_path.exists() and output_path.stat().st_size > 100_000:
        logger.info(
            "proofs: source video already cached at %s (%.1f MB) — skipping download",
            output_path.name, output_path.stat().st_size / (1024 * 1024),
        )
        return output_path

    try:
        import yt_dlp
    except ImportError as e:
        raise ProofsClipError(
            "yt-dlp not installed; add to parsers/requirements.txt"
        ) from e

    _ensure_deno_on_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    base = output_path.with_suffix("")
    outtmpl = str(base) + ".%(ext)s"

    ydl_opts = {
        "format": format_selector,
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # EJS components are fetched from GitHub on first run + cached.
        # Required since 2026-05-16 to solve YouTube's n-challenge.
        "remote_components": ["ejs:github"],
    }

    # SSRF: a scraped source_video_url must not steer the fetcher at localhost,
    # the LAN, or a cloud metadata endpoint (S-144 poisoned-upstream). Fail as a
    # ProofsClipError so the caller's existing handling applies.
    from safe_fetch import assert_safe_url, UnsafeUrlError
    try:
        assert_safe_url(source_video_url)
    except UnsafeUrlError as _ssrf_exc:
        raise ProofsClipError(
            f"refusing an unsafe source URL: {_ssrf_exc}"
        ) from _ssrf_exc

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source_video_url, download=True)
            downloaded = Path(ydl.prepare_filename(info))
    except Exception as e:
        raise ProofsClipError(
            f"yt-dlp source-video download failed: {e}"
        ) from e

    if not downloaded.exists():
        candidates = sorted(
            base.parent.glob(base.name + ".*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise ProofsClipError(
                f"yt-dlp returned but no source file at {downloaded}"
            )
        downloaded = candidates[0]

    # Normalize to the requested output_path so the cache check
    # short-circuits next time.
    if downloaded != output_path:
        if output_path.exists():
            output_path.unlink()
        downloaded.rename(output_path)

    return output_path


def cut_clip_from_source(
    source_path: Path,
    start_seconds: float,
    end_seconds: float,
    output_path: Path,
) -> Path:
    """ffmpeg fast-copy cut a window from an already-downloaded source.

    Uses `-c copy` so there's no re-encoding (fast, no quality loss).
    Snaps to the nearest preceding keyframe — typically within 2-5
    seconds of the requested start. Our 2-second lead/trail buffer
    around the actual quote text absorbs this, so the quote words are
    always inside the clip.

    Raises ProofsClipError on ffmpeg failure.
    """
    if not source_path.exists():
        raise ProofsClipError(f"source video not found: {source_path}")

    ffmpeg = _ffmpeg_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.1, end_seconds - start_seconds)

    # Note: `-ss` BEFORE `-i` is the "fast seek" form — ffmpeg seeks
    # directly to the keyframe near `start_seconds` without scanning
    # the file from the start. Combined with `-c copy`, this is
    # essentially instant regardless of file size.
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-ss", f"{start_seconds:.2f}",
        "-i", str(source_path),
        "-t", f"{duration:.2f}",
        "-c", "copy",
        # Avoid trailing 0-duration frames that some players choke on.
        "-avoid_negative_ts", "make_zero",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired as e:
        raise ProofsClipError(
            f"ffmpeg cut timed out after 300s on {source_path}"
        ) from e

    if result.returncode != 0:
        raise ProofsClipError(
            f"ffmpeg cut failed (rc={result.returncode}): "
            f"{result.stderr.strip()[:400]}"
        )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ProofsClipError(
            f"ffmpeg returned rc=0 but {output_path} is missing/empty"
        )

    return output_path


def extract_clip(
    source_video_url: str,
    start_seconds: float,
    end_seconds: float,
    output_path: Path,
    format_selector: str = "worst[ext=mp4]/worst",
) -> Path:
    """Orchestrator: ensure the full source video is cached locally,
    then ffmpeg-cut the requested window.

    Two-stage by design (see `download_source_video` for the network
    rationale). The source is cached as `source.mp4` in the same parent
    directory as `output_path`, so calling this multiple times against
    the same meeting reuses the source.

    Returns the path of the cut clip. Raises ProofsClipError on any
    failure.
    """
    source_path = output_path.parent / "source.mp4"
    download_source_video(source_video_url, source_path, format_selector=format_selector)
    cut_clip_from_source(source_path, start_seconds, end_seconds, output_path)
    return output_path


# ── SHA256 hashing ──────────────────────────────────────────────────


def compute_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA256 hexdigest of the file. Streamed read so
    multi-MB clips don't load fully into memory.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            buf = fh.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


# ── Upload metadata (title + description per D-040) ─────────────────


def build_title(meta: ProofClipMetadata) -> str:
    """`<city> – <meeting date> – <member name> – <topic tag>`

    Per D-040. YouTube title limit is 100 chars; we truncate the topic
    if needed since the leading fields are more search-relevant.
    """
    base = f"{meta.city_name} – {meta.meeting_date} – {meta.speaker_name}"
    suffix = f" – {meta.topic_tag}" if meta.topic_tag else ""
    full = base + suffix
    if len(full) > 100:
        # Truncate the topic, not the city/date/speaker.
        max_topic_chars = 100 - len(base) - len(" – ") - 1
        if max_topic_chars > 5 and meta.topic_tag:
            return base + " – " + meta.topic_tag[:max_topic_chars] + "…"
        return base[:99] + "…"
    return full


def build_description(
    meta: ProofClipMetadata,
    clip_sha256: str,
    timestamp_in_source: float,
) -> str:
    """Multi-line description embedding tamper-evidence + provenance.

    Per D-040: SHA256 in the description, plus a link back to the
    original meeting recording at the exact timestamp so any viewer
    can verify the clip is a verbatim slice of public-record audio.
    """
    seconds = int(timestamp_in_source)
    source_with_ts = (
        f"{meta.source_video_url}"
        f"{'&' if '?' in meta.source_video_url else '?'}t={seconds}s"
    )
    iso_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return (
        f"Verbatim slice from the public-record recording of the "
        f"{meta.city_name} council meeting on {meta.meeting_date}.\n\n"
        f"Speaker: {meta.speaker_name}\n"
        f"Topic: {meta.topic_tag or 'general'}\n"
        f"Meeting: {meta.meeting_title}\n\n"
        f"Quote (verbatim, cleaned of disfluencies):\n"
        f"“{meta.quote_text}”\n\n"
        f"Source recording (jumps to this quote's start): {source_with_ts}\n\n"
        f"Tamper-evidence:\n"
        f"  Clip SHA256: {clip_sha256}\n"
        f"  Z-SPAN upload timestamp: {iso_ts}\n\n"
        f"This clip is a Z-SPAN Proofs archive entry. Z-SPAN is an "
        f"independent civic-record streaming project covering Mohave "
        f"County, Arizona, council meetings. Government meeting "
        f"recordings are public records; this clip is lawful "
        f"republication for civic transparency purposes."
    )


# ── YouTube upload ──────────────────────────────────────────────────


def upload_clip(
    clip_path: Path,
    title: str,
    description: str,
    *,
    privacy_status: str = PRIVACY_UNLISTED,
    category_id: str = DEFAULT_CATEGORY_ID,
    tags: Optional[list[str]] = None,
) -> dict:
    """Upload a video file to the authenticated user's YouTube channel.

    Returns the API response dict; key field is `id` (the YouTube
    video ID). The public URL is `https://youtu.be/<id>`.

    Raises:
        ProofsAuthError if OAuth credentials are missing/revoked.
        ProofsUploadError on API failure.
    """
    if not clip_path.exists():
        raise ProofsUploadError(f"clip file not found: {clip_path}")

    # Lazy imports — keep the heavy Google deps off cold-start when
    # the uploader isn't actually being used.
    try:
        from youtube_oauth import build_youtube_service
        from googleapiclient.http import MediaFileUpload
        from googleapiclient.errors import HttpError
    except ImportError as e:
        raise ProofsAuthError(
            f"YouTube upload deps missing ({e}). Install via "
            "`pip install -r parsers/requirements.txt`."
        ) from e

    try:
        youtube = build_youtube_service()
    except RuntimeError as e:
        # build_youtube_service raises when not authorized.
        raise ProofsAuthError(str(e)) from e

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            # Embeddable + public stats default to true; explicit for clarity.
            "embeddable": True,
            "publicStatsViewable": True,
            "selfDeclaredMadeForKids": False,
        },
    }
    if tags:
        body["snippet"]["tags"] = tags

    media = MediaFileUpload(
        str(clip_path),
        chunksize=-1,        # upload in one shot for clips <100 MB
        resumable=True,
        mimetype="video/mp4",
    )

    try:
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )
        # Resumable upload — single next_chunk call when chunksize=-1.
        response = None
        while response is None:
            status, response = request.next_chunk()
        return response
    except HttpError as e:
        raise ProofsUploadError(f"YouTube API error: {e}") from e
    except Exception as e:
        raise ProofsUploadError(f"upload failed: {e}") from e


# ── Local clips (T-013 human review workflow) ──────────────────────
#
# Parallel destination to YouTube upload: write the clip + a sidecar
# JSON of metadata into a per-meeting `review_queue` folder, then
# generate a markdown REVIEW_GUIDE.md. The reviewer drags clips into
# Gemini Pro alongside the verification prompt template (per S-001 /
# T-013), converts human review from O(meeting) to O(clip).
#
# Same clip-extraction code as the YouTube upload path — the only
# difference is the output destination.


import json as _json
import re as _re


def _slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe slug. Lowercase, hyphenated, no path separators."""
    if not text:
        return "untitled"
    s = _re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    if len(s) > max_len:
        s = s[:max_len].rstrip("_")
    return s or "untitled"


def save_clip_sidecar(
    sidecar_path: Path,
    meta: ProofClipMetadata,
    clip_filename: str,
    clip_sha256: str,
    clip_size_bytes: int,
) -> Path:
    """Write the per-clip metadata JSON next to a local clip.

    Shape mirrors what the review UI (or any downstream automation)
    needs: speaker attribution, quote text, exact timestamps, file
    integrity hash, source-video deep-link. The verification status
    fields are placeholders for the future "round-trip" capability
    where the reviewer's Gemini Pro response gets parsed back + updates
    `member_quotes.verified_status`.
    """
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    seconds_in_source = int(meta.clip_start_seconds + CLIP_LEAD_SECONDS)
    sep = "&" if "?" in meta.source_video_url else "?"
    data = {
        "quote_id": meta.quote_id,
        "speaker_name": meta.speaker_name,
        "topic_tag": meta.topic_tag,
        "quote_text": meta.quote_text,
        "meeting": {
            "city": meta.city_name,
            "date": meta.meeting_date,
            "title": meta.meeting_title,
            "source_video_url": meta.source_video_url,
            "source_video_at_quote": f"{meta.source_video_url}{sep}t={seconds_in_source}s",
        },
        "clip": {
            "filename": clip_filename,
            "size_bytes": clip_size_bytes,
            "sha256": clip_sha256,
            "duration_seconds": round(
                meta.clip_end_seconds - meta.clip_start_seconds, 2
            ),
            "source_start_seconds": round(meta.clip_start_seconds, 2),
            "source_end_seconds": round(meta.clip_end_seconds, 2),
        },
        "review": {
            # Placeholders — populated by a future round-trip ingestion of
            # the reviewer's Gemini Pro response. Until then, the operator
            # can manually flip this when they approve / reject.
            "status": "pending",   # pending / verified / disputed / rejected
            "notes": None,
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    sidecar_path.write_text(
        _json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return sidecar_path


# ── Top-level orchestrator ──────────────────────────────────────────


def _resolve_quote_metadata(quote_id: int) -> Optional[ProofClipMetadata]:
    """Load the full ProofClipMetadata for a quote from the DB.
    Returns None if the quote, member, meeting, or word_timings are
    missing.
    """
    import json
    import sys
    from pathlib import Path as _P

    _parsers = _P(__file__).resolve().parent
    if str(_parsers) not in sys.path:
        sys.path.insert(0, str(_parsers))

    from database import get_connection

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT mq.id, mq.quote_text, mq.topic_tags, mq.word_timings,
               cm.name AS speaker_name,
               m.id AS meeting_id, m.city_name, m.meeting_date,
               m.meeting_title,
               COALESCE(wo.youtube_video_url, m.video_url) AS source_video_url
        FROM member_quotes mq
        JOIN council_members cm ON cm.id = mq.member_id
        JOIN meetings m ON m.id = mq.meeting_id
        LEFT JOIN work_orders wo ON wo.meeting_id = m.id
        WHERE mq.id = ?
        """,
        (quote_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None
    if not row["source_video_url"]:
        return None
    if not row["word_timings"]:
        return None

    try:
        word_timings = json.loads(row["word_timings"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(word_timings, list) or not word_timings:
        return None

    first_ms = word_timings[0].get("start_ms", 0)
    last_ms = word_timings[-1].get("end_ms", first_ms)
    clip_start = max(0.0, (first_ms / 1000.0) - CLIP_LEAD_SECONDS)
    clip_end = (last_ms / 1000.0) + CLIP_TRAIL_SECONDS

    # Primary topic — first tag, fallback to "other".
    topic_tag = "other"
    try:
        tags = json.loads(row["topic_tags"]) if row["topic_tags"] else []
        if isinstance(tags, list) and tags:
            topic_tag = str(tags[0])
    except (json.JSONDecodeError, TypeError):
        pass

    return ProofClipMetadata(
        quote_id=row["id"],
        quote_text=row["quote_text"],
        speaker_name=row["speaker_name"],
        topic_tag=topic_tag,
        city_name=row["city_name"],
        meeting_date=row["meeting_date"],
        meeting_title=row["meeting_title"],
        source_video_url=row["source_video_url"],
        clip_start_seconds=clip_start,
        clip_end_seconds=clip_end,
    )


def upload_proof_for_quote(
    quote_id: int,
    *,
    clip_work_dir: Optional[Path] = None,
    privacy_status: str = PRIVACY_UNLISTED,
    keep_clip: bool = False,
) -> dict:
    """Top-level orchestrator: lookup → clip → hash → upload → persist URL.

    Returns the final result dict:
        {
          "quote_id": int,
          "clip_path": str,        # set during clip extraction; deleted unless keep_clip
          "clip_sha256": str,
          "youtube_video_id": str,
          "youtube_url": str,
          "title": str,
          "privacy_status": str,
        }

    Raises ProofsError subclasses on any failure stage.
    """
    import sys
    from pathlib import Path as _P
    _parsers = _P(__file__).resolve().parent
    if str(_parsers) not in sys.path:
        sys.path.insert(0, str(_parsers))
    from database import get_connection

    meta = _resolve_quote_metadata(quote_id)
    if meta is None:
        raise ProofsError(
            f"Cannot build proof clip for quote {quote_id} — missing "
            "video URL, word_timings, or quote row. Ensure transcript_words "
            "+ alignment have run for the meeting."
        )

    # Default work dir: media/<meeting_id>/proofs/
    if clip_work_dir is None:
        # We pull the meeting_id back out from the orchestrator's resolve.
        # The orchestrator doesn't return meeting_id directly; query for it.
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT meeting_id FROM member_quotes WHERE id = ?", (quote_id,))
        row = cur.fetchone()
        conn.close()
        meeting_id = row["meeting_id"] if row else 0
        clip_work_dir = (
            _P(__file__).resolve().parent.parent / "media" / str(meeting_id) / "proofs"
        )

    clip_work_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clip_work_dir / f"quote_{quote_id}.mp4"

    logger.info(
        "proofs: extracting clip for quote %s (%.1fs-%.1fs from %s)",
        quote_id, meta.clip_start_seconds, meta.clip_end_seconds,
        meta.source_video_url,
    )
    actual_clip_path = extract_clip(
        meta.source_video_url,
        meta.clip_start_seconds,
        meta.clip_end_seconds,
        clip_path,
    )
    clip_size_mb = actual_clip_path.stat().st_size / (1024 * 1024)
    logger.info(
        "proofs: clip extracted, %s (%.2f MB)",
        actual_clip_path.name, clip_size_mb,
    )

    sha = compute_sha256(actual_clip_path)
    logger.info("proofs: SHA256 = %s", sha)

    title = build_title(meta)
    description = build_description(
        meta, sha, timestamp_in_source=meta.clip_start_seconds + CLIP_LEAD_SECONDS
    )

    logger.info("proofs: uploading to YouTube as '%s' (%s)", title, privacy_status)
    response = upload_clip(
        actual_clip_path,
        title,
        description,
        privacy_status=privacy_status,
        tags=[meta.city_name, meta.topic_tag, "Z-SPAN", "council meeting"],
    )

    video_id = response.get("id")
    if not video_id:
        raise ProofsUploadError(
            f"YouTube response missing video id: {response}"
        )
    youtube_url = f"https://youtu.be/{video_id}"
    logger.info("proofs: uploaded → %s", youtube_url)

    # Persist back to the quote row.
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE member_quotes SET proof_clip_url = ? WHERE id = ?",
        (youtube_url, quote_id),
    )
    conn.commit()
    conn.close()

    result = {
        "quote_id": quote_id,
        "clip_path": str(actual_clip_path),
        "clip_sha256": sha,
        "youtube_video_id": video_id,
        "youtube_url": youtube_url,
        "title": title,
        "privacy_status": privacy_status,
    }

    if not keep_clip:
        try:
            actual_clip_path.unlink()
            result["clip_path"] = None
        except OSError as e:
            logger.warning("proofs: failed to clean up clip %s: %s",
                           actual_clip_path, e)

    return result

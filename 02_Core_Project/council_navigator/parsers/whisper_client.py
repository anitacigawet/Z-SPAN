"""
whisper_client — word-level Whisper transcription for civic meetings.
=====================================================================

Used by the T-009 Phase 0 pipeline (per `DECISIONS.md § D-040`) to produce
word-level timestamps for council meeting recordings. The word-array feeds:

  * Phase 0a: per-quote "Watch at <m:ss>" deep-links (precise Whisper-
              anchored values once the alignment layer (Phase 0b) lands).
  * Phase 0c: the synced-transcript player on the Cast page (Otter/
              Descript-style word-by-word highlighting in sync with
              embedded YouTube playback).
  * Phase 1+: cost-optimized multimodal-clip verification (anchor for
              the 30s window around each featured quote).

Doctrinal note (same exemption as `quote_cleaner.py`):
The V1-RAG-3 pipeline remains the sole brain for civic content generation. Whisper is
*mechanical transcription* — it extracts WHAT was said, not WHICH parts
matter. The synthesis pass still picks the quotes, the synopsis, the topics.

──────────────────────────────────────────────────────────────────────
Three transcription paths:
──────────────────────────────────────────────────────────────────────

  * AssemblyAI pre-recorded transcription — flagship provider, with
    meeting-local anonymous speaker labels adapted at this boundary.
  * Local Mac transcription node — current default at James's setup
    (faster-whisper distil-large-v3 INT8 on the dedicated 2015 MacBook;
    see `02_Core_Project/mac_transcriber/`). $0 marginal cost; identity-
    isolated infrastructure per `FUTURE_THOUGHTS.md § S-008` pillar 3.
  * OpenAI paid Whisper API (`whisper-1`) — historical canonical, still
    available as the dormant fallback. Cost was ~$0.006/min ≈ $0.46
    per 76-min meeting (m101091 baseline) before the Mac retired it.

Routing happens in `transcribe_youtube()` at the bottom of this module
based on `user_settings.json:zspan_whisper_provider`:

  * `"mac_node"`  → POST to the Mac via `transcribe_youtube_via_mac_node`
  * `"openai"`    → paid API via `_transcribe_youtube_via_openai`
  * `"assemblyai"` → AssemblyAI REST upload + pre-recorded transcript
  * unset (default) → `"openai"` (no behavior change for callers that
                       haven't opted in to the Mac)

Mac-node errors raise `WhisperNodeError` and do NOT silently fall
through to OpenAI unless `user_settings.zspan_whisper_fallback_to_openai`
is set to `true`. This is deliberate — operator should notice when the
Mac is down rather than silently spending paid Whisper.

Key resolution:
  * AssemblyAI path: env var ASSEMBLYAI_API_KEY →
                     `user_settings.json:assemblyai_api_key`
  * OpenAI path: env var OPENAI_API_KEY → `user_settings.json:openai_api_key`
  * Mac path: base URL from `02_Core_Project/mac_transcriber/STATUS.json`
              (committed by Mac-side Claude during bootstrap) +
              bearer token from `user_settings.json:zspan_whisper_node_token`
              (operator-copied from the Mac's launchd plist; never committed)

Provider credentials are resolved at dispatch time from their documented
environment variables or `user_settings.json` keys.
(The `active_provider` toggle this used to sit beside was retired with the
legacy Navigator AI subsystem; the key itself is set on the Settings page.)

See:
  * `01_Project_Overview/FINANCIAL_AUDIT.md § 2.1` — why the swap happened
  * `01_Project_Overview/FUTURE_THOUGHTS.md § S-019` — Mac as infrastructure
                                                       + 8th-employee vision
  * `02_Core_Project/mac_transcriber/SETUP.md` — the Mac side
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from env_config import load_user_settings

logger = logging.getLogger(__name__)


WHISPER_MODEL = "whisper-1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
ASSEMBLYAI_BASE_URL = "https://api.assemblyai.com"
ASSEMBLYAI_SPEECH_MODELS = ["universal-3-5-pro", "universal-2"]
ASSEMBLYAI_POLL_INTERVAL_SECONDS = 3
ASSEMBLYAI_POLL_TIMEOUT_SECONDS = 10 * 60 * 60

# Whisper transcripts of multi-hour meetings can take several minutes
# server-side. Generous timeout protects against hung connections without
# blocking forever.
WHISPER_TIMEOUT_SECONDS = 900

# OpenAI's documented hard limit on uploaded audio file size.
WHISPER_MAX_FILE_BYTES = 25 * 1024 * 1024

# Hard ceiling for the source handed to yt-dlp. ``max_filesize`` handles
# declared Content-Length; the progress hook below covers chunked/unknown
# length responses.
SOURCE_DOWNLOAD_MAX_BYTES = 500 * 1024 * 1024


# ── Exceptions ────────────────────────────────────────────────────


class WhisperError(Exception):
    """Base class for Whisper pipeline failures."""


class WhisperConfigError(WhisperError):
    """Raised when the OpenAI API key isn't configured."""


class WhisperFileTooLargeError(WhisperError):
    """Raised when the transcoded audio still exceeds OpenAI's 25 MB upload
    limit AFTER the ffmpeg pass.

    With the default 24 kbps mono Opus output, ~140 minutes fits in 25 MB.
    Anything longer needs chunking (split audio into windows, transcribe
    each, merge with timestamp offsets). The chunking path is the Phase 0d
    follow-up; this exception is the trigger to build it.
    """


class WhisperFfmpegMissingError(WhisperError):
    """Raised when ffmpeg is not on PATH. Required for the transcode pass
    that gets the audio under Whisper's 25 MB limit. Install via the
    platform package manager (winget install Gyan.FFmpeg on Windows; brew
    install ffmpeg on macOS; apt-get install ffmpeg on Linux).
    """


class WhisperTranscodeError(WhisperError):
    """Raised when ffmpeg fails to transcode the downloaded audio. The
    underlying stderr is included in the exception message.
    """


class WhisperDownloadError(WhisperError):
    """Raised when yt-dlp fails to fetch the source audio."""


class WhisperHTTPError(WhisperError):
    """Raised on a non-200 response from the OpenAI Whisper endpoint."""


class WhisperNodeError(WhisperError):
    """Raised on a failure interacting with the local Mac transcription node
    (per `02_Core_Project/mac_transcriber/`). Network error, non-200 HTTP,
    non-JSON response, etc. Distinct from `WhisperHTTPError` (OpenAI path)
    so callers can tell which provider failed.
    """


class AssemblyAIError(WhisperError):
    """Raised when AssemblyAI upload, submission, polling, or adaptation fails."""


# ── Key resolution ────────────────────────────────────────────────


def _resolve_openai_key() -> str:
    """Resolve OPENAI_API_KEY env var → user_settings.json → empty."""
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key.strip()
    settings = load_user_settings()
    return (settings.get("openai_api_key") or "").strip()


def _resolve_assemblyai_key() -> str:
    """Resolve ASSEMBLYAI_API_KEY env var → user_settings.json → empty."""
    env_key = os.environ.get("ASSEMBLYAI_API_KEY")
    if env_key:
        return env_key.strip()
    settings = load_user_settings()
    return (settings.get("assemblyai_api_key") or "").strip()


def is_configured() -> bool:
    """Return True iff transcription can run via the active provider.

    Provider-aware: when `zspan_whisper_provider=mac_node` (the default since
    the D-111 Mac migration), the Mac transcriber + bearer token are what
    matter — no OpenAI key required. AssemblyAI and OpenAI each use their
    provider-specific key resolution.

    The previous version of this function checked only `_resolve_openai_key()`
    and silently failed transcript_words on every WO after the Mac swap. Fixed
    2026-06-18 (Bullhead WO#100791/100792 surfaced the bug — both had 11/12
    outputs succeed and the 12th `transcript_words` step rejected the WO with
    "OPENAI_API_KEY not configured" despite Mac-node being configured + working
    for the other outputs).
    """
    provider = _resolve_whisper_provider()
    if provider == "assemblyai":
        return bool(_resolve_assemblyai_key())
    if provider == "mac_node":
        try:
            base_url, token = _resolve_mac_node_config()
            return bool(base_url and token)
        except Exception:
            return False
    return bool(_resolve_openai_key())


# ── YouTube audio download via yt-dlp ─────────────────────────────


@dataclass
class DownloadedAudio:
    """Result of a YouTube audio download."""
    path: Path
    duration_seconds: Optional[float]
    title: Optional[str]


def _clear_download_partials(
    output_basepath: Path,
    progress_data: dict | None = None,
) -> None:
    """Delete only this output template's yt-dlp partial/current files."""
    parent = output_basepath.parent.resolve()
    prefix = output_basepath.name + "."

    for path in output_basepath.parent.glob(prefix + "*"):
        if path.is_file() and (
            path.name.endswith((".part", ".ytdl"))
            or ".part-Frag" in path.name
        ):
            path.unlink(missing_ok=True)

    for key in ("filename", "tmpfilename"):
        raw_path = (progress_data or {}).get(key)
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path).resolve()
        if path.parent == parent and path.name.startswith(prefix):
            path.unlink(missing_ok=True)


def _source_size_progress_hook(output_basepath: Path):
    """Abort an unknown-length source once its running total exceeds 500 MiB."""
    def _hook(progress_data: dict) -> None:
        if progress_data.get("status") != "downloading":
            return
        downloaded = progress_data.get("downloaded_bytes") or 0
        if downloaded <= SOURCE_DOWNLOAD_MAX_BYTES:
            return
        _clear_download_partials(output_basepath, progress_data)
        raise WhisperDownloadError(
            "yt-dlp source exceeded the 500 MiB download cap "
            f"({downloaded} bytes received)."
        )

    return _hook


def download_youtube_audio(
    youtube_url: str,
    output_basepath: Path,
) -> DownloadedAudio:
    """Download the smallest-bitrate audio-only stream from a YouTube URL.

    `output_basepath` is the path WITHOUT extension — yt-dlp appends its
    own (typically `.m4a` or `.webm`). The returned `DownloadedAudio.path`
    is the actual on-disk path including the extension yt-dlp chose.

    Format selection: `worstaudio` → yt-dlp picks the lowest-bitrate
    audio-only stream YouTube offers (typically 48-64 kbps). This keeps
    file size small for the Whisper upload limit. For a typical 1-2 hr
    meeting this yields a 20-40 MB file. Longer meetings exceed Whisper's
    25 MB limit and will fail on the transcribe step (caller's job to
    handle — see WhisperFileTooLargeError).

    Raises:
      WhisperDownloadError on yt-dlp failure (network, geo-block,
      unavailable video, etc.).
    """
    try:
        import yt_dlp  # noqa: F401
    except ImportError as e:
        raise WhisperDownloadError(
            "yt-dlp is not installed; add `yt-dlp` to parsers/requirements.txt"
        ) from e

    output_basepath.parent.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        # Smallest audio-only stream available.
        "format": "worstaudio/worst",
        "outtmpl": str(output_basepath) + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "max_filesize": SOURCE_DOWNLOAD_MAX_BYTES,
        "progress_hooks": [_source_size_progress_hook(output_basepath)],
        # We do NOT use FFmpegExtractAudio postprocessor — that would
        # require ffmpeg to be installed. The raw stream is fine for
        # Whisper; if it's too big the caller gets a clear error.
        "postprocessors": [],
    }

    # Pin each downloader hostname to its first all-public DNS answer set for
    # the complete yt-dlp operation. This includes extractor-discovered CDN
    # hosts and closes the gap between URL validation and yt-dlp's lookup.
    from safe_fetch import pinned_dns, UnsafeUrlError
    try:
        with pinned_dns(youtube_url):
            from yt_dlp import YoutubeDL
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=True)
                # `prepare_filename` returns the filename yt-dlp wrote to.
                downloaded = Path(ydl.prepare_filename(info))
    except UnsafeUrlError as _ssrf_exc:
        raise WhisperDownloadError(
            f"refusing an unsafe source URL: {_ssrf_exc}"
        ) from _ssrf_exc
    except WhisperDownloadError:
        raise
    except Exception as e:
        raise WhisperDownloadError(f"yt-dlp failed for {youtube_url}: {e}") from e

    if not downloaded.exists():
        # yt-dlp occasionally returns a different filename than
        # prepare_filename predicts (e.g., when the chosen format has a
        # different ext than info["ext"]). Glob the output dir.
        candidates = sorted(
            output_basepath.parent.glob(output_basepath.name + ".*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise WhisperDownloadError(
                f"yt-dlp did not produce a file at {downloaded}"
            )
        downloaded = candidates[0]

    return DownloadedAudio(
        path=downloaded,
        duration_seconds=(info.get("duration") if isinstance(info, dict) else None),
        title=(info.get("title") if isinstance(info, dict) else None),
    )


# ── ffmpeg transcode to a Whisper-friendly format ────────────────


# mp3 at 32 kbps mono is the safe choice for the Whisper upload: the API
# explicitly lists mp3 in its supported formats (Opus-in-OGG is NOT
# listed and returned HTTP 500 in testing 2026-05-16). 32 kbps mono fits
# ~7 min per MB.
DEFAULT_TRANSCODE_BITRATE = "32k"
DEFAULT_TRANSCODE_SAMPLE_RATE = 16000

# Audio is chunked before upload. Large multipart POSTs to OpenAI from
# the test environment (2026-05-16) failed with "Server disconnected
# without sending a response" / SSLEOFError at the 17 MB mark, even
# though a 1.15 MB upload of the same file succeeded in 19s. The exact
# cause is network-side (likely an intermediate proxy / antivirus
# inspecting HTTPS POST or an MTU fragmentation issue) and not worth
# diagnosing further when chunking trivially sidesteps it.
#
# 5-minute chunks at 32 kbps mono mp3 are ~1.2 MB each — small enough
# that the upload succeeds reliably AND the chunk count stays
# manageable (76-min meeting = 16 chunks). Whisper word timestamps
# within each chunk start at 0; the merge step adds `chunk_index *
# CHUNK_SECONDS` to each word's start/end so the final word array
# reads continuously across the full meeting.
CHUNK_SECONDS = 300


def _ffmpeg_path() -> str:
    """Locate ffmpeg on PATH. Raises WhisperFfmpegMissingError if absent."""
    p = shutil.which("ffmpeg")
    if not p:
        raise WhisperFfmpegMissingError(
            "ffmpeg not found on PATH. Install via your platform's package "
            "manager (e.g., winget install Gyan.FFmpeg, brew install ffmpeg, "
            "apt-get install ffmpeg)."
        )
    return p


def transcode_for_whisper(
    input_path: Path,
    output_path: Path,
    bitrate: str = DEFAULT_TRANSCODE_BITRATE,
    sample_rate: int = DEFAULT_TRANSCODE_SAMPLE_RATE,
) -> Path:
    """Transcode the downloaded audio to a low-bitrate mono Opus file that
    fits under Whisper's 25 MB upload cap.

    Whisper-1 is robust to low bitrates for speech — the quality floor is
    intelligibility, not fidelity. 24 kbps mono Opus at 16 kHz gives clean
    speech recognition and ~10 MB per hour of audio.

    Raises:
      WhisperFfmpegMissingError if ffmpeg isn't on PATH.
      WhisperTranscodeError on ffmpeg failure.
    """
    ffmpeg = _ffmpeg_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg,
        "-y",                       # overwrite
        "-loglevel", "error",
        "-i", str(input_path),
        "-vn",                      # discard any video stream
        "-c:a", "libmp3lame",       # mp3 — explicitly supported by Whisper
        "-b:a", bitrate,
        "-ac", "1",                 # mono
        "-ar", str(sample_rate),
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as e:
        raise WhisperTranscodeError(
            f"ffmpeg timed out after 600s on {input_path}"
        ) from e

    if result.returncode != 0:
        raise WhisperTranscodeError(
            f"ffmpeg failed (rc={result.returncode}): {result.stderr.strip()[:400]}"
        )

    if not output_path.exists():
        raise WhisperTranscodeError(
            f"ffmpeg returned rc=0 but {output_path} doesn't exist"
        )

    return output_path


def split_audio_into_chunks(
    input_path: Path,
    output_dir: Path,
    chunk_seconds: int = CHUNK_SECONDS,
) -> list[Path]:
    """Split `input_path` into N-second chunks via ffmpeg's segment muxer.

    Returns a sorted list of chunk paths. The segment muxer with `-c copy`
    is fast (no re-encode) — it splits at packet boundaries, so chunks
    may be slightly under/over `chunk_seconds`. Whisper handles short
    final chunks gracefully.

    Raises WhisperTranscodeError on ffmpeg failure.
    """
    ffmpeg = _ffmpeg_path()
    output_dir.mkdir(parents=True, exist_ok=True)
    template = output_dir / "chunk_%03d.mp3"

    cmd = [
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-i", str(input_path),
        "-f", "segment",
        "-segment_time", str(chunk_seconds),
        "-c", "copy",
        str(template),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as e:
        raise WhisperTranscodeError(
            f"ffmpeg segment timed out after 300s on {input_path}"
        ) from e

    if result.returncode != 0:
        raise WhisperTranscodeError(
            f"ffmpeg segment failed (rc={result.returncode}): "
            f"{result.stderr.strip()[:400]}"
        )

    chunks = sorted(output_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise WhisperTranscodeError(
            f"ffmpeg segment produced no chunk files in {output_dir}"
        )
    return chunks


# ── Whisper API call ──────────────────────────────────────────────


def transcribe_audio_file(
    audio_path: Path,
    *,
    prompt: Optional[str] = None,
) -> dict:
    """POST `audio_path` to the OpenAI Whisper endpoint, return word JSON.

    Returns a dict of shape:
        {
          "words": [{"word": str, "start": float, "end": float}, ...],
          "duration_seconds": float,
          "language": str,
        }

    Whisper's verbose_json with timestamp_granularities=["word"] is the
    native shape. We normalize each row to `{word, start, end}` to keep
    storage compact and downstream consumers stable. `start`/`end` are
    seconds (float).

    Args:
        audio_path: file to transcribe
        prompt: optional priming text (≤224 tokens). Whisper uses this as
            "prior context" and biases toward vocabulary it contains.
            T-017 Layer 1 uses this for proper-noun hints (council member
            names, local street names, civic terms) to reduce ASR errors
            at the source. See `build_whisper_prompt_for_city()`.

    Implementation uses the OpenAI SDK (`openai` package) rather than
    raw `requests`. The SDK handles SSL, retries, and multipart upload
    framing in a more robust way than our hand-rolled call. Note that
    large uploads (~17 MB+) failed network-side from the dev environment
    regardless of which HTTP library was used — chunk via
    `transcribe_audio_chunked` for production transcripts.

    Raises:
      WhisperConfigError if no API key is set.
      WhisperFileTooLargeError if the file exceeds 25 MB.
      WhisperHTTPError on a connection or API error.
    """
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)

    size_bytes = audio_path.stat().st_size
    if size_bytes > WHISPER_MAX_FILE_BYTES:
        size_mb = size_bytes / (1024 * 1024)
        raise WhisperFileTooLargeError(
            f"Audio file {audio_path.name} is {size_mb:.1f} MB; OpenAI "
            f"Whisper limit is 25 MB. Use `transcribe_audio_chunked` "
            f"or chunk manually with `split_audio_into_chunks` first."
        )

    api_key = _resolve_openai_key()
    if not api_key:
        raise WhisperConfigError("OPENAI_API_KEY not configured")

    # Lazy import: keeps the SDK out of cold-start for callers that
    # don't actually hit Whisper.
    from openai import OpenAI, APIError, APIConnectionError

    client = OpenAI(api_key=api_key, timeout=WHISPER_TIMEOUT_SECONDS)

    # Build call kwargs. `prompt` is optional — only include when set,
    # otherwise the SDK errors on `prompt=None` for some versions.
    create_kwargs = {
        "model": WHISPER_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities": ["word"],
    }
    if prompt:
        # Whisper's documented prompt limit is 224 tokens. Defensive
        # truncate at ~900 chars (~225 tokens for English) so we never
        # exceed it; longer prompts are silently truncated by OpenAI
        # but cleaner to handle at our boundary.
        create_kwargs["prompt"] = prompt[:900]

    try:
        with audio_path.open("rb") as fh:
            resp = client.audio.transcriptions.create(
                file=fh,
                **create_kwargs,
            )
    except APIConnectionError as e:
        raise WhisperHTTPError(f"connection error: {e}") from e
    except APIError as e:
        raise WhisperHTTPError(f"API error: {e}") from e

    # `resp.words` is a list of TranscriptionWord objects with .word/.start/.end
    raw_words = getattr(resp, "words", None) or []
    normalized_words = []
    for w in raw_words:
        word_text = getattr(w, "word", "") or ""
        word_text = word_text.strip()
        if not word_text:
            continue
        normalized_words.append({
            "word": word_text,
            "start": float(getattr(w, "start", 0.0) or 0.0),
            "end": float(getattr(w, "end", 0.0) or 0.0),
        })

    return {
        "words": normalized_words,
        "duration_seconds": float(getattr(resp, "duration", 0.0) or 0.0),
        "language": getattr(resp, "language", None) or "en",
    }


def transcribe_audio_chunked(
    audio_path: Path,
    chunk_dir: Optional[Path] = None,
    chunk_seconds: int = CHUNK_SECONDS,
    *,
    prompt: Optional[str] = None,
) -> dict:
    """Transcribe a (potentially long) audio file by splitting into chunks
    and merging the per-chunk word arrays with timestamp offsets.

    This is the production path for `transcribe_youtube` — long single-
    file uploads have proven unreliable from the dev environment (large
    multipart POSTs get dropped server-side), so we ALWAYS chunk even
    when the file would technically fit under the 25 MB cap. Per-chunk
    upload is ~1-2 MB and succeeds reliably in 15-25s.

    Args:
      audio_path: the (already-transcoded) audio file to process.
      chunk_dir: where to write the chunk files. Defaults to a `chunks/`
                 subdirectory next to audio_path. Cleaned up unless the
                 transcribe step raises (preserved for debugging).
      chunk_seconds: target chunk duration. 300s (5 min) is the default
                     and is reliably uploadable.

    Returns the same shape as `transcribe_audio_file`. `duration_seconds`
    is the SUM of per-chunk durations (essentially the original audio's
    duration, since copy-codec splits preserve duration).

    Raises the same exceptions as `transcribe_audio_file` (per-chunk).
    """
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)

    if chunk_dir is None:
        chunk_dir = audio_path.parent / "chunks"

    chunks = split_audio_into_chunks(audio_path, chunk_dir, chunk_seconds)
    logger.info(
        "whisper: split %s into %d chunks of ~%ds each",
        audio_path.name, len(chunks), chunk_seconds,
    )

    merged_words: list[dict] = []
    total_duration = 0.0
    language = "en"

    try:
        for idx, chunk_path in enumerate(chunks):
            offset = idx * float(chunk_seconds)
            chunk_size_mb = chunk_path.stat().st_size / (1024 * 1024)
            logger.info(
                "whisper: chunk %d/%d (%s, %.2f MB, offset=%.1fs)",
                idx + 1, len(chunks), chunk_path.name, chunk_size_mb, offset,
            )
            chunk_result = transcribe_audio_file(chunk_path, prompt=prompt)
            language = chunk_result.get("language") or language

            for w in chunk_result.get("words", []):
                merged_words.append({
                    "word": w["word"],
                    "start": w["start"] + offset,
                    "end": w["end"] + offset,
                })

            total_duration += chunk_result.get("duration_seconds") or 0.0
    finally:
        # Best-effort cleanup of chunk files. Leave them on a failure so
        # the operator can inspect; on success they're noise.
        for c in chunks:
            try:
                c.unlink()
            except OSError:
                pass
        try:
            chunk_dir.rmdir()
        except OSError:
            pass

    return {
        "words": merged_words,
        "duration_seconds": total_duration,
        "language": language,
    }


# ── Top-level orchestration ───────────────────────────────────────


def build_whisper_prompt_for_city(city_name: str) -> str:
    """T-017 Layer 1: build a Whisper `prompt` string from the city's
    canonical metadata. Reduces proper-noun ASR errors at the source
    by priming the model with council member names + civic vocab +
    optional per-city `whisper_vocabulary_hints` field from
    city_intelligence/<slug>.json.

    Returns "" if the city isn't found (caller can still transcribe
    without a prompt). Returns a natural-language sentence-style
    prompt under Whisper's 224-token / ~900-char budget.

    The format follows Whisper's documented best practice: prompts
    that read like prior natural-language context produce better
    biasing than raw token lists. So we emit:

      "This is a city council meeting in <city>, <state>. Speakers
      include <names>. Local references: <hints>."
    """
    if not city_name:
        return ""

    # Lazy import to avoid pulling database into modules that don't
    # need it. parsers/database.py is alongside this module.
    try:
        import sys
        _parsers = Path(__file__).resolve().parent
        if str(_parsers) not in sys.path:
            sys.path.insert(0, str(_parsers))
        from database import load_city_intelligence
    except Exception as e:
        logger.warning(
            "build_whisper_prompt_for_city: could not import database (%s); "
            "returning empty prompt.", e,
        )
        return ""

    intel = load_city_intelligence(city_name)
    if not intel:
        return ""

    state = (intel.get("state") or "").strip()
    members = intel.get("current_members") or []

    # Names sorted by role weight so the mayor/vice mayor lead the list
    # (most common in coverage; Whisper's biasing is order-sensitive).
    role_order = {"Mayor": 0, "Vice Mayor": 1, "Council Member": 2}
    sorted_members = sorted(
        members,
        key=lambda m: (role_order.get(m.get("role", ""), 9), m.get("name", "")),
    )
    names = [m.get("name", "").strip() for m in sorted_members if m.get("name")]

    hints = intel.get("whisper_vocabulary_hints") or []
    if isinstance(hints, str):
        # Tolerate single-string form ("Andy Devine Avenue, Beale Street")
        hints_text = hints.strip()
    elif isinstance(hints, list):
        # T-018: hints can be either strings (legacy / manual entries)
        # OR objects `{term, category?, first_seen?, source?, ...}`
        # promoted via the Vocabulary Inbox. Normalize to a flat term
        # list so the Whisper prompt sees just the spellings.
        terms: list[str] = []
        for h in hints:
            if isinstance(h, str):
                t = h.strip()
            elif isinstance(h, dict):
                t = str(h.get("term", "")).strip()
            else:
                t = ""
            if t:
                terms.append(t)
        hints_text = ", ".join(terms)
    else:
        hints_text = ""

    # Standard civic vocab — short, well under budget, useful for any
    # council-meeting recording.
    civic_vocab = (
        "agenda item, consent calendar, executive session, public comment, "
        "motion to approve, roll call, adjournment"
    )

    parts: list[str] = []
    parts.append(
        f"This is a city council meeting in {city_name}"
        + (f", {state}" if state else "")
        + "."
    )
    if names:
        parts.append("Speakers include " + ", ".join(names) + ".")
    if hints_text:
        parts.append("Local references: " + hints_text + ".")
    parts.append("Civic vocabulary: " + civic_vocab + ".")

    prompt = " ".join(parts)
    # Whisper's documented limit is 224 tokens (~900 chars English).
    # Hard truncate at the boundary.
    if len(prompt) > 900:
        prompt = prompt[:900].rsplit(" ", 1)[0] + "."
    return prompt


def _transcribe_youtube_via_openai(
    youtube_url: str,
    work_dir: Path,
    keep_audio: bool = False,
    *,
    prompt: Optional[str] = None,
) -> dict:
    """Download a YouTube audio file + transcribe via OpenAI Whisper API.

    The original `transcribe_youtube` implementation (renamed 2026-05-31
    when the Mac-node dispatcher landed at the public-API name). Still
    invoked by the dispatcher when `zspan_whisper_provider == "openai"`
    (the default — no behavior change for existing callers).

    Args:
      youtube_url: source URL.
      work_dir: directory the audio file is written to (created if needed).
      keep_audio: if True, leave the audio file on disk for inspection
                  (Phase 0b alignment work; debugging); if False, delete it
                  after transcription succeeds.

    Returns the same dict as `transcribe_audio_file`, plus:
      `source_url` (the YouTube URL),
      `source_title` (yt-dlp-extracted video title, if available).

    Cleanup: audio file is best-effort deleted on success when
    keep_audio=False. On failure mid-flight, the file is left for the
    caller to inspect (don't hide diagnostics by deleting evidence).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    output_basepath = work_dir / "audio"
    transcoded_path = work_dir / "audio.mp3"

    # Idempotency / retry-friendly: if a transcoded mp3 from a prior run
    # is already present in this work_dir, skip download + transcode and
    # go straight to chunked transcribe. This makes mid-flight retries
    # cheap (no re-download) and gives the operator a path to inspect
    # the audio between runs (just leave the mp3 in place). To force a
    # fresh download, delete `work_dir/audio.mp3` manually first.
    if transcoded_path.exists() and transcoded_path.stat().st_size > 0:
        logger.info(
            "whisper: reusing existing transcoded audio %s (%.1f MB) — "
            "skipping download + transcode",
            transcoded_path.name,
            transcoded_path.stat().st_size / (1024 * 1024),
        )
        downloaded = DownloadedAudio(
            path=transcoded_path, duration_seconds=None, title=None,
        )
        raw_size_mb = 0.0
        transcoded_size_mb = transcoded_path.stat().st_size / (1024 * 1024)
    else:
        downloaded = download_youtube_audio(youtube_url, output_basepath)
        raw_size_mb = downloaded.path.stat().st_size / (1024 * 1024)
        logger.info(
            "whisper: downloaded audio %s (%.1f MB raw, duration=%ss) for %s",
            downloaded.path.name,
            raw_size_mb,
            downloaded.duration_seconds,
            youtube_url,
        )

        # Transcode to low-bitrate mono mp3 so the file fits under
        # Whisper's 25 MB upload cap (per-chunk; we always chunk now).
        # Whisper explicitly supports mp3; Opus-in-OGG caused HTTP 500
        # in testing 2026-05-16.
        transcode_for_whisper(downloaded.path, transcoded_path)
        transcoded_size_mb = transcoded_path.stat().st_size / (1024 * 1024)
        logger.info(
            "whisper: transcoded to %s (%.1f MB, %.1fx compression)",
            transcoded_path.name,
            transcoded_size_mb,
            raw_size_mb / transcoded_size_mb if transcoded_size_mb else 0,
        )

    try:
        result = transcribe_audio_chunked(transcoded_path, prompt=prompt)
    finally:
        # Clean up the raw m4a/webm immediately — we only need the
        # transcoded file for the upload, and the raw is the bigger of the
        # two. Cleanup of the transcoded file happens after this block.
        # In the reuse path (downloaded.path == transcoded_path), skip
        # this cleanup so we don't double-delete + warn.
        if not keep_audio and downloaded.path != transcoded_path:
            try:
                downloaded.path.unlink()
            except OSError:
                pass

    result["source_url"] = youtube_url
    result["source_title"] = downloaded.title
    result["audio_raw_mb"] = round(raw_size_mb, 2)
    result["audio_uploaded_mb"] = round(transcoded_size_mb, 2)
    result["provider"] = "openai"
    result["model"] = WHISPER_MODEL

    if not keep_audio:
        try:
            transcoded_path.unlink()
        except OSError as e:
            logger.warning(
                "whisper: failed to clean up transcoded audio %s: %s",
                transcoded_path, e,
            )

    logger.info(
        "whisper: transcribed %d words for %s (duration=%.1fs)",
        len(result["words"]),
        youtube_url,
        result["duration_seconds"],
    )
    return result


# ── AssemblyAI pre-recorded transcription ─────────────────────────


def _assemblyai_json(response: requests.Response, operation: str) -> dict:
    """Return a JSON object for an AssemblyAI response or fail closed."""
    if response.status_code < 200 or response.status_code >= 300:
        raise AssemblyAIError(
            f"AssemblyAI {operation} returned HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise AssemblyAIError(
            f"AssemblyAI {operation} returned non-JSON: {response.text[:500]}"
        ) from exc
    if not isinstance(payload, dict):
        raise AssemblyAIError(
            f"AssemblyAI {operation} returned a non-object JSON payload"
        )
    return payload


def _resolve_assemblyai_model(native: dict) -> str:
    """Resolve best-effort model provenance without rejecting valid speech.

    AssemblyAI may omit singular ``speech_model`` when the request supplied
    plural ``speech_models``.  Prefer the explicit selected model, then the
    first requested speech model, then the acoustic model.  ``"unknown"`` is
    an honest final value when the response contains no usable model label.
    """
    speech_model = native.get("speech_model")
    if isinstance(speech_model, str) and speech_model.strip():
        return speech_model.strip()

    speech_models = native.get("speech_models")
    if isinstance(speech_models, list):
        for candidate in speech_models:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

    acoustic_model = native.get("acoustic_model")
    if isinstance(acoustic_model, str) and acoustic_model.strip():
        return acoustic_model.strip()

    return "unknown"


def _adapt_assemblyai_transcript(native: dict) -> dict:
    """Convert one completed AssemblyAI transcript to Z-SPAN's canonical shape."""
    if not isinstance(native, dict):
        raise AssemblyAIError("AssemblyAI transcript payload is not an object")

    status = native.get("status")
    if status == "error":
        raise AssemblyAIError(
            f"AssemblyAI transcription failed: {native.get('error') or 'unknown error'}"
        )
    if status != "completed":
        raise AssemblyAIError(
            f"AssemblyAI transcript has non-terminal status: {status!r}"
        )

    native_words = native.get("words")
    if not isinstance(native_words, list) or not native_words:
        raise AssemblyAIError("AssemblyAI completed transcript contained no words")

    words: list[dict] = []
    for index, native_word in enumerate(native_words):
        if not isinstance(native_word, dict):
            raise AssemblyAIError(f"AssemblyAI word {index} is not an object")
        text = native_word.get("text")
        start = native_word.get("start")
        end = native_word.get("end")
        speaker = native_word.get("speaker") or ""
        if not isinstance(text, str) or not text:
            raise AssemblyAIError(f"AssemblyAI word {index} has invalid text")
        if (
            not isinstance(start, (int, float)) or isinstance(start, bool)
            or not isinstance(end, (int, float)) or isinstance(end, bool)
        ):
            raise AssemblyAIError(
                f"AssemblyAI word {index} has invalid millisecond timestamps"
            )
        if not isinstance(speaker, str):
            raise AssemblyAIError(f"AssemblyAI word {index} has invalid speaker label")
        words.append({
            "word": text,
            "start": start / 1000,
            "end": end / 1000,
            "speaker_id": speaker,
        })

    model = _resolve_assemblyai_model(native)

    audio_duration = native.get("audio_duration")
    if isinstance(audio_duration, bool) or not isinstance(audio_duration, (int, float)):
        audio_duration = words[-1]["end"]

    language = native.get("language_code")
    if not isinstance(language, str):
        language = ""

    return {
        "words": words,
        "duration_seconds": float(audio_duration),
        "language": language,
        "provider": "assemblyai",
        "model": model,
    }


def _upload_audio_to_assemblyai(audio_path: Path, api_key: str) -> str:
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/octet-stream",
    }
    try:
        with audio_path.open("rb") as audio_file:
            response = requests.post(
                f"{ASSEMBLYAI_BASE_URL}/v2/upload",
                headers=headers,
                data=audio_file,
                timeout=WHISPER_TIMEOUT_SECONDS,
            )
    except (OSError, requests.RequestException) as exc:
        raise AssemblyAIError(f"AssemblyAI upload failed: {exc}") from exc
    payload = _assemblyai_json(response, "upload")
    upload_url = payload.get("upload_url")
    if not isinstance(upload_url, str) or not upload_url:
        raise AssemblyAIError("AssemblyAI upload response omitted upload_url")
    return upload_url


def _submit_assemblyai_transcript(audio_url: str, api_key: str) -> str:
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    body = {
        "audio_url": audio_url,
        "speech_models": ASSEMBLYAI_SPEECH_MODELS,
        "speaker_labels": True,
    }
    try:
        response = requests.post(
            f"{ASSEMBLYAI_BASE_URL}/v2/transcript",
            headers=headers,
            json=body,
            timeout=WHISPER_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise AssemblyAIError(f"AssemblyAI transcript submission failed: {exc}") from exc
    payload = _assemblyai_json(response, "transcript submission")
    transcript_id = payload.get("id")
    if not isinstance(transcript_id, str) or not transcript_id:
        raise AssemblyAIError("AssemblyAI transcript submission omitted id")
    return transcript_id


def _poll_assemblyai_transcript(transcript_id: str, api_key: str) -> dict:
    headers = {"Authorization": api_key}
    url = f"{ASSEMBLYAI_BASE_URL}/v2/transcript/{transcript_id}"
    deadline = time.monotonic() + ASSEMBLYAI_POLL_TIMEOUT_SECONDS
    while True:
        try:
            response = requests.get(
                url, headers=headers, timeout=WHISPER_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise AssemblyAIError(f"AssemblyAI transcript polling failed: {exc}") from exc
        payload = _assemblyai_json(response, "transcript polling")
        status = payload.get("status")
        if status in ("completed", "error"):
            return payload
        if status not in ("queued", "processing"):
            raise AssemblyAIError(f"AssemblyAI returned unknown status: {status!r}")
        if time.monotonic() >= deadline:
            raise AssemblyAIError(
                f"AssemblyAI transcript {transcript_id} did not complete before timeout"
            )
        time.sleep(ASSEMBLYAI_POLL_INTERVAL_SECONDS)


def transcribe_youtube_via_assemblyai(
    youtube_url: str,
    work_dir: Path,
    keep_audio: bool = False,
) -> dict:
    """Download with yt-dlp, upload once, and adapt AssemblyAI's result."""
    api_key = _resolve_assemblyai_key()
    if not api_key:
        raise WhisperConfigError(
            "AssemblyAI is selected but ASSEMBLYAI_API_KEY / "
            "assemblyai_api_key is not configured"
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    downloaded = download_youtube_audio(youtube_url, work_dir / "audio")
    size_mb = downloaded.path.stat().st_size / (1024 * 1024)

    upload_url = _upload_audio_to_assemblyai(downloaded.path, api_key)
    transcript_id = _submit_assemblyai_transcript(upload_url, api_key)
    native = _poll_assemblyai_transcript(transcript_id, api_key)
    result = _adapt_assemblyai_transcript(native)
    result.update({
        "source_url": youtube_url,
        "source_title": downloaded.title,
        "audio_raw_mb": round(size_mb, 2),
        "audio_uploaded_mb": round(size_mb, 2),
    })

    if not keep_audio:
        try:
            downloaded.path.unlink()
        except OSError as exc:
            logger.warning(
                "assemblyai: failed to clean up downloaded audio %s: %s",
                downloaded.path, exc,
            )

    logger.info(
        "assemblyai: transcribed %d words for %s (model=%s)",
        len(result["words"]), youtube_url, result["model"],
    )
    return result


# ── Mac transcription node — local dispatch (2026-05-31) ──────────
#
# Per `02_Core_Project/mac_transcriber/SETUP.md`, James's dedicated 2015
# MacBook runs a local FastAPI Whisper service (faster-whisper +
# distil-large-v3 INT8). When `zspan_whisper_provider == "mac_node"` in
# user_settings.json, we route YouTube transcription requests there
# instead of the paid OpenAI API. $0 marginal cost; identity-isolated
# infrastructure per [S-008](FUTURE_THOUGHTS.md#s-008) pillar 3.
#
# The dispatcher reads `02_Core_Project/mac_transcriber/STATUS.json`
# (committed to the repo by Mac-side Claude during bootstrap) for the
# connection URL, and `user_settings.json:zspan_whisper_node_token` for
# the bearer token (operator-copied from the Mac's launchd plist
# EnvironmentVariables — never committed to git).
#
# Default behavior is unchanged: `zspan_whisper_provider` defaults to
# "openai", so existing callers behave exactly as before until James
# flips the setting. Errors from the Mac path do NOT silently fall
# through to OpenAI — they raise `WhisperNodeError` so operator notices
# + can fix the Mac-side issue. Add `zspan_whisper_fallback_to_openai:
# true` to user_settings to override that strict-default if needed.


# Repo-relative path to the Mac transcriber handshake file. From this
# file (parsers/whisper_client.py), walk three parents up to reach
# 02_Core_Project, then descend into mac_transcriber/STATUS.json.
_MAC_NODE_STATUS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "mac_transcriber" / "STATUS.json"
)


def _resolve_whisper_provider() -> str:
    """Resolve the configured transcription provider, defaulting to OpenAI."""
    settings = load_user_settings()
    provider = (settings.get("zspan_whisper_provider") or "openai").strip().lower()
    if provider not in ("openai", "mac_node", "assemblyai"):
        logger.warning(
            "unknown zspan_whisper_provider=%r; defaulting to openai", provider,
        )
        return "openai"
    return provider


def _resolve_mac_node_config() -> tuple[str, str]:
    """Resolve the Mac transcription node base URL + bearer token.

    Returns:
      (base_url, bearer_token)

    Raises:
      WhisperConfigError if STATUS.json missing / up=false / token unset.

    D-099 Phase 2 C6 — when ZSPAN_WHISPER_LOCAL=true (Mac worker runtime),
    skip STATUS.json discovery and target 127.0.0.1:8765 directly. The
    Mac worker is co-located with mac_transcriber, so the LAN socket
    (the failure surface that killed WO 100791 today with
    ConnectionResetError 10054) is replaced by a localhost call that
    can't drop mid-Whisper.

    Post-D-111 substrate consolidation (2026-06-18): the env-var override
    is also exposed as a user_settings.json knob — `zspan_whisper_local`
    set to true/1/yes triggers the same loopback dispatch without needing
    a launchd plist edit. This is the canonical home for solo-Mac setups
    where the STATUS.json auto-publisher detects a VPN-tunnel egress IP
    (e.g. ProtonVPN's 10.2.0.0/16 utun range) that isn't reachable from
    the worker process on the same host.
    """
    settings = load_user_settings()
    settings_local = str(settings.get("zspan_whisper_local") or "").strip().lower()
    env_local = os.environ.get("ZSPAN_WHISPER_LOCAL", "").strip().lower()
    local_truthy = {"1", "true", "yes", "on"}
    if env_local in local_truthy or settings_local in local_truthy:
        base_url = "http://127.0.0.1:8765"
        settings = load_user_settings()
        token = (settings.get("zspan_whisper_node_token") or "").strip()
        if not token:
            raise WhisperConfigError(
                "ZSPAN_WHISPER_LOCAL=true but zspan_whisper_node_token "
                "missing from user_settings.json"
            )
        return base_url, token

    if not _MAC_NODE_STATUS_PATH.exists():
        raise WhisperConfigError(
            f"mac_transcriber STATUS.json not found at {_MAC_NODE_STATUS_PATH}; "
            "has Mac-side Claude bootstrapped the node yet?"
        )
    try:
        status = json.loads(_MAC_NODE_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise WhisperConfigError(
            f"mac_transcriber STATUS.json unreadable: {e}"
        ) from e

    if not status.get("up"):
        raise WhisperConfigError(
            "mac_transcriber STATUS.json reports up=false; check the Mac node"
        )
    base_url = (status.get("base_url") or "").rstrip("/")
    if not base_url:
        raise WhisperConfigError(
            "mac_transcriber STATUS.json missing base_url"
        )

    settings = load_user_settings()
    token = (settings.get("zspan_whisper_node_token") or "").strip()
    if not token:
        raise WhisperConfigError(
            "zspan_whisper_node_token not set in user_settings.json — copy "
            "from Mac's launchd plist EnvironmentVariables (ZSPAN_WHISPER_NODE_TOKEN)"
        )
    return base_url, token


# Timeout for the Mac-node /transcribe POST. Per-model expectations on a
# 2015 Intel quad-core (no GPU, no Apple Silicon) per FUTURE_THOUGHTS § S-019:
#
#   distil-large-v3 INT8 (current Mac default) — ~0.4x realtime, ~3 hrs / 76-min meeting
#   large-v3 INT8        (quality WIN upgrade)  — ~0.15x realtime, ~8 hrs / 76-min meeting
#   medium.en INT8                              — ~0.5x realtime, ~2.5 hrs
#   small.en INT8                               — ~1.0x realtime, ~76 min
#
# Default 10 hours gives headroom for the worst-case practical config
# (large-v3 on a 90-min meeting ~= 9.5 hrs). Operator can tighten or
# loosen via ZSPAN_WHISPER_NODE_TIMEOUT_SECONDS env var. Set at module-
# import time, so a Flask restart picks up changes; per-call override
# also available via transcribe_youtube_via_mac_node(..., timeout_seconds=).
#
# Brainstorm item 8 (2026-05-31): raised from 4hr -> 10hr default after
# noting large-v3 would 504 mid-transcript at the previous ceiling.
WHISPER_NODE_TIMEOUT_SECONDS = int(
    os.environ.get("ZSPAN_WHISPER_NODE_TIMEOUT_SECONDS", str(10 * 60 * 60))
)


def transcribe_youtube_via_mac_node(
    youtube_url: str,
    *,
    prompt: Optional[str] = None,
    timeout_seconds: int = WHISPER_NODE_TIMEOUT_SECONDS,
) -> dict:
    """POST a YouTube URL to the local Mac transcription node; return the
    Z-SPAN-shape transcript dict.

    The Mac side handles its own yt-dlp download + audio extraction +
    whisper inference; we just hand it the URL and wait for the JSON
    response. No local audio files involved on the PC side.

    Returns a dict matching `_transcribe_youtube_via_openai`'s shape:
      {
        "words":              [{"word": "...", "start": 0.0, "end": 0.5}, ...],
        "duration_seconds":   float,
        "language":           "en",
        "source_url":         <the input URL>,
        "source_title":       None (Mac doesn't extract it currently),
        "audio_raw_mb":       0.0 (no local audio on the PC side),
        "audio_uploaded_mb":  0.0,
      }
    """
    base_url, token = _resolve_mac_node_config()
    url = f"{base_url}/transcribe"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body: dict = {"youtube_url": youtube_url}
    if prompt:
        body["prompt"] = prompt

    logger.info(
        "mac node: POST %s for %s (prompt=%d chars, timeout=%ds)",
        url, youtube_url, len(prompt or ""), timeout_seconds,
    )
    try:
        resp = requests.post(
            url, json=body, headers=headers, timeout=timeout_seconds,
        )
    except requests.RequestException as e:
        raise WhisperNodeError(
            f"mac node POST failed (connection / timeout): {e}"
        ) from e

    if resp.status_code != 200:
        raise WhisperNodeError(
            f"mac node returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    try:
        result = resp.json()
    except ValueError as e:
        raise WhisperNodeError(
            f"mac node returned non-JSON: {resp.text[:500]}"
        ) from e

    # Pass through Mac's response + add Z-SPAN's expected metadata fields.
    # source_title / audio_*_mb are unknown on the Mac side; downstream
    # code that reads them gracefully degrades on None / 0.
    result["source_url"] = youtube_url
    result.setdefault("source_title", None)
    result.setdefault("audio_raw_mb", 0.0)
    result.setdefault("audio_uploaded_mb", 0.0)
    result.setdefault("provider", "mac_node")
    result.setdefault("model", "unknown")

    logger.info(
        "mac node: transcribed %d words for %s (duration=%.1fs, lang=%s)",
        len(result.get("words", [])),
        youtube_url,
        result.get("duration_seconds", 0.0),
        result.get("language", "?"),
    )
    return result


def transcribe_youtube(
    youtube_url: str,
    work_dir: Path,
    keep_audio: bool = False,
    *,
    prompt: Optional[str] = None,
) -> dict:
    """Top-level dispatcher — routes to the configured transcription provider based on
    `zspan_whisper_provider` in user_settings.json (default "openai").

    ⚠️ NAME IS LEGACY — handles ANY yt-dlp-ingestible URL, not just
    YouTube. Per S-037 V0, the dispatcher transparently handles Granicus
    direct-MP4 (`archive-video.granicus.com/...mp4`), Legistar streams,
    and other non-YouTube council-video surfaces; the `youtube_url`
    parameter name is preserved purely for drop-in compatibility with
    pre-S-037 callers. New callers should treat the URL as generic.
    Rename pass (transcribe_youtube → transcribe_via_yt_dlp or similar)
    is parked as the carry-forward from the 2026-06-20 evening brainstorm
    + the 2026-06-21 audit; deferred because the rename touches multiple
    call sites + isn't load-bearing for V1.

    Same signature as the original `transcribe_youtube` for drop-in
    compatibility with existing callers (zspan_pipeline/fetcher.py).
    The `work_dir` and `keep_audio` parameters are only meaningful for
    the OpenAI path (which writes intermediate audio locally); the
    mac_node path ignores them (Mac handles its own audio).

    Raises:
      WhisperNodeError on mac_node failures (does NOT silently fall
      through to OpenAI unless user_settings sets
      `zspan_whisper_fallback_to_openai: true`).
      WhisperError subclasses from the OpenAI path on its failures.
    """
    provider = _resolve_whisper_provider()
    if provider == "assemblyai":
        logger.info(
            "whisper provider: assemblyai (per zspan_whisper_provider setting)"
        )
        return transcribe_youtube_via_assemblyai(
            youtube_url, work_dir, keep_audio,
        )
    if provider == "mac_node":
        logger.info(
            "whisper provider: mac_node (per zspan_whisper_provider setting)"
        )
        try:
            return transcribe_youtube_via_mac_node(youtube_url, prompt=prompt)
        except (WhisperNodeError, WhisperConfigError) as e:
            settings = load_user_settings()
            if settings.get("zspan_whisper_fallback_to_openai"):
                logger.warning(
                    "mac_node failed (%s); falling through to OpenAI per "
                    "zspan_whisper_fallback_to_openai=true",
                    e,
                )
                return _transcribe_youtube_via_openai(
                    youtube_url, work_dir, keep_audio, prompt=prompt
                )
            raise

    logger.info("whisper provider: openai (default)")
    return _transcribe_youtube_via_openai(
        youtube_url, work_dir, keep_audio, prompt=prompt
    )


if __name__ == "__main__":
    # Quick smoke test:
    #   python3.11 whisper_client.py <youtube_url>
    # Writes audio to ./whisper_smoke/ and prints the first 20 words.
    # Provider is auto-selected from user_settings.zspan_whisper_provider.
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("usage: python whisper_client.py <youtube_url>")
        sys.exit(2)

    url = sys.argv[1]
    out = Path("whisper_smoke")
    try:
        result = transcribe_youtube(url, out, keep_audio=True)
    except WhisperError as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"language={result['language']} duration={result['duration_seconds']:.1f}s")
    print(f"first 20 words:")
    for w in result["words"][:20]:
        print(f"  {w['start']:.2f}-{w['end']:.2f}  {w['word']}")
    print(f"total words: {len(result['words'])}")

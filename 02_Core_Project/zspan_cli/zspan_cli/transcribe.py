"""Transcription for `zspan process` — local by default, free.

The floor is a local faster-whisper CPU model — no key, no cost, roughly
real-time on an ordinary laptop. The user's OpenAI key can OPTIONALLY speed it up via the
whisper-1 cloud API (--cloud-transcribe); it is a speed flag, never a
requirement.

Output shape mirrors the flagship's transcript_words payload exactly
(whisper_client.py / fetcher.py:_fetch_transcript_words):

    {"words": [{"word": str, "start": float, "end": float}, ...],
     "duration_seconds": float, "language": str, "source_url": str}

plus CLI-side provenance extras (transcriber, model) that downstream
consumers ignore. Word tokens are stripped and empties dropped, matching
the flagship's normalization.

The cloud path ports the flagship's chunked upload (whisper_client.py
transcribe_audio_chunked): ALWAYS split into ~300s copy-codec segments
via system ffmpeg and merge per-chunk words at offset = idx × chunk
seconds. ffmpeg is required only on this opt-in path — the local path
decodes through faster-whisper's bundled PyAV.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

import requests

DEFAULT_LOCAL_MODEL = "small.en"
CLOUD_CHUNK_SECONDS = 300
_CLOUD_TIMEOUT_SECONDS = 300


class TranscribeError(Exception):
    """Transcription failed in a way the user should read."""


# ---------------------------------------------------------------- local


def transcribe_local(
    media_path: Path,
    *,
    model_size: str = DEFAULT_LOCAL_MODEL,
    progress: Callable[[str], None] = print,
    on_segment: Optional[Callable[[str, float, float], None]] = None,
) -> dict:
    """The free local floor: faster-whisper on CPU, int8, word timestamps.

    The segments generator IS the transcription — words accumulate as the
    model works through the audio, with a progress line every ~10 minutes
    of audio so a long meeting never looks hung.

    `on_segment(text, start_s, end_s)` fires per decoded segment when
    provided — the HQ activity feed's per-segment stars; a callback
    error must never kill a transcription, so it's swallowed.
    """
    if not media_path.exists():
        raise TranscribeError(f"media file not found: {media_path}")

    try:
        from faster_whisper import WhisperModel  # lazy heavy import
    except ImportError as e:
        raise TranscribeError(
            "faster-whisper isn't installed — `pip install -r requirements.txt` "
            "inside the zspan_cli folder adds it."
        ) from e

    progress(f"  loading local Whisper model '{model_size}' (first run downloads it)...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    segments, info = model.transcribe(
        str(media_path),
        word_timestamps=True,
        vad_filter=True,  # skip long silences — council audio has plenty
    )

    words: list[dict] = []
    audio_minutes = (info.duration or 0.0) / 60.0
    progress(
        f"  transcribing {audio_minutes:.0f} minutes of audio locally "
        f"(free; an OpenAI key + --cloud-transcribe trades money for speed)..."
    )
    next_mark = 600.0
    last_end = 0.0
    for seg in segments:
        for w in seg.words or []:
            token = (w.word or "").strip()
            if not token:
                continue
            words.append({
                "word": token,
                "start": float(w.start or 0.0),
                "end": float(w.end or 0.0),
            })
        seg_start = float(seg.start or last_end)
        last_end = float(seg.end or last_end)
        if on_segment is not None:
            try:
                on_segment((seg.text or "").strip(), seg_start, last_end)
            except Exception:
                pass  # a watcher must never break the transcription
        if last_end >= next_mark:
            progress(f"  ... {int(last_end // 60)} minutes transcribed")
            next_mark += 600.0

    if not words:
        raise TranscribeError(
            "transcription produced zero words — the media may be silent, "
            "corrupt, or not actually contain the meeting audio."
        )
    return {
        "words": words,
        "duration_seconds": float(info.duration or last_end),
        "language": info.language or "en",
        "transcriber": "faster-whisper-local",
        "model": model_size,
    }


# ---------------------------------------------------------------- cloud


def transcribe_whisper1(
    media_path: Path,
    api_key: str,
    *,
    chunk_seconds: int = CLOUD_CHUNK_SECONDS,
    progress: Callable[[str], None] = print,
    on_segment: Optional[Callable[[str, float, float], None]] = None,
) -> dict:
    """The opt-in speed path: OpenAI whisper-1 (~$0.36/meeting-hour on
    the user's own key). Splits with system ffmpeg (required here only),
    uploads each segment, merges words at fixed offsets — the flagship's
    transcribe_audio_chunked pattern, ported lean over plain requests.
    """
    if not media_path.exists():
        raise TranscribeError(f"media file not found: {media_path}")
    if not _ffmpeg_available():
        raise TranscribeError(
            "--cloud-transcribe needs ffmpeg on PATH to split the audio for "
            "upload (the free local mode needs no ffmpeg — just drop the flag)."
        )

    chunk_dir = media_path.parent / f"{media_path.stem}_chunks"
    chunks = _split_audio(media_path, chunk_dir, chunk_seconds)
    progress(f"  split into {len(chunks)} × ~{chunk_seconds}s segments for upload")

    chunk_results: list[dict] = []
    try:
        for idx, chunk_path in enumerate(chunks):
            progress(f"  uploading segment {idx + 1}/{len(chunks)} to whisper-1...")
            result = _whisper1_one_file(chunk_path, api_key)
            chunk_results.append(result)
            if on_segment is not None:
                # One event per returned upload — the cloud path's version
                # of the per-segment stars. Text head only; the window is
                # the chunk's fixed offset slice.
                try:
                    head = " ".join(
                        (w.get("word") or "").strip()
                        for w in (result.get("words") or [])[:30]
                    ).strip()
                    offset = idx * float(chunk_seconds)
                    on_segment(
                        head, offset,
                        offset + float(result.get("duration_seconds") or 0.0),
                    )
                except Exception:
                    pass  # a watcher must never break the transcription
    finally:
        for c in chunks:
            c.unlink(missing_ok=True)
        try:
            chunk_dir.rmdir()
        except OSError:
            pass

    merged = merge_chunk_words(chunk_results, chunk_seconds)
    if not merged["words"]:
        raise TranscribeError("whisper-1 returned zero words across all segments.")
    merged["transcriber"] = "openai-whisper-1"
    merged["model"] = "whisper-1"
    return merged


def merge_chunk_words(chunk_results: list[dict], chunk_seconds: int) -> dict:
    """Merge per-chunk transcripts at offset = idx × chunk_seconds — the
    flagship's copy-codec-segments assumption (whisper_client.py:566).
    Pure function so the merge logic unit-tests without any network."""
    words: list[dict] = []
    total_duration = 0.0
    language = "en"
    for idx, result in enumerate(chunk_results):
        offset = idx * float(chunk_seconds)
        language = result.get("language") or language
        for w in result.get("words", []):
            token = (w.get("word") or "").strip()
            if not token:
                continue
            words.append({
                "word": token,
                "start": float(w.get("start", 0.0)) + offset,
                "end": float(w.get("end", 0.0)) + offset,
            })
        total_duration += float(result.get("duration_seconds") or 0.0)
    return {"words": words, "duration_seconds": total_duration, "language": language}


def _whisper1_one_file(audio_path: Path, api_key: str) -> dict:
    """One ≤25MB segment through the whisper-1 HTTP API — no SDK, matching
    the CLI's direct-to-provider posture."""
    try:
        with audio_path.open("rb") as fh:
            resp = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_path.name, fh, "application/octet-stream")},
                data={
                    "model": "whisper-1",
                    "response_format": "verbose_json",
                    "timestamp_granularities[]": "word",
                },
                timeout=_CLOUD_TIMEOUT_SECONDS,
                allow_redirects=False,  # RR-8: never resend the key on a redirect
            )
    except requests.exceptions.RequestException as e:
        # Report the exception TYPE only — never echo a body that could
        # carry credential material (the validate.py discipline).
        raise TranscribeError(f"whisper-1 upload failed: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise TranscribeError(
            f"whisper-1 answered HTTP {resp.status_code}: {_provider_error(resp)}"
        )
    data = resp.json()
    return {
        "words": data.get("words") or [],
        "duration_seconds": float(data.get("duration") or 0.0),
        "language": data.get("language") or "en",
    }


def _provider_error(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        return str(err.get("message") or "(no message)")[:300]
    except ValueError:
        return "(non-JSON body)"


def _ffmpeg_available() -> bool:
    import shutil
    return shutil.which("ffmpeg") is not None


def _split_audio(audio_path: Path, chunk_dir: Path, chunk_seconds: int) -> list[Path]:
    """Copy-codec segment split — no re-encode, boundaries land close
    enough to chunk_seconds for the fixed-offset merge (flagship-proven)."""
    chunk_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunk_dir / f"chunk%04d{audio_path.suffix}"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(audio_path),
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-c", "copy",
        str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TranscribeError(f"ffmpeg split failed: {result.stderr.strip()[:300]}")
    chunks = sorted(chunk_dir.glob(f"chunk*{audio_path.suffix}"))
    if not chunks:
        raise TranscribeError("ffmpeg split produced no segments.")
    return chunks


def save_transcript(transcript: dict, dest: Path) -> Path:
    """Persist the transcript JSON (the workspace's transcript_path
    artifact). Caller owns where; format is the flagship shape."""
    import json
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
    return dest


def load_transcript(path: Path) -> Optional[dict]:
    """Read a previously-saved transcript; None when absent, loud when
    corrupt (the config.py F8 pattern)."""
    import json
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TranscribeError(
            f"transcript file at {path} exists but isn't readable JSON ({e}). "
            "Delete it to re-transcribe."
        ) from e
    if not isinstance(data, dict) or not isinstance(data.get("words"), list):
        raise TranscribeError(
            f"transcript file at {path} doesn't carry a words list. "
            "Delete it to re-transcribe."
        )
    return data

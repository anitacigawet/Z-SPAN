"""Unit tests for the voice-memo detection + transcription helpers.

Covers the pure functions:
  - slack_listener._is_voice_memo_file
  - slack_listener._select_voice_memo_file
  - slack_listener._REJECT_SUBTYPES (the subtype filter the live handler
    uses; test surface lives at the constant)
  - slack_file_downloader._suffix_from_file

The integration of _process_im_message itself is exercised manually
when James DMs a voice memo (live e2e — same posture as the chunk-1c
status_tailer per TASKS.md).

Run via:
    cd 02_Core_Project/council_navigator/parsers
    python3.11 -m pytest tests/test_slack_voice_memo.py -v

Per Stage B piece 2 chunk 7 (TASKS.md, 2026-05-31).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the parsers dir importable for `slack_listener` + `slack_file_downloader`.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

import pytest

from slack_listener import (
    _REJECT_SUBTYPES,
    _is_voice_memo_file,
    _select_voice_memo_file,
)
from slack_file_downloader import _suffix_from_file


# ===== _is_voice_memo_file ================================================
# The 5-branch detection rule — each branch gets a positive test, plus the
# negative path (an image, a PDF, a generic file).


def test_voice_memo_ios_mp4():
    """The canonical iOS voice-memo shape — audio_message.mp4 + audio/mp4."""
    f = {
        "name": "audio_message.mp4",
        "mimetype": "audio/mp4",
        "url_private_download": "https://files.slack.com/...",
    }
    assert _is_voice_memo_file(f) is True


def test_voice_memo_media_display_type():
    """The undocumented media_display_type is the most deliberate signal."""
    f = {
        "name": "anything.bin",
        "mimetype": "application/octet-stream",
        "media_display_type": "audio_message",
    }
    assert _is_voice_memo_file(f) is True


def test_voice_memo_slack_clip_file_subtype():
    """Slack Clips set the FILE subtype to slack_audio."""
    f = {
        "name": "clip-12345.webm",
        "mimetype": "audio/webm",
        "subtype": "slack_audio",
    }
    assert _is_voice_memo_file(f) is True


def test_voice_memo_video_mp4_misclassification():
    """openclaw#4008: Slack ships voice memos with mimetype=video/mp4
    when the underlying container is MP4. The audio_message filename
    pattern catches these."""
    f = {
        "name": "audio_message1740123456789.mp4",
        "mimetype": "video/mp4",
    }
    assert _is_voice_memo_file(f) is True


def test_voice_memo_arbitrary_audio_upload():
    """Generic audio/* mimetype — a user dragging a .mp3 in. Treated
    the same as a voice memo; James can decide downstream if the intent
    differs."""
    f = {
        "name": "podcast-clip.mp3",
        "mimetype": "audio/mpeg",
    }
    assert _is_voice_memo_file(f) is True


def test_voice_memo_audio_wav():
    f = {"name": "recording.wav", "mimetype": "audio/wav"}
    assert _is_voice_memo_file(f) is True


def test_not_voice_memo_image():
    f = {
        "name": "screenshot.png",
        "mimetype": "image/png",
        "media_display_type": "unknown",
    }
    assert _is_voice_memo_file(f) is False


def test_not_voice_memo_pdf():
    f = {"name": "agenda-packet.pdf", "mimetype": "application/pdf"}
    assert _is_voice_memo_file(f) is False


def test_not_voice_memo_video_clip():
    """A real video (not the mp4-misclassified voice memo): mimetype is
    video/mp4 AND filename does NOT start with audio_message — should
    NOT trigger the voice-memo path."""
    f = {
        "name": "meeting-recording.mp4",
        "mimetype": "video/mp4",
        "media_display_type": "video_message",
    }
    assert _is_voice_memo_file(f) is False


def test_not_voice_memo_text_doc():
    f = {"name": "notes.txt", "mimetype": "text/plain"}
    assert _is_voice_memo_file(f) is False


def test_not_voice_memo_empty_payload():
    """Defensive: empty dict should not crash + should return False."""
    assert _is_voice_memo_file({}) is False


def test_not_voice_memo_none_mimetype():
    """Defensive: mimetype=None should be treated as absent, not crash."""
    f = {"name": "x.bin", "mimetype": None, "media_display_type": None}
    assert _is_voice_memo_file(f) is False


def test_voice_memo_case_insensitive_name():
    """Filename matching is lowercased internally so case from Slack
    doesn't bypass the check."""
    f = {"name": "AUDIO_MESSAGE.MP4", "mimetype": "audio/mp4"}
    assert _is_voice_memo_file(f) is True


# ===== _select_voice_memo_file ============================================


def test_select_first_voice_memo_in_list():
    """A multi-file message (image + voice memo) returns the voice memo."""
    files = [
        {"name": "screenshot.png", "mimetype": "image/png"},
        {"name": "audio_message.mp4", "mimetype": "audio/mp4"},
    ]
    result = _select_voice_memo_file(files)
    assert result is not None
    assert result["name"] == "audio_message.mp4"


def test_select_voice_memo_first_audio_when_multiple():
    """Multiple voice memos: return the first (rare but possible)."""
    files = [
        {"name": "audio_message1.mp4", "mimetype": "audio/mp4"},
        {"name": "audio_message2.mp4", "mimetype": "audio/mp4"},
    ]
    result = _select_voice_memo_file(files)
    assert result is not None
    assert result["name"] == "audio_message1.mp4"


def test_select_returns_none_no_audio():
    """A file-share with only an image returns None — the listener
    drops the message (no orchestrator spawn for image-only DMs)."""
    files = [
        {"name": "screenshot.png", "mimetype": "image/png"},
    ]
    assert _select_voice_memo_file(files) is None


def test_select_returns_none_empty_list():
    assert _select_voice_memo_file([]) is None


def test_select_returns_none_none_input():
    """Defensive: None instead of a list should not crash."""
    assert _select_voice_memo_file(None) is None


def test_select_skips_non_dict_entries():
    """Defensive: malformed entries shouldn't crash the iteration."""
    files = [
        "this is not a dict",
        None,
        {"name": "audio_message.mp4", "mimetype": "audio/mp4"},
    ]
    result = _select_voice_memo_file(files)
    assert result is not None
    assert result["name"] == "audio_message.mp4"


# ===== _REJECT_SUBTYPES ===================================================


def test_reject_subtypes_includes_message_changed():
    """Critical: edited voice memos arrive as message_changed and must
    stay dropped (re-transcribing on edit would double-spawn)."""
    assert "message_changed" in _REJECT_SUBTYPES


def test_reject_subtypes_includes_message_deleted():
    assert "message_deleted" in _REJECT_SUBTYPES


def test_reject_subtypes_includes_channel_events():
    """Channel-noise events shouldn't spawn the orchestrator."""
    for noise in ("channel_join", "channel_leave", "channel_topic",
                  "channel_purpose", "channel_name"):
        assert noise in _REJECT_SUBTYPES


def test_reject_subtypes_excludes_file_share():
    """The CORE chunk-7 invariant: file_share must NOT be rejected at
    the subtype filter, so voice memos can fall through to the
    voice-memo branch below the owner check. If this fails, voice
    memos silently disappear and chunk 7 is broken."""
    assert "file_share" not in _REJECT_SUBTYPES


def test_reject_subtypes_excludes_slack_audio():
    """Older Slack voice-memo subtype — must also fall through."""
    assert "slack_audio" not in _REJECT_SUBTYPES


def test_reject_subtypes_excludes_none():
    """Plain DMs with no subtype must NOT be rejected (they're text DMs
    going to the existing path)."""
    assert None not in _REJECT_SUBTYPES


# ===== _suffix_from_file (slack_file_downloader) ==========================


def test_suffix_from_audio_message_mp4():
    """The canonical iOS voice-memo filename → .mp4."""
    assert _suffix_from_file({"name": "audio_message.mp4"}) == ".mp4"


def test_suffix_from_audio_message_with_digits():
    """audio_message<timestamp>.mp4 — extension still extracted."""
    assert _suffix_from_file({"name": "audio_message1740123456789.mp4"}) == ".mp4"


def test_suffix_from_webm():
    assert _suffix_from_file({"name": "clip.webm"}) == ".webm"


def test_suffix_from_uppercase_extension():
    """Extensions get lowercased."""
    assert _suffix_from_file({"name": "RECORDING.MP3"}) == ".mp3"


def test_suffix_fallback_no_extension():
    """No extension → .bin fallback (safer than .tmp; Whisper sniffs
    by content for bin and rejects, which gives a clear error)."""
    assert _suffix_from_file({"name": "audio_message"}) == ".bin"


def test_suffix_fallback_empty_name():
    assert _suffix_from_file({"name": ""}) == ".bin"


def test_suffix_fallback_missing_name():
    assert _suffix_from_file({}) == ".bin"


def test_suffix_rejects_long_extension():
    """A long pseudo-extension (e.g. a path that just has dots) falls
    back to .bin rather than confusing Whisper with a 12-char .extension."""
    name = "audio_message.very_long_pseudo_extension"
    assert _suffix_from_file({"name": name}) == ".bin"


def test_suffix_rejects_non_alphanum_extension():
    """Defensive against pathological filenames with symbol-extensions."""
    assert _suffix_from_file({"name": "audio.weird!"}) == ".bin"

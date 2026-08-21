"""slack_file_downloader — auth'd download of Slack-hosted file payloads.

Used by `slack_listener._process_im_message` to fetch voice-memo audio
(and any other future file types — image OCR, PDF agenda packets) from
a `message.im` event's `files[]` entry before handing off to the
transcription / parsing layer.

Slack's file URLs (`url_private` + `url_private_download`) require the
bot token as `Authorization: Bearer xoxb-...`. This module is thin:
resolve the bot token via slack_notifier, GET the URL with streaming,
write to a NamedTemporaryFile, return the Path. Caller is responsible
for unlinking the returned Path when done (Windows-safe pattern —
delete=False so Whisper's second handle to the file can open it).

Required Slack scope: `files:read`. If missing, Slack returns an HTML
viewer page (not the binary) — detected via Content-Type and raised
as `SlackFileDownloadError` so the caller can ack a clear message
instead of failing silently downstream.

Per the Stage B piece 2 chunk 7 voice-memo flow (TASKS.md, 2026-05-31).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


_DOWNLOAD_TIMEOUT_SECONDS = 30
# 50 MB ceiling — voice memos are typically <1 MB; this protects against
# a runaway download if Slack ever serves something unexpected.
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


class SlackFileDownloadError(Exception):
    """Raised on any failure downloading a Slack-hosted file."""


def _suffix_from_file(file_payload: dict) -> str:
    """Pick a sensible file extension from the Slack file payload.

    Prefers the actual filename extension (e.g., `audio_message.mp4` → `.mp4`)
    because Whisper's content-type detection sniffs by extension; a
    mismatched extension can produce a 400 even when the bytes are valid.
    Falls back to `.bin` if the name has no extension.
    """
    name = (file_payload.get("name") or "").strip()
    if "." in name:
        ext = "." + name.rsplit(".", 1)[1].lower()
        # Constrain to short, sane extensions only.
        if 2 <= len(ext) <= 6 and ext[1:].isalnum():
            return ext
    return ".bin"


def download_slack_file(
    file_payload: dict,
    *,
    bot_token: Optional[str] = None,
) -> Path:
    """Download a Slack-hosted file to a tempfile; return the Path.

    Args:
        file_payload: a single entry from the message event's `files[]`
            array. Must have `url_private_download` (preferred) or
            `url_private`.
        bot_token: the xoxb-... token. If None, resolves via
            `slack_notifier._resolve_bot_token`.

    Returns the Path to the downloaded tempfile. **Caller MUST unlink
    it** (typically in a `try/finally`).

    Raises:
        SlackFileDownloadError on missing URL, missing token, non-200,
        oversized response, or HTML-viewer-page response (which usually
        means the bot lacks the `files:read` scope).
    """
    url = file_payload.get("url_private_download") or file_payload.get("url_private")
    if not url:
        raise SlackFileDownloadError(
            "Slack file payload has no url_private_download or url_private"
        )

    if bot_token is None:
        from slack_notifier import _resolve_bot_token
        bot_token = _resolve_bot_token()
    if not bot_token:
        raise SlackFileDownloadError(
            "No slack_bot_token configured; cannot auth file download"
        )

    # SSRF / credential-exfil guard (RR-8): the bot bearer token is about to be
    # attached to `url`, which arrives from the Slack event's files[] payload.
    # Pin the host to Slack so the token can never be sent to a non-Slack
    # (attacker-controlled) host, and refuse redirects so it can't be bounced
    # off-host mid-flight. Authorized Slack file downloads return 200 directly;
    # a 3xx therefore fails loudly via the status check below.
    _parsed = urlparse(url)
    _host = (_parsed.hostname or "").lower()
    # Enforce https (no plaintext bearer) AND reject any backslash: urlparse
    # and urllib3 disagree on a URL like `https://evil.com\@files.slack.com/x`
    # (urlparse reads the host as Slack, requests routes to evil.com) — a
    # parser-differential that would exfiltrate the bearer to the real
    # destination. Slack file URLs are always a clean https://<*.slack.com>/…
    # with no backslash. Both checks run BEFORE the token is attached below.
    if (
        _parsed.scheme != "https"
        or "\\" in url
        or not (_host == "slack.com" or _host.endswith(".slack.com"))
    ):
        raise SlackFileDownloadError(
            f"refusing to attach the Slack bot token to an untrusted URL "
            f"(scheme={_parsed.scheme!r} host={_host or '(no host)'})"
        )

    headers = {"Authorization": f"Bearer {bot_token}"}
    try:
        resp = requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        raise SlackFileDownloadError(
            f"Slack file download failed (network): {e}"
        ) from e

    if resp.status_code != 200:
        raise SlackFileDownloadError(
            f"Slack file download returned HTTP {resp.status_code}: "
            f"{resp.text[:300]}"
        )

    # Slack returns an HTML viewer page when the bot lacks `files:read`.
    # The actual file response should be a binary content-type. This
    # check is the difference between "silent 200 + wrong bytes" (which
    # would crash Whisper with a confusing error) and a clear actionable
    # message that points at the Slack app manifest.
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:
        raise SlackFileDownloadError(
            "Slack returned HTML instead of the file; the bot likely "
            "lacks the `files:read` scope. Add it to the Slack app "
            "manifest and reinstall the app to the workspace."
        )

    suffix = _suffix_from_file(file_payload)
    # delete=False is required on Windows: the consumer (Whisper) opens
    # the file via a SECOND handle in transcribe_audio_file, which
    # Windows refuses while NamedTemporaryFile's own handle is alive.
    tf = tempfile.NamedTemporaryFile(
        prefix="zspan_slack_",
        suffix=suffix,
        delete=False,
    )
    path = Path(tf.name)
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                tf.close()
                try:
                    path.unlink()
                except OSError:
                    pass
                raise SlackFileDownloadError(
                    f"Slack file exceeded download cap "
                    f"({_MAX_DOWNLOAD_BYTES} bytes); aborted"
                )
            tf.write(chunk)
    finally:
        tf.close()

    logger.info(
        "slack_file_downloader: saved %s -> %s (%d bytes)",
        url, path, total,
    )
    return path

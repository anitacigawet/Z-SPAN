"""Client for the Mac diarizer node (Phase 2 D1).

Mirrors `whisper_client.py`'s mac-node dispatch shape. The Mac diarizer
service at `02_Core_Project/mac_diarizer/server.py` exposes POST /diarize
on :8767 (sibling to the whisper-node at :8765); this module is the
worker-side client.

Lookup order for the base URL:
  1. `ZSPAN_DIARIZER_LOCAL` env var truthy OR `zspan_diarizer_local` user-setting truthy
     → http://127.0.0.1:8767 (canonical post-D-111 solo-Mac setup)
  2. Otherwise read `02_Core_Project/mac_diarizer/STATUS.json` (cross-machine)

Bearer token always comes from `parsers/user_settings.json:zspan_diarizer_node_token`.

Used by the worker's diarize step (Phase 2 D7) between the whisper-node
/transcribe call and the Qdrant index step.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from env_config import load_user_settings

logger = logging.getLogger(__name__)


class DiarizeError(Exception):
    """Base class for diarization pipeline failures."""


class DiarizeConfigError(DiarizeError):
    """Misconfiguration (missing STATUS.json, token, etc.)."""


class DiarizeHTTPError(DiarizeError):
    """The Mac diarizer node returned a non-200 response."""


_MAC_NODE_STATUS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "mac_diarizer" / "STATUS.json"
)

# Generous timeout for the /diarize POST. Mac CPU diarization is roughly
# realtime-equivalent on the bundled 30-sec sample (1.13× rtf); a 76-min
# meeting could take ~85 min wall-clock. A 3.5-hour council session needs
# ~4 hours of pyannote compute, so the prior 4-hour default was structurally
# undersized — m103753's 2026-06-24 Phase 2 D8 acceptance run timed out at
# exactly 4h with pyannote still running. Bumped to 8h for headroom on the
# realistic upper bound of council-meeting durations.
DIARIZER_NODE_TIMEOUT_SECONDS = int(
    os.environ.get("ZSPAN_DIARIZER_NODE_TIMEOUT_SECONDS", str(8 * 60 * 60))
)


def is_configured() -> bool:
    """True when the diarizer node looks reachable (token set + local OR
    STATUS.json present and up=true). False otherwise — callers should
    treat diarization as optional in that case (the worker pipeline
    falls back to undiarized indexing per D7's non-fatal contract)."""
    try:
        _resolve_diarizer_config()
        return True
    except DiarizeConfigError:
        return False


def _resolve_diarizer_config() -> tuple[str, str]:
    """Resolve the Mac diarizer node base URL + bearer token.

    Returns:
      (base_url, bearer_token)

    Raises:
      DiarizeConfigError if STATUS.json missing / up=false / token unset.
    """
    settings = load_user_settings()
    settings_local = str(settings.get("zspan_diarizer_local") or "").strip().lower()
    env_local = os.environ.get("ZSPAN_DIARIZER_LOCAL", "").strip().lower()
    local_truthy = {"1", "true", "yes", "on"}

    token = (settings.get("zspan_diarizer_node_token") or "").strip()
    if not token:
        raise DiarizeConfigError(
            "zspan_diarizer_node_token not set in user_settings.json — generate one "
            "and add it (see parsers/USER_SETTINGS_KEYS.md § Mac infrastructure)"
        )

    if env_local in local_truthy or settings_local in local_truthy:
        return "http://127.0.0.1:8767", token

    if not _MAC_NODE_STATUS_PATH.exists():
        raise DiarizeConfigError(
            f"mac_diarizer STATUS.json not found at {_MAC_NODE_STATUS_PATH}"
        )
    try:
        status = json.loads(_MAC_NODE_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DiarizeConfigError(
            f"mac_diarizer STATUS.json unreadable: {e}"
        ) from e

    if not status.get("up"):
        raise DiarizeConfigError(
            "mac_diarizer STATUS.json reports up=false; check the Mac node"
        )
    base_url = (status.get("base_url") or "").rstrip("/")
    if not base_url:
        raise DiarizeConfigError("mac_diarizer STATUS.json missing base_url")

    return base_url, token


def diarize_via_mac_node(
    *,
    youtube_url: Optional[str] = None,
    audio_url: Optional[str] = None,
    include_speaker_summary: bool = True,
    include_speaker_embeddings: bool = True,
    timeout_seconds: int = DIARIZER_NODE_TIMEOUT_SECONDS,
) -> dict:
    """POST a URL to the Mac diarizer node; return the parsed turns dict.

    Returns the dict shape produced by mac_diarizer/server.py /diarize:
        {
          "turns": [{start, end, speaker_label}, ...],
          "audio_duration_seconds": float,
          "speaker_summary": {SPEAKER_NN: {total_seconds, turn_count}, ...},
          "diarization_seconds": float,
          # When include_speaker_embeddings=True AND pyannote returned them:
          "speaker_embeddings": {SPEAKER_NN: [float, ...], ...},
          "embedding_model": "pyannote/wespeaker-voxceleb-resnet34-LM",
          "embedding_dim": 256
        }

    The speaker_embeddings block is V-Op-1 substrate for the voice-library
    (per the 2026-06-26 voice-sample architecture); callers that don't
    need it can set include_speaker_embeddings=False to skip the wire
    overhead (currently ~256 floats × N_speakers; negligible vs the turns
    payload but saves a few KB on large meetings).

    Raises DiarizeConfigError if the node isn't reachable; DiarizeHTTPError
    if the node returns a non-200 response.
    """
    if not youtube_url and not audio_url:
        raise ValueError("must provide youtube_url or audio_url")
    if youtube_url and audio_url:
        raise ValueError("provide only one of youtube_url or audio_url")

    import requests  # local import; matches whisper_client pattern

    base_url, token = _resolve_diarizer_config()
    endpoint = f"{base_url}/diarize"

    payload = {
        "include_speaker_summary": include_speaker_summary,
        "include_speaker_embeddings": include_speaker_embeddings,
    }
    if youtube_url:
        payload["youtube_url"] = youtube_url
    if audio_url:
        payload["audio_url"] = audio_url

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    logger.info(
        "diarize_via_mac_node: POST %s (url=%s)",
        endpoint,
        (youtube_url or audio_url)[:80],
    )
    try:
        resp = requests.post(
            endpoint, json=payload, headers=headers, timeout=timeout_seconds,
        )
    except requests.exceptions.RequestException as e:
        raise DiarizeHTTPError(f"diarizer node unreachable: {e}") from e

    if resp.status_code != 200:
        body_preview = resp.text[:500] if resp.text else "<empty>"
        raise DiarizeHTTPError(
            f"diarizer node returned {resp.status_code}: {body_preview}"
        )

    try:
        return resp.json()
    except ValueError as e:
        raise DiarizeHTTPError(
            f"diarizer node returned non-JSON body: {resp.text[:500]}"
        ) from e


def health_check() -> dict:
    """GET the diarizer's /health endpoint. Returns the parsed JSON or
    raises. Useful for smoke tests + worker-side preflight."""
    import requests

    base_url, _ = _resolve_diarizer_config()
    endpoint = f"{base_url}/health"
    resp = requests.get(endpoint, timeout=10)
    if resp.status_code != 200:
        raise DiarizeHTTPError(
            f"diarizer /health returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()

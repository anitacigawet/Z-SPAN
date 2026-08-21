"""~/.zspan/config.json — the CLI's one config file.

Shape (version 1):

    {
      "version": 1,
      "synthesis_provider": "openai",
      "api_keys": {"openai": "sk-..."},
      "flagship_url": "https://zspan.org",
      "created_at": "2026-07-09T12:00:00Z",
      "updated_at": "2026-07-09T12:00:00Z"
    }

Custody posture: the key lives in the user's own file on their own
machine (the ~/.aws/credentials pattern). Best-effort 0600 permissions on
POSIX; Windows relies on the user profile's own ACL. Unknown keys are
preserved on rewrite so later chunks can add fields without a migration.

Failure semantics follow the project F8 discipline: an ABSENT config is a
normal state (returns None); a CORRUPT config is loud (ConfigError naming
the path) — never silently treated as absent.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_VERSION = 1
DEFAULT_FLAGSHIP_URL = "https://zspan.org"
PROCESSING_ACK_VERSION = 2
PROCESSING_ACK_TEXT = (
    "Processing runs on YOUR machine: Z-SPAN downloads this meeting's public "
    "recording, transcribes it locally (or through your own cloud key when you "
    "opt in), and creates the meeting outputs with your own AI setup. Your "
    "provider key and downloaded media stay on your computer. The official "
    "client sends the generated transcript, final outputs, and their audit "
    "metadata to Z-SPAN's private intake. Z-SPAN stores them for review, "
    "verification, and possible later inclusion in the library; nothing is "
    "published automatically. The results are AI-generated and may contain "
    "errors. A processing run is not complete until that private submission "
    "succeeds."
)


class ConfigError(Exception):
    """Config file exists but cannot be used — corrupt JSON or wrong shape."""


def zspan_home() -> Path:
    """The config/workspace directory: $ZSPAN_HOME or ~/.zspan."""
    override = os.environ.get("ZSPAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".zspan"


def config_path() -> Path:
    return zspan_home() / "config.json"


def media_dir() -> Path:
    """Downloaded meeting media. Deleted after transcription by
    default — transcripts are the durable artifact, media is bulk."""
    return zspan_home() / "media"


def transcripts_dir() -> Path:
    """Per-meeting transcript JSONs — the flagship transcript_words
    shape; the workspace's transcript_path column points here."""
    return zspan_home() / "transcripts"


def videos_dir() -> Path:
    """Watchable local copies for the embed-disabled rescue: when a
    YouTube channel disallows embedding, the
    local site plays <meeting_id>.mp4 from here instead — kept, unlike
    the transcription media, because playback IS its purpose."""
    return zspan_home() / "media" / "video"


def flagship_url(config: Optional[Dict[str, Any]]) -> str:
    """Resolve the endpoint at use time: environment, config, default."""
    return (
        os.environ.get("ZSPAN_FLAGSHIP_URL", "").strip()
        or (config or {}).get("flagship_url")
        or DEFAULT_FLAGSHIP_URL
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config() -> Optional[Dict[str, Any]]:
    """Read the config. None when absent (normal first-run state); loud
    ConfigError when present-but-corrupt so a broken file is never
    mistaken for a fresh install."""
    path = config_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as e:
        raise ConfigError(
            f"Config file at {path} exists but could not be read as JSON "
            f"({e}). Fix or delete it, then re-run `zspan init`."
        ) from e
    if not isinstance(data, dict):
        raise ConfigError(
            f"Config file at {path} is valid JSON but not an object. "
            f"Fix or delete it, then re-run `zspan init`."
        )
    return data


def save_config(config: Dict[str, Any]) -> Path:
    """Write the config atomically, never world-readable for even an
    instant. Stamps version + timestamps; preserves whatever other keys
    the dict carries.

    The naive write-then-chmod pattern is unsafe for a file holding an API
    key: write_text() creates the file under the umask (0644 = world-
    readable), and only THEN chmods to 0600 — a window in which another
    local user can read the key, and if the chmod silently fails the key
    stays world-readable forever. Instead: create a temp file 0600 from
    birth (mkstemp never uses the umask), write + fsync, then atomically
    replace the target (os.replace carries the 0600 onto the final file).
    A crash mid-write leaves the previous config intact, not a truncated
    one. Windows has no POSIX mode bits; the user-profile ACL is the wall
    there (documented custody posture), and os.replace is still atomic.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The key's home dir — own-user-only (best-effort; no-op on Windows).
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    config = dict(config)
    config["version"] = CONFIG_VERSION
    config.setdefault("created_at", _utc_now_iso())
    config["updated_at"] = _utc_now_iso()
    body = json.dumps(config, indent=2) + "\n"

    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".config-", suffix=".tmp",
    )
    try:
        os.chmod(tmp, 0o600)  # mkstemp is already 0600; explicit + Windows-safe
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic; final file inherits the temp's 0600
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def home_jurisdiction(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the current home shape, including legacy read compatibility."""
    current = (config or {}).get("home_jurisdiction")
    if isinstance(current, dict):
        return {
            "state": current.get("state"),
            "county": current.get("county"),
            "city": current.get("city"),
        }
    legacy = (config or {}).get("picked_city")
    if isinstance(legacy, dict):
        return {
            "state": legacy.get("state"),
            "county": legacy.get("county"),
            "city": legacy.get("city"),
        }
    return None


def save_home_jurisdiction(
    config: Optional[Dict[str, Any]], state: str, county: str, city: str,
) -> Dict[str, Any]:
    updated = dict(config) if config else {}
    updated["home_jurisdiction"] = {
        "state": state,
        "county": county,
        "city": city,
    }
    save_config(updated)
    return load_config() or updated


def has_processing_ack(config: Optional[Dict[str, Any]]) -> bool:
    ack = (config or {}).get("local_processing_ack")
    if not isinstance(ack, dict):
        return False
    version = ack.get("version")
    return isinstance(version, int) and version >= PROCESSING_ACK_VERSION


def record_processing_ack(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    updated = dict(config) if config else {}
    updated["local_processing_ack"] = {
        "version": PROCESSING_ACK_VERSION,
        "accepted_at": _utc_now_iso(),
    }
    save_config(updated)
    return load_config() or updated


def key_fingerprint(key: str) -> str:
    """First 4 + last 4 of the key. The only form a key ever appears in
    on screen or in logs (ported from zspan_pipeline/byok_validate.py)."""
    if not key or len(key) < 12:
        return "(too short)"
    return f"{key[:4]}...{key[-4:]}"


def redact_key(text: str, key: str) -> str:
    """Scrub the API key out of an untrusted string before it surfaces.

    A provider's auth-error text is untrusted and sometimes echoes the
    submitted key (OpenAI's "Incorrect API key provided: sk-…" is the
    classic). The custody discipline is that a key only ever appears as
    its fingerprint — so any verbatim occurrence of the key, or a
    distinctive leading slice of it, is replaced with the fingerprint
    before the string is returned, logged, or shown."""
    if not text or not key:
        return text or ""
    fp = key_fingerprint(key)
    out = text.replace(key, fp)
    if len(key) >= 12:  # catch a leading-prefix echo, not just the whole key
        out = out.replace(key[:12], fp)
    return out

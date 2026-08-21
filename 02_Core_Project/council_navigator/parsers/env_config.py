"""Environment-backed parser configuration helpers."""

import json
import logging
import os
import stat
import tempfile
from functools import lru_cache

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'user_settings.json')


@lru_cache(maxsize=1)
def signin_enabled() -> bool:
    """Return whether new Google sign-in flows are available.

    The maintenance switch is enabled by default for backward compatibility.
    Only explicit false-like values disable sign-in; an empty value behaves
    the same as an unset variable.
    """
    raw_value = os.environ.get("ZSPAN_SIGNIN_ENABLED", "")
    enabled = raw_value.strip().lower() not in {"false", "0", "no", "off"}
    logger.info(
        "ZSPAN_SIGNIN_ENABLED resolved to %s",
        "enabled" if enabled else "disabled",
    )
    return enabled


def load_user_settings() -> dict:
    """Load user-configured settings from the on-disk settings file."""
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read user_settings.json: {e}")
        return {}


def _ensure_private_settings_parent(settings_path: str) -> str:
    """Create/tighten the settings directory before writing secrets."""
    parent = os.path.dirname(os.path.abspath(settings_path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.name != "posix":
        return parent
    current_mode = stat.S_IMODE(os.stat(parent).st_mode)
    if current_mode != 0o700:
        logger.warning(
            "Settings directory %s has mode %04o; correcting to 0700",
            parent,
            current_mode,
        )
        os.chmod(parent, 0o700)
    return parent


def _open_secure_named_temp(parent: str) -> tuple[int, str]:
    """Return an exclusively-created 0600 fd and same-directory temp path."""
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    for _ in range(10):
        # NamedTemporaryFile supplies a collision-resistant name; close/delete
        # the reservation before recreating it with the mandated os.open flags.
        with tempfile.NamedTemporaryFile(
            dir=parent,
            prefix=".user_settings-",
            suffix=".tmp",
        ) as reservation:
            tmp_path = reservation.name
        try:
            fd = os.open(tmp_path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            if os.name != "posix":
                pass
            elif hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(tmp_path, 0o600)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError as cleanup_error:
                logger.warning(
                    "Could not remove settings tempfile %s: %s",
                    tmp_path,
                    cleanup_error,
                )
            raise
        return fd, tmp_path
    raise FileExistsError("Could not allocate a secure settings tempfile")


def save_user_settings(settings: dict) -> None:
    """Persist user settings to disk."""
    parent = _ensure_private_settings_parent(SETTINGS_PATH)
    fd, tmp_path = _open_secure_named_temp(parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            json.dump(settings, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, SETTINGS_PATH)
    except BaseException:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            logger.warning(
                "Could not remove settings tempfile %s: %s",
                tmp_path,
                cleanup_error,
            )
        raise


def startup_correct_settings_permissions() -> None:
    """Tighten an existing settings file before it is read or reused."""
    if os.name != "posix" or not os.path.exists(SETTINGS_PATH):
        return
    current_mode = stat.S_IMODE(os.stat(SETTINGS_PATH).st_mode)
    if current_mode & ~0o600:
        logger.warning(
            "Settings file %s has mode %04o; correcting to 0600",
            SETTINGS_PATH,
            current_mode,
        )
        os.chmod(SETTINGS_PATH, 0o600)


startup_correct_settings_permissions()


def get_youtube_data_api_key() -> str:
    """Resolve YOUTUBE_DATA_API_KEY for the channel-to-video matcher (T-004).

    Resolution order matches the LLM-provider pattern:
      1. env var YOUTUBE_DATA_API_KEY
      2. parsers/user_settings.json field `youtube_data_api_key`

    Returns empty string if neither is set; callers should treat that as
    "key not configured" and surface a clear error to the operator.
    """
    settings = load_user_settings()
    return os.environ.get('YOUTUBE_DATA_API_KEY') or settings.get('youtube_data_api_key', '')


def get_gemini_consumer_cookies() -> tuple[str, str]:
    """Resolve the consumer Gemini Pro web UI cookies (SECURE_1PSID + SECURE_1PSIDTS).

    Used by the verify-mode workflow (`pipeline_operator_gemini_verify.py`) to
    drive gemini.google.com via the unofficial `gemini-webapi` wrapper against
    the user's Google One AI Pro entitlement (D-069). This is the consumer
    cookie path — NOT the official paid Gemini API (which uses gemini_api_key).

    Resolution order matches the rest of the BYOK pattern:
      1. env vars SECURE_1PSID + SECURE_1PSIDTS
      2. parsers/user_settings.json fields gemini_secure_1psid + gemini_secure_1psidts

    Returns ("", "") if either cookie is missing; callers should treat that as
    "cookies not configured" and surface a clear instruction to the operator
    (how to grab them from chrome://settings/cookies/detail?site=google.com).

    Refresh discipline: the underlying gemini-webapi v2.0.0 client auto-refreshes
    1PSIDTS in the background; SECURE_1PSID stays stable per-account but expires
    on full sign-out. If the wrapper starts returning auth errors, re-grab both
    cookies from a freshly-loaded Chrome session.
    """
    settings = load_user_settings()
    psid = os.environ.get('SECURE_1PSID') or settings.get('gemini_secure_1psid', '')
    psidts = os.environ.get('SECURE_1PSIDTS') or settings.get('gemini_secure_1psidts', '')
    return psid, psidts

"""Unit tests for the Mac Claude Relay PC-side client (commit 7b8708b).

`parsers/mac_claude_relay_client.py` is how PC-side Z-SPAN code reaches
the Mac Claude Relay service (the 8th-employee-substrate per S-019).
This test module focuses on the config-resolution branches — the
load-bearing decisions about WHEN to raise MacRelayConfigError vs proceed.

HTTP-level paths (invoke_mac_claude / shell_on_mac / health) are tested
only via their config-resolution preconditions; the actual POST behavior
against a live Mac is exercised end-to-end during smoke tests, not here.

Branches covered:
  * STATUS.json missing → MacRelayConfigError
  * STATUS.json present + up=false → MacRelayConfigError
  * STATUS.json present + up=true + missing base_url → MacRelayConfigError
  * STATUS.json invalid JSON → MacRelayConfigError
  * user_settings missing zspan_mac_relay_token → MacRelayConfigError
  * Happy path → (base_url, token) returned cleanly
  * base_url trailing slash stripped
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Make parsers/ importable when invoked from cwd=parsers/
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

import mac_claude_relay_client as mcr  # noqa: E402


# ── Test fixture: temp STATUS.json + user_settings ────────────────


@pytest.fixture
def temp_status_and_settings(tmp_path: Path, monkeypatch):
    """Yields a (write_status, set_settings) pair of helpers. Patches the
    module-level STATUS path + load_user_settings so each test controls
    its own filesystem fixture.
    """
    status_path = tmp_path / "STATUS.json"
    monkeypatch.setattr(mcr, "_MAC_RELAY_STATUS_PATH", status_path)

    def write_status(payload: dict | None) -> None:
        if payload is None:
            if status_path.exists():
                status_path.unlink()
        else:
            status_path.write_text(json.dumps(payload), encoding="utf-8")

    settings_state: dict = {}

    def set_settings(settings: dict) -> None:
        settings_state.clear()
        settings_state.update(settings)

    monkeypatch.setattr(mcr, "load_user_settings", lambda: dict(settings_state))

    return write_status, set_settings


# ── STATUS.json missing path ──────────────────────────────────────


def test_resolve_config_raises_when_status_missing(temp_status_and_settings):
    write_status, set_settings = temp_status_and_settings
    write_status(None)  # no file
    set_settings({"zspan_mac_relay_token": "tok-123"})

    with pytest.raises(mcr.MacRelayConfigError, match="STATUS.json not found"):
        mcr._resolve_mac_relay_config()


# ── STATUS.json up=false ──────────────────────────────────────────


def test_resolve_config_raises_when_status_says_up_false(temp_status_and_settings):
    write_status, set_settings = temp_status_and_settings
    write_status({
        "up": False,
        "base_url": "http://10.0.0.2:8766",
    })
    set_settings({"zspan_mac_relay_token": "tok-123"})

    with pytest.raises(mcr.MacRelayConfigError, match="up=false"):
        mcr._resolve_mac_relay_config()


# ── STATUS.json missing base_url ──────────────────────────────────


def test_resolve_config_raises_when_base_url_missing(temp_status_and_settings):
    write_status, set_settings = temp_status_and_settings
    write_status({"up": True})  # no base_url
    set_settings({"zspan_mac_relay_token": "tok-123"})

    with pytest.raises(mcr.MacRelayConfigError, match="missing base_url"):
        mcr._resolve_mac_relay_config()


def test_resolve_config_raises_when_base_url_empty_string(temp_status_and_settings):
    """Empty string base_url is treated as missing."""
    write_status, set_settings = temp_status_and_settings
    write_status({"up": True, "base_url": ""})
    set_settings({"zspan_mac_relay_token": "tok-123"})

    with pytest.raises(mcr.MacRelayConfigError, match="missing base_url"):
        mcr._resolve_mac_relay_config()


# ── Token missing path ────────────────────────────────────────────


def test_resolve_config_raises_when_token_missing(temp_status_and_settings):
    write_status, set_settings = temp_status_and_settings
    write_status({"up": True, "base_url": "http://10.0.0.2:8766"})
    set_settings({})  # no token

    with pytest.raises(mcr.MacRelayConfigError, match="zspan_mac_relay_token not set"):
        mcr._resolve_mac_relay_config()


def test_resolve_config_raises_when_token_empty_string(temp_status_and_settings):
    """Whitespace-only token is treated as missing."""
    write_status, set_settings = temp_status_and_settings
    write_status({"up": True, "base_url": "http://10.0.0.2:8766"})
    set_settings({"zspan_mac_relay_token": "   "})

    with pytest.raises(mcr.MacRelayConfigError, match="zspan_mac_relay_token not set"):
        mcr._resolve_mac_relay_config()


# ── Invalid JSON path ─────────────────────────────────────────────


def test_resolve_config_raises_on_invalid_json(temp_status_and_settings, tmp_path):
    write_status, set_settings = temp_status_and_settings
    # Write malformed JSON directly
    (tmp_path / "STATUS.json").write_text("{not valid json", encoding="utf-8")
    set_settings({"zspan_mac_relay_token": "tok-123"})

    with pytest.raises(mcr.MacRelayConfigError, match="unreadable"):
        mcr._resolve_mac_relay_config()


# ── Happy path ────────────────────────────────────────────────────


def test_resolve_config_happy_path(temp_status_and_settings):
    write_status, set_settings = temp_status_and_settings
    write_status({"up": True, "base_url": "http://10.0.0.2:8766"})
    set_settings({"zspan_mac_relay_token": "tok-abc-def"})

    base_url, token = mcr._resolve_mac_relay_config()
    assert base_url == "http://10.0.0.2:8766"
    assert token == "tok-abc-def"


def test_resolve_config_strips_base_url_trailing_slash(temp_status_and_settings):
    """If STATUS.json has a trailing slash on base_url, strip it so
    f-string concatenation like f'{base_url}/invoke' produces a clean URL.
    """
    write_status, set_settings = temp_status_and_settings
    write_status({"up": True, "base_url": "http://10.0.0.2:8766/"})
    set_settings({"zspan_mac_relay_token": "tok-abc"})

    base_url, _ = mcr._resolve_mac_relay_config()
    assert base_url == "http://10.0.0.2:8766"  # trailing slash stripped


def test_resolve_config_token_strips_whitespace(temp_status_and_settings):
    """A token with surrounding whitespace (paste artifacts) should be
    stripped on the way out.
    """
    write_status, set_settings = temp_status_and_settings
    write_status({"up": True, "base_url": "http://10.0.0.2:8766"})
    set_settings({"zspan_mac_relay_token": "  tok-abc  "})

    _, token = mcr._resolve_mac_relay_config()
    assert token == "tok-abc"


# ── Exception class hierarchy ────────────────────────────────────


def test_config_error_is_subclass_of_relay_error():
    """MacRelayConfigError should be a subclass of MacRelayError so
    callers can catch MacRelayError as a base case + dispatch on the
    specific subclass. Matches the parallel WhisperError / WhisperConfigError
    pattern.
    """
    assert issubclass(mcr.MacRelayConfigError, mcr.MacRelayError)
    assert issubclass(mcr.MacRelayHTTPError, mcr.MacRelayError)


# ── Path constant points where expected ──────────────────────────


def test_status_path_resolves_to_repo_relative_location():
    """The default _MAC_RELAY_STATUS_PATH should point at
    `02_Core_Project/mac_claude_relay/STATUS.json` relative to the repo.
    If anyone moves the file, the dispatcher breaks silently — lock the
    expected path with a test.
    """
    # This walks parser/.. .. .. mac_claude_relay STATUS.json. Verify it
    # ends with the expected suffix.
    p = mcr._MAC_RELAY_STATUS_PATH
    parts = p.parts
    # Last three parts should be: 02_Core_Project, mac_claude_relay, STATUS.json
    assert parts[-3:] == ("02_Core_Project", "mac_claude_relay", "STATUS.json"), (
        f"_MAC_RELAY_STATUS_PATH structure changed: {p}"
    )


# ── Public API surface present ───────────────────────────────────


def test_public_api_callables_exist():
    """The three public helpers (invoke_mac_claude, shell_on_mac, health)
    + the exception classes should all be importable from the module.
    If the module gets refactored and accidentally drops one, this catches it.
    """
    assert callable(mcr.invoke_mac_claude)
    assert callable(mcr.shell_on_mac)
    assert callable(mcr.health)
    assert isinstance(mcr.MacRelayError, type)
    assert isinstance(mcr.MacRelayConfigError, type)
    assert isinstance(mcr.MacRelayHTTPError, type)

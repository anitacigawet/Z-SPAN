"""V1.5-BYOK-Shell-1 — server-side BYOK key validation.

Per [BYOK_ARCHITECTURE_SPEC § 4.1](../../01_Project_Overview/BYOK_ARCHITECTURE_SPEC.md):
forwards a no-op test query to the provider's API, returns success/failure.
**The key is held in volatile request memory only for the test ping; never
persisted, never logged, never copied to any other variable.** This is the
ONE point where Z-SPAN sees the key — and only for ~100ms during the test.

For direct-browser-call providers (Gemini, Mistral) this is the ONLY
server-side touch the key ever gets in steady state. Live queries call
the provider directly from the browser with the key in the Authorization
header; Z-SPAN literally never sees them.

For CORS-blocked providers (OpenAI, Anthropic) this validates the key
once at onboarding; subsequent requests go through /api/byok/relay
(V1.5-Relay-1) with the same bytes-blind discipline.

Discipline this module enforces:
  - The `key` parameter is read into the outbound Authorization header
    directly; never assigned to any module-level variable
  - No logging of the key value (even at DEBUG); only its first 4 chars +
    last 4 chars when logging for forensic purposes
  - HTTP request body content NOT logged (caller's responsibility too, but
    we don't introduce any logging here)
  - Provider response body parsed only enough to extract success/failure;
    not stored
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


# Provider matrix for V1.5-BYOK-Shell-1. Per BYOK_ARCHITECTURE_SPEC § 2.4
# we ship with Gemini as the load-bearing free-tier direct-call provider.
# OpenAI + Anthropic come at V1.5-Relay-1 (need the CORS-relay shape).
# Audio + infographic providers come at their own chunks.

_GEMINI_VALIDATE_URL = (
    # The lightest-weight Gemini endpoint that succeeds with a valid key:
    # listModels. Returns the model list (small response), proves the key
    # is valid + scoped to the API correctly. No tokens consumed.
    "https://generativelanguage.googleapis.com/v1beta/models"
)

# Timeout for the validation ping. Must be tight — we're holding the key
# in volatile memory; the longer the call hangs the longer the key sits.
# 8s is plenty for a list-models call.
_VALIDATE_TIMEOUT_SECONDS = 8


def _key_fingerprint(key: str) -> str:
    """First 4 + last 4 of the key, joined by '...'. Safe to log; not
    enough material to reconstruct the key from logs."""
    if not key or len(key) < 12:
        return "(too short)"
    return f"{key[:4]}...{key[-4:]}"


def _redact_key(text: str, key: str) -> str:
    """Remove a submitted credential from untrusted provider text."""
    if not text or not key:
        return text or ""
    fingerprint = _key_fingerprint(key)
    redacted = text.replace(key, fingerprint)
    if len(key) >= 12:
        redacted = redacted.replace(key[:12], fingerprint)
    return redacted


def validate_gemini_key(api_key: str) -> Dict[str, Any]:
    """Forward a no-op listModels call to Google AI Studio Gemini with the
    user's key. Returns:
        {
            "valid": bool,
            "provider": "google-gemini",
            "fingerprint": "abcd...wxyz",
            "model_count": int (when valid),
            "error": str (when invalid; provider's error message),
        }

    Per the spec, the key is held in volatile request memory only for this
    call. Caller (the Flask endpoint) MUST NOT log the raw key — only the
    fingerprint that this module returns.
    """
    import requests  # local import; matches the codebase pattern

    fp = _key_fingerprint(api_key)
    logger.info("validate_gemini_key: pinging Gemini listModels with key=%s", fp)

    try:
        resp = requests.get(
            _GEMINI_VALIDATE_URL,
            headers={"X-Goog-Api-Key": api_key},
            timeout=_VALIDATE_TIMEOUT_SECONDS,
            # Hardcoded provider URL — refuse redirects so a hijacked/MITM'd
            # endpoint can't 3xx the user's key to another host (RR-8).
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as e:
        # Network-level failure. Don't expose key in error string.
        return {
            "valid": False,
            "provider": "google-gemini",
            "fingerprint": fp,
            "error": f"network error: {type(e).__name__}",
        }

    if resp.status_code == 200:
        try:
            data = resp.json()
            models = data.get("models") or []
            return {
                "valid": True,
                "provider": "google-gemini",
                "fingerprint": fp,
                "model_count": len(models),
            }
        except ValueError:
            return {
                "valid": False,
                "provider": "google-gemini",
                "fingerprint": fp,
                "error": "provider returned 200 but non-JSON body",
            }

    # Non-200: parse the provider's error message (Gemini returns
    # structured JSON for auth failures). Don't expose key in the
    # response we log/return.
    try:
        err_data = resp.json()
        err_msg = (
            (err_data.get("error") or {}).get("message")
            or f"HTTP {resp.status_code}"
        )
    except ValueError:
        err_msg = f"HTTP {resp.status_code} (non-JSON body)"

    err_msg = _redact_key(str(err_msg), api_key)

    logger.info(
        "validate_gemini_key: invalid key=%s status=%d msg=%r",
        fp, resp.status_code, err_msg[:200],
    )
    return {
        "valid": False,
        "provider": "google-gemini",
        "fingerprint": fp,
        "error": err_msg,
    }


_OPENAI_VALIDATE_URL = "https://api.openai.com/v1/models"
_ANTHROPIC_VALIDATE_URL = "https://api.anthropic.com/v1/models"


def validate_openai_key(api_key: str) -> Dict[str, Any]:
    """Forward a no-op listModels call to OpenAI. Returns the standard
    validate shape. Key in volatile request memory only; never persisted
    or logged (only fingerprint logged for forensics)."""
    import requests

    fp = _key_fingerprint(api_key)
    logger.info("validate_openai_key: pinging /v1/models with key=%s", fp)
    try:
        resp = requests.get(
            _OPENAI_VALIDATE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_VALIDATE_TIMEOUT_SECONDS,
            # Hardcoded provider URL — refuse redirects (RR-8).
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as e:
        return {
            "valid": False,
            "provider": "openai",
            "fingerprint": fp,
            "error": f"network error: {type(e).__name__}",
        }

    if resp.status_code == 200:
        try:
            data = resp.json()
            models = data.get("data") or []
            return {
                "valid": True,
                "provider": "openai",
                "fingerprint": fp,
                "model_count": len(models),
            }
        except ValueError:
            return {
                "valid": False,
                "provider": "openai",
                "fingerprint": fp,
                "error": "provider returned 200 but non-JSON body",
            }

    try:
        err_data = resp.json()
        err_msg = (
            (err_data.get("error") or {}).get("message")
            or f"HTTP {resp.status_code}"
        )
    except ValueError:
        err_msg = f"HTTP {resp.status_code} (non-JSON body)"
    logger.info(
        "validate_openai_key: invalid key=%s status=%d msg=%r",
        fp, resp.status_code, err_msg[:200],
    )
    return {
        "valid": False,
        "provider": "openai",
        "fingerprint": fp,
        "error": err_msg,
    }


def validate_anthropic_key(api_key: str) -> Dict[str, Any]:
    """Forward a no-op listModels call to Anthropic. Anthropic uses
    x-api-key + anthropic-version headers rather than Bearer. Key in
    volatile request memory only."""
    import requests

    fp = _key_fingerprint(api_key)
    logger.info("validate_anthropic_key: pinging /v1/models with key=%s", fp)
    try:
        resp = requests.get(
            _ANTHROPIC_VALIDATE_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=_VALIDATE_TIMEOUT_SECONDS,
            # Hardcoded provider URL — refuse redirects. requests does NOT
            # strip x-api-key on a cross-host redirect (only Authorization),
            # so this is the load-bearing guard for the Anthropic key (RR-8).
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as e:
        return {
            "valid": False,
            "provider": "anthropic",
            "fingerprint": fp,
            "error": f"network error: {type(e).__name__}",
        }

    if resp.status_code == 200:
        try:
            data = resp.json()
            models = data.get("data") or []
            return {
                "valid": True,
                "provider": "anthropic",
                "fingerprint": fp,
                "model_count": len(models),
            }
        except ValueError:
            return {
                "valid": False,
                "provider": "anthropic",
                "fingerprint": fp,
                "error": "provider returned 200 but non-JSON body",
            }

    try:
        err_data = resp.json()
        err_msg = (
            (err_data.get("error") or {}).get("message")
            or f"HTTP {resp.status_code}"
        )
    except ValueError:
        err_msg = f"HTTP {resp.status_code} (non-JSON body)"
    logger.info(
        "validate_anthropic_key: invalid key=%s status=%d msg=%r",
        fp, resp.status_code, err_msg[:200],
    )
    return {
        "valid": False,
        "provider": "anthropic",
        "fingerprint": fp,
        "error": err_msg,
    }


def validate_key(provider: str, api_key: str) -> Dict[str, Any]:
    """Dispatch table — route to per-provider validator.

    V1.5-Relay-1 shipped 2026-06-24 enables OpenAI + Anthropic key validation
    via per-provider listModels endpoints (both free, no token spend):
      - Gemini: GET generativelanguage.googleapis.com/v1beta/models
        (X-Goog-Api-Key auth)
      - OpenAI: GET api.openai.com/v1/models (Bearer auth)
      - Anthropic: GET api.anthropic.com/v1/models (x-api-key auth)

    Each holds the key in volatile memory only for the ping; never persisted
    or logged (only first4+last4 fingerprint). Per BYOK_ARCHITECTURE_SPEC § 4.
    """
    p = (provider or "").strip().lower()
    if p.startswith("google-gemini") or p == "gemini":
        return validate_gemini_key(api_key)
    if p.startswith("openai"):
        return validate_openai_key(api_key)
    if p.startswith("anthropic"):
        return validate_anthropic_key(api_key)
    return {
        "valid": False,
        "provider": provider,
        "fingerprint": _key_fingerprint(api_key),
        "error": (
            f"provider '{provider}' not yet supported by validate-key. "
            f"Supported: google-gemini-*, openai-*, anthropic-*."
        ),
    }

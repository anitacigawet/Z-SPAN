"""Key validation — a no-op list-models ping straight to the provider.

Lean port of zspan_pipeline/byok_validate.py for the local CLI. Same
discipline, simpler shape (one table instead of three near-identical
functions):

  - The key is read into the outbound request directly; never assigned
    anywhere else, never logged or printed — only the first4...last4
    fingerprint (config.key_fingerprint) ever surfaces.
  - The ping consumes no tokens on any provider (list-models is free).
  - The call goes DIRECTLY to the provider.

Return shape matches the original:
    {"valid": bool, "provider": str, "fingerprint": str,
     "model_count": int (when valid), "error": str (when not)}
"""
from __future__ import annotations

from typing import Any, Dict

import requests

from zspan_cli.config import key_fingerprint, redact_key

_TIMEOUT_SECONDS = 8  # tight on purpose — the key is in flight; don't let it hang


def _gemini_request(api_key: str) -> requests.Response:
    return requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": api_key},
        timeout=_TIMEOUT_SECONDS,
        allow_redirects=False,
    )


def _openai_request(api_key: str) -> requests.Response:
    return requests.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_TIMEOUT_SECONDS,
        allow_redirects=False,
    )


def _anthropic_request(api_key: str) -> requests.Response:
    return requests.get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        timeout=_TIMEOUT_SECONDS,
        # RR-8: refuse redirects. requests does NOT strip x-api-key on a
        # cross-host redirect (only Authorization), so a 302 would resend the
        # user's key to the redirect target. Hardcoded host -> no legit redirect.
        allow_redirects=False,
    )


# provider id -> (request fn, key for the model list in the 200 body)
_VALIDATORS = {
    "gemini": (_gemini_request, "models"),
    "openai": (_openai_request, "data"),
    "anthropic": (_anthropic_request, "data"),
}


def validate_key(provider: str, api_key: str) -> Dict[str, Any]:
    """Ping the provider's list-models endpoint with the user's key."""
    fp = key_fingerprint(api_key)
    entry = _VALIDATORS.get((provider or "").strip().lower())
    if entry is None:
        return {
            "valid": False,
            "provider": provider,
            "fingerprint": fp,
            "error": (
                f"provider '{provider}' is not supported. "
                f"Supported: {', '.join(sorted(_VALIDATORS))}."
            ),
        }
    request_fn, list_key = entry

    try:
        resp = request_fn(api_key)
    except requests.exceptions.RequestException as e:
        # Network-level failure; the exception text could echo the URL's
        # query string (Gemini carries the key there), so report the
        # exception TYPE only.
        return {
            "valid": False,
            "provider": provider,
            "fingerprint": fp,
            "error": f"network error: {type(e).__name__}",
        }

    if resp.status_code == 200:
        try:
            models = resp.json().get(list_key) or []
        except ValueError:
            return {
                "valid": False,
                "provider": provider,
                "fingerprint": fp,
                "error": "provider returned 200 but a non-JSON body",
            }
        # Model IDs travel too (capped) — the default-strongest-reachable
        # resolution ranks the key's OWN list rather than trusting a
        # hardcoded lineup. Gemini names come
        # back as "models/<id>"; strip the prefix for the canonical id.
        ids = []
        for m in models[:200]:
            raw = (m.get("id") or m.get("name") or "") if isinstance(m, dict) else ""
            if raw.startswith("models/"):
                raw = raw[len("models/"):]
            if raw:
                ids.append(raw)
        return {
            "valid": True,
            "provider": provider,
            "fingerprint": fp,
            "model_count": len(models),
            "model_ids": ids,
        }

    # Non-200: surface the provider's own error message (all three return
    # structured JSON for auth failures) — but scrub the key first, since an
    # auth-error body can echo the submitted key (OpenAI does).
    try:
        err = resp.json().get("error") or {}
        err_msg = err.get("message") or f"HTTP {resp.status_code}"
    except ValueError:
        err_msg = f"HTTP {resp.status_code} (non-JSON body)"
    return {
        "valid": False,
        "provider": provider,
        "fingerprint": fp,
        "error": redact_key(str(err_msg), api_key)[:300],
    }

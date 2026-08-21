"""V1.5-Relay-1 — server-side relay for CORS-blocked BYOK providers.

Per [BYOK_ARCHITECTURE_SPEC § 4.2](../../01_Project_Overview/BYOK_ARCHITECTURE_SPEC.md):
OpenAI + Anthropic don't include `Access-Control-Allow-Origin: *` in their
response headers, so browsers refuse to load their responses on a Z-SPAN
page. This module is the thin pass-through that lets BYOK still work for
those providers — user's browser POSTs to /api/byok/relay with their key
+ the request, we forward the bytes to the provider's API, return the
provider's response verbatim.

**Bytes-blind discipline:**
- The user's API key is read into the outbound Authorization header
  directly; never assigned to any module-level variable
- No logging of the key value (even at DEBUG); only first-4 + last-4
  fingerprint when logging for forensics
- HTTP request body content NOT logged
- Provider response NOT parsed/inspected/stored — only forwarded
- No middleware adds body logging; if future custom middleware breaks
  this, the CI test in BYOK_ARCHITECTURE_SPEC § 4.2 should fail

Per-provider request shaping is needed because OpenAI's
chat.completions API and Anthropic's messages API have different request
schemas. We don't want clients to know the per-provider differences;
they send us a normalized request {system_prompt, user_message,
max_tokens, temperature} and we shape it correctly per provider.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterator, Tuple

import requests

logger = logging.getLogger(__name__)


_RELAY_TIMEOUT_SECONDS = 60  # plenty for Q&A; bounded so a hung provider doesn't pin a Flask worker
# Bounded larger for streaming — the connection stays open the full response time
_RELAY_STREAM_TIMEOUT_SECONDS = 120


def _key_fingerprint(key: str) -> str:
    """First-4 + last-4 of the key. Safe to log."""
    if not key or len(key) < 12:
        return "(too short)"
    return f"{key[:4]}...{key[-4:]}"


def relay_to_openai(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> Tuple[int, Dict[str, Any]]:
    """Forward a chat-completions request to OpenAI. Returns (status_code,
    parsed_response_body). Key held in volatile memory only for the call."""
    url = "https://api.openai.com/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    fp = _key_fingerprint(api_key)
    logger.info(
        "byok_relay openai: model=%s key=%s max_tokens=%d temp=%.2f",
        model, fp, max_tokens, temperature,
    )
    try:
        resp = requests.post(
            url, json=payload, headers=headers, timeout=_RELAY_TIMEOUT_SECONDS,
            # Hardcoded provider URL — refuse redirects so a hijacked/MITM'd
            # endpoint can't 3xx the user's API key to another host (RR-8).
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as e:
        return 502, {
            "error": {
                "message": f"network error reaching OpenAI: {type(e).__name__}",
                "type": "network_error",
            }
        }
    try:
        body = resp.json()
    except ValueError:
        body = {"error": {"message": f"non-JSON response (HTTP {resp.status_code})", "raw": resp.text[:500]}}
    return resp.status_code, body


def relay_to_anthropic(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> Tuple[int, Dict[str, Any]]:
    """Forward a messages request to Anthropic. Returns (status_code,
    parsed_response_body). Anthropic uses x-api-key header (not Bearer) +
    has a separate top-level `system` field rather than a system role
    message — schema difference from OpenAI."""
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": model,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    fp = _key_fingerprint(api_key)
    logger.info(
        "byok_relay anthropic: model=%s key=%s max_tokens=%d temp=%.2f",
        model, fp, max_tokens, temperature,
    )
    try:
        resp = requests.post(
            url, json=payload, headers=headers, timeout=_RELAY_TIMEOUT_SECONDS,
            # Hardcoded provider URL — refuse redirects so a hijacked/MITM'd
            # endpoint can't 3xx the user's API key to another host (RR-8).
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as e:
        return 502, {
            "error": {
                "message": f"network error reaching Anthropic: {type(e).__name__}",
                "type": "network_error",
            }
        }
    try:
        body = resp.json()
    except ValueError:
        body = {"error": {"message": f"non-JSON response (HTTP {resp.status_code})", "raw": resp.text[:500]}}
    return resp.status_code, body


def relay(
    *,
    provider: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> Tuple[int, Dict[str, Any]]:
    """Dispatch table for CORS-blocked providers. Gemini is NOT routed
    here (it does direct browser calls); only OpenAI + Anthropic at V1.5.
    """
    p = (provider or "").strip().lower()
    if p.startswith("openai"):
        return relay_to_openai(
            api_key=api_key,
            model=model or "gpt-4o-mini",
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    if p.startswith("anthropic"):
        return relay_to_anthropic(
            api_key=api_key,
            model=model or "claude-3-haiku-20240307",
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    return 400, {
        "error": {
            "message": f"provider '{provider}' not routable via /api/byok/relay; supported: openai, anthropic. Gemini does direct browser calls.",
            "type": "unsupported_provider",
        }
    }


# ─────────────────────────────────────────────────────────────────
# Streaming variants (V1.5-BYOK-Stream-1, 2026-07-04)
# ─────────────────────────────────────────────────────────────────
#
# The one-shot relay above blocks until the provider returns the full body.
# For chat UX we want token-by-token typing. Providers offer SSE variants:
# OpenAI /chat/completions with {"stream": true}, Anthropic /v1/messages
# with {"stream": true}. This module yields the SSE bytes verbatim — the
# Flask endpoint wraps them in a streaming Response, Express pipes them
# through, and the browser parses the SSE events client-side.
#
# Byte-blind discipline preserved: we don't parse the deltas server-side,
# we just forward. If a client wants tokens, they parse them; if they want
# to log usage, they parse the final message.
#
# Terminal sentinel: a "data: [DONE]\n\n" line marks EOF for both providers'
# streams. OpenAI emits this natively; for Anthropic we synthesize one at
# the end of the message_stop event so the client has one uniform EOF
# marker regardless of provider.


def _sse_error(message: str, kind: str) -> bytes:
    """Emit an SSE error event the client can distinguish from provider deltas."""
    return f"event: relay_error\ndata: {json.dumps({'error': {'message': message, 'type': kind}})}\n\n".encode("utf-8")


def relay_stream_to_openai(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> Iterator[bytes]:
    """Stream chat-completions SSE from OpenAI. Yields raw SSE lines as
    bytes, one chunk per `data: {...}\\n\\n` block. Also requests
    `stream_options.include_usage` so the final chunk carries the token
    counts we need for cost bookkeeping.
    """
    url = "https://api.openai.com/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    fp = _key_fingerprint(api_key)
    logger.info(
        "byok_relay_stream openai: model=%s key=%s max_tokens=%d temp=%.2f",
        model, fp, max_tokens, temperature,
    )
    try:
        with requests.post(
            url, json=payload, headers=headers,
            stream=True, timeout=_RELAY_STREAM_TIMEOUT_SECONDS,
            # Hardcoded provider URL — refuse redirects so a hijacked/MITM'd
            # endpoint can't 3xx the user's API key to another host (RR-8).
            allow_redirects=False,
        ) as resp:
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    msg = (body.get("error") or {}).get("message") or f"OpenAI HTTP {resp.status_code}"
                except ValueError:
                    msg = f"OpenAI HTTP {resp.status_code}"
                yield _sse_error(msg, "provider_error")
                yield b"data: [DONE]\n\n"
                return
            for line in resp.iter_lines(decode_unicode=False):
                if line is None:
                    continue
                # requests strips the trailing \n; re-emit the SSE line-then-blank shape
                yield line + b"\n"
                # blank line between events is what SSE parsers key on
                if line == b"" or line == b"data: [DONE]":
                    # emit an extra blank so the browser's TextDecoder sees the boundary
                    yield b"\n"
    except requests.exceptions.RequestException as e:
        yield _sse_error(
            f"network error reaching OpenAI: {type(e).__name__}",
            "network_error",
        )
        yield b"data: [DONE]\n\n"


def relay_stream_to_anthropic(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> Iterator[bytes]:
    """Stream messages SSE from Anthropic. Yields raw SSE bytes and
    synthesizes a `data: [DONE]\\n\\n` sentinel at the end so the client has
    a uniform EOF signal across providers.
    """
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": model,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    fp = _key_fingerprint(api_key)
    logger.info(
        "byok_relay_stream anthropic: model=%s key=%s max_tokens=%d temp=%.2f",
        model, fp, max_tokens, temperature,
    )
    try:
        with requests.post(
            url, json=payload, headers=headers,
            stream=True, timeout=_RELAY_STREAM_TIMEOUT_SECONDS,
            # Hardcoded provider URL — refuse redirects so a hijacked/MITM'd
            # endpoint can't 3xx the user's API key to another host (RR-8).
            allow_redirects=False,
        ) as resp:
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    msg = (body.get("error") or {}).get("message") or f"Anthropic HTTP {resp.status_code}"
                except ValueError:
                    msg = f"Anthropic HTTP {resp.status_code}"
                yield _sse_error(msg, "provider_error")
                yield b"data: [DONE]\n\n"
                return
            for line in resp.iter_lines(decode_unicode=False):
                if line is None:
                    continue
                yield line + b"\n"
                if line == b"":
                    # SSE event boundary; add the extra newline the spec expects
                    yield b"\n"
            # Anthropic doesn't emit `data: [DONE]`; synthesize so client SSE
            # readers can use one uniform terminal check.
            yield b"data: [DONE]\n\n"
    except requests.exceptions.RequestException as e:
        yield _sse_error(
            f"network error reaching Anthropic: {type(e).__name__}",
            "network_error",
        )
        yield b"data: [DONE]\n\n"


def relay_stream(
    *,
    provider: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> Iterator[bytes]:
    """Streaming dispatch table. Gemini is NOT routed here (direct browser
    calls); only OpenAI + Anthropic. On unsupported provider we emit a
    single SSE error event + a [DONE] sentinel so client SSE readers can
    terminate cleanly instead of hanging on an empty stream.
    """
    p = (provider or "").strip().lower()
    if p.startswith("openai"):
        yield from relay_stream_to_openai(
            api_key=api_key,
            model=model or "gpt-4o-mini",
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return
    if p.startswith("anthropic"):
        yield from relay_stream_to_anthropic(
            api_key=api_key,
            model=model or "claude-3-haiku-20240307",
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return
    yield _sse_error(
        f"provider '{provider}' not routable via /api/byok/relay-stream; supported: openai, anthropic. Gemini does direct browser calls.",
        "unsupported_provider",
    )
    yield b"data: [DONE]\n\n"

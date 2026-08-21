"""
HQ skybox traffic-event bus (S-006 extension, post-Logstalgia pivot 2026-05-29).

Receives per-request events from two upstream feeds and fans them out to all
connected SSE subscribers (the in-HQ skybox viz — fiber-optic shooting stars
across an OLED-black sky above the HQ building).

Upstream feeds (chunks 4 + 5):
  - Flask access log (local dev + Railway prod) — backend/API traffic
  - Cloudflare Worker on zspan.org/* — direct Pages-side page-views

Downstream:
  - GET /api/hq/traffic-events (SSE) — the HQ skybox subscribes here
  - POST /api/hq/traffic-events/inject — owner-gated test injection (mock panel)
  - POST /api/hq/traffic-events/ingest — signed ingest from the CF Worker

Event shape — the single canonical contract:
  {
    "ts": "<ISO-8601 UTC>",
    "status": 200,
    "path_class": "broadcast"|"guide"|"api"|"static"|"admin"|"other",
    "bot_classification": "human"|"verified_bot"|"likely_bot"|"unknown",
    "source": "flask"|"cloudflare"|"mock",
  }

Color picker (frontend):
  - white: status < 400 AND bot_classification != "likely_bot"
  - red:   status >= 400 OR  bot_classification == "likely_bot"
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional


# ── event bus ─────────────────────────────────────────────────────────

class Subscriber:
    """One connected SSE client. The Flask SSE generator owns the lifecycle."""

    __slots__ = ('q', 'connected_at')

    def __init__(self, max_buffer: int = 1000):
        self.q: 'queue.Queue[Dict[str, Any]]' = queue.Queue(maxsize=max_buffer)
        self.connected_at = time.time()


_subscribers: List[Subscriber] = []
_subscribers_lock = threading.Lock()

# ── Defense-in-depth against floods (DDoS, runaway worker, etc.) ──────
# The renderer caps active stars at MAX_STARS=280 (oldest-dropped), and
# subscriber queues drop on backpressure — but the bus itself was
# unbounded. Adding a sliding-window cap on broadcast() so the SSE
# stream never exceeds what the canvas can absorb. Lined up at
# ~50 events/sec because StarField crosses in ~4.3s → ≈215 active
# stars in steady state, well inside MAX_STARS. Layered with the
# ingest-endpoint cap in api_server.py (which returns 429 above 200
# POSTs/sec — CPU protection on the receiver side).
_BROADCAST_RATE_LIMIT_PER_SEC = 50
_broadcast_times: Deque[float] = deque(
    maxlen=_BROADCAST_RATE_LIMIT_PER_SEC * 3
)
_broadcast_rate_lock = threading.Lock()
_broadcast_dropped_total = 0


def subscribe() -> Subscriber:
    """Register a new SSE subscriber. Caller MUST unsubscribe in a finally."""
    sub = Subscriber()
    with _subscribers_lock:
        _subscribers.append(sub)
    return sub


def unsubscribe(sub: Subscriber) -> None:
    """Drop a closed SSE subscriber. Safe to call multiple times."""
    with _subscribers_lock:
        try:
            _subscribers.remove(sub)
        except ValueError:
            pass


def broadcast(event: Dict[str, Any]) -> int:
    """Fan an event out to every connected subscriber.

    A slow consumer (full buffer) loses the event for itself only; the bus
    never backpressures. Returns the count of subscribers reached attempts
    were made for, not successful deliveries (a dropped event still "reached"
    that subscriber in the sense that the bus tried).

    Rate-limited at _BROADCAST_RATE_LIMIT_PER_SEC via a sliding window.
    Excess events are silently dropped (returned count is 0) — the
    StarField caps active stars at MAX_STARS anyway, so over-pumping the
    bus just wastes CPU on broadcasts the canvas would discard.
    """
    global _broadcast_dropped_total
    with _broadcast_rate_lock:
        now = time.time()
        while _broadcast_times and now - _broadcast_times[0] > 1.0:
            _broadcast_times.popleft()
        if len(_broadcast_times) >= _BROADCAST_RATE_LIMIT_PER_SEC:
            _broadcast_dropped_total += 1
            return 0
        _broadcast_times.append(now)

    with _subscribers_lock:
        subs = list(_subscribers)
    for s in subs:
        try:
            s.q.put_nowait(event)
        except queue.Full:
            # Slow consumer; drop the event for them only.
            pass
    return len(subs)


def broadcast_stats() -> Dict[str, int]:
    """Observability for the rate limiter (current sec rate + lifetime drops)."""
    with _broadcast_rate_lock:
        return {
            "current_per_sec": len(_broadcast_times),
            "dropped_total": _broadcast_dropped_total,
            "rate_limit_per_sec": _BROADCAST_RATE_LIMIT_PER_SEC,
        }


def subscriber_count() -> int:
    """Number of currently-connected subscribers."""
    with _subscribers_lock:
        return len(_subscribers)


# ── event normalization ──────────────────────────────────────────────

VALID_PATH_CLASSES = ('broadcast', 'guide', 'api', 'static', 'admin', 'other')
VALID_BOT_CLASSIFICATIONS = ('human', 'verified_bot', 'likely_bot', 'unknown')
VALID_SOURCES = ('flask', 'cloudflare', 'mock')


def normalize_event(raw: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Coerce a raw event from any feed into the canonical shape.

    Forgiving on missing fields — assigns defaults rather than rejecting.
    """
    ts = raw.get('ts') or _utc_now_iso()

    try:
        status = int(raw.get('status', 200))
    except (TypeError, ValueError):
        status = 200

    path_class = str(raw.get('path_class') or 'other').lower()
    if path_class not in VALID_PATH_CLASSES:
        path_class = 'other'

    bot_classification = str(raw.get('bot_classification') or 'unknown').lower()
    if bot_classification not in VALID_BOT_CLASSIFICATIONS:
        bot_classification = 'unknown'

    if source not in VALID_SOURCES:
        source = 'mock'

    return {
        'ts': ts,
        'status': status,
        'path_class': path_class,
        'bot_classification': bot_classification,
        'source': source,
    }


# ── path classification (chunks 4 + 5 use this) ──────────────────────

# Paths the Flask access-log tee should NOT broadcast. The viz showing the
# viz's own monitoring traffic would be both visually noisy and dishonest.
EXCLUDED_PATHS: tuple = (
    '/api/hq/traffic-events',     # the SSE stream itself
    '/api/operator/badges',       # orchestrator/operator poll
    '/api/orchestrator/autonomy', # gate-board poll
    '/api/ingestion/governor',    # governor poll
    '/api/hq/status',             # HQ board poll (when wired)
)


def classify_path(path: str) -> str:
    """Bucket a URL path into one of VALID_PATH_CLASSES."""
    if not path:
        return 'other'
    # Strip query string defensively (callers may pass full request URLs).
    p = path.split('?', 1)[0]
    if p == '/' or p == '':
        return 'other'
    # Order matters: more specific prefixes first.
    if p.startswith('/api/guide') or p.startswith('/guide'):
        return 'guide'
    if (p.startswith('/api/operator') or p.startswith('/api/orchestrator')
            or p.startswith('/api/ingestion') or p.startswith('/api/hq')
            or p.startswith('/api/work-orders') or p.startswith('/api/sync')):
        return 'admin'
    if (p.startswith('/broadcast') or p.startswith('/api/notebook')
            or p.startswith('/api/quotes') or p.startswith('/api/cast')
            or p.startswith('/api/truth-book')):
        return 'broadcast'
    if p.startswith('/api/'):
        return 'api'
    if (p.startswith('/media/') or p.startswith('/static/')
            or p.startswith('/assets/')):
        return 'static'
    return 'other'


def is_excluded_path(path: str) -> bool:
    """True if the Flask access-log tee should silently skip this path."""
    if not path:
        return True
    p = path.split('?', 1)[0]
    for prefix in EXCLUDED_PATHS:
        if p.startswith(prefix):
            return True
    return False


# ── helpers ──────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

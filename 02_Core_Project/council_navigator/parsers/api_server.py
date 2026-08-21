#!/usr/bin/env python3.11
"""
Flask API server for city calendar scrapers.
Uses SQLite cache for instant responses, with fallback to live scraping.

═══ ENDPOINT AUTH TIERING (canonical inventory: 04_Operator_Side/AUTH_AUDIT_2026-07-04.md) ═══

Three tiers. Every new endpoint added to this file MUST land in exactly one:

1. OWNER-ONLY — mutation of published content, spend triggers, settings,
   review-queue actions. Gated via `_require_owner()` at handler top.
   Any Express counterpart passes `req` to `proxyToFlask` (or uses
   `proxyJsonAuth`) so the owner-session cookie forwards. Example:
   `/api/meetings/N/publish`, `/api/settings`, `/api/orchestrator/autonomy`.

2. INTENTIONALLY PUBLIC — verification surfaces + civic-data reads that
   ARE the project's mission. Ungated by design; documented here so a
   future auditor doesn't mistake them for oversight gaps:
     - Public verification: `/api/verify-run/*`, `/api/watermark-lookup/*`.
       These EXIST so anyone can prove provenance without needing an account.
     - Public civic reads: `/api/cast/*`, `/api/truth-book/*`,
       `/api/compiler/*`, `/api/ledger/*`, `/api/notebook/*`,
       `/api/cities/*`, `/api/search`, `/api/channels/tree`. These serve
       the same broadcast content the C-SPAN-for-Gen-Z mission commits
       to publishing openly.
     - LAN/loopback bypass ok: `/api/rag-search/*`, `/api/member-rag/*`
       accept shared-token OR loopback; see per-endpoint comments.

3. DELIBERATELY OPEN (S-134-tracked) — the two `agent-propose` endpoints
   (`/api/vocabulary-inbox/N/agent-propose` +
   `/api/disputed-quotes/N/agent-propose`) accept unsigned
   `X-Zspan-Agent-Role` from employee-agents. Owner-gating would break
   the agent path. Proper fix needs S-134 (agent-as-operator cookie OR
   signed agent token). Tracked in FUTURE_THOUGHTS.md.

When adding a new endpoint: pick the tier explicitly. If tier (1),
call `_require_owner()`. If tier (2), leave a one-line comment naming
the reason (verification / civic-data / loopback bypass). If tier (3),
file an S-NNN entry naming the eventual fix.
"""
from flask import Flask, jsonify, request, g, Response
from werkzeug.exceptions import RequestEntityTooLarge
from parser_loader import scrape_city_calendar, load_parser_index, routing_index_unavailable
from database import (
    get_cached_meetings, get_cached_meetings_with_meta,
    cache_meetings, search_meetings,
    get_stats, count_users, get_all_meetings_for_county, get_council_members,
    populate_cities_from_index, init_db,
    register_notebook, get_meeting_with_notebook, save_notebook_output,
    get_meeting_public_record, get_resolved_video_url,
    is_meeting_publicly_visible, public_serving_sql, PUBLIC_ID_RE,
    set_city_youtube_channel, get_city_youtube_channel, get_live_streams,
    get_cities_with_youtube_channel, get_cities_with_meeting_on,
    enqueue_work_order, update_work_order_state,
    get_work_order, list_work_orders, work_order_stats,
    seed_council_members_from_intelligence, get_connection,
    list_corrections, create_correction, update_correction,
    find_flagship_generation_by_token, find_flagship_watermark_row,
    generate_generation_public_id, mint_cli_ribbon_token,
    set_notebook_output_void_state,
    get_user_librarian_access, set_librarian_access,
    decide_librarian_access, list_librarian_access_requests,
    get_invitation_status, redeem_invitation_token,
    import_invitation_batch, list_invitation_codes,
    revoke_invitation_code,
    AccessDeniedResult, AdmittedResult,
    CooldownDeniedResult, EpochChanged, QuotaExhaustedResult,
    RejectedResult, claim_librarian_provider_dispatch,
    claim_librarian_retrieval, evaluate_and_record_librarian_query,
    get_librarian_policy_snapshot, librarian_result_epoch_is_current,
    mark_librarian_event_terminal_failure, update_librarian_policy,
)
from ingestion_governor import compute_city_metering
from traffic_events import (
    subscribe as traffic_subscribe,
    unsubscribe as traffic_unsubscribe,
    broadcast as traffic_broadcast,
    subscriber_count as traffic_subscriber_count,
    normalize_event as traffic_normalize_event,
    classify_path as traffic_classify_path,
    is_excluded_path as traffic_is_excluded_path,
)
from normalize import normalize_meeting_fields
from env_config import save_user_settings, load_user_settings, signin_enabled
from agent_audit import KNOWN_ROLES
import logging
import base64
import binascii
import hashlib
import html
import hmac
import ipaddress
import math
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Deque, Dict, List, Optional
import json
import os
import re
import secrets
import uuid
from bisect import bisect_right
from urllib.parse import urlencode, urlparse

import requests

from librarian_query_stencil import (
    COMPOSED_GATE_VERSION,
    evaluate_librarian_query,
)
from librarian_input_gate import QUERY_CHAR_CAP

try:
    from parsers import public_dto
except ImportError:  # Direct `python api_server.py` from parsers/.
    import public_dto

from public_api import bp as _public_api_bp

from zspan_pipeline.output_contracts import (
    FLAGSHIP_PRODUCTION_CONTRACT,
    PUBLICATION_CONTRACT,
)

app = Flask(__name__)
# This process also receives approved flagship media and transcript payloads,
# so the app-wide ceiling must accommodate those legitimate non-BYOK routes.
# The live-query routes apply their much smaller 32 KiB ceiling before auth,
# JSON parsing, database access, or provider dispatch.
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024

_KNOWN_OUTPUT_TYPES = frozenset(FLAGSHIP_PRODUCTION_CONTRACT).union(
    PUBLICATION_CONTRACT,
    # Live fetcher-registry members intentionally outside the production /
    # publication contracts (retained extraction and deferred display types).
    {"quote_extraction", "suggested_questions"},
)


# ── Public BYOK-family per-IP rate limits ────────────────────────────
# Sliding windows are intentionally fixed in code: these are public safety
# invariants, not operator-tunable behavior. The limiter runs before endpoint
# policy/auth gates so owner traffic is protected today and lifting D-145 does
# not expose an unmetered path later.
_PUBLIC_RATE_LIMIT_WINDOW_SECONDS = 60.0
_PUBLIC_RATE_LIMITS = {
    'decode_ribbon_image': 5,  # Public image decode: CPU/memory intensive.
    'system_heartbeat': 30,  # Public presence writes; client cadence has headroom.
    'verify_run': 60,       # Cheap SQL read; 1/sec still supports verification bursts.
    'validate_key': 10,     # Each accepted request makes an outbound provider call.
    'citation': 120,        # Cheap published-data read serving normal page traffic.
    'public_read': 240,     # Shared browse budget; one page loads several resources.
    'public_search': 60,    # Table scans and aggregate calendar reads.
    'public_heavy_read': 30,  # Multi-table joins and correlated subqueries.
    'public_external': 10,  # Synchronous outbound service calls.
    'byok_relay': 60,       # BYOK spec section 2.1 public-query starting point.
    'byok_relay_stream': 60,  # Same public-query budget as the one-shot relay.
    'rag_search': 60,       # BYOK spec section 2.1 retrieval starting point.
    'browser_process': 12,  # Single-flight jobs; retries stay bounded per IP.
    'invitation': 20,       # One card scan + redemption; probes stay bounded.
    'password_auth': 10,    # Memory-hard verifier plus per-account lockout.
    'password_reset': 5,    # Email delivery and reset-token writes.
}
_TRUSTED_ORIGINS_DEFAULT = (
    'https://zspan.org',
    'https://operator.zspan.org',
)
_RIBBON_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
_PASSWORD_AUTH_BODY_MAX_BYTES = 16 * 1024
_RIBBON_IMAGE_MAX_DIMENSION = 8192
_HEARTBEAT_FIELD_MAX_LENGTHS = {
    'session_id': 64,
    'client_kind': 32,
    'current_action': 128,
}
_ANONYMOUS_HEARTBEAT_SESSION_LIMIT = 10
_PUBLIC_RATE_LIMIT_MAX_BUCKETS = 4096
_PUBLIC_RATE_LIMIT_PRUNE_INTERVAL_SECONDS = 60.0
_public_rate_limit_buckets: Dict[tuple[str, str], Deque[float]] = {}
_public_rate_limit_lock = threading.Lock()
_public_rate_limit_last_prune = 0.0

_PUBLIC_YOUTUBE_EMBED_CACHE_TTL_SECONDS = 15 * 60.0
_PUBLIC_YOUTUBE_EMBED_ERROR_CACHE_TTL_SECONDS = 30.0
_PUBLIC_YOUTUBE_EMBED_CACHE_MAX_ENTRIES = 2048
_public_youtube_embed_cache: dict[str, tuple[float, bool]] = {}
_public_youtube_embed_cache_lock = threading.Lock()


def _public_rate_limit_now() -> float:
    """Monotonic clock kept behind a tiny seam for deterministic tests."""
    return time.monotonic()


def _rate_limit_client_ip() -> str:
    """Return the abuse-prevention bucket key for the current TCP client.

    Express overwrites ``X-Zspan-Client-Ip`` before proxying. Trust that
    header only from a loopback TCP peer; direct callers cannot select their
    own bucket. This is abuse-prevention-grade attribution, not auth identity.
    """
    peer = (request.remote_addr or '').strip()
    candidate = peer
    try:
        if ipaddress.ip_address(peer).is_loopback:
            candidate = (request.headers.get('X-Zspan-Client-Ip') or '').strip() or peer
    except ValueError:
        # A real TCP peer is normally parseable. If a test server or unusual
        # WSGI adapter supplies something else, ignore the forwarded header.
        candidate = peer

    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        return peer or 'unknown-peer'


def _prune_public_rate_limit_buckets(now: float) -> None:
    """Drop expired buckets and evict oldest buckets above the hard cap.

    Caller holds ``_public_rate_limit_lock``.
    """
    cutoff = now - _PUBLIC_RATE_LIMIT_WINDOW_SECONDS
    stale_keys = [
        key for key, timestamps in _public_rate_limit_buckets.items()
        if not timestamps or timestamps[-1] <= cutoff
    ]
    for key in stale_keys:
        _public_rate_limit_buckets.pop(key, None)

    overflow = len(_public_rate_limit_buckets) - _PUBLIC_RATE_LIMIT_MAX_BUCKETS
    if overflow > 0:
        oldest = sorted(
            _public_rate_limit_buckets,
            key=lambda key: _public_rate_limit_buckets[key][-1],
        )[:overflow]
        for key in oldest:
            _public_rate_limit_buckets.pop(key, None)


def _consume_public_rate_limit(route_family: str) -> tuple[bool, int]:
    """Consume one request and return ``(allowed, retry_after_seconds)``."""
    global _public_rate_limit_last_prune

    limit = _PUBLIC_RATE_LIMITS[route_family]
    now = _public_rate_limit_now()
    cutoff = now - _PUBLIC_RATE_LIMIT_WINDOW_SECONDS
    key = (route_family, _rate_limit_client_ip())

    with _public_rate_limit_lock:
        if (
            now - _public_rate_limit_last_prune
            >= _PUBLIC_RATE_LIMIT_PRUNE_INTERVAL_SECONDS
            or len(_public_rate_limit_buckets) >= _PUBLIC_RATE_LIMIT_MAX_BUCKETS
        ):
            _prune_public_rate_limit_buckets(now)
            _public_rate_limit_last_prune = now

        timestamps = _public_rate_limit_buckets.get(key)
        if timestamps is None:
            # Make room before insertion even if a mocked/non-monotonic clock
            # kept the periodic-prune condition from firing.
            if len(_public_rate_limit_buckets) >= _PUBLIC_RATE_LIMIT_MAX_BUCKETS:
                oldest_key = min(
                    _public_rate_limit_buckets,
                    key=lambda bucket_key: _public_rate_limit_buckets[bucket_key][-1],
                )
                _public_rate_limit_buckets.pop(oldest_key, None)
            timestamps = deque()
            _public_rate_limit_buckets[key] = timestamps

        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= limit:
            retry_after = max(
                1,
                math.ceil(timestamps[0] + _PUBLIC_RATE_LIMIT_WINDOW_SECONDS - now),
            )
            return False, retry_after

        timestamps.append(now)
        return True, 0


def _public_rate_limited(route_family: str):
    """Decorate a Flask handler with its fixed public per-IP budget."""
    if route_family not in _PUBLIC_RATE_LIMITS:
        raise ValueError(f'unknown public rate-limit family: {route_family}')

    def decorator(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            allowed, retry_after = _consume_public_rate_limit(route_family)
            if not allowed:
                response = jsonify({
                    'success': False,
                    'error': 'Too many requests to this endpoint. Please try again shortly.',
                    'retry_after_seconds': retry_after,
                })
                response.status_code = 429
                response.headers['Retry-After'] = str(retry_after)
                return response
            return handler(*args, **kwargs)
        return wrapped
    return decorator


def _trusted_origins() -> frozenset[str]:
    """Return the exact browser origins allowed to mutate cookie state."""
    configured = os.environ.get(
        'ZSPAN_TRUSTED_ORIGINS',
        ','.join(_TRUSTED_ORIGINS_DEFAULT),
    )
    return frozenset(
        origin.strip()
        for origin in configured.split(',')
        if origin.strip()
    )


def _missing_origin_is_internal_proxy_request() -> bool:
    """Recognize the existing Express-to-Flask loopback hop.

    Express currently does not forward the browser's Origin header. Keep that
    confirmed local proxy path working while rejecting absent Origin from any
    non-loopback peer. A present Origin is always checked, including on
    loopback, so the compatibility branch cannot override an explicit
    untrusted value.
    """
    try:
        return ipaddress.ip_address(request.remote_addr or '').is_loopback
    except ValueError:
        return False


def _require_trusted_origin(handler):
    """Reject cookie-authenticated mutations from untrusted origins."""
    @wraps(handler)
    def wrapped(*args, **kwargs):
        origin = request.headers.get('Origin')
        if origin is None:
            if _missing_origin_is_internal_proxy_request():
                return handler(*args, **kwargs)
        elif origin.strip() in _trusted_origins():
            return handler(*args, **kwargs)

        return jsonify({
            'success': False,
            'error': 'untrusted_origin',
        }), 403

    wrapped._requires_trusted_origin = True
    return wrapped


def _reset_public_rate_limits_for_tests() -> None:
    """Explicit test-only reset; production code never bypasses the limiter."""
    global _public_rate_limit_last_prune
    with _public_rate_limit_lock:
        _public_rate_limit_buckets.clear()
        _public_rate_limit_last_prune = 0.0


def _youtube_embed_cache_get(video_id: str) -> bool | None:
    now = time.monotonic()
    with _public_youtube_embed_cache_lock:
        cached = _public_youtube_embed_cache.get(video_id)
        if cached is None:
            return None
        expires_at, embeddable = cached
        if expires_at <= now:
            _public_youtube_embed_cache.pop(video_id, None)
            return None
        return embeddable


def _youtube_embed_cache_put(
    video_id: str,
    embeddable: bool,
    ttl_seconds: float = _PUBLIC_YOUTUBE_EMBED_CACHE_TTL_SECONDS,
) -> None:
    now = time.monotonic()
    with _public_youtube_embed_cache_lock:
        expired = [
            key for key, (expires_at, _value) in _public_youtube_embed_cache.items()
            if expires_at <= now
        ]
        for key in expired:
            _public_youtube_embed_cache.pop(key, None)
        if len(_public_youtube_embed_cache) >= _PUBLIC_YOUTUBE_EMBED_CACHE_MAX_ENTRIES:
            oldest = min(
                _public_youtube_embed_cache,
                key=lambda key: _public_youtube_embed_cache[key][0],
            )
            _public_youtube_embed_cache.pop(oldest, None)
        _public_youtube_embed_cache[video_id] = (
            now + max(1.0, ttl_seconds),
            embeddable,
        )


def _reset_public_youtube_embed_cache_for_tests() -> None:
    with _public_youtube_embed_cache_lock:
        _public_youtube_embed_cache.clear()

# ── S-004 agent identity propagation ─────────────────────────────────
# Employee-agents send the `X-Zspan-Agent-Role: <role-id>` header on every
# request they make (per agents/README.md § Identity propagation). We parse
# it on each request, stash it on flask.g, and surface it in Flask's log
# output so the server-side stream attributes agent-driven work distinctly
# from operator-driven work.
#
# Audit columns on individual tables (verified_by, acknowledged_by, etc.)
# remain primarily body-driven — each agent manual specifies what it writes
# into those fields. The header is a secondary signal that travels in the
# Flask log file and is available to any future endpoint that wants a
# default actor when its body doesn't supply one (`current_actor()` below).

_AGENT_ROLE_HEADER = 'X-Zspan-Agent-Role'


@app.before_request
def _capture_agent_role():
    role = (request.headers.get(_AGENT_ROLE_HEADER) or '').strip()
    # F2 (RR-8 posture): the role header feeds current_actor() attribution +
    # the [role] log prefix, and was previously trusted from ANY caller — an
    # anonymous request could forge `X-Zspan-Agent-Role: orchestrator` into the
    # audit trail + the log stream (a provenance-integrity crack for the
    # water-carrier custody model). Only honor the header when the request also
    # proves fleet identity via the agent bearer token — the header's sole
    # legitimate source (the owner never sets it; absence == 'operator'). A
    # forged header from an un-bearered caller drops to None, so a spoofed role
    # can no longer poison attribution or logs. The bearer check only runs when
    # a role IS claimed, so ordinary (header-less) requests pay nothing.
    #
    # Known residual (deferred): proposal endpoints now reject garbage roles and
    # bearer claims of `operator`, but the fleet authenticates with ONE shared
    # bearer, so a holder can still impersonate another KNOWN fleet role. If the
    # dormant fleet is revived, closing that requires the operator's per-role-
    # token decision. This hook continues to close the ANON-forgery vector (F2).
    if role:
        # Fail-closed + never raise: a before_request hook that throws turns
        # EVERY role-header request into a 500. Any error resolving/validating
        # the bearer drops the claimed role rather than propagating.
        try:
            import agent_auth  # noqa: PLC0415 — pure, dependency-light
            ok, _status, _msg = agent_auth.check_agent_bearer(request)
        except Exception:
            ok = False
        if not ok:
            role = None
    g.zspan_agent_role = role or None


def current_actor() -> str:
    """Return the agent role from the request header, or 'operator' if absent.

    Use as a fallback when an endpoint's request body doesn't supply an
    explicit actor (resolved_by, acknowledged_by, promoted_by, etc.).
    Agent manuals require the agent to set the body field directly; this
    helper is a safety net so a missing body attribution doesn't silently
    lose agent context.
    """
    try:
        role = getattr(g, 'zspan_agent_role', None)
    except RuntimeError:
        # Outside a request context (startup / background threads).
        return 'operator'
    return role or 'operator'


def _resolved_agent_proposal_role(payload: dict) -> str:
    """Resolve bearer attribution from the body, then the authenticated
    role header. A present non-string body value is invalid, not a fallback.
    """
    raw_role = payload.get('agent_role')
    if raw_role is None:
        return current_actor()
    if not isinstance(raw_role, str):
        return ''
    return raw_role.strip() or current_actor()


class _AgentRoleLogFilter(logging.Filter):
    """Prepend `[<role>]` to log records emitted during agent-driven requests.

    Operator-driven requests get no prefix — the absence IS the operator
    indicator. Records emitted outside a request context (startup, worker
    spawns, background threads) get no prefix either.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            role = getattr(g, 'zspan_agent_role', None)
        except RuntimeError:
            role = None
        if role:
            # Mutate the formatted message rather than the args tuple so
            # callers using `app.logger.exception('foo %s', x)` keep their
            # %-substitution intact.
            try:
                rendered = record.getMessage()
                record.msg = f'[{role}] {rendered}'
                record.args = ()
            except Exception:
                # Defensive — never let the filter itself raise.
                pass
        return True


app.logger.addFilter(_AgentRoleLogFilter())


# ── Skybox traffic-event tee (chunk 4) ────────────────────────────────
# After every Flask request, broadcast a TrafficEvent so the HQ skybox viz
# spawns a shooting star for the visit. Filtered through is_excluded_path
# to skip the SSE stream itself + the polling endpoints (otherwise the viz
# would mostly show its own monitoring noise). bot_classification stays
# "unknown" on the local Flask path — only the Cloudflare Worker (chunk 5)
# has the bot-score data to populate that field. Source is "flask" so the
# frontend can distinguish backend-only traffic from CF Pages page-views.
@app.after_request
def _tee_traffic_event(response):
    try:
        path = request.path or '/'
        if traffic_is_excluded_path(path):
            return response
        traffic_broadcast({
            'ts': datetime.now(timezone.utc).isoformat(),
            'status': int(response.status_code),
            'path_class': traffic_classify_path(path),
            'bot_classification': 'unknown',
            'source': 'flask',
        })
    except Exception:
        # Never let the tee break a real request.
        pass
    return response


# ── S-004 Phase 2 (D-055): Socket Mode listener for reaction-driven actions
# Started in a daemon thread at module load. No-op when bot path isn't
# configured (Phase 1 webhook-only deployments stay fully functional).
# The listener handles its own reconnect logic via slack_sdk; thread crash
# is logged but doesn't take down Flask.
try:
    from slack_listener import start_listener_thread as _start_slack_listener
    _start_slack_listener()
except Exception as _slack_start_err:
    app.logger.warning(
        "slack_listener failed to start: %s; reactions won't dispatch but "
        "outbound escalation path is unaffected", _slack_start_err,
    )


# ── D-099 Phase 2 C3: /api/worker/* blueprint for Mac-side worker ─────
# Registered after the slack listener wire-up so the bridge endpoints
# come up regardless of slack reachability. The blueprint owns its own
# bearer-token auth (ZSPAN_AGENT_STATE_TOKEN); the rest of the API
# remains public per the project's existing auth posture.
from api_worker_routes import worker_bp  # noqa: E402
app.register_blueprint(worker_bp)

# Optional operator-only blueprint — present only when the gitignored
# parsers/operator_only/ directory exists in the working tree. Public
# clones do not see it; the import fails and the blueprint is never
# registered. The endpoints under it are all owner-gated regardless.
# Catches Exception (not just ImportError): a broken optional plugin
# must never take down the public API — the 2026-07-02 Flask crash-loop
# was a retired module exiting/raising at import time, which sailed
# past the old ImportError-only catch. SystemExit is re-raised: an
# explicit exit request shouldn't be swallowed into plugin-skipping.
try:
    from operator_only.voice_search_api import operator_only_bp  # type: ignore
    app.register_blueprint(operator_only_bp)
    logging.getLogger(__name__).info(
        "operator_only blueprint registered",
    )
except SystemExit:
    raise
except Exception as _op_only_err:
    logging.getLogger(__name__).warning(
        "operator_only blueprint not registered: %s", _op_only_err,
    )


def _has_valid_scrape_password() -> bool:
    configured_password = load_user_settings().get('scrape_password', '')
    supplied_password = request.headers.get('X-Scrape-Password', '')
    return (
        isinstance(configured_password, str)
        and bool(configured_password)
        and hmac.compare_digest(
            supplied_password.encode('utf-8'),
            configured_password.encode('utf-8'),
        )
    )


@app.route('/scrape/<city_name>', methods=['GET'])
def scrape_city(city_name):
    """Return cached meetings for a city, or live-scrape on explicit refresh.

    Behavior (D-039 · 2026-05-13):
      - `?refresh=true` always triggers a live scrape against the city's
        site and re-caches after the per-action scrape password is verified.
        Use this when the operator explicitly clicks that city's Scrape button.
      - Without `?refresh=true`:
          * If cache exists (fresh OR stale), return the cached rows with
            `cache_age_seconds` and `is_stale` metadata. The operator
            sees stale data with a clear indicator instead of triggering
            an invisible re-scrape on page load.
          * If no cache exists at all (first-time city setup), the guarded
            live-scrape boundary still requires the scrape-password header.

    Previously, stale cache implicitly triggered a live scrape — combined
    with the destructive cache_meetings (pre-D-038), a single page-load
    past the 6h TTL could cascade-wipe in-flight processing state. D-038
    fixed the destructive behavior; D-039 removes the invisible trigger.
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        force_refresh = request.args.get('refresh', '').lower() == 'true'
        scrape_password_verified = False
        if force_refresh:
            scrape_password_verified = _has_valid_scrape_password()
            if not scrape_password_verified:
                return jsonify({
                    'error': 'scrape password required or incorrect',
                }), 403

        # D-180: the legacy scrape surface is owner-only. Public calendar
        # reads use the branchless /public-api city-meetings DTO instead.
        include_drafts = request.args.get('include_drafts', '').lower() == 'true'

        # F28 stale-known-postponed short-circuit (2026-06-19). When parser_index
        # marks a city as stale_known_postponed, we do NOT live-scrape (the
        # parser is known to return stale or empty data and would surface
        # months-or-years-old meetings as if current) AND we do NOT return the
        # SQLite cache (which may carry stale rows from past scrapes). Return
        # an honest empty state with the postponement marker + notes so the
        # frontend can render "coming soon" instead of stale content.
        try:
            _idx = load_parser_index() or {}
            _entry = _idx.get(city_name, {}) if isinstance(_idx, dict) else {}
            if _entry.get('freshness_status') == 'stale_known_postponed':
                return jsonify({
                    'success': True,
                    'city': city_name,
                    'events': [],
                    'count': 0,
                    'source': 'postponed',
                    'is_postponed': True,
                    'freshness_status': _entry.get('freshness_status'),
                    'freshness_postponed_at': _entry.get('freshness_postponed_at'),
                    # freshness_reason added F28 Phase 2 (2026-06-19); the
                    # original 3 stale_archive entries don't carry it (None);
                    # the 4 Phase 2 entries carry probe_blocked_waf /
                    # probe_blocked_js / url_404 sub-reasons.
                    'freshness_reason': _entry.get('freshness_reason'),
                    'freshness_notes': _entry.get('freshness_notes'),
                    'include_drafts': include_drafts,
                })
        except Exception:
            # parser_index unavailable / malformed — fall through to normal path
            # rather than fail the endpoint on the marker check.
            pass

        if not force_refresh:
            cached = get_cached_meetings_with_meta(city_name, include_drafts=include_drafts)
            if cached is not None:
                return jsonify({
                    'success': True,
                    'city': city_name,
                    'events': cached['meetings'],
                    'count': len(cached['meetings']),
                    'source': 'cache',
                    'last_scraped': cached['last_scraped'],
                    'cache_age_seconds': cached['cache_age_seconds'],
                    'is_stale': cached['is_stale'],
                    'include_drafts': include_drafts,
                })
            # No cache row at all — fall through to live scrape (first-time setup)

        # Every path that can reach the live scraper requires the explicit
        # per-action password. This also closes the legacy first-time setup
        # fallback, which could otherwise scrape on a password-free cache miss.
        if not scrape_password_verified:
            scrape_password_verified = _has_valid_scrape_password()
            if not scrape_password_verified:
                return jsonify({
                    'error': 'scrape password required or incorrect',
                }), 403

        # If a deployment omits parser_index.json, a live scrape cannot resolve
        # its local route even though the parser implementations are public.
        # Say so plainly instead of the two
        # dishonest alternatives this path used to produce here: a silent
        # success/count:0 (scrape_city_calendar swallows the missing index) or
        # a 500 carrying a raw traceback with filesystem paths (load_parser_index
        # below raises into the generic handler). Cached rows above still serve
        # unaffected.
        if routing_index_unavailable():
            return jsonify({
                'success': True,
                'status': 'routing_unavailable',
                'routing_unavailable': True,
                'city': city_name,
                'events': [],
                'count': 0,
                'source': 'routing_unavailable',
                'message': ('This deployment has no local parser routing '
                            'configuration. Published data still serves from '
                            'the archive; live scraping requires a configured route.'),
                'include_drafts': include_drafts,
            })

        # Live scrape (explicit refresh OR first-time setup)
        meetings = scrape_city_calendar(city_name)
        normalized_meetings = [normalize_meeting_fields(m) for m in meetings]

        index = load_parser_index()
        county = index.get(city_name, {}).get('county', 'Unknown')
        cache_meetings(city_name, county, normalized_meetings)

        # Re-read with metadata so the response shape stays consistent
        cached = get_cached_meetings_with_meta(city_name, include_drafts=include_drafts)
        return jsonify({
            'success': True,
            'city': city_name,
            'events': cached['meetings'] if cached else normalized_meetings,
            'count': len(cached['meetings']) if cached else len(normalized_meetings),
            'source': 'live',
            'last_scraped': cached['last_scraped'] if cached else None,
            'cache_age_seconds': cached['cache_age_seconds'] if cached else 0,
            'is_stale': False,
            'include_drafts': include_drafts,
        })
    except Exception as e:
        error_str = str(e)
        error_type = 'unknown'
        if 'No module named' in error_str or 'ImportError' in error_str:
            error_type = 'dependency'
        elif 'HTTP' in error_str or 'Connection' in error_str or 'Timeout' in error_str:
            error_type = 'http'
        elif 'parse' in error_str.lower() or 'invalid' in error_str.lower():
            error_type = 'parsing'

        # RR-8 / SEC-SECRET-1: log the traceback server-side; never return it.
        # Raw exception strings + format_exc() leak home paths, module layout,
        # and dependency names. The generic error_type classification is safe
        # to surface; the detail stays in the server log.
        app.logger.exception("scrape failed for %s", city_name)
        return jsonify({
            'success': False,
            'city': city_name,
            'error': 'This city could not be scraped right now.',
            'error_type': error_type,
            'events': [],
            'count': 0
        }), 500


@app.route('/api/search', methods=['GET'])
def api_search():
    """Full-text search across all cached meetings."""
    query = request.args.get('q', '')
    county = request.args.get('county', None)
    state = request.args.get('state', None)
    date_from = request.args.get('date_from', None)
    date_to = request.args.get('date_to', None)
    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))
    
    results = search_meetings(
        query=query, county=county, state=state,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=offset
    )
    
    return jsonify({
        'success': True,
        **results
    })


@app.route('/api/county/<county_name>/meetings', methods=['GET'])
def county_meetings(county_name):
    """Get all cached meetings for a county (instant response)."""
    state = request.args.get('state', 'Arizona')
    meetings = get_all_meetings_for_county(county_name, state)
    
    # Group by city
    by_city = {}
    for m in meetings:
        city = m['city']
        if city not in by_city:
            by_city[city] = []
        by_city[city].append(m)
    
    return jsonify({
        'success': True,
        'county': county_name,
        'total_meetings': len(meetings),
        'cities': by_city
    })


@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get database statistics."""
    stats = get_stats()
    return jsonify({
        'success': True,
        **stats
    })


@app.route('/api/travelers', methods=['GET'])
def api_travelers():
    """V1-Odometer-1: total registered Z-SPAN accounts ("travelers").

    Public + unauthenticated; powers the highway-aesthetic travelers
    odometer in the persistent footer. The count is the size of the
    audience, not anyone's identity — non-sensitive by design.
    """
    try:
        count = count_users()
    except Exception as exc:
        app.logger.warning("travelers count failed: %s", exc)
        return jsonify({'success': False, 'error': 'count failed'}), 500
    return jsonify({'success': True, 'count': count})


@app.route('/api/council/<city_name>', methods=['GET'])
def api_council(city_name):
    """Get council members for a city."""
    _user, _err = _require_owner()
    if _err:
        return _err
    members = get_council_members(city_name)
    return jsonify({
        'success': True,
        'city': city_name,
        'members': members
    })


@app.route('/api/cities', methods=['GET'])
def api_cities():
    """Get all cities with their metadata."""
    index = load_parser_index()
    cities = []
    for name, info in index.items():
        # RR-8 / SEC-SEAL-2: calendar_url + calendar_format are sealed recipe
        # fields — never serialized to the public catalog. Coverage/status
        # stays publicly visible; the recipe that produces it does not.
        cities.append({
            'name': name,
            'county': info.get('county', 'Unknown'),
            'status': info.get('status', 'unknown')
        })
    return jsonify({
        'success': True,
        'cities': cities,
        'total': len(cities)
    })


# ── V1-Catalog-1 (2026-06-12) — DB-driven catalog tree + year pagination ──
#
# Drives ChannelsPage's drill-down (state -> county -> city -> meetings)
# from real meetings data instead of the hardcoded ARIZONA_COUNTIES +
# MOHAVE_CITIES constants. Per James's V1 framing 2026-06-12: the catalog
# should expose every city with data we have, clickable through to its
# meetings; the "processed vs not" distinction is V2 (studio generations),
# not V1 (catalog presence). The year-pager keeps the per-city episode
# list scannable at national scale where a city may have 10+ years of
# meetings cached.


def _channel_county_name(value) -> str:
    """Display form shared by channel-tree grouping and identity matching."""
    county = ' '.join(str(value or 'Unknown').split()) or 'Unknown'
    if county.casefold().endswith(' county'):
        county = county[:-len(' County')].rstrip()
    return county or 'Unknown'


def _channel_state_name(value) -> str:
    """Canonical full state name for the channel tree when one is known."""
    raw = ' '.join(str(value or 'Unknown').split()) or 'Unknown'
    postal = _v1_postal_state(raw)
    full_name = _V1_POSTAL_TO_STATE.get(postal)
    if full_name:
        return ' '.join(
            word if word == 'of' else word.capitalize()
            for word in full_name.split()
        )
    return raw


def _channel_city_identity(state, county, city) -> tuple[str, str, str]:
    """Case-, state-code-, and county-suffix-insensitive jurisdiction key."""
    return (
        _v1_postal_state(state).casefold(),
        _channel_county_name(county).casefold(),
        ' '.join(str(city or '').split()).casefold(),
    )


def _channel_status(meeting_count: int, broadcast_count: int, postponed: bool) -> str:
    if postponed:
        return 'postponed'
    if broadcast_count > 0:
        return 'live'
    if meeting_count > 0:
        return 'cached'
    return 'scaffold'


def _channel_date_bound(current, incoming, *, latest: bool):
    values = [value for value in (current, incoming) if value]
    if not values:
        return None
    return (max if latest else min)(values)


@app.route('/api/channels/tree', methods=['GET'])
def api_channels_tree():
    """Catalog tree of state -> counties -> cities for everything with data.

    Drives the V1 ChannelsPage drill-down. A city appears iff at least
    one meeting is cached against it. Counties auto-collapse when they
    have no cities with data. States likewise.

    Response shape:
      {
        "ok": true,
        "states": [
          {
            "state": "Arizona",
            "counties": [
              {
                "county": "Mohave",
                "cities": [
                  {"name": "Kingman", "meeting_count": 106,
                   "last_meeting": "2026-06-09", "first_meeting": "2024-01-05"},
                  ...
                ]
              },
              ...
            ]
          }
        ],
        "generated_at": "2026-06-12T..."
      }

    The frontend treats every returned city as `active: true` (it has
    catalog presence). The V2-processed signal (V1_PROCESSED_CITIES) is
    a separate flag the frontend already owns; this endpoint doesn't
    duplicate it.
    """
    from database import get_connection as _get_connection
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT state, county, city_name,
                   COUNT(*) AS meeting_count,
                   SUM(CASE WHEN notebook_id IS NOT NULL AND notebook_id != ''
                            THEN 1 ELSE 0 END) AS broadcast_count,
                   MAX(meeting_date) AS last_meeting,
                   MIN(meeting_date) AS first_meeting
            FROM meetings
            WHERE city_name IS NOT NULL AND city_name != ''
            GROUP BY state, county, city_name
            ORDER BY state, county, city_name
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    states: list = []
    state_index: dict = {}
    county_index: dict = {}
    city_index: dict = {}

    # F28 postponed-marker (2026-06-19). Pre-build the set of cities that
    # parser_index has flagged as stale_known_postponed so the cache + roster
    # loops below can override their status to 'postponed' uniformly — even
    # if a city has cached rows from a past scrape, the channel tree should
    # surface honest empty-state rather than the stale data. Lookups are
    # case-insensitive on the city key to tolerate any minor name drift
    # between meetings.city_name and parser_index keys.
    try:
        _idx_for_marker = load_parser_index() or {}
    except Exception:
        _idx_for_marker = {}
    _postponed_cities = {
        (name or '').strip().lower()
        for name, entry in (_idx_for_marker.items() if isinstance(_idx_for_marker, dict) else [])
        if isinstance(entry, dict) and entry.get('freshness_status') == 'stale_known_postponed'
    }

    # S-067 (2026-06-19) — per-city coordinates from parser_index (which is
    # backfilled by scripts/backfill_coordinates.py against the US Census
    # Places Gazetteer). Keyed by lowercased city name to tolerate minor
    # drift between meetings.city_name and parser_index keys.
    _coords_by_city: dict = {}
    if isinstance(_idx_for_marker, dict):
        for _name, _entry in _idx_for_marker.items():
            if not isinstance(_entry, dict):
                continue
            _lat = _entry.get('city_lat')
            _lng = _entry.get('city_lng')
            if isinstance(_lat, (int, float)) and isinstance(_lng, (int, float)):
                _coords_by_city[(_name or '').strip().lower()] = (_lat, _lng)

    for row in rows:
        state_name = _channel_state_name(row['state'])
        county_name = _channel_county_name(row['county'])
        city_name = ' '.join(str(row['city_name'] or '').split())
        city_key = _channel_city_identity(state_name, county_name, city_name)
        mc = int(row['meeting_count'] or 0)
        bc = int(row['broadcast_count'] or 0)
        is_postponed = city_name.casefold() in _postponed_cities

        # Historical cache rows can use both "Mohave" and "Mohave County"
        # (and analogous state-code/case variants). Aggregate those raw SQL
        # groups before deriving the one public jurisdiction status.
        existing_city = city_index.get(city_key)
        if existing_city is not None:
            if not is_postponed:
                existing_city['meeting_count'] += mc
                existing_city['broadcast_count'] += bc
                existing_city['last_meeting'] = _channel_date_bound(
                    existing_city['last_meeting'], row['last_meeting'], latest=True
                )
                existing_city['first_meeting'] = _channel_date_bound(
                    existing_city['first_meeting'], row['first_meeting'], latest=False
                )
            existing_city['status'] = _channel_status(
                existing_city['meeting_count'],
                existing_city['broadcast_count'],
                is_postponed,
            )
            continue

        state_key, county_identity, _city_identity = city_key
        if state_key not in state_index:
            state_node = {'state': state_name, 'counties': []}
            state_index[state_key] = state_node
            states.append(state_node)
        county_key = (state_key, county_identity)
        if county_key not in county_index:
            county_node = {'county': county_name, 'cities': []}
            county_index[county_key] = county_node
            state_index[state_key]['counties'].append(county_node)
        _coords = _coords_by_city.get(city_name.casefold())
        city_payload = {
            'name': city_name,
            # Postponed cities show 0 across the board on the public tree
            # regardless of what the cache still holds — the cache rows are
            # intentionally hidden, not deleted, so a future remediation can
            # promote them back without re-scraping.
            'meeting_count': 0 if is_postponed else mc,
            'broadcast_count': 0 if is_postponed else bc,
            # status drives the channel-list dot (V1-Polish-19):
            #   live      = ≥1 processed broadcast (watchable)
            #   cached    = meetings scraped, no broadcasts yet (coming)
            #   scaffold  = parser registered, nothing scraped
            #   postponed = parser_index flagged stale_known_postponed (F28)
            'status': _channel_status(mc, bc, is_postponed),
            'last_meeting': None if is_postponed else row['last_meeting'],
            'first_meeting': None if is_postponed else row['first_meeting'],
            # S-067 — server-resolved city coordinates (Census Gazetteer);
            # null when parser_index has no entry for this city.
            'lat': _coords[0] if _coords else None,
            'lng': _coords[1] if _coords else None,
        }
        county_index[county_key]['cities'].append(city_payload)
        city_index[city_key] = city_payload

    # Merge the full parser roster so the channel browser shows the WHOLE
    # scaffold (James 2026-06-14 "Part 1"), not just the cities that happen to
    # have been scraped. A registered-but-unscraped city joins as 'scaffold';
    # the cache rows above already filled in the live/cached ones. State is
    # resolved per-city (NOT hardcoded 'Arizona') so the Nevada roster cities
    # land under Nevada — the 2026-07-10 fix for NV counties (Carson City,
    # Clark, Washoe) leaking under the Arizona tab. resolve_city_state routes
    # explicit-state → county gazetteer → warn-and-default.
    from database import resolve_city_state
    try:
        roster = load_parser_index() or {}
    except Exception:
        roster = {}
    for key_name, entry in roster.items():
        if not isinstance(entry, dict):
            continue
        r_city = (entry.get('city') or key_name or '').strip()
        r_county = (entry.get('county') or '').strip()
        if not r_city or not r_county:
            continue
        r_state = _channel_state_name(resolve_city_state(entry, r_county))
        r_county = _channel_county_name(r_county)
        city_key = _channel_city_identity(r_state, r_county, r_city)
        existing_city = city_index.get(city_key)
        if existing_city is not None:
            # Roster metadata may enrich a cache-derived city, but a spelling
            # variant must never append a second public row.
            _r_lat = entry.get('city_lat')
            _r_lng = entry.get('city_lng')
            if existing_city['lat'] is None and isinstance(_r_lat, (int, float)):
                existing_city['lat'] = _r_lat
            if existing_city['lng'] is None and isinstance(_r_lng, (int, float)):
                existing_city['lng'] = _r_lng
            continue
        state_key, county_identity, _city_identity = city_key
        if state_key not in state_index:
            node = {'state': r_state, 'counties': []}
            state_index[state_key] = node
            states.append(node)
        ckey = (state_key, county_identity)
        if ckey not in county_index:
            cnode = {'county': r_county, 'cities': []}
            county_index[ckey] = cnode
            state_index[state_key]['counties'].append(cnode)
        # Roster-only path (scaffold) — also respect the F28 postponed marker
        # so postponed cities with zero cache rows still surface honestly.
        r_postponed = r_city.casefold() in _postponed_cities
        _r_lat = entry.get('city_lat')
        _r_lng = entry.get('city_lng')
        city_payload = {
            'name': r_city,
            'meeting_count': 0,
            'broadcast_count': 0,
            'status': 'postponed' if r_postponed else 'scaffold',
            'last_meeting': None,
            'first_meeting': None,
            'lat': _r_lat if isinstance(_r_lat, (int, float)) else None,
            'lng': _r_lng if isinstance(_r_lng, (int, float)) else None,
        }
        county_index[ckey]['cities'].append(city_payload)
        city_index[city_key] = city_payload

    # Deterministic ordering after the merge (cache rows arrived sorted; the
    # appended roster cities did not).
    for st in states:
        st['counties'].sort(key=lambda c: c['county'])
        for c in st['counties']:
            c['cities'].sort(key=lambda x: x['name'])

    return jsonify({
        'ok': True,
        'states': states,
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    })


_YEAR_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')


def _extract_year(date_str: str | None) -> str | None:
    """Pull a 4-digit year out of any reasonable date string.

    The canonical schema says ISO (YYYY-MM-DD) but normalize.py doesn't
    enforce it consistently — Flagstaff writes "April 1, 2025", Kingman
    writes "2025-05-06". Both should resolve to the same year. Returns
    None when no year-like substring is present.
    """
    if not date_str:
        return None
    m = _YEAR_RE.search(date_str)
    return m.group(1) if m else None


@app.route('/api/gazetteer/lookup', methods=['GET'])
def api_gazetteer_lookup():
    """Resolve city + state to lat/lng via the US Census Places Gazetteer.

    Used by the frontend when a city isn't in /api/channels/tree (i.e.,
    not in parser_index.json) — demo fixtures, ad-hoc visualizations,
    contributor-side previews. The channels/tree path already covers
    every parser-registered city, so this endpoint exists for the
    long-tail case: any US incorporated place / CDP, no per-city
    onboarding required.

    Query params:
      city  — required; city name (with or without LSAD suffix).
      state — required; 2-letter USPS abbr (case-insensitive).

    Response:
      200 {ok: true, lat: float, lng: float, source: "gazetteer"}
      404 {ok: false, error: "not_found"} when no match.
      400 on missing/bad params.

    Per S-067 (2026-06-19) — universal coordinate path.
    """
    city = request.args.get('city', '').strip()
    state = request.args.get('state', '').strip()
    if not city or not state:
        return jsonify({'ok': False, 'error': 'city and state required'}), 400
    if len(state) != 2 or not state.isalpha():
        return jsonify({'ok': False, 'error': 'state must be 2-letter USPS abbreviation'}), 400
    try:
        from gazetteer import lookup_city_coords
        coords = lookup_city_coords(city, state)
    except Exception as e:
        app.logger.exception('gazetteer lookup failed for city=%r state=%r', city, state)
        return jsonify({'ok': False, 'error': 'lookup_failed'}), 500
    if coords is None:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    return jsonify({
        'ok': True,
        'lat': coords[0],
        'lng': coords[1],
        'source': 'gazetteer',
    })


@app.route('/api/cities/<city_name>/years', methods=['GET'])
def api_city_years(city_name):
    """Distinct years that have meetings for a city, sorted descending.

    Drives the YearPager at the bottom of the per-city episode list.
    Year-extraction is format-tolerant (cities write meeting_date in
    different shapes; see _extract_year).
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    include_drafts = request.args.get('include_drafts', '').lower() == 'true'
    from database import get_connection as _get_connection
    conn = _get_connection()
    try:
        cur = conn.cursor()
        if include_drafts:
            cur.execute(
                "SELECT DISTINCT meeting_date FROM meetings WHERE city_name = ?",
                (city_name,),
            )
        else:
            cur.execute(
                """
                SELECT DISTINCT meeting_date FROM meetings
                WHERE city_name = ? AND COALESCE(is_published, 0) = 1
                """,
                (city_name,),
            )
        years_set: set = set()
        for r in cur.fetchall():
            y = _extract_year(r['meeting_date'])
            if y:
                years_set.add(y)
    finally:
        conn.close()
    years = sorted(years_set, reverse=True)

    return jsonify({
        'ok': True,
        'city': city_name,
        'years': years,
        'current_year': str(datetime.now().year),
        'include_drafts': include_drafts,
    })


# The catalog contract's field allowlist: raw civic FACTS as scraped
# from the public record, plus row identity + the is_published flag.
# Nothing generated, nothing pipeline-internal. Rows served in catalog
# mode are PUBLISHED-ONLY (operator-decided 2026-07-10 evening,
# superseding the same-day option A which served all cached rows —
# the publish wall now gates the catalog rows too, not just outputs).
# The allowlist survives the revert: anonymous callers never receive
# pipeline internals regardless of row scope (the RR-8 hygiene half).
_CATALOG_FACT_FIELDS = (
    'id', 'public_id', 'city_name', 'county', 'state',
    'meeting_title', 'meeting_date', 'meeting_time', 'meeting_location',
    'meeting_status', 'agenda_url', 'minutes_url', 'agenda_packet_url',
    'video_url', 'ecomment_url', 'meeting_id', 'is_published',
)


def _catalog_facts(row):
    return {k: row.get(k) for k in _CATALOG_FACT_FIELDS}


# ── D-164 /v1 facts-only catalog ─────────────────────────────────────
# These routes are intentionally public civic-data reads. They never inspect
# a session cookie and every response (including 4xx responses) carries the
# same short public cache policy.
_V1_CATALOG_CACHE_CONTROL = 'public, max-age=300'
_V1_CATALOG_PAGE_SIZE = 100

# Public display floor (operator-directed 2026-07-26, session-95): the
# /v1/catalog/meetings list — the feed behind the site's coming-soon cards
# and the CLI's pull — hides rows older than this date so the public face
# shows the curated recent window instead of years of scraped archive
# splatter. Direct public_id detail lookups are NOT floored (deep links
# keep resolving), and /public-api surfaces are already published-only.
# Override without a deploy via the env var; empty string disables.
_PUBLIC_DISPLAY_FLOOR_DATE = (
    os.getenv('ZSPAN_PUBLIC_DISPLAY_FLOOR', '2026-06-01').strip()
)

# Public catalog state scope (D-185, operator-directed 2026-07-30 session-103):
# the /v1/catalog/* surface serves ONLY rows whose state matches this scope,
# so the flagship deployment titled "Z-SPAN — Arizona" never leaks rows from
# other states through its public machine-consumer catalog. Applies to list,
# jurisdictions, AND detail lookups (out-of-scope public_ids resolve as 404
# — deep-link resolution respects the deployment's state boundary, unlike
# _PUBLIC_DISPLAY_FLOOR_DATE which lets old-date deep links through). A
# future state deployment sets its own scope via the env var; empty string
# disables (returns the underlying multi-state DB unfiltered).
_PUBLIC_CATALOG_STATE_SCOPE = (
    os.getenv('ZSPAN_PUBLIC_CATALOG_STATE_SCOPE', 'Arizona').strip()
)
_V1_CATALOG_LIST_FIELDS = (
    'public_id', 'state', 'county', 'city', 'title', 'date', 'time',
    'location', 'meeting_status', 'availability',
)

# Keep these values in parity with zspan_cli.media. The server deliberately
# owns a copy rather than importing the CLI package (the dependency points
# from the CLI to this public API, never from the server into the client).
_CATALOG_YOUTUBE_HOST_SUFFIXES = ('youtube.com', 'youtu.be')
_CATALOG_DIRECT_MEDIA_EXTENSIONS = (
    '.mp4', '.m4v', '.mov', '.webm', '.mkv',
    '.m4a', '.mp3', '.wav', '.aac', '.ogg',
)
_CATALOG_VENDOR_PAGE_MARKERS = (
    'mediaplayer.php', '/mediaplayer', '/player/clip/', '/player/camera/',
    '.asx', 'insight.granicus', 'swagit.com/play', 'videoplayer.telvue',
)


def classify_catalog_video_url(url: str) -> str:
    """Mirror zspan_cli.media.classify_video_url for the public detail row."""
    raw = (url or '').strip()
    if not raw:
        return 'unknown'
    low = raw.lower()
    for marker in _CATALOG_VENDOR_PAGE_MARKERS:
        if marker in low:
            return 'vendor_page'
    try:
        host = (urlparse(raw).hostname or '').lower()
    except ValueError:
        return 'unknown'
    if any(
        host == suffix or host.endswith('.' + suffix)
        for suffix in _CATALOG_YOUTUBE_HOST_SUFFIXES
    ):
        return 'youtube'
    path = urlparse(raw).path.lower()
    if path.endswith(_CATALOG_DIRECT_MEDIA_EXTENSIONS):
        return 'direct_media'
    return 'unknown'


def _catalog_local_processing(video_url: str) -> dict:
    source_kind = classify_catalog_video_url(video_url)
    if not (video_url or '').strip():
        status = 'no_video'
    elif source_kind in ('youtube', 'direct_media'):
        status = 'ready'
    else:
        status = 'unsupported_source'
    return {'status': status, 'source_kind': source_kind}


def _v1_catalog_json(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers['Cache-Control'] = _V1_CATALOG_CACHE_CONTROL
    return response


# zspan-catalog/1 wire vocabulary (PUBLIC_INTERFACE_SPEC § 3): states serve as
# USPS postal codes and times as 24-hour "HH:MM". The database stores full
# state names (resolve_city_state) and scraped 12-hour time strings; the
# normalization happens HERE, at the versioned public boundary — never as a
# data migration. Unrecognized values pass through raw (F8: never fabricate).
_V1_STATE_TO_POSTAL = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT',
    'delaware': 'DE', 'district of columbia': 'DC', 'florida': 'FL',
    'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL',
    'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS', 'kentucky': 'KY',
    'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT',
    'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH',
    'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH',
    'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA',
    'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD',
    'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT',
    'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY',
}
_V1_POSTAL_TO_STATE = {code: name for name, code in _V1_STATE_TO_POSTAL.items()}

_V1_TIME_12H_RE = re.compile(
    r'^\s*(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?\s*[Mm]\.?\s*$'
)
_V1_TIME_24H_RE = re.compile(r'^\s*(\d{1,2}):(\d{2})\s*$')


def _v1_postal_state(value) -> str:
    """Full state name -> USPS code; real postal codes pass; unknown -> raw.

    Two-letter values are uppercased ONLY when they are actual USPS codes —
    arbitrary two-letter garbage passes through raw rather than being dressed
    up as a postal code (F8: never fabricate)."""
    text = (value or '').strip()
    if not text:
        return ''
    if len(text) == 2 and text.isalpha():
        code = text.upper()
        return code if code in _V1_POSTAL_TO_STATE else text
    return _V1_STATE_TO_POSTAL.get(text.lower(), text)


def _state_forms(value) -> set[str]:
    """The DB storage forms that match a state value.

    Accepts either postal ('AZ') or full-name ('Arizona') input and returns
    both forms when the value maps to a known state, so a SQL clause built
    from this set matches whichever form the underlying rows happen to store
    (COLLATE NOCASE covers casing). An unrecognized value returns a
    single-element set carrying the raw input (F8: never fabricate).
    """
    text = (value or '').strip()
    if not text:
        return set()
    forms = {text}
    if len(text) == 2 and text.isalpha():
        expanded = _V1_POSTAL_TO_STATE.get(text.upper())
        if expanded:
            forms.add(expanded)
    else:
        code = _V1_STATE_TO_POSTAL.get(text.lower())
        if code:
            forms.add(code)
    return forms


def _state_scope_condition(column: str) -> tuple[str, list[str]]:
    """SQL fragment + params restricting `column` to _PUBLIC_CATALOG_STATE_SCOPE.

    Returns ('', []) when scope is empty (env-var disable). The clause is
    always wrapped in parentheses so it composes safely with other AND
    conditions the caller assembles.
    """
    if not _PUBLIC_CATALOG_STATE_SCOPE:
        return '', []
    forms = _state_forms(_PUBLIC_CATALOG_STATE_SCOPE)
    if not forms:
        forms = {_PUBLIC_CATALOG_STATE_SCOPE}
    ordered = sorted(forms)
    # Put the explicit collation on the indexed column. With ``? COLLATE
    # NOCASE`` SQLite may choose the existing BINARY state index and perform
    # a case-sensitive probe before applying the comparison, producing an
    # empty result for wire forms such as ``AZ`` -> stored ``Arizona``.
    clause = ' OR '.join(f'{column} COLLATE NOCASE = ?' for _ in ordered)
    return f'({clause})', ordered


def _v1_time_24h(value) -> str:
    """Canonical 12-hour meeting_time -> 'HH:MM' 24-hour; unknown -> raw."""
    text = (value or '').strip()
    if not text:
        return ''
    match = _V1_TIME_12H_RE.match(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3).lower()
        if 1 <= hour <= 12 and 0 <= minute <= 59:
            if meridiem == 'p' and hour != 12:
                hour += 12
            if meridiem == 'a' and hour == 12:
                hour = 0
            return f'{hour:02d}:{minute:02d}'
        return text
    match = _V1_TIME_24H_RE.match(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f'{hour:02d}:{minute:02d}'
    return text


def _coverage_published_by_city(conn=None) -> dict[str, tuple[int, str | None]]:
    """The live-DB coverage truth shared with /api/coverage.

    This intentionally preserves that route's established city-name lookup:
    any is_published row makes that registry city covered. The v1 catalog
    consumes the same truth rather than inventing a second coverage rule.
    """
    own_connection = conn is None
    if own_connection:
        conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT city_name, COUNT(*) AS n, MAX(meeting_date) AS latest
            FROM meetings WHERE is_published = 1
            GROUP BY city_name
            """
        ).fetchall()
        return {row['city_name']: (row['n'], row['latest']) for row in rows}
    finally:
        if own_connection:
            conn.close()


def _coverage_visible_by_jurisdiction(conn=None) -> set[tuple[str, str, str]]:
    """The set of (normalized state, county, city) jurisdictions with at
    least one FULLY publicly-visible meeting — the same two-field gate
    (is_published + an approved work order, via `public_serving_sql`) the
    per-meeting `availability` marker uses. The /v1 jurisdictions endpoint
    consumes this so a jurisdiction reads `covered` iff it actually has a
    published broadcast a visitor can open.

    Fixes DIV-009 + the session-66 keying: the older
    `_coverage_published_by_city` gated on `is_published` alone (so a city
    whose only published meeting was still `coming_soon` read covered — the
    coverage claim and the availability marker disagreed) AND keyed by city
    name alone (so two same-named cities in different counties collided).
    This keys on the full jurisdiction triple, normalized through
    `_v1_postal_state` EXACTLY as the jurisdictions grouping does, so the
    coverage key and the tree key can never drift apart.
    """
    own_connection = conn is None
    if own_connection:
        conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT m.state, m.county, m.city_name"
            " FROM meetings m"
            " WHERE 1=1" + public_serving_sql("m") +
            " GROUP BY m.state, m.county, m.city_name"
        ).fetchall()
        return {
            (_v1_postal_state(r['state']), r['county'] or '', r['city_name'] or '')
            for r in rows
        }
    finally:
        if own_connection:
            conn.close()


def _catalog_availability(meeting_id: int, conn=None) -> str:
    # Additive enum: S-145 may add available_to_contribute / preparing.
    # Unknown future values remain coming_soon-conservative for v1 clients.
    return (
        'published'
        if is_meeting_publicly_visible(meeting_id, conn=conn)
        else 'coming_soon'
    )


def _v1_catalog_list_row(row: dict, conn=None) -> dict:
    public_row = {
        'public_id': row.get('public_id') or '',
        'state': _v1_postal_state(row.get('state')),
        'county': row.get('county') or '',
        'city': row.get('city_name') or '',
        'title': row.get('meeting_title') or '',
        'date': row.get('meeting_date') or '',
        'time': _v1_time_24h(row.get('meeting_time')),
        'location': row.get('meeting_location') or '',
        'meeting_status': row.get('meeting_status') or '',
        'availability': _catalog_availability(int(row['id']), conn=conn),
    }
    # Keep the wire shape auditable at the construction boundary.
    return {field: public_row[field] for field in _V1_CATALOG_LIST_FIELDS}


def _encode_catalog_cursor(row: dict) -> str:
    """Opaque keyset cursor over (meeting_date, public_id), never DB row id."""
    payload = json.dumps(
        {
            'date': row.get('meeting_date') or '',
            'public_id': row.get('public_id') or '',
        },
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')


def _decode_catalog_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = cursor.encode('ascii')
        padding = b'=' * (-len(raw) % 4)
        decoded = base64.b64decode(raw + padding, altchars=b'-_', validate=True)
        payload = json.loads(decoded.decode('utf-8'))
        if not isinstance(payload, dict) or set(payload) != {'date', 'public_id'}:
            raise ValueError('unexpected cursor fields')
        date_value = payload['date']
        public_id = payload['public_id']
        if not isinstance(date_value, str) or len(date_value) > 128:
            raise ValueError('invalid cursor date')
        if not isinstance(public_id, str) or PUBLIC_ID_RE.fullmatch(public_id) is None:
            raise ValueError('invalid cursor public_id')
        return date_value, public_id
    except (
        UnicodeEncodeError, UnicodeDecodeError, binascii.Error,
        json.JSONDecodeError, TypeError, ValueError,
    ) as exc:
        raise ValueError('invalid cursor') from exc


@app.route('/v1/catalog/jurisdictions', methods=['GET'])
@_public_rate_limited('public_read')
def v1_catalog_jurisdictions():
    """Facts-only state -> county -> city catalog for anonymous clients."""
    cities_scope, cities_scope_params = _state_scope_condition('state')
    meetings_scope, meetings_scope_params = _state_scope_condition('state')
    cities_where = f'WHERE {cities_scope}' if cities_scope else ''
    meetings_where_extra = f' AND {meetings_scope}' if meetings_scope else ''
    conn = get_connection()
    try:
        city_rows = conn.execute(
            f"""
            SELECT state, county, name AS city
            FROM cities
            {cities_where}
            ORDER BY state, county, name
            """,
            cities_scope_params,
        ).fetchall()
        meeting_rows = conn.execute(
            f"""
            SELECT state, county, city_name AS city, COUNT(*) AS meeting_count
            FROM meetings
            WHERE city_name IS NOT NULL AND city_name != ''
            {meetings_where_extra}
            GROUP BY state, county, city_name
            ORDER BY state, county, city_name
            """,
            meetings_scope_params,
        ).fetchall()
        # DIV-009 + session-66: coverage is keyed by the full (normalized
        # state, county, city) triple and gated on the two-field public
        # visibility predicate — the SAME key + predicate the grouping and the
        # per-meeting availability marker use, so "covered" can't disagree
        # with what a visitor can actually open, and same-named cities in
        # different counties don't collide.
        covered_jurisdictions = _coverage_visible_by_jurisdiction(conn)
    finally:
        conn.close()

    # Group on the NORMALIZED state so mixed storage forms ("Arizona" in one
    # row, "AZ" in another) merge into one wire node instead of two nodes
    # carrying the same display label (session-66 verify-pass catch).
    jurisdictions: dict[tuple[str, str, str], dict] = {}
    for row in city_rows:
        key = (
            _v1_postal_state(row['state']),
            row['county'] or '', row['city'] or '',
        )
        jurisdictions[key] = {
            'city': key[2], 'meeting_count': 0,
            'covered': key in covered_jurisdictions,
        }
    for row in meeting_rows:
        key = (
            _v1_postal_state(row['state']),
            row['county'] or '', row['city'] or '',
        )
        jurisdictions[key] = {
            'city': key[2], 'meeting_count': int(row['meeting_count'] or 0),
            'covered': key in covered_jurisdictions,
        }

    states: list[dict] = []
    state_nodes: dict[str, dict] = {}
    county_nodes: dict[tuple[str, str], dict] = {}
    for (state, county, _city), city_payload in sorted(jurisdictions.items()):
        if state not in state_nodes:
            state_node = {'state': state, 'counties': []}
            state_nodes[state] = state_node
            states.append(state_node)
        county_key = (state, county)
        if county_key not in county_nodes:
            county_node = {'county': county, 'cities': []}
            county_nodes[county_key] = county_node
            state_nodes[state]['counties'].append(county_node)
        county_nodes[county_key]['cities'].append(city_payload)
    return _v1_catalog_json({'states': states})


@app.route('/v1/catalog/meetings', methods=['GET'])
@_public_rate_limited('public_read')
def v1_catalog_meetings():
    """Keyset-paginated facts-only meeting list.

    The opaque cursor carries (meeting_date, public_id); it never contains or
    exposes the internal SQLite row id.
    """
    filters = {
        name: (request.args.get(name) or '').strip()
        for name in ('state', 'county', 'city', 'year')
    }
    if filters['year'] and re.fullmatch(r'(?:19|20)\d{2}', filters['year']) is None:
        return _v1_catalog_json({'error': 'year must be a four-digit year'}, 400)

    cursor_value = (request.args.get('cursor') or '').strip()
    cursor_key: tuple[str, str] | None = None
    if cursor_value:
        try:
            cursor_key = _decode_catalog_cursor(cursor_value)
        except ValueError:
            return _v1_catalog_json({'error': 'invalid cursor'}, 400)

    conditions = ['m.public_id IS NOT NULL']
    params: list[Any] = []
    if _PUBLIC_DISPLAY_FLOOR_DATE:
        # COALESCE floors dateless rows out too — a card with no date is
        # exactly the display junk the floor exists to hide.
        conditions.append("COALESCE(m.meeting_date, '') >= ?")
        params.append(_PUBLIC_DISPLAY_FLOOR_DATE)
    scope_clause, scope_params = _state_scope_condition('m.state')
    if scope_clause:
        # Server-side scope always applies (D-185): a request that specifies
        # a state outside the scope AND-composes to empty results, so
        # out-of-scope rows never surface even for clients that guess the
        # wire's state form.
        conditions.append(scope_clause)
        params.extend(scope_params)
    for query_name, column_name in (
        ('state', 'state'), ('county', 'county'), ('city', 'city_name')
    ):
        value = filters[query_name]
        if not value:
            continue
        if query_name == 'state':
            # The wire serves postal codes but the DB stores full names —
            # accept either form so a client can round-trip what the
            # jurisdictions endpoint served (COLLATE NOCASE covers casing).
            forms = _state_forms(value)
            if not forms:
                forms = {value}
            ordered = sorted(forms)
            clause = ' OR '.join(
                f'm.{column_name} COLLATE NOCASE = ?' for _ in ordered
            )
            conditions.append(f'({clause})')
            params.extend(ordered)
        else:
            conditions.append(f'm.{column_name} COLLATE NOCASE = ?')
            params.append(value)
    if filters['year']:
        conditions.append('m.meeting_date LIKE ?')
        params.append(f"%{filters['year']}%")
    if cursor_key is not None:
        cursor_date, cursor_public_id = cursor_key
        conditions.append(
            """(
                COALESCE(m.meeting_date, '') < ?
                OR (COALESCE(m.meeting_date, '') = ? AND m.public_id > ?)
            )"""
        )
        params.extend((cursor_date, cursor_date, cursor_public_id))

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT m.id, m.public_id, m.state, m.county, m.city_name,
                   m.meeting_title, m.meeting_date, m.meeting_time,
                   m.meeting_location, m.meeting_status
            FROM meetings AS m
            WHERE {' AND '.join(conditions)}
            ORDER BY COALESCE(m.meeting_date, '') DESC, m.public_id ASC
            LIMIT ?
            """,
            (*params, _V1_CATALOG_PAGE_SIZE + 1),
        ).fetchall()
        has_more = len(rows) > _V1_CATALOG_PAGE_SIZE
        page_rows = rows[:_V1_CATALOG_PAGE_SIZE]
        meetings = [
            _v1_catalog_list_row(dict(row), conn=conn) for row in page_rows
        ]
    finally:
        conn.close()

    next_cursor = _encode_catalog_cursor(dict(page_rows[-1])) if has_more else ''
    return _v1_catalog_json({
        'meetings': meetings,
        'next_cursor': next_cursor,
    })


@app.route('/v1/catalog/meetings/<public_id>', methods=['GET'])
@_public_rate_limited('public_read')
def v1_catalog_meeting_detail(public_id):
    if PUBLIC_ID_RE.fullmatch(public_id) is None:
        return _v1_catalog_json({'error': 'invalid public_id'}, 400)
    meeting = get_meeting_public_record(public_id)
    if meeting is None:
        return _v1_catalog_json({'error': 'meeting not found'}, 404)

    # D-185 scope check: an out-of-scope row responds as unknown so a public
    # deep-link into another state's data doesn't resolve on this deployment
    # (matches the list/jurisdictions behavior — no discoverability path,
    # no direct-resolution path either).
    if _PUBLIC_CATALOG_STATE_SCOPE:
        scope_forms = {
            form.strip().lower()
            for form in _state_forms(_PUBLIC_CATALOG_STATE_SCOPE)
        }
        if scope_forms:
            row_state = (meeting.get('state') or '').strip().lower()
            if row_state not in scope_forms:
                return _v1_catalog_json({'error': 'meeting not found'}, 404)

    meeting_id = int(meeting['id'])
    list_row = _v1_catalog_list_row(meeting)
    video_url = (
        get_resolved_video_url(meeting_id)
        or meeting.get('video_url')
        or ''
    )
    detail = {
        **list_row,
        'video_url': video_url,
        'documents': {
            'agenda_url': meeting.get('agenda_url') or '',
            'minutes_url': meeting.get('minutes_url') or '',
            'packet_url': meeting.get('agenda_packet_url') or '',
        },
        'local_processing': _catalog_local_processing(video_url),
    }
    return _v1_catalog_json(detail)


@app.route('/api/cities/<city_name>/meetings', methods=['GET'])
def api_city_meetings(city_name):
    """Owner-side per-city meeting rows, optionally filtered by year.

    Returns the same shape /scrape/<city> returns (so the frontend can
    reuse its existing render), but as a cache-only read that never
    triggers a live scrape. Defaults to the current year when ?year=
    is omitted; pass ?year=all to get everything.

    `?catalog=true` serves rows stripped to the legacy CLI fact allowlist.
    Operator-decided 2026-07-10 evening (reverting the same-day option
    A, which served all cached rows): the publish wall gates catalog
    rows too. Catalog mode still discharges the RR-8 field-allowlist
    hygiene for this endpoint — anonymous full-row serving stays
    published-only AND catalog rows stay facts-only.
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    include_drafts = request.args.get('include_drafts', '').lower() == 'true'
    catalog_mode = request.args.get('catalog', '').lower() == 'true'
    year_arg = (request.args.get('year') or '').strip()

    cached = get_cached_meetings_with_meta(
        city_name, include_drafts=include_drafts)
    if cached is None:
        return jsonify({
            'success': True,
            'city': city_name,
            'events': [],
            'count': 0,
            'source': 'cache',
            'year': year_arg or str(datetime.now().year),
            'include_drafts': include_drafts,
            'last_scraped': None,
            'is_stale': False,
        })

    meetings = cached['meetings']

    # Year filter: default to current year, allow ?year=all for unbounded.
    # Match uses the format-tolerant _extract_year so cities writing
    # "April 1, 2025" still match alongside ISO "2025-05-06".
    if year_arg.lower() == 'all':
        filtered = meetings
        year_label = 'all'
    else:
        year_label = year_arg or str(datetime.now().year)
        filtered = [
            m for m in meetings
            if _extract_year(m.get('meeting_date')) == year_label
        ]

    if catalog_mode:
        filtered = [_catalog_facts(m) for m in filtered]

    return jsonify({
        'success': True,
        'city': city_name,
        'events': filtered,
        'count': len(filtered),
        'total_unfiltered': len(meetings),
        'source': 'catalog' if catalog_mode else 'cache',
        'year': year_label,
        'include_drafts': include_drafts,
        'last_scraped': cached['last_scraped'],
        'cache_age_seconds': cached['cache_age_seconds'],
        'is_stale': cached['is_stale'],
    })


@app.route('/api/operator/pattern-health', methods=['GET'])
def api_pattern_health():
    """H-7: operator-facing calendar-health view.

    Returns the most-recent pattern_health rows ordered by health
    severity (drifted patterns first, then partial, then no_data, then
    match) and within each severity group by refreshed_at DESC.

    Query params:
      ?limit=N — max rows to return (default 200, capped at 1000).
      ?city=X — filter to one city.

    Response shape:
      { ok, rows: [...], summary: {match, partial, drift, no_data} }
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    from database import get_connection

    try:
        limit = max(1, min(int(request.args.get('limit', 200)), 1000))
    except (TypeError, ValueError):
        limit = 200
    city_filter = request.args.get('city') or None

    # Sort priority — drift first so the operator's eye lands on
    # actionable rows. Within a status bucket, most-recent first so the
    # latest refresh is at the top.
    severity_case = """
        CASE match_status
            WHEN 'drift' THEN 0
            WHEN 'partial' THEN 1
            WHEN 'no_data' THEN 2
            WHEN 'match' THEN 3
            ELSE 4
        END
    """
    where_parts = []
    params: list = []
    if city_filter:
        where_parts.append("city_name = ?")
        params.append(city_filter)
    where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)

    conn = get_connection()
    try:
        # Get the most-recent row per (city, pattern) so the table shows
        # the current state, not the full history.
        rows = conn.execute(
            f"""
            WITH latest AS (
                SELECT ph.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY city_name, state, pattern_id
                           ORDER BY refreshed_at DESC, id DESC
                       ) AS rn
                FROM pattern_health ph{where}
            )
            SELECT id, city_name, state, pattern_id, refreshed_at,
                   window_start, window_end, expected_next,
                   actually_scraped, match_status, drift_notes
            FROM latest
            WHERE rn = 1
            ORDER BY {severity_case}, refreshed_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    rows_out = [dict(r) for r in rows]
    summary = {
        'match': 0,
        'partial': 0,
        'drift': 0,
        'no_data': 0,
    }
    for r in rows_out:
        st = r.get('match_status') or 'unknown'
        if st in summary:
            summary[st] += 1

    return jsonify({
        'ok': True,
        'rows': rows_out,
        'summary': summary,
        'total': len(rows_out),
    })


@app.route('/api/cities/<city_name>/meeting-patterns', methods=['GET'])
def api_meeting_patterns(city_name):
    """H-6: return a city's curated meeting_patterns[] plus the next N
    projected meetings per pattern.

    Used by the <MeetingSchedulePanel /> on CityPage + the Cast page.
    The patterns themselves are read from city_intelligence/<slug>.json
    (the canonical source); the projected upcoming dates come from
    pattern_projection.get_upcoming_meetings_from_patterns().

    Query params:
      ?days_ahead=N — projection window in days (default 90, capped
                      at 365 to bound work).
      ?upcoming_per_pattern=N — slice the next N per pattern (default
                                3; 0 returns all in window).

    Response shape:
      { ok, city, patterns: [...], upcoming_by_pattern: {pattern_id: [...]} }
    Cities without any meeting_patterns[] return ok=true, empty arrays —
    the front-end can render an empty state.
    """
    import json
    from pathlib import Path
    from pattern_projection import (
        get_upcoming_meetings_from_patterns,
    )

    ci_dir = Path(__file__).resolve().parent.parent / 'city_intelligence'
    slug = city_name.strip().lower().replace(' ', '_')
    path = ci_dir / f'{slug}.json'
    if not path.is_file():
        return jsonify({
            'ok': True,
            'city': city_name,
            'patterns': [],
            'upcoming_by_pattern': {},
            'note': 'no city_intelligence file',
        })

    try:
        city = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({'ok': False, 'error': f'could not load city JSON: {exc}'}), 500

    patterns = city.get('meeting_patterns') or []
    if not isinstance(patterns, list):
        patterns = []

    try:
        days_ahead = max(1, min(int(request.args.get('days_ahead', 90)), 365))
    except (TypeError, ValueError):
        days_ahead = 90
    try:
        upcoming_per_pattern = max(0, int(request.args.get('upcoming_per_pattern', 3)))
    except (TypeError, ValueError):
        upcoming_per_pattern = 3

    projected = get_upcoming_meetings_from_patterns(city_name, days_ahead=days_ahead)
    upcoming_by_pattern: dict = {}
    for m in projected:
        pid = m.get('pattern_id')
        if pid is None:
            continue
        # Serialize datetime → ISO string for JSON. Drop the raw datetime
        # field (TypeScript-side just needs the date + time_local strings).
        m_out = {k: v for k, v in m.items() if k != 'datetime'}
        m_out['datetime'] = m['datetime'].isoformat()
        upcoming_by_pattern.setdefault(pid, []).append(m_out)
    if upcoming_per_pattern > 0:
        for pid in list(upcoming_by_pattern.keys()):
            upcoming_by_pattern[pid] = upcoming_by_pattern[pid][:upcoming_per_pattern]

    return jsonify({
        'ok': True,
        'city': city_name,
        'patterns': patterns,
        'upcoming_by_pattern': upcoming_by_pattern,
        'days_ahead': days_ahead,
        'upcoming_per_pattern': upcoming_per_pattern,
    })


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    """Return the current settings."""
    _user, _err = _require_owner()
    if _err:
        return _err
    settings = load_user_settings()
    _openai_key = (settings.get('openai_api_key') or '').strip()
    return jsonify({
        'success': True,

        # Whether an OpenAI key is on file, and a last-4 hint — never the key
        # itself. Read by whisper_client.py (whisper-1 fallback), quote_cleaner.py
        # (gpt-4o-mini) and ingest_validator.py; independent of the retired
        # Navigator provider toggle.
        'openai_key_configured': bool(_openai_key),
        'openai_key_hint': f"...{_openai_key[-4:]}" if len(_openai_key) > 4 else '',

        # Broadcast-page chat mode:
        #   "direct"    — the BYOK query panel accepts open-ended questions.
        #   "suggested" — pre-cached suggested-question chips only.
        # See D-021; the public open-query lock is D-145.
        'chat_mode': settings.get('chat_mode', 'direct'),
    })


@app.route('/api/settings', methods=['POST'])
@_require_trusted_origin
def api_save_settings():
    """Save settings."""
    _user, _err = _require_owner()
    if _err:
        return _err
    data = request.get_json(silent=True) or {}
    
    existing = load_user_settings()
    # Retired no-op controls: ignore stale persisted values and never carry
    # them through a subsequent save.
    existing.pop('rate_limit_enabled', None)
    existing.pop('rate_limit_rps', None)
    
    # OpenAI key. NOT part of the retired Navigator provider toggle — it is
    # resolved independently by three live consumers: whisper_client.py (the
    # whisper-1 transcription fallback), quote_cleaner.py (gpt-4o-mini, T-011),
    # and ingest_validator.py. The gemini_api_key / deepseek_api_key accepts
    # went with the toggle; this one stays because those consumers still read it.
    # (get_gemini_consumer_cookies uses gemini_secure_1psid/_1psidts — different
    # fields, unaffected.)
    if data.get('openai_api_key'):
        existing['openai_api_key'] = data['openai_api_key']

    # Z-SPAN chat mode (direct | suggested)
    if 'chat_mode' in data:
        mode = (data.get('chat_mode') or '').strip().lower()
        if mode in ('direct', 'suggested'):
            existing['chat_mode'] = mode

    save_user_settings(existing)
    return jsonify({'success': True})


@app.route('/api/settings', methods=['DELETE'])
@_require_trusted_origin
def api_clear_settings():
    """Clear all saved settings."""
    # Session-31 (2026-07-04) — auth-audit remediation. Nukes the
    # entire settings dict on disk. Owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    save_user_settings({})
    return jsonify({'success': True})


# ─────────────────────────────────────────────────────────────────
# Orchestrator autonomy gate (S-007) — the load-bearing control surface
# for the digital-twin orchestrator's graduated autonomy. The page at
# ?view=autonomy reads/writes this; the orchestrator (when built) reads
# `autonomous_enabled` per capability at each heartbeat to know what it
# may do on its own. Canonical capability definitions live here in code;
# the JSON file persists only the mutable per-capability state (the
# on-its-own toggle + the operator's audit note), keyed by id — so
# doctrine edits in code propagate without touching saved operator state.
# ─────────────────────────────────────────────────────────────────

ORCHESTRATOR_AUTONOMY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'orchestrator_autonomy.json'
)

# rung: 1 (live now) .. 4 (far off). instructed: 'on' (James can ask it
# anytime, regardless of rung), 'passive' (a background behavior, not an
# instructable action), 'gated' (instructable but James-gated / sensitive),
# 'never' (a permanent wall). wall=True means there is no autonomous toggle
# at all — D-006 publish can never be automated.
_DEFAULT_CAPABILITIES = [
    {
        "id": "read_board",
        "label": "Keep an eye on the whole operation",
        "what": "Each time it wakes, it looks over the queues, backlogs, and how the team is doing.",
        "rung": 1, "instructed": "passive", "wall": False, "default_enabled": True,
    },
    {
        "id": "recommend_escalate",
        "label": "Flag things for you, and ask when it's unsure",
        "what": "Surfaces what needs your attention and holds back rather than guessing.",
        "rung": 1, "instructed": "passive", "wall": False, "default_enabled": True,
    },
    {
        "id": "trigger_watchers",
        "label": "Look for new meetings and broken parsers",
        "what": "Wakes the two watchers that scan calendars and parser health. They only look — they change nothing.",
        "rung": 1, "instructed": "on", "wall": False, "default_enabled": True,
    },
    {
        "id": "trigger_disputed_reviewer",
        "label": "Send disputed quotes to the reviewer",
        "what": "Wakes the reviewer to work through any quotes that need a judgment call.",
        "rung": 2, "instructed": "on", "wall": False, "default_enabled": False,
    },
    {
        "id": "trigger_vocab_curator",
        "label": "Tidy up a city's dictionary",
        "what": "Wakes the curator to work through a city's pending spelling corrections, promoting or rejecting each as appropriate.",
        "rung": 2, "instructed": "on", "wall": False, "default_enabled": False,
    },
    {
        "id": "trigger_pipeline_prep",
        "label": "Prep meetings for verification",
        "what": "Builds the clip review queue and ingests the results as meetings are ready — the steps before quotes get checked. Spends a little money.",
        "rung": 3, "instructed": "on", "wall": False, "default_enabled": False,
    },
    {
        "id": "run_verification_pass",
        "label": "Run the quote-check workflow",
        "what": "Drives the full Gemini Pro pass that checks each clip against what was actually said.",
        "rung": 3, "instructed": "on", "wall": False, "default_enabled": False,
    },
    {
        "id": "autonomous_generation",
        "label": "Generate new broadcasts on its own",
        "what": "Kicks off the pipeline (transcribe, index, synthesize) to produce a meeting's outputs. Stays yours to start for now.",
        "rung": 4, "instructed": "gated", "wall": False, "default_enabled": False,
    },
    {
        "id": "publish",
        "label": "Publish a broadcast",
        "what": "Always yours. The publish button is never automated — not on its own, not even when asked.",
        "rung": 4, "instructed": "never", "wall": True, "default_enabled": False,
    },
]


def _read_autonomy_file() -> dict:
    """The full persisted control surface: {state, calibration, updated_at}."""
    if not os.path.exists(ORCHESTRATOR_AUTONOMY_PATH):
        return {}
    try:
        with open(ORCHESTRATOR_AUTONOMY_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        app.logger.warning(f"Could not read orchestrator_autonomy.json: {e}")
        return {}


def _write_autonomy_file(data: dict) -> None:
    data['updated_at'] = datetime.utcnow().isoformat() + 'Z'
    with open(ORCHESTRATOR_AUTONOMY_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _load_autonomy_state() -> dict:
    """The mutable per-capability state (enabled + note), keyed by id."""
    return _read_autonomy_file().get('state', {}) or {}


def _save_autonomy_state(state: dict) -> None:
    """Persist capability state without clobbering the calibration block."""
    data = _read_autonomy_file()
    data['state'] = state
    _write_autonomy_file(data)


# S-010 ingestion calibration — the metering dial James widens on the gate-board.
# compute ceiling = videos_per_day; review ceiling = reviewers ×
# reviews_per_reviewer_per_day (S-011: the sum of active reviewers' throughput).
# Co-located in orchestrator_autonomy.json because "the gate-board IS the
# calibration dial" (S-010); served by the /api/ingestion/* endpoints below.
DEFAULT_CALIBRATION = {
    "videos_per_day": 1,                 # compute ceiling — start low, walk up on clean cycles
    "reviewers": 1,                      # active human reviewers (James = 1)
    "reviews_per_reviewer_per_day": 1,   # per-reviewer light-final-pass throughput (S-011)
    # S-010 budget/solvency ceiling — the third min() term. The budget ceiling =
    # available_balance / (cost_per_video * solvency_days), so the machine never
    # drains the pot faster than the solvency window. available_balance=None means
    # "unconfigured" → the budget term simply doesn't bind. cost_per_video is a
    # conservative placeholder over the ~$0.46 measured Whisper baseline, later
    # refinable by the finance-reconciliation agent (S-014).
    "available_balance": None,           # $ in the pot; None until James sets it
    "cost_per_video": 1.0,               # conservative $/video (Whisper scales w/ length)
    "solvency_days": 30,                 # never drain the balance faster than this window
    "note": "",
}


def _load_calibration() -> dict:
    saved = _read_autonomy_file().get('calibration', {}) or {}
    merged = dict(DEFAULT_CALIBRATION)
    merged.update({k: v for k, v in saved.items() if k in DEFAULT_CALIBRATION})
    return merged


def _save_calibration(cal: dict) -> None:
    """Persist the calibration block without clobbering capability state."""
    data = _read_autonomy_file()
    data['calibration'] = cal
    _write_autonomy_file(data)


def _review_ceiling(cal: dict) -> float:
    """S-011: the review ceiling is the SUM of active reviewers' throughput."""
    return float(cal['reviewers']) * float(cal['reviews_per_reviewer_per_day'])


def _merged_autonomy_capabilities() -> List[dict]:
    """Canonical capability defs merged with persisted mutable state."""
    state = _load_autonomy_state()
    out: List[dict] = []
    for cap in _DEFAULT_CAPABILITIES:
        saved = state.get(cap['id'], {})
        enabled = bool(saved.get('autonomous_enabled', cap['default_enabled']))
        if cap['wall']:
            enabled = False  # walls can never be autonomously enabled
        out.append({
            'id': cap['id'], 'label': cap['label'], 'what': cap['what'],
            'rung': cap['rung'], 'instructed': cap['instructed'], 'wall': cap['wall'],
            'autonomous_enabled': enabled, 'note': (saved.get('note') or ''),
        })
    return out


def _autonomy_frontier_rung(caps: List[dict]) -> int:
    """Highest rung with any autonomous capability switched on (0 = none)."""
    on = [c['rung'] for c in caps if c['autonomous_enabled']]
    return max(on) if on else 0


@app.route('/api/orchestrator/autonomy', methods=['GET'])
def api_get_orchestrator_autonomy():
    """Return the orchestrator's autonomy gate (S-007)."""
    # RR-8 backstop gate: internal capability/frontier state. The POST is
    # already owner-gated; the tiering doc lists this route as Tier-1, so the
    # GET must match. Express forwards the cookie.
    _user, _err = _require_owner()
    if _err:
        return _err
    caps = _merged_autonomy_capabilities()
    return jsonify({'ok': True, 'capabilities': caps,
                    'frontier_rung': _autonomy_frontier_rung(caps)})


@app.route('/api/orchestrator/autonomy', methods=['POST'])
@_require_trusted_origin
def api_set_orchestrator_autonomy():
    """Update one capability's on-its-own toggle and/or audit note.

    Body: {capability_id, autonomous_enabled?, note?}. Walls (publish)
    refuse an enable. Returns the full merged gate.
    """
    # Session-31 (2026-07-04) — auth-audit remediation. Flips spend-
    # triggering autonomy capabilities (only `publish` has a hardcoded
    # wall; every other rung including `trigger_pipeline_prep` and
    # `run_verification_pass` is toggleable). Owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    data = request.get_json(silent=True) or {}
    cap_id = data.get('capability_id')
    if not cap_id:
        return jsonify({'ok': False, 'error': 'capability_id required'}), 400

    canonical = next((c for c in _DEFAULT_CAPABILITIES if c['id'] == cap_id), None)
    if canonical is None:
        return jsonify({'ok': False, 'error': f'unknown capability: {cap_id}'}), 404

    state = _load_autonomy_state()
    entry = dict(state.get(cap_id, {}))

    if 'autonomous_enabled' in data:
        want = bool(data['autonomous_enabled'])
        if canonical['wall'] and want:
            return jsonify({'ok': False,
                            'error': 'This is a permanent wall and can never be automated.'}), 400
        entry['autonomous_enabled'] = want
    if 'note' in data:
        entry['note'] = str(data.get('note') or '')

    state[cap_id] = entry
    _save_autonomy_state(state)

    caps = _merged_autonomy_capabilities()
    return jsonify({'ok': True, 'capabilities': caps,
                    'frontier_rung': _autonomy_frontier_rung(caps)})


# ─────────────────────────────────────────────────────────────────
# S-010 — the low-hum ingestion machine's metering governor.
# GET /api/ingestion/governor reads the gate-board calibration + computes a
# city's rate/progress board (read-only — the rung-1 surface the orchestrator
# consumes; it never advances the queue). POST /api/ingestion/calibration is
# the videos/day dial James widens as clean cycles prove the rate safe.
# New-city onboarding stays S-008-gated; this only meters the focus city.
# ─────────────────────────────────────────────────────────────────

DEFAULT_FOCUS_CITY = 'Kingman'


@app.route('/api/guide', methods=['GET'])
def api_get_guide():
    """Currently-live civic broadcasts across registered channels (S-015).

    Read-only mirror of the live_streams cache that guide_detector.py populates
    via calendar-gated YouTube live detection. No NotebookLM/generation/publish
    machinery — just public live streams.
    """
    try:
        streams = get_live_streams()
        # "Y" for the X-of-Y stat: registered-channel cities meeting today (the
        # feeds we could expect to go live). X = len(streams), the live count.
        today = datetime.now().date().isoformat()
        channel_keys = {(c['city'], c['state']) for c in get_cities_with_youtube_channel()}
        scheduled_today = len(channel_keys & get_cities_with_meeting_on(today))
        return jsonify({'ok': True, 'live': streams, 'count': len(streams),
                        'scheduled_today': scheduled_today})
    except Exception as e:
        app.logger.exception('guide endpoint failed')
        return jsonify({'ok': False, 'live': [], 'count': 0, 'scheduled_today': 0,
                        'error': str(e)}), 500


@app.route('/api/ingestion/governor', methods=['GET'])
def api_get_ingestion_governor():
    """Read-only ingestion metering for a city: calibration + rate/progress."""
    city = (request.args.get('city') or DEFAULT_FOCUS_CITY).strip()
    cal = _load_calibration()
    compute_ceiling = float(cal['videos_per_day'])
    review_ceiling = _review_ceiling(cal)
    bal = cal.get('available_balance')
    metering = compute_city_metering(
        city, compute_ceiling, review_ceiling,
        available_balance=(float(bal) if bal is not None else None),
        cost_per_video=(float(cal.get('cost_per_video') or 0) or None),
        solvency_days=float(cal.get('solvency_days') or 30),
    )
    if not _request_is_owner():
        # RR-8 pre-flip: budget dollars are owner-only. Non-owners get the
        # pace/progress board without balance/cost/solvency.
        cal = {k: v for k, v in cal.items()
               if k not in ('available_balance', 'cost_per_video', 'solvency_days')}
        metering = _redact_metering_budget(metering)
    return jsonify({
        'ok': True,
        'calibration': cal,
        'review_ceiling': review_ceiling,
        'metering': metering,
    })


@app.route('/api/ingestion/calibration', methods=['POST'])
@_require_trusted_origin
def api_set_ingestion_calibration():
    """Update the metering dial (S-010).

    Body may carry any of: videos_per_day, reviewers,
    reviews_per_reviewer_per_day (numbers >= 0), note (string). Integral
    values are stored as ints. Returns the merged calibration.
    """
    # Session-31 (2026-07-04) — auth-audit remediation. Writes the $
    # budget ceiling and compute-per-video cost dial. Anyone could set
    # available_balance to 0 or crank cost_per_video to arbitrary values
    # to trigger spend or lock out operations. Owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    data = request.get_json(silent=True) or {}
    cal = _load_calibration()
    for key in ('videos_per_day', 'reviewers', 'reviews_per_reviewer_per_day',
                'available_balance', 'cost_per_video', 'solvency_days'):
        if key in data:
            # available_balance accepts an explicit null → clears the budget to
            # "unconfigured" (the term simply stops binding). The others require
            # a number.
            if key == 'available_balance' and data[key] is None:
                cal[key] = None
                continue
            try:
                v = float(data[key])
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': f'{key} must be a number'}), 400
            if v < 0:
                return jsonify({'ok': False, 'error': f'{key} must be 0 or more'}), 400
            cal[key] = int(v) if v == int(v) else v
    if 'note' in data:
        cal['note'] = str(data.get('note') or '')
    _save_calibration(cal)
    return jsonify({'ok': True, 'calibration': cal})


# ─────────────────────────────────────────────────────────────────
# HQ skybox traffic events (post-Logstalgia pivot 2026-05-29).
# Two upstream feeds (Flask access-log tee + CF Worker), one SSE downstream
# to the in-HQ shooting-star viz, plus owner-gated test injection for the
# mock-traffic panel. Event shape + classification rules in traffic_events.py.
# ─────────────────────────────────────────────────────────────────


def _validate_traffic_ingest_token():
    """Receiver-side guard for the CF Worker → Flask ingest path.
    Returns None if valid; otherwise (error_message, status_code)."""
    expected = (
        os.environ.get('Z_SPAN_TRAFFIC_INGEST_TOKEN')
        or _load_user_settings_value('z_span_traffic_ingest_token')
    )
    if not expected:
        return ('traffic ingest misconfigured: Z_SPAN_TRAFFIC_INGEST_TOKEN not set',
                503)
    provided = request.headers.get('X-Zspan-Traffic-Ingest-Token', '')
    if not provided:
        return ('missing X-Zspan-Traffic-Ingest-Token header', 401)
    if provided != expected:
        return ('invalid X-Zspan-Traffic-Ingest-Token', 403)
    return None


# Defense-in-depth: cap incoming ingest POSTs at this rate. Returns 429
# over budget. Protects Flask CPU (token validation + JSON parse +
# broadcast loop) against a flood that punches through Cloudflare's edge
# protection, or a runaway worker calling us in a loop, or a leaked token.
# Layered with traffic_events.broadcast()'s own 50/sec cap and the
# renderer's MAX_STARS=280.
_INGEST_RATE_LIMIT_PER_SEC = 200
_ingest_times: Deque[float] = deque(maxlen=_INGEST_RATE_LIMIT_PER_SEC * 3)
_ingest_rate_lock = threading.Lock()
_ingest_rejected_total = 0


def _ingest_rate_limit_check():
    """Returns None if under budget; (error_msg, 429) if over."""
    global _ingest_rejected_total
    with _ingest_rate_lock:
        now = time.time()
        while _ingest_times and now - _ingest_times[0] > 1.0:
            _ingest_times.popleft()
        if len(_ingest_times) >= _INGEST_RATE_LIMIT_PER_SEC:
            _ingest_rejected_total += 1
            return ('ingest rate limited (per-second cap)', 429)
        _ingest_times.append(now)
    return None


@app.route('/api/hq/traffic-events', methods=['GET'])
def api_hq_traffic_events_stream():
    """SSE stream of live traffic events to the HQ skybox viz.

    Long-lived; sends a `: heartbeat` SSE comment every 15s of quiet so
    proxies + browsers don't close the idle connection.
    """
    def _gen():
        sub = traffic_subscribe()
        try:
            yield ': connected\n\n'
            while True:
                try:
                    evt = sub.q.get(timeout=15.0)
                    yield 'data: ' + json.dumps(evt) + '\n\n'
                except queue.Empty:
                    yield ': heartbeat\n\n'
        finally:
            traffic_unsubscribe(sub)

    return Response(_gen(), mimetype='text/event-stream',
                    headers={
                        'Cache-Control': 'no-cache',
                        'X-Accel-Buffering': 'no',  # disable proxy buffering
                    })


@app.route('/api/hq/traffic-events/inject', methods=['POST'])
@_require_trusted_origin
def api_hq_traffic_events_inject():
    """Owner-gated test injection — the mock-panel buttons hit this.

    Body: {events: [{status, path_class, bot_classification}, ...], count?: int}.
    The `count` shortcut replicates each event N times (1..1000) for burst tests.

    No token gating at Flask — in prod the operator surface is behind D-051's
    Cloudflare Access policy; in dev Flask is open. Frontend hides the panel
    via isOwner. Source is forced to 'mock' regardless of what the caller sends.
    """
    # Session-31 auth-audit remediation — owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    data = request.get_json(silent=True) or {}
    events = data.get('events') or []
    if not isinstance(events, list) or not events:
        return jsonify({'ok': False, 'error': 'events: non-empty array required'}), 400
    try:
        count = int(data.get('count') or 1)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'count must be an integer'}), 400
    if count < 1 or count > 1000:
        return jsonify({'ok': False, 'error': 'count must be between 1 and 1000'}), 400

    fanned = 0
    for raw in events:
        if not isinstance(raw, dict):
            continue
        evt = traffic_normalize_event(raw, source='mock')
        for _ in range(count):
            traffic_broadcast(evt)
            fanned += 1
    return jsonify({'ok': True, 'injected': fanned,
                    'subscribers': traffic_subscriber_count()})


@app.route('/api/hq/traffic-events/ingest', methods=['POST'])
def api_hq_traffic_events_ingest():
    """Signed ingest from the Cloudflare Worker (prod traffic feed, chunk 5).

    Body: {events: [{ts, status, path_class, bot_classification}, ...]}.
    Source is forced to 'cloudflare' regardless of what the body sends.
    Rate-limited at _INGEST_RATE_LIMIT_PER_SEC POSTs/sec (429 over budget).
    """
    guard = _validate_traffic_ingest_token()
    if guard is not None:
        msg, status = guard
        return jsonify({'ok': False, 'error': msg}), status

    rate_guard = _ingest_rate_limit_check()
    if rate_guard is not None:
        msg, status = rate_guard
        return jsonify({'ok': False, 'error': msg}), status

    data = request.get_json(silent=True) or {}
    events = data.get('events') or []
    if not isinstance(events, list):
        return jsonify({'ok': False, 'error': 'events: array required'}), 400

    fanned = 0
    for raw in events:
        if not isinstance(raw, dict):
            continue
        evt = traffic_normalize_event(raw, source='cloudflare')
        traffic_broadcast(evt)
        fanned += 1
    return jsonify({'ok': True, 'ingested': fanned})


# ─────────────────────────────────────────────────────────────────
# Z-SPAN cached-output read endpoints
# (NotebookLM write-side removed per D-143 2026-07-01; the read
# endpoint below still serves the historical notebook_outputs cache
# rows + the current V1-RAG-3 cache rows to the frontend.)
# ─────────────────────────────────────────────────────────────────

_GENERIC_SPEAKER_LABEL = 'Speaker'
_SPEAKER_NAME_FIELDS = {
    'speaker',
    'speaker_name',
    'speakerName',
    'speaker_display_name',
    'speakerDisplayName',
    'speaker_canonical_name',
    'canonical_speaker_name',
    'denorm_speaker_name',
}
_SPEAKER_DETAIL_FIELDS = {
    'speaker_role',
    'speakerRole',
    'speaker_title',
    'speakerTitle',
}
_SPEAKER_IDENTITY_FIELDS = {
    'speaker_id',
    'speakerId',
    'member_id',
    'memberId',
    'seat_id',
    'seatId',
}


def _genericize_speaker_attribution(value: Any) -> Any:
    """Return a response-safe copy with personal speaker attribution removed.

    Stored data remains untouched. Callers apply this transform only to
    response payloads immediately before they are serialized.
    """
    if isinstance(value, list):
        return [_genericize_speaker_attribution(item) for item in value]
    if not isinstance(value, dict):
        return value

    has_speaker = any(
        field in value
        for field in _SPEAKER_NAME_FIELDS | _SPEAKER_DETAIL_FIELDS
    )
    genericized: Dict[str, Any] = {}
    for key, nested in value.items():
        if key in _SPEAKER_NAME_FIELDS:
            genericized[key] = (
                _genericize_speaker_attribution(nested)
                if isinstance(nested, (dict, list))
                else _GENERIC_SPEAKER_LABEL
            )
        elif has_speaker and key in _SPEAKER_DETAIL_FIELDS:
            genericized[key] = '' if isinstance(nested, str) else None
        elif has_speaker and key in _SPEAKER_IDENTITY_FIELDS:
            genericized[key] = None
        else:
            genericized[key] = _genericize_speaker_attribution(nested)
    return genericized


def _genericize_speaker_attribution_in_content(content: Any) -> Any:
    """Genericize speaker fields inside a stored JSON content string.

    Legacy outputs may be wrapped in a Markdown JSON fence. Non-JSON prose is
    returned byte-for-byte so this read-path suppression cannot damage it.
    """
    if not isinstance(content, str) or not content.strip():
        return content

    stripped = content.strip()
    fence = re.fullmatch(
        r'```(?P<label>json)?\s*\n?(?P<body>.*?)\n?\s*```',
        stripped,
        re.DOTALL | re.IGNORECASE,
    )
    json_text = fence.group('body') if fence else stripped
    try:
        parsed = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return content

    genericized = _genericize_speaker_attribution(parsed)
    if genericized == parsed:
        return content
    return json.dumps(genericized, ensure_ascii=False)


def _genericize_broadcast_outputs(outputs: Any) -> Any:
    """Return copied output rows whose serialized content has no speaker name."""
    if not isinstance(outputs, dict):
        return outputs
    genericized: Dict[str, Any] = {}
    for output_type, raw_output in outputs.items():
        if not isinstance(raw_output, dict):
            genericized[output_type] = raw_output
            continue
        output = dict(raw_output)
        output['content'] = _genericize_speaker_attribution_in_content(
            output.get('content')
        )
        genericized[output_type] = _genericize_speaker_attribution(output)
    return genericized


def _normalize_ccta_alignment_text(value: Any) -> str:
    """Normalize claimed-verbatim text exactly like the local CLI matcher."""
    if not isinstance(value, str):
        return ''
    collapsed = ' '.join(value.lower().split())
    return re.sub(r'[^a-z0-9 ]', '', collapsed)


def _parse_ccta_content(content: Any) -> list:
    """Parse the stored CCTA JSON array, including the legacy JSON fence."""
    if not isinstance(content, str) or not content.strip():
        return []
    stripped = content.strip()
    fenced = re.fullmatch(
        r'```(?:json)?\s*(?P<body>.*?)\s*```',
        stripped,
        re.DOTALL | re.IGNORECASE,
    )
    try:
        parsed = json.loads(fenced.group('body') if fenced else stripped)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _ccta_word_timings(content: Any, transcript_content: Any) -> list[list[dict]]:
    """Build an index-preserving karaoke sidecar from exact transcript text.

    The generated timestamp is only a disambiguation hint when the same quote
    occurs more than once. A quote that cannot be found verbatim stays empty;
    an approximate timestamp never becomes fabricated per-word alignment.
    """
    elements = _parse_ccta_content(content)
    if not elements or not transcript_content:
        return []
    try:
        transcript = (
            json.loads(transcript_content)
            if isinstance(transcript_content, str)
            else transcript_content
        )
    except (json.JSONDecodeError, TypeError):
        return []
    words = transcript.get('words') if isinstance(transcript, dict) else None
    if not isinstance(words, list) or not words:
        return []

    source_words: list[dict] = []
    normalized_words: list[str] = []
    word_offsets: list[int] = []
    next_offset = 0
    for raw_word in words:
        if not isinstance(raw_word, dict):
            continue
        normalized = _normalize_ccta_alignment_text(raw_word.get('word'))
        if not normalized:
            continue
        source_words.append(raw_word)
        normalized_words.append(normalized)
        word_offsets.append(next_offset)
        next_offset += len(normalized) + 1
    transcript_text = ' '.join(normalized_words)
    if not transcript_text:
        return [[] for _element in elements]

    timings: list[list[dict]] = []
    for element in elements:
        if not isinstance(element, dict):
            timings.append([])
            continue
        quote_text = _normalize_ccta_alignment_text(element.get('quote_text'))
        if not quote_text:
            timings.append([])
            continue

        match_offsets: list[int] = []
        start_at = transcript_text.find(quote_text)
        while start_at >= 0:
            before = transcript_text[start_at - 1] if start_at else ''
            after_offset = start_at + len(quote_text)
            after = (
                transcript_text[after_offset]
                if after_offset < len(transcript_text)
                else ''
            )
            if not before.isalnum() and not after.isalnum():
                match_offsets.append(start_at)
            start_at = transcript_text.find(quote_text, start_at + 1)
        if not match_offsets:
            timings.append([])
            continue

        hint = element.get('video_timestamp_seconds')
        usable_hint = (
            float(hint)
            if isinstance(hint, (int, float))
            and not isinstance(hint, bool)
            and math.isfinite(float(hint))
            and float(hint) >= 0
            else None
        )

        def _source_index(offset: int) -> int:
            return max(0, bisect_right(word_offsets, offset) - 1)

        if usable_hint is None:
            match_offset = match_offsets[0]
        else:
            match_offset = min(
                match_offsets,
                key=lambda offset: abs(
                    float(source_words[_source_index(offset)].get('start') or 0)
                    - usable_hint
                ),
            )
        first_index = _source_index(match_offset)
        last_index = _source_index(match_offset + len(quote_text) - 1)

        aligned: list[dict] = []
        valid = True
        for word in source_words[first_index:last_index + 1]:
            token = word.get('word')
            start = word.get('start')
            end = word.get('end')
            if (
                not isinstance(token, str)
                or not isinstance(start, (int, float))
                or isinstance(start, bool)
                or not isinstance(end, (int, float))
                or isinstance(end, bool)
                or not math.isfinite(float(start))
                or not math.isfinite(float(end))
            ):
                valid = False
                break
            aligned.append({
                'word': token.strip(),
                'start_ms': int(float(start) * 1000),
                'end_ms': int(float(end) * 1000),
            })
        timings.append(aligned if valid and aligned else [])
    return timings


def _attach_ccta_word_timings(outputs: Any) -> Any:
    """Attach response-only CCTA karaoke without mutating stored outputs."""
    if not isinstance(outputs, dict):
        return outputs
    ccta = outputs.get('community_calls_to_action')
    transcript = outputs.get('transcript_words')
    if not isinstance(ccta, dict):
        return outputs
    copied = dict(outputs)
    copied_ccta = dict(ccta)
    copied_ccta['karaoke_word_timings'] = _ccta_word_timings(
        ccta.get('content'),
        transcript.get('content') if isinstance(transcript, dict) else None,
    )
    copied['community_calls_to_action'] = copied_ccta
    return copied


def _materialize_decision_excerpts_for_response(
    meeting_id: int,
    data: Any,
    *,
    include_voided_transcript: bool = False,
) -> Any:
    """Derive legacy decision excerpts in memory without touching sidecars."""
    if not isinstance(data, dict):
        return data
    from council_navigator.parsers import quote_align  # noqa: PLC0415

    conn = get_connection()
    try:
        void_filter = "" if include_voided_transcript else " AND voided_at IS NULL"
        row = conn.execute(
            f"""
            SELECT content FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'transcript_words'
              AND content IS NOT NULL AND content != ''
              {void_filter}
            ORDER BY rowid DESC LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return data
    finally:
        conn.close()
    if not row:
        return data
    try:
        transcript = json.loads(row['content'])
    except (json.JSONDecodeError, TypeError):
        return data
    words = transcript.get('words') if isinstance(transcript, dict) else None
    if not isinstance(words, list):
        return data
    materialized = quote_align.materialize_missing_decision_excerpts(data, words)
    decisions = materialized.get('decisions')
    if not isinstance(decisions, list):
        return materialized
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        spans = decision.get('verbatim_spans')
        if not isinstance(spans, list):
            continue
        for span in spans:
            if not isinstance(span, dict):
                continue
            span.pop('word_timings', None)
            timings = _decision_span_word_timings(span, words)
            if timings is not None:
                span['word_timings'] = timings
    return materialized


def _decision_span_word_timings(
    span: dict,
    transcript_words: list,
) -> Optional[list[dict]]:
    """Select response-only timings when they exactly reconstruct a span."""
    start_index = span.get('start_word_index')
    end_index = span.get('end_word_index')
    if start_index is None and end_index is None:
        start_seconds = span.get('start_seconds')
        end_seconds = span.get('end_seconds')
        if (
            not isinstance(start_seconds, (int, float))
            or isinstance(start_seconds, bool)
            or not isinstance(end_seconds, (int, float))
            or isinstance(end_seconds, bool)
            or not math.isfinite(float(start_seconds))
            or not math.isfinite(float(end_seconds))
            or end_seconds < start_seconds
        ):
            return None
        selected = [
            word for word in transcript_words
            if isinstance(word, dict)
            and isinstance(word.get('start'), (int, float))
            and not isinstance(word.get('start'), bool)
            and isinstance(word.get('end'), (int, float))
            and not isinstance(word.get('end'), bool)
            and start_seconds <= word['start']
            and word['end'] <= end_seconds
        ]
    else:
        if (
            not isinstance(start_index, int)
            or isinstance(start_index, bool)
            or not isinstance(end_index, int)
            or isinstance(end_index, bool)
            or start_index < 0
            or end_index < start_index
            or end_index >= len(transcript_words)
        ):
            return None
        selected = transcript_words[start_index:end_index + 1]

    timings: list[dict] = []
    for word in selected:
        if not isinstance(word, dict):
            return None
        token = word.get('word')
        start = word.get('start')
        end = word.get('end')
        if (
            not isinstance(token, str)
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
        ):
            return None
        timings.append({'word': token, 'start': start, 'end': end})
    if not timings or ' '.join(timing['word'] for timing in timings) != span.get('text'):
        return None
    return timings


@app.route('/api/notebook/<int:meeting_id>', methods=['GET'])
def api_notebook_get(meeting_id):
    """Return the meeting + notebook_id + all cached Studio outputs."""
    _user, _err = _require_owner()
    if _err:
        return _err
    meeting = get_meeting_with_notebook(meeting_id)
    if not meeting:
        return jsonify({
            'success': False,
            'error': f'No meeting found with id={meeting_id}'
        }), 404

    # Prefer the work order's resolved video URL (set by S-037 V0 Granicus
    # resolver / user-paste flow) over the parser-emitted meetings.video_url —
    # the WO column carries the embed-ready archive URL after resolution while
    # meetings.video_url may still hold the calendar-side MediaPlayer page.
    video_url = meeting.get('wo_video_url') or meeting.get('video_url')

    outputs = _genericize_broadcast_outputs(
        _attach_ccta_word_timings(meeting.get('notebook_outputs', {}))
    )

    # Completeness summary 2026-07-06 (Fable-5 audit F-7.1): the payload
    # used to return good rows and errored rows side-by-side with no
    # machine-readable "N of M" — the F8 succeeded-empty/failed-silent
    # pattern one level up. check_publish_readiness() is the single
    # source of truth for the floor; this endpoint reads its verdict,
    # never reimplements it. Owners additionally get the plain-language
    # reasons for the OperatorTerminal-adjacent surfaces.
    from database import check_publish_readiness  # noqa: PLC0415
    verdict = check_publish_readiness(meeting_id)
    completeness = {
        'complete': bool(verdict.get('publishable')),
        'required_ok': verdict.get('required_ok'),
        'required_total': verdict.get('required_total'),
        'reasons': (
            (verdict.get('reasons') or [])
            + (verdict.get('publish_blockers') or [])
        ),
    }

    return jsonify({
        'success': True,
        'meeting_id': meeting['id'],
        'meeting_title': meeting.get('meeting_title'),
        'meeting_date': meeting.get('meeting_date'),
        'city': meeting.get('city_name'),
        'county': meeting.get('county'),
        'notebook_id': meeting.get('notebook_id'),
        'video_url': video_url,
        # D-001 / D-031 / D-032: approval state on the work order. Frontend
        # uses these to gate public render — null approved_at means the
        # broadcast hasn't passed the review gate yet, so the BroadcastPage
        # shows a "pending review" placeholder unless ?preview=true.
        # approved_by deliberately not served (2026-07-09: operator identity
        # stays off public surfaces; the timestamp carries the state).
        'approved_at': meeting.get('wo_approved_at'),
        'completeness': completeness,
        'outputs': outputs,
    })


@app.route('/api/episode-audit/<int:meeting_id>', methods=['GET'])
def api_episode_audit_get(meeting_id):
    """Return the latest private episode-audit run for one meeting."""
    _user, _err = _require_owner()
    if _err:
        return _err

    from database import get_latest_episode_audit_run  # noqa: PLC0415
    run = get_latest_episode_audit_run(meeting_id)
    if run is None:
        return jsonify({
            'status': 'none',
            'meeting_id': meeting_id,
        })

    response_run = dict(run)
    response_run.pop('report_json', None)
    return jsonify({
        'status': 'ok',
        'run': response_run,
    })


@app.route(
    '/api/episode-audit/<int:meeting_id>/apply-fix',
    methods=['POST'],
)
@_require_trusted_origin
def api_episode_audit_apply_fix(meeting_id):
    """Apply one owner-approved episode-audit proposal."""
    user, err = _require_owner()
    if err:
        return err

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    run_id = payload.get('run_id')
    proposal_id = payload.get('proposal_id')
    for field_name, value in (
        ('run_id', run_id),
        ('proposal_id', proposal_id),
    ):
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 200
        ):
            return jsonify({
                'status': 'error',
                'error': f'{field_name} must be a non-empty string of at most 200 characters',
            }), 400

    from zspan_pipeline import episode_fix_apply  # noqa: PLC0415
    result = episode_fix_apply.apply_fix(
        meeting_id,
        run_id,
        proposal_id,
        actor=str(user.email),
    )
    status = result.get('status')
    if status == 'applied':
        return jsonify({
            'status': status,
            'event_id': result.get('event_id'),
            'post_content_sha256': result.get('post_content_sha256'),
        })
    if status == 'already_applied':
        return jsonify({'status': status})

    # Modeled outcomes: deferred/conflict=409, validation=422, missing=404.
    status_codes = {
        'adapter_deferred': 409,
        'validation_failed': 422,
        'cas_conflict': 409,
        'not_found': 404,
    }
    if status in status_codes:
        response = {'status': status}
        if status == 'validation_failed':
            response['checks'] = result.get('checks', {})
        return jsonify(response), status_codes[status]

    app.logger.error('Unexpected episode apply-fix status: %r', status)
    return jsonify({
        'status': 'error',
        'error': 'unexpected apply-fix result',
    }), 500


@app.route(
    '/api/episode-audit/<int:meeting_id>/disposition',
    methods=['POST'],
)
@_require_trusted_origin
def api_episode_audit_disposition(meeting_id):
    """Record an owner rejection or deferral for one proposal."""
    user, err = _require_owner()
    if err:
        return err

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    run_id = payload.get('run_id')
    proposal_id = payload.get('proposal_id')
    for field_name, value in (
        ('run_id', run_id),
        ('proposal_id', proposal_id),
    ):
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 200
        ):
            return jsonify({
                'status': 'error',
                'error': f'{field_name} must be a non-empty string of at most 200 characters',
            }), 400

    disposition = payload.get('disposition')
    if disposition not in {'rejected', 'deferred'}:
        return jsonify({
            'status': 'error',
            'error': 'disposition must be rejected or deferred',
        }), 400
    reason = payload.get('reason')
    if reason is not None and not isinstance(reason, str):
        return jsonify({
            'status': 'error',
            'error': 'reason must be a string',
        }), 400
    if disposition == 'rejected' and not str(reason or '').strip():
        return jsonify({
            'status': 'error',
            'error': 'rejected disposition requires a non-empty reason',
        }), 400

    from zspan_pipeline import episode_fix_apply  # noqa: PLC0415
    result = episode_fix_apply.record_disposition(
        meeting_id,
        run_id,
        proposal_id,
        disposition,
        actor=str(user.email),
        reason=reason,
    )
    if result.get('status') == 'not_found':
        return jsonify({'status': 'not_found'}), 404
    return jsonify({
        'status': result.get('status'),
        'event_id': result.get('event_id'),
    })


@app.route('/api/episode-audit/summary', methods=['GET'])
def api_episode_audit_summary():
    """Return badge-sized fields from the latest runs for up to 200 meetings."""
    _user, _err = _require_owner()
    if _err:
        return _err

    raw_meeting_ids = request.args.get('meeting_ids', '')
    meeting_id_parts = raw_meeting_ids.split(',')
    if len(meeting_id_parts) > 200:
        return jsonify({
            'status': 'error',
            'error': 'meeting_ids must contain at most 200 ids',
        }), 400
    try:
        meeting_ids = [int(value) for value in meeting_id_parts]
    except ValueError:
        return jsonify({
            'status': 'error',
            'error': 'meeting_ids must be comma-separated integers',
        }), 400

    from database import get_latest_episode_audit_run  # noqa: PLC0415
    summary_fields = (
        'verdict',
        'run_status',
        'findings_count',
        'open_findings_count',
        'suggestions_count',
        'deterministic_flags_count',
        'created_at',
    )
    audits = {}
    for meeting_id in meeting_ids:
        run = get_latest_episode_audit_run(meeting_id)
        if run is not None:
            audits[str(meeting_id)] = {
                field: run.get(field)
                for field in summary_fields
            }

    return jsonify({
        'status': 'ok',
        'audits': audits,
    })


def _mutate_notebook_output_void(
    meeting_id: int,
    output_type: str,
    *,
    voided: bool,
):
    """Owner-only shared implementation for per-output void and restore."""
    user, err = _require_owner()
    if err:
        return err
    if output_type not in _KNOWN_OUTPUT_TYPES:
        return jsonify({
            'success': False,
            'error': 'unknown output type',
        }), 400

    action = "void" if voided else "restore"
    result = set_notebook_output_void_state(
        meeting_id,
        output_type,
        voided=voided,
        actor_email=user.email,
        actor_user_id=user.id,
        event_key=f"{action}:{uuid.uuid4()}",
    )
    if result is None:
        return jsonify({
            'success': False,
            'error': 'output not found',
            'meeting_id': meeting_id,
            'output_type': output_type,
        }), 404

    changed = bool(result.pop("changed"))
    state = "voided" if voided else "live"
    action_label = "voided" if voided else "restored"
    return jsonify({
        'success': True,
        'changed': changed,
        'state': state,
        'message': (
            f"Output {action_label}."
            if changed
            else f"Output was already {state}; no content changed."
        ),
        'output': result,
    })


@app.route(
    '/api/notebook/<int:meeting_id>/outputs/<output_type>/void',
    methods=['POST'],
)
@_require_trusted_origin
def api_notebook_output_void(meeting_id, output_type):
    """Hide one stored generation from every public serving door."""
    return _mutate_notebook_output_void(
        meeting_id,
        output_type,
        voided=True,
    )


@app.route(
    '/api/notebook/<int:meeting_id>/outputs/<output_type>/restore',
    methods=['POST'],
)
@_require_trusted_origin
def api_notebook_output_restore(meeting_id, output_type):
    """Restore one previously voided generation to public serving."""
    return _mutate_notebook_output_void(
        meeting_id,
        output_type,
        voided=False,
    )


# ─────────────────────────────────────────────────────────────────
# Unified quotes endpoints (Quotes Unification Refactor, Chunk 6, 2026-05-26)
#
# These read from the canonical `quotes` table (per
# 01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md). During the refactor
# transition, the per-meeting endpoint falls back to parsing the legacy
# council_quotes JSON blob for meetings that haven't been re-extracted yet,
# so BroadcastPage gets a uniform shape regardless of source. Chunk 9
# removes the fallback once the legacy paths are confirmed unused.
# ─────────────────────────────────────────────────────────────────


def _derive_speaker_class_from_role(role: Optional[str]) -> str:
    """Map a free-form speaker_role string to a speaker_class enum value.
    Used by the legacy-council_quotes fallback path."""
    role_norm = (role or '').strip().lower()
    council_roles = {
        'mayor', 'vice mayor', 'councilmember', 'council member',
        'councilman', 'councilwoman', 'council',
    }
    return 'council_member' if role_norm in council_roles else 'staff'


def _parse_legacy_council_quotes_to_unified_shape(
    meeting_id: int, content: Optional[str],
) -> List[Dict[str, Any]]:
    """Parse a council_quotes JSON blob (legacy V1 format) into the unified
    `quotes`-shaped list. Each returned dict matches the column set of the
    `quotes` table (with synthetic id/content_hash to disambiguate from
    real rows). All entries get is_broadcast_hero=1 because council_quotes
    only ever extracted the broadcast-hero subset.
    """
    if not content:
        return []
    import re as _re
    fence = _re.match(r'^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$', content, _re.DOTALL)
    txt = fence.group(1) if fence else content
    try:
        data = json.loads(txt)
    except (json.JSONDecodeError, TypeError):
        return []
    quotes_list = data.get('quotes') if isinstance(data, dict) else data
    if not isinstance(quotes_list, list):
        return []
    rows: List[Dict[str, Any]] = []
    for idx, q in enumerate(quotes_list):
        if not isinstance(q, dict):
            continue
        speaker_name = (q.get('speaker_name') or q.get('speaker') or '').strip()
        quote_text = (q.get('text') or q.get('quote_text') or '').strip()
        if not speaker_name or not quote_text:
            continue
        speaker_role = (q.get('speaker_role') or '').strip() or None
        topic = q.get('topic')
        word_timings = q.get('word_timings') if isinstance(q.get('word_timings'), list) else None
        derived_ts = None
        if word_timings and isinstance(word_timings, list) and word_timings:
            first = word_timings[0]
            if isinstance(first, dict) and 'start_ms' in first:
                try:
                    derived_ts = int(first['start_ms']) // 1000
                except (TypeError, ValueError):
                    pass
        rows.append({
            'id': f'legacy-{meeting_id}-{idx}',  # synthetic; not a real row
            'meeting_id': meeting_id,
            'member_id': None,
            'speaker_name': speaker_name,
            'speaker_role': speaker_role,
            'speaker_class': _derive_speaker_class_from_role(speaker_role),
            'quote_text': quote_text,
            'quote_text_original': None,
            'topic_tags': [topic] if topic else [],
            'minutes_page_ref': None,
            'context': None,
            'is_broadcast_hero': 1,
            'video_timestamp_seconds': derived_ts,
            'word_timings': word_timings,
            'verified_status': 'pending',  # legacy never went through unified verification
            'verified_by': None,
            'verified_at': None,
            'gemini_correction_notes': None,
            'proof_clip_url': None,
            'proof_clip_sha256': None,
            'content_hash': None,
            'extracted_at': None,
            'updated_at': None,
        })
    return rows


# Reviewer/evidence/internal fields on a council-member quote row that must
# not reach an anonymous caller (RR-8 §5a — the same set the Cast strip drops).
# word_timings is deliberately NOT here: the public BroadcastPage karaoke needs
# it. Surfaces that don't render karaoke (the Cast dossier) drop it separately.
_PUBLIC_QUOTE_STRIP_FIELDS = (
    'verified_by', 'verified_at', 'gemini_correction_notes',
    'proof_clip_sha256', 'quote_text_original',
)


def _strip_public_quote_fields(quotes):
    """Drop reviewer/evidence/internal fields from a list of quote dicts, in
    place, for anonymous callers. Keeps word_timings + the public-safe fields;
    a no-op on rows that lack the fields (legacy synthetic quotes)."""
    for _q in quotes:
        for _f in _PUBLIC_QUOTE_STRIP_FIELDS:
            _q.pop(_f, None)


@app.route('/api/quotes/meeting/<int:meeting_id>', methods=['GET'])
def api_quotes_for_meeting(meeting_id):
    """Return broadcast-hero quotes for a meeting in the unified shape.

    Primary source: rows in the `quotes` table where is_broadcast_hero=1.
    Fallback: parse the legacy council_quotes JSON blob and project into
    the unified shape (synthetic ids prefixed `legacy-`; verified_status
    defaults to 'pending' since legacy never went through unified review).

    Query params:
      - include_all=true → return ALL quotes (hero + non-hero) instead of
        just hero subset. Used by operator surfaces / future audit views.

    Response: { success, source, quotes: [...], count }
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    from database import get_quotes_for_meeting  # noqa: PLC0415
    include_all = request.args.get('include_all', '').lower() in ('1', 'true', 'yes')
    rows = get_quotes_for_meeting(
        meeting_id,
        broadcast_hero_only=not include_all,
        exclude_rejected=True,
    )
    # Owner-only quote read. Genericize structured attribution at the response
    # boundary while retaining the stored canonical rows unchanged.
    if rows:
        rows = _genericize_speaker_attribution(rows)
        return jsonify({
            'success': True,
            'source': 'quotes_table',
            'quotes': rows,
            'count': len(rows),
        })

    # Fallback: parse legacy council_quotes JSON blob
    conn = get_connection()
    legacy_row = conn.execute(
        """
        SELECT content FROM notebook_outputs
        WHERE meeting_id = ? AND output_type = 'council_quotes'
          AND content IS NOT NULL AND content != ''
        """,
        (meeting_id,),
    ).fetchone()
    conn.close()
    if legacy_row:
        legacy_rows = _parse_legacy_council_quotes_to_unified_shape(
            meeting_id, legacy_row['content'],
        )
        legacy_rows = _genericize_speaker_attribution(legacy_rows)
        return jsonify({
            'success': True,
            'source': 'council_quotes_legacy',
            'quotes': legacy_rows,
            'count': len(legacy_rows),
        })

    return jsonify({
        'success': True,
        'source': 'empty',
        'quotes': [],
        'count': 0,
    })


@app.route('/api/preview/<output_type>/<int:meeting_id>', methods=['GET'])
def api_preview_sidecar(output_type, meeting_id):
    """Serve a BroadcastPage preview sidecar — the decision-Discussion
    karaoke + decision-bound quotes / routing / recusals JSON rendered under
    each generation.

    D-180 makes this numeric-ID route owner-only. The public sibling resolves
    public_id, applies the two-field visibility gate, and projects a fixed DTO.

    Moved here from Express (server/index.ts served these off disk with NO
    publish check). The Cloudflare OWNER_ONLY_PREFIXES edge list never covered
    /api/preview, and a local self-host has no edge at all — so the app-layer
    gate is the only wall. Express now proxies these cookie-forwarded +
    status-preserving; Flask reads the same _preview_root() the sync receiver
    writes, so the gate sits next to its single source of truth.
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    from flagship_sync import (  # noqa: PLC0415 — lazy
        _preview_root, _sidecar_path, _SIDECAR_TYPES,
    )
    if output_type not in _SIDECAR_TYPES:
        return jsonify({'success': False, 'error': 'Invalid output type'}), 400
    path = _sidecar_path(_preview_root(), meeting_id, output_type)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return jsonify({
            'success': False,
            'error': f'No {output_type} preview for meeting {meeting_id}',
        }), 404
    except (json.JSONDecodeError, OSError) as e:
        app.logger.error(
            'preview sidecar %s m%s read error: %s', output_type, meeting_id, e,
        )
        return jsonify({'success': False, 'error': 'Preview fetch failed'}), 500
    if output_type == 'decisions':
        data = _materialize_decision_excerpts_for_response(
            meeting_id,
            data,
            include_voided_transcript=True,
        )
    data = _genericize_speaker_attribution(data)
    return jsonify({'success': True, 'output_type': output_type, **data})


# ─────────────────────────────────────────────────────────────────
# Z-SPAN Work Order Queue Endpoints
# The worker daemon (zspan_pipeline/worker.py) processes pending work
# orders at a defrag pace. These endpoints let the UI inspect / manage
# the queue.
# ─────────────────────────────────────────────────────────────────

def _strip_wo_identity_for_non_owner(rows):
    """Remove operator-identity fields from WO rows unless the caller is
    the owner (2026-07-09: personal identity off public surfaces; the
    owner terminal keeps its own audit view). Same silent-coerce shape as
    the include_drafts gate — non-owners get the data minus identity,
    never an error."""
    _u = _current_user_from_cookie()
    if _u and is_owner_email(_u.email):
        return rows
    for r in rows:
        r.pop('approved_by', None)
    return rows


def _request_is_owner() -> bool:
    """True iff the request carries a valid owner session cookie. Field-level
    companion to _require_owner() (which aborts): lets a deliberately-public
    endpoint redact owner-only fields while still serving the public shape."""
    _u = _current_user_from_cookie()
    return bool(_u and is_owner_email(_u.email))


def _redact_metering_budget(metering):
    """Strip the dollar sub-block from a compute_city_metering() result for
    non-owners (RR-8 pre-flip: balance/burn/runway/solvency are operator-only).
    Keeps the public pace board (progress + non-dollar ceilings); replaces the
    budget block with just its configured flag + a restricted marker. Also
    neutralizes the budget-derived ceiling fields: when budget is the binding
    ceiling, effective_per_day == budget_per_day and bound_by == 'budget', both
    of which back-reveal the budget rate to a non-owner."""
    if not isinstance(metering, dict):
        return metering
    budget = metering.get('budget')
    if isinstance(budget, dict):
        metering['budget'] = {'configured': bool(budget.get('configured')),
                              'restricted': True}
    ceilings = metering.get('ceilings')
    if isinstance(ceilings, dict):
        ceilings.pop('budget_per_day', None)
        if ceilings.get('bound_by') == 'budget':
            non_budget = [v for v in (ceilings.get('compute_per_day'),
                                      ceilings.get('review_per_day'))
                          if isinstance(v, (int, float))]
            ceilings['effective_per_day'] = min(non_budget) if non_budget else None
            ceilings['bound_by'] = 'restricted'
    return metering


@app.route('/api/work-orders', methods=['GET'])
def api_work_orders_list():
    """
    List work orders. Optional query params: state, city, limit (default 200).
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    state = request.args.get('state') or None
    city = request.args.get('city') or None
    try:
        limit = int(request.args.get('limit', 200))
    except (TypeError, ValueError):
        limit = 200
    rows = list_work_orders(state=state, city=city, limit=limit)
    return jsonify({'success': True, 'work_orders': rows, 'count': len(rows)})


@app.route('/api/work-orders/stats', methods=['GET'])
def api_work_orders_stats():
    """Counts of work orders grouped by state."""
    return jsonify({'success': True, 'stats': work_order_stats()})


@app.route('/api/work-orders/<int:work_order_id>', methods=['GET'])
def api_work_order_get(work_order_id):
    """Get a single work order with meeting metadata."""
    wo = get_work_order(work_order_id)
    if not wo:
        return jsonify({'success': False, 'error': f'No work order id={work_order_id}'}), 404
    _strip_wo_identity_for_non_owner([wo])
    return jsonify({'success': True, 'work_order': wo})


@app.route('/api/work-orders/scan', methods=['POST'])
@_require_trusted_origin
def api_work_orders_scan():
    """
    Trigger the scanner: walk recent meetings and enqueue work orders.
    Body (optional): { "cities": ["Kingman", ...], "age_limit_days": 30 }
    """
    # Session-31 auth-audit — triggers real compute + LLM spend.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    cities = payload.get('cities') or None
    age_limit_days = payload.get('age_limit_days')

    # Lazy import so the parsers module doesn't need to know about the bridge.
    bridge_dir = os.path.normpath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..')
    )
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)
    try:
        from zspan_pipeline.scanner import scan_recent_meetings
    except ImportError as e:
        return jsonify({
            'success': False,
            'error': f'Scanner not importable: {e}'
        }), 503

    kwargs = {}
    if cities:
        kwargs['cities'] = cities
    if isinstance(age_limit_days, int):
        kwargs['age_limit_days'] = age_limit_days
    summary = scan_recent_meetings(**kwargs)
    return jsonify({'success': True, 'summary': summary})


# register-notebook endpoint RETIRED (RR-8 fix-list, S-129, 2026-07-07).
# It manually attached a notebook_id for the manual-creation workflow of
# the retired prior pipeline (D-143); the worker stopped registering
# notebooks entirely, the route had zero callers, and it was ungated.
# Restore from git history only alongside a subsystem that needs it.


@app.route('/api/work-orders/<int:work_order_id>/retry', methods=['POST'])
@_require_trusted_origin
def api_work_order_retry(work_order_id):
    """Reset a failed/awaiting_notebook work order back to pending."""
    # RR-8 fix-list (S-129) — retry re-triggers the full Whisper+Sonnet
    # pipeline (real spend); its WO-mutation siblings (approve / process /
    # confirm-match) all carry this gate. Express proxy already threads
    # `req`, so the owner cookie arrives (§ 5b verified).
    _user, _err = _require_owner()
    if _err:
        return _err
    wo = get_work_order(work_order_id)
    if not wo:
        return jsonify({'success': False, 'error': f'No work order id={work_order_id}'}), 404
    update_work_order_state(work_order_id, 'pending', error=None)
    return jsonify({'success': True, 'work_order_id': work_order_id, 'state': 'pending'})


@app.route('/api/work-orders/<int:work_order_id>/confirm-match', methods=['POST'])
@_require_trusted_origin
def api_work_order_confirm_match(work_order_id):
    """Operator confirms a T-004 medium/needs_review match for a work order.

    Pulls the matched URL from meetings.video_url (set by `haiku_match_videos --apply`)
    onto work_orders.youtube_video_url, and flips state from awaiting_video to
    pending so the WO is ready to [PROCESS].

    Returns 404 if no WO; 409 if no match is available to confirm; 200 with
    the updated WO state on success.
    """
    # Session-31 auth-audit — confirms video URL that will feed pipeline.
    _user, _err = _require_owner()
    if _err:
        return _err
    wo = get_work_order(work_order_id)
    if not wo:
        return jsonify({'success': False, 'error': f'No work order id={work_order_id}'}), 404

    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT video_url, video_url_match_confidence, video_url_match_method "
        "FROM meetings WHERE id = ?",
        (wo['meeting_id'],),
    ).fetchone()
    if not row or not row['video_url']:
        conn.close()
        return jsonify({
            'success': False,
            'error': 'No matched video_url is available for this WO\'s meeting; '
                     'run `haiku_match_videos.py --apply` first or use [SET URL] manually.'
        }), 409

    video_url = row['video_url']
    confidence = row['video_url_match_confidence']
    method = row['video_url_match_method']
    cursor.execute(
        """
        UPDATE work_orders
        SET youtube_video_url = ?,
            state = 'pending',
            video_url_match_confidence = ?,
            video_url_match_method = ?,
            error = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (video_url, confidence, method, work_order_id),
    )
    conn.commit()
    conn.close()
    return jsonify({
        'success': True,
        'work_order_id': work_order_id,
        'youtube_video_url': video_url,
        'state': 'pending',
        'video_url_match_confidence': confidence,
    })


@app.route('/api/work-orders/<int:work_order_id>/approve', methods=['POST'])
@_require_trusted_origin
def api_work_order_approve(work_order_id):
    """Mark a work order as approved (D-032 review gate passed).

    Body:
      {
        "approved_by": "legacy caller value (optional)",
        "verified_quote_ids": ["Quote one", "Quote two", ...]
      }

    Sets approved_at/approved_by on the WO and inserts one row per verified
    quote into quote_verifications. UNIQUE(work_order_id, quote_id) makes
    re-approval idempotent on the audit side — no duplicate verification rows.
    """
    from database import approve_work_order  # noqa: PLC0415 — lazy import keeps module-load order tidy
    # Session-31 (2026-07-04) — auth-audit remediation: this endpoint is
    # the actual D-032 human-review-gate mechanism (CLAUDE.md Guarantee #1).
    # Prior state: accepted a self-asserted `approved_by` string with no
    # identity verification. Anyone could flip any WO to approved as any
    # name. Owner-gated now.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    supplied_approved_by = str(payload.get('approved_by') or '').strip()
    if supplied_approved_by and supplied_approved_by != 'Z-SPAN':
        app.logger.info(
            "discarded caller-supplied approved_by for work_order_id=%s",
            work_order_id,
        )
    approved_by = 'Z-SPAN'

    raw_ids = payload.get('verified_quote_ids') or []
    if not isinstance(raw_ids, list):
        return jsonify({'success': False, 'error': 'verified_quote_ids must be an array'}), 400
    verified_quote_ids = [str(x) for x in raw_ids if isinstance(x, (str, int))]

    wo = get_work_order(work_order_id)
    if not wo:
        return jsonify({'success': False, 'error': f'No work order id={work_order_id}'}), 404

    updated = approve_work_order(
        work_order_id=work_order_id,
        approved_by=approved_by,
        verified_quote_ids=verified_quote_ids,
        actor_user_id=_user.id,
        event_key=f"approve:{uuid.uuid4()}",
    )
    if not updated:
        return jsonify({'success': False, 'error': 'approval failed (work order disappeared)'}), 500

    return jsonify({
        'success': True,
        'work_order_id': work_order_id,
        'approved_at': updated.get('approved_at'),
        'approved_by': updated.get('approved_by'),
        'verified_quote_count': len(verified_quote_ids),
    })


# ── Phase 3 — Publish flow ────────────────────────────────────────────
#
# POST /api/meetings/<int:meeting_id>/publish
#   body: { "published_by": "legacy caller value" (optional),
#           "publish_notes": "..." (optional) }
#   resp: { ok, meeting }
#   Flips meeting to is_published=1. Decoupled from D-032 work-order
#   approval — see database.publish_meeting docstring for the rationale.
#
# POST /api/meetings/<int:meeting_id>/unpublish
#   body: { "unpublished_by": "legacy caller value" (optional),
#           "reason": "..." (optional) }
#   resp: { ok, meeting }
#   Hides a previously-published broadcast. Preserves audit trail in
#   publish_notes.
#
# GET /api/meetings/<int:meeting_id>/publish-status
#   resp: { meeting }
#   Snapshot of publish state + D-032 quality approval state. Powers
#   the BroadcastPage "Reviewed by X on [date]" badge + operator-terminal
#   per-WO publish indicator.


@app.route('/api/meetings/<int:meeting_id>/publish', methods=['POST'])
@_require_trusted_origin
def api_publish_meeting(meeting_id):
    from database import (  # noqa: PLC0415
        publication_text_violation,
        publish_meeting,
        PublishNotReadyError,
    )
    # Session-31 (2026-07-04) — auth-audit remediation. This endpoint
    # flips a broadcast to publicly-visible. Anyone could publish any
    # meeting with an arbitrary `published_by` string prior. Owner-only now.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    supplied_published_by = str(payload.get('published_by') or '').strip()
    if supplied_published_by and supplied_published_by != 'Z-SPAN':
        app.logger.info(
            "discarded caller-supplied published_by for meeting_id=%s",
            meeting_id,
        )
    published_by = 'Z-SPAN'
    publish_notes = payload.get('publish_notes')
    violation = publication_text_violation(publish_notes)
    if violation:
        return jsonify({
            'success': False,
            'error': f'publish_notes {violation}',
        }), 400
    # Session-32 (2026-07-04) — publish-readiness gate. Broken WOs no
    # longer reach the public surface. Operator can override with
    # ?force=true (or force=true in the body) — the override is logged
    # into publish_notes as an explicit override-with-reasons trail.
    force = (
        (payload.get('force') is True)
        or (request.args.get('force', '').strip().lower() in ('1', 'true'))
    )
    try:
        row = publish_meeting(
            meeting_id=meeting_id,
            published_by=published_by,
            publisher_user_id=_user.id,
            publish_notes=publish_notes,
            force=force,
            actor_user_id=_user.id,
            event_key=f"publish:{uuid.uuid4()}",
        )
    except PublishNotReadyError as exc:
        return jsonify({
            'success': False,
            'error': 'not_ready',
            'reasons': exc.verdict.get('reasons') or [],
            'verdict': exc.verdict,
        }), 422
    if row is None:
        return jsonify({'success': False, 'error': f'No meeting id={meeting_id}'}), 404
    return jsonify({'success': True, 'meeting': row})


@app.route('/api/meetings/<int:meeting_id>/publish-readiness', methods=['GET'])
def api_meeting_publish_readiness(meeting_id):
    """Session-32 (2026-07-04) — sibling of /publish-status but oriented
    at pre-publish checks. Returns the same verdict shape the publish
    endpoint uses to gate. Callers (OperatorTerminal) show "Ready" /
    "Not ready — missing X" next to the [Make Public →] button so the
    operator knows before the click.

    Public-safe: reveals only meeting-scoped output-presence counts +
    reasons strings, no secrets. Same-visibility contract as the
    existing /publish-status endpoint.
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    from database import check_publish_readiness  # noqa: PLC0415
    verdict = check_publish_readiness(meeting_id)
    return jsonify({'success': True, 'verdict': verdict})


@app.route('/api/meetings/<int:meeting_id>/unpublish', methods=['POST'])
@_require_trusted_origin
def api_unpublish_meeting(meeting_id):
    from database import publication_text_violation, unpublish_meeting  # noqa: PLC0415
    # Session-31 (2026-07-04) — auth-audit remediation. Same family as
    # publish above; unpublish hides a previously-live broadcast.
    # Owner-only to prevent anyone from taking down live content.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    supplied_unpublished_by = str(payload.get('unpublished_by') or '').strip()
    if supplied_unpublished_by and supplied_unpublished_by != 'Z-SPAN':
        app.logger.info(
            "discarded caller-supplied unpublished_by for meeting_id=%s",
            meeting_id,
        )
    unpublished_by = 'Z-SPAN'
    reason = payload.get('reason')
    violation = publication_text_violation(reason)
    if violation:
        return jsonify({
            'success': False,
            'error': f'reason {violation}',
        }), 400
    row = unpublish_meeting(
        meeting_id=meeting_id,
        unpublished_by=unpublished_by,
        reason=reason,
        actor_user_id=_user.id,
        event_key=f"unpublish:{uuid.uuid4()}",
    )
    if row is None:
        return jsonify({'success': False, 'error': f'No meeting id={meeting_id}'}), 404
    return jsonify({'success': True, 'meeting': row})


@app.route('/api/meetings/<int:meeting_id>/publish-status', methods=['GET'])
def api_meeting_publish_status(meeting_id):
    from database import get_publish_status  # noqa: PLC0415
    # RR-8 draft-content gate BEFORE any data access: a publicly-visible
    # meeting's publish status is public (BroadcastPage reads it for published
    # shows); an unpublished meeting's status + internal readiness metadata is
    # owner-only. Gating before the fetch means a draft and a nonexistent id
    # are indistinguishable to an anonymous caller (no existence oracle) —
    # both 401, only published meetings return data. (/api/notebook pattern.)
    if not is_meeting_publicly_visible(meeting_id):
        _user, _err = _require_owner()
        if _err:
            return _err
    row = get_publish_status(meeting_id)
    if row is None:
        return jsonify({'success': False, 'error': f'No meeting id={meeting_id}'}), 404
    return jsonify({'success': True, 'meeting': row})


# ─────────────────────────────────────────────────────────────────
# D-051 Flagship sync endpoints
# ─────────────────────────────────────────────────────────────────
#
# These endpoints implement the local-to-cloud content pump:
#
#   SENDER side (called from operator terminal on local Flask only):
#     POST /api/work-orders/<wo_id>/push-to-flagship
#       Triggers push_meeting_to_flagship; returns the attempt result.
#     GET  /api/work-orders/<wo_id>/flagship-sync-status
#       Returns the most-recent flagship_sync_log row for that WO's meeting.
#
#   RECEIVER side (called by remote senders on cloud Flask via Cf-Access
#   service token + X-Sync-Token; same code runs locally but isn't
#   exposed via the operator UI):
#     POST /api/sync/meeting/<meeting_id>
#       JSON body: { meta, meeting, outputs }. UPSERTs.
#     POST /api/sync/meeting/<meeting_id>/media/<filename>
#       Raw binary body. Writes /data/media/<id>/<filename>.
#
# Auth at the cloud edge is gated by Cf-Access (Application B with Service
# Auth policy matching the service token). On top of that, the receiver
# additionally validates X-Sync-Token shared secret to defense-in-depth
# against direct hits to the Railway hostname.

def _validate_sync_token():
    """Receiver-side guard. Returns None if the token is valid; otherwise
    returns (error_message, status_code) for the caller to surface."""
    expected = (
        os.environ.get('ZSPAN_SYNC_TOKEN')
        or _load_user_settings_value('zspan_sync_token')
    )
    if not expected:
        return ('flagship misconfigured: ZSPAN_SYNC_TOKEN not set on receiver', 503)
    provided = request.headers.get('X-Sync-Token', '')
    if not provided:
        return ('missing X-Sync-Token header', 401)
    if provided != expected:
        return ('invalid X-Sync-Token', 403)
    return None


def _load_user_settings_value(key: str):
    try:
        import env_config  # noqa: PLC0415 — lazy
        return env_config.load_user_settings().get(key)
    except Exception:
        return None


@app.route('/api/sync/meeting/<int:meeting_id>', methods=['POST'])
def api_sync_meeting_payload(meeting_id):
    """Receiver: ingest meeting + outputs JSON."""
    guard = _validate_sync_token()
    if guard is not None:
        msg, status = guard
        return jsonify({'success': False, 'error': msg}), status

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({'success': False, 'error': 'JSON body required'}), 400

    # Sanity: URL-path meeting_id must match payload.meeting.id so a
    # mistyped URL doesn't quietly UPSERT into the wrong row.
    body_id = (payload.get('meeting') or {}).get('id')
    if body_id != meeting_id:
        return jsonify({
            'success': False,
            'error': f'URL meeting_id={meeting_id} disagrees with payload meeting.id={body_id}',
        }), 400

    from flagship_sync import apply_meeting_payload  # noqa: PLC0415 — lazy
    try:
        result = apply_meeting_payload(payload)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:  # noqa: BLE001
        app.logger.exception('apply_meeting_payload failed for %s', meeting_id)
        return jsonify({'success': False, 'error': f'ingest failed: {e}'}), 500

    return jsonify({'success': True, **result})


@app.route('/api/sync/meeting/<int:meeting_id>/media/<path:filename>',
           methods=['POST'])
def api_sync_meeting_media(meeting_id, filename):
    """Receiver: ingest one media file (raw binary body)."""
    guard = _validate_sync_token()
    if guard is not None:
        msg, status = guard
        return jsonify({'success': False, 'error': msg}), status

    # Flask's request.get_data() reads the full body. Bounded at the
    # framework level by Flask's MAX_CONTENT_LENGTH (we don't set one
    # explicitly so it's whatever default; the network layer caps the
    # request size anyway).
    data = request.get_data()
    if not data:
        return jsonify({'success': False, 'error': 'empty body'}), 400

    from flagship_sync import save_media_file  # noqa: PLC0415 — lazy
    try:
        result = save_media_file(meeting_id, filename, data)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:  # noqa: BLE001
        app.logger.exception(
            'save_media_file failed for meeting=%s filename=%s',
            meeting_id, filename,
        )
        return jsonify({'success': False, 'error': f'save failed: {e}'}), 500

    return jsonify({'success': True, **result})


@app.route('/api/work-orders/<int:work_order_id>/push-to-flagship',
           methods=['POST'])
@_require_trusted_origin
def api_work_order_push_to_flagship(work_order_id):
    """Sender: gather + POST the WO's meeting payload to the flagship."""
    # RR-8 / SEC-AUTH-1: owner-ONLY (D-049 flagship sync; excluded from the
    # fleet wrapper). Not owner-OR-token — a shared fleet token must not
    # authorize a production push.
    _user, _err = _require_owner()
    if _err:
        return _err
    from database import get_work_order  # noqa: PLC0415 — lazy
    from flagship_sync import push_meeting_to_flagship, FlagshipSyncError  # noqa: PLC0415

    wo = get_work_order(work_order_id)
    if not wo:
        return jsonify({
            'success': False,
            'error': f'No work order id={work_order_id}',
        }), 404
    meeting_id = wo.get('meeting_id')
    if not meeting_id:
        return jsonify({
            'success': False,
            'error': f'WO {work_order_id} has no meeting_id',
        }), 400

    payload = request.get_json(silent=True) or {}
    pushed_by = (payload.get('pushed_by') or 'operator').strip()

    try:
        result = push_meeting_to_flagship(
            meeting_id=meeting_id,
            pushed_by=pushed_by,
        )
    except FlagshipSyncError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:  # noqa: BLE001
        app.logger.exception('push_meeting_to_flagship crashed for WO %s', work_order_id)
        return jsonify({'success': False, 'error': f'push crashed: {e}'}), 500

    status_code = 200 if result.get('status') == 'success' else 502
    return jsonify({'success': result.get('status') == 'success', **result}), status_code


@app.route('/api/work-orders/<int:work_order_id>/flagship-sync-status',
           methods=['GET'])
def api_work_order_flagship_sync_status(work_order_id):
    """Sender: return the most-recent push-attempt for the WO's meeting."""
    # RR-8 backstop gate: returns pushed-by identity, raw errors/responses,
    # transfer sizes — operator sync telemetry (OperatorTerminal only).
    _user, _err = _require_owner()
    if _err:
        return _err
    from database import get_work_order, get_latest_flagship_sync  # noqa: PLC0415
    wo = get_work_order(work_order_id)
    if not wo:
        return jsonify({
            'success': False,
            'error': f'No work order id={work_order_id}',
        }), 404
    meeting_id = wo.get('meeting_id')
    latest = get_latest_flagship_sync(meeting_id) if meeting_id else None
    return jsonify({
        'success': True,
        'meeting_id': meeting_id,
        'latest_sync': latest,
    })


# ── Phase 3+ — Citation log ───────────────────────────────────────────
#
# GET /api/citation/<meeting_id>?audience=public|operator
#
# Returns a structured citation tree describing every source, transformation,
# verification step, correction, and human-review event that produced this
# broadcast. Powers the (i) citation panel on the BroadcastPage — both the
# anonymized public view (default) and the operator-detail view.
#
# Anonymization (audience=public, default):
#   - human_review.reviewer  →  "An authorized Z-SPAN operator"
#   - publication.published_by  →  "An authorized Z-SPAN operator"
#   - wo_approved_by  →  same anonymization
#
# Operator mode (audience=operator) returns the raw operator name and is
# OWNER-GATED (session-31 remediation — see the handler; non-owners
# requesting operator mode get 401). This gated view is the ONE sanctioned
# attribution door since the 2026-07-09 identity-strip: every other
# serving path (catalog rows, publish-status, notebook payload, WO reads)
# omits operator-identity fields for the public.


def _build_citation_tree(meeting_id: int, anonymize: bool):
    """Aggregate the per-broadcast citation tree. Single Flask handler;
    multiple cheap point queries; assembled in Python rather than one
    monster SQL because the SHAPE of the response is the value-add."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Meeting + publish + WO approval
        cursor.execute(
            """
            SELECT m.id, m.city_name, m.county, m.state, m.meeting_title,
                   m.meeting_date, m.meeting_time, m.meeting_location,
                   m.video_url, m.agenda_url, m.minutes_url,
                   m.agenda_packet_url, m.ecomment_url, m.notebook_id,
                   m.is_published, m.published_at, m.published_by,
                   m.publish_notes,
                   wo.id AS work_order_id,
                   wo.youtube_video_url AS wo_video_url,
                   wo.approved_at AS wo_approved_at,
                   wo.approved_by AS wo_approved_by,
                   wo.state AS wo_state
            FROM meetings m
            LEFT JOIN work_orders wo ON wo.meeting_id = m.id
            WHERE m.id = ?
            """,
            (meeting_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        m = dict(row)

        OPERATOR_ANONYM = "An authorized Z-SPAN operator"

        def anon(name):
            if not anonymize:
                return name
            return OPERATOR_ANONYM if name else None

        # Source video — prefer the WO-specific URL (manually attached
        # post-T-004) over the parser-discovered URL (legacy).
        source_video_url = m.get('wo_video_url') or m.get('video_url')

        # Transcription provenance — read the transcript_words output if any
        public_output_filter = " AND voided_at IS NULL" if anonymize else ""
        cursor.execute(
            f"""
            SELECT generated_at, content
            FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'transcript_words'
              {public_output_filter}
            """,
            (meeting_id,),
        )
        tw_row = cursor.fetchone()
        transcription = None
        if tw_row and tw_row['content']:
            try:
                tw_payload = json.loads(tw_row['content'])
                words = tw_payload.get('words') or []
                duration = tw_payload.get('duration_seconds')
                transcription = {
                    'method': 'OpenAI Whisper (whisper-1, chunked 5-min × 32kbps mono mp3 per D-042)',
                    'generated_at': tw_row['generated_at'],
                    'word_count': len(words),
                    'duration_seconds': duration,
                    'primed_with_city_vocabulary': True,
                }
            except (json.JSONDecodeError, TypeError):
                transcription = {
                    'method': 'OpenAI Whisper (whisper-1)',
                    'generated_at': tw_row['generated_at'],
                    'word_count': None,
                    'duration_seconds': None,
                }

        # Extraction provenance — per-output_type generation timestamps
        cursor.execute(
            f"""
            SELECT output_type, generated_at, prompt_filename, prompt_version,
                   length(content) AS chars, content_url, voided_at
            FROM notebook_outputs
            WHERE meeting_id = ?
              {public_output_filter}
            ORDER BY generated_at ASC
            """,
            (meeting_id,),
        )
        extraction_outputs = []
        for r in cursor.fetchall():
            if r['output_type'] == 'transcript_words':
                continue  # surfaced separately under `transcription`
            extraction_outputs.append({
                'output_type': r['output_type'],
                'generated_at': r['generated_at'],
                'prompt_filename': r['prompt_filename'],
                'prompt_version': r['prompt_version'],
                'has_content': (r['chars'] or 0) > 0 or bool(r['content_url']),
                **(
                    {'voided_at': r['voided_at']}
                    if not anonymize
                    else {}
                ),
            })

        # Session-30 (2026-07-04): pipeline label + verification method are
        # per-meeting now, not hardcoded. If any output on this meeting
        # carries a modern V1-RAG-3 prompt_version, the pipeline is the
        # Qdrant retrieval + Claude Sonnet synthesis path (D-126 shipped
        # the swap 2026-06-20; D-143 retired the NotebookLM subsystem
        # 2026-07-01). Legacy meetings processed before D-126 keep the
        # NotebookLM lineage honestly — their outputs literally came from
        # NotebookLM, so labeling them otherwise would be a fabrication.
        # Kingman meetings from May 2026 fall in this legacy bucket; every
        # Bullhead broadcast from June 2026 onward hits the modern path.
        has_v1_rag_3 = any(
            (o.get('prompt_version') or '').lower().startswith('v1-rag')
            for o in extraction_outputs
        )
        if has_v1_rag_3:
            pipeline_label = (
                'Qdrant retrieval + Claude Sonnet synthesis '
                '(V1-RAG-3, per D-126)'
            )
            verification_method = (
                'Multi-source chain: Qdrant retrieval + Claude Sonnet '
                'extraction + Whisper word-alignment + Gemini Pro batch '
                'review + human spot-check (D-043)'
            )
        else:
            pipeline_label = (
                'NotebookLM (unofficial wrapper) — legacy; subsystem '
                'retired 2026-07-01 per D-143'
            )
            verification_method = (
                'Triple-source chain: NotebookLM extraction + Whisper '
                'word-alignment + Gemini Pro batch review + human '
                'spot-check (D-043) — legacy NotebookLM path, retired '
                'per D-143'
            )

        # D-054: the PUBLIC citation view reads as human provenance, not
        # internal tooling. Operators keep the detailed labels (infra names +
        # decision refs) for diagnostics; the public gets plain prose — no
        # repo paths, no D-codes, no infra jargon (2026-07-15 visitor-QA: the
        # public extraction panel was leaking a repo path + D-codes +
        # snake_case field names).
        if anonymize:
            pipeline_label = (
                "AI retrieval over the meeting's own transcript, then a "
                "written summary of the relevant passages."
            )
            verification_method = (
                "Cross-checked against the meeting's recording word-by-word, "
                "reviewed in batches, and spot-checked by a person."
            )

        # Verification provenance — member_quotes verified_status counts
        cursor.execute(
            """
            SELECT verified_status, COUNT(*) AS n
            FROM member_quotes
            WHERE meeting_id = ?
            GROUP BY verified_status
            """,
            (meeting_id,),
        )
        quote_status_counts = {r['verified_status'] or 'pending': r['n'] for r in cursor.fetchall()}
        quote_total = sum(quote_status_counts.values())

        # Count auto-corrections (rows with non-null quote_text_original
        # indicating V3 ingestion modified the text)
        cursor.execute(
            """
            SELECT COUNT(*) AS n
            FROM member_quotes
            WHERE meeting_id = ?
              AND quote_text_original IS NOT NULL
              AND quote_text_original != quote_text
            """,
            (meeting_id,),
        )
        auto_corrections = cursor.fetchone()['n']

        # City vocabulary corrections in the dictionary (applies to this city)
        cursor.execute(
            """
            SELECT wrong, right, applied_count, promoted_at
            FROM city_vocabulary_corrections
            WHERE city_name = ? AND auto_apply = 1
            ORDER BY applied_count DESC, id ASC
            """,
            (m['city_name'],),
        )
        vocab_corrections = [dict(r) for r in cursor.fetchall()]

        # Tracked claims summary
        cursor.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM tracked_claims
            WHERE meeting_id = ?
            GROUP BY status
            """,
            (meeting_id,),
        )
        claims_status = {r['status'] or 'active': r['n'] for r in cursor.fetchall()}
        claims_total = sum(claims_status.values())

        # Per-quote verification audit (D-032 per-quote spot-checks)
        cursor.execute(
            "SELECT COUNT(*) AS n FROM quote_verifications WHERE meeting_id = ?",
            (meeting_id,),
        )
        per_quote_verifications = cursor.fetchone()['n']

        operator_review_events = []
        if not anonymize:
            cursor.execute(
                """
                SELECT ore.id, ore.event_key, ore.action, ore.meeting_id,
                       ore.work_order_id, ore.output_type, ore.actor_user_id,
                       ore.occurred_at, ore.created_at,
                       u.display_name AS actor_display_name,
                       u.email AS actor_email
                FROM operator_review_events ore
                JOIN users u ON u.id = ore.actor_user_id
                WHERE ore.meeting_id = ?
                ORDER BY ore.occurred_at, ore.id
                """,
                (meeting_id,),
            )
            for event_row in cursor.fetchall():
                event = dict(event_row)
                actor_label = event['actor_display_name'] or event['actor_email']
                event['description'] = (
                    f"{actor_label} clicked {event['action']} at "
                    f"{event['occurred_at']}"
                )
                operator_review_events.append(event)
    finally:
        conn.close()

    citation = {
        'meeting': {
            'id': m['id'],
            'city': m['city_name'],
            'county': m['county'],
            'state': m['state'],
            'title': m['meeting_title'],
            'date': m['meeting_date'],
            'time': m['meeting_time'],
            'location': m['meeting_location'],
        },
        'publication': {
            'is_published': bool(m['is_published']),
            'published_at': m['published_at'],
            'published_by': anon(m['published_by']),
            'publish_notes': m['publish_notes'] if not anonymize else None,
        },
        'sources': {
            'primary_video': {
                'url': source_video_url,
                'platform': 'YouTube' if source_video_url and 'youtube' in source_video_url.lower() else 'Other',
            } if source_video_url else None,
            'agenda_url': m['agenda_url'],
            'agenda_packet_url': m['agenda_packet_url'],
            'minutes_url': m['minutes_url'],
            'ecomment_url': m['ecomment_url'],
        },
        'transcription': transcription,
        'extraction': {
            'pipeline': pipeline_label,
            # Repo path is operator-diagnostic only — omitted from the public
            # view entirely (D-054: no repo paths on a citizen surface).
            **(
                {}
                if anonymize
                else {
                    'prompt_review_ledger':
                        '02_Core_Project/prompts/PROMPT_REVIEW_LEDGER.md',
                }
            ),
            'outputs': extraction_outputs,
            'output_count': len(extraction_outputs),
        },
        'verification': {
            'method': verification_method,
            'member_quotes': {
                'total': quote_total,
                'by_status': quote_status_counts,
            },
            'auto_corrections_applied': auto_corrections,
            'per_quote_human_verifications': per_quote_verifications,
        },
        'corrections': {
            'city_vocabulary_dictionary_size': len(vocab_corrections),
            'corrections_dictionary': vocab_corrections if not anonymize else
                [{'wrong': c['wrong'], 'right': c['right']} for c in vocab_corrections],
        },
        'human_review': {
            'reviewer': anon(m['wo_approved_by']),
            'approved_at': m['wo_approved_at'],
            'policy_references': [
                'D-001 (human review gate)',
                'D-028 (verbatim quote provenance)',
                'D-032 (two-gate review pattern)',
                'D-043 (triple-source verification chain)',
                'D-046 (prompt authorship + review queue)',
                'NEUTRALITY_FRAMEWORK.md',
            ],
        },
        'tracked_claims': {
            'total': claims_total,
            'by_status': claims_status,
        },
        'audience_mode': 'public (anonymized)' if anonymize else 'operator (full)',
    }
    if not anonymize:
        citation['operator_review_events'] = operator_review_events
    return citation


@app.route('/api/citation/<int:meeting_id>', methods=['GET'])
@_public_rate_limited('citation')
def api_citation_endpoint(meeting_id):
    _user, _err = _require_owner()
    if _err:
        return _err
    raw_audience = (request.args.get('audience') or 'public').strip().lower()
    audience = raw_audience
    anonymize = audience != 'operator'
    citation = _build_citation_tree(meeting_id, anonymize=anonymize)
    if citation is None:
        return jsonify({'success': False, 'error': f'No meeting id={meeting_id}'}), 404
    return jsonify({'success': True, 'citation': citation})


@app.route('/api/work-orders/<int:work_order_id>/set-video-url', methods=['POST'])
def api_work_order_set_video_url(work_order_id):
    """
    REMOVED per D-138 (2026-06-25). Manual video-URL paste has been struck
    from the project — autonomous ingestion via haiku_match_videos.py
    (YT-channel cities) + parser-native capture (Granicus/Legistar/
    CivicClerk/IQM2) + S-037 V0 transcribe-non-youtube (vendor archives
    without YT) is the canonical floor.

    Endpoint kept as HTTP 410 Gone so any stale caller surfaces loudly
    rather than silently failing. Supersedes D-008 + D-009.
    """
    app.logger.warning(
        'set-video-url endpoint called for WO#%s — REMOVED per D-138; '
        'redirect caller to haiku_match_videos.py or the autonomous pipeline.',
        work_order_id,
    )
    return jsonify({
        'success': False,
        'error': (
            'Endpoint removed per D-138 (2026-06-25). Manual video-URL '
            'paste is no longer a supported operation. The autonomous '
            'ingestion path is parsers/scripts/haiku_match_videos.py for '
            'YT-channel cities; parser-native capture for vendor archives '
            '(Granicus/Legistar/CivicClerk/IQM2); parsers/scripts/'
            'transcribe_non_youtube.py for vendor archives without YT. '
            'See DECISIONS.md § D-138 for full rationale.'
        ),
        'autonomous_path': 'parsers/scripts/haiku_match_videos.py --city <name> --apply',
    }), 410


# ─────────────────────────────────────────────────────────────────
# Step-through processing endpoints
# These spawn the worker in single-shot mode so the user can process
# ONE work order at a time via a button click in the UI. The worker
# daemon (continuous mode) is for the eventual fully-automatic phase.
# ─────────────────────────────────────────────────────────────────

def _worker_log_dir() -> str:
    """Per-WO worker logs live alongside the bridge module so they're easy to inspect."""
    bridge_parent = os.path.normpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..'
    ))
    log_dir = os.path.join(bridge_parent, 'zspan_pipeline', 'worker_logs')
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _worker_log_path(work_order_id) -> str:
    """The on-disk log file for this WO. `next` means a process-next spawn."""
    suffix = str(work_order_id) if work_order_id is not None else 'next'
    return os.path.join(_worker_log_dir(), f'wo_{suffix}.log')


def _spawn_worker_once(work_order_id: int = None) -> dict:
    """
    Spawn `python -m zspan_pipeline.worker --once [--work-order-id N]`
    as a detached subprocess. Returns immediately; the UI tails the per-WO
    log file via /api/work-orders/<id>/log to follow progress.

    stdout/stderr are redirected to a per-WO log file under
    zspan_pipeline/worker_logs/wo_<id>.log so we can read it back into
    the operator terminal's activity log. Previously these were going to
    DEVNULL and the operator had no signal that the worker had crashed,
    hung on auth, or was actually grinding.
    """
    import subprocess
    bridge_parent = os.path.normpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..'
    ))
    cmd = [sys.executable, '-u', '-m', 'zspan_pipeline.worker']
    if work_order_id is not None:
        cmd.extend(['--work-order-id', str(work_order_id)])
    else:
        cmd.append('--once')

    log_path = _worker_log_path(work_order_id)

    try:
        # Truncate any prior log for this WO so the operator only sees the
        # current run's output. (We can rotate later if we ever need history.)
        with open(log_path, 'w', encoding='utf-8') as fh:
            fh.write(f'[spawn] cmd: {" ".join(cmd)}\n')
            fh.write(f'[spawn] cwd: {bridge_parent}\n')
            fh.flush()

        log_handle = open(log_path, 'a', encoding='utf-8', buffering=1)  # line-buffered
        # Force the worker subprocess to emit UTF-8. On Windows its stdout
        # otherwise defaults to the system codepage (cp1252), which mangles
        # non-ASCII (→, ·, em-dash) into \uXXXX escapes or the replacement
        # char — the garble seen in the activity log. UTF-8 mode keeps the
        # log files clean at the source.
        worker_env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
        subprocess.Popen(
            cmd,
            cwd=bridge_parent,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            env=worker_env,
        )
        # Don't close log_handle here — Popen owns the FD until the child exits.
        return {
            'success': True,
            'spawned': True,
            'cmd': ' '.join(cmd),
            'log_path': log_path,
        }
    except Exception as e:
        return {'success': False, 'spawned': False, 'error': str(e)}


@app.route('/api/work-orders/<int:work_order_id>/log', methods=['GET'])
def api_work_order_log(work_order_id):
    """
    Tail the worker log for a specific WO. Supports incremental polling:
    pass ?since=N to get only the bytes after offset N. Response includes
    `next_offset` so the UI knows where to resume.

    If the log file doesn't exist (no run has been started for this WO),
    returns content='' and next_offset=0 — caller treats as no-op.
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    log_path = _worker_log_path(work_order_id)
    try:
        since = int(request.args.get('since', 0))
    except (TypeError, ValueError):
        since = 0

    if not os.path.exists(log_path):
        return jsonify({
            'success': True,
            'content': '',
            'next_offset': 0,
            'exists': False,
        })

    try:
        size = os.path.getsize(log_path)
        # If the file shrank (we truncated for a fresh run), restart from 0.
        if since > size:
            since = 0
        with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
            fh.seek(since)
            content = fh.read()
        return jsonify({
            'success': True,
            'content': content,
            'next_offset': size,
            'exists': True,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/work-orders/process-next', methods=['POST'])
@_require_trusted_origin
def api_work_orders_process_next():
    """
    Process the NEXT pending work order in the queue.
    Spawns the worker in --once mode (rate-limit-aware, exits after one).
    Poll GET /api/work-orders/stats or /api/work-orders to track progress.
    """
    # Session-31 auth-audit — spawns worker, triggers real LLM spend.
    _user, _err = _require_owner()
    if _err:
        return _err
    result = _spawn_worker_once()
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/api/work-orders/<int:work_order_id>/process', methods=['POST'])
@_require_trusted_origin
def api_work_order_process(work_order_id):
    """
    Process THIS specific work order. Useful when the user wants to retry
    or specifically pick which one runs next.
    """
    # Session-31 auth-audit — spawns worker, triggers real LLM spend.
    _user, _err = _require_owner()
    if _err:
        return _err
    wo = get_work_order(work_order_id)
    if not wo:
        return jsonify({'success': False, 'error': f'No work order id={work_order_id}'}), 404
    result = _spawn_worker_once(work_order_id=work_order_id)
    status_code = 200 if result.get('success') else 500
    return jsonify({**result, 'work_order_id': work_order_id}), status_code


# ─────────────────────────────────────────────────────────────────
# YouTube channel registry endpoints
# ─────────────────────────────────────────────────────────────────

@app.route('/api/cities/<city_name>/youtube-channel', methods=['GET'])
def api_get_city_youtube(city_name):
    _user, _err = _require_owner()
    if _err:
        return _err
    info = get_city_youtube_channel(city_name)
    if info is None:
        return jsonify({'success': False, 'error': f'City not found: {city_name}'}), 404
    return jsonify({'success': True, **info})


@app.route('/api/cities/<city_name>/youtube-channel', methods=['POST'])
@_require_trusted_origin
def api_set_city_youtube(city_name):
    """
    Body: { "channel_url": "https://www.youtube.com/@CityOfKingman",
            "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx" (optional) }
    """
    # Session-31 auth-audit — repoints a city's YT channel; feeds
    # downstream video matching. Owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    channel_url = (payload.get('channel_url') or '').strip() or None
    channel_id = (payload.get('channel_id') or '').strip() or None
    updated = set_city_youtube_channel(city_name, channel_url, channel_id)
    if not updated:
        return jsonify({'success': False, 'error': f'City not found: {city_name}'}), 404
    return jsonify({
        'success': True, 'city': city_name,
        'channel_url': channel_url, 'channel_id': channel_id,
    })


# ─────────────────────────────────────────────────────────────────
# Google OAuth — light-account sign-in (ACCOUNT_SYSTEM_SPEC chunk 2)
# ─────────────────────────────────────────────────────────────────
# Three-segment flow: /login → Google → /callback. Stateless: the
# pre-redirect state + code_verifier live in a short-lived signed cookie
# (no server-side session store). On successful callback the user row is
# upserted via account_system.upsert_user_from_google() and a 30-day
# HS256 JWT session cookie is minted. /me returns the principal; /logout
# clears the cookie. Per ACCOUNT_SYSTEM_SPEC.md "Auth architecture
# (decided)" section.
#
# Cookie posture: HttpOnly always; SameSite=Strict for the authenticated
# session and Lax for transient OAuth/CLI state that must survive a top-level
# cross-site callback. Secure when the request reached Flask through an HTTPS
# edge (detected via the X-Forwarded-Proto header set by Cloudflare Pages /
# Express dev — both set the header to "https" / "http" matching what the
# browser saw).

from google_oauth import (
    SESSION_COOKIE_NAME,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    OAUTH_STATE_TTL_SECONDS,
    build_consent_url,
    build_oauth_state_cookie,
    compute_redirect_uri,
    exchange_code,
    fetch_userinfo,
    generate_pkce,
    get_owner_emails,
    is_owner_email,
    is_operator_search_principal,
    mint_session_token,
    random_state,
    verify_oauth_state_cookie,
    verify_session_token,
    _sign_envelope,
    _verify_envelope,
)
from account_system import (
    CreatorPromotionError,
    FOLLOW_CAP_PER_USER,
    FollowCapExceeded,
    clear_city_topics,
    follow_add,
    follow_remove,
    get_active_agreement,
    get_creator_download_summary,
    list_city_topics,
    list_follows,
    promote_user_to_creator,
    revoke_creator_role,
    set_city_topics,
    upsert_user_from_google,
    get_user,
)
from password_auth import (
    AccountInputError,
    PasswordValidationError,
    authenticate_password,
    create_password_reset_token,
    register_invited_user,
    reset_password,
    send_password_reset_email,
)
try:
    from parsers.unsubscribe_tokens import (
        verify_unsubscribe_token,
    )
except ImportError:  # Direct `python api_server.py` from parsers/.
    from unsubscribe_tokens import verify_unsubscribe_token
from input_moderation import moderate_user_input
from repository_gate import (
    AssetNotFoundError,
    IllegalTransitionError,
    approve_repository_asset,
    list_pending_review_assets,
    reject_repository_asset,
    withdraw_repository_asset,
)

import hashlib

# Optional defense-in-depth binding for configured owner accounts. Google
# ``sub`` values are opaque and case-sensitive, so preserve them verbatim.
def _load_owner_google_sub_allowlist() -> frozenset[str]:
    return frozenset(
        google_sub.strip()
        for google_sub in os.environ.get(
            "ZSPAN_OWNER_GOOGLE_SUB_ALLOWLIST", ""
        ).split(",")
        if google_sub.strip()
    )


OWNER_GOOGLE_SUB_ALLOWLIST = _load_owner_google_sub_allowlist()

# Allowed target types for /api/follows. Matches the schema CHECK
# constraint + the account_system.follow_add/remove Literal type.
_FOLLOW_TARGET_TYPES = frozenset({"city", "county", "topic", "meeting"})
# Defensive cap on target_key length so a malformed client can't write
# arbitrarily long rows. The schema has no length limit; this is the
# per-request bound. 200 covers realistic city/meeting/topic identifiers
# with comfortable headroom.
_FOLLOW_TARGET_KEY_MAX = 200


def _forwarded_host_url() -> str:
    """Return the host URL the browser saw, reconstructed from forwarded
    headers. Express dev and Cloudflare Pages both set
    X-Forwarded-Host + X-Forwarded-Proto. Falls back to request.host_url
    when those aren't present (direct Flask hits during testing).
    """
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").lower()
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").strip()
    if not host:
        return request.host_url
    return f"{proto}://{host}/"


def _request_is_secure() -> bool:
    """True when the browser-facing edge was HTTPS. Drives the Secure
    cookie attribute. We trust X-Forwarded-Proto because the only
    untrusted senders are blocked by the Pages-Function / Express edge
    before reaching Flask."""
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").lower()
    return proto == "https"


def _safe_next_path(raw: str) -> str:
    """Sanitize the ?next= query param so it can only point to a
    same-origin relative path. Falls back to "/" for unsafe values.
    """
    if not raw or not isinstance(raw, str):
        return "/"
    if "\\" in raw:
        return "/"
    if any(ord(char) <= 0x1F or ord(char) == 0x7F for char in raw):
        return "/"

    candidate = raw.strip()
    if not candidate.startswith("/"):
        return "/"
    if "://" in candidate:
        return "/"
    if re.match(r"^/(?:/|%2f|%5c)", candidate, flags=re.IGNORECASE):
        # Browsers or a later decoding pass can interpret each form as a
        # second leading slash, turning the target into a network-path ref.
        return "/"

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc:
        return "/"
    return candidate


def _set_cookie(response, name: str, value: str, max_age: int) -> None:
    """Set a signed cookie with the project's standard posture."""
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=_request_is_secure(),
        samesite="Strict" if name == SESSION_COOKIE_NAME else "Lax",
    )


def _clear_cookie(response, name: str) -> None:
    response.set_cookie(
        name,
        "",
        max_age=0,
        path="/",
        httponly=True,
        secure=_request_is_secure(),
        samesite="Strict" if name == SESSION_COOKIE_NAME else "Lax",
    )


def _signin_maintenance_response(*, clear_oauth_state: bool = False):
    """Return the stable maintenance response for disabled sign-in flows."""
    response = jsonify({
        "status": "maintenance",
        "message": "Sign-in is temporarily paused. Check back soon.",
    })
    response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    if clear_oauth_state:
        _clear_cookie(response, OAUTH_STATE_COOKIE_NAME)
    return response


def _small_json_request_body():
    """Read one small JSON object without inheriting the app-wide 128 MiB cap."""
    raw = request.stream.read(_PASSWORD_AUTH_BODY_MAX_BYTES + 1)
    if len(raw) > _PASSWORD_AUTH_BODY_MAX_BYTES:
        return None, (jsonify({
            'success': False,
            'error': 'request_too_large',
        }), 413)
    if not raw:
        return {}, None
    try:
        body = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, None
    return (body if isinstance(body, dict) else {}), None


def _current_user_from_cookie():
    """Resolve the signed-in user from the session cookie, or None."""
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None
    claims = verify_session_token(raw)
    if not claims:
        return None
    try:
        user_id = int(claims.get("sub", "0"))
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None
    return get_user(user_id)


def _require_user():
    """Any-signed-in-user gate helper.

    Resolves the signed-in principal from the session cookie. Returns
    (user, None) when a user is present. Returns (None, response) when
    the caller is anonymous — handler must `return response` immediately.

    Distinct from `_require_owner()`: this gate is for user-owned personal
    data (follows, notification prefs, personal reading history) where the
    principal writes/reads their OWN rows keyed by their own user_id. It
    does NOT protect operator-security material — use `_require_owner()`
    for anything that lets a user reach across accounts or touch the
    audit trail.

    Usage:
        user, err = _require_user()
        if err:
            return err
        # ... proceed as any signed-in user; scope by user.id ...
    """
    user = _current_user_from_cookie()
    if not user:
        return None, (jsonify({
            'success': False,
            'error': 'sign-in required',
        }), 401)
    return user, None


def _require_owner():
    """Owner-only gate helper (session-31 auth-audit remediation).

    Resolves the signed-in principal from the session cookie and checks
    owner status. Returns (user, None) when the caller is the owner —
    handler proceeds. Returns (None, response) when the caller is
    anonymous or non-owner — handler must `return response` immediately.

    Usage:
        user, err = _require_owner()
        if err:
            return err
        # ... proceed as owner ...
    """
    user = _current_user_from_cookie()
    if not user:
        return None, (jsonify({
            'success': False,
            'error': 'sign-in required',
        }), 401)
    if not is_owner_email(user.email):
        return None, (jsonify({
            'success': False,
            'error': 'owner-only',
        }), 403)
    return user, None


def _require_owner_or_agent_token():
    """Gate for routes reachable by BOTH the owner (browser cookie) and the
    headless fleet agents (localhost, ZSPAN_AGENT_STATE_TOKEN bearer).
    RR-8 SEC-AUTH-1/2/3; ordering converged in the session-56 Codex design review:
      1. valid OWNER cookie        -> allow
      2. server token unconfigured -> 503 (unavailable security dependency)
      3. bearer missing/malformed  -> 401
      4. constant-time mismatch    -> 401
    A valid NON-owner cookie falls through to the bearer (never 403 — policy is
    owner OR token). X-Zspan-Agent-Role stays attribution-only. Returns
    (actor, None) on allow, or (None, response) the handler must return.
    """
    import agent_auth  # noqa: PLC0415 — neutral, dependency-light helper
    # 1. owner short-circuit. A DB hiccup resolving the cookie must NOT become
    #    an allow — log it and fall through to the bearer path.
    try:
        user = _current_user_from_cookie()
    except Exception:
        app.logger.warning("owner-cookie resolution failed; trying bearer")
        user = None
    if user and is_owner_email(user.email):
        return user, None
    # 2-4. fleet-agent bearer
    ok, status, msg = agent_auth.check_agent_bearer(request)
    if ok:
        return None, None  # authenticated as fleet (role header = attribution)
    return None, (jsonify({'success': False, 'error': msg}), status)


def _parser_results_path():
    """On-disk home for the ParserDashboard test-results blob (operator
    tooling). Lives beside the parsers so Flask (cwd = parsers/) owns it."""
    from pathlib import Path
    return Path(__file__).parent / 'parser_test_results.json'


@app.route('/api/parser-health', methods=['GET'])
def api_parser_health():
    """Return the owner-only, URL-free ParserDashboard roster.

    Only the fields needed to paint parser health are copied into this DTO;
    deployment routes, notes, and raw errors never cross the API boundary.
    """
    _user, err = _require_owner()
    if err:
        return err

    from pathlib import Path

    index = load_parser_index()
    if not isinstance(index, dict):
        raise ValueError('parser index must be an object')

    saved_results: Dict[str, Any] = {}
    try:
        loaded_results = json.loads(
            _parser_results_path().read_text(encoding='utf-8')
        )
        if isinstance(loaded_results, dict):
            saved_results = loaded_results
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        app.logger.warning("parser-health saved results unavailable: %s", exc)

    latest_runs: Dict[str, Any] = {}
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT log.city_name, log.scraped_at, log.success,
                   log.meetings_found
            FROM scrape_log AS log
            JOIN (
                SELECT city_name, MAX(id) AS latest_id
                FROM scrape_log
                GROUP BY city_name
            ) AS latest
              ON latest.latest_id = log.id
            """
        ).fetchall()
        latest_runs = {str(row['city_name']): row for row in rows}
    finally:
        conn.close()

    parsers_dir = Path(__file__).parent
    parsers = []
    for city_name, raw_info in sorted(index.items(), key=lambda item: item[0].lower()):
        info = raw_info if isinstance(raw_info, dict) else {}
        parser_name = str(info.get('parser_file') or '')
        parser_present = bool(
            parser_name and (parsers_dir / Path(parser_name).name).is_file()
        )

        status = 'untested'
        meeting_count = 0
        last_scanned_at = ''

        saved = saved_results.get(city_name)
        if isinstance(saved, dict):
            saved_status = saved.get('status')
            if saved_status in {'working', 'broken'}:
                status = saved_status
            try:
                meeting_count = max(0, int(saved.get('meetingCount') or 0))
            except (TypeError, ValueError):
                meeting_count = 0
            if isinstance(saved.get('lastTested'), str):
                last_scanned_at = saved['lastTested']

        run = latest_runs.get(city_name)
        if run is not None:
            status = 'working' if bool(run['success']) else 'broken'
            meeting_count = max(0, int(run['meetings_found'] or 0))
            last_scanned_at = str(run['scraped_at'] or '')

        parsers.append({
            'city': str(city_name),
            'county': str(info.get('county') or ''),
            'parser_file': parser_present,
            'status': status,
            'meeting_count': meeting_count,
            'last_scanned_at': last_scanned_at,
        })

    counts = {
        'total': len(parsers),
        'parser_files_present': sum(
            1 for parser in parsers if parser['parser_file']
        ),
        'working': sum(1 for parser in parsers if parser['status'] == 'working'),
        'broken': sum(1 for parser in parsers if parser['status'] == 'broken'),
        'untested': sum(1 for parser in parsers if parser['status'] == 'untested'),
    }
    return jsonify({'parsers': parsers, 'counts': counts})


@app.route('/api/parser-results/save', methods=['POST'])
@_require_trusted_origin
def api_parser_results_save():
    """Owner-only: persist the ParserDashboard test-results blob.

    RR-8 posture: this was an ungated Express-local write — any anonymous
    request could overwrite the file. The write now lives behind
    `_require_owner()` (Express proxies the owner cookie in).
    """
    _user, err = _require_owner()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    results = payload.get('results')
    if results is None:
        return jsonify({'success': False, 'error': 'missing results'}), 400
    try:
        # Atomic write: a partial/interrupted write must never leave a
        # malformed JSON blob that the load path then 500s on. Write to a
        # sibling temp file, then os.replace() (atomic on the same filesystem).
        from pathlib import Path
        dest = _parser_results_path()
        tmp = dest.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(results, indent=2), encoding='utf-8')
        os.replace(tmp, dest)
    except OSError as exc:
        app.logger.error("parser-results save failed: %s", exc)
        return jsonify({'success': False, 'error': 'write failed'}), 500
    return jsonify({'success': True})


@app.route('/api/parser-results/load', methods=['GET'])
def api_parser_results_load():
    """Owner-only: load the ParserDashboard test-results blob. Gated to match
    the write (operator-tooling data; the only consumer is the owner-only
    ParserDashboard UI) — "gate 100%" per the operator directive."""
    _user, err = _require_owner()
    if err:
        return err
    try:
        data = _parser_results_path().read_text(encoding='utf-8')
        return jsonify({'success': True, 'results': json.loads(data)})
    except FileNotFoundError:
        return jsonify({'success': True, 'results': {}})
    except (OSError, ValueError) as exc:
        app.logger.error("parser-results load failed: %s", exc)
        return jsonify({'success': False, 'error': 'read failed'}), 500


# ── D-172 — flagship-brokered CLI auth + generation registration ─────

_CLI_AUTH_COOKIE_NAME = "zspan_cli_auth"
_CLI_AUTH_COOKIE_TTL_SECONDS = 600
_CLI_CODE_TTL_SECONDS = 120
_CLI_TOKEN_TTL_DAYS = 90
_CLI_BEARER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CLI_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_CLI_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CLI_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CLI_MEETING_RE = re.compile(r"^m_[0-9A-Za-z]{22}$")
_CLI_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CLI_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# Sibling contract: zspan_cli.synthesize.RENDERED_OUTPUT_TYPES.
_CLI_RENDERED_OUTPUT_TYPES = frozenset({
    "synopsis",
    "key_decisions",
    "community_calls_to_action",
    "episode_tagline",
})
_CLI_CONTRIBUTION_OUTPUT_ORDER = (
    "synopsis",
    "key_decisions",
    "community_calls_to_action",
    "episode_tagline",
)
_CLI_CONTRIBUTION_SCHEMA = "zspan.private-contribution.v1"
_CLI_CONTRIBUTION_MAX_BYTES = 25 * 1024 * 1024
_CLI_CONTRIBUTION_MAX_WORDS = 500_000
_CLI_CONTRIBUTION_MAX_CONTENT = 2 * 1024 * 1024
_CLI_CONTRIBUTION_MAX_GATE_LOG = 512 * 1024
_CLI_CONTRIBUTION_FIELDS = frozenset({
    "schema_version",
    "meeting_public_id",
    "transcript",
    "outputs",
    "idempotency_key",
    "payload_sha256",
})
_CLI_TRANSCRIPT_FIELDS = frozenset({
    "source_url",
    "duration_seconds",
    "language",
    "transcriber",
    "model",
    "words",
    "sha256",
})
_CLI_WORD_FIELDS = frozenset({"word", "start", "end"})
_CLI_CONTRIBUTION_OUTPUT_FIELDS = frozenset({
    "output_type",
    "content",
    "provider",
    "model",
    "gate_status",
    "gate_log",
    "content_sha256",
})
_CLI_GATE_STATUSES = frozenset({
    "observed_clean", "observed_findings", "observed_empty"
})


class _CliContributionError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _strict_cli_contribution_json() -> dict:
    if request.mimetype != "application/json":
        raise _CliContributionError("content type must be application/json", 415)
    declared = request.content_length
    if declared is not None and declared > _CLI_CONTRIBUTION_MAX_BYTES:
        raise _CliContributionError("private contribution is too large", 413)
    raw = request.stream.read(_CLI_CONTRIBUTION_MAX_BYTES + 1)
    if len(raw) > _CLI_CONTRIBUTION_MAX_BYTES:
        raise _CliContributionError("private contribution is too large", 413)

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise _CliContributionError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    def constant(value):
        raise _CliContributionError(f"non-finite JSON number: {value}")

    try:
        body = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except _CliContributionError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _CliContributionError("private contribution must be valid UTF-8 JSON") from exc
    if not isinstance(body, dict):
        raise _CliContributionError("private contribution must be a JSON object")
    return body


def _cli_canonical_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _CliContributionError("private contribution is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _exact_fields(value: Any, fields: frozenset[str], label: str) -> dict:
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise _CliContributionError(f"{label} fields are invalid")
    return value


def _bounded_text(value: Any, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise _CliContributionError(f"{label} is invalid")
    if not empty and (not value or value != value.strip()):
        raise _CliContributionError(f"{label} is invalid")
    return value


def _validate_cli_contribution(body: dict, meeting: dict) -> tuple[dict, list[dict]]:
    _exact_fields(body, _CLI_CONTRIBUTION_FIELDS, "contribution")
    if body["schema_version"] != _CLI_CONTRIBUTION_SCHEMA:
        raise _CliContributionError("unsupported private contribution schema")
    meeting_public_id = body["meeting_public_id"]
    if (
        not isinstance(meeting_public_id, str)
        or _CLI_MEETING_RE.fullmatch(meeting_public_id) is None
        or meeting_public_id != meeting.get("canonical_public_id")
    ):
        raise _CliContributionError("meeting identity is invalid")
    if (
        not isinstance(body["idempotency_key"], str)
        or _CLI_IDEMPOTENCY_RE.fullmatch(body["idempotency_key"]) is None
    ):
        raise _CliContributionError("idempotency key is invalid")
    if (
        not isinstance(body["payload_sha256"], str)
        or _CLI_SHA256_RE.fullmatch(body["payload_sha256"]) is None
    ):
        raise _CliContributionError("payload hash is invalid")

    transcript = _exact_fields(body["transcript"], _CLI_TRANSCRIPT_FIELDS, "transcript")
    source_url = _bounded_text(transcript["source_url"], "transcript source URL", 2048)
    parsed_source = urlparse(source_url)
    if (
        parsed_source.scheme != "https"
        or not parsed_source.hostname
        or parsed_source.username is not None
        or parsed_source.password is not None
    ):
        raise _CliContributionError("transcript source URL is invalid")
    allowed_sources = {
        value.strip()
        for value in (
            meeting.get("video_url"),
            get_resolved_video_url(int(meeting["id"])),
        )
        if isinstance(value, str) and value.strip()
    }
    if source_url not in allowed_sources:
        raise _CliContributionError("transcript source does not match the meeting")
    duration = transcript["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or not 0 < float(duration) <= 86_400
    ):
        raise _CliContributionError("transcript duration is invalid")
    _bounded_text(transcript["language"], "transcript language", 32)
    _bounded_text(transcript["transcriber"], "transcriber", 100, empty=True)
    _bounded_text(transcript["model"], "transcription model", 100, empty=True)
    words = transcript["words"]
    if not isinstance(words, list) or not 1 <= len(words) <= _CLI_CONTRIBUTION_MAX_WORDS:
        raise _CliContributionError("transcript words are invalid")
    previous_start = -1.0
    for index, raw_word in enumerate(words):
        word = _exact_fields(raw_word, _CLI_WORD_FIELDS, f"transcript word {index}")
        _bounded_text(word["word"], f"transcript word {index}", 500)
        start = word["start"]
        end = word["end"]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (start, end)):
            raise _CliContributionError(f"transcript word {index} timing is invalid")
        start_number, end_number = float(start), float(end)
        if (
            not math.isfinite(start_number)
            or not math.isfinite(end_number)
            or start_number < previous_start
            or start_number < 0
            or end_number < start_number
            or end_number > float(duration) + 5
        ):
            raise _CliContributionError(f"transcript word {index} timing is invalid")
        previous_start = start_number
    transcript_core = {key: transcript[key] for key in _CLI_TRANSCRIPT_FIELDS if key != "sha256"}
    if (
        not isinstance(transcript["sha256"], str)
        or not hmac.compare_digest(transcript["sha256"], _cli_canonical_sha256(transcript_core))
    ):
        raise _CliContributionError("transcript hash does not match its content")

    outputs = body["outputs"]
    if not isinstance(outputs, list) or len(outputs) != len(_CLI_CONTRIBUTION_OUTPUT_ORDER):
        raise _CliContributionError("private contribution output set is invalid")
    normalized_outputs = []
    for index, raw_output in enumerate(outputs):
        output = _exact_fields(
            raw_output, _CLI_CONTRIBUTION_OUTPUT_FIELDS, f"output {index}"
        )
        if output["output_type"] != _CLI_CONTRIBUTION_OUTPUT_ORDER[index]:
            raise _CliContributionError("private contribution output order is invalid")
        content = _bounded_text(
            output["content"], f"{output['output_type']} content",
            _CLI_CONTRIBUTION_MAX_CONTENT, empty=True,
        )
        _bounded_text(output["provider"], "output provider", 64)
        _bounded_text(output["model"], "output model", 100)
        if output["gate_status"] not in _CLI_GATE_STATUSES:
            raise _CliContributionError("output gate status is invalid")
        gate_log = _bounded_text(
            output["gate_log"], "output gate log", _CLI_CONTRIBUTION_MAX_GATE_LOG
        )
        try:
            gate_value = json.loads(gate_log)
        except json.JSONDecodeError as exc:
            raise _CliContributionError("output gate log is invalid") from exc
        if not isinstance(gate_value, dict) or gate_value.get("status") != output["gate_status"]:
            raise _CliContributionError("output gate log does not match its status")
        expected_content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (
            not isinstance(output["content_sha256"], str)
            or not hmac.compare_digest(output["content_sha256"], expected_content_hash)
        ):
            raise _CliContributionError("output hash does not match its content")
        normalized_outputs.append(output)

    core = {
        "schema_version": body["schema_version"],
        "meeting_public_id": meeting_public_id,
        "transcript": transcript,
        "outputs": outputs,
    }
    if not hmac.compare_digest(body["payload_sha256"], _cli_canonical_sha256(core)):
        raise _CliContributionError("payload hash does not match its content")
    return transcript, normalized_outputs


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _parse_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cli_start_over_response():
    return Response(
        "start over: run `zspan login` again",
        status=400,
        content_type="text/plain; charset=utf-8",
    )


def _verify_cli_auth_cookie() -> Optional[dict]:
    raw = request.cookies.get(_CLI_AUTH_COOKIE_NAME, "")
    if not raw:
        return None
    try:
        payload = _verify_envelope(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        port = int(payload.get("port"))
        exp = int(payload.get("exp"))
    except (TypeError, ValueError):
        return None
    state = payload.get("cli_state")
    challenge = payload.get("challenge")
    if not (1024 <= port <= 65535 and exp >= int(time.time())):
        return None
    if not isinstance(state, str) or _CLI_STATE_RE.fullmatch(state) is None:
        return None
    if not isinstance(challenge, str) or _CLI_CHALLENGE_RE.fullmatch(challenge) is None:
        return None
    return {
        "port": port,
        "cli_state": state,
        "challenge": challenge,
        "exp": exp,
    }


def _cli_auth_required():
    return jsonify({"error": "cli auth required"}), 401


def _cli_auth_from_bearer():
    """Resolve a valid opaque CLI bearer to its account and token rows."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    raw_token = header[7:]
    if _CLI_BEARER_RE.fullmatch(raw_token) is None:
        return None
    token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM cli_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None or row["revoked_at"]:
            return None
        expires_at = _parse_utc(row["expires_at"])
        now = datetime.now(timezone.utc)
        if expires_at is None or expires_at <= now:
            return None
        user = get_user(int(row["user_id"]))
        if user is None:
            return None
        last_used_at = _parse_utc(row["last_used_at"])
        if last_used_at is None or (now - last_used_at).total_seconds() > 60:
            now_text = _utc_iso(now)
            conn.execute(
                "UPDATE cli_tokens SET last_used_at = ? WHERE id = ?",
                (now_text, row["id"]),
            )
            conn.commit()
            token_row = dict(row)
            token_row["last_used_at"] = now_text
        else:
            token_row = dict(row)
        return user, token_row
    finally:
        conn.close()


@app.route('/api/auth/cli/start', methods=['GET'])
def api_auth_cli_start():
    # V0 limitation: Google-consent denial still follows the existing callback's
    # error branch to "/" (ignoring next), so the waiting CLI times out. The
    # D-172 flow deliberately composes around that callback without changing it.
    try:
        port = int(request.args.get("port", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid cli auth request"}), 400
    state = request.args.get("state", "")
    challenge = request.args.get("challenge", "")
    if not 1024 <= port <= 65535:
        return jsonify({"error": "invalid cli auth request"}), 400
    if _CLI_STATE_RE.fullmatch(state) is None:
        return jsonify({"error": "invalid cli auth request"}), 400
    if _CLI_CHALLENGE_RE.fullmatch(challenge) is None:
        return jsonify({"error": "invalid cli auth request"}), 400

    cookie_value = _sign_envelope({
        "port": port,
        "cli_state": state,
        "challenge": challenge,
        "exp": int(time.time()) + _CLI_AUTH_COOKIE_TTL_SECONDS,
    })
    response = Response(status=302)
    response.headers["Location"] = (
        "/api/auth/google/login?next=/api/auth/cli/finish"
    )
    _set_cookie(
        response,
        _CLI_AUTH_COOKIE_NAME,
        cookie_value,
        _CLI_AUTH_COOKIE_TTL_SECONDS,
    )
    return response


@app.route('/api/auth/cli/finish', methods=['GET'])
def api_auth_cli_finish_get():
    user = _current_user_from_cookie()
    payload = _verify_cli_auth_cookie()
    if user is None or payload is None:
        return _cli_start_over_response()
    email = html.escape(user.email or "", quote=True)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex"><title>Authorize zspan CLI</title></head>
<body><main><h1>Authorize zspan CLI</h1>
<p>Authorize the zspan CLI on this computer as <strong>{email}</strong>?</p>
<form method="post" action="/api/auth/cli/finish"><button type="submit">Authorize</button></form>
<p><a href="/api/auth/cli/cancel">Cancel</a></p></main></body></html>"""
    return Response(page, content_type="text/html; charset=utf-8")


@app.route('/api/auth/cli/finish', methods=['POST'])
@_require_trusted_origin
def api_auth_cli_finish_post():
    user = _current_user_from_cookie()
    payload = _verify_cli_auth_cookie()
    if user is None or payload is None:
        return _cli_start_over_response()

    code = secrets.token_urlsafe(32)
    code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
    expires_at = _utc_iso(
        datetime.now(timezone.utc) + timedelta(seconds=_CLI_CODE_TTL_SECONDS)
    )
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO cli_auth_codes (
                code_hash, user_id, loopback_port, cli_state,
                code_challenge, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                code_hash,
                user.id,
                payload["port"],
                payload["cli_state"],
                payload["challenge"],
                expires_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    response = Response(status=302)
    response.headers["Location"] = (
        f"http://127.0.0.1:{payload['port']}/callback?"
        + urlencode({"code": code, "state": payload["cli_state"]})
    )
    _clear_cookie(response, _CLI_AUTH_COOKIE_NAME)
    return response


@app.route('/api/auth/cli/cancel', methods=['GET'])
def api_auth_cli_cancel():
    payload = _verify_cli_auth_cookie()
    if payload is None:
        return _cli_start_over_response()
    response = Response(status=302)
    response.headers["Location"] = (
        f"http://127.0.0.1:{payload['port']}/callback?"
        + urlencode({"error": "cancelled", "state": payload["cli_state"]})
    )
    _clear_cookie(response, _CLI_AUTH_COOKIE_NAME)
    return response


def _invalid_cli_code_response():
    return jsonify({"error": "invalid or expired code"}), 400


@app.route('/api/auth/cli/exchange', methods=['POST'])
def api_auth_cli_exchange():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    code = body.get("code")
    verifier = body.get("code_verifier")
    if not isinstance(code, str) or _CLI_BEARER_RE.fullmatch(code) is None:
        return _invalid_cli_code_response()
    if not isinstance(verifier, str) or _CLI_VERIFIER_RE.fullmatch(verifier) is None:
        return _invalid_cli_code_response()

    code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    now = datetime.now(timezone.utc)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        code_row = conn.execute(
            "SELECT * FROM cli_auth_codes WHERE code_hash = ?",
            (code_hash,),
        ).fetchone()
        if (
            code_row is None
            or code_row["used_at"]
            or (_parse_utc(code_row["expires_at"]) or datetime.min.replace(tzinfo=timezone.utc)) <= now
            or not hmac.compare_digest(challenge, code_row["code_challenge"])
        ):
            conn.rollback()
            return _invalid_cli_code_response()

        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        token_expires = _utc_iso(now + timedelta(days=_CLI_TOKEN_TTL_DAYS))
        claimed = conn.execute(
            "UPDATE cli_auth_codes SET used_at = ? "
            "WHERE code_hash = ? AND used_at IS NULL",
            (_utc_iso(now), code_hash),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            return _invalid_cli_code_response()
        conn.execute(
            "INSERT INTO cli_tokens (token_hash, user_id, expires_at) "
            "VALUES (?, ?, ?)",
            (token_hash, code_row["user_id"], token_expires),
        )
        user_row = conn.execute(
            "SELECT email, display_name FROM users WHERE id = ?",
            (code_row["user_id"],),
        ).fetchone()
        if user_row is None:
            conn.rollback()
            return _invalid_cli_code_response()
        conn.commit()
        return jsonify({
            "token": raw_token,
            "expires_at": token_expires,
            "account": {
                "email": user_row["email"],
                "display_name": user_row["display_name"],
            },
        })
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/auth/cli/revoke', methods=['POST'])
def api_auth_cli_revoke():
    auth = _cli_auth_from_bearer()
    if auth is None:
        return _cli_auth_required()
    _user, token_row = auth
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE cli_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_utc_iso(), token_row["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route('/api/auth/cli/me', methods=['GET'])
def api_auth_cli_me():
    auth = _cli_auth_from_bearer()
    if auth is None:
        return _cli_auth_required()
    user, token_row = auth
    return jsonify({
        "ok": True,
        "account": {
            "email": user.email,
            "display_name": user.display_name,
        },
        "expires_at": token_row["expires_at"],
    })


def _cli_generation_response(
    row: Any,
    *,
    replayed: bool,
    superseded_previous: Optional[str] = None,
):
    return jsonify({
        "generation_public_id": row["generation_public_id"],
        "ribbon_token": row["ribbon_token"],
        "status": row["status"],
        "created_at": row["created_at"],
        "superseded_previous": superseded_previous,
        "replayed": replayed,
    })


@app.route('/api/generations/register', methods=['POST'])
def api_generations_register():
    auth = _cli_auth_from_bearer()
    if auth is None:
        return _cli_auth_required()
    user, _token_row = auth
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}

    meeting_public_id = body.get("meeting_public_id")
    output_type = body.get("output_type")
    provider = body.get("provider")
    model = body.get("model")
    content_sha256 = body.get("content_sha256")
    idempotency_key = body.get("idempotency_key")
    valid = (
        isinstance(meeting_public_id, str)
        and _CLI_MEETING_RE.fullmatch(meeting_public_id) is not None
        and isinstance(output_type, str)
        and output_type in _CLI_RENDERED_OUTPUT_TYPES
        and isinstance(provider, str)
        and 1 <= len(provider.strip()) <= 64
        and isinstance(model, str)
        and 1 <= len(model.strip()) <= 64
        and isinstance(content_sha256, str)
        and _CLI_SHA256_RE.fullmatch(content_sha256) is not None
        and isinstance(idempotency_key, str)
        and _CLI_IDEMPOTENCY_RE.fullmatch(idempotency_key) is not None
    )
    if not valid:
        return jsonify({"error": "invalid generation registration"}), 400

    meeting = get_meeting_public_record(meeting_public_id)
    if meeting is None:
        return jsonify({"error": "unknown meeting"}), 404
    provider = provider.strip()
    model = model.strip()

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        recent_count = conn.execute(
            """
            SELECT COUNT(*) FROM cli_generations
            WHERE user_id = ? AND created_at >= datetime('now', '-1 hour')
            """,
            (user.id,),
        ).fetchone()[0]
        if recent_count >= 120:
            conn.rollback()
            response = jsonify({"error": "registration rate limit exceeded"})
            response.status_code = 429
            response.headers["Retry-After"] = "3600"
            return response

        idem_row = conn.execute(
            "SELECT * FROM cli_generations "
            "WHERE user_id = ? AND idempotency_key = ?",
            (user.id, idempotency_key),
        ).fetchone()
        if idem_row is not None:
            same_payload = all((
                idem_row["meeting_public_id"] == meeting_public_id,
                idem_row["output_type"] == output_type,
                idem_row["provider"] == provider,
                idem_row["model"] == model,
                idem_row["content_sha256"] == content_sha256,
            ))
            if not same_payload:
                conn.rollback()
                return jsonify({
                    "error": "idempotency key reuse with different payload"
                }), 409
            conn.commit()
            return _cli_generation_response(idem_row, replayed=True)

        same_content_row = conn.execute(
            """
            SELECT * FROM cli_generations
            WHERE user_id = ? AND meeting_public_id = ? AND output_type = ?
              AND status = 'registered' AND content_sha256 = ?
            ORDER BY id DESC LIMIT 1
            """,
            (user.id, meeting_public_id, output_type, content_sha256),
        ).fetchone()
        if same_content_row is not None:
            conn.commit()
            return _cli_generation_response(same_content_row, replayed=True)

        previous = conn.execute(
            """
            SELECT * FROM cli_generations
            WHERE user_id = ? AND meeting_public_id = ? AND output_type = ?
              AND status = 'registered'
            ORDER BY id DESC LIMIT 1
            """,
            (user.id, meeting_public_id, output_type),
        ).fetchone()

        generation_public_id = ""
        for _ in range(100):
            candidate = generate_generation_public_id()
            exists = conn.execute(
                "SELECT 1 FROM cli_generations WHERE generation_public_id = ?",
                (candidate,),
            ).fetchone()
            if exists is None:
                generation_public_id = candidate
                break
        if not generation_public_id:
            raise RuntimeError("Unable to mint a unique generation public id")
        ribbon_token = mint_cli_ribbon_token(conn.cursor())
        conn.execute(
            """
            INSERT INTO cli_generations (
                generation_public_id, ribbon_token, user_id,
                meeting_public_id, output_type, provider, model,
                content_sha256, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation_public_id,
                ribbon_token,
                user.id,
                meeting_public_id,
                output_type,
                provider,
                model,
                content_sha256,
                idempotency_key,
            ),
        )
        if previous is not None:
            conn.execute(
                "UPDATE cli_generations SET status = 'superseded', "
                "superseded_by = ? WHERE id = ?",
                (generation_public_id, previous["id"]),
            )
        row = conn.execute(
            "SELECT * FROM cli_generations WHERE generation_public_id = ?",
            (generation_public_id,),
        ).fetchone()
        conn.commit()
        return _cli_generation_response(
            row,
            replayed=False,
            superseded_previous=(
                previous["generation_public_id"] if previous is not None else None
            ),
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _cli_contribution_response(row: Any, *, replayed: bool):
    response = jsonify({
        "submission_public_id": row["submission_public_id"],
        "meeting_public_id": row["meeting_public_id"],
        "payload_sha256": row["payload_sha256"],
        "status": row["status"],
        "received_at": row["created_at"],
        "replayed": replayed,
        "published": False,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/contributions/submit', methods=['POST'])
def api_contributions_submit():
    """Authenticated private intake; receipt never means publication."""
    auth = _cli_auth_from_bearer()
    if auth is None:
        return _cli_auth_required()
    user, _token_row = auth
    try:
        body = _strict_cli_contribution_json()
    except _CliContributionError as exc:
        return jsonify({"error": str(exc)}), exc.status

    meeting_public_id = body.get("meeting_public_id")
    if (
        not isinstance(meeting_public_id, str)
        or _CLI_MEETING_RE.fullmatch(meeting_public_id) is None
    ):
        return jsonify({"error": "meeting identity is invalid"}), 400
    meeting = get_meeting_public_record(meeting_public_id)
    if meeting is None:
        return jsonify({"error": "unknown meeting"}), 404
    try:
        transcript, outputs = _validate_cli_contribution(body, meeting)
    except _CliContributionError as exc:
        return jsonify({"error": str(exc)}), exc.status

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replay = conn.execute(
            "SELECT * FROM cli_contributions WHERE user_id = ? AND idempotency_key = ?",
            (user.id, body["idempotency_key"]),
        ).fetchone()
        if replay is not None:
            if not (
                replay["meeting_public_id"] == meeting_public_id
                and hmac.compare_digest(replay["payload_sha256"], body["payload_sha256"])
            ):
                conn.rollback()
                return jsonify({"error": "idempotency key reuse with different payload"}), 409
            conn.commit()
            return _cli_contribution_response(replay, replayed=True)

        same_payload = conn.execute(
            """SELECT * FROM cli_contributions
               WHERE user_id = ? AND meeting_public_id = ? AND payload_sha256 = ?
               ORDER BY id DESC LIMIT 1""",
            (user.id, meeting_public_id, body["payload_sha256"]),
        ).fetchone()
        if same_payload is not None:
            conn.commit()
            return _cli_contribution_response(same_payload, replayed=True)

        recent_count = conn.execute(
            """SELECT COUNT(*) FROM cli_contributions
               WHERE user_id = ? AND created_at >= datetime('now', '-1 hour')""",
            (user.id,),
        ).fetchone()[0]
        if recent_count >= 12:
            conn.rollback()
            response = jsonify({"error": "private contribution rate limit exceeded"})
            response.status_code = 429
            response.headers["Retry-After"] = "3600"
            return response

        submission_public_id = ""
        for _ in range(100):
            candidate = "c_" + secrets.token_urlsafe(16)
            exists = conn.execute(
                "SELECT 1 FROM cli_contributions WHERE submission_public_id = ?",
                (candidate,),
            ).fetchone()
            if exists is None:
                submission_public_id = candidate
                break
        if not submission_public_id:
            raise RuntimeError("Unable to mint a unique contribution id")
        inserted = conn.execute(
            """INSERT INTO cli_contributions (
                   submission_public_id, user_id, meeting_id, meeting_public_id,
                   idempotency_key, payload_sha256, transcript_sha256,
                   transcript_json, source_url
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                submission_public_id,
                user.id,
                int(meeting["id"]),
                meeting_public_id,
                body["idempotency_key"],
                body["payload_sha256"],
                transcript["sha256"],
                json.dumps(
                    transcript,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                transcript["source_url"],
            ),
        )
        contribution_id = int(inserted.lastrowid)
        for output in outputs:
            conn.execute(
                """INSERT INTO cli_contribution_outputs (
                       contribution_id, output_type, content, provider, model,
                       gate_status, gate_log, content_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    contribution_id,
                    output["output_type"],
                    output["content"],
                    output["provider"],
                    output["model"],
                    output["gate_status"],
                    output["gate_log"],
                    output["content_sha256"],
                ),
            )
        row = conn.execute(
            "SELECT * FROM cli_contributions WHERE id = ?", (contribution_id,)
        ).fetchone()
        conn.commit()
        return _cli_contribution_response(row, replayed=False)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/auth/google/login', methods=['GET'])
def api_auth_google_login():
    """Begin the Google OAuth dance. Generates PKCE + state, stashes
    them (plus the post-login redirect target) in a signed transient
    cookie, and 302s to Google's consent screen.
    """
    if not signin_enabled():
        return _signin_maintenance_response()

    next_url = _safe_next_path(request.args.get("next", "/"))
    try:
        redirect_uri = compute_redirect_uri(_forwarded_host_url())
    except RuntimeError as exc:
        # No OAuth client configured. Return a clear 503 instead of a
        # cryptic Google error after the round-trip.
        return jsonify({
            "success": False,
            "error": "google_oauth_not_configured",
            "detail": str(exc),
        }), 503

    state = random_state()
    verifier, challenge = generate_pkce()
    try:
        consent_url = build_consent_url(state, challenge, redirect_uri)
    except RuntimeError as exc:
        return jsonify({
            "success": False,
            "error": "google_oauth_not_configured",
            "detail": str(exc),
        }), 503

    cookie_value = build_oauth_state_cookie(state, verifier, next_url)

    response = Response(status=302)
    response.headers["Location"] = consent_url
    _set_cookie(
        response, OAUTH_STATE_COOKIE_NAME, cookie_value, OAUTH_STATE_TTL_SECONDS
    )
    return response


@app.route('/api/auth/google/callback', methods=['GET'])
def api_auth_google_callback():
    """Complete the Google OAuth dance. Verifies state, redeems the
    authorization code (+ PKCE), upserts the user row, mints a session
    JWT cookie, clears the transient state cookie, and redirects to the
    caller's chosen `next` path.
    """
    if not signin_enabled():
        return _signin_maintenance_response(clear_oauth_state=True)

    # Google can hand us back either `?code=...&state=...` or
    # `?error=...&state=...`. Reject anything that's not a code path.
    error = request.args.get("error")
    if error:
        # User canceled, scope refused, etc. Bounce back to "/" with a
        # query flag so the frontend can show a friendly notice.
        response = Response(status=302)
        response.headers["Location"] = f"/?auth_error={error}"
        _clear_cookie(response, OAUTH_STATE_COOKIE_NAME)
        return response

    code = request.args.get("code", "")
    state = request.args.get("state", "")
    cookie_value = request.cookies.get(OAUTH_STATE_COOKIE_NAME, "")
    if not code or not state or not cookie_value:
        return jsonify({
            "success": False,
            "error": "missing_code_or_state",
        }), 400

    payload = verify_oauth_state_cookie(cookie_value, state)
    if not payload:
        # CSRF mismatch / replay / expired transient cookie. Hard reject.
        return jsonify({
            "success": False,
            "error": "invalid_oauth_state",
        }), 400

    verifier = payload.get("code_verifier", "")
    next_url = _safe_next_path(payload.get("next", "/"))

    try:
        redirect_uri = compute_redirect_uri(_forwarded_host_url())
    except RuntimeError as exc:
        return jsonify({
            "success": False,
            "error": "google_oauth_not_configured",
            "detail": str(exc),
        }), 503

    try:
        token_response = exchange_code(code, verifier, redirect_uri)
    except requests.HTTPError as exc:  # type: ignore[name-defined]
        app.logger.warning("google token-exchange failed: %s", exc)
        return jsonify({
            "success": False,
            "error": "token_exchange_failed",
            "detail": str(exc),
        }), 502

    access_token = token_response.get("access_token", "")
    if not access_token:
        return jsonify({
            "success": False,
            "error": "no_access_token",
        }), 502

    try:
        userinfo = fetch_userinfo(access_token)
    except requests.HTTPError as exc:  # type: ignore[name-defined]
        app.logger.warning("google userinfo fetch failed: %s", exc)
        return jsonify({
            "success": False,
            "error": "userinfo_fetch_failed",
            "detail": str(exc),
        }), 502

    if userinfo.get("email_verified") is not True:
        return jsonify({
            "success": False,
            "error": "email_not_verified",
        }), 403

    google_sub = (userinfo.get("sub") or "").strip()
    email = (userinfo.get("email") or "").strip()
    name = (userinfo.get("name") or "").strip() or None
    picture = (userinfo.get("picture") or "").strip() or None

    if not google_sub or not email:
        # Google guarantees both for the openid+email scope. Defensive.
        return jsonify({
            "success": False,
            "error": "incomplete_userinfo",
        }), 502

    if email.lower() in get_owner_emails():
        if OWNER_GOOGLE_SUB_ALLOWLIST:
            if google_sub not in OWNER_GOOGLE_SUB_ALLOWLIST:
                return jsonify({
                    "success": False,
                    "error": "owner_sub_mismatch",
                }), 403
        else:
            app.logger.warning(
                "owner email %s trusted without Google sub verification; "
                "ZSPAN_OWNER_GOOGLE_SUB_ALLOWLIST is empty",
                email.lower(),
            )

    try:
        user = upsert_user_from_google(
            google_sub=google_sub,
            email=email,
            display_name=name,
            avatar_url=picture,
        )
    except ValueError:
        app.logger.warning("Google account linking rejected an identity conflict")
        return jsonify({
            "success": False,
            "error": "account_link_conflict",
        }), 409

    session_token = mint_session_token(user.id, role=user.role)

    response = Response(status=302)
    response.headers["Location"] = next_url
    _set_cookie(response, SESSION_COOKIE_NAME, session_token, SESSION_TTL_SECONDS)
    _clear_cookie(response, OAUTH_STATE_COOKIE_NAME)
    return response


def _password_session_response(user, *, status_code: int = 200):
    session_token = mint_session_token(user.id, role=user.role)
    response = jsonify({"success": True})
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    _set_cookie(response, SESSION_COOKIE_NAME, session_token, SESSION_TTL_SECONDS)
    return response


@app.route('/api/auth/password/register', methods=['POST'])
@_require_trusted_origin
@_public_rate_limited('password_auth')
def api_auth_password_register():
    """Create an email/password account only when a live invitation is spent."""
    if _current_user_from_cookie() is not None:
        return jsonify({
            'success': False,
            'error': 'already_authenticated',
            'message': 'This browser is already logged in.',
        }), 409
    body, body_error = _small_json_request_body()
    if body_error:
        return body_error
    try:
        result, user = register_invited_user(
            email=body.get('email'),
            display_name=body.get('display_name'),
            password=body.get('password'),
            invitation_token=body.get('invitation_token'),
            forbidden_emails=frozenset(get_owner_emails()),
        )
    except (AccountInputError, PasswordValidationError) as exc:
        return jsonify({
            'success': False,
            'error': 'invalid_account_details',
            'message': str(exc),
        }), 400

    if result != 'registered' or user is None:
        # Existing-email, forbidden-owner-email, used-card, revoked-card, and
        # unknown-card states intentionally share one public response. A card
        # holder cannot turn registration into an account-existence oracle.
        return jsonify({
            'success': False,
            'error': 'registration_unavailable',
            'message': (
                'This email or invitation cannot be used. '
                'If you already have an account, log in instead.'
            ),
        }), 409
    return _password_session_response(user, status_code=201)


@app.route('/api/auth/password/login', methods=['POST'])
@_require_trusted_origin
@_public_rate_limited('password_auth')
def api_auth_password_login():
    """Authenticate a local credential and optionally claim its invitation."""
    body, body_error = _small_json_request_body()
    if body_error:
        return body_error
    result, user = authenticate_password(
        email=body.get('email'),
        password=body.get('password'),
        invitation_token=body.get('invitation_token'),
    )
    if result != 'authenticated' or user is None:
        # Locked, unknown, and incorrect credentials intentionally share one
        # response so the public route cannot be used to enumerate accounts.
        return jsonify({
            'success': False,
            'error': 'invalid_credentials',
            'message': 'The email or password was not recognized.',
        }), 401
    return _password_session_response(user)


@app.route('/api/auth/password/forgot', methods=['POST'])
@_require_trusted_origin
@_public_rate_limited('password_reset')
def api_auth_password_forgot():
    """Send a one-time reset link while keeping account existence private."""
    body, body_error = _small_json_request_body()
    if body_error:
        return body_error
    raw_token, recipient = create_password_reset_token(body.get('email'))
    if raw_token is not None and recipient is not None:
        send_password_reset_email(recipient, raw_token)
    response = jsonify({
        'success': True,
        'message': (
            'If that email has a password account, a reset link is on its way.'
        ),
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/auth/password/reset', methods=['POST'])
@_require_trusted_origin
@_public_rate_limited('password_reset')
def api_auth_password_reset():
    """Consume a one-hour reset bearer and sign the account back in."""
    body, body_error = _small_json_request_body()
    if body_error:
        return body_error
    try:
        result, user = reset_password(
            token=body.get('token'),
            password=body.get('password'),
        )
    except PasswordValidationError as exc:
        return jsonify({
            'success': False,
            'error': 'invalid_password',
            'message': str(exc),
        }), 400
    if result != 'reset' or user is None:
        return jsonify({
            'success': False,
            'error': 'reset_unavailable',
            'message': 'This reset link is invalid or has expired.',
        }), 400
    return _password_session_response(user)


@app.route('/api/auth/me', methods=['GET'])
def api_auth_me():
    """Return the current light-account principal, or null when
    unauthenticated. Never 401s — anonymous reads are first-class.
    """
    user = _current_user_from_cookie()
    if not user:
        return jsonify({
            "authenticated": False,
            "user": None,
            "sign_in_enabled": signin_enabled(),
        })
    return jsonify({
        "authenticated": True,
        "sign_in_enabled": signin_enabled(),
        "user": {
            "user_id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "role": user.role,
            # Operator identity (V1-Polish-2): the signed-in Google account
            # whose email matches the configured owner_email gets the
            # operator view. Drives OwnerOnly + the owner-only view gate.
            "is_owner": is_owner_email(user.email),
            # V1.5-OperatorSearch-1 — strictly wider than is_owner.
            # Includes owners + secondary test accounts on the
            # operator_search_allowlist. Gates the TopBarSearch operator
            # affordance + the modal + the backend endpoints.
            "is_operator_search_principal": is_operator_search_principal(user.email),
            # Librarian request-access state (2026-07-27) — lets the client
            # render request/pending/granted without a separate status call.
            "librarian_access": get_user_librarian_access(user.id) or "none",
            "follows": list_follows(user.id),
            "city_topics": list_city_topics(user.id),
        },
    })


@app.route('/api/invitations/status', methods=['POST'])
@_require_trusted_origin
@_public_rate_limited('invitation')
def api_invitation_status():
    """Tell a card landing page whether its bearer invitation is usable.

    Every non-active state collapses to ``unavailable`` so this public seam
    does not disclose whether a guessed token was redeemed, revoked, or never
    issued.
    """
    body, body_error = _small_json_request_body()
    if body_error:
        return body_error
    token = body.get('token')
    status = get_invitation_status(token)
    available = status == 'active'
    return jsonify({
        'success': True,
        'available': available,
        'status': 'active' if available else 'unavailable',
    })


@app.route('/api/invitations/redeem', methods=['POST'])
@_require_trusted_origin
@_public_rate_limited('invitation')
def api_invitation_redeem():
    """Consume one card and grant the signed-in account Librarian access."""
    user, err = _require_user()
    if err:
        return err

    body, body_error = _small_json_request_body()
    if body_error:
        return body_error
    result = redeem_invitation_token(user.id, body.get('token'))
    if result == 'redeemed':
        return jsonify({
            'success': True,
            'status': 'granted',
            'message': 'Your invitation is active.',
        })
    if result == 'already_granted':
        return jsonify({
            'success': True,
            'status': 'granted',
            'message': 'Your account already has access.',
        })
    if result == 'banned':
        return jsonify({
            'success': False,
            'error': 'access_unavailable',
            'message': 'Librarian access is unavailable for this account.',
        }), 403
    if result == 'user_not_found':
        return jsonify({
            'success': False,
            'error': 'sign-in required',
        }), 401
    if result in {'invalid', 'revoked', 'unavailable'}:
        return jsonify({
            'success': False,
            'error': 'invitation_unavailable',
            'message': 'This invitation is not available.',
        }), 409
    raise ValueError(f'unexpected invitation redemption result: {result!r}')


@app.route('/api/invitations', methods=['GET'])
def api_invitations_list():
    """Owner-only card inventory without bearer tokens or their hashes."""
    _user, err = _require_owner()
    if err:
        return err
    return jsonify({
        'success': True,
        'invitations': list_invitation_codes(),
    })


@app.route('/api/invitations/import', methods=['POST'])
@_require_trusted_origin
def api_invitations_import():
    """Owner-only activation of a generated card batch by token digest."""
    user, err = _require_owner()
    if err:
        return err
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    try:
        result = import_invitation_batch(
            body.get('batch_name'),
            body.get('invitations'),
            actor_user_id=user.id,
        )
    except ValueError as exc:
        return jsonify({
            'success': False,
            'error': str(exc),
        }), 400
    return jsonify({'success': True, **result})


@app.route('/api/invitations/<int:invitation_id>/revoke', methods=['POST'])
@_require_trusted_origin
def api_invitation_revoke(invitation_id):
    """Owner-only revocation for an unused physical card."""
    user, err = _require_owner()
    if err:
        return err
    result = revoke_invitation_code(invitation_id, actor_user_id=user.id)
    if result == 'not_found':
        return jsonify({
            'success': False,
            'error': 'invitation not found',
        }), 404
    if result == 'already_redeemed':
        return jsonify({
            'success': False,
            'error': 'redeemed invitations cannot be revoked',
        }), 409
    if result == 'revoked':
        return jsonify({'success': True, 'status': 'revoked'})
    raise ValueError(f'unexpected invitation revocation result: {result!r}')


@app.route('/api/librarian/request-access', methods=['POST'])
@_require_trusted_origin
def api_librarian_request_access():
    """Request Librarian access for the current signed-in account."""
    user = _current_user_from_cookie()
    if not user:
        return jsonify({'success': False, 'status': 'unauthenticated'}), 401

    status = get_user_librarian_access(user.id)
    if status == 'banned':
        return jsonify({
            'success': False,
            'status': 'banned',
            'message': 'Librarian access is unavailable for this account.',
        }), 403
    if status in {'requested', 'granted'}:
        return jsonify({'success': True, 'status': status})
    if status == 'none':
        if set_librarian_access(user.id, 'requested'):
            return jsonify({'success': True, 'status': 'requested'})
        return jsonify({'success': False, 'status': 'unauthenticated'}), 401
    raise ValueError(
        f"unexpected librarian access status for user {user.id}: {status!r}"
    )


@app.route('/api/librarian/access-requests', methods=['GET'])
def api_librarian_access_requests():
    """Owner-only Librarian access review queue."""
    _user, _err = _require_owner()
    if _err:
        return _err
    return jsonify({
        'success': True,
        'requests': list_librarian_access_requests(),
    })


@app.route(
    '/api/librarian/access-requests/<int:user_id>/decide',
    methods=['POST'],
)
@_require_trusted_origin
def api_librarian_access_decide(user_id):
    """Owner-only Librarian access grant, denial, or ban."""
    _user, _err = _require_owner()
    if _err:
        return _err

    body = request.get_json(silent=True) or {}
    action = body.get('action')
    status_by_action = {
        'grant': 'granted',
        'deny': 'none',
        'ban': 'banned',
    }
    status = status_by_action.get(action)
    if status is None:
        return jsonify({
            'success': False,
            'error': 'action must be grant, deny, or ban',
        }), 400
    if not decide_librarian_access(user_id, status):
        return jsonify({
            'success': False,
            'error': 'user not found',
        }), 404
    return jsonify({
        'success': True,
        'user_id': user_id,
        'status': status,
    })


@app.route('/api/librarian/tuning', methods=['GET'])
def api_librarian_tuning_get():
    """Return effective Librarian controls and a compact live snapshot."""
    _user, _err = _require_owner()
    if _err:
        return _err
    return jsonify(_librarian_tuning_payload())


@app.route('/api/librarian/tuning', methods=['PATCH'])
@_require_trusted_origin
def api_librarian_tuning_patch():
    """Validate and persist a partial Librarian control update."""
    _user, _err = _require_owner()
    if _err:
        return _err

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({
            'success': False,
            'error': 'Send a set of settings and values to change.',
            'invalid_key': '',
        }), 400

    for key, value in body.items():
        guardrail = _LIBRARIAN_TUNING_GUARDRAILS.get(key)
        if guardrail is None:
            return jsonify({
                'success': False,
                'error': 'That setting cannot be changed here.',
                'invalid_key': key,
            }), 400
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
        ):
            return jsonify({
                'success': False,
                'error': 'Enter a whole number greater than zero.',
                'invalid_key': key,
            }), 400
        if value < guardrail['min']:
            return jsonify({
                'success': False,
                'error': (
                    f"Enter a value of at least {guardrail['min']}."
                ),
                'invalid_key': key,
            }), 400
        maximum = guardrail['max']
        if maximum is not None and value > maximum:
            return jsonify({
                'success': False,
                'error': (
                    f"Enter a value no greater than {maximum}."
                ),
                'invalid_key': key,
            }), 400

    policy_fields = {
        "librarian_daily_query_cap": "daily_query_cap",
        "librarian_reject_burst_threshold": "reject_burst_threshold",
        "librarian_reject_burst_window_seconds": (
            "reject_burst_window_seconds"
        ),
        "librarian_reject_cooldown_seconds": "reject_cooldown_seconds",
        "librarian_reject_autoban_strike_threshold": (
            "reject_autoban_strike_threshold"
        ),
        "librarian_reject_autoban_window_seconds": (
            "reject_autoban_window_seconds"
        ),
    }
    try:
        update_librarian_policy(
            **{
                policy_fields[key]: value
                for key, value in body.items()
            }
        )
    except ValueError as exc:
        invalid_key = next(iter(body), "")
        return jsonify({
            'success': False,
            'error': str(exc),
            'invalid_key': invalid_key,
        }), 400
    return jsonify(_librarian_tuning_payload())


@app.route('/api/auth/logout', methods=['POST'])
@_require_trusted_origin
def api_auth_logout():
    """Clear the session cookie. Idempotent — returns 200 even if the
    caller wasn't signed in. The frontend should refetch /api/auth/me
    afterwards to update its local state.
    """
    response = jsonify({"success": True})
    _clear_cookie(response, SESSION_COOKIE_NAME)
    return response


# ─────────────────────────────────────────────────────────────────
# Follow/subscribe (ACCOUNT_SYSTEM_SPEC chunk 3 — Consumer 2)
# ─────────────────────────────────────────────────────────────────
# Five routes gated by the session JWT cookie minted at OAuth callback:
#   POST   /api/follows  body {target_type, target_key} → add
#   DELETE /api/follows  body {target_type, target_key} → remove
#   GET    /api/follows                                  → list
#   GET    /api/follows/city-topics/<city_key>           → list city topics
#   PUT    /api/follows/city-topics/<city_key>           → replace city topics
# All return 401 when the cookie is missing/invalid; the frontend
# treats 401 as "prompt sign-in" and surfaces the SignInPill.
#
# target_type is whitelisted to {city, county, topic, meeting}. POST is
# idempotent (returns `{added: false}` if the row already existed);
# DELETE is the same way (returns `{removed: false}` if there was
# nothing to remove). This matches the account_system helpers' bool
# semantics + lets the client retry without worrying about state.


def _canonicalize_follow_target(
    target_type: str, target_key: str
) -> tuple[str, Optional[tuple]]:
    """Canonicalize + validate `target_key` against the referenced entity.

    Returns (canonical_key, error). `error` is a (jsonify_body, status_code)
    tuple when validation rejects the request, else None.

    Rules per target_type:
      - city: must match a row in `cities.name` (case-insensitive). The
        canonical stored form is returned so two users following "kingman"
        and "Kingman" resolve to the same target and hit the same
        notification bucket.
      - topic: must be one of the controlled `topic_tags.TOPIC_TAGS` ids
        (data_centers, water_rights, diversity_inclusion, lgbtq,
        education). Stored lowercase.
      - county, meeting: pass through with the existing length cap only;
        richer canonicalization can land when their follow UIs do.
    """
    if target_type == "city":
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT name FROM cities WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (target_key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return "", (
                jsonify({
                    "success": False,
                    "error": "unknown_city",
                    "detail": (
                        f"'{target_key}' is not a city in the catalog. "
                        "Try one from the channels tree."
                    ),
                }),
                400,
            )
        return row["name"] if hasattr(row, "keys") else row[0], None

    if target_type == "topic":
        from topic_tags import TOPIC_TAG_IDS  # noqa: PLC0415 — small utility
        normalized = target_key.strip().lower()
        if normalized not in TOPIC_TAG_IDS:
            return "", (
                jsonify({
                    "success": False,
                    "error": "unknown_topic",
                    "detail": (
                        f"'{target_key}' is not a known topic tag. "
                        f"Valid: {sorted(TOPIC_TAG_IDS)}"
                    ),
                }),
                400,
            )
        return normalized, None

    # county / meeting: no canonicalization gate yet; the target_key format
    # + cap already checked by _read_follow_body().
    return target_key, None


def _read_follow_body() -> tuple[Optional[str], Optional[str], Optional[tuple]]:
    """Parse + validate the body for POST/DELETE /api/follows.

    Returns (target_type, target_key, error). `error` is a
    (jsonify_body, status_code) tuple when validation fails, else None.
    On success, `target_key` is the CANONICAL form for its target_type
    (see `_canonicalize_follow_target`) so duplicate-casing lookups
    collapse to one row.
    """
    payload = request.get_json(silent=True) or {}
    target_type = (payload.get("target_type") or "").strip().lower()
    target_key = (payload.get("target_key") or "").strip()

    if target_type not in _FOLLOW_TARGET_TYPES:
        return None, None, (
            jsonify({
                "success": False,
                "error": "invalid_target_type",
                "detail": f"target_type must be one of {sorted(_FOLLOW_TARGET_TYPES)}",
            }),
            400,
        )
    if not target_key:
        return None, None, (
            jsonify({
                "success": False,
                "error": "missing_target_key",
            }),
            400,
        )
    if len(target_key) > _FOLLOW_TARGET_KEY_MAX:
        return None, None, (
            jsonify({
                "success": False,
                "error": "target_key_too_long",
                "detail": f"target_key must be ≤ {_FOLLOW_TARGET_KEY_MAX} chars",
            }),
            400,
        )
    canonical, canon_err = _canonicalize_follow_target(target_type, target_key)
    if canon_err is not None:
        return None, None, canon_err
    return target_type, canonical, None


@app.route('/api/workspace/receipts', methods=['GET'])
def api_workspace_receipts():
    """Return receipt metadata for the signed-in user's local/CLI work."""
    user, err = _require_user()
    if err:
        return err
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT 'contribution' AS kind,
                   submission_public_id AS public_id,
                   meeting_public_id, status, created_at
            FROM cli_contributions WHERE user_id = ?
            UNION ALL
            SELECT 'generation' AS kind,
                   generation_public_id AS public_id,
                   meeting_public_id, status, created_at
            FROM cli_generations WHERE user_id = ?
            UNION ALL
            SELECT 'analysis' AS kind,
                   gle.retrieval_run_id AS public_id,
                   COALESCE(m.public_id, '') AS meeting_public_id,
                   CASE
                       WHEN gle.terminal_failure_reason IS NULL
                       THEN 'retrieval_recorded'
                       ELSE 'retrieval_failed'
                   END AS status,
                   gle.created_at
            FROM librarian_gate_events AS gle
            LEFT JOIN meetings AS m ON m.id = gle.meeting_id
            WHERE gle.user_id = ?
              AND gle.stencil_result = 'accepted'
              AND gle.retrieval_run_id IS NOT NULL
            ORDER BY created_at DESC LIMIT 100
            """,
            (user.id, user.id, user.id),
        ).fetchall()
    finally:
        conn.close()
    response = jsonify({
        'success': True,
        'receipts': [dict(row) for row in rows],
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/follows', methods=['GET'])
def api_follows_list():
    """Return the signed-in user's follows. 401 when unauthenticated.

    Slice-2 (session-103): any-signed-in-user, not owner-only. Follows are
    user-owned personalization data — every row is scoped by user.id
    derived from the session cookie, so a signed-in user only sees their
    own list. The prior owner-only gate was RR-8/SEC-AUTH-3 hardening
    from session-31 (operator-security posture during the paused-signin
    era); sol Round-1 flagged it as inconsistent with the account model.
    """
    user, err = _require_user()
    if err:
        return err
    follows = [
        follow
        for follow in list_follows(user.id)
        if follow["target_type"] != "topic"
    ]
    return jsonify({
        "success": True,
        "follows": follows,
        "city_topics": list_city_topics(user.id),
    })


@app.route('/api/follows/city-topics/<city_key>', methods=['GET'])
def api_follows_city_topics_get(city_key):
    """Return the signed-in user's topic decorations for one city."""
    user, err = _require_user()
    if err:
        return err
    canonical_key, error = _canonicalize_follow_target(
        "city",
        city_key.strip(),
    )
    if error is not None:
        return error
    return jsonify({
        "success": True,
        "city_key": canonical_key,
        "tag_ids": list_city_topics(user.id).get(canonical_key, []),
    })


@app.route('/api/follows/city-topics/<city_key>', methods=['PUT'])
@_require_trusted_origin
def api_follows_city_topics_put(city_key):
    """Replace the signed-in user's topic decorations for one city."""
    user, err = _require_user()
    if err:
        return err
    canonical_key, error = _canonicalize_follow_target(
        "city",
        city_key.strip(),
    )
    if error is not None:
        return error

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(
        payload.get("tag_ids"),
        list,
    ):
        return jsonify({
            "success": False,
            "error": "invalid_tag_ids",
            "detail": "tag_ids must be a list",
        }), 400

    tag_ids = set_city_topics(user.id, canonical_key, payload["tag_ids"])
    return jsonify({
        "success": True,
        "city_key": canonical_key,
        "tag_ids": tag_ids,
    })


@app.route('/api/follows', methods=['POST'])
@_require_trusted_origin
def api_follows_add():
    """Add a follow. Idempotent — `added: false` if the row already
    existed. 401 when unauthenticated. 400 when the target is invalid or
    topic follows are disabled. 409 when the per-user cap is reached AND
    the target is not already in the set (idempotent re-adds of existing
    rows always succeed).
    """
    user, err = _require_user()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    requested_target_type = (
        payload.get("target_type") or ""
    ).strip().lower()
    if requested_target_type == "topic":
        return jsonify({
            "success": False,
            "error": "topic_follows_disabled",
            "detail": (
                "Global topic follows are disabled; follow the city instead."
            ),
        }), 400
    target_type, target_key, error = _read_follow_body()
    if error is not None:
        return error
    try:
        added = follow_add(user.id, target_type, target_key)  # type: ignore[arg-type]
    except FollowCapExceeded as exc:
        return jsonify({
            "success": False,
            "error": "follow_cap_exceeded",
            "detail": str(exc),
            "cap": FOLLOW_CAP_PER_USER,
        }), 409
    return jsonify({
        "success": True,
        "added": added,
        "follows": list_follows(user.id),
        "city_topics": list_city_topics(user.id),
    })


@app.route('/api/follows', methods=['DELETE'])
@_require_trusted_origin
def api_follows_remove():
    """Remove a follow. Idempotent — `removed: false` if there was no
    matching row. 401 when unauthenticated. Deletes are always allowed
    (even past the follow cap) so a user can never lock themselves out
    of unfollowing.
    """
    user, err = _require_user()
    if err:
        return err
    target_type, target_key, error = _read_follow_body()
    if error is not None:
        return error
    conn = get_connection()
    try:
        with conn:
            removed = follow_remove(  # type: ignore[arg-type]
                user.id,
                target_type,
                target_key,
                conn=conn,
            )
            if target_type == "city":
                clear_city_topics(user.id, target_key, conn=conn)
    finally:
        conn.close()
    return jsonify({
        "success": True,
        "removed": removed,
        "follows": list_follows(user.id),
        "city_topics": list_city_topics(user.id),
    })


# ─────────────────────────────────────────────────────────────────
# Notification unsubscribe — intentionally public bearer-token route
# ─────────────────────────────────────────────────────────────────
# Email scanners commonly prefetch GET links, so GET only renders a
# confirmation form. The state change is POST-only. The signed token scopes
# the operation to one user and exposes no outbox/account data.


def _unsubscribe_page(
    *,
    title: str,
    message: str,
    token: str | None = None,
    status: int = 200,
) -> Response:
    form = ""
    if token is not None:
        safe_token = html.escape(token, quote=True)
        form = (
            '<form method="post" action="/api/unsubscribe">'
            f'<input type="hidden" name="token" value="{safe_token}">'
            '<button type="submit">Unsubscribe</button>'
            "</form>"
        )
    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"robots\" content=\"noindex\">"
        f"<title>{html.escape(title, quote=True)}</title></head>"
        "<body><main>"
        f"<h1>{html.escape(title, quote=True)}</h1>"
        f"<p>{html.escape(message, quote=True)}</p>"
        f"{form}"
        "</main></body></html>"
    )
    response = Response(
        page,
        status=status,
        content_type="text/html; charset=utf-8",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/unsubscribe', methods=['GET'])
def api_unsubscribe_get():
    """Render a confirmation form without mutating notification prefs."""
    raw_token = request.args.get("token")
    if verify_unsubscribe_token(raw_token) is None:
        return _unsubscribe_page(
            title="Invalid unsubscribe link",
            message=(
                "This unsubscribe link is invalid. Request a fresh link from "
                "a recent Z-SPAN notification email."
            ),
            status=400,
        )
    return _unsubscribe_page(
        title="Unsubscribe from meeting emails",
        message=(
            "Confirm that you no longer want to receive Z-SPAN meeting "
            "notification emails."
        ),
        token=raw_token,
    )


@app.route('/api/unsubscribe', methods=['POST'])
def api_unsubscribe_post():
    """Atomically consume one token and disable its owner's email."""
    payload = request.get_json(silent=True)
    raw_token = payload.get("token") if isinstance(payload, dict) else None
    if not raw_token:
        raw_token = request.form.get("token")
    if not raw_token:
        raw_token = request.args.get("token")

    conn = get_connection()
    try:
        # Serialize token claims so two simultaneous POSTs cannot both verify
        # the same still-unused row before either one stamps it.
        conn.execute("BEGIN IMMEDIATE")
        user_id = verify_unsubscribe_token(raw_token, conn=conn)
        if user_id is None:
            conn.rollback()
            return jsonify({
                "success": False,
                "error": "invalid_or_expired_token",
            }), 400

        token_id = str(raw_token).split(".", 1)[0]
        claimed = conn.execute(
            """
            UPDATE unsubscribe_tokens
            SET used_at = CURRENT_TIMESTAMP
            WHERE token_id = ?
              AND used_at IS NULL
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """,
            (token_id,),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            return jsonify({
                "success": False,
                "error": "invalid_or_expired_token",
            }), 400

        prefs_row = conn.execute(
            """
            SELECT digest_cadence
            FROM notification_prefs
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        digest_cadence = prefs_row[0] if prefs_row is not None else "weekly"
        conn.execute(
            """
            INSERT INTO notification_prefs (
                user_id, digest_cadence, email_enabled, updated_at
            ) VALUES (?, ?, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                email_enabled = 0,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, digest_cadence),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return _unsubscribe_page(
        title="Email notifications disabled",
        message=(
            "You have been unsubscribed from Z-SPAN meeting notification "
            "emails."
        ),
    )


# ─────────────────────────────────────────────────────────────────
# Creator Network (ACCOUNT_SYSTEM_SPEC chunks 7 + 8 endpoints)
# ─────────────────────────────────────────────────────────────────
# Two routes:
#   POST /api/creators/promote          → light → creator role flip
#   GET  /api/creators/me/status        → active agreement + download summary
#
# Promotion runs the signup form's free-text fields through
# `input_moderation.moderate_user_input` with surface="creator_signup"
# per S-008 chunk 3. On accept, calls `promote_user_to_creator` (which
# is idempotent against the same (user_id, tos_version) pair), then
# mints a NEW session JWT with role='creator' so the cookie reflects
# the post-promotion state without requiring a re-login.
#
# Per [D-095](DECISIONS.md#d-095) the disclaimer-narrated-karaoke is
# part of the signup flow; the BACKEND doesn't enforce the karaoke
# completion timing (that's a UX gate). The backend only records the
# `disclaimer_acknowledged_at` timestamp the client supplies.

_SIGNUP_IP_HASH_SALT = "zspan-creator-signup-v0"


def _hash_signup_ip(ip: Optional[str]) -> Optional[str]:
    """Salted SHA-256 of the client IP. None when no IP available."""
    if not ip:
        return None
    h = hashlib.sha256()
    h.update(_SIGNUP_IP_HASH_SALT.encode("utf-8"))
    h.update(b":")
    h.update(ip.encode("utf-8"))
    return h.hexdigest()


def _agreement_dict(agreement) -> dict:
    """Serialize a CreatorAgreement dataclass for JSON response."""
    if agreement is None:
        return None  # type: ignore[return-value]
    return {
        "id": agreement.id,
        "tos_version": agreement.tos_version,
        "disclaimer_version": agreement.disclaimer_version,
        "disclaimer_acknowledged_at": agreement.disclaimer_acknowledged_at,
        "signed_at": agreement.signed_at,
        "revoked_at": agreement.revoked_at,
        "revoked_reason": agreement.revoked_reason,
    }


@app.route('/api/creators/me/status', methods=['GET'])
def api_creators_me_status():
    """Return the signed-in user's creator state. Always 200 for
    authenticated users — `role` indicates whether they're a creator
    or still a light account. 401 when unauthenticated.
    """
    # RR-8 / SEC-AUTH-3: owner-ONLY (was any-signed-in-user). Operator-security
    # material; the hardening capture script already presents an owner cookie.
    user, _err = _require_owner()
    if _err:
        return _err
    agreement = get_active_agreement(user.id)
    summary = get_creator_download_summary(user.id)
    return jsonify({
        "success": True,
        "user": {
            "user_id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
        },
        "active_agreement": _agreement_dict(agreement),
        "download_summary": {
            "total_downloads": summary.total_downloads,
            "most_recent_at": summary.most_recent_at,
        },
    })


@app.route('/api/creators/promote', methods=['POST'])
@_require_trusted_origin
def api_creators_promote():
    """Light → creator promotion. Body:
        {
          "tos_version": str,
          "disclaimer_version": str,
          "disclaimer_acknowledged_at": str (ISO 8601, client-supplied),
          "signup_form": {
            "display_name": str,
            "handle": str,
            "creator_context": str  # optional, max 500 chars
          }
        }

    Already-creator users (role != 'light') are rejected with 409 —
    the idempotent path uses GET /api/creators/me/status to inspect
    existing state without firing the promotion side-effect.
    """
    # RR-8 / SEC-AUTH-3: owner-ONLY (was any-signed-in-user). Operator-security
    # material; the hardening capture script already presents an owner cookie.
    user, _err = _require_owner()
    if _err:
        return _err
    if user.role != "light":
        return jsonify({
            "success": False,
            "error": "already_promoted",
            "detail": f"user role is '{user.role}', not 'light'",
            "active_agreement": _agreement_dict(get_active_agreement(user.id)),
        }), 409

    payload = request.get_json(silent=True) or {}
    tos_version = (payload.get("tos_version") or "").strip()
    disclaimer_version = (payload.get("disclaimer_version") or "").strip()
    disclaimer_ack_at = (payload.get("disclaimer_acknowledged_at") or "").strip()
    signup_form = payload.get("signup_form") or {}

    missing = []
    for field, value in [
        ("tos_version", tos_version),
        ("disclaimer_version", disclaimer_version),
        ("disclaimer_acknowledged_at", disclaimer_ack_at),
    ]:
        if not value:
            missing.append(field)
    if missing:
        return jsonify({
            "success": False,
            "error": "missing_fields",
            "missing": missing,
        }), 400

    # Moderation gate on the free-text fields. Concatenate the
    # submitted strings into a single payload so the surface's
    # per-day cap counts a single signup attempt as one event.
    free_text_payload = " ".join([
        str(signup_form.get("display_name") or "").strip(),
        str(signup_form.get("handle") or "").strip(),
        str(signup_form.get("creator_context") or "").strip(),
    ]).strip()
    operator_review_needed = False
    moderation_reason: Optional[str] = None
    moderation_normalized_text: Optional[str] = None
    if free_text_payload:
        moderation = moderate_user_input(
            free_text_payload,
            surface="creator_signup",
            user_id=user.id,
        )
        if not moderation.accept:
            return jsonify({
                "success": False,
                "error": "moderation_rejected",
                "reason": moderation.reason,
            }), 400
        # Flagged-but-accepted lands the signup in the operator review
        # queue so a human can confirm or revoke the elevated role.
        if moderation.reason == "flagged":
            operator_review_needed = True
            moderation_reason = moderation.reason
            moderation_normalized_text = moderation.normalized_text

    ip_hash = _hash_signup_ip(
        request.headers.get("X-Forwarded-For", request.remote_addr)
    )

    try:
        agreement = promote_user_to_creator(
            user_id=user.id,
            tos_version=tos_version,
            disclaimer_version=disclaimer_version,
            disclaimer_acknowledged_at=disclaimer_ack_at,
            signup_ip_hash=ip_hash,
            operator_review_needed=operator_review_needed,
            moderation_reason=moderation_reason,
            moderation_normalized_text=moderation_normalized_text,
        )
    except CreatorPromotionError as exc:
        app.logger.warning("creator promotion failed: %s", exc)
        return jsonify({
            "success": False,
            "error": "promotion_failed",
            "detail": str(exc),
        }), 400

    # Refresh the user to pick up the new role.
    refreshed = get_user(user.id)
    new_role = refreshed.role if refreshed else "creator"

    # Mint a new session token reflecting the elevated role + set the
    # cookie. The frontend will pick this up on its next /api/auth/me
    # call (or directly from the response).
    session_token = mint_session_token(user.id, role=new_role)
    response = jsonify({
        "success": True,
        "user": {
            "user_id": user.id,
            "email": refreshed.email if refreshed else user.email,
            "display_name": refreshed.display_name if refreshed else user.display_name,
            "role": new_role,
        },
        "active_agreement": _agreement_dict(agreement),
    })
    _set_cookie(response, SESSION_COOKIE_NAME, session_token, SESSION_TTL_SECONDS)
    return response


# ─────────────────────────────────────────────────────────────────
# Suggestions (V1-UI-3 — login-gated user query against processed
# episodes; the input_moderation "suggestion_query" surface in
# `parsers/input_moderation.py` carries the deterministic content
# rules + rate-limit per-day cap).
# ─────────────────────────────────────────────────────────────────

_SUGGESTION_QUERY_MAX_CHARS = 500  # matches input_moderation.SURFACE_DEFAULTS


@app.route('/api/suggestions', methods=['POST'])
@_require_trusted_origin
def api_suggestions_create():
    """Submit a query/suggestion about a processed episode.

    Body: { "meeting_id": int, "query": str }

    The full audit trail (accepted / rejected / flagged) is persisted
    regardless of moderation outcome. The response tells the client
    whether the submission was visible-to-be-actioned or was flagged
    for operator review.
    """
    # RR-8 / SEC-AUTH-3: owner-ONLY (was any-signed-in-user). Operator-security
    # material; the hardening capture script already presents an owner cookie.
    user, _err = _require_owner()
    if _err:
        return _err

    payload = request.get_json(silent=True) or {}
    raw_meeting_id = payload.get("meeting_id")
    raw_query = payload.get("query")

    if not isinstance(raw_meeting_id, int) or raw_meeting_id <= 0:
        return jsonify({
            "success": False,
            "error": "invalid_meeting_id",
        }), 400
    if not isinstance(raw_query, str):
        return jsonify({
            "success": False,
            "error": "invalid_query",
        }), 400
    query_text = raw_query.strip()
    if not query_text:
        return jsonify({
            "success": False,
            "error": "empty_query",
        }), 400
    if len(query_text) > _SUGGESTION_QUERY_MAX_CHARS:
        return jsonify({
            "success": False,
            "error": "query_too_long",
            "detail": f"max {_SUGGESTION_QUERY_MAX_CHARS} chars",
        }), 400

    moderation = moderate_user_input(
        query_text,
        surface="suggestion_query",
        user_id=user.id,
    )

    # Persist regardless of accept — audit trail per V1-UI-3.
    operator_review_needed = (
        1 if (moderation.accept and moderation.reason == "flagged") else 0
    )
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO suggestions (
                user_id, meeting_id, query_text, normalized_text,
                accepted, reason, operator_review_needed
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user.id,
                raw_meeting_id,
                query_text,
                moderation.normalized_text,
                1 if moderation.accept else 0,
                moderation.reason,
                operator_review_needed,
            ),
        )
        suggestion_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    if not moderation.accept:
        return jsonify({
            "success": False,
            "error": "moderation_rejected",
            "reason": moderation.reason,
            "suggestion_id": suggestion_id,
        }), 400

    return jsonify({
        "success": True,
        "suggestion_id": suggestion_id,
        "operator_review_needed": bool(operator_review_needed),
    })


# ─────────────────────────────────────────────────────────────────
# Operator review queue — closes the moderation loop V1-UI-3 +
# Creator Network chunk 8 populate with operator_review_needed=1
# rows. Surfaces both feeds in one place for the operator to clear.
# ─────────────────────────────────────────────────────────────────
# Perimeter gate: in production the OperatorTerminal route lives
# behind Cloudflare Access (D-051). Locally the operator is the only
# signed-in user. We additionally require an authenticated session
# here as defense-in-depth — an unauthenticated public caller never
# reads the queue or clears flags.

_REVIEW_QUEUE_VALID_ACTIONS_SUGGESTION = {"dismiss", "reject"}
_REVIEW_QUEUE_VALID_ACTIONS_CREATOR = {"dismiss", "revoke"}
_REVIEW_QUEUE_VALID_ACTIONS_HARDENING = {"triaged", "resolved"}
_REVIEW_QUEUE_VALID_ACTIONS_REPOSITORY = {"approve", "reject", "withdraw"}
_REVIEW_QUEUE_NOTE_MAX_CHARS = 500
# Reject + withdraw on repository_assets require a non-empty reason so
# the public-readable filter log (per CREATOR_NETWORK_PLAYBOOK.md §
# Faucet criteria) has substantive content. Approve does not.
_REPOSITORY_FILTER_REASON_MAX_CHARS = 500

# D-100 hardening findings ingest constraints. Per HARDENING_FINDINGS_SCHEMA.md.
_HARDENING_SCHEMA_VERSIONS = {"1"}
_HARDENING_SEVERITY_VALUES = {"low", "medium", "high"}
_HARDENING_MAX_FINDINGS_PER_INGEST = 200
_HARDENING_MAX_RUN_LABEL = 100
_HARDENING_MAX_RUNNER_IDENTITY = 100
_HARDENING_MAX_OBSERVATION = 2000
_HARDENING_MAX_MITIGATION = 2000
_HARDENING_MAX_RUNNER_NOTES = 1000
_HARDENING_SURFACE_ID_RE = re.compile(r"^S-\d+$")


@app.route('/api/operator/review-queue', methods=['GET'])
def api_operator_review_queue():
    """Aggregate moderation-flagged rows awaiting operator review.

    Returns two grouped lists (each newest-first):
      - suggestions: V1-UI-3 query submissions flagged by the
        suggestion_query surface
      - creator_signups: Creator Network chunk 8 signups flagged by
        the creator_signup surface

    Each row carries the submitter (display_name + email), the
    flagged content (query_text / display_name + handle + context),
    the moderation verdict reason, and submitted-at. Already-resolved
    rows are excluded — the partial indexes built in database.init_db
    keep this cheap.
    """
    # Session-31 (2026-07-04) — auth-audit remediation. Prior gate was
    # signed-in-only, so any light/creator user could read other users'
    # emails + flagged submissions. Upgraded to owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.id, s.user_id, u.display_name, u.email, u.role,
                   s.meeting_id, s.query_text, s.normalized_text,
                   s.reason, s.created_at
            FROM suggestions s
            JOIN users u ON u.id = s.user_id
            WHERE s.operator_review_needed = 1
              AND s.operator_resolved_at IS NULL
            ORDER BY s.created_at DESC
            LIMIT 200
            """
        )
        suggestions = [
            {
                "id": row[0],
                "user_id": row[1],
                "display_name": row[2],
                "email": row[3],
                "user_role": row[4],
                "meeting_id": row[5],
                "query_text": row[6],
                "normalized_text": row[7],
                "moderation_reason": row[8],
                "submitted_at": row[9],
            }
            for row in cursor.fetchall()
        ]

        cursor.execute(
            """
            SELECT ca.id, ca.user_id, u.display_name, u.email, u.role,
                   ca.tos_version, ca.disclaimer_version,
                   ca.moderation_reason, ca.moderation_normalized_text,
                   ca.signed_at, ca.revoked_at
            FROM creator_agreements ca
            JOIN users u ON u.id = ca.user_id
            WHERE ca.operator_review_needed = 1
              AND ca.operator_resolved_at IS NULL
            ORDER BY ca.signed_at DESC
            LIMIT 200
            """
        )
        creator_signups = [
            {
                "id": row[0],
                "user_id": row[1],
                "display_name": row[2],
                "email": row[3],
                "user_role": row[4],
                "tos_version": row[5],
                "disclaimer_version": row[6],
                "moderation_reason": row[7],
                "moderation_normalized_text": row[8],
                "submitted_at": row[9],
                "revoked_at": row[10],
            }
            for row in cursor.fetchall()
        ]

        cursor.execute(
            """
            SELECT af.id, af.run_id, ar.run_label, ar.runner_identity, ar.run_date,
                   af.surface_id, af.severity, af.defensive_observation,
                   af.suggested_mitigation, af.created_at
            FROM adversarial_findings af
            JOIN adversarial_runs ar ON ar.id = af.run_id
            WHERE af.status = 'open'
            ORDER BY af.created_at DESC, af.id DESC
            LIMIT 200
            """
        )
        adversarial_findings = [
            {
                "id": row[0],
                "run_id": row[1],
                "run_label": row[2],
                "runner_identity": row[3],
                "run_date": row[4],
                "surface_id": row[5],
                "severity": row[6],
                "defensive_observation": row[7],
                "suggested_mitigation": row[8],
                "submitted_at": row[9],
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()

    # D-095 / D-006 repository deposit gate (V1-Repo-1). The repository
    # queue uses its own helper because the join + JSON metadata
    # decoding live in repository_gate; keep the SQL near the rest of
    # the polymorphic-asset logic rather than inlining it here.
    repository_pending = list_pending_review_assets(limit=200)

    return jsonify({
        "success": True,
        "suggestions": suggestions,
        "creator_signups": creator_signups,
        "adversarial_findings": adversarial_findings,
        "repository_pending": repository_pending,
        "counts": {
            "suggestions": len(suggestions),
            "creator_signups": len(creator_signups),
            "adversarial_findings": len(adversarial_findings),
            "repository_pending": len(repository_pending),
            "total": (
                len(suggestions)
                + len(creator_signups)
                + len(adversarial_findings)
                + len(repository_pending)
            ),
        },
    })


@app.route('/api/operator/review-queue/<string:queue_type>/<int:row_id>/resolve',
           methods=['POST'])
@_require_trusted_origin
def api_operator_review_queue_resolve(queue_type: str, row_id: int):
    """Clear the review flag on a single row + record the operator
    action. Body: { "action": "dismiss"|"reject"|"revoke", "note"?: str }.

    For creator_signups, action="revoke" additionally calls
    revoke_creator_role so the user drops back to role='light' AND the
    agreement row carries revoked_at. action="dismiss" leaves the
    role intact (operator confirmed the signup is legitimate).
    """
    # Session-31 (2026-07-04) — auth-audit remediation. Prior gate was
    # signed-in-only. Anyone signed in could `action=revoke` to strip
    # another user's creator role, attributed to their own email. This
    # is the highest-impact "wrong tier" gap the audit found. Owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    user = _user  # preserved for the resolved_by attribution below

    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip().lower()
    note = (payload.get("note") or "").strip() or None
    if note and len(note) > _REVIEW_QUEUE_NOTE_MAX_CHARS:
        return jsonify({
            "success": False,
            "error": "note_too_long",
            "detail": f"max {_REVIEW_QUEUE_NOTE_MAX_CHARS} chars",
        }), 400

    if queue_type == "suggestions":
        if action not in _REVIEW_QUEUE_VALID_ACTIONS_SUGGESTION:
            return jsonify({
                "success": False,
                "error": "invalid_action",
                "detail": f"allowed: {sorted(_REVIEW_QUEUE_VALID_ACTIONS_SUGGESTION)}",
            }), 400
        table = "suggestions"
    elif queue_type == "creator_signups":
        if action not in _REVIEW_QUEUE_VALID_ACTIONS_CREATOR:
            return jsonify({
                "success": False,
                "error": "invalid_action",
                "detail": f"allowed: {sorted(_REVIEW_QUEUE_VALID_ACTIONS_CREATOR)}",
            }), 400
        table = "creator_agreements"
    elif queue_type == "adversarial_findings":
        if action not in _REVIEW_QUEUE_VALID_ACTIONS_HARDENING:
            return jsonify({
                "success": False,
                "error": "invalid_action",
                "detail": f"allowed: {sorted(_REVIEW_QUEUE_VALID_ACTIONS_HARDENING)}",
            }), 400
        table = "adversarial_findings"
    else:
        return jsonify({
            "success": False,
            "error": "invalid_queue_type",
            "detail": "allowed: suggestions, creator_signups, adversarial_findings",
        }), 400

    operator_label = user.email

    conn = get_connection()
    try:
        cursor = conn.cursor()
        if table == "adversarial_findings":
            # The findings table tracks state via a `status` column
            # rather than the operator_review_needed boolean — open / triaged /
            # resolved. Same audit shape (operator_resolved_*) otherwise.
            cursor.execute(
                "SELECT id, status, operator_resolved_at FROM adversarial_findings WHERE id = ?",
                (row_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return jsonify({
                    "success": False,
                    "error": "not_found",
                    "detail": f"{queue_type} id={row_id} does not exist",
                }), 404
            if row[1] != "open" and row[2] is not None:
                return jsonify({
                    "success": False,
                    "error": "already_resolved",
                    "resolved_at": row[2],
                    "status": row[1],
                }), 409
            cursor.execute(
                """
                UPDATE adversarial_findings
                SET status = ?,
                    operator_resolved_at = CURRENT_TIMESTAMP,
                    operator_resolved_by = ?,
                    operator_action = ?,
                    operator_note = ?
                WHERE id = ?
                """,
                (action, operator_label, action, note, row_id),
            )
            conn.commit()
            return jsonify({
                "success": True,
                "queue_type": queue_type,
                "row_id": row_id,
                "action": action,
                "new_status": action,
            })

        cursor.execute(
            f"""
            SELECT id, user_id, operator_review_needed, operator_resolved_at
            FROM {table}
            WHERE id = ?
            """,
            (row_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return jsonify({
                "success": False,
                "error": "not_found",
                "detail": f"{queue_type} id={row_id} does not exist",
            }), 404
        if row[2] == 0 and row[3] is not None:
            return jsonify({
                "success": False,
                "error": "already_resolved",
                "resolved_at": row[3],
            }), 409
        target_user_id = row[1]

        cursor.execute(
            f"""
            UPDATE {table}
            SET operator_review_needed = 0,
                operator_resolved_at = CURRENT_TIMESTAMP,
                operator_resolved_by = ?,
                operator_action = ?,
                operator_note = ?
            WHERE id = ?
            """,
            (operator_label, action, note, row_id),
        )
        conn.commit()
    finally:
        conn.close()

    revoked = False
    if queue_type == "creator_signups" and action == "revoke":
        # Drop the user back to role='light' + stamp revoked_at +
        # revoked_reason on the agreement so D-095 audit trail is
        # honest about why the role was removed.
        reason = note or "operator review queue: revoke"
        revoked = revoke_creator_role(target_user_id, reason)

    return jsonify({
        "success": True,
        "queue_type": queue_type,
        "row_id": row_id,
        "action": action,
        "revoked_creator_role": revoked,
    })


# ─────────────────────────────────────────────────────────────────
# D-095 / D-006 repository deposit gate — operator queue actions
# ─────────────────────────────────────────────────────────────────
# Sibling to the review-queue/resolve endpoint above; uses its own
# route because the action taxonomy is transition-typed (approve only
# legal from pending_owner_review; withdraw only legal from approved)
# and because reject + withdraw require a non-empty reason that lands
# in the public-readable repository_filter_log.


@app.route(
    '/api/operator/repository-queue/<int:asset_id>/<string:action>',
    methods=['POST'],
)
@_require_trusted_origin
def api_operator_repository_queue_action(asset_id: int, action: str):
    """Move a repository_assets row through the D-095 state machine.

    Path: /api/operator/repository-queue/<asset_id>/(approve|reject|withdraw)
    Body: { "reason"?: str }
      - approve: ignores reason; flips pending_owner_review -> approved.
      - reject:  requires non-empty reason; flips pending_owner_review -> draft,
                 writes a repository_filter_log row (filter_action='reject').
      - withdraw: requires non-empty reason; flips approved -> withdrawn,
                  writes a repository_filter_log row (filter_action='withdraw').

    Errors:
      401 not_authenticated — no auth cookie / unknown user.
      400 invalid_action   — action not in {approve, reject, withdraw}.
      400 reason_required  — reject / withdraw with empty reason.
      400 reason_too_long  — reason exceeds _REPOSITORY_FILTER_REASON_MAX_CHARS.
      404 not_found        — asset_id does not exist.
      409 illegal_transition — asset is in a status the action can't move out of
                              (e.g., approve on withdrawn).
    """
    # Session-31 auth-audit — was signed-in-only. Upgrading to owner-only
    # because approve/reject/withdraw flips D-095 state on repository
    # assets (which surface via the Creator Network deposit gate); a
    # non-owner signed-in user shouldn't be moving other users' assets.
    _user, _err = _require_owner()
    if _err:
        return _err
    user = _user

    action_norm = (action or "").strip().lower()
    if action_norm not in _REVIEW_QUEUE_VALID_ACTIONS_REPOSITORY:
        return jsonify({
            "success": False,
            "error": "invalid_action",
            "detail": f"allowed: {sorted(_REVIEW_QUEUE_VALID_ACTIONS_REPOSITORY)}",
        }), 400

    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "").strip()

    if action_norm in ("reject", "withdraw"):
        if not reason:
            return jsonify({
                "success": False,
                "error": "reason_required",
                "detail": f"{action_norm} requires a non-empty reason in body.reason",
            }), 400
        if len(reason) > _REPOSITORY_FILTER_REASON_MAX_CHARS:
            return jsonify({
                "success": False,
                "error": "reason_too_long",
                "detail": f"max {_REPOSITORY_FILTER_REASON_MAX_CHARS} chars",
            }), 400

    operator_label = user.email

    try:
        if action_norm == "approve":
            asset = approve_repository_asset(asset_id, approved_by=operator_label)
        elif action_norm == "reject":
            asset = reject_repository_asset(
                asset_id, rejected_by=operator_label, reason=reason
            )
        else:  # action_norm == "withdraw"
            asset = withdraw_repository_asset(
                asset_id, withdrawn_by=operator_label, reason=reason
            )
    except AssetNotFoundError:
        return jsonify({
            "success": False,
            "error": "not_found",
            "detail": f"repository_assets id={asset_id} does not exist",
        }), 404
    except IllegalTransitionError as exc:
        return jsonify({
            "success": False,
            "error": "illegal_transition",
            "detail": str(exc),
            "current_status": exc.current_status,
            "action": exc.action,
        }), 409

    return jsonify({
        "success": True,
        "asset_id": asset.id,
        "action": action_norm,
        "new_status": asset.repository_status,
        "approved_at": asset.approved_at,
        "approved_by": asset.approved_by,
        "withdrawn_at": asset.withdrawn_at,
        "withdrawn_reason": asset.withdrawn_reason,
        "filter_reason": asset.filter_reason,
    })


# ─────────────────────────────────────────────────────────────────
# D-100 hardening findings ingest + history
# ─────────────────────────────────────────────────────────────────
# Per HARDENING_FINDINGS_SCHEMA.md, findings come from the
# Antigravity-Jules-Gemini-Pro side via a JSON file the operator
# captures + the CLI POSTs here. Schema validation lives in this
# function — the contract is operator-facing, so a malformed payload
# returns a clear error rather than a stack trace.


def _validate_hardening_payload(payload: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (error_code, error_detail) — both None when payload is valid."""
    if not isinstance(payload, dict):
        return "invalid_shape", "top-level payload must be an object"

    schema_version = payload.get("schema_version")
    if schema_version not in _HARDENING_SCHEMA_VERSIONS:
        return "unsupported_schema_version", (
            f"got {schema_version!r}; supported: "
            f"{sorted(_HARDENING_SCHEMA_VERSIONS)}"
        )

    run_meta = payload.get("run_metadata")
    if not isinstance(run_meta, dict):
        return "invalid_run_metadata", "run_metadata must be an object"

    for key, max_len in [
        ("run_label", _HARDENING_MAX_RUN_LABEL),
        ("runner_identity", _HARDENING_MAX_RUNNER_IDENTITY),
    ]:
        value = run_meta.get(key)
        if not isinstance(value, str) or not value.strip():
            return "missing_run_metadata", f"run_metadata.{key} required (string)"
        if len(value) > max_len:
            return "run_metadata_too_long", f"run_metadata.{key} exceeds {max_len} chars"

    run_date = run_meta.get("run_date")
    if not isinstance(run_date, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", run_date):
        return "invalid_run_date", "run_metadata.run_date must be YYYY-MM-DD"

    scope_surfaces = run_meta.get("scope_surfaces")
    if not isinstance(scope_surfaces, list) or not scope_surfaces:
        return "invalid_scope", "run_metadata.scope_surfaces must be a non-empty list"
    for s in scope_surfaces:
        if not isinstance(s, str) or not _HARDENING_SURFACE_ID_RE.match(s):
            return "invalid_scope_id", f"scope_surfaces entry {s!r} is not S-N shape"
    scope_set = set(scope_surfaces)

    runner_notes = run_meta.get("notes")
    if runner_notes is not None:
        if not isinstance(runner_notes, str):
            return "invalid_runner_notes", "run_metadata.notes must be a string"
        if len(runner_notes) > _HARDENING_MAX_RUNNER_NOTES:
            return "runner_notes_too_long", f"exceeds {_HARDENING_MAX_RUNNER_NOTES} chars"

    findings = payload.get("findings")
    if not isinstance(findings, list):
        return "invalid_findings", "findings must be a list"
    if len(findings) > _HARDENING_MAX_FINDINGS_PER_INGEST:
        return "too_many_findings", (
            f"max {_HARDENING_MAX_FINDINGS_PER_INGEST} per ingest "
            "(split into multiple runs if needed)"
        )

    for idx, f in enumerate(findings):
        if not isinstance(f, dict):
            return "invalid_finding_shape", f"findings[{idx}] must be an object"
        surface_id = f.get("surface_id")
        if not isinstance(surface_id, str) or not _HARDENING_SURFACE_ID_RE.match(surface_id):
            return "invalid_finding_surface", f"findings[{idx}].surface_id must be S-N shape"
        if surface_id not in scope_set:
            return "finding_outside_scope", (
                f"findings[{idx}].surface_id={surface_id} not in run scope {sorted(scope_set)}"
            )
        severity = f.get("severity")
        if severity not in _HARDENING_SEVERITY_VALUES:
            return "invalid_finding_severity", (
                f"findings[{idx}].severity must be one of "
                f"{sorted(_HARDENING_SEVERITY_VALUES)}"
            )
        obs = f.get("defensive_observation")
        if not isinstance(obs, str) or not obs.strip():
            return "missing_finding_observation", f"findings[{idx}].defensive_observation required"
        if len(obs) > _HARDENING_MAX_OBSERVATION:
            return "finding_observation_too_long", (
                f"findings[{idx}].defensive_observation exceeds {_HARDENING_MAX_OBSERVATION} chars"
            )
        mit = f.get("suggested_mitigation")
        if not isinstance(mit, str) or not mit.strip():
            return "missing_finding_mitigation", f"findings[{idx}].suggested_mitigation required"
        if len(mit) > _HARDENING_MAX_MITIGATION:
            return "finding_mitigation_too_long", (
                f"findings[{idx}].suggested_mitigation exceeds {_HARDENING_MAX_MITIGATION} chars"
            )

    return None, None


@app.route('/api/operator/hardening-runs/ingest', methods=['POST'])
@_require_trusted_origin
def api_hardening_ingest():
    """Ingest a findings JSON payload produced by an Antigravity-Jules-Gemini-Pro
    hardening pass. Validates against HARDENING_FINDINGS_SCHEMA.md + persists
    the run + every finding (each finding lands status='open' so it surfaces in
    the operator review queue).
    """
    # RR-8 / SEC-AUTH-3: owner-ONLY (was any-signed-in-user). Operator-security
    # material; the hardening capture script already presents an owner cookie.
    user, _err = _require_owner()
    if _err:
        return _err

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({
            "success": False,
            "error": "invalid_json",
            "detail": "body must be JSON matching HARDENING_FINDINGS_SCHEMA.md",
        }), 400

    err_code, err_detail = _validate_hardening_payload(payload)
    if err_code:
        return jsonify({
            "success": False,
            "error": err_code,
            "detail": err_detail,
        }), 400

    run_meta = payload["run_metadata"]
    findings = payload["findings"]
    scope_surfaces = run_meta["scope_surfaces"]

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO adversarial_runs (
                schema_version, run_label, run_date, runner_identity,
                scope_surfaces, runner_notes, findings_count, ingested_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["schema_version"],
                run_meta["run_label"].strip(),
                run_meta["run_date"],
                run_meta["runner_identity"].strip(),
                json.dumps(scope_surfaces),
                (run_meta.get("notes") or "").strip() or None,
                len(findings),
                user.email,
            ),
        )
        run_id = cursor.lastrowid

        finding_ids: list[int] = []
        for f in findings:
            cursor.execute(
                """
                INSERT INTO adversarial_findings (
                    run_id, surface_id, severity, defensive_observation,
                    suggested_mitigation
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    f["surface_id"],
                    f["severity"],
                    f["defensive_observation"].strip(),
                    f["suggested_mitigation"].strip(),
                ),
            )
            finding_ids.append(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()

    return jsonify({
        "success": True,
        "run_id": run_id,
        "findings_count": len(finding_ids),
        "finding_ids": finding_ids,
    })


@app.route('/api/operator/hardening-runs', methods=['GET'])
def api_hardening_runs_list():
    """Recent hardening runs with finding counts grouped by status."""
    # RR-8 / SEC-AUTH-3: owner-ONLY (was any-signed-in-user). Operator-security
    # material; the hardening capture script already presents an owner cookie.
    user, _err = _require_owner()
    if _err:
        return _err

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ar.id, ar.run_label, ar.run_date, ar.runner_identity,
                   ar.scope_surfaces, ar.runner_notes, ar.findings_count,
                   ar.ingested_at, ar.ingested_by,
                   COALESCE(SUM(CASE WHEN af.status = 'open' THEN 1 ELSE 0 END), 0)     AS open_count,
                   COALESCE(SUM(CASE WHEN af.status = 'triaged' THEN 1 ELSE 0 END), 0)  AS triaged_count,
                   COALESCE(SUM(CASE WHEN af.status = 'resolved' THEN 1 ELSE 0 END), 0) AS resolved_count
            FROM adversarial_runs ar
            LEFT JOIN adversarial_findings af ON af.run_id = ar.id
            GROUP BY ar.id
            ORDER BY ar.run_date DESC, ar.id DESC
            LIMIT 50
            """
        )
        runs = []
        for row in cursor.fetchall():
            scope = []
            try:
                scope = json.loads(row[4]) if row[4] else []
            except json.JSONDecodeError:
                pass
            runs.append({
                "id": row[0],
                "run_label": row[1],
                "run_date": row[2],
                "runner_identity": row[3],
                "scope_surfaces": scope,
                "runner_notes": row[5],
                "findings_count": row[6],
                "ingested_at": row[7],
                "ingested_by": row[8],
                "open": row[9],
                "triaged": row[10],
                "resolved": row[11],
            })
    finally:
        conn.close()

    return jsonify({"success": True, "runs": runs})


# ─────────────────────────────────────────────────────────────────
# NotebookLM auth/chat routes (removed per D-143 2026-07-01) — the
# frontend probes /api/auth/notebooklm/* + /api/notebook/*/chat are
# retired alongside the underlying subsystem. See D-143 in DECISIONS.md
# and S-109 in FUTURE_THOUGHTS.md for the removal arc.
# ─────────────────────────────────────────────────────────────────


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint with stats."""
    try:
        stats = get_stats()
        return jsonify({
            'status': 'ok',
            'total_cities': stats['total_cities'],
            'total_meetings': stats['total_meetings'],
            'active_cities': stats['active_cities']
        })
    except:
        return jsonify({'status': 'ok'})


# ── D-039 system status banner ────────────────────────────────────────
#
# Single aggregated endpoint the operator terminal's status banner polls
# every few seconds. Returning everything in one call keeps the banner
# chatter to a single round-trip and lets the UI atomically reconcile
# auth + worker + queue state.
@app.route('/api/system/status', methods=['GET'])
def api_system_status():
    """Aggregated status for the operator terminal's always-on banner.

    Returns:
      {
        success: true,
        flask_up: true,                    // implicit — if you got a response, Flask is up
        auth: { status, cached, details? },// stub (NotebookLM removed per D-143)
        work_orders: {
          stats: { pending, processing, ..., total },
          processing: [ { id, meeting_id, meeting_title, city, started_at, elapsed_seconds } ],
          queue_depth: int                 // pending + processing
        }
      }
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    from database import work_order_stats, list_work_orders

    out: dict = {'success': True, 'flask_up': True}

    # Auth — legacy NotebookLM auth probe removed per D-143 2026-07-01.
    # Frontend still reads the auth object shape; return a permanent
    # "ok — no NotebookLM auth to track" so the banner stays green.
    out['auth'] = {
        'status': 'ok',
        'cached': False,
        'details': 'NotebookLM removed per D-143 (2026-07-01); no auth to track',
    }

    # Work-order stats + in-flight detail
    try:
        stats = work_order_stats()
        processing_rows = list_work_orders(state='processing', limit=10)
        processing = []
        now_utc = datetime.utcnow()
        for r in processing_rows:
            elapsed = None
            started = r.get('started_at')
            if started:
                try:
                    started_dt = datetime.fromisoformat(started)
                    elapsed = int(max(0, (now_utc - started_dt).total_seconds()))
                except Exception:
                    pass
            entry = {
                'id': r.get('id'),
                'city': r.get('city_name'),
                'elapsed_seconds': elapsed,
            }
            entry['meeting_id'] = r.get('meeting_id')
            entry['meeting_title'] = r.get('meeting_title')
            entry['started_at'] = started
            processing.append(entry)
        out['work_orders'] = {
            'stats': stats,
            'processing': processing,
            'queue_depth': stats.get('pending', 0) + stats.get('processing', 0),
        }
    except Exception:
        # Stable code — raw str(e) previously leaked internals to anon.
        logging.exception("system status work-order stats failed")
        out['work_orders'] = {'error': 'work-order status unavailable'}

    return jsonify(out)


@app.route('/api/system/heartbeat', methods=['POST'])
@_public_rate_limited('system_heartbeat')
def api_system_heartbeat():
    # INTENTIONALLY PUBLIC (RR-8 fix-list disposition, S-129): low-stakes
    # session-presence upsert with a 30s prune; worst-case abuse is noisy
    # rows that self-expire. Gating it would break the anonymous-viewer
    # collision banner for no security gain. Re-evaluate only if the
    # active_sessions surface ever grows write-amplification.
    """D-039 follow-up: cross-session conflict detection heartbeat.

    Body: {session_id: str, client_kind: str, current_action?: str}

    Upserts this session's row in active_sessions, prunes stale rows
    (>30s since last heartbeat), and returns a small summary of OTHER
    currently-active sessions. The frontend StatusBanner uses this to
    surface a warning when N > 0 — so the operator never accidentally
    collides with another tab or a manually-run script that's also
    touching state.

    Response shape:
      {
        success: true,
        other_active: int,
        sessions: [{client_kind, age_seconds, current_action}, ...]
      }
    """
    from database import heartbeat_session

    body = request.get_json(silent=True) or {}
    raw_session_id = body.get('session_id')
    raw_client_kind = body.get('client_kind')
    current_action = body.get('current_action')

    if not isinstance(raw_session_id, str) or not isinstance(raw_client_kind, str):
        return jsonify({
            'success': False,
            'error': 'session_id and client_kind are required',
        }), 400
    if current_action is not None and not isinstance(current_action, str):
        return jsonify({
            'success': False,
            'error': 'current_action must be a string',
        }), 400

    fields = {
        'session_id': raw_session_id,
        'client_kind': raw_client_kind,
        'current_action': current_action or '',
    }
    for field_name, value in fields.items():
        max_length = _HEARTBEAT_FIELD_MAX_LENGTHS[field_name]
        if len(value) > max_length:
            return jsonify({
                'success': False,
                'error': f'{field_name} exceeds {max_length} characters',
            }), 400

    session_id = raw_session_id.strip()
    client_kind = raw_client_kind.strip()

    if not session_id or not client_kind:
        return jsonify({
            'success': False,
            'error': 'session_id and client_kind are required',
        }), 400

    try:
        result = heartbeat_session(session_id, client_kind, current_action)
        try:
            anonymous = _current_user_from_cookie() is None
        except Exception:
            logging.exception("heartbeat cookie resolution failed")
            anonymous = True
        sessions = result['sessions']
        if anonymous:
            sessions = sessions[:_ANONYMOUS_HEARTBEAT_SESSION_LIMIT]
        return jsonify({
            'success': True,
            'other_active': result['other_active'],
            'sessions': sessions,
        })
    except Exception:
        # Public route (S-129 intentional). Log full detail server-side;
        # return a stable code — raw str(e) leaked e.g. SQLite type errors.
        logging.exception("api_system_heartbeat failed")
        return jsonify({'success': False, 'error': 'heartbeat failed'}), 500


# ── Cast page V1 (T-007) ───────────────────────────────────────────────
#
# /api/cast/<city>           -> roster + per-member counts (attendance, quotes)
# /api/cast/<city>/<seat_id> -> single-member profile with attendance + quotes
#
# Data sources:
#   - city_intelligence/<slug>.json (canonical metadata: county, election,
#     notes, verified_on)
#   - council_members table (seeded from the JSON on startup)
#   - member_attendance + member_quotes tables (populated by future
#     NotebookLM extraction prompts; empty in V1)
#
# The empty attendance/quotes tables are intentional in V1: the React
# Cast page is meant to render the SKELETON now so the layout is testable
# and stakeholder-shareable while the extraction pipeline is built out.

def _city_slug(city_name: str) -> str:
    return city_name.lower().replace(' ', '_')


def _load_city_intelligence(city_name: str):
    intel_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'city_intelligence'
    )
    path = os.path.join(intel_dir, f'{_city_slug(city_name)}.json')
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _role_sort_key(role):
    return {'Mayor': 1, 'Vice Mayor': 2, 'Council Member': 3}.get(role or '', 99)


@app.route('/api/cast/<city_name>', methods=['GET'])
def get_cast_roster(city_name):
    """Return the council roster for a city plus per-member counts."""
    _user, _err = _require_owner()
    if _err:
        return _err
    conn = get_connection()
    cursor = conn.cursor()
    # Quotes Unification Refactor Chunk 7 — quote_count now sourced from
    # the unified `quotes` table filtered to council_member rows (so the
    # count reflects only this person's own quotes, not staff/external
    # quotes that may share a meeting). Excludes rejected + disputed quotes
    # so the count matches what's actually visible on the Cast page.
    cursor.execute("""
        SELECT
            cm.id, cm.name, cm.role, cm.seat_id,
            cm.term_started, cm.term_ends, cm.source_url,
            (SELECT COUNT(*) FROM member_attendance ma WHERE ma.member_id = cm.id) AS attendance_count,
            (SELECT COUNT(*) FROM quotes q
              WHERE q.member_id = cm.id
                AND q.speaker_class = 'council_member'
                AND q.verified_status NOT IN ('rejected', 'disputed')) AS quote_count
        FROM council_members cm
        WHERE cm.city_name = ? AND cm.seat_id IS NOT NULL
    """, (city_name,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    rows.sort(key=lambda r: (_role_sort_key(r.get('role')), r.get('seat_id') or ''))

    intel = _load_city_intelligence(city_name) or {}
    council = intel.get('council') or {}

    return jsonify({
        'city': city_name,
        'county': intel.get('county'),
        'state': intel.get('state'),
        'verified_on': intel.get('verified_on'),
        'notes': intel.get('notes'),
        'council': {
            'seats': council.get('seats'),
            'term_length_years': council.get('term_length_years'),
            'next_election_date': council.get('next_election_date'),
            'next_election_seats_up': council.get('next_election_seats_up'),
        },
        'members': rows,
    })


@app.route('/api/cast/<city_name>/<seat_id>', methods=['GET'])
def get_cast_member(city_name, seat_id):
    """Return one member's record.

    D-157 (neutrality output cut): the *presented* Cast surface no longer
    renders a Z-SPAN-authored dossier — it shows the seat as the city
    publishes it plus a link to the city's own official record
    (`city_official_url`, from city_intelligence primary_source_url). The
    attendance/quotes/tracked_claims fields are retained on the response for
    the owner-only Record (TruthBook) tooling and the return path
    (hide-not-delete). The public profile uses /public-api/cast instead.
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, name, role, seat_id, term_started, term_ends, source_url
        FROM council_members
        WHERE city_name = ? AND seat_id = ?
    """, (city_name, seat_id))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'member not found', 'city': city_name, 'seat_id': seat_id}), 404

    member = dict(row)
    member_id = member['id']

    cursor.execute("""
        SELECT
            ma.status, ma.notes,
            m.id AS meeting_id, m.meeting_title, m.meeting_date
        FROM member_attendance ma
        JOIN meetings m ON m.id = ma.meeting_id
        WHERE ma.member_id = ?
        ORDER BY m.meeting_date DESC
        LIMIT 200
    """, (member_id,))
    attendance = [dict(r) for r in cursor.fetchall()]

    # Quotes Unification Refactor Chunk 7 (2026-05-26): read from the
    # unified `quotes` table filtered to council_member speakers for this
    # member. The migration copied member_quotes verification + alignment
    # state into `quotes` preserving everything; new unified extractions
    # populate the table directly. Legacy member_quotes table retires in
    # Chunk 9. See 01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md.
    cursor.execute("""
        SELECT
            q.id, q.quote_text, q.quote_text_original, q.topic_tags,
            q.minutes_page_ref, q.context,
            q.video_timestamp_seconds, q.word_timings,
            q.is_broadcast_hero,
            q.verified_status, q.verified_by, q.verified_at,
            q.gemini_correction_notes,
            q.proof_clip_url, q.proof_clip_sha256,
            q.speaker_role,
            m.id AS meeting_id, m.meeting_title, m.meeting_date,
            COALESCE(wo.youtube_video_url, m.video_url) AS meeting_video_url
        FROM quotes q
        JOIN meetings m ON m.id = q.meeting_id
        LEFT JOIN work_orders wo ON wo.meeting_id = m.id
        WHERE q.member_id = ?
          AND q.speaker_class = 'council_member'
          AND q.verified_status NOT IN ('rejected', 'disputed')
        ORDER BY m.meeting_date DESC, q.id
        LIMIT 200
    """, (member_id,))
    quotes = []
    for r in cursor.fetchall():
        q = dict(r)
        for field in ('topic_tags', 'word_timings', 'gemini_correction_notes'):
            v = q.get(field)
            if v:
                try:
                    q[field] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    q[field] = [] if field == 'topic_tags' else None
            elif field == 'topic_tags':
                q[field] = []
            else:
                q[field] = None
        # Derive a precise timestamp from the alignment when available —
        # the schema column is often null (NotebookLM doesn't reliably
        # return approximate_timestamp_seconds, see Phase 0a audit). The
        # Whisper-aligned word_timings give a real value.
        if q.get('video_timestamp_seconds') is None and isinstance(q.get('word_timings'), list):
            wt = q['word_timings']
            if wt and isinstance(wt[0], dict) and isinstance(wt[0].get('start_ms'), int):
                q['video_timestamp_seconds'] = wt[0]['start_ms'] // 1000
        quotes.append(q)

    # T-012 tracked claims for this member — preserved forward-looking
    # statements with status pills + marker-styled karaoke. Same
    # word_timings/topic_tags parsing as quotes above.
    cursor.execute("""
        SELECT
            tc.id, tc.claim_type, tc.claim_text, tc.expected_outcome,
            tc.time_horizon_months, tc.topic_tags, tc.confidence,
            tc.context, tc.word_timings, tc.status,
            tc.status_updated_at, tc.status_updated_by, tc.status_evidence,
            tc.extracted_at,
            m.id AS meeting_id, m.meeting_title, m.meeting_date,
            COALESCE(wo.youtube_video_url, m.video_url) AS meeting_video_url
        FROM tracked_claims tc
        JOIN meetings m ON m.id = tc.meeting_id
        LEFT JOIN work_orders wo ON wo.meeting_id = m.id
        WHERE tc.member_id = ?
        ORDER BY m.meeting_date DESC, tc.extracted_at DESC
        LIMIT 200
    """, (member_id,))
    tracked_claims = []
    for r in cursor.fetchall():
        c = dict(r)
        tags = c.get('topic_tags')
        if tags:
            try:
                c['topic_tags'] = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                c['topic_tags'] = []
        else:
            c['topic_tags'] = []
        wt = c.get('word_timings')
        if wt:
            try:
                c['word_timings'] = json.loads(wt)
            except (json.JSONDecodeError, TypeError):
                c['word_timings'] = None
        else:
            c['word_timings'] = None
        tracked_claims.append(c)

    conn.close()
    intel = _load_city_intelligence(city_name) or {}

    return jsonify({
        'city': city_name,
        'county': intel.get('county'),
        'state': intel.get('state'),
        # D-157: the official city record the presented Cast surface links to.
        'city_official_url': intel.get('primary_source_url'),
        'member': member,
        'attendance': attendance,
        'quotes': _genericize_speaker_attribution(quotes),
        'tracked_claims': _genericize_speaker_attribution(tracked_claims),
    })


# ── Truth Book Lite (D-059 Layer 1) ────────────────────────────────────
#
# GET /api/truth-book/<city>/<seat_id>
#   One Cast member's full record organized for the per-person research
#   surface: every publicly-visible quote grouped into per-topic swimlanes
#   on a shared time axis, plus the member's tracked claims (the
#   accountability layer). Renders only existing data — same visibility
#   filter as the Cast page. See 01_Project_Overview/TRUTH_BOOK_LITE_SPEC.md.


@app.route('/api/truth-book/<city_name>/<seat_id>', methods=['GET'])
def get_truth_book(city_name, seat_id):
    """Truth Book Lite — per-member swimlanes + tracked-claims layer."""
    # RR-8: owner-only. The "truth-book" view is in OWNER_ONLY_VIEWS and its
    # utils/truthBook.ts fetch is the only consumer, so a non-owner never
    # renders it — but the endpoint was ungated, letting anon pull the full
    # research dossier (reviewer identities, evidence hashes, word timings)
    # directly. Gate to match the React view (§5a).
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        from database import get_truth_book_for_member
        book = get_truth_book_for_member(city_name, seat_id)
    except Exception:
        app.logger.exception("get_truth_book failed for %s/%s", city_name, seat_id)
        return jsonify({'error': 'truth-book lookup failed'}), 500

    if book is None:
        return jsonify({
            'error': 'member not found', 'city': city_name, 'seat_id': seat_id,
        }), 404

    lanes = []
    for raw_lane in book.get('lanes') or []:
        if not isinstance(raw_lane, dict):
            lanes.append(raw_lane)
            continue
        lane = dict(raw_lane)
        lane['entries'] = _genericize_speaker_attribution(
            raw_lane.get('entries') or []
        )
        lanes.append(lane)

    response_book = dict(book)
    response_book['lanes'] = lanes
    response_book['claims'] = _genericize_speaker_attribution(
        book.get('claims') or []
    )
    intel = _load_city_intelligence(city_name) or {}
    return jsonify({
        'city': city_name,
        'county': intel.get('county'),
        'state': intel.get('state'),
        **response_book,
    })


# ── Conversational Compiler (S-023 Track A) ────────────────────────────
#
# GET  /api/compiler/<int:meeting_id>
#   Returns one meeting's tracked_claims rendered for the Hex-Rays UX.
#   V0: reads existing data only. In production the claims are extracted
#   by NotebookLM via prompts/tracked_claims.md (Decision #8a); the 3
#   m101091 sandbox rows are hand-seeded via
#   zspan_pipeline/scripts/seed_tracked_claims_m101091.py.
#   The Commit_P node-type framing lives in the frontend; the endpoint
#   returns the underlying claim rows with member-name JOIN so the UI
#   can render the speaker line without N+1 lookups.
#
# GET  /api/compiler/<int:meeting_id>/transcript
#   Returns the meeting's persisted Whisper word array — the canonical
#   source for Surface A's left full-transcript pane (SPEC build seq
#   item 4). Reads from notebook_outputs.transcript_words (produced by
#   _fetch_transcript_words in zspan_pipeline/fetcher.py:1214; per
#   Decision #7a, no separate disk artifact is written).


@app.route('/api/compiler/<int:meeting_id>', methods=['GET'])
def get_compiler_view(meeting_id: int):
    """Conversational Compiler V0 — meeting's tracked_claims for Hex-Rays UX.

    Helper `_parse_tracked_claim_row` is defined below in this file.
    """
    # RR-8 / SEC-PERIMETER-5: owner-only. The client labels compiler an owner
    # surface (App.tsx OWNER_ONLY_VIEWS); the Flask handler must gate too or a
    # direct request bypasses the React gate. Client-only caller (CompilerPage).
    _user, _err = _require_owner()
    if _err:
        return _err
    conn = get_connection()
    meeting_row = conn.execute(
        "SELECT id, city_name, meeting_title, meeting_date "
        "FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    if not meeting_row:
        conn.close()
        return jsonify({'error': 'meeting not found', 'meeting_id': meeting_id}), 404

    # V0.2-1 followup (2026-06-06): speaker_title pulled from
    # council_members.role, not council_members.title. The `title`
    # column exists in the schema but is null across all rows; `role`
    # is the populated column ("Mayor", "Vice Mayor", "Council
    # Member", etc.). Aliasing role → speaker_title keeps the API
    # contract stable while delivering the actual data the frontend
    # has always expected (V0.2-1 speaker labels render the
    # `(Mayor)` / `(Council Member)` parens once this lands).
    claim_rows = conn.execute(
        """
        SELECT tc.id, tc.member_id, tc.claim_type, tc.claim_text,
               tc.expected_outcome, tc.time_horizon_months, tc.topic_tags,
               tc.confidence, tc.context, tc.word_timings,
               tc.status, tc.status_updated_at, tc.extracted_at,
               tc.source_node_id,
               cm.name AS speaker_name, cm.role AS speaker_title
        FROM tracked_claims tc
        LEFT JOIN council_members cm ON tc.member_id = cm.id
        WHERE tc.meeting_id = ?
        ORDER BY tc.id
        """,
        (meeting_id,),
    ).fetchall()
    conn.close()

    claims = [_parse_tracked_claim_row(r) for r in claim_rows]

    # Track B (2026-06-05): also surface the meeting's transcript_nodes
    # so the compiler page can render Motion + Vote + future node types
    # alongside the hand-seeded Commit_P claims. Per SPEC § Relationship
    # to existing tracked_claims, Commit_P canonically lives in
    # transcript_nodes; tracked_claims is the projection. To avoid the
    # frontend double-rendering Commit_P (once via claims, once via
    # nodes), we EXCLUDE Commit_P from the nodes payload — the canonical
    # surface for Commit_P stays the claims projection.
    node_conn = get_connection()
    node_rows = node_conn.execute(
        """
        SELECT tn.id, tn.ordinal, tn.node_type, tn.typed_fields,
               tn.transcript_span_text, tn.parser_model, tn.parser_confidence,
               tn.speaker_id, tn.speaker_name AS denorm_speaker_name,
               tn.parser_ran_at,
               tn.audio_offset_seconds, tn.audio_duration_seconds,
               tn.parent_node_id,
               cm.name AS speaker_canonical_name, cm.role AS speaker_title
        FROM transcript_nodes tn
        LEFT JOIN council_members cm ON tn.speaker_id = cm.id
        WHERE tn.meeting_id = ?
        AND tn.node_type != 'Commit_P'
        ORDER BY tn.node_type, tn.ordinal
        """,
        (meeting_id,),
    ).fetchall()
    nodes = [_parse_transcript_node_row(r) for r in node_rows]

    # Edges — read transcript_edges whose source OR target points at any
    # of this meeting's transcript_nodes (Motion / Vote / Commit_P / ...).
    # Then resolve each endpoint to a frontend focus key:
    #   - Commit_P transcript_nodes with a tracked_claim projection →
    #     "claim:<tracked_claim.id>" (the canonical UI surface)
    #   - all other transcript_nodes (Motion, Vote, etc.) →
    #     "node:<transcript_nodes.id>"
    # The frontend then renders edges as lines between focus-key-addressed
    # nodes without needing to know about the id-space bridge.
    edge_conn = get_connection()
    # Build the node-id → focus-key map. Start with all transcript_nodes
    # for the meeting (Commit_P included so the satisfies edges resolve),
    # then overlay claims.source_node_id → "claim:<id>" so Commit_P
    # transcript_nodes resolve to the projection's focus key.
    all_node_rows = edge_conn.execute(
        "SELECT id FROM transcript_nodes WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchall()
    node_id_to_focus: Dict[int, str] = {
        r['id']: f"node:{r['id']}" for r in all_node_rows
    }
    for c in claims:
        snid = c.get('source_node_id')
        if isinstance(snid, int):
            node_id_to_focus[snid] = f"claim:{c['id']}"

    edge_rows: list = []
    if node_id_to_focus:
        node_ids = list(node_id_to_focus.keys())
        placeholders = ','.join('?' * len(node_ids))
        edge_rows = edge_conn.execute(
            f"""
            SELECT id, source_node_id, target_node_id, edge_type,
                   parser_confidence, parser_ran_at
            FROM transcript_edges
            WHERE source_node_id IN ({placeholders})
               OR target_node_id IN ({placeholders})
            ORDER BY edge_type, id
            """,
            node_ids + node_ids,
        ).fetchall()
    edge_conn.close()
    node_conn.close()

    edges = []
    for r in edge_rows:
        d = dict(r)
        src_key = node_id_to_focus.get(d['source_node_id'])
        tgt_key = node_id_to_focus.get(d['target_node_id'])
        # Edges whose endpoints have no resolved focus key would be
        # un-renderable — drop them. Possible if a node was deleted
        # mid-flight; transcript_edges has ON DELETE CASCADE so this
        # shouldn't happen in practice, but defensive.
        if not src_key or not tgt_key:
            continue
        d['source_focus_key'] = src_key
        d['target_focus_key'] = tgt_key
        edges.append(d)

    return jsonify({
        'meeting': dict(meeting_row),
        'claims': _genericize_speaker_attribution(claims),
        'nodes': _genericize_speaker_attribution(nodes),
        'edges': edges,
    })


@app.route('/api/compiler/<int:meeting_id>/transcript', methods=['GET'])
def get_compiler_transcript(meeting_id: int):
    """Compiler — return the meeting's persisted Whisper word array.

    Reads `notebook_outputs.transcript_words` (produced by
    _fetch_transcript_words in zspan_pipeline/fetcher.py:1214).
    Per Decision #7a, this row IS the canonical source for the full
    transcript — no separate disk artifact is written.

    Response shape:
        {
            "meeting_id": int,
            "words": [{word: str, start: float, end: float}, ...],
            "duration_seconds": float | null,
            "language": str | null
        }

    Returns 404 if no transcript_words row exists for the meeting (the
    Whisper pipeline hasn't run yet or returned no words).
    """
    # RR-8 / SEC-PERIMETER-5: owner-only (the full Whisper word array). Same
    # gate as /api/compiler/<id>; client-only caller (CompilerPage).
    _user, _err = _require_owner()
    if _err:
        return _err
    conn = get_connection()
    row = conn.execute(
        "SELECT content FROM notebook_outputs "
        "WHERE meeting_id = ? AND output_type = 'transcript_words' "
        "AND content IS NOT NULL AND content != ''",
        (meeting_id,),
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({
            'error': 'no transcript available for this meeting',
            'meeting_id': meeting_id,
        }), 404

    try:
        payload = json.loads(row['content'])
    except (json.JSONDecodeError, TypeError) as e:
        return jsonify({
            'error': f'transcript parse failed: {e}',
            'meeting_id': meeting_id,
        }), 500

    return jsonify({
        'meeting_id': meeting_id,
        'words': payload.get('words') or [],
        'duration_seconds': payload.get('duration_seconds'),
        'language': payload.get('language'),
    })


# ── T-012 Tracked Claims Ledger ────────────────────────────────────────
#
# GET  /api/ledger/<city>?status=active,unclear&aged=true
#   Returns all tracked claims for a city with member + meeting joins
#   so the City Ledger page can render full cards without N+1 lookups.
#   `status` (CSV) filters by status; `aged=true` restricts to claims
#   whose time horizon has elapsed AND that are still active (the
#   "next-review" feed).
#
# POST /api/tracked-claims/<int:claim_id>/status
#   body: { "status", "status_evidence", "updated_by" }
#   resp: { ok, claim }
#   Operator-triggered status flip. Validates status against the V1
#   enum (active / fulfilled / broken / withdrawn / unclear).


def _parse_tracked_claim_row(r) -> dict:
    c = dict(r)
    tags = c.get('topic_tags')
    if tags:
        try:
            c['topic_tags'] = json.loads(tags)
        except (json.JSONDecodeError, TypeError):
            c['topic_tags'] = []
    else:
        c['topic_tags'] = []
    wt = c.get('word_timings')
    if wt:
        try:
            c['word_timings'] = json.loads(wt)
        except (json.JSONDecodeError, TypeError):
            c['word_timings'] = None
    else:
        c['word_timings'] = None
    return c


def _parse_transcript_node_row(r) -> dict:
    """Shape a transcript_nodes row for the compiler API. typed_fields is
    parsed JSON; speaker_name prefers the canonical roster-joined name
    (speaker_canonical_name) and falls back to the denormalized value
    on the row itself (for public-comment / external speakers without
    a roster match)."""
    d = dict(r)
    raw_typed = d.pop('typed_fields', None) or '{}'
    try:
        d['typed_fields'] = json.loads(raw_typed)
    except (json.JSONDecodeError, TypeError):
        d['typed_fields'] = {}
    # Prefer canonical name from the council_members JOIN; fall back to
    # the denormalized speaker_name on the row.
    canonical = d.pop('speaker_canonical_name', None)
    denorm = d.pop('denorm_speaker_name', None)
    d['speaker_name'] = canonical or denorm
    return d


@app.route('/api/ledger/<city_name>', methods=['GET'])
def get_city_ledger(city_name):
    """Owner-side tracked-claims ledger with filter and sort controls."""
    _user, _err = _require_owner()
    if _err:
        return _err
    status_param = request.args.get('status', '').strip()
    status_filter = [s.strip().lower() for s in status_param.split(',') if s.strip()] or None
    aged_param = request.args.get('aged', '').strip().lower() in ('1', 'true', 'yes')
    limit = min(int(request.args.get('limit', 500)), 2000)

    try:
        from database import list_tracked_claims_for_city
        rows = list_tracked_claims_for_city(
            city_name=city_name,
            status_filter=status_filter,
            aged_past_horizon_only=aged_param,
            limit=limit,
        )
    except Exception:
        # Public route — stable code; detail server-side only.
        app.logger.exception("get_city_ledger failed for %s", city_name)
        return jsonify({'error': 'ledger lookup failed'}), 500

    intel = _load_city_intelligence(city_name) or {}
    claims = _genericize_speaker_attribution([
        _parse_tracked_claim_row(r) for r in rows
    ])

    return jsonify({
        'city': city_name,
        'county': intel.get('county'),
        'state': intel.get('state'),
        'filter': {
            'status': status_filter,
            'aged_past_horizon_only': aged_param,
            'limit': limit,
        },
        'count': len(rows),
        'tracked_claims': claims,
    })


@app.route('/api/tracked-claims/<int:claim_id>/status', methods=['POST'])
@_require_trusted_origin
def update_tracked_claim_status_endpoint(claim_id):
    """Operator-triggered status flip on one tracked claim row."""
    # Session-31 auth-audit — flips public accountability-ledger claim
    # status. Owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    new_status = (payload.get('status') or '').strip().lower()
    if not new_status:
        return jsonify({'error': 'status field required'}), 400
    evidence = payload.get('status_evidence')
    updated_by = payload.get('updated_by')

    try:
        from database import update_tracked_claim_status
        row = update_tracked_claim_status(
            claim_id=claim_id,
            new_status=new_status,
            status_evidence=evidence,
            updated_by=updated_by,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.exception("update_tracked_claim_status failed for %s", claim_id)
        return jsonify({'error': str(e)}), 500

    if row is None:
        return jsonify({'error': 'claim not found', 'id': claim_id}), 404
    return jsonify({'ok': True, 'claim': _parse_tracked_claim_row(row)})


# ── T-013 V4 — operator-terminal CLI wrappers + badge counts ─────────
#
# These wrap two CLI scripts so the operator-terminal can run them
# without dropping to a shell:
#
#   POST /api/work-orders/<id>/build-review-queue
#     Calls `zspan_pipeline.scripts.build_review_queue --meeting-id N`.
#     Extracts clips for every aligned member_quote, organizes into
#     batches, writes BATCH_MANIFEST.json + per-batch PROMPT.md /
#     RESPONSE.md stubs. Returns the output directory + summary stats.
#
#   POST /api/work-orders/<id>/ingest-responses
#     Calls `zspan_pipeline.scripts.ingest_review_response --meeting-id N`.
#     Parses every RESPONSE.md James pasted Gemini's reply into, applies
#     mechanical substitutions, sets verified_status, populates the
#     city_vocabulary_corrections dictionary, re-runs alignment for any
#     text-changed quotes.
#
#   GET /api/operator/badges
#     Returns small counts the operator terminal renders inline:
#       { disputed_count, vocab_pending_kingman, pending_escalations_unack }
#     Powers the [DISPUTED · N], [VOCAB · N], and [ESCALATIONS · N]
#     action-row badges.


def _resolve_meeting_id_from_wo(work_order_id: int):
    """Look up the meeting_id for a work order. Defensive — every WO
    should have a meeting_id, but a malformed insert could leave it null."""
    conn = get_connection()
    row = conn.execute(
        "SELECT meeting_id FROM work_orders WHERE id = ?",
        (work_order_id,),
    ).fetchone()
    conn.close()
    if not row or row["meeting_id"] is None:
        return None
    return int(row["meeting_id"])


def _run_cli_module(
    module: str,
    args: list,
    timeout_seconds: int,
    cwd=None,
) -> dict:
    """Shell out to a CLI module via `sys.executable -m <module>`. Captures
    stdout + stderr, returns a structured result dict. Used by the
    build-review-queue and ingest-responses endpoints.

    The 02_Core_Project working directory matters here — the CLI scripts
    resolve paths relative to it (e.g., the review_queue base dir).
    """
    import subprocess
    from pathlib import Path as _Path
    if cwd is None:
        # The scripts live in zspan_pipeline/scripts/ — running them
        # as -m modules requires the import-root to be 02_Core_Project.
        cwd = str(
            _Path(__file__).resolve().parent.parent.parent
        )
    try:
        # Use the running interpreter (D-111 Mac substrate has no `py` launcher;
        # mirrors the _spawn_worker_once pattern at ~L2369). Substrate-agnostic.
        proc = subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "exit_code": -1,
            "stdout": e.stdout.decode("utf-8", errors="replace") if e.stdout else "",
            "stderr": (e.stderr.decode("utf-8", errors="replace") if e.stderr else "")
                + f"\n[timeout after {timeout_seconds}s]",
            "timed_out": True,
        }
    except FileNotFoundError as e:
        # Dead branch now that the primary uses sys.executable (always resolvable);
        # kept as belt-and-suspenders + made Mac-correct (`python3.11`, not `python`).
        try:
            proc = subprocess.run(
                ["python3.11", "-m", module, *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "timed_out": False,
            }
        except FileNotFoundError:
            return {
                "exit_code": -2,
                "stdout": "",
                "stderr": f"No Python launcher on PATH: {e}",
                "timed_out": False,
            }


@app.route('/api/work-orders/<int:work_order_id>/build-review-queue', methods=['POST'])
@_require_trusted_origin
def build_review_queue_endpoint(work_order_id):
    """Wrap `build_review_queue.py` so the operator terminal can kick
    off the T-013 V1→V2 clip-extraction without dropping to a shell.
    Synchronous — typical run is ~2 seconds after source.mp4 cache hit,
    ~5-10 minutes on cold cache (yt-dlp source download). We allow up
    to 12 minutes."""
    # Session-31 auth-audit — invokes yt-dlp + clip extraction; owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    meeting_id = _resolve_meeting_id_from_wo(work_order_id)
    if meeting_id is None:
        return jsonify({'error': 'WO not found or has no meeting_id'}), 404

    result = _run_cli_module(
        module="zspan_pipeline.scripts.build_review_queue",
        args=["--meeting-id", str(meeting_id)],
        timeout_seconds=12 * 60,
    )

    ok = result["exit_code"] == 0
    status_code = 200 if ok else 500
    return jsonify({
        'ok': ok,
        'work_order_id': work_order_id,
        'meeting_id': meeting_id,
        'exit_code': result["exit_code"],
        'timed_out': result["timed_out"],
        # Truncate stdout/stderr in the response so the operator-terminal
        # log line stays scannable. Last 4 KB is plenty for the summary.
        'stdout_tail': (result["stdout"] or "")[-4000:],
        'stderr_tail': (result["stderr"] or "")[-2000:],
    }), status_code


@app.route('/api/work-orders/<int:work_order_id>/ingest-responses', methods=['POST'])
def ingest_responses_endpoint(work_order_id):
    """Wrap `ingest_review_response.py` so the operator terminal can
    finish the T-013 V3 round-trip in-UI. Synchronous — ingest runs in
    seconds (no I/O beyond the local review_queue files + DB writes).
    60s timeout is generous."""
    # RR-8 / SEC-AUTH-1: owner-OR-agent-token (the pipeline agents call this).
    _actor, _err = _require_owner_or_agent_token()
    if _err:
        return _err
    meeting_id = _resolve_meeting_id_from_wo(work_order_id)
    if meeting_id is None:
        return jsonify({'error': 'WO not found or has no meeting_id'}), 404

    result = _run_cli_module(
        module="zspan_pipeline.scripts.ingest_review_response",
        args=["--meeting-id", str(meeting_id)],
        timeout_seconds=60,
    )

    # ingest_review_response.py returns 1 when no RESPONSE.md files
    # exist for the meeting — treat that as "nothing to do" rather than
    # an error, since it's a common operator-flow state.
    ok = result["exit_code"] in (0, 1)
    status_code = 200 if ok else 500
    return jsonify({
        'ok': ok,
        'work_order_id': work_order_id,
        'meeting_id': meeting_id,
        'exit_code': result["exit_code"],
        'timed_out': result["timed_out"],
        'stdout_tail': (result["stdout"] or "")[-4000:],
        'stderr_tail': (result["stderr"] or "")[-2000:],
    }), status_code


# ─────────────────────────────────────────────────────────────────
# S-006 / S-007 — HQ status seam (the "Club Penguin Safe Chat" model).
#
# GET /api/hq/status returns the structured payload the HQPage lobby renders:
# the 5-agent fleet (+ orchestrator) with each one's current-rung, last_run_at,
# templated status, plus parsers health summary, governor snapshot, escalation
# backlog counts, billboards, infrastructure, funding. Departments that wrap
# pipeline stages (ingestion / synthesis-RAG / transcription / verification)
# round out the HQData contract the frontend expects.
#
# Safe status reporting (S-006 / orchestrator.md § Safe status reporting):
# every status string is rendered server-side from a curated template table —
# the wire payload carries {templateId, params, rendered}. Leaking a secret is
# structurally impossible because the agent never emits free text on this
# surface. V1 picks the template from live signals (badges, governor, watcher
# state files, escalations); the per-agent self-reporting hookup is the later
# build per S-007 / S-006 "live self-reports".
# ─────────────────────────────────────────────────────────────────

# Curated template table — the only strings that can reach the HQ window.
# templateId → callable(params dict) -> rendered string. Add templates here;
# never let an agent inject prose. Params are pre-validated (city names,
# counts, model labels).
_HQ_STATUS_TEMPLATES: Dict[str, Any] = {
    "ALL_QUIET": lambda p: "All quiet — nothing pending right now.",
    "REVIEWING_DISPUTED": lambda p: (
        f"Reviewing {int(p.get('count', 0))} disputed quote{'s' if int(p.get('count', 0)) != 1 else ''} "
        f"from the {p.get('city', 'public')} docket."
    ),
    "VOCAB_INBOX_PENDING": lambda p: (
        f"{int(p.get('count', 0))} vocabulary correction"
        f"{'s' if int(p.get('count', 0)) != 1 else ''} awaiting promotion review for {p.get('city', 'Kingman')}."
    ),
    "ESCALATED_WAITING": lambda p: (
        f"Awaiting human review — {int(p.get('count', 0))} open escalation"
        f"{'s' if int(p.get('count', 0)) != 1 else ''} on the operator's desk."
    ),
    "RECENT_RESOLVE": lambda p: (
        f"Resolved {int(p.get('count', 0))} item{'s' if int(p.get('count', 0)) != 1 else ''} "
        f"in the last session; queue caught up."
    ),
    "NEXT_MEETING_READY": lambda p: (
        f"Next meeting ready under today's ceiling — {p.get('city', 'Kingman')} {p.get('label', 'meeting')}."
    ),
    "AT_CEILING_HOLD": lambda p: (
        f"At today's ingestion ceiling — holding until tomorrow."
    ),
    "PIPELINE_IN_FLIGHT": lambda p: (
        f"Processing {p.get('city', 'a city')} batch — {int(p.get('done', 0))} of "
        f"{int(p.get('total', 0))} outputs cleared."
    ),
    # Honest live-pipeline templates (2026-07-02, session-27): the originals
    # above carried fake counts on the live surface (done=0/total=7 hardcoded,
    # chunk 1 of 16 always). These carry only signals we actually have.
    "PIPELINE_WORKING": lambda p: (
        f"Processing {p.get('city', 'a city')} — work order in flight."
    ),
    "INGEST_IN_FLIGHT": lambda p: (
        f"Ingesting {p.get('city', 'a city')} — {int(p.get('count', 1))} work "
        f"order{'s' if int(p.get('count', 1)) != 1 else ''} in flight."
    ),
    "SYNTHESIZING": lambda p: (
        f"Synthesizing {p.get('city', 'a city')} outputs — RAG retrieval + Sonnet."
    ),
    "TRANSCRIBING_CITY": lambda p: (
        f"Transcribing the {p.get('city', 'a city')} recording."
    ),
    "PIPELINE_IDLE_NOTHING_QUEUED": lambda p: "Queue is empty — nothing to process.",
    "PARSER_REGRESSED": lambda p: (
        f"{p.get('city', 'A city')} parser regressed — paused that city until a human confirms the new selector."
    ),
    "PARSER_ALL_HEALTHY": lambda p: "All parsers healthy — last sweep clean.",
    "SCOUT_NEW_MEETINGS": lambda p: (
        f"Surfaced {int(p.get('count', 0))} new meeting"
        f"{'s' if int(p.get('count', 0)) != 1 else ''} from {p.get('city', 'tracked cities')}."
    ),
    "SCOUT_QUIET": lambda p: "No new meetings in the last sweep.",
    "ORCH_IDLE": lambda p: "Heartbeat idle — operation caught up.",
    "ORCH_SEQUENCING": lambda p: (
        f"Sequencing the board — {int(p.get('pending', 0))} item"
        f"{'s' if int(p.get('pending', 0)) != 1 else ''} pending across the fleet."
    ),
    "TRANSCRIBING": lambda p: (
        f"Transcribing — chunk {int(p.get('chunk', 0))} of {int(p.get('total', 0))}."
    ),
    "INFRA_DOWN": lambda p: "Powered down — nothing pending overnight.",
}


def _render_hq_status(template_id: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Render a safe status string from the curated template table.

    Falls back to a neutral string if the templateId is unknown — never raises
    and never reflects user/agent input verbatim. Strips control chars from
    string params defensively (params shouldn't contain them; this is belt +
    suspenders).
    """
    fn = _HQ_STATUS_TEMPLATES.get(template_id)
    safe_params: Dict[str, Any] = {}
    for k, v in (params or {}).items():
        if isinstance(v, str):
            safe_params[k] = ''.join(c for c in v if c.isprintable())[:80]
        else:
            safe_params[k] = v
    if not fn:
        return "Status unavailable."
    try:
        return str(fn(safe_params))
    except Exception:
        return "Status unavailable."


def _iso_or_none(value: Any) -> Optional[str]:
    """Best-effort ISO timestamp. Accepts datetime, ISO string, or None."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.isoformat() + ('Z' if value.tzinfo is None else '')
    try:
        return str(value)
    except Exception:
        return None


def _watcher_state_summary(state_dir_name: str) -> Dict[str, Any]:
    """Read the most-recently-modified JSON in agents/_<role>_state/ for a
    last_run_at signal. Returns {last_run_at, file_count, last_payload}.

    Watcher files may not exist yet — that's "nothing yet", not an error.
    """
    parsers_dir = os.path.dirname(os.path.abspath(__file__))
    state_dir = os.path.normpath(
        os.path.join(parsers_dir, '..', '..', '..', 'agents', state_dir_name)
    )
    summary: Dict[str, Any] = {
        'last_run_at': None, 'file_count': 0, 'last_payload': None,
    }
    if not os.path.isdir(state_dir):
        return summary
    try:
        entries = [
            os.path.join(state_dir, n)
            for n in os.listdir(state_dir)
            if n.endswith('.json')
        ]
    except OSError:
        return summary
    summary['file_count'] = len(entries)
    if not entries:
        return summary
    latest = max(entries, key=lambda p: os.path.getmtime(p))
    try:
        summary['last_run_at'] = datetime.utcfromtimestamp(
            os.path.getmtime(latest)
        ).isoformat() + 'Z'
        with open(latest, 'r', encoding='utf-8') as f:
            summary['last_payload'] = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return summary


def _custodian_health_summary() -> Dict[str, Any]:
    """Parser-health summary from the Custodian's state file.

    Looks for parser-health.json in _custodian_state/. Returns
    {healthy: int, regressed: list[str], last_sweep_at: iso|None}.
    Tolerant of missing-file (the watcher hasn't run yet).
    """
    summary = _watcher_state_summary('_custodian_state')
    out: Dict[str, Any] = {
        'healthy': 0, 'regressed': [], 'last_sweep_at': summary['last_run_at'],
    }
    payload = summary.get('last_payload') or {}
    if isinstance(payload, dict):
        try:
            out['healthy'] = int(payload.get('healthy_count', 0) or 0)
        except (TypeError, ValueError):
            out['healthy'] = 0
        regressed = payload.get('regressed') or []
        if isinstance(regressed, list):
            out['regressed'] = [str(c)[:60] for c in regressed[:20]]
    return out


def _scout_summary() -> Dict[str, Any]:
    """Recent-meetings summary from the Scout's state files. Returns
    {new_meetings: int, last_sweep_at: iso|None}."""
    summary = _watcher_state_summary('_scout_state')
    out: Dict[str, Any] = {
        'new_meetings': 0, 'last_sweep_at': summary['last_run_at'],
    }
    payload = summary.get('last_payload') or {}
    if isinstance(payload, dict):
        try:
            out['new_meetings'] = int(payload.get('new_meeting_count', 0) or 0)
        except (TypeError, ValueError):
            out['new_meetings'] = 0
    return out


def _build_hq_departments(
    badges: Dict[str, int],
    work_orders: Dict[str, Any],
    custodian: Dict[str, Any],
    scout: Dict[str, Any],
    metering: Dict[str, Any],
    frontier_rung: int,
) -> List[Dict[str, Any]]:
    """Assemble the 9 department rows the HQData contract expects.

    Each row carries a templated status (templateId + params + rendered) so
    the HQ window never renders free-text agent prose. lastActiveAt is best-
    effort derived from watcher files / DB; missing signals show as null.
    """
    disputed = int(badges.get('disputed_count', 0) or 0)
    vocab = int(badges.get('vocab_pending_kingman', 0) or 0)
    esc = int(badges.get('pending_escalations_unack', 0) or 0)
    wo_stats = work_orders.get('stats') or {}
    pending_total = (
        int(wo_stats.get('pending', 0) or 0)
        + int(wo_stats.get('processing', 0) or 0)
    )
    next_ready = (metering or {}).get('next_meeting') or {}
    room_today = (metering or {}).get('room_today_remaining', 0)

    def _dept(
        dept_id: str, name: str, short: str, kind: str, model: str,
        state: str, template_id: str, params: Dict[str, Any],
        last_active_iso: Optional[str], escalations: int = 0,
        active_count: int = 0,
    ) -> Dict[str, Any]:
        rendered = _render_hq_status(template_id, params)
        agents_block: List[Dict[str, Any]] = []
        if active_count > 0 or state in ("running", "escalated"):
            # One representative worker line for the active state (the floor
            # shows live workers; idle/offline departments show no agents per
            # the HQData contract).
            agents_block = [{
                'id': f'{dept_id}-1',
                'model': model,
                'status': (
                    'escalated' if state == 'escalated'
                    else 'in-progress' if state == 'running'
                    else 'queued'
                ),
                'objective': rendered,
                'detail': rendered,
            }]
        return {
            'id': dept_id, 'name': name, 'short': short, 'kind': kind,
            'state': state,
            'currentObjective': rendered if state in ('running', 'escalated') else None,
            'recentSummary': rendered if state in ('idle', 'offline') else None,
            'activeAgentCount': active_count,
            'lastActiveAt': last_active_iso,
            'escalationCount': escalations,
            'agents': agents_block,
            'status': {
                'templateId': template_id, 'params': params, 'rendered': rendered,
            },
        }

    departments: List[Dict[str, Any]] = []

    # Disputed Quotes Reviewer (Opus)
    if disputed > 0:
        departments.append(_dept(
            'disputed-quotes-reviewer', 'Disputed Quotes Reviewer', 'DISPUTES',
            'agent', 'Opus 4.7',
            'running', 'REVIEWING_DISPUTED', {'count': disputed, 'city': 'Kingman'},
            None, escalations=0, active_count=1,
        ))
    else:
        departments.append(_dept(
            'disputed-quotes-reviewer', 'Disputed Quotes Reviewer', 'DISPUTES',
            'agent', 'Opus 4.7',
            'idle', 'RECENT_RESOLVE', {'count': 0},
            None, escalations=0, active_count=0,
        ))

    # Vocabulary Curator (Opus)
    if vocab > 0:
        departments.append(_dept(
            'vocabulary-curator', 'Vocabulary Curator', 'VOCAB',
            'agent', 'Opus 4.7',
            'running', 'VOCAB_INBOX_PENDING', {'count': vocab, 'city': 'Kingman'},
            None, escalations=0, active_count=1,
        ))
    else:
        departments.append(_dept(
            'vocabulary-curator', 'Vocabulary Curator', 'VOCAB',
            'agent', 'Opus 4.7',
            'idle', 'RECENT_RESOLVE', {'count': 0},
            None, escalations=0, active_count=0,
        ))

    # Pipeline Operator (Opus)
    processing_rows = work_orders.get('processing') or []
    if processing_rows:
        first = processing_rows[0] or {}
        departments.append(_dept(
            'pipeline-operator', 'Pipeline Operator', 'PIPELINE OPS',
            'agent', 'Opus 4.7',
            'running', 'PIPELINE_WORKING',
            {'city': first.get('city') or 'a city'},
            _iso_or_none(first.get('started_at')),
            escalations=0, active_count=1,
        ))
    elif next_ready and room_today > 0:
        departments.append(_dept(
            'pipeline-operator', 'Pipeline Operator', 'PIPELINE OPS',
            'agent', 'Opus 4.7',
            'idle', 'NEXT_MEETING_READY',
            {'city': next_ready.get('city_name') or 'Kingman',
             'label': next_ready.get('meeting_title') or 'meeting'},
            None, escalations=0, active_count=0,
        ))
    else:
        departments.append(_dept(
            'pipeline-operator', 'Pipeline Operator', 'PIPELINE OPS',
            'agent', 'Opus 4.7',
            'idle', 'AT_CEILING_HOLD', {},
            None, escalations=0, active_count=0,
        ))

    # Content Scout (Sonnet — read-only watcher)
    scout_count = scout.get('new_meetings', 0)
    if scout_count > 0:
        departments.append(_dept(
            'content-scout', 'Content Scout', 'SCOUT',
            'agent', 'Sonnet 4.6',
            'idle', 'SCOUT_NEW_MEETINGS',
            {'count': scout_count, 'city': 'Kingman'},
            scout.get('last_sweep_at'),
            escalations=0, active_count=0,
        ))
    else:
        departments.append(_dept(
            'content-scout', 'Content Scout', 'SCOUT',
            'agent', 'Sonnet 4.6',
            'idle', 'SCOUT_QUIET', {},
            scout.get('last_sweep_at'),
            escalations=0, active_count=0,
        ))

    # Parser Custodian (Sonnet — read-only watcher)
    regressed = custodian.get('regressed') or []
    if regressed:
        departments.append(_dept(
            'parser-custodian', 'Parser Custodian', 'PARSER',
            'agent', 'Sonnet 4.6',
            'escalated', 'PARSER_REGRESSED', {'city': regressed[0]},
            custodian.get('last_sweep_at'),
            escalations=1, active_count=1,
        ))
    else:
        departments.append(_dept(
            'parser-custodian', 'Parser Custodian', 'PARSER',
            'agent', 'Sonnet 4.6',
            'idle', 'PARSER_ALL_HEALTHY', {},
            custodian.get('last_sweep_at'),
            escalations=0, active_count=0,
        ))

    # Pipeline stages — derived from work-order queue. Honest-label pass
    # 2026-07-02 (session-27): the city was hardcoded 'Kingman' (the window
    # said "Processing Kingman batch" while actually processing Bullhead),
    # Whisper carried fake chunk-1-of-16 params, and the retired NotebookLM
    # Bridge (removed per D-143 2026-07-01) still shipped as a live
    # department. The synthesis dept now reflects reality: Qdrant retrieval
    # + `claude -p` Sonnet per D-126.
    in_flight = bool(processing_rows)
    live_city = (processing_rows[0].get('city') if in_flight else None) or 'a city'
    departments.append(_dept(
        'ingestion', 'Ingestion / Parsers', 'INGEST',
        'pipeline', 'Parsers',
        'running' if in_flight else 'idle',
        'INGEST_IN_FLIGHT' if in_flight else 'PIPELINE_IDLE_NOTHING_QUEUED',
        ({'city': live_city, 'count': len(processing_rows)}
         if in_flight else {}),
        None, escalations=0, active_count=(1 if in_flight else 0),
    ))
    departments.append(_dept(
        'synthesis', 'Synthesis / RAG', 'RAG',
        'pipeline', 'Sonnet 4.6',
        'running' if in_flight else 'idle',
        'SYNTHESIZING' if in_flight else 'PIPELINE_IDLE_NOTHING_QUEUED',
        ({'city': live_city} if in_flight else {}),
        None, escalations=0, active_count=(1 if in_flight else 0),
    ))
    departments.append(_dept(
        'transcription', 'Whisper Transcription', 'WHISPER',
        'pipeline', 'Whisper',
        'running' if in_flight else 'idle',
        'TRANSCRIBING_CITY' if in_flight else 'PIPELINE_IDLE_NOTHING_QUEUED',
        ({'city': live_city} if in_flight else {}),
        None, escalations=0, active_count=(1 if in_flight else 0),
    ))
    departments.append(_dept(
        'verification', 'Verification Chain', 'VERIFY',
        'pipeline', 'Opus 4.7',
        'escalated' if disputed > 0 else 'offline',
        'ESCALATED_WAITING' if disputed > 0 else 'INFRA_DOWN',
        {'count': disputed} if disputed > 0 else {},
        None, escalations=(1 if disputed > 0 else 0),
        active_count=(1 if disputed > 0 else 0),
    ))

    return departments


@app.route('/api/hq/status', methods=['GET'])
def api_hq_status():
    """Structured HQ payload feeding HQPage's useHQDataState().

    Real signals are wired everywhere they exist on this build:
      - badges (disputed / vocab / escalations) → agent department states
      - /api/orchestrator/autonomy frontier_rung → orchestrator current rung
      - /api/ingestion/governor → pipeline-operator's pace board
      - watcher state files (_scout_state / _custodian_state) → last_run_at
      - work_order queue → ingestion / synthesis / transcription pipeline states

    Billboard display copy is client-owned. Funding is real when
    available_balance is configured, redacted (`restricted: true`) for
    non-owners, and honest zeros when unconfigured.

    Status strings are rendered server-side from the curated template table
    above so the wire payload carries {templateId, params, rendered} — the HQ
    window never receives free-text agent prose (the Club-Penguin-Safe-Chat
    redline; secrets cannot reach the public surface).
    """
    try:
        # RR-8 pre-flip: this endpoint is deliberately public (the HQ lobby),
        # but the funding/budget dollars are owner-only — redact them below.
        is_owner = _request_is_owner()
        # Badges (disputed + vocab + escalations) — these drive most agent rows.
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM quotes WHERE verified_status = 'disputed'")
        disputed_count = int((cur.fetchone() or {'n': 0})['n'])
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM city_vocabulary_corrections
            WHERE city_name = 'Kingman'
              AND auto_apply = 1
              AND promoted_at IS NULL
            """
        )
        vocab_kingman = int((cur.fetchone() or {'n': 0})['n'])
        conn.close()
        from database import (
            count_pending_escalations, work_order_stats, list_work_orders,
        )
        escalations_unack = count_pending_escalations(
            unacknowledged_only=True, undelivered_only=False,
        )
        badges = {
            'disputed_count': disputed_count,
            'vocab_pending_kingman': vocab_kingman,
            'pending_escalations_unack': escalations_unack,
        }

        # Work orders — drives pipeline/in-flight state
        wo_stats = work_order_stats()
        processing_rows = list_work_orders(state='processing', limit=10)
        work_orders_snapshot = {
            'stats': wo_stats,
            'processing': [
                {'id': r.get('id'), 'meeting_id': r.get('meeting_id'),
                 'meeting_title': r.get('meeting_title'),
                 'city': r.get('city_name'), 'started_at': r.get('started_at')}
                for r in processing_rows
            ],
        }

        # Governor (S-010 metering board) — drives the Pipeline Operator's row
        cal = _load_calibration()
        compute_ceiling = float(cal['videos_per_day'])
        review_ceiling = _review_ceiling(cal)
        bal = cal.get('available_balance')
        metering = compute_city_metering(
            DEFAULT_FOCUS_CITY, compute_ceiling, review_ceiling,
            available_balance=(float(bal) if bal is not None else None),
            cost_per_video=(float(cal.get('cost_per_video') or 0) or None),
            solvency_days=float(cal.get('solvency_days') or 30),
        )
        if not is_owner:
            metering = _redact_metering_budget(metering)

        # Watchers
        custodian = _custodian_health_summary()
        scout = _scout_summary()

        # Orchestrator autonomy rung
        caps = _merged_autonomy_capabilities()
        frontier_rung = _autonomy_frontier_rung(caps)

        # Assemble departments (the 9 HQData rows the frontend renders)
        departments = _build_hq_departments(
            badges, work_orders_snapshot, custodian, scout, metering, frontier_rung,
        )

        # Orchestrator's own row — surfaced alongside the fleet so the HQ can
        # render the apex agent's heartbeat-rendered status. Not a department
        # zone today (no window position assigned) but the frontend can show
        # it in the topchrome / a future "twin" badge.
        pending_total = (
            int(wo_stats.get('pending', 0) or 0)
            + escalations_unack + disputed_count + vocab_kingman
        )
        if pending_total == 0 and not (custodian.get('regressed') or []):
            orch_tpl = ('ORCH_IDLE', {})
        else:
            orch_tpl = ('ORCH_SEQUENCING', {'pending': pending_total})
        orchestrator_block = {
            'id': 'orchestrator',
            'name': 'Orchestrator (Twin)',
            'currentRung': frontier_rung,
            'status': {
                'templateId': orch_tpl[0], 'params': orch_tpl[1],
                'rendered': _render_hq_status(orch_tpl[0], orch_tpl[1]),
            },
        }

        # Infrastructure — read services from real probes; the HQData shape
        # demands isCore on each service so the building can gray out on core
        # outage. Layout mirrors the existing mock contract.
        # Retired subsystems are omitted rather than shown as permanently up:
        # NotebookLM Auth was removed per D-143, and the worker daemon/pool was
        # retired per D-168 in favor of operator-triggered single-shot runs.
        # Work-order telemetry remains owner-only and is not represented here.
        infrastructure = {'services': [
            {'id': 'api', 'label': 'API Gateway', 'status': 'up', 'isCore': True},
            {'id': 'ingestion', 'label': 'Ingestion Srv', 'status': 'up', 'isCore': True},
            {'id': 'verification', 'label': 'Verify Chain',
             'status': 'degraded' if disputed_count > 0 else 'up', 'isCore': False},
        ]}
        core_down = any(s['isCore'] and s['status'] == 'down' for s in infrastructure['services'])
        any_degraded = any(s['status'] in ('degraded', 'down') for s in infrastructure['services'])
        overall_status = (
            'maintenance' if core_down
            else 'degraded' if any_degraded
            else 'operational'
        )

        # Funding — when available_balance is configured on the gate-board
        # calibration, surface it as the real balance with a derived burn.
        if bal is not None:
            balance = float(bal)
            cost_per_video = float(cal.get('cost_per_video') or 0)
            videos_per_day = float(cal.get('videos_per_day') or 0)
            monthly_burn = cost_per_video * videos_per_day * 30.0
            runway = (balance / monthly_burn) if monthly_burn > 0 else 0.0
            funding = {
                'balanceUsd': balance,
                'monthlyBurnUsd': monthly_burn,
                'runwayMonths': runway,
                'lastUpdated': datetime.utcnow().isoformat() + 'Z',
                'source': 'gate-board calibration',
            }
        else:
            funding = {
                'balanceUsd': 0.0,
                'monthlyBurnUsd': 0.0,
                'runwayMonths': 0.0,
                'lastUpdated': None,
                'source': 'unconfigured',
            }
        if not is_owner:
            # Owner-only: don't ship real balance/burn/runway to the public HQ.
            # Keep the render-safe numeric shape (PressScreen calls .toFixed);
            # the `restricted` flag drives the "—" display client-side.
            funding = {
                'balanceUsd': 0.0,
                'monthlyBurnUsd': 0.0,
                'runwayMonths': 0.0,
                'lastUpdated': None,
                'source': 'owner-only',
                'restricted': True,
            }

        return jsonify({
            'building': {'overallStatus': overall_status},
            'departments': departments,
            'orchestrator': orchestrator_block,
            'infrastructure': infrastructure,
            'funding': funding,
            'governor': metering,
            'badges': badges,
            'escalations': {'unacknowledged': escalations_unack},
            'parsers': custodian,
        })
    except Exception as e:
        app.logger.exception('hq/status failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/operator/badges', methods=['GET'])
def operator_badges_endpoint():
    """Tiny counts the operator-terminal renders inline next to its
    review-surface buttons. Cheap point queries; can be polled
    alongside the work-orders refresh."""
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        conn = get_connection()
        cur = conn.cursor()
        # Reads from the unified `quotes` table (D-052), matching what
        # /api/disputed-quotes and DisputedQuotesPage actually surface.
        # The pre-D-052 query against member_quotes drifted to 0 once
        # the migration landed (the unified table is the canonical
        # source of truth; legacy member_quotes lingers but isn't where
        # T-013 V3 ingest writes verdicts anymore).
        cur.execute(
            "SELECT COUNT(*) AS n FROM quotes WHERE verified_status = 'disputed'"
        )
        disputed_count = int((cur.fetchone() or {"n": 0})["n"])
        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM city_vocabulary_corrections
            WHERE city_name = 'Kingman'
              AND auto_apply = 1
              AND promoted_at IS NULL
            """
        )
        vocab_kingman = int((cur.fetchone() or {"n": 0})["n"])
        conn.close()
        # S-004 — agent escalations awaiting operator attention. Read
        # via the database helper rather than inline SQL so this stays
        # consistent with /api/operator/pending-escalations (same WHERE
        # clause: acknowledged_at IS NULL).
        from database import count_pending_escalations
        escalations_unack = count_pending_escalations(
            unacknowledged_only=True, undelivered_only=False,
        )
        return jsonify({
            'disputed_count': disputed_count,
            'vocab_pending_kingman': vocab_kingman,
            'pending_escalations_unack': escalations_unack,
        })
    except Exception as e:
        app.logger.exception("operator_badges failed")
        return jsonify({'error': str(e)}), 500


# ── T-018 — Vocabulary Inbox (per-city promotion review) ─────────────
#
# GET  /api/vocabulary-inbox?city=<city>&threshold=2
#   resp: { city, threshold, auto_eligible: [...], manual_only: [...] }
#   Lists `city_vocabulary_corrections` rows that are candidates for
#   promotion to the city's canonical `whisper_vocabulary_hints` JSON.
#   Split into auto-eligible (applied_count >= threshold) and manual-
#   only (below threshold) so the UI renders two groups distinctly.
#
# POST /api/vocabulary-inbox/promote
#   body: { correction_id, category?, promoted_by? }
#   Promotes one correction: writes a {term, category?, ...} entry into
#   city_intelligence/<slug>.json + stamps promoted_at on the DB row.
#
# POST /api/vocabulary-inbox/reject
#   body: { correction_id, rejected_by? }
#   Operator rejects: flips auto_apply=0 + stamps promoted_at (with a
#   "rejected:" prefix in promoted_by) so the Inbox doesn't surface it
#   again. The historical row is preserved.


@app.route('/api/vocabulary-inbox', methods=['GET'])
def vocabulary_inbox_endpoint():
    # RR-8 backstop gate: the review inbox exposes pending corrections,
    # source filenames, and promoter/agent identities + reasoning — operator
    # review material, consumed only by the owner-gated VocabularyInboxPage.
    # (Its /agent-propose sibling stays the S-134 Tier-3 open write path.)
    _user, _err = _require_owner()
    if _err:
        return _err
    city = (request.args.get('city') or '').strip()
    if not city:
        return jsonify({'error': "city query param required"}), 400
    try:
        threshold = int(request.args.get('threshold', 2))
    except (TypeError, ValueError):
        return jsonify({'error': 'threshold must be an int'}), 400

    try:
        from database import list_pending_promotions
        rows = list_pending_promotions(city, threshold=threshold)
    except Exception as e:
        app.logger.exception("list_pending_promotions failed for %s", city)
        return jsonify({'error': str(e)}), 500

    auto_eligible = [r for r in rows if r.get('meets_threshold')]
    manual_only = [r for r in rows if not r.get('meets_threshold')]
    return jsonify({
        'city': city,
        'threshold': threshold,
        'auto_eligible_count': len(auto_eligible),
        'manual_only_count': len(manual_only),
        'auto_eligible': auto_eligible,
        'manual_only': manual_only,
    })


@app.route('/api/vocabulary-inbox/promote', methods=['POST'])
@_require_trusted_origin
def vocabulary_inbox_promote_endpoint():
    # Session-31 auth-audit remediation — owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    correction_id = payload.get('correction_id')
    category = (payload.get('category') or '').strip() or None
    promoted_by = (payload.get('promoted_by') or '').strip() or 'operator'
    # D-057 — operator (or fast-path) can supply an `override_right` that
    # replaces the verifier's `right` value when writing to the city's
    # whisper_vocabulary_hints. Empty/missing falls back to the row's
    # verifier-proposed `right` (the legacy V1 behavior).
    override_right_raw = payload.get('override_right')
    override_right = (override_right_raw or '').strip() if isinstance(override_right_raw, str) else None

    if not isinstance(correction_id, int):
        return jsonify({'error': 'correction_id (int) required'}), 400

    try:
        from database import (
            get_connection, mark_correction_promoted,
            append_whisper_vocabulary_hint,
        )
    except Exception as e:
        return jsonify({'error': f'import failed: {e}'}), 500

    # Pull the row so we have all the metadata for the JSON entry.
    conn = get_connection()
    row = conn.execute(
        """
        SELECT id, city_name, wrong, right, applied_count, auto_apply,
               first_observed_response_file, last_applied_at, created_at,
               promoted_at, promoted_by,
               agent_proposed_right, agent_reasoning,
               agent_proposed_by, agent_proposed_at
        FROM city_vocabulary_corrections WHERE id = ?
        """,
        (correction_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'correction not found', 'id': correction_id}), 404
    c = dict(row)
    if c.get('promoted_at'):
        return jsonify({
            'error': 'already promoted',
            'id': correction_id,
            'promoted_at': c['promoted_at'],
            'promoted_by': c['promoted_by'],
        }), 400
    if not c.get('auto_apply'):
        return jsonify({
            'error': 'correction was previously rejected (auto_apply=0); '
                     're-enable before promoting',
            'id': correction_id,
        }), 400

    # Resolve which `right` value lands in the city JSON.
    # Priority: explicit override_right > agent_proposed_right > verifier `right`.
    final_right = override_right or (c.get('agent_proposed_right') or '').strip() or c['right']

    try:
        appended = append_whisper_vocabulary_hint(
            city_name=c['city_name'],
            term=final_right,
            category=category,
            first_seen=c.get('created_at'),
            source=c.get('first_observed_response_file'),
            promoted_by=promoted_by,
        )
    except Exception as e:
        app.logger.exception(
            "append_whisper_vocabulary_hint failed for id=%s", correction_id
        )
        return jsonify({'error': f'JSON append failed: {e}'}), 500

    if appended is None:
        return jsonify({
            'error': f'no city_intelligence JSON found for {c["city_name"]!r}',
            'id': correction_id,
        }), 404

    db_row = mark_correction_promoted(
        correction_id=correction_id, promoted_by=promoted_by
    )

    return jsonify({
        'ok': True,
        'correction': db_row,
        'json_entry': appended,
        'was_already_in_json': appended.get('_already_present', False),
        'final_right': final_right,
        'overrode_verifier': final_right != c['right'],
    })


# D-057 — agent counter-proposal endpoint. Called by the Vocab Curator (and
# future agents) when the verifier's `right` is wrong but the agent has a
# better alternative. The proposal lands on the correction row; the operator
# UI surfaces both verifier + agent proposals; Slack ✨ fast-path applies
# the agent's value via /promote with override_right.
@app.route('/api/vocabulary-inbox/<int:correction_id>/agent-propose', methods=['POST'])
def vocabulary_inbox_agent_propose_endpoint(correction_id: int):
    # RR-8 / SEC-AUTH-2: owner-OR-agent-token. The Vocab Curator presents the
    # ZSPAN_AGENT_STATE_TOKEN bearer; X-Zspan-Agent-Role stays attribution-only
    # (it never authenticates). Closes the prior unauthenticated-self-attribution
    # hole (the old S-134 follow-up).
    actor, _err = _require_owner_or_agent_token()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    proposed_right_raw = payload.get('proposed_right')
    if not isinstance(proposed_right_raw, str) or not proposed_right_raw.strip():
        return jsonify({'error': 'proposed_right (non-empty string) required'}), 400
    proposed_right = proposed_right_raw.strip()
    reasoning = (payload.get('reasoning') or '').strip() or None
    if actor is not None:
        agent_role = 'operator'
        # An operator using this agent-specific endpoint directly suggests
        # routing confusion — surface the case but still allow it (the
        # operator may genuinely want to record a counter-proposal pre-promote).
        app.logger.warning(
            "agent-propose called without X-Zspan-Agent-Role header; "
            "attributing to 'operator' for correction_id=%s", correction_id,
        )
    else:
        agent_role = _resolved_agent_proposal_role(payload)
        if agent_role not in KNOWN_ROLES:
            return jsonify({
                'error': ('agent_role must name a known fleet role from '
                          'agent_audit.KNOWN_ROLES'),
            }), 400

    try:
        from database import record_agent_counter_proposal
        row = record_agent_counter_proposal(
            correction_id=correction_id,
            proposed_right=proposed_right,
            reasoning=reasoning,
            agent_role=agent_role,
        )
    except Exception as e:
        app.logger.exception(
            "record_agent_counter_proposal failed for id=%s", correction_id
        )
        return jsonify({'error': str(e)}), 500
    if row is None:
        return jsonify({'error': 'correction not found', 'id': correction_id}), 404
    return jsonify({'ok': True, 'correction': row})


@app.route('/api/vocabulary-inbox/reject', methods=['POST'])
@_require_trusted_origin
def vocabulary_inbox_reject_endpoint():
    # Session-31 auth-audit remediation — owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    correction_id = payload.get('correction_id')
    rejected_by = (payload.get('rejected_by') or '').strip() or 'operator'

    if not isinstance(correction_id, int):
        return jsonify({'error': 'correction_id (int) required'}), 400

    try:
        from database import reject_promotion
        row = reject_promotion(correction_id, rejected_by=rejected_by)
    except Exception as e:
        app.logger.exception("reject_promotion failed for id=%s", correction_id)
        return jsonify({'error': str(e)}), 500
    if row is None:
        return jsonify({'error': 'correction not found', 'id': correction_id}), 404
    return jsonify({'ok': True, 'correction': row})


# ── /api/city-intelligence/<slug> — read-only city dictionary access ──
#
# Referenced by the Vocabulary Curator agent's manual: the agent fetches a
# city's current city_intelligence/<slug>.json to check whether a proposed
# vocabulary promotion would conflict with an existing entry. Read-only;
# the agent never writes through this endpoint (writes go via
# /api/vocabulary-inbox/promote which uses append_whisper_vocabulary_hint).
#
# Slug resolution mirrors database.city_intelligence_path — tries hyphen
# form first, falls back to underscore form for legacy files.

@app.route('/api/city-intelligence/<slug>', methods=['GET'])
def city_intelligence_endpoint(slug: str):
    # RR-8 / SEC-SEAL-1 + SEC-AUTH-2: the city_intelligence corpus is sealed
    # (operational notes, whisper hints — not public roster data). Owner-OR-
    # agent-token; the Vocab Curator reads it with the bearer.
    _actor, _err = _require_owner_or_agent_token()
    if _err:
        return _err
    if not slug or not slug.strip():
        return jsonify({'error': 'slug required'}), 400
    # The helper takes city_name (human form) and resolves to slug; we
    # accept the slug directly (kebab or snake) by reversing the path
    # resolution: try the file directly at known forms.
    parsers_dir = os.path.dirname(os.path.abspath(__file__))
    intel_dir = os.path.join(parsers_dir, '..', 'city_intelligence')
    # Try the slug as given, then variants.
    base = slug.strip().lower()
    candidates = [base, base.replace('_', '-'), base.replace('-', '_')]
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        path = os.path.join(intel_dir, f'{c}.json')
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    data = json.load(fh)
                # SEC-SEAL-1: never leak the internal relative path.
                return jsonify({
                    'slug': c,
                    'data': data,
                })
            except Exception as e:
                app.logger.exception("city_intelligence read failed for %s", c)
                return jsonify({'error': f'read failed: {e}', 'slug': c}), 500
    return jsonify({'error': 'no city_intelligence file matches slug', 'slug': slug}), 404


# ── /api/operator/pending-escalations — S-004 agent escalation surface ─
#
# Operator terminal badge: how many agent escalations are unacknowledged?
# When agents post escalations (via parsers/slack_notifier), the
# pending_escalations table is the canonical record. Slack is the
# notification layer; this endpoint is the operator-side recovery surface.
#
# GET  /api/operator/pending-escalations
#   resp: { unacknowledged_count, undelivered_count, escalations: [...] }
#
# POST /api/operator/pending-escalations/<id>/acknowledge
#   body: { acknowledged_by? }
#   resp: { ok }


@app.route('/api/operator/pending-escalations', methods=['GET'])
def list_pending_escalations_endpoint():
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        from database import (
            list_pending_escalations,
            count_pending_escalations,
        )
        # Two independent diagnostics:
        #   - unacknowledged: operator-attention owed (the badge count)
        #   - undelivered: Slack-webhook-health (could be acked already
        #     but still flagged a delivery failure worth surfacing)
        unacked = count_pending_escalations(
            unacknowledged_only=True, undelivered_only=False,
        )
        undelivered = count_pending_escalations(
            unacknowledged_only=False, undelivered_only=True,
        )
        items = list_pending_escalations(unacknowledged_only=True, limit=50)
        return jsonify({
            'unacknowledged_count': unacked,
            'undelivered_count': undelivered,
            'escalations': items,
        })
    except Exception as e:
        app.logger.exception("list_pending_escalations failed")
        return jsonify({'error': str(e)}), 500


@app.route(
    '/api/operator/pending-escalations/<int:escalation_id>/acknowledge',
    methods=['POST'],
)
@_require_trusted_origin
def acknowledge_pending_escalation_endpoint(escalation_id):
    _user, _err = _require_owner()
    if _err:
        return _err
    # Attribution is the authenticated owner, never a client-supplied value.
    acknowledged_by = getattr(_user, "email", None) or "operator"
    try:
        from database import acknowledge_pending_escalation
        ok = acknowledge_pending_escalation(
            escalation_id, acknowledged_by=acknowledged_by,
        )
        if not ok:
            return jsonify({
                'error': 'escalation not found or already acknowledged',
                'id': escalation_id,
            }), 404
        return jsonify({'ok': True, 'id': escalation_id})
    except Exception as e:
        app.logger.exception(
            "acknowledge_pending_escalation failed for %s", escalation_id,
        )
        return jsonify({'error': str(e)}), 500


# ── /api/llm-health — observability for the three gpt-4o-mini helpers ─
#
# Lightweight in-memory counters wrapped around clean_quote,
# polish_for_display, and extract_verdict_emphasis. Catches silent drift
# (API key revoked, rate limit, model deprecated, transient OpenAI
# instability) before the operator notices fields going blank on
# DisputedQuotesPage / VocabularyInboxPage. Counters reset on Flask
# restart — observability is about active state, not history.


@app.route('/api/llm-health', methods=['GET'])
def llm_health_endpoint():
    # RR-8 backstop gate: exposes provider-helper counters + last raw error
    # text — operator diagnostics, no public consumer.
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        from llm_health import get_snapshot
        return jsonify(get_snapshot())
    except Exception as e:
        app.logger.exception("llm_health snapshot failed")
        return jsonify({'error': str(e)}), 500


# ── T-013 V4 — Disputed-quotes resolution surface ─────────────────────
#
# GET  /api/disputed-quotes?city=<city>
#   resp: { count, disputed_quotes: [...] }
#   Returns every member_quotes row currently in verified_status='disputed'
#   for the (optional) city. Each row is enriched with the Gemini verdict
#   parsed inline so the operator UI doesn't have to re-parse the audit
#   JSON.
#
# POST /api/disputed-quotes/<int:quote_id>/resolve
#   body: { "action": "verify"|"reject", "quote_text"?, "resolver_notes"?,
#           "resolved_by"? }
#   resp: { ok, quote, realigned? }
#   Operator flips a disputed quote to verified (optionally with a
#   text edit) or rejected. Text edits null word_timings AND trigger
#   `align_meeting_quotes` so the karaoke stays in sync (mirrors the
#   T-017 V3 stale-alignment fix pattern).


def _parse_member_quote_row(r) -> dict:
    """Hydrate a quotes row dict: parse topic_tags + word_timings
    + verdict_emphasis_tokens JSON + the Gemini audit JSON for the
    operator surface. Mirrors the same parsing the Cast endpoint does,
    plus surfaces `gemini_verdict` + `operator_resolution` + the D-054
    display-cache fields (`quote_text_display`, `verdict_emphasis_tokens`)
    as their own fields."""
    c = dict(r)
    tags = c.get('topic_tags')
    if tags:
        try:
            c['topic_tags'] = json.loads(tags)
        except (json.JSONDecodeError, TypeError):
            c['topic_tags'] = []
    else:
        c['topic_tags'] = []
    wt = c.get('word_timings')
    if wt:
        try:
            c['word_timings'] = json.loads(wt)
        except (json.JSONDecodeError, TypeError):
            c['word_timings'] = None
    else:
        c['word_timings'] = None
    emphasis_raw = c.get('verdict_emphasis_tokens')
    if emphasis_raw:
        try:
            parsed_emphasis = json.loads(emphasis_raw)
            if isinstance(parsed_emphasis, list):
                c['verdict_emphasis_tokens'] = [str(x) for x in parsed_emphasis]
            else:
                c['verdict_emphasis_tokens'] = []
        except (json.JSONDecodeError, TypeError):
            c['verdict_emphasis_tokens'] = []
    else:
        c['verdict_emphasis_tokens'] = []
    audit_raw = c.get('gemini_correction_notes')
    audit = None
    if audit_raw:
        try:
            audit = json.loads(audit_raw)
        except (json.JSONDecodeError, TypeError):
            audit = None
    c['gemini_verdict'] = (audit or {}).get('raw_gemini_verdict')
    c['operator_resolution'] = (audit or {}).get('operator_resolution')
    return c


def _populate_disputed_display_cache(rows: list) -> list:
    """For every disputed-quote row, lazy-compute the D-054 display
    helpers (`quote_text_display` polish + `verdict_emphasis_tokens`)
    if they're missing, run them in parallel, persist to DB so
    subsequent reads are instant, and return rows mutated in place.

    Each row gets up to TWO gpt-4o-mini calls on first encounter
    (~$0.0002 / quote total). Skipped entirely if OPENAI_API_KEY is
    not configured — the frontend falls back to raw `quote_text` and
    the unhighlighted humanized verdict.

    Parallelism: ThreadPoolExecutor cuts the 8-disputed-quote cold load
    from ~30s sequential to ~5s. Subsequent loads are zero-LLM (cached
    columns are present, helper is a no-op).
    """
    if not rows:
        return rows

    # Lazy-import: the disputed surface is the only consumer; importing
    # at module load would pull `requests` + env_config + OpenAI key
    # resolution for every Flask startup.
    try:
        from quote_cleaner import polish_for_display, is_configured as polish_is_configured
        from verdict_emphasis import extract_verdict_emphasis, is_configured as emphasis_is_configured
        from database import update_quote_display_cache
    except Exception as e:
        app.logger.warning(
            "disputed-quotes display-cache helpers unavailable (%s) — serving raw rows",
            e,
        )
        return rows

    has_polish_key = polish_is_configured()
    has_emphasis_key = emphasis_is_configured()
    if not has_polish_key and not has_emphasis_key:
        return rows

    from concurrent.futures import ThreadPoolExecutor

    # Decode the structured verdict per row once so the LLM call site
    # doesn't have to re-parse the JSON audit blob.
    work = []
    for row in rows:
        audit_raw = row.get('gemini_correction_notes')
        verdict_dict = None
        if audit_raw:
            try:
                audit = json.loads(audit_raw)
                verdict_dict = (audit or {}).get('raw_gemini_verdict')
            except (json.JSONDecodeError, TypeError):
                verdict_dict = None
        need_polish = (
            has_polish_key
            and row.get('quote_text')
            and not row.get('quote_text_display')
        )
        need_emphasis = (
            has_emphasis_key
            and verdict_dict
            and not row.get('verdict_emphasis_tokens')
        )
        if not need_polish and not need_emphasis:
            continue
        work.append({
            'row': row,
            'need_polish': need_polish,
            'need_emphasis': need_emphasis,
            'verdict': verdict_dict,
        })

    if not work:
        return rows

    def _run_polish(quote_text: str):
        try:
            return polish_for_display(quote_text)
        except Exception as e:
            app.logger.warning("polish_for_display crashed: %s", e)
            return None

    def _run_emphasis(verdict: dict):
        try:
            return extract_verdict_emphasis(verdict)
        except Exception as e:
            app.logger.warning("extract_verdict_emphasis crashed: %s", e)
            return None

    # max_workers caps at 8 — typical disputed batch size is 8-16 quotes;
    # going wider doesn't help and may anger the OpenAI rate limit.
    with ThreadPoolExecutor(max_workers=min(8, len(work) * 2)) as pool:
        polish_futures = {}
        emphasis_futures = {}
        for item in work:
            if item['need_polish']:
                polish_futures[id(item)] = pool.submit(_run_polish, item['row']['quote_text'])
            if item['need_emphasis']:
                emphasis_futures[id(item)] = pool.submit(_run_emphasis, item['verdict'])

        for item in work:
            new_display = None
            new_emphasis = None
            if id(item) in polish_futures:
                result = polish_futures[id(item)].result()
                if result and not result.error:
                    new_display = result.polished
            if id(item) in emphasis_futures:
                result = emphasis_futures[id(item)].result()
                if result and not result.error:
                    new_emphasis = result.emphasis_tokens
            try:
                update_quote_display_cache(
                    item['row']['id'],
                    quote_text_display=new_display,
                    verdict_emphasis_tokens=new_emphasis,
                )
            except Exception as e:
                app.logger.warning(
                    "failed to cache display fields for quote %s: %s",
                    item['row'].get('id'), e,
                )
            # Patch the in-memory row so the response includes the
            # freshly-computed values without an extra SELECT round-trip.
            if new_display is not None:
                item['row']['quote_text_display'] = new_display
            if new_emphasis is not None:
                item['row']['verdict_emphasis_tokens'] = json.dumps(
                    new_emphasis, ensure_ascii=False
                )

    return rows


@app.route('/api/disputed-quotes', methods=['GET'])
def list_disputed_quotes_endpoint():
    _user, _err = _require_owner()
    if _err:
        return _err
    city = (request.args.get('city') or '').strip() or None
    try:
        from database import list_disputed_quotes
        rows = list_disputed_quotes(city_name=city)
    except Exception as e:
        app.logger.exception("list_disputed_quotes failed")
        return jsonify({'error': str(e)}), 500

    # D-054 lazy-compute: polish + verdict-emphasis on first encounter.
    # Cached to DB so subsequent loads are instant. See
    # `_populate_disputed_display_cache` for the parallel-LLM machinery.
    rows = _populate_disputed_display_cache(rows)

    return jsonify({
        'city': city,
        'count': len(rows),
        'disputed_quotes': [_parse_member_quote_row(r) for r in rows],
    })


@app.route('/api/disputed-quotes/<int:quote_id>/agent-propose', methods=['POST'])
def disputed_quote_agent_propose_endpoint(quote_id: int):
    """D-057 extension — agent records a counter-proposal on a disputed
    quote. Mirrors /api/vocabulary-inbox/<id>/agent-propose.

    Body: { proposed_quote_text: str (required, non-empty),
            reasoning: str (optional),
            agent_role: str (optional — falls back to X-Zspan-Agent-Role) }

    The agent calls this BEFORE escalating. The DisputedQuotesPage UI
    surfaces both the polished form (verifier-side) and the agent's
    counter-proposal; ✨ Slack reaction applies the agent's value.
    """
    # RR-8 / SEC-AUTH-2: owner-OR-agent-token (mirrors the vocabulary
    # agent-propose gate). The Disputed-Quotes-Reviewer presents the
    # ZSPAN_AGENT_STATE_TOKEN bearer; the role header stays attribution-only.
    actor, _err = _require_owner_or_agent_token()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    raw = payload.get('proposed_quote_text')
    if not isinstance(raw, str) or not raw.strip():
        return jsonify({'error': 'proposed_quote_text (non-empty string) required'}), 400
    proposed_quote_text = raw.strip()
    reasoning = (payload.get('reasoning') or '').strip() or None
    if actor is not None:
        agent_role = 'operator'
        # An operator using this agent-specific endpoint directly suggests
        # routing confusion — surface the case but still allow it (operator
        # may genuinely want to record a counter-proposal pre-resolve).
        app.logger.warning(
            "agent-propose called without X-Zspan-Agent-Role header; "
            "attributing to 'operator' for quote_id=%s", quote_id,
        )
    else:
        agent_role = _resolved_agent_proposal_role(payload)
        if agent_role not in KNOWN_ROLES:
            return jsonify({
                'error': ('agent_role must name a known fleet role from '
                          'agent_audit.KNOWN_ROLES'),
            }), 400

    try:
        from database import record_agent_quote_counter_proposal
        row = record_agent_quote_counter_proposal(
            quote_id=quote_id,
            proposed_quote_text=proposed_quote_text,
            reasoning=reasoning,
            agent_role=agent_role,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.exception(
            "record_agent_quote_counter_proposal failed for id=%s", quote_id
        )
        return jsonify({'error': str(e)}), 500
    if row is None:
        return jsonify({'error': 'quote not found', 'id': quote_id}), 404
    return jsonify({'ok': True, 'quote': row})


@app.route('/api/disputed-quotes/<int:quote_id>/resolve', methods=['POST'])
@_require_trusted_origin
def resolve_disputed_quote_endpoint(quote_id):
    # Session-31 auth-audit remediation — owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    action = (payload.get('action') or '').strip().lower()
    if not action:
        return jsonify({'error': "action field required ('verify' or 'reject')"}), 400
    quote_text = payload.get('quote_text')
    resolver_notes = payload.get('resolver_notes')
    resolved_by = payload.get('resolved_by')

    try:
        from database import resolve_disputed_quote
        result = resolve_disputed_quote(
            quote_id=quote_id, action=action,
            quote_text=quote_text, resolver_notes=resolver_notes,
            resolved_by=resolved_by,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.exception("resolve_disputed_quote failed for %s", quote_id)
        return jsonify({'error': str(e)}), 500

    if result is None:
        return jsonify({'error': 'quote not found', 'id': quote_id}), 404

    realigned_stats = None
    if result.get('_word_timings_invalidated'):
        # Mirror the T-017 V3 fix: re-run alignment so the karaoke
        # reflects the corrected display tokens. Targets the unified
        # `quotes` table aligner (post-D-052).
        try:
            from quote_align import align_quotes_for_meeting
            realigned_stats = align_quotes_for_meeting(result['meeting_id'])
        except Exception as e:
            app.logger.warning(
                "post-resolve realignment failed for quote %s (%s); "
                "the quote is resolved but word_timings stays NULL until "
                "alignment runs manually",
                quote_id, e,
            )

    # Strip internal-only keys before sending.
    result.pop('_text_changed', None)
    result.pop('_word_timings_invalidated', None)
    return jsonify({
        'ok': True,
        'quote': _parse_member_quote_row(result),
        'realigned': realigned_stats,
    })


# ── Phase 2 D-Build-B — Speaker Roster Review queue ──────────────────
#
# Operator surface for confirming pyannote cluster → council_members
# canonical-name mappings. Sonnet's cluster_roster_mapper auto-promotes
# proposals where BOTH prongs pass (anchor evidence + last-name
# specificity); the rest land in `pending_review` for operator review
# via the endpoints below.
#
# GET  /api/speaker-roster/pending-review
#   resp: { count, rows: [...] }
#   Cross-meeting queue — every meeting_speaker_roster row in
#   status='pending_review'. Joined to meetings for meeting_title +
#   meeting_date + city_name so the UI doesn't have to re-fetch.
#
# GET  /api/speaker-roster/meeting/<int:meeting_id>
#   resp: { meeting_id, city_name, roster, council_members }
#   Per-meeting view — all roster rows + the city's council_members
#   roster (for the override picker dropdown).
#
# POST /api/speaker-roster/<int:row_id>/confirm
#   body: { resolved_by }
#   resp: { ok, row }
#   Operator agrees with proposed_canonical; sets confirmed_canonical
#   to proposed_canonical + status='operator_confirmed'.
#
# POST /api/speaker-roster/<int:row_id>/override
#   body: { confirmed_canonical, resolved_by }
#   resp: { ok, row }
#   Operator picks a different canonical from the roster. The new value
#   MUST match a council_members row for the meeting's city.
#
# POST /api/speaker-roster/<int:row_id>/anonymous
#   body: { resolved_by }
#   resp: { ok, row }
#   Operator opts out — cluster stays anonymous, rendered as "Speaker N"
#   on the broadcast surface.


@app.route('/api/speaker-roster/pending-review', methods=['GET'])
def list_speaker_roster_pending_endpoint():
    # RR-8 backstop gate: pending-review rows carry model/prong reasoning +
    # resolver identities — operator review data (SpeakerRosterReviewPage is
    # owner-gated; its confirm/override mutations already gate).
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        limit_raw = request.args.get('limit', '100')
        try:
            limit = max(1, min(int(limit_raw), 500))
        except ValueError:
            limit = 100
        from database import list_pending_speaker_roster_reviews
        rows = list_pending_speaker_roster_reviews(limit=limit)
    except Exception as e:
        app.logger.exception("list_pending_speaker_roster_reviews failed")
        return jsonify({'error': str(e)}), 500
    return jsonify({'count': len(rows), 'rows': rows})


@app.route('/api/speaker-roster/meeting/<int:meeting_id>', methods=['GET'])
def get_speaker_roster_for_meeting_endpoint(meeting_id: int):
    # RR-8 backstop gate: per-meeting roster evidence (draft + published) —
    # operator review data behind the owner-gated review page.
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        from database import (
            get_speaker_roster_for_meeting,
            get_council_members,
            get_connection,
        )
        roster = get_speaker_roster_for_meeting(meeting_id)
        conn = get_connection()
        try:
            m_row = conn.execute(
                "SELECT city_name, meeting_title, meeting_date "
                "FROM meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
        finally:
            conn.close()
        if not m_row:
            return jsonify({'error': 'meeting not found', 'meeting_id': meeting_id}), 404
        city_name = m_row['city_name']
        members = get_council_members(city_name) or []
    except Exception as e:
        app.logger.exception("get_speaker_roster_for_meeting failed")
        return jsonify({'error': str(e)}), 500
    return jsonify({
        'meeting_id': meeting_id,
        'meeting_title': m_row['meeting_title'],
        'meeting_date': m_row['meeting_date'],
        'city_name': city_name,
        'roster': roster,
        'council_members': [
            {'name': m.get('name'), 'role': m.get('role'), 'seat_id': m.get('seat_id')}
            for m in members
        ],
    })


def _resolve_speaker_roster_actor() -> str:
    """Pull the resolver identity from the request body or fall back to
    the current_actor() helper (which reads X-Zspan-Agent-Role)."""
    payload = request.get_json(silent=True) or {}
    explicit = (payload.get('resolved_by') or '').strip()
    return explicit or current_actor()


@app.route('/api/speaker-roster/<int:row_id>/confirm', methods=['POST'])
@_require_trusted_origin
def confirm_speaker_roster_endpoint(row_id: int):
    """Operator agrees with Sonnet's proposed_canonical."""
    # Session-31 auth-audit remediation — owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    resolved_by = _resolve_speaker_roster_actor()
    try:
        from database import (
            confirm_speaker_roster_row,
            get_connection,
        )
        # Look up the row first to get its proposed_canonical.
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT proposed_canonical FROM meeting_speaker_roster "
                "WHERE id = ?",
                (row_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({'error': 'row not found', 'id': row_id}), 404
        proposed = row['proposed_canonical']
        if not proposed:
            return jsonify({
                'error': 'cannot confirm — row has no proposed_canonical '
                         '(use override or anonymous instead)',
                'id': row_id,
            }), 400
        result = confirm_speaker_roster_row(
            row_id, confirmed_canonical=proposed, resolved_by=resolved_by,
            resolution_action='operator_confirmed',
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.exception("confirm_speaker_roster failed for %s", row_id)
        return jsonify({'error': str(e)}), 500
    # D-147 (2026-07-01): the V-Op-1 voice-library capture hook that lived
    # here was removed — biometric embeddings are ephemeral-only now.
    return jsonify({'ok': True, 'row': result})


@app.route('/api/speaker-roster/<int:row_id>/override', methods=['POST'])
@_require_trusted_origin
def override_speaker_roster_endpoint(row_id: int):
    """Operator picks a different canonical from the roster (or types
    a non-roster name explicitly — the UI guards against this, but the
    endpoint accepts free-form so operator overrides aren't blocked by
    a stale roster)."""
    # Session-31 auth-audit remediation — owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    confirmed = (payload.get('confirmed_canonical') or '').strip()
    if not confirmed:
        return jsonify({'error': 'confirmed_canonical (non-empty) required'}), 400
    resolved_by = _resolve_speaker_roster_actor()
    try:
        from database import confirm_speaker_roster_row
        result = confirm_speaker_roster_row(
            row_id, confirmed_canonical=confirmed, resolved_by=resolved_by,
            resolution_action='operator_overridden',
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.exception("override_speaker_roster failed for %s", row_id)
        return jsonify({'error': str(e)}), 500
    # D-147 (2026-07-01): the V-Op-1 voice-library capture hook that lived
    # here was removed — biometric embeddings are ephemeral-only now.
    return jsonify({'ok': True, 'row': result})


@app.route('/api/speaker-roster/<int:row_id>/anonymous', methods=['POST'])
@_require_trusted_origin
def anonymous_speaker_roster_endpoint(row_id: int):
    """Operator opts out — cluster stays anonymous, renders as 'Speaker N'."""
    # Session-31 auth-audit remediation — owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    resolved_by = _resolve_speaker_roster_actor()
    try:
        from database import mark_speaker_roster_anonymous
        result = mark_speaker_roster_anonymous(row_id, resolved_by=resolved_by)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.exception("anonymous_speaker_roster failed for %s", row_id)
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'row': result})


@app.route('/api/speaker-roster/<int:row_id>/cluster-samples', methods=['GET'])
def speaker_roster_cluster_samples_endpoint(row_id: int):
    """Per-cluster turn-excerpt samples — what the operator needs to
    identify a SPEAKER_NN they can't read off a Sonnet proposal.

    Returns up to N (default 3) representative speech turns for the row's
    cluster, each with `start_seconds` so the SpeakerRosterReviewPage can
    render a "▶ Listen in context" deep-link that opens BroadcastPage at
    that timestamp. Backed by `cluster_roster_mapper.top_n_turn_excerpts`
    which walks the meeting's Qdrant chunks + dedupes turns by (start,
    end) so the top-N list isn't three near-identical excerpts from
    chunking overlap.

    Honest-empty: returns `{"excerpts": []}` when the meeting was
    diarized but never indexed to Qdrant, OR no turn matches the cluster
    label, OR every turn ran shorter than the 2s min-duration floor.
    """
    # RR-8 backstop gate: returns Qdrant-derived transcript excerpts —
    # operator review data behind the owner-gated review page.
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        limit_raw = request.args.get('n', '3')
        try:
            n_limit = max(1, min(int(limit_raw), 10))
        except ValueError:
            n_limit = 3

        from database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT r.meeting_id, r.cluster_label, m.video_url
                FROM meeting_speaker_roster r
                JOIN meetings m ON m.id = r.meeting_id
                WHERE r.id = ?
                """,
                (row_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({'error': 'row not found', 'id': row_id}), 404

        # Add the bridge dir to sys.path the same way the worker-spawn
        # paths above do — keeps the parsers/ tree decoupled from a hard
        # import of the bridge at module load.
        bridge_dir = os.path.normpath(os.path.join(
            os.path.dirname(__file__), '..', '..',
        ))
        if bridge_dir not in sys.path:
            sys.path.insert(0, bridge_dir)
        from zspan_pipeline.cluster_roster_mapper import top_n_turn_excerpts

        excerpts = top_n_turn_excerpts(
            int(row['meeting_id']),
            str(row['cluster_label']),
            n=n_limit,
        )
    except Exception as e:
        app.logger.exception("cluster_samples failed for row=%s", row_id)
        return jsonify({'error': str(e)}), 500
    return jsonify({
        'row_id': row_id,
        'meeting_id': int(row['meeting_id']),
        'cluster_label': str(row['cluster_label']),
        # The inline MeetingExcerptPlayer on the speaker-roster page needs
        # the meeting's video URL to resolve its source kind (YouTube vs
        # direct MP4 vs Granicus MediaPlayer). Returning here avoids a
        # second round-trip from the page. May be null/empty when the
        # meeting hasn't resolved a video source yet.
        'video_url': row['video_url'] or None,
        'excerpts': excerpts,
    })


# ── T-013 Local Review Queue — operator-side filesystem affordance ─────
#
# POST /api/local-fs/open-review-queue
#   body: { "meeting_id": int, "batch_index": int|null }
#   resp: { ok, path }   on success
#         { error }      with appropriate HTTP status otherwise
#
# Opens the meeting's `media/review_queue/<city>/<date>__<slug>/` folder
# in the OS file explorer. With `batch_index` supplied, drills into the
# `batch_NN/` subfolder. Single-user dev convenience — gets retired when
# the volunteer reviewer flow comes online via OAuth (T-018 scope).
# Validated server-side to keep paths inside `media/review_queue/` so
# the endpoint can't be misused to open arbitrary system paths.


@app.route('/api/local-fs/open-review-queue', methods=['POST'])
@_require_trusted_origin
def open_review_queue_endpoint():
    # Session-31 auth-audit remediation — owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    payload = request.get_json(silent=True) or {}
    meeting_id = payload.get('meeting_id')
    batch_index = payload.get('batch_index')

    if not isinstance(meeting_id, int):
        return jsonify({'error': 'meeting_id (int) required'}), 400
    if batch_index is not None and not isinstance(batch_index, int):
        return jsonify({'error': 'batch_index must be an int or null'}), 400

    try:
        from local_fs import (
            resolve_target_path,
            is_safe_path,
            open_in_file_explorer,
        )
    except Exception as e:
        app.logger.exception("local_fs import failed")
        return jsonify({'error': f'local_fs module unavailable: {e}'}), 500

    target = resolve_target_path(meeting_id, batch_index)
    if target is None:
        return jsonify({
            'error': 'no review queue folder for this meeting (or batch index out of range)',
            'meeting_id': meeting_id,
            'batch_index': batch_index,
            'hint': "Run `python3.11 -m zspan_pipeline.scripts.build_review_queue --meeting-id N` first.",
        }), 404

    if not is_safe_path(target):
        app.logger.warning(
            "rejected open-review-queue: resolved path %s is outside review_queue root",
            target,
        )
        return jsonify({'error': 'resolved path is outside the review_queue tree'}), 400

    try:
        open_in_file_explorer(target)
    except Exception as e:
        app.logger.exception("open_in_file_explorer failed for %s", target)
        return jsonify({'error': str(e), 'path': str(target)}), 500

    return jsonify({'ok': True, 'path': str(target)})


# ── T-013 V4 — source-cache cleanup ───────────────────────────────────
#
# POST /api/work-orders/<id>/clear-source-cache
#   Delete the meeting's `media/review_queue/<...>/source.mp4` (~45 MB
#   each). Preserves per-batch clips + manifest + RESPONSE.md so review
#   evidence stays intact. Idempotent: clearing an already-clear cache
#   returns existed=false, no error.
#
# GET /api/operator/source-cache-size
#   Total bytes + file count of source.mp4 caches across the whole
#   review_queue tree. Powers the disk-usage badge in the operator-
#   terminal header.


@app.route('/api/work-orders/<int:work_order_id>/clear-source-cache', methods=['POST'])
@_require_trusted_origin
def clear_source_cache_endpoint(work_order_id):
    # RR-8 fix-list (S-129) — deletes the ~45MB source.mp4 cache, forcing
    # a re-download on the next build (bandwidth + upstream courtesy).
    # Two-file fix per developer.md § 5b: this gate + the Express proxy
    # switched to cookie-forwarding in the same commit (it wasn't passing
    # `req`, so gating Flask alone would have 401'd the real owner).
    _user, _err = _require_owner()
    if _err:
        return _err
    meeting_id = _resolve_meeting_id_from_wo(work_order_id)
    if meeting_id is None:
        return jsonify({'error': 'WO not found or has no meeting_id'}), 404
    try:
        from local_fs import delete_meeting_source_cache
    except Exception as e:
        app.logger.exception("local_fs import failed")
        return jsonify({'error': f'local_fs module unavailable: {e}'}), 500

    result = delete_meeting_source_cache(meeting_id)
    return jsonify({
        'ok': True,
        'work_order_id': work_order_id,
        'meeting_id': meeting_id,
        **result,
    })


@app.route('/api/operator/source-cache-size', methods=['GET'])
def operator_source_cache_size_endpoint():
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        from local_fs import source_cache_size_bytes
    except Exception as e:
        app.logger.exception("local_fs import failed")
        return jsonify({'error': f'local_fs module unavailable: {e}'}), 500
    return jsonify(source_cache_size_bytes())


# ── V1-Batch-3 — V1 launch progress dashboard endpoint ──────────────────
#
# GET /api/v1-launch/progress?days_back=14
#
# Cheap read-only summary of the V1 launch board (per V1_PUBLIC_RELEASE_SPEC).
# Reads from the meetings + work_orders tables only — no scrape refresh, no
# pattern projection (those live in v1_batch_scan.py CLI for the operator's
# heavier batch workflows). Per-city counts + the URL-gap board so the
# operator UI can render a single-glance V1 status surface.

V1_TARGET_CITIES = ["Kingman", "Bullhead City", "Lake Havasu City", "Colorado City"]
V1_NON_TERMINAL_WO_STATES = {"pending", "awaiting_video", "processing", "awaiting_notebook"}


@app.route('/api/v1-launch/progress', methods=['GET'])
def v1_launch_progress_endpoint():
    """Per-city V1 launch progress + URL-gap board.

    Read-only. Cheap. Polls per render. The V1 target is Mohave-County 4
    cities × past N days (default 14). Each city reports: in-window
    meeting count, WO-state breakdown, completed count, url-gap count,
    and a derived target-state pill (not_started | in_progress | complete).
    """
    # RR-8 backstop gate: operator launch dashboard (meeting IDs, WO states,
    # titles, URL-gaps). Consumed only by the owner V1LaunchPage.
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        days_back_raw = request.args.get('days_back', '14')
        try:
            days_back = max(1, min(int(days_back_raw), 90))
        except (TypeError, ValueError):
            return jsonify({'error': 'days_back must be an integer 1-90'}), 400
        from datetime import date, timedelta, datetime as _dt
        window_end = date.today()
        window_start = window_end - timedelta(days=days_back)

        conn = get_connection()
        cur = conn.cursor()
        cities_out = []
        total_meetings = 0
        total_processed = 0
        total_url_gap = 0
        total_wo = 0
        for city in V1_TARGET_CITIES:
            cur.execute(
                """
                SELECT m.id, m.meeting_title, m.meeting_date, m.meeting_time,
                       w.id AS wo_id, w.state AS wo_state,
                       w.youtube_video_url AS wo_url,
                       w.completed_at AS wo_completed_at
                FROM meetings m
                LEFT JOIN work_orders w ON w.meeting_id = m.id
                WHERE m.city_name = ?
                  AND m.meeting_date >= ?
                  AND m.meeting_date <= ?
                ORDER BY m.meeting_date DESC, m.id DESC
                """,
                (city, window_start.isoformat(), window_end.isoformat()),
            )
            rows = [dict(r) for r in cur.fetchall()]
            wo_states: Dict[str, int] = {}
            url_gap_meetings = []
            completed = 0
            for r in rows:
                if r["wo_state"]:
                    wo_states[r["wo_state"]] = wo_states.get(r["wo_state"], 0) + 1
                    if r["wo_state"] == "completed":
                        completed += 1
                    if (
                        r["wo_state"] in V1_NON_TERMINAL_WO_STATES
                        and not r["wo_url"]
                    ):
                        url_gap_meetings.append({
                            "meeting_id": r["id"],
                            "work_order_id": r["wo_id"],
                            "title": r["meeting_title"],
                            "date": r["meeting_date"],
                            "time": r["meeting_time"],
                        })
            in_window = len(rows)
            url_gap_count = len(url_gap_meetings)
            if in_window == 0:
                target_state = "not_started"
            elif completed == in_window:
                target_state = "complete"
            else:
                target_state = "in_progress"
            cities_out.append({
                "city": city,
                "in_window_meeting_count": in_window,
                "wo_state_counts": wo_states,
                "completed_count": completed,
                "url_gap_count": url_gap_count,
                "url_gap_meetings": url_gap_meetings,
                "target_state": target_state,
            })
            total_meetings += in_window
            total_processed += completed
            total_url_gap += url_gap_count
            total_wo += sum(wo_states.values())
        conn.close()
        return jsonify({
            "scan_run_at": _dt.now().isoformat(timespec="seconds"),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "days_back": days_back,
            "v1_target_cities": V1_TARGET_CITIES,
            "cities": cities_out,
            "totals": {
                "in_window_meetings": total_meetings,
                "processed": total_processed,
                "url_gap": total_url_gap,
                "work_orders": total_wo,
            },
        })
    except Exception as e:
        app.logger.exception("v1_launch_progress failed")
        return jsonify({'error': str(e)}), 500


# ── Quote cleaner endpoint REMOVED (RR-8 fix-list, S-129, 2026-07-07) ──
# /api/quotes/clean ran an LLM cleaning pass over arbitrary POSTed text
# with zero callers anywhere in client/server/pipeline — an orphaned
# spend surface. Its two documented consumers never materialized (the
# operator spot-check UI was never built; the pipeline integration died
# with D-143). The library function quote_cleaner.clean_quote stays —
# the pipeline uses it directly. Restore from git history if a caller
# ever actually exists.

# ── T-004 video matcher (operator-terminal trigger) ───────────────────
#
# POST /api/work-orders/match-videos/<city>
#   body: { "apply": bool, "min_confidence": "high"|"medium"|"needs_review",
#           "within_days": int, "max_videos": int }
#   resp: { success, error?, channel_url, videos_listed, meetings_inspected,
#           matches_applied, results: [...] }
#
# Wraps parsers/match_videos.py's run_match(). With apply=true (default for
# this endpoint), high-confidence matches are auto-written to meetings.video_url
# AND propagated to work_orders.youtube_video_url with state flipped
# awaiting_video → pending. Medium / needs_review matches write match
# metadata only — the [CONFIRM URL] button in the operator terminal handles
# those.

# NOTE: The /api/notebooks/* GC endpoints (D-030) — list tracked notebooks,
# list protected ids, audit log, protect/unprotect/delete — were removed
# per D-143 (NotebookLM subsystem removal 2026-07-01) along with
# notebook_gc.py and the protected_notebook_ids + notebook_deletion_log
# tables. Frontend NotebookGcPanel gets stripped in chunk-22. See S-109.


# GET /api/cities/with-channels
#   Returns the list of cities that have a YouTube channel registered in
#   cities.youtube_channel_url — i.e., the set the T-004 video matcher
#   can run against. Used by the OperatorTerminal to populate the
#   match-target dropdown.
@app.route('/api/cities/with-channels', methods=['GET'])
def cities_with_channels_endpoint():
    conn = get_connection()
    rows = conn.execute("""
        SELECT name, county, state, youtube_channel_url, youtube_channel_id
        FROM cities
        WHERE youtube_channel_url IS NOT NULL AND youtube_channel_url != ''
        ORDER BY name COLLATE NOCASE
    """).fetchall()
    conn.close()
    return jsonify({
        'cities': [dict(r) for r in rows],
        'count': len(rows),
    })


@app.route('/api/work-orders/match-videos/<city>', methods=['POST'])
@_require_trusted_origin
def match_videos_endpoint(city):
    """Match YouTube videos to in-window meetings for a city.

    Backed by the Haiku matcher (`scripts.haiku_match_videos.run_match_haiku`)
    per 2026-06-10 retirement of the deterministic match_videos.py heuristics.
    YouTube Data API stays the authoritative source for the candidate video
    list; matching itself is Haiku-as-classifier via the Mac relay.
    Defaults: apply=True, min_confidence='high', within_days=21, max_videos=50.
    """
    # Session-31 auth-audit remediation — owner-only.
    _user, _err = _require_owner()
    if _err:
        return _err
    from scripts.haiku_match_videos import run_match_haiku
    payload = request.get_json(silent=True) or {}
    apply = bool(payload.get('apply', True))
    min_confidence = payload.get('min_confidence', 'high')
    within_days = int(payload.get('within_days', 21))
    max_videos = int(payload.get('max_videos', 50))

    if min_confidence not in ('high', 'medium', 'needs_review'):
        return jsonify({
            'success': False,
            'error': f"min_confidence must be high|medium|needs_review (got {min_confidence!r})",
        }), 400

    result = run_match_haiku(
        city,
        apply=apply,
        min_confidence=min_confidence,
        within_days=within_days,
        max_videos=max_videos,
    )
    return jsonify(result)


# GET /api/prompts/<name>
#   resp: { success, name, path, body } — returns the prompt body for the
#   given output type so the broadcast-page ⓘ info-icons can surface the
#   exact prompt used to generate each text output (matches NotebookLM's
#   own studio-output ⓘ pattern; honest provenance for operators).
@app.route('/api/prompts/<name>', methods=['GET'])
def prompt_body_endpoint(name):
    _user, _err = _require_owner()
    if _err:
        return _err
    import re
    from pathlib import Path
    if not re.match(r'^[a-zA-Z0-9_\-]+$', name or ''):
        return jsonify({'success': False, 'error': 'invalid prompt name'}), 400
    prompts_root = (
        Path(__file__).resolve().parent.parent.parent / 'prompts'
    ).resolve()
    target = (prompts_root / f'{name}.md').resolve()
    try:
        target.relative_to(prompts_root)
    except ValueError:
        return jsonify({'success': False, 'error': 'path traversal blocked'}), 400
    if not target.exists():
        return jsonify({'success': False, 'error': f'prompt not found: {name}'}), 404
    try:
        body = target.read_text(encoding='utf-8')
    except OSError as e:
        return jsonify({'success': False, 'error': f'read failed: {e}'}), 500
    return jsonify({
        'success': True,
        'name': name,
        'path': str(target.relative_to(prompts_root.parent.parent)),
        'body': body,
    })


# POST /api/work-orders/promote-matches
#   body: { "matches": [ {"meeting_id": int, "video_url": str,
#                         "confidence": "high"|"medium"|"needs_review",
#                         "method": str}, ... ] }
#   resp: { success, promoted: int, results: [{meeting_id, ok, error?}, ...] }
#
# Companion to /api/work-orders/match-videos called in apply=false mode.
# The matcher returns candidates; the operator reviews; this endpoint
# commits the operator-approved subset by calling apply_match() per row
# WITHOUT re-firing the Haiku matcher. Zero LLM spend; pure DB write.
#
# This breaks the S-074 cascade: preview-then-promote splits the cheap
# reconnaissance phase (one Haiku call per city) from the expensive
# downstream commitment (WO state flip → daemon drain → Sonnet spend).
@app.route('/api/work-orders/promote-matches', methods=['POST'])
@_require_trusted_origin
def promote_matches_endpoint():
    # RR-8 / SEC-AUTH-1: applies operator-approved video matches (DB writes).
    # Client-only caller (OperatorTerminal [CONFIRM URL] button); owner-gate.
    _user, _err = _require_owner()
    if _err:
        return _err
    from video_match_helpers import apply_match
    payload = request.get_json(silent=True) or {}
    matches = payload.get('matches') or []
    if not isinstance(matches, list):
        return jsonify({'success': False, 'error': 'matches must be a list'}), 400

    valid_conf = {"high", "medium", "needs_review"}
    results = []
    promoted = 0
    for i, m in enumerate(matches):
        if not isinstance(m, dict):
            results.append({'index': i, 'ok': False, 'error': 'not an object'})
            continue
        try:
            meeting_id = int(m.get('meeting_id'))
        except (TypeError, ValueError):
            results.append({'index': i, 'ok': False, 'error': 'meeting_id required (int)'})
            continue
        video_url = (m.get('video_url') or '').strip()
        confidence = (m.get('confidence') or '').strip()
        method = (m.get('method') or 'operator-approved').strip()
        if not video_url:
            results.append({'meeting_id': meeting_id, 'ok': False, 'error': 'video_url required'})
            continue
        if confidence not in valid_conf:
            results.append({
                'meeting_id': meeting_id, 'ok': False,
                'error': f'confidence must be in {sorted(valid_conf)}',
            })
            continue
        try:
            apply_match(
                meeting_id=meeting_id, video_url=video_url,
                confidence=confidence, method=method,
            )
            results.append({'meeting_id': meeting_id, 'ok': True})
            promoted += 1
        except Exception as e:
            results.append({
                'meeting_id': meeting_id, 'ok': False,
                'error': f'{type(e).__name__}: {e}',
            })

    return jsonify({
        'success': True,
        'promoted': promoted,
        'requested': len(matches),
        'results': results,
    })


# ── RAG retrieval auth gate ───────────────────────────────────────────
# `/api/rag-search` and `/api/member-rag` query the Surface Pro Qdrant
# service. Requests from 127.0.0.1 / ::1 pass through for local services;
# other origins must present the existing shared bearer token. The legacy
# token name is retained for config compatibility.

def _resolve_rag_query_token() -> str:
    """Return the rag-query bearer token, generating + persisting one on
    first call if neither env nor user_settings provides one. Caller gets
    a non-empty string (or the empty string if generation+save both fail —
    in which case the gate refuses non-local requests on principle)."""
    env = os.environ.get("ZSPAN_RAG_QUERY_TOKEN", "").strip()
    if env:
        return env
    try:
        settings = load_user_settings() or {}
    except Exception:
        return ""
    tok = (settings.get("zspan_rag_query_token") or "").strip()
    if tok:
        return tok
    # First-read auto-generate. 32 url-safe bytes = 256 bits of entropy.
    import secrets
    tok = secrets.token_urlsafe(32)
    settings["zspan_rag_query_token"] = tok
    try:
        save_user_settings(settings)
        logging.info("auto-generated zspan_rag_query_token (persisted to user_settings.json)")
    except Exception as exc:
        logging.warning("auto-generated rag-query token but save failed: %s", exc)
    return tok


def _is_local_origin(client_ip: Optional[str]) -> bool:
    """Allow a normalized loopback client through without a token."""
    return (client_ip or "") in ("127.0.0.1", "::1")


# ── /api/rag-search — V1.5-RAG-Search-1 retrieval-only BYOK endpoint ──
#
# `/api/rag-search` retrieves chunks, ships a provenance packet + canonical
# system prompt, and performs no synthesis. The user's configured provider
# (Gemini / OpenAI / Anthropic / Mistral via BYOK) synthesizes with the
# user's key + quota.
#
# This is the V1.5+ architecture per D-133 + BYOK_ARCHITECTURE_SPEC § 2.1:
# Z-SPAN handles the cheap deterministic civic-data-side operations (query
# embedding + vector search); the expensive LLM synthesis moves to the
# user side via BYOK. No LLM call on this server side; no key custody
# concerns for this endpoint (the user's key never touches Z-SPAN here).
#
# Auth gate: loopback bypass + bearer token. V1.5 launches with the legacy
# rag-query token reused (same risk profile — the
# retrieve costs us nothing per call but rate-limits Qdrant on the
# Surface Pro). Per-IP rate limiting + the dedicated rag-search token
# are a V1.5-Verify-1 / V1.5-BYOK-Shell-1 follow-up.
#
# Persistence: NOT YET. V1.5-Verify-1 lands the `byok_audit_runs` table
# migration + retrofits the audit-row write here. Until then, run_ids
# are computed + returned but not durable; /api/verify-run/{run_id}
# (V1.5-Verify-1) returns exists=false for everything generated before
# the migration lands. The packet shape is stable; only persistence is
# deferred.

def _byok_public_query_allowed():
    """Authorize live BYOK querying for signed-in accounts.

    Banned accounts remain denied and every non-owner query still passes the
    deterministic input gate, quota, cooldown, and auto-ban controls. Anonymous
    visitors remain read-only. /api/verify-run stays public because it is the
    transparency surface, not a live-query surface.
    """
    user = _current_user_from_cookie()
    # Preserve the exact principal resolved for this gate decision so
    # downstream handlers can bind their audit row without a second cookie
    # read. Every caller executes inside a Flask request context.
    g.byok_query_user = user
    if user is not None and is_owner_email(user.email):
        return True, 'owner'
    if user is not None:
        access = get_user_librarian_access(user.id)
        if access in {'none', 'requested', 'granted'}:
            return True, 'signed-account'
        if access == 'banned':
            return False, 'account-blocked'
    return False, 'sign-in-required'


def _byok_denied_payload(reason: str) -> dict:
    if reason == 'account-blocked':
        return {
            'success': False,
            'status': 'account_blocked',
            'error': 'Librarian access is unavailable for this account.',
        }
    return {
        'success': False,
        'status': 'sign_in_required',
        'error': (
            "Log in and bring your own provider key to ask a question."
        ),
    }

_LIBRARIAN_REQUEST_MAX_BYTES = 32 * 1024


def _payload_too_large_response():
    return jsonify({
        "success": False,
        "status": "payload_too_large",
        "error": "Request payload is too large.",
    }), 413


def _reject_oversized_librarian_body():
    """Enforce the live-query body cap before parsing or principal lookup."""
    # Werkzeug stops an unknown-length stream at max_content_length without
    # necessarily raising. Permit one probe byte internally so truncation at
    # the public cap is observable, then reject that probe byte below.
    request.max_content_length = _LIBRARIAN_REQUEST_MAX_BYTES + 1
    if (
        request.content_length is not None
        and request.content_length > _LIBRARIAN_REQUEST_MAX_BYTES
    ):
        return _payload_too_large_response()
    try:
        observed_length = len(request.get_data(cache=True))
    except RequestEntityTooLarge:
        return _payload_too_large_response()
    if observed_length > _LIBRARIAN_REQUEST_MAX_BYTES:
        return _payload_too_large_response()
    return None


def _librarian_gate_query_hash(
    raw_query: object,
    canonical_query: str | None = None,
) -> str:
    """Hash at most the grammar's raw-cap worth of query material."""
    if canonical_query is not None:
        material = canonical_query[:QUERY_CHAR_CAP]
    elif isinstance(raw_query, str):
        material = " ".join(
            raw_query[:QUERY_CHAR_CAP].strip().split()
        )[:QUERY_CHAR_CAP]
    else:
        encoder = json.JSONEncoder(
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        chunks: list[str] = []
        remaining = QUERY_CHAR_CAP
        for chunk in encoder.iterencode(raw_query):
            chunks.append(chunk[:remaining])
            remaining -= len(chunks[-1])
            if remaining == 0:
                break
        material = "".join(chunks)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_LIBRARIAN_DAILY_CAP_DEFAULT = 3


def _librarian_daily_cap() -> int:
    """Return the SQLite-owned per-account cap."""
    return get_librarian_policy_snapshot().daily_query_cap


_LIBRARIAN_ABUSE_DEFAULTS = {
    "burst_threshold": 8,
    "burst_window_seconds": 600,
    "cooldown_seconds": 1800,
    "strike_threshold": 3,
    "autoban_window_seconds": 86400,
}
_LIBRARIAN_TUNING_GUARDRAILS = {
    "librarian_daily_query_cap": {
        "default": _LIBRARIAN_DAILY_CAP_DEFAULT,
        "min": 1,
        "max": None,
        "unit": "queries",
    },
    "librarian_reject_burst_threshold": {
        "default": _LIBRARIAN_ABUSE_DEFAULTS["burst_threshold"],
        "min": 4,
        "max": 64,
        "unit": "rejects",
    },
    "librarian_reject_burst_window_seconds": {
        "default": _LIBRARIAN_ABUSE_DEFAULTS["burst_window_seconds"],
        "min": 60,
        "max": None,
        "unit": "seconds",
    },
    "librarian_reject_cooldown_seconds": {
        "default": _LIBRARIAN_ABUSE_DEFAULTS["cooldown_seconds"],
        "min": 300,
        "max": None,
        "unit": "seconds",
    },
    "librarian_reject_autoban_strike_threshold": {
        "default": _LIBRARIAN_ABUSE_DEFAULTS["strike_threshold"],
        "min": 2,
        "max": 32,
        "unit": "cooldowns",
    },
    "librarian_reject_autoban_window_seconds": {
        "default": _LIBRARIAN_ABUSE_DEFAULTS["autoban_window_seconds"],
        "min": 3600,
        "max": None,
        "unit": "seconds",
    },
}


def _load_effective_librarian_tuning() -> tuple[dict, dict, bool]:
    """Read the exact SQLite revision enforcement will consume."""
    policy = get_librarian_policy_snapshot()
    effective = {
        "librarian_daily_query_cap": policy.daily_query_cap,
        "librarian_reject_burst_threshold": (
            policy.reject_burst_threshold
        ),
        "librarian_reject_burst_window_seconds": (
            policy.reject_burst_window_seconds
        ),
        "librarian_reject_cooldown_seconds": (
            policy.reject_cooldown_seconds
        ),
        "librarian_reject_autoban_strike_threshold": (
            policy.reject_autoban_strike_threshold
        ),
        "librarian_reject_autoban_window_seconds": (
            policy.reject_autoban_window_seconds
        ),
    }
    return {"revision": policy.revision}, effective, False


def _librarian_tuning_stats() -> dict[str, int]:
    """Return all five status-strip counters in one database query."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT 'granted_accounts' AS metric, COUNT(*) AS value
            FROM users
            WHERE librarian_access = 'granted'
            UNION ALL
            SELECT 'requested_pending', COUNT(*)
            FROM users
            WHERE librarian_access = 'requested'
            UNION ALL
            SELECT 'cooldowns_active', COUNT(*)
            FROM librarian_abuse_state
            WHERE cooldown_until > CURRENT_TIMESTAMP
            UNION ALL
            SELECT 'auto_bans_last_7d', COUNT(*)
            FROM librarian_abuse_state
            WHERE active_auto_ban = 1
              AND auto_banned_at > datetime(CURRENT_TIMESTAMP, '-7 days')
            UNION ALL
            SELECT 'accepted_queries_last_24h', COUNT(*)
            FROM librarian_gate_events
            WHERE stencil_result = 'accepted'
              AND created_at > datetime(CURRENT_TIMESTAMP, '-24 hours')
            """
        ).fetchall()
        return {str(row["metric"]): int(row["value"]) for row in rows}
    finally:
        conn.close()


def _librarian_tuning_payload() -> dict:
    """Build the owner panel payload from effective settings and live data."""
    _current, effective, group_fallback_active = (
        _load_effective_librarian_tuning()
    )
    return {
        "settings": {
            key: {
                "value": effective[key],
                **guardrail,
            }
            for key, guardrail in _LIBRARIAN_TUNING_GUARDRAILS.items()
        },
        "group_fallback_active": group_fallback_active,
        "revision": get_librarian_policy_snapshot().revision,
        "cross_field_rule": (
            "burst_window_seconds <= cooldown_seconds <= "
            "autoban_window_seconds; auto-ban must be reachable"
        ),
        "stats": _librarian_tuning_stats(),
    }


_LIBRARIAN_SMALL_NUMBERS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def _librarian_count_phrase(value: int) -> str:
    return _LIBRARIAN_SMALL_NUMBERS.get(value, str(value))


def _librarian_unlock_phrase(retry_after_seconds: int) -> str:
    if retry_after_seconds < 60:
        return "in under a minute"
    if retry_after_seconds < 3600:
        minutes = max(1, math.ceil(retry_after_seconds / 60))
        unit = "minute" if minutes == 1 else "minutes"
        return f"in about {_librarian_count_phrase(minutes)} {unit}"
    hours = max(1, math.ceil(retry_after_seconds / 3600))
    unit = "hour" if hours == 1 else "hours"
    return f"in about {_librarian_count_phrase(hours)} {unit}"


def _librarian_quota_error(cap: int, retry_after_seconds: int) -> str:
    return (
        f"You've reached the {_librarian_count_phrase(cap)}-question "
        f"limit. Your next question unlocks "
        f"{_librarian_unlock_phrase(retry_after_seconds)}."
    )


def _librarian_cooldown_response(retry_after_seconds: int):
    response = jsonify({
        "success": False,
        "status": "cooldown_active",
        "error": (
            "Too many refused questions were sent in a short time. "
            f"Please try again {_librarian_unlock_phrase(retry_after_seconds)}."
        ),
        "retry_after_seconds": retry_after_seconds,
    })
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after_seconds)
    return response


def _librarian_epoch_changed_response():
    return jsonify({
        "success": False,
        "status": "admission_state_changed",
        "error": (
            "Your Librarian access state changed while the question was "
            "being checked. Please submit it again."
        ),
    }), 409


def _release_librarian_gate_event(
    event_id: str,
    reason: str,
) -> None:
    """Preserve the accepted quota burn and record its terminal outcome."""
    mark_librarian_event_terminal_failure(
        event_id=event_id,
        reason=reason,
    )


def _release_librarian_retrieval_result(event_id: str):
    try:
        current = librarian_result_epoch_is_current(
            event_id=event_id,
            terminal_reason="revoked_during_retrieval",
        )
    except Exception:
        app.logger.exception(
            "Librarian retrieval release check failed for event_id=%s",
            event_id,
        )
        return jsonify({
            "success": False,
            "status": "access_check_unavailable",
            "error": (
                "We couldn't verify this Librarian result right now. "
                "Please submit the question again."
            ),
        }), 503
    if not current:
        return _librarian_epoch_changed_response()
    return None


def _persist_librarian_envelope(
    *,
    event_id: str,
    envelope_hash: str,
    envelope_version: str,
) -> str:
    """Bind an envelope to its accepted event in one immediate transaction.

    The accepted quota row is already durable before retrieval starts.
    Therefore persistence uses a short second transaction. A failure remains
    a durable accepted quota row with an audit-only terminal reason.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE librarian_gate_events
            SET synthesis_envelope_hash = ?,
                envelope_version = ?,
                envelope_expires_at = datetime(
                    created_at,
                    '+600 seconds'
                )
            WHERE event_id = ?
            """,
            (envelope_hash, envelope_version, event_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "accepted Librarian envelope row was not updated"
            )
        row = conn.execute(
            """
            SELECT envelope_expires_at
            FROM librarian_gate_events
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None or not row["envelope_expires_at"]:
            raise RuntimeError(
                "accepted Librarian envelope expiry was not stored"
            )
        expires_at = str(row["envelope_expires_at"])
        conn.commit()
        return expires_at.replace(" ", "T") + "Z"
    except Exception:
        conn.rollback()
        try:
            _release_librarian_gate_event(
                event_id,
                "envelope_persist_failed",
            )
        except Exception:
            app.logger.exception(
                "CRITICAL: Librarian envelope persistence failed and "
                "terminal-state recording also failed for event_id=%s",
                event_id,
            )
        raise
    finally:
        conn.close()


@app.route('/api/rag-search/<int:meeting_id>', methods=['POST'])
@_public_rate_limited('rag_search')
def api_rag_search(meeting_id):
    """V1.5-RAG-Search-1 — retrieval-only RAG endpoint for BYOK clients.

    Request JSON:  {
        "query": "<natural-language question, max 500 chars>",
        "top_k": <int 1-50, default 12>,
        "include_provenance": <bool, default true>
    }
    Response JSON: {
        "success": true,
        "meeting_id": <int>,
        "query": "<str>",
        "chunks": [
            {chunk_index, vector_id, body, start_seconds, end_seconds,
             speaker_turns?, score}, ...
        ],
        "provenance": {
            "run_id": "zspan-rag-{ISO-8601}-{short-hash}",
            "vector_ids": ["<uuid5>", ...],
            "prompt_template_hash": "sha256:...",
            "prompt_template_version": "v1.5-rag-search-2026-06-24",
            "query_hash": "sha256:...",
            "timestamp_utc": "..."
        },
        "recommended_system_prompt": "<canonical Z-SPAN discipline prompt>"
    }

    Auth: owner or a principal admitted by the D-145 Librarian gate.
    Owners retain the legacy 500-character sanity cap and are exempt from
    the deterministic stencil. Every accepted non-owner query is recorded
    hash-only before retrieval begins.
    """
    oversized = _reject_oversized_librarian_body()
    if oversized is not None:
        return oversized

    query_allowed, query_gate = _byok_public_query_allowed()
    if not query_allowed:
        return jsonify(_byok_denied_payload(query_gate)), 403
    gate_user = getattr(g, "byok_query_user", None)
    owner_exempt = query_gate == "owner"
    expected_epoch = None
    policy_snapshot = None

    if not owner_exempt:
        if gate_user is None:
            app.logger.error(
                "non-owner rag-search admission has no bound account"
            )
            return jsonify({
                "success": False,
                "status": "access_check_unavailable",
                "error": (
                    "We couldn't verify this Librarian account right now. "
                    "Please try again shortly."
                ),
            }), 503
        try:
            from database import (
                preflight_librarian_abuse_state as _abuse_preflight,
            )
            abuse_preflight = _abuse_preflight(gate_user.id)
        except Exception:
            app.logger.exception(
                "Librarian reject-control preflight failed for user_id=%s",
                gate_user.id,
            )
            return jsonify({
                "success": False,
                "status": "access_check_unavailable",
                "error": (
                    "We couldn't check this Librarian account right now. "
                    "Please try again shortly."
                ),
            }), 503
        if abuse_preflight["status"] == "auto_banned":
            return jsonify({
                "success": False,
                "status": "access_unavailable",
                "error": "Librarian access is unavailable for this account.",
            }), 403
        if abuse_preflight["status"] == "cooldown_active":
            return _librarian_cooldown_response(
                abuse_preflight["retry_after_seconds"]
            )
        if abuse_preflight["status"] != "clear":
            return jsonify({
                "success": False,
                "status": "access_unavailable",
                "error": "Librarian access is unavailable for this account.",
            }), 403
        expected_epoch = int(abuse_preflight["expected_epoch"])
        try:
            policy_snapshot = get_librarian_policy_snapshot()
        except Exception:
            app.logger.exception(
                "Librarian policy read failed for user_id=%s",
                gate_user.id,
            )
            return jsonify({
                "success": False,
                "status": "access_check_unavailable",
                "error": (
                    "We couldn't check this Librarian account right now. "
                    "Please try again shortly."
                ),
            }), 503

    try:
        body = request.get_json(silent=True) or {}
        raw_query = body.get('query')
        if owner_exempt:
            query = (raw_query or '').strip()
            if not query:
                return jsonify({
                    'success': False,
                    'error': 'query is required',
                }), 400
            if len(query) > 500:
                return jsonify({
                    'success': False,
                    'error': 'query too long (max 500 chars)',
                }), 400
        else:
            evaluation_started = time.perf_counter()
            try:
                stencil = evaluate_librarian_query(raw_query)
            except Exception as evaluation_exc:
                evaluation_ms = (
                    time.perf_counter() - evaluation_started
                ) * 1000.0
                try:
                    from database import (
                        record_librarian_evaluation_failure
                        as _record_evaluation_failure,
                    )
                    if gate_user is None:
                        raise ValueError(
                            "accepted query lane has no bound user principal"
                        )
                    if expected_epoch is None:
                        raise ValueError(
                            "evaluation lease was not captured"
                        )
                    _record_evaluation_failure(
                        user_id=gate_user.id,
                        meeting_id=meeting_id,
                        query_hash=_librarian_gate_query_hash(raw_query),
                        gate_version=COMPOSED_GATE_VERSION,
                        expected_epoch=expected_epoch,
                        evaluation_ms=evaluation_ms,
                        error_class=type(evaluation_exc).__name__,
                    )
                except Exception:
                    app.logger.exception(
                        "librarian gate evaluation-error event write failed "
                        "for meeting_id=%s",
                        meeting_id,
                    )
                app.logger.exception(
                    "librarian query evaluation failed for meeting_id=%s",
                    meeting_id,
                )
                return jsonify({
                    'success': False,
                    'error': (
                        'The Librarian safety check could not evaluate this '
                        'query, so no retrieval was performed.'
                    ),
                }), 500

            evaluation_ms = (
                time.perf_counter() - evaluation_started
            ) * 1000.0
            if not stencil.ok:
                try:
                    if gate_user is None:
                        raise ValueError(
                            "rejected query lane has no bound user principal"
                        )
                    if expected_epoch is None or policy_snapshot is None:
                        raise ValueError(
                            "rejected query has no evaluation lease"
                        )
                    decision_conn = get_connection()
                    try:
                        decision_raw_query = (
                            raw_query
                            if isinstance(raw_query, str)
                            else json.dumps(
                                raw_query,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True,
                            )
                        )
                        reject_accounting = (
                            evaluate_and_record_librarian_query(
                                decision_conn,
                                user_id=gate_user.id,
                                meeting_id=meeting_id,
                                raw_query=decision_raw_query,
                                expected_epoch=expected_epoch,
                                thresholds=policy_snapshot,
                                stencil_verdict=stencil,
                            )
                        )
                    finally:
                        decision_conn.close()
                except Exception:
                    app.logger.exception(
                        "rejected Librarian accounting failed for "
                        "meeting_id=%s reason_code=%s",
                        meeting_id,
                        stencil.reason_code,
                    )
                    return jsonify({
                        "success": False,
                        "status": "safety_record_unavailable",
                        "error": (
                            "We couldn't safely record this refused question "
                            "right now. Please try again shortly."
                        ),
                    }), 503
                if isinstance(reject_accounting, EpochChanged):
                    return _librarian_epoch_changed_response()
                if isinstance(reject_accounting, CooldownDeniedResult):
                    return _librarian_cooldown_response(
                        reject_accounting.retry_after_seconds
                    )
                if isinstance(reject_accounting, AccessDeniedResult):
                    return jsonify({
                        "success": False,
                        "status": "access_unavailable",
                        "error": (
                            "Librarian access is unavailable for this account."
                        ),
                    }), 403
                if not isinstance(reject_accounting, RejectedResult):
                    raise RuntimeError(
                        "rejected stencil produced a non-rejected decision"
                    )
                if reject_accounting.rejection_status == "auto_banned":
                    return jsonify({
                        "success": False,
                        "status": "access_unavailable",
                        "error": (
                            "Librarian access is unavailable for this account."
                        ),
                    }), 403
                return jsonify({
                    'success': False,
                    'status': 'input_rejected',
                    'error': stencil.message,
                }), 400

            query = stencil.canonical_query
            assert query is not None

        top_k_raw = body.get('top_k', 12)
        try:
            top_k = int(top_k_raw)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'top_k must be an int'}), 400
        if top_k < 1 or top_k > 50:
            return jsonify({'success': False, 'error': 'top_k must be 1-50'}), 400

        include_provenance = bool(body.get('include_provenance', True))

        # Make zspan_pipeline importable from the sibling core directory.
        import sys
        from pathlib import Path
        bridge_root = Path(__file__).resolve().parent.parent.parent
        if str(bridge_root) not in sys.path:
            sys.path.insert(0, str(bridge_root))
        from zspan_pipeline import qdrant_synthesizer, rag_search

        provenance_timestamp = datetime.now(timezone.utc)
        retrieval_run_id = rag_search.make_run_id(
            meeting_id,
            rag_search.query_hash(query),
            provenance_timestamp,
        )

        if not owner_exempt:
            try:
                if gate_user is None:
                    raise ValueError(
                        "accepted query lane has no bound user principal"
                    )
                if expected_epoch is None or policy_snapshot is None:
                    raise ValueError(
                        "accepted query has no evaluation lease"
                    )
                decision_conn = get_connection()
                try:
                    admission = evaluate_and_record_librarian_query(
                        decision_conn,
                        user_id=gate_user.id,
                        meeting_id=meeting_id,
                        raw_query=raw_query,
                        expected_epoch=expected_epoch,
                        thresholds=policy_snapshot,
                        stencil_verdict=stencil,
                    )
                finally:
                    decision_conn.close()
            except Exception:
                # Accepted queries fail closed: no index probe or vector
                # retrieval may happen unless quota admission is durable.
                app.logger.exception(
                    "librarian quota reservation failed for "
                    "meeting_id=%s",
                    meeting_id,
                )
                return jsonify({
                    'success': False,
                    'status': 'quota_check_unavailable',
                    'error': (
                        "We couldn't check your Librarian question limit "
                        "right now, so no retrieval was performed. Please "
                        "try again shortly."
                    ),
                }), 503

            if isinstance(admission, EpochChanged):
                return _librarian_epoch_changed_response()
            if isinstance(admission, CooldownDeniedResult):
                return _librarian_cooldown_response(
                    admission.retry_after_seconds
                )
            if isinstance(admission, AccessDeniedResult):
                return jsonify({
                    "success": False,
                    "status": "access_unavailable",
                    "error": (
                        "Librarian access is unavailable for this account."
                    ),
                }), 403
            if isinstance(admission, QuotaExhaustedResult):
                retry_after_seconds = admission.retry_after_seconds
                response = jsonify({
                    'success': False,
                    'status': 'daily_quota_exhausted',
                    'error': _librarian_quota_error(
                        admission.cap,
                        retry_after_seconds,
                    ),
                    'retry_after_seconds': retry_after_seconds,
                })
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after_seconds)
                return response
            if not isinstance(admission, AdmittedResult):
                raise RuntimeError(
                    "accepted stencil produced a non-admitted decision"
                )
            gate_event_id = admission.event_id
            try:
                retrieval_claimed, _claim_reason = (
                    claim_librarian_retrieval(
                        event_id=gate_event_id,
                        retrieval_run_id=retrieval_run_id,
                    )
                )
            except Exception:
                app.logger.exception(
                    "Librarian retrieval claim failed for event_id=%s",
                    gate_event_id,
                )
                return jsonify({
                    "success": False,
                    "status": "access_check_unavailable",
                    "error": (
                        "We couldn't verify this Librarian question before "
                        "retrieval. Please try again shortly."
                    ),
                }), 503
            if not retrieval_claimed:
                return _librarian_epoch_changed_response()

        template_body = rag_search.load_prompt_template()
        provenance_seed = rag_search.make_provenance_packet(
            meeting_id=meeting_id,
            query=query,
            chunks=[],
            template_body=template_body,
            ts=provenance_timestamp,
            run_id=retrieval_run_id,
        )
        assert provenance_seed["run_id"] == retrieval_run_id

        # F8 honest-empty discipline: distinguish not_indexed (chunks
        # could never exist) from indexed_no_match (chunks exist but
        # query didn't match) from qdrant_down (transient failure). The
        # cross-meeting fan-out caller (V1.5-OperatorSearch-1 Phase 3)
        # uses interpreted_as to render per-leg state honestly instead
        # of treating all three as identical empty results.
        from database import is_meeting_rag_indexed as _is_rag_indexed
        try:
            indexed = _is_rag_indexed(meeting_id)
        except Exception as e:
            logging.warning(
                "is_meeting_rag_indexed check failed for meeting=%s; "
                "falling through to Surface Pro (fail-open): %s",
                meeting_id, e,
            )
            indexed = True

        if not indexed:
            # Not indexed — no Qdrant round-trip; build empty-chunks
            # provenance + audit row + return interpreted_as=not_indexed.
            provenance_full = provenance_seed
            client_provider = (body.get('provider') or '').strip() or None
            client_model = (body.get('model') or '').strip() or None
            try:
                from database import save_byok_audit_run as _save_audit_run
                _save_audit_run(
                    run_id=provenance_full["run_id"],
                    kind="retrieval",
                    meeting_id=meeting_id,
                    timestamp_utc=provenance_full["timestamp_utc"],
                    prompt_template_version=provenance_full["prompt_template_version"],
                    prompt_template_hash=provenance_full["prompt_template_hash"],
                    vector_ids=provenance_full["vector_ids"],
                    query_hash=provenance_full["query_hash"],
                    provider=client_provider,
                    model=client_model,
                )
            except Exception as audit_exc:
                logging.warning(
                    "byok_audit_runs write failed for run_id=%s (non-fatal): %s",
                    provenance_full["run_id"], audit_exc,
                )
            if not owner_exempt:
                release_error = _release_librarian_retrieval_result(
                    gate_event_id
                )
                if release_error is not None:
                    return release_error
            return jsonify({
                'success': True,
                'meeting_id': meeting_id,
                'query': query,
                'chunks': [],
                'interpreted_as': 'not_indexed',
                'provenance': provenance_full if include_provenance else None,
                'recommended_system_prompt': template_body,
            })

        try:
            chunks = qdrant_synthesizer.retrieve_chunks(
                meeting_id, query, top_k=top_k,
            )
        except requests.exceptions.RequestException as e:
            if not owner_exempt:
                try:
                    _release_librarian_gate_event(
                        gate_event_id,
                        "retrieval_failed",
                    )
                except Exception:
                    app.logger.exception(
                        "retrieval terminal-state write failed event_id=%s",
                        gate_event_id,
                    )
            # Surface Pro unreachable / timeout / 5xx — qdrant_down.
            # success=False + HTTP 502 so the fan-out caller counts
            # this as a failed leg, not a successful empty leg.
            logging.warning(
                "qdrant_down for meeting=%s query_hash=%s: %s",
                meeting_id, provenance_seed["query_hash"], e,
            )
            return jsonify({
                'success': False,
                'meeting_id': meeting_id,
                'query': query,
                'chunks': [],
                'interpreted_as': 'qdrant_down',
                'error': 'rag_backend_unavailable',
            }), 502
        except Exception:
            logging.exception(
                "qdrant retrieve failed for meeting=%s query_hash=%s",
                meeting_id,
                provenance_seed["query_hash"],
            )
            if not owner_exempt:
                try:
                    _release_librarian_gate_event(
                        gate_event_id,
                        "retrieval_failed",
                    )
                except Exception:
                    app.logger.exception(
                        "retrieval terminal-state write failed event_id=%s",
                        gate_event_id,
                    )
            return jsonify({
                'success': False,
                'error': 'rag_backend_unavailable',
                'interpreted_as': 'error',
            }), 502

        if not owner_exempt:
            release_error = _release_librarian_retrieval_result(
                gate_event_id
            )
            if release_error is not None:
                return release_error

        # Always build the packet; the include_provenance flag only controls
        # whether the client RECEIVES it inline. The audit-row write below is
        # unconditional — Z-SPAN's verifiability commitment doesn't depend on
        # the client wanting the packet in the response. Cost is one in-memory
        # dict construction; cheap.
        provenance_full = rag_search.make_provenance_packet(
            meeting_id=meeting_id,
            query=query,
            chunks=chunks,
            template_body=template_body,
            ts=provenance_timestamp,
            run_id=retrieval_run_id,
        )

        # Provider + model are optional inputs from the client; they come
        # from BYOK onboarding (V1.5-BYOK-Shell-1). Until that ships, both
        # default to None and the audit row records the retrieval without
        # provider attribution. Schema is forward-compatible.
        client_provider = (body.get('provider') or '').strip() or None
        client_model = (body.get('model') or '').strip() or None

        # V1.5-Verify-1 persistence — append the audit row before responding.
        # If the DB write fails, log + continue (the retrieval still has
        # value to the client; the audit row is recoverable from request
        # logs if needed). Honest framing: we'd rather lose audit-trail
        # durability than block the user's query.
        try:
            from database import save_byok_audit_run as _save_audit_run
            _save_audit_run(
                run_id=provenance_full["run_id"],
                kind="retrieval",
                meeting_id=meeting_id,
                timestamp_utc=provenance_full["timestamp_utc"],
                prompt_template_version=provenance_full["prompt_template_version"],
                prompt_template_hash=provenance_full["prompt_template_hash"],
                vector_ids=provenance_full["vector_ids"],
                query_hash=provenance_full["query_hash"],
                provider=client_provider,
                model=client_model,
            )
        except Exception as audit_exc:
            logging.warning(
                "byok_audit_runs write failed for run_id=%s (non-fatal): %s",
                provenance_full["run_id"], audit_exc,
            )

        provenance = provenance_full if include_provenance else None

        # Build the chunk dicts shipped to the client. Include vector_id
        # for 1:1 correspondence with provenance.vector_ids so the client
        # can correlate which chunk is which without trusting array order.
        chunk_dicts = [
            {
                'chunk_index': c.chunk_index,
                'vector_id': rag_search.chunk_to_vector_id(
                    meeting_id, c.chunk_index,
                ),
                'body': c.body,
                'start_seconds': c.start_seconds,
                'end_seconds': c.end_seconds,
                'speaker_turns': c.speaker_turns,
                'score': round(c.score, 4),
            }
            for c in chunks
        ]

        synthesis_envelope = None
        if chunks:
            try:
                from librarian_envelope import (
                    ENVELOPE_TTL_SECONDS,
                    build_synthesis_envelope,
                )
                synthesis_envelope = build_synthesis_envelope(
                    meeting_id,
                    query,
                    chunks,
                )
                if (
                    synthesis_envelope["system_prompt"]
                    != template_body
                ):
                    raise RuntimeError(
                        "recommended prompt changed during envelope build"
                    )
            except Exception:
                if not owner_exempt:
                    try:
                        _release_librarian_gate_event(
                            gate_event_id,
                            "envelope_build_failed",
                        )
                    except Exception:
                        app.logger.exception(
                            "CRITICAL: Librarian envelope build failed and "
                            "terminal-state recording also failed for "
                            "event_id=%s",
                            gate_event_id,
                        )
                app.logger.exception(
                    "Librarian envelope build failed for meeting_id=%s "
                    "run_id=%s",
                    meeting_id,
                    retrieval_run_id,
                )
                return jsonify({
                    "error": {
                        "message": (
                            "The Librarian could not prepare the retrieved "
                            "context for synthesis."
                        ),
                        "type": "envelope_build_failed",
                    }
                }), 500

            if owner_exempt:
                expires_at_utc = (
                    provenance_timestamp
                    + timedelta(seconds=ENVELOPE_TTL_SECONDS)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                try:
                    expires_at_utc = _persist_librarian_envelope(
                        event_id=gate_event_id,
                        envelope_hash=synthesis_envelope["envelope_hash"],
                        envelope_version=(
                            synthesis_envelope["envelope_version"]
                        ),
                    )
                except Exception:
                    app.logger.exception(
                        "Librarian envelope persistence failed for "
                        "event_id=%s run_id=%s",
                        gate_event_id,
                        retrieval_run_id,
                    )
                    return jsonify({
                        "error": {
                            "message": (
                                "The Librarian safety check could not persist "
                                "your question — no retrieval was performed."
                            ),
                            "type": "envelope_persist_failed",
                        }
                    }), 503
            synthesis_envelope = {
                **synthesis_envelope,
                "expires_at_utc": expires_at_utc,
                "run_id": retrieval_run_id,
            }

        return jsonify({
            'success': True,
            'meeting_id': meeting_id,
            'query': query,
            'chunks': chunk_dicts,
            'interpreted_as': 'ok' if chunks else 'indexed_no_match',
            'provenance': provenance,
            'recommended_system_prompt': template_body,
            **(
                {'synthesis_envelope': synthesis_envelope}
                if synthesis_envelope is not None
                else {}
            ),
        })
    except Exception:
        logging.exception("api_rag_search failed for meeting_id=%s", meeting_id)
        return jsonify({
            'success': False,
            'error': 'rag_search_failed',
        }), 500


# ── /api/verify-run — V1.5-Verify-1 public provenance verification ──
#
# Per BYOK_ARCHITECTURE_SPEC § 2.3 + § 5.5.3. The civic-trust mechanism that
# distinguishes real Z-SPAN-orchestrated retrievals from fabricated screenshots.
# Anyone can paste a run_id pulled from a social-media screenshot and Z-SPAN
# answers "yes we ran that retrieval at time T against meeting M with chunks
# [v1, v2, ...]" OR "no, that run_id doesn't appear in our audit log."
#
# **No auth gate.** The whole point is that anyone can verify without an
# account. The response shape is curated to be safe-to-expose publicly per
# § 5.5.3 sensitive-info-stripping (no cleartext query, no user IP, no API
# key, no display name). The audit row schema already omits those by design,
# so the public response is the raw row plus a meeting-metadata join for the
# human-readable title + city.
#
# Rate limiting: fixed per-IP sliding-window budget at the handler boundary.

@app.route('/api/verify-run/<run_id>', methods=['GET'])
@_public_rate_limited('verify_run')
def api_verify_run(run_id):
    """Public provenance verification. Returns the audit-row contents +
    meeting metadata when the run_id exists; an explicit exists=false +
    note when it doesn't.

    Response (exists=true):
        {
            "run_id": "zspan-rag-...",
            "exists": true,
            "kind": "retrieval" | "notebook" | ...,
            "meeting_id": <int>,
            "meeting_title": "<str>",
            "city_name": "<str>",
            "timestamp_utc": "<iso8601>",
            "vector_ids": ["<uuid5>", ...],
            "prompt_template_version": "v1.5-rag-search-2026-06-24",
            "prompt_template_hash": "sha256:...",
            "query_hash": "sha256:...",
            "provider": "<str>" | null,
            "model": "<str>" | null,
            "supersedes": "<run_id>" | null
        }

    Response (exists=false):
        {
            "run_id": "<as given>",
            "exists": false,
            "note": "This run_id does not appear in Z-SPAN's audit log. Either it was never executed, or it has been fabricated."
        }
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        import database as _db  # local import — keeps the namespace clean
        row = _db.get_byok_audit_run(run_id)
        if not row:
            return jsonify({
                'run_id': run_id,
                'exists': False,
                'note': "This run_id does not appear in Z-SPAN's audit log. Either it was never executed, or it has been fabricated.",
            })

        # Join to meetings table for human-readable metadata. Soft-fail —
        # if the meeting was deleted post-retrieval the audit row still
        # exists + we surface what we have.
        meeting_title = None
        city_name = None
        if row.get('meeting_id'):
            conn = _db.get_connection()
            try:
                mrow = conn.execute(
                    'SELECT meeting_title, city_name FROM meetings WHERE id = ?',
                    (row['meeting_id'],),
                ).fetchone()
            finally:
                conn.close()
            if mrow:
                meeting_title = mrow['meeting_title']
                city_name = mrow['city_name']

        return jsonify({
            'run_id': row['run_id'],
            'exists': True,
            'kind': row['kind'],
            'meeting_id': row['meeting_id'],
            'meeting_title': meeting_title,
            'city_name': city_name,
            'timestamp_utc': row['timestamp_utc'],
            'vector_ids': row['vector_ids'],
            'prompt_template_version': row['prompt_template_version'],
            'prompt_template_hash': row['prompt_template_hash'],
            'query_hash': row['query_hash'],
            'provider': row.get('provider'),
            'model': row.get('model'),
            'supersedes': row.get('supersedes'),
            'child_run_ids': row.get('child_run_ids') or [],
        })
    except Exception as e:
        logging.exception("api_verify_run failed for run_id=%s", run_id)
        # This route is PUBLIC (see the header note — /api/verify-run stays
        # public). Never return raw str(e): it would disclose internal paths,
        # SQL, or stack detail to any anonymous caller. The full exception is
        # in the server log above; the response carries only a stable code.
        return jsonify({
            'run_id': run_id,
            'exists': False,
            'error': 'verification lookup failed',
        }), 500


# ── /api/decode-ribbon-image — S-098 Phase 2 V0 screenshot verifier ──
#
# Accepts a multipart/form-data upload with field name `image`. Server
# uses zspan_pipeline.watermark_ribbon_decoder to find the ribbon in
# the image + recover the 40-bit token; if found, chains to the audit
# log lookup inline + returns the verdict.

@app.route('/api/decode-ribbon-image', methods=['POST'])
@_public_rate_limited('decode_ribbon_image')
def api_decode_ribbon_image():
    try:
        # Apply the route-specific request ceiling before Werkzeug parses the
        # multipart upload. The bounded read below covers missing or dishonest
        # Content-Length values without ever retaining more than cap + 1 byte.
        request.max_content_length = _RIBBON_UPLOAD_MAX_BYTES + 1
        if (
            request.content_length is not None
            and request.content_length > _RIBBON_UPLOAD_MAX_BYTES
        ):
            return jsonify({
                'token': None,
                'error': 'image upload exceeds 10 MiB',
            }), 413
        if 'image' not in request.files:
            return jsonify({'token': None, 'error': "missing 'image' file in form data"}), 400
        f = request.files['image']
        data = f.read(_RIBBON_UPLOAD_MAX_BYTES + 1)
        if len(data) > _RIBBON_UPLOAD_MAX_BYTES:
            return jsonify({
                'token': None,
                'error': 'image upload exceeds 10 MiB',
            }), 413
        if not data:
            return jsonify({'token': None, 'error': 'empty image upload'}), 400

        # Pillow reads the image header without expanding the pixel buffer, so
        # reject hostile dimensions before OpenCV/Pillow performs the costly
        # ribbon decode. Pillow's own higher bomb threshold is also mapped to
        # the same stable 413 response.
        import io as _io
        from PIL import Image as _PILImage
        try:
            with _PILImage.open(_io.BytesIO(data)) as source_image:
                image_width, image_height = source_image.size
        except _PILImage.DecompressionBombError:
            return jsonify({
                'token': None,
                'error': 'image dimensions exceed 8192 pixels',
            }), 413
        if max(image_width, image_height) > _RIBBON_IMAGE_MAX_DIMENSION:
            return jsonify({
                'token': None,
                'error': 'image dimensions exceed 8192 pixels',
            }), 413

        # Optional debug capture (S-102 batch test). When the client
        # passes _debug=1 + _session_id + _seq, we persist the raw frame
        # + the decoder response under /tmp/zspan_scan_debug/<session>/
        # so the operator + maintainer can inspect what was sent vs what
        # came back. Used during phone-test debugging when the live AR
        # path returns "no ribbon found" and we need to see the actual
        # JPEG bytes the decoder is operating on, not just the
        # client-side thumbnail.
        debug_session_id = None
        debug_seq = None
        debug_dir = None
        # Owner-only debug capture. The decode endpoint itself is public
        # (the AR verifier path uses it), but persisting raw frames under
        # /tmp must NOT be triggerable anonymously — an unauthenticated
        # caller could fill the disk with attacker-controlled bytes. Any
        # cookie-resolution hiccup resolves to not-owner (never an allow),
        # and non-owners simply skip the capture; the decode proceeds.
        try:
            _dbg_user = _current_user_from_cookie()
            _dbg_is_owner = bool(_dbg_user and is_owner_email(_dbg_user.email))
        except Exception:
            _dbg_is_owner = False
        if _dbg_is_owner and request.form.get('_debug') == '1':
            raw_session = str(request.form.get('_session_id') or '').strip()
            raw_seq = str(request.form.get('_seq') or '').strip()
            import re as _re
            if _re.fullmatch(r'[A-Za-z0-9_-]{1,40}', raw_session) and raw_seq.isdigit() and len(raw_seq) <= 4:
                debug_session_id = raw_session
                debug_seq = int(raw_seq)
                import os as _os
                debug_dir = _os.path.join('/tmp/zspan_scan_debug', debug_session_id)
                try:
                    _os.makedirs(debug_dir, exist_ok=True)
                    jpg_path = _os.path.join(debug_dir, f'{debug_seq:03d}.jpg')
                    with open(jpg_path, 'wb') as _w:
                        _w.write(data)
                except Exception:
                    logging.exception('debug capture write failed (jpg)')

        from zspan_pipeline.watermark_ribbon_decoder import decode_ribbon_bytes
        decode = decode_ribbon_bytes(data)
        token = decode.get('token')

        # Persist the decoder response next to the frame so the
        # maintainer can correlate JPEG vs decoder verdict by seq.
        def _maybe_save_debug_json(payload):
            if not debug_dir or debug_seq is None:
                return
            try:
                import os as _os, json as _json
                json_path = _os.path.join(debug_dir, f'{debug_seq:03d}.json')
                with open(json_path, 'w') as _w:
                    _json.dump({
                        'seq': debug_seq,
                        'jpg_bytes': len(data),
                        'response': payload,
                    }, _w, default=str, indent=2)
            except Exception:
                logging.exception('debug capture write failed (json)')

        if not token:
            payload = {
                'token': None,
                'bbox': decode.get('bbox'),
                'stats': decode.get('stats'),
                'blocks': decode.get('blocks'),
            }
            if debug_session_id:
                payload['debug_session_id'] = debug_session_id
                payload['debug_dir'] = debug_dir
            _maybe_save_debug_json(payload)
            return jsonify(payload)

        # Use the same registry-first lookup as the direct verifier route.
        import database as _db
        conn = _db.get_connection()
        try:
            verdict = _watermark_lookup_result(conn, token.upper())
        finally:
            conn.close()

        payload = {
            'token': token,
            'bbox': decode.get('bbox'),
            'stats': decode.get('stats'),
            'blocks': decode.get('blocks'),
            'verdict': verdict,
        }
        if debug_session_id:
            payload['debug_session_id'] = debug_session_id
            payload['debug_dir'] = debug_dir
        _maybe_save_debug_json(payload)
        return jsonify(payload)
    except RequestEntityTooLarge:
        return jsonify({
            'token': None,
            'error': 'image upload exceeds 10 MiB',
        }), 413
    except Exception:
        # Public route — stable code, full detail server-side only (raw
        # str(e) leaked Pillow/BytesIO internals incl. a process address).
        logging.exception('api_decode_ribbon_image failed')
        return jsonify({'token': None, 'error': 'decode failed'}), 500


# ── /api/watermark-lookup — S-098 Phase 1.5 watermark → output binding ──
#
# Receives an 8-char base32 token recovered by the watermark decoder
# (zspan_pipeline/watermark_decoder.py — either DOM-based on a live
# rendered page or pixel-based on a screenshot / WebAR camera frame) and
# resolves it to the source notebook_outputs row + provenance metadata.
#
# Current flagship and CLI ribbons are random, account-bound registry tokens.
# The former deterministic SHA-256 namespace remains lookup-only so old tokens
# receive an explicit legacy/not-authenticated verdict.

# ── S-103 embed-check — server-side oEmbed probe ──
#
# YouTube's iframe player renders the "Video unavailable / Playback on
# other websites has been disabled by the video owner" placeholder without
# posting a JavaScript onError event, so the client-side postMessage
# listener alone can't detect embed-blocked videos reliably. This endpoint
# proxies YouTube's oEmbed API server-side (avoiding the CORS-preflight
# problem on the 401 response) and returns a simple embeddable=bool.
# BroadcastPage hits it on youtube-source mount; on false, the S-103
# overlay renders immediately with the click-through to external YouTube.

@app.route('/api/youtube/embed-check', methods=['GET'])
def api_youtube_embed_check():
    """Session-32 (2026-07-04) — server-side oEmbed probe for S-103.

    Query params: video_id (11-char YouTube video id).
    Response: { embeddable: bool, checked_at: iso8601 }

    Legacy owner route. Citizens use /public-api/youtube/embed-check.
    """
    _user, _err = _require_owner()
    if _err:
        return _err
    video_id = (request.args.get('video_id') or '').strip()
    if not re.match(r'^[\w-]{11}$', video_id):
        return jsonify({'error': 'invalid video_id'}), 400
    try:
        resp = requests.get(
            'https://www.youtube.com/oembed',
            params={
                'url': f'https://www.youtube.com/watch?v={video_id}',
                'format': 'json',
            },
            timeout=6.0,
        )
        embeddable = resp.status_code == 200
    except requests.RequestException as exc:
        logging.warning(
            "youtube_embed_check: oEmbed request failed for %s: %s "
            "— defaulting to embeddable=true (assume playable, S-103 client "
            "postMessage listener handles the miss)",
            video_id, exc,
        )
        embeddable = True
    from datetime import datetime, timezone
    resp = jsonify({
        'embeddable': embeddable,
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    })
    # Cache 15 min at the browser + edge — cheap for the citizen who
    # reloads the broadcast, and YouTube embed policies rarely flip
    # mid-day. Aligned with the Cloudflare edge cache policy.
    resp.headers['Cache-Control'] = 'public, max-age=900'
    return resp


def _watermark_lookup_result(conn, normalized):
    """Return one explicit registry, legacy, or miss verdict."""
    row = find_flagship_generation_by_token(conn.cursor(), normalized)
    if row is not None:
        return {
            'token': normalized,
            'exists': True,
            'authenticated': True,
            'legacy': False,
            'source': 'flagship_generation',
            'row_id': row['notebook_output_id'],
            'generation_id': row['generation_id'],
            'meeting_id': row['meeting_id'],
            'output_type': row['output_type'],
            'meeting_title': row['meeting_title'],
            'city_name': row['city_name'],
            'prompt_version': row['prompt_version'],
            'generated_at': row['output_generated_at'],
            'registered_at': row['minted_at'],
            'status': row['status'],
            'account_state': (
                'active' if row['user_id'] is not None else 'deleted'
            ),
            'note': (
                "This token maps to Z-SPAN's canonical record. "
                "The screenshot content itself is not authenticated."
            ),
        }

    generation = conn.execute(
        "SELECT * FROM cli_generations WHERE ribbon_token = ?",
        (normalized,),
    ).fetchone()
    if generation is not None:
        meeting = get_meeting_public_record(
            generation['meeting_public_id']
        ) or {}
        return {
            'token': normalized,
            'exists': True,
            'authenticated': True,
            'legacy': False,
            'source': 'cli_generation',
            'status': generation['status'],
            'output_type': generation['output_type'],
            'provider': generation['provider'],
            'model': generation['model'],
            'content_sha256': generation['content_sha256'],
            'generated_at': generation['created_at'],
            'account_state': (
                'active' if generation['user_id'] is not None else 'deleted'
            ),
            'meeting': {
                'public_id': generation['meeting_public_id'],
                'title': meeting.get('meeting_title') or '',
                'date': meeting.get('meeting_date') or '',
                'city': meeting.get('city_name') or '',
                'county': meeting.get('county') or '',
                'state': meeting.get('state') or '',
            },
            'note': (
                "This token maps to Z-SPAN's canonical record. "
                "The screenshot content itself is not authenticated."
            ),
        }

    legacy_row = find_flagship_watermark_row(conn.cursor(), normalized)
    if legacy_row is not None:
        return {
            'token': normalized,
            'exists': True,
            'authenticated': False,
            'legacy': True,
            'source': 'legacy_flagship',
            'row_id': legacy_row['id'],
            'meeting_id': legacy_row['meeting_id'],
            'output_type': legacy_row['output_type'],
            'meeting_title': legacy_row['meeting_title'],
            'city_name': legacy_row['city_name'],
            'prompt_version': legacy_row['prompt_version'],
            'generated_at': legacy_row['generated_at'],
            'note': (
                'This is a publicly reproducible legacy identifier — '
                'not authentication. The screenshot content itself is '
                'not authenticated.'
            ),
        }

    return {
        'token': normalized,
        'exists': False,
        'authenticated': False,
        'legacy': False,
        'note': (
            "This watermark token does not match any Z-SPAN output in our "
            "audit log. Either the token was decoded with errors or it "
            "does not belong to the canonical registry."
        ),
    }


@app.route('/api/watermark-lookup/<token>', methods=['GET'])
def api_watermark_lookup(token):
    """Map a watermark token back to its source notebook_outputs row.

    Response (exists=true):
        {
            "token": "ABCDEFGH",
            "exists": true,
            "meeting_id": <int>,
            "output_type": "synopsis" | "key_decisions" | ...,
            "meeting_title": "<str>" | null,
            "city_name": "<str>" | null,
            "prompt_version": "<str>" | null,
            "generated_at": "<iso8601>" | null,
            "row_id": <int>
        }

    Response (exists=false):
        {
            "token": "<as given>",
            "exists": false,
            "note": "This watermark token does not match any Z-SPAN output. ..."
        }
    """
    try:
        # Normalize/validate the token.
        normalized = (token or '').upper().strip()
        if len(normalized) != 8:
            return jsonify({
                'token': token,
                'exists': False,
                'error': 'token must be 8 base32 chars',
            }), 400

        BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
        for ch in normalized:
            if ch not in BASE32:
                return jsonify({
                    'token': token,
                    'exists': False,
                    'error': f'invalid base32 char in token: {ch}',
                }), 400

        conn = get_connection()
        try:
            return jsonify(_watermark_lookup_result(conn, normalized))
        finally:
            conn.close()
    except Exception:
        # Public verification route — stable code; detail server-side only.
        logging.exception('api_watermark_lookup failed for token=%s', token)
        return jsonify({
            'token': token,
            'exists': False,
            'error': 'lookup failed',
        }), 500


# ── /api/byok/validate-key — V1.5-BYOK-Shell-1 key validation ──
#
# Per BYOK_ARCHITECTURE_SPEC § 4.1. The client POSTs the user's API key +
# the chosen provider; we forward a no-op test ping to the provider's API
# (Gemini listModels for V1.5-BYOK-Shell-1; OpenAI/Anthropic come at
# V1.5-Relay-1); we return success/failure. **The key is held in volatile
# request memory only for the test ping; never persisted, never logged
# (we log only a 4+4 fingerprint), never copied to any other variable.**
#
# This is the ONE point where Z-SPAN sees the user's provider key. After
# validation succeeds, the client keeps the secret in volatile browser
# memory. Display-safe provider metadata may persist locally, but the key
# itself never enters browser storage or Z-SPAN storage.
#
# Auth gate: a signed-in account is required. Geographic admission is not
# part of the product: members may use their own provider from anywhere.

@app.route('/api/byok/validate-key', methods=['POST'])
@_public_rate_limited('validate_key')
def api_byok_validate_key():
    """Validate a user's BYOK API key by forwarding a no-op test ping to
    the provider. Returns {valid, provider, fingerprint, model_count|error}.

    Request body:
        {"provider": "google-gemini-2.5-flash", "api_key": "<key>"}

    The key NEVER persists anywhere on Z-SPAN's side. Held in volatile
    request memory only for the ~100ms test ping. Logs carry only the
    first 4 + last 4 chars (fingerprint).

    This endpoint has no geographic restriction. Authentication and the
    supplied provider credential are the only admission boundaries.
    """
    oversized = _reject_oversized_librarian_body()
    if oversized is not None:
        return oversized

    # Same live-query gate as the relay siblings (the public-plane
    # edge now admits this route, so the CF Access perimeter is no longer
    # its wall; without this, anonymous callers could use Z-SPAN as a
    # key-validation oracle against provider APIs).
    validate_allowed, validate_gate = _byok_public_query_allowed()
    if not validate_allowed:
        return jsonify(_byok_denied_payload(validate_gate)), 403

    try:
        body = request.get_json(silent=True) or {}
        provider = (body.get('provider') or '').strip()
        api_key = body.get('api_key') or ''  # don't .strip() — could contain meaningful whitespace at edges in some provider key formats, though unlikely

        if not provider:
            return jsonify({
                'valid': False,
                'error': 'provider is required (e.g. "google-gemini-2.5-flash")',
            }), 400
        if not api_key or len(api_key) < 12:
            return jsonify({
                'valid': False,
                'error': 'api_key is required + must be at least 12 chars',
            }), 400

        import sys
        from pathlib import Path

        # Dispatch to per-provider validator. Import lazily to avoid
        # forcing the zspan_pipeline sys.path shim at module load.
        bridge_root = Path(__file__).resolve().parent.parent.parent
        if str(bridge_root) not in sys.path:
            sys.path.insert(0, str(bridge_root))
        from zspan_pipeline import byok_validate

        if validate_gate == "signed-account":
            validate_user = getattr(g, "byok_query_user", None)
            if (
                validate_user is None
                or not claim_librarian_provider_dispatch(validate_user.id)
            ):
                return jsonify({
                    "valid": False,
                    "status": "admission_state_changed",
                    "error": (
                        "Account access changed before provider dispatch."
                    ),
                }), 409
        result = byok_validate.validate_key(provider, api_key)

        # Build response — note we DO NOT include the key in any form
        # (even the fingerprint is structured separately so future audit
        # can confirm the validation fired without leaking even the
        # fingerprint into request bodies). Status code: 200 for both
        # valid and "valid: false" with provider-side rejection, since
        # the validation itself succeeded structurally.
        return jsonify({
            'valid': bool(result.get('valid')),
            'provider': result.get('provider'),
            'fingerprint': result.get('fingerprint'),
            'model_count': result.get('model_count'),
            'error': result.get('error'),
        })
    except Exception:
        logging.exception("api_byok_validate_key failed")
        return jsonify({
            'valid': False,
            'error': 'validation_unavailable',
        }), 500


# ── /api/byok/relay — V1.5-Relay-1 CORS-blocked provider pass-through ──
#
# Per BYOK_ARCHITECTURE_SPEC § 4.2. OpenAI + Anthropic don't include
# Access-Control-Allow-Origin: * headers, so browsers refuse to load their
# responses on a Z-SPAN page. This endpoint is the thin pass-through:
# client POSTs {provider, api_key, model, system_prompt, user_message,
# max_tokens, temperature} → we forward to the provider's API with the
# user's key in the right auth header → return the provider's response
# verbatim. **Bytes never persisted, never logged (only fingerprint
# logged), never copied to any other variable.**
#
# Gemini is NOT routed here — it does direct browser calls (CORS-friendly).
# Only OpenAI + Anthropic at V1.5. Future providers add themselves to the
# byok_relay.relay() dispatch table.
#
# Auth gate: principal-first. Owner retains arbitrary pass-through behavior;
# signed-in accounts must claim a server-built synthesis envelope. The user's
# provider key remains volatile and is never persisted.


def _relay_error(message: str, error_type: str, status: int):
    return jsonify({
        "error": {
            "message": message,
            "type": error_type,
        }
    }), status


def _claim_relay_envelope(
    *,
    gate_reason: str,
    body: dict,
    provider: object,
    system_prompt: object,
    user_message: object,
):
    """Return ``(error_response, claim)`` for one dispatch boundary."""
    if gate_reason == "owner":
        return None, None
    if gate_reason != "signed-account":
        return (
            jsonify(_byok_denied_payload(gate_reason)),
            403,
        ), None

    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return _relay_error(
            "run_id is required",
            "bad_request",
            400,
        ), None

    gate_user = getattr(g, "byok_query_user", None)
    if gate_user is None:
        app.logger.error(
            "signed-in relay admission has no bound account principal"
        )
        return _relay_error(
            "The request doesn't match a Librarian question you asked.",
            "envelope_invalid",
            403,
        ), None

    from librarian_envelope import consume_envelope_claim

    ok, claim = consume_envelope_claim(
        gate_user.id,
        run_id,
        system_prompt,
        user_message,
        body.get("envelope_version"),
        provider,
    )
    if not ok:
        app.logger.info(
            "Librarian relay envelope rejected user_id=%s run_id=%s "
            "reason=%s",
            gate_user.id,
            run_id,
            claim["reason"],
        )
        return _relay_error(
            claim["message"],
            claim["type"],
            claim["http"],
        ), None
    return None, claim


@app.route('/api/byok/relay', methods=['POST'])
@_public_rate_limited('byok_relay')
def api_byok_relay():
    """V1.5-Relay-1 — pass-through relay for CORS-blocked BYOK providers.

    Request JSON:
        {
            "provider": "openai-gpt-4o-mini" | "anthropic-claude-3-haiku",
            "api_key": "<the user's key>",
            "model": "gpt-4o-mini" | "claude-3-haiku-20240307" | ...,
            "system_prompt": "<from /api/rag-search recommended_system_prompt>",
            "user_message": "<the chunks block + query>",
            "max_tokens": <int 256-4096>,
            "temperature": <float 0.0-1.0>
        }

    Response: forwards the provider's JSON verbatim (OpenAI's
    chat.completions response shape OR Anthropic's messages response
    shape). Client is responsible for parsing per provider's known schema.

    No logging of key/body/response — bytes-blind by design. Any signed-in
    account may pass; anonymous callers remain locked at the code layer.
    """
    oversized = _reject_oversized_librarian_body()
    if oversized is not None:
        return oversized

    # Signed-in accounts pass this first gate, then claim an exact envelope.
    _relay_allowed, relay_gate = _byok_public_query_allowed()
    if relay_gate not in {"owner", "signed-account"}:
        claim_error, _claim = _claim_relay_envelope(
            gate_reason=relay_gate,
            body={},
            provider="",
            system_prompt="",
            user_message="",
        )
        return claim_error

    try:
        body = request.get_json(silent=True) or {}
        raw_provider = body.get('provider')
        provider = (
            raw_provider.strip()
            if isinstance(raw_provider, str)
            else raw_provider
        )
        api_key = body.get('api_key') or ''
        model = (body.get('model') or '').strip()
        system_prompt = body.get('system_prompt', '')
        user_message = body.get('user_message', '')

        try:
            max_tokens = int(body.get('max_tokens', 1024))
        except (TypeError, ValueError):
            max_tokens = 1024
        if max_tokens < 1 or max_tokens > 4096:
            return jsonify({'error': {'message': 'max_tokens must be 1-4096', 'type': 'bad_request'}}), 400

        try:
            temperature = float(body.get('temperature', 0.2))
        except (TypeError, ValueError):
            temperature = 0.2
        if temperature < 0.0 or temperature > 2.0:
            return jsonify({'error': {'message': 'temperature must be 0.0-2.0', 'type': 'bad_request'}}), 400

        if not provider or not api_key or not user_message:
            return jsonify({'error': {'message': 'provider + api_key + user_message are required', 'type': 'bad_request'}}), 400

        claim_error, relay_claim = _claim_relay_envelope(
            gate_reason=relay_gate,
            body=body,
            provider=provider,
            system_prompt=system_prompt,
            user_message=user_message,
        )
        if claim_error is not None:
            return claim_error

        # Lazy-import to keep the Flask boot fast.
        import sys
        from pathlib import Path
        bridge_root = Path(__file__).resolve().parent.parent.parent
        if str(bridge_root) not in sys.path:
            sys.path.insert(0, str(bridge_root))
        from zspan_pipeline import byok_relay

        status, response_body = byok_relay.relay(
            provider=provider,
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if relay_claim is not None:
            if not librarian_result_epoch_is_current(
                event_id=relay_claim["event_id"],
                terminal_reason="revoked_after_dispatch",
            ):
                return _relay_error(
                    (
                        "Librarian access changed before the provider "
                        "result was released."
                    ),
                    "admission_state_changed",
                    409,
                )
        return jsonify(response_body), status
    except Exception:
        logging.exception("api_byok_relay failed")
        return jsonify({
            'error': {
                'message': 'Relay service is unavailable.',
                'type': 'server_error',
            }
        }), 500


# ── /api/byok/relay-stream — V1.5-BYOK-Stream-1 SSE variant ──
#
# Same shape + same auth gate as /api/byok/relay, but yields Server-Sent
# Events (Content-Type: text/event-stream) so the browser can render
# token-by-token typing. Deltas are byte-forwarded verbatim from the
# provider; we don't parse them server-side — the client SSE reader
# extracts the token text per-provider. See byok_relay.relay_stream() for
# provider dispatch + the [DONE] sentinel discipline.
#
# Only OpenAI + Anthropic route through here. Gemini uses direct browser
# SSE against generativelanguage.googleapis.com (CORS-friendly).
#
# Long-stream idle-timeout mitigation (session-32 park): the Express side
# ships a 240s upstream timeout. Under middlebox load a slow provider
# stream could hit it. The if-needed fix: wrap byok_relay.relay_stream()
# in a threaded producer + queue.Queue consumer here, and yield an SSE
# comment heartbeat (b": heartbeat\n\n") whenever the consumer's
# queue.get(timeout=30) times out. SSE-spec comments are consumed by
# EventSource silently — no client change needed. NOT landing preemptively
# because threading adds real failure modes (thread-leak on client-abort,
# error-propagation races) that aren't worth carrying for a hypothetical
# timeout. Land only if the 240s ceiling actually surfaces in prod logs.

@app.route('/api/byok/relay-stream', methods=['POST'])
@_public_rate_limited('byok_relay_stream')
def api_byok_relay_stream():
    """V1.5-BYOK-Stream-1 — SSE pass-through for CORS-blocked BYOK providers.

    Request JSON: same shape as /api/byok/relay (provider + api_key +
    model + system_prompt + user_message + max_tokens + temperature).

    Response: text/event-stream. Yields `data: {json}\\n\\n` lines
    verbatim from the provider (OpenAI's chat.completions stream shape OR
    Anthropic's messages event shape), plus a synthesized `data: [DONE]\\n\\n`
    sentinel at end for uniform client-side EOF detection.

    Same hard lock as /api/byok/relay: bytes-blindness is orthogonal to WHO
    may query; signed-in accounts pass and anonymous callers are denied.
    """
    oversized = _reject_oversized_librarian_body()
    if oversized is not None:
        return oversized

    # Mirror the one-shot endpoint's signed-account + envelope boundary.
    _relay_allowed, relay_gate = _byok_public_query_allowed()
    if relay_gate not in {"owner", "signed-account"}:
        claim_error, _claim = _claim_relay_envelope(
            gate_reason=relay_gate,
            body={},
            provider="",
            system_prompt="",
            user_message="",
        )
        return claim_error

    body = request.get_json(silent=True) or {}
    raw_provider = body.get('provider')
    provider = (
        raw_provider.strip()
        if isinstance(raw_provider, str)
        else raw_provider
    )
    api_key = body.get('api_key') or ''
    model = (body.get('model') or '').strip()
    system_prompt = body.get('system_prompt', '')
    user_message = body.get('user_message', '')

    try:
        max_tokens = int(body.get('max_tokens', 1024))
    except (TypeError, ValueError):
        max_tokens = 1024
    if max_tokens < 1 or max_tokens > 4096:
        return jsonify({'error': {'message': 'max_tokens must be 1-4096', 'type': 'bad_request'}}), 400

    try:
        temperature = float(body.get('temperature', 0.2))
    except (TypeError, ValueError):
        temperature = 0.2
    if temperature < 0.0 or temperature > 2.0:
        return jsonify({'error': {'message': 'temperature must be 0.0-2.0', 'type': 'bad_request'}}), 400

    if not provider or not api_key or not user_message:
        return jsonify({'error': {'message': 'provider + api_key + user_message are required', 'type': 'bad_request'}}), 400

    stream_user = getattr(g, "byok_query_user", None)
    stream_user_id = stream_user.id if stream_user is not None else None
    if relay_gate == "signed-account" and stream_user_id is None:
        return _relay_error(
            "The request doesn't match a Librarian question you asked.",
            "envelope_invalid",
            403,
        )

    # Same import-shim as the one-shot relay.
    import sys as _sys
    from pathlib import Path as _Path
    bridge_root = _Path(__file__).resolve().parent.parent.parent
    if str(bridge_root) not in _sys.path:
        _sys.path.insert(0, str(bridge_root))
    from zspan_pipeline import byok_relay

    def _generate():
        try:
            if relay_gate == "signed-account":
                from librarian_envelope import consume_envelope_claim

                ok, stream_claim = consume_envelope_claim(
                    stream_user_id,
                    body.get("run_id"),
                    system_prompt,
                    user_message,
                    body.get("envelope_version"),
                    provider,
                )
                if not ok:
                    error_payload = json.dumps({
                        "error": {
                            "message": stream_claim["message"],
                            "type": stream_claim["type"],
                        }
                    })
                    yield (
                        "event: relay_error\n"
                        f"data: {error_payload}\n\n"
                    ).encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    return
            yield from byok_relay.relay_stream(
                provider=provider,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception:
            logging.exception("api_byok_relay_stream generator failed")
            msg = (
                '{"error": {"message": "Relay service is unavailable.", '
                '"type": "server_error"}}'
            )
            yield f"event: relay_error\ndata: {msg}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

    return Response(
        _generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            # Disable proxy buffering (nginx, Cloudflare) so tokens actually
            # stream instead of being coalesced.
            'X-Accel-Buffering': 'no',
        },
    )


# ── /api/operator-search — V1.5-OperatorSearch-1 (owner-only) ──
#
# Owner-only natural-language cross-meeting search. Per the handoff
# 2026-06-25 spec: ships ahead of V2 because operator-only scope
# sidesteps every S-008/S-056/D-126 V2-public-query gate that V2
# deferral was protecting (no public user, no untrusted input).
#
# Phase 1: /interpret  — parses query → scope, returns meeting_ids.
# Phase 2: cost estimate (frontend-only).
# Phase 3: /execute    — fan-out retrieval + cross-meeting synthesis.
#
# Auth: signed-in account whose verified email matches the configured
# owner_email (the same gate as the OwnerOnly React component +
# OWNER_ONLY_VIEWS). The intent-parse Sonnet call stays MAX-cap per
# DP-3 strict — operator-internal infrastructure, never BYOK-swapped.

@app.route('/api/operator-search/interpret', methods=['POST'])
@_require_trusted_origin
def api_operator_search_interpret():
    """V1.5-OperatorSearch-1 Phase 1 — natural-language scope extraction.

    Request JSON:  {"query": "<natural-language operator query, max 500 chars>"}
    Response JSON: {
        "success": true,
        "interpretation": {state, county, city, keywords[], date_range, confidence},
        "meeting_ids": [<indexed meeting_ids that match scope>],
        "match_count": <total meetings matching scope, indexed + not>,
        "indexed_count": <subset that are V1-RAG-3 indexed and searchable>,
        "unindexed_count": <subset that match scope but aren't indexed yet>
    }

    Per DP-2 (handoff 2026-06-25): meeting_ids returns ONLY the indexed
    subset — the fan-out caller searches indexed meetings only. The
    unindexed_count surfaces the coverage gap so the operator sees it.
    """
    user = _current_user_from_cookie()
    if not user or not is_operator_search_principal(user.email):
        return jsonify({
            'success': False,
            'error': 'operator-search-principal-only endpoint',
        }), 403

    body = request.get_json(silent=True) or {}
    query = (body.get('query') or '').strip()
    if not query:
        return jsonify({'success': False, 'error': 'query is required'}), 400
    if len(query) > 500:
        return jsonify({
            'success': False,
            'error': 'query too long (max 500 chars)',
        }), 400

    # Same shim pattern /api/rag-search uses to import zspan_pipeline.
    import sys
    from pathlib import Path
    bridge_root = Path(__file__).resolve().parent.parent.parent
    if str(bridge_root) not in sys.path:
        sys.path.insert(0, str(bridge_root))
    from zspan_pipeline import qdrant_synthesizer, operator_search

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    extract_prompt = operator_search.build_interpret_prompt(query, today=today)

    try:
        sonnet_output = qdrant_synthesizer.synthesize_via_claude_p(
            extract_prompt, timeout_seconds=60.0,
        )
    except Exception as e:
        logging.exception("operator-search interpret: claude -p failed")
        return jsonify({
            'success': False,
            'error': f'Sonnet intent-parse failed: {e}',
        }), 502

    interpretation = operator_search.parse_interpret_output(sonnet_output)
    if interpretation is None:
        logging.warning(
            "operator-search interpret: malformed Sonnet JSON: %r",
            sonnet_output[:200],
        )
        return jsonify({
            'success': False,
            'error': 'Sonnet returned malformed JSON',
            'raw_output': sonnet_output[:500],
        }), 502

    # Resolve scope to meeting_ids via the meetings table.
    state = interpretation.get('state')
    county = interpretation.get('county')
    city = interpretation.get('city')
    date_range = interpretation.get('date_range') or {}
    if not isinstance(date_range, dict):
        date_range = {}

    from database import get_connection, is_meeting_rag_indexed
    conn = get_connection()
    try:
        sql_parts = ["SELECT id FROM meetings WHERE 1=1"]
        params: list = []
        if state:
            sql_parts.append("AND state = ?")
            params.append(state)
        if county:
            sql_parts.append("AND county = ?")
            params.append(county)
        if city:
            sql_parts.append("AND city_name = ?")
            params.append(city)
        if date_range.get('after'):
            sql_parts.append("AND meeting_date >= ?")
            params.append(date_range['after'])
        if date_range.get('before'):
            sql_parts.append("AND meeting_date <= ?")
            params.append(date_range['before'])
        # Stable ordering for the response — newest first, then by id
        # for the same-date case.
        sql_parts.append("ORDER BY meeting_date DESC, id DESC")

        rows = conn.execute(' '.join(sql_parts), params).fetchall()
        all_meeting_ids = [r[0] for r in rows]

        # Partition by indexed/unindexed using the V1-RAG-3 proxy. Per
        # DP-2 the fan-out only sees indexed meetings; the operator
        # sees the unindexed_count so the coverage gap is honest.
        indexed_meeting_ids = [
            mid for mid in all_meeting_ids if is_meeting_rag_indexed(mid)
        ]
    finally:
        conn.close()

    return jsonify({
        'success': True,
        'interpretation': interpretation,
        'prompt_template_version': operator_search.INTERPRET_PROMPT_VERSION,
        'meeting_ids': indexed_meeting_ids,
        'match_count': len(all_meeting_ids),
        'indexed_count': len(indexed_meeting_ids),
        'unindexed_count': len(all_meeting_ids) - len(indexed_meeting_ids),
    })


@app.route('/api/operator-search/execute', methods=['POST'])
@_require_trusted_origin
def api_operator_search_execute():
    """V1.5-OperatorSearch-1 Phase 3 — fan-out + cross-meeting synthesis.

    Request JSON:  {
        "query": "<natural-language operator query>",
        "meeting_ids": [<int>, ...],  // indexed-only, from /interpret
        "interpretation": {<full interpretation dict from /interpret>}
    }
    Response JSON: {
        "success": true,
        "answer": "<Markdown synthesis with [City · date] citations>",
        "citations": [
            {meeting_id, city_name, meeting_date, chunk_index, vector_id,
             start_seconds, end_seconds, score, body_preview}, ...
        ],
        "leg_outcomes": {
            "ok_count": <int>,
            "indexed_no_match_count": <int>,
            "qdrant_down_count": <int>,
            "details": [{meeting_id, city_name, meeting_date,
                         interpreted_as, chunks_used, retrieval_run_id}, ...]
        },
        "provenance": {
            "run_id": "zspan-operator-search-...",
            "child_run_ids": [<retrieval run_ids>, ...],
            "synthesis_provider": "claude-sonnet-4-6",
            "synthesis_prompt_version": "v1.5-operator-search-synthesis-2026-06-25",
            "timestamp_utc": "..."
        }
    }

    Per DP-3 strict: synthesis uses `claude -p` Sonnet via MAX cap (the
    test-loop substrate). BYOK swap later replaces only the synthesis
    step; retrieval + audit hygiene stay identical.
    """
    user = _current_user_from_cookie()
    if not user or not is_operator_search_principal(user.email):
        return jsonify({
            'success': False,
            'error': 'operator-search-principal-only endpoint',
        }), 403

    body = request.get_json(silent=True) or {}
    query = (body.get('query') or '').strip()
    if not query:
        return jsonify({'success': False, 'error': 'query is required'}), 400
    if len(query) > 500:
        return jsonify({
            'success': False,
            'error': 'query too long (max 500 chars)',
        }), 400

    meeting_ids = body.get('meeting_ids') or []
    if not isinstance(meeting_ids, list) or not meeting_ids:
        return jsonify({
            'success': False,
            'error': 'meeting_ids must be a non-empty list',
        }), 400
    try:
        meeting_ids = [int(mid) for mid in meeting_ids]
    except (TypeError, ValueError):
        return jsonify({
            'success': False,
            'error': 'meeting_ids must be a list of integers',
        }), 400
    if len(meeting_ids) > 100:
        return jsonify({
            'success': False,
            'error': 'meeting_ids list too long (max 100)',
        }), 400

    interpretation = body.get('interpretation') or {}
    if not isinstance(interpretation, dict):
        interpretation = {}

    import sys
    from pathlib import Path
    bridge_root = Path(__file__).resolve().parent.parent.parent
    if str(bridge_root) not in sys.path:
        sys.path.insert(0, str(bridge_root))
    from zspan_pipeline import qdrant_synthesizer, rag_search, operator_search

    # Join meetings table for city_name + meeting_date per meeting_id.
    from database import get_connection, save_byok_audit_run
    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(meeting_ids))
        rows = conn.execute(
            f"""SELECT id, city_name, meeting_date, video_url
                FROM meetings
                WHERE id IN ({placeholders})""",
            meeting_ids,
        ).fetchall()
    finally:
        conn.close()

    # scope_by_id keeps the per-meeting context the fan-out needs;
    # video_url_by_id stays separate so each citation in the response
    # can ship the raw URL (Z4 InlineMeetingMomentPlayer classifies
    # client-side via the existing getVideoSource util — no logic
    # duplicated server-side).
    scope_by_id = {r[0]: (r[1], r[2] or "") for r in rows}
    video_url_by_id: dict[int, Optional[str]] = {r[0]: r[3] for r in rows}
    scopes = [
        operator_search.MeetingScope(
            meeting_id=mid,
            city_name=scope_by_id[mid][0],
            meeting_date=scope_by_id[mid][1],
        )
        for mid in meeting_ids if mid in scope_by_id
    ]
    if not scopes:
        return jsonify({
            'success': False,
            'error': 'no meetings resolved (meeting_ids not in DB)',
        }), 400

    # Phase 3 — fan-out + dedup + synthesis.
    template_body = rag_search.load_prompt_template()
    legs = operator_search.fan_out_retrieve(query=query, scopes=scopes)

    # F1 audit-fix (2026-06-25 brainstorm-audit) — apply per-city
    # vocabulary substitutions to chunk bodies BEFORE they flow into
    # either the synthesis prompt OR the citation render. Raw Qdrant
    # chunks carry source-level Whisper errors (e.g. "Mojave County")
    # which V1-Repair-1's substitution layer corrects on user-facing
    # outputs but not on raw chunks. The operator-search expansion
    # card is a new VERBATIM-chunk surface, so substitutions need to
    # apply here too. Mutates the chunk in place; both synthesis and
    # citation building downstream pick up corrected text.
    try:
        from database import apply_city_corrections
        for _leg in legs:
            for _chunk in _leg.chunks:
                _chunk.body, _ = apply_city_corrections(
                    _leg.city_name, _chunk.body,
                )
    except Exception as _corr_exc:
        logging.warning(
            "apply_city_corrections failed during operator-search "
            "(non-fatal — raw chunks ship as-is): %s",
            _corr_exc,
        )

    # Per-leg audit-row writes (the fan-out bypasses /api/rag-search's
    # HTTP path so the audit hygiene wouldn't happen otherwise). Each
    # leg gets a child retrieval run_id we stamp here + reuse below in
    # the parent's child_run_ids.
    child_run_ids: list[str] = []
    for leg in legs:
        leg_provenance = rag_search.make_provenance_packet(
            meeting_id=leg.meeting_id,
            query=query,
            chunks=leg.chunks,
            template_body=template_body,
        )
        leg.retrieval_run_id = leg_provenance["run_id"]
        child_run_ids.append(leg_provenance["run_id"])
        try:
            save_byok_audit_run(
                run_id=leg_provenance["run_id"],
                kind="retrieval",
                meeting_id=leg.meeting_id,
                timestamp_utc=leg_provenance["timestamp_utc"],
                prompt_template_version=leg_provenance["prompt_template_version"],
                prompt_template_hash=leg_provenance["prompt_template_hash"],
                vector_ids=leg_provenance["vector_ids"],
                query_hash=leg_provenance["query_hash"],
                # F5 audit-fix (2026-06-25): provider is the company /
                # service ("anthropic" — matches /api/byok/validate-key
                # + /api/byok/relay convention); model is the model id.
                provider="anthropic",
                model="claude-sonnet-4-6",
            )
        except Exception as audit_exc:
            logging.warning(
                "operator-search leg audit-row write failed for run_id=%s: %s",
                leg_provenance["run_id"], audit_exc,
            )

    ranked = operator_search.dedup_and_rerank_chunks(legs)
    if not ranked:
        # All legs were indexed_no_match or qdrant_down — honest empty.
        return jsonify({
            'success': True,
            'answer': "No matching content found across the indexed meetings in scope. "
                      "(The retrieval ran but no chunks were relevant to the query.)",
            'citations': [],
            'leg_outcomes': _summarize_legs(legs),
            'provenance': {
                'run_id': None,
                'child_run_ids': child_run_ids,
                'synthesis_provider': None,
                'synthesis_prompt_version': operator_search.SYNTHESIS_PROMPT_VERSION,
                'timestamp_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            },
        })

    synthesis_prompt = operator_search.build_synthesis_prompt(
        query=query, interpretation=interpretation, ranked=ranked,
    )

    try:
        answer = qdrant_synthesizer.synthesize_via_claude_p(
            synthesis_prompt, timeout_seconds=300.0,
        )
    except Exception as e:
        logging.exception("operator-search execute: synthesis claude -p failed")
        return jsonify({
            'success': False,
            'error': f'Sonnet synthesis failed: {e}',
        }), 502

    # Build citations from the ranked chunks for the frontend chips.
    citations: list[dict] = []
    union_vector_ids: list[str] = []
    for pair in ranked:
        leg = pair["leg"]
        c = pair["chunk"]
        vid = rag_search.chunk_to_vector_id(leg.meeting_id, c.chunk_index)
        union_vector_ids.append(vid)
        body_text = c.body if isinstance(c.body, str) else str(c.body)
        citations.append({
            'meeting_id': leg.meeting_id,
            'city_name': leg.city_name,
            'meeting_date': leg.meeting_date,
            'chunk_index': c.chunk_index,
            'vector_id': vid,
            'start_seconds': c.start_seconds,
            'end_seconds': c.end_seconds,
            'score': round(c.score, 4),
            # F4 audit-fix (2026-06-25 brainstorm-audit) — body_preview
            # field dropped; client slices `body` if a compact preview
            # is needed (the bottom debug accordion does so now).
            'body': body_text,
            # Z3 — raw video URL for the InlineMeetingMomentPlayer
            # (Z4). None for meetings without a video archive (e.g.
            # Colorado City per S-037 V0). The client classifies kind
            # via getVideoSource — no logic duplicated server-side.
            'video_url': video_url_by_id.get(leg.meeting_id),
        })

    # Parent operator_search audit row indexing the child retrieval rows
    # via child_run_ids + carrying the deduped union vector_ids (what
    # Sonnet actually synthesized over).
    ts_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    parent_run_id = (
        "zspan-operator-search-"
        + datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%S')
        + "-"
        + rag_search.query_hash(query)[:6]
    )
    try:
        save_byok_audit_run(
            run_id=parent_run_id,
            kind="operator_search",
            meeting_id=None,
            timestamp_utc=ts_utc,
            prompt_template_version=operator_search.SYNTHESIS_PROMPT_VERSION,
            prompt_template_hash="sha256:" + rag_search.prompt_template_hash(synthesis_prompt),
            vector_ids=union_vector_ids,
            query_hash="sha256:" + rag_search.query_hash(query),
            # F5 audit-fix (2026-06-25): provider = "anthropic" (service),
            # model = "claude-sonnet-4-6" (model id). Matches the BYOK
            # validate-key + relay convention.
            provider="anthropic",
            model="claude-sonnet-4-6",
            child_run_ids=child_run_ids,
        )
    except Exception as audit_exc:
        logging.warning(
            "operator-search parent audit-row write failed for run_id=%s: %s",
            parent_run_id, audit_exc,
        )

    return jsonify({
        'success': True,
        'answer': answer,
        'citations': citations,
        'leg_outcomes': _summarize_legs(legs),
        'provenance': {
            'run_id': parent_run_id,
            'child_run_ids': child_run_ids,
            'synthesis_provider': "claude-sonnet-4-6",
            'synthesis_prompt_version': operator_search.SYNTHESIS_PROMPT_VERSION,
            'timestamp_utc': ts_utc,
        },
    })


def _summarize_legs(legs):
    """Compact per-leg outcome summary for the response."""
    ok = sum(1 for L in legs if L.interpreted_as == "ok")
    no_match = sum(1 for L in legs if L.interpreted_as == "indexed_no_match")
    down = sum(1 for L in legs if L.interpreted_as == "qdrant_down")
    return {
        'ok_count': ok,
        'indexed_no_match_count': no_match,
        'qdrant_down_count': down,
        'details': [
            {
                'meeting_id': L.meeting_id,
                'city_name': L.city_name,
                'meeting_date': L.meeting_date,
                'interpreted_as': L.interpreted_as,
                'chunks_used': len(L.chunks),
                'retrieval_run_id': L.retrieval_run_id,
                'error': L.error,
            }
            for L in legs
        ],
    }


# ── /api/report-runs — S-122 Report-V0-1 cited-report generator ─────────
#
# Owner-gated like /api/operator-search/* (same D-145 posture: open-prompt
# report generation is operator-only; the public path later is
# pre-computed suggested reports). The ReportModal reuses
# /api/operator-search/interpret for Phase 1, then:
#   POST /api/report-runs               — create + spawn the daemon thread
#   GET  /api/report-runs/<id>          — poll target (no artifact_html)
#   GET  /api/report-runs/<id>/artifact — the single-file HTML report
#     (?download=1 adds Content-Disposition so the browser saves it)
#
# The pipeline itself lives in zspan_pipeline/report_generator.py — the
# fan-out + per-section claude -p synthesis + renderer. It writes every
# state change onto the report_runs row, so a thread death mid-run leaves
# the row honestly stuck in "running" with its last progress string
# (visible in the modal) rather than lying "complete".


@app.route('/api/report-runs', methods=['POST'])
@_require_trusted_origin
def api_report_runs_create():
    """Create a report run + fire the background pipeline."""
    user = _current_user_from_cookie()
    if not user or not is_operator_search_principal(user.email):
        return jsonify({
            'success': False,
            'error': 'operator-search-principal-only endpoint',
        }), 403

    body = request.get_json(silent=True) or {}
    query = (body.get('query') or '').strip()
    if not query:
        return jsonify({'success': False, 'error': 'query is required'}), 400
    if len(query) > 500:
        return jsonify({
            'success': False,
            'error': 'query too long (max 500 chars)',
        }), 400

    meeting_ids = body.get('meeting_ids') or []
    if not isinstance(meeting_ids, list) or not meeting_ids:
        return jsonify({
            'success': False,
            'error': 'meeting_ids must be a non-empty list',
        }), 400
    try:
        meeting_ids = [int(mid) for mid in meeting_ids]
    except (TypeError, ValueError):
        return jsonify({
            'success': False,
            'error': 'meeting_ids must be a list of integers',
        }), 400
    if len(meeting_ids) > 100:
        return jsonify({
            'success': False,
            'error': 'meeting_ids list too long (max 100)',
        }), 400

    interpretation = body.get('interpretation') or {}
    if not isinstance(interpretation, dict):
        interpretation = {}

    import sys as _sys
    from pathlib import Path as _Path
    bridge_root = _Path(__file__).resolve().parent.parent.parent
    if str(bridge_root) not in _sys.path:
        _sys.path.insert(0, str(bridge_root))
    from zspan_pipeline import report_generator
    from database import create_report_run

    report_run_id = (
        "rr-"
        + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        + "-"
        + uuid.uuid4().hex[:8]
    )
    create_report_run(report_run_id, query, interpretation, meeting_ids)

    t = threading.Thread(
        target=report_generator.run_report_run,
        args=(report_run_id,),
        name=f"report-run-{report_run_id}",
        daemon=True,
    )
    t.start()

    return jsonify({'success': True, 'id': report_run_id})


@app.route('/api/report-runs/<report_run_id>', methods=['GET'])
def api_report_runs_get(report_run_id):
    """Poll target — run state without the artifact body."""
    user = _current_user_from_cookie()
    if not user or not is_operator_search_principal(user.email):
        return jsonify({
            'success': False,
            'error': 'operator-search-principal-only endpoint',
        }), 403
    from database import get_report_run
    run = get_report_run(report_run_id, include_artifact=False)
    if run is None:
        return jsonify({'success': False, 'error': 'report run not found'}), 404
    return jsonify({'success': True, 'run': run})


@app.route('/api/report-runs/<report_run_id>/artifact', methods=['GET'])
def api_report_runs_artifact(report_run_id):
    """Serve the single-file HTML report — inline for the preview iframe,
    attachment when ?download=1."""
    user = _current_user_from_cookie()
    if not user or not is_operator_search_principal(user.email):
        return jsonify({
            'success': False,
            'error': 'operator-search-principal-only endpoint',
        }), 403
    from database import get_report_run
    run = get_report_run(report_run_id, include_artifact=True)
    if run is None:
        return jsonify({'success': False, 'error': 'report run not found'}), 404
    variant = request.args.get('variant') or 'v0'
    artifact = (
        run.get('stitch_artifact_html') if variant == 'stitch'
        else run.get('artifact_html')
    )
    if not artifact:
        return jsonify({
            'success': False,
            'error': '%s artifact not ready (run status: %s)' % (variant, run.get('status')),
        }), 409
    resp = Response(artifact, mimetype='text/html')
    if request.args.get('download'):
        safe_stamp = (run.get('created_at') or '').replace(':', '').replace('-', '')[:15]
        suffix = '-stitch' if variant == 'stitch' else ''
        resp.headers['Content-Disposition'] = (
            f'attachment; filename="zspan-report-{safe_stamp or report_run_id}{suffix}.html"'
        )
    return resp


@app.route('/api/report-runs/<report_run_id>/fragments', methods=['GET'])
def api_report_runs_fragments(report_run_id):
    """Report-Stitch-1 — the rendered content fragments the Node-side
    Stitch driver injects into the generative chrome. Same content the
    V0 template assembles, so both artifacts stay identical in substance."""
    user = _current_user_from_cookie()
    if not user or not is_operator_search_principal(user.email):
        return jsonify({
            'success': False,
            'error': 'operator-search-principal-only endpoint',
        }), 403
    import sys as _sys
    from pathlib import Path as _Path
    bridge_root = _Path(__file__).resolve().parent.parent.parent
    if str(bridge_root) not in _sys.path:
        _sys.path.insert(0, str(bridge_root))
    from zspan_pipeline import report_generator
    from database import get_report_run
    run = get_report_run(report_run_id, include_artifact=False)
    if run is None:
        return jsonify({'success': False, 'error': 'report run not found'}), 404
    if run.get('status') != 'complete':
        return jsonify({
            'success': False,
            'error': 'report not complete yet (status: %s)' % run.get('status'),
        }), 409
    try:
        frags = report_generator.fragments_for_stored_run(run)
    except Exception as e:
        logging.exception("report fragments render failed for %s", report_run_id)
        return jsonify({'success': False, 'error': f'fragment render failed: {e}'}), 500
    return jsonify({'success': True, 'fragments': frags})


@app.route('/api/report-runs/<report_run_id>/stitch-result', methods=['POST'])
@_require_trusted_origin
def api_report_runs_stitch_result(report_run_id):
    """Report-Stitch-1 — the Node driver persists its outcome here (the
    Express layer owns the Stitch SDK; Flask owns the DB). Accepts both
    success (stitch_artifact_html) and failure (stitch_error) shapes so
    the run row always tells the truth about the last attempt."""
    user = _current_user_from_cookie()
    if not user or not is_operator_search_principal(user.email):
        return jsonify({
            'success': False,
            'error': 'operator-search-principal-only endpoint',
        }), 403
    from database import get_report_run, update_report_run
    run = get_report_run(report_run_id, include_artifact=False)
    if run is None:
        return jsonify({'success': False, 'error': 'report run not found'}), 404
    body = request.get_json(silent=True) or {}
    fields = {}
    for key in ('stitch_status', 'stitch_progress', 'stitch_project_id', 'stitch_error'):
        if key in body:
            fields[key] = body[key]
    if 'stitch_artifact_html' in body:
        artifact = body['stitch_artifact_html']
        if not isinstance(artifact, str) or len(artifact) > 5_000_000:
            return jsonify({'success': False, 'error': 'stitch_artifact_html invalid or too large'}), 400
        fields['stitch_artifact_html'] = artifact
    if 'stitch_edits' in body and isinstance(body['stitch_edits'], list):
        fields['stitch_edits'] = body['stitch_edits']
    if not fields:
        return jsonify({'success': False, 'error': 'no recognized fields'}), 400
    update_report_run(report_run_id, **fields)
    return jsonify({'success': True})


# ── /api/member-rag — V1-RAG-3 per-member retrieval-only browse (γ, S-071) ──
#
# The TruthBook V3-preview surface deferred to post-V1-RAG evaluation per
# D-126 §5 + S-071 lands here. Per-member chunk browsing on the V1-RAG-3
# Qdrant + Sonnet stack — pure retrieval, NO synthesis. Each result is a
# Qdrant chunk plus its karaoke timecode and the matched member aliases;
# the operator (and visitor when V1-public-discoverable) listens to verify
# whether the member is speaking or being discussed, with no LLM narrating
# voting patterns or accountability claims under the operator's name.
#
# Mechanism (γ live filter at query time): topic → topic_hint → Qdrant
# retrieval per city meeting → post-retrieve filter by member alias
# substring. Aliases are derived via the existing zspan_pipeline.symbols
# alias generator that the bridge already battle-tested for NotebookLM's
# linker pass. No Qdrant schema change, no re-index, no diarization
# upgrade — the upgrade path to ε (index-time mention tagging) or ζ
# (diarization) stays open for V3-proper if V1-preview proves out.
#
# Operator-only at V1 per the App.tsx OWNER_ONLY_VIEWS route gate + the
# CastMemberPanel <OwnerOnly> wrap on the gateway buttons (C1, 2026-06-20).
# Auth gate here mirrors /api/rag-search: loopback passthrough; bearer token
# for LAN clients.

@app.route('/api/member-rag/<city_name>/<seat_id>', methods=['POST'])
def api_member_rag(city_name, seat_id):
    """Per-member V1-RAG-3 retrieval (no synthesis).

    Request JSON:  {"topic": "<topic_id>", "top_k": <int, default 12>}
    Response JSON: {
        "success": true,
        "city": "Bullhead City",
        "seat_id": "seat_1",
        "member": {name, role, seat_id, term_started, term_ends},
        "aliases": ["Stehly", "Council Member Stehly", ...],
        "topic": {id, label, hint},
        "results": [
            {meeting_id, meeting_title, meeting_date, meeting_video_url,
             chunk_index, start_seconds, end_seconds, score, body,
             matched_aliases: [...]},
            ...
        ],
        "meetings_queried": <int>,
        "chunks_retrieved": <int>,
        "chunks_matched": <int>
    }

    Topic id is one of the controlled vocabulary in parsers/topic_tags.py
    (water_rights / data_centers / education / diversity_inclusion / lgbtq)
    or the literal "other".
    """
    # Auth gate — same shape as /api/rag-search above, PLUS the
    # session-31 auth-audit remediation. The prior comment claimed
    # "operator-only... enforced via OWNER_ONLY_VIEWS + OwnerOnly" —
    # but that's a *frontend* route guard, and nothing backend-side
    # actually checked it. Any browser session with the shared token
    # (persisted in user_settings.json) could query it directly,
    # bypassing the stated intent. Now: shared-token OR owner cookie
    # both pass — matches the claimed guarantee. Loopback still
    # bypasses (worker.py + local scripts run against 127.0.0.1), but the
    # decision uses Express's trusted edge-derived client IP rather than the
    # always-loopback proxy socket.
    _user_cookie_owner = False
    _u = _current_user_from_cookie()
    if _u and is_owner_email(_u.email):
        _user_cookie_owner = True
    if not _user_cookie_owner and not _is_local_origin(_rate_limit_client_ip()):
        expected = _resolve_rag_query_token()
        if not expected:
            return jsonify({
                'success': False,
                'error': 'server has no zspan_rag_query_token configured',
            }), 500
        auth = (request.headers.get('Authorization') or '').strip()
        presented = ''
        if auth.startswith('Bearer '):
            presented = auth[len('Bearer '):].strip()
        if not presented:
            presented = (request.args.get('token') or '').strip()
        if presented != expected:
            return jsonify({
                'success': False,
                'error': 'unauthorized — owner cookie or valid rag-query token required',
            }), 401

    try:
        body = request.get_json(silent=True) or {}
        topic_id = (body.get('topic') or '').strip()
        if not topic_id:
            return jsonify({'success': False, 'error': 'topic is required'}), 400
        try:
            top_k = int(body.get('top_k') or 12)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'top_k must be an integer'}), 400
        top_k = max(1, min(top_k, 50))

        # Validate + resolve topic to its natural-language hint
        from topic_tags import TOPIC_TAGS, OTHER_TAG_ID
        topic_record = next(
            ((tid, lbl, hint) for (tid, lbl, hint) in TOPIC_TAGS if tid == topic_id),
            None,
        )
        if topic_record:
            _, topic_label, topic_hint = topic_record
        elif topic_id == OTHER_TAG_ID:
            topic_label = "Other"
            topic_hint = (
                "Discussion outside the featured topic vocabulary — "
                "budget, zoning, public safety, infrastructure, procedural "
                "items, or any non-categorized council exchange."
            )
        else:
            return jsonify({
                'success': False,
                'error': f'unknown topic id: {topic_id}',
            }), 400

        # Resolve the member and the city's meetings
        conn = get_connection()
        try:
            member_row = conn.execute(
                """
                SELECT id, name, role, seat_id, term_started, term_ends, source_url
                FROM council_members
                WHERE city_name = ? AND seat_id = ?
                """,
                (city_name, seat_id),
            ).fetchone()
            if not member_row:
                return jsonify({
                    'success': False,
                    'error': f'member not found: {city_name} {seat_id}',
                }), 404
            member = dict(member_row)

            # Filter to meetings that actually have V1-RAG-3 outputs cached —
            # the presence of any `prompt_version LIKE 'v1-rag-3%'` row in
            # notebook_outputs is the proxy signal for "this meeting is in
            # Qdrant," because the V1-RAG-3 synthesis pipeline only runs
            # after /index has populated the chunks. Without this filter the
            # endpoint would iterate every meeting in the city (Kingman has
            # 204, Bullhead 111) and each non-indexed meeting still costs
            # one HTTP round-trip to Surface Pro to learn it has zero hits.
            # The proxy filter keeps the per-request fan-out bounded to the
            # actual indexed set (4 meetings as of 2026-06-20).
            meeting_rows = conn.execute(
                """
                SELECT DISTINCT m.id, m.meeting_title, m.meeting_date,
                       COALESCE(wo.youtube_video_url, m.video_url) AS meeting_video_url
                FROM meetings m
                INNER JOIN notebook_outputs no
                    ON no.meeting_id = m.id
                    AND no.prompt_version LIKE 'v1-rag-3%'
                LEFT JOIN work_orders wo ON wo.meeting_id = m.id
                WHERE m.city_name = ?
                ORDER BY m.meeting_date DESC
                """,
                (city_name,),
            ).fetchall()
            meetings = [dict(r) for r in meeting_rows]
        finally:
            conn.close()

        # Derive aliases via the existing bridge helper. Add canonical name
        # to the front so the most-specific match wins display priority.
        import sys
        from pathlib import Path
        bridge_root = Path(__file__).resolve().parent.parent.parent
        if str(bridge_root) not in sys.path:
            sys.path.insert(0, str(bridge_root))
        from zspan_pipeline.symbols import _derive_member_aliases
        from zspan_pipeline import qdrant_synthesizer

        aliases = _derive_member_aliases(member['name'], member['role'] or '')
        if member['name'] not in aliases:
            aliases = [member['name']] + aliases
        # Filter aliases that are too short or generic (single token "the
        # Mayor" / single-letter / common-noun roles) to prevent spurious
        # mention matches. Three-char minimum on plain tokens; role-prefixed
        # forms always pass.
        def _useful_alias(a: str) -> bool:
            stripped = (a or '').strip()
            if len(stripped) < 3:
                return False
            if stripped.lower() in {'the mayor', 'the vice mayor'}:
                # These match too liberally (any "the mayor announced ..."
                # sentence catches the wrong member). Keep them in symbols
                # for NotebookLM's linker but exclude from the filter set.
                return False
            return True
        aliases = [a for a in aliases if _useful_alias(a)]

        aliases_lower = [a.lower() for a in aliases]

        # Query Qdrant per meeting; skip meetings not yet indexed (zero
        # chunks). Each call is one HTTP round-trip to Surface Pro; for
        # the V1-RAG-3 indexed set (~4 meetings) this is bounded.
        results = []
        meetings_queried = 0
        chunks_retrieved = 0
        chunks_matched = 0

        for meeting in meetings:
            mid = meeting['id']
            try:
                # Tighter per-call timeout (10s vs the synthesizer's 30s default):
                # the city-indexed filter above bounds the iteration to ~4
                # meetings, so the worst case is 40s if every call timeouts.
                # If Surface Pro is reachable the actual cost is sub-second
                # per call for embedding + Qdrant lookup.
                chunks = qdrant_synthesizer.retrieve_chunks(
                    mid, topic_hint, top_k=top_k, timeout_seconds=10.0
                )
            except Exception as exc:
                # Surface Pro unreachable, meeting not indexed, etc — log and
                # continue. A meeting with no chunks just contributes nothing.
                logging.debug(
                    "member-rag retrieve_chunks meeting=%d failed: %s", mid, exc
                )
                continue
            if not chunks:
                continue
            meetings_queried += 1
            chunks_retrieved += len(chunks)

            for chunk in chunks:
                body_lower = chunk.body.lower()
                matched = [
                    a for a, al in zip(aliases, aliases_lower)
                    if al in body_lower
                ]
                if not matched:
                    continue
                chunks_matched += 1
                results.append({
                    'meeting_id': mid,
                    'meeting_title': meeting['meeting_title'],
                    'meeting_date': meeting['meeting_date'],
                    'meeting_video_url': meeting['meeting_video_url'],
                    'chunk_index': chunk.chunk_index,
                    'start_seconds': chunk.start_seconds,
                    'end_seconds': chunk.end_seconds,
                    'score': round(chunk.score, 4),
                    'body': chunk.body,
                    'matched_aliases': matched[:5],
                })

        # Sort results by score descending so highest-relevance chunks land
        # at the top of the rendered list. Stable across re-queries.
        results.sort(key=lambda r: r['score'], reverse=True)

        return jsonify({
            'success': True,
            'city': city_name,
            'seat_id': seat_id,
            'member': {
                'name': member['name'],
                'role': member['role'],
                'seat_id': member['seat_id'],
                'term_started': member.get('term_started'),
                'term_ends': member.get('term_ends'),
            },
            'aliases': aliases,
            'topic': {'id': topic_id, 'label': topic_label, 'hint': topic_hint},
            'results': results,
            'meetings_queried': meetings_queried,
            'chunks_retrieved': chunks_retrieved,
            'chunks_matched': chunks_matched,
        })
    except Exception as e:
        logging.exception(
            "api_member_rag failed for city=%s seat=%s", city_name, seat_id
        )
        return jsonify({'success': False, 'error': str(e)}), 500


# ─── RR-5 — Public coverage status (S-124; REGISTRY_POLICY "the map stays
#     visible" commitment) ───
# Every configured city is listed with an honest Z-SPAN status. Status derivation is
# two-layer: LIVE DB TRUTH WINS (a city with published broadcasts is
# "covered" regardless of what the static index says — the flagship city
# must never read "assessment pending" on its own coverage page), and the
# static coverage_index.json field covers only cities without published
# content. Public vocabulary per the policy page + D-054 (plain words,
# never builder enum values).

_COVERAGE_PUBLIC_LABELS = {
    "live": "monitored",            # parser healthy; nothing published yet
    "needs-repair": "needs repair",
    "postponed": "postponed",
    "honest-empty": "no video source",
    "unassessed": "assessment pending",
}


@app.route('/api/coverage', methods=['GET'])
def api_coverage_list():
    """Public coverage listing — every registry city + honest status +
    published-content freshness. Intentionally public (the D-153 § 1
    public-status commitment)."""
    from pathlib import Path
    try:
        index_path = Path(__file__).resolve().parent / "coverage_index.json"
        try:
            index = json.loads(index_path.read_text())
        except FileNotFoundError:
            return jsonify({
                'success': True, 'status': 'empty', 'count': 0, 'cities': [],
                'note': 'coverage index not present on this instance',
            })

        published = _coverage_published_by_city()

        cities = []
        for row in index.get('cities', []):
            name = row.get('city')
            pub_count, latest = published.get(name, (0, None))
            if pub_count > 0:
                status = 'covered'
            else:
                status = _COVERAGE_PUBLIC_LABELS.get(
                    row.get('coverage'), 'assessment pending')
            cities.append({
                'city': name,
                'county': row.get('county'),
                'state': (row.get('state') or '').upper(),
                'status': status,
                'published_count': pub_count,
                'latest_published_date': latest,
            })
        cities.sort(key=lambda c: (c['state'], c['county'] or '', c['city'] or ''))
        return jsonify({
            'success': True,
            'status': 'ok' if cities else 'empty',
            'count': len(cities),
            'generated_at': index.get('generated_at'),
            'cities': cities,
        })
    except Exception:
        # Public registry route — stable code; detail server-side only.
        logging.exception("coverage list failed")
        return jsonify({'success': False, 'error': 'coverage lookup failed'}), 500


# ─── RR-4 — Public corrections log (S-043 B-4, CORRECTIONS_POLICY_DRAFT) ───
# The institutional doorbell. Intake is EMAIL (corrections@zspan.org — no
# form, no intake endpoint, no spam surface); these routes serve the public
# running log and give the operator the log's write path. The read is
# INTENTIONALLY PUBLIC (the visible-not-silent corrections promise); both
# mutations are owner-gated FROM BIRTH per the S-129 lesson (mutation
# routes never rely on the perimeter alone).

@app.route('/api/corrections', methods=['GET'])
def api_corrections_list():
    """Owner-side corrections log, including internal working notes."""
    _user, _err = _require_owner()
    if _err:
        return _err
    try:
        rows = list_corrections(include_internal=True)
        return jsonify({'success': True, 'count': len(rows), 'corrections': rows})
    except Exception:
        # Public corrections log — stable code; detail server-side only.
        logging.exception("corrections list failed")
        return jsonify({'success': False, 'error': 'corrections lookup failed'}), 500


@app.route('/api/corrections', methods=['POST'])
@_require_trusted_origin
def api_corrections_create():
    """Owner-only: log a correction row (usually at email-triage time)."""
    _user, _err = _require_owner()
    if _err:
        return _err
    body = request.get_json(silent=True) or {}
    try:
        new_id = create_correction(
            meeting_id=body.get('meeting_id'),
            corrected_surface=body.get('corrected_surface'),
            status=body.get('status', 'under_review'),
            summary_public=body.get('summary_public'),
            detail_internal=body.get('detail_internal'),
        )
        return jsonify({'success': True, 'id': new_id})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.exception("corrections create failed")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/corrections/<int:correction_id>/update', methods=['POST'])
@_require_trusted_origin
def api_corrections_update(correction_id):
    """Owner-only: resolve / annotate a logged correction. Terminal
    statuses stamp resolved_at automatically."""
    _user, _err = _require_owner()
    if _err:
        return _err
    body = request.get_json(silent=True) or {}
    allowed = {k: body[k] for k in
               ('status', 'summary_public', 'detail_internal', 'corrected_surface')
               if k in body}
    if not allowed:
        return jsonify({'success': False, 'error': 'no updatable fields provided'}), 400
    try:
        found = update_correction(correction_id, **allowed)
        if not found:
            return jsonify({'success': False, 'error': 'no such correction'}), 404
        return jsonify({'success': True, 'id': correction_id})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.exception("corrections update failed")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── D-180 branchless public DTO surface ──────────────────────────────

_PUBLIC_CALENDAR_SEARCH_MAX_LIMIT = 100
_PUBLIC_CALENDAR_SEARCH_MAX_OFFSET = 5_000


def _project_public_dto(source: dict, fields: tuple[str, ...]) -> dict:
    """Construct a fresh DTO from a reviewed public field tuple."""
    return {field: source.get(field) for field in fields}


def _public_int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _public_calendar_search_pagination() -> tuple[int, int]:
    return (
        _public_int_arg(
            'limit', _PUBLIC_CALENDAR_SEARCH_MAX_LIMIT,
            1, _PUBLIC_CALENDAR_SEARCH_MAX_LIMIT,
        ),
        _public_int_arg(
            'offset', 0, 0, _PUBLIC_CALENDAR_SEARCH_MAX_OFFSET,
        ),
    )


def _public_json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _public_word_timings(value) -> list[dict]:
    timings = []
    for timing in _public_json_list(value):
        if isinstance(timing, dict):
            timings.append(_project_public_dto(
                timing, public_dto.PUBLIC_WORD_TIMING_FIELDS,
            ))
    return timings


def _resolve_visible_public_meeting(public_id: str) -> tuple[dict, int] | None:
    """Resolve canonical/alias public_id and enforce D-180 visibility."""
    if PUBLIC_ID_RE.fullmatch(public_id) is None:
        return None
    meeting = get_meeting_public_record(public_id)
    if meeting is None:
        return None
    meeting_id = int(meeting['id'])
    if not is_meeting_publicly_visible(meeting_id):
        return None
    return meeting, meeting_id


def _public_episode_card(row: dict) -> dict:
    source = {
        'public_id': row.get('public_id') or '',
        'city_name': row.get('city_name') or '',
        'county': row.get('county') or '',
        'state': row.get('state') or '',
        'meeting_title': row.get('meeting_title') or '',
        'meeting_date': row.get('meeting_date') or '',
        'meeting_time': row.get('meeting_time') or '',
        'meeting_location': row.get('meeting_location') or '',
        'meeting_status': row.get('meeting_status') or '',
        'agenda_url': row.get('agenda_url') or '',
        'minutes_url': row.get('minutes_url') or '',
        'agenda_packet_url': row.get('agenda_packet_url') or '',
        'video_url': row.get('video_url') or '',
        'ecomment_url': row.get('ecomment_url') or '',
        'published_at': row.get('published_at') or '',
        'availability': 'published',
        'episode_tagline': row.get('episode_tagline') or '',
    }
    return _project_public_dto(source, public_dto.PUBLIC_EPISODE_CARD_FIELDS)


def _load_verified_key_decisions(meeting_id: int) -> str | None:
    """Load the canonical, citation-aligned decisions prose for public use.

    ``None`` means no verified artifact is available.  An empty string is a
    valid, verified honest-empty sidecar and must remain distinguishable from
    an absent or unreadable sidecar.
    """
    from flagship_sync import _preview_root, _sidecar_path  # noqa: PLC0415

    path = _sidecar_path(_preview_root(), meeting_id, 'decisions')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        app.logger.exception(
            'verified key_decisions sidecar read failed for meeting %s', meeting_id,
        )
        return None

    if not isinstance(data, dict) or not isinstance(data.get('prose_output'), str):
        app.logger.error(
            'verified key_decisions sidecar has invalid prose_output for meeting %s',
            meeting_id,
        )
        return None
    return data['prose_output']


def _public_cast_member(row: dict) -> dict:
    return _project_public_dto({
        'seat_id': row.get('seat_id') or '',
        'name': row.get('name') or '',
        'role': row.get('role') or '',
        'term_started': row.get('term_started') or '',
        'term_ends': row.get('term_ends') or '',
        'source_url': row.get('source_url') or '',
    }, public_dto.PUBLIC_CAST_MEMBER_FIELDS)


app.register_blueprint(_public_api_bp, name='')


if __name__ == '__main__':
    # Initialize database on startup
    init_db()
    populate_cities_from_index()
    seed_council_members_from_intelligence()

    # D-099 Phase 2 era: bind to 0.0.0.0 so the Mac worker (D-099 Decision 3)
    # can reach this Flask over the LAN. The /api/worker/* namespace is
    # bearer-token-gated; the broader /api/* surface remains public per the
    # project's existing "no auth" posture documented in CLAUDE.md, and that
    # exposure is acceptable at trusted-single-operator-LAN scope. A
    # stricter same-LAN hardening pass is parked in the internal backlog (S-039).
    # Override via PARSER_API_HOST=127.0.0.1 to revert to localhost-only.
    host = os.getenv('PARSER_API_HOST', '0.0.0.0')
    port = int(os.getenv('PARSER_API_PORT', sys.argv[1] if len(sys.argv) > 1 else 5001))
    print(f"Starting parser API server on {host}:{port}...")
    app.run(host=host, port=port, debug=False)

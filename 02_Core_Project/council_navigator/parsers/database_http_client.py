"""D-099 Phase 2 C4b — Mac-side HTTP shim for database.py's worker surface.

Each function here mirrors the SIGNATURE of the database.py equivalent,
but routes through Flask's /api/worker/* endpoints (per C3). The
Mac-side worker.py + fetcher.py + scanner.py select this module over
the SQLite-direct database.py via the C5 env-var dispatch.

Config (env vars):
    ZSPAN_FLASK_BASE_URL      e.g. http://127.0.0.1:5001 (preferred, per D-111
                              substrate consolidation). Falls back to legacy
                              ZSPAN_PC_FLASK_BASE_URL if the preferred name
                              isn't set — kept for backward compat with old
                              launchd plists until they're reinstalled.
    ZSPAN_AGENT_STATE_TOKEN   bearer for the /api/worker/* endpoints
                              (falls back to user_settings.json)

All calls use a hard request timeout — Mac worker depends on these
landing or failing fast. Network errors raise DatabaseHTTPError; the
caller handles them the same way it would handle a SQLite OperationalError.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class DatabaseHTTPError(RuntimeError):
    """Network / HTTP failure talking to the PC Flask DB shim. Caller
    decides whether to retry, log, or bubble up."""


# Hard timeouts. Reads can be fast; writes occasionally hit the
# auto_enqueue hook (V1-Repo-1) which does extra DB work. Scan is
# slow on PC because it walks the whole meetings table; give it room.
_DEFAULT_READ_TIMEOUT = 30.0
_DEFAULT_WRITE_TIMEOUT = 60.0
_SCAN_TIMEOUT = 300.0


def _base_url() -> str:
    # Prefer the post-D-111 name; fall back to the legacy PC-prefixed name
    # for compat with old launchd plists until they're reinstalled.
    url = (os.environ.get("ZSPAN_FLASK_BASE_URL") or "").strip().rstrip("/")
    if not url:
        url = (os.environ.get("ZSPAN_PC_FLASK_BASE_URL") or "").strip().rstrip("/")
    if not url:
        raise DatabaseHTTPError(
            "ZSPAN_FLASK_BASE_URL (or legacy ZSPAN_PC_FLASK_BASE_URL) not set — "
            "worker can't reach Flask. Set e.g. http://127.0.0.1:5001"
        )
    return url


def _token() -> str:
    env = (os.environ.get("ZSPAN_AGENT_STATE_TOKEN") or "").strip()
    if env:
        return env
    # Fallback to user_settings.json (the file is per-machine; Mac may
    # carry its own copy in the cloned repo). Lazy import to avoid the
    # cycle in case env_config grows database deps later.
    try:
        from env_config import load_user_settings
        settings = load_user_settings() or {}
        tok = (settings.get("zspan_agent_state_token") or "").strip()
        if tok:
            return tok
    except Exception:  # noqa: BLE001 — caller surfaces a clearer error
        pass
    raise DatabaseHTTPError(
        "ZSPAN_AGENT_STATE_TOKEN not set and user_settings.json has no "
        "zspan_agent_state_token. Provision the token first."
    )


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "X-Zspan-Agent-Role": "pipeline-worker",
    }


def _get(path: str, params: Optional[Dict[str, Any]] = None,
         timeout: float = _DEFAULT_READ_TIMEOUT) -> Any:
    url = f"{_base_url()}{path}"
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=timeout)
    except requests.RequestException as e:
        raise DatabaseHTTPError(f"GET {path} failed: {e}") from e
    if resp.status_code == 404:
        return None
    if not resp.ok:
        raise DatabaseHTTPError(
            f"GET {path} returned {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


def _post(path: str, body: Optional[Dict[str, Any]] = None,
          timeout: float = _DEFAULT_WRITE_TIMEOUT) -> Any:
    url = f"{_base_url()}{path}"
    try:
        resp = requests.post(url, headers=_headers(), json=(body or {}), timeout=timeout)
    except requests.RequestException as e:
        raise DatabaseHTTPError(f"POST {path} failed: {e}") from e
    if not resp.ok:
        raise DatabaseHTTPError(
            f"POST {path} returned {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


# ── Work-order surface ───────────────────────────────────────────────


def get_work_order(work_order_id: int) -> Optional[Dict]:
    return _get(f"/api/worker/work-orders/{int(work_order_id)}")


def next_pending_work_order() -> Optional[Dict]:
    payload = _post("/api/worker/work-orders/next")
    return (payload or {}).get("work_order")


def list_work_orders(state: Optional[str] = None,
                     city: Optional[str] = None,
                     limit: int = 200) -> List[Dict]:
    params: Dict[str, Any] = {"limit": int(limit)}
    if state:
        params["state"] = state
    if city:
        params["city"] = city
    payload = _get("/api/worker/work-orders", params=params)
    return (payload or {}).get("work_orders", [])


def work_order_stats() -> Dict[str, int]:
    return _get("/api/worker/work-orders/stats") or {}


def update_work_order_state(
    work_order_id: int,
    state: str,
    error=None,
    notebook_id: Optional[str] = None,
    youtube_video_url: Optional[str] = None,
    increment_retry: bool = False,
) -> None:
    body: Dict[str, Any] = {"state": state}
    # error semantics mirror the SQLite version: None means "don't touch",
    # a string sets the value. The C3 endpoint reads `error` only when the
    # key is PRESENT in the JSON body, so omit it entirely when None.
    if error is not None:
        body["error"] = error
    if notebook_id is not None:
        body["notebook_id"] = notebook_id
    if youtube_video_url is not None:
        body["youtube_video_url"] = youtube_video_url
    if increment_retry:
        body["increment_retry"] = True
    _post(f"/api/worker/work-orders/{int(work_order_id)}/state", body=body)


def update_meeting_diarization_status(
    meeting_id: int,
    status: str,
    detail: Optional[str] = None,
) -> bool:
    body: Dict[str, Any] = {"status": status, "detail": detail}
    payload = _post(
        f"/api/worker/meetings/{int(meeting_id)}/diarization-status",
        body=body,
    )
    return bool((payload or {}).get("updated"))


def enqueue_work_order(
    meeting_id: int,
    youtube_video_url: Optional[str] = None,
    priority: int = 0,
    requested_outputs: Optional[str] = None,
) -> int:
    body: Dict[str, Any] = {"meeting_id": int(meeting_id), "priority": int(priority)}
    if youtube_video_url is not None:
        body["youtube_video_url"] = youtube_video_url
    if requested_outputs is not None:
        body["requested_outputs"] = requested_outputs
    payload = _post("/api/worker/work-orders/enqueue", body=body)
    return int((payload or {}).get("work_order_id", 0))


def recover_stale_work_orders(hours: float = 2.0) -> List[Dict]:
    payload = _post("/api/worker/work-orders/recover-stale", body={"hours": float(hours)})
    return (payload or {}).get("recovered", [])


def bump_eligible_failed_to_pending(base_backoff_minutes: int = 5) -> List[Dict]:
    """HTTP-client mirror of database.py:bump_eligible_failed_to_pending (F1, 2026-06-19).

    The HTTP backend (D-099 Phase 2) routes the worker's database calls through Flask,
    so this client-side equivalent composes existing primitives (list_work_orders +
    update_work_order_state) into the same failed→pending bump the SQLite version does.
    Backoff window is evaluated in Python rather than SQLite's datetime() builtin;
    SQLite CURRENT_TIMESTAMP is naive UTC so we compare against datetime.utcnow().

    Backoff progression: retry_count=0 → 5min, =1 → 10min, =2 → 20min after updated_at.
    """
    from datetime import datetime, timedelta

    failed = list_work_orders(state="failed", limit=200)
    if not failed:
        return []
    now = datetime.utcnow()
    backoff_by_retry = {
        0: base_backoff_minutes,
        1: base_backoff_minutes * 2,
        2: base_backoff_minutes * 4,
    }
    eligible: List[Dict] = []
    for wo in failed:
        try:
            retry_count = int(wo.get("retry_count") or 0)
            max_retries = int(wo.get("max_retries") or 3)
        except (TypeError, ValueError):
            continue
        if retry_count >= max_retries or retry_count not in backoff_by_retry:
            continue
        updated_at_raw = wo.get("updated_at")
        if not updated_at_raw:
            continue
        try:
            updated_dt = datetime.strptime(updated_at_raw, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if now - updated_dt < timedelta(minutes=backoff_by_retry[retry_count]):
            continue
        eligible.append(wo)
    for wo in eligible:
        try:
            update_work_order_state(int(wo["id"]), "pending")
        except DatabaseHTTPError as exc:
            logger.warning("bump_eligible: state-update failed for WO %s: %s", wo.get("id"), exc)
    return eligible


# ── Meeting / notebook surface ───────────────────────────────────────


def register_notebook(meeting_id: int, notebook_id: str) -> bool:
    payload = _post(
        f"/api/worker/meetings/{int(meeting_id)}/notebook",
        body={"notebook_id": notebook_id},
    )
    return bool((payload or {}).get("updated"))


def get_meeting_city(meeting_id: int) -> Optional[str]:
    payload = _get(f"/api/worker/meetings/{int(meeting_id)}/city")
    return (payload or {}).get("city_name") if payload else None


def get_resolved_video_url(meeting_id: int) -> Optional[str]:
    payload = _get(f"/api/worker/meetings/{int(meeting_id)}/resolved-video-url")
    # Endpoint returns 200 with {"url": null} when meeting has no video.
    return (payload or {}).get("url") if payload else None


def load_city_intelligence(city_name: str) -> Optional[Dict]:
    # Use requests.utils.quote via params? No — city is in path. The
    # blueprint declares /city-intelligence/<path:city_name> so spaces are
    # tolerated; requests handles URL-encoding internally.
    payload = _get(f"/api/worker/city-intelligence/{city_name}")
    return (payload or {}).get("intelligence") if payload else None


# ── Notebook output surface ──────────────────────────────────────────


def save_notebook_output(
    meeting_id: int,
    notebook_id: str,
    output_type: str,
    content: Optional[str] = None,
    content_url: Optional[str] = None,
    prompt_filename: Optional[str] = None,
    prompt_version: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    body: Dict[str, Any] = {
        "meeting_id": int(meeting_id),
        "notebook_id": notebook_id,
        "output_type": output_type,
    }
    # Pass nullable fields through verbatim — server stores None as
    # NULL, which matches the SQLite contract.
    for key, val in (
        ("content", content),
        ("content_url", content_url),
        ("prompt_filename", prompt_filename),
        ("prompt_version", prompt_version),
        ("error", error),
    ):
        if val is not None:
            body[key] = val
    _post("/api/worker/outputs", body=body)


def is_output_already_present(meeting_id: int, output_type: str) -> Optional[Dict]:
    payload = _get(
        "/api/worker/outputs/check",
        params={"meeting_id": int(meeting_id), "output_type": output_type},
    )
    return (payload or {}).get("existing")


def save_member_attendance_batch(
    meeting_id: int, city_name: str, items: List[Dict],
) -> Dict[str, int]:
    return _post(
        "/api/worker/member-attendance",
        body={"meeting_id": int(meeting_id), "city_name": city_name, "items": items},
    ) or {}


def save_member_quotes_batch(
    meeting_id: int, city_name: str, items: List[Dict],
) -> Dict[str, int]:
    return _post(
        "/api/worker/member-quotes",
        body={"meeting_id": int(meeting_id), "city_name": city_name, "items": items},
    ) or {}


# ── Scanner surface ──────────────────────────────────────────────────


def scan_recent_meetings(cities=None, age_limit_days: Optional[int] = None) -> Dict[str, int]:
    """The PC-side endpoint runs scanner.scan_recent_meetings() against
    PC's local DB and returns its counters. Reached by the operator-triggered
    scan path — the worker's autonomous idle-tick auto-scan was retired with
    the daemon (1A)."""
    body: Dict[str, Any] = {}
    if cities is not None:
        body["cities"] = list(cities)
    if age_limit_days is not None:
        body["age_limit_days"] = int(age_limit_days)
    return _post("/api/worker/scan", body=body, timeout=_SCAN_TIMEOUT) or {}


# ── Connection placeholder ───────────────────────────────────────────
# Anything still calling get_connection() on Mac is a bug — the scanner
# does its work via /api/worker/scan and the fetcher/worker now use the
# typed wrappers above. Surface the bug loudly rather than silently
# half-working.


def get_connection():
    raise NotImplementedError(
        "get_connection() is not available in HTTP-shim mode. Refactor "
        "the caller to use one of the typed database functions, or expose "
        "a new /api/worker/* endpoint for the specific read shape."
    )

"""D-099 Phase 2 C3 — /api/worker/* HTTP endpoints.

Exposes the database.py + scanner + fetcher operations the pipeline
bridge worker needs, so the worker can run on Mac (per D-099 Decision 3)
while PC's SQLite remains the canonical store (per D-099 Decision 1).

Auth: Bearer token in the Authorization header. Token resolution order:
  1. ZSPAN_AGENT_STATE_TOKEN env var
  2. parsers/user_settings.json:zspan_agent_state_token

Endpoints wrap one database.py function each (or a small specialized
read for the two inline-SQL spots in fetcher.py + scanner.py). Same
JSON shapes as the Python functions return so the Mac-side
database_http_client.py (C4) can stay a thin pass-through.

Cross-references:
  - DECISIONS.md § D-099 (parent decision)
  - parsers/api_worker_routes_smoke.py (per-endpoint smoke tests)
"""
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from database import (
    enqueue_work_order,
    get_meeting_city,
    get_resolved_video_url,
    get_work_order,
    is_output_already_present,
    list_work_orders,
    load_city_intelligence,
    next_pending_work_order,
    recover_stale_work_orders,
    register_notebook,
    save_member_attendance_batch,
    save_member_quotes_batch,
    save_notebook_output,
    update_meeting_diarization_status,
    update_work_order_state,
    work_order_stats,
)

logger = logging.getLogger(__name__)

worker_bp = Blueprint("worker_bp", __name__, url_prefix="/api/worker")


# ── Bearer-token auth ────────────────────────────────────────────────


def _resolve_token() -> Optional[str]:
    """Resolve the agent-state bearer token. Env var wins; user_settings
    file is the fallback so the token survives across shell environments."""
    env = os.environ.get("ZSPAN_AGENT_STATE_TOKEN", "").strip()
    if env:
        return env
    try:
        from env_config import load_user_settings
        settings = load_user_settings() or {}
        return (settings.get("zspan_agent_state_token") or "").strip() or None
    except Exception:
        return None


def require_agent_token(fn):
    """Decorator — reject requests without a matching Bearer token."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = _resolve_token()
        if not expected:
            return jsonify({"error": "server has no zspan_agent_state_token configured"}), 500
        auth = (request.headers.get("Authorization") or "").strip()
        if not auth.startswith("Bearer "):
            return jsonify({"error": "missing Bearer token"}), 401
        presented = auth[len("Bearer "):].strip()
        if presented != expected:
            return jsonify({"error": "invalid bearer token"}), 401
        return fn(*args, **kwargs)

    return wrapper


# ── Work-order endpoints ─────────────────────────────────────────────


@worker_bp.route("/work-orders/<int:wo_id>", methods=["GET"])
@require_agent_token
def get_wo(wo_id: int):
    row = get_work_order(wo_id)
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(row), 200


@worker_bp.route("/work-orders/next", methods=["POST"])
@require_agent_token
def next_wo():
    row = next_pending_work_order()
    return jsonify({"work_order": row}), 200


@worker_bp.route("/work-orders/stats", methods=["GET"])
@require_agent_token
def stats():
    return jsonify(work_order_stats()), 200


@worker_bp.route("/work-orders", methods=["GET"])
@require_agent_token
def list_wos():
    state = (request.args.get("state") or "").strip() or None
    city = (request.args.get("city") or "").strip() or None
    try:
        limit = int(request.args.get("limit", "200"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    rows = list_work_orders(state=state, city=city, limit=limit)
    return jsonify({"work_orders": rows, "count": len(rows)}), 200


@worker_bp.route("/work-orders/<int:wo_id>/state", methods=["POST"])
@require_agent_token
def set_wo_state(wo_id: int):
    body = request.get_json(silent=True) or {}
    state = (body.get("state") or "").strip()
    if not state:
        return jsonify({"error": "state is required"}), 400

    kwargs: dict[str, Any] = {}
    if "error" in body:
        # body["error"] can be a string OR None; pass through as-is.
        kwargs["error"] = body["error"]
    if body.get("notebook_id") is not None:
        kwargs["notebook_id"] = body["notebook_id"]
    if body.get("youtube_video_url") is not None:
        kwargs["youtube_video_url"] = body["youtube_video_url"]
    if body.get("increment_retry"):
        kwargs["increment_retry"] = True

    try:
        update_work_order_state(wo_id, state, **kwargs)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True}), 200


@worker_bp.route("/work-orders/enqueue", methods=["POST"])
@require_agent_token
def enqueue_wo():
    body = request.get_json(silent=True) or {}
    meeting_id = body.get("meeting_id")
    if not isinstance(meeting_id, int):
        return jsonify({"error": "meeting_id (int) is required"}), 400
    wo_id = enqueue_work_order(
        meeting_id=meeting_id,
        youtube_video_url=body.get("youtube_video_url"),
        priority=int(body.get("priority", 0) or 0),
        requested_outputs=body.get("requested_outputs"),
    )
    return jsonify({"work_order_id": wo_id}), 200


# ── Meeting / notebook endpoints ──────────────────────────────────────


@worker_bp.route("/meetings/<int:meeting_id>/notebook", methods=["POST"])
@require_agent_token
def set_meeting_notebook(meeting_id: int):
    body = request.get_json(silent=True) or {}
    notebook_id = (body.get("notebook_id") or "").strip()
    if not notebook_id:
        return jsonify({"error": "notebook_id is required"}), 400
    updated = register_notebook(meeting_id, notebook_id)
    return jsonify({"updated": updated}), 200


@worker_bp.route("/meetings/<int:meeting_id>/diarization-status", methods=["POST"])
@require_agent_token
def set_meeting_diarization_status(meeting_id: int):
    """Persist the optional diarization substatus without changing WO state."""
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    if not status:
        return jsonify({"error": "status is required"}), 400
    detail = body.get("detail")
    if detail is not None and not isinstance(detail, str):
        return jsonify({"error": "detail must be a string or null"}), 400
    try:
        updated = update_meeting_diarization_status(meeting_id, status, detail)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"updated": updated}), 200


@worker_bp.route("/meetings/<int:meeting_id>/city", methods=["GET"])
@require_agent_token
def get_meeting_city_route(meeting_id: int):
    """Mirror database.get_meeting_city."""
    city = get_meeting_city(meeting_id)
    if city is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"city_name": city}), 200


@worker_bp.route("/meetings/<int:meeting_id>/resolved-video-url", methods=["GET"])
@require_agent_token
def get_resolved_video_url_route(meeting_id: int):
    """Mirror database.get_resolved_video_url — COALESCE
    wo.youtube_video_url over meetings.video_url."""
    url = get_resolved_video_url(meeting_id)
    # Even when row exists, url can legitimately be None (no video). Return
    # 200 with null payload — caller distinguishes None from 404.
    return jsonify({"url": url}), 200


@worker_bp.route("/work-orders/recover-stale", methods=["POST"])
@require_agent_token
def recover_stale_route():
    """Reset 'processing' WOs older than `hours` back to 'pending'. Body:
    {"hours": 2.0} (default 2.0)."""
    body = request.get_json(silent=True) or {}
    try:
        hours = float(body.get("hours", 2.0))
    except (TypeError, ValueError):
        return jsonify({"error": "hours must be a number"}), 400
    recovered = recover_stale_work_orders(hours)
    return jsonify({"recovered": recovered, "count": len(recovered)}), 200


@worker_bp.route("/city-intelligence/<path:city_name>", methods=["GET"])
@require_agent_token
def get_city_intel(city_name: str):
    """Returns the city's `city_intelligence/<slug>.json` payload, or
    null if no file exists. Caller (fetcher.py) handles None gracefully."""
    intel = load_city_intelligence(city_name)
    return jsonify({"intelligence": intel}), 200


# ── Notebook output endpoints ────────────────────────────────────────


@worker_bp.route("/outputs", methods=["POST"])
@require_agent_token
def save_output():
    body = request.get_json(silent=True) or {}
    meeting_id = body.get("meeting_id")
    notebook_id = body.get("notebook_id")
    output_type = body.get("output_type")
    if not isinstance(meeting_id, int):
        return jsonify({"error": "meeting_id (int) is required"}), 400
    if not isinstance(notebook_id, str) or not notebook_id:
        return jsonify({"error": "notebook_id (str) is required"}), 400
    if not isinstance(output_type, str) or not output_type:
        return jsonify({"error": "output_type (str) is required"}), 400
    save_notebook_output(
        meeting_id=meeting_id,
        notebook_id=notebook_id,
        output_type=output_type,
        content=body.get("content"),
        content_url=body.get("content_url"),
        prompt_filename=body.get("prompt_filename"),
        prompt_version=body.get("prompt_version"),
        error=body.get("error"),
    )
    return jsonify({"ok": True}), 200


@worker_bp.route("/outputs/check", methods=["GET"])
@require_agent_token
def check_output():
    try:
        meeting_id = int(request.args.get("meeting_id", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "meeting_id (int) is required"}), 400
    output_type = (request.args.get("output_type") or "").strip()
    if not output_type:
        return jsonify({"error": "output_type is required"}), 400
    existing = is_output_already_present(meeting_id, output_type)
    return jsonify({"existing": existing}), 200


@worker_bp.route("/member-attendance", methods=["POST"])
@require_agent_token
def save_attendance():
    body = request.get_json(silent=True) or {}
    meeting_id = body.get("meeting_id")
    city_name = body.get("city_name")
    items = body.get("items")
    if not isinstance(meeting_id, int):
        return jsonify({"error": "meeting_id (int) is required"}), 400
    if not isinstance(city_name, str) or not city_name:
        return jsonify({"error": "city_name (str) is required"}), 400
    if not isinstance(items, list):
        return jsonify({"error": "items (list) is required"}), 400
    counts = save_member_attendance_batch(meeting_id, city_name, items)
    return jsonify(counts), 200


@worker_bp.route("/member-quotes", methods=["POST"])
@require_agent_token
def save_quotes():
    body = request.get_json(silent=True) or {}
    meeting_id = body.get("meeting_id")
    city_name = body.get("city_name")
    items = body.get("items")
    if not isinstance(meeting_id, int):
        return jsonify({"error": "meeting_id (int) is required"}), 400
    if not isinstance(city_name, str) or not city_name:
        return jsonify({"error": "city_name (str) is required"}), 400
    if not isinstance(items, list):
        return jsonify({"error": "items (list) is required"}), 400
    counts = save_member_quotes_batch(meeting_id, city_name, items)
    return jsonify(counts), 200


# ── Scanner endpoint ─────────────────────────────────────────────────


@worker_bp.route("/scan", methods=["POST"])
@require_agent_token
def scan():
    """Run scanner.scan_recent_meetings() on PC and return its counters.

    The scan itself touches many rows; keeping it on PC means Mac doesn't
    have to ship the meetings table over the wire. Reached by the
    operator-triggered scan (`/api/work-orders/scan`) — the worker's
    autonomous idle-tick auto-scan was retired with the daemon (1A).

    Optional body:
        {"cities": ["Kingman", ...], "age_limit_days": 30}
    """
    body = request.get_json(silent=True) or {}
    cities = body.get("cities")
    age_limit_days = body.get("age_limit_days")

    # Lazy import — keeps module import light; scanner deps load only
    # don't want to load at api_server startup.
    import sys
    from pathlib import Path
    bridge_parent = Path(__file__).resolve().parent.parent.parent / "zspan_pipeline"
    bridge_root = bridge_parent.parent
    if str(bridge_root) not in sys.path:
        sys.path.insert(0, str(bridge_root))
    from zspan_pipeline.scanner import scan_recent_meetings  # noqa: E402

    kwargs: dict[str, Any] = {}
    if cities is not None:
        if not isinstance(cities, list):
            return jsonify({"error": "cities must be a list"}), 400
        kwargs["cities"] = cities
    if age_limit_days is not None:
        try:
            kwargs["age_limit_days"] = int(age_limit_days)
        except (TypeError, ValueError):
            return jsonify({"error": "age_limit_days must be an int"}), 400

    counters = scan_recent_meetings(**kwargs)
    return jsonify(counters), 200


# ── Health endpoint (no auth — used to probe reachability) ───────────


@worker_bp.route("/health", methods=["GET"])
def health():
    """Public reachability probe. No auth — Mac calls this before invoke
    to confirm the Flask is reachable. Don't expose anything sensitive."""
    return jsonify({
        "service": "zspan-worker-api",
        "endpoints": [
            "GET    /api/worker/health",
            "GET    /api/worker/work-orders/<id>",
            "POST   /api/worker/work-orders/next",
            "GET    /api/worker/work-orders/stats",
            "GET    /api/worker/work-orders[?state=&city=&limit=]",
            "POST   /api/worker/work-orders/<id>/state",
            "POST   /api/worker/work-orders/enqueue",
            "POST   /api/worker/work-orders/recover-stale",
            "POST   /api/worker/meetings/<id>/notebook",
            "POST   /api/worker/meetings/<id>/diarization-status",
            "GET    /api/worker/meetings/<id>/city",
            "GET    /api/worker/meetings/<id>/resolved-video-url",
            "GET    /api/worker/city-intelligence/<city_name>",
            "POST   /api/worker/outputs",
            "GET    /api/worker/outputs/check?meeting_id=&output_type=",
            "POST   /api/worker/member-attendance",
            "POST   /api/worker/member-quotes",
            "POST   /api/worker/scan",
        ],
        "auth": "Bearer ZSPAN_AGENT_STATE_TOKEN (env or user_settings.json) on all non-health endpoints",
    }), 200

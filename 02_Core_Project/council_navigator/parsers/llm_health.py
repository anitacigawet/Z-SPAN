"""llm_health — lightweight in-memory counters for the three gpt-4o-mini helpers
====================================================================================

There are three mechanical-helper LLM call sites in Z-SPAN:
  - `quote_cleaner.clean_quote`            — T-011 filler-word stripper
  - `quote_cleaner.polish_for_display`     — D-054 readability polish
  - `verdict_emphasis.extract_verdict_emphasis` — D-054 red-highlight tokens

All three use gpt-4o-mini, all three live behind locked prompts that the
operator never sees. When one starts silently failing (API key revoked,
rate limited, model deprecated, OpenAI outage), the operator notices only
when fields go blank on DisputedQuotesPage / VocabularyInboxPage — by which
point the failure has been happening for a while.

This module is the lightest thing that closes that visibility gap: each
helper records ok/fail on completion, and a Flask endpoint exposes the
current snapshot. No persistence — counters reset on Flask restart. The
goal is catching active drift, not historical analysis.

Future S-004 cross-reference: the agent-employees layer will likely want
richer observability (per-agent token spend, escalation rate, etc.). This
module is the seed pattern — the agent-side counters can use the same
shape.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

# Thread-safety: ThreadPoolExecutor in _populate_disputed_display_cache and
# quote_display_precompute means multiple workers may record_ok / record_fail
# concurrently. The dict mutation is small enough that a single lock around
# read+write is fine — contention will be microseconds.
_lock = threading.Lock()

# Schema per helper: {ok, fail, last_error, last_error_at, last_call_at}
_state: Dict[str, Dict[str, Any]] = {
    "clean_quote": {"ok": 0, "fail": 0, "last_error": None, "last_error_at": None, "last_call_at": None},
    "polish_for_display": {"ok": 0, "fail": 0, "last_error": None, "last_error_at": None, "last_call_at": None},
    "extract_verdict_emphasis": {"ok": 0, "fail": 0, "last_error": None, "last_error_at": None, "last_call_at": None},
}


def _now_iso() -> str:
    """ISO timestamp in UTC for last_error_at / last_call_at."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_ok(helper: str) -> None:
    """Record a successful call to one of the three known helpers.

    Unknown helper names are silently ignored (defensive — we'd rather not
    raise from a logging path and break the actual call).
    """
    with _lock:
        s = _state.get(helper)
        if s is None:
            return
        s["ok"] += 1
        s["last_call_at"] = _now_iso()


def record_fail(helper: str, error: Optional[str]) -> None:
    """Record a failed call. `error` is truncated to 200 chars for the snapshot."""
    with _lock:
        s = _state.get(helper)
        if s is None:
            return
        s["fail"] += 1
        if error is not None:
            s["last_error"] = str(error)[:200]
        s["last_error_at"] = _now_iso()
        s["last_call_at"] = _now_iso()


def get_snapshot() -> Dict[str, Any]:
    """Return a copy of current counter state, plus a derived health verdict.

    Health verdict per helper:
      - "ok"           — fail rate below 20% OR fewer than 5 calls total
      - "degraded"     — fail rate >= 20% with at least 5 calls
      - "broken"       — fail rate >= 50% with at least 5 calls

    Plus a top-level `overall` field that's the worst of the three.
    """
    with _lock:
        snap = {k: dict(v) for k, v in _state.items()}

    def _verdict(s: Dict[str, Any]) -> str:
        total = s["ok"] + s["fail"]
        if total < 5:
            return "ok"
        fail_rate = s["fail"] / total
        if fail_rate >= 0.5:
            return "broken"
        if fail_rate >= 0.2:
            return "degraded"
        return "ok"

    for k, s in snap.items():
        s["total"] = s["ok"] + s["fail"]
        s["fail_rate"] = round(s["fail"] / s["total"], 3) if s["total"] else 0.0
        s["verdict"] = _verdict(s)

    # Worst-of-three is the overall verdict.
    rank = {"ok": 0, "degraded": 1, "broken": 2}
    worst = max(snap.values(), key=lambda s: rank[s["verdict"]])
    snap["_overall"] = {
        "verdict": worst["verdict"],
        "any_failures": any(s["fail"] > 0 for k, s in snap.items() if not k.startswith("_")),
    }
    return snap


def reset() -> None:
    """Reset all counters. Used by tests; not exposed via Flask."""
    with _lock:
        for s in _state.values():
            s["ok"] = 0
            s["fail"] = 0
            s["last_error"] = None
            s["last_error_at"] = None
            s["last_call_at"] = None

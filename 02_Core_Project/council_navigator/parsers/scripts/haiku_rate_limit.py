"""S-036 V1-complete — D-078 persisted invocation counter for the Haiku HTML scraper.

Implements the structural defenses D-078 mandates for any API integration whose
billing surface is "soft-capped" — including consumer subscriptions whose
provider-side rate limit is generous enough that a runaway loop would waste a
real amount of quota before the operator notices.

The Haiku scraper runs under the Max subscription (flat-rate, no per-call $),
but D-078 explicitly extends the discipline to "any-API-class even if free now"
since the orchestrator's autonomous-trigger paths could fire this wrapper
hundreds of times overnight in a bad-loop scenario. The counter is the
structural wall that catches that scenario before it becomes a wasted-quota
incident.

Three defenses, applied in order:

  1. **Per-day invocation ceiling** — count invocations recorded in
     balance_ledger today (UTC midnight rollover); refuse if the next one
     would exceed `MAX_INVOCATIONS_PER_DAY`. Hardcoded constant; only
     changeable via code edit (git-auditable, per D-078's structural-wall
     principle).

  2. **Wall-clock cooldown** — if the most recent recorded invocation ended
     less than `MIN_COOLDOWN_SECONDS` ago, refuse. Cheap defense against
     tight-loop runaway even when the daily ceiling is high.

  3. **Soft threshold warning** — when today's count passes
     `ESCALATION_THRESHOLD_FRACTION` of the ceiling, log a warning. Doesn't
     block; surfaces the approach so the operator can intervene.

D-078 item 5 (anomaly refusal: spend exceeds projected by >5% → refuse all +
escalate) is implemented in soft form: the 90% threshold warns, the 100%
ceiling blocks. Slack escalation can layer on top later via slack_notifier;
the structural wall is here.

Persistence shape (matches D-078 item 1 "persist a daily counter in
balance_ledger"):

  provider          = "claude_haiku_scraper"
  event_type        = "invocation"
  bucket_start_time = wall-clock start (epoch seconds; per-invocation unique)
  bucket_end_time   = wall-clock end   (epoch seconds)
  amount_cents      = 0 (not a $ event; uses the ledger for durability)
  source            = caller name (typically "haiku_html_scrape.py")
  notes             = "city=<x> | url=<y> | exit=<n>" — operator-readable
  external_ref      = .jsonl log path so audit links to the full trace

Configuration constants are at the top of this file — CODE-EDIT ONLY. Per
D-078's structural-wall principle, runtime-toggleable ceilings defeat the
purpose. A code edit + commit is the auditable change path.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Bootstrap import path — the wrapper may invoke us from parsers/scripts/ or
# from parsers/ depending on the cwd; ensure `import database` works either way.
_HERE = Path(__file__).resolve().parent
_PARSERS_DIR = _HERE.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

try:
    import database  # type: ignore
except ImportError:
    from parsers import database  # type: ignore


logger = logging.getLogger(__name__)


# ── Doctrine constants (D-078; CODE-EDIT ONLY — never runtime-toggleable) ──

# Per-day invocation ceiling. Calibrated for the Maricopa Legistar V1-complete
# validation set (3 cities) with substantial headroom for retries / re-runs /
# ad-hoc operator probes. Resets at UTC midnight per D-078 item 1.
MAX_INVOCATIONS_PER_DAY = 50

# Wall-clock seconds between consecutive invocations to the same provider.
# The recon's average invocation was ~30s end-to-end, so 5s cooldown is
# invisible in normal operation but catches a fast-fire runaway loop.
MIN_COOLDOWN_SECONDS = 5

# When today's count passes this fraction of MAX, log a warning so the
# operator knows the ceiling is approaching. Doesn't block; just warns.
ESCALATION_THRESHOLD_FRACTION = 0.90

# Ledger keys — see module docstring for the row shape.
PROVIDER = "claude_haiku_scraper"
EVENT_TYPE = "invocation"


class HaikuRateLimitError(Exception):
    """Raised when an invocation would violate a D-078 structural defense.

    `reason_code` is one of "daily_ceiling" | "cooldown"; callers surface it
    plain-language to the operator without translation.
    """
    def __init__(self, reason_code: str, message: str, *, current_count: int = 0):
        super().__init__(message)
        self.reason_code = reason_code
        self.current_count = current_count


@dataclass
class InvocationReservation:
    """Returned by `check_and_reserve_invocation`. The caller threads the
    `started_at_epoch_s` field into `record_invocation_complete` after the
    invocation finishes so the bucket_start/end pair brackets the real call.
    """
    started_at_epoch_s: int
    today_count_before_this_call: int


def _today_utc_start_epoch_s(now_provider=None) -> int:
    """UTC midnight start of today.

    D-078 item 1 specifies UTC midnight as the daily reset boundary so the
    ceiling behaves identically regardless of operator timezone.
    """
    now = (now_provider or _wall_clock_now)()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day_start.timestamp())


def _wall_clock_now() -> datetime:
    """Indirection point for tests — production reads real UTC."""
    return datetime.now(timezone.utc)


def _count_invocations_today() -> int:
    """Count invocation rows whose bucket_start_time falls in the current UTC day."""
    today_start = _today_utc_start_epoch_s()
    conn = database.get_connection()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM balance_ledger
            WHERE provider = ?
              AND event_type = ?
              AND bucket_start_time >= ?
            """,
            (PROVIDER, EVENT_TYPE, today_start),
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


def _last_invocation_end_epoch_s() -> Optional[int]:
    """Most recent invocation's bucket_end_time, or None if no prior."""
    conn = database.get_connection()
    try:
        row = conn.execute(
            """
            SELECT bucket_end_time
            FROM balance_ledger
            WHERE provider = ? AND event_type = ?
            ORDER BY bucket_end_time DESC
            LIMIT 1
            """,
            (PROVIDER, EVENT_TYPE),
        ).fetchone()
        if row is None or row["bucket_end_time"] is None:
            return None
        return int(row["bucket_end_time"])
    finally:
        conn.close()


def check_and_reserve_invocation(
    *,
    city: Optional[str] = None,  # currently unused; reserved for future per-city ceilings
    url: Optional[str] = None,   # currently unused; reserved for future per-host ceilings
) -> InvocationReservation:
    """Run D-078 structural defenses before an invocation. Raises on violation.

    Should be called BEFORE the `claude -p` subprocess fires. On clean return,
    the caller proceeds with the invocation. On `HaikuRateLimitError`, the
    caller refuses + surfaces the reason to the operator (the exception's
    message is plain-language and operator-facing).

    After the invocation completes (success OR failure — both states count
    toward the ceiling because both consume quota), the caller must call
    `record_invocation_complete` with the reservation returned here.
    """
    # Defense 1 — per-day invocation ceiling.
    today_count = _count_invocations_today()
    if today_count + 1 > MAX_INVOCATIONS_PER_DAY:
        raise HaikuRateLimitError(
            reason_code="daily_ceiling",
            message=(
                f"D-078 daily ceiling hit — {today_count} Haiku scraper "
                f"invocations recorded today; one more would exceed "
                f"{MAX_INVOCATIONS_PER_DAY}/day. Counter resets at UTC "
                f"midnight. To change the ceiling, edit "
                f"MAX_INVOCATIONS_PER_DAY in haiku_rate_limit.py and "
                f"commit (code-edit-only per D-078)."
            ),
            current_count=today_count,
        )

    # Defense 2 — wall-clock cooldown. Skip if elapsed is negative (system
    # clock skew or out-of-order writes) rather than blocking forever.
    last_end = _last_invocation_end_epoch_s()
    if last_end is not None:
        now = int(time.time())
        elapsed = now - last_end
        if 0 <= elapsed < MIN_COOLDOWN_SECONDS:
            raise HaikuRateLimitError(
                reason_code="cooldown",
                message=(
                    f"D-078 wall-clock cooldown — last Haiku scraper "
                    f"invocation ended {elapsed}s ago; minimum "
                    f"{MIN_COOLDOWN_SECONDS}s between calls. Try again "
                    f"in {MIN_COOLDOWN_SECONDS - elapsed}s."
                ),
                current_count=today_count,
            )

    # Defense 3 — soft threshold warning. Doesn't block; surfaces approach.
    threshold = int(MAX_INVOCATIONS_PER_DAY * ESCALATION_THRESHOLD_FRACTION)
    if today_count + 1 > threshold:
        logger.warning(
            "haiku_rate_limit: approaching ceiling — invocation "
            "%d/%d (threshold %d). Operator should review usage pattern.",
            today_count + 1, MAX_INVOCATIONS_PER_DAY, threshold,
        )

    return InvocationReservation(
        started_at_epoch_s=int(time.time()),
        today_count_before_this_call=today_count,
    )


def record_invocation_complete(
    reservation: InvocationReservation,
    *,
    exit_code: int,
    log_path: Optional[Path] = None,
    city: Optional[str] = None,
    url: Optional[str] = None,
    source: str = "haiku_html_scrape.py",
) -> Optional[int]:
    """Persist the completed invocation to balance_ledger.

    Call this AFTER the `claude -p` subprocess returns (whether it exited 0
    or non-zero). Both success and failure are counted because both consumed
    quota.

    Returns the new row's id, or None if a UNIQUE-constraint collision (which
    shouldn't happen at second-resolution granularity but the underlying
    helper is INSERT OR IGNORE so we tolerate it gracefully).
    """
    completed_at = int(time.time())

    notes_parts = []
    if city:
        notes_parts.append(f"city={city}")
    if url:
        url_short = url if len(url) <= 200 else url[:197] + "..."
        notes_parts.append(f"url={url_short}")
    notes_parts.append(f"exit={exit_code}")
    notes = " | ".join(notes_parts)

    return database.append_ledger_event(
        provider=PROVIDER,
        event_type=EVENT_TYPE,
        amount_cents=0,
        bucket_start_time=reservation.started_at_epoch_s,
        bucket_end_time=completed_at,
        source=source,
        notes=notes,
        external_ref=str(log_path) if log_path else None,
    )

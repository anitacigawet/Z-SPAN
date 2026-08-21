"""
parser_health — S-062 V0 logic, DORMANT.

Per James 2026-06-19 (post speed-audit): an automatic background system
he'd eventually forget about — one randomly pinging thousands of city
endpoints — must never exist. So the health-check LOGIC gets built, but
nothing activates and nothing is scheduled for testing; activation
questions wait until well after the build ships.

This module is the BUILT-NOT-ACTIVATED logic. The functions below are
callable + tested by shape (data interfaces lock in now so the future
activation is a wire-in, not a redesign). NO scheduler invokes them. They
sit dormant until a future operator decision flips them on.

Original wire-in target was `auto_scraper.py`'s inner per-city loop,
retired per D-169 (parser scraper daemon retirement). The new wire-in
target when it lands: whichever surface replaces ScrapeStatusPanel — the
BitTorrent parser-view redesign's per-city on-demand scrape callsite.

────────────────────────────────────────────────────────────────────────
Surface (the locked-in interface):

    classify_run(current_count, baseline_count) → (status, reason, delta_pct)
        Pure function. Returns one of "ok" | "empty" | "degraded" | "error"
        per the F8 success/empty/degraded/error convention in CLAUDE.md §
        Conventions ("Distinguish 'succeeded-empty' from 'failed-silent'
        in any API/wrapper return shape").

    compute_baseline(city_name, window_days=14) → Optional[float]
        Rolling-mean meetings_found over the last `window_days` of
        SUCCESSFUL scrapes for this city. Returns None if no successful
        scrapes in the window (insufficient history to baseline against).

    classify_and_record(city_name, current_count, scrape_log_id, window_days=14)
        Composed: compute_baseline + classify_run + write to
        parser_health_alerts if status != "ok". Returns the alert id on
        write, None on no-write (status == ok). This is the function that
        the future per-city-scrape wire-in will call (post-D-169 the target
        is the BitTorrent parser-view redesign's per-city scrape callsite).
        NOT called today.

    list_open_alerts(city_name=None, status=None, limit=200)
        Operator-side query helper. Returns rows where acknowledged_at IS NULL.

    acknowledge_alert(alert_id, acknowledged_by)
        Operator-side dismissal. Sets acknowledged_at + acknowledged_by.

────────────────────────────────────────────────────────────────────────
F8 status enum (anchor: CLAUDE.md § "succeeded-empty vs failed-silent"):

    ok        — current_count > 0, OR baseline is None (insufficient
                history), OR baseline == 0 (genuinely-empty city — e.g.
                Colorado City). Healthy.

    empty     — current_count == 0 AND baseline > 0. Silently-lost
                pattern: the parser used to find meetings + now finds
                zero. Prime suspect for stale CMS/website restructuring
                (cf. the Maricopa polish discovery 2026-06-* where 6/7
                parsers were quietly stale).

    degraded  — current_count > 0 AND dropped >50% vs baseline. Probably
                partial parse: structural drift in the site that's
                eating SOME rows but not all. Worth investigation but
                not as urgent as empty.

    error     — scrape itself raised / returned None. Already captured in
                scrape_log.success=0; surfaced here for unified
                dashboarding when the wire-in fires from a TRY/EXCEPT
                wrapper instead of the success path.

────────────────────────────────────────────────────────────────────────
Activation wire-in (FOR FUTURE OPERATOR REVIEW — NOT TODAY):

    # in the future per-city-scrape callsite (BitTorrent redesign),
    # AFTER a successful save_scrape_log call:
    from parser_health import classify_and_record
    classify_and_record(
        city_name=city,
        current_count=meetings_found,
        scrape_log_id=scrape_log_id,
    )

    # operator surface (ParserDashboard read path):
    from parser_health import list_open_alerts
    open_alerts = list_open_alerts()

    # operator dismissal:
    from parser_health import acknowledge_alert
    acknowledge_alert(alert_id, acknowledged_by=current_user)

When activation lands, also surface in ParserDashboard + flip
`status` in parser_index.json based on the rolling alert state. That's
follow-on UI work, NOT in scope here.
────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple, List, Dict

from database import get_connection

logger = logging.getLogger(__name__)

# F8 status enum — locked-in vocabulary
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_DEGRADED = "degraded"
STATUS_ERROR = "error"

# Tunable thresholds (deliberately conservative defaults; revisit at activation)
DEFAULT_BASELINE_WINDOW_DAYS = 14
DEGRADED_DROP_THRESHOLD = 0.5  # >50% drop


def classify_run(
    current_count: int,
    baseline_count: Optional[float],
) -> Tuple[str, str, Optional[float]]:
    """
    Pure classifier. Inputs are post-scrape state; output is the F8 verdict.

    Returns: (status, reason, delta_pct)
      status: one of STATUS_OK / STATUS_EMPTY / STATUS_DEGRADED / STATUS_ERROR
      reason: human-readable trigger explanation
      delta_pct: percent change vs baseline; None when baseline is None/0

    NOTE: STATUS_ERROR is NOT inferred here — the scrape-itself-failed case
    must be passed in by the caller (the call-site has the exception
    context that this pure function lacks). This function only distinguishes
    the SUCCESS-path verdicts (ok/empty/degraded).
    """
    # Insufficient baseline history → can't make a judgment. Default to ok.
    if baseline_count is None:
        return (
            STATUS_OK,
            f"no baseline yet; current={current_count}",
            None,
        )

    # Baseline of zero means city has historically yielded zero meetings
    # (e.g. Colorado City — genuinely no archive). Current zero is honest-empty.
    if baseline_count == 0:
        if current_count > 0:
            return (
                STATUS_OK,
                f"baseline=0 → current={current_count} is new activity",
                None,
            )
        return (
            STATUS_OK,
            "baseline=0 and current=0; honest-empty city",
            0.0,
        )

    # Baseline > 0; compute delta
    delta = (current_count - baseline_count) / baseline_count
    delta_pct = delta * 100.0

    if current_count == 0:
        return (
            STATUS_EMPTY,
            f"baseline={baseline_count:.1f} → current=0; silently-lost pattern",
            delta_pct,
        )

    if delta <= -DEGRADED_DROP_THRESHOLD:
        return (
            STATUS_DEGRADED,
            f"dropped {abs(delta_pct):.0f}% (baseline={baseline_count:.1f} → current={current_count})",
            delta_pct,
        )

    return (
        STATUS_OK,
        f"within tolerance (delta={delta_pct:+.0f}%)",
        delta_pct,
    )


def compute_baseline(
    city_name: str,
    window_days: int = DEFAULT_BASELINE_WINDOW_DAYS,
) -> Optional[float]:
    """
    Rolling-mean meetings_found over the last `window_days` of SUCCESSFUL
    scrapes for `city_name`. Excludes rows where auto_suppressed=1 (operator
    has flagged the run as not-representative).

    Returns None if fewer than 2 successful, un-suppressed scrapes in window
    (insufficient signal to baseline against).
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT meetings_found
            FROM scrape_log
            WHERE city_name = ?
              AND success = 1
              AND COALESCE(auto_suppressed, 0) = 0
              AND scraped_at >= datetime('now', ?)
            """,
            (city_name, f"-{int(window_days)} days"),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        return None

    counts = [r[0] or 0 for r in rows]
    return sum(counts) / len(counts)


def classify_and_record(
    city_name: str,
    current_count: int,
    scrape_log_id: Optional[int] = None,
    window_days: int = DEFAULT_BASELINE_WINDOW_DAYS,
    error_message: Optional[str] = None,
) -> Optional[int]:
    """
    Compose: compute_baseline + classify_run + persist alert row if status
    is not ok.

    If `error_message` is non-None, this run is the EXCEPTION-path
    (scrape itself failed); status is forced to STATUS_ERROR.

    Returns: the parser_health_alerts.id of the written row, or None if
    no row was written (status == ok).

    DORMANT NOTE: this function is the activation surface for the future
    per-city-scrape wire-in (the BitTorrent parser-view redesign that
    replaces ScrapeStatusPanel, post-D-169). Not called from anywhere in
    the codebase today. Tests / smoke runs of this module are explicitly
    deferred per James 2026-06-19.
    """
    if error_message is not None:
        status = STATUS_ERROR
        reason = f"scrape exception: {error_message[:200]}"
        baseline = None
        delta_pct = None
    else:
        baseline = compute_baseline(city_name, window_days=window_days)
        status, reason, delta_pct = classify_run(current_count, baseline)

    if status == STATUS_OK:
        return None

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO parser_health_alerts
                (city_name, status, current_count, baseline_count,
                 baseline_window_days, delta_pct, reason, raw_scrape_log_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                city_name,
                status,
                current_count,
                baseline,
                window_days,
                delta_pct,
                reason,
                scrape_log_id,
            ),
        )
        alert_id = cursor.lastrowid
        conn.commit()
        logger.info(
            "parser_health alert recorded city=%s status=%s id=%s reason=%s",
            city_name, status, alert_id, reason,
        )
        return alert_id
    finally:
        conn.close()


def list_open_alerts(
    city_name: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
) -> List[Dict]:
    """
    Operator-side query helper. Returns un-acknowledged alerts, optionally
    filtered by city and/or status. Most recent first.

    Each row dict includes: id, city_name, detected_at, status,
    current_count, baseline_count, baseline_window_days, delta_pct, reason,
    raw_scrape_log_id.
    """
    where = ["acknowledged_at IS NULL"]
    params: list = []
    if city_name:
        where.append("city_name = ?")
        params.append(city_name)
    if status:
        where.append("status = ?")
        params.append(status)
    where_sql = " AND ".join(where)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT id, city_name, detected_at, status, current_count,
                   baseline_count, baseline_window_days, delta_pct, reason,
                   raw_scrape_log_id
            FROM parser_health_alerts
            WHERE {where_sql}
            ORDER BY detected_at DESC
            LIMIT ?
            """,
            (*params, int(limit)),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]
    finally:
        conn.close()


def acknowledge_alert(alert_id: int, acknowledged_by: str) -> bool:
    """
    Operator dismissal of an alert. Sets acknowledged_at + acknowledged_by.
    Returns True if a row was updated, False if no matching un-acknowledged
    row was found.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE parser_health_alerts
            SET acknowledged_at = CURRENT_TIMESTAMP,
                acknowledged_by = ?
            WHERE id = ? AND acknowledged_at IS NULL
            """,
            (acknowledged_by, int(alert_id)),
        )
        updated = cursor.rowcount
        conn.commit()
        return updated > 0
    finally:
        conn.close()

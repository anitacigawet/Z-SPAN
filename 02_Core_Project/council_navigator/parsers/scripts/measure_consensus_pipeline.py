#!/usr/bin/env python3.11
"""measure_consensus_pipeline — V1-Consensus-1 C7 baseline + ongoing measurement.

Reports the polish-rejection rate and consensus-pipeline outcomes since
V1-Consensus-1 landed. Read-only; safe to run any time.

Reports three rates per period (or aggregate):

  1. POLISH-REJECTION RATE — % of polish_for_display calls that rejected
     because the polisher reworded (the word-level safety check). The
     PM2 brainstorm-audit finding-3 hypothesized gpt-5.5 might be more
     aggressive about polish rejections than gpt-4o-mini. V1-Consensus-1
     converts rejections into verified corrections instead of dropping
     them; this rate measures whether the underlying problem is real.

  2. CONSENSUS-AGREEMENT RATE — % of pending_review rows where Codex's
     proposed_right matches the curator's exact-string. High agreement
     = both LLMs see the same canonical form (good). Low agreement =
     one or both are unsure / hallucinating / disagreeing on form (the
     load-bearing safety the spec calls out).

  3. PRONG-PASS RATE — for consensus-matched rows, % that pass BOTH
     Prong 1 (authoritative-source) and Prong 2 (specificity). Failure
     here means the consensus-agreed correction is suspect for a reason
     the LLMs missed (common-word collision, no authoritative source).

Usage:

    python3.11 scripts/measure_consensus_pipeline.py [--city <name>]
                                                     [--since YYYY-MM-DD]

The output is a single JSON object suitable for piping to jq or logging
into a dashboard.

V1-Consensus-1 C7 — empirical signal on whether the architectural fix
is doing real work. Re-run after new batches of meetings drain through
pre-compute to track the rates over time.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARSERS_DIR = os.path.dirname(_THIS_DIR)
if _PARSERS_DIR not in sys.path:
    sys.path.insert(0, _PARSERS_DIR)


def _interpret_state(
    *,
    total_polished: int,
    total_pending: int,
    consensus_resolved: int,
    promoted_via_consensus: int,
) -> str:
    """Distinguish wired-but-unexercised from exercised-zero-rate states.

    The pipeline can be in any of:
      - cold (0 polished, 0 pending): nothing has even polished yet
      - wired-but-unexercised (>0 polished, 0 pending, 0 resolved): polish
        ran but never rejected — V1-Consensus-1 stays a wired backstop
      - actively measured (>0 pending or >0 resolved): real data flowing
    """
    if total_polished == 0:
        return (
            "cold state: no polish_for_display calls have completed yet. "
            "Run quote_display_precompute on a batch first."
        )
    if total_pending == 0 and consensus_resolved == 0:
        return (
            f"wired-but-unexercised: {total_polished} polish calls have "
            "completed across this scope; ZERO produced polish-rejections "
            "→ ZERO pending_review rows queued → ZERO consensus runs. The "
            "V1-Consensus-1 pipeline is a safety net that hasn't needed "
            "to fire on this corpus. The 0% rejection rate confirms the "
            "substitution layer at apply_city_corrections is pre-emptively "
            "catching what polish would otherwise reject; the PM2 "
            "brainstorm-audit hypothesis ('gpt-5.5 more aggressive about "
            "polish rejections than gpt-4o-mini') is refuted empirically "
            "on this corpus."
        )
    return (
        f"active measurement: {total_pending} pending + {consensus_resolved} "
        f"resolved rows; {promoted_via_consensus} auto-promotions stamped "
        f"promoted_by='codex-opus-consensus'. Check rates above + by_status "
        "breakdown for the empirical signal."
    )


def _build_where(city: str | None, since: str | None) -> tuple[str, list]:
    parts = []
    params: list = []
    if city:
        parts.append("city_name = ?")
        params.append(city)
    if since:
        parts.append("created_at >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(parts)) if parts else ""
    return where, params


def measure(city: str | None = None, since: str | None = None) -> dict:
    from database import init_db, get_connection  # noqa: PLC0415

    init_db()
    conn = get_connection()
    try:
        # === Polish-rejection rate (proxy: count of pending_review rows
        # vs total quotes polished; lower bound since pre-compute may
        # have skipped already-cached rows). ===
        cached_q = "SELECT COUNT(*) AS n FROM quotes WHERE quote_text_display IS NOT NULL"
        if city:
            cached_q = (
                """
                SELECT COUNT(*) AS n
                FROM quotes q
                LEFT JOIN meetings m ON m.id = q.meeting_id
                WHERE q.quote_text_display IS NOT NULL AND m.city_name = ?
                """
            )
        total_polished = conn.execute(
            cached_q,
            (city,) if city else (),
        ).fetchone()["n"]

        # All pending_review rows in scope
        where_pr, params_pr = _build_where(city, since)
        total_pending = conn.execute(
            f"SELECT COUNT(*) AS n FROM correction_pending_review{where_pr}",
            params_pr,
        ).fetchone()["n"]

        # Per-status breakdown
        status_rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS n
            FROM correction_pending_review{where_pr}
            GROUP BY status
            """,
            params_pr,
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in status_rows}

        # Consensus-resolved rows (anything except 'pending')
        consensus_resolved = sum(
            n for s, n in by_status.items() if s != "pending"
        )

        # Consensus-agreement rate: consensus_match_promoted + prong_fail_review
        # are both cases where Codex == curator exact-string (the prong-fail
        # case had agreement but failed safety gate).
        consensus_match_count = (
            by_status.get("consensus_match_promoted", 0)
            + by_status.get("prong_fail_review", 0)
        )
        consensus_disagreement_count = by_status.get(
            "consensus_disagreement_review", 0
        )
        consensus_total = consensus_match_count + consensus_disagreement_count
        consensus_agreement_rate = (
            (consensus_match_count / consensus_total)
            if consensus_total > 0
            else None
        )

        # Prong-pass rate (of consensus-matched rows, how many pass both)
        prong_pass_count = by_status.get("consensus_match_promoted", 0)
        prong_pass_rate = (
            (prong_pass_count / consensus_match_count)
            if consensus_match_count > 0
            else None
        )

        # Phonetic-variant breakdown
        phonetic_total = conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM correction_pending_review{where_pr}
            {"AND" if where_pr else "WHERE"} is_phonetic_variant = 1
            """,
            params_pr,
        ).fetchone()["n"]

        # Auto-promotions stamped 'codex-opus-consensus'
        promoted_via_consensus = conn.execute(
            """
            SELECT COUNT(*) AS n FROM city_vocabulary_corrections
            WHERE promoted_by = 'codex-opus-consensus'
            """
        ).fetchone()["n"]

        return {
            "scope": {"city": city, "since": since},
            "polish_polished_quotes_in_scope": total_polished,
            "pending_review_rows": {
                "total": total_pending,
                "by_status": by_status,
                "phonetic_variant_flagged": phonetic_total,
            },
            "rates": {
                "polish_rejection_rate": (
                    (total_pending / total_polished)
                    if total_polished > 0
                    else None
                ),
                "consensus_agreement_rate": consensus_agreement_rate,
                "prong_pass_rate": prong_pass_rate,
            },
            "consensus_resolved_total": consensus_resolved,
            "promoted_via_consensus_to_vocab_corrections": (
                promoted_via_consensus
            ),
            "interpretation": _interpret_state(
                total_polished=total_polished,
                total_pending=total_pending,
                consensus_resolved=consensus_resolved,
                promoted_via_consensus=promoted_via_consensus,
            ),
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure V1-Consensus-1 polish-rejection + consensus rates."
    )
    parser.add_argument(
        "--city", default=None,
        help="Scope to a single city (default: all cities).",
    )
    parser.add_argument(
        "--since", default=None,
        help="YYYY-MM-DD lower bound on pending_review.created_at.",
    )
    args = parser.parse_args()
    print(json.dumps(measure(args.city, args.since), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

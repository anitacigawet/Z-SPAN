"""quote_display_precompute — shared parallel pre-compute of polish + emphasis
=============================================================================

D-054 display helpers (`quote_text_display` from `quote_cleaner.polish_for_display`,
`verdict_emphasis_tokens` from `verdict_emphasis.extract_verdict_emphasis`) need
to land BEFORE the operator opens DisputedQuotesPage so they don't see a 30s
cold-load delay. Two call sites both want to populate these caches:

  - `zspan_pipeline/scripts/ingest_review_response.py` runs EAGERLY at V3
    ingest time — right after a quote enters `verified_status='disputed'`.
    This is the primary path: by the time the operator opens the disputed
    queue, every newly-disputed quote already has both fields cached.

  - `parsers/api_server._populate_disputed_display_cache` runs LAZILY on
    GET /api/disputed-quotes — defense-in-depth fallback for cases the
    eager path missed (e.g. ingest ran without OPENAI_API_KEY configured
    and the key got added later; or `update_quote_verification` NULLed the
    cache on a text correction).

Both paths funnel through this module's `precompute_display_cache_for_quote_ids`.

Pacing: ThreadPoolExecutor with max_workers=min(8, len(work) * 2). Polish +
emphasis run in parallel per-quote. Across quotes, work is also parallel up
to the pool size. ~3-5s per cold quote, ~30s for an 8-quote batch in practice
(OpenAI latency dominates).

OPENAI_API_KEY not configured: function no-ops cleanly. The lazy path remains
as fallback.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def precompute_display_cache_for_quote_ids(quote_ids: List[int]) -> Dict[str, Any]:
    """Eagerly compute polish + emphasis for the given quote IDs and persist.

    Idempotent: skips rows whose cache fields are already populated. Safe to
    call with a mix of newly-disputed + already-cached IDs.

    Returns a stats dict suitable for logging:
        {
            "requested": int,
            "polished": int,
            "emphasis_extracted": int,
            "skipped_already_cached": int,
            "skipped_no_key": int (only present when no key was configured),
            "error": str (only present on import failure),
        }
    """
    if not quote_ids:
        return {
            "requested": 0,
            "polished": 0,
            "emphasis_extracted": 0,
            "skipped_already_cached": 0,
        }

    # Lazy imports keep ingest-script startup cheap when OPENAI_API_KEY isn't
    # configured (no `requests` / openai resolution unless we need it).
    try:
        from quote_cleaner import polish_for_display, is_configured as polish_is_configured
        from verdict_emphasis import (
            extract_verdict_emphasis,
            is_configured as emphasis_is_configured,
        )
        from database import update_quote_display_cache, get_connection
    except Exception as e:
        logger.warning(
            "display-cache pre-compute imports failed (%s) — skipping", e
        )
        return {"requested": len(quote_ids), "error": str(e)}

    has_polish_key = polish_is_configured()
    has_emphasis_key = emphasis_is_configured()
    if not has_polish_key and not has_emphasis_key:
        logger.info(
            "OPENAI_API_KEY not configured — display-cache pre-compute skipped "
            "(lazy fallback in /api/disputed-quotes will fire if key arrives later)"
        )
        return {
            "requested": len(quote_ids),
            "polished": 0,
            "emphasis_extracted": 0,
            "skipped_no_key": len(quote_ids),
            "skipped_already_cached": 0,
        }

    # Fetch the rows we need to operate on. JOIN meetings for city_name +
    # meeting_id so polish-rejection events can route to the V1-Consensus-1
    # pending-review queue (consensus_vocab.route_polish_rejection_to_consensus).
    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(quote_ids))
        rows = conn.execute(
            f"""
            SELECT q.id, q.quote_text, q.quote_text_display,
                   q.verdict_emphasis_tokens, q.gemini_correction_notes,
                   q.meeting_id, m.city_name
            FROM quotes q
            LEFT JOIN meetings m ON m.id = q.meeting_id
            WHERE q.id IN ({placeholders})
            """,
            quote_ids,
        ).fetchall()
    finally:
        conn.close()

    # Decide per-row what work is needed. Skip rows already cached.
    work: List[Dict[str, Any]] = []
    skipped_already_cached = 0
    for row in rows:
        row_dict = dict(row)
        audit_raw = row_dict.get("gemini_correction_notes")
        verdict_dict = None
        if audit_raw:
            try:
                audit = json.loads(audit_raw)
                verdict_dict = (audit or {}).get("raw_gemini_verdict")
            except (json.JSONDecodeError, TypeError):
                verdict_dict = None
        need_polish = (
            has_polish_key
            and bool(row_dict.get("quote_text"))
            and not row_dict.get("quote_text_display")
        )
        need_emphasis = (
            has_emphasis_key
            and bool(verdict_dict)
            and not row_dict.get("verdict_emphasis_tokens")
        )
        if not need_polish and not need_emphasis:
            skipped_already_cached += 1
            continue
        work.append({
            "id": row_dict["id"],
            "quote_text": row_dict["quote_text"],
            "verdict": verdict_dict,
            "need_polish": need_polish,
            "need_emphasis": need_emphasis,
            "meeting_id": row_dict.get("meeting_id"),
            "city_name": row_dict.get("city_name"),
        })

    if not work:
        return {
            "requested": len(quote_ids),
            "polished": 0,
            "emphasis_extracted": 0,
            "skipped_already_cached": skipped_already_cached,
        }

    def _run_polish(text: str):
        try:
            return polish_for_display(text)
        except Exception as e:
            logger.warning("polish_for_display crashed: %s", e)
            return None

    def _run_emphasis(verdict: dict):
        try:
            return extract_verdict_emphasis(verdict)
        except Exception as e:
            logger.warning("extract_verdict_emphasis crashed: %s", e)
            return None

    polished_count = 0
    emphasis_count = 0
    polish_rejections_routed = 0
    with ThreadPoolExecutor(max_workers=min(8, len(work) * 2)) as pool:
        polish_futs: Dict[int, Any] = {}
        emphasis_futs: Dict[int, Any] = {}
        for item in work:
            if item["need_polish"]:
                polish_futs[item["id"]] = pool.submit(_run_polish, item["quote_text"])
            if item["need_emphasis"]:
                emphasis_futs[item["id"]] = pool.submit(_run_emphasis, item["verdict"])

        item_by_id = {item["id"]: item for item in work}
        for item in work:
            qid = item["id"]
            new_display = None
            new_emphasis = None
            polish_result = None
            if qid in polish_futs:
                polish_result = polish_futs[qid].result()
                if polish_result is not None and not polish_result.error:
                    new_display = polish_result.polished
                    polished_count += 1
            if qid in emphasis_futs:
                r = emphasis_futs[qid].result()
                if r is not None and not r.error:
                    new_emphasis = r.emphasis_tokens
                    emphasis_count += 1
            try:
                update_quote_display_cache(
                    qid,
                    quote_text_display=new_display,
                    verdict_emphasis_tokens=new_emphasis,
                )
            except Exception as e:
                logger.warning(
                    "failed to cache display fields for quote %s: %s", qid, e
                )

            # V1-Consensus-1 C4 — route polish-rejection events (word-level
            # rejection branch only; longer-than-input rejections are a
            # different signal class). City context comes from the JOIN
            # against meetings above. Failures are non-fatal; the display
            # cache write already landed.
            if (
                polish_result is not None
                and getattr(polish_result, "rejected_polish_proposal", None)
                and item_by_id[qid].get("city_name")
            ):
                try:
                    from consensus_vocab import route_polish_rejection_to_consensus  # noqa: PLC0415

                    routed = route_polish_rejection_to_consensus(
                        polish_result,
                        city_name=item_by_id[qid]["city_name"],
                        meeting_id=item_by_id[qid].get("meeting_id"),
                        quote_id=qid,
                    )
                    if routed and routed.get("created"):
                        polish_rejections_routed += 1
                except Exception as e:
                    logger.warning(
                        "polish-rejection consensus routing failed for quote %s: %s",
                        qid, e,
                    )

    return {
        "polish_rejections_routed_to_consensus": polish_rejections_routed,
        "requested": len(quote_ids),
        "polished": polished_count,
        "emphasis_extracted": emphasis_count,
        "skipped_already_cached": skipped_already_cached,
    }

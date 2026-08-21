"""slack_actions — reaction-to-action dispatch table for the S-004 Phase 2
================================================================

Each entry maps a Slack reaction emoji name to a handler function that
takes (escalation_row, user_id) and returns a dict shaped:

    {
      "ok": bool,          # whether the action ran cleanly
      "reply_text": str,   # short text the listener posts as a thread reply
      "side_effect": str,  # description of what changed (audit log line)
    }

V1 taxonomy locked in DECISIONS.md § D-055:
- :white_check_mark: → acknowledge the escalation (clears the badge)
- :eyes:             → operator-aware (thread reply only, no DB change)
- :no_entry:         → "apply the agent's recommendation if it was reject"
                       (only fires when what_id_do recommends reject;
                       ignored on other recommendations — defensive
                       against the agent's recommendation being wrong)

Reactions are triage-from-anywhere. Parametric decisions still belong on
the EscalationsInboxPage button. See agents/SLACK_PHASE2_SCOPING.md.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── User label resolution ─────────────────────────────────────────────


def _resolve_user_label(user_id: str) -> str:
    """Best-effort: turn a Slack user_id (U-...) into something the audit
    trail can read. Tries users.info via the bot token if available, falls
    back to `slack:U-...` when email/profile isn't accessible (the bot
    doesn't have users:read.email scope in V1; falls back gracefully).
    """
    if not user_id:
        return "operator"
    try:
        from slack_sdk import WebClient
        from slack_notifier import _resolve_bot_token  # local import to avoid cycle
        token = _resolve_bot_token()
        if not token:
            return f"slack:{user_id}"
        client = WebClient(token=token)
        resp = client.users_info(user=user_id)
        if resp.get("ok"):
            profile = (resp.get("user") or {}).get("profile") or {}
            email = profile.get("email")
            if email:
                return email
            name = (resp.get("user") or {}).get("real_name") or profile.get("display_name")
            if name:
                return f"slack:{name}"
    except Exception as e:
        logger.debug("users.info lookup failed for %s: %s", user_id, e)
    return f"slack:{user_id}"


# ── Handlers ──────────────────────────────────────────────────────────


def _decode_what_id_do(escalation_row: Dict[str, Any]) -> list[str]:
    raw = escalation_row.get("what_id_do") or []
    if isinstance(raw, str):
        try:
            return json.loads(raw) or []
        except Exception:
            return []
    return raw if isinstance(raw, list) else []


def _extract_quote_id(audit_row: Optional[str]) -> Optional[int]:
    """Parse `quotes.id=44` style audit_row references back to an int."""
    if not audit_row:
        return None
    m = re.match(r"^quotes\.id\s*=\s*(\d+)\s*$", audit_row.strip())
    return int(m.group(1)) if m else None


def _extract_vocab_correction_id(audit_row: Optional[str]) -> Optional[int]:
    """Parse `city_vocabulary_corrections.id=8` style audit_row refs."""
    if not audit_row:
        return None
    m = re.match(
        r"^city_vocabulary_corrections\.id\s*=\s*(\d+)\s*$", audit_row.strip()
    )
    return int(m.group(1)) if m else None


def handle_ack(escalation_row: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """:white_check_mark: → acknowledge the escalation."""
    from database import acknowledge_pending_escalation
    user_label = _resolve_user_label(user_id)
    ok = acknowledge_pending_escalation(
        escalation_row["id"], acknowledged_by=user_label
    )
    if ok:
        return {
            "ok": True,
            "reply_text": f"Acknowledged by {user_label}.",
            "side_effect": f"pending_escalations.id={escalation_row['id']} acknowledged_by={user_label}",
        }
    return {
        "ok": False,
        "reply_text": "Already acknowledged — no state change.",
        "side_effect": "no-op (already acknowledged)",
    }


def handle_operator_aware(escalation_row: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """:eyes: → operator-aware. No DB change; just thread visibility."""
    user_label = _resolve_user_label(user_id)
    return {
        "ok": True,
        "reply_text": f"Noted — {user_label} is on it.",
        "side_effect": "no DB change (operator-aware signal only)",
    }


def handle_apply_if_reject(escalation_row: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """:no_entry: → apply the agent's recommendation if it was reject.

    Defensive: only fires when `what_id_do` actually contains "reject"
    in its first line (the agent's primary recommendation). Other shapes
    are ignored — the operator's :no_entry: was likely a misclick or the
    agent's recommendation isn't a clean reject.
    """
    what_id_do = _decode_what_id_do(escalation_row)
    primary = (what_id_do[0] if what_id_do else "").lower()
    if "reject" not in primary or "verify" in primary:
        # "verify" disambiguates cases like "lean toward verify, escalating
        # for safety" — don't auto-reject those.
        return {
            "ok": False,
            "reply_text": "Agent did not primarily recommend reject — no action taken. Use the operator UI for the resolution.",
            "side_effect": "no-op (agent recommendation was not 'reject')",
        }

    quote_id = _extract_quote_id(escalation_row.get("audit_row"))
    if quote_id is None:
        return {
            "ok": False,
            "reply_text": "Could not resolve quote_id from audit_row — no action taken.",
            "side_effect": "no-op (audit_row not parseable)",
        }

    # Call the resolve endpoint with action=reject + resolver_notes that
    # capture the operator's emoji-driven decision in the audit trail.
    try:
        from database import resolve_disputed_quote, acknowledge_pending_escalation
        user_label = _resolve_user_label(user_id)
        result = resolve_disputed_quote(
            quote_id=quote_id,
            action="reject",
            quote_text=None,
            resolver_notes=f"Operator reacted with :no_entry: in Slack — applied agent's reject recommendation. Reactor: {user_label}.",
            resolved_by=user_label,
        )
        if result is None:
            return {
                "ok": False,
                "reply_text": f"Quote #{quote_id} not found — may have been resolved already.",
                "side_effect": "no-op (quote not found)",
            }
        # Also acknowledge the escalation since the action is complete.
        acknowledge_pending_escalation(escalation_row["id"], acknowledged_by=user_label)
        return {
            "ok": True,
            "reply_text": f"Applied agent's recommendation — quote #{quote_id} rejected by {user_label}.",
            "side_effect": f"quotes.id={quote_id} rejected; pending_escalations.id={escalation_row['id']} acknowledged",
        }
    except Exception as e:
        logger.exception("handle_apply_if_reject failed for quote %s", quote_id)
        return {
            "ok": False,
            "reply_text": f"Could not apply recommendation: {e}",
            "side_effect": f"error: {e}",
        }


def handle_apply_agent_proposal(
    escalation_row: Dict[str, Any], user_id: str
) -> Dict[str, Any]:
    """:sparkles: → apply the agent's counter-proposal (D-057 fast-path).

    Dispatches by audit_row shape:

      - `city_vocabulary_corrections.id=N` → vocab path (the original
        D-057 case): apply via `append_whisper_vocabulary_hint` +
        `mark_correction_promoted` with the agent's `agent_proposed_right`
        as the canonical entry.
      - `quotes.id=N` → disputed-quotes path (D-057 extension): apply via
        `resolve_disputed_quote(action='verify',
        quote_text=agent_proposed_quote_text)` so the disputed quote
        flips to verified with the agent's value baked in (and karaoke
        re-alignment fires automatically per the existing
        `_word_timings_invalidated` flow).

    Both paths then acknowledge the escalation + post a thread-reply.

    Defensive: skips with explanation when the audit_row doesn't match
    either pattern; when the underlying row has no counter-proposal on
    file; when the row is already in a terminal state (promoted /
    auto_apply=0 / non-disputed); or when a backend write fails.
    """
    audit_row = escalation_row.get("audit_row")
    correction_id = _extract_vocab_correction_id(audit_row)
    quote_id = _extract_quote_id(audit_row) if correction_id is None else None

    if correction_id is not None:
        return _apply_vocab_agent_proposal(escalation_row, user_id, correction_id)
    if quote_id is not None:
        return _apply_quote_agent_proposal(escalation_row, user_id, quote_id)
    return {
        "ok": False,
        "reply_text": (
            "✨ fast-path needs a `city_vocabulary_corrections.id=N` or "
            "`quotes.id=N` audit_row — this escalation's audit_row doesn't "
            "match. Use the operator UI."
        ),
        "side_effect": "no-op (audit_row not parseable for fast-path)",
    }


def _apply_vocab_agent_proposal(
    escalation_row: Dict[str, Any], user_id: str, correction_id: int
) -> Dict[str, Any]:
    """D-057 vocab path — apply the agent's `agent_proposed_right` to the
    city dictionary via the same helpers the /promote endpoint uses.
    """
    try:
        from database import (
            get_connection, mark_correction_promoted,
            append_whisper_vocabulary_hint, acknowledge_pending_escalation,
        )
    except Exception as e:
        logger.exception("vocab agent-proposal apply: import failed")
        return {
            "ok": False,
            "reply_text": f"Could not apply: import error ({e})",
            "side_effect": f"error: import {e}",
        }

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, city_name, wrong, right, applied_count, auto_apply,
                   first_observed_response_file, created_at,
                   promoted_at, promoted_by,
                   agent_proposed_right, agent_reasoning, agent_proposed_by
            FROM city_vocabulary_corrections WHERE id = ?
            """,
            (correction_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {
            "ok": False,
            "reply_text": f"Correction #{correction_id} not found.",
            "side_effect": "no-op (correction missing)",
        }
    c = dict(row)
    if c.get("promoted_at"):
        return {
            "ok": False,
            "reply_text": (
                f"Correction #{correction_id} was already promoted "
                f"at {c['promoted_at']} by {c.get('promoted_by') or 'operator'}."
            ),
            "side_effect": "no-op (already promoted)",
        }
    if not c.get("auto_apply"):
        return {
            "ok": False,
            "reply_text": (
                f"Correction #{correction_id} was previously rejected — "
                "re-enable on the operator UI before applying."
            ),
            "side_effect": "no-op (auto_apply=0)",
        }
    agent_value = (c.get("agent_proposed_right") or "").strip()
    if not agent_value:
        return {
            "ok": False,
            "reply_text": (
                f"Correction #{correction_id} has no agent counter-proposal "
                "on file. Use ✅ to acknowledge or the operator UI to promote."
            ),
            "side_effect": "no-op (no agent_proposed_right)",
        }

    user_label = _resolve_user_label(user_id)
    try:
        appended = append_whisper_vocabulary_hint(
            city_name=c["city_name"],
            term=agent_value,
            category=None,
            first_seen=c.get("created_at"),
            source=c.get("first_observed_response_file"),
            promoted_by=user_label,
        )
    except Exception as e:
        logger.exception(
            "append_whisper_vocabulary_hint failed for correction %s",
            correction_id,
        )
        return {
            "ok": False,
            "reply_text": f"Could not write to city JSON: {e}",
            "side_effect": f"error: {e}",
        }
    if appended is None:
        return {
            "ok": False,
            "reply_text": (
                f"No city_intelligence JSON found for {c['city_name']!r} — "
                "the dictionary file may be missing."
            ),
            "side_effect": "no-op (city JSON missing)",
        }

    mark_correction_promoted(
        correction_id=correction_id, promoted_by=user_label
    )
    acknowledge_pending_escalation(
        escalation_row["id"], acknowledged_by=user_label
    )

    overrode = agent_value != c["right"]
    return {
        "ok": True,
        "reply_text": (
            f"Applied {c.get('agent_proposed_by') or 'agent'}'s counter-proposal — "
            f"`{c['wrong']}` → `{agent_value}` added to {c['city_name']}'s dictionary "
            f"by {user_label}."
            + (f" (overrode verifier's `{c['right']}`)" if overrode else "")
        ),
        "side_effect": (
            f"city_vocabulary_corrections.id={correction_id} promoted with "
            f"agent value; pending_escalations.id={escalation_row['id']} acknowledged"
        ),
    }


def _apply_quote_agent_proposal(
    escalation_row: Dict[str, Any], user_id: str, quote_id: int
) -> Dict[str, Any]:
    """D-057 extension — disputed-quotes path. Apply the agent's
    `agent_proposed_quote_text` to a disputed quote via the existing
    `resolve_disputed_quote(action='verify', quote_text=<agent value>)`
    pipeline. Karaoke re-alignment fires automatically when the text
    differs (the `_word_timings_invalidated` flow in the resolve helper).
    """
    try:
        from database import (
            get_connection, resolve_disputed_quote,
            acknowledge_pending_escalation,
        )
        from quote_align import align_quotes_for_meeting
    except Exception as e:
        logger.exception("quote agent-proposal apply: import failed")
        return {
            "ok": False,
            "reply_text": f"Could not apply: import error ({e})",
            "side_effect": f"error: import {e}",
        }

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, meeting_id, speaker_name, quote_text, verified_status,
                   agent_proposed_quote_text, agent_reasoning, agent_proposed_by
            FROM quotes WHERE id = ?
            """,
            (quote_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {
            "ok": False,
            "reply_text": f"Quote #{quote_id} not found.",
            "side_effect": "no-op (quote missing)",
        }
    q = dict(row)
    if q.get("verified_status") != "disputed":
        return {
            "ok": False,
            "reply_text": (
                f"Quote #{quote_id} is in state {q['verified_status']!r}, "
                f"not 'disputed' — counter-proposal fast-path only applies "
                f"to unresolved disputed quotes. Use the operator UI."
            ),
            "side_effect": f"no-op (verified_status={q.get('verified_status')!r})",
        }
    agent_value = (q.get("agent_proposed_quote_text") or "").strip()
    if not agent_value:
        return {
            "ok": False,
            "reply_text": (
                f"Quote #{quote_id} has no agent counter-proposal on file. "
                "Use ✅ to acknowledge or the operator UI to verify/reject."
            ),
            "side_effect": "no-op (no agent_proposed_quote_text)",
        }

    user_label = _resolve_user_label(user_id)
    try:
        result = resolve_disputed_quote(
            quote_id=quote_id,
            action="verify",
            quote_text=agent_value,
            resolver_notes=(
                f"Operator reacted with :sparkles: in Slack — applied "
                f"{q.get('agent_proposed_by') or 'agent'}'s counter-proposal. "
                f"Reactor: {user_label}."
            ),
            resolved_by=user_label,
        )
    except Exception as e:
        logger.exception("resolve_disputed_quote failed for quote %s", quote_id)
        return {
            "ok": False,
            "reply_text": f"Could not apply counter-proposal: {e}",
            "side_effect": f"error: {e}",
        }
    if result is None:
        return {
            "ok": False,
            "reply_text": f"Quote #{quote_id} not found at resolve-time.",
            "side_effect": "no-op (quote disappeared between fetch and resolve)",
        }

    # Mirror the Flask endpoint's post-resolve realignment when the text
    # changed (resolve_disputed_quote sets _word_timings_invalidated when
    # quote_text differs from the prior value). The Flask resolve route
    # already does this for operator-driven calls; we replicate it here
    # so the Slack fast-path lands quotes with karaoke aligned to the
    # new display tokens.
    if result.get("_word_timings_invalidated"):
        try:
            align_quotes_for_meeting(result["meeting_id"])
        except Exception as e:
            logger.warning(
                "post-fast-path realignment failed for quote %s (%s); "
                "the quote is verified but word_timings stays NULL until "
                "alignment runs manually",
                quote_id, e,
            )

    acknowledge_pending_escalation(
        escalation_row["id"], acknowledged_by=user_label
    )
    return {
        "ok": True,
        "reply_text": (
            f"Applied {q.get('agent_proposed_by') or 'agent'}'s counter-proposal — "
            f"quote #{quote_id} ({q.get('speaker_name') or 'speaker'}) verified "
            f"with the agent's text by {user_label}."
        ),
        "side_effect": (
            f"quotes.id={quote_id} verified with agent value; "
            f"pending_escalations.id={escalation_row['id']} acknowledged"
        ),
    }


# ── Dispatch table ────────────────────────────────────────────────────


REACTION_HANDLERS: Dict[str, Any] = {
    "white_check_mark": handle_ack,
    "eyes": handle_operator_aware,
    "no_entry": handle_apply_if_reject,
    "sparkles": handle_apply_agent_proposal,
}


def dispatch(reaction_name: str, escalation_row: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    """Look up the handler for `reaction_name` and run it. Returns None
    when the reaction isn't in our taxonomy (listener should ignore those
    silently — operators may use other reactions for their own purposes)."""
    handler = REACTION_HANDLERS.get(reaction_name)
    if handler is None:
        return None
    try:
        return handler(escalation_row, user_id)
    except Exception as e:
        logger.exception("reaction handler crashed for %s", reaction_name)
        return {
            "ok": False,
            "reply_text": f"Handler crashed: {e}",
            "side_effect": f"error in {reaction_name} handler",
        }

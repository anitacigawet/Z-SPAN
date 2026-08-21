"""slack_notifier — escalation transport for the S-004 agent-employees layer
=============================================================================

When an employee-agent encounters a case its manual doesn't cover, it
escalates to James via Slack (per S-004 § Escalation channel, locked
2026-05-26). This module is the shared transport every agent's manual
references.

V1 design:
  - Slack Incoming Webhook (no bot/OAuth setup)
  - URL stored in `parsers/user_settings.json` as `slack_webhook_url`
  - One-way outbound (agent posts; James resolves on the operator surface)
  - Fallback: write to local `pending_escalations` table when the webhook
    is unreachable. Slack is primary but not load-bearing — graceful
    degradation when not configured.
  - Per-role rate limit (default 5/hour) to prevent stuck-agent spam.
    Beyond rate: queue locally + send ONE rate-limited notice.

Severity levels:
  - info     — surfacing something the manual covers (low-noise)
  - decision — agent needs a human call to proceed
  - blocked  — agent cannot continue until James acts
  - error    — agent crashed unexpectedly

Standard escalation message shape:
  [<Agent Role>] <one-sentence summary>

  What I see:
  - <bullet 1>
  - <bullet 2>

  What I'd do if forced:
  - <agent's best-effort recommendation>

  Open this on <surface>: <deep link>
  Audit row: <table.id reference>

Both Slack and the fallback table preserve all fields so the operator can
read either source. The activity-log stream gets a one-line summary
regardless of which transport succeeded.

Cross-references:
  - `agents/README.md § Escalation` for the agent-side contract
  - `01_Project_Overview/FUTURE_THOUGHTS.md § S-004 § Escalation channel`
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from env_config import load_user_settings

logger = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_SECONDS = 8

# Default port for the operator UI (Express + Vite, per CLAUDE.md).
_OPERATOR_UI_PORT = 3000

# Severity enum. Order matters for the worst-of-batch summary at the
# top of `get_recent_escalations`.
SEVERITY_LEVELS = ("info", "decision", "blocked", "error")
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITY_LEVELS)}

# Per-role hourly rate limit. In-memory; resets on Flask restart.
# Tracking is (role, ts) — we keep a sliding window of timestamps and
# count how many fall within the last hour.
DEFAULT_PER_ROLE_HOURLY_LIMIT = 5

# Per-role hourly limit overrides. Roles not listed inherit DEFAULT.
# Mirrors each agent manual's "Per-hour Slack escalation rate limit" line —
# the manual documents the policy; this registry enforces it. Keep in sync
# with the manuals in `agents/<role>.md`.
ROLE_HOURLY_LIMITS: Dict[str, int] = {
    "content-scout": 3,
    "parser-custodian": 3,
}

_RATE_LIMIT_WINDOW_SECONDS = 3600
_rate_lock = threading.Lock()
_rate_timestamps: Dict[str, List[float]] = {}
# Per-role flag: have we already sent the "rate-limited" notice for the
# current window? Reset when the window rolls forward.
_rate_limit_notice_sent: Dict[str, float] = {}


def resolve_hourly_limit(role: str, override: Optional[int] = None) -> int:
    """Return the effective hourly escalation limit for `role`.

    If `override` is not None, the caller's explicit value wins. Otherwise
    look up `role` in ROLE_HOURLY_LIMITS; fall back to
    DEFAULT_PER_ROLE_HOURLY_LIMIT for roles that don't override.
    """
    if override is not None:
        return override
    return ROLE_HOURLY_LIMITS.get(role, DEFAULT_PER_ROLE_HOURLY_LIMIT)


@dataclass
class EscalationResult:
    """Returned by send_escalation. The agent logs from this."""
    delivered_to_slack: bool = False
    queued_locally: bool = False
    rate_limited: bool = False
    pending_id: Optional[int] = None
    error: Optional[str] = None
    # Phase 2 / D-055 fields — populated only on the chat.postMessage send
    # path (bot token configured). Webhook fallback leaves them None.
    slack_message_ts: Optional[str] = None
    clip_attached: bool = False
    clip_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "delivered_to_slack": self.delivered_to_slack,
            "queued_locally": self.queued_locally,
            "rate_limited": self.rate_limited,
            "pending_id": self.pending_id,
            "error": self.error,
            "slack_message_ts": self.slack_message_ts,
            "clip_attached": self.clip_attached,
            "clip_error": self.clip_error,
        }


def _resolve_webhook_url() -> str:
    """Resolve SLACK_WEBHOOK_URL env var -> user_settings.json -> empty."""
    env = os.environ.get("SLACK_WEBHOOK_URL")
    if env:
        return env.strip()
    settings = load_user_settings()
    return (settings.get("slack_webhook_url") or "").strip()


def is_configured() -> bool:
    """True iff a Slack webhook URL is resolvable. Used by callers that
    want to short-circuit when Slack isn't set up — they fall back to the
    local pending_escalations queue without trying the network at all."""
    return bool(_resolve_webhook_url())


# ── Bot-token send path (Phase 2 / D-055) ─────────────────────────────
# When slack_bot_token is configured, the canonical send path becomes
# chat.postMessage (which returns the message ts so reactions can map back
# to escalations). Webhook stays as fallback when the bot path isn't
# configured or fails.


def _resolve_bot_token() -> str:
    env = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    if env:
        return env
    settings = load_user_settings()
    return (settings.get("slack_bot_token") or "").strip()


def _resolve_channel_id() -> str:
    env = (os.environ.get("SLACK_CHANNEL_ID") or "").strip()
    if env:
        return env
    settings = load_user_settings()
    return (settings.get("slack_channel_id") or "").strip()


def resolve_owner_user_id() -> Optional[str]:
    """James's Slack user ID for the operator DM (D-062a / D-071).

    Source priority: SLACK_OWNER_USER_ID env var > slack_owner_user_id key
    in user_settings.json. Returns None if neither is set.

    Shared by `slack_listener._process_im_message` (gate inbound DMs to the
    owner) and `_resolve_orchestrator_dm_channel` (target outbound DMs to
    the owner's DM channel).
    """
    env = (os.environ.get("SLACK_OWNER_USER_ID") or "").strip()
    if env:
        return env
    settings = load_user_settings()
    raw = (settings.get("slack_owner_user_id") or "").strip()
    return raw or None


# DM channel cache — `conversations.open` returns the same channel ID for
# repeated calls against the same user, so we resolve once per process and
# memoize. Resets on Flask restart (acceptable; the resolution call is cheap).
_DM_CHANNEL_CACHE: Dict[str, str] = {}
_DM_CHANNEL_LOCK = threading.Lock()


def _resolve_orchestrator_dm_channel() -> Optional[str]:
    """Resolve the DM channel ID for posting to James's operator DM thread.

    Returns the cached channel ID if already resolved; otherwise calls
    `conversations.open` against `slack_owner_user_id` and caches. Returns
    None when owner ID isn't configured, the bot token isn't available,
    or `conversations.open` fails — callers fall back to the shared agents
    channel in that case.

    D-062a / D-071 (DM bridge outbound, chunk 1b).
    """
    owner_id = resolve_owner_user_id()
    if not owner_id:
        return None
    token = _resolve_bot_token()
    if not token:
        return None

    with _DM_CHANNEL_LOCK:
        cached = _DM_CHANNEL_CACHE.get(owner_id)
        if cached:
            return cached

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except Exception as e:
        logger.error("slack_notifier: slack_sdk import failed: %s", e)
        return None

    try:
        client = WebClient(token=token)
        resp = client.conversations_open(users=owner_id)
        if not resp.get("ok"):
            logger.warning(
                "slack_notifier: conversations.open failed for owner=%s: %s",
                owner_id, resp.get("error"),
            )
            return None
        channel_obj = resp.get("channel") or {}
        channel_id = (channel_obj.get("id") or "").strip()
        if not channel_id:
            return None
        with _DM_CHANNEL_LOCK:
            _DM_CHANNEL_CACHE[owner_id] = channel_id
        return channel_id
    except SlackApiError as e:
        logger.warning(
            "slack_notifier: conversations.open SlackApiError for owner=%s: %s",
            owner_id, e.response.get("error", str(e)),
        )
        return None
    except Exception as e:
        logger.warning(
            "slack_notifier: conversations.open raised for owner=%s: %s",
            owner_id, e,
        )
        return None


def bot_path_available() -> bool:
    """True iff both slack_bot_token and slack_channel_id are configured.

    When False, send_escalation falls back to the webhook path; clip-upload
    helpers no-op (clip attachments require the bot token).
    """
    return bool(_resolve_bot_token()) and bool(_resolve_channel_id())


def _post_via_bot(
    fallback_text: str,
    blocks: List[Dict[str, Any]],
    channel_override: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Call chat.postMessage via slack_sdk.WebClient. Returns a dict with
    `ok`, `ts`, `channel`, and `error` (when not ok). Raises only on
    catastrophic local failures (import error, etc).

    Blocks are sent at TOP LEVEL (not wrapped in a colored attachment): an
    attachment renders Slack's "Show more / Added by <app>" collapse, which
    hides the recommended action + button behind a click. Severity color is
    carried by the colored-circle header marker instead (see _SEVERITY_EMOJI).

    Args:
      channel_override: If set, post to this channel instead of the
        configured `slack_channel_id`. Used by the orchestrator DM path
        (D-062a / D-071) to route orchestrator-attributed escalations to
        James's operator DM instead of the shared agents channel.
      thread_ts: If set, threads the post to that message ts. Used by the
        orchestrator's Mode B instructed-spawn path — replies thread to
        the inbound DM that triggered the spawn.
    """
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except Exception as e:
        logger.error("slack_sdk import failed: %s", e)
        return {"ok": False, "ts": None, "channel": None, "error": f"import: {e}"}

    token = _resolve_bot_token()
    channel = (channel_override or _resolve_channel_id()).strip()
    if not token or not channel:
        return {"ok": False, "ts": None, "channel": None, "error": "bot path not configured"}

    client = WebClient(token=token)
    try:
        post_kwargs = {
            "channel": channel,
            "text": fallback_text,
            "blocks": blocks,
            # Disable Slack's auto-unfurl since the Block Kit message already
            # structures everything we want to surface.
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            post_kwargs["thread_ts"] = thread_ts
        resp = client.chat_postMessage(**post_kwargs)
        return {
            "ok": bool(resp.get("ok")),
            "ts": resp.get("ts"),
            "channel": resp.get("channel"),
            "error": resp.get("error"),
        }
    except SlackApiError as e:
        return {
            "ok": False,
            "ts": None,
            "channel": None,
            "error": str(e.response.get("error", e)),
        }
    except Exception as e:
        return {"ok": False, "ts": None, "channel": None, "error": str(e)}


# ── Clip path resolution + upload ─────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe slug — mirrors proofs_uploader._slugify so the
    review_queue path computed here matches what build_review_queue.py
    wrote at clip-extraction time."""
    if not text:
        return "untitled"
    s = _SLUG_RE.sub("_", text).strip("_").lower()
    if len(s) > max_len:
        s = s[:max_len].rstrip("_")
    return s or "untitled"


def _resolve_clip_path_for_quote(quote_id: int) -> Optional[str]:
    """Locate the on-disk MP4 clip for a given quote_id.

    Path schema (per build_review_queue.py):
      <repo>/media/review_queue/<city_slug>/<date>__<meeting_slug>/batch_NN/quote_<id>__<speaker_slug>.mp4

    Returns the first matching path as a string, or None when the clip
    doesn't exist on disk (operator hasn't built the review queue yet,
    or source cache was cleaned). Defensive — never raises.
    """
    try:
        from database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT q.id, q.speaker_name, m.city_name, m.meeting_date, m.meeting_title
                FROM quotes q
                JOIN meetings m ON m.id = q.meeting_id
                WHERE q.id = ?
                """,
                (quote_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        city_slug = _slugify(row["city_name"])
        meeting_slug = _slugify(row["meeting_title"])
        meeting_date = (row["meeting_date"] or "").strip()
        speaker_slug = _slugify(row["speaker_name"])
    except Exception as e:
        logger.warning("clip-path resolution DB lookup failed for quote_id=%s: %s", quote_id, e)
        return None

    # parsers/ is two levels above the repo's media/ directory.
    parsers_dir = os.path.dirname(os.path.abspath(__file__))
    media_root = os.path.normpath(
        os.path.join(parsers_dir, "..", "media", "review_queue")
    )
    meeting_dir = os.path.join(
        media_root, city_slug, f"{meeting_date}__{meeting_slug}"
    )
    if not os.path.isdir(meeting_dir):
        return None

    # Glob batch_NN subdirs for the matching quote file. build_review_queue
    # filename is `quote_<id>__<speaker_slug>.mp4`.
    import glob
    pattern = os.path.join(
        meeting_dir, "batch_*", f"quote_{quote_id}__{speaker_slug}.mp4"
    )
    matches = glob.glob(pattern)
    if matches:
        return matches[0]
    # Fallback: speaker slug might differ slightly — try any quote_<id>__*.mp4
    pattern_any = os.path.join(meeting_dir, "batch_*", f"quote_{quote_id}__*.mp4")
    matches = glob.glob(pattern_any)
    return matches[0] if matches else None


def attach_clip_for_quote(
    quote_id: int,
    slack_message_ts: str,
    initial_comment: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload the clip for `quote_id` as a thread reply under the
    escalation message identified by `slack_message_ts`.

    Returns {ok, error, clip_path, file_id?}. Non-blocking on the
    caller's side — clip upload failures don't void the escalation
    itself (the text message already landed before this is called).
    """
    if not bot_path_available():
        return {"ok": False, "error": "bot path not configured", "clip_path": None}

    clip_path = _resolve_clip_path_for_quote(quote_id)
    if not clip_path:
        return {"ok": False, "error": "clip not on disk", "clip_path": None}

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except Exception as e:
        return {"ok": False, "error": f"import: {e}", "clip_path": clip_path}

    token = _resolve_bot_token()
    channel = _resolve_channel_id()
    client = WebClient(token=token)
    try:
        resp = client.files_upload_v2(
            channel=channel,
            thread_ts=slack_message_ts,
            file=clip_path,
            title=f"Clip for quote #{quote_id}",
            initial_comment=initial_comment or "Source clip — play to review",
        )
        return {
            "ok": bool(resp.get("ok")),
            "clip_path": clip_path,
            "file_id": (resp.get("file") or {}).get("id"),
            "error": resp.get("error"),
        }
    except SlackApiError as e:
        return {
            "ok": False,
            "clip_path": clip_path,
            "error": str(e.response.get("error", e)),
        }
    except Exception as e:
        return {"ok": False, "clip_path": clip_path, "error": str(e)}


# ── Operator-terminal deep-link rewriting ─────────────────────────────
# Agents construct deep_links like "http://localhost:3000/?view=..." in their
# escalations. When James reads the escalation from his phone (or any device
# that isn't the host machine), localhost is unreachable. Rewrite the host:port
# to the machine's LAN-accessible address before the link leaves the process,
# so the same URL works from desktop, phone-on-same-wifi, or any LAN client.
#
# Resolution priority:
#   1. SLACK_OPERATOR_BASE_URL env var (full URL, e.g. http://<lan-ip>:3000)
#   2. user_settings.json `operator_terminal_base_url` (same shape)
#   3. Auto-detect: open a non-routed UDP socket to a public address, read the
#      local socket's IP via getsockname(). Picks the primary outbound
#      interface. No packets are actually sent.
#
# WiFi changes invalidate the auto-detected IP. The override slot is the
# escape hatch for VPN, multi-NIC, or when James wants to hardwire the IP
# (his 2026-05-26 call: if per-call auto-detection is too risky, hardwire
# the IP in one place and accept having to remember it on WiFi changes).


def _detect_lan_ip() -> str:
    """Return the machine's primary outbound interface IP.

    Uses the UDP-socket trick — opens a non-routed socket "connected" to a
    public address and reads the local socket's IP. No packets are sent;
    the connect() just configures the socket so getsockname() reflects
    which interface the OS would use for outbound traffic.

    Returns 'localhost' on any failure (e.g., no network configured).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # 8.8.8.8 is Google's public DNS — convenient sentinel for
            # "the default-route interface."
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception as e:
        logger.warning("LAN IP auto-detect failed: %s; falling back to localhost", e)
        return "localhost"


def resolve_operator_base_url() -> str:
    """Resolve the base URL the operator UI is reachable at.

    Resolution order: env var → user_settings override → auto-detected LAN IP.
    Always returns a URL with no trailing slash.
    """
    env = (os.environ.get("SLACK_OPERATOR_BASE_URL") or "").strip()
    if env:
        return env.rstrip("/")
    settings = load_user_settings()
    explicit = (settings.get("operator_terminal_base_url") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    lan = _detect_lan_ip()
    return f"http://{lan}:{_OPERATOR_UI_PORT}"


_LOCALHOST_RE = re.compile(
    r"^(https?)://(localhost|127\.0\.0\.1)(?::\d+)?(/.*)?$",
    re.IGNORECASE,
)


def _rewrite_deep_link(url: Optional[str]) -> Optional[str]:
    """Rewrite localhost / 127.0.0.1 URLs to the resolved operator base URL.

    URLs that already point elsewhere (a real domain, a different host)
    pass through unchanged. None / empty input passes through unchanged.
    """
    if not url:
        return url
    m = _LOCALHOST_RE.match(url.strip())
    if not m:
        return url
    path = m.group(3) or ""
    return f"{resolve_operator_base_url()}{path}"


def _check_rate_limit(role: str, limit: int) -> tuple[bool, int]:
    """Return (is_within_limit, remaining_after_this_one).

    Threadsafe. Trims the timestamp window to the last hour every call.
    """
    with _rate_lock:
        now = time.time()
        window_start = now - _RATE_LIMIT_WINDOW_SECONDS
        timestamps = _rate_timestamps.setdefault(role, [])
        # Trim expired
        timestamps[:] = [t for t in timestamps if t >= window_start]
        if len(timestamps) >= limit:
            return False, 0
        timestamps.append(now)
        remaining = max(limit - len(timestamps), 0)
        return True, remaining


def _maybe_send_rate_limit_notice(role: str, webhook_url: str, limit: int) -> None:
    """Send ONE 'rate-limited' notice per window per role.

    Prevents the rate-limited-stuck-agent from continuing to spam a
    rate-limited notice every minute when the underlying issue isn't
    resolved. Notice fires once per hour-window per role.
    """
    with _rate_lock:
        now = time.time()
        last = _rate_limit_notice_sent.get(role, 0)
        if now - last < _RATE_LIMIT_WINDOW_SECONDS:
            return
        _rate_limit_notice_sent[role] = now

    text = (
        f"[{role}] rate-limited\n\n"
        f"The agent has hit its hourly escalation ceiling "
        f"({limit}/hour). Additional escalations are queueing locally to the "
        f"pending_escalations table. Check the operator terminal badge "
        f"for the count, or investigate why the agent is escalating "
        f"so frequently."
    )
    try:
        requests.post(
            webhook_url,
            data=json.dumps({"text": text}),
            headers={"Content-Type": "application/json"},
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.warning(
            "Could not send rate-limit-notice to Slack: %s", e,
        )


# Severity → colored-circle header marker. Slack mrkdwn has NO inline text
# color, and the attachment color-bar (the left stripe) forces Slack to
# collapse the message behind "Show more" — hiding the recommended action +
# button. A colored circle in the (never-truncated) header is the reliable
# "colored character" severity cue: green = FYI/healthy, amber = decision
# needed, red = blocked, 💥 = crash. Always visible, no truncation cost.
# (D-054 applied to Slack: the operator reads severity before words.)
_SEVERITY_EMOJI: Dict[str, str] = {
    "info":     "🟢",
    "decision": "🟡",
    "blocked":  "🔴",
    "error":    "💥",
}


# Role-id → persona name. The personas are the org's named employees
# (THE_BLACK_BOX.md § 2): the briefing voice the formatter uses in the
# header + fallback summary. Mapping a role-id here is the only thing the
# formatter needs to brief James as a colleague speaking, not a database
# field reading. Roles not mapped fall back to title-case via
# _humanize_role (graceful degradation for new agents during rollout).
_ROLE_TO_PERSONA: Dict[str, str] = {
    "orchestrator": "The Twin",
    "disputed-quotes-reviewer": "The Reviewer",
    "vocabulary-curator": "The Curator",
    "pipeline-operator": "The Producer",
    "content-scout": "The Scout",
    "parser-custodian": "The Custodian",
}


def _humanize_role(role: str) -> str:
    """Fallback persona renderer — `disputed-quotes-reviewer` →
    `Disputed Quotes Reviewer`. Used only for roles not in
    _ROLE_TO_PERSONA. Title-case kebab-id, the pre-D-067 shape.
    """
    return role.replace("-", " ").replace("_", " ").strip().title()


def _persona_for(role: str) -> str:
    """Return the briefing persona for `role` (e.g. "The Reviewer").

    Primary: lookup in _ROLE_TO_PERSONA. Fallback: title-cased role-id.
    This is the briefing voice — what the operator reads in the header
    of every escalation (D-067).
    """
    return _ROLE_TO_PERSONA.get(role) or _humanize_role(role)


def _persona_inline(role: str) -> str:
    """Inline form of the persona — strip a leading 'The ' so it reads
    naturally mid-sentence. "The Reviewer" → "the Reviewer". Used in the
    `references` rendering ("*Source:* the Reviewer · the Custodian")."""
    p = _persona_for(role)
    if p.startswith("The "):
        return "the " + p[4:]
    return p


def _strip_bold(text: str) -> str:
    """Remove mrkdwn bold markers (`*`) from text. The summary headline is
    bolded uniformly by the formatter; if an agent ALSO wrapped a phrase in
    `*...*` inside the summary, Slack can't nest bold and renders literal
    asterisks. Stripping the agent's `*` before the formatter re-bolds keeps
    the headline clean. (Agents bold key phrases in the bullets, not the
    summary — see agents/README.md § Escalation.)"""
    return (text or "").replace("*", "")


# ── S-008 V0 / surface S-14 escalation sanitization ──────────────────────
# Bidi control characters per Unicode 15. Bidi controls can render text
# visually different from how Slack/clients parse it; civic-content
# escalations never legitimately contain bidi controls, so strip on the way in.
_ESCALATION_BIDI_CHARS = frozenset(
    chr(cp) for cp in (
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2066, 0x2067, 0x2068, 0x2069,
    )
)

# Fence-marker substring catalog (kept local to avoid coupling slack_notifier
# to the input_security package — slack_notifier is also called from contexts
# that don't import the broader parsers stack).
_ESCALATION_FENCE_MARKERS = (
    "<zspan-content-begin",
    "<zspan-content-end",
)


def _sanitize_escalation_text(text: Optional[str]) -> Optional[str]:
    """Defensive sanitizer for agent-emitted escalation strings.

    Strips bidi controls and replaces literal fence-marker substrings with a
    redaction marker. The deeper structural defenses (Pydantic length caps
    on the relay; agent_audit.validate_agent_text on action wrappers) catch
    these upstream; this layer is belt-and-suspenders so even an off-path
    escalation (no relay, no wrapper) lands sanitized.

    Returns None for None input (preserves Optional shape for callers).
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return str(text)
    out_chars: list[str] = []
    for ch in text:
        if ch in _ESCALATION_BIDI_CHARS:
            continue
        out_chars.append(ch)
    cleaned = "".join(out_chars)
    lowered = cleaned.lower()
    for marker in _ESCALATION_FENCE_MARKERS:
        if marker in lowered:
            # Case-insensitive replace via index walk; rare path so OK to do
            # the work even when the marker appears multiple times.
            replaced: list[str] = []
            i = 0
            while i < len(cleaned):
                low_chunk = cleaned[i:i + len(marker)].lower()
                if low_chunk == marker:
                    replaced.append("[fence-marker-stripped]")
                    i += len(marker)
                else:
                    replaced.append(cleaned[i])
                    i += 1
            cleaned = "".join(replaced)
            lowered = cleaned.lower()
    return cleaned


def _sanitize_escalation_bullets(
    bullets: Optional[List[str]],
) -> Optional[List[str]]:
    if bullets is None:
        return None
    return [_sanitize_escalation_text(b) or "" for b in bullets]


def _format_summary_line(role: str, severity: str, summary: str) -> str:
    """One-line message used as the `text:` field (push-notification fallback +
    legacy-client rendering). Slack truncates this for mobile lock-screen
    previews, so the leading words should carry the signal.

    Uses the briefing persona (D-067) so the push notification reads as the
    employee speaking, not a config field.
    """
    emoji = _SEVERITY_EMOJI.get(severity, "")
    # Strip mrkdwn from the plain-text fallback (push-notification preview).
    return f"{emoji} {_persona_for(role)}: {_strip_bold(summary)}".strip()


def _format_blocks(
    role: str,
    severity: str,
    summary: str,
    what_i_see: Optional[List[str]] = None,
    what_id_do: Optional[List[str]] = None,
    deep_link: Optional[str] = None,
    audit_row: Optional[str] = None,
    references: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Build Slack Block Kit blocks for the escalation message.

    Structure (D-054 + D-067 operator-oriented hierarchy):
      header (persona — e.g. "The Reviewer") → bold summary headline →
      [optional] "Source:" line naming any other agents whose output this
      brief references → divider → "What I see" bullets → "→ Recommended"
      (the agent's FIRST what_id_do bullet, given prominence) → "Other
      options" (remaining bullets, in a subtle context block) → URL button
      → audit-row footnote (soft italic, not code-formatted).

    Severity is carried by the colored-circle header marker (_SEVERITY_EMOJI),
    not an attachment color-bar — attachments collapse behind Slack's "Show
    more", hiding the action + button. The "one primary action prominent,
    alternatives subtle" split mirrors the DisputedQuotesPage /
    VocabularyInboxPage operator surfaces: the agent puts its recommended
    action first; the formatter renders it boldest.

    `references` is the D-067 cross-agent citation channel — when one agent
    briefs James about something another agent surfaced (e.g., the Twin
    relays the Reviewer's flag), pass `references=["disputed-quotes-reviewer"]`
    and the formatter adds a "*Source:* the Reviewer" line. Names the org's
    reasoning instead of laundering it into a single agent's voice.

    Agents are expected to write human prose (no schema field names, no
    code syntax) and may use *bold* mrkdwn around the load-bearing phrase
    in their bullets — the formatter provides the structural scaffold; the
    agent provides the content emphasis. See agents/README.md § Escalation.
    """
    emoji = _SEVERITY_EMOJI.get(severity, "")
    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                # Header blocks support emoji but not mrkdwn. Persona-first
                # (D-067 briefing voice), capped to Slack's 150-char header limit.
                "text": f"{emoji} {_persona_for(role)}"[:150],
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                # The summary is the headline — bolded uniformly so the
                # operator reads it first. Strip any agent-supplied `*` first
                # so inner bold doesn't collide with the wrapping bold (Slack
                # can't nest bold → renders literal asterisks otherwise).
                "text": f"*{_strip_bold(summary)[:2950]}*",
            },
        },
    ]

    if references:
        # Cross-agent citation (D-067). Names the source agents whose output
        # this brief references. Renders as a small context line right after
        # the summary so the operator reads "who's speaking on whose behalf"
        # before the body.
        cited = [_persona_inline(r) for r in references if r]
        if cited:
            label = "Source" if len(cited) == 1 else "Sources"
            cited_str = " · ".join(cited)
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"*{label}:* {cited_str}"[:2950]},
                ],
            })

    if what_i_see:
        bullets = "\n".join(f"• {line}" for line in what_i_see)
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*What I see*\n{bullets}"[:2950],
            },
        })

    if what_id_do:
        # First bullet = the recommended action, given prominence (its own
        # section with a bold "→ Recommended" label). Remaining bullets =
        # alternatives, demoted to a small grey context block so the eye
        # lands on the recommendation first (D-054 rule #4: one primary
        # action prominent, one escape subtle).
        recommended = what_id_do[0]
        alternatives = what_id_do[1:]
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*→ Recommended*\n{recommended}"[:2950],
            },
        })
        if alternatives:
            alt_text = "  ·  ".join(alternatives)
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_Other options:_ {alt_text}"[:2950]},
                ],
            })

    if deep_link:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Open operator surface",
                        "emoji": False,
                    },
                    "url": deep_link,
                    "style": "primary",
                },
            ],
        })

    if audit_row:
        # Soft footnote (D-067) — italic prose framing rather than a code
        # block. The data stays machine-readable (acknowledge_escalations_for
        # parses on this exact substring) but it reads as "for the record"
        # rather than as a config dump.
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"_for the record · {audit_row}_"},
            ],
        })

    return blocks


def send_escalation(
    *,
    role: str,
    severity: str,
    summary: str,
    what_i_see: Optional[List[str]] = None,
    what_id_do: Optional[List[str]] = None,
    deep_link: Optional[str] = None,
    audit_row: Optional[str] = None,
    rate_limit: Optional[int] = None,
    quote_id: Optional[int] = None,
    references: Optional[List[str]] = None,
) -> EscalationResult:
    """Send an escalation to Slack, with local-fallback + rate limiting.

    `role` is the agent's kebab-case role identifier (matches
    `X-Zspan-Agent-Role` header value).

    `rate_limit` is an optional per-call override. When None (the default),
    the effective limit is resolved via `resolve_hourly_limit(role)` —
    which consults the ROLE_HOURLY_LIMITS registry first, then falls back
    to DEFAULT_PER_ROLE_HOURLY_LIMIT. Pass an explicit int only when you
    need to override the role's documented policy for a specific call.

    Returns an EscalationResult. The agent logs from this — at minimum:
        "escalated <audit_row> severity=<severity> " +
        ("via slack" if delivered_to_slack else "via local queue")

    Behavior:
    1. Format the message.
    2. Always write to `pending_escalations` table FIRST. This guarantees
       the escalation is preserved even if Slack succeeds — the table is
       the canonical record; Slack is the notification layer. The
       `delivered_to_slack` flag updates after a successful POST.
    3. Check rate limit. If exceeded:
       - Mark the row `rate_limited=1`.
       - Send the once-per-window rate-limit notice to Slack (if
         configured + reachable).
       - Return early with rate_limited=True, queued_locally=True.
    4. Otherwise, attempt the POST to Slack.
    5. On success: update the row's `delivered_to_slack=1` + return
       `delivered_to_slack=True`.
    6. On failure: leave the row pending; return `queued_locally=True,
       error=<reason>`. James sees the badge count in the operator
       terminal and can recover.
    """
    if severity not in SEVERITY_LEVELS:
        raise ValueError(
            f"severity must be one of {SEVERITY_LEVELS}; got {severity!r}"
        )
    if not role or not role.strip():
        raise ValueError("role is required")
    if not summary or not summary.strip():
        raise ValueError("summary is required")

    # S-008 V0 / surface S-14: strip bidi controls + fence markers from all
    # agent-emitted free-text fields before they reach Slack, the
    # pending_escalations table, or the operator UI. Defensive belt-and-
    # suspenders against an off-path escalation that bypassed earlier layers.
    summary = _sanitize_escalation_text(summary) or summary
    what_i_see = _sanitize_escalation_bullets(what_i_see)
    what_id_do = _sanitize_escalation_bullets(what_id_do)

    # Rewrite localhost deep_links to the LAN-accessible base URL so the link
    # works from any device on the same network (notably James's phone).
    # Stored in the DB in rewritten form so the operator UI surfaces the same
    # URL whether read from Slack or from EscalationsInboxPage.
    deep_link = _rewrite_deep_link(deep_link)

    # Always write to the pending table first. This is the canonical record.
    # Lazy import — pending_escalations helper lives in database.py
    try:
        from database import insert_pending_escalation, mark_pending_escalation_delivered
    except Exception as e:
        # Defensive: the table helpers should always be available, but
        # if there's an import error, fall back to logging only.
        logger.error(
            "Could not import pending_escalations helpers (%s); "
            "escalation will be logged only: [%s/%s] %s",
            e, role, severity, summary,
        )
        return EscalationResult(error=f"import error: {e}")

    pending_id = insert_pending_escalation(
        role=role,
        severity=severity,
        summary=summary,
        what_i_see=what_i_see or [],
        what_id_do=what_id_do or [],
        deep_link=deep_link,
        audit_row=audit_row,
    )
    result = EscalationResult(queued_locally=True, pending_id=pending_id)

    webhook_url = _resolve_webhook_url()
    if not webhook_url:
        result.error = "slack_webhook_url not configured"
        return result

    # Resolve the effective limit (caller override > role registry > default).
    effective_limit = resolve_hourly_limit(role, rate_limit)

    # Rate limit check.
    within, _remaining = _check_rate_limit(role, effective_limit)
    if not within:
        result.rate_limited = True
        result.error = f"rate-limited ({effective_limit}/hour)"
        _maybe_send_rate_limit_notice(role, webhook_url, effective_limit)
        return result

    # Build the message payload. `text` is the push-notification fallback
    # (Slack uses it for mobile lock-screen previews + legacy-client
    # rendering); `blocks` is the structured Block Kit message that
    # renders for modern clients.
    fallback = _format_summary_line(role, severity, summary)
    blocks = _format_blocks(
        role=role,
        severity=severity,
        summary=summary,
        what_i_see=what_i_see,
        what_id_do=what_id_do,
        deep_link=deep_link,
        audit_row=audit_row,
        references=references,
    )
    # Severity is signaled by the colored-circle header marker (in `blocks`);
    # no attachment wrapping (attachments collapse behind "Show more").

    # Phase 2 / D-055 — prefer the bot-token send path when configured
    # (chat.postMessage returns ts so reactions can map back to escalations
    # and clip attachments can thread under the message). Webhook stays as
    # graceful-degradation fallback.
    if bot_path_available():
        # D-062a / D-071 (chunk 1b): orchestrator-attributed escalations go
        # to James's operator DM instead of the shared agents channel —
        # when both owner_user_id and the DM channel resolve cleanly. If
        # either step fails, fall through to the shared-channel post so the
        # message still lands somewhere visible (degraded-mode).
        channel_override: Optional[str] = None
        thread_ts: Optional[str] = None
        target_surface = "shared-channel"
        if role == "orchestrator":
            dm_channel = _resolve_orchestrator_dm_channel()
            if dm_channel:
                channel_override = dm_channel
                target_surface = "owner-dm"
                # Optional thread context — set by the Mode B instructed-spawn
                # script so the orchestrator's reply threads to the inbound
                # DM that triggered the spawn. Heartbeat-mode spawns don't
                # set it, so reads as None and the message starts a new top-
                # level thread in the DM.
                env_thread_ts = (os.environ.get("ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS") or "").strip()
                if env_thread_ts:
                    thread_ts = env_thread_ts
            else:
                logger.warning(
                    "slack_notifier: orchestrator escalation falling back to "
                    "shared channel (DM channel did not resolve; "
                    "slack_owner_user_id configured? bot has im:write?)"
                )

        bot_resp = _post_via_bot(
            fallback, blocks,
            channel_override=channel_override,
            thread_ts=thread_ts,
        )
        if bot_resp.get("ok"):
            ts = bot_resp.get("ts")
            mark_pending_escalation_delivered(pending_id, slack_message_ts=ts)
            result.delivered_to_slack = True
            result.queued_locally = False
            result.slack_message_ts = ts
            logger.info(
                "slack_notifier: posted role=%s severity=%s to %s ts=%s thread=%s",
                role, severity, target_surface, ts, thread_ts or "(top-level)",
            )
            # Optional clip attach for disputed-quote escalations. Never
            # attaches for orchestrator (orchestrator doesn't pass quote_id);
            # safe regardless of which surface we posted to.
            if quote_id is not None and ts:
                clip_resp = attach_clip_for_quote(quote_id, ts)
                if clip_resp.get("ok"):
                    result.clip_attached = True
                else:
                    result.clip_error = clip_resp.get("error")
            return result
        # Bot path failed — log the reason but DON'T fall through to webhook
        # for chat.postMessage failures (errors here usually mean a real
        # config issue James should fix, not transient). Surface via
        # EscalationResult.error; row stays queued_locally for the operator
        # to acknowledge from EscalationsInboxPage.
        result.error = (
            f"chat.postMessage failed ({target_surface}): {bot_resp.get('error')}"
        )
        return result

    # Webhook fallback — Phase 1 path. No ts captured (webhook response
    # doesn't expose it), so reactions on these messages can't be
    # dispatched programmatically. The operator uses EscalationsInboxPage
    # for resolution instead.
    try:
        resp = requests.post(
            webhook_url,
            # Top-level blocks (no attachment) so the full message stays
            # visible; severity is in the colored-circle header marker.
            data=json.dumps({"text": fallback, "blocks": blocks}),
            headers={"Content-Type": "application/json"},
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200 and resp.text.strip() == "ok":
            mark_pending_escalation_delivered(pending_id)
            result.delivered_to_slack = True
            result.queued_locally = False
            return result
        result.error = f"HTTP {resp.status_code}: {resp.text[:80]}"
        return result
    except requests.RequestException as exc:
        result.error = f"network error: {exc}"
        return result


# ── Convenience wrappers per severity ─────────────────────────────────


def info(role: str, summary: str, **kwargs) -> EscalationResult:
    return send_escalation(role=role, severity="info", summary=summary, **kwargs)


def decision(role: str, summary: str, **kwargs) -> EscalationResult:
    return send_escalation(role=role, severity="decision", summary=summary, **kwargs)


def blocked(role: str, summary: str, **kwargs) -> EscalationResult:
    return send_escalation(role=role, severity="blocked", summary=summary, **kwargs)


def error(role: str, summary: str, **kwargs) -> EscalationResult:
    return send_escalation(role=role, severity="error", summary=summary, **kwargs)


# ── DM prose path (D-073) — daily-brief plain-text posts ─────────────
#
# Distinct from send_escalation: the daily brief is NOT an escalation. It's a
# scheduled informational digest. Bypasses the pending_escalations table
# (won't pollute the badge count), bypasses the Block Kit formatter (plain
# prose paragraphs per James's chunk-2 format choice), bypasses the rate
# limiter (cron-fired once daily; rate-limiting is irrelevant).


def send_dm_prose(
    text: str,
    thread_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Post a plain-text message to James's operator DM.

    Used by the daily-brief script and any future scheduled-digest paths
    that want to land readable prose in the DM without the escalation
    wrapper's structured Block Kit shape.

    Returns a dict with `ok`, `ts`, `channel`, `error` (when not ok). Caller
    decides whether to log the failure; this function logs at WARNING when
    something goes wrong but never raises (the brief script must keep
    running through transient Slack issues).

    `thread_ts` is optional — if set, threads the post to that ts. Daily
    briefs don't thread (they're standalone messages); reserved for any
    on-demand-brief path that wants to reply to a James DM.

    D-073 / Stage B piece 2 chunk 2.
    """
    if not text or not text.strip():
        return {"ok": False, "ts": None, "channel": None, "error": "empty text"}

    dm_channel = _resolve_orchestrator_dm_channel()
    if not dm_channel:
        logger.warning(
            "slack_notifier.send_dm_prose: DM channel did not resolve "
            "(slack_owner_user_id configured? bot has im:write?). "
            "Brief NOT delivered."
        )
        return {"ok": False, "ts": None, "channel": None, "error": "DM channel not resolved"}

    if not bot_path_available():
        logger.warning(
            "slack_notifier.send_dm_prose: bot path not configured. "
            "Brief NOT delivered."
        )
        return {"ok": False, "ts": None, "channel": None, "error": "bot path not configured"}

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except Exception as e:
        logger.error("slack_sdk import failed: %s", e)
        return {"ok": False, "ts": None, "channel": None, "error": f"import: {e}"}

    token = _resolve_bot_token()
    client = WebClient(token=token)
    try:
        post_kwargs: Dict[str, Any] = {
            "channel": dm_channel,
            "text": text,
            # No blocks — plain text only. Slack renders mrkdwn (*bold*,
            # _italic_, `code`) inline, which is the lightweight emphasis
            # the brief uses for phone scanning.
            "mrkdwn": True,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            post_kwargs["thread_ts"] = thread_ts
        resp = client.chat_postMessage(**post_kwargs)
        ok = bool(resp.get("ok"))
        if not ok:
            logger.warning(
                "slack_notifier.send_dm_prose: chat.postMessage returned ok=false: %s",
                resp.get("error"),
            )
        return {
            "ok": ok,
            "ts": resp.get("ts"),
            "channel": resp.get("channel"),
            "error": resp.get("error"),
        }
    except SlackApiError as e:
        err = str(e.response.get("error", e))
        logger.warning("slack_notifier.send_dm_prose: SlackApiError: %s", err)
        return {"ok": False, "ts": None, "channel": None, "error": err}
    except Exception as e:
        logger.warning("slack_notifier.send_dm_prose: unexpected error: %s", e)
        return {"ok": False, "ts": None, "channel": None, "error": str(e)}


if __name__ == "__main__":
    # Smoke test — fires a fake escalation as if the Disputed Quotes
    # Reviewer hit a rule-9 case. If slack_webhook_url is not configured,
    # falls back cleanly to pending_escalations.
    result = send_escalation(
        role="disputed-quotes-reviewer",
        severity="decision",
        summary="Cannot decide quote #42 — content-bearing omission requires operator judgment",
        what_i_see=[
            "Gemini flagged: 'misses for the overtime at the very end'",
            "The omitted phrase is the semantic completion of a payment clause",
            "Current quote_text ends mid-thought without it",
        ],
        what_id_do=[
            "Lean toward verify-as-is — the omission appears trailing-incidental — but I'm not sure",
        ],
        deep_link="http://localhost:3000/?view=disputed-quotes&focus=42",
        audit_row="quotes.id=42",
    )
    print("Escalation result:", result.to_dict())

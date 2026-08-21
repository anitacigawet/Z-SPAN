"""slack_listener — Socket Mode listener for the S-004 Phase 2 reaction surface
=============================================================================

Opens a long-lived Socket Mode WebSocket connection to Slack and dispatches
events to the appropriate handlers. The connection runs in a Flask-spawned
daemon thread; the SDK handles reconnect on transient failures.

Two event classes handled:

1. `reaction_added` (D-055, S-004 Phase 2) — operator reactions on escalation
   messages in the shared agents channel. Dispatched to `slack_actions.py`.

2. `message` with `channel_type=im` (D-062a / Stage B piece 2 chunk 1a,
   2026-05-30) — direct messages to the bot in the operator DM thread.
   Spawn an orchestrator session via `ops/orchestrator-instructed-spawn.ps1`
   with James's message as the Mode B instruction. The orchestrator handles
   the request per its manual; the listener just posts an immediate "got it"
   ack to the DM thread so James knows the message landed.

   Filter: only James (`slack_owner_user_id` in user_settings.json) — the
   single-operator pattern. Other users DMing the bot are ignored silently
   (defense-in-depth; the bot SHOULD only have a DM with James, but if it
   ends up in another DM by accident, no spawn happens).

   Voice memos (Stage B piece 2 chunk 7, 2026-05-31) ride the same DM
   surface. When the message carries an audio file (iOS voice memo /
   Slack Clip / generic audio upload), the listener downloads it via the
   Slack files API (using the bot token's `files:read` scope), transcribes
   via `whisper_client.transcribe_audio_file`, and treats the transcript
   as if James had typed it. The ack includes a preview of the transcript
   so James can sanity-check what was heard before the orchestrator reasons
   on it.

V1 architecture (D-055):
- Started by `api_server.py § _maybe_start_slack_listener` at module import,
  right after init_db(). Idempotent — repeated calls are no-ops.
- No-op when bot_path_available() is False (Phase 1 webhook-only mode).
- Reactions on messages NOT in pending_escalations are ignored silently
  (operators may use emojis for their own purposes — only our taxonomy
  triggers actions).
- Reactions outside the V1 taxonomy (`white_check_mark` / `eyes` /
  `no_entry`) are ignored silently too.
- DMs from non-owner users are ignored silently. DMs with no text or from
  the bot itself (echo-back) are skipped.

Slack app scope additions for DM handling (one-time, James does in app config):
  - `im:history` — read DM messages
  - `im:read` — see DM channel metadata
  - `im:write` — post replies into the DM
  - `files:read` — download voice-memo files (chunk 7); without this
    scope Slack returns an HTML viewer page instead of the audio bytes
    and the voice-memo path acks a clear error
  - Event subscription: `message.im`
  - Messages tab enabled on the app's "Home" config

Failure modes:
- Bot token revoked → SDK raises on connect; the listener thread logs +
  exits. Next Flask restart picks it up. Webhook escalation path stays
  fully functional (Phase 1 stays alive).
- Network blip → SDK reconnects automatically.
- Handler crash → caught in slack_actions.dispatch; logged + listener
  keeps running.
- Instructed-spawn fork fails → the DM handler logs + acks James anyway
  with a clear error message; the listener thread stays alive.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Resolve the Mac fleet runner absolute path once. This file lives at
# 02_Core_Project/council_navigator/parsers/slack_listener.py;
# the runner lives at ops/fleet_heartbeat.py at the repo root. The instructed
# spawn is `fleet_heartbeat.py --role orchestrator-instructed-spawn ...` (D-120
# Mac launchd port, superseding ops/orchestrator-instructed-spawn.ps1).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_INSTRUCTED_SPAWN_SCRIPT = _REPO_ROOT / "ops" / "fleet_heartbeat.py"


_LISTENER_LOCK = threading.Lock()
_LISTENER_THREAD: Optional[threading.Thread] = None


# ── Voice-memo plumbing (Stage B piece 2 chunk 7, 2026-05-31) ─────────
#
# DMs that carry a voice memo (iOS Slack hold-mic-to-record) arrive with
# subtype="file_share" + a files[] entry shaped roughly like:
#   {"name": "audio_message.mp4", "mimetype": "audio/mp4" (or
#    "video/mp4" on older clients per Slack bug openclaw#4008),
#    "media_display_type": "audio_message",
#    "url_private_download": "...",
#    "subtype": "slack_audio" (sometimes; file-subtype not message-subtype)}
# We download with the bot's `files:read` scope, transcribe via Whisper,
# and treat the transcript as if James had typed it.
#
# The blanket subtype filter that existed before chunk 7 dropped these
# silently — they share the "non-None subtype" shape with message edits
# and channel events. We swap that for a known-reject set so unknown
# subtypes (especially "file_share") fall through to the voice-memo
# inspection below.

_REJECT_SUBTYPES = frozenset({
    "message_changed",
    "message_deleted",
    "message_replied",
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "pinned_item",
    "unpinned_item",
    "thread_broadcast",
})


def _is_voice_memo_file(file_payload: dict) -> bool:
    """True iff the given Slack files[] entry looks like a voice memo
    or an audio attachment we should treat as one.

    Defensive against three known Slack inconsistencies:
      1. iOS native voice memos historically arrive with mimetype
         "video/mp4" (Slack bug openclaw#4008); the durable signal is
         the filename pattern "audio_message[<digits>].mp4".
      2. Recent Slack Clips set files[].subtype="slack_audio".
      3. The undocumented media_display_type field is the most
         deliberate "this is voice audio" signal when present
         (per node-slack-sdk #2040).
    """
    name = (file_payload.get("name") or "").lower()
    mime = (file_payload.get("mimetype") or "").lower()
    mdt = (file_payload.get("media_display_type") or "").lower()
    fsub = (file_payload.get("subtype") or "").lower()
    return (
        mdt == "audio_message"
        or fsub == "slack_audio"
        or name.startswith("audio_message")
        or mime.startswith("audio/")
        or (mime == "video/mp4" and name.startswith("audio_message"))
    )


def _select_voice_memo_file(files: list) -> Optional[dict]:
    """Return the first voice-memo file in `files`, or None."""
    for f in files or []:
        if isinstance(f, dict) and _is_voice_memo_file(f):
            return f
    return None


def _post_voice_memo_error(
    web_client,
    channel: str,
    thread_ts: str,
    detail: str,
) -> None:
    """Best-effort error ack for the voice-memo path. Swallows any
    second-order Slack-API error — better to silently fail the ack
    than crash the listener thread.
    """
    try:
        web_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                f"Heard your voice memo but {detail}. The orchestrator "
                "wasn't spawned. Investigate from the operator terminal."
            ),
        )
    except Exception:
        pass


def _transcribe_voice_memo(
    file_payload: dict,
    web_client,
    channel: str,
    thread_ts: str,
) -> Optional[str]:
    """Download a Slack voice-memo file, run Whisper, return the
    transcript text. Returns None on any failure (with a thread reply
    already posted explaining the failure to James).
    """
    try:
        from slack_file_downloader import download_slack_file, SlackFileDownloadError
    except ImportError as e:
        logger.exception(
            "slack_listener: slack_file_downloader unimportable: %s", e,
        )
        _post_voice_memo_error(
            web_client, channel, thread_ts,
            "voice-memo plumbing isn't installed correctly",
        )
        return None
    try:
        from whisper_client import transcribe_audio_file, WhisperError
    except ImportError as e:
        logger.exception("slack_listener: whisper_client unimportable: %s", e)
        _post_voice_memo_error(
            web_client, channel, thread_ts,
            "whisper_client is unavailable",
        )
        return None

    tmp_path = None
    try:
        tmp_path = download_slack_file(file_payload)
    except SlackFileDownloadError as e:
        logger.exception("slack_listener: voice-memo download failed: %s", e)
        _post_voice_memo_error(
            web_client, channel, thread_ts,
            f"couldn't download the voice memo ({e})",
        )
        return None

    try:
        result = transcribe_audio_file(tmp_path)
    except WhisperError as e:
        logger.exception(
            "slack_listener: voice-memo transcription failed: %s", e,
        )
        _post_voice_memo_error(
            web_client, channel, thread_ts,
            f"Whisper couldn't transcribe it ({e})",
        )
        return None
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    words = result.get("words") or []
    text = " ".join(w.get("word", "") for w in words).strip()
    return text or None


def is_running() -> bool:
    """True iff the listener daemon thread is alive."""
    with _LISTENER_LOCK:
        return _LISTENER_THREAD is not None and _LISTENER_THREAD.is_alive()


def start_listener_thread() -> bool:
    """Idempotent. Returns True if the listener is now running (or was
    already running), False if it can't start (bot path not configured).
    """
    global _LISTENER_THREAD
    with _LISTENER_LOCK:
        if _LISTENER_THREAD is not None and _LISTENER_THREAD.is_alive():
            return True
        from slack_notifier import bot_path_available
        if not bot_path_available():
            logger.info(
                "slack_listener: bot path not configured (no slack_bot_token "
                "/ slack_channel_id); listener will not start"
            )
            return False
        # Verify slack_sdk is importable before claiming we're running.
        try:
            from slack_sdk import WebClient  # noqa: F401
            from slack_sdk.socket_mode import SocketModeClient  # noqa: F401
        except Exception as e:
            logger.error("slack_listener: slack_sdk import failed: %s", e)
            return False
        thread = threading.Thread(
            target=_run_listener_blocking,
            name="slack_listener",
            daemon=True,
        )
        thread.start()
        _LISTENER_THREAD = thread
        # WARNING-level so Flask's default stdout shows whether the listener
        # actually started — without it, ops have to inspect the running
        # process to know if Socket Mode is alive.
        logger.warning("slack_listener: daemon thread started")
        return True


def _run_listener_blocking() -> None:
    """The actual listener loop. Connects to Socket Mode, processes events
    until process exit. SDK handles reconnect on transient failures; an
    unrecoverable error (e.g., token revoked) logs + exits the thread.
    """
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_notifier import _resolve_bot_token, _resolve_channel_id
    except Exception as e:
        logger.exception("slack_listener: import or resolve failed: %s", e)
        return

    bot_token = _resolve_bot_token()
    settings = _load_app_token()
    if not bot_token or not settings:
        logger.error("slack_listener: missing bot or app token; cannot connect")
        return

    web_client = WebClient(token=bot_token)
    client = SocketModeClient(app_token=settings, web_client=web_client)

    def _handle(socket_client: SocketModeClient, req: SocketModeRequest) -> None:
        # Always ack the envelope back to Slack first so the event isn't
        # redelivered. We then process — if our handler fails, we log;
        # Slack doesn't retry already-acked envelopes.
        try:
            response = SocketModeResponse(envelope_id=req.envelope_id)
            socket_client.send_socket_mode_response(response)
        except Exception as ack_err:
            logger.warning("slack_listener: failed to ack envelope: %s", ack_err)

        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}

        event_type = event.get("type")
        if event_type == "reaction_added":
            _process_reaction_added(event, web_client)
            return
        # D-062a / Stage B piece 2 chunk 1a: the operator DM inbound surface.
        # Only DM messages (channel_type == 'im') get routed; channel/group
        # messages stay outside this handler's lane.
        if event_type == "message" and event.get("channel_type") == "im":
            _process_im_message(event, web_client)
            return

    client.socket_mode_request_listeners.append(_handle)

    try:
        client.connect()
        # WARNING-level so the connect signal shows in Flask's stdout (Flask's
        # default log level suppresses INFO). After this line is visible, the
        # listener is alive + receiving events.
        logger.warning("slack_listener: connected to Socket Mode")
        # DM-routing config visibility — same log level so it's easy to see
        # whether DM routing is enabled at boot.
        owner_id = _resolve_owner_user_id()
        if owner_id:
            logger.warning(
                "slack_listener: DM routing enabled for user_id=%s "
                "(operator DM -> orchestrator instructed-spawn)",
                owner_id,
            )
        else:
            logger.warning(
                "slack_listener: DM routing DISABLED — "
                "set slack_owner_user_id in user_settings.json to enable "
                "(D-062a operator DM bridge)"
            )
        # Block forever. The daemon-thread flag ensures the thread dies
        # when Flask exits.
        threading.Event().wait()
    except Exception as e:
        logger.exception("slack_listener: connection or run-loop failed: %s", e)


def _load_app_token() -> Optional[str]:
    """Resolve the app-level token used for Socket Mode handshake."""
    import os
    from env_config import load_user_settings
    env = (os.environ.get("SLACK_APP_TOKEN") or "").strip()
    if env:
        return env
    settings = load_user_settings()
    return (settings.get("slack_app_token") or "").strip() or None


def _resolve_owner_user_id() -> Optional[str]:
    """Resolve James's Slack user ID for the DM-handling filter.

    Thin wrapper around `slack_notifier.resolve_owner_user_id` (D-071 chunk 1b
    promoted the helper into slack_notifier so the outbound DM-routing path
    can share it). Kept here as a local alias so the existing call sites in
    `_process_im_message` + `_run_listener_blocking` don't need touching.

    Source priority: SLACK_OWNER_USER_ID env var > slack_owner_user_id in
    user_settings.json. Returns None if neither is set — the DM handler
    treats that as "DM gating not configured, ignore inbound DMs silently"
    rather than spawning the orchestrator for any DM.
    """
    from slack_notifier import resolve_owner_user_id
    return resolve_owner_user_id()


def _process_reaction_added(event: dict, web_client) -> None:
    """Dispatch a reaction_added event through slack_actions, post a
    thread reply with the result. All exceptions are caught + logged —
    a handler failure must not crash the listener thread.
    """
    try:
        reaction = event.get("reaction") or ""
        user_id = event.get("user") or ""
        item = event.get("item") or {}
        msg_ts = item.get("ts") or ""
        channel = item.get("channel") or ""

        if not msg_ts:
            return
        # Ignore reactions on non-message items (file shares, etc.)
        if (item.get("type") or "") != "message":
            return

        from slack_actions import dispatch, REACTION_HANDLERS
        if reaction not in REACTION_HANDLERS:
            return  # Outside V1 taxonomy — silent ignore

        from database import find_pending_escalation_by_message_ts
        row = find_pending_escalation_by_message_ts(msg_ts)
        if row is None:
            # Reaction on a message we don't track. Could be a legacy
            # webhook-fallback escalation (no ts captured) or an unrelated
            # message in the channel. Silent ignore.
            return

        # Don't act on reactions to messages that are already acknowledged
        # (idempotent — operators may stack reactions).
        if row.get("acknowledged_at"):
            try:
                web_client.chat_postMessage(
                    channel=channel,
                    thread_ts=msg_ts,
                    text=f"Already acknowledged at {row['acknowledged_at']} (by {row.get('acknowledged_by') or 'operator'}). No additional action taken.",
                )
            except Exception:
                pass
            return

        result = dispatch(reaction, row, user_id)
        if result is None:
            return

        # Post the thread reply confirming what happened.
        try:
            web_client.chat_postMessage(
                channel=channel,
                thread_ts=msg_ts,
                text=result.get("reply_text") or "Done.",
            )
        except Exception as post_err:
            logger.warning(
                "slack_listener: thread reply failed for ts=%s: %s",
                msg_ts, post_err,
            )

        # Audit trail (server log, not the activity stream — the listener
        # runs from Flask but doesn't have request context for the agent-role
        # prefix to apply; just log plainly).
        logger.info(
            "slack_listener: reaction=%s user=%s pending_escalation_id=%s ok=%s side_effect=%s",
            reaction, user_id, row.get("id"), result.get("ok"), result.get("side_effect"),
        )
    except Exception as e:
        logger.exception("slack_listener: _process_reaction_added crashed: %s", e)


def _process_im_message(event: dict, web_client) -> None:
    """Handle a direct-message event in the operator DM thread.

    Routing:
      - Skip echoes of the bot's own posts (subtype=bot_message OR bot_id set).
      - Skip if `slack_owner_user_id` isn't configured (cannot safely route
        DMs without an owner filter; ignore silently).
      - Skip if the sender isn't the configured owner (defense-in-depth;
        the bot SHOULD only have a DM with James, but unexpected DMs from
        other users get dropped on the floor instead of spawning agents).
      - Skip messages with no usable text (file uploads, edits, etc.).
      - Post an immediate "got it" ack to the DM thread.
      - Fire ops/orchestrator-instructed-spawn.ps1 detached so the listener
        thread returns instantly. The orchestrator handles the rest per its
        Mode B instructions.

    All exceptions are caught + logged — a handler failure must not crash
    the listener thread.

    Stage B piece 2 chunk 1a (D-062a DM bridge inbound).
    """
    try:
        # Drop bot echoes early. Slack delivers a `message` event for the
        # bot's OWN chat_postMessage too; we'd loop forever if we didn't
        # filter it.
        if event.get("subtype") == "bot_message" or event.get("bot_id"):
            return
        # Drop known-noise subtypes (edits, deletes, channel events).
        # Voice memos arrive with subtype="file_share" + files[] carrying
        # an audio_message.mp4 — those fall through to the voice-memo
        # branch below the owner check.
        if event.get("subtype") in _REJECT_SUBTYPES:
            return

        text = (event.get("text") or "").strip()
        user_id = (event.get("user") or "").strip()
        channel = (event.get("channel") or "").strip()
        message_ts = (event.get("ts") or "").strip()

        # `text` may be empty for voice memos (no caption); other fields
        # are required regardless. The empty-text drop lives below the
        # voice-memo branch so the transcript can populate `text` first.
        if not user_id or not channel or not message_ts:
            return

        owner_id = _resolve_owner_user_id()
        if not owner_id:
            # Not configured. Ignore silently — never spawn the orchestrator
            # for an arbitrary DM.
            logger.info(
                "slack_listener: ignoring DM from user=%s — "
                "slack_owner_user_id not configured",
                user_id,
            )
            return

        if user_id != owner_id:
            # DM from someone other than James. Drop silently — no ack, no
            # spawn. This is the defense-in-depth against accidentally
            # routing third-party DMs into the orchestrator's auth.
            logger.warning(
                "slack_listener: ignoring DM from non-owner user=%s "
                "(expected %s)",
                user_id, owner_id,
            )
            return

        # Voice-memo branch (Stage B piece 2 chunk 7, 2026-05-31). If
        # the DM carries an audio file (iOS voice memo / Slack Clip /
        # audio upload), download + Whisper-transcribe + treat the
        # transcript as the instruction text. Caption text (if any)
        # gets concatenated alongside the transcript so the orchestrator
        # sees both.
        audio_file = _select_voice_memo_file(event.get("files") or [])
        if audio_file:
            logger.info(
                "slack_listener: voice memo detected in DM channel=%s "
                "ts=%s file=%s mime=%s size=%s",
                channel, message_ts,
                audio_file.get("name"), audio_file.get("mimetype"),
                audio_file.get("size"),
            )
            transcript = _transcribe_voice_memo(
                audio_file, web_client, channel, message_ts,
            )
            if not transcript:
                # _transcribe_voice_memo already acked the failure; bail.
                return
            text = (text + " " + transcript).strip() if text else transcript

        # Final empty-text guard: covers genuinely empty DMs AND voice
        # memos whose transcript came back with no recognizable speech.
        if not text:
            return

        # Confirm the spawn script exists before acking — we'd rather tell
        # James the bridge is broken than ack and silently drop his message.
        if not _INSTRUCTED_SPAWN_SCRIPT.is_file():
            logger.error(
                "slack_listener: instructed-spawn script missing at %s",
                _INSTRUCTED_SPAWN_SCRIPT,
            )
            try:
                web_client.chat_postMessage(
                    channel=channel,
                    thread_ts=message_ts,
                    text=(
                        "I heard your message, but my instructed-spawn script "
                        "is missing on disk so I can't spawn the orchestrator. "
                        f"Expected at `{_INSTRUCTED_SPAWN_SCRIPT}`. "
                        "Investigate from the operator terminal."
                    ),
                )
            except Exception:
                pass
            return

        # Immediate ack so James knows the message landed. The ack is short
        # and the status_tailer (chunk 1c) edits it in place as the
        # orchestrator progresses -- Discord-bot style. The orchestrator's
        # actual reply lands as a separate threaded message via its
        # escalate path (D-072 DM routing).
        #
        # For voice memos, surface a preview of the Whisper transcript in
        # the ack so James can sanity-check what was heard BEFORE the
        # orchestrator reasons on it. Mis-transcription gets caught early.
        if audio_file:
            preview = text[:140] + ("…" if len(text) > 140 else "")
            ack_text = f'Heard: "{preview}" — spawning the orchestrator'
        else:
            ack_text = "Spawning the orchestrator"
        ack_ts: Optional[str] = None
        try:
            ack_resp = web_client.chat_postMessage(
                channel=channel,
                thread_ts=message_ts,
                text=ack_text,
            )
            ack_ts = ack_resp.get("ts")
        except Exception as ack_err:
            logger.warning(
                "slack_listener: ack post failed for DM channel=%s ts=%s: %s",
                channel, message_ts, ack_err,
            )
            # Don't return — still attempt the spawn. James can read the
            # orchestrator's eventual output even if the ack didn't post.

        # Spawn the orchestrator detached so this handler returns instantly.
        # The runner writes its own JSONL transcript via the metering wrapper;
        # we don't capture stdout/stderr here (the listener thread doesn't want
        # them). start_new_session detaches it (POSIX; replaces the Windows
        # DETACHED_PROCESS creationflags). D-120 Mac launchd port.
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(_INSTRUCTED_SPAWN_SCRIPT),
                    "--role", "orchestrator-instructed-spawn",
                    "--instruction-text", text,
                    "--dm-channel", channel,
                    "--thread-ts", message_ts,
                ],
                cwd=str(_REPO_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            logger.info(
                "slack_listener: spawned instructed orchestrator for DM "
                "channel=%s ts=%s user=%s text_len=%d pid=%s",
                channel, message_ts, user_id, len(text), proc.pid,
            )

            # Start the live-updating ack watcher (chunk 1c). Only fires
            # when both the ack landed (we have an ack_ts to edit) AND
            # the spawn succeeded. Watcher is a daemon thread; it dies
            # with Flask and the listener doesn't need to join it.
            if ack_ts:
                try:
                    from slack_listener_status_tailer import start_status_watcher
                    start_status_watcher(
                        web_client=web_client,
                        channel=channel,
                        ack_ts=ack_ts,
                        proc=proc,
                    )
                except Exception as watch_err:
                    logger.warning(
                        "slack_listener: status watcher failed to start "
                        "for ack_ts=%s: %s (spawn continues; ack stays static)",
                        ack_ts, watch_err,
                    )
        except Exception as spawn_err:
            logger.exception(
                "slack_listener: instructed-spawn failed for DM "
                "channel=%s ts=%s: %s",
                channel, message_ts, spawn_err,
            )
            try:
                web_client.chat_postMessage(
                    channel=channel,
                    thread_ts=message_ts,
                    text=(
                        "Spawn failed: "
                        f"`{type(spawn_err).__name__}: {spawn_err}`. "
                        "The listener is alive but couldn't fire the "
                        "orchestrator script. Investigate from the "
                        "operator terminal."
                    ),
                )
            except Exception:
                pass
    except Exception as e:
        logger.exception("slack_listener: _process_im_message crashed: %s", e)


if __name__ == "__main__":
    # Smoke-run: start the listener and block. Useful for local testing
    # before wiring into api_server.py boot.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    started = start_listener_thread()
    print(f"listener started: {started}")
    if started:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\nexit")

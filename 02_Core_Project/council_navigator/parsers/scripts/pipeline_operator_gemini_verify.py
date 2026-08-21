#!/usr/bin/env python3.11
"""pipeline_operator_gemini_verify — drive the T-013 V4 verification workflow end-to-end via `gemini-webapi`.

D-069 wrapper: the verify-mode core. Supersedes D-068's Chrome-MCP-driven
verify mode. Takes a work_order_id, walks the meeting's review-queue batches,
calls `gemini-webapi` against the consumer Gemini Pro UI for each batch, writes
the responses, and triggers ingestion.

NO `claude -p` for verify mode. NO Chrome MCP. NO agent reasoning at runtime.
This is plain Python — runs against the operator's Google One AI Pro
entitlement at $0 marginal compute.

Forge-resistance properties (the structural wall, per D-066/D-068 discipline):
- Hardcoded base path for the review_queue: cannot write to arbitrary disk paths.
- Cookies sourced ONLY from env_config.get_gemini_consumer_cookies(): the
  wrapper cannot be talked into using a different identity by a prompt
  injection in the meeting metadata or the WO row.
- Slug components derived from the WO via Flask, not from CLI args.
- Target filenames hardcoded (RESPONSE.md per batch); never user-supplied.
- Body length cap on RESPONSE.md (2 MB).
- Wraps the existing pipeline_operator_action.py for [BUILD] + [INGEST] so the
  role-attribution + endpoint allowlist of the action wrapper still applies.

Usage:
    python3.11 scripts/pipeline_operator_gemini_verify.py --work-order-id 100688

Options:
    --skip-build       Skip the [BUILD] step (assume review queue already exists).
    --skip-ingest      Skip the [INGEST] step (just write RESPONSE.md files).
    --force-rewrite    Re-Gemini batches that already have RESPONSE.md (default: skip).
                       Existing RESPONSE.md is auto-backed-up to RESPONSE.md.bak.<epoch>
                       before being overwritten (real data safety, since media/ is
                       gitignored). If the backup itself fails, the rewrite is refused
                       for that batch.
    --batch <N>        Process only batch_NN (default: all batches).
    --dry-run          Walk the plan + report what would happen; don't touch Gemini or disk.

Cookies — required before first run. Either:
  - Set env vars SECURE_1PSID + SECURE_1PSIDTS in the shell that runs this, OR
  - Add gemini_secure_1psid + gemini_secure_1psidts to user_settings.json.
  To grab: open chrome://settings/cookies/detail?site=google.com in the Chrome
  signed into your Google One AI Pro account; copy the Content of cookies named
  __Secure-1PSID and __Secure-1PSIDTS.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# Make `parsers/` importable for env_config + slugify reuse.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from env_config import get_gemini_consumer_cookies  # noqa: E402
from slack_notifier import _slugify as canonical_slugify  # noqa: E402

# Hardcoded — agent cannot redirect.
_REPO_ROOT = Path(__file__).resolve().parents[4]
REVIEW_QUEUE_BASE = (
    _REPO_ROOT
    / "02_Core_Project"
    / "council_navigator"
    / "media"
    / "review_queue"
)

FLASK_BASE = "http://127.0.0.1:5001"
ROLE = "pipeline-operator"
HTTP_TIMEOUT = 30

ALLOWED_CLIP_EXTENSIONS = (".mp4", ".wav")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB

CITY_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MEETING_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
BATCH_DIR_PATTERN = re.compile(r"^batch_(\d{2,3})$")

# Per-batch Gemini timeout — verification batches tend to come back in 30s-3min;
# 6 min is a comfortable ceiling. Worth flagging if a batch hits this.
GEMINI_BATCH_TIMEOUT_SECONDS = 6 * 60

# Total session ceiling — one meeting per session, but cap total wall-clock to
# protect against runaway loops.
SESSION_TIMEOUT_SECONDS = 60 * 60  # 60 min


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _validate_slug(slug: str, pattern: re.Pattern, kind: str) -> tuple[bool, str]:
    if not slug:
        return False, f"{kind} slug is empty"
    if not pattern.fullmatch(slug):
        return False, f"{kind} slug {slug!r} fails pattern {pattern.pattern}"
    return True, ""


def fetch_work_order(work_order_id: int) -> dict:
    """Resolve WO via Flask (board-read style — GET only)."""
    url = f"{FLASK_BASE}/api/work-orders/{work_order_id}"
    headers = {"X-Zspan-Agent-Role": ROLE, "Accept": "application/json"}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    wo = data.get("work_order") if isinstance(data, dict) else None
    if not wo:
        wo = data
    for field in ("city_name", "meeting_title", "meeting_date", "state", "meeting_id"):
        if field not in wo:
            raise RuntimeError(
                f"WO {work_order_id} response is missing field {field!r}: {wo!r}"
            )
    return wo


def derive_slugs(wo: dict) -> tuple[str, str]:
    """Compute (city_slug, meeting_slug) using the canonical _slugify mirror
    from slack_notifier. Matches what build_review_queue wrote at clip extraction."""
    city_slug = canonical_slugify(wo["city_name"])
    meeting_slug = canonical_slugify(wo["meeting_title"])
    return city_slug, meeting_slug


def resolve_meeting_dir(city_slug: str, meeting_date: str, meeting_slug: str) -> Optional[Path]:
    """Resolve and validate the meeting's review_queue dir; None if not present.

    Slugs are strict-validated to block path traversal. The on-disk dir name
    is `<YYYY-MM-DD>__<meeting_slug>`.
    """
    ok, reason = _validate_slug(city_slug, CITY_SLUG_PATTERN, "city")
    if not ok:
        raise RuntimeError(f"DENIED: {reason}")
    ok, reason = _validate_slug(meeting_slug, MEETING_SLUG_PATTERN, "meeting")
    if not ok:
        raise RuntimeError(f"DENIED: {reason}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meeting_date):
        raise RuntimeError(f"DENIED: meeting_date {meeting_date!r} not YYYY-MM-DD")

    target = REVIEW_QUEUE_BASE / city_slug / f"{meeting_date}__{meeting_slug}"
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(REVIEW_QUEUE_BASE.resolve(strict=False))
    except Exception:
        raise RuntimeError(f"DENIED: resolved path escapes review_queue base: {target}")

    return target if target.exists() and target.is_dir() else None


def discover_batches(meeting_dir: Path) -> list[tuple[int, Path]]:
    """Return [(batch_num, batch_dir)] sorted ascending. Skips non-batch dirs."""
    out: list[tuple[int, Path]] = []
    for entry in sorted(meeting_dir.iterdir()):
        if not entry.is_dir():
            continue
        m = BATCH_DIR_PATTERN.match(entry.name)
        if not m:
            continue
        out.append((int(m.group(1)), entry))
    return sorted(out, key=lambda t: t[0])


def discover_clips(batch_dir: Path) -> list[Path]:
    """Find all .mp4/.wav clips in a batch dir, sorted. Returns [] if none."""
    return sorted(
        p for p in batch_dir.iterdir()
        if p.is_file() and p.suffix.lower() in ALLOWED_CLIP_EXTENSIONS
    )


def shell_action(sub_command: str, work_order_id: int) -> tuple[int, str]:
    """Shell to pipeline_operator_action.py for [BUILD] or [INGEST].
    Returns (exit_code, combined_output)."""
    script = _PARSERS_DIR / "scripts" / "pipeline_operator_action.py"
    cmd = [
        sys.executable, str(script), sub_command,
        "--work-order-id", str(work_order_id),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15 * 60,  # cold [BUILD] can take ~12 min
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after 15 min: {' '.join(cmd)}"
    except Exception as exc:
        return 1, f"EXEC FAILED: {exc}"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


async def verify_batch(client, batch_num: int, batch_dir: Path) -> tuple[bool, str]:
    """Drive one batch through Gemini. Returns (ok, message).

    On success, RESPONSE.md is written to batch_dir/RESPONSE.md.
    On non-auth failure (timeout, oversize, Gemini transient), nothing is
    written and the error is returned in the message.

    AuthError is re-raised — the caller stops iterating + escalates blocked,
    since cookie staleness affects every subsequent batch (no point trying
    9 more times with the same dead credentials).
    """
    # Lazy-import the exception class so the function definition itself
    # doesn't require gemini-webapi at import time.
    from gemini_webapi.exceptions import AuthError

    prompt_path = batch_dir / "PROMPT.md"
    if not prompt_path.is_file():
        return False, f"batch_{batch_num:02d}: PROMPT.md missing"

    prompt = prompt_path.read_text(encoding="utf-8")
    if not prompt.strip():
        return False, f"batch_{batch_num:02d}: PROMPT.md is empty"

    clips = discover_clips(batch_dir)
    if not clips:
        return False, f"batch_{batch_num:02d}: no .mp4/.wav clips found"

    log(f"  batch_{batch_num:02d}: {len(clips)} clip(s), prompt {len(prompt)} chars -> Gemini")

    try:
        response = await asyncio.wait_for(
            client.generate_content(prompt, files=[str(p) for p in clips]),
            timeout=GEMINI_BATCH_TIMEOUT_SECONDS,
        )
    except AuthError:
        raise  # let the outer loop handle this (escalate + stop)
    except asyncio.TimeoutError:
        return False, f"batch_{batch_num:02d}: Gemini timed out after {GEMINI_BATCH_TIMEOUT_SECONDS}s"
    except Exception as exc:
        return False, f"batch_{batch_num:02d}: gemini-webapi raised {type(exc).__name__}: {exc}"

    text = getattr(response, "text", None) or ""
    if not text.strip():
        return False, f"batch_{batch_num:02d}: response is empty / whitespace-only"

    body_bytes = text.encode("utf-8")
    if len(body_bytes) > MAX_RESPONSE_BYTES:
        return False, (
            f"batch_{batch_num:02d}: response {len(body_bytes)}b over {MAX_RESPONSE_BYTES}b cap; "
            "refusing to write"
        )

    target = batch_dir / "RESPONSE.md"
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except Exception as exc:
        return False, f"batch_{batch_num:02d}: write {target.name} failed: {exc}"

    return True, f"batch_{batch_num:02d}: wrote RESPONSE.md ({len(body_bytes)} bytes)"


def shell_escalate(severity: str, summary: str, see: list[str], do: list[str],
                   audit_row: str | None = None) -> None:
    """Fire an escalation via pipeline_operator_escalate.py. Best-effort —
    if escalation itself fails, log + continue (the wrapper's own non-zero
    exit code is still surfaced to the spawn script + transcript)."""
    script = _PARSERS_DIR / "scripts" / "pipeline_operator_escalate.py"
    cmd: list[str] = [
        sys.executable, str(script),
        "--severity", severity,
        "--summary", summary,
    ]
    for line in see:
        cmd += ["--see", line]
    for line in do:
        cmd += ["--do", line]
    if audit_row:
        cmd += ["--audit-row", audit_row]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                                encoding="utf-8", errors="replace")
        if result.returncode == 0:
            log(f"  escalated (severity={severity}): {result.stdout.strip()}")
        else:
            log(f"  ESCALATION ITSELF FAILED (exit {result.returncode}): {result.stderr[:300]}")
    except Exception as exc:
        log(f"  ESCALATION ITSELF FAILED ({type(exc).__name__}): {exc}")


def escalate_stale_cookies(work_order_id: int, reason: str) -> None:
    """The cookies-stale-need-refresh escalation. severity=blocked because the
    workflow can't proceed without the operator's hand — same shape as the
    auth-expired blocked event in orchestrator.md.

    Cadence note: gemini-webapi v2.0.0 auto-refreshes __Secure-1PSIDTS in the
    background, so this fires only when __Secure-1PSID itself rotates (typically
    after the operator signs out of Google) — order-of-months apart, not hourly.
    """
    shell_escalate(
        severity="blocked",
        summary=(
            "Verify is paused — your Gemini Pro cookies need a refresh."
        ),
        see=[
            f"Tried to drive Gemini Pro for work order *{work_order_id}* and got "
            f"an authentication error from the unofficial wrapper.",
            f"Underlying reason: {reason}",
            "The library auto-refreshes the time-sensitive cookie on its own; "
            "this kind of failure means the *long-stable* `__Secure-1PSID` "
            "rotated (usually because you signed out + back in to Google).",
        ],
        do=[
            "Open chrome://settings/cookies/detail?site=google.com in the Chrome "
            "signed into your Google One AI Pro account; copy the Content of "
            "*__Secure-1PSID* and *__Secure-1PSIDTS*; update "
            "`gemini_secure_1psid` + `gemini_secure_1psidts` in "
            "`parsers/user_settings.json`; re-fire the verify.",
            "Or pause verify and circle back when you're at the terminal.",
        ],
        audit_row=f"work_orders.id={work_order_id}",
    )


def escalate_likely_stale_cookies(work_order_id: int, reason: str) -> None:
    """The softer "this looks like it might be auth, but I'm not sure" path.
    severity=decision because the signature (APIError 1100) was observed in
    the D-069 live test alongside repeated UNAUTHENTICATED warnings, but
    also overlaps with general gemini-webapi upstream weather (Google-side
    sagging, TLS-fingerprint 429s). The operator decides whether to refresh
    cookies or wait it out — neither is destructive.

    Distinct from escalate_stale_cookies above: that fires on a clean
    gemini_webapi.exceptions.AuthError (high confidence, blocked severity).
    This fires on the message-pattern heuristic only when AuthError did NOT
    fire — uncertain signal, lower-urgency surface. T-5 / D-070 follow-up.

    NOTE re UNAUTHENTICATED warnings: gemini-webapi v2.0.0 logs them via
    loguru even on successful requests (it's noise from the user-status
    probe), so the wrapper can't reliably detect them from inside the
    exception path. APIError 1100 alone is the surfaceable signal.
    """
    shell_escalate(
        severity="decision",
        summary=(
            "Verify failed in a way that might mean stale Gemini Pro cookies."
        ),
        see=[
            f"Tried to drive Gemini Pro for work order *{work_order_id}* and "
            f"got *APIError 1100* from the unofficial wrapper.",
            "This signature was observed in the D-069 live test and *may* "
            "mean cookies are stale — but it also overlaps with general "
            "upstream API weather (the wrapper's file-upload path has been "
            "sagging since late May). Lower confidence than a clean AuthError.",
            f"Underlying error: {reason}",
        ],
        do=[
            "Re-grab cookies from chrome://settings/cookies/detail?site=google.com "
            "in the Chrome signed into your Google One AI Pro account and update "
            "`gemini_secure_1psid` + `gemini_secure_1psidts` in "
            "`parsers/user_settings.json` — low cost, no harm if cookies were fine.",
            "Or wait out the upstream weather (a day or two) and retry the verify.",
        ],
        audit_row=f"work_orders.id={work_order_id}",
    )


def _is_likely_auth_apierror_message(msg: str) -> bool:
    """Heuristic: detect the APIError-1100 signature in a verify_batch
    failure message. Used to gate the soft-escalation path (above) once
    per session. Conservative: only matches when both 'APIError' and '1100'
    appear in the message — APIError alone is too generic."""
    return "APIError" in msg and "1100" in msg


async def main_async(args: argparse.Namespace) -> int:
    # Dry-run doesn't touch Gemini, so cookies aren't needed for the plumbing check.
    psid, psidts = ("", "")
    if not args.dry_run:
        psid, psidts = get_gemini_consumer_cookies()
        if not psid or not psidts:
            log("ERROR: Gemini cookies not configured.")
            log("  Either set env vars SECURE_1PSID + SECURE_1PSIDTS, or add")
            log("  gemini_secure_1psid + gemini_secure_1psidts to user_settings.json.")
            log("  To grab them: open chrome://settings/cookies/detail?site=google.com")
            log("  in the Chrome signed into your Google One AI Pro account; copy the")
            log("  Content of __Secure-1PSID and __Secure-1PSIDTS.")
            # First-time missing-cookies isn't a "stale" event (could be just
            # never-configured); escalate as blocked anyway so the orchestrator
            # surfaces it on the next heartbeat, but with a clearer summary.
            escalate_stale_cookies(
                args.work_order_id,
                reason="Gemini cookies are not configured (first-time setup, or env/user_settings entries were removed)",
            )
            return 3

    log(f"=== pipeline_operator_gemini_verify --work-order-id {args.work_order_id} ===")

    try:
        wo = fetch_work_order(args.work_order_id)
    except Exception as exc:
        log(f"ERROR: could not fetch WO {args.work_order_id}: {exc}")
        return 4

    log(
        f"  WO {args.work_order_id} | meeting_id={wo['meeting_id']} | "
        f"{wo['meeting_date']} | {wo['city_name']!r} | state={wo['state']}"
    )

    if wo["state"] != "completed":
        log(
            f"ERROR: WO state is {wo['state']!r}, not 'completed'. "
            "Verify mode only operates on completed WOs."
        )
        return 5

    try:
        city_slug, meeting_slug = derive_slugs(wo)
        meeting_dir = resolve_meeting_dir(city_slug, wo["meeting_date"], meeting_slug)
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        return 6

    log(f"  city_slug={city_slug} meeting_slug={meeting_slug}")
    log(f"  expected dir: {meeting_dir or '(not yet built)'}")

    # [BUILD] phase
    if meeting_dir is None or not discover_batches(meeting_dir):
        if args.skip_build:
            log("ERROR: --skip-build set but review queue doesn't exist. Run [BUILD] first.")
            return 7
        if args.dry_run:
            log("  [DRY-RUN] would invoke pipeline_operator_action.py build-review-queue")
        else:
            log("  Review queue not built yet. Invoking [BUILD]...")
            rc, output = shell_action("build-review-queue", args.work_order_id)
            log(output.rstrip())
            if rc != 0:
                # Detect the specific "nothing to build" signal from
                # build_review_queue.py: hero quotes exist but aren't aligned
                # (no word_timings), OR all hero quotes are already verified.
                # That's a clean no-op, not a hard failure -- exit 0.
                if "No pending/disputed aligned quotes found" in output:
                    log("")
                    log("  [BUILD] found nothing to verify for this meeting.")
                    log("  Usual causes:")
                    log("    - Hero quotes haven't been aligned yet (no word_timings;")
                    log("      run T-013 alignment first), OR")
                    log("    - All hero quotes are already in verified status.")
                    log("  Exiting cleanly -- no verify work to do.")
                    return 0
                log(f"ERROR: [BUILD] exited {rc}")
                return 8
            # Re-resolve after BUILD
            meeting_dir = resolve_meeting_dir(city_slug, wo["meeting_date"], meeting_slug)
            if meeting_dir is None:
                log("ERROR: [BUILD] succeeded but expected meeting dir still missing.")
                return 9

    batches = discover_batches(meeting_dir) if meeting_dir else []
    if not batches:
        log("ERROR: no batch dirs found after [BUILD].")
        return 10

    if args.batch is not None:
        batches = [b for b in batches if b[0] == args.batch]
        if not batches:
            log(f"ERROR: --batch {args.batch} doesn't match any of the available batches.")
            return 11

    log(f"  {len(batches)} batch(es) to process: {[n for n, _ in batches]}")

    # [VERIFY] phase
    if args.dry_run:
        for batch_num, batch_dir in batches:
            existing = (batch_dir / "RESPONSE.md").exists()
            verb = "SKIP (exists)" if existing and not args.force_rewrite else "would-Gemini"
            clips = discover_clips(batch_dir)
            log(f"  [DRY-RUN] batch_{batch_num:02d}: {verb}, {len(clips)} clip(s)")
        return 0

    from gemini_webapi import GeminiClient, set_log_level  # imported here so --dry-run works without the dep
    from gemini_webapi.exceptions import AuthError  # AuthError is the wrapper's "cookies stale" signal
    set_log_level("WARNING")

    log("  Initializing GeminiClient...")
    client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts)
    try:
        await asyncio.wait_for(client.init(timeout=30), timeout=45)
    except AuthError as exc:
        log(f"ERROR: GeminiClient init failed with auth error: {exc}")
        escalate_stale_cookies(
            args.work_order_id,
            reason=f"AuthError during GeminiClient.init: {exc}",
        )
        return 12
    except Exception as exc:
        log(f"ERROR: GeminiClient init failed: {type(exc).__name__}: {exc}")
        log("  If this looks like an auth failure, re-grab the cookies from Chrome.")
        return 12
    log("  Init OK.")

    successes: list[str] = []
    failures: list[str] = []
    skipped: list[str] = []
    auth_failed = False
    # Once-per-session flag: the soft APIError-1100 escalation fires at most
    # once even if multiple batches fail with the same signature. AuthError
    # below already halts the loop on the first occurrence; this flag is
    # only for the non-halting decision-severity path. T-5 / D-070 follow-up.
    fired_apierror_decision = False
    try:
        for batch_num, batch_dir in batches:
            response_path = batch_dir / "RESPONSE.md"
            if response_path.exists() and not args.force_rewrite:
                msg = f"batch_{batch_num:02d}: RESPONSE.md exists; skipping (--force-rewrite to override)"
                log(f"  {msg}")
                skipped.append(msg)
                continue

            # T-8 / D-070 follow-up: auto-backup before --force-rewrite would
            # overwrite real verified data. media/ is fully gitignored, so
            # there's no git-restore path for an accidental destructive
            # rewrite — the .bak file is the only safety net. If the backup
            # itself fails, refuse to proceed with the rewrite for this batch
            # (the operator gets a failure message + the original file stays
            # intact).
            if response_path.exists() and args.force_rewrite:
                bak = batch_dir / f"RESPONSE.md.bak.{int(time.time())}"
                try:
                    shutil.copyfile(response_path, bak)
                    log(
                        f"  batch_{batch_num:02d}: backed up existing "
                        f"RESPONSE.md -> {bak.name}"
                    )
                except Exception as exc:
                    fail_msg = (
                        f"batch_{batch_num:02d}: backup of existing RESPONSE.md "
                        f"failed ({type(exc).__name__}: {exc}); refusing to "
                        "proceed with --force-rewrite for this batch"
                    )
                    log(f"  FAIL {fail_msg}")
                    failures.append(fail_msg)
                    continue

            try:
                ok, msg = await verify_batch(client, batch_num, batch_dir)
            except AuthError as exc:
                log(f"  FAIL batch_{batch_num:02d}: auth error mid-session: {exc}")
                log("  Cookies went stale during the run; stopping + escalating.")
                escalate_stale_cookies(
                    args.work_order_id,
                    reason=f"AuthError mid-session at batch_{batch_num:02d}: {exc}",
                )
                auth_failed = True
                failures.append(
                    f"batch_{batch_num:02d}: AuthError (cookies stale mid-session); "
                    f"halted; subsequent batches not attempted"
                )
                break
            if ok:
                log(f"  OK  {msg}")
                successes.append(msg)
            else:
                log(f"  FAIL {msg}")
                failures.append(msg)
                # Soft auth signal: APIError-1100 looks like it might be
                # stale cookies (observed pattern in the D-069 live test) but
                # also overlaps with upstream weather. Escalate ONCE per
                # session at decision severity; do NOT halt the loop —
                # subsequent batches may still try and fail the same way,
                # and the operator decides whether to refresh cookies or
                # wait it out. AuthError (above) is the high-confidence
                # blocked path that DOES halt.
                if (
                    not fired_apierror_decision
                    and _is_likely_auth_apierror_message(msg)
                ):
                    log(
                        "  (APIError 1100 signature — escalating once at "
                        "decision severity in case cookies are stale)"
                    )
                    escalate_likely_stale_cookies(
                        args.work_order_id, reason=msg
                    )
                    fired_apierror_decision = True
    finally:
        try:
            await client.close()
        except Exception:
            pass

    # [INGEST] phase
    ingest_status = "skipped"
    if not args.skip_ingest:
        if successes or skipped:
            log("  Invoking [INGEST]...")
            rc, output = shell_action("ingest-responses", args.work_order_id)
            log(output.rstrip())
            ingest_status = "ok" if rc == 0 else f"failed (exit {rc})"
        else:
            log("  Nothing successful to ingest; skipping [INGEST].")
            ingest_status = "skipped (no successes)"

    # Summary
    log("=== SUMMARY ===")
    log(f"  successes: {len(successes)}")
    log(f"  skipped:   {len(skipped)}")
    log(f"  failures:  {len(failures)}")
    log(f"  ingest:    {ingest_status}")
    if auth_failed:
        log("  auth status: COOKIES STALE — escalated (severity=blocked) for refresh.")
    elif fired_apierror_decision:
        log(
            "  auth status: APIError 1100 observed — escalated (severity=decision) "
            "in case cookies are stale (could also be upstream weather)."
        )
    if failures:
        log("  failure detail:")
        for m in failures:
            log(f"    - {m}")
    if auth_failed:
        return 12  # cookie-stale exit code — matches init-time AuthError
    return 0 if not failures else 13


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Drive the T-013 V4 verification workflow end-to-end via the "
            "unofficial gemini-webapi wrapper (D-069). Operates on ONE meeting "
            "per session."
        ),
    )
    parser.add_argument("--work-order-id", type=int, required=True)
    parser.add_argument(
        "--skip-build", action="store_true",
        help="Skip [BUILD]; assume review queue already exists.",
    )
    parser.add_argument(
        "--skip-ingest", action="store_true",
        help="Skip [INGEST]; just write RESPONSE.md files.",
    )
    parser.add_argument(
        "--force-rewrite", action="store_true",
        help=(
            "Re-Gemini batches that already have RESPONSE.md (default: skip). "
            "Existing RESPONSE.md is auto-backed-up to RESPONSE.md.bak.<epoch> "
            "before being overwritten."
        ),
    )
    parser.add_argument(
        "--batch", type=int, default=None,
        help="Process only the given batch number (e.g., --batch 2 for batch_02).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Walk the plan and report; don't touch Gemini or disk.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(asyncio.wait_for(main_async(args), timeout=SESSION_TIMEOUT_SECONDS))
    except asyncio.TimeoutError:
        log(f"ERROR: session exceeded {SESSION_TIMEOUT_SECONDS}s ceiling; exiting.")
        return 124


if __name__ == "__main__":
    sys.exit(main())

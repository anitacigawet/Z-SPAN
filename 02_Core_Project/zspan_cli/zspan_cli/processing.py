"""The local pipeline core, callable from anywhere — the
`zspan process` command and the local site's Process button run THIS
one implementation. Extracted from the command handler once the served
site gained its own process affordance: one pipeline, N surfaces.

resolve media → transcribe (local floor / cloud opt-in) → chunk + embed
→ retrieve → synthesize through an approved frontier provider → the
deterministic audit on every output → cache. Progress is a callback so the terminal
prints it and the web panel streams it — same lines, same honesty.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any, Callable, Dict, List, Optional

from zspan_cli import approval as approval_mod
from zspan_cli import contribution as contribution_mod
from zspan_cli import workspace
from zspan_cli.auth import current_auth
from zspan_cli.config import flagship_url, key_fingerprint, media_dir, transcripts_dir
from zspan_cli.flagship import (
    FlagshipError,
    fetch_cli_me,
    register_generation,
    submit_private_contribution,
)
from zspan_cli.providers import PROVIDERS


class PipelineSetupError(Exception):
    """Config-level problems the caller should surface as their own
    message (no key, no provider) — distinct from per-output failures."""


def resolve_synthesis_setup(
    config: Dict[str, Any],
    *,
    model_override: str = "",
    provider_override: str = "",
):
    """(provider, api_key, model) for a run.

    Provider: explicit override (the web Process button's engine choice)
    → config synthesis_provider → first stored key → installed Codex
    CLI. The "codex" engine is keyless (the user's own subscription) and
    is preferred only when the caller has not selected a BYOK provider.

    Model: explicit override → config synthesis_model → the strongest
    APPROVED model the key's own list reaches → an approved static
    default. Economy tiers are rejected at process time, including when
    explicitly configured; civic synthesis never silently opts down.
    """
    from zspan_cli.providers import (
        CODEX_DEFAULT_MODEL,
        CODEX_PROVIDER_ID,
        codex_available,
        codex_unavailable_message,
        is_approved_synthesis_model,
        model_floor_message,
        strongest_reachable,
    )

    api_keys: Dict[str, str] = (config or {}).get("api_keys") or {}
    override_provider = (provider_override or "").strip().lower()
    configured_provider = str(
        (config or {}).get("synthesis_provider") or ""
    ).strip().lower()
    stored_provider = next(iter(api_keys), "")
    selected_provider = (
        override_provider or configured_provider or stored_provider
    )
    provider = selected_provider

    # A saved synthesis_provider (or the legacy first-key fallback) is a
    # deliberate bring-your-own-key selection. Do not silently replace it
    # with Codex merely because the binary happens to be installed.
    if not selected_provider and codex_available(config):
        provider = CODEX_PROVIDER_ID

    if provider == CODEX_PROVIDER_ID:
        if not codex_available(config):
            raise PipelineSetupError(codex_unavailable_message(config))
        model = (model_override or "").strip() \
            or (config or {}).get("synthesis_model_codex") \
            or CODEX_DEFAULT_MODEL
        if not is_approved_synthesis_model(provider, model):
            raise PipelineSetupError(model_floor_message(provider, model))
        return CODEX_PROVIDER_ID, "", model

    if not api_keys:
        raise PipelineSetupError(
            "no synthesis engine is reachable — "
            f"{codex_unavailable_message(config)} No API-key provider is configured."
        )
    if provider not in api_keys:
        provider = next(iter(api_keys))
    if provider not in PROVIDERS:
        raise PipelineSetupError(
            f"synthesis provider '{provider}' is not supported. "
            f"Choose one of: {', '.join(sorted(PROVIDERS))}, or install the Codex CLI."
        )
    reachable = ((config or {}).get("available_models") or {}).get(provider)
    model = (
        (model_override or "").strip()
        or (config or {}).get("synthesis_model")
        or strongest_reachable(provider, reachable)
        or PROVIDERS[provider]["default_model"]
    )
    if reachable and not strongest_reachable(provider, reachable) \
            and not (model_override or "").strip() \
            and not (config or {}).get("synthesis_model"):
        visible = ", ".join(str(m) for m in reachable[:8])
        raise PipelineSetupError(
            model_floor_message(provider)
            + (f" Reachable models reported by the key: {visible}." if visible else "")
        )
    if not is_approved_synthesis_model(provider, model):
        raise PipelineSetupError(model_floor_message(provider, model))
    return provider, api_keys[provider], model


ActivityFn = Callable[[str, str, str, int], None]
"""(kind, label, detail, status) — the HQ activity feed's event shape.
status uses HTTP vocabulary because the skybox colors on it: 200-class
= white star, 400+ = red (a gate refutation or a failed step)."""


def _fmt_ts(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def ensure_transcript(
    row,
    config: Dict[str, Any],
    *,
    progress: Callable[[str], None],
    whisper_model: str = "small.en",
    cloud_transcribe: bool = False,
    keep_media: bool = False,
    activity: Optional[ActivityFn] = None,
):
    """The transcript dict for a meeting — reused from disk when present
    (transcription is the expensive step; delete the file to redo it).
    Returns (transcript, freshly_made). Raises MediaError/TranscribeError
    with user-readable sentences."""
    from zspan_cli import media as media_mod
    from zspan_cli import transcribe as tr

    act = activity or (lambda kind, label, detail="", status=200: None)

    meeting_id = int(row["id"])
    tpath = transcripts_dir() / f"{meeting_id}.json"
    existing = tr.load_transcript(tpath)
    if existing:
        progress(f"transcript already on disk ({len(existing['words'])} words) — reusing")
        act("transcription",
            f"transcript reused from disk ({len(existing['words'])} words)",
            "", 200)
        return existing, False

    video_url = (row["video_url"] or "").strip()
    if not video_url:
        raise media_mod.MediaError(
            "this meeting carries no video source — nothing to transcribe."
        )
    kind = media_mod.classify_video_url(video_url)
    if kind not in (media_mod.KIND_YOUTUBE, media_mod.KIND_DIRECT_MEDIA):
        raise media_mod.MediaError(media_mod.unsupported_reason(kind, video_url))

    progress(f"fetching audio from the {kind.replace('_', ' ')} source...")
    act("media", "fetching the meeting recording", video_url, 200)
    downloaded = media_mod.download_audio(
        video_url, media_dir(), meeting_id, progress=progress
    )
    progress(f"media on disk ({downloaded.bytes / 1_048_576:.0f} MB)")
    act("media",
        f"recording on disk ({downloaded.bytes / 1_048_576:.0f} MB)",
        downloaded.path.name, 200)

    def _on_segment(text: str, start_s: float, end_s: float) -> None:
        act("transcription",
            f"heard {_fmt_ts(start_s)}–{_fmt_ts(end_s)}", text, 200)

    if cloud_transcribe:
        openai_key = ((config or {}).get("api_keys") or {}).get("openai")
        if not openai_key:
            raise tr.TranscribeError(
                "cloud transcription uses OpenAI's whisper-1, but no OpenAI "
                "key is stored. `zspan init --provider openai` adds one, or "
                "use the free local mode."
            )
        transcript = tr.transcribe_whisper1(
            downloaded.path, openai_key, progress=progress,
            on_segment=_on_segment,
        )
    else:
        transcript = tr.transcribe_local(
            downloaded.path, model_size=whisper_model, progress=progress,
            on_segment=_on_segment,
        )

    transcript["source_url"] = video_url
    tr.save_transcript(transcript, tpath)
    if not keep_media:
        downloaded.path.unlink(missing_ok=True)
    return transcript, True


def ensure_watchable_video(
    row,
    *,
    progress: Callable[[str], None],
    activity: Optional[ActivityFn] = None,
) -> None:
    """The embed-disabled rescue: when a
    YouTube channel disallows embedding, fetch a watchable local copy
    (≤720p) so the served site plays natively — the embed wall never
    shows. Never fatal: playback is presentation; the pipeline is the
    product. Offline or fetch-failed just means the external-open
    fallback keeps doing its job."""
    from zspan_cli import media as media_mod
    from zspan_cli.config import videos_dir

    act = activity or (lambda kind, label, detail="", status=200: None)
    video_url = (row["video_url"] or "").strip()
    # Direct media files play natively already; vendor pages have no
    # embed concept. Only YouTube carries the embed-disabled wall.
    if media_mod.classify_video_url(video_url) != media_mod.KIND_YOUTUBE:
        return

    try:
        import requests
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": video_url, "format": "json"},
            timeout=5,
        )
        if resp.status_code == 200:
            return  # embeddable — the normal player needs no local copy
    except Exception:
        return  # offline — skip silently; the next run retries

    progress("this channel disallows YouTube embedding — fetching a "
             "watchable local copy so playback works here...")
    act("media", "fetching a watchable local copy (embed-disabled channel)",
        video_url, 200)
    try:
        got = media_mod.download_video(
            video_url, videos_dir(), int(row["id"]), progress=progress
        )
        progress(f"watchable copy on disk ({got.bytes / 1_048_576:.0f} MB) — "
                 "the local player uses it automatically")
        act("media",
            f"watchable copy on disk ({got.bytes / 1_048_576:.0f} MB)",
            got.path.name, 200)
    except media_mod.MediaError as e:
        progress(f"video rescue skipped: {e}")
        act("media", "video rescue skipped", str(e), 422)


def _register_cached_output(
    conn,
    meeting_row,
    output_row,
    *,
    base_url: str,
    bearer: str,
    account_email: str,
    progress: Callable[[str], None],
) -> bool:
    """Register one durable output row; leave it pending on any API failure."""
    output_type = output_row["output_type"]
    state = output_row["registration_state"]
    bound_account = output_row["registered_account"]
    if state == "pending" and bound_account != account_email:
        shown = bound_account or "an unknown account"
        progress(
            f"{output_type} is pending under {shown}; signed in as "
            f"{account_email}, so it was not reassigned"
        )
        return False

    idempotency_key = output_row["registration_idempotency_key"]
    if state is None:
        idempotency_key = secrets.token_urlsafe(24)
        workspace.prepare_legacy_registration(
            conn,
            int(meeting_row["id"]),
            output_type,
            idempotency_key=idempotency_key,
            registered_account=account_email,
        )
    if not idempotency_key:
        progress(
            f"{output_type} remains UNREGISTERED because its pending row has no "
            "idempotency key"
        )
        return False

    # Pre-contract workspace rows have no stable catalog identity to send to
    # the flagship. Keep their outputs pending (including the legacy-state
    # transition above), but do not make a request that can only return 400.
    if not meeting_row["public_id"]:
        return False

    content = output_row["content"] or ""
    payload = {
        "meeting_public_id": meeting_row["public_id"] or "",
        "output_type": output_type,
        "provider": output_row["provider"] or "",
        "model": output_row["model"] or "",
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "idempotency_key": idempotency_key,
    }
    try:
        registered = register_generation(base_url, payload, bearer)
        ribbon_token = registered.get("ribbon_token")
        generation_public_id = registered.get("generation_public_id")
        if not isinstance(ribbon_token, str) or not isinstance(
            generation_public_id, str
        ):
            raise FlagshipError(
                "the generation registration response was incomplete.", status=200
            )
    except FlagshipError:
        progress(
            f"{output_type}: output cached but UNREGISTERED — the next process of this "
            "meeting retries registration"
        )
        return False

    workspace.update_registration(
        conn,
        int(meeting_row["id"]),
        output_type,
        ribbon_token=ribbon_token,
        generation_public_id=generation_public_id,
        state="registered",
    )
    progress(f"✓ {output_type} registered for public verification")
    return True


def _submit_complete_contribution(
    conn,
    meeting_row,
    transcript: Dict[str, Any],
    *,
    config: Dict[str, Any],
    base_url: str,
    bearer: str,
    attempt_network: bool,
    progress: Callable[[str], None],
) -> bool:
    """Seal exact local artifacts, persist retry state, and submit once."""
    try:
        core = contribution_mod.build_core(
            meeting_row,
            transcript,
            workspace.load_outputs(conn, int(meeting_row["id"])),
        )
        contribution_mod.assert_secrets_absent(
            core, ((config.get("api_keys") or {}).values())
        )
    except contribution_mod.ContributionError as exc:
        progress(f"private contribution is not ready: {exc}")
        return False

    payload_sha256 = contribution_mod.sha256_json(core)
    idempotency_key = workspace.prepare_contribution(
        conn, int(meeting_row["id"]), payload_sha256
    )
    submission = workspace.contribution_submission(conn, int(meeting_row["id"]))
    if submission is not None and submission["state"] == "submitted":
        progress("✓ this exact private contribution was already received")
        return True
    payload = contribution_mod.finish(core, idempotency_key)
    if not attempt_network:
        progress(
            "private contribution queued on this computer — processing remains "
            "incomplete until the endpoint can accept it"
        )
        return False
    try:
        response = submit_private_contribution(base_url, payload, bearer)
    except FlagshipError:
        progress(
            "private contribution queued on this computer — the next process of "
            "this meeting retries the required submission"
        )
        return False
    submission_public_id = response.get("submission_public_id")
    valid_response = (
        isinstance(submission_public_id, str)
        and 3 <= len(submission_public_id) <= 64
        and response.get("payload_sha256") == payload_sha256
        and response.get("status") == "received_unverified"
    )
    if not valid_response:
        progress(
            "private contribution remains queued because the endpoint response "
            "was incomplete"
        )
        return False
    workspace.mark_contribution_submitted(
        conn,
        int(meeting_row["id"]),
        payload_sha256=payload_sha256,
        submission_public_id=submission_public_id,
    )
    progress("✓ transcript and outputs received by Z-SPAN's private intake")
    return True


def run_pipeline(
    meeting_id: int,
    *,
    config: Dict[str, Any],
    progress: Callable[[str], None] = print,
    model_override: str = "",
    provider_override: str = "",
    whisper_model: str = "small.en",
    cloud_transcribe: bool = False,
    keep_media: bool = False,
    force: bool = False,
    yes_to_all: bool = False,
    approval_fn: Optional[Callable[..., approval_mod.ApprovalDecision]] = None,
    activity: Optional[ActivityFn] = None,
) -> Dict[str, Any]:
    """Process one meeting end-to-end into the workspace. Returns
    {"ok", "synthesized", "skipped", "failures", "fully_processed"} —
    plus ``aborted_by_operator=True`` on an operator abort. Per-output
    failures never abort the run (the report-generator isolation precedent);
    setup problems raise.

    `approval_fn` is the presentation seam for per-output approval. It
    receives the same keyword envelope as ``approval.approve_chunk`` and
    returns an ``ApprovalDecision``. The terminal renderer remains the
    default; bypass policy is enforced here before either renderer runs.

    `activity` mirrors `progress` but structured — (kind, label, detail,
    status) events for the HQ skybox feed. Terminal runs pass nothing;
    the local server passes its publisher."""
    from zspan_cli.config import (
        PROCESSING_ACK_TEXT,
        has_processing_ack,
    )

    if not has_processing_ack(config):
        raise PipelineSetupError(PROCESSING_ACK_TEXT)

    auth = current_auth(config)
    if auth is None:
        raise PipelineSetupError(
            "generation requires Google sign-in — run `zspan login` first."
        )
    base_url = flagship_url(config)
    bearer = auth["token"]
    flagship_available = True
    try:
        live_auth = fetch_cli_me(base_url, bearer)
    except FlagshipError as e:
        if e.status == 401:
            raise PipelineSetupError(
                "your sign-in expired — run `zspan login` again."
            ) from e
        flagship_available = False
        live_auth = {"account": {"email": auth.get("email") or ""}}
        progress(
            "the Z-SPAN endpoint is temporarily unreachable — local work can "
            "continue, but the required private contribution will remain queued"
        )
    live_account = live_auth.get("account")
    account_email = (
        live_account.get("email", "") if isinstance(live_account, dict) else ""
    )
    if not isinstance(account_email, str) or not account_email:
        raise PipelineSetupError(
            "your saved sign-in has no account identity — run `zspan login` again."
        )

    from zspan_cli import gate as gate_mod
    from zspan_cli import grounding
    from zspan_cli import media as media_mod
    from zspan_cli import pipeline as pl
    from zspan_cli import synthesize as syn
    from zspan_cli import transcribe as tr

    act = activity or (lambda kind, label, detail="", status=200: None)

    conn = workspace.connect()
    try:
        row = workspace.get_meeting(conn, meeting_id)
        if row is None:
            raise PipelineSetupError(
                f"meeting {meeting_id} isn't in your workspace — "
                "`zspan pull` fetches your city's catalog first."
            )

        progress(f"Processing: {row['title'] or '(untitled)'}")
        progress(f"{row['city']} · {row['meeting_date']} · meeting {meeting_id}")
        if not row["public_id"]:
            progress(
                "this meeting has no catalog id in your workspace (pulled before "
                "the catalog contract) — registration will engage once the "
                "workspace row carries one."
            )
        act("pipeline",
            f"processing {row['city']} · {row['meeting_date']}",
            row["title"] or "(untitled)", 200)

        if flagship_available:
            for pending_row in workspace.rows_needing_registration(conn, meeting_id):
                _register_cached_output(
                    conn,
                    row,
                    pending_row,
                    base_url=base_url,
                    bearer=bearer,
                    account_email=account_email,
                    progress=progress,
                )

        provider, api_key, model = resolve_synthesis_setup(
            config,
            model_override=model_override,
            provider_override=provider_override,
        )
        fp = key_fingerprint(api_key)

        transcript, fresh_transcript = ensure_transcript(
            row, config,
            progress=progress,
            whisper_model=whisper_model,
            cloud_transcribe=cloud_transcribe,
            keep_media=keep_media,
            activity=activity,
        )
        words = transcript["words"]
        workspace.set_transcript_path(
            conn, meeting_id, str(transcripts_dir() / f"{meeting_id}.json")
        )
        minutes = (transcript.get("duration_seconds") or 0.0) / 60.0
        progress(f"transcript: {len(words)} words · {minutes:.0f} minutes of audio")

        if fresh_transcript or workspace.chunk_count(conn, meeting_id) == 0:
            counter, exact = pl.load_token_counter()
            if not exact:
                progress("note: exact tokenizer unavailable — chunking on a "
                         "words-per-token estimate")
            chunks = pl.chunk_transcript(words, token_counter=counter, exact=exact)
            vectors = pl.embed_texts([c.text for c in chunks], progress=progress)
            workspace.replace_chunks(conn, meeting_id, chunks, vectors)
            progress(f"indexed {len(chunks)} chunks into the workspace")
            act("index",
                f"indexed {len(chunks)} chunks into the workspace",
                f"bge-small embeddings, {len(words)} words chunked", 200)
        else:
            progress(f"{workspace.chunk_count(conn, meeting_id)} chunks already "
                     "indexed — reusing")

        prompts_dir = syn.resolve_prompts_dir(config)
        chunk_rows, matrix = workspace.load_chunk_matrix(conn, meeting_id)
        if not chunk_rows:
            raise PipelineSetupError(
                "no chunks in the workspace for this meeting — the transcript "
                "may be empty. Delete its file to redo it."
            )
        tindex = grounding.build_transcript_index(words)
        already = workspace.existing_outputs(conn, meeting_id)

        synthesized: List[str] = []
        skipped: List[str] = []
        failures: List[str] = []
        RENDERED_TOTAL = len(syn.RENDERED_OUTPUT_TYPES)
        aborted_by_operator = False
        approval_required = approval_mod.should_prompt(yes_to_all)
        approve = approval_fn or approval_mod.approve_chunk
        if approval_required and approval_fn is None:
            approval_mod.print_approval_intro(progress)
        for chunk_i, output_type in enumerate(syn.RENDERED_OUTPUT_TYPES, start=1):
            if output_type in already and not force:
                progress(f"{output_type} already synthesized "
                         f"(gate: {already[output_type] or '?'}) — skipping")
                skipped.append(output_type)
                continue

            query_vec = pl.embed_query(syn.OUTPUT_QUERIES[output_type])
            top = pl.top_k_cosine(matrix, query_vec, k=12)
            retrieved = [
                pl.RetrievedChunk(
                    chunk_index=chunk_rows[i]["chunk_index"],
                    text=chunk_rows[i]["text"],
                    start_seconds=chunk_rows[i]["start_seconds"],
                    end_seconds=chunk_rows[i]["end_seconds"],
                    score=score,
                )
                for i, score in top
            ]
            act("retrieval",
                f"searching the record — {output_type.replace('_', ' ')}",
                syn.OUTPUT_QUERIES[output_type], 200)
            try:
                canonical = syn.load_canonical_prompt(output_type, prompts_dir)
                prompt = syn.build_synthesis_prompt(
                    output_type=output_type,
                    canonical_prompt=canonical,
                    meeting_id=meeting_id,
                    chunks=retrieved,
                )
                if approval_required:
                    decision = approve(
                        output_type=output_type,
                        chunk_index=chunk_i,
                        chunk_total=RENDERED_TOTAL,
                        retrieval_query=syn.OUTPUT_QUERIES[output_type],
                        retrieved_chunks=retrieved,
                        canonical_prompt=canonical,
                        full_envelope=prompt,
                        provider=provider,
                        model=model,
                        key_fingerprint_str=fp,
                        yes_to_all=False,
                    )
                else:
                    decision = approval_mod.ApprovalDecision.PROCEED
                if decision is approval_mod.ApprovalDecision.SKIP:
                    progress(f"skipped {output_type} at user's request — no key spent")
                    act(
                        "gate",
                        f"user skipped {output_type.replace('_', ' ')}",
                        "operator declined to send this chunk to the provider",
                        200,
                    )
                    skipped.append(output_type)
                    continue
                if decision is approval_mod.ApprovalDecision.ABORT_ALL:
                    progress(
                        f"aborted synthesis at user's request "
                        f"({chunk_i - 1} of {RENDERED_TOTAL} completed before abort)"
                    )
                    act(
                        "gate",
                        "operator aborted synthesis",
                        f"aborted before chunk {chunk_i} ({output_type})",
                        200,
                    )
                    aborted_by_operator = True
                    break
                progress(f"synthesizing {output_type} via {provider} ({model})...")
                act("synthesis",
                    f"{output_type.replace('_', ' ')} via {provider} ({model})",
                    "chunks " + ", ".join(
                        f"{c.chunk_index} [{_fmt_ts(c.start_seconds)}]"
                        for c in retrieved[:8]
                    ) + (" …" if len(retrieved) > 8 else ""), 200)
                content = syn.synthesize(provider, api_key, model, prompt)

                final, report = gate_mod.gate_and_retry(
                    output_type, content, tindex, progress=progress,
                )
                if output_type == "community_calls_to_action":
                    final = gate_mod.normalize_ccta(final)
                idempotency_key = secrets.token_urlsafe(24)
                workspace.save_output(
                    conn, meeting_id, output_type,
                    content=final, provider=provider, model=model,
                    gate_status=report.status, gate_log=report.to_json(),
                    registration_idempotency_key=idempotency_key,
                    registered_account=account_email,
                )
                progress(f"✓ {output_type} cached (gate: {report.status}"
                         ")")
                saved_row = conn.execute(
                    "SELECT * FROM outputs WHERE meeting_id = ? AND output_type = ?",
                    (meeting_id, output_type),
                ).fetchone()
                if flagship_available:
                    _register_cached_output(
                        conn,
                        row,
                        saved_row,
                        base_url=base_url,
                        bearer=bearer,
                        account_email=account_email,
                        progress=progress,
                    )
                act("gate",
                    f"gate audit on {output_type.replace('_', ' ')}: {report.status}",
                    "\n".join(report.determinate_failures)
                    or f"{report.detail}",
                    200 if report.status in (
                        "observed_clean", "observed_findings"
                    ) else 422)
                synthesized.append(output_type)
            except syn.SynthesisError as e:
                failures.append(f"{output_type}: {e}")
                progress(f"✗ {output_type} failed: {e}")
                act("synthesis",
                    f"{output_type.replace('_', ' ')} failed", str(e), 502)

        if aborted_by_operator:
            progress(
                f"synthesis aborted by operator — "
                f"{len(synthesized)}/{RENDERED_TOTAL} outputs cached before abort"
            )
            return {
                "ok": False,
                "aborted_by_operator": True,
                "synthesized": synthesized,
                "skipped": skipped,
                "failures": failures,
                "fully_processed": False,
            }

        # The embed-disabled rescue rides AFTER synthesis — the broadcast
        # text is the product and lands first; the watchable copy trails
        # it and never fails the run.
        ensure_watchable_video(row, progress=progress, activity=activity)

        done = workspace.existing_outputs(conn, meeting_id)
        outputs_complete = all(t in done for t in syn.RENDERED_OUTPUT_TYPES)
        submitted = False
        if outputs_complete:
            submitted = _submit_complete_contribution(
                conn,
                row,
                transcript,
                config=config,
                base_url=base_url,
                bearer=bearer,
                attempt_network=flagship_available,
                progress=progress,
            )
        fully = outputs_complete and submitted
        if fully:
            workspace.mark_processed(conn, meeting_id)
            progress(f"Meeting {meeting_id} fully processed — "
                     f"{len(done)}/{len(syn.RENDERED_OUTPUT_TYPES)} outputs cached "
                     "and private contribution received.")
            act("pipeline",
                f"{row['city']} · {row['meeting_date']} fully processed",
                f"{len(done)}/{len(syn.RENDERED_OUTPUT_TYPES)} outputs cached; "
                "private contribution received", 200)
        elif outputs_complete:
            progress(
                "Pending: every output is cached, but processing is not complete "
                "until the private contribution is received."
            )
            act(
                "pipeline",
                "private contribution pending",
                "all outputs remain cached; process again to retry submission",
                422,
            )
        else:
            progress(f"Partial: {len(done)}/{len(syn.RENDERED_OUTPUT_TYPES)} outputs "
                     "landed — process again to retry the missing ones.")
            act("pipeline",
                f"partial — {len(done)}/{len(syn.RENDERED_OUTPUT_TYPES)} outputs landed",
                "process again to retry the missing ones", 422)
        return {
            "ok": not failures and fully,
            "synthesized": synthesized,
            "skipped": skipped,
            "failures": failures,
            "fully_processed": fully,
            "contribution_state": "submitted" if submitted else "pending",
        }
    finally:
        conn.close()


# Re-exported for callers that need the error types without importing
# the heavy modules directly.
def pipeline_error_types():
    from zspan_cli import media as media_mod
    from zspan_cli import transcribe as tr
    return (PipelineSetupError, media_mod.MediaError, tr.TranscribeError)

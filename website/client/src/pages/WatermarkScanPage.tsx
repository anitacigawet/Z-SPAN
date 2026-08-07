/**
 *— citizen-facing /scan front door (cold-stranger version of the
 * AR ribbon verifier). Same getUserMedia + frame-capture mechanics as the
 * pre-rename WatermarkCameraPage; the differences are framing and copy.
 *
 * Audience: someone who saw a Z-SPAN ribbon (on a screen, on a sticker
 * stuck to a streetlight, on a flyer) and typed `zspan.org/scan` from the
 * ribbon's microtext. They may have zero prior context about the project.
 * Copy assumes nothing.
 *
 * Both paths to verification live here: AR camera primary, screenshot
 * upload secondary. A small "Curious how this works? Learn more →"
 * link below the action area routes to /audit for the architecture story.
 *
 * Requirements satisfied at runtime:
 * - HTTPS (iOS Safari refuses getUserMedia on plain HTTP) — handled at
 * the prod edge by the Cloudflare cert + the lab.zspan.org tunnel.
 * - User gesture for first camera permission — handled by the Start
 * button click.
 *
 * Loop shape: ~1 fps frame capture via offscreen canvas → JPEG blob →
 * POST /api/decode-ribbon-image. A lock prevents overlapping requests so
 * a slow decode never queues up frames. First confident result stops the
 * loop + tears down the stream.
 */
import { useEffect, useRef, useState } from "react";

type Verdict = {
 token: string;
 exists: boolean;
 authenticated?: boolean;
 legacy?: boolean;
 meeting_id?: number;
 output_type?: string;
 meeting_title?: string;
 city_name?: string;
 prompt_version?: string;
 generated_at?: string;
 note?: string;
};

type DecodeResult = {
 token: string | null;
 bbox?: number[] | null;
 stats?: any;
 blocks?: unknown[];
 verdict?: Verdict;
 error?: string;
 debug_session_id?: string;
 debug_dir?: string;
};

// Per-frame record retained for the batch-debug results panel. Carries
// the local-thumbnail dataURL (~120px wide JPEG) alongside the server's
// decoder response so the operator can scroll all 15 attempts on the
// phone + the maintainer can correlate against the full-res JPEG saved
// server-side under /tmp/zspan_scan_debug/<session>/.
type BatchFrame = {
 seq: number;
 preview: string;
 result: DecodeResult | { error: string };
 elapsedMs: number;
};

type Phase =
 | "idle"
 | "requesting"
 | "scanning"
 | "verdict"
 | "error"
 | "debug-capturing"
 | "debug-results";

const DEBUG_BATCH_COUNT = 15;

// Brand palette mirrors watermark_ribbon_decoder.py BRAND_PALETTE +
// WatermarkRibbon.tsx PALETTE. 2-bit code = index in this array.
const PALETTE_COLORS = ["#1A3A7C", "#EF4444", "#22C55E", "#F5A524"] as const;
const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

// Vote accumulator parameters. A block is "resolved" when one palette
// classification has >= MIN_VOTES_TO_RESOLVE votes AND leads the
// second-place by at least MIN_LEAD. Tight thresholds favor correctness
// (we'd rather have the operator hold the phone a beat longer than
// produce a wrong consensus token).
const MIN_VOTES_TO_RESOLVE = 3;
const MIN_LEAD = 1;

// After this many consecutive frames without a ribbon detected, treat
// the lock as lost and reset the vote accumulator + return to the
// no-lock scanning UI. ~5 frames at 400ms cadence = 2 seconds — long
// enough to absorb a brief lens-bump, short enough that the operator
// notices to re-aim.
const MAX_LOCK_LOSS_FRAMES = 5;

type BlockVotes = { [paletteIdx: number]: number };

// Per-block decoder payload as actually surfaced by /api/decode-
// ribbon-image's blocks array (decoder hue-classification path).
type DecoderBlock = {
 sampled_rgb?: number[];
 classified?: number;
 hue_distance_deg?: number;
 saturation?: number;
 lightness?: number;
 fail_reason?: string | null;
};

function tokenFromClassifications(classes: number[]): string {
 // 20 × 2-bit values → 40 bits → 8 base32 chars.
 const bits: number[] = [];
 for (const c of classes) {
 bits.push((c >> 1) & 1);
 bits.push(c & 1);
 }
 const chars: string[] = [];
 for (let i = 0; i < 40; i += 5) {
 let v = 0;
 for (let b = 0; b < 5; b++) v = (v << 1) | bits[i + b];
 chars.push(BASE32_ALPHABET[v]);
 }
 return chars.join("");
}

function resolvedFromVotes(votes: BlockVotes): number | null {
 const entries = Object.entries(votes).map(([k, v]) => [Number(k), v]) as [number, number][];
 if (entries.length === 0) return null;
 entries.sort((a, b) => b[1] - a[1]);
 const [topClass, topCount] = entries[0];
 const secondCount = entries[1]?.[1] ?? 0;
 if (topCount >= MIN_VOTES_TO_RESOLVE && topCount - secondCount >= MIN_LEAD) {
 return topClass;
 }
 return null;
}

export default function WatermarkScanPage() {
 const videoRef = useRef<HTMLVideoElement>(null);
 const canvasRef = useRef<HTMLCanvasElement>(null);
 const streamRef = useRef<MediaStream | null>(null);
 const inFlightRef = useRef(false);
 const captureTimerRef = useRef<number | null>(null);
 const stoppedRef = useRef(false);

 const [phase, setPhase] = useState<Phase>("idle");
 const [errorMsg, setErrorMsg] = useState<string | null>(null);
 const [result, setResult] = useState<DecodeResult | null>(null);
 const [attempts, setAttempts] = useState(0);
 // Last-attempt summary surfaced in the scanning overlay so the user
 // can see whether frames are reaching the decoder + what the decoder
 // is reporting. Without this, "Scanning · N" can run forever with no
 // signal that anything is actually being analyzed.
 const [lastStatus, setLastStatus] = useState<string>("warming up…");
 const [lastFramePreview, setLastFramePreview] = useState<string | null>(null);

 // Batch-debug mode: capture DEBUG_BATCH_COUNT frames, send each with
 // _debug=1 so the server persists the raw JPEG + decoder response to
 // /tmp/zspan_scan_debug/<session>/. Operator sees all results inline;
 // maintainer reads the server-side dir for the full-resolution JPEGs.
 const [debugFrames, setDebugFrames] = useState<BatchFrame[]>([]);
 const [debugSessionId, setDebugSessionId] = useState<string | null>(null);
 const [debugProgress, setDebugProgress] = useState<number>(0);

 // Live-scan vote accumulator. Per-block-position vote counts across
 // frames since the current lock was acquired. Ref + state pattern: ref
 // for synchronous reads inside the capture loop, state for triggering
 // re-renders of the virtual-ribbon UI as votes accumulate.
 const blockVotesRef = useRef<BlockVotes[]>(
 Array.from({ length: 20 }, () => ({} as BlockVotes)),
 );
 const [resolvedBlocks, setResolvedBlocks] = useState<(number | null)[]>(
 () => Array(20).fill(null),
 );
 const lockLossCountRef = useRef(0);
 const [lockState, setLockState] = useState<"searching" | "locked" | "verifying">("searching");
 const [lastBbox, setLastBbox] = useState<number[] | null>(null);
 const [lastFrameDims, setLastFrameDims] = useState<{ w: number; h: number } | null>(null);
 const [consensusToken, setConsensusToken] = useState<string | null>(null);
 const [resolvedCount, setResolvedCount] = useState(0);

 // Fuzz-animation tick. Increments at ~10fps while scanning so the
 // virtual ribbon's unresolved blocks cycle through palette colors —
 // the visible "key-lock-decryption" effect James specced.
 const [fuzzTick, setFuzzTick] = useState(0);
 useEffect(() => {
 if (phase !== "scanning") return;
 const id = window.setInterval(() => setFuzzTick((t) => t + 1), 100);
 return () => window.clearInterval(id);
 }, [phase]);

 const resetVoteAccumulator = () => {
 blockVotesRef.current = Array.from({ length: 20 }, () => ({} as BlockVotes));
 setResolvedBlocks(Array(20).fill(null));
 setResolvedCount(0);
 setLastBbox(null);
 setLockState("searching");
 lockLossCountRef.current = 0;
 setConsensusToken(null);
 };

 const cleanup = () => {
 stoppedRef.current = true;
 if (captureTimerRef.current !== null) {
 window.clearInterval(captureTimerRef.current);
 captureTimerRef.current = null;
 }
 if (streamRef.current) {
 streamRef.current.getTracks().forEach((t) => t.stop());
 streamRef.current = null;
 }
 if (videoRef.current) {
 videoRef.current.srcObject = null;
 }
 };

 useEffect(() => cleanup, []);

 // The <video> element only mounts when phase becomes "scanning" /
 // "verdict" / "debug-capturing", so attaching srcObject inside the
 // start handlers races React's render cycle and hits a null ref.
 // Wire the stream once the video element is present. EVERY phase
 // that renders a <video> with videoRef must be listed here — a
 // missing phase reproduces the original black-screen bug.
 useEffect(() => {
 if (
 (phase === "scanning" ||
 phase === "verdict" ||
 phase === "debug-capturing") &&
 videoRef.current &&
 streamRef.current &&
 videoRef.current.srcObject !== streamRef.current
 ) {
 videoRef.current.srcObject = streamRef.current;
 videoRef.current.play().catch(() => undefined);
 }
 }, [phase]);

 const startScan = async () => {
 setErrorMsg(null);
 setResult(null);
 setAttempts(0);
 setLastStatus("warming up…");
 setLastFramePreview(null);
 resetVoteAccumulator();
 stoppedRef.current = false;

 if (!navigator.mediaDevices?.getUserMedia) {
 setErrorMsg(
 "This browser can't open the camera. Try Safari on iOS or Chrome on Android, or upload a screenshot instead.",
 );
 setPhase("error");
 return;
 }

 setPhase("requesting");
 try {
 // Request the highest-resolution stream the device will give us.
 // The OpenCV ribbon-finder relies on geometric contour detection;
 // more pixels per ribbon = better edge fidelity = higher hit rate.
 // The phone scales naturally down to its viewport for display.
 const stream = await navigator.mediaDevices.getUserMedia({
 video: {
 facingMode: { ideal: "environment" },
 width: { ideal: 1920 },
 height: { ideal: 1080 },
 },
 audio: false,
 });
 streamRef.current = stream;
 setPhase("scanning");
 // Crank cadence to ~2.5 fps (operator-authorized 2026-06-30 after
 // 1 fps wasn't finding the ribbon — more shots-on-goal increases
 // the chance a non-blurry well-framed frame lands). The
 // inFlightRef lock prevents request pile-up if the decoder takes
 // longer than the interval.
 captureTimerRef.current = window.setInterval(captureAndDecode, 400);
 } catch (err: any) {
 const denied = err?.name === "NotAllowedError" || err?.name === "PermissionDeniedError";
 setErrorMsg(
 denied
 ? "Camera access denied. Re-enable it in the browser address bar, or upload a screenshot instead."
 : `Couldn't start the camera: ${err?.message || String(err)}`,
 );
 setPhase("error");
 }
 };

 const captureAndDecode = async () => {
 if (inFlightRef.current || stoppedRef.current) return;
 const video = videoRef.current;
 const canvas = canvasRef.current;
 if (!video || !canvas) return;
 if (video.videoWidth === 0 || video.videoHeight === 0) return;

 const maxDim = 1920;
 const scale = Math.min(1, maxDim / Math.max(video.videoWidth, video.videoHeight));
 const w = Math.round(video.videoWidth * scale);
 const h = Math.round(video.videoHeight * scale);
 canvas.width = w;
 canvas.height = h;
 const ctx = canvas.getContext("2d");
 if (!ctx) return;
 ctx.drawImage(video, 0, 0, w, h);

 inFlightRef.current = true;
 setAttempts((c) => c + 1);
 try {
 const blob: Blob | null = await new Promise((resolve) =>
 canvas.toBlob(resolve, "image/jpeg", 0.9),
 );
 if (!blob) return;

 const form = new FormData();
 form.append("image", blob, "frame.jpg");
 const resp = await fetch("/api/decode-ribbon-image", {
 method: "POST",
 body: form,
 });
 const data: DecodeResult = await resp.json();
 if (stoppedRef.current) return;

 const detected = data.stats?.detected === true;
 const bbox = (data.bbox as number[] | undefined) ?? null;
 const blocks = (data.blocks as DecoderBlock[] | undefined) ?? [];

 if (!detected || !bbox || blocks.length !== 20) {
 // No ribbon (or partial) in this frame — increment lock-loss
 // counter. If we exceed MAX_LOCK_LOSS_FRAMES while having held a
 // lock, reset the accumulator + go back to no-lock UI.
 lockLossCountRef.current += 1;
 if (lockLossCountRef.current >= MAX_LOCK_LOSS_FRAMES) {
 resetVoteAccumulator();
 setLastStatus("looking for a ribbon…");
 } else {
 setLastStatus(
 lockState === "locked"
 ? `focus lost · retrying (${lockLossCountRef.current}/${MAX_LOCK_LOSS_FRAMES})`
 : "looking for a ribbon…",
 );
 }
 return;
 }

 // Ribbon detected this frame. Reset loss counter + record bbox +
 // accumulate votes.
 lockLossCountRef.current = 0;
 setLastBbox(bbox);
 setLastFrameDims({ w, h });
 setLockState("locked");

 // Single-frame fast path: if the decoder produced a clean token
 // AND it resolved against the audit log, land the verdict
 // immediately. No reason to wait for voting cycles when the
 // first good frame already nailed it. The vote accumulator
 // still fires as a fallback for frames that produced blocks
 // but no clean token (1-2 blocks past the gate).
 if (data.token && data.verdict?.exists) {
 setResult(data);
 setLastStatus(`✅ ${data.token}`);
 setPhase("verdict");
 cleanup();
 return;
 }

 const newVotes = blockVotesRef.current.map((v) => ({ ...v }));
 for (let i = 0; i < 20; i++) {
 const block = blocks[i];
 if (!block || block.fail_reason || typeof block.classified !== "number") {
 continue;
 }
 const cls = block.classified;
 newVotes[i][cls] = (newVotes[i][cls] ?? 0) + 1;
 }
 blockVotesRef.current = newVotes;

 const resolved = newVotes.map(resolvedFromVotes);
 setResolvedBlocks(resolved);
 const resolvedN = resolved.filter((r) => r !== null).length;
 setResolvedCount(resolvedN);
 setLastStatus(`scanning · ${resolvedN}/20 blocks locked`);

 // All 20 blocks resolved → assemble token + submit lookup.
 if (resolvedN === 20) {
 const token = tokenFromClassifications(resolved as number[]);
 setConsensusToken(token);
 setLockState("verifying");
 setLastStatus(`verifying token ${token}…`);
 try {
 const lookupResp = await fetch(
 `/api/watermark-lookup/${encodeURIComponent(token)}`,
 );
 const verdict: Verdict = await lookupResp.json();
 if (stoppedRef.current) return;
 setResult({
 token,
 verdict,
 bbox,
 stats: data.stats,
 });
 setPhase("verdict");
 cleanup();
 } catch (lookupErr: any) {
 setLastStatus(`lookup failed: ${lookupErr?.message || "retry"}`);
 // Don't reset votes — give the next frame a chance to retry
 // the lookup. The lock is still good, the network just blipped.
 }
 }
 } catch (e: any) {
 // Network blip during scan; keep trying.
 setLastStatus(`network blip: ${e?.message || "retrying"}`);
 } finally {
 inFlightRef.current = false;
 }
 };

 // Batch-debug capture — exactly DEBUG_BATCH_COUNT frames in a row,
 // each sent with _debug=1 so the server stashes the full-res JPEG +
 // response under /tmp/zspan_scan_debug/<session>/. After all 15 fire
 // we show a results panel with thumbnails + decoder status per
 // attempt; the maintainer can read the server dir for full fidelity.
 const startDebugCapture = async () => {
 setErrorMsg(null);
 setResult(null);
 setAttempts(0);
 setLastStatus("warming up…");
 setLastFramePreview(null);
 setDebugFrames([]);
 setDebugProgress(0);
 stoppedRef.current = false;

 if (!navigator.mediaDevices?.getUserMedia) {
 setErrorMsg(
 "This browser can't open the camera. Try Safari on iOS or Chrome on Android, or upload a screenshot instead.",
 );
 setPhase("error");
 return;
 }

 const sessionId = `dbg-${Date.now().toString(36)}`;
 setDebugSessionId(sessionId);
 setPhase("requesting");

 try {
 const stream = await navigator.mediaDevices.getUserMedia({
 video: {
 facingMode: { ideal: "environment" },
 width: { ideal: 1920 },
 height: { ideal: 1080 },
 },
 audio: false,
 });
 streamRef.current = stream;
 setPhase("debug-capturing");
 } catch (err: any) {
 const denied = err?.name === "NotAllowedError" || err?.name === "PermissionDeniedError";
 setErrorMsg(
 denied
 ? "Camera access denied. Re-enable it in the browser address bar, or upload a screenshot instead."
 : `Couldn't start the camera: ${err?.message || String(err)}`,
 );
 setPhase("error");
 return;
 }

 // Give the video element a moment to mount + the camera to focus
 // before we start grabbing frames. iOS Safari especially needs a
 // beat for first-frame to arrive.
 await new Promise((r) => setTimeout(r, 800));

 const collected: BatchFrame[] = [];
 for (let i = 0; i < DEBUG_BATCH_COUNT; i++) {
 if (stoppedRef.current) break;
 const frame = await captureOneDebugFrame(sessionId, i);
 if (!frame) {
 // Camera not ready yet — wait + retry the same seq.
 await new Promise((r) => setTimeout(r, 250));
 i--;
 continue;
 }
 collected.push(frame);
 setDebugFrames([...collected]);
 setDebugProgress(collected.length);
 // Brief pause between frames so the user can pan slightly +
 // give the camera time to refocus.
 await new Promise((r) => setTimeout(r, 350));
 }

 cleanup();
 setPhase("debug-results");
 };

 const captureOneDebugFrame = async (
 sessionId: string,
 seq: number,
 ): Promise<BatchFrame | null> => {
 const t0 = performance.now();
 const video = videoRef.current;
 const canvas = canvasRef.current;
 if (!video || !canvas) return null;
 if (video.videoWidth === 0 || video.videoHeight === 0) return null;

 const maxDim = 1920;
 const scale = Math.min(1, maxDim / Math.max(video.videoWidth, video.videoHeight));
 const w = Math.round(video.videoWidth * scale);
 const h = Math.round(video.videoHeight * scale);
 canvas.width = w;
 canvas.height = h;
 const ctx = canvas.getContext("2d");
 if (!ctx) return null;
 ctx.drawImage(video, 0, 0, w, h);

 const blob: Blob | null = await new Promise((resolve) =>
 canvas.toBlob(resolve, "image/jpeg", 0.9),
 );
 if (!blob) return null;

 // Thumbnail for inline rendering on the phone.
 const previewCanvas = document.createElement("canvas");
 const pw = 240;
 const ph = Math.round(pw * (h / w));
 previewCanvas.width = pw;
 previewCanvas.height = ph;
 previewCanvas.getContext("2d")?.drawImage(canvas, 0, 0, pw, ph);
 const previewUrl = previewCanvas.toDataURL("image/jpeg", 0.7);

 const form = new FormData();
 form.append("image", blob, `frame.jpg`);
 form.append("_debug", "1");
 form.append("_session_id", sessionId);
 form.append("_seq", seq.toString());

 let result: DecodeResult | { error: string };
 try {
 const resp = await fetch("/api/decode-ribbon-image", {
 method: "POST",
 body: form,
 });
 result = (await resp.json()) as DecodeResult;
 } catch (e: any) {
 result = { error: e?.message || String(e) };
 }

 return {
 seq,
 preview: previewUrl,
 result,
 elapsedMs: Math.round(performance.now() - t0),
 };
 };

 const scanAgain = () => {
 setResult(null);
 setPhase("idle");
 void startScan();
 };

 const goToVerifyPage = () => {
 cleanup();
 window.location.search = "?view=watermark-verify";
 };

 const goToAuditPage = () => {
 cleanup();
 window.location.search = "?view=audit";
 };

 const renderVerdictCard = (v: Verdict | undefined | null) => {
 if (!v) return null;
 const authenticated = v.exists && v.authenticated === true;
 const legacy = v.legacy === true;
 const borderColor = legacy
 ? "rgba(245,165,36,0.5)"
 : authenticated
 ? "rgba(34,197,94,0.4)"
 : "rgba(239,68,68,0.4)";
 const background = legacy
 ? "rgba(245,165,36,0.08)"
 : authenticated
 ? "rgba(34,197,94,0.08)"
 : "rgba(239,68,68,0.08)";
 return (
 <div
 className="border rounded-lg p-4"
 style={{ borderColor, background }}
 >
 <div
 className="text-[16px] font-semibold mb-2"
 style={{
 color: legacy
 ? "#F5A524"
 : authenticated
 ? "var(--success-green)"
 : "var(--alert-red)",
 }}
 >
 {legacy
 ? "Legacy identifier — not authentication"
 : authenticated
 ? "Registry match · canonical record"
 : "Token not registered"}
 </div>
 {legacy ? (
 <div className="text-[13px] text-gray-300">
 This is a publicly reproducible legacy identifier — not authentication.
 The screenshot content itself is not authenticated.
 </div>
 ) : authenticated ? (
 <div className="text-[13px] text-gray-300">
 <div className="mb-2">
 This token maps to Z-SPAN&apos;s canonical record. The screenshot
 content itself is not authenticated.
 </div>
 <span className="text-white font-medium">{v.city_name}</span>
 {v.meeting_title && <span className="text-gray-400"> · {v.meeting_title}</span>}
 </div>
 ) : (
 <div className="text-[13px] text-gray-300">
 {v.note || "This token isn't in our audit log."}
 </div>
 )}
 </div>
 );
 };

 return (
 <div className="min-h-screen bg-[#0A0A0A] text-white">
 <div className="max-w-md mx-auto px-5 py-6">
 <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-4">
 Z-SPAN · Scan a ribbon
 </div>

 {phase === "idle" && (
 <>
 <h1 className="text-[18px] font-semibold leading-snug mb-3">
 You found a Z-SPAN ribbon.
 </h1>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-5">
 Point your camera at it to check what it's from — or upload a
 screenshot if you saved one earlier. Either way you'll see
 the actual council member, meeting, and timestamp it came
 from.
 </p>
 <p className="text-[12px] text-gray-500 leading-relaxed mb-5">
 <span className="text-[#F5A524]">Tip:</span> get close enough
 that the ribbon fills most of the dashed amber box. Steady
 hands help — focus matters more than aim.
 </p>
 <button
 onClick={() => void startScan()}
 className="w-full bg-white text-black font-semibold py-3 rounded-lg text-[14px] tracking-wide"
 >
 Open camera
 </button>
 <button
 onClick={goToVerifyPage}
 className="w-full mt-2 border border-white/20 text-white font-medium py-3 rounded-lg text-[14px] tracking-wide"
 >
 Upload a screenshot instead
 </button>
 <p className="text-[11px] text-gray-500 mt-4 leading-relaxed">
 The first time, your browser will ask for camera permission.
 Nothing is recorded — frames are sent to the audit log only
 while you're scanning.
 </p>
 <div className="mt-8 pt-5 border-t border-white/5">
 <button
 onClick={goToAuditPage}
 className="text-[12px] text-gray-400 hover:text-white underline-offset-4 hover:underline"
 >
 Curious how this works? Learn more →
 </button>
 </div>
 {/* Maintainer-only debug capture — 15 frames with full
 server-side persistence for decoder inspection. Gated to dev
 builds so it never ships to the citizen-facing production
 /scan page (2026-07-15 visitor-QA: it was visible to the
 public). */}
 {import.meta.env.DEV && (
 <div className="mt-5">
 <button
 onClick={() => void startDebugCapture()}
 className="w-full border border-dashed border-[#F5A524]/50 text-[#F5A524] font-mono text-[11px] py-2.5 rounded-lg uppercase tracking-[0.12em]"
 >
 ⏺ Debug capture · {DEBUG_BATCH_COUNT} frames
 </button>
 <p className="text-[10px] text-gray-500 mt-2 leading-relaxed">
 Captures {DEBUG_BATCH_COUNT} frames with full server-side
 logging. Use when the live scan isn't finding ribbons.
 </p>
 </div>
 )}
 </>
 )}

 {phase === "debug-capturing" && (
 <div className="relative rounded-xl overflow-hidden border border-[#F5A524]/40 bg-black mb-4">
 <video
 ref={videoRef}
 playsInline
 muted
 className="block w-full h-auto"
 style={{ maxHeight: "60vh", objectFit: "cover" }}
 />
 <canvas ref={canvasRef} className="hidden" />
 <div
 aria-hidden
 className="absolute inset-0 pointer-events-none flex items-center justify-center"
 >
 <div
 style={{
 width: "80%",
 height: "16%",
 border: "1px dashed rgba(245,165,36,0.7)",
 borderRadius: 8,
 }}
 />
 </div>
 <div
 className="absolute top-2 left-2 text-[11px] font-mono uppercase tracking-[0.18em] px-2 py-1 rounded"
 style={{
 color: "#F5A524",
 background: "rgba(0,0,0,0.65)",
 }}
 >
 Debug capture · {debugProgress}/{DEBUG_BATCH_COUNT}
 </div>
 <div className="absolute inset-x-0 bottom-0 px-3 py-2 bg-gradient-to-t from-black/90 to-transparent">
 <div className="text-[11px] text-gray-300 leading-snug">
 Hold the phone over a ribbon. We'll snap{" "}
 {DEBUG_BATCH_COUNT} frames so the maintainer can see
 what the decoder is working with.
 </div>
 </div>
 </div>
 )}

 {phase === "debug-results" && (
 <>
 <div className="border border-[#F5A524]/30 bg-[#F5A524]/5 rounded-lg p-3 mb-4 text-[12px]">
 <div className="text-[#F5A524] font-semibold mb-1">
 Debug batch complete
 </div>
 <div className="text-gray-300 leading-snug">
 Session <span className="font-mono">{debugSessionId}</span> ·
 {" "}{debugFrames.length} frames captured.
 </div>
 <div className="text-gray-400 leading-snug mt-1 font-mono text-[10px] break-all">
 Server dir: /tmp/zspan_scan_debug/{debugSessionId}/
 </div>
 <div className="text-gray-400 leading-snug mt-1">
 Decoded:{" "}
 {debugFrames.filter((f) =>
 "token" in f.result && f.result.token,
 ).length}{" "}
 / {debugFrames.length}
 </div>
 </div>
 <div className="space-y-3">
 {debugFrames.map((f) => {
 const r = f.result as DecodeResult;
 const ok = "token" in r && r.token && r.verdict?.exists;
 const found = "token" in r && r.token;
 const err = "error" in r ? (r as any).error : undefined;
 const status = ok
 ? `✅ ${r.token}`
 : found
 ? `🟠 token ${r.token} (not in audit log)`
 : err
 ? `❌ error: ${err}`
 : "⬜ no ribbon found";
 return (
 <div
 key={f.seq}
 className="border border-white/10 rounded-lg overflow-hidden"
 >
 <img
 src={f.preview}
 alt={`frame ${f.seq}`}
 className="block w-full h-auto"
 />
 <div className="px-3 py-2 bg-[#141416]">
 <div className="flex items-center justify-between text-[11px] font-mono mb-1">
 <span className="text-gray-400">
 #{f.seq.toString().padStart(2, "0")} · {f.elapsedMs}ms
 </span>
 <span className="text-gray-300">{status}</span>
 </div>
 {"stats" in r && r.stats && (
 <div className="text-[10px] font-mono text-gray-500 break-all">
 {JSON.stringify(r.stats).slice(0, 200)}
 </div>
 )}
 </div>
 </div>
 );
 })}
 </div>
 <div className="flex gap-2 mt-5">
 <button
 onClick={() => {
 setPhase("idle");
 setDebugFrames([]);
 setDebugSessionId(null);
 setDebugProgress(0);
 }}
 className="flex-1 border border-white/20 text-white font-medium py-3 rounded-lg text-[14px]"
 >
 Done
 </button>
 <button
 onClick={() => void startDebugCapture()}
 className="flex-1 bg-white text-black font-semibold py-3 rounded-lg text-[14px]"
 >
 Capture again
 </button>
 </div>
 </>
 )}

 {phase === "requesting" && (
 <div className="text-[13px] text-gray-400 italic">Asking the browser for camera access…</div>
 )}

 {(phase === "scanning" || phase === "verdict") && (
 <div className="relative rounded-xl overflow-hidden border border-white/10 bg-black mb-3">
 <video
 ref={videoRef}
 playsInline
 muted
 className="block w-full h-auto"
 style={{ maxHeight: "70vh", objectFit: "cover" }}
 />
 <canvas ref={canvasRef} className="hidden" />

 {/* Searching reticle — fades out once we lock on. */}
 {phase === "scanning" && lockState === "searching" && (
 <div
 aria-hidden
 className="absolute inset-0 pointer-events-none flex items-center justify-center"
 >
 <div
 style={{
 width: "80%",
 height: "14%",
 border: "1px dashed rgba(245,165,36,0.45)",
 borderRadius: 8,
 }}
 />
 </div>
 )}

 {/* AR overlay — anchored to the detected bbox.
 Corner brackets, scanline sweep, per-block illumination,
 verdict bloom. All live ON the live video, anchored to
 the ribbon's actual location. */}
 {(lockState === "locked" || lockState === "verifying" || phase === "verdict") &&
 lastBbox &&
 lastFrameDims &&
 videoRef.current && (() => {
 const v = videoRef.current!;
 const sx = v.clientWidth / lastFrameDims.w;
 const sy = v.clientHeight / lastFrameDims.h;
 const bL = lastBbox[0] * sx;
 const bT = lastBbox[1] * sy;
 const bW = (lastBbox[2] - lastBbox[0]) * sx;
 const bH = (lastBbox[3] - lastBbox[1]) * sy;

 // Bracket color: amber while scanning/verifying,
 // green when verdict has landed and was authentic.
 const verdictAuthenticated =
 result?.verdict?.authenticated === true;
 const verdictLegacy = result?.verdict?.legacy === true;
 const bracketColor =
 phase === "verdict"
 ? verdictLegacy
 ? "#F5A524"
 : verdictAuthenticated
 ? "#22C55E"
 : "#EF4444"
 : "#F5A524";

 // Bracket sizing — ~18% of the shorter bbox edge,
 // capped at 22px so they stay readable on small bbox.
 const bracketLen = Math.min(22, Math.max(10, Math.min(bW, bH) * 0.4));
 const bracketW = 2.5;

 // Inner block-strip geometry mirroring the Python
 // decoder constants. Same INNER_X_START_RATIO=0.318
 // tied to the FRAME_LABEL string length.
 const INNER_X_START = 0.318;
 const INNER_X_END = 0.981;
 const INNER_Y_START = 0.15;
 const INNER_Y_END = 0.85;
 const stripL = bL + bW * INNER_X_START;
 const stripT = bT + bH * INNER_Y_START;
 const stripW = bW * (INNER_X_END - INNER_X_START);
 const stripH = bH * (INNER_Y_END - INNER_Y_START);
 const blockW = stripW / 20;

 // Scanline position — sweeps left-to-right across the
 // inner block strip at ~1.5s per pass. Hidden once
 // verdict resolves.
 const scanProgress = ((fuzzTick * 5) % 100) / 100;
 const scanLeft = stripL + scanProgress * stripW;

 return (
 <>
 {/* Bracket: top-left */}
 <div
 aria-hidden
 className="absolute pointer-events-none"
 style={{
 left: bL,
 top: bT,
 width: bracketLen,
 height: bracketLen,
 borderLeft: `${bracketW}px solid ${bracketColor}`,
 borderTop: `${bracketW}px solid ${bracketColor}`,
 borderTopLeftRadius: 3,
 transition: "left 100ms ease-out, top 100ms ease-out, border-color 250ms ease",
 }}
 />
 {/* Bracket: top-right */}
 <div
 aria-hidden
 className="absolute pointer-events-none"
 style={{
 left: bL + bW - bracketLen,
 top: bT,
 width: bracketLen,
 height: bracketLen,
 borderRight: `${bracketW}px solid ${bracketColor}`,
 borderTop: `${bracketW}px solid ${bracketColor}`,
 borderTopRightRadius: 3,
 transition: "left 100ms ease-out, top 100ms ease-out, border-color 250ms ease",
 }}
 />
 {/* Bracket: bottom-left */}
 <div
 aria-hidden
 className="absolute pointer-events-none"
 style={{
 left: bL,
 top: bT + bH - bracketLen,
 width: bracketLen,
 height: bracketLen,
 borderLeft: `${bracketW}px solid ${bracketColor}`,
 borderBottom: `${bracketW}px solid ${bracketColor}`,
 borderBottomLeftRadius: 3,
 transition: "left 100ms ease-out, top 100ms ease-out, border-color 250ms ease",
 }}
 />
 {/* Bracket: bottom-right */}
 <div
 aria-hidden
 className="absolute pointer-events-none"
 style={{
 left: bL + bW - bracketLen,
 top: bT + bH - bracketLen,
 width: bracketLen,
 height: bracketLen,
 borderRight: `${bracketW}px solid ${bracketColor}`,
 borderBottom: `${bracketW}px solid ${bracketColor}`,
 borderBottomRightRadius: 3,
 transition: "left 100ms ease-out, top 100ms ease-out, border-color 250ms ease",
 }}
 />

 {/* Per-block illumination — color overlays on each
 block position inside the strip. Brightens as
 the vote accumulator gains confidence; pulses
 the palette color when a block resolves. */}
 {phase === "scanning" &&
 resolvedBlocks.map((r, i) => {
 const blockL = stripL + i * blockW;
 const innerInset = Math.max(1, blockW * 0.08);
 const innerL = blockL + innerInset;
 const innerW = blockW - innerInset * 2;
 const votes = blockVotesRef.current[i] ?? {};
 const totalVotes = Object.values(votes).reduce<number>(
 (a, b) => a + (b as number),
 0,
 );
 let bgColor: string;
 let opacity: number;
 if (r !== null) {
 // Resolved — solid palette overlay at high
 // opacity for the locked-in feel.
 bgColor = PALETTE_COLORS[r];
 opacity = 0.85;
 } else if (totalVotes > 0) {
 // Voting — cycle palette color at low-mid
 // opacity scaled by vote count.
 bgColor = PALETTE_COLORS[(fuzzTick + i * 3) % 4];
 opacity = 0.2 + Math.min(totalVotes * 0.15, 0.4);
 } else {
 // Unseen — invisible. The ribbon's real
 // colors below should show through clean.
 bgColor = "transparent";
 opacity = 0;
 }
 return (
 <div
 key={i}
 aria-hidden
 className="absolute pointer-events-none"
 style={{
 left: innerL,
 top: stripT,
 width: innerW,
 height: stripH,
 background: bgColor,
 opacity,
 mixBlendMode: "screen",
 borderRadius: 1,
 transition: "opacity 200ms ease, background 150ms ease",
 }}
 />
 );
 })}

 {/* Scanline — vertical sweep across the inner
 strip during scanning. Hidden in verdict phase. */}
 {phase === "scanning" && lockState === "locked" && (
 <div
 aria-hidden
 className="absolute pointer-events-none"
 style={{
 left: scanLeft,
 top: stripT - 3,
 width: 2,
 height: stripH + 6,
 background: PALETTE_COLORS[fuzzTick % 4],
 boxShadow: `0 0 8px ${PALETTE_COLORS[fuzzTick % 4]}`,
 opacity: 0.85,
 }}
 />
 )}

 {/* Verdict bloom — the verdict card materializes
 from the bbox center and anchors above or below
 the ribbon depending on screen space. */}
 {phase === "verdict" && result?.verdict && (
 <VerdictBloom
 bbox={{ left: bL, top: bT, width: bW, height: bH }}
 videoHeight={v.clientHeight}
 verdict={result.verdict}
 />
 )}
 </>
 );
 })()}

 {/* Status pill — minimal, top-left. Color reflects lock
 state at a glance. */}
 {phase === "scanning" && (
 <div
 className="absolute top-2 left-2 text-[10px] font-mono uppercase tracking-[0.18em] px-2 py-1 rounded"
 style={{
 color:
 lockState === "locked" || lockState === "verifying"
 ? "#22C55E"
 : "#F5A524",
 background: "rgba(0,0,0,0.55)",
 }}
 >
 {lockState === "verifying"
 ? "Verifying…"
 : lockState === "locked"
 ? `Locked · ${resolvedCount}/20`
 : "Searching for a ribbon…"}
 </div>
 )}
 </div>
 )}

 {phase === "scanning" && (
 <button
 onClick={() => {
 cleanup();
 setPhase("idle");
 }}
 className="w-full border border-white/20 text-white font-medium py-2.5 rounded-lg text-[13px]"
 >
 Stop
 </button>
 )}

 {phase === "verdict" && (
 <div className="flex gap-2">
 <button
 onClick={scanAgain}
 className="flex-1 bg-white text-black font-semibold py-3 rounded-lg text-[14px] tracking-wide"
 >
 Scan again
 </button>
 <button
 onClick={goToAuditPage}
 className="flex-1 border border-white/20 text-white font-medium py-3 rounded-lg text-[14px]"
 >
 Learn more
 </button>
 </div>
 )}

 {phase === "error" && (
 <>
 <div
 className="border border-[var(--alert-red)]/40 bg-[var(--alert-red)]/5 rounded-lg p-3 text-[13px] text-gray-200 mb-4"
 >
 <div className="text-[var(--alert-red)] font-semibold mb-1">Camera unavailable</div>
 {errorMsg}
 </div>
 <button
 onClick={() => void startScan()}
 className="w-full bg-white text-black font-semibold py-3 rounded-lg text-[14px] tracking-wide mb-2"
 >
 Try again
 </button>
 <button
 onClick={goToVerifyPage}
 className="w-full border border-white/20 text-white font-medium py-2.5 rounded-lg text-[13px]"
 >
 Upload a screenshot instead
 </button>
 </>
 )}
 </div>
 </div>
 );
}

// VerdictBloom — the verdict card materializes anchored to the
// detected ribbon's bbox. Animates in with scale + opacity from a tight
// origin at the ribbon center. Positioned above or below the bbox
// depending on screen real estate (whichever side has more room),
// with a small caret pointing back at the ribbon.
function VerdictBloom({
 bbox,
 videoHeight,
 verdict,
}: {
 bbox: { left: number; top: number; width: number; height: number };
 videoHeight: number;
 verdict: Verdict;
}) {
 const [visible, setVisible] = useState(false);
 useEffect(() => {
 // Trigger the scale-in on next frame so the initial render is at
 // scale 0 and the user sees the bloom.
 const t = window.setTimeout(() => setVisible(true), 30);
 return () => window.clearTimeout(t);
 }, []);

 const authenticated = verdict.exists && verdict.authenticated === true;
 const legacy = verdict.legacy === true;
 const tint = legacy ? "#F5A524" : authenticated ? "#22C55E" : "#EF4444";
 const bgTint = legacy
 ? "rgba(245,165,36,0.15)"
 : authenticated
 ? "rgba(34,197,94,0.15)"
 : "rgba(239,68,68,0.15)";

 // Place above the bbox if there's more room above; below otherwise.
 // Most laptop-screen captures put the ribbon mid-frame so above is
 // typical.
 const roomAbove = bbox.top;
 const roomBelow = videoHeight - (bbox.top + bbox.height);
 const placeAbove = roomAbove >= roomBelow;

 const cardOffset = 18;
 const cardStyle: React.CSSProperties = placeAbove
 ? { bottom: videoHeight - bbox.top + cardOffset }
 : { top: bbox.top + bbox.height + cardOffset };

 const originX = bbox.left + bbox.width / 2;

 return (
 <>
 {/* Connector line from card to bbox. */}
 <div
 aria-hidden
 className="absolute pointer-events-none"
 style={{
 left: originX - 0.5,
 width: 1,
 background: tint,
 opacity: visible ? 0.5 : 0,
 top: placeAbove ? bbox.top - cardOffset : bbox.top + bbox.height,
 height: cardOffset,
 transition: "opacity 350ms ease 200ms",
 }}
 />
 <div
 className="absolute pointer-events-none px-4 py-3 rounded-lg"
 style={{
 ...cardStyle,
 left: 12,
 right: 12,
 border: `1px solid ${tint}`,
 background: `linear-gradient(180deg, ${bgTint}, rgba(10,10,10,0.92))`,
 backdropFilter: "blur(8px)",
 WebkitBackdropFilter: "blur(8px)",
 opacity: visible ? 1 : 0,
 transform: visible ? "scale(1)" : "scale(0.85)",
 transformOrigin: `${originX - 12}px ${placeAbove ? "100%" : "0%"}`,
 transition:
 "opacity 350ms cubic-bezier(0.16, 1, 0.3, 1), transform 450ms cubic-bezier(0.16, 1, 0.3, 1)",
 }}
 >
 <div
 className="text-[15px] font-semibold mb-1"
 style={{ color: tint }}
 >
 {legacy
 ? "Legacy identifier — not authentication"
 : authenticated
 ? "Registry match · canonical record"
 : "Token not registered"}
 </div>
 {legacy ? (
 <div className="text-[12px] text-gray-300 leading-snug">
 This is a publicly reproducible legacy identifier — not authentication.
 The screenshot content itself is not authenticated.
 </div>
 ) : authenticated ? (
 <>
 <div className="text-[11px] text-gray-300 leading-snug mb-1">
 This token maps to Z-SPAN&apos;s canonical record. The screenshot
 content itself is not authenticated.
 </div>
 <div className="text-[13px] text-white font-medium leading-tight">
 {verdict.city_name}
 </div>
 {verdict.meeting_title && (
 <div className="text-[11px] text-gray-300 leading-snug mt-0.5">
 {verdict.meeting_title}
 </div>
 )}
 {verdict.output_type && (
 <div className="text-[10px] text-gray-500 font-mono mt-1">
 {verdict.output_type}
 </div>
 )}
 </>
 ) : (
 <div className="text-[12px] text-gray-300 leading-snug">
 {verdict.note || "This token isn't in our audit log."}
 </div>
 )}
 </div>
 </>
 );
}

// VirtualRibbon — visible build-up of the decoded ribbon. Each of the
// 20 cells shows one of three states:
//
// resolved → solid palette color (the vote accumulator locked in a
// winner for this block position)
// voting → semi-bright palette color cycling through the votes
// seen so far; opacity scales with vote count so blocks
// near a confident verdict look "almost there"
// unseen → low-opacity grayscale cycling, the "scrambled" state
//
// The fuzzTick prop increments at ~10fps while scanning; unresolved
// cells pick a palette color based on (fuzzTick + i*3) % 4 — staggered
// so adjacent cells don't all show the same color at the same moment.
// Net effect: a key-lock-decryption-style visualization of the scan.
function VirtualRibbon({
 resolved,
 votes,
 fuzzTick,
 consensusToken,
}: {
 resolved: (number | null)[];
 votes: BlockVotes[];
 fuzzTick: number;
 consensusToken: string | null;
}) {
 const resolvedCount = resolved.filter((r) => r !== null).length;

 const cellW = 16;
 const gap = 2;
 const cellH = 32;
 const totalW = (cellW + gap) * 20 - gap;

 const renderCell = (i: number) => {
 const r = resolved[i];
 if (r !== null) {
 // Resolved — solid palette color.
 return (
 <rect
 key={i}
 x={i * (cellW + gap)}
 y={0}
 width={cellW}
 height={cellH}
 fill={PALETTE_COLORS[r]}
 rx={1}
 ry={1}
 />
 );
 }
 const v = votes[i] ?? {};
 const totalVotes = Object.values(v).reduce<number>((a, b) => a + (b as number), 0);
 // Cycle through palette colors at the fuzz cadence; stagger per
 // cell so the ribbon doesn't pulse as one solid block.
 const fuzzColor = PALETTE_COLORS[(fuzzTick + i * 3) % 4];
 // Opacity scales with votes so cells close to resolving look
 // "almost there" — visual progress indicator beyond the count.
 const opacity = totalVotes === 0 ? 0.15 : 0.25 + Math.min(totalVotes * 0.15, 0.55);
 return (
 <rect
 key={i}
 x={i * (cellW + gap)}
 y={0}
 width={cellW}
 height={cellH}
 fill={totalVotes === 0 ? "#3F3F46" : fuzzColor}
 fillOpacity={opacity}
 rx={1}
 ry={1}
 />
 );
 };

 return (
 <div className="border border-white/10 rounded-lg p-3 bg-[#141416]">
 <div className="flex items-center justify-between mb-2">
 <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72]">
 Decoding the ribbon
 </div>
 <div className="text-[10px] font-mono text-gray-400">
 {resolvedCount}/20
 </div>
 </div>
 <div className="flex justify-center mb-2">
 <svg
 width={totalW}
 height={cellH}
 viewBox={`0 0 ${totalW} ${cellH}`}
 xmlns="http://www.w3.org/2000/svg"
 style={{ display: "block", maxWidth: "100%" }}
 >
 {Array.from({ length: 20 }, (_, i) => renderCell(i))}
 </svg>
 </div>
 {consensusToken && (
 <div className="font-mono text-[14px] tracking-[0.2em] text-center text-white pt-1">
 {consensusToken}
 </div>
 )}
 </div>
 );
}

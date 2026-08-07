/**
 * ReportGeneratorModal —Report-V0-1 (cited-report generator).
 *
 * Opened from the TopBarSearch dropdown's owner-only "Generate report"
 * entry. Reuses the OperatorSearch Phase-1 interpret endpoint verbatim
 * (same natural-language → scope extraction), then drives the report
 * pipeline instead of the chat answer:
 *
 * interpret → confirm (scope + cost) → POST /api/report-runs →
 * poll GET /api/report-runs/:id → preview iframe + download.
 *
 * Owner-only at the trigger surface AND at every backend endpoint
 * (is_operator_search_principal), same posture as
 * OperatorSearchModal — this modal can't render for non-owners because
 * the query prop is only ever set by an owner-gated affordance.
 *
 * The generation runs in a Flask-side daemon thread; closing this modal
 * does NOT cancel it (the run row keeps its state — V0 has no resume
 * surface, so the running-phase copy says so honestly).
 */
import { useEffect, useRef, useState } from "react";
import { X, Download, ExternalLink, Loader2, Check, AlertTriangle, FileText, Sparkles } from "lucide-react";

interface InterpretResponse {
 success: boolean;
 interpretation: {
 state?: string | null;
 county?: string | null;
 city?: string | null;
 keywords?: string[];
 confidence?: string;
 };
 meeting_ids: number[];
 match_count: number;
 indexed_count: number;
 unindexed_count: number;
 error?: string;
}

interface ReportRun {
 id: string;
 status: "pending" | "running" | "complete" | "error";
 progress?: string;
 current_section?: string | null;
 sections?: Record<
 string,
 { status: "ok" | "error"; error?: string; duration_ms?: number }
 > | null;
 run_id?: string | null;
 has_artifact?: boolean;
 error?: string | null;
}

// Keep in sync with REPORT_SECTIONS in zspan_pipeline/report_generator.py.
const SECTION_STEPS: Array<{ key: string; label: string }> = [
 { key: "synopsis", label: "Executive synopsis" },
 { key: "findings", label: "Findings" },
 { key: "jurisdictions", label: "By jurisdiction" },
 { key: "quotes", label: "Key quotes" },
 { key: "decisions", label: "Decisions & votes" },
];

type Phase =
 | { kind: "interpreting" }
 | { kind: "interpret_error"; message: string }
 | { kind: "confirm"; interp: InterpretResponse }
 | { kind: "creating"; interp: InterpretResponse }
 | { kind: "running"; runId: string; run: ReportRun | null }
 | { kind: "complete"; runId: string; run: ReportRun }
 | { kind: "failed"; message: string; runId?: string };

interface ReportGeneratorModalProps {
 query: string | null; // null = closed; set = open with this query
 onClose: () => void;
}

function scopeLabel(interp: InterpretResponse["interpretation"]): string {
 const bits = [interp.state, interp.county, interp.city].filter(
 Boolean,
 ) as string[];
 return bits.length ? bits.join(" · ") : "all locations";
}

// Report-Stitch-1 (V0.5) — generative-chrome sub-state, only meaningful
// on the complete phase. idle → running (poll /stitch-status) →
// complete | error. The V0 artifact always stays available; the Stitch
// artifact is an additional variant.
type StitchState =
 | { kind: "idle" }
 | { kind: "running"; progress: string; passes: number }
 | { kind: "complete" }
 | { kind: "error"; message: string };

export function ReportGeneratorModal({ query, onClose }: ReportGeneratorModalProps) {
 const [phase, setPhase] = useState<Phase>({ kind: "interpreting" });
 const [stitch, setStitch] = useState<StitchState>({ kind: "idle" });
 const [previewVariant, setPreviewVariant] = useState<"v0" | "stitch">("v0");
 const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
 const stitchPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

 const stopPolling = () => {
 if (pollRef.current) {
 clearInterval(pollRef.current);
 pollRef.current = null;
 }
 if (stitchPollRef.current) {
 clearInterval(stitchPollRef.current);
 stitchPollRef.current = null;
 }
 };

 const startStitch = async (runId: string) => {
 setStitch({ kind: "running", progress: "Starting Stitch chrome...", passes: 0 });
 try {
 const r = await fetch(
 `/api/report-runs/${encodeURIComponent(runId)}/stitch`,
 { method: "POST", headers: { "Content-Type": "application/json" } },
 );
 const d = await r.json();
 if (!d.success) {
 setStitch({ kind: "error", message: d.error || "Could not start Stitch run." });
 return;
 }
 stitchPollRef.current = setInterval(async () => {
 try {
 const sr = await fetch(
 `/api/report-runs/${encodeURIComponent(runId)}/stitch-status`,
 );
 const sd = await sr.json();
 if (!sd.success) return;
 if (sd.status === "complete") {
 if (stitchPollRef.current) clearInterval(stitchPollRef.current);
 stitchPollRef.current = null;
 setStitch({ kind: "complete" });
 setPreviewVariant("stitch");
 } else if (sd.status === "error") {
 if (stitchPollRef.current) clearInterval(stitchPollRef.current);
 stitchPollRef.current = null;
 setStitch({ kind: "error", message: sd.error || "Stitch run failed." });
 } else if (sd.status === "running") {
 setStitch({
 kind: "running",
 progress: sd.progress || "Designing...",
 passes: sd.passes ?? 0,
 });
 }
 } catch {
 // network blip — keep polling
 }
 }, 3000);
 } catch {
 setStitch({ kind: "error", message: "Could not start Stitch run (network error)." });
 }
 };

 // Phase 1 — interpret, on open. Same endpoint the OperatorSearch modal
 // uses; the scope machinery is shared by design.
 useEffect(() => {
 if (!query) return;
 let cancelled = false;
 setPhase({ kind: "interpreting" });
 fetch("/api/operator-search/interpret", {
 method: "POST",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify({ query }),
 })
 .then(r => r.json())
 .then((d: InterpretResponse) => {
 if (cancelled) return;
 if (!d.success) {
 setPhase({
 kind: "interpret_error",
 message: d.error || "Scope interpretation failed.",
 });
 return;
 }
 setPhase({ kind: "confirm", interp: d });
 })
 .catch(() => {
 if (!cancelled) {
 setPhase({
 kind: "interpret_error",
 message: "Scope interpretation failed or timed out.",
 });
 }
 });
 return () => {
 cancelled = true;
 stopPolling();
 };
 }, [query]);

 // Poll loop while a run is in flight.
 const beginPolling = (runId: string) => {
 stopPolling();
 pollRef.current = setInterval(async () => {
 try {
 const r = await fetch(`/api/report-runs/${encodeURIComponent(runId)}`);
 const d = await r.json();
 if (!d.success) return; // transient — keep polling
 const run: ReportRun = d.run;
 if (run.status === "complete") {
 stopPolling();
 setPhase({ kind: "complete", runId, run });
 } else if (run.status === "error") {
 stopPolling();
 setPhase({
 kind: "failed",
 runId,
 message: run.error || "Report generation failed.",
 });
 } else {
 setPhase({ kind: "running", runId, run });
 }
 } catch {
 // network blip — keep polling silently
 }
 }, 2500);
 };

 const startGeneration = async (interp: InterpretResponse) => {
 setPhase({ kind: "creating", interp });
 try {
 const r = await fetch("/api/report-runs", {
 method: "POST",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify({
 query,
 meeting_ids: interp.meeting_ids,
 interpretation: interp.interpretation,
 }),
 });
 const d = await r.json();
 if (!d.success) {
 setPhase({
 kind: "failed",
 message: d.error || "Could not start the report run.",
 });
 return;
 }
 setPhase({ kind: "running", runId: d.id, run: null });
 beginPolling(d.id);
 } catch {
 setPhase({
 kind: "failed",
 message: "Could not start the report run (network error).",
 });
 }
 };

 const close = () => {
 stopPolling();
 onClose();
 };

 // Escape closes at any phase.
 useEffect(() => {
 if (!query) return;
 const onKey = (e: KeyboardEvent) => {
 if (e.key === "Escape") close();
 };
 document.addEventListener("keydown", onKey);
 return () => document.removeEventListener("keydown", onKey);
 // eslint-disable-next-line react-hooks/exhaustive-deps
 }, [query]);

 if (!query) return null;

 const artifactPath = (runId: string) =>
 `/api/report-runs/${encodeURIComponent(runId)}/artifact`;

 return (
 <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
 <div className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-white/15 bg-[#0A0A0C] shadow-2xl">
 {/* Header */}
 <div className="flex items-start justify-between gap-3 border-b border-white/10 px-5 py-4">
 <div className="min-w-0">
 <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-amber-400/70">
 <FileText className="h-3 w-3" />
 Cited report · operator
 </div>
 <div className="mt-1 truncate font-sans text-[15px] text-white/90">
 “{query}”
 </div>
 </div>
 <button
 type="button"
 onClick={close}
 className="flex-none rounded-md p-1.5 text-white/40 transition-colors hover:bg-white/5 hover:text-white/80"
 aria-label="Close"
 >
 <X className="h-4 w-4" />
 </button>
 </div>

 {/* Body */}
 <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4 text-[13px] text-white/75">
 {phase.kind === "interpreting" && (
 <div className="flex items-center gap-2 py-6 text-white/60">
 <Loader2 className="h-4 w-4 animate-spin" />
 Reading the query and resolving its scope...
 </div>
 )}

 {phase.kind === "interpret_error" && (
 <div className="flex items-start gap-2 py-4 text-red-300/90">
 <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" />
 <span>{phase.message}</span>
 </div>
 )}

 {(phase.kind === "confirm" || phase.kind === "creating") && (
 <div className="space-y-4">
 <div className="rounded-lg border border-white/10 bg-white/[0.03] px-4 py-3">
 <div className="text-[10px] uppercase tracking-widest text-white/35">
 Scope
 </div>
 <div className="mt-1 text-white/85">
 {scopeLabel(phase.interp.interpretation)}
 </div>
 <div className="mt-2 text-[12px] leading-relaxed text-white/55">
 {phase.interp.indexed_count} indexed meeting
 {phase.interp.indexed_count !== 1 ? "s" : ""} will be
 searched
 {phase.interp.unindexed_count > 0 && (
 <>
 {" "}
 ({phase.interp.unindexed_count} matching meeting
 {phase.interp.unindexed_count !== 1 ? "s" : ""} not yet
 indexed — the report will honestly cover only the
 indexed record)
 </>
 )}
 .
 </div>
 </div>
 <div className="rounded-lg border border-white/10 bg-white/[0.03] px-4 py-3 text-[12px] leading-relaxed text-white/55">
 Five report sections, one Sonnet synthesis pass each, over a
 shared retrieval union — synthesis rides the MAX
 subscription ($0 incremental). Typical wall-clock is 3-6
 minutes; the report keeps generating server-side even if you
 close this window.
 </div>
 <div className="flex items-center justify-end gap-2">
 <button
 type="button"
 onClick={close}
 className="rounded-md px-3 py-1.5 text-[12px] text-white/50 transition-colors hover:bg-white/5 hover:text-white/80"
 >
 Cancel
 </button>
 <button
 type="button"
 disabled={
 phase.kind === "creating" ||
 phase.interp.indexed_count === 0
 }
 onClick={() =>
 phase.kind === "confirm" && startGeneration(phase.interp)
 }
 className="flex items-center gap-2 rounded-md border border-amber-400/40 bg-amber-400/10 px-4 py-1.5 text-[12px] font-medium text-amber-200 transition-colors hover:bg-amber-400/20 disabled:cursor-not-allowed disabled:opacity-50"
 >
 {phase.kind === "creating" && (
 <Loader2 className="h-3.5 w-3.5 animate-spin" />
 )}
 {phase.interp.indexed_count === 0
 ? "No indexed meetings in scope"
 : "Generate report"}
 </button>
 </div>
 </div>
 )}

 {phase.kind === "running" && (
 <div className="space-y-4">
 <div className="flex items-center gap-2 text-white/70">
 <Loader2 className="h-4 w-4 animate-spin text-amber-400/80" />
 {phase.run?.progress || "Starting the pipeline..."}
 </div>
 <ol className="space-y-1.5">
 {SECTION_STEPS.map(step => {
 const s = phase.run?.sections?.[step.key];
 const isCurrent = phase.run?.current_section === step.key;
 return (
 <li
 key={step.key}
 className="flex items-center gap-2 text-[12.5px]"
 >
 {s?.status === "ok" ? (
 <Check className="h-3.5 w-3.5 flex-none text-emerald-400/80" />
 ) : s?.status === "error" ? (
 <AlertTriangle className="h-3.5 w-3.5 flex-none text-orange-400/80" />
 ) : isCurrent ? (
 <Loader2 className="h-3.5 w-3.5 flex-none animate-spin text-amber-400/70" />
 ) : (
 <span className="h-3.5 w-3.5 flex-none rounded-full border border-white/15" />
 )}
 <span
 className={
 s?.status === "ok"
 ? "text-white/80"
 : isCurrent
 ? "text-amber-200/90"
 : "text-white/40"
 }
 >
 {step.label}
 {s?.status === "error" && (
 <span className="ml-1 text-orange-300/70">
 — failed (report continues without it)
 </span>
 )}
 </span>
 </li>
 );
 })}
 </ol>
 <div className="text-[11.5px] leading-relaxed text-white/35">
 Closing this window won’t cancel the run — it completes
 server-side. Retrieval + provenance rows are already written.
 </div>
 </div>
 )}

 {phase.kind === "complete" && (
 <div className="space-y-3">
 <div className="flex items-center gap-2 text-emerald-300/90">
 <Check className="h-4 w-4" />
 Report complete
 {phase.run.run_id && (
 <span className="truncate font-mono text-[10.5px] text-white/35">
 {phase.run.run_id}
 </span>
 )}
 {/* Variant toggle appears once the Stitch chrome exists */}
 {stitch.kind === "complete" && (
 <span className="ml-auto flex items-center gap-1 text-[11px]">
 {(["v0", "stitch"] as const).map(v => (
 <button
 key={v}
 type="button"
 onClick={() => setPreviewVariant(v)}
 className={`rounded px-2 py-0.5 transition-colors ${
 previewVariant === v
 ? "bg-white/10 text-white/90"
 : "text-white/40 hover:text-white/70"
 }`}
 >
 {v === "v0" ? "Branded" : "Stitch"}
 </button>
 ))}
 </span>
 )}
 </div>
 <div className="overflow-hidden rounded-lg border border-white/12 bg-white">
 <iframe
 title="Report preview"
 src={`${artifactPath(phase.runId)}${previewVariant === "stitch" ? "?variant=stitch" : ""}`}
 sandbox="allow-popups"
 className="h-[46vh] w-full"
 />
 </div>
 {/* Report-Stitch-1 — generative chrome (V0.5). The branded
 artifact always exists; this designs an alternate skin
 around the SAME injected content. */}
 {stitch.kind === "idle" && (
 <div className="flex items-center justify-between gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2">
 <span className="text-[11.5px] leading-relaxed text-white/45">
 Optional: design a generative chrome around this report via
 Google Stitch (~4-6 design passes; content and citations are
 injected locally and never sent to Stitch).
 </span>
 <button
 type="button"
 onClick={() => startStitch(phase.runId)}
 className="flex flex-none items-center gap-1.5 rounded-md border border-white/15 px-3 py-1.5 text-[12px] text-white/70 transition-colors hover:bg-white/5 hover:text-white/90"
 >
 <Sparkles className="h-3.5 w-3.5" />
 Generate Stitch chrome
 </button>
 </div>
 )}
 {stitch.kind === "running" && (
 <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-[12px] text-white/60">
 <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-400/70" />
 {stitch.progress}
 <span className="ml-auto font-mono text-[10px] text-white/30">
 {stitch.passes} pass{stitch.passes !== 1 ? "es" : ""}
 </span>
 </div>
 )}
 {stitch.kind === "error" && (
 <div className="flex items-start justify-between gap-2 rounded-lg border border-orange-400/20 bg-orange-400/5 px-3 py-2 text-[12px] text-orange-300/80">
 <span>Stitch chrome failed: {stitch.message} — the branded report is unaffected.</span>
 <button
 type="button"
 onClick={() => startStitch(phase.runId)}
 className="flex-none text-white/50 underline-offset-2 hover:text-white/80 hover:underline"
 >
 Retry
 </button>
 </div>
 )}
 <div className="flex items-center justify-end gap-2">
 <a
 href={`${artifactPath(phase.runId)}${previewVariant === "stitch" ? "?variant=stitch" : ""}`}
 target="_blank"
 rel="noopener noreferrer"
 className="flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] text-white/60 transition-colors hover:bg-white/5 hover:text-white/90"
 >
 <ExternalLink className="h-3.5 w-3.5" />
 Open in tab
 </a>
 <a
 href={`${artifactPath(phase.runId)}?download=1${previewVariant === "stitch" ? "&variant=stitch" : ""}`}
 className="flex items-center gap-1.5 rounded-md border border-amber-400/40 bg-amber-400/10 px-4 py-1.5 text-[12px] font-medium text-amber-200 transition-colors hover:bg-amber-400/20"
 >
 <Download className="h-3.5 w-3.5" />
 Download {previewVariant === "stitch" ? "Stitch report" : "report"}
 </a>
 </div>
 </div>
 )}

 {phase.kind === "failed" && (
 <div className="space-y-3">
 <div className="flex items-start gap-2 text-red-300/90">
 <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" />
 <span>{phase.message}</span>
 </div>
 {phase.runId && (
 <div className="font-mono text-[10.5px] text-white/30">
 run {phase.runId}
 </div>
 )}
 </div>
 )}
 </div>
 </div>
 </div>
 );
}

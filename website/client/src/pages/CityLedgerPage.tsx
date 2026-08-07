/**
 * CityLedgerPage — Tracked Claims Ledger, city-level view.
 *
 * The accountability surface where every forward-looking statement made
 * by officials in this city accumulates as long-term evidence. Each row
 * is a verbatim quote rendered with the marker-styled karaoke — the
 * audio drawing across the words IS the evidence-grade UI.
 *
 * Filters: status (active / fulfilled / broken / unclear / withdrawn)
 * and an "aged past horizon" toggle that surfaces the next-review feed
 * (claims whose time horizon has elapsed but status is still active —
 * the actionable backlog for journalists and citizens).
 */
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowUpRight, Building2 } from "lucide-react";
import SyncedQuote from "../components/SyncedQuote";
import { fetchForPlane } from "../lib/planeFetch";
import { isPublicPlane } from "../lib/trustPlane";
import {
 TrackedClaim,
 TrackedClaimStatus,
 TRACKED_CLAIM_STATUS_DISPLAY,
 TRACKED_CLAIM_TYPE_DISPLAY,
 TRACKED_CLAIM_MARKER_COLOR,
 formatTimeHorizon,
 isAgedPastHorizon,
} from "../utils/trackedClaims";

interface CityLedgerPageProps {
 cityName: string;
 onBack: () => void;
 onNavigate: (view: string, params?: Record<string, unknown>) => void;
}

type LedgerPayload = {
 city: string;
 county: string | null;
 state: string | null;
 count: number;
 tracked_claims: TrackedClaim[];
};

const ALL_STATUSES: TrackedClaimStatus[] = [
 "active", "unclear", "fulfilled", "broken", "withdrawn",
];

function formatMeetingDate(s: string | null | undefined): string {
 if (!s) return "—";
 const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s + "T00:00:00") : new Date(s);
 if (isNaN(d.getTime())) return s;
 return d.toLocaleDateString("en-US", {
 month: "short", day: "numeric", year: "numeric",
 });
}

export default function CityLedgerPage({
 cityName, onBack, onNavigate,
}: CityLedgerPageProps) {
 const [payload, setPayload] = useState<LedgerPayload | null>(null);
 const [loading, setLoading] = useState(false);
 const [error, setError] = useState<string | null>(null);

 // Filters
 const [statusFilter, setStatusFilter] = useState<Set<TrackedClaimStatus>>(
 () => new Set<TrackedClaimStatus>(["active", "unclear"])
 );
 const [agedOnly, setAgedOnly] = useState(false);

 // Only one karaoke active across the page.
 const [activeClaimId, setActiveClaimId] = useState<string | number | null>(null);

 // Fetch — refetch when filters change. `aged=true` is server-side; status
 // filter is sent as CSV. The server filters even though we could do it
 // client-side, because the backend's aged check uses SQL date math and
 // is the source of truth.
 useEffect(() => {
 let aborted = false;
 setLoading(true);
 setError(null);

 const qs = new URLSearchParams();
 if (statusFilter.size > 0 && statusFilter.size < ALL_STATUSES.length) {
 qs.set("status", Array.from(statusFilter).join(","));
 }
 if (agedOnly) qs.set("aged", "true");

 const suffix = qs.toString() ? `?${qs}` : "";
 fetchForPlane({
 publicPath: `/public-api/ledger/${encodeURIComponent(cityName)}${suffix}`,
 operatorPath: `/api/ledger/${encodeURIComponent(cityName)}${suffix}`,
 })
 .then(async r => {
 if (!r.ok) throw new Error(`HTTP ${r.status}`);
 return r.json();
 })
 .then(data => {
 if (aborted) return;
 setPayload(data);
 setActiveClaimId(null);
 })
 .catch(e => {
 if (aborted) return;
 setError(e?.message ?? String(e));
 })
 .finally(() => {
 if (aborted) return;
 setLoading(false);
 });

 return () => { aborted = true; };
 }, [cityName, statusFilter, agedOnly]);

 const claims = payload?.tracked_claims ?? [];
 const totalLabel = useMemo(() => {
 if (loading) return "Loading…";
 if (!claims.length) return "No claims";
 return `${claims.length} ${claims.length === 1 ? "claim" : "claims"}`;
 }, [claims.length, loading]);

 return (
 <div className="min-h-screen bg-background text-foreground">
 {/* Header */}
 <header className="sticky top-0 z-40 bg-[var(--canvas)]/95 backdrop-blur border-b border-[var(--line)]">
 <div className="max-w-7xl mx-auto px-6 lg:px-10 py-5 flex items-center justify-between gap-4">
 <div className="flex items-center gap-5 min-w-0">
 <button
 onClick={onBack}
 className="group flex items-center gap-2 text-muted-foreground hover:text-foreground transition-colors"
 >
 <ArrowLeft className="w-3.5 h-3.5 group-hover:-translate-x-0.5 transition-transform" />
 <span className="text-xs font-medium tracking-wide uppercase">Back</span>
 </button>
 <div className="h-4 w-px bg-[var(--line)]" />
 <div className="flex items-center gap-3 min-w-0">
 <div className="bg-white text-black p-1.5 rounded-md flex-shrink-0">
 <Building2 className="w-4 h-4" />
 </div>
 <div className="min-w-0">
 <p className="text-[10px] uppercase tracking-[0.22em] text-foreground/45">
 Accountability Ledger
 </p>
 <h1 className="text-base font-bold tracking-wider uppercase truncate text-white">
 {cityName}
 </h1>
 </div>
 </div>
 </div>
 <div className="hidden sm:flex items-center gap-2.5 px-3 py-1.5 rounded-md border border-[var(--line)] bg-[var(--surface)]">
 <span className="text-[11px] text-muted-foreground font-medium tracking-wide uppercase tabular-nums">
 {totalLabel}
 </span>
 </div>
 </div>
 </header>

 <main className="max-w-7xl mx-auto px-6 lg:px-10 py-8">
 {/* Filter rail */}
 <div className="mb-8 flex flex-wrap items-center gap-3 text-[10px] uppercase tracking-[0.18em]">
 <span className="text-foreground/45">Status</span>
 {ALL_STATUSES.map(s => {
 const display = TRACKED_CLAIM_STATUS_DISPLAY[s];
 const on = statusFilter.has(s);
 return (
 <button
 key={s}
 onClick={() =>
 setStatusFilter(prev => {
 const next = new Set(prev);
 if (on) next.delete(s); else next.add(s);
 return next;
 })
 }
 className="px-2 py-1 border rounded-sm transition-colors"
 style={{
 color: on ? display.fg : "rgba(255,255,255,0.35)",
 borderColor: on ? display.border : "var(--line)",
 backgroundColor: on ? "rgba(255,255,255,0.02)" : "transparent",
 }}
 >
 {display.label}
 </button>
 );
 })}
 <span className="mx-2 h-3 w-px bg-[var(--line)]" />
 <button
 onClick={() => setAgedOnly(v => !v)}
 className="px-2 py-1 border rounded-sm transition-colors"
 style={{
 color: agedOnly ? "#F87171" : "rgba(255,255,255,0.35)",
 borderColor: agedOnly ? "rgba(248,113,113,0.45)" : "var(--line)",
 backgroundColor: agedOnly ? "rgba(255,255,255,0.02)" : "transparent",
 }}
 title="Show only active claims whose time horizon has elapsed"
 >
 Aged past horizon only
 </button>
 </div>

 {error && (
 <div className="border border-[var(--line)] rounded-md px-4 py-3 text-sm text-red-400">
 Could not load ledger: {error}
 </div>
 )}

 {!error && !loading && claims.length === 0 && (
 <div className="text-[13px] text-muted-foreground leading-relaxed max-w-2xl">
 <p>No tracked claims for this filter.</p>
 <p className="mt-3 text-foreground/40">
 The ledger fills as the NotebookLM <code>tracked_claims</code> prompt
 runs against each processed meeting. Every assurance, commitment,
 prediction, or promise made by an official accumulates here with a
 verbatim audio anchor + a status that operators flip as the outcome
 becomes clear.
 </p>
 </div>
 )}

 {/* Claim cards */}
 <ul className="flex flex-col gap-6">
 {claims.map(c => {
 const claimKey =
 c.id ??
 `${c.meeting_public_id ?? c.meeting_id}:${c.seat_id ?? ""}:${c.claim_text}`;
 const canKaraoke =
 Array.isArray(c.word_timings) &&
 c.word_timings.length > 0 &&
 !!(c.video_url ?? c.meeting_video_url);
 const statusDisplay =
 TRACKED_CLAIM_STATUS_DISPLAY[c.status] ??
 TRACKED_CLAIM_STATUS_DISPLAY.active;
 const typeLabel =
 (c.claim_type && TRACKED_CLAIM_TYPE_DISPLAY[c.claim_type]) ||
 (c.claim_type ?? "Claim");
 const aged = isAgedPastHorizon(c);
 return (
 <li
 key={claimKey}
 className="border border-[var(--line)] rounded-md p-5 bg-[var(--surface)]/40"
 >
 {/* Speaker + meeting header */}
 <div className="flex items-start justify-between gap-4 mb-3">
 <div className="min-w-0">
 <p className="text-[14px] text-white font-semibold leading-tight">
 {c.speaker_name ?? "Speaker"}
 {c.speaker_role && (
 <span className="text-foreground/45 font-normal text-[12px] ml-2">
 {c.speaker_role}
 </span>
 )}
 </p>
 <button
 onClick={() =>
 onNavigate(
 "broadcast",
 isPublicPlane()
 ? { publicId: c.meeting_public_id }
 : { meetingId: c.meeting_id },
 )
 }
 className="mt-1 text-[11px] uppercase tracking-[0.18em] text-foreground/45 hover:text-white transition-colors inline-flex items-center gap-1.5"
 >
 {formatMeetingDate(c.meeting_date)}
 <ArrowUpRight className="w-2.5 h-2.5" />
 <span className="normal-case tracking-normal text-foreground/40">
 {c.meeting_title}
 </span>
 </button>
 </div>
 <div className="flex flex-col items-end gap-2 flex-shrink-0">
 <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.18em]">
 <span
 className="px-1.5 py-0.5 border rounded-sm tabular-nums"
 style={{
 color: statusDisplay.fg,
 borderColor: statusDisplay.border,
 }}
 >
 {statusDisplay.label}
 </span>
 <span className="text-foreground/40">{typeLabel}</span>
 </div>
 <span className="text-[10px] uppercase tracking-[0.18em] text-foreground/35">
 Horizon: {formatTimeHorizon(c.time_horizon_months)}
 </span>
 {aged && (
 <span
 className="text-[9px] px-1 py-0.5 border rounded-sm uppercase tracking-[0.18em]"
 style={{
 color: "#F87171",
 borderColor: "rgba(248, 113, 113, 0.45)",
 }}
 title="Time horizon elapsed; still active"
 >
 Aged past horizon
 </span>
 )}
 </div>
 </div>

 {/* Marker karaoke or fallback */}
 {canKaraoke ? (
 <SyncedQuote
 wordTimings={c.word_timings!}
 videoUrl={(c.video_url ?? c.meeting_video_url)!}
 isActive={activeClaimId === claimKey}
 onActivate={() => setActiveClaimId(claimKey)}
 onDeactivate={() =>
 setActiveClaimId(prev => (prev === claimKey ? null : prev))
 }
 markerColor={TRACKED_CLAIM_MARKER_COLOR}
 />
 ) : (
 <p className="text-[14px] text-white/85 leading-snug italic">
 &ldquo;{c.claim_text}&rdquo;
 </p>
 )}

 {/* Verifies-if + status evidence */}
 {c.expected_outcome && (
 <p className="text-[12px] text-foreground/60 mt-3 leading-snug">
 <span className="text-foreground/35 uppercase tracking-[0.18em] text-[9px] mr-1.5">
 Verifies if
 </span>
 {c.expected_outcome}
 </p>
 )}
 {c.status_evidence && (
 <p className="text-[11px] text-foreground/50 mt-2 leading-snug border-l-2 border-[var(--line)] pl-3">
 <span className="text-foreground/30 uppercase tracking-[0.18em] text-[9px] mr-1.5">
 Status note
 </span>
 {c.status_evidence}
 {c.status_updated_by && (
 <span className="text-foreground/30 ml-2">
 — {c.status_updated_by}
 </span>
 )}
 </p>
 )}
 {c.context && (
 <p className="text-[11px] text-foreground/45 mt-2 leading-snug">
 <span className="text-foreground/30 uppercase tracking-[0.18em] text-[9px] mr-1.5">
 Context
 </span>
 {c.context}
 </p>
 )}
 </li>
 );
 })}
 </ul>
 </main>
 </div>
 );
}

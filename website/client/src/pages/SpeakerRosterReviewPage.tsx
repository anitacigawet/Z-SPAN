/**
 * SpeakerRosterReviewPage — Phase 2 D-Build-B operator review surface.
 *
 * Lists `meeting_speaker_roster` rows where Sonnet's cluster→canonical
 * mapping landed in `pending_review` (either prong failed, or Sonnet
 * couldn't infer at all). The operator confirms / overrides / leaves
 * anonymous; auto-promoted rows don't appear here — the two-prong gate
 * already cleared them.
 *
 * V1.5 follow-up (per architecture spec.md § 5.5 — V1.5-Audit-Deeplink-1):
 * each row will gain an ⓘ icon that deep-links into the audit page
 * with the cluster_roster_mapper's Sonnet run_id prefilled. This lets
 * any reviewer verify the proposal's provenance (which opening-chunk
 * vector_ids Sonnet saw, what prompt template version, what model)
 * before confirming/overriding. Currently no audit hooks because the
 * verify endpoint doesn't exist yet (V1.5-Verify-1 ships first).
 *
 * Design principle (mirrors DisputedQuotesPage 2026-05-26): scannable
 * decisions, not a database-row dump. Each row reads as:
 * - One line of plain language: which meeting + which cluster
 * - The verbatim evidence snippet Sonnet pulled from the opening chunks
 * - Prong reasoning chips (color-coded)
 * - Three actions: Confirm (when Sonnet proposed something), Override
 * (picker dropdown of the city's roster), Leave anonymous
 *
 * The override picker takes a non-canonical free-form override if the
 * operator types one — the API endpoint accepts it for the rare case a
 * roster is stale. Default mode is dropdown to prevent typos.
 */
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, Check, X, UserCircle, AlertTriangle, Volume2 } from "lucide-react";
import MeetingExcerptPlayer from "../components/MeetingExcerptPlayer";

interface SpeakerRosterReviewPageProps {
 onBack: () => void;
 onNavigate?: (view: string, params?: Record<string, unknown>) => void;
}

type ClusterExcerpt = {
 start_seconds: number;
 end_seconds: number;
 duration_seconds: number;
 text: string;
 chunk_index: number;
};

type ClusterSamplesPayload = {
 row_id: number;
 meeting_id: number;
 cluster_label: string;
 video_url: string | null;
 excerpts: ClusterExcerpt[];
};

function formatTimecode(s: number): string {
 if (!Number.isFinite(s) || s < 0) return "—";
 const total = Math.floor(s);
 const h = Math.floor(total / 3600);
 const m = Math.floor((total % 3600) / 60);
 const sec = total % 60;
 const pad = (n: number) => String(n).padStart(2, "0");
 return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

type RosterRow = {
 id: number;
 meeting_id: number;
 meeting_title: string;
 meeting_date: string;
 city_name: string;
 cluster_label: string;
 proposed_canonical: string | null;
 evidence_text: string | null;
 prong_1_passed: number | null;
 prong_1_reasoning: string | null;
 prong_2_passed: number | null;
 prong_2_reasoning: string | null;
 status: string;
 model_id: string | null;
 created_at: string;
};

type ListPayload = {
 count: number;
 rows: RosterRow[];
};

type CouncilMember = {
 name: string;
 role: string | null;
 seat_id: string | null;
};

type MeetingDetailPayload = {
 meeting_id: number;
 meeting_title: string;
 meeting_date: string;
 city_name: string;
 roster: RosterRow[];
 council_members: CouncilMember[];
};

function formatMeetingDate(s: string | null | undefined): string {
 if (!s) return "—";
 const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s + "T00:00:00") : new Date(s);
 if (isNaN(d.getTime())) return s;
 return d.toLocaleDateString("en-US", {
 month: "short", day: "numeric", year: "numeric",
 });
}

function ProngChip({
 passed, label, reasoning,
}: { passed: number | null; label: string; reasoning: string | null }) {
 const isPass = passed === 1;
 const isFail = passed === 0;
 const bg = isPass
 ? "rgba(34, 197, 94, 0.12)"
 : isFail ? "rgba(239, 68, 68, 0.12)" : "rgba(148, 163, 184, 0.08)";
 const fg = isPass
 ? "rgb(74, 222, 128)"
 : isFail ? "rgb(252, 165, 165)" : "rgb(148, 163, 184)";
 return (
 <span
 title={reasoning || ""}
 style={{
 display: "inline-flex", alignItems: "center", gap: 4,
 fontSize: 11, padding: "2px 7px", borderRadius: 4,
 background: bg, color: fg,
 border: `1px solid ${fg}33`,
 fontFamily: "ui-monospace, SFMono-Regular, monospace",
 }}
 >
 {label}: {isPass ? "PASS" : isFail ? "FAIL" : "—"}
 </span>
 );
}

export default function SpeakerRosterReviewPage({ onBack }: SpeakerRosterReviewPageProps) {
 const [payload, setPayload] = useState<ListPayload | null>(null);
 const [loading, setLoading] = useState(false);
 const [error, setError] = useState<string | null>(null);
 const [busyId, setBusyId] = useState<number | null>(null);

 // Per-row override picker state (lazy-load council_members for the
 // row's meeting on first picker open). Keyed by row.id → meeting payload.
 const [meetingDetails, setMeetingDetails] = useState<Record<number, MeetingDetailPayload | null>>({});
 const [overrideOpen, setOverrideOpen] = useState<Record<number, boolean>>({});
 const [overrideValue, setOverrideValue] = useState<Record<number, string>>({});

 // Per-row cluster excerpts — fetched once per row.id when the queue
 // loads. Keyed by row.id → either the payload, "loading", or "error"
 // so the UI can render a loading shimmer + a graceful failure note
 // without making either case look like honest-empty.
 const [clusterSamples, setClusterSamples] = useState<
 Record<number, ClusterSamplesPayload | "loading" | "error">
 >({});

 // Active inline player — one across the whole page so we never have two
 // iframes humming at once. Identifies the (row.id, excerpt index) pair
 // that owns the currently-mounted MeetingExcerptPlayer; null = no
 // player active.
 const [activePlayer, setActivePlayer] = useState<{ rowId: number; excerptIndex: number } | null>(null);

 const fetchQueue = () => {
 setLoading(true);
 setError(null);
 fetch("/api/speaker-roster/pending-review")
 .then(async r => {
 if (!r.ok) throw new Error(`HTTP ${r.status}`);
 return r.json();
 })
 .then((data: ListPayload) => setPayload(data))
 .catch(e => setError(e?.message ?? String(e)))
 .finally(() => setLoading(false));
 };

 useEffect(() => { fetchQueue(); }, []);

 // Lazy-load excerpts for every visible row whenever the queue updates.
 // Each row's fetch is independent; failures land in `clusterSamples`
 // as "error" so the UI can render an honest fallback instead of a
 // permanent loading state.
 useEffect(() => {
 if (!payload?.rows.length) return;
 for (const row of payload.rows) {
 // Skip rows we've already fetched (success OR terminal error) —
 // refresh is operator-driven via the Refresh button.
 if (clusterSamples[row.id] !== undefined && clusterSamples[row.id] !== "loading") {
 continue;
 }
 setClusterSamples(prev => ({ ...prev, [row.id]: "loading" }));
 fetch(`/api/speaker-roster/${row.id}/cluster-samples`)
 .then(async r => {
 if (!r.ok) throw new Error(`HTTP ${r.status}`);
 return r.json() as Promise<ClusterSamplesPayload>;
 })
 .then(data => setClusterSamples(prev => ({ ...prev, [row.id]: data })))
 .catch(() => setClusterSamples(prev => ({ ...prev, [row.id]: "error" })));
 }
 // eslint-disable-next-line react-hooks/exhaustive-deps
 }, [payload]);

 const loadMeetingDetail = async (row: RosterRow) => {
 if (meetingDetails[row.meeting_id]) return;
 try {
 const r = await fetch(`/api/speaker-roster/meeting/${row.meeting_id}`);
 if (!r.ok) throw new Error(`HTTP ${r.status}`);
 const data: MeetingDetailPayload = await r.json();
 setMeetingDetails(prev => ({ ...prev, [row.meeting_id]: data }));
 } catch (e: any) {
 setError(`Could not load roster for meeting ${row.meeting_id}: ${e?.message ?? e}`);
 }
 };

 const callAction = async (
 row: RosterRow, action: "confirm" | "override" | "anonymous", confirmedCanonical?: string,
 ) => {
 if (busyId !== null) return;
 setBusyId(row.id);
 try {
 const body: Record<string, unknown> = { resolved_by: "operator" };
 if (action === "override") {
 if (!confirmedCanonical?.trim()) {
 throw new Error("Pick a canonical name to override with");
 }
 body.confirmed_canonical = confirmedCanonical.trim();
 }
 const r = await fetch(`/api/speaker-roster/${row.id}/${action}`, {
 method: "POST",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify(body),
 });
 const data = await r.json().catch(() => null);
 if (!r.ok || !data?.ok) {
 throw new Error(data?.error ?? `HTTP ${r.status}`);
 }
 fetchQueue();
 // Close picker if it was open
 setOverrideOpen(prev => ({ ...prev, [row.id]: false }));
 } catch (e: any) {
 setError(`Action failed for #${row.id}: ${e?.message ?? e}`);
 } finally {
 setBusyId(null);
 }
 };

 const meetingsRepresented = useMemo(() => {
 if (!payload?.rows) return 0;
 const set = new Set(payload.rows.map(r => r.meeting_id));
 return set.size;
 }, [payload]);

 return (
 <div
 style={{
 minHeight: "100vh",
 background: "var(--bg-primary, #0a0e14)",
 color: "var(--text-primary, #e6e1cf)",
 padding: "32px 24px 80px",
 fontFamily: "Inter, -apple-system, BlinkMacSystemFont, sans-serif",
 }}
 >
 <div style={{ maxWidth: 920, margin: "0 auto" }}>
 {/* Header */}
 <div style={{ marginBottom: 24 }}>
 <button
 onClick={onBack}
 style={{
 display: "inline-flex", alignItems: "center", gap: 6,
 background: "transparent", border: "none", color: "var(--text-secondary, #8b95a8)",
 fontSize: 13, cursor: "pointer", padding: "4px 0", marginBottom: 16,
 }}
 >
 <ArrowLeft size={14} />
 Back
 </button>
 <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
 <UserCircle size={22} style={{ color: "var(--accent-blue, #4a90e2)" }} />
 <h1 style={{ fontSize: 22, fontWeight: 600, margin: 0 }}>
 Speaker Roster Review
 </h1>
 </div>
 <p style={{
 fontSize: 14, color: "var(--text-secondary, #8b95a8)",
 margin: "8px 0 0", lineHeight: 1.5,
 }}>
 Each row is a speaker cluster pyannote detected in a meeting where Sonnet either
 couldn&rsquo;t infer the canonical council member with confidence, or the two-prong
 safety gate (anchor evidence + last-name specificity) didn&rsquo;t both pass. Click
 <em> Listen</em> on any voice excerpt to play that segment inline (the player mounts
 below the quote &mdash; no page switch); then <strong>Confirm</strong> Sonnet&rsquo;s
 proposal, <strong>Override</strong> with the right roster member, or
 <strong> Leave anonymous</strong> to render as &ldquo;Speaker N&rdquo;.
 </p>
 </div>

 {/* Status row */}
 <div style={{
 display: "flex", justifyContent: "space-between", alignItems: "center",
 fontSize: 12, color: "var(--text-secondary, #8b95a8)",
 marginBottom: 16, paddingBottom: 12,
 borderBottom: "1px solid rgba(148, 163, 184, 0.12)",
 }}>
 <div>
 {loading ? "Loading..." :
 payload ? `${payload.count} pending across ${meetingsRepresented} meeting${meetingsRepresented === 1 ? "" : "s"}` :
 "—"}
 </div>
 <button
 onClick={fetchQueue}
 disabled={loading}
 style={{
 background: "transparent",
 border: "1px solid rgba(148, 163, 184, 0.2)",
 color: "var(--text-secondary, #8b95a8)",
 fontSize: 11, padding: "4px 10px", borderRadius: 4,
 cursor: loading ? "wait" : "pointer",
 }}
 >
 Refresh
 </button>
 </div>

 {error && (
 <div style={{
 display: "flex", alignItems: "center", gap: 8,
 background: "rgba(239, 68, 68, 0.08)",
 border: "1px solid rgba(239, 68, 68, 0.25)",
 color: "rgb(252, 165, 165)",
 fontSize: 13, padding: "10px 14px", borderRadius: 6, marginBottom: 16,
 }}>
 <AlertTriangle size={14} />
 {error}
 </div>
 )}

 {/* Rows */}
 {!loading && payload && payload.count === 0 && (
 <div style={{
 textAlign: "center", padding: "48px 0",
 color: "var(--text-secondary, #8b95a8)",
 fontSize: 14,
 }}>
 No pending speaker roster reviews. Auto-promoted mappings landed clean.
 </div>
 )}

 {payload?.rows.map((row) => {
 const detail = meetingDetails[row.meeting_id];
 const pickerOpen = overrideOpen[row.id];
 const pickerValue = overrideValue[row.id] ?? "";
 const isBusy = busyId === row.id;
 return (
 <div
 key={row.id}
 style={{
 border: "1px solid rgba(148, 163, 184, 0.15)",
 borderRadius: 8,
 padding: "16px 18px",
 marginBottom: 14,
 background: "rgba(148, 163, 184, 0.03)",
 }}
 >
 {/* Top line: meeting + cluster */}
 <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
 <div>
 <div style={{ fontSize: 14, fontWeight: 500 }}>
 {row.city_name} · {formatMeetingDate(row.meeting_date)}
 </div>
 <div style={{ fontSize: 12, color: "var(--text-secondary, #8b95a8)" }}>
 {row.meeting_title}
 </div>
 </div>
 <div style={{
 fontSize: 11, fontFamily: "ui-monospace, SFMono-Regular, monospace",
 background: "rgba(74, 144, 226, 0.12)", color: "rgb(96, 165, 250)",
 padding: "3px 8px", borderRadius: 4, alignSelf: "flex-start",
 border: "1px solid rgba(74, 144, 226, 0.3)",
 }}>
 {row.cluster_label}
 </div>
 </div>

 {/* Proposed canonical */}
 <div style={{ marginBottom: 10 }}>
 <div style={{ fontSize: 12, color: "var(--text-secondary, #8b95a8)", marginBottom: 4 }}>
 Sonnet proposed:
 </div>
 {row.proposed_canonical ? (
 <div style={{ fontSize: 15, fontWeight: 500 }}>
 {row.proposed_canonical}
 </div>
 ) : (
 <div style={{
 fontSize: 13, color: "rgb(252, 165, 165)",
 lineHeight: 1.5,
 }}>
 Sonnet didn&rsquo;t see this speaker in the meeting&rsquo;s opening
 minutes &mdash; introductions, gavel, roll call &mdash; so there&rsquo;s
 no LLM-attributed name to verify. Listen to one of the excerpts below
 to identify them by voice, then <strong>Override</strong> with the
 right roster member, or <strong>Leave anonymous</strong> if they
 weren&rsquo;t a councilmember.
 </div>
 )}
 </div>

 {/* Cluster samples — operator-listen surface (replaces the old
 one-line evidence_text block; carries it plus more, with
 timestamps + deep-link). Falls back to evidence_text when
 excerpts are unavailable (meeting not indexed, etc.). */}
 {(() => {
 const samples = clusterSamples[row.id];
 if (samples === "loading" || samples === undefined) {
 return (
 <div style={{
 marginBottom: 10, fontSize: 12,
 color: "var(--text-secondary, #8b95a8)",
 fontStyle: "italic",
 }}>
 Loading voice excerpts&hellip;
 </div>
 );
 }
 if (samples === "error" || samples.excerpts.length === 0) {
 // Fall back to the legacy single-line evidence_text when
 // the per-cluster sample endpoint can't deliver (meeting
 // not indexed to Qdrant, or no qualifying turn ≥2s).
 if (!row.evidence_text) return null;
 return (
 <div style={{ marginBottom: 10 }}>
 <div style={{ fontSize: 12, color: "var(--text-secondary, #8b95a8)", marginBottom: 4 }}>
 Evidence:
 </div>
 <blockquote style={{
 margin: 0, padding: "8px 12px",
 background: "rgba(0, 0, 0, 0.2)",
 borderLeft: "3px solid rgba(148, 163, 184, 0.3)",
 fontSize: 13, fontStyle: "italic", lineHeight: 1.45,
 borderRadius: 3,
 }}>
 &ldquo;{row.evidence_text}&rdquo;
 </blockquote>
 {samples === "error" && (
 <div style={{
 marginTop: 4, fontSize: 11,
 color: "var(--text-secondary, #8b95a8)",
 fontStyle: "italic",
 }}>
 Could not load timestamped excerpts &mdash; showing
 Sonnet&rsquo;s saved evidence instead.
 </div>
 )}
 </div>
 );
 }
 return (
 <div style={{ marginBottom: 10 }}>
 <div style={{
 display: "flex", alignItems: "center", gap: 6,
 fontSize: 12, color: "var(--text-secondary, #8b95a8)",
 marginBottom: 6,
 }}>
 <Volume2 size={12} />
 Voice excerpts &mdash; click <em>Listen</em> to play the segment inline.
 </div>
 <div style={{ display: "grid", gap: 6 }}>
 {samples.excerpts.map((ex, i) => {
 const isPlayerActive =
 activePlayer?.rowId === row.id &&
 activePlayer.excerptIndex === i;
 return (
 <div
 key={i}
 style={{
 display: "flex", flexWrap: "wrap", alignItems: "stretch",
 gap: 0,
 background: "rgba(0, 0, 0, 0.2)",
 border: "1px solid rgba(148, 163, 184, 0.12)",
 borderRadius: 4,
 overflow: "hidden",
 }}
 >
 <blockquote style={{
 margin: 0, padding: "8px 12px",
 borderLeft: "3px solid rgba(148, 163, 184, 0.3)",
 fontSize: 13, fontStyle: "italic", lineHeight: 1.45,
 flex: 1, minWidth: 0,
 }}>
 <div style={{
 fontFamily: "ui-monospace, SFMono-Regular, monospace",
 fontStyle: "normal", fontSize: 11,
 color: "var(--text-secondary, #8b95a8)",
 marginBottom: 4,
 }}>
 {formatTimecode(ex.start_seconds)} &middot; {Math.round(ex.duration_seconds)}s
 </div>
 &ldquo;{ex.text}&rdquo;
 </blockquote>
 <MeetingExcerptPlayer
 videoUrl={samples.video_url}
 startSeconds={ex.start_seconds}
 endSeconds={ex.end_seconds}
 isActive={isPlayerActive}
 onActivate={() => setActivePlayer({ rowId: row.id, excerptIndex: i })}
 onDeactivate={() => setActivePlayer(prev =>
 prev?.rowId === row.id && prev.excerptIndex === i ? null : prev,
 )}
 />
 </div>
 );
 })}
 </div>
 </div>
 );
 })()}

 {/* Prong chips */}
 <div style={{ display: "flex", gap: 6, marginBottom: 14 }}>
 <ProngChip
 passed={row.prong_1_passed}
 label="Anchor"
 reasoning={row.prong_1_reasoning}
 />
 <ProngChip
 passed={row.prong_2_passed}
 label="Specificity"
 reasoning={row.prong_2_reasoning}
 />
 </div>

 {/* Actions */}
 <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
 {row.proposed_canonical && (
 <button
 onClick={() => callAction(row, "confirm")}
 disabled={isBusy}
 style={{
 display: "inline-flex", alignItems: "center", gap: 5,
 background: "rgba(34, 197, 94, 0.15)",
 color: "rgb(74, 222, 128)",
 border: "1px solid rgba(34, 197, 94, 0.35)",
 fontSize: 13, padding: "6px 14px", borderRadius: 5,
 cursor: isBusy ? "wait" : "pointer", fontWeight: 500,
 }}
 >
 <Check size={13} /> Confirm
 </button>
 )}
 <button
 onClick={() => {
 loadMeetingDetail(row);
 setOverrideOpen(prev => ({ ...prev, [row.id]: !pickerOpen }));
 }}
 disabled={isBusy}
 style={{
 background: "rgba(74, 144, 226, 0.12)",
 color: "rgb(96, 165, 250)",
 border: "1px solid rgba(74, 144, 226, 0.3)",
 fontSize: 13, padding: "6px 14px", borderRadius: 5,
 cursor: isBusy ? "wait" : "pointer",
 }}
 >
 {pickerOpen ? "Cancel override" : "Override"}
 </button>
 <button
 onClick={() => callAction(row, "anonymous")}
 disabled={isBusy}
 style={{
 background: "transparent",
 color: "var(--text-secondary, #8b95a8)",
 border: "1px solid rgba(148, 163, 184, 0.2)",
 fontSize: 13, padding: "6px 14px", borderRadius: 5,
 cursor: isBusy ? "wait" : "pointer",
 }}
 >
 <X size={13} style={{ marginRight: 5 }} />
 Leave anonymous
 </button>
 </div>

 {/* Override picker */}
 {pickerOpen && (
 <div style={{
 marginTop: 12, padding: 12,
 background: "rgba(0, 0, 0, 0.2)",
 borderRadius: 5,
 border: "1px solid rgba(148, 163, 184, 0.12)",
 }}>
 <div style={{ fontSize: 12, color: "var(--text-secondary, #8b95a8)", marginBottom: 8 }}>
 Pick the canonical roster member for {row.cluster_label}:
 </div>
 <select
 value={pickerValue}
 onChange={(e) => setOverrideValue(prev => ({ ...prev, [row.id]: e.target.value }))}
 style={{
 width: "100%", padding: "6px 10px",
 background: "rgba(0, 0, 0, 0.3)",
 color: "var(--text-primary, #e6e1cf)",
 border: "1px solid rgba(148, 163, 184, 0.25)",
 borderRadius: 4, fontSize: 13,
 marginBottom: 8,
 }}
 >
 <option value="">— select council member —</option>
 {detail?.council_members.map(m => (
 <option key={m.name} value={m.name}>
 {m.name} {m.role ? `(${m.role})` : ""}
 </option>
 ))}
 </select>
 <button
 onClick={() => callAction(row, "override", pickerValue)}
 disabled={!pickerValue || isBusy}
 style={{
 background: pickerValue ? "rgba(34, 197, 94, 0.15)" : "transparent",
 color: pickerValue ? "rgb(74, 222, 128)" : "rgb(100, 116, 139)",
 border: `1px solid ${pickerValue ? "rgba(34, 197, 94, 0.35)" : "rgba(148, 163, 184, 0.2)"}`,
 fontSize: 13, padding: "6px 14px", borderRadius: 5,
 cursor: pickerValue && !isBusy ? "pointer" : "not-allowed",
 fontWeight: 500,
 }}
 >
 Save override
 </button>
 </div>
 )}
 </div>
 );
 })}

 {/* Footer help */}
 <div style={{
 marginTop: 32, padding: "16px 18px",
 background: "rgba(148, 163, 184, 0.05)",
 borderRadius: 6,
 fontSize: 12, color: "var(--text-secondary, #8b95a8)",
 lineHeight: 1.6,
 }}>
 <strong style={{ color: "var(--text-primary, #e6e1cf)" }}>How this queue works:</strong>
 <br />
 Phase 2 diarization assigns anonymous cluster labels (SPEAKER_00,
 SPEAKER_01, ...) to each speaker pyannote detects. The
 <code style={{ background: "rgba(0, 0, 0, 0.25)", padding: "1px 5px", borderRadius: 3, margin: "0 3px" }}>
 cluster_roster_mapper
 </code>
 Sonnet pass tries to attribute each cluster to a canonical
 <code style={{ background: "rgba(0, 0, 0, 0.25)", padding: "1px 5px", borderRadius: 3, margin: "0 3px" }}>
 council_members
 </code>
 row using the meeting&rsquo;s opening minutes (gavel, roll call, introductions). Mappings
 where BOTH the <em>Anchor</em> prong (role + last-name phrase present in evidence) AND
 the <em>Specificity</em> prong (unique last-name match in the roster) pass get
 auto-promoted and don&rsquo;t appear here. Everything else lands in this queue,
 including speakers who introduce themselves mid-meeting (food-bank operators, business
 owners during public comment, late-arriving councilmembers) &mdash; Sonnet only sees
 opening chunks, so those need voice-identification by the operator.
 </div>
 </div>
 </div>
 );
}

/**
 * Truth Book Lite Layer 1) — per-person research surface.
 *
 * Chunk 2 (the page shell): fetches the assembled record and renders a plain
 * chronological list per topic + the tracked-claims layer, proving the data
 * flows end-to-end. Chunk 3 adds the swimlane timeline (the visual centerpiece)
 * above the lists. Drill-down karaoke + accountability arcs are chunks 4-5.
 * Labels are human sentences,
 * never schema field names.
 */
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, X } from "lucide-react";
import {
 fetchTruthBook,
 fetchMemberRag,
 formatChunkTimecode,
 totalLaneEntries,
 type TruthBookResponse,
 type TruthBookQuoteEntry,
 type TruthBookClaimEntry,
 type MemberRagResponse,
 type MemberRagChunk,
} from "../utils/truthBook";
import { verificationBadgeFor } from "../utils/unifiedQuotes";
import {
 TRACKED_CLAIM_STATUS_DISPLAY,
 TRACKED_CLAIM_TYPE_DISPLAY,
 formatTimeHorizon,
 type TrackedClaimStatus,
} from "../utils/trackedClaims";
import SyncedQuote, { type QuoteWordTiming } from "../components/SyncedQuote";
import GameplanBoard from "../components/truthbook/GameplanBoard";

interface TruthBookPageProps {
 cityName: string;
 seatId: string;
 onBack: () => void;
 onOpenMeeting?: (meetingId: number) => void;
 /** Chunk 6: arriving from a Cast-page per-topic link scrolls + flashes that
 * topic's section. A controlled vocabulary topic id (e.g. "water_rights"). */
 focusTopic?: string;
}

// A clicked timeline marker: a lane's date-cluster of quotes, or a single
// tracked claim. Drives the chunk-4 drill-down panel.
type TimelineSelection =
 | { kind: "quotes"; label: string; date: string; items: TruthBookQuoteEntry[] }
 | { kind: "claim"; claim: TruthBookClaimEntry };

type ClaimsView = "context" | "lane";

function formatDate(iso: string | null): string {
 if (!iso) return "";
 // meeting_date / time_range are date-only (YYYY-MM-DD); pin to local noon
 // so the displayed day doesn't shift across timezones.
 const d = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
 if (Number.isNaN(d.getTime())) return iso;
 return d.toLocaleDateString("en-US", {
 month: "short",
 day: "numeric",
 year: "numeric",
 });
}

function termLine(m: TruthBookResponse["member"]): string | null {
 const start = m.term_started ? new Date(m.term_started).getFullYear() : NaN;
 const end = m.term_ends ? new Date(m.term_ends).getFullYear() : NaN;
 if (!Number.isNaN(start) && !Number.isNaN(end)) return `Term ${start}–${end}`;
 if (!Number.isNaN(start)) return `Term began ${start}`;
 return null;
}

export default function TruthBookPage({
 cityName,
 seatId,
 onBack,
 onOpenMeeting,
 focusTopic,
}: TruthBookPageProps) {
 const [data, setData] = useState<TruthBookResponse | null>(null);
 const [loading, setLoading] = useState(true);
 const [error, setError] = useState<string | null>(null);
 const [selected, setSelected] = useState<TimelineSelection | null>(null);
 // Chunk 5: tracked claims interleaved into their topic lanes (default,
 // claim-in-context) vs. collapsed into one dedicated accountability lane.
 // Pure view state — the endpoint already returns claims with their tags.
 const [claimsView, setClaimsView] = useState<ClaimsView>("context");
 const [flashTopic, setFlashTopic] = useState<string | null>(null);
 // C4 V3-preview: per-topic lazy-loaded RAG retrieval state.
 // "idle" before the operator clicks Browse; "loading" while the fetch is
 // in flight; the response object once results land; an error object if
 // the call failed (Surface Pro unreachable, no aliases match, etc).
 type LaneRagState =
 | { kind: "idle" }
 | { kind: "loading" }
 | { kind: "ready"; data: MemberRagResponse }
 | { kind: "error"; message: string };
 const [ragByTopic, setRagByTopic] = useState<Record<string, LaneRagState>>(
 {},
 );

 const loadLaneRag = (topic: string) => {
 setRagByTopic(prev => ({ ...prev, [topic]: { kind: "loading" } }));
 fetchMemberRag(cityName, seatId, topic)
 .then(d =>
 setRagByTopic(prev => ({ ...prev, [topic]: { kind: "ready", data: d } })),
 )
 .catch(e =>
 setRagByTopic(prev => ({
 ...prev,
 [topic]: {
 kind: "error",
 message: e?.message ?? "Failed to retrieve",
 },
 })),
 );
 };

 useEffect(() => {
 let cancelled = false;
 const ctrl = new AbortController();
 setLoading(true);
 setError(null);
 setData(null);
 setSelected(null);
 fetchTruthBook(cityName, seatId, { signal: ctrl.signal })
 .then(d => {
 if (!cancelled) setData(d);
 })
 .catch(e => {
 if (!cancelled && e?.name !== "AbortError") {
 setError(e?.message ?? "Failed to load");
 }
 })
 .finally(() => {
 if (!cancelled) setLoading(false);
 });
 return () => {
 cancelled = true;
 ctrl.abort();
 };
 }, [cityName, seatId]);

 // Chunk 6 deep-link: arriving with a focus topic (from a Cast-page "in the
 // Record" link) scrolls that topic's section into view and briefly flashes it.
 useEffect(() => {
 if (!data || !focusTopic) return;
 const el = document.getElementById(`tb-lane-${focusTopic}`);
 if (!el) return;
 const reduce =
 window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
 el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
 setFlashTopic(focusTopic);
 const t = window.setTimeout(() => setFlashTopic(null), 1800);
 return () => window.clearTimeout(t);
 }, [data, focusTopic]);

 const member = data?.member;
 const totalQuotes = data ? totalLaneEntries(data.lanes) : 0;
 const totalClaims = data?.claims.length ?? 0;
 const hasAnything = totalQuotes > 0 || totalClaims > 0;
 const firstName = member?.name?.split(" ")[0] ?? "this member";

 return (
 <div className="min-h-screen bg-background text-white">
 <div className="mx-auto max-w-4xl px-6 py-8">
 <button
 onClick={onBack}
 className="mb-6 inline-flex items-center gap-2 text-sm text-white/60 transition-colors hover:text-white"
 >
 <ArrowLeft size={16} /> Back
 </button>

 {loading && (
 <div className="py-24 text-center text-white/50">Loading the record…</div>
 )}

 {error && !loading && (
 <div className="rounded-lg border border-[rgba(248,113,113,0.4)] bg-[rgba(248,113,113,0.08)] px-5 py-4 text-sm text-[#FCA5A5]">
 Couldn{"’"}t load this member{"’"}s record: {error}
 </div>
 )}

 {data && !loading && member && (
 <>
 <header className="mb-10 border-b border-[var(--line)] pb-6">
 <p className="mb-2 text-xs uppercase tracking-[0.18em] text-[#3B82F6]">
 Truth Book {"·"} {data.city}
 </p>
 <h1 className="text-3xl font-semibold">{member.name}</h1>
 <p className="mt-1 text-white/60">
 {[member.role, termLine(member)].filter(Boolean).join(" · ")}
 </p>
 <p className="mt-4 max-w-2xl text-sm leading-relaxed text-white/55">
 Everything Z-SPAN has on record for {firstName} {"—"} every publicly
 verified quote and tracked commitment, organized by topic.
 </p>
 <p className="mt-3 text-sm text-white/45">
 {hasAnything ? (
 <>
 {totalQuotes} quote{totalQuotes === 1 ? "" : "s"} {"·"} {totalClaims}{" "}
 tracked claim{totalClaims === 1 ? "" : "s"}
 {data.time_range.earliest && data.time_range.latest && (
 <>
 {" · "}
 {formatDate(data.time_range.earliest)} {"–"}{" "}
 {formatDate(data.time_range.latest)}
 </>
 )}
 </>
 ) : (
 "No public record on file yet."
 )}
 </p>
 </header>

 {/* The Tracking Board — the lead visualization (2026-07-03 v2,
 * operator-redirected same day): baseball-card stat line + the
 * two tracking fields (commitments being tracked · overt
 * stances) in the Madden diagram grammar. Fires the page's
 * TimelineSelection so the chunk-4 drill-down serves it.
 * The swimlane timeline is UNMOUNTED per operator ("the
 * timeline thing doesn't really make sense visually") —
 * TimelineSwimlanes stays on disk per the no-delete rule. */}
 <GameplanBoard data={data} onSelect={setSelected} />

 {selected && (
 <DrillDownPanel
 selection={selected}
 onClose={() => setSelected(null)}
 onOpenMeeting={onOpenMeeting}
 />
 )}

 <section className="space-y-8">
 {data.lanes.map(lane => (
 <div
 key={lane.topic}
 id={`tb-lane-${lane.topic}`}
 className={`scroll-mt-6 rounded-lg transition-shadow ${
 flashTopic === lane.topic
 ? "shadow-[0_0_0_1px_rgba(59,130,246,0.6)]"
 : ""
 }`}
 >
 <div className="mb-3 flex items-baseline justify-between">
 <h2 className="text-lg font-medium">{lane.label}</h2>
 <span className="text-xs text-white/40">
 {lane.entries.length === 0
 ? "—"
 : `${lane.entries.length} quote${lane.entries.length === 1 ? "" : "s"}`}
 </span>
 </div>
 {lane.entries.length === 0 ? (
 <p className="text-sm italic text-white/30">
 No quotes on this topic yet.
 </p>
 ) : (
 <ul className="space-y-3">
 {lane.entries.map(q => (
 <QuoteCard
 key={q.quote_id}
 q={q}
 onOpenMeeting={onOpenMeeting}
 />
 ))}
 </ul>
 )}

 <RagPreviewSection
 topic={lane.topic}
 topicLabel={lane.label}
 state={ragByTopic[lane.topic] ?? { kind: "idle" }}
 onLoad={() => loadLaneRag(lane.topic)}
 onOpenMeeting={onOpenMeeting}
 />
 </div>
 ))}
 </section>

 <section className="mt-12">
 <h2 className="mb-3 text-lg font-medium">Commitments &amp; claims</h2>
 {totalClaims === 0 ? (
 <p className="text-sm italic text-white/30">
 No tracked commitments on record yet.
 </p>
 ) : (
 <ul className="space-y-3">
 {data.claims.map(c => (
 <ClaimCard
 key={c.claim_id}
 c={c}
 onOpenMeeting={onOpenMeeting}
 />
 ))}
 </ul>
 )}
 </section>
 </>
 )}
 </div>
 </div>
 );
}

function QuoteCard({
 q,
 onOpenMeeting,
}: {
 q: TruthBookQuoteEntry;
 onOpenMeeting?: (meetingId: number) => void;
}) {
 const badge = verificationBadgeFor(q.verified_status);
 return (
 <li className="rounded-lg border border-[var(--line)] bg-[var(--surface-2)] px-4 py-3">
 <div className="mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
 <span className="text-white/50">{formatDate(q.meeting_date)}</span>
 {q.meeting_title && (
 <button
 onClick={() => onOpenMeeting?.(q.meeting_id)}
 className="text-[#3B82F6] hover:underline"
 >
 {q.meeting_title}
 </button>
 )}
 {q.is_broadcast_hero === 1 && (
 <span className="rounded-full border border-[rgba(245,165,36,0.4)] px-2 py-0.5 text-[10px] uppercase tracking-wide text-[#F5A524]">
 Featured
 </span>
 )}
 {badge && (
 <span
 className="rounded-full px-2 py-0.5 text-[10px] font-medium"
 style={{ color: badge.color, backgroundColor: badge.bgColor }}
 title={badge.tooltip}
 >
 {badge.label}
 </span>
 )}
 </div>
 <p className="text-sm leading-relaxed text-white/85">
 {"“"}
 {q.text}
 {"”"}
 </p>
 </li>
 );
}

function ClaimCard({
 c,
 onOpenMeeting,
}: {
 c: TruthBookClaimEntry;
 onOpenMeeting?: (meetingId: number) => void;
}) {
 const statusKey = (c.status ?? "active") as TrackedClaimStatus;
 const statusDisplay =
 TRACKED_CLAIM_STATUS_DISPLAY[statusKey] ?? TRACKED_CLAIM_STATUS_DISPLAY.active;
 const typeLabel = c.claim_type
 ? TRACKED_CLAIM_TYPE_DISPLAY[c.claim_type] ?? c.claim_type
 : null;
 return (
 <li className="rounded-lg border border-[var(--line)] bg-[var(--surface-2)] px-4 py-3">
 <div className="mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
 <span
 className="rounded-full border px-2 py-0.5 text-[10px] font-medium"
 style={{ color: statusDisplay.fg, borderColor: statusDisplay.border }}
 >
 {statusDisplay.label}
 </span>
 {typeLabel && <span className="text-white/50">{typeLabel}</span>}
 <span className="text-white/30">{"·"}</span>
 <span className="text-white/50">{formatDate(c.meeting_date)}</span>
 {c.meeting_title && (
 <button
 onClick={() => onOpenMeeting?.(c.meeting_id)}
 className="text-[#3B82F6] hover:underline"
 >
 {c.meeting_title}
 </button>
 )}
 </div>
 <p className="text-sm leading-relaxed text-white/85">
 {"“"}
 {c.claim_text}
 {"”"}
 </p>
 {c.expected_outcome && (
 <p className="mt-2 text-xs text-white/50">
 Expected: {c.expected_outcome} {"·"}{" "}
 {formatTimeHorizon(c.time_horizon_months)}
 </p>
 )}
 </li>
 );
}

// ── C4 V3-preview — per-topic RAG retrieval section ─────────────
//
// Operator-only at V1 per the App.tsx route-level gate (the page itself
// can't be reached by a non-owner — they get ViewerModeFallback). Renders
// underneath each topic lane: a "Browse transcript matches" affordance
// that lazy-loads chunks from /api/member-rag, then displays each chunk
// with its timecode + matched aliases + a deep-link to the broadcast page.
// The deep-link sets a `seekTo` query param the broadcast page can read
// to auto-position the player at the chunk's start_seconds (renders fine
// without it too — operator manually seeks).
//
// Retrieval-only: NO claude -p / Sonnet call on this path. Each result IS
// the chunk text as Whisper transcribed it, plus the karaoke timecode.
// The operator (or visitor, if V1-public is ever greenlit) listens to
// verify whether the member is speaking or being mentioned. No accountability
// claim or voting summary is synthesized.

function RagPreviewSection({
 topic,
 topicLabel,
 state,
 onLoad,
 onOpenMeeting,
}: {
 topic: string;
 topicLabel: string;
 state:
 | { kind: "idle" }
 | { kind: "loading" }
 | { kind: "ready"; data: MemberRagResponse }
 | { kind: "error"; message: string };
 onLoad: () => void;
 onOpenMeeting?: (meetingId: number) => void;
}) {
 if (state.kind === "idle") {
 return (
 <div className="mt-3 border-t border-[var(--line)] pt-3">
 <button
 type="button"
 onClick={onLoad}
 className="inline-flex items-center gap-1.5 text-[11px] text-[#F2A91C]/85 hover:text-[#F2A91C] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#F2A91C] rounded"
 title={`V3 preview · search the indexed transcripts for ${topicLabel} mentions of this member`}
 >
 <span className="inline-block h-1.5 w-1.5 rounded-full bg-[#F2A91C]/60" />
 Browse transcript matches (preview){" "}
 <span className="text-white/30">↗</span>
 </button>
 </div>
 );
 }
 if (state.kind === "loading") {
 return (
 <div className="mt-3 border-t border-[var(--line)] pt-3 text-[11px] text-white/40 italic">
 Searching the indexed transcripts…
 </div>
 );
 }
 if (state.kind === "error") {
 return (
 <div className="mt-3 border-t border-[var(--line)] pt-3">
 <p className="text-[11px] text-[#FCA5A5]">
 Couldn{"’"}t retrieve: {state.message}
 </p>
 <button
 type="button"
 onClick={onLoad}
 className="mt-1 text-[11px] text-[#3B82F6] hover:underline"
 >
 Try again
 </button>
 </div>
 );
 }
 // state.kind === "ready"
 const d = state.data;
 return (
 <div className="mt-3 border-t border-[#F2A91C]/20 pt-3">
 <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2 text-[11px]">
 <p className="text-[#F2A91C]/80">
 V3 preview · transcript matches
 </p>
 <p className="text-white/40 tabular-nums">
 {d.chunks_matched} of {d.chunks_retrieved} chunks matched across {d.meetings_queried} indexed meeting{d.meetings_queried === 1 ? "" : "s"}
 </p>
 </div>
 {d.results.length === 0 ? (
 <p className="text-[11px] italic text-white/30">
 {d.meetings_queried === 0
 ? "No indexed meetings for this city in Qdrant yet."
 : `No chunks in the indexed meetings mention ${d.member.name} for this topic.`}
 </p>
 ) : (
 <ul className="space-y-2">
 {d.results.map((chunk, i) => (
 <RagChunkCard
 key={`${chunk.meeting_id}-${chunk.chunk_index}-${i}`}
 chunk={chunk}
 onOpenMeeting={onOpenMeeting}
 />
 ))}
 </ul>
 )}
 {d.aliases.length > 0 && (
 <p className="mt-2 text-[10px] text-white/30">
 Filtered by aliases:{" "}
 <span className="font-mono">{d.aliases.slice(0, 6).join(" · ")}</span>
 {d.aliases.length > 6 && <span> · +{d.aliases.length - 6} more</span>}
 </p>
 )}
 </div>
 );
}

function RagChunkCard({
 chunk,
 onOpenMeeting,
}: {
 chunk: MemberRagChunk;
 onOpenMeeting?: (meetingId: number) => void;
}) {
 const tc = formatChunkTimecode(chunk.start_seconds);
 return (
 <li className="rounded-md border border-[#F2A91C]/15 bg-[#F2A91C]/[0.03] px-3 py-2">
 <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-white/40">
 <span className="text-[#F2A91C]/75">at {tc}</span>
 {chunk.meeting_title && (
 <button
 onClick={() => onOpenMeeting?.(chunk.meeting_id)}
 className="text-[#3B82F6] hover:underline"
 title="Open this meeting on the broadcast page"
 >
 {chunk.meeting_title}
 </button>
 )}
 {chunk.meeting_date && <span>{formatDate(chunk.meeting_date)}</span>}
 <span className="ml-auto tabular-nums text-white/30">
 score {chunk.score.toFixed(3)}
 </span>
 </div>
 <p className="text-[12px] leading-relaxed text-white/80">{chunk.body}</p>
 {chunk.matched_aliases.length > 0 && (
 <p className="mt-1 text-[10px] text-white/35">
 matched: {chunk.matched_aliases.join(", ")}
 </p>
 )}
 </li>
 );
}

// ── Chunk 4: drill-down panel ───────────────────────────────────────────
//
// Clicking a timeline marker opens this panel: the quote(s) or claim rendered
// with <SyncedQuote> karaoke (when word_timings + a meeting video URL exist),
// an audited citation (meeting + date + broadcast link + proof fingerprint),
// and the verification badge. Reuses the BroadcastPage highlighter marker.

function DrillDownPanel({
 selection,
 onClose,
 onOpenMeeting,
}: {
 selection: TimelineSelection;
 onClose: () => void;
 onOpenMeeting?: (meetingId: number) => void;
}) {
 // Only one karaoke player active at a time within the panel.
 const [activeKey, setActiveKey] = useState<string | null>(null);
 const closeRef = useRef<HTMLButtonElement | null>(null);
 // a11y: move focus into the inline panel when it opens, and close on Escape.
 // It's not a modal, so focus isn't trapped — just made keyboard-reachable.
 useEffect(() => {
 closeRef.current?.focus();
 }, []);
 useEffect(() => {
 const onKey = (e: KeyboardEvent) => {
 if (e.key === "Escape") onClose();
 };
 window.addEventListener("keydown", onKey);
 return () => window.removeEventListener("keydown", onKey);
 }, [onClose]);
 const heading =
 selection.kind === "claim"
 ? "Tracked commitment"
 : `${selection.label} · ${formatDate(selection.date)}`;
 return (
 <div
 role="region"
 aria-label={`Detail: ${heading}`}
 className="mb-12 rounded-lg border border-[#3B82F6]/30 bg-[var(--surface-2)] p-5"
 >
 <div className="mb-4 flex items-start justify-between gap-4">
 <h2 className="text-xs font-medium uppercase tracking-[0.15em] text-white/50">
 {heading}
 </h2>
 <button
 ref={closeRef}
 onClick={onClose}
 className="inline-flex shrink-0 items-center gap-1 rounded text-[10px] uppercase tracking-[0.18em] text-white/45 transition-colors hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-[#3B82F6]"
 aria-label="Close detail"
 >
 <X size={12} /> Close
 </button>
 </div>
 {selection.kind === "quotes" ? (
 <div className="space-y-6">
 {selection.items.map(q => (
 <QuoteDetail
 key={q.quote_id}
 q={q}
 active={activeKey === `q${q.quote_id}`}
 onActivate={() => setActiveKey(`q${q.quote_id}`)}
 onDeactivate={() => setActiveKey(null)}
 onOpenMeeting={onOpenMeeting}
 />
 ))}
 </div>
 ) : (
 <ClaimDetail
 c={selection.claim}
 active={activeKey === `c${selection.claim.claim_id}`}
 onActivate={() => setActiveKey(`c${selection.claim.claim_id}`)}
 onDeactivate={() => setActiveKey(null)}
 onOpenMeeting={onOpenMeeting}
 />
 )}
 </div>
 );
}

function Citation({
 meetingId,
 meetingTitle,
 meetingDate,
 sha256,
 onOpenMeeting,
}: {
 meetingId: number;
 meetingTitle: string | null;
 meetingDate: string | null;
 sha256?: string | null;
 onOpenMeeting?: (meetingId: number) => void;
}) {
 return (
 <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-[var(--line)] pt-3 text-[11px] text-white/45">
 <span>Source</span>
 <button
 onClick={() => onOpenMeeting?.(meetingId)}
 className="text-[#3B82F6] hover:underline"
 >
 {meetingTitle ?? `Meeting ${meetingId}`}
 {meetingDate && !(meetingTitle ?? "").includes(formatDate(meetingDate))
 ? ` · ${formatDate(meetingDate)}`
 : ""}{" "}
 {"↗"}
 </button>
 {sha256 && (
 <span
 className="font-mono text-white/35"
 title={`Proof clip SHA-256: ${sha256}`}
 >
 proof {sha256.slice(0, 10)}…
 </span>
 )}
 </div>
 );
}

function QuoteDetail({
 q,
 active,
 onActivate,
 onDeactivate,
 onOpenMeeting,
}: {
 q: TruthBookQuoteEntry;
 active: boolean;
 onActivate: () => void;
 onDeactivate: () => void;
 onOpenMeeting?: (meetingId: number) => void;
}) {
 const badge = verificationBadgeFor(q.verified_status);
 const canKaraoke =
 !!q.meeting_video_url && !!q.word_timings && q.word_timings.length > 0;
 return (
 <div>
 <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
 {q.is_broadcast_hero === 1 && (
 <span className="rounded-full border border-[rgba(245,165,36,0.4)] px-2 py-0.5 text-[10px] uppercase tracking-wide text-[#F5A524]">
 Featured
 </span>
 )}
 {badge && (
 <span
 className="rounded-full px-2 py-0.5 text-[10px] font-medium"
 style={{ color: badge.color, backgroundColor: badge.bgColor }}
 title={badge.tooltip}
 >
 {badge.label}
 </span>
 )}
 {q.speaker_role && <span className="text-white/45">{q.speaker_role}</span>}
 </div>
 {canKaraoke ? (
 <SyncedQuote
 wordTimings={q.word_timings as QuoteWordTiming[]}
 videoUrl={q.meeting_video_url as string}
 isActive={active}
 onActivate={onActivate}
 onDeactivate={onDeactivate}
 markerColor="#F2A91C"
 />
 ) : (
 <p className="text-sm italic leading-relaxed text-white/85">
 {"“"}
 {q.text}
 {"”"}
 </p>
 )}
 <Citation
 meetingId={q.meeting_id}
 meetingTitle={q.meeting_title}
 meetingDate={q.meeting_date}
 sha256={q.proof_clip_sha256}
 onOpenMeeting={onOpenMeeting}
 />
 </div>
 );
}

function ClaimDetail({
 c,
 active,
 onActivate,
 onDeactivate,
 onOpenMeeting,
}: {
 c: TruthBookClaimEntry;
 active: boolean;
 onActivate: () => void;
 onDeactivate: () => void;
 onOpenMeeting?: (meetingId: number) => void;
}) {
 const statusKey = (c.status ?? "active") as TrackedClaimStatus;
 const statusDisplay =
 TRACKED_CLAIM_STATUS_DISPLAY[statusKey] ?? TRACKED_CLAIM_STATUS_DISPLAY.active;
 const typeLabel = c.claim_type
 ? TRACKED_CLAIM_TYPE_DISPLAY[c.claim_type] ?? c.claim_type
 : null;
 const canKaraoke =
 !!c.meeting_video_url && !!c.word_timings && c.word_timings.length > 0;
 return (
 <div>
 <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
 <span
 className="rounded-full border px-2 py-0.5 text-[10px] font-medium"
 style={{ color: statusDisplay.fg, borderColor: statusDisplay.border }}
 >
 {statusDisplay.label}
 </span>
 {typeLabel && <span className="text-white/50">{typeLabel}</span>}
 </div>
 {canKaraoke ? (
 <SyncedQuote
 wordTimings={c.word_timings as QuoteWordTiming[]}
 videoUrl={c.meeting_video_url as string}
 isActive={active}
 onActivate={onActivate}
 onDeactivate={onDeactivate}
 markerColor="#F2A91C"
 />
 ) : (
 <p className="text-sm italic leading-relaxed text-white/85">
 {"“"}
 {c.claim_text}
 {"”"}
 </p>
 )}
 {c.expected_outcome && (
 <p className="mt-2 text-xs text-white/50">
 Expected: {c.expected_outcome} · {formatTimeHorizon(c.time_horizon_months)}
 </p>
 )}
 <Citation
 meetingId={c.meeting_id}
 meetingTitle={c.meeting_title}
 meetingDate={c.meeting_date}
 onOpenMeeting={onOpenMeeting}
 />
 </div>
 );
}

// ── Chunk 3: swimlane timeline (the visual centerpiece) ─────────────────
//
// One horizontal lane per topic + an Accountability lane for tracked claims,
// sharing a single date axis. Quote dots are positioned by meeting date
// (clustered per date with a count); claim markers are diamonds colored by
// status. A lane with no entries still renders so the absence is legible.
// Hover a marker for a quick peek; the full drill-down panel is chunk 4.
// Hidden on mobile — the chronological lists below are the fallback there.

const TIMELINE_LABEL_W = "8.5rem";

function datePct(iso: string | null, earliest: string, latest: string): number | null {
 if (!iso) return null;
 const norm = (s: string) => Date.parse(s.length === 10 ? `${s}T12:00:00` : s);
 const t = norm(iso);
 const t0 = norm(earliest);
 const t1 = norm(latest);
 if (Number.isNaN(t) || Number.isNaN(t0) || Number.isNaN(t1)) return null;
 if (t1 === t0) return 50;
 return Math.max(0, Math.min(100, ((t - t0) / (t1 - t0)) * 100));
}

function monthTicks(earliest: string, latest: string): { label: string; pct: number }[] {
 const norm = (s: string) => new Date(s.length === 10 ? `${s}T12:00:00` : s);
 const t0 = norm(earliest);
 const t1 = norm(latest);
 if (Number.isNaN(t0.getTime()) || Number.isNaN(t1.getTime())) return [];
 const span = t1.getTime() - t0.getTime() || 1;
 const ticks: { label: string; pct: number }[] = [];
 const cur = new Date(t0.getFullYear(), t0.getMonth(), 1);
 const end = new Date(t1.getFullYear(), t1.getMonth(), 1);
 let guard = 0;
 while (cur <= end && guard < 60) {
 ticks.push({
 label: cur.toLocaleDateString("en-US", { month: "short" }),
 pct: Math.max(0, Math.min(100, ((cur.getTime() - t0.getTime()) / span) * 100)),
 });
 cur.setMonth(cur.getMonth() + 1);
 guard++;
 }
 return ticks;
}

function groupByDate(
 entries: TruthBookQuoteEntry[],
): { date: string; items: TruthBookQuoteEntry[] }[] {
 const order: string[] = [];
 const byDate: Record<string, TruthBookQuoteEntry[]> = {};
 for (const e of entries) {
 const k = e.meeting_date ?? "unknown";
 if (!byDate[k]) {
 byDate[k] = [];
 order.push(k);
 }
 byDate[k].push(e);
 }
 return order.map(date => ({ date, items: byDate[date] }));
}

// meeting_id → date across every entry, so a claim's resolution
// (resolved_meeting_id) can be placed on the axis once the backend emits it.
function buildMeetingDateIndex(data: TruthBookResponse): Record<number, string> {
 const idx: Record<number, string> = {};
 for (const lane of data.lanes) {
 for (const e of lane.entries) {
 if (e.meeting_date && idx[e.meeting_id] == null) idx[e.meeting_id] = e.meeting_date;
 }
 }
 for (const c of data.claims) {
 if (c.meeting_date && idx[c.meeting_id] == null) idx[c.meeting_id] = c.meeting_date;
 }
 return idx;
}

// Interleaved mode: which claims belong in a given topic lane. A claim shows in
// every lane whose topic is in its tags; claims that match no featured topic
// fall to the "other" lane so they're never dropped.
function claimsForLane(
 laneTopic: string,
 claims: TruthBookClaimEntry[],
 laneTopics: Set<string>,
): TruthBookClaimEntry[] {
 return claims.filter(c => {
 const tags = c.topic_tags ?? [];
 if (tags.includes(laneTopic)) return true;
 if (laneTopic === "other" && !tags.some(t => laneTopics.has(t))) return true;
 return false;
 });
}

// Interleaved mode: claims that match no lane AND have no "other" lane to catch
// them — rendered in a residual row so the record stays complete.
function residualClaims(
 claims: TruthBookClaimEntry[],
 laneTopics: Set<string>,
): TruthBookClaimEntry[] {
 if (laneTopics.has("other")) return [];
 return claims.filter(c => !(c.topic_tags ?? []).some(t => laneTopics.has(t)));
}

function TimelineSwimlanes({
 data,
 claimsView,
 onChangeClaimsView,
 onSelect,
}: {
 data: TruthBookResponse;
 claimsView: ClaimsView;
 onChangeClaimsView: (v: ClaimsView) => void;
 onSelect: (s: TimelineSelection) => void;
}) {
 const { earliest, latest } = data.time_range;
 if (!earliest || !latest) return null;
 const ticks = monthTicks(earliest, latest);
 const meetingDateById = buildMeetingDateIndex(data);
 const laneTopics = new Set(data.lanes.map(l => l.topic));
 const residual =
 claimsView === "context" ? residualClaims(data.claims, laneTopics) : [];
 return (
 <div className="mb-12 hidden rounded-lg border border-[var(--line)] bg-[var(--surface-2)]/40 p-5 md:block">
 <div className="mb-4 flex items-center justify-between gap-4">
 <h2 className="text-xs font-medium uppercase tracking-[0.15em] text-white/50">
 Timeline
 </h2>
 <div className="flex items-center gap-4">
 <div className="hidden items-center gap-3 text-[10px] text-white/40 sm:flex">
 <span className="flex items-center gap-1.5">
 <span className="inline-block h-2 w-2 rounded-full bg-[#3B82F6]" /> quote
 </span>
 <span className="flex items-center gap-1.5">
 <span className="inline-block h-2 w-2 rotate-45 bg-[#F2A91C]" /> commitment
 </span>
 </div>
 {data.claims.length > 0 && (
 <div
 role="group"
 aria-label="Show commitments in their topic lanes or collected in one lane"
 className="inline-flex overflow-hidden rounded-md border border-[var(--line)] text-[10px]"
 >
 <button
 type="button"
 aria-pressed={claimsView === "context"}
 onClick={() => onChangeClaimsView("context")}
 className={
 claimsView === "context"
 ? "bg-[#F2A91C]/15 px-2.5 py-1 text-[#F2A91C]"
 : "px-2.5 py-1 text-white/45 transition-colors hover:text-white/70"
 }
 title="Show each commitment in its topic lane"
 >
 In context
 </button>
 <button
 type="button"
 aria-pressed={claimsView === "lane"}
 onClick={() => onChangeClaimsView("lane")}
 className={
 claimsView === "lane"
 ? "bg-[#F2A91C]/15 px-2.5 py-1 text-[#F2A91C]"
 : "px-2.5 py-1 text-white/45 transition-colors hover:text-white/70"
 }
 title="Collect all commitments into one lane"
 >
 Own lane
 </button>
 </div>
 )}
 </div>
 </div>

 <div className="relative mb-2 h-4" style={{ marginLeft: TIMELINE_LABEL_W }}>
 {ticks.map((t, i) => (
 <span
 key={i}
 className="absolute -translate-x-1/2 text-[10px] text-white/35"
 style={{ left: `${4 + t.pct * 0.92}%` }}
 >
 {t.label}
 </span>
 ))}
 </div>

 <div className="space-y-1.5">
 {data.lanes.map(lane => (
 <LaneRow
 key={lane.topic}
 label={lane.label}
 entries={lane.entries}
 claims={
 claimsView === "context"
 ? claimsForLane(lane.topic, data.claims, laneTopics)
 : []
 }
 meetingDateById={meetingDateById}
 earliest={earliest}
 latest={latest}
 onSelect={onSelect}
 />
 ))}
 {claimsView === "lane" && (
 <ClaimLaneRow
 label="Accountability"
 claims={data.claims}
 meetingDateById={meetingDateById}
 earliest={earliest}
 latest={latest}
 onSelect={onSelect}
 />
 )}
 {claimsView === "context" && residual.length > 0 && (
 <ClaimLaneRow
 label="Other commitments"
 claims={residual}
 meetingDateById={meetingDateById}
 earliest={earliest}
 latest={latest}
 onSelect={onSelect}
 />
 )}
 </div>
 </div>
 );
}

function LaneRow({
 label,
 entries,
 claims,
 meetingDateById,
 earliest,
 latest,
 onSelect,
}: {
 label: string;
 entries: TruthBookQuoteEntry[];
 claims: TruthBookClaimEntry[];
 meetingDateById: Record<number, string>;
 earliest: string;
 latest: string;
 onSelect: (s: TimelineSelection) => void;
}) {
 const groups = groupByDate(entries);
 const isEmpty = entries.length === 0 && claims.length === 0;
 return (
 <div className="flex items-center">
 <div
 className="shrink-0 truncate pr-3 text-xs text-white/55"
 style={{ width: TIMELINE_LABEL_W }}
 >
 {label}
 </div>
 <div className="relative h-6 flex-1 rounded bg-white/[0.03]">
 <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-white/[0.06]" />
 {isEmpty && (
 <span className="absolute left-2 top-1/2 -translate-y-1/2 text-[10px] italic text-white/20">
 none
 </span>
 )}
 {claims.map((c, i) => (
 <ClaimConnector
 key={`conn-${c.claim_id}-${i}`}
 c={c}
 meetingDateById={meetingDateById}
 earliest={earliest}
 latest={latest}
 />
 ))}
 {groups.map((g, i) => {
 const pct = datePct(g.date, earliest, latest);
 if (pct == null) return null;
 const snippet = (g.items[0].text ?? "").slice(0, 140);
 const tip = `${formatDate(g.date)} · ${g.items.length} quote${g.items.length === 1 ? "" : "s"}\n${snippet}`;
 return (
 <button
 key={`q-${i}`}
 type="button"
 title={tip}
 onClick={() =>
 onSelect({ kind: "quotes", label, date: g.date, items: g.items })
 }
 className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 cursor-pointer rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
 style={{ left: `${4 + pct * 0.92}%` }}
 aria-label={`${g.items.length} quote${g.items.length === 1 ? "" : "s"} on ${formatDate(g.date)}`}
 >
 <span className="block h-2.5 w-2.5 rounded-full bg-[#3B82F6] ring-2 ring-[#3B82F6]/20 transition-transform hover:scale-150" />
 {g.items.length > 1 && (
 <span className="absolute -right-2.5 -top-2 rounded-full bg-[#3B82F6] px-1 text-[8px] font-semibold leading-tight text-white">
 {g.items.length}
 </span>
 )}
 </button>
 );
 })}
 {claims.map((c, i) => (
 <ClaimMarker
 key={`cm-${c.claim_id}-${i}`}
 c={c}
 earliest={earliest}
 latest={latest}
 elevated
 onSelect={onSelect}
 />
 ))}
 </div>
 </div>
 );
}

function ClaimLaneRow({
 label,
 claims,
 meetingDateById,
 earliest,
 latest,
 onSelect,
}: {
 label: string;
 claims: TruthBookClaimEntry[];
 meetingDateById: Record<number, string>;
 earliest: string;
 latest: string;
 onSelect: (s: TimelineSelection) => void;
}) {
 return (
 <div className="flex items-center pt-1.5">
 <div
 className="shrink-0 truncate pr-3 text-xs text-[#F2A91C]/85"
 style={{ width: TIMELINE_LABEL_W }}
 >
 {label}
 </div>
 <div className="relative h-6 flex-1 rounded bg-[#F2A91C]/[0.04]">
 <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-[#F2A91C]/[0.12]" />
 {claims.length === 0 ? (
 <span className="absolute left-2 top-1/2 -translate-y-1/2 text-[10px] italic text-white/20">
 none
 </span>
 ) : (
 <>
 {claims.map((c, i) => (
 <ClaimConnector
 key={`conn-${c.claim_id}-${i}`}
 c={c}
 meetingDateById={meetingDateById}
 earliest={earliest}
 latest={latest}
 />
 ))}
 {claims.map((c, i) => (
 <ClaimMarker
 key={`cm-${c.claim_id}-${i}`}
 c={c}
 earliest={earliest}
 latest={latest}
 onSelect={onSelect}
 />
 ))}
 </>
 )}
 </div>
 </div>
 );
}

// A single tracked-claim diamond, colored by status. `elevated` nudges it
// above lane-center so it reads distinctly from the quote dots when the claim
// is interleaved into a topic lane.
function ClaimMarker({
 c,
 earliest,
 latest,
 elevated,
 onSelect,
}: {
 c: TruthBookClaimEntry;
 earliest: string;
 latest: string;
 elevated?: boolean;
 onSelect: (s: TimelineSelection) => void;
}) {
 const pct = datePct(c.meeting_date, earliest, latest);
 if (pct == null) return null;
 const status = (c.status ?? "active") as TrackedClaimStatus;
 const disp =
 TRACKED_CLAIM_STATUS_DISPLAY[status] ?? TRACKED_CLAIM_STATUS_DISPLAY.active;
 const typeLabel = c.claim_type
 ? TRACKED_CLAIM_TYPE_DISPLAY[c.claim_type] ?? c.claim_type
 : "claim";
 const tip = `${formatDate(c.meeting_date)} · ${typeLabel} (${disp.label})\n${(c.claim_text ?? "").slice(0, 140)}`;
 return (
 <button
 type="button"
 title={tip}
 onClick={() => onSelect({ kind: "claim", claim: c })}
 className="absolute z-10 -translate-x-1/2 -translate-y-1/2 rotate-45 cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
 style={{ left: `${4 + pct * 0.92}%`, top: elevated ? "34%" : "50%" }}
 aria-label={`${typeLabel} claim on ${formatDate(c.meeting_date)}`}
 >
 <span
 className="block h-2.5 w-2.5 ring-1 ring-black/30 transition-transform hover:scale-150"
 style={{ backgroundColor: disp.fg }}
 />
 </button>
 );
}

// Claim → resolution connector: a dashed line from where the claim was made to
// where it resolved. Dormant until the backend populates resolved_meeting_id
// (TRUTH_BOOK_LITE_SPEC chunk 5) — renders nothing while that's null.
function ClaimConnector({
 c,
 meetingDateById,
 earliest,
 latest,
}: {
 c: TruthBookClaimEntry;
 meetingDateById: Record<number, string>;
 earliest: string;
 latest: string;
}) {
 if (c.resolved_meeting_id == null) return null;
 const resDate = meetingDateById[c.resolved_meeting_id] ?? null;
 const fromPct = datePct(c.meeting_date, earliest, latest);
 const toPct = datePct(resDate, earliest, latest);
 if (fromPct == null || toPct == null) return null;
 const status = (c.status ?? "active") as TrackedClaimStatus;
 const disp =
 TRACKED_CLAIM_STATUS_DISPLAY[status] ?? TRACKED_CLAIM_STATUS_DISPLAY.active;
 const left = 4 + Math.min(fromPct, toPct) * 0.92;
 const width = Math.abs(toPct - fromPct) * 0.92;
 return (
 <span
 aria-hidden
 className="pointer-events-none absolute top-1/2 -translate-y-1/2 border-t border-dashed"
 style={{ left: `${left}%`, width: `${width}%`, borderColor: disp.fg, opacity: 0.45 }}
 />
 );
}

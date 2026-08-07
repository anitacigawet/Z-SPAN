/**
 * Conversational Compiler V0.2-3 — Navigator strip (Surface A bird's-eye view).
 *
 * Horizontal strip spanning the meeting's full audio duration end-to-end.
 * Direct analog of IDA Pro's Navigator bar: color-coded segments per
 * IR-node kind let the operator see the program shape at a glance,
 * uncheck conversational fill to isolate typed events, click to seek
 * (both transcript pane + IR pane scroll to the closest IR node).
 *
 * Per CONVERSATIONAL_COMPILER_SPEC.md § Surface A V0.2 Chunk V0.2-3.
 *
 * Color palette: derived from the authoritative statusColors module
 * (amber Commit_P, sky/zinc Motion sub/procedural, emerald/rose
 * Vote passed/failed, cyan Second, slate AgendaTransition ticks).
 * NOT the SPEC's pre-implementation example colors (blue/red/purple) —
 * those were illustrative; the shipped palette settled during V0.1's
 * Opus critique pass and V0.2-1's de-collision pass.
 *
 * Honest scope: V0.2-3 ships click-to-seek; drag-to-scrub is deferred
 * to a follow-up if operator workflow demands it. Click already covers
 * "jump to this point in the meeting" which is the primary use case.
 */
import { useMemo } from "react";
import type { CompilerClaim, CompilerNode } from "../../utils/compiler";

export type NavigatorKind =
 | "commitments"
 | "motions"
 | "votes"
 | "seconds"
 | "agenda_transitions";

export interface NavigatorFilters {
 commitments: boolean;
 motions: boolean;
 votes: boolean;
 seconds: boolean;
 agenda_transitions: boolean;
 /** "Conversational" toggle dims/hides the strip background. The
 * typed-event segments stay visible per their own kind toggles. */
 conversational: boolean;
}

export const DEFAULT_NAVIGATOR_FILTERS: NavigatorFilters = {
 commitments: true,
 motions: true,
 votes: true,
 seconds: true,
 agenda_transitions: true,
 conversational: true,
};

interface Segment {
 startMs: number;
 endMs: number;
 hex: string;
 kind: NavigatorKind;
 focusKey: string;
 /** AgendaTransitions render as thin vertical ticks (point-events),
 * not proportional fill bars. SPEC: "narrow segments don't read
 * well as fill blocks; thin vertical markers are honest about the
 * transition being a point-event." */
 isAgendaTick: boolean;
}

/** Hex lookup mirroring statusColors.ts. Inlined here to avoid the
 * full nodeVisual() call (the strip only needs colors, not the
 * pill / border / word fields). Keep this in sync with statusColors
 * when palettes shift. */
const HEX = {
 commitment: "#f59e0b", // amber-500
 motion_substantive: "#0ea5e9", // sky-500
 motion_procedural: "#71717a", // zinc-500 (quieter)
 vote_passed: "#10b981", // emerald-500
 vote_failed: "#f43f5e", // rose-500
 vote_tabled: "#f59e0b", // amber-500 (matches statusColors)
 vote_withdrawn: "#71717a", // zinc-500
 vote_tied: "#8b5cf6", // violet-500
 second: "#22d3ee", // cyan-400
 agenda_transition: "#94a3b8", // slate-400
} as const;

function classifyMotion(typedFields: Record<string, unknown>): string {
 const mt = (typeof typedFields.motion_type === "string"
 ? typedFields.motion_type
 : "").toLowerCase();
 return mt === "substantive" ? HEX.motion_substantive : HEX.motion_procedural;
}

function classifyVote(typedFields: Record<string, unknown>): string {
 const r = (typeof typedFields.vote_result === "string"
 ? typedFields.vote_result
 : "").toLowerCase();
 if (r === "passed") return HEX.vote_passed;
 if (r === "failed") return HEX.vote_failed;
 if (r === "tabled") return HEX.vote_tabled;
 if (r === "withdrawn") return HEX.vote_withdrawn;
 if (r === "tied") return HEX.vote_tied;
 return HEX.motion_procedural; // fallback to neutral
}

function buildSegments(
 claims: CompilerClaim[],
 nodes: CompilerNode[],
): Segment[] {
 const out: Segment[] = [];
 for (const c of claims) {
 const wt = c.word_timings;
 if (!wt || wt.length === 0) continue;
 const startMs = wt[0].start_ms;
 const endMs = wt[wt.length - 1].end_ms;
 if (typeof startMs !== "number" || typeof endMs !== "number") continue;
 out.push({
 startMs,
 endMs,
 hex: HEX.commitment,
 kind: "commitments",
 focusKey: `claim:${c.id}`,
 isAgendaTick: false,
 });
 }
 for (const n of nodes) {
 const off = n.audio_offset_seconds;
 const dur = n.audio_duration_seconds;
 if (typeof off !== "number") continue;
 const startMs = off * 1000;
 const endMs = typeof dur === "number" ? (off + dur) * 1000 : startMs + 500;
 let hex: string;
 let kind: NavigatorKind;
 let isAgendaTick = false;
 if (n.node_type === "Motion") {
 hex = classifyMotion(n.typed_fields);
 kind = "motions";
 } else if (n.node_type === "Vote") {
 hex = classifyVote(n.typed_fields);
 kind = "votes";
 } else if (n.node_type === "Second") {
 hex = HEX.second;
 kind = "seconds";
 } else if (n.node_type === "AgendaTransition") {
 hex = HEX.agenda_transition;
 kind = "agenda_transitions";
 isAgendaTick = true;
 } else {
 continue;
 }
 out.push({
 startMs,
 endMs,
 hex,
 kind,
 focusKey: `node:${n.id}`,
 isAgendaTick,
 });
 }
 return out;
}

const KIND_LABELS: Record<NavigatorKind, string> = {
 commitments: "Commitments",
 motions: "Motions",
 votes: "Votes",
 seconds: "Seconds",
 agenda_transitions: "Agenda items",
};

const KIND_PILL_HEX: Record<NavigatorKind, string> = {
 commitments: HEX.commitment,
 motions: HEX.motion_substantive,
 votes: HEX.vote_passed,
 seconds: HEX.second,
 agenda_transitions: HEX.agenda_transition,
};

export interface NavigatorStripProps {
 /** Total meeting duration in ms; the strip spans 0 → durationMs.
 * Caller should pass max(endMs) across all NodeRanges + a small
 * buffer if the real audio duration isn't yet wired through. */
 durationMs: number;
 claims: CompilerClaim[];
 nodes: CompilerNode[];
 /** The currently-focused row's key, for the vertical position
 * indicator. Null when nothing is focused. */
 focusKey: string | null;
 filters: NavigatorFilters;
 onFilterChange: (next: NavigatorFilters) => void;
 /** Operator clicked the strip at relative position pct (0-1) —
 * caller maps to the closest IR node and updates focus. */
 onSeek: (timeMs: number) => void;
}

/** Bar height — tall enough that the segments read at a glance but
 * not so tall that it dominates the page above the IR panes. */
const BAR_HEIGHT = 28;
const AGENDA_TICK_WIDTH = 2;

export default function NavigatorStrip({
 durationMs,
 claims,
 nodes,
 focusKey,
 filters,
 onFilterChange,
 onSeek,
}: NavigatorStripProps) {
 const segments = useMemo(() => buildSegments(claims, nodes), [claims, nodes]);

 // Resolve the focused row's time position for the vertical indicator.
 const focusPctX = useMemo<number | null>(() => {
 if (!focusKey) return null;
 const seg = segments.find(s => s.focusKey === focusKey);
 if (!seg) return null;
 const midMs = (seg.startMs + seg.endMs) / 2;
 if (durationMs <= 0) return null;
 return Math.min(1, Math.max(0, midMs / durationMs));
 }, [focusKey, segments, durationMs]);

 // Click handler — map relative click X to a timestamp + caller seeks.
 const handleStripClick = (e: React.MouseEvent<HTMLDivElement>) => {
 const rect = e.currentTarget.getBoundingClientRect();
 const x = e.clientX - rect.left;
 const pct = Math.min(1, Math.max(0, x / rect.width));
 onSeek(pct * durationMs);
 };

 const toggleKind = (kind: keyof NavigatorFilters) => {
 onFilterChange({ ...filters, [kind]: !filters[kind] });
 };

 // Per-kind segment lists for layered rendering. AgendaTransition ticks
 // render LAST so they sit above the fill segments — matches the
 // "purple eyebrow ticks above the bar" intent in the SPEC.
 const fillSegments = segments.filter(s => !s.isAgendaTick);
 const tickSegments = segments.filter(s => s.isAgendaTick);

 // Conversational fill dims/hides via the strip background opacity.
 const bgOpacity = filters.conversational ? 1 : 0.25;

 return (
 <div className="select-none">
 {/* The strip */}
 <div
 onClick={handleStripClick}
 className="relative w-full rounded-md cursor-pointer overflow-hidden"
 style={{
 height: BAR_HEIGHT,
 // Conversational fill = the bg of the strip when nothing
 // typed is happening. Dim zinc/grey reads as "noise gap."
 backgroundColor: `rgba(63, 63, 70, ${0.35 * bgOpacity})`, // zinc-700-ish at low opacity
 border: "1px solid rgba(63, 63, 70, 0.5)",
 }}
 role="slider"
 aria-label="Meeting navigator — click to seek"
 aria-valuemin={0}
 aria-valuemax={durationMs}
 aria-valuenow={focusPctX !== null ? focusPctX * durationMs : 0}
 >
 {/* Fill segments — commitments, motions, votes, seconds. Render
 before ticks so the tick eyebrows sit above the fills. */}
 {fillSegments.map((seg, i) => {
 if (durationMs <= 0) return null;
 if (!filters[seg.kind]) return null;
 const leftPct = (seg.startMs / durationMs) * 100;
 const widthPct = Math.max(
 0.15, // minimum visual width so tiny segments still register
 ((seg.endMs - seg.startMs) / durationMs) * 100,
 );
 return (
 <div
 key={`${seg.focusKey}-${i}`}
 className="absolute top-0 bottom-0 pointer-events-none"
 style={{
 left: `${leftPct}%`,
 width: `${widthPct}%`,
 backgroundColor: seg.hex,
 opacity: 0.85,
 }}
 title={KIND_LABELS[seg.kind]}
 />
 );
 })}

 {/* AgendaTransition eyebrow ticks — thin vertical markers above
 fills. Point-events visualized as 2px lines. */}
 {tickSegments.map((seg, i) => {
 if (durationMs <= 0) return null;
 if (!filters.agenda_transitions) return null;
 const leftPct = (seg.startMs / durationMs) * 100;
 return (
 <div
 key={`tick-${seg.focusKey}-${i}`}
 className="absolute top-0 bottom-0 pointer-events-none"
 style={{
 left: `${leftPct}%`,
 width: AGENDA_TICK_WIDTH,
 backgroundColor: seg.hex,
 opacity: 0.95,
 }}
 title={KIND_LABELS[seg.kind]}
 />
 );
 })}

 {/* Focused-row position indicator — vertical sky line that
 tracks the IR pane's current focus. The line straddles the
 bar's full height so it reads as a "playhead." */}
 {focusPctX !== null && (
 <div
 className="absolute top-0 bottom-0 pointer-events-none"
 style={{
 left: `${focusPctX * 100}%`,
 width: 2,
 marginLeft: -1,
 backgroundColor: "#7dd3fc", // sky-300
 opacity: 0.9,
 boxShadow: "0 0 8px rgba(125, 211, 252, 0.5)",
 }}
 />
 )}
 </div>

 {/* Filter row — inline checkboxes, color-coded by kind. */}
 <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] font-mono">
 {(["commitments", "motions", "votes", "seconds", "agenda_transitions"] as const).map(kind => (
 <FilterCheckbox
 key={kind}
 checked={filters[kind]}
 onChange={() => toggleKind(kind)}
 label={KIND_LABELS[kind]}
 hex={KIND_PILL_HEX[kind]}
 />
 ))}
 <span className="text-zinc-700">·</span>
 <FilterCheckbox
 checked={filters.conversational}
 onChange={() => toggleKind("conversational")}
 label="Conversational fill"
 hex="#52525b" // zinc-600 — the conversational bg color
 />
 </div>
 </div>
 );
}

function FilterCheckbox({
 checked,
 onChange,
 label,
 hex,
}: {
 checked: boolean;
 onChange: () => void;
 label: string;
 hex: string;
}) {
 return (
 <label className="inline-flex items-center gap-1.5 cursor-pointer text-zinc-400 hover:text-zinc-200 transition-colors">
 <input
 type="checkbox"
 checked={checked}
 onChange={onChange}
 className="appearance-none w-3 h-3 rounded-sm border border-zinc-600 checked:border-transparent cursor-pointer"
 style={{
 backgroundColor: checked ? hex : "transparent",
 }}
 />
 <span
 aria-hidden
 className="inline-block w-2 h-2 rounded-sm"
 style={{ backgroundColor: hex, opacity: checked ? 0.9 : 0.3 }}
 />
 <span className={checked ? "" : "opacity-60"}>{label}</span>
 </label>
 );
}

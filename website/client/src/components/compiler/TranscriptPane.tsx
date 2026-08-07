/**
 * Conversational Compiler — full meeting transcript pane (Surface A left).
 *
 * Per SPEC build sequence item 4 + Decision #7a — reads the persisted
 * Whisper word array via /api/compiler/<id>/transcript and renders it
 * as a scrollable document. When an IR node is focused on the right,
 * its corresponding word range gets highlighted + scrolled into view.
 *
 * The token-coloring layer (SPEC item 6 — utterance-kind colored spans)
 * is deferred until the parser tags individual transcript positions
 * with node types; this V1 renders the prose neutrally + highlights
 * only the focused node's range.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { FileText, Loader2 } from "lucide-react";
import {
 fetchCompilerTranscript,
 type CompilerTranscriptWord,
} from "../../utils/compiler";

/** One node's audio range, paired with its visual identity. Computed
 * upstream (CompilerPage) and passed in so TranscriptPane stays
 * agnostic of node-type semantics. Per SPEC build seq item 6 — the
 * token-coloring layer reads these ranges + applies the node-kind
 * text color to each word inside. */
export interface NodeRange {
 startMs: number;
 endMs: number;
 /** "claim:N" / "node:N" focus key the parent uses for selection
 * state. Lets a future enhancement scroll-to-range on click. */
 focusKey: string;
 /** Kind discriminator — drives the visual classification. */
 kind:
 | "Commit_P"
 | "Motion"
 | "Vote"
 | "Second"
 | "AgendaTransition"
 | "Utterance"
 | "Contradiction";
 /** Tailwind text color class applied to each word inside the range.
 * Composed upstream from statusColors.nodeVisual / statusVisual so
 * the palette stays in lockstep with the right-pane chrome. */
 textColorClass: string;
 /** AgendaTransitions render as eyebrow markers BEFORE their first
 * word, not as a colored body — this label is what shows on the
 * eyebrow ("Item 4B · Open the zoning code amendment public
 * hearing"). Null for non-agenda-transition ranges. */
 agendaEyebrowLabel?: string | null;
 /** Surface A V0.2-1 — speaker attribution. When a range carries a
 * confidently-resolved speaker, the transcript pane renders the
 * speaker's name as an inline label at the start of their turn
 * (where "turn" = a contiguous run of words attributable to the
 * same speaker). Null when the range has no speaker (e.g.,
 * AgendaTransitions are procedural markers without a single
 * speaker). Honest sparseness — labels appear only where confident;
 * the rest reads as continuous prose. */
 speakerName?: string | null;
 /** Title / role string from council_members.role — typically
 * "Mayor", "Vice Mayor", "Council Member". Rendered in parens
 * after the speaker's last name. Null when the speaker has no
 * canonical-roster join. */
 speakerTitle?: string | null;
}

/** Stable color tint per speaker — deterministic hash on the speaker's
 * canonical name → one of 8 muted palette entries. Same speaker always
 * gets the same tint across reloads, across meetings; different
 * speakers in the same meeting get visually distinguishable tints.
 * Per the V0.2-1 polish, this lets the operator's eye learn the palette
 * over the course of a transcript without manual color choices.
 *
 * Palette de-collision (Opus critique 2026-06-04): the palette must NOT
 * overlap with V0.1's node-kind token colors, otherwise the eye
 * accidentally attributes colored words to the wrong speaker. V0.1
 * uses text-sky-300 (Motion) and text-emerald-300 (Vote-passed) as
 * its two MOST FREQUENT token colors — both are dropped from this
 * speaker palette in favor of text-indigo-300 and text-stone-300
 * (neither used by any V0.1 token semantic). The other six hues
 * collide only with rare token colors (Vote-failed / Vote-tabled /
 * Second / etc.) where the context separates them. */
function tintForSpeaker(name: string): string {
 const palette = [
 "text-indigo-300",
 "text-stone-300",
 "text-amber-300",
 "text-violet-300",
 "text-rose-300",
 "text-cyan-300",
 "text-fuchsia-300",
 "text-lime-300",
 ];
 let h = 0;
 for (let i = 0; i < name.length; i++) {
 h = (h * 31 + name.charCodeAt(i)) >>> 0;
 }
 return palette[h % palette.length];
}

/** Extract the last name from a full name for compact label rendering.
 * "Ken Watkins" → "Watkins"; "Jamie Scott Stehly" → "Stehly". Single-
 * word names pass through unchanged. Heuristic, not perfect — names
 * with suffixes ("Smith Jr.") would mis-extract, but the Kingman pilot
 * roster has none. Revisit when first city with such names ships. */
function lastNameOf(name: string): string {
 const trimmed = name.trim();
 const lastSpace = trimmed.lastIndexOf(" ");
 return lastSpace === -1 ? trimmed : trimmed.slice(lastSpace + 1);
}

interface TranscriptPaneProps {
 meetingId: number;
 /** Word-range to highlight + scroll into view. Null = no highlight.
 * Times are in MILLISECONDS to match the existing `word_timings`
 * convention used across the project (tracked_claims.word_timings,
 * quotes.word_timings — same shape). The Whisper word array's
 * `start`/`end` fields are in SECONDS; this component converts on
 * the fly. */
 highlightSpanMs: { startMs: number; endMs: number } | null;
 /** Slot above the transcript for the focused node's metadata
 * (speaker + sub-kind). Null = no header (the focus is on the
 * transcript itself). */
 focusHeader: React.ReactNode | null;
 /** SPEC build seq item 5 — bidirectional scroll sync. Fires when the
 * operator scrolls the transcript (debounced), passing the timestamp
 * (ms) of the word currently sitting at ~25% from the top of the
 * viewport (the natural "what's the operator reading right now"
 * signal). CompilerPage maps the timestamp back to an IR node and
 * updates focus. Programmatic scroll-to-highlight does NOT fire this
 * callback (suppression window prevents the feedback loop). */
 onVisibleTimeChange?: (timeMs: number) => void;
 /** SPEC build seq item 6 — token coloring. Each word whose start
 * time falls inside a NodeRange gets the range's textColorClass
 * applied. Overlap rule: smaller (more specific) ranges win, so a
 * Motion inside an AgendaTransition wins its words. AgendaTransitions
 * render as eyebrows at their start position, not as full-range
 * coloring (they wrap too many words to color without dominating). */
 nodeRanges?: NodeRange[];
}

/** Stable identifier for the Whisper word index range that fits a
 * given highlight span. Memoized on (words, span) so the hot path
 * (render with N=11k words) doesn't rescan. */
function useHighlightRange(
 words: CompilerTranscriptWord[],
 span: { startMs: number; endMs: number } | null,
): { startIdx: number; endIdx: number } | null {
 return useMemo(() => {
 if (!span || words.length === 0) return null;
 const startSec = span.startMs / 1000;
 const endSec = span.endMs / 1000;
 let startIdx = -1;
 let endIdx = -1;
 for (let i = 0; i < words.length; i++) {
 const w = words[i];
 if (w.end >= startSec && startIdx === -1) startIdx = i;
 if (w.start <= endSec) endIdx = i;
 else if (startIdx !== -1) break;
 }
 if (startIdx === -1 || endIdx === -1) return null;
 return { startIdx, endIdx };
 }, [words, span]);
}

export default function TranscriptPane({
 meetingId,
 highlightSpanMs,
 focusHeader,
 onVisibleTimeChange,
 nodeRanges,
}: TranscriptPaneProps) {
 const [words, setWords] = useState<CompilerTranscriptWord[]>([]);
 const [loading, setLoading] = useState(true);
 const [errorMsg, setErrorMsg] = useState<string | null>(null);
 const scrollRef = useRef<HTMLDivElement | null>(null);
 const firstHighlightRef = useRef<HTMLSpanElement | null>(null);
 // SPEC item 5 — programmatic-scroll suppression. When we scroll the
 // transcript via the highlight-into-view useEffect (response to a
 // focusKey change from outside), don't fire the onVisibleTimeChange
 // callback or we'd flip focus to whichever word landed at the
 // viewport's reading position — a feedback loop with the parent's
 // focus-setting flow.
 const suppressNextScrollUntilRef = useRef<number>(0);
 // Last time emitted, so we throttle the callback to once per ~150ms
 // and only emit when the time actually changes by more than 250ms.
 const lastEmittedTimeMsRef = useRef<number | null>(null);

 useEffect(() => {
 let cancelled = false;
 const ctrl = new AbortController();
 setLoading(true);
 setErrorMsg(null);
 setWords([]);
 fetchCompilerTranscript(meetingId, { signal: ctrl.signal })
 .then(d => {
 if (!cancelled) setWords(d.words);
 })
 .catch(e => {
 if (!cancelled && e?.name !== "AbortError") {
 setErrorMsg(e?.message ?? "Failed to load transcript");
 }
 })
 .finally(() => {
 if (!cancelled) setLoading(false);
 });
 return () => {
 cancelled = true;
 ctrl.abort();
 };
 }, [meetingId]);

 const highlightRange = useHighlightRange(words, highlightSpanMs);

 // Scroll the first highlighted word into view when the range changes.
 // useEffect with the highlight range identity (not the ref) so it
 // fires on every focus change.
 //
 // SPEC item 5 — when the focus change came from THIS pane's own
 // scroll emit (the bidirectional sync's transcript→IR direction),
 // skip the auto-scroll. Otherwise the transcript would "fight" the
 // operator's scroll by snapping to put the newly-focused span at
 // 35% from the top, which feels disorienting. We detect this by
 // checking whether the new highlight range's start time is close
 // to the last time we emitted upward.
 useEffect(() => {
 if (!highlightRange || !highlightSpanMs) return;
 const lastEmitted = lastEmittedTimeMsRef.current;
 const fromOwnScroll =
 lastEmitted !== null &&
 Math.abs(lastEmitted - highlightSpanMs.startMs) < 1500;
 if (fromOwnScroll) {
 // Reset so a subsequent UNRELATED focus change (e.g., right-pane
 // click on a different node) still triggers the scroll.
 lastEmittedTimeMsRef.current = null;
 return;
 }
 const el = firstHighlightRef.current;
 const scroller = scrollRef.current;
 if (!el || !scroller) return;
 // Mark this scroll as programmatic so the onScroll handler skips
 // the visible-time emit for the smooth-scroll window (~400ms).
 // Set the suppression deadline +800ms to give ample headroom for
 // the easing curve to settle.
 suppressNextScrollUntilRef.current = Date.now() + 800;
 // requestAnimationFrame so React has finished applying the className
 // before we measure offsets.
 const id = requestAnimationFrame(() => {
 const elTop = el.offsetTop;
 const targetTop = elTop - scroller.clientHeight * 0.35;
 scroller.scrollTo({ top: Math.max(0, targetTop), behavior: "smooth" });
 });
 return () => cancelAnimationFrame(id);
 }, [highlightRange, highlightSpanMs]);

 // SPEC item 5 — debounced scroll handler that maps the current scroll
 // position to a transcript timestamp + emits it to the parent. The
 // parent (CompilerPage) uses the timestamp to find the matching IR
 // node and update focus.
 useEffect(() => {
 const scroller = scrollRef.current;
 if (!scroller || !onVisibleTimeChange) return;
 let rafId: number | null = null;
 const onScroll = () => {
 if (rafId !== null) return;
 rafId = requestAnimationFrame(() => {
 rafId = null;
 // Suppress while a programmatic scroll is in flight.
 if (Date.now() < suppressNextScrollUntilRef.current) return;
 // Pick the word at the viewport's 25% reading line — natural
 // "what the operator is looking at" point. Iterate the
 // word-span DOM (data-start attr) and find the first one whose
 // top is below the reading line.
 const readingLineY =
 scroller.getBoundingClientRect().top + scroller.clientHeight * 0.25;
 const allSpans = scroller.querySelectorAll<HTMLSpanElement>(
 "span[data-start]",
 );
 // Binary-search would be cleaner but linear scan across 11k
 // spans on a debounced event is still cheap (~ms). Premature-
 // optimize later if profiling shows it matters.
 let chosenStart: number | null = null;
 for (let i = 0; i < allSpans.length; i++) {
 const rect = allSpans[i].getBoundingClientRect();
 if (rect.top + rect.height >= readingLineY) {
 const start = Number(allSpans[i].dataset.start);
 if (Number.isFinite(start)) chosenStart = start * 1000;
 break;
 }
 }
 if (chosenStart === null) return;
 const last = lastEmittedTimeMsRef.current;
 // Only emit when the time has materially changed (250ms
 // threshold avoids firing on micro-scrolls / inertia ticks).
 if (last !== null && Math.abs(chosenStart - last) < 250) return;
 lastEmittedTimeMsRef.current = chosenStart;
 onVisibleTimeChange(chosenStart);
 });
 };
 scroller.addEventListener("scroll", onScroll, { passive: true });
 return () => {
 scroller.removeEventListener("scroll", onScroll);
 if (rafId !== null) cancelAnimationFrame(rafId);
 };
 }, [onVisibleTimeChange, words]);

 // SPEC item 6 + V0.2-1 polish — pre-compute, per-word:
 // (a) the smallest containing colored range (Motion / Vote / Second
 // / Commit_P) — drives the text-color class
 // (b) the smallest containing range with speaker attribution —
 // drives the "Watkins (Mayor):" inline labels at speaker-turn
 // changes
 // (c) the AgendaTransition that starts before this word — drives
 // the eyebrow marker
 // The colored-range and speaker-range walks are separate because a
 // word can carry color from one node (e.g., a Motion) AND speaker
 // attribution from the same node — but the smallest-wins semantics
 // are identical, so we share one walk.
 const { wordColorClass, agendaTransitionStarts, speakerTurnStarts } = useMemo(() => {
 const colorClass: Array<string | null> = new Array(words.length).fill(null);
 const transitionStarts: Array<{
 atIdx: number;
 label: string;
 key: string;
 }> = [];
 const turnStarts: Array<{
 atIdx: number;
 name: string;
 title: string | null;
 tintClass: string;
 }> = [];
 if (!nodeRanges || nodeRanges.length === 0 || words.length === 0) {
 return {
 wordColorClass: colorClass,
 agendaTransitionStarts: transitionStarts,
 speakerTurnStarts: turnStarts,
 };
 }
 // Per-word speaker — used internally to derive turnStarts (the
 // smallest-range-wins walk populates this), then the array is
 // discarded. Only the turn-start list is needed at render time.
 const speakerName: Array<string | null> = new Array(words.length).fill(null);
 const speakerTitle: Array<string | null> = new Array(words.length).fill(null);
 // Split colored ranges from agenda eyebrows.
 const colored = nodeRanges.filter(r => r.kind !== "AgendaTransition");
 const transitions = nodeRanges.filter(r => r.kind === "AgendaTransition");
 // Sort colored ranges by size ASC so the SMALLEST (most specific)
 // wins when we assign in order — a Commit_P inside an Item 4B
 // AgendaTransition wins its words.
 colored.sort((a, b) => a.endMs - a.startMs - (b.endMs - b.startMs));
 // For each word, walk ranges (sorted small→large), apply the
 // first matching range's color + speaker attribution. Linear over
 // 11k words × ~10 colored ranges = ~100k comparisons. Cheap on
 // every focus update.
 for (let i = 0; i < words.length; i++) {
 const wMs = words[i].start * 1000;
 for (const r of colored) {
 if (wMs >= r.startMs && wMs <= r.endMs) {
 colorClass[i] = r.textColorClass;
 // Speaker info comes from the same smallest-range-wins walk
 // (different ranges may have different speakers; the most
 // specific range owns the word).
 if (r.speakerName) {
 speakerName[i] = r.speakerName;
 speakerTitle[i] = r.speakerTitle ?? null;
 }
 break;
 }
 }
 }
 // For each AgendaTransition, find the first word whose start is
 // ≥ the transition's start. That's where the eyebrow marker
 // attaches.
 transitions.sort((a, b) => a.startMs - b.startMs);
 let cursor = 0;
 for (const t of transitions) {
 while (cursor < words.length && words[cursor].start * 1000 < t.startMs) {
 cursor++;
 }
 if (cursor < words.length) {
 transitionStarts.push({
 atIdx: cursor,
 label: t.agendaEyebrowLabel ?? "Agenda item",
 key: t.focusKey,
 });
 }
 }
 // Compute speaker-turn starts. A "turn" begins at any word i where
 // speakerName[i] is non-null AND speakerName[i-1] is either null
 // or a different speaker. Single-word turns are still labeled
 // (honest representation of fast back-and-forth).
 let prev: string | null = null;
 for (let i = 0; i < words.length; i++) {
 const curr = speakerName[i];
 if (curr !== null && curr !== prev) {
 turnStarts.push({
 atIdx: i,
 name: curr,
 title: speakerTitle[i],
 tintClass: tintForSpeaker(curr),
 });
 }
 prev = curr;
 }
 return {
 wordColorClass: colorClass,
 agendaTransitionStarts: transitionStarts,
 speakerTurnStarts: turnStarts,
 };
 }, [words, nodeRanges]);

 // Render the word stream. Each word is a span with whitespace before
 // (except the first); highlighted-range words carry the amber
 // background; node-range words carry the per-kind text color from
 // wordColorClass; AgendaTransition starts emit an eyebrow row before
 // the word at their position; speaker-turn starts emit a labeled
 // line ("Watkins (Mayor):") before the word at their position.
 const renderedTranscript = useMemo(() => {
 if (words.length === 0) return null;
 const startIdx = highlightRange?.startIdx ?? -1;
 const endIdx = highlightRange?.endIdx ?? -1;
 // Build fast lookups for transition-eyebrow + speaker-turn positions.
 const transitionAt: Map<number, { label: string; key: string }> = new Map();
 for (const t of agendaTransitionStarts) {
 transitionAt.set(t.atIdx, { label: t.label, key: t.key });
 }
 const turnAt: Map<number, { name: string; title: string | null; tintClass: string }> = new Map();
 for (const t of speakerTurnStarts) {
 turnAt.set(t.atIdx, { name: t.name, title: t.title, tintClass: t.tintClass });
 }
 return (
 <p className="text-[15px] leading-relaxed text-zinc-300 font-serif whitespace-pre-wrap">
 {words.map((w, i) => {
 const isHighlight = i >= startIdx && i <= endIdx && startIdx !== -1;
 const isFirstHighlight = i === startIdx;
 const prior = i > 0 ? words[i - 1].word : "";
 const noSpaceAfter = /[(\[{]$/.test(prior);
 const noSpaceBefore = /^[.,;:!?)\]}'']/.test(w.word);
 const eyebrow = transitionAt.get(i);
 const turn = turnAt.get(i);
 // The block-level inserts (eyebrow + speaker label) replace
 // the natural inter-word space — if either is present, no
 // leading space is needed before this word.
 const needsSpace = i > 0 && !noSpaceAfter && !noSpaceBefore && !eyebrow && !turn;
 const nodeColor = wordColorClass[i];
 const wordClass = [
 isHighlight ? "bg-amber-500/20 text-amber-100 rounded px-0.5" : "",
 !isHighlight && nodeColor ? nodeColor : "",
 ]
 .filter(Boolean)
 .join(" ");
 return (
 <span key={i}>
 {eyebrow && (
 <span
 data-agenda-eyebrow={eyebrow.key}
 className="block mt-6 mb-2 text-[10px] font-mono uppercase tracking-wider text-slate-400 border-t border-slate-700/40 pt-3"
 >
 ■ {eyebrow.label}
 </span>
 )}
 {turn && (
 <span
 data-speaker-label={turn.name}
 className={`block ${eyebrow ? "mt-2" : "mt-3"} mb-1 text-[13px] font-sans font-semibold ${turn.tintClass}`}
 >
 {lastNameOf(turn.name)}
 {turn.title && (
 <span className="text-zinc-500 ml-1.5 font-normal">({turn.title})</span>
 )}
 <span className="text-zinc-500 font-medium">:</span>
 </span>
 )}
 {needsSpace ? " " : ""}
 <span
 ref={isFirstHighlight ? firstHighlightRef : undefined}
 className={wordClass}
 data-start={w.start}
 data-end={w.end}
 >
 {w.word}
 </span>
 </span>
 );
 })}
 </p>
 );
 }, [words, highlightRange, wordColorClass, agendaTransitionStarts, speakerTurnStarts]);

 return (
 <div ref={scrollRef} className="h-full overflow-y-auto">
 {focusHeader && (
 <div className="sticky top-0 z-10 bg-[var(--surface)]/95 backdrop-blur px-6 lg:px-8 py-3 border-b border-[var(--line)]/60">
 {focusHeader}
 </div>
 )}

 <div className="px-6 lg:px-8 py-6">
 {loading && (
 <div className="flex items-center gap-2 text-sm text-zinc-500">
 <Loader2 className="w-4 h-4 animate-spin" />
 Loading transcript…
 </div>
 )}

 {!loading && errorMsg && (
 <div className="max-w-sm">
 <FileText className="w-8 h-8 mb-4 text-zinc-700" />
 <p className="text-sm text-zinc-400 leading-relaxed">
 No transcript yet for this meeting.
 </p>
 <p className="text-xs text-zinc-600 mt-3 leading-relaxed">
 The Whisper word-level transcript is produced by the
 ingestion pipeline when a meeting is processed. Until it
 lands, the right-pane IR nodes are the source of truth.
 </p>
 <p className="text-[10px] text-zinc-700 mt-3 font-mono">{errorMsg}</p>
 </div>
 )}

 {!loading && !errorMsg && words.length === 0 && (
 <p className="text-sm text-zinc-500">Transcript is empty.</p>
 )}

 {!loading && !errorMsg && renderedTranscript}
 </div>
 </div>
 );
}

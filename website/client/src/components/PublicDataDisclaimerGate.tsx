/**
 * PublicDataDisclaimerGate —C3 per-surface wrapper + shared
 * acknowledgment modal.
 *
 * Spec source-of-truth: `01_Project_Overview/S091_DISCLAIMER_COPY_LOCKED.md`.
 * Pattern adapted from `CreatorsSignupPage::StepTwo` fleet-media
 * disclaimer karaoke).
 *
 * Architecture:
 * - `<PublicDataDisclaimerProvider>` mounts ONCE at App root. Holds
 * ack state in React context + localStorage. Renders the modal
 * when any gate triggers it.
 * - `<PublicDataDisclaimerGate surfaceName="...">` wraps each public
 * surface that displays AI-generated content (per the inventory at
 * `01_Project_Overview/S091_GATED_SURFACE_INVENTORY.md`). If acked,
 * silently renders children. If not, renders a placeholder cover
 * that opens the shared modal on click.
 *
 * State machine (per locked spec):
 * STATE 1 READING → 10-sec dwell (public) / 1-sec (operator) →
 * [Click here] becomes clickable →
 * STATE 2 CONFIRMING → karaoke animates acknowledgment sentence,
 * ~2-sec public / 1-sec operator →
 * [Yes] / [No] buttons appear →
 * STATE 3 UNLOCKED (Yes) → localStorage records ack, all gates
 * silently re-render children →
 * STATE 4 DECLINED (No) → window.location.href = "google.com"
 * hard exit per operator direction: a visitor who treats the
 * decline as a kick-me-out button gets exactly that.
 *
 * The "as soon as you [Click here]" framing in the disclaimer body is
 * load-bearing: by clicking, the user accepts the consequences of
 * disregarding. Preserve this intent if you refactor the gate.
 *
 * Future scope: progressive lockout for repeat decliners —
 * fingerprint + IP correlation, threshold-based escalation. STATE 4
 * currently is bare google.com exit;will layer escalation on
 * top.
 *
 * Operator-debug bypass (operator-deferred to a future pass): a
 * toggle that fully bypasses the gate for operator session when
 * debugging. Out of C3 scope; filed as C3 follow-up. Until then,
 * operator gets collapsed 1-sec timings (still visible, just fast).
 */

import {
 createContext,
 ReactElement,
 ReactNode,
 useCallback,
 useContext,
 useEffect,
 useMemo,
 useState,
} from "react";
import { Lock, X } from "lucide-react";

import { useCurrentUser } from "@/hooks/useCurrentUser";
import {
 DISCLAIMER_ACK_PERSIST_ACROSS_RELOADS,
 DISCLAIMER_ACK_SEGMENTS,
 DISCLAIMER_LOCALSTORAGE_KEY,
 DISCLAIMER_TIMINGS,
 DISCLAIMER_VERSION,
} from "@/lib/projectMeta";

// ─────────────────────────────────────────────────────────────────
// Context
// ─────────────────────────────────────────────────────────────────

interface DisclaimerContextValue {
 acked: boolean;
 openModal: (surfaceName: string) => void;
 isOperator: boolean;
}

const PublicDataDisclaimerContext = createContext<DisclaimerContextValue | null>(
 null,
);

function useDisclaimerContext(): DisclaimerContextValue {
 const ctx = useContext(PublicDataDisclaimerContext);
 if (!ctx) {
 throw new Error(
 "PublicDataDisclaimerGate must be used inside <PublicDataDisclaimerProvider>",
 );
 }
 return ctx;
}

/** Read-only ack state for surfaces that must WAIT for the gate —
 * e.g. the ?t= deep-link auto-seek holds its autoplay until the
 * visitor acknowledges (2026-07-07 audit finding: the
 * seek effect fired under the modal, starting meeting audio before
 * acknowledgment — reliable post-PLAYER-1 where it used to be racy). */
export function useDisclaimerAcked(): boolean {
 return useDisclaimerContext().acked;
}

// ─────────────────────────────────────────────────────────────────
// Provider — wrap once at App root
// ─────────────────────────────────────────────────────────────────

export function PublicDataDisclaimerProvider({
 children,
 scopeKey,
 autoAck = false,
}: {
 children: ReactNode;
 /** When this string changes, the ack resets (gate re-fires). Per
 * operator 2026-06-25, the gate must re-fire on every navigation
 * to a different episode — App.tsx passes a key derived from
 * navigation.view + meetingId. Pass `undefined` for surfaces that
 * don't need scope-keyed reset (the only reset is then full-page-
 * reload, which already starts unacked when
 * DISCLAIMER_ACK_PERSIST_ACROSS_RELOADS is false).
 */
 scopeKey?: string;
 /** Session-32 (2026-07-04) — auto-acknowledge every surface, no
 * placeholder, no modal. Used by peek-mode (?peek=1) so the operator
 * reviewing a broadcast in the terminal iframe sees the real content
 * instead of the ack placeholder gating each surface. Skipping the
 * auto-fire alone wasn't sufficient — the per-surface
 * PublicDataDisclaimerGate wrappers ALSO check `acked`, so the
 * placeholders showed even when the modal was suppressed. This flag
 * forces `acked=true` at the context level, which silences both. */
 autoAck?: boolean;
}): ReactElement {
 const { isOwner } = useCurrentUser();
 const isOperator = isOwner;

 // Per operator 2026-06-25 + the DISCLAIMER_ACK_PERSIST_ACROSS_RELOADS
 // feature-flag in projectMeta.ts: initialize unacked every page load.
 // The operator called the every-reload re-fire overkill and required
 // it anyway — the disclaimer's seriousness must reach every visitor
 // on every load, operator sessions included. When the flag flips back
 // to true (operator decides post-launch), restore the
 // localStorage-read in this initializer.
 const [acked, setAcked] = useState<boolean>(() => {
 if (autoAck) return true;
 if (!DISCLAIMER_ACK_PERSIST_ACROSS_RELOADS) return false;
 try {
 return typeof window !== "undefined"
 && window.localStorage.getItem(DISCLAIMER_LOCALSTORAGE_KEY) === "1";
 } catch {
 return false;
 }
 });
 const [modalOpenFor, setModalOpenFor] = useState<string | null>(null);

 // Reset ack + auto-fire the modal on scope-key change (navigating to
 // a new BroadcastPage). PerLOCKED spec § State machine, the
 // gate re-fires on EVERY page load AND every episode navigation, so
 // we open the modal here rather than wait for a placeholder click.
 // The initial mount also fires this effect (scopeKey is a dep). When
 // scopeKey is undefined (App.tsx opt-out for views without gated
 // content), the modal stays closed — App.tsx passes a defined
 // scopeKey only for views with gated surfaces (currently
 // BroadcastPage via navigation.view + '/' + meetingId).
 //
 // The placeholder-click pattern still works as a fallback: if the
 // auto-fire is bypassed for any reason, clicking a placeholder will
 // still open the modal via openModal(). Belt-and-suspenders.
 // Per operator 2026-07-23, every episode navigation re-fires the
 // disclaimer, even when another episode was acknowledged in this tab.
 useEffect(() => {
 if (autoAck) {
 setAcked(true);
 setModalOpenFor(null);
 return;
 }
 setAcked(false);
 if (scopeKey !== undefined) {
 setModalOpenFor("__page_load__");
 } else {
 setModalOpenFor(null);
 }
 }, [scopeKey, autoAck]);

 const openModal = useCallback(
 (surfaceName: string) => {
 if (acked) return;
 setModalOpenFor(surfaceName);
 },
 [acked],
 );

 const handleAcknowledge = useCallback(() => {
 // localStorage WRITE is intentionally omitted while
 // DISCLAIMER_ACK_PERSIST_ACROSS_RELOADS is false — there's
 // nothing to persist if the read isn't honored. When the flag
 // flips back to true, restore the write here AND the read in
 // the useState initializer above. Together they re-enable the
 // once-per-DISCLAIMER_VERSION persistence shape.
 setAcked(true);
 setModalOpenFor(null);
 }, []);

 // 2026-06-26: STATE 4 google.com hard-exit retired. The new STATE 2
 // surface has three buttons (Sure / Okay / I Understand) where the
 // two wrong-button paths return to STATE 1 reading rather than exit
 // the site. Operator-set semantics: Sure/Okay mean the reader didn't
 // actually listen, so those buttons loop back to the start of the
 // disclaimer text.progressive-lockout still composes on top of the
 // I-Understand path if operator later wants escalation; the decline
 // path no longer triggers external navigation.

 const value = useMemo<DisclaimerContextValue>(
 () => ({ acked, openModal, isOperator }),
 [acked, openModal, isOperator],
 );

 return (
 <PublicDataDisclaimerContext.Provider value={value}>
 {children}
 {modalOpenFor && !acked && (
 <DisclaimerModal
 surfaceName={modalOpenFor}
 isOperator={isOperator}
 onAcknowledge={handleAcknowledge}
 onClose={() => setModalOpenFor(null)}
 />
 )}
 </PublicDataDisclaimerContext.Provider>
 );
}

// ─────────────────────────────────────────────────────────────────
// Per-surface wrapper
// ─────────────────────────────────────────────────────────────────

export function PublicDataDisclaimerGate({
 surfaceName,
 children,
 placeholderHeight,
}: {
 surfaceName: string;
 children: ReactNode;
 /** Optional minHeight for the placeholder so the gated surface
 * doesn't reflow when ack happens. Defaults to undefined (let the
 * placeholder size to its content). */
 placeholderHeight?: string | number;
}): ReactElement {
 const { acked, openModal } = useDisclaimerContext();

 if (acked) {
 return <>{children}</>;
 }

 return (
 <button
 type="button"
 onClick={() => openModal(surfaceName)}
 className="w-full rounded-lg border border-dashed border-white/20 bg-white/[0.015] hover:border-white/35 hover:bg-white/[0.025] transition px-5 py-8 text-left cursor-pointer group"
 aria-label={`Acknowledge disclaimer to view ${surfaceName.replace(/_/g, " ")}`}
 style={placeholderHeight ? { minHeight: placeholderHeight } : undefined}
 >
 <div className="flex items-center gap-3 mb-2">
 <Lock className="w-4 h-4 text-foreground/40 group-hover:text-foreground/60 transition" />
 <span className="text-[11px] uppercase tracking-[0.18em] text-foreground/45 group-hover:text-foreground/65 transition">
 Disclaimer required
 </span>
 </div>
 <p className="text-sm text-foreground/55 group-hover:text-foreground/75 transition leading-relaxed">
 Click here to read the disclaimer before viewing this content. One acknowledgment unlocks every data surface on the site.
 </p>
 </button>
 );
}

// ─────────────────────────────────────────────────────────────────
// Modal — internal; renders when openModal fires
// ─────────────────────────────────────────────────────────────────

type ModalStage = "reading" | "confirming";

function DisclaimerModal({
 surfaceName: _surfaceName,
 isOperator,
 onAcknowledge,
}: {
 surfaceName: string;
 isOperator: boolean;
 onAcknowledge: () => void;
 onClose: () => void;
}): ReactElement {
 // surfaceName recorded for analytics/audit (futurefingerprint
 // correlation). Session-80 (2026-07-18): the top-right X button
 // dismisses the modal immediately via onAcknowledge — an operator
 // escape hatch so the STATE 1 dwell + STATE 2 karaoke can be
 // bypassed at any moment. Acknowledgment (not just closing) is what
 // fires so the modal doesn't immediately re-open on the next scopeKey
 // change.
 const timings = isOperator ? DISCLAIMER_TIMINGS.operator : DISCLAIMER_TIMINGS.public;
 const [stage, setStage] = useState<ModalStage>("reading");
 const [clickHereEnabled, setClickHereEnabled] = useState(false);
 const [karaokeCompleted, setKaraokeCompleted] = useState(false);
 const [karaokeWordIdx, setKaraokeWordIdx] = useState(0);
 const [dwellRemaining, setDwellRemaining] = useState(
 Math.ceil(timings.stage1DwellMs / 1000),
 );

 // ── STATE 1 dwell timer ──
 useEffect(() => {
 if (stage !== "reading" || clickHereEnabled) return;
 const enableAt = Date.now() + timings.stage1DwellMs;
 const tick = window.setInterval(() => {
 const remaining = Math.max(0, Math.ceil((enableAt - Date.now()) / 1000));
 setDwellRemaining(remaining);
 if (remaining === 0) {
 setClickHereEnabled(true);
 window.clearInterval(tick);
 }
 }, 100);
 return () => window.clearInterval(tick);
 }, [stage, clickHereEnabled, timings.stage1DwellMs]);

 // ── STATE 2 etch-reveal tokens ──
 // Flatten DISCLAIMER_ACK_SEGMENTS into a render-ordered token list
 // with per-word indices for the etch-reveal animation timing. Words
 // carry { kind: "word"; wIdx; red }; newlines render as <br>.
 const ackTokens = useMemo(() => {
 type Token =
 | { kind: "word"; text: string; wIdx: number; red: boolean }
 | { kind: "newline" };
 const out: Token[] = [];
 let wIdx = 0;
 for (const seg of DISCLAIMER_ACK_SEGMENTS) {
 if (seg.newline) {
 out.push({ kind: "newline" });
 continue;
 }
 const words = (seg.text ?? "").split(/\s+/).filter(Boolean);
 for (const w of words) {
 out.push({ kind: "word", text: w, wIdx: wIdx++, red: !!seg.red });
 }
 }
 return out;
 }, []);
 const totalAckWords = useMemo(
 () => ackTokens.filter((t): t is { kind: "word"; text: string; wIdx: number; red: boolean } => t.kind === "word").length,
 [ackTokens],
 );

 useEffect(() => {
 if (stage !== "confirming" || karaokeCompleted) return;
 const msPerWord = timings.stage2KaraokeMs / totalAckWords;
 const id = window.setInterval(() => {
 setKaraokeWordIdx((idx) => {
 if (idx >= totalAckWords - 1) {
 setKaraokeCompleted(true);
 window.clearInterval(id);
 return totalAckWords - 1;
 }
 return idx + 1;
 });
 }, msPerWord);
 return () => window.clearInterval(id);
 }, [stage, karaokeCompleted, totalAckWords, timings.stage2KaraokeMs]);

 const advanceToConfirming = useCallback(() => {
 if (!clickHereEnabled) return;
 setStage("confirming");
 }, [clickHereEnabled]);

 // 2026-06-26: STATE 2 wrong-button path. Sure + Okay both return the
 // user to STATE 1 ("they didnt listen"). Resets everything that
 // governs STATE 1 + STATE 2 progression: the dwell timer restarts,
 // the karaoke needs to re-play, the [Click here] becomes disabled
 // again. Operator's intent — make the wrong-button branch enforce a
 // re-read rather than a quick retry. No color coding on the buttons
 // is what prevents subconscious-mode clickthrough; the dwell reset
 // is what makes "click wrong → click right immediately" not work.
 const resetToReading = useCallback(() => {
 setStage("reading");
 setClickHereEnabled(false);
 setKaraokeCompleted(false);
 setKaraokeWordIdx(0);
 setDwellRemaining(Math.ceil(timings.stage1DwellMs / 1000));
 }, [timings.stage1DwellMs]);

 // Inline emphasis renderer supporting markdown-like `*italic*` and
 // `**bold**` wrappers in the disclaimer body. Bold pattern matches
 // first (greedy on `**`), then italic; non-matching segments render
 // as plain spans.
 const renderInlineEmphasis = (s: string): ReactNode => {
 const parts = s.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g);
 return parts.map((part, i) => {
 if (part.startsWith("**") && part.endsWith("**")) {
 return (
 <strong key={i} className="font-bold text-white">
 {part.slice(2, -2)}
 </strong>
 );
 }
 if (part.startsWith("*") && part.endsWith("*")) {
 return (
 <em key={i} className="italic font-medium text-white">
 {part.slice(1, -1)}
 </em>
 );
 }
 return <span key={i}>{part}</span>;
 });
 };

 return (
 <div
 className="fixed inset-0 z-[110] bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 overflow-y-auto"
 role="dialog"
 aria-modal="true"
 aria-label="Public data disclaimer"
 >
 <div className="relative max-w-2xl w-full max-h-[90vh] overflow-y-auto rounded-lg border border-white/15 bg-[#0A0A0A] p-8 shadow-2xl">
 {/* Operator escape hatch — the X was intended for the owner only
 (2026-07-18 shipped it unguarded; operator flagged
 2026-07-24 that non-owner visitors could click through the
 full disclaimer flow with one click). Non-owners see no X
 and must acknowledge via the STATE 1 dwell + STATE 2 karaoke
 path. */}
 {isOperator && (
 <button
 type="button"
 onClick={onAcknowledge}
 className="absolute top-3 right-3 p-1.5 rounded-md text-foreground/40 hover:text-white hover:bg-white/10 transition-colors z-10"
 aria-label="Close disclaimer"
 title="Close"
 >
 <X className="w-4 h-4" />
 </button>
 )}
 {/* Header — small project mark. Version badge retired 2026-07-04
 — the DISCLAIMER_VERSION constant still keys the
 localStorage ack (see projectMeta.ts) but doesn't need to
 surface on the reader's screen.
 Session-31 (2026-07-04): "Z-SPAN" text swapped for the
 green highway wordmark logo per operator direction — a
 recognizable branded asset reads stronger than the
 uppercase abbreviation.
 Session-32 (2026-07-04): wordmark swapped for the compact
 interstate-shield badge (`zspan-shield.png`, the same
 transparent-bg asset used on BroadcastPage's sidebar
 channel-mark). Shape reads as a decorative badge next to
 the eyebrow text rather than a repeated brand-name; the
 square silhouette also sits better as an eyebrow-height
 icon than the wide wordmark. */}
 <div className="flex items-center gap-2 mb-6">
 <img
 src="/brand/zspan-shield.png"
 alt="Z-SPAN"
 className="h-6 w-6 object-contain select-none flex-shrink-0"
 draggable={false}
 />
 <span className="text-[11px] uppercase tracking-[0.18em] text-foreground/45">
 · Public Data Disclaimer
 </span>
 </div>

 {/* STATE 1: READING — disclaimer body. Top paragraphs operator-
 revised 2026-06-26 (intent statement + numbered list); further
 revised: "intentions" → "goal";
 "Translating" → "Converting"; strikethrough on item 2's
 back-half removed entirely; "claim" → "generation"; "cited
 supporting evidence" → "cited evidence"; ", or" tail on
 chunks bullet dropped; speaker's → officials own words; the
 authoritative-source paragraph rewritten with *not* markers
 rendered blood-red and the follow-the-citations sentence
 wrapped in the .kd-highlight-nuance highlighter-orange mark
 (the natural-paper marker aesthetic Key Decisions uses); the
 "disregard this disclaimer" paragraph collapsed to a single
 "if you take anything without second thought" sentence
 (operator-voice comma before em-dash preserved verbatim). */}
 {stage === "reading" && (() => {
 // Session-30 cascade: every rendered word carries an inline
 // animation-delay so words reveal sequentially across
 // stage1DwellMs. Structural JSX (list bullets, marks, links,
 // the button itself) render at their reading position via
 // cJSX — each cJSX unit consumes one cascade slot equivalent
 // to one word. Whitespace preserved raw.
 //
 // Session-32 (2026-07-04): TOTAL_CASCADE_UNITS is now derived
 // programmatically via a dry-count pass over the anchoring
 // content (the top paragraphs through the [Click here]
 // button). Previously hardcoded to 130, which was tuned to
 // the operator's design intent: cascade rate scales to the
 // TOP portion only so the last "to proceed." word finishes
 // revealing right at the [Click here] unlock; footer copy
 // overflows past 10s at karaoke pace, which is fine (it's
 // supplementary reading, not gate-blocking). Deriving from
 // renderAnchoringContent() preserves that intent while
 // eliminating the fragile constant — any copy change to the
 // anchoring paragraphs now re-tunes automatically.
 type C = (text: string) => ReactNode[];
 type CJsx = (element: ReactNode) => ReactNode;

 // The anchoring content: top paragraphs through "to proceed.".
 // The cascade rate is tuned to fit THIS content into stage1DwellMs.
 const renderAnchoringContent = (c: C, cJSX: CJsx): ReactNode => (
 <>
 <p>
 {c("Z-SPAN is a solo artificial intelligence project")}
 {c(" with the goal of:")}
 </p>

 <ol className="list-decimal pl-6 space-y-1.5">
 <li>{c("Strengthening democracy")}</li>
 <li>{c("Converting city council meetings to a more ‘digestible’ format")}</li>
 <li>{c("Encouraging civic participation")}</li>
 </ol>

 <p>
 {c("Every generation you see ")}
 <em className="italic font-medium text-white">{c("will")}</em>
 {c(" be backed with cited evidence")}
 {c(" in the form of:")}
 </p>

 <ul className="list-disc pl-6 space-y-1.5">
 <li>{c("An auditable trail of semantic & contextually relevant “chunks”")}</li>
 <li>{c("The officials own words, verbatim")}</li>
 </ul>

 <p>
 {c("I am ")}
 <strong className="font-bold text-red-600">{c("not")}</strong>
 {c(" claiming every single output is 100% accurate. ")}
 <mark className="kd-highlight-nuance">
 {c("It is up to ")}
 <em className="italic font-medium text-white">{c("you")}</em>
 {c(" to follow the citations, and ensure what you are reading is factually correct.")}
 </mark>
 </p>

 <p>
 {c("If you understand, please ")}
 {cJSX(
 <button
 type="button"
 onClick={advanceToConfirming}
 disabled={!clickHereEnabled}
 className={
 clickHereEnabled
 ? "inline-flex items-center px-3 py-1 rounded border border-emerald-400/50 bg-emerald-400/10 text-emerald-200 hover:border-emerald-400 hover:bg-emerald-400/20 font-medium transition"
 : "inline-flex items-center px-3 py-1 rounded border border-white/15 bg-white/[0.03] text-foreground/40 cursor-not-allowed font-medium"
 }
 >
 {clickHereEnabled ? "Click here" : `Click here (${dwellRemaining}s)`}
 </button>,
 )}
 {c(" to proceed.")}
 </p>
 </>
 );

 // Supplementary contribute-via-GitHub paragraph removed
 // 2026-07-25 honesty sweep): the repo is private while
 // open-sourcing is postponed, so directing visitors to it was
 // a dead link. Restore from git history at the reopening.

 // Pass 1 — dry count over anchoring content only. Returns
 // discardable JSX; only the side-effect on countRef matters.
 const countRef = { current: 0 };
 const countingC: C = (text) => {
 const tokens = text.split(/(\s+)/);
 tokens.forEach((token) => {
 if (token === "" || /^\s+$/.test(token)) return;
 countRef.current++;
 });
 return [];
 };
 const countingCJSX: CJsx = () => {
 countRef.current++;
 return null;
 };
 // Invoke to populate countRef; the returned JSX is thrown away.
 void renderAnchoringContent(countingC, countingCJSX);
 const TOTAL_CASCADE_UNITS = Math.max(countRef.current, 1);
 const msPerUnit = timings.stage1DwellMs / TOTAL_CASCADE_UNITS;

 // Pass 2 — real render. Shared cIdx counter across anchoring
 // + supplementary so the footer keeps cascading past the
 // Click-here unlock as intended.
 const cIdx = { current: 0 };
 const cSpanStyle = (idx: number) => ({
 animationDelay: `${idx * msPerUnit}ms`,
 });
 const c: C = (text) => {
 const tokens = text.split(/(\s+)/);
 const out: ReactNode[] = [];
 tokens.forEach((token, i) => {
 if (token === "") return;
 if (/^\s+$/.test(token)) {
 out.push(token);
 return;
 }
 const idx = cIdx.current++;
 out.push(
 <span
 key={`w-${idx}-${i}`}
 className="zs-etch-cascade"
 style={cSpanStyle(idx)}
 >
 {token}
 </span>,
 );
 });
 return out;
 };
 const cJSX: CJsx = (element) => {
 const idx = cIdx.current++;
 return (
 <span
 key={`j-${idx}`}
 className="zs-etch-cascade"
 style={cSpanStyle(idx)}
 >
 {element}
 </span>
 );
 };
 return (
 <div className="space-y-5 text-base leading-relaxed text-foreground/80">
 {renderAnchoringContent(c, cJSX)}
 </div>
 );
 })()}

 {/* STATE 2: CONFIRMING — etch-reveal acknowledgment sentence.
 Per operator 2026-06-25: the words should feel etched into
 the page in front of the reader as they render — the same
 effect as the lightbulb animation on the homepage footer
 text. Two sentences across a line break per operator's post-
 visual iteration; "is not 100% accurate" carries the red
 color modifier (zs-etch-red) on top of zs-etch-revealed for
 emphasis. Distinct from CreatorsSignupPage::StepTwo
 highlight-current-word karaoke — here, once a word is
 revealed it STAYS bright (white or red, per segment) with
 subtle glow. Unrevealed words occupy layout space
 (zs-etch-hidden) so the line doesn't reflow as words
 appear. */}
 {stage === "confirming" && (
 <div className="space-y-6 py-4">
 {/* "Etching…" eyebrow retired 2026-07-04 — the
 karaoke-etch itself IS the visual signal, so labeling it
 with a schema-shape word was redundant and lingered on
 screen after the animation completed. */}
 <p className="text-xl leading-relaxed">
 {ackTokens.map((tok, i) => {
 if (tok.kind === "newline") {
 return <br key={i} />;
 }
 // Session-30 (2026-07-04): look-ahead to skip the
 // trailing space when the next word starts with
 // punctuation — fixes the "accurate , and" artifact
 // where a segment-boundary comma inherits a leading
 // space from the prior word's unconditional trailer.
 const nextWord = ackTokens
 .slice(i + 1)
 .find((t): t is { kind: "word"; text: string; wIdx: number; red: boolean } => t.kind === "word");
 const nextStartsWithPunct = !!nextWord && /^[,.;:!?]/.test(nextWord.text);
 const revealedClass = tok.wIdx <= karaokeWordIdx
 ? "zs-etch-revealed"
 : "zs-etch-hidden";
 const redClass = tok.red ? " zs-etch-red" : "";
 return (
 <span key={i} className={`${revealedClass}${redClass}`}>
 {tok.text}{nextStartsWithPunct ? "" : " "}
 </span>
 );
 })}
 </p>

 {/* 2026-06-26: Three buttons in neutral styling — operator
 explicitly removed color coding so users can't
 subconsciously pattern-match a "good color" to click
 through. They have to read each label. Sure + Okay
 reset to STATE 1 (you didn't read carefully — go back
 and try again); I Understand advances to STATE 3.
 Session-31 (2026-07-04): buttons container always renders
 (reserves space) + fades opacity when karaokeCompleted
 flips. Previous mount-on-completion caused a height jump
 + snap-flash that read as jarring — operator flagged.
 `pointer-events-none` while hidden so premature clicks
 can't reach the invisible buttons. */}
 <div
 className={`pt-4 flex items-center justify-end gap-3 flex-wrap transition-opacity duration-500 ease-out ${
 karaokeCompleted
 ? "opacity-100"
 : "opacity-0 pointer-events-none"
 }`}
 aria-hidden={!karaokeCompleted}
 >
 <button
 type="button"
 onClick={resetToReading}
 className="inline-flex items-center px-5 py-2 rounded border border-white/20 bg-white/5 text-foreground/80 hover:border-white/40 hover:bg-white/10 font-medium transition"
 >
 Sure
 </button>
 <button
 type="button"
 onClick={resetToReading}
 className="inline-flex items-center px-5 py-2 rounded border border-white/20 bg-white/5 text-foreground/80 hover:border-white/40 hover:bg-white/10 font-medium transition"
 >
 Okay
 </button>
 <button
 type="button"
 onClick={onAcknowledge}
 className="inline-flex items-center px-5 py-2 rounded border border-white/20 bg-white/5 text-foreground/80 hover:border-white/40 hover:bg-white/10 font-medium transition"
 >
 I Understand
 </button>
 </div>
 </div>
 )}
 </div>
 </div>
 );
}

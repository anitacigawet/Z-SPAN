/**
 * V1.5-Query-1 + V1.5-ByokPanel-Polish-1 — active BYOK query panel.
 *
 * Renders when byokConfig is set. Replaces the BYOK-locked "Ask anything"
 * surface on BroadcastPage with an active chat shape that fires the user's
 * BYOK provider (Gemini direct OR OpenAI/Anthropic via /api/byok/relay)
 * with chunks + system prompt from /api/rag-search.
 *
 * Chat shape (parity with BroadcastPage's existing chat-passthrough surface):
 * - Scrollable history above, input pinned at bottom
 * - User queries render as right-aligned bubbles
 * - Answers render as left-aligned text with green-pill `[at MM:SS]`
 * citation chips (shared KaraokeText component)
 * - Pending state shows the "..." bouncing-dots loader
 * - Per-turn cost + token + run_id stamp under each answer
 *
 * Operator-trust UX (preserved from V1.5-Query-1):
 * - Live cost + token counter inline after every response
 * - Per-session spend running total visible
 * - Cancel button mid-flight (fetch abort)
 * - Rate-limit countdown on Send button
 * - Per-minute cap with cooldown toast
 * - Per-session spend-threshold confirm before next query
 * - Citation [at MM:SS] inline chips clickable — fires onCitationClick
 * which BroadcastPage wires to seekVideoTo() for YouTube/MP4/Granicus
 */

import React, { useEffect, useRef, useState } from "react";
import { Send, X, Settings as SettingsIcon, KeyRound, AlertTriangle } from "lucide-react";
import {
 ByokConfig,
 ByokSettings,
 ByokQueryResult,
 executeByokQuery,
 getByokSettings,
 formatCost,
} from "@/lib/byok";
import { KaraokeText, KaraokeLoadingDots } from "@/lib/karaokeRender";
import { evaluateLibrarianQuery } from "@/lib/librarianQueryStencil";

interface ByokQueryPanelProps {
 meetingId: number;
 byokConfig: ByokConfig;
 onOpenSettings: () => void;
 /** Optional callback for clicking a [at MM:SS] citation — receives the
 * seconds offset so the video player can seek. BroadcastPage wires this
 * to its seekVideoTo() which handles YouTube/MP4/Granicus dispatch. */
 onCitationClick?: (seconds: number) => void;
 /** Pre-canned question prompts shown only in the empty state (history
 * has zero turns). They prefill the input rather than firing — matches
 * the existing chat-passthrough surface's suggestion-card pattern at
 * BroadcastPage.tsx:2728-2736. They disappear the moment the user sends
 * their first query, leaving the chat history clean. */
 suggestedQueries?: string[];
 /** Session-30 (2026-07-04): fires whenever the panel's internal `sending`
 * state flips. BroadcastPage uses this to drive the Librarian
 * character-video animation (plays while sending, pauses + resets when
 * the LLM starts producing / finishes). Purely observational — the
 * panel never reads back from this callback. */
 onSendingChange?: (sending: boolean) => void;
 /** Apply the public Librarian grammar gate before rate limiting or send. */
 enforceInputGate?: boolean;
}

interface SessionStats {
 queries: number;
 totalCostUsd: number;
 totalInputTokens: number;
 totalOutputTokens: number;
}

interface Turn {
 id: string;
 query: string;
 pending: boolean;
 answer?: string;
 error?: string;
 provider?: string;
 inputTokens?: number;
 outputTokens?: number;
 costUsd?: number;
 runId?: string;
 /** V1.5-BYOK-Verbatim-1 (2026-07-04) — retrieved chunks whose bodies
 * drive the verbatim-substring highlighter on this turn's answer.
 * Set in the final updateTurn once the stream completes; undefined
 * during pending / streaming (highlights appear all-at-once at
 * completion, which reads as intentional rather than piecemeal). */
 chunks?: Array<{
 chunk_index: number;
 start_seconds: number;
 body: string;
 }>;
}

const initialStats: SessionStats = {
 queries: 0,
 totalCostUsd: 0,
 totalInputTokens: 0,
 totalOutputTokens: 0,
};

export function ByokQueryPanel({
 meetingId,
 byokConfig,
 onOpenSettings,
 onCitationClick,
 suggestedQueries,
 onSendingChange,
 enforceInputGate = false,
}: ByokQueryPanelProps) {
 const [query, setQuery] = useState<string>("");
 const [inputGateError, setInputGateError] = useState<string | null>(
 null,
 );
 const [sending, setSending] = useState<boolean>(false);

 // Session-31 (2026-07-04): Librarian animation was previously coupled
 // 1:1 to `sending`, so the video played the entire duration of the
 // stream and stopped only at completion. Operator direction: stop the
 // animation when text STARTS streaming (first token arrives) — the
 // Librarian's job is signaling "I'm getting ready to answer"; once
 // text is arriving, the answer IS the signal. So `onSendingChange` is
 // now fired imperatively at the animation boundaries (start of send,
 // first token, or error-before-first-token), decoupled from the
 // internal `sending` state that still governs the Send↔Cancel button
 // + rate-limit tracking (which stay active for the whole stream).
 const [history, setHistory] = useState<Turn[]>([]);
 const [settings, setSettings] = useState<ByokSettings>(() => getByokSettings());
 const [sessionStats, setSessionStats] = useState<SessionStats>(initialStats);
 const [cooldownRemaining, setCooldownRemaining] = useState<number>(0);
 const [perMinuteCooldownRemaining, setPerMinuteCooldownRemaining] = useState<number>(0);

 // Rate-limit bookkeeping. Refs (not state) so they update synchronously
 // + don't trigger re-renders mid-flight.
 const lastQueryAtRef = useRef<number>(0);
 const queryTimestampsRef = useRef<number[]>([]);
 const abortControllerRef = useRef<AbortController | null>(null);
 const turnCounterRef = useRef<number>(0);
 const messagesEndRef = useRef<HTMLDivElement | null>(null);

 // Re-read settings when the modal might have changed them.
 useEffect(() => {
 const handler = () => setSettings(getByokSettings());
 window.addEventListener("storage", handler);
 return () => window.removeEventListener("storage", handler);
 }, []);

 // Min-seconds-between-queries countdown tick
 useEffect(() => {
 if (cooldownRemaining <= 0) return;
 const id = setTimeout(() => setCooldownRemaining(cooldownRemaining - 1), 1000);
 return () => clearTimeout(id);
 }, [cooldownRemaining]);

 // Per-minute cooldown countdown tick
 useEffect(() => {
 if (perMinuteCooldownRemaining <= 0) return;
 const id = setTimeout(() => setPerMinuteCooldownRemaining(perMinuteCooldownRemaining - 1), 1000);
 return () => clearTimeout(id);
 }, [perMinuteCooldownRemaining]);

 // Auto-scroll to bottom on history change (matches BroadcastPage chat).
 useEffect(() => {
 messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
 }, [history, sending]);

 const updateTurn = (id: string, patch: Partial<Turn>) => {
 setHistory((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
 };

 const handleSend = async () => {
 if (sending) return;

 let sentQuery = query.trim();
 if (enforceInputGate) {
 const gateResult = evaluateLibrarianQuery(query);
 if (!gateResult.ok) {
 setInputGateError(
 gateResult.message ?? "Write one focused question.",
 );
 return;
 }
 sentQuery = gateResult.canonicalQuery!;
 setInputGateError(null);
 } else if (!sentQuery) {
 return;
 }

 const now = Date.now();

 // Rate-limit: min seconds between queries
 const elapsedSecondsSinceLast = (now - lastQueryAtRef.current) / 1000;
 if (lastQueryAtRef.current > 0 && elapsedSecondsSinceLast < settings.min_seconds_between_queries) {
 const remaining = Math.ceil(settings.min_seconds_between_queries - elapsedSecondsSinceLast);
 setCooldownRemaining(remaining);
 return;
 }

 // Rate-limit: per-minute cap (rolling 60s window)
 const sixtySecondsAgo = now - 60_000;
 queryTimestampsRef.current = queryTimestampsRef.current.filter((t) => t > sixtySecondsAgo);
 if (queryTimestampsRef.current.length >= settings.per_minute_query_cap) {
 setPerMinuteCooldownRemaining(30);
 return;
 }

 // Per-session spend-warn threshold confirm
 if (
 sessionStats.totalCostUsd >= settings.per_session_spend_warn_usd &&
 !window.confirm(
 `You've spent ${formatCost(sessionStats.totalCostUsd)} this session across ${sessionStats.queries} queries. Continue?`,
 )
 ) {
 return;
 }

 turnCounterRef.current += 1;
 const turnId = `turn-${turnCounterRef.current}`;
 const newTurn: Turn = {
 id: turnId,
 query: sentQuery,
 pending: true,
 };
 setHistory((prev) => [...prev, newTurn]);
 setQuery("");
 setSending(true);
 onSendingChange?.(true); // Librarian animation START
 lastQueryAtRef.current = now;
 queryTimestampsRef.current.push(now);

 const controller = new AbortController();
 abortControllerRef.current = controller;
 let firstTokenSeen = false;

 try {
 const result: ByokQueryResult = await executeByokQuery(
 meetingId,
 sentQuery,
 byokConfig,
 settings,
 {
 signal: controller.signal,
 // V1.5-BYOK-Stream-1 (2026-07-04) — flip pending → false on the
 // FIRST token so the loading dots disappear immediately + the
 // answer text starts typing. Subsequent tokens append. Metadata
 // (cost / tokens / run_id) still lands via the final updateTurn
 // below once the stream completes cleanly.
 //
 // Session-31 (2026-07-04): also fire onSendingChange(false) on
 // the FIRST token so the Librarian animation stops as soon as
 // the answer starts flowing (operator direction: the Librarian
 // animation should stop when text starts streaming, not hold
 // until the very end). The internal `sending` state
 // stays true through the rest of the stream to keep the
 // Cancel button + rate-limit tracking correct.
 onDelta: (delta) => {
 if (!firstTokenSeen) {
 firstTokenSeen = true;
 onSendingChange?.(false); // Librarian animation STOP
 }
 setHistory((prev) =>
 prev.map((t) =>
 t.id === turnId
 ? { ...t, pending: false, answer: (t.answer ?? "") + delta }
 : t,
 ),
 );
 },
 },
 );
 updateTurn(turnId, {
 pending: false,
 answer: result.answer,
 provider: result.provider,
 inputTokens: result.inputTokens,
 outputTokens: result.outputTokens,
 costUsd: result.costUsd,
 runId: result.runId,
 // V1.5-BYOK-Verbatim-1 — carry the chunk bodies onto the turn
 // so KaraokeText can highlight verbatim substrings + wire the
 // click-to-hear seek. Landing here (after the stream completes)
 // means the highlights appear in one intentional pass rather
 // than progressively per token.
 chunks: result.chunks.map((c) => ({
 chunk_index: c.chunk_index,
 start_seconds: c.start_seconds,
 body: c.body,
 })),
 });
 setSessionStats((prev) => ({
 queries: prev.queries + 1,
 totalCostUsd: prev.totalCostUsd + result.costUsd,
 totalInputTokens: prev.totalInputTokens + result.inputTokens,
 totalOutputTokens: prev.totalOutputTokens + result.outputTokens,
 }));
 } catch (e) {
 const isAbort = e instanceof Error && e.name === "AbortError";
 updateTurn(turnId, {
 pending: false,
 error: isAbort ? "Cancelled." : e instanceof Error ? e.message : String(e),
 });
 // Session-31 audit-fix — stop the Librarian animation IMMEDIATELY
 // on error path so the video doesn't keep playing while the error
 // message is already displayed. Prior state waited for the finally
 // block, which felt janky when the error surfaced fast.
 if (!firstTokenSeen) {
 onSendingChange?.(false);
 }
 } finally {
 setSending(false);
 if (!firstTokenSeen) {
 // Backstop: honest-empty (rag-search returned no chunks so no
 // stream fired at all). The catch above already fired for the
 // error path — this only kicks in when executeByokQuery returned
 // normally with zero tokens.
 onSendingChange?.(false);
 }
 abortControllerRef.current = null;
 }
 };

 const handleCancel = () => {
 abortControllerRef.current?.abort();
 };

 const sendDisabled =
 sending || !query.trim() || cooldownRemaining > 0 || perMinuteCooldownRemaining > 0;

 return (
 <div className="flex flex-col flex-1 min-h-0 border-t border-white/5">
 {/* History — scrollable, history above input (chat shape parity) */}
 <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-4 space-y-5 min-h-0">
 {history.length === 0 ? (
 <div className="space-y-5">
 {/* Session-30 (2026-07-04): the italic tagline that lived
 here was the styling operator liked from the Librarian
 subtitle, so it moved up into the Librarian header (see
 BroadcastPage.tsx). Rendering it here too was a dupe. */}
 {suggestedQueries && suggestedQueries.length > 0 && (
 <div className="space-y-1.5">
 <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-600">
 Suggestions
 </p>
 {suggestedQueries.slice(0, 3).map((q, i) => (
 <button
 key={i}
 type="button"
 onClick={() => setQuery(q)}
 className="block w-full text-left text-[12px] leading-snug text-gray-400 hover:text-white px-3 py-2 rounded-md bg-white/[0.02] hover:bg-white/[0.05] border border-white/[0.04] hover:border-white/10 transition-colors"
 >
 {q}
 </button>
 ))}
 </div>
 )}
 </div>
 ) : (
 history.map((t) => (
 <div key={t.id} className="space-y-3">
 {/* User query — right-aligned bubble */}
 <div className="flex justify-end">
 <div className="bg-[#2A2A2D] text-white px-4 py-2.5 rounded-2xl rounded-tr-sm text-[13px] font-medium max-w-[85%]">
 {t.query}
 </div>
 </div>
 {/* Pending / error / answer — left-aligned */}
 <div className="flex justify-start">
 {t.pending ? (
 <KaraokeLoadingDots />
 ) : t.error ? (
 <div className="text-[13px] text-red-400 font-medium flex items-start gap-2 max-w-[95%]">
 <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
 <span>{t.error}</span>
 </div>
 ) : (
 <div className="text-[14px] leading-relaxed text-gray-300 font-medium pt-1 max-w-[95%] whitespace-pre-wrap">
 {/* V1.5-BYOK-Verbatim-1 — KaraokeText handles both
 layers: green [at MM:SS] pills (LLM-emitted
 timecodes) + click-to-hear verbatim highlighter
 marks (chunk substrings). Chunks arrive on the
 turn once the stream completes; highlights
 appear then. The chip-pass runs even without
 chunks, so streaming still shows timecodes. */}
 {t.answer ? (
 <KaraokeText
 text={t.answer}
 onSeek={onCitationClick}
 chunks={t.chunks}
 onVerbatimClick={onCitationClick}
 />
 ) : (
 <span></span>
 )}
 {/* Per-turn cost + run_id footer */}
 {t.provider && (
 <div className="flex items-center gap-2 text-[10px] text-white/30 font-mono pt-2 mt-2 border-t border-white/5 flex-wrap">
 <span>{t.provider}</span>
 <span>·</span>
 <span>
 {t.inputTokens} in + {t.outputTokens} out tokens
 </span>
 <span>·</span>
 <span className="text-[#22C55E]/70">{formatCost(t.costUsd ?? 0)}</span>
 {t.runId && (
 <>
 <span>·</span>
 <a
 href={`/api/verify-run/${encodeURIComponent(t.runId)}`}
 target="_blank"
 rel="noopener noreferrer"
 className="text-white/30 hover:text-white/60 underline"
 title="Verify this run's provenance"
 >
 {t.runId.slice(0, 24)}…
 </a>
 </>
 )}
 </div>
 )}
 </div>
 )}
 </div>
 </div>
 ))
 )}
 {/* Per-minute cooldown surface — non-blocking, sits above input */}
 {perMinuteCooldownRemaining > 0 && (
 <div className="text-[12px] text-amber-400/80 italic px-1">
 Per-minute cap ({settings.per_minute_query_cap}) hit — {perMinuteCooldownRemaining}s cooldown.
 </div>
 )}
 <div ref={messagesEndRef} />
 </div>

 {/* Input row pinned at bottom */}
 <div className="p-4 border-t border-white/5 flex-shrink-0">
 {inputGateError && (
 <p className="mb-2 px-1 text-[12px] leading-snug text-red-400">
 {inputGateError}
 </p>
 )}
 <div className="flex gap-3 items-center">
 <div className="relative flex-1">
 <input
 type="text"
 value={query}
 onChange={(e) => {
 setQuery(e.target.value);
 if (inputGateError) setInputGateError(null);
 }}
 onKeyDown={(e) => {
 if (e.key === "Enter" && !e.shiftKey) {
 e.preventDefault();
 handleSend();
 }
 }}
 placeholder={
 perMinuteCooldownRemaining > 0
 ? `Per-minute cap — ${perMinuteCooldownRemaining}s cooldown`
 : "Ask anything"
 }
 disabled={sending || perMinuteCooldownRemaining > 0}
 className="w-full bg-[#0E0E10] border border-[#22C55E]/30 h-12 rounded-full pl-5 pr-32 text-[14px] text-white placeholder:text-gray-500 focus:outline-none focus:border-[#22C55E]/60 disabled:opacity-60"
 />
 <div className="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-1.5">
 {/* Local-workspace mode (zspan CLI `open`): the key lives in
 ~/.zspan/config.json and is managed by `zspan init` in the
 terminal — the flagship's in-memory key modal would be
 the wrong surface, so the gear hides. */}
 {byokConfig.provider !== "local-workspace" && (
 <button
 type="button"
 onClick={onOpenSettings}
 className="text-white/40 hover:text-white/80 transition-colors"
 title="BYOK settings · provider · key management"
 >
 <SettingsIcon className="w-3.5 h-3.5" />
 </button>
 )}
 <KeyRound className="w-4 h-4 text-[#22C55E]/60" />
 <span className="text-[8px] uppercase tracking-widest text-[#22C55E]/70 leading-none">
 {byokConfig.provider === "local-workspace" ? "your key" : "BYOK"}
 </span>
 </div>
 </div>
 {sending ? (
 <button
 type="button"
 onClick={handleCancel}
 className="h-12 w-12 rounded-full bg-red-500/20 hover:bg-red-500/40 text-red-400 shrink-0 flex items-center justify-center cursor-pointer"
 title="Cancel this query"
 >
 <X className="w-4 h-4" />
 </button>
 ) : (
 <button
 type="button"
 onClick={handleSend}
 disabled={sendDisabled}
 className="h-12 w-12 rounded-full bg-[#22C55E]/20 hover:bg-[#22C55E]/40 text-[#22C55E] shrink-0 flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed transition-colors relative"
 title={
 cooldownRemaining > 0
 ? `Wait ${cooldownRemaining}s before next query`
 : byokConfig.provider === "local-workspace"
 ? "Send query (your stored key, from this machine)"
 : `Send query (powered by ${byokConfig.provider})`
 }
 >
 {cooldownRemaining > 0 ? (
 <span className="text-[11px] font-mono">{cooldownRemaining}</span>
 ) : (
 <Send className="w-4 h-4 ml-0.5" />
 )}
 </button>
 )}
 </div>

 {/* Session counter — always visible footer when at least one query fired */}
 {sessionStats.queries > 0 && (
 <div className="text-[10px] text-white/30 font-mono mt-2 text-right">
 This session: {sessionStats.queries} {sessionStats.queries === 1 ? "query" : "queries"} · {formatCost(sessionStats.totalCostUsd)} · {sessionStats.totalInputTokens + sessionStats.totalOutputTokens} total tokens
 </div>
 )}
 </div>
 </div>
 );
}

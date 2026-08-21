/**
 * OperatorSearchModal — V1.5-OperatorSearch-1 surface.
 *
 * Owner-only natural-language cross-meeting search. Ships ahead of V2
 * because operator-only scope sidesteps every S-008/S-056/D-126
 * V2-public-query gate (no public user touches it, no untrusted input).
 *
 * Modal floats over the current page; closing returns the operator to
 * wherever they were (DP-1=b per the chunk plan). The four phases land
 * incrementally — this commit ships Phase 1 (intent-interpret +
 * Refine/Proceed affordance). Phases 2-4 plug into the same state
 * machine + add cost-confirm / fan-out execution / citation chips.
 */
import { Fragment, useEffect, useRef, useState } from "react";
import type { ReactElement } from "react";
import { ChevronDown, Loader2, Play, Search, X } from "lucide-react";
import { InlineMeetingMomentPlayer } from "./InlineMeetingMomentPlayer";

interface Interpretation {
  state: string | null;
  county: string | null;
  city: string | null;
  keywords: string[];
  date_range: { after: string | null; before: string | null } | null;
  confidence: "high" | "medium" | "low";
}

interface InterpretResponse {
  success: boolean;
  interpretation?: Interpretation;
  prompt_template_version?: string;
  meeting_ids?: number[];
  match_count?: number;
  indexed_count?: number;
  unindexed_count?: number;
  error?: string;
  raw_output?: string;
}

interface OperatorSearchModalProps {
  // The query the operator typed in TopBarSearch. null = closed.
  query: string | null;
  onClose: () => void;
  // V1.5-OperatorSearch-1 Phase 4 — citation chips deep-link to the
  // broadcast page at the chunk's start timecode. The modal closes
  // automatically as part of the navigation.
  onNavigate?: (view: string, params?: any) => void;
}

interface Citation {
  meeting_id: number;
  city_name: string;
  meeting_date: string;
  chunk_index: number;
  vector_id: string;
  start_seconds: number;
  end_seconds: number;
  score: number;
  body: string;
  // Z3 — meetings.video_url JOINed server-side. May be null for
  // meetings without a video archive (Colorado City per S-037 V0).
  // Z4's InlineMeetingMomentPlayer classifies kind client-side.
  video_url: string | null;
}

// F6 audit-fix (2026-06-25 brainstorm-audit) — Whisper transcripts
// have artifacts like " ,000 people" (leading space before comma)
// and double-spaces. Light client-side normalize before rendering
// keeps the chunk text legibly civic-grade without damaging anything
// load-bearing. Source data on disk is unchanged.
function normalizeChunkBody(body: string): string {
  return body
    .replace(/ +([,.!?;:])/g, "$1")
    .replace(/[ \t]{2,}/g, " ");
}

interface LegDetail {
  meeting_id: number;
  city_name: string;
  meeting_date: string;
  interpreted_as: "ok" | "indexed_no_match" | "qdrant_down";
  chunks_used: number;
  retrieval_run_id: string | null;
  error: string | null;
}

interface ExecuteResponse {
  success: boolean;
  answer?: string;
  citations?: Citation[];
  leg_outcomes?: {
    ok_count: number;
    indexed_no_match_count: number;
    qdrant_down_count: number;
    details: LegDetail[];
  };
  provenance?: {
    run_id: string | null;
    child_run_ids: string[];
    synthesis_provider: string | null;
    synthesis_prompt_version: string;
    timestamp_utc: string;
  };
  error?: string;
}

type Phase =
  | { kind: "loading" }
  | { kind: "interpreted"; result: InterpretResponse }
  | { kind: "cost_confirm"; result: InterpretResponse }
  | { kind: "executing"; result: InterpretResponse }
  | { kind: "answered"; result: InterpretResponse; execute: ExecuteResponse }
  | { kind: "error"; message: string; raw?: string };

// Phase 2 — cost projection. The fan-out retrieves top-K chunks per
// indexed meeting then synthesizes cross-meeting via Sonnet.
//
// The Sonnet-via-MAX-cap test-loop path costs $0 incrementally (the
// subscription absorbs it per D-121); the cost panel says so explicitly
// rather than projecting tokens for a model that doesn't bill per-call.
// When the BYOK swap lands (Phase 3 follow-up), the same panel will
// project tokens against the user's provider rates.
const TOP_K_PER_MEETING = 8;
const MAX_UNION_CHUNKS = 50;
const APPROX_INPUT_TOKENS_PER_CHUNK = 150;
const APPROX_OUTPUT_TOKENS = 1500;

function projectCost(indexedCount: number) {
  const retrievalCalls = indexedCount;
  const rawChunks = indexedCount * TOP_K_PER_MEETING;
  const dedupedChunks = Math.min(rawChunks, MAX_UNION_CHUNKS);
  const inputTokens = dedupedChunks * APPROX_INPUT_TOKENS_PER_CHUNK;
  const outputTokens = APPROX_OUTPUT_TOKENS;
  return { retrievalCalls, rawChunks, dedupedChunks, inputTokens, outputTokens };
}

export function OperatorSearchModal({
  query,
  onClose,
  onNavigate,
}: OperatorSearchModalProps) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const containerRef = useRef<HTMLDivElement>(null);

  // Fire intent-interpret whenever the query changes. The query string is
  // also the modal's "open" signal — null means closed, set means open
  // with that query as the initial intent.
  useEffect(() => {
    if (!query) return;
    setPhase({ kind: "loading" });
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/operator-search/interpret", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ query }),
        });
        const data = (await res.json()) as InterpretResponse;
        if (cancelled) return;
        if (!res.ok || !data.success) {
          setPhase({
            kind: "error",
            message: data.error || `HTTP ${res.status}`,
            raw: data.raw_output,
          });
          return;
        }
        setPhase({ kind: "interpreted", result: data });
      } catch (err: any) {
        if (cancelled) return;
        setPhase({
          kind: "error",
          message: err?.message || "network error",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [query]);

  // ESC to close + outside-click to close.
  useEffect(() => {
    if (!query) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onClick = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    // Defer one tick so the click that opened the modal doesn't immediately close it.
    const t = setTimeout(() => document.addEventListener("mousedown", onClick), 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [query, onClose]);

  if (!query) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-start justify-center bg-black/60 px-4 pb-8 pt-24">
      <div
        ref={containerRef}
        className="flex max-h-[calc(100vh-8rem)] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-white/10 bg-[#0A0A0C] shadow-2xl"
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-4 border-b border-white/10 px-5 py-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-amber-400/80">
              <Search className="h-3 w-3" />
              Operator search
            </div>
            <div className="mt-1 truncate font-sans text-[15px] text-white/90">
              {query}
            </div>
            {/* Scope chip — visible once the interpretation has landed.
                Shows what scope the fan-out is operating against so the
                operator can see at a glance that this isn't a free-form
                search over the public internet — it's bounded to N
                indexed Z-SPAN meetings. */}
            <ScopeChip phase={phase} />
            {/* Operator-personal reminder (added 2026-06-30 session-16, per
                S-100). When V2 RAG search lands, this surface should reshape
                around the BigQuery AI.AGG-style "prompt + location" pill
                pair — one pill for the natural-language prompt, one for the
                scope. Remove this bubble + the surrounding chip when V2 RAG
                ships using that pattern. See FUTURE_THOUGHTS S-100. */}
            <div className="mt-2 inline-flex items-center gap-1.5 rounded-full border border-amber-400/25 bg-amber-400/5 px-2 py-0.5 text-[10px] italic text-amber-200/70">
              <span aria-hidden>💡</span>
              <span>V2 RAG → reshape around prompt+location pills (S-100 AI.AGG pattern)</span>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close operator search"
            className="flex-none rounded-md p-1.5 text-white/40 transition-colors hover:bg-white/5 hover:text-white/80"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Body — flex-1 + overflow-y-auto so the header stays sticky and
            the body scrolls when the answer + citations overflow. Prevents
            the modal from extending below the viewport with no internal
            scroll affordance (which was the 2026-06-25 failure mode James
            hit on the bus-route-changes synthesis answer). */}
        <div className="flex-1 overflow-y-auto px-5 py-5">
          {phase.kind === "loading" && (
            <div className="flex items-center gap-3 text-[13px] text-white/60">
              <Loader2 className="h-4 w-4 animate-spin text-amber-400/80" />
              <span>Resolving scope via Sonnet…</span>
            </div>
          )}

          {phase.kind === "error" && (
            <div className="space-y-2">
              <div className="text-[13px] text-rose-300">
                Couldn't interpret the query: {phase.message}
              </div>
              {phase.raw && (
                <pre className="overflow-x-auto rounded-md border border-white/10 bg-black/30 p-3 font-mono text-[11px] text-white/50">
                  {phase.raw}
                </pre>
              )}
              <button
                type="button"
                onClick={onClose}
                className="rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-[12px] text-white/80 transition-colors hover:bg-white/10"
              >
                Close
              </button>
            </div>
          )}

          {phase.kind === "interpreted" && (
            <InterpretationPanel
              result={phase.result}
              onClose={onClose}
              onProceed={() =>
                setPhase({ kind: "cost_confirm", result: phase.result })
              }
            />
          )}

          {phase.kind === "cost_confirm" && (
            <CostConfirmPanel
              result={phase.result}
              onCancel={() =>
                setPhase({ kind: "interpreted", result: phase.result })
              }
              onConfirm={async () => {
                const captured = phase.result;
                setPhase({ kind: "executing", result: captured });
                try {
                  const res = await fetch("/api/operator-search/execute", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    credentials: "include",
                    body: JSON.stringify({
                      query,
                      meeting_ids: captured.meeting_ids,
                      interpretation: captured.interpretation,
                    }),
                  });
                  const data = (await res.json()) as ExecuteResponse;
                  if (!res.ok || !data.success) {
                    setPhase({
                      kind: "error",
                      message: data.error || `HTTP ${res.status}`,
                    });
                    return;
                  }
                  setPhase({
                    kind: "answered",
                    result: captured,
                    execute: data,
                  });
                } catch (err: any) {
                  setPhase({
                    kind: "error",
                    message: err?.message || "network error",
                  });
                }
              }}
            />
          )}

          {phase.kind === "executing" && (
            <ExecutingPanel result={phase.result} />
          )}

          {phase.kind === "answered" && (
            <AnsweredPanel
              result={phase.result}
              execute={phase.execute}
              onClose={onClose}
              onNavigate={onNavigate}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// ── Interpretation render ─────────────────────────────────────────────

function InterpretationPanel({
  result,
  onClose,
  onProceed,
}: {
  result: InterpretResponse;
  onClose: () => void;
  onProceed: () => void;
}) {
  const i = result.interpretation;
  if (!i) return null;

  const scopeBits: string[] = [];
  if (i.state) scopeBits.push(i.state);
  if (i.county) scopeBits.push(i.county);
  if (i.city) scopeBits.push(i.city);
  const scopeLabel = scopeBits.length ? scopeBits.join(" · ") : "all locations";

  const dateLabel = (() => {
    if (!i.date_range) return "all dates";
    const after = i.date_range.after;
    const before = i.date_range.before;
    if (after && before) return `${after} → ${before}`;
    if (after) return `after ${after}`;
    if (before) return `before ${before}`;
    return "all dates";
  })();

  const indexed = result.indexed_count ?? 0;
  const unindexed = result.unindexed_count ?? 0;
  const noIndexedHits = indexed === 0;

  return (
    <div className="space-y-4">
      <div className="text-[10px] uppercase tracking-widest text-white/40">
        Scope
      </div>
      <div className="rounded-md border border-white/10 bg-white/[0.02] p-3 text-[13px] text-white/85">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span>{scopeLabel}</span>
          <span className="text-white/30">·</span>
          <span>{dateLabel}</span>
          <span className="text-white/30">·</span>
          <span
            className={`text-[10px] uppercase tracking-widest ${
              i.confidence === "high"
                ? "text-emerald-400/80"
                : i.confidence === "medium"
                  ? "text-amber-400/80"
                  : "text-rose-400/80"
            }`}
          >
            {i.confidence} confidence
          </span>
        </div>
        {i.keywords.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {i.keywords.map((k) => (
              <span
                key={k}
                className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[11px] text-white/75"
              >
                {k}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="text-[10px] uppercase tracking-widest text-white/40">
        Coverage
      </div>
      <div className="rounded-md border border-white/10 bg-white/[0.02] p-3 text-[13px] text-white/85">
        <div className="flex items-baseline gap-2">
          <span className="text-[20px] font-medium text-emerald-300">
            {indexed}
          </span>
          <span className="text-white/60">
            indexed meeting{indexed === 1 ? "" : "s"} ready to search
          </span>
        </div>
        {unindexed > 0 && (
          <div className="mt-1 text-[12px] text-white/45">
            {unindexed} matching meeting{unindexed === 1 ? "" : "s"} not yet
            V1-RAG-3 indexed — coverage will broaden as the worker drains.
          </div>
        )}
        {noIndexedHits && (
          <div className="mt-2 text-[12px] text-amber-300/80">
            No indexed meetings in this scope. Refine the query, or wait
            for indexing to broaden.
          </div>
        )}
      </div>

      {/* Phase 2-4 land here; for now the next step is a placeholder so
          the operator can confirm-or-refine without errors. */}
      <div className="flex items-center justify-end gap-2 border-t border-white/10 pt-4">
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-[12px] text-white/80 transition-colors hover:bg-white/10"
        >
          Refine
        </button>
        <button
          type="button"
          disabled={noIndexedHits}
          onClick={onProceed}
          className={`rounded-md border px-3 py-1.5 text-[12px] transition-colors ${
            noIndexedHits
              ? "cursor-not-allowed border-white/10 bg-white/[0.02] text-white/30"
              : "border-amber-400/40 bg-amber-400/10 text-amber-200 hover:bg-amber-400/20"
          }`}
        >
          Proceed → cost estimate
        </button>
      </div>

      {result.prompt_template_version && (
        <div className="border-t border-white/5 pt-3 text-[10px] text-white/30">
          Intent-parse template: {result.prompt_template_version}
        </div>
      )}
    </div>
  );
}

// ── Cost-confirm panel ────────────────────────────────────────────────

function CostConfirmPanel({
  result,
  onCancel,
  onConfirm,
}: {
  result: InterpretResponse;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const indexed = result.indexed_count ?? 0;
  const projection = projectCost(indexed);

  return (
    <div className="space-y-4">
      <div className="text-[10px] uppercase tracking-widest text-white/40">
        Cost estimate
      </div>
      <div className="rounded-md border border-white/10 bg-white/[0.02] p-3 text-[13px] text-white/85">
        <div className="space-y-1.5">
          <CostRow
            label="Retrieval calls"
            value={`${projection.retrievalCalls} × Surface Pro /query`}
            sublabel="local Qdrant — $0"
          />
          <CostRow
            label="Top-K per meeting"
            value={`${TOP_K_PER_MEETING}`}
            sublabel={`raw chunks: ${projection.rawChunks}, deduped to ≤${MAX_UNION_CHUNKS}`}
          />
          <CostRow
            label="Synthesis input"
            value={`~${projection.inputTokens.toLocaleString()} tokens`}
            sublabel={`${projection.dedupedChunks} chunks × ~${APPROX_INPUT_TOKENS_PER_CHUNK} tokens`}
          />
          <CostRow
            label="Synthesis output"
            value={`~${projection.outputTokens.toLocaleString()} tokens`}
            sublabel="single Sonnet pass"
          />
        </div>
        <div className="mt-3 border-t border-white/10 pt-3">
          <div className="flex items-baseline gap-2">
            <span className="text-[18px] font-medium text-emerald-300">$0</span>
            <span className="text-[12px] text-white/65">
              incremental — Sonnet via MAX cap (test-loop path)
            </span>
          </div>
          <div className="mt-1 text-[11px] text-white/40">
            BYOK swap (Phase 3 follow-up) will project against the
            operator's provider rate instead. The retrieval side stays
            $0 either way; only the synthesis token cost changes.
          </div>
        </div>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-white/10 pt-4">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-[12px] text-white/80 transition-colors hover:bg-white/10"
        >
          Back to scope
        </button>
        <button
          type="button"
          onClick={onConfirm}
          className="rounded-md border border-emerald-400/40 bg-emerald-400/10 px-3 py-1.5 text-[12px] text-emerald-200 transition-colors hover:bg-emerald-400/20"
        >
          Confirm → run search (Phase 3)
        </button>
      </div>
    </div>
  );
}

function CostRow({
  label,
  value,
  sublabel,
}: {
  label: string;
  value: string;
  sublabel?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-[12px]">
      <span className="text-white/55">{label}</span>
      <span className="text-right">
        <span className="text-white/90">{value}</span>
        {sublabel && (
          <span className="ml-2 text-white/40">· {sublabel}</span>
        )}
      </span>
    </div>
  );
}

// ── Executing panel ───────────────────────────────────────────────────

function ExecutingPanel({ result }: { result: InterpretResponse }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 text-[13px] text-white/80">
        <Loader2 className="h-4 w-4 animate-spin text-emerald-400/80" />
        <span>
          Retrieving across {result.indexed_count ?? 0} indexed meeting
          {(result.indexed_count ?? 0) === 1 ? "" : "s"} and synthesizing…
        </span>
      </div>
      <div className="rounded-md border border-white/10 bg-white/[0.02] p-3 text-[11px] text-white/40">
        Fan-out is concurrency-capped at 10 to respect Surface Pro's embed
        model lock; synthesis is a single Sonnet pass via the MAX cap.
        Typical wall-clock: ~15-60 seconds depending on chunk volume.
      </div>
    </div>
  );
}

// ── Scope chip ────────────────────────────────────────────────────────

function ScopeChip({ phase }: { phase: Phase }) {
  // Only render once interpretation has landed.
  const result = (() => {
    if (phase.kind === "interpreted") return phase.result;
    if (phase.kind === "cost_confirm") return phase.result;
    if (phase.kind === "executing") return phase.result;
    if (phase.kind === "answered") return phase.result;
    return null;
  })();
  if (!result) return null;
  const indexed = result.indexed_count ?? 0;
  const i = result.interpretation;
  const scopeBits: string[] = [];
  if (i?.state) scopeBits.push(i.state);
  if (i?.county) scopeBits.push(i.county);
  if (i?.city) scopeBits.push(i.city);
  const scopeLabel = scopeBits.length ? scopeBits.join(" · ") : "all locations";
  return (
    <div className="mt-2 inline-flex items-center gap-2 rounded-full border border-emerald-400/30 bg-emerald-400/[0.06] px-2.5 py-0.5 text-[10px] uppercase tracking-widest text-emerald-300/80">
      <span className="h-1.5 w-1.5 rounded-full bg-emerald-400/70" />
      <span>{scopeLabel}</span>
      <span className="text-emerald-300/40">·</span>
      <span>
        {indexed} indexed meeting{indexed === 1 ? "" : "s"}
      </span>
    </div>
  );
}

// ── Answered panel ────────────────────────────────────────────────────

// Group citations by (city, date). The expansion card shows ALL chunks
// for that meeting Sonnet retrieved, chronologically ordered by
// start_seconds. Per James 2026-06-25: each chip is the audit-trail
// entry point — the operator needs to see every chunk for the meeting,
// not just the highest-scored one.
function indexCitationsByTag(citations: Citation[]) {
  const byTag = new Map<string, Citation[]>();
  for (const c of citations) {
    const tag = `${c.city_name} · ${c.meeting_date}`;
    if (!byTag.has(tag)) byTag.set(tag, []);
    byTag.get(tag)!.push(c);
  }
  Array.from(byTag.values()).forEach((arr: Citation[]) => {
    arr.sort((a, b) => a.start_seconds - b.start_seconds);
  });
  return byTag;
}

// Permissive on whitespace around the middle dot to survive Sonnet
// variations. The trailing optional `· TIMECODE` is silently swallowed
// because the chip itself surfaces every chunk's timecode in the
// expansion card — the inline tag stays compact.
const CITATION_RE =
  /\[([A-Z][A-Za-z .'-]+?)\s*[·]\s*(\d{4}-\d{2}-\d{2})(?:\s*[·]\s*\d{1,2}:\d{2}(?::\d{2})?)?\]/g;

// ── Minimal markdown renderer ────────────────────────────────────────
//
// Sonnet's answer is Markdown. The subset it actually emits is small:
// **bold**, paragraph breaks (\n\n), occasional `---` separators,
// occasional `## headings`, occasional `- bullet` lists, single-newline
// hard breaks within a paragraph. We hand-roll a parser rather than
// pulling in react-markdown because the surface is bounded and the
// citation-chip integration needs control over inline tokenization.

type Block =
  | { kind: "paragraph"; lines: string[] }
  | { kind: "heading"; level: number; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "hr" };

function splitBlocks(answer: string): Block[] {
  const blocks: Block[] = [];
  const lines = answer.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const raw = lines[i];
    const trimmed = raw.trim();
    if (!trimmed) {
      i++;
      continue;
    }
    if (/^---+$/.test(trimmed) || /^\*\*\*+$/.test(trimmed)) {
      blocks.push({ kind: "hr" });
      i++;
      continue;
    }
    const h = trimmed.match(/^(#{1,3})\s+(.*)$/);
    if (h) {
      blocks.push({ kind: "heading", level: h[1].length, text: h[2] });
      i++;
      continue;
    }
    if (/^[-*]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*]\s+/, ""));
        i++;
      }
      blocks.push({ kind: "list", items });
      continue;
    }
    // Paragraph — accumulate consecutive non-empty non-special lines.
    const paraLines: string[] = [raw];
    i++;
    while (i < lines.length) {
      const next = lines[i];
      if (!next.trim()) break;
      if (/^---+$/.test(next.trim()) || /^\*\*\*+$/.test(next.trim())) break;
      if (/^#{1,3}\s+/.test(next.trim())) break;
      if (/^[-*]\s+/.test(next.trim())) break;
      paraLines.push(next);
      i++;
    }
    blocks.push({ kind: "paragraph", lines: paraLines });
  }
  return blocks;
}

// Tokenize an inline string into runs: plain text, bold, italic, and
// citation chips. Citation tokens are matched first; bold (**…**) and
// italic (*…*) are matched on the remaining text.
//
// Known limitation (F10 audit 2026-06-25): nested bold-italic
// (`***triple***`) doesn't round-trip cleanly — the parser sees `**`
// + `*` + close + close, which the recursive tokenizeInline handles
// for the `**bold *italic***` case but produces ambiguous output for
// `***triple***`. Sonnet rarely emits triple-asterisk wrapping in
// practice; if it becomes a real pattern, switch to a tokenizer
// that handles the ambiguity explicitly (or accept react-markdown).
type InlineRun =
  | { kind: "text"; text: string }
  | { kind: "bold"; children: InlineRun[] }
  | { kind: "italic"; children: InlineRun[] }
  | { kind: "citation"; raw: string; city: string; date: string };

function tokenizeInline(s: string): InlineRun[] {
  const out: InlineRun[] = [];
  let i = 0;
  while (i < s.length) {
    // Citation first — Sonnet's tags are non-overlapping with **/*.
    CITATION_RE.lastIndex = i;
    const cite = CITATION_RE.exec(s);
    if (cite && cite.index === i) {
      out.push({
        kind: "citation",
        raw: cite[0],
        city: cite[1].trim(),
        date: cite[2],
      });
      i += cite[0].length;
      continue;
    }
    // Bold **…**
    if (s.startsWith("**", i)) {
      const close = s.indexOf("**", i + 2);
      if (close !== -1) {
        out.push({ kind: "bold", children: tokenizeInline(s.slice(i + 2, close)) });
        i = close + 2;
        continue;
      }
    }
    // Italic *…*  (single asterisk, must not be followed by another *)
    if (s[i] === "*" && s[i + 1] !== "*") {
      const close = s.indexOf("*", i + 1);
      if (close !== -1 && s[close + 1] !== "*") {
        out.push({
          kind: "italic",
          children: tokenizeInline(s.slice(i + 1, close)),
        });
        i = close + 1;
        continue;
      }
    }
    // Walk text until the next special-token start.
    let next = i + 1;
    while (next < s.length) {
      const c = s[next];
      if (c === "*") break;
      if (c === "[") {
        CITATION_RE.lastIndex = next;
        const probe = CITATION_RE.exec(s);
        if (probe && probe.index === next) break;
      }
      next++;
    }
    out.push({ kind: "text", text: s.slice(i, next) });
    i = next;
  }
  return out;
}

// Render an inline run array into React elements. Citation chips are
// looked up against `byTag` and rendered as buttons that toggle the
// expansion of the meeting's chunk list.
function renderInline(
  runs: InlineRun[],
  byTag: Map<string, Citation[]>,
  expandedTags: Set<string>,
  onToggleTag: (tag: string) => void,
  keyPrefix: string,
): ReactElement[] {
  return runs.map((run, idx) => {
    const k = `${keyPrefix}-${idx}`;
    if (run.kind === "text") {
      return <span key={k}>{run.text}</span>;
    }
    if (run.kind === "bold") {
      return (
        <strong key={k} className="font-semibold text-white">
          {renderInline(run.children, byTag, expandedTags, onToggleTag, k)}
        </strong>
      );
    }
    if (run.kind === "italic") {
      return (
        <em key={k}>
          {renderInline(run.children, byTag, expandedTags, onToggleTag, k)}
        </em>
      );
    }
    // citation
    const tag = `${run.city} · ${run.date}`;
    const matched = byTag.get(tag);
    if (!matched) {
      return (
        <span
          key={k}
          className="mx-0.5 inline-flex items-center rounded-md border border-rose-400/30 bg-rose-400/[0.06] px-1.5 py-0.5 align-baseline text-[11px] text-rose-300/80 line-through"
          title="Tagged meeting not in retrieval set — possible hallucination"
        >
          {run.raw}
        </span>
      );
    }
    const isExpanded = expandedTags.has(tag);
    return (
      <CitationChip
        key={k}
        tag={tag}
        citations={matched}
        isExpanded={isExpanded}
        onToggle={() => onToggleTag(tag)}
      />
    );
  });
}

// Render a single block (paragraph / heading / list / hr) plus, AFTER
// the block, any expansion cards for chips that fired inside it. This
// preserves reading flow — finish the paragraph, then see the source
// for each cited claim in the same vertical column.
function renderBlock(
  block: Block,
  byTag: Map<string, Citation[]>,
  expandedTags: Set<string>,
  onToggleTag: (tag: string) => void,
  keywords: string[],
  blockKey: string,
  onOpenBroadcast?: (c: Citation) => void,
): ReactElement {
  // Find the citations to expand AFTER this block — the chips currently
  // expanded that appear inline in this block.
  const expandedForThisBlock: string[] = [];
  const collect = (s: string) => {
    const matches = Array.from(s.matchAll(CITATION_RE));
    for (const m of matches) {
      const tag = `${m[1].trim()} · ${m[2]}`;
      if (expandedTags.has(tag) && !expandedForThisBlock.includes(tag)) {
        expandedForThisBlock.push(tag);
      }
    }
  };

  let inner: ReactElement | null = null;
  if (block.kind === "paragraph") {
    block.lines.forEach(collect);
    const joined = block.lines.join("\n");
    // Split the paragraph back on \n so single-newline hard breaks
    // render as <br/>.
    const lineRuns = block.lines.map((line, li) => (
      <Fragment key={`${blockKey}-line-${li}`}>
        {li > 0 && <br />}
        {renderInline(
          tokenizeInline(line),
          byTag,
          expandedTags,
          onToggleTag,
          `${blockKey}-line-${li}`,
        )}
      </Fragment>
    ));
    inner = (
      <div className="text-[14px] leading-relaxed text-white/90">
        {lineRuns}
      </div>
    );
    void joined;
  } else if (block.kind === "heading") {
    collect(block.text);
    const headingClass =
      block.level === 1
        ? "text-[18px] font-semibold text-white"
        : block.level === 2
          ? "text-[16px] font-semibold text-white"
          : "text-[14px] font-semibold uppercase tracking-wider text-white/80";
    inner = (
      <div className={`${headingClass} mt-1`}>
        {renderInline(
          tokenizeInline(block.text),
          byTag,
          expandedTags,
          onToggleTag,
          `${blockKey}-h`,
        )}
      </div>
    );
  } else if (block.kind === "list") {
    block.items.forEach(collect);
    inner = (
      <ul className="ml-1 list-none space-y-1 text-[14px] leading-relaxed text-white/90">
        {block.items.map((item, li) => (
          <li key={`${blockKey}-li-${li}`} className="flex gap-2">
            <span className="select-none text-emerald-400/60">•</span>
            <span>
              {renderInline(
                tokenizeInline(item),
                byTag,
                expandedTags,
                onToggleTag,
                `${blockKey}-li-${li}`,
              )}
            </span>
          </li>
        ))}
      </ul>
    );
  } else if (block.kind === "hr") {
    inner = <hr className="border-white/10" />;
  }

  return (
    <Fragment key={blockKey}>
      {inner}
      {expandedForThisBlock.map((tag) => {
        const cits = byTag.get(tag);
        if (!cits) return null;
        return (
          <CitationCard
            key={`${blockKey}-card-${tag}`}
            tag={tag}
            citations={cits}
            keywords={keywords}
            onClose={() => onToggleTag(tag)}
            onOpenBroadcast={onOpenBroadcast}
          />
        );
      })}
    </Fragment>
  );
}

function renderAnswerWithChips(
  answer: string,
  byTag: Map<string, Citation[]>,
  expandedTags: Set<string>,
  onToggleTag: (tag: string) => void,
  keywords: string[],
  onOpenBroadcast?: (c: Citation) => void,
): ReactElement[] {
  const blocks = splitBlocks(answer);
  return blocks.map((b, i) =>
    renderBlock(
      b,
      byTag,
      expandedTags,
      onToggleTag,
      keywords,
      `b-${i}`,
      onOpenBroadcast,
    ),
  );
}

function AnsweredPanel({
  result,
  execute,
  onClose,
  onNavigate,
}: {
  result: InterpretResponse;
  execute: ExecuteResponse;
  onClose: () => void;
  onNavigate?: (view: string, params?: any) => void;
}) {
  const answer = execute.answer ?? "";
  const citations = execute.citations ?? [];
  const outcomes = execute.leg_outcomes;
  const provenance = execute.provenance;
  const byTag = indexCitationsByTag(citations);
  const keywords = result.interpretation?.keywords ?? [];

  // Each chip toggle adds/removes its tag to the expandedTags set;
  // expansion cards render below the block (paragraph/heading/list)
  // that triggered them.
  const [expandedTags, setExpandedTags] = useState<Set<string>>(new Set());
  const toggleTag = (tag: string) => {
    setExpandedTags((prev) => {
      const next = new Set(prev);
      if (next.has(tag)) next.delete(tag);
      else next.add(tag);
      return next;
    });
  };

  // Reserved for the "Open broadcast page" link inside each expansion
  // card — the chip itself no longer navigates per James 2026-06-25
  // ("i hate to see them redirect me to the other page").
  const openBroadcast = (c: Citation) => {
    if (!onNavigate) return;
    onClose();
    onNavigate("broadcast", {
      meetingId: c.meeting_id,
      seek: c.start_seconds,
    });
  };

  return (
    <div className="space-y-4">
      {/* Leg outcomes ribbon */}
      {outcomes && (
        <div className="flex items-center gap-3 text-[11px] text-white/55">
          <span>
            <span className="text-emerald-300">{outcomes.ok_count}</span>{" "}
            meeting{outcomes.ok_count === 1 ? "" : "s"} contributed chunks
          </span>
          {outcomes.indexed_no_match_count > 0 && (
            <span>
              ·{" "}
              <span className="text-white/40">
                {outcomes.indexed_no_match_count} indexed-no-match
              </span>
            </span>
          )}
          {outcomes.qdrant_down_count > 0 && (
            <span>
              ·{" "}
              <span className="text-rose-300">
                {outcomes.qdrant_down_count} qdrant-down
              </span>
            </span>
          )}
        </div>
      )}

      {/* Answer body — Markdown-rendered with toggleable citation chips.
          Chip click toggles an expansion card BELOW the paragraph
          showing ALL chunks Sonnet retrieved from that meeting plus
          keyword highlighting + a "play this moment" stub (Z3/Z4
          will wire the inline player). */}
      <div className="space-y-3 rounded-md border border-white/10 bg-white/[0.02] p-4">
        {renderAnswerWithChips(
          answer,
          byTag,
          expandedTags,
          toggleTag,
          keywords,
          openBroadcast,
        )}
      </div>

      {/* Citation list — Phase 4 styles these as green pill chips with
          deep-link to BroadcastPage?meeting_id=X&seek=Y. For now: a
          plain list so the operator can see what Sonnet was working
          from. */}
      {citations.length > 0 && (
        <details className="rounded-md border border-white/10 bg-white/[0.02] p-3" open>
          <summary className="cursor-pointer text-[11px] uppercase tracking-widest text-white/40">
            {citations.length} source chunk{citations.length === 1 ? "" : "s"}
          </summary>
          <div className="mt-2 space-y-2">
            {citations.map((c) => (
              <div
                key={c.vector_id}
                className="rounded border border-white/10 bg-black/30 p-2 text-[11px]"
              >
                <div className="flex items-center justify-between gap-2 text-white/60">
                  <span>
                    {c.city_name} · {c.meeting_date}
                  </span>
                  <span className="text-white/40">
                    {formatSeconds(c.start_seconds)} · score {c.score}
                  </span>
                </div>
                <div className="mt-1 text-white/70">
                  {c.body.length > 240 ? c.body.slice(0, 240) + "…" : c.body}
                </div>
              </div>
            ))}
          </div>
        </details>
      )}

      {/* Footer — run_id + actions */}
      <div className="flex items-center justify-between gap-3 border-t border-white/10 pt-3 text-[10px] text-white/30">
        <span className="truncate">
          {provenance?.run_id ? `Run: ${provenance.run_id}` : ""}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-white/15 bg-white/5 px-3 py-1 text-[12px] text-white/80 transition-colors hover:bg-white/10"
        >
          Close
        </button>
      </div>
    </div>
  );
}

function CitationChip({
  tag,
  citations,
  isExpanded,
  onToggle,
}: {
  tag: string;
  citations: Citation[];
  isExpanded: boolean;
  onToggle: () => void;
}) {
  // Display the city + date; the chunk count badge surfaces that
  // multiple chunks contribute to this citation when N > 1. Per
  // James 2026-06-25 the chip no longer carries a single timestamp —
  // the expansion card lists every chunk with its own timecode.
  const first = citations[0];
  const chunkCount = citations.length;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={isExpanded}
      title={
        chunkCount === 1
          ? `${first.city_name} · ${first.meeting_date} — 1 chunk. Click to show source.`
          : `${first.city_name} · ${first.meeting_date} — ${chunkCount} chunks. Click to show source.`
      }
      className={`mx-0.5 inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 align-baseline text-[11px] transition-colors ${
        isExpanded
          ? "border-emerald-400/70 bg-emerald-400/20 text-emerald-100"
          : "border-emerald-400/40 bg-emerald-400/10 text-emerald-200 hover:bg-emerald-400/20"
      }`}
    >
      <span>{first.city_name}</span>
      <span className="text-emerald-300/50">·</span>
      <span>{first.meeting_date}</span>
      {chunkCount > 1 && (
        <>
          <span className="text-emerald-300/40">·</span>
          <span className="font-mono">{chunkCount}×</span>
        </>
      )}
      <ChevronDown
        className={`h-3 w-3 text-emerald-300/70 transition-transform ${
          isExpanded ? "rotate-180" : ""
        }`}
      />
      <span className="sr-only">
        {isExpanded ? "Collapse source" : "Expand source"}
      </span>
    </button>
  );
}

// Stopwords to drop from the keyword-highlight token set. Cheap civic-
// content list — not exhaustive but covers the noise the Sonnet
// extracted-keywords tend to carry ("the route changes" → only "route"
// + "changes" stay; "the" + "in" + etc. dropped).
const HIGHLIGHT_STOPWORDS = new Set([
  "the", "a", "an", "and", "or", "of", "in", "on", "for", "with",
  "across", "to", "by", "at", "as", "is", "was", "were", "be", "been",
  "any", "all", "this", "that", "these", "those", "it", "its",
]);

// Highlight keyword matches in a chunk body. Sonnet's interpretation
// keywords are noun phrases like "bus route changes" — literal whole-
// phrase matching almost never hits because the chunks use natural
// language ("the route", "yellow line", "transit budget"). We split
// the phrases into individual words, drop stopwords, and word-boundary-
// match each one. Result: every meaningful token from the keyword set
// gets highlighted wherever it appears in the chunk text.
function highlightKeywords(body: string, keywords: string[]): ReactElement {
  if (!keywords.length) return <>{body}</>;
  const words = new Set<string>();
  for (const k of keywords) {
    for (const word of k.split(/\s+/)) {
      const w = word.trim().toLowerCase().replace(/[^a-z0-9-]/g, "");
      if (w.length >= 3 && !HIGHLIGHT_STOPWORDS.has(w)) {
        words.add(w);
      }
    }
  }
  if (!words.size) return <>{body}</>;
  const escaped = Array.from(words).map((k) =>
    k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
  );
  const re = new RegExp(`\\b(${escaped.join("|")})\\b`, "gi");
  const parts = body.split(re);
  return (
    <>
      {parts.map((part, i) => {
        if (i % 2 === 1) {
          return (
            <mark
              key={i}
              className="rounded-sm bg-amber-400/20 px-0.5 text-amber-100"
            >
              {part}
            </mark>
          );
        }
        return <Fragment key={i}>{part}</Fragment>;
      })}
    </>
  );
}

// ── Citation expansion card — show-your-work surface ──────────────────
//
// Renders below the block containing an expanded chip. Lists EVERY
// chunk Sonnet retrieved from this meeting (all of them, per James
// 2026-06-25 — no top-N truncation) chronologically by start_seconds.
// Each chunk-card has its
// own header (timestamp + score + chunk index), full body with keyword
// highlights, and a stubbed "Play this moment" button (Z3/Z4 wires the
// inline video player).

function CitationCard({
  tag,
  citations,
  keywords,
  onClose,
  onOpenBroadcast,
}: {
  tag: string;
  citations: Citation[];
  keywords: string[];
  onClose: () => void;
  onOpenBroadcast?: (c: Citation) => void;
}) {
  // Per-chunk "Play this moment" state. Keyed on vector_id so multiple
  // chunks within the same expansion can be played independently (each
  // chunk's player mounts inline below its own sub-card). Per James
  // 2026-06-25: click-to-play (no autoplay) so audio doesn't blast
  // when the operator expands a citation.
  const [playingVectorId, setPlayingVectorId] = useState<string | null>(null);

  return (
    <div className="my-2 rounded-lg border border-emerald-400/30 bg-emerald-400/[0.04] p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-emerald-300/80">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400/70" />
          <span>{tag}</span>
          <span className="text-emerald-300/40">·</span>
          <span>
            {citations.length} chunk{citations.length === 1 ? "" : "s"}
          </span>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Collapse source"
          className="rounded-md p-1 text-emerald-300/60 transition-colors hover:bg-emerald-400/10 hover:text-emerald-100"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="space-y-2">
        {citations.map((c) => {
          const isPlaying = playingVectorId === c.vector_id;
          const hasVideo = !!c.video_url;
          return (
            <div
              key={c.vector_id}
              className="rounded-md border border-white/10 bg-black/30 p-3"
            >
              <div className="mb-2 flex items-center justify-between gap-3 text-[11px]">
                <div className="flex items-center gap-2 text-white/55">
                  <span className="font-mono text-emerald-300">
                    {formatSeconds(c.start_seconds)}
                  </span>
                  <span className="text-white/30">→</span>
                  <span className="font-mono text-white/50">
                    {formatSeconds(c.end_seconds)}
                  </span>
                  <span className="text-white/30">·</span>
                  <span>
                    cosine{" "}
                    <span className="font-mono text-white/70">{c.score}</span>
                  </span>
                  <span className="text-white/30">·</span>
                  <span className="text-white/40">chunk #{c.chunk_index}</span>
                </div>
                <button
                  type="button"
                  onClick={() =>
                    setPlayingVectorId(isPlaying ? null : c.vector_id)
                  }
                  disabled={!hasVideo}
                  title={
                    !hasVideo
                      ? "No video archive registered for this meeting"
                      : isPlaying
                        ? "Close player"
                        : "Play this moment"
                  }
                  className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors ${
                    !hasVideo
                      ? "cursor-not-allowed border-white/10 bg-white/[0.03] text-white/30"
                      : isPlaying
                        ? "border-emerald-400/50 bg-emerald-400/20 text-emerald-100 hover:bg-emerald-400/30"
                        : "border-emerald-400/30 bg-emerald-400/[0.06] text-emerald-200 hover:bg-emerald-400/15"
                  }`}
                >
                  {isPlaying ? (
                    <X className="h-3 w-3" />
                  ) : (
                    <Play className="h-3 w-3" />
                  )}
                  <span>
                    {isPlaying ? "Close player" : "Play this moment"}
                  </span>
                </button>
              </div>
              <div className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-white/85">
                {highlightKeywords(normalizeChunkBody(c.body), keywords)}
              </div>
              {isPlaying && (
                <InlineMeetingMomentPlayer
                  videoUrl={c.video_url}
                  seek={c.start_seconds}
                />
              )}
              {onOpenBroadcast && (
                <div className="mt-2 flex justify-end">
                  <button
                    type="button"
                    onClick={() => onOpenBroadcast(c)}
                    className="text-[11px] text-emerald-300/70 transition-colors hover:text-emerald-200"
                  >
                    Open broadcast page →
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function formatSeconds(s: number): string {
  if (!Number.isFinite(s)) return "—";
  const total = Math.floor(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }
  return `${m}:${String(sec).padStart(2, "0")}`;
}

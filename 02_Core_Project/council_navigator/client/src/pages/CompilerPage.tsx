/**
 * Conversational Compiler V0 (S-023 Track A) — Hex-Rays-style two-pane UX.
 *
 * V0 renders the meeting's existing tracked_claims (NotebookLM-extracted in
 * production via prompts/tracked_claims.md; the 3 m101091 sandbox rows are
 * hand-seeded via notebooklm_bridge/scripts/seed_tracked_claims_m101091.py)
 * as Commit_P nodes in typed-IR pseudo-code, syntax-highlighted by field type.
 * LEFT pane shows the source context of the focused node; RIGHT pane is the
 * scrollable IR. Click an IR block → context populates. No parser yet.
 *
 * When Track B (the parser pipeline) ships, this same surface will render
 * parser-generated Commit_P nodes — only the upstream changes, not the UX.
 *
 * See FUTURE_THOUGHTS § S-023 for the full architectural framing.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, FileCode2, List, Network } from "lucide-react";
import {
  fetchCompilerView,
  type CompilerResponse,
  type CompilerClaim,
  type CompilerNode,
} from "../utils/compiler";
import {
  IRBodyPseudoCode,
  IRBodyForNode,
  IRFunctionCallForClaim,
  IRFunctionCallForNode,
} from "../components/compiler/IRBlock";
import CompilerGraphPane from "../components/compiler/CompilerGraphPane";
import TranscriptPane, { type NodeRange } from "../components/compiler/TranscriptPane";
import NavigatorStrip, {
  type NavigatorFilters,
  DEFAULT_NAVIGATOR_FILTERS,
} from "../components/compiler/NavigatorStrip";
import { statusVisual, nodeVisual } from "../components/compiler/statusColors";
import { rememberCompilerMeeting } from "../components/TopBar";

export type CompilerMode = "list" | "graph";

interface CompilerPageProps {
  meetingId: number;
  /** Initial render mode. URL-driven (?mode=list|graph) via App.tsx. The
   * in-page toggle mutates this via history.replaceState so deep links
   * carry the mode the operator switched to. */
  initialMode?: CompilerMode;
  onBack: () => void;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

// Status visuals (word + pill + left-border) come from the shared
// statusColors module — keeps list-mode + graph-mode in sync (Opus
// critique 2026-06-04 item 7).

/** One Commit_P IR block — the pseudo-code rendering of a single claim,
 * wrapped in the V0 list-mode chrome (header strip + status pill +
 * border accent + clickable focus). V0.2-2: shows the Hex-Rays-style
 * function-call signature collapsed by default; click row to expand
 * (and focus); chevron in the signature line collapses without losing
 * focus. The full structured `IRBodyPseudoCode` card is the expanded
 * variant — no data lost, just hidden until requested. */
function IRBlock({
  claim,
  index,
  isFocused,
  expanded,
  onClick,
  onChevronClick,
}: {
  claim: CompilerClaim;
  index: number;
  isFocused: boolean;
  expanded: boolean;
  onClick: () => void;
  onChevronClick: (e: React.MouseEvent) => void;
}) {
  const speakerLine = claim.speaker_name
    ? `${claim.speaker_name}${claim.speaker_title ? `, ${claim.speaker_title}` : ""}`
    : "Speaker unresolved";
  const v = statusVisual(claim.status);

  return (
    <button
      type="button"
      onClick={onClick}
      // SPEC item 5 — the data-focus-key attribute lets CompilerPage
      // querySelector this block when focusKey changes via the
      // transcript-scroll path (the "scroll the right pane to match
      // the visible transcript region" half of bidirectional sync).
      data-focus-key={`claim:${claim.id}`}
      className={[
        "block w-full text-left transition-colors",
        "bg-[var(--surface)] border border-[var(--line)] border-l-4",
        v.borderLClass,
        "rounded-md overflow-hidden",
        "hover:border-zinc-600",
        isFocused ? "ring-1 ring-sky-400/60 border-zinc-600" : "",
      ].join(" ")}
    >
      {/* Header strip: node label + status pill */}
      <div className="flex items-center justify-between gap-3 px-4 py-2.5 border-b border-[var(--line)]/60 bg-black/20">
        <div className="font-mono text-[11px] tracking-wider text-zinc-400 uppercase">
          {/* Card-header chrome reads "Commitment" — the D-054 polish
              call. The IR identifier "Commit_P" stays inside the
              pseudo-code body (which IS the program's source identifier);
              the chrome is the human-oriented section eyebrow and
              dropped the underscore-suffix tell per Opus pre-commit
              critique. */}
          <span className="text-zinc-500">node </span>
          <span style={{ color: v.hex, opacity: 0.9 }}>Commitment</span>
          <span className="text-zinc-600"> · </span>
          <span className="text-zinc-400">#{index + 1}</span>
          <span className="text-zinc-600"> · </span>
          <span className="text-zinc-300 normal-case tracking-normal">{speakerLine}</span>
        </div>
        <span
          className={[
            "inline-flex items-center px-2 py-0.5 rounded-full border text-[10px] font-medium uppercase tracking-wider",
            v.pillClasses,
          ].join(" ")}
        >
          {v.word}
        </span>
      </div>

      {/* V0.2-2: function-call signature (always shown) + structured
          body (only when expanded). The signature carries the chevron
          toggle — clicking the chevron stops propagation so it can
          collapse without losing focus; clicking elsewhere on the row
          fires onClick (focus + ensure expanded). */}
      <IRFunctionCallForClaim
        claim={claim}
        expanded={expanded}
        onChevronClick={onChevronClick}
      />
      {expanded && <IRBodyPseudoCode claim={claim} />}
    </button>
  );
}

// Node-kind visuals (word + pill + left-border + hex) come from the
// shared statusColors module — see nodeVisual() there. Same module
// also owns nodeTypeHeaderColor() so the type label and the stripe
// agree (Opus critique 2026-06-05 items 5 + 6 — previously this page
// and CompilerGraphPane had parallel implementations that diverged).

/** Wrapper around an IRBodyFor* leaf that adds list-mode chrome (header
 * + status pill + clickable focus). Generic over `transcript_nodes` —
 * dispatches the body via IRBodyForNode (Motion / Vote / fallback).
 * V0.2-2: same collapsed/expanded model as IRBlock — function-call
 * signature shown always; structured body only when expanded. */
function NodeIRBlock({
  node,
  index,
  isFocused,
  expanded,
  onClick,
  onChevronClick,
}: {
  node: CompilerNode;
  /** 1-based index within the same node_type group (so the header reads
   * "Motion · #1", "Motion · #2", etc. — matches the list-mode IRBlock
   * ordinal convention rather than the row's transcript_nodes.ordinal). */
  index: number;
  isFocused: boolean;
  expanded: boolean;
  onClick: () => void;
  onChevronClick: (e: React.MouseEvent) => void;
}) {
  const v = nodeVisual(node.node_type, node.typed_fields);
  const speakerLine = node.speaker_name
    ? `${node.speaker_name}${node.speaker_title ? `, ${node.speaker_title}` : ""}`
    : node.node_type === "Vote"
      ? "Body action"
      : "Speaker unresolved";

  return (
    <button
      type="button"
      onClick={onClick}
      // SPEC item 5 — see IRBlock for the rationale.
      data-focus-key={`node:${node.id}`}
      className={[
        "block w-full text-left transition-colors",
        "bg-[var(--surface)] border border-[var(--line)] border-l-4",
        v.borderLClass,
        "rounded-md overflow-hidden",
        "hover:border-zinc-600",
        isFocused ? "ring-1 ring-sky-400/60 border-zinc-600" : "",
      ].join(" ")}
    >
      <div className="flex items-center justify-between gap-3 px-4 py-2.5 border-b border-[var(--line)]/60 bg-black/20">
        <div className="font-mono text-[11px] tracking-wider text-zinc-400 uppercase">
          <span className="text-zinc-500">node </span>
          {/* Type label uses the kind's stripe hex (Opus item 5) so the
              type word and the left-border accent agree. */}
          <span style={{ color: v.hex, opacity: 0.9 }}>{node.node_type}</span>
          <span className="text-zinc-600"> · </span>
          <span className="text-zinc-400">#{index + 1}</span>
          <span className="text-zinc-600"> · </span>
          <span className="text-zinc-300 normal-case tracking-normal">{speakerLine}</span>
        </div>
        <span
          className={[
            "inline-flex items-center px-2 py-0.5 rounded-full border text-[10px] font-medium uppercase tracking-wider",
            v.pillClasses,
          ].join(" ")}
        >
          {v.word}
        </span>
      </div>

      <IRFunctionCallForNode
        node={node}
        expanded={expanded}
        onChevronClick={onChevronClick}
      />
      {expanded && <IRBodyForNode node={node} />}
    </button>
  );
}

/** Two-button toggle between list mode (V0 typed-IR list) and graph mode
 * (CFG node-link rendering — Surface A). Lives in the page header so it
 * stays visible regardless of scroll. Icon-only with text labels for
 * scannability; the active mode gets a subtle inset highlight. */
function ModeToggle({
  mode,
  onChange,
}: {
  mode: CompilerMode;
  onChange: (next: CompilerMode) => void;
}) {
  const btn = (m: CompilerMode, label: string, Icon: typeof List) => {
    const active = mode === m;
    return (
      <button
        type="button"
        onClick={() => onChange(m)}
        aria-pressed={active}
        className={[
          "flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] font-medium tracking-wide uppercase transition-colors",
          active
            ? "bg-zinc-700/60 text-zinc-100 shadow-inner"
            : "text-zinc-400 hover:text-zinc-200",
        ].join(" ")}
      >
        <Icon className="w-3.5 h-3.5" />
        <span>{label}</span>
      </button>
    );
  };
  return (
    <div className="inline-flex items-center gap-0.5 p-0.5 rounded-md border border-[var(--line)] bg-black/30">
      {btn("list", "List", List)}
      {btn("graph", "Graph", Network)}
    </div>
  );
}

/** Discriminated union for the focused IR row. Track A (Commit_P claims
 * from tracked_claims) and Track B (transcript_nodes — Motion / Vote /
 * future types) have separate id spaces, so the focus state carries the
 * kind too. Stored as a string ("claim:N" / "node:N") in component state
 * so React equality checks stay primitive. */
type FocusKey = `claim:${number}` | `node:${number}` | null;

export default function CompilerPage({
  meetingId,
  initialMode = "list",
  onBack,
}: CompilerPageProps) {
  const [data, setData] = useState<CompilerResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [focusKey, setFocusKey] = useState<FocusKey>(null);
  const [mode, setModeState] = useState<CompilerMode>(initialMode);
  // V0.2-2 — list-mode expanded/collapsed state. Hex-Rays-style: each
  // row shows a one-line function-call signature collapsed by default;
  // operator clicks the row to expand (focus + ensure expanded), and
  // clicks the chevron in the signature line to collapse without
  // losing focus. Multiple rows can be expanded simultaneously so the
  // operator can compare two commitments / motions / votes side-by-
  // side; the chevron is the only path to collapse.
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set());
  // V0.2-3 — Navigator strip filter state. Defaults to "everything on"
  // so the operator sees the full meeting shape on first render; can
  // mute the conversational gray fill or any typed-event kind to
  // isolate signal. Persisted to component state only — not URL — so
  // toggles don't pollute deep links.
  const [navFilters, setNavFilters] = useState<NavigatorFilters>(
    DEFAULT_NAVIGATOR_FILTERS,
  );

  // Toggling the mode also mutates ?mode= via history.replaceState so the
  // URL stays a shareable deep link to the currently-displayed view —
  // matches the existing App.tsx pattern of treating the URL as the
  // canonical entry-point for owner deep links.
  const setMode = (next: CompilerMode) => {
    setModeState(next);
    if (typeof window !== "undefined" && window.history?.replaceState) {
      const url = new URL(window.location.href);
      url.searchParams.set("mode", next);
      window.history.replaceState(null, "", url.toString());
    }
  };

  // Cache the most recently opened compiler meeting so the universal TopBar's
  // Compiler link can deep-link back to it from anywhere in the app.
  useEffect(() => {
    rememberCompilerMeeting(meetingId);
  }, [meetingId]);

  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    setData(null);
    setFocusKey(null);
    fetchCompilerView(meetingId, { signal: ctrl.signal })
      .then(d => {
        if (!cancelled) {
          setData(d);
          // Default-focus the first available row — claim if any, else
          // first node — so the left pane shows something useful on load.
          let initial: FocusKey = null;
          if (d.claims.length > 0) initial = `claim:${d.claims[0].id}`;
          else if (d.nodes && d.nodes.length > 0)
            initial = `node:${d.nodes[0].id}`;
          setFocusKey(initial);
          // V0.2-2: auto-expand the initial focused row so the page
          // doesn't open in a "everything collapsed, nothing useful
          // visible" state. Subsequent navigation keeps multi-expansion
          // (the chevron is the only collapse path).
          setExpandedKeys(initial ? new Set([initial]) : new Set());
        }
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
  }, [meetingId]);

  // Resolve the focus key to the actual row + its per-group ordinal
  // label. Track A claims and Track B nodes have separate id spaces,
  // so the key carries the kind. Nodes are grouped by node_type and
  // ordinaled within each group so the right-pane "Motion #3" matches
  // the left-pane "Motion #3" label (D-054: don't surface DB ids to
  // the operator).
  const claims = data?.claims ?? [];
  const nodes = data?.nodes ?? [];
  const edges = data?.edges ?? [];
  const motions = nodes.filter(n => n.node_type === "Motion");
  const votes = nodes.filter(n => n.node_type === "Vote");
  const seconds = nodes.filter(n => n.node_type === "Second");
  const agendaTransitions = nodes.filter(n => n.node_type === "AgendaTransition");
  const otherNodes = nodes.filter(
    n =>
      n.node_type !== "Motion" &&
      n.node_type !== "Vote" &&
      n.node_type !== "Second" &&
      n.node_type !== "AgendaTransition",
  );

  let focusedRow:
    | { kind: "claim"; claim: CompilerClaim }
    | { kind: "node"; node: CompilerNode }
    | null = null;
  let focusedOrdinalLabel: string | null = null;
  if (focusKey?.startsWith("claim:")) {
    const id = Number(focusKey.slice("claim:".length));
    const c = claims.find(c => c.id === id);
    if (c) {
      focusedRow = { kind: "claim", claim: c };
      const ord = claims.findIndex(x => x.id === id) + 1;
      focusedOrdinalLabel = `Commitment #${ord}`;
    }
  } else if (focusKey?.startsWith("node:")) {
    const id = Number(focusKey.slice("node:".length));
    const n = nodes.find(n => n.id === id);
    if (n) {
      focusedRow = { kind: "node", node: n };
      const group =
        n.node_type === "Motion" ? motions
        : n.node_type === "Vote" ? votes
        : otherNodes.filter(o => o.node_type === n.node_type);
      const ord = group.findIndex(x => x.id === id) + 1;
      focusedOrdinalLabel = `${n.node_type} #${ord}`;
    }
  }

  const totalRows = claims.length + nodes.length;

  // Resolve the focused row to a millisecond span for transcript
  // highlighting. Per SPEC build seq item 4: the left pane consumes
  // /api/compiler/<id>/transcript and highlights the focused node's
  // word range. Source priority:
  //   1. Claim with word_timings → use the karaoke positions (the
  //      most precise: aligned by quote_align to the Whisper word
  //      array per quote/claim).
  //   2. Node with audio_offset_seconds + audio_duration_seconds →
  //      derive ms range (not currently populated by save_motions /
  //      save_votes / save_seconds_batch — NotebookLM doesn't expose
  //      absolute timestamps in its output; a future post-extraction
  //      timing pass would fill these in).
  //   3. Otherwise null — transcript renders un-highlighted.
  const focusedSpanMs = useMemo<{
    startMs: number;
    endMs: number;
  } | null>(() => {
    if (!focusedRow) return null;
    if (focusedRow.kind === "claim") {
      const wt = focusedRow.claim.word_timings;
      if (!wt || wt.length === 0) return null;
      const startMs = wt[0].start_ms;
      const endMs = wt[wt.length - 1].end_ms;
      if (typeof startMs !== "number" || typeof endMs !== "number") return null;
      return { startMs, endMs };
    }
    // Node case
    const n = focusedRow.node;
    const tfAny = n as unknown as {
      audio_offset_seconds?: number | null;
      audio_duration_seconds?: number | null;
    };
    const off = tfAny.audio_offset_seconds;
    const dur = tfAny.audio_duration_seconds;
    if (typeof off !== "number") return null;
    const startMs = off * 1000;
    const endMs = typeof dur === "number" ? (off + dur) * 1000 : startMs + 500;
    return { startMs, endMs };
  }, [focusedRow]);

  // SPEC item 6 — token coloring. Build the NodeRange list combining
  // both claims (Commit_P with word_timings on the projection) and
  // nodes (audio_offset_seconds + audio_duration_seconds backfilled
  // by parsers/node_timing.py). TranscriptPane consumes these to
  // color each word by its containing node's kind.
  const nodeRanges = useMemo<NodeRange[]>(() => {
    const ranges: NodeRange[] = [];
    // Claim ranges — amber. Per claim_type the visual identity
    // collapses to the same "Commitment" amber per statusVisual.
    for (const claim of claims) {
      const wt = claim.word_timings;
      if (!wt || wt.length === 0) continue;
      const startMs = wt[0].start_ms;
      const endMs = wt[wt.length - 1].end_ms;
      if (typeof startMs !== "number" || typeof endMs !== "number") continue;
      ranges.push({
        startMs,
        endMs,
        focusKey: `claim:${claim.id}`,
        kind: "Commit_P",
        // Amber for commitments — but lighter than the focus-highlight
        // bg so the two compose without visual conflict.
        textColorClass: "text-amber-200/90 font-medium",
        // V0.2-1 speaker attribution — feeds the transcript pane's
        // inline "Watkins (Mayor):" labels at turn changes.
        speakerName: claim.speaker_name,
        speakerTitle: claim.speaker_title,
      });
    }
    // Node ranges — per kind text color
    for (const n of nodes) {
      const off = n.audio_offset_seconds;
      const dur = n.audio_duration_seconds;
      if (typeof off !== "number" || typeof dur !== "number") continue;
      const startMs = off * 1000;
      const endMs = (off + dur) * 1000;
      let textColorClass = "text-zinc-300";
      let agendaLabel: string | null = null;
      if (n.node_type === "Motion") {
        const mt = typeof n.typed_fields.motion_type === "string"
          ? n.typed_fields.motion_type.toLowerCase() : "";
        textColorClass = mt === "substantive"
          ? "text-sky-300 font-medium"
          : "text-zinc-300 font-medium";
      } else if (n.node_type === "Vote") {
        const r = typeof n.typed_fields.vote_result === "string"
          ? n.typed_fields.vote_result.toLowerCase() : "";
        textColorClass =
          r === "passed" ? "text-emerald-300 font-medium"
          : r === "failed" ? "text-rose-300 font-medium"
          : r === "tabled" ? "text-amber-300 font-medium"
          : r === "withdrawn" ? "text-zinc-400 font-medium"
          : r === "tied" ? "text-violet-300 font-medium"
          : "text-zinc-300 font-medium";
      } else if (n.node_type === "Second") {
        textColorClass = "text-cyan-300 font-medium";
      } else if (n.node_type === "AgendaTransition") {
        // AgendaTransitions render as eyebrows, not full-range coloring
        // — the eyebrow label is the item number + title.
        const num = typeof n.typed_fields.agenda_item_number === "string"
          ? n.typed_fields.agenda_item_number : null;
        const title = typeof n.typed_fields.agenda_item_title === "string"
          ? n.typed_fields.agenda_item_title : "";
        const sentence = typeof n.typed_fields.summary_sentence === "string"
          ? n.typed_fields.summary_sentence : "";
        agendaLabel = num
          ? `Item ${num} · ${sentence || title}`
          : sentence || title || "Agenda transition";
        textColorClass = "text-slate-400";
      }
      ranges.push({
        startMs,
        endMs,
        focusKey: `node:${n.id}`,
        kind: n.node_type as NodeRange["kind"],
        textColorClass,
        agendaEyebrowLabel: agendaLabel,
        // V0.2-1 speaker attribution. AgendaTransitions intentionally
        // get no speaker — they're procedural markers, not utterances.
        // Votes pass through whatever the node carries (typically null
        // for body-action votes; populated for hand-roll votes).
        speakerName: n.node_type === "AgendaTransition" ? null : n.speaker_name,
        speakerTitle: n.node_type === "AgendaTransition" ? null : n.speaker_title,
      });
    }
    return ranges;
  }, [claims, nodes]);

  // Focus header for the transcript pane — slim metadata strip above
  // the prose so the operator knows what the highlight corresponds to.
  // Mirrors the V0 SourceContextPane's header (speaker + sub-kind) but
  // sits inside the transcript scroll container.
  const focusHeader = useMemo<React.ReactNode>(() => {
    if (!focusedRow) return null;
    let speakerLine: string;
    let subKind: string;
    if (focusedRow.kind === "claim") {
      const claim = focusedRow.claim;
      speakerLine = claim.speaker_name
        ? `${claim.speaker_name}${claim.speaker_title ? `, ${claim.speaker_title}` : ""}`
        : "Speaker unresolved";
      subKind = claim.claim_type
        ? claim.claim_type.charAt(0).toUpperCase() + claim.claim_type.slice(1).toLowerCase()
        : "Commitment";
    } else {
      const n = focusedRow.node;
      const tf = n.typed_fields;
      speakerLine = n.speaker_name
        ? `${n.speaker_name}${n.speaker_title ? `, ${n.speaker_title}` : ""}`
        : n.node_type === "Vote"
          ? "Body action"
          : "Speaker unresolved";
      if (n.node_type === "Motion" && typeof tf.motion_type === "string") {
        subKind = `${tf.motion_type.charAt(0).toUpperCase()}${tf.motion_type.slice(1)} motion`;
      } else if (n.node_type === "Vote") {
        const r = (typeof tf.vote_result === "string" ? tf.vote_result : "").toLowerCase();
        subKind =
          r === "passed" ? "Vote passed"
          : r === "failed" ? "Vote failed"
          : r === "tabled" ? "Vote tabled"
          : r === "withdrawn" ? "Vote withdrawn"
          : r === "tied" ? "Tied vote"
          : "Vote";
      } else {
        subKind = n.node_type;
      }
    }
    const noHighlight = focusedSpanMs === null;
    return (
      <>
        <div className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider mb-1.5">
          Focused · {focusedOrdinalLabel ?? "—"}
          {noHighlight && (
            <span className="text-zinc-600 normal-case tracking-normal ml-2">
              (no audio timing yet)
            </span>
          )}
        </div>
        <div className="text-sm text-zinc-300">
          <span className="text-zinc-100 font-medium">{speakerLine}</span>
          <span className="text-zinc-600 mx-2">·</span>
          <span className="text-zinc-400">{subKind}</span>
        </div>
      </>
    );
  }, [focusedRow, focusedOrdinalLabel, focusedSpanMs]);

  // SPEC item 5 — transcript scroll → IR node focus. When the operator
  // scrolls the transcript, TranscriptPane fires onVisibleTimeChange
  // with the timestamp at the viewport's reading line; we map that
  // timestamp back to whichever claim is closest in time + update
  // focus. We use CLOSEST-BY-MIDPOINT rather than strict containment
  // because claim word_timings are narrow windows (7-17s typical) in
  // a multi-hour transcript — strict containment almost never matches.
  // Closest-by-midpoint gives the "currently-active CFG block" feel
  // from SPEC Decision #1: as the operator scrolls past the midpoint
  // between two claims, focus switches to the next one. Future:
  // include transcript_nodes once they carry audio_offset.
  const handleVisibleTimeChange = useCallback(
    (timeMs: number) => {
      if (claims.length === 0) return;
      let bestClaim: CompilerClaim | null = null;
      let bestDistance = Infinity;
      for (const claim of claims) {
        const wt = claim.word_timings;
        if (!wt || wt.length === 0) continue;
        const startMs = wt[0].start_ms;
        const endMs = wt[wt.length - 1].end_ms;
        if (typeof startMs !== "number" || typeof endMs !== "number") continue;
        const midMs = (startMs + endMs) / 2;
        const dist = Math.abs(midMs - timeMs);
        if (dist < bestDistance) {
          bestDistance = dist;
          bestClaim = claim;
        }
      }
      if (!bestClaim) return;
      const next: FocusKey = `claim:${bestClaim.id}`;
      if (next !== focusKey) setFocusKey(next);
    },
    [claims, focusKey],
  );

  // SPEC item 5 — IR node focus → right-pane scroll-into-view. When
  // focusKey changes (from any path: click, transcript-scroll), scroll
  // the matching IRBlock into view in the right pane. block:'nearest'
  // gives a gentle scroll only when the block is offscreen — no jitter
  // if it's already visible.
  useEffect(() => {
    if (!focusKey) return;
    if (typeof document === "undefined") return;
    const el = document.querySelector<HTMLElement>(
      `[data-focus-key="${focusKey}"]`,
    );
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [focusKey]);

  // V0.2-2 — IR row click handler. Sets focus AND ensures the row is
  // expanded. Clicking an already-expanded row is a no-op on the
  // expansion side (focus only), so the operator can re-focus a row
  // they're reading without it collapsing. The only path to collapse
  // is the chevron's onClick (handleChevronClick below).
  const handleRowClick = useCallback((key: string) => {
    setFocusKey(key as FocusKey);
    setExpandedKeys(prev => {
      if (prev.has(key)) return prev;
      const next = new Set(prev);
      next.add(key);
      return next;
    });
  }, []);

  // V0.2-2 — chevron click handler. Toggles expansion ONLY (focus
  // stays unchanged). stopPropagation prevents the outer button's
  // onClick from re-expanding the row the chevron just collapsed.
  const handleChevronClick = useCallback(
    (key: string, e: React.MouseEvent) => {
      e.stopPropagation();
      setExpandedKeys(prev => {
        const next = new Set(prev);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
    },
    [],
  );

  // V0.2-3 — Navigator strip total duration. Computed as max endMs
  // across every timed IR range so the strip spans the full extent
  // of typed events. The real audio duration (notebook_outputs.
  // audio_duration_seconds) may be slightly longer than the last
  // typed event — the trailing gray on the strip slightly under-
  // counts vs the actual audio for now. Acceptable for V0.2-3
  // first cut; a follow-up can plumb the real duration through
  // CompilerResponse.meeting if precise tail accuracy matters.
  const navigatorDurationMs = useMemo(() => {
    let maxMs = 0;
    for (const r of nodeRanges) {
      if (r.endMs > maxMs) maxMs = r.endMs;
    }
    // Small tail buffer so the right edge isn't a hard click target.
    return maxMs > 0 ? maxMs * 1.02 : 1;
  }, [nodeRanges]);

  // V0.2-3 — Navigator click → seek. Map clicked timestamp to the
  // closest IR row (claim or node) and update focus. Uses midpoint
  // distance like handleVisibleTimeChange but considers BOTH claims
  // and nodes so the Navigator can land on any kind. The focusKey
  // change triggers the existing scrollIntoView useEffect, so both
  // the IR pane and transcript pane sync automatically.
  const handleNavigatorSeek = useCallback(
    (timeMs: number) => {
      type Candidate = { focusKey: string; midMs: number };
      const candidates: Candidate[] = [];
      for (const claim of claims) {
        const wt = claim.word_timings;
        if (!wt || wt.length === 0) continue;
        const startMs = wt[0].start_ms;
        const endMs = wt[wt.length - 1].end_ms;
        if (typeof startMs !== "number" || typeof endMs !== "number") continue;
        candidates.push({
          focusKey: `claim:${claim.id}`,
          midMs: (startMs + endMs) / 2,
        });
      }
      for (const n of nodes) {
        const off = n.audio_offset_seconds;
        const dur = n.audio_duration_seconds;
        if (typeof off !== "number") continue;
        const startMs = off * 1000;
        const endMs = typeof dur === "number" ? (off + dur) * 1000 : startMs + 500;
        candidates.push({
          focusKey: `node:${n.id}`,
          midMs: (startMs + endMs) / 2,
        });
      }
      if (candidates.length === 0) return;
      let best: Candidate | null = null;
      let bestDist = Infinity;
      for (const c of candidates) {
        const dist = Math.abs(c.midMs - timeMs);
        if (dist < bestDist) {
          bestDist = dist;
          best = c;
        }
      }
      if (!best) return;
      const nextKey = best.focusKey as FocusKey;
      if (nextKey !== focusKey) setFocusKey(nextKey);
      // Auto-expand the seeked row so the operator immediately sees
      // its details — matches the row-click semantic from V0.2-2.
      if (nextKey) {
        const key = nextKey as string;
        setExpandedKeys(prev => {
          if (prev.has(key)) return prev;
          const next = new Set(prev);
          next.add(key);
          return next;
        });
      }
    },
    [claims, nodes, focusKey],
  );

  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col">
      {/* IDA Pro chrome — evoke-only decoration per SPEC build seq item 7.
          Menu items are visual identity, not functional. The Future-option
          note in the SPEC reserves the working-menu version for when the
          surface earns the engineering investment. The hex-stripe at the
          top mirrors the CFG node headers — gives the page the same
          decompiler-IDE feel as the right-pane chrome. */}
      <div className="bg-[#050810] border-b border-[#1e3a5f]">
        <div className="h-[3px] bg-gradient-to-r from-amber-500/40 via-sky-400/40 to-emerald-400/40" />
        <div className="max-w-[1600px] mx-auto px-6 lg:px-10 py-1.5 flex items-center gap-5 text-[11px] font-mono text-zinc-400">
          {(["File", "Edit", "View", "Graph", "Debugger", "Options", "Windows", "Help"] as const).map(item => (
            <span
              key={item}
              className="hover:text-zinc-200 cursor-default select-none transition-colors"
              title="Decorative — full menu graduates if the surface becomes a power-user tool"
            >
              {item}
            </span>
          ))}
          <span className="flex-1" />
          <span className="text-zinc-600">
            ida-view-a · arrow-keys to scroll
          </span>
        </div>
      </div>

      {/* Header — eyebrow + meeting line + back */}
      <header className="sticky top-0 z-40 bg-[var(--canvas)]/95 backdrop-blur border-b border-[var(--line)]">
        <div className="max-w-[1600px] mx-auto px-6 lg:px-10 py-4 flex items-center justify-between gap-4">
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
                <FileCode2 className="w-4 h-4" />
              </div>
              <div className="min-w-0">
                <div className="text-[10px] font-mono uppercase tracking-wider text-zinc-500 mb-0.5">
                  Conversational Compiler · V0
                </div>
                <h1 className="text-base font-semibold tracking-wide text-white truncate">
                  {data?.meeting.meeting_title ?? "Loading…"}
                </h1>
              </div>
            </div>
          </div>
          {data && (
            <div className="flex items-center gap-4">
              <div className="hidden sm:flex items-center gap-2.5 text-xs text-zinc-400">
                <span className="font-mono">{data.meeting.city_name}</span>
                <span className="text-zinc-700">·</span>
                <span className="font-mono">{formatDate(data.meeting.meeting_date)}</span>
                <span className="text-zinc-700">·</span>
                <span>
                  {totalRows === 1 ? "1 IR node" : `${totalRows} IR nodes`}
                </span>
              </div>
              <ModeToggle mode={mode} onChange={setMode} />
            </div>
          )}
        </div>
      </header>

      {/* Two-pane body */}
      <main className="flex-1 max-w-[1600px] w-full mx-auto px-6 lg:px-10 py-6">
        {loading && (
          <div className="h-[60vh] flex items-center justify-center text-sm text-zinc-500">
            Loading compiler view…
          </div>
        )}

        {!loading && error && (
          <div className="max-w-md mx-auto mt-20 p-6 rounded-md border border-rose-500/30 bg-rose-500/5 text-sm">
            <div className="font-medium text-rose-300 mb-2">Couldn't load this meeting.</div>
            <div className="text-rose-200/80 text-xs font-mono">{error}</div>
            <div className="mt-3 text-zinc-400 text-xs">
              The compiler V0 only knows meetings whose tracked_claims have
              been curated. Try meeting 101091 (Kingman, the sandbox).
            </div>
          </div>
        )}

        {!loading && !error && data && totalRows === 0 && (
          <div className="max-w-md mx-auto mt-20 p-6 rounded-md border border-zinc-700 bg-zinc-900/40 text-sm">
            <div className="font-medium text-zinc-300 mb-2">
              No IR nodes for this meeting yet.
            </div>
            <div className="text-zinc-500 text-xs leading-relaxed">
              The compiler renders both hand-seeded tracked claims (Track A) and
              parser-pipeline transcript_nodes (Track B). When neither has
              produced anything for this meeting, this surface stays empty.
            </div>
          </div>
        )}

        {/* V0.2-3 — Navigator strip. Rendered in BOTH list + graph modes
            above the main content; bird's-eye view of the meeting's
            shape spanning the full audio duration. Hidden when no
            timed IR data exists (the strip would be empty). */}
        {!loading && !error && data && totalRows > 0 && nodeRanges.length > 0 && (
          <div className="mb-4">
            <NavigatorStrip
              durationMs={navigatorDurationMs}
              claims={claims}
              nodes={nodes}
              focusKey={focusKey}
              filters={navFilters}
              onFilterChange={setNavFilters}
              onSeek={handleNavigatorSeek}
            />
          </div>
        )}

        {!loading && !error && data && totalRows > 0 && mode === "list" && (
          <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,4fr)_minmax(0,8fr)] gap-6 h-[calc(100vh-13rem)]">
            {/* LEFT — full meeting transcript (SPEC build seq item 4).
                The focused row's metadata sits in a sticky header above
                the prose; the focused span (when timing is available)
                gets amber-highlighted + auto-scrolled into view. */}
            <section className="rounded-md border border-[var(--line)] bg-[var(--surface)]/40 overflow-hidden">
              <TranscriptPane
                meetingId={meetingId}
                highlightSpanMs={focusedSpanMs}
                focusHeader={focusHeader}
                onVisibleTimeChange={handleVisibleTimeChange}
                nodeRanges={nodeRanges}
              />
            </section>

            {/* RIGHT — IR pseudo-code list, grouped by node kind.
                The top eyebrow is the single source of inventory truth
                ("Typed IR · 14 nodes (3 Commitments · 5 Motions · 6
                Votes)"); per-group section headers carry the kind word
                only — no repeated count, no DB-shaped underscore
                identifier. */}
            <section className="overflow-y-auto pr-1 space-y-6">
              <div className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider mb-2 sticky top-0 bg-background/95 backdrop-blur py-1 z-10">
                Typed IR · {totalRows} nodes
                {(() => {
                  // Inventory breakdown — Commitments / Motions / Votes /
                  // Seconds / Agenda items, only listing the kinds present.
                  const parts: string[] = [];
                  if (claims.length > 0)
                    parts.push(`${claims.length} ${claims.length === 1 ? "Commitment" : "Commitments"}`);
                  if (motions.length > 0)
                    parts.push(`${motions.length} ${motions.length === 1 ? "Motion" : "Motions"}`);
                  if (votes.length > 0)
                    parts.push(`${votes.length} ${votes.length === 1 ? "Vote" : "Votes"}`);
                  if (seconds.length > 0)
                    parts.push(`${seconds.length} ${seconds.length === 1 ? "Second" : "Seconds"}`);
                  if (agendaTransitions.length > 0)
                    parts.push(`${agendaTransitions.length} agenda ${agendaTransitions.length === 1 ? "item" : "items"}`);
                  return parts.length > 1 ? (
                    <span className="text-zinc-600 normal-case tracking-normal ml-3">
                      ({parts.join(" · ")})
                    </span>
                  ) : null;
                })()}
              </div>

              {/* Commit_P group — claims from tracked_claims. Section
                  header reads "Commitments" (human) rather than
                  "Commit_P · 3 nodes" (schema-as-label + redundant
                  count). */}
              {claims.length > 0 && (
                <div className="space-y-3">
                  {(motions.length > 0 || votes.length > 0) && (
                    <div className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider">
                      {claims.length === 1 ? "Commitment" : "Commitments"}
                    </div>
                  )}
                  {claims.map((claim, i) => {
                    const key = `claim:${claim.id}`;
                    return (
                      <IRBlock
                        key={key}
                        claim={claim}
                        index={i}
                        isFocused={focusKey === key}
                        expanded={expandedKeys.has(key)}
                        onClick={() => handleRowClick(key)}
                        onChevronClick={e => handleChevronClick(key, e)}
                      />
                    );
                  })}
                </div>
              )}

              {/* Motion group */}
              {motions.length > 0 && (
                <div className="space-y-3">
                  <div className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider">
                    {motions.length === 1 ? "Motion" : "Motions"}
                  </div>
                  {motions.map((n, i) => {
                    const key = `node:${n.id}`;
                    return (
                      <NodeIRBlock
                        key={key}
                        node={n}
                        index={i}
                        isFocused={focusKey === key}
                        expanded={expandedKeys.has(key)}
                        onClick={() => handleRowClick(key)}
                        onChevronClick={e => handleChevronClick(key, e)}
                      />
                    );
                  })}
                </div>
              )}

              {/* Vote group */}
              {votes.length > 0 && (
                <div className="space-y-3">
                  <div className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider">
                    {votes.length === 1 ? "Vote" : "Votes"}
                  </div>
                  {votes.map((n, i) => {
                    const key = `node:${n.id}`;
                    return (
                      <NodeIRBlock
                        key={key}
                        node={n}
                        index={i}
                        isFocused={focusKey === key}
                        expanded={expandedKeys.has(key)}
                        onClick={() => handleRowClick(key)}
                        onChevronClick={e => handleChevronClick(key, e)}
                      />
                    );
                  })}
                </div>
              )}

              {/* Second group — completes the Motion → Second → Vote
                  procedural triad. Body renderer ships with B-4. */}
              {seconds.length > 0 && (
                <div className="space-y-3">
                  <div className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider">
                    {seconds.length === 1 ? "Second" : "Seconds"}
                  </div>
                  {seconds.map((n, i) => {
                    const key = `node:${n.id}`;
                    return (
                      <NodeIRBlock
                        key={key}
                        node={n}
                        index={i}
                        isFocused={focusKey === key}
                        expanded={expandedKeys.has(key)}
                        onClick={() => handleRowClick(key)}
                        onChevronClick={e => handleChevronClick(key, e)}
                      />
                    );
                  })}
                </div>
              )}

              {/* AgendaTransition group — the structural backbone the
                  Motion/Vote/Commit_P nodes hang under (via
                  parent_node_id, SPEC Decision #2). */}
              {agendaTransitions.length > 0 && (
                <div className="space-y-3">
                  <div className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider">
                    Agenda items
                  </div>
                  {agendaTransitions.map((n, i) => {
                    const key = `node:${n.id}`;
                    return (
                      <NodeIRBlock
                        key={key}
                        node={n}
                        index={i}
                        isFocused={focusKey === key}
                        expanded={expandedKeys.has(key)}
                        onClick={() => handleRowClick(key)}
                        onChevronClick={e => handleChevronClick(key, e)}
                      />
                    );
                  })}
                </div>
              )}

              {/* Other node types — fallback group for anything we
                  haven't given a dedicated section yet (Utterance,
                  Contradiction). Stays as a catch-all so the operator
                  always sees every IR node. */}
              {otherNodes.length > 0 && (
                <div className="space-y-3">
                  <div className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider">
                    Other procedural events
                  </div>
                  {otherNodes.map((n, i) => {
                    const key = `node:${n.id}`;
                    return (
                      <NodeIRBlock
                        key={key}
                        node={n}
                        index={i}
                        isFocused={focusKey === key}
                        expanded={expandedKeys.has(key)}
                        onClick={() => handleRowClick(key)}
                        onChevronClick={e => handleChevronClick(key, e)}
                      />
                    );
                  })}
                </div>
              )}
            </section>
          </div>
        )}

        {!loading && !error && data && totalRows > 0 && mode === "graph" && (
          <CompilerGraphPane
            claims={claims}
            nodes={nodes}
            edges={edges}
            meetingId={meetingId}
            focusKey={focusKey}
            onFocus={setFocusKey}
          />
        )}
      </main>
    </div>
  );
}

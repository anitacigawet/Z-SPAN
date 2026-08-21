/**
 * Conversational Compiler — single source of truth for claim-status colors.
 *
 * Shared between list-mode (status pill + left-border accent on IRBlock)
 * and graph-mode (top hex-stripe + status pill + focus ring on CFG nodes).
 *
 * Per Opus critique item 7 (2026-06-04): the two modes previously drifted
 * on shade — list used `violet-500` while graph used `violet-400` for the
 * same `unclear` status. Centralizing here prevents drift; both modes
 * read this map.
 *
 * D-054: `word` is the plain-English label the user sees (Open / Kept /
 * Broken / Withdrawn / Unclear), NEVER the raw enum.
 */

export interface StatusVisual {
  /** Plain-English word for the status pill — D-054. */
  word: string;
  /** SVG-side raw hex for graph mode (hex-stripe, pill fill at alpha,
   * focus ring). */
  hex: string;
  /** Tailwind classes for the list-mode pill (border + bg + text). */
  pillClasses: string;
  /** Tailwind class for the list-mode left-border accent on IRBlock. */
  borderLClass: string;
}

const DEFAULT: StatusVisual = {
  word: "Open",
  hex: "#f59e0b", // amber-500
  pillClasses: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  borderLClass: "border-l-amber-500/60",
};

const STATUS_VISUALS: Record<string, StatusVisual> = {
  active: DEFAULT,
  fulfilled: {
    word: "Kept",
    hex: "#10b981", // emerald-500
    pillClasses: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
    borderLClass: "border-l-emerald-500/60",
  },
  broken: {
    word: "Broken",
    hex: "#f43f5e", // rose-500
    pillClasses: "bg-rose-500/15 text-rose-300 border-rose-500/30",
    borderLClass: "border-l-rose-500/60",
  },
  withdrawn: {
    word: "Withdrawn",
    hex: "#71717a", // zinc-500
    pillClasses: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
    borderLClass: "border-l-zinc-500/60",
  },
  unclear: {
    word: "Unclear",
    hex: "#8b5cf6", // violet-500 (was violet-400 in graph; aligned to list)
    pillClasses: "bg-violet-500/15 text-violet-300 border-violet-500/30",
    borderLClass: "border-l-violet-500/60",
  },
};

export function statusVisual(status: string | null | undefined): StatusVisual {
  if (!status) return DEFAULT;
  return STATUS_VISUALS[status.toLowerCase()] ?? DEFAULT;
}

// ── Track B / transcript_nodes node-kind visuals ──────────────────────
//
// Single source of truth for Motion + Vote (and future node-type) chrome
// colors — extends statusVisual()'s pattern to discriminated nodes. Per
// Opus critique 2026-06-05 item 6: previously the same mapping was
// duplicated between CompilerPage.tsx (`nodeStatusVisual`) and
// CompilerGraphPane.tsx (`itemVisualHex`), with different return shapes
// (Tailwind classes vs raw hex). Drift risk the moment a new Motion
// sub-type or Vote result lands.

const MOTION_SUBSTANTIVE: StatusVisual = {
  word: "Substantive",
  hex: "#0ea5e9", // sky-500
  pillClasses: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  borderLClass: "border-l-sky-500/60",
};

const MOTION_PROCEDURAL: StatusVisual = {
  word: "Procedural",
  hex: "#71717a", // zinc-500
  pillClasses: "bg-zinc-500/15 text-zinc-300 border-zinc-500/30",
  borderLClass: "border-l-zinc-500/60",
};

const VOTE_PASSED: StatusVisual = {
  word: "Passed",
  hex: "#10b981", // emerald-500
  pillClasses: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  borderLClass: "border-l-emerald-500/60",
};
const VOTE_FAILED: StatusVisual = {
  word: "Failed",
  hex: "#f43f5e", // rose-500
  pillClasses: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  borderLClass: "border-l-rose-500/60",
};
const VOTE_TABLED: StatusVisual = {
  word: "Tabled",
  hex: "#f59e0b", // amber-500
  pillClasses: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  borderLClass: "border-l-amber-500/60",
};
const VOTE_WITHDRAWN: StatusVisual = {
  word: "Withdrawn",
  hex: "#71717a", // zinc-500
  pillClasses: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
  borderLClass: "border-l-zinc-500/60",
};
const VOTE_TIED: StatusVisual = {
  word: "Tied",
  hex: "#8b5cf6", // violet-500
  pillClasses: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  borderLClass: "border-l-violet-500/60",
};

const COMMIT_P_HEADER_COLOR: StatusVisual = {
  word: "Commit_P",
  hex: "#f59e0b", // amber (matches tracked_claims default)
  pillClasses: "",
  borderLClass: "",
};

/** Visual identity for a `transcript_nodes` row — picks the per-kind
 * pill word, hex, Tailwind classes per node_type + typed_fields enum
 * (motion_type for Motion, vote_result for Vote). Other node types
 * fall back to a neutral zinc presentation. */
export function nodeVisual(
  nodeType: string,
  typedFields: Record<string, unknown>,
): StatusVisual {
  if (nodeType === "Motion") {
    const mt = (typeof typedFields.motion_type === "string"
      ? typedFields.motion_type
      : "").toLowerCase();
    if (mt === "substantive") return MOTION_SUBSTANTIVE;
    if (mt === "procedural") return MOTION_PROCEDURAL;
    // Unknown motion_type — keep the procedural palette but surface the
    // raw value so the operator sees what the extractor produced.
    return { ...MOTION_PROCEDURAL, word: mt || "Motion" };
  }
  if (nodeType === "Vote") {
    const r = (typeof typedFields.vote_result === "string"
      ? typedFields.vote_result
      : "").toLowerCase();
    if (r === "passed") return VOTE_PASSED;
    if (r === "failed") return VOTE_FAILED;
    if (r === "tabled") return VOTE_TABLED;
    if (r === "withdrawn") return VOTE_WITHDRAWN;
    if (r === "tied") return VOTE_TIED;
    return { ...MOTION_PROCEDURAL, word: r || "Vote" };
  }
  if (nodeType === "Second") {
    // Cyan — quiet procedural affirmation; the second is the response
    // to a motion, not the motion itself, so we want a distinct hue
    // from Motion's sky but still in the cool/cyan family.
    return {
      word: "Second",
      hex: "#22d3ee", // cyan-400
      pillClasses: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
      borderLClass: "border-l-cyan-500/60",
    };
  }
  if (nodeType === "AgendaTransition") {
    // Slate — quieter still; AgendaTransitions are structural backbone
    // (the chair's navigation), not deliberation. They frame the
    // procedural action without competing visually with the substantive
    // Motion / Vote / Commit_P nodes that hang under them.
    return {
      word: "Agenda item",
      hex: "#94a3b8", // slate-400
      pillClasses: "bg-slate-500/15 text-slate-300 border-slate-500/30",
      borderLClass: "border-l-slate-500/60",
    };
  }
  // Generic fallback for Utterance / Contradiction / etc.
  return { ...MOTION_PROCEDURAL, word: nodeType };
}

/** The node-type label's text color in headers (e.g. "node Motion · #1"
 * — the "Motion" word). Per Opus critique 2026-06-05 item 5: previously
 * always sky-300, regardless of kind, so the type discrimination only
 * lived in the border-stripe + status pill. Now the type word matches
 * its visual identity. */
export function nodeTypeHeaderColor(
  nodeType: string,
  typedFields: Record<string, unknown>,
): string {
  if (nodeType === "Commit_P") return COMMIT_P_HEADER_COLOR.hex;
  return nodeVisual(nodeType, typedFields).hex;
}

// ── Track B / transcript_edges edge visuals ───────────────────────────
//
// CFG edges paint per edge_type to match the IDA-Pro semantic of "what
// kind of control transfer is this". responds_to (procedural) reads as
// a neutral sky link — Vote echoes its Motion. satisfies (Heap-allocation
// freed) reads emerald — the commitment was operationalized. Other
// types fall back to zinc until their inference passes ship.

export interface EdgeVisual {
  /** SVG stroke color. */
  hex: string;
  /** Plain-English label for the edge legend. */
  word: string;
  /** Stroke style — solid for confident structural edges, dashed for
   * inferred semantic edges. */
  dash: string | undefined;
}

const EDGE_VISUALS: Record<string, EdgeVisual> = {
  responds_to: {
    hex: "#7dd3fc", // sky-300 — procedural response, the same family as motion chrome
    word: "responds to",
    dash: undefined,
  },
  satisfies: {
    hex: "#34d399", // emerald-400 — the "Heap-allocation freed" semantic
    word: "satisfies",
    dash: undefined,
  },
  references: {
    hex: "#a1a1aa", // zinc-400 — quiet cross-reference
    word: "references",
    dash: "4 2",
  },
  entails: {
    hex: "#8b5cf6", // violet-500 — logical implication
    word: "entails",
    dash: "4 2",
  },
  contradicts: {
    hex: "#f43f5e", // rose-500 — contradiction
    word: "contradicts",
    dash: "4 2",
  },
};

const EDGE_VISUAL_DEFAULT: EdgeVisual = {
  hex: "#71717a",
  word: "links",
  dash: "4 2",
};

export function edgeVisual(edgeType: string): EdgeVisual {
  return EDGE_VISUALS[edgeType] ?? EDGE_VISUAL_DEFAULT;
}

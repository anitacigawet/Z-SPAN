/**
 * DefinitionHint — a minimal circled-"i" info icon that reveals a short
 * definition + source-attribution link on hover or keyboard focus.
 *
 * Educational, unobtrusive — Z-SPAN doesn't presume the reader already
 * knows civic vocabulary or AI-infrastructure vocabulary. The pattern
 * originated on ChannelsPage (James 2026-06-14) linking terms to
 * Merriam-Webster; extracted to a shared component
 * so the Librarian header can point "RAG" at the NVIDIA blog under the
 * same visual pattern.
 *
 * Props:
 * term — the word being defined (rendered as an uppercase eyebrow)
 * definition — a short paragraph, plain sentence-case
 * sourceUrl — where to send the reader for the full-length source
 * sourceLabel — display text for the citation link; defaults to
 * "Merriam-Webster ↗" to preserve the pre-extraction
 * behavior when consumers don't specify one
 */
import { Info } from "lucide-react";

interface DefinitionHintProps {
 term: string;
 definition: string;
 sourceUrl: string;
 sourceLabel?: string;
 /** Which edge of the icon the tooltip anchors from. Default `left`
 * preserves the original ChannelsPage behavior (tooltip extends
 * rightward into a wide heading area). Use `right` when the icon
 * sits near a container's right edge (e.g. the narrow Librarian
 * column) — the tooltip then extends leftward and stays in-viewport. */
 align?: "left" | "right";
}

export default function DefinitionHint({
 term,
 definition,
 sourceUrl,
 sourceLabel = "Merriam-Webster ↗",
 align = "left",
}: DefinitionHintProps) {
 return (
 <span className="group relative inline-flex align-middle">
 <Info
 className="h-[17px] w-[17px] cursor-help text-white/40 transition-colors hover:text-white/85"
 tabIndex={0}
 role="img"
 aria-label={`What is a ${term.toLowerCase()}?`}
 />
 {/* The outer span is the hover bridge: its box starts at the icon's
 bottom (top-full) and the pt-2 closes the visual gap so the cursor
 can travel onto the card — and the citation link — without the
 tooltip closing. Interactive only while shown so a hidden tooltip
 never blocks clicks. */}
 <span className={`pointer-events-none absolute top-full z-50 w-72 pt-2 opacity-0 transition-opacity duration-150 group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100 ${align === "right" ? "right-0" : "left-0"}`}>
 <span
 role="tooltip"
 className="block rounded-lg border border-[var(--line)] bg-[var(--canvas)]/98 px-3.5 py-3 text-left shadow-2xl backdrop-blur"
 >
 <span className="block text-[11px] font-semibold uppercase tracking-[0.15em] text-white/75">
 {term}
 </span>
 <span className="mt-1.5 block text-[12.5px] font-normal normal-case leading-relaxed tracking-normal text-foreground/70">
 {definition}
 </span>
 <a
 href={sourceUrl}
 target="_blank"
 rel="noopener noreferrer"
 className="mt-2 inline-block text-[10px] uppercase tracking-[0.15em] text-white/40 underline decoration-dotted underline-offset-2 transition-colors hover:text-white/85"
 >
 {sourceLabel}
 </a>
 </span>
 </span>
 </span>
 );
}

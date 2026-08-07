/**
 * Conversational Compiler — IR body pseudo-code leaf components.
 *
 * The typed-IR rendering of a single node, syntax-highlighted by field
 * type. PURE leaves — no chrome, no interaction, no sizing wrapper.
 *
 * One body component per node type:
 * - IRBodyPseudoCode → Commit_P (claim) — the original Track A V0
 * renderer. Sources data from `CompilerClaim` (tracked_claims row).
 * - IRBodyMotion → Motion node (Track B 2026-06-05)
 * - IRBodyVote → Vote node (Track B 2026-06-05)
 *
 * Reused by:
 * - CompilerPage.tsx (list mode) → wrapped in clickable IRBlock chrome
 * - CompilerGraphPane.tsx (graph) → embedded inside an SVG <foreignObject>
 * per dagre-positioned node
 *
 * Per SPEC § "CFG layout: dagre + hand-rolled SVG" + BRAINSTORM_LOG
 * 2026-06-04 item 4 — bodies split out so Surface A's CFG nodes can drop
 * them in without the list-mode-specific header chrome.
 */
import type { CompilerClaim, CompilerNode } from "../../utils/compiler";
import { nodeVisual } from "./statusColors";

/** Format a time-horizon integer as a plain-English phrase.: keep
 * the IR pseudo-code human-readable while still typed (never raw enum). */
export function formatHorizon(months: number | null): string {
 if (months == null) return "null";
 if (months === 1) return "1 month";
 if (months < 12) return `${months} months`;
 if (months === 12) return "1 year";
 if (months % 12 === 0) return `${months / 12} years`;
 return `${months} months`;
}

/** Tiny syntax-highlighted KV line. Keys/values/punctuation get distinct
 * colors so the pseudo-code is scannable at a glance. Font-size + family
 * inherit from the parent wrapper (lets compact / non-compact share). */
function IRLine({
 k,
 value,
 valueClass,
 isLast,
 compact,
}: {
 k: string;
 value: React.ReactNode;
 valueClass?: string;
 isLast?: boolean;
 compact?: boolean;
}) {
 const indent = compact ? "pl-4" : "pl-6";
 const leading = compact ? "leading-snug" : "leading-relaxed";
 return (
 <div className={`${leading} ${indent}`}>
 <span className="text-sky-300/90">{k}</span>
 <span className="text-zinc-500">: </span>
 <span className={valueClass ?? "text-amber-200/90"}>{value}</span>
 {!isLast && <span className="text-zinc-500">,</span>}
 </div>
 );
}

function IRStringValue({ children }: { children: React.ReactNode }) {
 return (
 <span className="text-amber-200/90">
 <span className="text-zinc-600">'</span>
 {children}
 <span className="text-zinc-600">'</span>
 </span>
 );
}

function IRArrayValue({ items }: { items: string[] }) {
 if (!items || items.length === 0) {
 return <span className="text-zinc-500">[]</span>;
 }
 return (
 <span>
 <span className="text-zinc-500">[</span>
 {items.map((t, i) => (
 <span key={t}>
 <IRStringValue>{t}</IRStringValue>
 {i < items.length - 1 && <span className="text-zinc-500">, </span>}
 </span>
 ))}
 <span className="text-zinc-500">]</span>
 </span>
 );
}

/** The typed-IR pseudo-code body for one Commit_P claim. Pure leaf —
 * NO chrome, NO interaction, NO outer sizing.
 *
 * `compact` shrinks the body for graph-mode CFG nodes (Opus critique
 * 2026-06-04 item 2): list-mode is for reading every field at 13px;
 * graph-mode is for seeing the *shape* of the program, so the body
 * collapses to 11px in narrower SVG `<foreignObject>` boxes. */
export function IRBodyPseudoCode({
 claim,
 compact = false,
}: {
 claim: CompilerClaim;
 compact?: boolean;
}) {
 const fontSize = compact ? "text-[11px]" : "text-[13px]";
 const padding = compact ? "px-3 py-2" : "px-4 py-3";
 return (
 <div className={`${padding} font-mono ${fontSize}`}>
 <div className="text-zinc-400">
 <span className="text-sky-300/90">Commit_P</span>
 <span className="text-zinc-500"> {"{"}</span>
 </div>

 <IRLine
 k="claim_text"
 value={<IRStringValue>{claim.claim_text}</IRStringValue>}
 compact={compact}
 />
 {claim.claim_type && (
 <IRLine
 k="claim_type"
 value={<IRStringValue>{claim.claim_type}</IRStringValue>}
 compact={compact}
 />
 )}
 <IRLine
 k="subject_tags"
 value={<IRArrayValue items={claim.topic_tags} />}
 compact={compact}
 />
 <IRLine
 k="time_horizon"
 value={
 <span className="text-emerald-300/90">{formatHorizon(claim.time_horizon_months)}</span>
 }
 compact={compact}
 />
 {claim.expected_outcome && (
 <IRLine
 k="expected_outcome"
 value={<IRStringValue>{claim.expected_outcome}</IRStringValue>}
 compact={compact}
 />
 )}
 {claim.confidence && (
 <IRLine
 k="confidence"
 value={<IRStringValue>{claim.confidence}</IRStringValue>}
 compact={compact}
 />
 )}
 <IRLine
 k="status"
 value={<span className="text-violet-300/90">{claim.status}</span>}
 isLast
 compact={compact}
 />

 <div className="text-zinc-500">{"}"}</div>
 </div>
 );
}

/** Safely read a string field from a CompilerNode's typed_fields blob.
 * The API parses typed_fields as JSON server-side; we still defensive-
 * cast here because typed_fields is declared `Record<string, unknown>`. */
function tfString(tf: Record<string, unknown>, key: string): string | null {
 const v = tf[key];
 return typeof v === "string" && v.trim() ? v : null;
}

/** Body — Motion node (Track B). Per SPEC § Node types Motion typed_fields:
 * {summary_sentence, motion_text, motion_type, agenda_item, context}. */
export function IRBodyMotion({
 node,
 compact = false,
}: {
 node: CompilerNode;
 compact?: boolean;
}) {
 const tf = node.typed_fields;
 const fontSize = compact ? "text-[11px]" : "text-[13px]";
 const padding = compact ? "px-3 py-2" : "px-4 py-3";
 const summary = tfString(tf, "summary_sentence");
 const motionText = tfString(tf, "motion_text");
 const motionType = tfString(tf, "motion_type");
 const agendaItem = tfString(tf, "agenda_item");
 const context = tfString(tf, "context");

 return (
 <div className={`${padding} font-mono ${fontSize}`}>
 <div className="text-zinc-400">
 <span className="text-sky-300/90">Motion</span>
 <span className="text-zinc-500"> {"{"}</span>
 </div>
 {summary && (
 <IRLine
 k="summary"
 value={<IRStringValue>{summary}</IRStringValue>}
 compact={compact}
 />
 )}
 {motionText && (
 <IRLine
 k="motion_text"
 value={<IRStringValue>{motionText}</IRStringValue>}
 compact={compact}
 />
 )}
 {motionType && (
 <IRLine
 k="motion_type"
 value={<span className="text-violet-300/90">{motionType}</span>}
 compact={compact}
 />
 )}
 {agendaItem && (
 <IRLine
 k="agenda_item"
 value={<IRStringValue>{agendaItem}</IRStringValue>}
 compact={compact}
 />
 )}
 <IRLine
 k="context"
 value={
 context ? <IRStringValue>{context}</IRStringValue> : <span className="text-zinc-500">null</span>
 }
 isLast
 compact={compact}
 />
 <div className="text-zinc-500">{"}"}</div>
 </div>
 );
}

/** Body — Vote node (Track B). Per SPEC § Node types Vote typed_fields:
 * {summary_sentence, motion_reference, vote_result, vote_method,
 * per_member_votes: [{member, vote}], tally, agenda_item, context}. */
export function IRBodyVote({
 node,
 compact = false,
}: {
 node: CompilerNode;
 compact?: boolean;
}) {
 const tf = node.typed_fields;
 const fontSize = compact ? "text-[11px]" : "text-[13px]";
 const padding = compact ? "px-3 py-2" : "px-4 py-3";
 const summary = tfString(tf, "summary_sentence");
 const motionRef = tfString(tf, "motion_reference");
 const voteResult = tfString(tf, "vote_result");
 const voteMethod = tfString(tf, "vote_method");
 const agendaItem = tfString(tf, "agenda_item");
 const context = tfString(tf, "context");
 const tally = tf.tally as Record<string, number> | null | undefined;
 const perMember = Array.isArray(tf.per_member_votes)
 ? (tf.per_member_votes as Array<{ member: string; vote: string }>)
 : [];

 const tallyParts = tally
 ? (["aye", "nay", "abstain", "absent"] as const)
 .map(k => (typeof tally[k] === "number" ? `${k}: ${tally[k]}` : null))
 .filter((v): v is string => v !== null)
 : [];

 return (
 <div className={`${padding} font-mono ${fontSize}`}>
 <div className="text-zinc-400">
 <span className="text-sky-300/90">Vote</span>
 <span className="text-zinc-500"> {"{"}</span>
 </div>
 {summary && (
 <IRLine
 k="summary"
 value={<IRStringValue>{summary}</IRStringValue>}
 compact={compact}
 />
 )}
 {motionRef && (
 <IRLine
 k="motion_reference"
 value={<IRStringValue>{motionRef}</IRStringValue>}
 compact={compact}
 />
 )}
 {voteResult && (
 <IRLine
 k="vote_result"
 value={<span className="text-violet-300/90">{voteResult}</span>}
 compact={compact}
 />
 )}
 {voteMethod && (
 <IRLine
 k="vote_method"
 value={<span className="text-emerald-300/90">{voteMethod}</span>}
 compact={compact}
 />
 )}
 {tallyParts.length > 0 && (
 <IRLine
 k="tally"
 value={
 <span>
 <span className="text-zinc-500">{"{"}</span>
 <span className="text-amber-200/90">{tallyParts.join(", ")}</span>
 <span className="text-zinc-500">{"}"}</span>
 </span>
 }
 compact={compact}
 />
 )}
 {perMember.length > 0 && (() => {
 // Per Opus critique 2026-06-05 item 9: "N entries" was
 // collapsing the substantive content (who-voted-how) to a row
 // count. Group by vote so the operator sees aye / nay / abstain
 // / absent / recused as arrays of member names, IR-style.
 const groups: Record<string, string[]> = {};
 for (const pm of perMember) {
 const v = (pm.vote || "").toLowerCase();
 if (!v || !pm.member) continue;
 (groups[v] ??= []).push(pm.member);
 }
 const order: ReadonlyArray<string> = ["aye", "nay", "abstain", "absent", "recused"];
 const lines: Array<{ vote: string; members: string[] }> = [];
 for (const v of order) {
 const m = groups[v];
 if (m && m.length > 0) lines.push({ vote: v, members: m });
 }
 if (lines.length === 0) {
 return (
 <IRLine
 k="per_member_votes"
 value={<IRArrayValue items={[]} />}
 compact={compact}
 />
 );
 }
 return (
 <>
 {lines.map((l, i) => (
 <IRLine
 key={l.vote}
 k={`members.${l.vote}`}
 value={<IRArrayValue items={l.members} />}
 compact={compact}
 // None of these lines are "last" — the parent's `status`
 // / closing brace handles that; but if for some reason
 // this were the final field (no agenda_item, no context),
 // skip the trailing comma on the visually-last entry.
 isLast={i === lines.length - 1 && !tfString(tf, "agenda_item") && !tfString(tf, "context")}
 />
 ))}
 </>
 );
 })()}
 {agendaItem && (
 <IRLine
 k="agenda_item"
 value={<IRStringValue>{agendaItem}</IRStringValue>}
 compact={compact}
 />
 )}
 <IRLine
 k="context"
 value={
 context ? <IRStringValue>{context}</IRStringValue> : <span className="text-zinc-500">null</span>
 }
 isLast
 compact={compact}
 />
 <div className="text-zinc-500">{"}"}</div>
 </div>
 );
}

/** Body — Second node (Track B, Chunk B-4). typed_fields:
 * {summary_sentence, motion_reference, second_text, agenda_item,
 * context}. */
export function IRBodySecond({
 node,
 compact = false,
}: {
 node: CompilerNode;
 compact?: boolean;
}) {
 const tf = node.typed_fields;
 const fontSize = compact ? "text-[11px]" : "text-[13px]";
 const padding = compact ? "px-3 py-2" : "px-4 py-3";
 const summary = tfString(tf, "summary_sentence");
 const motionRef = tfString(tf, "motion_reference");
 const secondText = tfString(tf, "second_text");
 const agendaItem = tfString(tf, "agenda_item");
 const context = tfString(tf, "context");
 return (
 <div className={`${padding} font-mono ${fontSize}`}>
 <div className="text-zinc-400">
 <span className="text-sky-300/90">Second</span>
 <span className="text-zinc-500"> {"{"}</span>
 </div>
 {summary && (
 <IRLine k="summary" value={<IRStringValue>{summary}</IRStringValue>} compact={compact} />
 )}
 {motionRef && (
 <IRLine k="motion_reference" value={<IRStringValue>{motionRef}</IRStringValue>} compact={compact} />
 )}
 {secondText && (
 <IRLine k="second_text" value={<IRStringValue>{secondText}</IRStringValue>} compact={compact} />
 )}
 {agendaItem && (
 <IRLine k="agenda_item" value={<IRStringValue>{agendaItem}</IRStringValue>} compact={compact} />
 )}
 <IRLine
 k="context"
 value={context ? <IRStringValue>{context}</IRStringValue> : <span className="text-zinc-500">null</span>}
 isLast
 compact={compact}
 />
 <div className="text-zinc-500">{"}"}</div>
 </div>
 );
}

/** Body — AgendaTransition node (Track B, Chunk B-3). typed_fields:
 * {summary_sentence, agenda_item_number, agenda_item_title,
 * transition_text, context}. */
export function IRBodyAgendaTransition({
 node,
 compact = false,
}: {
 node: CompilerNode;
 compact?: boolean;
}) {
 const tf = node.typed_fields;
 const fontSize = compact ? "text-[11px]" : "text-[13px]";
 const padding = compact ? "px-3 py-2" : "px-4 py-3";
 const summary = tfString(tf, "summary_sentence");
 const itemNumber = tfString(tf, "agenda_item_number");
 const itemTitle = tfString(tf, "agenda_item_title");
 const transitionText = tfString(tf, "transition_text");
 const context = tfString(tf, "context");
 return (
 <div className={`${padding} font-mono ${fontSize}`}>
 <div className="text-zinc-400">
 <span className="text-sky-300/90">AgendaTransition</span>
 <span className="text-zinc-500"> {"{"}</span>
 </div>
 {summary && (
 <IRLine k="summary" value={<IRStringValue>{summary}</IRStringValue>} compact={compact} />
 )}
 {itemNumber && (
 <IRLine k="agenda_item_number" value={<IRStringValue>{itemNumber}</IRStringValue>} compact={compact} />
 )}
 {itemTitle && (
 <IRLine k="agenda_item_title" value={<IRStringValue>{itemTitle}</IRStringValue>} compact={compact} />
 )}
 {transitionText && (
 <IRLine k="transition_text" value={<IRStringValue>{transitionText}</IRStringValue>} compact={compact} />
 )}
 <IRLine
 k="context"
 value={context ? <IRStringValue>{context}</IRStringValue> : <span className="text-zinc-500">null</span>}
 isLast
 compact={compact}
 />
 <div className="text-zinc-500">{"}"}</div>
 </div>
 );
}

/** Dispatcher — picks the right body component for a given CompilerNode.
 * Falls back to a tiny "transcript-span-only" stub for node_types we
 * haven't built first-class bodies for yet (Utterance, Contradiction).
 * The stub still shows the transcript span so the operator sees the
 * actual captured words — not a "renderer pending" placeholder. */
export function IRBodyForNode({
 node,
 compact = false,
}: {
 node: CompilerNode;
 compact?: boolean;
}) {
 if (node.node_type === "Motion") return <IRBodyMotion node={node} compact={compact} />;
 if (node.node_type === "Vote") return <IRBodyVote node={node} compact={compact} />;
 if (node.node_type === "Second") return <IRBodySecond node={node} compact={compact} />;
 if (node.node_type === "AgendaTransition") return <IRBodyAgendaTransition node={node} compact={compact} />;
 // Fallback for node types we haven't built fielded bodies for yet —
 // the operator still sees the captured transcript span so the node
 // isn't a blank placeholder. lead with the actual content
 // (the span), drop meta-status words like "body renderer pending"
 // which read as construction-debt visible-to-the-operator.
 const fontSize = compact ? "text-[11px]" : "text-[13px]";
 const padding = compact ? "px-3 py-2" : "px-4 py-3";
 return (
 <div className={`${padding} font-mono ${fontSize}`}>
 <div className="text-zinc-400">
 <span className="text-sky-300/90">{node.node_type}</span>
 <span className="text-zinc-500"> {"{"}</span>
 </div>
 <IRLine
 k="span"
 value={<IRStringValue>{node.transcript_span_text}</IRStringValue>}
 isLast
 compact={compact}
 />
 <div className="text-zinc-500">{"}"}</div>
 </div>
 );
}

// ── V0.2-2 — Hex-Rays-style function-call signatures ────────────────────
//
// Per CONVERSATIONAL_COMPILER_SPEC.md § Surface A V0.2 Chunk V0.2-2:
// each typed IR node also renders as a one-line function-call signature.
// In list mode this is the collapsed-default view (click to expand to
// the full IRBody* card). In graph mode it's the only view (graph nodes
// show the function-call line, not the full body).
//
// Color taxonomy: function name takes the kind's hex (commitment = amber
// per Commit_P; motion = sky-or-zinc per Motion-substantive/procedural;
// vote = emerald/rose per Vote-passed/failed); keys are sky (matching
// IRLine's key color); string values are amber-quoted; bool/number/
// ident values are emerald; nulls are zinc.
//
// Hover tooltip uses the native title attribute for V0.2-2 first cut.
// Secondary fields (confidence, expected_outcome, context, motion_type)
// populate the tooltip. Upgrading to a portal-based tooltip is a
// follow-up if the Opus visual critique flags it.

/** Truncation helper for the inline function-call signatures — caps
 * inline string values at `max` chars with a `…` suffix. Full text
 * lives in the hover tooltip + the expanded structured body. */
function truncateInline(text: string | null | undefined, max: number): string {
 if (!text) return "";
 if (text.length <= max) return text;
 return text.slice(0, max - 1).trimEnd() + "…";
}

/** One key=value pair inside a function-call signature. */
function FnArg({
 k,
 value,
 isLast,
}: {
 k: string;
 value: React.ReactNode;
 isLast?: boolean;
}) {
 return (
 <>
 <span className="text-sky-300/85">{k}</span>
 <span className="text-zinc-600">=</span>
 {value}
 {!isLast && <span className="text-zinc-600">, </span>}
 </>
 );
}

/** Quoted-string value (amber). */
function FnString({ children }: { children: React.ReactNode }) {
 return (
 <span>
 <span className="text-zinc-600">"</span>
 <span className="text-amber-200/90">{children}</span>
 <span className="text-zinc-600">"</span>
 </span>
 );
}

/** Bare-identifier value (violet) — for status enums, motion_type
 * markers, etc. Use FnString for normal text values. */
function FnIdent({ value }: { value: string }) {
 return <span className="text-violet-300/90">{value}</span>;
}

/** Numeric value (emerald). */
function FnNumber({ value }: { value: number }) {
 return <span className="text-emerald-300/90">{value}</span>;
}

/** Boolean value (emerald). */
function FnBool({ value }: { value: boolean }) {
 return <span className="text-emerald-300/90">{value ? "true" : "false"}</span>;
}

/** Null literal (zinc). */
function FnNull() {
 return <span className="text-zinc-500">null</span>;
}

/** The chevron + `fn_name(...args)` line wrapper. The chevron's glyph
 * (▸ collapsed / ▾ expanded) is driven by `expanded`. Clicking the
 * chevron specifically stops propagation and fires `onChevronClick`
 * (caller toggles its expanded state); clicking elsewhere bubbles up
 * to the outer button's focus handler. Both interactions co-exist so
 * the row stays a single tap target for focus.
 *
 * `compact` shrinks font + padding for graph-mode <foreignObject>
 * embedding (matches IRBodyPseudoCode's compact convention). */
function FnCallLine({
 fnName,
 fnHex,
 expanded,
 compact,
 children,
 title,
 onChevronClick,
}: {
 fnName: string;
 fnHex: string;
 expanded?: boolean;
 compact?: boolean;
 children: React.ReactNode;
 title?: string;
 onChevronClick?: (e: React.MouseEvent) => void;
}) {
 const fontSize = compact ? "text-[11px]" : "text-[13px]";
 const padding = compact ? "px-3 py-1.5" : "px-4 py-2.5";
 const chev = expanded ? "▾" : "▸";
 // List mode (non-compact): force the signature to a single line with
 // CSS truncation so the SPEC's "one-line function-call signature"
 // intent is honored even when string values are longer than the JS
 // truncation alone keeps in budget. The native title attribute carries
 // secondary fields for hover; click to expand reveals the full
 // structured body. Graph mode (compact) allows natural wrap inside
 // the 380×96px SVG <foreignObject> — narrower viewport, more
 // vertical headroom, and wrap is honest about content density.
 // List mode (non-compact): force the signature to a single line with
 // CSS truncation so the SPEC's "one-line function-call signature"
 // intent is honored even when string values are longer than the JS
 // truncation alone keeps in budget. The native title attribute carries
 // secondary fields for hover; click to expand reveals the full
 // structured body. Graph mode (compact) allows natural wrap inside
 // the 380×96px SVG <foreignObject> — narrower viewport, more
 // vertical headroom, and wrap is honest about content density.
 const wrap = compact
 ? ""
 : "whitespace-nowrap overflow-hidden text-ellipsis";
 return (
 <div
 className={`${padding} font-mono ${fontSize} text-zinc-300 leading-snug ${wrap}`}
 title={title || undefined}
 >
 {/* Chevron uses role=button rather than a real <button> because
 this whole row sits inside another <button> (the IRBlock
 wrapper) — nested <button>s are invalid HTML and React warns.
 stopPropagation on the onClick keeps the outer button's
 focus/expand handler from also firing. Wider click target
 + hover bg for discoverability. */}
 <span
 onClick={onChevronClick}
 role={onChevronClick ? "button" : undefined}
 aria-label={onChevronClick ? (expanded ? "Collapse" : "Expand") : undefined}
 aria-expanded={onChevronClick ? expanded : undefined}
 tabIndex={onChevronClick ? 0 : undefined}
 className={[
 "text-zinc-600 select-none inline-block",
 "w-4 text-center mr-1 rounded",
 onChevronClick ? "cursor-pointer hover:text-zinc-300 hover:bg-white/5" : "",
 ].join(" ")}
 >
 {chev}
 </span>
 <span style={{ color: fnHex }} className="font-medium">{fnName}</span>
 <span className="text-zinc-500">(</span>
 {children}
 <span className="text-zinc-500">)</span>
 </div>
 );
}

/** Function-call signature for a Commit_P claim:
 * ▸ commitment(by="Stehly", what="ADA barrier replacement", due="6 months", status="active")
 * Secondary fields (confidence, expected_outcome, context, topic_tags)
 * populate the hover tooltip. */
export function IRFunctionCallForClaim({
 claim,
 expanded = false,
 compact = false,
 onChevronClick,
}: {
 claim: CompilerClaim;
 expanded?: boolean;
 compact?: boolean;
 onChevronClick?: (e: React.MouseEvent) => void;
}) {
 const fnHex = "#f59e0b"; // amber — matches statusVisual default for Commit_P
 const tooltipLines: string[] = [];
 if (claim.confidence) tooltipLines.push(`confidence: ${claim.confidence}`);
 if (claim.expected_outcome) tooltipLines.push(`expected_outcome: ${claim.expected_outcome}`);
 if (claim.topic_tags?.length) tooltipLines.push(`topic_tags: [${claim.topic_tags.join(", ")}]`);
 if (claim.context) tooltipLines.push(`context: ${claim.context}`);
 return (
 <FnCallLine
 fnName="commitment"
 fnHex={fnHex}
 expanded={expanded}
 compact={compact}
 title={tooltipLines.join("\n")}
 onChevronClick={onChevronClick}
 >
 <FnArg
 k="by"
 value={claim.speaker_name ? <FnString>{claim.speaker_name}</FnString> : <FnNull />}
 />
 <FnArg k="what" value={<FnString>{truncateInline(claim.claim_text, 45)}</FnString>} />
 <FnArg
 k="due"
 value={
 claim.time_horizon_months !== null
 ? <FnString>{formatHorizon(claim.time_horizon_months)}</FnString>
 : <FnNull />
 }
 />
 <FnArg k="status" value={<FnIdent value={claim.status} />} isLast />
 </FnCallLine>
 );
}

/** Function-call signature for a Motion node:
 * ▸ motion(mover="Walker", body="approve zoning 4B", agenda_item="4B")
 * Secondary fields (motion_type, context, summary if distinct from
 * motion_text) populate the hover tooltip. */
function IRFunctionCallMotion({
 node,
 expanded = false,
 compact = false,
 onChevronClick,
}: {
 node: CompilerNode;
 expanded?: boolean;
 compact?: boolean;
 onChevronClick?: (e: React.MouseEvent) => void;
}) {
 const tf = node.typed_fields;
 const v = nodeVisual(node.node_type, tf);
 const summary = tfString(tf, "summary_sentence");
 const motionText = tfString(tf, "motion_text");
 const motionType = tfString(tf, "motion_type");
 const agendaItem = tfString(tf, "agenda_item");
 const context = tfString(tf, "context");
 // Prefer the explicit motion_text; fall back to summary_sentence (some
 // procedural motions just have the summary); final fallback is the raw
 // transcript span so the operator always sees substance, never empty.
 const body = motionText || summary || node.transcript_span_text;

 const tooltipLines: string[] = [];
 if (summary && motionText && summary !== motionText) {
 tooltipLines.push(`summary: ${summary}`);
 }
 if (motionType && !agendaItem) {
 // motion_type already shown when agenda_item is absent (see args
 // below); skip it from the tooltip in that case.
 } else if (motionType) {
 tooltipLines.push(`motion_type: ${motionType}`);
 }
 if (context) tooltipLines.push(`context: ${context}`);

 return (
 <FnCallLine
 fnName="motion"
 fnHex={v.hex}
 expanded={expanded}
 compact={compact}
 title={tooltipLines.join("\n")}
 onChevronClick={onChevronClick}
 >
 <FnArg
 k="mover"
 value={node.speaker_name ? <FnString>{node.speaker_name}</FnString> : <FnNull />}
 />
 <FnArg k="body" value={<FnString>{truncateInline(body, 40)}</FnString>} />
 {agendaItem ? (
 <FnArg k="agenda_item" value={<FnString>{agendaItem}</FnString>} isLast />
 ) : (
 <FnArg
 k="type"
 value={motionType ? <FnIdent value={motionType} /> : <FnNull />}
 isLast
 />
 )}
 </FnCallLine>
 );
}

/** Function-call signature for a Vote node:
 * ▸ vote(motion="Item 4B", result="passed", ayes=5, nays=0)
 * Per-member tallies + vote_method populate the hover tooltip. */
function IRFunctionCallVote({
 node,
 expanded = false,
 compact = false,
 onChevronClick,
}: {
 node: CompilerNode;
 expanded?: boolean;
 compact?: boolean;
 onChevronClick?: (e: React.MouseEvent) => void;
}) {
 const tf = node.typed_fields;
 const v = nodeVisual(node.node_type, tf);
 const motionRef = tfString(tf, "motion_reference");
 const voteResult = tfString(tf, "vote_result");
 const voteMethod = tfString(tf, "vote_method");
 const agendaItem = tfString(tf, "agenda_item");
 const context = tfString(tf, "context");
 const tally = tf.tally as Record<string, number> | null | undefined;
 const ayes = typeof tally?.aye === "number" ? tally.aye : null;
 const nays = typeof tally?.nay === "number" ? tally.nay : null;
 const motionLabel = motionRef || agendaItem || null;

 const tooltipLines: string[] = [];
 if (voteMethod) tooltipLines.push(`vote_method: ${voteMethod}`);
 if (tally) {
 const parts = (["aye", "nay", "abstain", "absent"] as const)
 .map(k => (typeof tally[k] === "number" ? `${k}: ${tally[k]}` : null))
 .filter((x): x is string => x !== null);
 if (parts.length > 0) tooltipLines.push(`tally: {${parts.join(", ")}}`);
 }
 if (context) tooltipLines.push(`context: ${context}`);

 return (
 <FnCallLine
 fnName="vote"
 fnHex={v.hex}
 expanded={expanded}
 compact={compact}
 title={tooltipLines.join("\n")}
 onChevronClick={onChevronClick}
 >
 <FnArg
 k="motion"
 value={motionLabel ? <FnString>{truncateInline(motionLabel, 35)}</FnString> : <FnNull />}
 />
 <FnArg
 k="result"
 value={voteResult ? <FnIdent value={voteResult} /> : <FnNull />}
 isLast={ayes === null && nays === null}
 />
 {ayes !== null && (
 <FnArg k="ayes" value={<FnNumber value={ayes} />} isLast={nays === null} />
 )}
 {nays !== null && (
 <FnArg k="nays" value={<FnNumber value={nays} />} isLast />
 )}
 </FnCallLine>
 );
}

/** Function-call signature for a Second node:
 * ▸ second(motion="Item 4B", by="Savage") */
function IRFunctionCallSecond({
 node,
 expanded = false,
 compact = false,
 onChevronClick,
}: {
 node: CompilerNode;
 expanded?: boolean;
 compact?: boolean;
 onChevronClick?: (e: React.MouseEvent) => void;
}) {
 const tf = node.typed_fields;
 const v = nodeVisual(node.node_type, tf);
 const motionRef = tfString(tf, "motion_reference");
 const agendaItem = tfString(tf, "agenda_item");
 const secondText = tfString(tf, "second_text");
 const context = tfString(tf, "context");
 const motionLabel = motionRef || agendaItem || null;

 const tooltipLines: string[] = [];
 if (secondText) tooltipLines.push(`second_text: ${secondText}`);
 if (context) tooltipLines.push(`context: ${context}`);

 return (
 <FnCallLine
 fnName="second"
 fnHex={v.hex}
 expanded={expanded}
 compact={compact}
 title={tooltipLines.join("\n")}
 onChevronClick={onChevronClick}
 >
 <FnArg
 k="motion"
 value={motionLabel ? <FnString>{truncateInline(motionLabel, 35)}</FnString> : <FnNull />}
 />
 <FnArg
 k="by"
 value={node.speaker_name ? <FnString>{node.speaker_name}</FnString> : <FnNull />}
 isLast
 />
 </FnCallLine>
 );
}

/** Function-call signature for an AgendaTransition node:
 * ▸ agenda_transition(item="2E", title="Take up the MOU with US Capitol Police") */
function IRFunctionCallAgendaTransition({
 node,
 expanded = false,
 compact = false,
 onChevronClick,
}: {
 node: CompilerNode;
 expanded?: boolean;
 compact?: boolean;
 onChevronClick?: (e: React.MouseEvent) => void;
}) {
 const tf = node.typed_fields;
 const v = nodeVisual(node.node_type, tf);
 const itemNumber = tfString(tf, "agenda_item_number");
 const itemTitle = tfString(tf, "agenda_item_title");
 const transitionText = tfString(tf, "transition_text");
 const context = tfString(tf, "context");

 const tooltipLines: string[] = [];
 if (transitionText) tooltipLines.push(`transition_text: ${transitionText}`);
 if (context) tooltipLines.push(`context: ${context}`);

 return (
 <FnCallLine
 fnName="agenda_transition"
 fnHex={v.hex}
 expanded={expanded}
 compact={compact}
 title={tooltipLines.join("\n")}
 onChevronClick={onChevronClick}
 >
 <FnArg
 k="item"
 value={itemNumber ? <FnString>{itemNumber}</FnString> : <FnNull />}
 />
 <FnArg
 k="title"
 value={itemTitle ? <FnString>{truncateInline(itemTitle, 45)}</FnString> : <FnNull />}
 isLast
 />
 </FnCallLine>
 );
}

/** Generic fallback for node types without first-class signatures yet.
 * Shows: ▸ utterance(speaker="...", span="...") */
function IRFunctionCallGeneric({
 node,
 expanded = false,
 compact = false,
 onChevronClick,
}: {
 node: CompilerNode;
 expanded?: boolean;
 compact?: boolean;
 onChevronClick?: (e: React.MouseEvent) => void;
}) {
 const v = nodeVisual(node.node_type, node.typed_fields);
 const fnName = node.node_type.toLowerCase();
 return (
 <FnCallLine
 fnName={fnName}
 fnHex={v.hex}
 expanded={expanded}
 compact={compact}
 onChevronClick={onChevronClick}
 >
 {node.speaker_name && (
 <FnArg k="speaker" value={<FnString>{node.speaker_name}</FnString>} />
 )}
 <FnArg
 k="span"
 value={<FnString>{truncateInline(node.transcript_span_text, 40)}</FnString>}
 isLast
 />
 </FnCallLine>
 );
}

/** Dispatcher — picks the right function-call signature for a given
 * CompilerNode. Mirrors IRBodyForNode's dispatch but emits the
 * one-line collapsed-default signature instead of the full structured
 * body. */
export function IRFunctionCallForNode({
 node,
 expanded = false,
 compact = false,
 onChevronClick,
}: {
 node: CompilerNode;
 expanded?: boolean;
 compact?: boolean;
 onChevronClick?: (e: React.MouseEvent) => void;
}) {
 const common = { node, expanded, compact, onChevronClick };
 if (node.node_type === "Motion") return <IRFunctionCallMotion {...common} />;
 if (node.node_type === "Vote") return <IRFunctionCallVote {...common} />;
 if (node.node_type === "Second") return <IRFunctionCallSecond {...common} />;
 if (node.node_type === "AgendaTransition") return <IRFunctionCallAgendaTransition {...common} />;
 return <IRFunctionCallGeneric {...common} />;
}

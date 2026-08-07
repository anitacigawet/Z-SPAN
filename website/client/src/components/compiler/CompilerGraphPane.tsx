/**
 * Conversational Compiler — CFG node-graph rendering primitive (Surface A).
 *
 * Renders the meeting's Commit_P claims as a hand-rolled SVG control-flow
 * graph. Per SPEC § "CFG layout: dagre + hand-rolled SVG" (Decision 2026-
 * 06-04, James) — react-flow was the easy path but its built-in node
 * styles push back on the IDA-Pro aesthetic. dagre does positions only;
 * we own the rendering.
 *
 * V0.2 scope: 3 isolated nodes (no edges) since Track B's parser doesn't
 * exist yet. The no-edges case takes a manual grid-wrap layout (per Opus
 * critique 2026-06-04 item 3 — a single horizontal row of dagre-output
 * nodes read as broken at typical viewports). When Track B ships and
 * `transcript_edges` data lands, the layout falls back to dagre's
 * hierarchical TB output and the grid-wrap path is bypassed.
 */
import { useEffect, useMemo, useRef } from "react";
import dagre from "dagre";
import {
 TransformWrapper,
 TransformComponent,
 type ReactZoomPanPinchRef,
} from "react-zoom-pan-pinch";
import { Maximize2, Minus, Plus, Locate } from "lucide-react";
import type { CompilerClaim, CompilerNode, CompilerEdge } from "../../utils/compiler";
import {
 IRFunctionCallForClaim,
 IRFunctionCallForNode,
} from "./IRBlock";
import { statusVisual, nodeVisual, edgeVisual } from "./statusColors";

/** String focus key matching CompilerPage's discriminator: "claim:N" or
 * "node:N". Lets the graph pane address both Track A claims and Track B
 * transcript_nodes in the same selection state without id collision. */
export type GraphFocusKey = `claim:${number}` | `node:${number}` | null;

// Compact sizes per Opus critique item 2 — list mode reads the full body;
// graph mode is for seeing the shape of the program. Body font is 11px
// (set via IRFunctionCall's `compact` prop). V0.2-2: graph nodes show
// the one-line function-call signature only (no expanded body), so
// NODE_HEIGHT shrinks from 280 → 96 (header 32 + signature line ~52
// + breathing room). Smaller nodes also let more of the program shape
// fit per viewport at the default zoom (0.65), reinforcing "see the
// program at a glance" — list mode is where the operator reads details.
const NODE_WIDTH = 380;
const NODE_HEIGHT = 96;
const HEADER_HEIGHT = 32;
const RANKSEP = 60;
const NODESEP = 48;
const GRID_GAP = 24;
const CANVAS_PADDING = 40;
// Target SVG canvas width for the no-edges grid wrap. Picked so 3 nodes
// fit at typical 1440-1600px viewports with the IRBlock chrome around
// them; if more claims appear, the grid wraps to a second row.
const GRID_TARGET_WIDTH = 1320;

/** Polymorphic CFG item — wraps either a Track A Commit_P claim or a
 * Track B transcript_nodes row. Used as the layout/render unit so
 * dagre + the SVG renderer don't have to know about the upstream
 * source. The `focusKey` field is what binds the item back to
 * CompilerPage's focus state. */
type GraphItem =
 | { kind: "claim"; focusKey: `claim:${number}`; claim: CompilerClaim; index: number }
 | { kind: "node"; focusKey: `node:${number}`; node: CompilerNode; index: number };

interface LaidOutItem {
 /** Unique key for React + layout indexing — string form of the focus
 * key ("claim:5" / "node:7"). */
 key: string;
 focusKey: GraphItem["focusKey"];
 x: number; // top-left
 y: number;
 width: number;
 height: number;
 item: GraphItem;
}

interface LaidOutEdge {
 edge: CompilerEdge;
 /** Polyline waypoints from dagre, or a hand-routed two-point fallback
 * when dagre wasn't given this edge (shouldn't happen in V0 since we
 * always seed dagre with the edges we render). */
 points: Array<{ x: number; y: number }>;
}

interface Layout {
 items: LaidOutItem[];
 edges: LaidOutEdge[];
 width: number;
 height: number;
}

function gridWrapLayout(items: GraphItem[]): Layout {
 const perRow = Math.max(
 1,
 Math.floor((GRID_TARGET_WIDTH - 2 * CANVAS_PADDING + GRID_GAP) / (NODE_WIDTH + GRID_GAP)),
 );
 const laid: LaidOutItem[] = items.map((it, index) => {
 const row = Math.floor(index / perRow);
 const col = index % perRow;
 return {
 key: it.focusKey,
 focusKey: it.focusKey,
 x: CANVAS_PADDING + col * (NODE_WIDTH + GRID_GAP),
 y: CANVAS_PADDING + row * (NODE_HEIGHT + GRID_GAP),
 width: NODE_WIDTH,
 height: NODE_HEIGHT,
 item: it,
 };
 });
 const totalRows = Math.ceil(items.length / perRow);
 const cols = Math.min(items.length, perRow);
 return {
 items: laid,
 edges: [],
 width: cols * NODE_WIDTH + (cols - 1) * GRID_GAP + 2 * CANVAS_PADDING,
 height: totalRows * NODE_HEIGHT + (totalRows - 1) * GRID_GAP + 2 * CANVAS_PADDING,
 };
}

function dagreLayout(items: GraphItem[], edges: CompilerEdge[]): Layout {
 const g = new dagre.graphlib.Graph();
 g.setGraph({
 rankdir: "TB",
 ranksep: RANKSEP,
 nodesep: NODESEP,
 marginx: CANVAS_PADDING,
 marginy: CANVAS_PADDING,
 });
 g.setDefaultEdgeLabel(() => ({}));

 // Build the set of focus keys we have items for. dagre will throw if
 // we add an edge whose endpoints aren't both registered nodes, so
 // filter edges to only those whose both endpoints are in the item set.
 const focusKeys = new Set(items.map(it => it.focusKey));
 items.forEach(it => {
 g.setNode(it.focusKey, { width: NODE_WIDTH, height: NODE_HEIGHT });
 });
 const renderable = edges.filter(
 e => focusKeys.has(e.source_focus_key as `claim:${number}` | `node:${number}`)
 && focusKeys.has(e.target_focus_key as `claim:${number}` | `node:${number}`),
 );
 renderable.forEach(e => {
 g.setEdge(e.source_focus_key, e.target_focus_key, { id: e.id });
 });

 dagre.layout(g);

 const laid: LaidOutItem[] = items.map(it => {
 const n = g.node(it.focusKey);
 return {
 key: it.focusKey,
 focusKey: it.focusKey,
 x: n.x - n.width / 2,
 y: n.y - n.height / 2,
 width: n.width,
 height: n.height,
 item: it,
 };
 });

 const laidEdges: LaidOutEdge[] = renderable.map(e => {
 const dagreEdge = g.edge(e.source_focus_key, e.target_focus_key);
 const points = dagreEdge?.points ?? [];
 return { edge: e, points };
 });

 const gr = g.graph();
 return {
 items: laid,
 edges: laidEdges,
 width: gr.width ?? NODE_WIDTH,
 height: gr.height ?? NODE_HEIGHT,
 };
}

function useGraphLayout(items: GraphItem[], edges: CompilerEdge[]): Layout {
 return useMemo(
 () => (edges.length > 0 ? dagreLayout(items, edges) : gridWrapLayout(items)),
 [items, edges],
 );
}

/** Map a CFG item's source to the visual identity tokens used on its
 * SVG chrome (hex stripe, pill word). Delegates to the shared
 * statusVisual (Commit_P claims) + nodeVisual (transcript_nodes)
 * helpers so list mode and graph mode stay in lockstep. Per Opus
 * critique 2026-06-05 item 6 — previously this was duplicated in
 * CompilerPage.tsx / CompilerGraphPane.tsx with different return
 * shapes; risk of drift the moment a new Motion sub-type or Vote
 * result landed. */
function itemVisualHex(item: GraphItem): { hex: string; word: string } {
 if (item.kind === "claim") {
 const v = statusVisual(item.claim.status);
 return { hex: v.hex, word: v.word };
 }
 const v = nodeVisual(item.node.node_type, item.node.typed_fields);
 return { hex: v.hex, word: v.word };
}

/** Speaker line for an item — Commit_P uses the claim's speaker; Vote
 * has no single speaker (body action); Motion has the mover. */
function itemSpeakerLine(item: GraphItem): string {
 if (item.kind === "claim") {
 const c = item.claim;
 return c.speaker_name
 ? `${c.speaker_name}${c.speaker_title ? `, ${c.speaker_title}` : ""}`
 : "Speaker unresolved";
 }
 const n = item.node;
 if (n.speaker_name) {
 return `${n.speaker_name}${n.speaker_title ? `, ${n.speaker_title}` : ""}`;
 }
 return n.node_type === "Vote" ? "Body action" : "Speaker unresolved";
}

/** node_type label string used in the SVG header chrome. "Commitment"
 * for Commit_P (the chrome polish — the underscore-suffix IR
 * identifier stays inside the pseudo-code body where it IS the
 * program's source identifier; the chrome reads in human vocabulary).
 * Transcript_nodes types are passed through verbatim since the
 * canonical IR types for those are already underscore-free
 * (Motion / Vote / Second / AgendaTransition / etc.). */
function itemNodeTypeLabel(item: GraphItem): string {
 return item.kind === "claim" ? "Commitment" : item.node.node_type;
}

/** One CFG node, rendered as an SVG <g> at its computed position.
 * Polymorphic over GraphItem — Commit_P claim or Track B transcript
 * node. Body dispatches via IRBodyPseudoCode (Commit_P) or
 * IRBodyForNode (Motion / Vote / fallback). */
function CFGNode({
 laid,
 isFocused,
 onFocus,
}: {
 laid: LaidOutItem;
 isFocused: boolean;
 onFocus: () => void;
}) {
 const { item, x, y, width, height } = laid;
 const { hex, word } = itemVisualHex(item);
 const speakerLine = itemSpeakerLine(item);
 const nodeTypeLabel = itemNodeTypeLabel(item);
 const ordinal = item.index + 1;

 // Focus visuals — Opus critique item 5 said the 1px border shift was
 // imperceptible. Bump to 3px + brighter cyan + a faint outer halo via
 // a second rect for a "this is selected" cue the user can't miss.
 const borderStroke = isFocused ? "#7dd3fc" : "#2563eb33";
 const borderWidth = isFocused ? 2.5 : 1;
 const PILL_W = 84;
 const PILL_H = 18;
 const PILL_X = width - PILL_W - 10;

 return (
 <g
 transform={`translate(${x}, ${y})`}
 onClick={onFocus}
 // The `cfg-node-clickable` class is the marker react-zoom-pan-pinch
 // uses to exclude this element from initiating a pan — clicks on
 // nodes focus the node; pans only start when the operator drags
 // empty canvas space. Without the marker, clicking-and-holding a
 // node would initiate a pan instead of registering a click.
 className="cfg-node-clickable cursor-pointer"
 style={{ pointerEvents: "all" }}
 >
 {/* Focus halo */}
 {isFocused && (
 <rect
 x={-4}
 y={-4}
 width={width + 8}
 height={height + 8}
 rx={9}
 ry={9}
 fill="none"
 stroke="#7dd3fc"
 strokeOpacity={0.25}
 strokeWidth={6}
 />
 )}

 {/* Outer node body */}
 <rect
 width={width}
 height={height}
 rx={6}
 ry={6}
 fill="#0a0f1a"
 stroke={borderStroke}
 strokeWidth={borderWidth}
 />

 {/* Hex-stripe header */}
 <rect width={width} height={3} fill={hex} />

 {/* Header strip */}
 <g transform={`translate(0, 3)`}>
 <rect width={width} height={HEADER_HEIGHT - 3} fill="#0e1626" />
 <line
 x1={0}
 y1={HEADER_HEIGHT - 3}
 x2={width}
 y2={HEADER_HEIGHT - 3}
 stroke="#1e3a5f"
 strokeWidth={1}
 />
 <text
 x={12}
 y={(HEADER_HEIGHT - 3) / 2 + 3}
 dy="0.35em"
 fontSize={10}
 fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
 letterSpacing="0.08em"
 fill="#a1a1aa"
 textRendering="optimizeLegibility"
 >
 <tspan fill="#71717a" style={{ textTransform: "uppercase" }}>NODE </tspan>
 {/* Per Opus item 5: type label paints the kind's hex so it
 agrees with the stripe + pill (was always sky-300). */}
 <tspan fill={hex}>{nodeTypeLabel}</tspan>
 <tspan fill="#52525b"> · </tspan>
 <tspan fill="#a1a1aa">#{ordinal}</tspan>
 <tspan fill="#52525b"> · </tspan>
 <tspan fill="#d4d4d8">{speakerLine}</tspan>
 </text>
 {/* Status pill — color-coded per node kind */}
 <g transform={`translate(${PILL_X}, ${(HEADER_HEIGHT - 3 - PILL_H) / 2})`}>
 <rect
 width={PILL_W}
 height={PILL_H}
 rx={9}
 ry={9}
 fill={hex}
 fillOpacity={0.15}
 stroke={hex}
 strokeOpacity={0.35}
 strokeWidth={1}
 />
 <text
 x={PILL_W / 2}
 y={PILL_H / 2}
 dy="0.35em"
 fontSize={9}
 fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
 letterSpacing="0.1em"
 fontWeight={500}
 fill={hex}
 textAnchor="middle"
 style={{ textTransform: "uppercase" }}
 >
 {word}
 </text>
 </g>
 </g>

 {/* Body — V0.2-2: function-call signature only (graph mode is
 the "see program shape" view; the full structured body lives
 in list mode where the operator goes to read details). The
 chevron renders as a visual indicator only — graph mode
 doesn't carry an expanded state, so no onChevronClick. */}
 <foreignObject
 x={0}
 y={HEADER_HEIGHT}
 width={width}
 height={height - HEADER_HEIGHT}
 >
 <div style={{ width: "100%", height: "100%", overflow: "hidden" }}>
 {item.kind === "claim" ? (
 <IRFunctionCallForClaim claim={item.claim} compact />
 ) : (
 <IRFunctionCallForNode node={item.node} compact />
 )}
 </div>
 </foreignObject>
 </g>
 );
}

/** Render one edge as an SVG polyline between dagre's computed
 * waypoints, with a small arrowhead at the target via marker-end.
 * Edge color + stroke + label vocabulary come from edgeVisual() in
 * statusColors. `satisfies` edges are rendered heavier (wider stroke +
 * a faint outer glow) so the semantically heavier "Heap-allocation-
 * freed" edge reads with more visual weight than the procedural
 * `responds_to` edges — per the Opus critique pre-commit pass: hue
 * alone wasn't carrying the semantic distinction. */
function CFGEdge({ laid }: { laid: LaidOutEdge }) {
 const { edge, points } = laid;
 if (points.length < 2) return null;
 const v = edgeVisual(edge.edge_type);
 const d = points
 .map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`)
 .join(" ");
 const isHeavy = edge.edge_type === "satisfies";
 return (
 <g className="edge" data-edge-type={edge.edge_type}>
 {isHeavy && (
 <path
 d={d}
 fill="none"
 stroke={v.hex}
 strokeWidth={5}
 strokeOpacity={0.18}
 markerEnd={undefined}
 />
 )}
 <path
 d={d}
 fill="none"
 stroke={v.hex}
 strokeWidth={isHeavy ? 2.25 : 1.5}
 strokeOpacity={isHeavy ? 0.95 : 0.85}
 strokeDasharray={v.dash}
 markerEnd={`url(#cfg-arrow-${edge.edge_type})`}
 />
 </g>
 );
}

/** Compute a single representative point on the edge polyline for the
 * "responds to" / "satisfies" label. Picked as the midpoint of the
 * longest segment, with a small offset so the label sits beside the
 * line rather than on top of it. */
function edgeLabelPosition(points: Array<{ x: number; y: number }>): {
 x: number;
 y: number;
} | null {
 if (points.length < 2) return null;
 let bestLen = -1;
 let bestMidX = points[0].x;
 let bestMidY = points[0].y;
 for (let i = 0; i < points.length - 1; i++) {
 const a = points[i];
 const b = points[i + 1];
 const dx = b.x - a.x;
 const dy = b.y - a.y;
 const len = Math.hypot(dx, dy);
 if (len > bestLen) {
 bestLen = len;
 bestMidX = (a.x + b.x) / 2;
 bestMidY = (a.y + b.y) / 2;
 }
 }
 return { x: bestMidX, y: bestMidY };
}

/** Floating overlay with +, −, fit-to-content, and recenter controls.
 * Sibling to the TransformWrapper — uses the wrapper's ref directly so
 * it doesn't need to be inside the wrapper context (`useControls` only
 * works from inside; the ref pattern works from anywhere). */
function ZoomControlsOverlay({
 zoomRef,
 onRecenter,
 onFitToContent,
}: {
 zoomRef: React.RefObject<ReactZoomPanPinchRef | null>;
 onRecenter: () => void;
 onFitToContent: () => void;
}) {
 const btn = (
 title: string,
 onClick: () => void,
 Icon: typeof Plus,
 ) => (
 <button
 type="button"
 title={title}
 onClick={onClick}
 className="flex items-center justify-center w-8 h-8 rounded text-zinc-300 hover:text-white hover:bg-white/5 active:bg-white/10 transition-colors"
 >
 <Icon className="w-4 h-4" />
 </button>
 );
 return (
 <div className="absolute top-3 right-3 z-20 flex flex-col gap-0.5 p-1 rounded-md border border-[var(--line)] bg-[#050810]/85 backdrop-blur shadow-lg">
 {btn("Zoom in (scroll up)", () => zoomRef.current?.zoomIn(0.25, 200), Plus)}
 {btn("Zoom out (scroll down)", () => zoomRef.current?.zoomOut(0.25, 200), Minus)}
 <div className="h-px bg-[var(--line)]/60 my-0.5 mx-0.5" />
 {btn("Fit whole graph in view", onFitToContent, Maximize2)}
 {btn("Recenter on first edge", onRecenter, Locate)}
 </div>
 );
}

interface CompilerGraphPaneProps {
 claims: CompilerClaim[];
 nodes: CompilerNode[];
 edges: CompilerEdge[];
 meetingId: number;
 focusKey: GraphFocusKey;
 onFocus: (next: GraphFocusKey) => void;
}

export default function CompilerGraphPane({
 claims,
 nodes,
 edges,
 meetingId,
 focusKey,
 onFocus,
}: CompilerGraphPaneProps) {
 // Build the unified GraphItem list, claims first (Commit_P) then nodes
 // (Motion / Vote / others). Per-kind ordinals are within each group
 // so the header "Motion #3" stays aligned with list mode's grouping.
 const items: GraphItem[] = useMemo(() => {
 const out: GraphItem[] = [];
 claims.forEach((claim, index) =>
 out.push({ kind: "claim", focusKey: `claim:${claim.id}`, claim, index }),
 );
 // Group nodes by type so each Motion/Vote/etc. gets its own ordinal
 // sequence — mirrors the list-mode grouping logic.
 const byType = new Map<string, number>();
 nodes.forEach(n => {
 const next = (byType.get(n.node_type) ?? 0);
 out.push({ kind: "node", focusKey: `node:${n.id}`, node: n, index: next });
 byType.set(n.node_type, next + 1);
 });
 return out;
 }, [claims, nodes]);

 const layout = useGraphLayout(items, edges);

 // Pan/zoom controller — react-zoom-pan-pinch's ref API. Used both for
 // the auto-center-on-first-edge initial transform AND for the zoom
 // control buttons (+ / − / fit / reset). All four behaviors share one
 // wrapper instance so panning, zooming, and programmatic positioning
 // stay in lockstep.
 const zoomRef = useRef<ReactZoomPanPinchRef | null>(null);
 const viewportRef = useRef<HTMLDivElement | null>(null);

 // Initial scale — picked so the connected CFG region fits comfortably
 // in a typical 1600×900 viewport without pinching to read individual
 // node bodies. Operator can zoom in to read prose, zoom out to see
 // the whole program shape.
 const INITIAL_SCALE = 0.65;

 // Auto-center on first render so the operator lands inside the edge-
 // bearing region rather than on the leftmost orphan nodes. dagre
 // packs source-only orphans at rank 0 (top-left); without this,
 // the first impression is "isolated boxes" rather than "control-flow
 // graph". Computes the transform that puts the leftmost edge's
 // source node roughly 30% from the left + 25% from the top.
 useEffect(() => {
 const zoom = zoomRef.current;
 const vp = viewportRef.current;
 if (!zoom || !vp) return;
 if (layout.edges.length === 0) {
 // No edges — center on the first node so the operator at least
 // lands on substance.
 const firstNode = layout.items[0];
 if (!firstNode) return;
 const focalX = firstNode.x + firstNode.width / 2;
 const focalY = firstNode.y + firstNode.height / 2;
 const tx = vp.clientWidth * 0.5 - focalX * INITIAL_SCALE;
 const ty = vp.clientHeight * 0.35 - focalY * INITIAL_SCALE;
 zoom.setTransform(tx, ty, INITIAL_SCALE, 0);
 return;
 }
 // Pick the leftmost edge's source node as the focal point.
 const firstEdge = layout.edges.reduce((best, cur) => {
 const bestX = best.points[0]?.x ?? Infinity;
 const curX = cur.points[0]?.x ?? Infinity;
 return curX < bestX ? cur : best;
 });
 const focalX = firstEdge.points[0]?.x ?? 0;
 const focalY = firstEdge.points[0]?.y ?? 0;
 // Land the focal point at 30% from the left + 35% from the top so
 // the operator sees the source rank (top) + at least one full
 // Motion/Commit_P target rank (bottom) inside the viewport.
 const tx = vp.clientWidth * 0.3 - focalX * INITIAL_SCALE;
 const ty = vp.clientHeight * 0.35 - focalY * INITIAL_SCALE;
 zoom.setTransform(tx, ty, INITIAL_SCALE, 0);
 }, [layout]);

 // Edge-type tally for the footer + legend.
 const edgeCountsByType = useMemo(() => {
 const out: Record<string, number> = {};
 layout.edges.forEach(le => {
 const t = le.edge.edge_type;
 out[t] = (out[t] ?? 0) + 1;
 });
 return out;
 }, [layout.edges]);
 const edgeTypesPresent = Object.keys(edgeCountsByType);

 return (
 <div className="h-[calc(100vh-13rem)] rounded-md border border-[var(--line)] bg-[#050810] overflow-hidden flex flex-col">
 <div
 ref={viewportRef}
 className="flex-1 relative overflow-hidden"
 // Cursor + touch-action: react-zoom-pan-pinch sets these on its
 // own wrapper, but bumping touch-action: none at the viewport
 // level lets pinch-to-zoom work cleanly on iPad without the
 // browser hijacking the gesture for page-scroll. Also: cursor
 // grab/grabbing flow handled by the library inside the wrapper.
 style={{ touchAction: "none" }}
 >
 <TransformWrapper
 ref={zoomRef}
 initialScale={INITIAL_SCALE}
 minScale={0.2}
 maxScale={3}
 limitToBounds={false}
 centerOnInit={false}
 smooth
 wheel={{ step: 0.15 }}
 pinch={{ step: 5 }}
 doubleClick={{ disabled: true }}
 // Exclude clicks on nodes from initiating pan — clicking a
 // node should focus it, not start dragging the canvas. The
 // library walks up from the mousedown target looking for
 // any element with one of these classes; nodes carry it via
 // CFGNode's wrapper <g className="cfg-node-clickable">.
 panning={{
 excluded: ["cfg-node-clickable"],
 velocityDisabled: false,
 }}
 >
 <TransformComponent
 wrapperStyle={{ width: "100%", height: "100%" }}
 contentStyle={{ width: "auto", height: "auto" }}
 >
 {/* Sized to the actual SVG layout so the grid + canvas
 pan + zoom together — gives the spatial sense of
 navigating through a fixed-world canvas. */}
 <div
 style={{
 position: "relative",
 width: layout.width,
 height: layout.height,
 }}
 >
 <div
 aria-hidden
 className="absolute inset-0 pointer-events-none"
 style={{
 backgroundImage:
 "radial-gradient(rgba(125, 211, 252, 0.08) 1px, transparent 1px)",
 backgroundSize: "24px 24px",
 }}
 />
 <svg
 width={layout.width}
 height={layout.height}
 viewBox={`0 0 ${layout.width} ${layout.height}`}
 style={{ display: "block", position: "relative", zIndex: 1 }}
 >
 {/* Per-edge-type arrowhead markers. One marker per type so
 arrowhead color matches the edge color without runtime
 recoloring. */}
 <defs>
 {edgeTypesPresent.map(t => {
 const v = edgeVisual(t);
 return (
 <marker
 key={t}
 id={`cfg-arrow-${t}`}
 viewBox="0 0 10 10"
 refX="9"
 refY="5"
 markerWidth="6"
 markerHeight="6"
 orient="auto-start-reverse"
 >
 <path d="M 0 0 L 10 5 L 0 10 z" fill={v.hex} fillOpacity={0.85} />
 </marker>
 );
 })}
 </defs>
 <g className="edges">
 {layout.edges.map(le => (
 <CFGEdge key={le.edge.id} laid={le} />
 ))}
 {/* Edge labels (responds to / satisfies) at the midpoint of
 the longest segment, slightly above the line. */}
 {layout.edges.map(le => {
 const pos = edgeLabelPosition(le.points);
 if (!pos) return null;
 const v = edgeVisual(le.edge.edge_type);
 return (
 <g key={`label-${le.edge.id}`} transform={`translate(${pos.x}, ${pos.y - 6})`}>
 <text
 fontSize={9}
 fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
 letterSpacing="0.05em"
 fill={v.hex}
 fillOpacity={0.85}
 textAnchor="middle"
 style={{ textTransform: "lowercase", paintOrder: "stroke", stroke: "#050810", strokeWidth: 3, strokeLinejoin: "round" }}
 >
 {v.word}
 </text>
 </g>
 );
 })}
 </g>
 <g className="nodes">
 {layout.items.map(laid => (
 <CFGNode
 key={laid.key}
 laid={laid}
 isFocused={focusKey === laid.focusKey}
 onFocus={() => onFocus(laid.focusKey)}
 />
 ))}
 </g>
 </svg>
 </div>
 </TransformComponent>
 </TransformWrapper>

 {/* Zoom controls — overlay top-right. Uses useControls under the
 hood; the buttons fire programmatic zoom in / out / reset
 via the same wrapper instance the pan + auto-center logic
 shares. */}
 <ZoomControlsOverlay
 zoomRef={zoomRef}
 onRecenter={() => {
 // Re-run the same focal-point math as the initial useEffect
 // so the operator can reset to the "land on first edge"
 // default after exploring.
 const zoom = zoomRef.current;
 const vp = viewportRef.current;
 if (!zoom || !vp) return;
 if (layout.edges.length === 0) {
 const firstNode = layout.items[0];
 if (!firstNode) return;
 const focalX = firstNode.x + firstNode.width / 2;
 const focalY = firstNode.y + firstNode.height / 2;
 const tx = vp.clientWidth * 0.5 - focalX * INITIAL_SCALE;
 const ty = vp.clientHeight * 0.35 - focalY * INITIAL_SCALE;
 zoom.setTransform(tx, ty, INITIAL_SCALE, 300);
 return;
 }
 const firstEdge = layout.edges.reduce((best, cur) => {
 const bestX = best.points[0]?.x ?? Infinity;
 const curX = cur.points[0]?.x ?? Infinity;
 return curX < bestX ? cur : best;
 });
 const focalX = firstEdge.points[0]?.x ?? 0;
 const focalY = firstEdge.points[0]?.y ?? 0;
 const tx = vp.clientWidth * 0.3 - focalX * INITIAL_SCALE;
 const ty = vp.clientHeight * 0.35 - focalY * INITIAL_SCALE;
 zoom.setTransform(tx, ty, INITIAL_SCALE, 300);
 }}
 onFitToContent={() => {
 // Fit-to-content: zoom out until the whole canvas fits
 // inside the viewport, then center. Useful for getting
 // the bird's-eye view of a tall meeting's CFG.
 const zoom = zoomRef.current;
 const vp = viewportRef.current;
 if (!zoom || !vp) return;
 const padding = 60;
 const sx = (vp.clientWidth - padding * 2) / layout.width;
 const sy = (vp.clientHeight - padding * 2) / layout.height;
 const scale = Math.max(0.2, Math.min(1, Math.min(sx, sy)));
 const tx = (vp.clientWidth - layout.width * scale) / 2;
 const ty = (vp.clientHeight - layout.height * scale) / 2;
 zoom.setTransform(tx, ty, scale, 300);
 }}
 />
 </div>

 <div className="shrink-0 border-t border-[var(--line)] bg-[#050810]/90 backdrop-blur px-4 py-1.5 text-[10px] font-mono text-zinc-500 flex items-center gap-4 h-8 overflow-x-auto whitespace-nowrap">
 <span>
 <span className="text-zinc-600">Analyzing:</span>{" "}
 meeting_{meetingId}.transcript
 </span>
 <span className="text-zinc-700">|</span>
 <span>
 <span className="text-zinc-600">Nodes:</span> {layout.items.length}
 </span>
 <span className="text-zinc-700">|</span>
 <span>
 <span className="text-zinc-600">Edges:</span> {layout.edges.length}
 </span>
 {edgeTypesPresent.length > 0 && (
 <>
 <span className="text-zinc-700">|</span>
 {edgeTypesPresent.map(t => {
 const v = edgeVisual(t);
 return (
 <span key={t} className="flex items-center gap-1.5">
 <span
 aria-hidden
 style={{
 display: "inline-block",
 width: 10,
 height: 2,
 background: v.hex,
 opacity: 0.85,
 }}
 />
 <span className="text-zinc-500">{v.word} · {edgeCountsByType[t]}</span>
 </span>
 );
 })}
 </>
 )}
 {edgeTypesPresent.length === 0 && (
 <span className="text-zinc-600 italic">
 edge inference pending — no flow detected yet
 </span>
 )}
 </div>
 </div>
 );
}

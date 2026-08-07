/**
 * CityDeskPlant — the operator's hand-built growth tree, ported for the
 * City Desk.
 *
 * The GEOMETRY below is a verbatim copy of the plant the operator built
 * in PrisonBreak (PetalFlower.tsx — 1:1 with his prisonbreakdesign.pdf
 * sketch, refined over many Claude Design passes): eight leaves on
 * quadratic-bezier stems converging at one base point, a tall central
 * leaf, and a detached bud floating above the crown. Do NOT re-tune
 * these paths — the hand-drawn organic variation IS the artifact.
 *
 * What changed in the port is only the meaning bound to it:
 * PrisonBreak: case notebooks grow as petals → the case blooms
 * City Desk: agenda items grow as leaves as their approvals land →
 * the tall central leaf is the meeting record itself →
 * the detached bud is the official minutes, and it
 * blooms at adjournment.
 *
 * Status vocabulary preserved exactly:
 * pending — faint pencil outline
 * building — ink outline + diagonal hatch progressively revealed
 * base→tip as progress climbs (cross-hatch on the back
 * half), with the hand-drawn "growing" callout + dashed
 * leader-line + arrowhead anchored at the leaf's midpoint
 * completed — leaf wash + the small trumpet flower at the tip
 * failed — red ink + faint red wash
 * skipped — very faint pencil
 *
 * Colors ride the .city-desk scoped palette (--ink/--paper/--bloom/
 * --flower/--hot), so the same component renders correctly on the
 * dark-walnut desk theme.
 */
import { useMemo } from "react";

export type LeafStatus =
 | "pending"
 | "building"
 | "completed"
 | "failed"
 | "skipped";

export interface LeafEntry {
 key: string;
 label: string;
 /** Short hand-written lines shown in the growing callout. */
 subs: string[];
 status: LeafStatus;
 progress: number; // 0..100
}

interface CityDeskPlantProps {
 /** Up to 8 entries; they fill the plant's slots in sketch order
 * (outer leaves first, then the tall central leaf, then the
 * detached crown bud). */
 leaves: LeafEntry[];
 activeKey?: string | null;
 /** Callout header line (hand font). Default: "growing item". */
 calloutTitle?: string;
 /** Callout parenthetical (mono). Default: "( routing approvals )". */
 calloutSub?: string;
 onLeafClick?: (key: string) => void;
 hideCallout?: boolean;
}

const VIEWBOX_W = 760;
const VIEWBOX_H = 980;

// ── Verbatim geometry from the operator's PetalFlower (slot order =
// his registry order, read left-to-right / outside-in / center /
// crown). Keys renamed to neutral slot names only. ────────────────
const SLOT_ORDER = [
 "slot1",
 "slot2",
 "slot3",
 "slot4",
 "slot5",
 "slot6",
 "center",
 "crown",
] as const;

const GEOMETRY: Record<
 string,
 {
 stem: string | null;
 leaf: string;
 tip: { x: number; y: number };
 mid: { x: number; y: number };
 detached?: boolean;
 }
> = {
 slot1: {
 stem: "M380,900 C 360,830 310,750 265,665",
 leaf: "M215,535 C 160,520 110,560 105,620 C 105,675 175,690 240,650 C 300,610 280,548 215,535 Z",
 tip: { x: 215, y: 535 },
 mid: { x: 195, y: 595 },
 },
 slot2: {
 stem: "M380,900 C 405,830 455,750 495,665",
 leaf: "M555,540 C 610,530 660,575 660,630 C 660,680 595,685 530,650 C 475,615 495,548 555,540 Z",
 tip: { x: 555, y: 540 },
 mid: { x: 580, y: 595 },
 },
 slot3: {
 stem: "M380,900 C 360,780 320,640 285,495",
 leaf: "M240,330 C 190,320 145,355 145,420 C 145,495 215,520 275,470 C 325,425 300,345 240,330 Z",
 tip: { x: 240, y: 330 },
 mid: { x: 210, y: 415 },
 },
 slot4: {
 stem: "M380,900 C 400,780 440,640 480,495",
 leaf: "M530,330 C 585,318 635,355 635,420 C 635,495 560,520 500,470 C 450,425 475,345 530,330 Z",
 tip: { x: 530, y: 330 },
 mid: { x: 565, y: 415 },
 },
 slot5: {
 stem: "M380,900 C 365,870 295,830 220,780",
 leaf: "M175,685 C 120,657 70,685 55,745 C 50,785 95,805 160,785 C 230,765 250,725 175,685 Z",
 tip: { x: 175, y: 685 },
 mid: { x: 165, y: 745 },
 },
 slot6: {
 stem: "M380,900 C 405,870 480,830 560,785",
 leaf: "M590,705 C 640,695 690,725 700,775 C 695,820 640,820 575,790 C 525,765 535,717 590,705 Z",
 tip: { x: 590, y: 705 },
 mid: { x: 615, y: 750 },
 },
 center: {
 stem: "M380,900 C 380,750 380,580 380,440",
 leaf: "M380,250 C 340,250 310,290 315,360 C 320,430 365,440 380,440 C 395,440 440,430 445,360 C 450,290 420,250 380,250 Z",
 tip: { x: 380, y: 250 },
 mid: { x: 380, y: 350 },
 },
 crown: {
 stem: null,
 leaf: "M395,140 C 350,130 320,160 320,200 C 320,238 360,250 395,238 C 432,225 442,180 432,160 C 422,142 410,140 395,140 Z",
 tip: { x: 380, y: 175 },
 mid: { x: 380, y: 195 },
 detached: true,
 },
};

const STATUS_ORDER: Record<LeafStatus, number> = {
 skipped: 0,
 pending: 1,
 failed: 2,
 completed: 3,
 building: 4,
};

function inkStrokeFor(status: LeafStatus): {
 color: string;
 width: number;
 opacity: number;
} {
 switch (status) {
 case "completed":
 return { color: "var(--ink)", width: 1.4, opacity: 1 };
 case "building":
 return { color: "var(--ink)", width: 1.6, opacity: 1 };
 case "failed":
 return { color: "var(--hot)", width: 1.4, opacity: 1 };
 case "skipped":
 return { color: "var(--ink)", width: 1.0, opacity: 0.35 };
 case "pending":
 default:
 return { color: "var(--ink)", width: 1.0, opacity: 0.55 };
 }
}

/** The small trumpet flower that lands on a completed leaf's tip —
 * verbatim from the operator's Bloom. */
function Bloom() {
 return (
 <g>
 <path
 d="M -14 -2 C -18 -22, 14 -22, 14 -2 C 12 8, -12 8, -14 -2 Z"
 fill="var(--paper)"
 stroke="var(--ink)"
 strokeWidth="1"
 />
 <path
 d="M -10 -2 C -12 -16, 10 -16, 10 -2 Z"
 fill="var(--flower)"
 fillOpacity="0.5"
 />
 <circle cx="0" cy="-1" r="1.2" fill="var(--ink)" />
 <path d="M 0 -2 L 0 -14" stroke="var(--ink)" strokeWidth="0.7" />
 <path d="M -4 -3 L -6 -13" stroke="var(--ink)" strokeWidth="0.6" />
 <path d="M 4 -3 L 6 -13" stroke="var(--ink)" strokeWidth="0.6" />
 </g>
 );
}

interface LeafProps {
 slot: string;
 status: LeafStatus;
 progress: number;
 isActive: boolean;
 onClick?: () => void;
}

function Leaf({ slot, status, progress, isActive, onClick }: LeafProps) {
 const g = GEOMETRY[slot];
 if (!g) return null;
 const stroke = inkStrokeFor(status);
 const pct = Math.max(0, Math.min(100, progress)) / 100;

 const stripeCount = 44;
 const stripes = Array.from({ length: stripeCount }, (_, i) => i - 20);

 return (
 <g
 onClick={onClick}
 style={{ cursor: onClick ? "pointer" : "default" }}
 data-leaf={slot}
 data-status={status}
 >
 {g.stem && (
 <path
 d={g.stem}
 stroke={stroke.color}
 strokeWidth={status === "pending" || status === "skipped" ? 1 : 1.4}
 strokeOpacity={stroke.opacity}
 fill="none"
 strokeLinecap="round"
 style={{ transition: "stroke 0.4s, stroke-opacity 0.4s" }}
 />
 )}

 {status === "completed" && (
 <>
 <path d={g.leaf} fill="var(--bloom)" fillOpacity="0.16" />
 <path d={g.leaf} fill="var(--bloom-soft)" fillOpacity="0.42" />
 {g.stem && (
 <path
 d={g.stem}
 stroke="var(--bloom)"
 strokeWidth="0.9"
 strokeOpacity="0.55"
 fill="none"
 />
 )}
 </>
 )}

 {status === "failed" && <path d={g.leaf} fill="var(--hot)" fillOpacity="0.10" />}

 {status === "building" && (
 <g clipPath={`url(#cdclip-${slot})`}>
 <path d={g.leaf} fill="var(--paper-deep)" fillOpacity="0.55" />
 {stripes.map((i) => {
 const x = i * 14;
 const visible = i / 24 < pct * 1.2 - 0.15;
 return (
 <line
 key={`s-${i}`}
 x1={x}
 y1={-200}
 x2={x + 260}
 y2={1100}
 stroke="var(--ink)"
 strokeWidth="0.9"
 strokeOpacity={visible ? 0.7 : 0}
 style={{ transition: "stroke-opacity 0.4s ease" }}
 />
 );
 })}
 <g
 style={{
 opacity: pct > 0.55 ? ((pct - 0.55) / 0.45) * 0.55 : 0,
 transition: "opacity 0.4s",
 }}
 >
 {stripes.map((i) => (
 <line
 key={`x-${i}`}
 x1={i * 16 - 200}
 y1={1000}
 x2={i * 16 + 200}
 y2={-200}
 stroke="var(--ink)"
 strokeWidth="0.7"
 strokeOpacity="0.6"
 />
 ))}
 </g>
 </g>
 )}

 <path
 d={g.leaf}
 fill="transparent"
 stroke={stroke.color}
 strokeWidth={isActive ? stroke.width + 0.4 : stroke.width}
 strokeOpacity={stroke.opacity}
 strokeLinejoin="round"
 strokeLinecap="round"
 style={{ transition: "stroke-width 0.3s, stroke-opacity 0.4s" }}
 />

 {status === "completed" && (
 <g transform={`translate(${g.tip.x} ${g.tip.y})`}>
 <Bloom />
 </g>
 )}
 </g>
 );
}

// ── The growing callout — box + dashed leader + arrowhead. Layout math
// reworked 2026-07-02 after the Opus visual check root-caused the
// operator's "things pop out of frame": the original 240×180-unit
// foreignObject rendered ~103px tall at the desk panel's scale while
// its content needed ~195px, guillotining the stage/percent footer
// mid-glyph. The box is now 320×300 units (enough real pixels at
// small render scales), clamps horizontally inside the viewBox, and
// FLIPS ABOVE the leaf when the below-position would cross the
// viewBox bottom (the two low leaves + the center leaf's deep mids),
// with the leader + arrowhead mirrored to point down at the leaf. ──
const STAGES = ["drafted", "reviewed", "signed", "on agenda"];
const ARROW_BARB = 11;
const BOX_W = 320;
const BOX_H = 300;

function calloutLayout(anchor: { x: number; y: number }) {
 const boxLeft = Math.max(20, Math.min(VIEWBOX_W - BOX_W - 20, anchor.x - 140));
 const below = anchor.y + 130;
 const flip = below + BOX_H > VIEWBOX_H - 20;
 const boxTop = flip ? Math.max(20, anchor.y - 90 - BOX_H) : below;
 const tipX = anchor.x;
 const tipY = flip ? anchor.y - 58 : anchor.y + 70;
 const leaderStartY = flip ? boxTop + BOX_H : boxTop;
 const leaderPath =
 `M ${boxLeft + BOX_W / 2} ${leaderStartY} ` +
 `C ${boxLeft + BOX_W / 2} ${leaderStartY + (flip ? 20 : -20)}, ` +
 `${tipX} ${(tipY + leaderStartY) / 2}, ` +
 `${tipX} ${tipY}`;
 // Arrowhead barbs sit on the box side of the tip so the arrow always
 // points AT the leaf (up when the box is below, down when above).
 const barbOffset = flip ? -ARROW_BARB : ARROW_BARB;
 const barb1x = tipX + ARROW_BARB * 0.55;
 const barb2x = tipX - ARROW_BARB * 0.55;
 const barbY = tipY + barbOffset;
 const arrowPath = `M ${barb1x} ${barbY} L ${tipX} ${tipY} L ${barb2x} ${barbY}`;
 return { boxLeft, boxTop, leaderPath, arrowPath };
}

function CalloutLine({ anchor }: { anchor: { x: number; y: number } }) {
 const { leaderPath, arrowPath } = calloutLayout(anchor);
 return (
 <g>
 <path
 d={leaderPath}
 stroke="var(--ink)"
 strokeWidth="1"
 fill="none"
 strokeDasharray="3 4"
 strokeLinecap="round"
 />
 <path
 d={arrowPath}
 stroke="var(--ink)"
 strokeWidth={1.4}
 fill="none"
 strokeLinecap="round"
 strokeLinejoin="round"
 />
 </g>
 );
}

function CalloutBox({
 title,
 sub,
 label,
 subs,
 progress,
 anchor,
}: {
 title: string;
 sub: string;
 label: string;
 subs: string[];
 progress: number;
 anchor: { x: number; y: number };
}) {
 const { boxLeft, boxTop } = calloutLayout(anchor);
 const stepIdx = Math.min(3, Math.floor((progress / 100) * 4));
 return (
 <foreignObject
 x={boxLeft}
 y={boxTop}
 width={BOX_W}
 height={BOX_H}
 style={{ pointerEvents: "none" }}
 >
 <div
 style={{
 pointerEvents: "auto",
 background: "var(--paper)",
 border: "1.4px solid var(--ink)",
 borderRadius: 4,
 padding: "10px 12px 12px",
 lineHeight: 1.1,
 boxShadow:
 "0 1px 0 rgb(0 0 0 / 0.04), 0 12px 36px -28px rgb(0 0 0 / 0.35)",
 }}
 >
 <div
 style={{
 fontFamily: '"Atkinson Hyperlegible", sans-serif',
 fontWeight: 700,
 fontSize: 15,
 lineHeight: 1.2,
 color: "var(--ink)",
 }}
 >
 {title}
 </div>
 <div
 style={{
 fontFamily: '"JetBrains Mono", ui-monospace, monospace',
 fontSize: 11.5,
 color: "var(--ink-soft)",
 letterSpacing: ".08em",
 margin: "4px 0 8px",
 }}
 >
 {sub}
 </div>
 <div
 style={{
 fontFamily: '"Atkinson Hyperlegible", sans-serif',
 fontWeight: 700,
 fontSize: 14,
 color: "var(--ink)",
 lineHeight: 1.3,
 }}
 >
 {label}
 </div>
 {subs.length > 0 && (
 <ul style={{ margin: "4px 0 8px", padding: 0, listStyle: "none" }}>
 {subs.map((s) => (
 <li
 key={s}
 style={{
 fontFamily: '"Atkinson Hyperlegible", sans-serif',
 fontSize: 12.5,
 color: "var(--ink-soft)",
 lineHeight: 1.35,
 }}
 >
 · {s}
 </li>
 ))}
 </ul>
 )}
 <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
 {STAGES.map((s, i) => (
 <div
 key={s}
 style={{
 flex: 1,
 height: 6,
 background: i <= stepIdx ? "var(--ink)" : "transparent",
 border: "1px solid var(--ink)",
 }}
 />
 ))}
 </div>
 <div
 style={{
 fontFamily: '"JetBrains Mono", ui-monospace, monospace',
 fontSize: 11.5,
 color: "var(--ink-soft)",
 marginTop: 4,
 letterSpacing: ".04em",
 }}
 >
 stage: {STAGES[stepIdx]} · {Math.round(progress)}%
 </div>
 </div>
 </foreignObject>
 );
}

export function CityDeskPlant({
 leaves,
 activeKey,
 calloutTitle = "Growing item",
 calloutSub = "( routing approvals )",
 onLeafClick,
 hideCallout = false,
}: CityDeskPlantProps) {
 // Assign incoming entries to the plant's slots in sketch order.
 const placed = useMemo(
 () =>
 leaves.slice(0, SLOT_ORDER.length).map((entry, i) => ({
 ...entry,
 slot: SLOT_ORDER[i],
 isActive: entry.key === activeKey,
 })),
 [leaves, activeKey],
 );

 const sorted = useMemo(
 () => [...placed].sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status]),
 [placed],
 );

 const building =
 placed.find((l) => l.status === "building") ??
 (activeKey ? placed.find((l) => l.key === activeKey) : null);

 return (
 <svg
 viewBox={`0 0 ${VIEWBOX_W} ${VIEWBOX_H}`}
 xmlns="http://www.w3.org/2000/svg"
 className="h-full w-full"
 role="img"
 aria-label="Meeting growth — each agenda item is a leaf that blooms as its approvals complete; the crown bud blooms into the official minutes at adjournment"
 >
 <defs>
 {SLOT_ORDER.map((slot) => (
 <clipPath id={`cdclip-${slot}`} key={`cdclip-${slot}`}>
 <path d={GEOMETRY[slot].leaf} />
 </clipPath>
 ))}
 </defs>

 {/* soil tick + stalk bundle — verbatim */}
 <path
 d="M 280 905 C 340 901, 420 901, 480 905"
 stroke="var(--ink)"
 strokeWidth="1"
 fill="none"
 strokeLinecap="round"
 />
 <path
 d="M 380 905 C 380 880, 380 855, 380 825"
 stroke="var(--ink)"
 strokeWidth="1.5"
 fill="none"
 strokeLinecap="round"
 />
 <path
 d="M 384 905 C 386 882, 384 858, 382 828"
 stroke="var(--ink)"
 strokeWidth="1"
 strokeOpacity="0.6"
 fill="none"
 strokeLinecap="round"
 />

 {sorted.map((l) => (
 <Leaf
 key={l.key}
 slot={l.slot}
 status={l.status}
 progress={l.progress}
 isActive={l.isActive}
 onClick={onLeafClick ? () => onLeafClick(l.key) : undefined}
 />
 ))}

 {!hideCallout && building && (
 <CalloutLine anchor={GEOMETRY[building.slot].mid} />
 )}
 {!hideCallout && building && (
 <CalloutBox
 title={calloutTitle}
 sub={calloutSub}
 label={building.label}
 subs={building.subs}
 progress={building.progress}
 anchor={GEOMETRY[building.slot].mid}
 />
 )}
 </svg>
 );
}

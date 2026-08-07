import { useState } from "react";
import type { Rect } from "@/components/hq/hqHelpers";

/**
 * Subway entrance — GTA-IV Easton-Station side-view redesign, 2026-07-02
 * (operator reference photos).
 *
 * History: v1 (pre-2026-06-02) was an overbuilt awning-billboard. v2
 * (2026-06-02) went minimal but drew the pit FRONT-ON — straight-down
 * stairs between rails — which at building scale read as a dark slatted
 * loading-dock bumper, not a subway. Operator 2026-07-02, with the GTA-IV
 * Easton Station reference: the element should read as a SIDE-VIEW
 * descent — "descending into subway station element, side-view vibe."
 *
 * v3 draws the cross-section: sidewalk band at street level, iron picket
 * railing above it, a pixel-step staircase descending left→right through
 * the cut-away earth into a tunnel mouth, with a faint warm platform
 * glow deep inside (there's a station down there). A globe lamp marks
 * the street corner, GTA/NYC style. The Z roundel sign hangs over the
 * descent on two posts.
 *
 * Placement job (unchanged since 2026-06-02): covers the V1 painted
 * plaza pipe junction along the right ground floor — the pipe elbows
 * down behind this element's left edge and a second run tucks behind
 * its right end; the solid earth cross-section hides both.
 *
 * Architecture stays: clicking fires `onLeave` (parent shows the
 * leaving-site modal before opening the maintainer's GitHub).
 */
export default function SvgSubwayEntrance({
 rect,
 onLeave,
}: {
 rect: Rect;
 onLeave: () => void;
}) {
 const [hovered, setHovered] = useState(false);

 // Staircase geometry — 9 pixel steps descending left→right from the
 // sidewalk cut to the tunnel mouth. Deterministic; drawn as real
 // treads + risers so the descent reads at building scale.
 const STEPS = 9;
 const stepX0 = 22;
 const stepY0 = 38;
 const stepW = 14.5;
 const stepH = 5.6;

 return (
 <div
 className={`svg-subway ${hovered ? "is-hover" : ""}`}
 style={{
 position: "absolute",
 top: `${rect.top}%`,
 left: `${rect.left}%`,
 width: `${rect.width}%`,
 height: `${rect.height}%`,
 zIndex: 16,
 cursor: "pointer",
 }}
 onClick={onLeave}
 onMouseEnter={() => setHovered(true)}
 onMouseLeave={() => setHovered(false)}
 onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onLeave()}
 role="button"
 tabIndex={0}
 aria-label="Leave Z-SPAN — exit to the maintainer's GitHub"
 >
 <svg viewBox="0 0 200 100" preserveAspectRatio="none">
 <defs>
 {/* Below-grade earth — darkens with depth. */}
 <linearGradient id="subway-earth-v4" x1="0" y1="0" x2="0" y2="1">
 <stop offset="0" stopColor="#262a38" />
 <stop offset="1" stopColor="#191c26" />
 </linearGradient>
 {/* Open shaft above the stairs — dusk light falls in from the
 * street, fading toward the tunnel end. */}
 <linearGradient id="subway-shaft-v4" x1="0" y1="0" x2="1" y2="0">
 <stop offset="0" stopColor="#1c2333" />
 <stop offset="0.7" stopColor="#0d1220" />
 <stop offset="1" stopColor="#05070d" />
 </linearGradient>
 {/* Tunnel mouth — near-black with the faintest floor bounce. */}
 <linearGradient id="subway-tunnel-v4" x1="0" y1="0" x2="0" y2="1">
 <stop offset="0" stopColor="#04060b" />
 <stop offset="1" stopColor="#0a0d16" />
 </linearGradient>
 <radialGradient id="subway-globe-v4" cx="0.35" cy="0.3" r="1">
 <stop offset="0" stopColor="#fff7e8" />
 <stop offset="0.55" stopColor="#ffe9bd" />
 <stop offset="1" stopColor="#d9b46a" />
 </radialGradient>
 </defs>

 {/* ============ BELOW-GRADE CROSS-SECTION ============ */}
 {/* Solid earth block — this is what hides the painted pipe
 * junction behind the element. */}
 <rect x="0" y="32" width="200" height="68" fill="url(#subway-earth-v4)" />
 {/* Foundation texture — sparse mortar lines, pixel-idiom. */}
 {[46, 62, 78, 92].map((y) => (
 <rect key={`m-${y}`} x="0" y={y} width="200" height="0.8" fill="rgba(0,0,0,0.28)" />
 ))}
 {[24, 88, 152].map((x, i) => (
 <rect key={`mv-${x}`} x={x} y={i % 2 ? 62 : 46} width="0.8" height="16" fill="rgba(0,0,0,0.22)" />
 ))}

 {/* Stairwell cavity — the open shaft the stairs descend through.
 * Top edge is the sidewalk cut; bottom follows the stair diagonal. */}
 <polygon
 points={`${stepX0 - 4},34 196,34 196,${stepY0 + STEPS * stepH + 6} ${stepX0 - 4},${stepY0 + 2}`}
 fill="url(#subway-shaft-v4)"
 />

 {/* Tunnel mouth at the deep end — where the stairs arrive. */}
 <rect
 x={stepX0 + STEPS * stepW - 8}
 y={stepY0 + (STEPS - 4) * stepH}
 width="46"
 height={4.5 * stepH + 8}
 fill="url(#subway-tunnel-v4)"
 />
 {/* The platform light deep inside — one warm pixel; there's a
 * station down there. */}
 <rect
 x={stepX0 + STEPS * stepW + 26}
 y={stepY0 + STEPS * stepH - 3}
 width="5"
 height="1.6"
 fill="rgba(255, 214, 140, 0.85)"
 />
 <rect
 x={stepX0 + STEPS * stepW + 22}
 y={stepY0 + STEPS * stepH - 1}
 width="13"
 height="4"
 fill="rgba(255, 200, 120, 0.10)"
 />

 {/* Staircase — real treads + risers, lit from the street above:
 * bright at the mouth, dimming with depth. */}
 {Array.from({ length: STEPS }, (_, i) => {
 const x = stepX0 + i * stepW;
 const y = stepY0 + i * stepH;
 const lum = 1 - i / (STEPS + 2);
 return (
 <g key={`step-${i}`}>
 {/* Step body (the earth under each tread) */}
 <polygon
 points={`${x},${y} ${x + stepW},${y} ${x + stepW},${y + stepH} ${x},${y + stepH}`}
 fill="#141824"
 />
 {/* Tread top — the lit edge */}
 <rect
 x={x}
 y={y}
 width={stepW + 0.5}
 height="1.4"
 fill={`rgba(158, 173, 205, ${0.55 * lum + 0.08})`}
 />
 {/* Riser face — dimmer vertical */}
 <rect
 x={x + stepW - 0.9}
 y={y}
 width="0.9"
 height={stepH + 1.2}
 fill={`rgba(90, 100, 128, ${0.4 * lum + 0.05})`}
 />
 </g>
 );
 })}

 {/* ============ STREET LEVEL ============ */}
 {/* Sidewalk band — pavement with a dusk-lit top edge. The cut for
 * the stairwell interrupts it. */}
 <rect x="0" y="26" width={stepX0 - 4} height="8" fill="#343b4e" />
 <rect x={stepX0 - 4} y="26" width={200 - stepX0 + 4} height="8" fill="#343b4e" />
 <rect x="0" y="26" width="200" height="1.2" fill="#5a6478" />
 {/* Curb shadow under the sidewalk lip at the stair mouth */}
 <rect x={stepX0 - 4} y="33" width="6" height="2.4" fill="rgba(0,0,0,0.5)" />

 {/* ============ IRON PICKET RAILING ============ */}
 {/* GTA/NYC black iron: top rail + mid rail + dense pickets, running
 * the length of the stairwell at street level. Near-black with a
 * steel top highlight so it reads against the dusk. */}
 <rect x="12" y="12.5" width="174" height="2.2" fill="#232837" />
 <rect x="12" y="12.5" width="174" height="0.6" fill="#6a7691" />
 <rect x="12" y="20" width="174" height="1.3" fill="#232837" />
 {Array.from({ length: 22 }, (_, i) => {
 const x = 14 + i * 8;
 return (
 <rect key={`p-${i}`} x={x} y="13" width="1.1" height="14" fill="#1e2331" />
 );
 })}
 {/* End posts — slightly heavier, with finial caps. */}
 {[12, 184].map((x) => (
 <g key={`post-${x}`}>
 <rect x={x} y="10.5" width="2.4" height="16.5" fill="#171a24" />
 <rect x={x - 0.4} y="9.6" width="3.2" height="1.6" rx="0.6" fill="#20242f" />
 </g>
 ))}

 {/* ============ GLOBE LAMP (street corner) ============ */}
 <g transform="translate(5, 0)">
 <rect x="-0.9" y="6" width="1.9" height="21" fill="#10131c" />
 <rect x="-1.6" y="25.5" width="3.4" height="1.6" fill="#171a24" />
 <circle cx="0" cy="4.2" r="4.2" fill="url(#subway-globe-v4)" />
 <circle cx="0" cy="4.2" r="6.6" fill="rgba(255, 226, 160, 0.16)" />
 </g>

 {/* ============ STATION SIGN over the descent ============ */}
 {/* Hangs on two thin posts above the railing — the Z roundel +
 * wordmark, kept small like a real station sign. */}
 <g transform="translate(84, -1) scale(1.55)">
 <rect x="4" y="7.5" width="1" height="5" fill="#10131c" />
 <rect x="39" y="7.5" width="1" height="5" fill="#10131c" />
 <rect x="0" y="-2" width="44" height="10" rx="1" fill="#0a0e18" stroke="#1a2336" strokeWidth="0.5" />
 <circle cx="6" cy="3" r="3.4" fill="#8be9fd" />
 <text
 x="6" y="4.6"
 textAnchor="middle"
 fontSize="4.4"
 fill="#000"
 fontFamily="monospace"
 fontWeight="bold"
 >
 Z
 </text>
 <text
 x="11.5" y="2.6"
 fontSize="3.4"
 fill="rgba(220, 230, 250, 0.92)"
 fontFamily="monospace"
 letterSpacing="1.0"
 fontWeight="bold"
 >
 Z-SPAN
 </text>
 <text
 x="11.5" y="6.6"
 fontSize="2.2"
 fill="rgba(170, 195, 225, 0.7)"
 fontFamily="monospace"
 letterSpacing="0.8"
 >
 ARCHIVES
 </text>
 </g>

 {/* Subtle hover glow ring around the entire structure */}
 <rect
 x="2"
 y="2"
 width="196"
 height="96"
 rx="1.5"
 fill="none"
 stroke="rgba(139, 233, 253, 0.0)"
 strokeWidth="0.6"
 className="subway-hover-ring"
 />
 </svg>

 {/* Hover hint label below the entrance. */}
 <div className="svg-subway-hint" aria-hidden>
 Click to leave site &rarr;
 </div>
 </div>
 );
}

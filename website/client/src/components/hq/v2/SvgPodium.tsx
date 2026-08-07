import type { Rect } from "@/components/hq/hqHelpers";

// Podium + speaker — pass 3 (2026-06-02). James reference: V1 painted
// pixel-art with clean simple shapes — BROWN wood podium box, speaker
// with round skin-tone head + small hair band ON TOP + dark suit with
// white shirt V + thin maroon tie. Earlier passes over-detailed.
// This pass leans on simple rectangles + circles, no over-shaping.
export default function SvgPodium({ rect }: { rect: Rect }) {
 return (
 <div
 className="svg-podium"
 style={{
 position: "absolute",
 top: `${rect.top}%`,
 left: `${rect.left}%`,
 width: `${rect.width}%`,
 height: `${rect.height}%`,
 zIndex: 14,
 }}
 aria-hidden
 >
 <svg viewBox="0 0 100 100" preserveAspectRatio="none" shapeRendering="crispEdges">
 {/* === SPEAKER === */}
 {/* Suit body — broad shoulders, rectangular torso. */}
 <polygon points="28,30 72,30 70,55 30,55" fill="#0e1326" />
 {/* White shirt collar — single triangular V. */}
 <polygon points="44,30 50,38 56,30" fill="#cdd1dc" />
 {/* Maroon tie — thin vertical band. */}
 <rect x="48" y="30" width="4" height="14" fill="#7a2a2a" />
 {/* Head — round skin-tone ellipse. */}
 <ellipse cx="50" cy="20" rx="9" ry="10" fill="#c89878" />
 {/* Hair — single dark band on TOP of head only (no face coverage). */}
 <path
 d="M 41 16 Q 41 10 50 9 Q 59 10 59 16 Q 50 14 41 16 Z"
 fill="#1a0e06"
 />
 {/* Subtle eye dots — minimal pixel-art facial features. */}
 <rect x="46" y="20" width="1.6" height="1.6" fill="#1a0e06" />
 <rect x="52.4" y="20" width="1.6" height="1.6" fill="#1a0e06" />

 {/* === PODIUM (brown wood box) === */}
 {/* Lectern top — thin horizontal strip, wider than body. */}
 <rect x="18" y="54" width="64" height="3" fill="#2c1a0c" />
 <rect x="18" y="57" width="64" height="1.5" fill="#1a0e06" />
 {/* Body — brown wood box. */}
 <rect x="22" y="58.5" width="56" height="40" fill="#5a3a22" />
 {/* Front panel — slightly darker recessed face. */}
 <rect x="26" y="62" width="48" height="33" fill="#48301c" />
 {/* Floor shadow under podium. */}
 <rect x="22" y="98.5" width="56" height="1.5" fill="#1a100a" />

 {/* === MIC === */}
 {/* Thin vertical stem rising from lectern. */}
 <rect x="49" y="40" width="2" height="14" fill="#0a0a14" />
 {/* Mic capsule — small dark oval at top of stem. */}
 <ellipse cx="50" cy="38" rx="2.5" ry="3" fill="#0a0a14" />
 </svg>
 </div>
 );
}

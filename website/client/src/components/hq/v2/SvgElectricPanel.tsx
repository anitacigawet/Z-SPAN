import type { Rect } from "@/components/hq/hqHelpers";

// V2 SVG power substation — replaces the V1 painted A/C generator
// outbuilding (stripped by rembg_postprocess.py at expanded region).
// James 2026-06-02 corrected the intent: not a small electric box,
// but a small **power substation** with truss towers, insulator
// strings, wire spans, transformers, pipes, and a concrete pad.
//
// The five InfraPanel LEDs mount above the front breaker bank as the
// live service-status indicators.
export default function SvgElectricPanel({ rect }: { rect: Rect }) {
 return (
 <div
 className="svg-electric-panel"
 style={{
 position: "absolute",
 top: `${rect.top}%`,
 left: `${rect.left}%`,
 width: `${rect.width}%`,
 height: `${rect.height}%`,
 zIndex: 8,
 }}
 aria-hidden
 >
 <svg viewBox="0 0 100 100" preserveAspectRatio="none">
 <defs>
 <linearGradient id="sub-steel" x1="0" y1="0" x2="0" y2="1">
 <stop offset="0" stopColor="#3a4254" />
 <stop offset="0.5" stopColor="#2a3142" />
 <stop offset="1" stopColor="#1a2030" />
 </linearGradient>
 <linearGradient id="sub-transformer" x1="0" y1="0" x2="0" y2="1">
 <stop offset="0" stopColor="#1f2638" />
 <stop offset="1" stopColor="#0e1322" />
 </linearGradient>
 <linearGradient id="sub-pad" x1="0" y1="0" x2="0" y2="1">
 <stop offset="0" stopColor="#1a1f2d" />
 <stop offset="1" stopColor="#0a0e18" />
 </linearGradient>
 </defs>

 {/* ============ WIRE SPANS BETWEEN TOWERS (back) ============ */}
 {/* Two slack-curved high-voltage lines spanning all three towers.
 * Catenary approximated with quadratic curves. */}
 <path
 d="M 14,16 Q 50,22 86,18"
 fill="none"
 stroke="#5a6275"
 strokeWidth="0.5"
 />
 <path
 d="M 14,18 Q 50,24.5 86,20"
 fill="none"
 stroke="#5a6275"
 strokeWidth="0.5"
 />

 {/* Wires going UP off-screen from each tower top — implies
 * connection to the rest of the grid. */}
 <line x1="13" y1="14" x2="11" y2="0" stroke="#5a6275" strokeWidth="0.5" />
 <line x1="46" y1="9" x2="46" y2="0" stroke="#5a6275" strokeWidth="0.5" />
 <line x1="48" y1="9" x2="48" y2="0" stroke="#5a6275" strokeWidth="0.5" />
 <line x1="86" y1="14" x2="88" y2="0" stroke="#5a6275" strokeWidth="0.5" />

 {/* ============ TOWER 1 (LEFT) — truss with transformer ============ */}
 {/* Left vertical leg */}
 <rect x="6" y="14" width="1.4" height="52" fill="url(#sub-steel)" />
 {/* Right vertical leg */}
 <rect x="20" y="14" width="1.4" height="52" fill="url(#sub-steel)" />
 {/* Cross-brace top cap */}
 <rect x="5" y="13" width="17" height="1.4" fill="url(#sub-steel)" />
 {/* X-bracing (lattice) */}
 {[18, 26, 34, 42, 50, 58].map((y) => (
 <g key={`t1-${y}`}>
 <line x1="7" y1={y} x2="20" y2={y + 7} stroke="#4a5266" strokeWidth="0.4" />
 <line x1="20" y1={y} x2="7" y2={y + 7} stroke="#4a5266" strokeWidth="0.4" />
 <line x1="7" y1={y + 3.5} x2="20" y2={y + 3.5} stroke="#4a5266" strokeWidth="0.4" />
 </g>
 ))}
 {/* Insulator strings hanging from top cap */}
 <line x1="10" y1="14" x2="10" y2="22" stroke="#3a4254" strokeWidth="0.5" />
 {[15, 17, 19, 21].map((y) => (
 <circle key={`t1i-${y}`} cx="10" cy={y} r="0.9" fill="#3a4254" stroke="#1a2030" strokeWidth="0.15" />
 ))}

 {/* Transformer at base of tower 1 */}
 <rect x="2" y="66" width="22" height="16" rx="0.6" fill="url(#sub-transformer)" stroke="#070a14" strokeWidth="0.7" />
 {/* Transformer top connections (3 bushings) */}
 {[6, 13, 20].map((x) => (
 <g key={`tx-${x}`}>
 <rect x={x - 0.7} y="62" width="1.4" height="4" fill="#1a2030" stroke="#070a14" strokeWidth="0.3" />
 <circle cx={x} cy="62" r="1" fill="#2a3142" stroke="#070a14" strokeWidth="0.2" />
 </g>
 ))}
 {/* Fins on transformer side */}
 {[68, 70, 72, 74, 76, 78, 80].map((y) => (
 <line key={`fin-${y}`} x1="24" y1={y} x2="25" y2={y} stroke="#3a4254" strokeWidth="0.3" />
 ))}
 {/* HV label */}
 <text x="13" y="76" textAnchor="middle" fontSize="2.6" fill="rgba(255,140,140,0.7)" fontFamily="monospace" letterSpacing="0.5" fontWeight="bold">
 HIGH VOLTAGE
 </text>

 {/* ============ TOWER 2 (CENTER) — taller, with insulator strings ============ */}
 <rect x="40" y="9" width="1.4" height="55" fill="url(#sub-steel)" />
 <rect x="53" y="9" width="1.4" height="55" fill="url(#sub-steel)" />
 <rect x="39" y="8" width="16" height="1.4" fill="url(#sub-steel)" />
 {[12, 20, 28, 36, 44, 52].map((y) => (
 <g key={`t2-${y}`}>
 <line x1="41" y1={y} x2="53" y2={y + 7} stroke="#4a5266" strokeWidth="0.4" />
 <line x1="53" y1={y} x2="41" y2={y + 7} stroke="#4a5266" strokeWidth="0.4" />
 <line x1="41" y1={y + 3.5} x2="53" y2={y + 3.5} stroke="#4a5266" strokeWidth="0.4" />
 </g>
 ))}
 {/* Insulator strings hanging from top cap */}
 {[44, 47, 50].map((x) => (
 <g key={`t2i-${x}`}>
 <line x1={x} y1="9" x2={x} y2="22" stroke="#3a4254" strokeWidth="0.5" />
 {[12, 14, 16, 18, 20].map((y) => (
 <circle key={`t2is-${x}-${y}`} cx={x} cy={y} r="0.7" fill="#3a4254" stroke="#1a2030" strokeWidth="0.12" />
 ))}
 </g>
 ))}

 {/* ============ TOWER 3 (RIGHT) — switches at base ============ */}
 <rect x="78" y="12" width="1.4" height="54" fill="url(#sub-steel)" />
 <rect x="91" y="12" width="1.4" height="54" fill="url(#sub-steel)" />
 <rect x="77" y="11" width="16" height="1.4" fill="url(#sub-steel)" />
 {[15, 23, 31, 39, 47, 55].map((y) => (
 <g key={`t3-${y}`}>
 <line x1="79" y1={y} x2="91" y2={y + 7} stroke="#4a5266" strokeWidth="0.4" />
 <line x1="91" y1={y} x2="79" y2={y + 7} stroke="#4a5266" strokeWidth="0.4" />
 <line x1="79" y1={y + 3.5} x2="91" y2={y + 3.5} stroke="#4a5266" strokeWidth="0.4" />
 </g>
 ))}
 {/* Insulator string */}
 <line x1="85" y1="12" x2="85" y2="22" stroke="#3a4254" strokeWidth="0.5" />
 {[14, 16, 18, 20].map((y) => (
 <circle key={`t3i-${y}`} cx="85" cy={y} r="0.7" fill="#3a4254" stroke="#1a2030" strokeWidth="0.12" />
 ))}

 {/* ============ CENTER PIPES + VALVE ============ */}
 {/* Vertical pipes */}
 <rect x="28" y="60" width="2.4" height="25" fill="#3a4254" stroke="#0e1322" strokeWidth="0.3" />
 <rect x="32" y="62" width="2.4" height="23" fill="#3a4254" stroke="#0e1322" strokeWidth="0.3" />
 {/* Horizontal pipe top connection */}
 <rect x="28" y="60" width="6.4" height="2.4" fill="#3a4254" stroke="#0e1322" strokeWidth="0.3" />
 {/* Valve handle (round, red) */}
 <circle cx="33" cy="58" r="2.2" fill="#7a1e1e" stroke="#0e1322" strokeWidth="0.35" />
 <line x1="31" y1="58" x2="35" y2="58" stroke="#cdd6f4" strokeWidth="0.3" />
 <line x1="33" y1="56" x2="33" y2="60" stroke="#cdd6f4" strokeWidth="0.3" />

 {/* Control / switch box on the right side at base */}
 <rect x="58" y="64" width="34" height="18" rx="0.5" fill="#1a2030" stroke="#0a0e18" strokeWidth="0.6" />
 {/* Inner recessed panel */}
 <rect x="60" y="66" width="30" height="14" fill="#10162a" stroke="#070a14" strokeWidth="0.4" />

 {/* Breaker switches in a row (5 of them — one per service) */}
 {[0, 1, 2, 3, 4].map((i) => (
 <g key={`br-${i}`} transform={`translate(${62 + i * 6}, 70)`}>
 <rect width="4" height="9" rx="0.3" fill="#1a2030" stroke="#070a14" strokeWidth="0.3" />
 {/* Switch lever (engaged position) */}
 <rect x="1.3" y="1.8" width="1.4" height="3.3" fill="#3a4254" stroke="#0a0e18" strokeWidth="0.2" />
 {/* ON label */}
 <text x="2" y="7.6" textAnchor="middle" fontSize="1.3" fill="rgba(139,233,253,0.55)" fontFamily="monospace">
 ON
 </text>
 </g>
 ))}

 {/* ============ CONCRETE PAD WITH DRAIN GRATE ============ */}
 <rect x="0" y="85" width="100" height="9" fill="url(#sub-pad)" stroke="#070a14" strokeWidth="0.4" />
 {/* Drain grate at front-right */}
 <rect x="78" y="87.5" width="14" height="4.5" fill="#0a0e18" stroke="#000" strokeWidth="0.3" />
 {[80, 82, 84, 86, 88, 90].map((x) => (
 <line key={`gr-${x}`} x1={x} y1="87.5" x2={x} y2="92" stroke="#2a3142" strokeWidth="0.3" />
 ))}
 {/* Pad seams */}
 <line x1="0" y1="88" x2="100" y2="88" stroke="#070a14" strokeWidth="0.3" />
 <line x1="30" y1="85" x2="30" y2="94" stroke="#070a14" strokeWidth="0.2" />
 <line x1="60" y1="85" x2="60" y2="94" stroke="#070a14" strokeWidth="0.2" />

 {/* Z-SPAN UTILS faceplate on the control box */}
 <text x="75" y="84" textAnchor="middle" fontSize="2.2" fill="rgba(139, 233, 253, 0.4)" fontFamily="monospace" letterSpacing="0.9">
 Z-SPAN UTILS
 </text>

 {/* Ground shadow */}
 <ellipse cx="50" cy="96" rx="46" ry="1.1" fill="rgba(0,0,0,0.45)" />
 </svg>
 </div>
 );
}

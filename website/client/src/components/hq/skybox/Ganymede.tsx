import { useState } from "react";

/**
 * Ganymede — the moon in the skybox's top-right space region.
 *
 * The operator's painted moon is used when available, with a procedural
 * gradient-and-crater moon as its fallback. The moon belongs to the sky and
 * scrolls with the page.
 */

const MOON_CX = 40;
const MOON_CY = 50;

export default function Ganymede() {
 // Operator moon-art drop-in (2026-07-02): if /hq/ganymede.png exists
 // (the operator's painted moon, black background removed), it replaces
 // the procedural circle+craters below. No file → onError flips this
 // flag and the code-drawn moon renders as always. Bump the ?v= stamp
 // when the art changes (asset URLs are cached hard).
 const [moonImgOk, setMoonImgOk] = useState(true);

 return (
 <div
 className="ganymede"
 aria-label="Ganymede moon"
 aria-hidden="false"
 >
 <svg
 viewBox="0 0 100 100"
 preserveAspectRatio="xMidYMid meet"
 className="ganymede-svg"
 role="img"
 >
 <defs>
 {/* Cool-gray realistic moon shading per photo-1 reference. */}
 <radialGradient id="ganymede-moon-shade" cx="32%" cy="32%" r="85%">
 <stop offset="0%" stopColor="#dfe1e0" />
 <stop offset="35%" stopColor="#9ea0a0" />
 <stop offset="70%" stopColor="#54565a" />
 <stop offset="100%" stopColor="#1f2024" />
 </radialGradient>
 <radialGradient id="ganymede-moon-glow" cx="50%" cy="50%" r="50%">
 <stop offset="0%" stopColor="rgba(230, 235, 245, 0.26)" />
 <stop offset="50%" stopColor="rgba(220, 230, 240, 0.10)" />
 <stop offset="100%" stopColor="rgba(220, 230, 240, 0)" />
 </radialGradient>
 </defs>

 {/* Moon glow (rendered as a larger soft circle behind the moon). */}
 <circle
 cx={MOON_CX}
 cy={MOON_CY}
 r="18"
 fill="url(#ganymede-moon-glow)"
 />

 {/* The moon — the operator's painted art when the drop-in asset
 * exists; the procedural gradient+craters otherwise. */}
 {moonImgOk ? (
 <image
 href="/hq/ganymede.png?v=3"
 x={MOON_CX - 10.5}
 y={MOON_CY - 10.5}
 width="21"
 height="21"
 preserveAspectRatio="xMidYMid meet"
 className="ganymede-moon-body"
 onError={() => setMoonImgOk(false)}
 />
 ) : (
 <>
 <circle
 cx={MOON_CX}
 cy={MOON_CY}
 r="9"
 fill="url(#ganymede-moon-shade)"
 className="ganymede-moon-body"
 />
 {/* Craters — small darker patches. Cool gray-blue tones. */}
 <ellipse cx={MOON_CX - 2.5} cy={MOON_CY - 1.5} rx="0.9" ry="0.7" fill="rgba(30, 32, 38, 0.45)" />
 <circle cx={MOON_CX + 1.8} cy={MOON_CY - 3.0} r="0.55" fill="rgba(35, 37, 42, 0.42)" />
 <ellipse cx={MOON_CX - 1.0} cy={MOON_CY + 2.2} rx="1.1" ry="0.9" fill="rgba(20, 22, 28, 0.55)" />
 <circle cx={MOON_CX + 3.2} cy={MOON_CY + 1.0} r="0.45" fill="rgba(28, 30, 36, 0.50)" />
 <circle cx={MOON_CX - 3.2} cy={MOON_CY + 3.2} r="0.40" fill="rgba(35, 38, 44, 0.45)" />
 <ellipse cx={MOON_CX + 0.5} cy={MOON_CY + 4.0} rx="0.7" ry="0.5" fill="rgba(20, 22, 28, 0.50)" />
 <circle cx={MOON_CX - 4.2} cy={MOON_CY + 0.5} r="0.35" fill="rgba(35, 38, 44, 0.42)" />
 <circle cx={MOON_CX + 4.2} cy={MOON_CY - 1.5} r="0.40" fill="rgba(30, 32, 38, 0.42)" />
 <circle cx={MOON_CX - 0.5} cy={MOON_CY - 3.8} r="0.30" fill="rgba(40, 42, 48, 0.35)" />
 <circle cx={MOON_CX + 2.8} cy={MOON_CY + 3.5} r="0.35" fill="rgba(30, 32, 38, 0.45)" />
 </>
 )}
 </svg>
 </div>
 );
}

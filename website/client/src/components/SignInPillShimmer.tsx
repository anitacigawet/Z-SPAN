/**
 * SignInPillShimmer — one-lap "pixie dust" outline trace around the
 * Sign-in-with-Google pill, fired once when SignInBenefitsToast first
 * appears.
 *
 * Aesthetic reference (per James 2026-06-24): the old Disney-channel
 * commercials where a wand draws a glowing outline around the subject.
 * Purple→blue gradient, one clockwise lap, then unmounts itself. Sits
 * in the same intentional-detail family as the lightbulb animation +
 * the Hawthorne-effect cues.
 *
 * Mechanics:
 * - Fixed-position overlay sized to the pill's getBoundingClientRect()
 * - SVG rounded-rect path matches the pill's border-radius
 * - The path stroke "draws on" via stroke-dashoffset
 * - A glowing head particle traces the path (CSS offset-path)
 * - Three trailing sparkles fade in at delayed phase offsets
 * - One animation cycle (~1.6s) then onComplete fires
 *
 * Pointer-events: none on the whole overlay so the pill itself stays
 * clickable during the shimmer (a viewer who clicks fast doesn't get
 * intercepted by the decoration).
 *
 * Browser compatibility (named 2026-06-24 brainstorm-audit): uses
 * `offset-path: path(...)` CSS, supported in Chrome/Edge/Firefox + Safari
 * 16.4+ (March 2023). Safari 15 and earlier render the path stroke
 * statically without the animated head — degrades to "outline glows but
 * doesn't trace." Acceptable degradation; users on legacy Safari are
 * a small slice + the static glow still reads as a visual flourish.
 *
 * CSS persistence note (named 2026-06-24 brainstorm-audit): the keyframes
 * stylesheet is appended to document.head once via the styleInjected
 * guard and never removed. Class-scoped so harmless across page
 * navigation; intentional trade-off vs. the complexity of a teardown
 * mechanism that'd need component-instance ref-counting.
 */
import { useEffect, useState } from "react";

interface SignInPillShimmerProps {
 /** The pill's bounding rect in viewport coordinates. */
 rect: DOMRect;
 /** Called when the one-lap animation completes (parent unmounts us). */
 onComplete: () => void;
}

const LAP_MS = 1600;
const PADDING = 8; // extra space around the pill for the trace to breathe

export function SignInPillShimmer({ rect, onComplete }: SignInPillShimmerProps) {
 const [styleInjected, setStyleInjected] = useState(false);

 // Inject the keyframes once into <head>. Tailwind doesn't cover
 // dynamic keyframes + offset-path cleanly, so plain CSS at module
 // level is the path of least friction.
 useEffect(() => {
 const id = "zspan-signin-shimmer-styles";
 if (document.getElementById(id)) {
 setStyleInjected(true);
 return;
 }
 const styleEl = document.createElement("style");
 styleEl.id = id;
 styleEl.textContent = SHIMMER_CSS;
 document.head.appendChild(styleEl);
 setStyleInjected(true);
 }, []);

 // Unmount after the one lap finishes.
 useEffect(() => {
 const t = window.setTimeout(onComplete, LAP_MS + 80);
 return () => window.clearTimeout(t);
 }, [onComplete]);

 if (!styleInjected) return null;

 // The overlay covers the pill plus padding so the trace circles
 // around the OUTSIDE of the border, not on top of the text.
 const left = rect.left - PADDING;
 const top = rect.top - PADDING;
 const width = rect.width + PADDING * 2;
 const height = rect.height + PADDING * 2;

 // SVG path = rounded rectangle matching the pill (border-radius ~999
 // → full pill, so we use width/2 effectively, but here we use the
 // shorter dimension to ensure the corners stay correct).
 const rx = Math.min(width, height) / 2;
 // Path string — clockwise rounded rectangle starting at top-center.
 // Using arcs at each corner. Path data is a continuous loop so
 // offset-path mathematics is clean.
 const pathD = roundedRectPath(width, height, rx);

 return (
 <div
 aria-hidden="true"
 className="zspan-signin-shimmer-overlay"
 style={{
 position: "fixed",
 left,
 top,
 width,
 height,
 pointerEvents: "none",
 zIndex: 71, // above the toast (z-70), below modal layers (z-90+)
 }}
 >
 <svg
 width={width}
 height={height}
 viewBox={`0 0 ${width} ${height}`}
 style={{ overflow: "visible" }}
 >
 <defs>
 <linearGradient id="zspan-shimmer-stroke" x1="0%" y1="0%" x2="100%" y2="100%">
 <stop offset="0%" stopColor="#b8a4ff" stopOpacity="0.9" />
 <stop offset="50%" stopColor="#8b9eff" stopOpacity="0.95" />
 <stop offset="100%" stopColor="#c9b6ff" stopOpacity="0.85" />
 </linearGradient>
 <radialGradient id="zspan-shimmer-head" cx="50%" cy="50%" r="50%">
 <stop offset="0%" stopColor="#fff7d6" stopOpacity="1" />
 <stop offset="35%" stopColor="#d9c1ff" stopOpacity="0.95" />
 <stop offset="100%" stopColor="#7d8eff" stopOpacity="0" />
 </radialGradient>
 <filter id="zspan-shimmer-glow" x="-50%" y="-50%" width="200%" height="200%">
 <feGaussianBlur stdDeviation="2.5" result="blur" />
 <feMerge>
 <feMergeNode in="blur" />
 <feMergeNode in="SourceGraphic" />
 </feMerge>
 </filter>
 </defs>

 {/* The outline path that gets revealed by the trace. */}
 <path
 d={pathD}
 fill="none"
 stroke="url(#zspan-shimmer-stroke)"
 strokeWidth="1.2"
 strokeLinecap="round"
 filter="url(#zspan-shimmer-glow)"
 className="zspan-shimmer-trace"
 />

 {/* The head particle traces the path via offset-path. Three
 trailing sparkles fade in at delayed offsets so the visual
 reads as a comet tail. */}
 <g style={{ "--zspan-shimmer-path": `path('${pathD}')` } as any}>
 {/* Head — bright + warm */}
 <circle
 r="3.5"
 fill="url(#zspan-shimmer-head)"
 filter="url(#zspan-shimmer-glow)"
 className="zspan-shimmer-head"
 />
 {/* Tail sparkles — smaller + cooler */}
 <circle r="1.6" fill="#d4c4ff" filter="url(#zspan-shimmer-glow)" className="zspan-shimmer-spark zspan-shimmer-spark-1" />
 <circle r="1.2" fill="#a8b6ff" filter="url(#zspan-shimmer-glow)" className="zspan-shimmer-spark zspan-shimmer-spark-2" />
 <circle r="0.9" fill="#e0d4ff" filter="url(#zspan-shimmer-glow)" className="zspan-shimmer-spark zspan-shimmer-spark-3" />
 </g>
 </svg>
 </div>
 );
}

/** Build a clockwise rounded-rectangle path starting at top-center.
 * Continuous loop, no Z-close (so offset-path mathematics doesn't
 * hit the closing line as an extra step). */
function roundedRectPath(w: number, h: number, r: number): string {
 // Clamp r so we don't blow past geometric limits on tiny rects.
 const radius = Math.min(r, w / 2, h / 2);
 const right = w;
 const bottom = h;
 // Start at top-center of the top edge.
 // M startX,0 → H right-r → A r,r 0 0 1 right,r → V bottom-r
 // → A r,r 0 0 1 right-r,bottom → H r → A r,r 0 0 1 0,bottom-r
 // → V r → A r,r 0 0 1 r,0 → H startX
 return [
 `M ${radius} 0`,
 `H ${right - radius}`,
 `A ${radius} ${radius} 0 0 1 ${right} ${radius}`,
 `V ${bottom - radius}`,
 `A ${radius} ${radius} 0 0 1 ${right - radius} ${bottom}`,
 `H ${radius}`,
 `A ${radius} ${radius} 0 0 1 0 ${bottom - radius}`,
 `V ${radius}`,
 `A ${radius} ${radius} 0 0 1 ${radius} 0`,
 ].join(" ");
}

const SHIMMER_CSS = `
@keyframes zspan-shimmer-draw {
 0% { stroke-dashoffset: var(--zspan-shimmer-len); opacity: 0; }
 8% { opacity: 1; }
 82% { stroke-dashoffset: 0; opacity: 1; }
 100% { opacity: 0; stroke-dashoffset: 0; }
}
@keyframes zspan-shimmer-trace {
 0% { offset-distance: 0%; opacity: 0; }
 6% { opacity: 1; }
 85% { offset-distance: 100%; opacity: 1; }
 100% { offset-distance: 100%; opacity: 0; }
}
.zspan-shimmer-trace {
 /* Use a fairly long pathLength so the dash math approximates the
 real geometry without us measuring it. SVG dashoffset honors the
 declared pathLength when set. */
 stroke-dasharray: 600;
 --zspan-shimmer-len: 600;
 stroke-dashoffset: 600;
 animation: zspan-shimmer-draw ${LAP_MS}ms cubic-bezier(.45,.05,.25,1) forwards;
}
.zspan-shimmer-head,
.zspan-shimmer-spark {
 offset-path: var(--zspan-shimmer-path);
 offset-rotate: 0deg;
 animation: zspan-shimmer-trace ${LAP_MS}ms cubic-bezier(.45,.05,.25,1) forwards;
}
.zspan-shimmer-spark-1 { animation-delay: 80ms; opacity: 0.85; }
.zspan-shimmer-spark-2 { animation-delay: 160ms; opacity: 0.7; }
.zspan-shimmer-spark-3 { animation-delay: 240ms; opacity: 0.55; }
`;

/** Custom event the toast dispatches on mount; SignInPill listens. */
export const SIGNIN_PILL_SHIMMER_EVENT = "zspan-signin-pill-shimmer";

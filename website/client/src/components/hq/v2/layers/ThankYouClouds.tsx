import { useEffect } from "react";
import { useLayerVisibility } from "../LayerVisibility";

/**
 * V2-fog-2 realized (2026-07-02) — the "Thank you for visiting!" farewell sky.
 *
 * Lineage: the fog-band CSS carried a James-pinned note since 2026-06-02 —
 * "Photoreal cumulus upgrade via Gemini/Flow is pinned for soon (TASKS
 * V2-fog-2)". The operator ran the HQ sky through Gemini (2026-07-02,
 * soft-launch eve) and brought back a mock: the sky's cloud reshaped into
 * cloud-lettered "Thank you for visiting!", a red/white/blue long-exposure
 * meteor field arcing toward Ganymede, and a cluster of golden octagon
 * spirals where the trails converge. This layer builds that mock in SVG.
 *
 * What this layer deliberately does NOT do:
 * - Touch FogBand. The advection fog keeps hiding the seam and keeps
 * being the whole-band Settings click target. This layer is
 * pointer-events:none everywhere.
 * - Touch the StarField canvas. The live SSE shooting stars keep flying;
 * the static streaks here are a low-opacity "long-exposure memory"
 * UNDER the live trails' brightness, in the civic red/white/blue.
 * - Spawn randomness at runtime. All geometry is deterministic (fixed
 * params) so the sky composes identically every visit.
 *
 * Placement: absolutely positioned over the Skybox region (75vh), between
 * the Skybox and the FogBand in HQPageV2. Registered as the "thankyou"
 * LayerVisibility key so the operator can toggle it like every other V2
 * layer.
 */

const FONT_HREF =
 "https://fonts.googleapis.com/css2?family=Baloo+2:wght@700;800&display=swap";

/** Runtime font injection — same pattern as cityDeskTheme.usePaperFonts. */
function useCloudFont(): void {
 useEffect(() => {
 if (document.querySelector('link[data-hq-thankyou-font="1"]')) return;
 const link = document.createElement("link");
 link.rel = "stylesheet";
 link.href = FONT_HREF;
 link.setAttribute("data-hq-thankyou-font", "1");
 document.head.appendChild(link);
 }, []);
}

/* Session-32 (2026-07-04) — long-exposure streak field + golden octagon
 * spirals REMOVED per operator direction. They were the static "long-
 * exposure memory" layer + the amber octagon cluster around Ganymede.
 * Everything else in the sky (StarField SSE shooting stars, thank-you
 * text clouds, July-4 fireworks, moon) stays.
 * Removed:
 * - octagonSpiral() helper
 * - Streak type + RED/BLUE/WHITE color constants
 * - STREAKS array (14 hardcoded paths)
 * - SPIRALS array (5 hardcoded spiral centers)
 * - <g className="hq-ty-streaks"> render block
 * - <g className="hq-ty-spirals"> render block
 * See git history if you ever want them back. */

export default function ThankYouClouds() {
 useCloudFont();
 const { visibility } = useLayerVisibility();
 if (!visibility.thankyou) return null;

 return (
 <div className="hq-v2-thankyou" aria-hidden="true">
 <svg
 className="hq-v2-thankyou__svg"
 viewBox="0 0 1600 760"
 preserveAspectRatio="xMidYMid slice"
 xmlns="http://www.w3.org/2000/svg"
 >
 <defs>
 {/* Volumetric cloud filter (v3, operator side-by-side feedback):
 * v2's flat flood-fill read as a paper sticker with an offset
 * shadow. Real cloud = the FILL itself needs texture + lighting.
 * Stack: silhouette (dilate → blur → turbulence displacement →
 * alpha contrast) + a separate fine-turbulence field driven
 * through feDiffuseLighting as a bump map, lit from above
 * (azimuth 270 in SVG's y-down space) — bright billow tops,
 * shaded crevices, internal wisps. Composite the lit field into
 * the silhouette, then feather the edge so the letters dissolve
 * like vapor instead of cutting off. Seeds static (recompute
 * cost); Blink renders feDiffuseLighting consistently — Chrome
 * is the operator surface. */}
 <filter id="hq-ty-cloud-vol" x="-25%" y="-70%" width="150%" height="240%">
 {/* v4 legibility retune: v3's displacement scale 30 melted the
 * glyphs into cloud banks again. The reference's silhouette
 * barely distorts — its fluff is in the FILL texture and a
 * soft edge fade. Small mid-frequency nibble (scale 9) +
 * gentle alpha shoulder (8/-3, not 14/-5) keep the words
 * readable while the edges stay vapor. */}
 <feMorphology operator="dilate" radius="2" in="SourceAlpha" result="fat" />
 <feGaussianBlur in="fat" stdDeviation="4" result="soft" />
 <feTurbulence
 type="fractalNoise"
 baseFrequency="0.032 0.05"
 numOctaves="3"
 seed="7"
 result="bignoise"
 />
 <feDisplacementMap in="soft" in2="bignoise" scale="9" result="wobbly" />
 <feColorMatrix
 in="wobbly"
 type="matrix"
 values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 8 -3"
 result="sil"
 />
 <feTurbulence
 type="fractalNoise"
 baseFrequency="0.011 0.022"
 numOctaves="5"
 seed="3"
 result="cloudtex"
 />
 <feDiffuseLighting
 in="cloudtex"
 surfaceScale="13"
 diffuseConstant="1.05"
 lightingColor="#ffffff"
 result="lit"
 >
 <feDistantLight azimuth="270" elevation="58" />
 </feDiffuseLighting>
 {/* Tint the lit field lavender-white so the cloud sits in the
 * dusk atmosphere instead of reading printer-paper white. */}
 <feColorMatrix
 in="lit"
 type="matrix"
 values="0.52 0 0 0 0.16 0 0.55 0 0 0.17 0 0 0.66 0 0.22 0 0 0 1 0"
 result="litTint"
 />
 <feComposite in="litTint" in2="sil" operator="in" result="cloudbody" />
 <feGaussianBlur in="cloudbody" stdDeviation="1.7" />
 </filter>
 {/* Ambient under-shade — a soft violet mass beneath the letters
 * (NOT the v2 hard offset copy that read as a sticker shadow). */}
 <filter id="hq-ty-cloud-shadow" x="-30%" y="-80%" width="160%" height="260%">
 <feMorphology operator="dilate" radius="5" in="SourceAlpha" result="fat" />
 <feGaussianBlur in="fat" stdDeviation="13" result="soft" />
 <feColorMatrix
 in="soft"
 type="matrix"
 values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 10 -4"
 result="goo"
 />
 <feFlood floodColor="#4d5378" result="fill" />
 <feComposite in="fill" in2="goo" operator="in" />
 </filter>
 {/* Haze bridge — wide soft puffs the text rises out of, echoing
 * the FogBand below so the letters extend off the cloud layer. */}
 <filter id="hq-ty-haze" x="-40%" y="-200%" width="180%" height="500%">
 <feGaussianBlur stdDeviation="24" />
 </filter>
 {/* Soft glow for streak heads + spirals. */}
 <filter id="hq-ty-glow" x="-120%" y="-120%" width="340%" height="340%">
 <feGaussianBlur stdDeviation="3.2" />
 </filter>
 </defs>

 {/* Long-exposure streak field + golden octagon spirals removed
 * (2026-07-04) per operator direction. Data structures
 * and helper retired at the top of this file. */}

 {/* — July-4 sky fireworks — the pops for the plaza barrel's
 * launches. Rockets rise out of the fog and burst right above
 * the thank-you text, USA colors, one launch every 3s on a 9s
 * cycle. SMIL animation (see SvgFireworkBarrel's header for why
 * not CSS keyframes: Blink won't interpolate var()-bearing
 * transforms, and scale() pivoted on the viewBox corner).
 * Local-origin groups; reduced-motion hides .fw-anim via CSS. — */}
 <g className="hq-ty-fwsky">
 {[
 { off: 0, color: "#ff5a4e", ax: 596, ay: 388, ring: [46, 32] },
 { off: 3, color: "#f2f5ff", ax: 806, ay: 322, ring: [52, 36] },
 { off: 6, color: "#6fa0ff", ax: 1004, ay: 402, ring: [44, 30] },
 ].map((g) => {
 const launchX = g.ax - 14;
 const launchY = 756;
 const rise = g.ay - launchY; // negative: upward
 const dirs = Array.from({ length: 12 }, (_, i) => {
 const a = (i / 12) * Math.PI * 2;
 const rr = i % 2 === 0 ? g.ring[0] : g.ring[1];
 return {
 dx: +(rr * Math.cos(a)).toFixed(1),
 dy: +(rr * Math.sin(a)).toFixed(1),
 };
 });
 const dur = "9s";
 const begin = `${g.off}s`;
 return (
 <g key={g.off} className="fw-anim">
 {/* Rocket — local origin at the launch point in the fog. */}
 <g transform={`translate(${launchX}, ${launchY})`}>
 <g opacity="0">
 <animate
 attributeName="opacity"
 values="0;1;1;0;0"
 keyTimes="0;0.004;0.126;0.138;1"
 dur={dur}
 begin={begin}
 repeatCount="indefinite"
 />
 <animateTransform
 attributeName="transform"
 type="translate"
 values={`0 0; 14 ${rise}; 14 ${rise}`}
 keyTimes="0;0.133;1"
 calcMode="spline"
 keySplines="0.18 0.65 0.45 1; 0 0 1 1"
 dur={dur}
 begin={begin}
 repeatCount="indefinite"
 />
 <rect x="-1.4" y="-26" width="2.8" height="26" fill="#fff4d6" opacity="0.9" />
 <rect x="-1" y="0" width="2" height="30" fill="rgba(255, 214, 140, 0.4)" />
 </g>
 </g>
 {/* Burst — local origin at the apex; scale/translate pivot
 * on the element, not the viewBox corner. */}
 <g transform={`translate(${g.ax}, ${g.ay})`}>
 <circle r="7" fill={g.color} opacity="0">
 <animate
 attributeName="opacity"
 values="0;0;1;0;0"
 keyTimes="0;0.132;0.142;0.176;1"
 dur={dur}
 begin={begin}
 repeatCount="indefinite"
 />
 <animateTransform
 attributeName="transform"
 type="scale"
 values="0.4;0.4;1.5;0.7;0.7"
 keyTimes="0;0.132;0.15;0.176;1"
 dur={dur}
 begin={begin}
 repeatCount="indefinite"
 />
 </circle>
 {dirs.map((d, i) => (
 <rect
 key={i}
 x="-1.7"
 y="-1.7"
 width="3.4"
 height="3.4"
 fill={g.color}
 opacity="0"
 >
 <animate
 attributeName="opacity"
 values="0;0;1;0.85;0;0"
 keyTimes="0;0.133;0.142;0.21;0.26;1"
 dur={dur}
 begin={begin}
 repeatCount="indefinite"
 />
 <animateTransform
 attributeName="transform"
 type="translate"
 values={`0 0; 0 0; ${d.dx} ${d.dy}; ${(d.dx * 1.12).toFixed(1)} ${(d.dy + 12).toFixed(1)}; ${(d.dx * 1.12).toFixed(1)} ${(d.dy + 12).toFixed(1)}`}
 keyTimes="0;0.133;0.21;0.26;1"
 calcMode="spline"
 keySplines="0 0 1 1; 0.1 0.8 0.3 1; 0.5 0 0.8 0.6; 0 0 1 1"
 dur={dur}
 begin={begin}
 repeatCount="indefinite"
 />
 </rect>
 ))}
 </g>
 </g>
 );
 })}
 </g>

 {/* — The four-point sparkle beside the text — */}
 <g className="hq-ty-sparkle" transform="translate(1408 578)">
 <path
 d="M0,-16 C1.4,-4.4 4.4,-1.4 16,0 C4.4,1.4 1.4,4.4 0,16 C-1.4,4.4 -4.4,1.4 -16,0 C-4.4,-1.4 -1.4,-4.4 0,-16 Z"
 fill="#ffffff"
 opacity="0.9"
 />
 </g>
 </svg>

 {/* Cloud-text SVG — its own element inside two drift wrappers so the
 * 42s breathe + 86s drift (matched to the FogBand's layer-A cadence)
 * animate a composited transform without re-running the cloud
 * filters each frame. Filters render once; the layer floats.
 * (Filter ids referenced here live in the accents SVG's defs —
 * same-document URL references resolve across SVG elements.) */}
 <div className="hq-ty-drift-x">
 <div className="hq-ty-drift-y">
 <svg
 className="hq-v2-thankyou__svg"
 viewBox="0 0 1600 760"
 preserveAspectRatio="xMidYMid slice"
 xmlns="http://www.w3.org/2000/svg"
 >
 {/* — The cloud text — sits low so it rises out of the haze layer
 * (operator side-by-side feedback: the text should read as an
 * extension of the existing clouds, not a separate element).
 * Haze bridge behind, ambient under-shade,
 * then the volumetric lit body. — */}
 <g className="hq-ty-cloudtext" transform="rotate(-1.6 800 608)">
 <g filter="url(#hq-ty-haze)" opacity="0.5">
 <ellipse cx="800" cy="674" rx="580" ry="44" fill="#a9b6d4" />
 <ellipse cx="420" cy="654" rx="270" ry="32" fill="#b6c2dd" />
 <ellipse cx="1190" cy="658" rx="310" ry="34" fill="#b6c2dd" />
 </g>
 <g filter="url(#hq-ty-cloud-shadow)" transform="translate(0 14)" opacity="0.62">
 <text className="hq-ty-text" x="800" y="608" textAnchor="middle">
 <tspan>Thank</tspan>
 <tspan dx="30" dy="6">you</tspan>
 <tspan dx="30" dy="-10">for</tspan>
 <tspan dx="30" dy="7" letterSpacing="0.13em">visiting!</tspan>
 </text>
 </g>
 <g filter="url(#hq-ty-cloud-vol)">
 <text className="hq-ty-text" x="800" y="608" textAnchor="middle">
 <tspan>Thank</tspan>
 <tspan dx="30" dy="6">you</tspan>
 <tspan dx="30" dy="-10">for</tspan>
 <tspan dx="30" dy="7" letterSpacing="0.13em">visiting!</tspan>
 </text>
 </g>
 </g>

 </svg>
 </div>
 </div>
 </div>
 );
}

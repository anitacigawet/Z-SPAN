/* QUARANTINED 2026-06-01 — see ./README.md for the rationale.
 *
 * Not imported anywhere. Kept on disk for reference + reversibility per
 * [[hq-v2-means-clone-v1-not-reinterpret]]. The composite approach
 * (V1 painted building + rembg + V2 CSS gradient) shipped instead of
 * the code-built primitive in this file.
 *
 * Do NOT delete in a janitorial sweep — composite vs code-built is
 * owner-architecture territory, not cleanup. See README.md for revival
 * instructions if a future iteration wants this layer back.
 */
import { useLayerVisibility } from "../LayerVisibility";

/**
 * V2-11 — the ground-floor lobby glow + entrance frame. Draws the
 * warm-amber light that spills out of the entrance doors at street
 * level. The clickable Entrance hotspot itself is V1's `<Entrance>`
 * component, mounted by V2-15.
 *
 * Visibility gated by `visibility.ground`.
 */
export default function GroundFloor() {
 const { visibility } = useLayerVisibility();
 if (!visibility.ground) return null;

 return (
 <svg
 className="hq-v2-ground"
 viewBox="0 0 100 100"
 preserveAspectRatio="none"
 aria-hidden="true"
 style={{
 position: "absolute",
 inset: 0,
 pointerEvents: "none",
 zIndex: 5,
 }}
 >
 <defs>
 {/* Warm lobby glow — radial amber centered on the entrance doors */}
 <radialGradient id="hq-v2-lobby-glow" cx="50%" cy="100%" r="50%">
 <stop offset="0%" stopColor="rgba(245, 165, 36, 0.55)" />
 <stop offset="60%" stopColor="rgba(245, 165, 36, 0.15)" />
 <stop offset="100%" stopColor="rgba(245, 165, 36, 0)" />
 </radialGradient>
 </defs>

 {/* Lobby glow — a soft amber pool below the doors */}
 <ellipse
 cx="50"
 cy="88"
 rx="14"
 ry="3"
 fill="url(#hq-v2-lobby-glow)"
 />

 {/* Entrance backplate — solid amber rectangle for the doors. Not
 * semi-transparent, because the dense window grid at z=4 would
 * otherwise show through and obscure the doorway. Two rects:
 * a dark frame + the warm lit doorway inside. */}
 <rect
 x="46"
 y="76.4"
 width="8.1"
 height="11.7"
 rx="0.3"
 fill="#1a1410"
 />
 <rect
 x="46.7"
 y="77.2"
 width="6.7"
 height="10.5"
 rx="0.2"
 fill="#ffcc78"
 />
 {/* Door split — vertical dark line down the middle of the lit doorway */}
 <line
 x1="50"
 y1="77.2"
 x2="50"
 y2="87.7"
 stroke="#3a2a14"
 strokeWidth="0.15"
 />
 </svg>
 );
}

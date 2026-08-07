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
 * V2-6 — the building silhouette as a single SVG path, traced from the
 * BuildingCoords TIERS envelopes. Stepped tower: penthouse (narrowest) →
 * upper → upperBillboard (wider, mounts across) → mid (narrower) →
 * lowerBillboard (wider again) → ground (widest building tier). Roof
 * (tier 0) is the Z-SPAN sign, shipped separately in V2-9 — it sits
 * ABOVE this silhouette.
 *
 * The path traces the LEFT edge top→bottom, then the RIGHT edge
 * bottom→top, closing back to the starting point. Coordinates are
 * percentages of the V2 .scene container (viewBox 0 0 100 100,
 * `preserveAspectRatio="none"` so the SVG stretches to whatever
 * the .scene's aspect-locked dimensions resolve to).
 *
 * The fill is a vertical facade-charcoal gradient — lighter at the top,
 * darker at the bottom. V2-7 BuildingDepth layers ledge highlights +
 * tier shadows over this; V2-8 TierWindows paints the window grids on
 * top.
 *
 * Renders at z=2 — above Mountains (z=1) and below BuildingDepth (z=3).
 * Visibility gated by useLayerVisibility().
 */
export default function BuildingSilhouette() {
 const { visibility } = useLayerVisibility();
 if (!visibility.building) return null;

 // Path traces from penthouse top-left clockwise around the silhouette,
 // stepping in/out at each tier transition. See the plan § 3 diagram for
 // the stepped shape.
 const silhouettePath = [
 "M 41.5 14", // penthouse top-left (just below the roof/sign)
 "L 41.5 25", // penthouse bottom-left
 "L 32 25", // step out — upper top-left
 "L 32 47.5", // upper bottom-left
 "L 19.5 47.5", // step out — upper billboard top-left
 "L 19.5 53.5", // upper billboard bottom-left
 "L 35 53.5", // step in — mid top-left
 "L 35 61.5", // mid bottom-left
 "L 19.5 61.5", // step out — lower billboard top-left
 "L 19.5 67.5", // lower billboard bottom-left
 "L 14 67.5", // step out — ground top-left
 "L 14 87", // ground bottom-left (at horizon)
 "L 86 87", // ground bottom-right
 "L 86 67.5", // ground top-right
 "L 79.5 67.5", // step in — lower billboard top-right
 "L 79.5 61.5", // lower billboard bottom-right (= top-right of below)
 "L 65 61.5", // step in — mid top-right
 "L 65 53.5", // mid top-right (going up)
 "L 79.5 53.5", // step out — upper billboard bottom-right
 "L 79.5 47.5", // upper billboard top-right
 "L 68 47.5", // step in — upper top-right
 "L 68 25", // upper top-right (going up)
 "L 58.5 25", // step in — penthouse bottom-right
 "L 58.5 14", // penthouse top-right
 "Z", // close back to penthouse top-left
 ].join(" ");

 const containerStyle: React.CSSProperties = {
 position: "absolute",
 inset: 0,
 pointerEvents: "none",
 zIndex: 2,
 };

 return (
 <svg
 className="hq-v2-silhouette"
 viewBox="0 0 100 100"
 preserveAspectRatio="none"
 aria-hidden="true"
 style={containerStyle}
 >
 <defs>
 {/* Facade gradient — Z-SPAN palette charcoals (BRANDING_AND_IDENTITY).
 * Slightly lighter at top (closer to ambient sky), darker at base
 * (ground-floor shadow). The depth layer (V2-7) adds the per-tier
 * ledge highlights/shadows that make the silhouette read as 3D. */}
 <linearGradient id="hq-v2-facade" x1="0%" y1="0%" x2="0%" y2="100%">
 {/* Mid-tone facade — must be visibly LIGHTER than the sky
 * gradient behind it so the building reads as a solid mass.
 * V1 photo's building face is in the #3a-#4a range. */}
 <stop offset="0%" stopColor="#3e3e44" />
 <stop offset="60%" stopColor="#36363c" />
 <stop offset="100%" stopColor="#2c2c32" />
 </linearGradient>
 </defs>

 <path
 d={silhouettePath}
 fill="url(#hq-v2-facade)"
 stroke="#18181c"
 strokeWidth="0.2"
 strokeLinejoin="miter"
 />
 </svg>
 );
}

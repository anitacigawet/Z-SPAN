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
import { TIERS, rectToStyle } from "../BuildingCoords";

/**
 * V2-10 — the LED billboard frames (upper + lower bands). Just the
 * housings — the actual ticker mechanism is V1's `<Billboard>` component
 * which V2-15 mounts inside these frames once the V2 coordinate space
 * is settled. Drawn as CSS divs with a dark inner panel, civic-cyan
 * border, and the existing scanline overlay pattern from V1's
 * `.hq-root .ticker::before`.
 *
 * Renders at z=5 alongside the sign. Visibility gated by
 * `visibility.billboards`.
 */
export default function BillboardFrames() {
 const { visibility } = useLayerVisibility();
 if (!visibility.billboards) return null;

 return (
 <>
 <div
 className="hq-v2-billboard-frame hq-v2-billboard-frame--upper"
 style={rectToStyle(TIERS.upperBillboard)}
 aria-hidden="true"
 />
 <div
 className="hq-v2-billboard-frame hq-v2-billboard-frame--lower"
 style={rectToStyle(TIERS.lowerBillboard)}
 aria-hidden="true"
 />
 </>
 );
}

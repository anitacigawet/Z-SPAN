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
 * V2-7 — depth cues painted ON TOP of the BuildingSilhouette: thin
 * highlight lines along ledge TOPS (where a wider tier extends past the
 * narrower tier above it, catching ambient sky light) and thin shadow
 * lines along ledge UNDERSIDES (where a wider tier's bottom is exposed
 * by a narrower tier below it). Plus a subtle base shadow tucked under
 * the ground floor where the building meets the horizon.
 *
 * Per HQ_V2_BACKGROUND_REBUILD_PLAN.md § 2 (table row 7): "ledge
 * highlights + tier shadows + window-recess shadows. Output: the tower
 * looks dimensional." The window-recess shadows ship with the actual
 * window grids in V2-8 (the per-cell `data-state` shading is the
 * recess effect at scale).
 *
 * Renders at z=3, between the silhouette (z=2) and the windows/sign/
 * billboards above. Visibility piggy-backs on `building` — depth is part
 * of the building primitive, no separate toggle.
 */
export default function BuildingDepth() {
  const { visibility } = useLayerVisibility();
  if (!visibility.building) return null;

  // Ledge segments — pairs of [x1, x2] where each tier transition exposes
  // a visible ledge top (wider tier under narrower tier) or underside
  // (narrower tier under wider tier). y values match the BuildingCoords
  // tier-top transitions. Stroke width is in viewBox units (=% of scene).
  const HIGHLIGHT = "rgba(255, 255, 255, 0.16)";
  const SHADOW = "rgba(0, 0, 0, 0.55)";
  const LEDGE_HEIGHT = 0.4;

  return (
    <svg
      className="hq-v2-depth"
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      aria-hidden="true"
      style={{
        position: "absolute",
        inset: 0,
        pointerEvents: "none",
        zIndex: 3,
      }}
    >
      {/* Penthouse top — full-width highlight (catches sky) */}
      <line
        x1="41.5"
        x2="58.5"
        y1={14 + LEDGE_HEIGHT / 2}
        y2={14 + LEDGE_HEIGHT / 2}
        stroke={HIGHLIGHT}
        strokeWidth={LEDGE_HEIGHT}
      />

      {/* y=25 — Upper tier top, exposed on both sides of penthouse */}
      <line x1="32" x2="41.5" y1={25 + LEDGE_HEIGHT / 2} y2={25 + LEDGE_HEIGHT / 2} stroke={HIGHLIGHT} strokeWidth={LEDGE_HEIGHT} />
      <line x1="58.5" x2="68" y1={25 + LEDGE_HEIGHT / 2} y2={25 + LEDGE_HEIGHT / 2} stroke={HIGHLIGHT} strokeWidth={LEDGE_HEIGHT} />

      {/* y=47.5 — UpperBillboard top, exposed on both sides of upper */}
      <line x1="19.5" x2="32" y1={47.5 + LEDGE_HEIGHT / 2} y2={47.5 + LEDGE_HEIGHT / 2} stroke={HIGHLIGHT} strokeWidth={LEDGE_HEIGHT} />
      <line x1="68" x2="79.5" y1={47.5 + LEDGE_HEIGHT / 2} y2={47.5 + LEDGE_HEIGHT / 2} stroke={HIGHLIGHT} strokeWidth={LEDGE_HEIGHT} />

      {/* y=53.5 — UpperBillboard underside (mid is narrower), exposed shadow */}
      <line x1="19.5" x2="35" y1={53.5 - LEDGE_HEIGHT / 2} y2={53.5 - LEDGE_HEIGHT / 2} stroke={SHADOW} strokeWidth={LEDGE_HEIGHT} />
      <line x1="65" x2="79.5" y1={53.5 - LEDGE_HEIGHT / 2} y2={53.5 - LEDGE_HEIGHT / 2} stroke={SHADOW} strokeWidth={LEDGE_HEIGHT} />

      {/* y=61.5 — LowerBillboard top, exposed on both sides of mid */}
      <line x1="19.5" x2="35" y1={61.5 + LEDGE_HEIGHT / 2} y2={61.5 + LEDGE_HEIGHT / 2} stroke={HIGHLIGHT} strokeWidth={LEDGE_HEIGHT} />
      <line x1="65" x2="79.5" y1={61.5 + LEDGE_HEIGHT / 2} y2={61.5 + LEDGE_HEIGHT / 2} stroke={HIGHLIGHT} strokeWidth={LEDGE_HEIGHT} />

      {/* y=67.5 — Ground top, exposed on both sides of lower billboard */}
      <line x1="14" x2="19.5" y1={67.5 + LEDGE_HEIGHT / 2} y2={67.5 + LEDGE_HEIGHT / 2} stroke={HIGHLIGHT} strokeWidth={LEDGE_HEIGHT} />
      <line x1="79.5" x2="86" y1={67.5 + LEDGE_HEIGHT / 2} y2={67.5 + LEDGE_HEIGHT / 2} stroke={HIGHLIGHT} strokeWidth={LEDGE_HEIGHT} />

      {/* Ground shadow tucked under the base — a subtle radial darkening
       * just below the horizon where the building's ground floor casts
       * onto the plaza. */}
      <ellipse
        cx="50"
        cy="87"
        rx="40"
        ry="2"
        fill="rgba(0, 0, 0, 0.55)"
        opacity="0.7"
      />
    </svg>
  );
}

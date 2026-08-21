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
import { HORIZON_PCT } from "../BuildingCoords";

/**
 * V2-5 — desert mountains on the horizon (the soft civic-broadcast-town
 * backdrop the V1 photo had baked in). Three SVG ranges with atmospheric
 * perspective: farthest is lightest (sky-haze color), closest is near-black.
 * Dithered overlay softens the silhouettes per HQ_CONCEPT_IMAGE_PROMPT.md
 * § THE SETTING ("the soft silhouette of desert mountains on the horizon").
 *
 * The SVG fills a band that ends AT the horizon (`HORIZON_PCT` from
 * BuildingCoords) and extends ~24% of the scene above it, so the foothills
 * meet the ground floor cleanly.
 *
 * Rendered at z=1 — above the sky gradient, below the building silhouette
 * (V2-6). Visibility gated by the LayerVisibility hook so it can be toggled
 * off independently while iterating on other layers.
 */
export default function Mountains() {
  const { visibility } = useLayerVisibility();
  if (!visibility.mountains) return null;

  // The mountains band sits anchored to the horizon. Height is the % of the
  // scene the mountains occupy above the horizon line.
  const bandStyle: React.CSSProperties = {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: `${100 - HORIZON_PCT}%`,
    height: "24%",
    pointerEvents: "none",
    zIndex: 1,
  };

  return (
    <svg
      className="hq-v2-mountains"
      viewBox="0 0 1376 200"
      preserveAspectRatio="none"
      aria-hidden="true"
      style={bandStyle}
    >
      <defs>
        {/* Dithered overlay — small dots that soften the harder ridge edges
         *  and read as "haze + low desert dust" per the concept prompt. */}
        <pattern
          id="hq-v2-mountain-dither"
          patternUnits="userSpaceOnUse"
          width="3"
          height="3"
        >
          <rect width="3" height="3" fill="transparent" />
          <circle cx="0.5" cy="0.5" r="0.4" fill="rgba(11, 14, 31, 0.6)" />
        </pattern>
      </defs>

      {/* Range 1 — farthest, the haze layer. Softest profile, sky-tinted. */}
      <path
        d="M0,135 L70,108 L160,128 L260,98 L360,118 L460,92 L560,115 L660,98 L760,118 L860,95 L960,118 L1060,95 L1160,115 L1260,98 L1376,112 L1376,200 L0,200 Z"
        fill="#1f2c52"
      />

      {/* Range 2 — mid-distance Cerbat-style jagged ridges. */}
      <path
        d="M0,162 L50,142 L120,158 L200,128 L280,148 L360,118 L440,142 L520,118 L620,148 L720,118 L800,138 L880,118 L960,142 L1040,118 L1120,138 L1220,118 L1300,142 L1376,128 L1376,200 L0,200 Z"
        fill="#13203d"
      />
      <path
        d="M0,162 L50,142 L120,158 L200,128 L280,148 L360,118 L440,142 L520,118 L620,148 L720,118 L800,138 L880,118 L960,142 L1040,118 L1120,138 L1220,118 L1300,142 L1376,128 L1376,200 L0,200 Z"
        fill="url(#hq-v2-mountain-dither)"
      />

      {/* Range 3 — closest foothills, near-black. Sharpest silhouette. */}
      <path
        d="M0,180 L36,170 L92,182 L160,160 L228,176 L300,158 L380,178 L460,154 L540,176 L620,160 L700,178 L780,160 L860,180 L940,162 L1020,178 L1100,158 L1180,176 L1260,164 L1340,180 L1376,172 L1376,200 L0,200 Z"
        fill="#0a0e1f"
      />
    </svg>
  );
}

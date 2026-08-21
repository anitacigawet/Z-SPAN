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
 * V2-14 — the foreground plaza band: street, a couple of cars with
 * headlight/taillight glows, and 3-5 small pedestrian silhouettes per
 * James 2026-06-01 ("minimal, 3-5 silhouettes total"). Ambient layer
 * that sits ABOVE every building element so it reads as foreground.
 *
 * Visibility gated by `visibility.plaza`.
 */
export default function Plaza() {
  const { visibility } = useLayerVisibility();
  if (!visibility.plaza) return null;

  return (
    <svg
      className="hq-v2-plaza"
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      aria-hidden="true"
      style={{
        position: "absolute",
        inset: 0,
        pointerEvents: "none",
        zIndex: 6,
      }}
    >
      {/* Street strip — pure black foreground band below the building.
       *  Sits over the dark plaza-floor portion of the scene gradient. */}
      <rect x="0" y="93" width="100" height="7" fill="#04050a" />
      {/* Curb highlight */}
      <rect x="0" y="92.8" width="100" height="0.25" fill="#3a2a22" />

      {/* Faint lane marking dashes */}
      <g fill="#6a6f7a">
        <rect x="20" y="97" width="2.5" height="0.4" />
        <rect x="30" y="97" width="2.5" height="0.4" />
        <rect x="40" y="97" width="2.5" height="0.4" />
        <rect x="60" y="97" width="2.5" height="0.4" />
        <rect x="70" y="97" width="2.5" height="0.4" />
        <rect x="80" y="97" width="2.5" height="0.4" />
      </g>

      {/* Car 1 with white headlights — lighter body so it reads against
       *  the dark street. */}
      <g>
        <rect x="22" y="95.4" width="6" height="2.0" rx="0.4" fill="#2e323c" />
        <rect x="23.2" y="94.9" width="3.6" height="0.7" rx="0.2" fill="#2e323c" />
        <circle cx="22" cy="96.6" r="0.5" fill="rgba(255, 245, 200, 0.95)" />
        <circle cx="28" cy="96.6" r="0.4" fill="#ff6464" />
      </g>

      {/* Car 2 with white headlights (other direction) */}
      <g>
        <rect x="68" y="95.4" width="6" height="2.0" rx="0.4" fill="#2e323c" />
        <rect x="69.2" y="94.9" width="3.6" height="0.7" rx="0.2" fill="#2e323c" />
        <circle cx="74" cy="96.6" r="0.5" fill="rgba(255, 245, 200, 0.95)" />
        <circle cx="68" cy="96.6" r="0.4" fill="#ff6464" />
      </g>

      {/* Pedestrians — slightly bigger silhouettes against the warm
       *  horizon band so they read as people. */}
      <g fill="#040406">
        <circle cx="40" cy="92.2" r="0.7" />
        <rect x="39.3" y="92.8" width="1.4" height="2.2" rx="0.3" />
        <circle cx="45" cy="92.2" r="0.7" />
        <rect x="44.3" y="92.8" width="1.4" height="2.2" rx="0.3" />
        <circle cx="55" cy="92.2" r="0.7" />
        <rect x="54.3" y="92.8" width="1.4" height="2.2" rx="0.3" />
        <circle cx="60" cy="92.2" r="0.7" />
        <rect x="59.3" y="92.8" width="1.4" height="2.2" rx="0.3" />
      </g>
    </svg>
  );
}

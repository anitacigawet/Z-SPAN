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
 * V2-13 — the press-conference vignette (bottom-left). SVG with a
 * podium, a screen mount, and 4-5 small audience-silhouette figures.
 * V1's `<PressScreen>` (the funding-stats display) mounts inside the
 * screen rectangle in V2-15.
 *
 * Visibility gated by `visibility.press`.
 */
export default function PressVignette() {
  const { visibility } = useLayerVisibility();
  if (!visibility.press) return null;

  return (
    <svg
      className="hq-v2-press"
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
      {/* Screen mount — bright civic-cyan bordered panel, must read
       *  against the warm dusk horizon at this y. Lighter frame so the
       *  panel pops as "screen on the building wall." */}
      <rect x="1.4" y="75.4" width="17.5" height="10.5" fill="#10131a" stroke="#3aa8d6" strokeWidth="0.3" />
      <rect x="1.4" y="75.4" width="17.5" height="0.4" fill="rgba(255, 255, 255, 0.22)" />
      <rect x="1.4" y="85.5" width="17.5" height="0.4" fill="rgba(0, 0, 0, 0.5)" />

      {/* Podium — small lectern in front of the screen */}
      <rect x="8.5" y="87.5" width="3.5" height="2.5" fill="#2c2f38" stroke="#0a0a0a" strokeWidth="0.15" />
      <rect x="9.3" y="86.5" width="1.9" height="1.2" fill="#2c2f38" />

      {/* Audience silhouettes — solid-black silhouettes against the
       *  warm horizon read as foreground people. Bumped size slightly
       *  so they're recognizable. */}
      <g fill="#06060a">
        <circle cx="3" cy="92.5" r="0.9" />
        <rect x="2.1" y="93.1" width="1.8" height="2.6" rx="0.3" />
        <circle cx="5.7" cy="92.7" r="0.9" />
        <rect x="4.8" y="93.3" width="1.8" height="2.4" rx="0.3" />
        <circle cx="14.4" cy="92.6" r="0.9" />
        <rect x="13.5" y="93.2" width="1.8" height="2.5" rx="0.3" />
        <circle cx="17" cy="92.5" r="0.9" />
        <rect x="16.1" y="93.1" width="1.8" height="2.6" rx="0.3" />
      </g>
    </svg>
  );
}

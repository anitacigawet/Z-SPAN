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
 * V2-9 — the rooftop marquee. Editable SVG <text> for the wordmark (so
 * V2-stretch-a can swap it to "Click to enter" for the onboarding demo
 * without touching pixel art), two <rect> posts anchoring the marquee
 * to the penthouse top, an antenna <line> rising from the center, and
 * a blinking beacon <circle> at the antenna's tip.
 *
 * Sized to the TIERS.roof rect (top=6.5, left=39, width=22, height=8.5)
 * with the antenna extending above. Renders at z=5 — above the building
 * primitive, below the billboard tickers + V1 overlays.
 *
 * Visibility gated by `visibility.sign`.
 */
export default function ZSpanSign({ label = "Z-SPAN" }: { label?: string }) {
  const { visibility } = useLayerVisibility();
  if (!visibility.sign) return null;

  // All coordinates in viewBox-space (0..100 horizontal, 0..100 vertical).
  // The roof tier is roughly y=6.5 to 15. The marquee sits at the top of
  // the penthouse with the antenna rising above into the sky.
  const marqueeLeft = 40;
  const marqueeRight = 60;
  const marqueeTop = 8;
  const marqueeBottom = 13;
  const marqueeCenter = (marqueeLeft + marqueeRight) / 2;

  return (
    <svg
      className="hq-v2-sign"
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      aria-label="Z-SPAN rooftop marquee"
      style={{
        position: "absolute",
        inset: 0,
        pointerEvents: "none",
        zIndex: 5,
      }}
    >
      <defs>
        {/* Soft amber glow filter — Z-SPAN's signage gold (#F2A91C) with
         * a warm halo. Applied to the marquee rectangle + text. */}
        <filter id="hq-v2-sign-glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="0.4" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* Antenna — thin line rising from the marquee center to ~y=2 */}
      <line
        x1={marqueeCenter}
        y1={marqueeTop}
        x2={marqueeCenter}
        y2={2}
        stroke="#3a3a40"
        strokeWidth="0.25"
      />
      {/* Antenna crossbars (visual cue that it's a radio antenna) */}
      <line
        x1={marqueeCenter - 1.2}
        y1={4}
        x2={marqueeCenter + 1.2}
        y2={4}
        stroke="#3a3a40"
        strokeWidth="0.2"
      />
      <line
        x1={marqueeCenter - 0.8}
        y1={3}
        x2={marqueeCenter + 0.8}
        y2={3}
        stroke="#3a3a40"
        strokeWidth="0.2"
      />

      {/* Beacon — red circle at the tip with a pulse animation */}
      <circle
        cx={marqueeCenter}
        cy={1.8}
        r="0.5"
        fill="#ff5454"
        className="hq-v2-sign__beacon"
      />

      {/* Marquee posts — anchor the sign to the penthouse roof */}
      <rect
        x={marqueeLeft + 1}
        y={marqueeBottom}
        width="0.8"
        height={14.5 - marqueeBottom}
        fill="#2a2a2e"
      />
      <rect
        x={marqueeRight - 1.8}
        y={marqueeBottom}
        width="0.8"
        height={14.5 - marqueeBottom}
        fill="#2a2a2e"
      />

      {/* Marquee frame — dark backing, gold border, amber glow */}
      <rect
        x={marqueeLeft}
        y={marqueeTop}
        width={marqueeRight - marqueeLeft}
        height={marqueeBottom - marqueeTop}
        rx="0.4"
        fill="#0c0d10"
        stroke="#F2A91C"
        strokeWidth="0.3"
        filter="url(#hq-v2-sign-glow)"
      />

      {/* Wordmark — editable <text>. Swap `label` prop for onboarding. */}
      <text
        x={marqueeCenter}
        y={marqueeTop + (marqueeBottom - marqueeTop) / 2 + 1.1}
        textAnchor="middle"
        fontFamily="'VT323', ui-monospace, monospace"
        fontSize="3.4"
        fontWeight="700"
        letterSpacing="0.18"
        fill="#F5A524"
        filter="url(#hq-v2-sign-glow)"
        style={{ textTransform: "uppercase" }}
      >
        {label}
      </text>
    </svg>
  );
}

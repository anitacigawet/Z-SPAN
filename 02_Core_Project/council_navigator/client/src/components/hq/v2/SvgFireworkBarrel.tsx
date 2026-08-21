import type { Rect } from "@/components/hq/hqHelpers";

/**
 * July-4 firework barrel — seasonal plaza accent (operator ask, 2026-07-02,
 * soft-launch eve / July-4 preview week).
 *
 * A tiny pixel-art powder barrel with red/white/blue rocket sticks sits on
 * the plaza in the sky gap between the funding billboard and the tower.
 * Every 3 seconds a rocket launches with a flash and streaks up out of the
 * rect toward the sky; the BURST pops way up in the skybox above the
 * thank-you cloud text (ThankYouClouds' firework group). Three staggered
 * launches per 9s master cycle rotate the USA palette.
 *
 * Animation is SMIL (v3 rewrite, same idiom as the FogBand's turbulence):
 * the first CSS build animated `translate(var(--rise))` inside @keyframes,
 * and Blink refuses to interpolate var()-bearing transforms — the rocket
 * TELEPORTED to its endpoint instead of streaking, and the burst particles'
 * scale() pivoted on the viewBox corner so they slid in diagonally. SMIL
 * animateTransform carries literal per-element values, interpolates
 * everywhere, and begin-offsets give the stagger natively. Every animated
 * piece lives in a local-origin <g> so transforms pivot on the element,
 * not the viewBox corner. prefers-reduced-motion hides .fw-anim (CSS).
 *
 * Registered as the "fireworks" LayerVisibility key so the operator can
 * retire the element after the 4th without a code change.
 */

const USA = { red: "#ff5a4e", white: "#f2f5ff", blue: "#6fa0ff" } as const;
const LAUNCH_X = 50;
const LAUNCH_Y = 348;
const CYCLE = 9; // seconds; one launch every 3s across three groups

const GROUPS = [
  { off: 0, color: USA.red },
  { off: 3, color: USA.white },
  { off: 6, color: USA.blue },
];

export default function SvgFireworkBarrel({ rect }: { rect: Rect }) {
  return (
    <div
      className="svg-firework-barrel"
      aria-hidden="true"
      style={{
        position: "absolute",
        top: `${rect.top}%`,
        left: `${rect.left}%`,
        width: `${rect.width}%`,
        height: `${rect.height}%`,
        zIndex: 14,
        pointerEvents: "none",
      }}
    >
      <svg width="100%" height="100%" viewBox="0 0 100 400" preserveAspectRatio="none">
        {/* Launch flash + rising rocket per color slot. Local-origin group
         *  at the barrel mouth; the inner group carries the SMIL rise. */}
        <g transform={`translate(${LAUNCH_X}, ${LAUNCH_Y})`}>
          {GROUPS.map((g) => (
            <g key={g.off} className="fw-anim">
              <circle r="4" fill={g.color} opacity="0">
                <animate
                  attributeName="opacity"
                  values="0;0.9;0;0"
                  keyTimes="0;0.006;0.026;1"
                  dur={`${CYCLE}s`}
                  begin={`${g.off}s`}
                  repeatCount="indefinite"
                />
              </circle>
              <g opacity="0">
                <animate
                  attributeName="opacity"
                  values="0;1;1;0;0"
                  keyTimes="0;0.004;0.128;0.14;1"
                  dur={`${CYCLE}s`}
                  begin={`${g.off}s`}
                  repeatCount="indefinite"
                />
                <animateTransform
                  attributeName="transform"
                  type="translate"
                  values="0 0; 0 -392; 0 -392"
                  keyTimes="0;0.133;1"
                  calcMode="spline"
                  keySplines="0.18 0.65 0.45 1; 0 0 1 1"
                  dur={`${CYCLE}s`}
                  begin={`${g.off}s`}
                  repeatCount="indefinite"
                />
                <rect x="-1.2" y="-7" width="2.4" height="7" fill="#fff4d6" />
                <rect x="-0.8" y="0" width="1.6" height="9" fill="rgba(255, 214, 140, 0.5)" />
              </g>
            </g>
          ))}
        </g>

        {/* ============ THE BARREL (always visible) ============ */}
        <g transform="translate(33, 352)">
          {/* Rocket sticks poking out — red / white / blue tips */}
          <g>
            <rect x="8" y="-14" width="1.6" height="16" fill="#7a5a38" transform="rotate(-14 9 -6)" />
            <rect x="7" y="-18" width="3.6" height="6" rx="0.8" fill={USA.red} transform="rotate(-14 9 -14)" />
            <rect x="16" y="-17" width="1.6" height="18" fill="#7a5a38" />
            <rect x="15" y="-22" width="3.6" height="6" rx="0.8" fill={USA.white} />
            <rect x="24" y="-13" width="1.6" height="15" fill="#7a5a38" transform="rotate(13 25 -6)" />
            <rect x="23" y="-17" width="3.6" height="6" rx="0.8" fill={USA.blue} transform="rotate(13 25 -13)" />
          </g>
          {/* Barrel body — staves + hoops, pixel idiom */}
          <rect x="0" y="0" width="34" height="30" rx="3" fill="#7d5a36" />
          <rect x="0" y="0" width="34" height="30" rx="3" fill="none" stroke="#3d2c18" strokeWidth="1.2" />
          {[8.5, 17, 25.5].map((x) => (
            <rect key={x} x={x} y="1" width="1" height="28" fill="rgba(61, 44, 24, 0.55)" />
          ))}
          <rect x="-1" y="5" width="36" height="3" fill="#2e2318" />
          <rect x="-1" y="22" width="36" height="3" fill="#2e2318" />
          <rect x="2" y="0.5" width="30" height="2" fill="rgba(255, 220, 160, 0.25)" />
          <ellipse cx="17" cy="2.5" rx="11" ry="2" fill="#1c1410" />
        </g>
      </svg>
    </div>
  );
}

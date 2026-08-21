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
import type { ReactElement } from "react";
import { useLayerVisibility } from "../LayerVisibility";
import { TIERS, type Rect } from "../BuildingCoords";

/**
 * V2-8 — window grid for one window-bearing tier. The four window tiers
 * (penthouse, upper, mid, ground) each mount an instance with their own
 * row × col count + a stable seed. The billboard bands (upperBillboard,
 * lowerBillboard) don't get window grids — they're LED panels shipping
 * in V2-10. The roof + plaza tiers don't get grids either.
 *
 * Each cell has a deterministic `data-state` of `lit` | `dim` | `dark`
 * driven by a simple seeded hash of (seed, row, col) — same state across
 * reloads. Lit cells get a warm amber glow + a slow flicker animation
 * (the "heads at work" cue from REPROMPT_01 § Depth).
 *
 * Renders at z=4. Visibility gated by `visibility.building` since the
 * windows are part of the building primitive — toggle Building off and
 * the whole stack (silhouette + depth + windows) disappears together.
 */

// Simple deterministic 32-bit-ish string hash → 0..1. Same seed always
// produces the same value, so the lit-window pattern is stable across
// renders and reloads.
function seededRandom(seed: string): number {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 100000) / 100000;
}

function cellState(seed: string): "lit" | "dim" | "dark" {
  const r = seededRandom(seed);
  // 45% lit / 35% dim / 20% dark — matches V1's "most-offices-on,
  // some-dark, dusk-evening" distribution. Tuned against the
  // zspan-hq.png reference 2026-06-01.
  if (r < 0.45) return "lit";
  if (r < 0.8) return "dim";
  return "dark";
}

interface TierWindowsProps {
  /** Tier name — used as the random seed so each tier has its own pattern. */
  tier: keyof typeof TIERS;
  /** Number of window rows */
  rows: number;
  /** Number of window columns */
  cols: number;
  /** Optional inset within the tier rect (each side, %). Default 0. */
  inset?: number;
}

/**
 * Render a window grid sized to the named tier's rectangle.
 */
export function TierWindows({ tier, rows, cols, inset = 0 }: TierWindowsProps) {
  const rect: Rect = TIERS[tier];
  const style: React.CSSProperties = {
    position: "absolute",
    top: `${rect.top + inset}%`,
    left: `${rect.left + inset}%`,
    width: `${rect.width - inset * 2}%`,
    height: `${rect.height - inset * 2}%`,
    display: "grid",
    gridTemplateColumns: `repeat(${cols}, 1fr)`,
    gridTemplateRows: `repeat(${rows}, 1fr)`,
    pointerEvents: "none",
    zIndex: 4,
  };

  const cells: ReactElement[] = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const state = cellState(`${tier}-${r}-${c}`);
      cells.push(
        <div
          key={`${r}-${c}`}
          className="hq-v2-window"
          data-state={state}
          // Stagger the flicker animation per cell so lit windows don't
          // all blink in sync. Seeded delay 0..3.9s by hashing again.
          style={{
            animationDelay: state === "lit"
              ? `${(seededRandom(`${tier}-${r}-${c}-d`) * 3.9).toFixed(2)}s`
              : undefined,
          }}
        />,
      );
    }
  }

  return (
    <div className="hq-v2-windows" style={style}>
      {cells}
    </div>
  );
}

/**
 * Mounts all four window-bearing tiers in a single component. HQPageV2
 * just renders <TierWindowsLayer /> and gets the full grid set.
 */
export default function TierWindowsLayer() {
  const { visibility } = useLayerVisibility();
  if (!visibility.building) return null;

  return (
    <>
      <TierWindows tier="penthouse" rows={5} cols={11} inset={0.4} />
      <TierWindows tier="upper" rows={14} cols={42} inset={0.4} />
      <TierWindows tier="mid" rows={5} cols={38} inset={0.3} />
      <TierWindows tier="ground" rows={14} cols={80} inset={0.4} />
    </>
  );
}

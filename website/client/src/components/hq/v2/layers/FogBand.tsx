import { useState } from "react";
import SettingsCloudPanel from "@/components/hq/skybox/SettingsCloudPanel";
import { useLayerVisibility } from "../LayerVisibility";

type FogBandProps = {
 /** Active StarField variant id — forwarded into the settings panel. */
 variantId?: string;
 /** StarField variant change handler — forwarded into the settings panel. */
 onVariantChange?: (id: string) => void;
};

/**
 * V2 fog band — a hypnotic SF-fog accent strip across the Skybox /
 * scene-wrap seam (~75vh) that hides the horizon line and ports V1's
 * Settings affordance.
 *
 * Two design choices James called 2026-06-02:
 *
 * 1. WHOLE BAND IS THE CLICK TARGET. Unlike V1's CloudDivider — which
 * had one distinct "settings cloud" puff as the click target — V2's
 * fog band is uniformly clickable. A subtle "Settings" hint fades in
 * on hover so the affordance stays discoverable.
 *
 * 2. HYPNOTIC SF FOG, not stylized cloud puffs. Three layered passes
 * produce the motion:
 * - Procedural feTurbulence (back) — animated baseFrequency + seed
 * give the morphing churn that pure drift can't deliver.
 * - 3 photographic CC0 SF-fog cutouts — drift at different speeds
 * and counter-directions for parallax depth.
 * - One mid-layer also "breathes" vertically over 40s so fog levels
 * rise and fall the way real advection fog does.
 *
 * The photographic sources were pre-processed into the fog PNGs that ship
 * here. The raw sources and `process_fog.py` preprocessing script were
 * retired on 2026-07-15; these PNGs are the derived work, cropped to PURE
 * FOG (no bridge structure visible), tinted to V2 navy, and feathered
 * top/bottom into transparency so each strip dissolves into the sky.
 */
export default function FogBand({
 variantId,
 onVariantChange,
}: FogBandProps = {}) {
 const [open, setOpen] = useState(false);
 const { visibility } = useLayerVisibility();
 if (!visibility.fog) return null;

 return (
 <>
 <button
 type="button"
 className="hq-v2-fog-band"
 aria-label="Open HQ settings — click anywhere on the fog"
 aria-expanded={open}
 onClick={() => setOpen(true)}
 >
 {/* Procedural turbulence base — the slow churn underneath the
 * photographic accents. SMIL animates baseFrequency + seed so
 * the noise pattern morphs rather than just drifting flat. */}
 <svg
 className="hq-v2-fog-band__noise"
 aria-hidden="true"
 preserveAspectRatio="none"
 xmlns="http://www.w3.org/2000/svg"
 >
 <defs>
 <filter id="hq-v2-fog-noise-far" x="0%" y="0%" width="100%" height="100%">
 <feTurbulence
 type="fractalNoise"
 baseFrequency="0.009 0.022"
 numOctaves="3"
 seed="2"
 result="noise"
 >
 <animate
 attributeName="baseFrequency"
 values="0.009 0.022; 0.013 0.028; 0.009 0.022"
 dur="32s"
 repeatCount="indefinite"
 />
 <animate
 attributeName="seed"
 values="2; 7; 2"
 dur="48s"
 repeatCount="indefinite"
 />
 </feTurbulence>
 <feColorMatrix
 in="noise"
 type="matrix"
 values="0 0 0 0 0.62
 0 0 0 0 0.70
 0 0 0 0 0.86
 0.45 0 0 0 0"
 />
 </filter>
 <filter id="hq-v2-fog-noise-near" x="0%" y="0%" width="100%" height="100%">
 <feTurbulence
 type="fractalNoise"
 baseFrequency="0.025 0.05"
 numOctaves="2"
 seed="13"
 result="noise"
 >
 <animate
 attributeName="baseFrequency"
 values="0.025 0.05; 0.030 0.058; 0.025 0.05"
 dur="22s"
 repeatCount="indefinite"
 />
 </feTurbulence>
 <feColorMatrix
 in="noise"
 type="matrix"
 values="0 0 0 0 0.78
 0 0 0 0 0.84
 0 0 0 0 0.96
 0.22 0 0 0 0"
 />
 </filter>
 </defs>
 <rect
 x="-10%"
 y="-10%"
 width="120%"
 height="120%"
 filter="url(#hq-v2-fog-noise-far)"
 />
 <rect
 x="-10%"
 y="-10%"
 width="120%"
 height="120%"
 filter="url(#hq-v2-fog-noise-near)"
 />
 </svg>

 {/* Photographic fog accents — parallax drift at different speeds
 * and counter-directions. Layer C is the broadest + slowest
 * back layer; A is the mid-band (also "breathes" vertically);
 * B is the smaller, faster near-layer for foreground texture. */}
 <div
 className="hq-v2-fog-band__photo hq-v2-fog-band__photo--c"
 aria-hidden="true"
 />
 <div
 className="hq-v2-fog-band__photo hq-v2-fog-band__photo--a"
 aria-hidden="true"
 />
 <div
 className="hq-v2-fog-band__photo hq-v2-fog-band__photo--b"
 aria-hidden="true"
 />

 {/* Affordance hint — fades in on hover so whole-band-clickable
 * stays discoverable. Center-anchored so it shows wherever the
 * cursor enters the band. */}
 <span className="hq-v2-fog-band__hint" aria-hidden="true">
 Settings
 </span>
 </button>

 {open && (
 <SettingsCloudPanel
 onClose={() => setOpen(false)}
 variantId={variantId}
 onVariantChange={onVariantChange}
 />
 )}
 </>
 );
}

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
 * V2-12 — the generator outbuilding (bottom-right). SVG of the housing
 * box + a couple of pipes routing up to it from the ground, plus a
 * status light. V1's `<InfraPanel>` (the service-status list) mounts
 * on top in V2-15.
 *
 * Visibility gated by `visibility.generator`.
 */
export default function GeneratorOutbuilding() {
 const { visibility } = useLayerVisibility();
 if (!visibility.generator) return null;

 return (
 <svg
 className="hq-v2-generator"
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
 {/* Outbuilding box — sits to the right of the ground floor. Must
 * be mid-tone enough to read against the dusk gradient bg. */}
 <rect x="84" y="80" width="15" height="7" fill="#3a3c42" stroke="#18181c" strokeWidth="0.2" />
 <rect x="84" y="80" width="15" height="0.6" fill="rgba(255,255,255,0.18)" />
 <rect x="84" y="86.4" width="15" height="0.6" fill="rgba(0,0,0,0.45)" />

 {/* Pipes — vertical rises with a horizontal cross-tie */}
 <line x1="86" y1="79.6" x2="86" y2="74" stroke="#4a4d56" strokeWidth="0.8" />
 <line x1="89" y1="79.6" x2="89" y2="76" stroke="#4a4d56" strokeWidth="0.8" />
 <line x1="92" y1="79.6" x2="92" y2="73" stroke="#4a4d56" strokeWidth="0.8" />
 <line x1="86" y1="74" x2="92" y2="74" stroke="#4a4d56" strokeWidth="0.5" />

 {/* Status light — small green dot pulses */}
 <circle cx="97" cy="83" r="0.5" fill="#5cf08a" className="hq-v2-generator__status" />
 </svg>
 );
}

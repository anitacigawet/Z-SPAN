import { useState } from "react";
import SettingsCloudPanel from "./SettingsCloudPanel";

type CloudDividerProps = {
 /** Active fiber-optic variant id — forwarded into the settings panel
 * so the variant switcher controls render with the current selection. */
 variantId?: string;
 /** Variant change handler — forwarded into the settings panel. */
 onVariantChange?: (id: string) => void;
};

/**
 * Horizontal cloud band at the Skybox/scene-wrap seam.
 *
 * Two jobs (per HQ_SECOND_MONITOR_AND_ONBOARDING_NOTES.md § 1 + § 2 +
 * James's chunk-7 design call 2026-05-31):
 *
 * 1. **Hide the seam.** The Skybox ends at 75vh and the scene-wrap
 * begins immediately below. Without this band the cut reads as
 * "two stitched products" rather than "one continuous scene." The
 * decorative cloud puffs extend across the seam y-coordinate so
 * the transition becomes atmospheric layer, not hairline edge.
 *
 * 2. **Host the settings entry point.** One of the puffs is the
 * *settings cloud* — distinct (slightly more luminous), clickable,
 * with a "Settings" hint that brightens on hover. Click opens the
 * SettingsCloudPanel where the variant switcher + mock-inject
 * controls (chunks 3-4) + the view-mode toggle (chunk 5) live.
 *
 * Pixel art is deliberately NOT used — pure CSS radial gradients render
 * crisply at any resolution (sidesteps the upscale-artifact problem
 * that motivates the V2 background-rebuild track) and the look ports
 * cleanly to V2 unchanged.
 *
 * Pointer events: the container has `pointer-events: none` so it never
 * intercepts clicks on the DeptZones / Entrance / Billboards underneath;
 * only the settings button claims `pointer-events: auto`.
 *
 * z-index sits between the scene-wrap (z:1) and the chrome / vignette
 * (z:40+) so it's visible without blocking foreground UI.
 */
export default function CloudDivider({
 variantId,
 onVariantChange,
}: CloudDividerProps = {}) {
 const [open, setOpen] = useState(false);

 return (
 <>
 <div className="cloud-divider" aria-hidden="true">
 <div className="cloud-divider-puff cloud-divider-puff--left-far" />
 <div className="cloud-divider-puff cloud-divider-puff--left-near" />
 <button
 type="button"
 className="cloud-divider-settings"
 aria-label="Open HQ settings"
 aria-expanded={open}
 aria-hidden="false"
 onClick={() => setOpen(true)}
 >
 <span className="cloud-divider-settings-puff" />
 <span className="cloud-divider-settings-hint">Settings</span>
 </button>
 <div className="cloud-divider-puff cloud-divider-puff--right-near" />
 <div className="cloud-divider-puff cloud-divider-puff--right-far" />
 </div>
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

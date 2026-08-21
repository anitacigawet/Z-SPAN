import "./hq.css";
import HQPage from "./HQPage";
import HQPageV2 from "./HQPageV2";

// Side-by-side compare view: V1 (image-overlay) on the left, V2 (code-built)
// on the right. Mounts when ?view=hq&compare=true. Dev-only opt-in per
// HQ_V2_BACKGROUND_REBUILD_PLAN.md § 1 — V1 stays the default until V2-17
// graduation. The wrapper is intentionally thin: each pane just hosts its
// own page component inside a 50vw column, and the CSS override in hq.css
// (.hq-compare-pane .hq-root .scene) clamps the inner scene to the pane
// width so the building art stays inside its half.
//
// Note on fixed-position chrome (TopChrome, clock, CloudDivider, Skybox,
// Ganymede): V1 renders these with position:fixed, so they currently span
// the full viewport across both panes. This is a known V2-1 limitation —
// V2 has no fixed chrome of its own yet (the scaffolding placeholder is
// flow-positioned), so V1's chrome simply sits on top across both halves.
// V2-15 (overlay re-target) revisits this if it becomes visually distracting.
export default function HQCompare({
  onNavigate,
}: {
  onNavigate: (view: string, params?: unknown) => void;
}) {
  return (
    <div className="hq-compare" data-mode="side-by-side">
      <div className="hq-compare-pane hq-compare-pane--v1">
        <div className="hq-compare-label" aria-label="V1 — image overlay">
          V1 · image overlay
        </div>
        <HQPage onNavigate={onNavigate} />
      </div>
      <div className="hq-compare-pane hq-compare-pane--v2">
        <div className="hq-compare-label" aria-label="V2 — code-built">
          V2 · code-built
        </div>
        <HQPageV2 onNavigate={onNavigate} />
      </div>
    </div>
  );
}

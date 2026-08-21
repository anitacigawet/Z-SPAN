import { useEffect } from "react";
import VariantSwitcherControls from "./VariantSwitcherControls";
import MockInjectControls from "./MockInjectControls";
import { OwnerOnly } from "@/components/OwnerOnly";
import { useHQModeContext, type HQMode } from "@/components/hq/v2/HQMode";

const HQ_MODE_OPTIONS: { value: HQMode; label: string; description: string }[] = [
  { value: "v2", label: "V2", description: "Composite — default" },
  { value: "v1", label: "V1", description: "Painted image — legacy" },
  { value: "compare", label: "Compare", description: "V1 left, V2 right" },
];

/**
 * The HQ settings panel — opens when the settings cloud is clicked.
 *
 * Deliberately tiny per HQ_SECOND_MONITOR_AND_ONBOARDING_NOTES.md § 2
 * — for the ambient-second-monitor viewer who wants to fine-tune what
 * they're looking at without leaving the page. Hosts:
 *
 *   - Variant switcher (chunk 3)
 *   - HQ render mode picker — V1 / V2 / Compare (V2-17c, owner-only,
 *     rendered from useHQModeContext when present)
 *   - Mock-inject controls (owner-only)
 *
 * Click-away backdrop is `position: fixed inset: 0` over a translucent
 * dim — so the user can dismiss by clicking anywhere outside the
 * panel itself. Escape also closes.
 */
export default function SettingsCloudPanel({
  onClose,
  variantId,
  onVariantChange,
}: {
  onClose: () => void;
  variantId?: string;
  onVariantChange?: (id: string) => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const hasVariantControls = !!variantId && !!onVariantChange;
  const modeCtx = useHQModeContext();

  return (
    <div
      className="settings-cloud-overlay"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="settings-cloud-panel"
        role="dialog"
        aria-modal="true"
        aria-label="HQ settings"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="settings-cloud-head">
          <span>HQ Settings</span>
          <button
            type="button"
            className="settings-cloud-close"
            onClick={onClose}
            aria-label="Close settings"
          >
            ×
          </button>
        </div>
        <div className="settings-cloud-body">
          {hasVariantControls && (
            <section className="settings-cloud-section">
              <VariantSwitcherControls
                variantId={variantId}
                onVariantChange={onVariantChange}
              />
            </section>
          )}
          <OwnerOnly>
            <div className="settings-cloud-owner-eyebrow">Developer</div>
            {modeCtx && (
              <section className="settings-cloud-section settings-cloud-section--owner">
                <div className="settings-cloud-mode">
                  <div className="settings-cloud-mode-head">Render mode</div>
                  <div
                    className="settings-cloud-mode-group"
                    role="radiogroup"
                    aria-label="HQ render mode"
                  >
                    {HQ_MODE_OPTIONS.map((opt) => {
                      const active = modeCtx.mode === opt.value;
                      return (
                        <button
                          key={opt.value}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          className={`settings-cloud-mode-btn${active ? " is-active" : ""}`}
                          onClick={() => modeCtx.setMode(opt.value)}
                        >
                          {opt.label}
                        </button>
                      );
                    })}
                  </div>
                  <div className="settings-cloud-mode-foot">
                    {HQ_MODE_OPTIONS.find((o) => o.value === modeCtx.mode)
                      ?.description}
                  </div>
                </div>
              </section>
            )}
            <section className="settings-cloud-section settings-cloud-section--owner">
              <MockInjectControls />
            </section>
          </OwnerOnly>
        </div>
      </div>
    </div>
  );
}

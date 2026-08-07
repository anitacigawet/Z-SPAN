import { VARIANTS } from "./StarField";

/**
 * The fiber-optic variant switcher, decoupled from positioning.
 *
 * Renders just the rows (header + button list + footnote). The original
 * VariantSwitcherPanel wrapped these in a fixed-positioned floating box
 * at bottom-left; chunk 3 of the HQ V1 polish (2026-05-31) retires that
 * always-on UI in favor of embedding these controls inside the
 * SettingsCloudPanel (the click-the-cloud-to-open modal).
 *
 * Same visual language as the variant rows had before — terminal mono,
 * 2px corners, `›` active-row prefix. The footnote that previously
 * read "Use MockInject (bottom-right) to fire test stars" is updated
 * to reflect the consolidated layout (mock-inject now also lives in
 * this panel as of chunk 4).
 */
export default function VariantSwitcherControls({
 variantId,
 onVariantChange,
}: {
 variantId: string;
 onVariantChange: (id: string) => void;
}) {
 return (
 <div className="variant-controls">
 <div className="variant-controls-head">Fiber-optic variant</div>
 <div className="variant-controls-list">
 {VARIANTS.map((v) => {
 const active = v.id === variantId;
 return (
 <button
 key={v.id}
 type="button"
 className={`variant-controls-row ${active ? "is-active" : ""}`}
 onClick={() => onVariantChange(v.id)}
 >
 <span className="variant-controls-prefix">
 {active ? "›" : " "}
 </span>
 {v.display_name}
 </button>
 );
 })}
 </div>
 </div>
 );
}

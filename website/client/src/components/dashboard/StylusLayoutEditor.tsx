/**
 * Stylus Layout Editor (2026-07-03) — operator-driven pixel-precise
 * overlay positioning for the City Dashboard.
 *
 * The methodology: instead of Claude guessing at painted-panel bounds
 * via mask scans / eyeball measurements, the operator toggles ?layout=1,
 * drags + resizes each overlay with their stylus to pixel-perfect
 * position over the painted panel, and Claude reads the final coords
 * out of the DOM and locks them into the CSS.
 *
 * Precision wins:
 * - Painted panels have decorative frames + shadow + inner body —
 * no single "correct" edge exists in the source PNG
 * - Operator has direct stylus input; can align by eye, not math
 * - Round-trip is one screenshot vs many attempts
 *
 * See `.claude/skills` for the cross-project pattern.
 */
import { useCallback, useEffect, useState } from "react";

export interface PanelRect {
 top: number; // percent of scene container
 left: number;
 width: number;
 height: number;
}

type RectKey = "nav" | "weather" | "market" | "player" | "news";

interface Props {
 active: boolean;
 panels: Record<RectKey, PanelRect>;
 onChange: (key: RectKey, rect: PanelRect) => void;
}


export default function StylusLayoutEditor({ active, panels, onChange }: Props) {
 const [selected, setSelected] = useState<RectKey | null>(null);
 const [copied, setCopied] = useState(false);

 const emit = useCallback(() => {
 const css = (Object.entries(panels) as [RectKey, PanelRect][])
 .map(
 ([k, r]) =>
 `.cdash-${k} { top: ${r.top.toFixed(2)}%; left: ${r.left.toFixed(2)}%; width: ${r.width.toFixed(2)}%; height: ${r.height.toFixed(2)}%; }`,
 )
 .join("\n");
 navigator.clipboard?.writeText(css).then(() => {
 setCopied(true);
 setTimeout(() => setCopied(false), 1600);
 });
 }, [panels]);

 if (!active) return null;

 return (
 <div className="cdash-layout-editor" aria-label="Stylus layout editor">
 <div className="cdash-layout-editor-title">LAYOUT · STYLUS EDIT</div>
 <div className="cdash-layout-editor-hint">
 Drag any panel to move · pull the ◢ handle to resize · click panel
 name to focus. Coords live-update below.
 </div>
 <ul className="cdash-layout-editor-list">
 {(Object.keys(panels) as RectKey[]).map(k => {
 const r = panels[k];
 const isSel = selected === k;
 return (
 <li
 key={k}
 className={isSel ? "is-selected" : ""}
 onClick={() => setSelected(k)}
 >
 <div className="cdash-layout-editor-key">
 {k}
 </div>
 <div className="cdash-layout-editor-vals">
 <span>top {r.top.toFixed(2)}%</span>
 <span>left {r.left.toFixed(2)}%</span>
 <span>w {r.width.toFixed(2)}%</span>
 <span>h {r.height.toFixed(2)}%</span>
 </div>
 </li>
 );
 })}
 </ul>
 <button
 type="button"
 className="cdash-layout-editor-copy"
 onClick={emit}
 >
 {copied ? "✓ CSS copied" : "Copy CSS to clipboard"}
 </button>
 <div className="cdash-layout-editor-hint" style={{ fontSize: 9.5 }}>
 When you say "perfect," I read these values from the DOM and
 lock them into <code>city-dashboard.css</code>.
 </div>
 </div>
 );
}

/* EditableWrapper was drafted here (2026-07-03) as a wrapper-component
 * approach to bolt drag+resize onto each panel. Pivoted to the inline
 * bindEditable() helper in CityDashboardPage.tsx before shipping — the
 * wrapper collided with panels that carry their own semantics (role,
 * aria-label). This component was never imported anywhere in the codebase
 * and was retired 2026-07-03 during the handoff audit-fix sweep. */

/** URL flag: `?layout=1` turns edit mode on. Persisted to localStorage
 * so a reload after the App.tsx URL sync (which drops unknown params)
 * keeps the operator in edit mode. Clearing localStorage or landing
 * without ?layout=1 AND no prior flag turns it back off. */
const LAYOUT_MODE_KEY = "zspan.cdash.layoutMode";

export function useLayoutEditMode(): boolean {
 const [on, setOn] = useState(false);
 useEffect(() => {
 if (typeof window === "undefined") return;
 const sp = new URLSearchParams(window.location.search);
 // Explicit disable: ?layout=0 wipes the persisted flag AND the saved
 // positions, so the dashboard returns to its committed CSS values.
 if (sp.get("layout") === "0") {
 window.localStorage.removeItem(LAYOUT_MODE_KEY);
 window.localStorage.removeItem(PANELS_KEY);
 setOn(false);
 return;
 }
 if (sp.get("layout") === "1") {
 window.localStorage.setItem(LAYOUT_MODE_KEY, "1");
 setOn(true);
 return;
 }
 setOn(window.localStorage.getItem(LAYOUT_MODE_KEY) === "1");
 }, []);
 return on;
}

/** Load persisted panel rects (if any) — used to seed useState so a
 * page reload doesn't lose the operator's in-progress positioning. */
const PANELS_KEY = "zspan.cdash.panels.v1";

export function loadPersistedPanels<
 T extends Record<string, PanelRect>,
>(defaults: T): T {
 if (typeof window === "undefined") return defaults;
 try {
 const raw = window.localStorage.getItem(PANELS_KEY);
 if (!raw) return defaults;
 const saved = JSON.parse(raw) as T;
 // Merge saved onto defaults so new keys pick up their initial rect
 return { ...defaults, ...saved };
 } catch {
 return defaults;
 }
}

/** Save panel rects — called whenever the operator drags/resizes. */
export function savePersistedPanels(panels: Record<string, PanelRect>): void {
 if (typeof window === "undefined") return;
 try {
 window.localStorage.setItem(PANELS_KEY, JSON.stringify(panels));
 } catch {
 /* private-mode / quota — ignore */
 }
}

/** Emit a small "Reset" button in the editor's sidecar so the operator
 * can wipe both localStorage entries and start over. */
export function clearPersistedLayout(): void {
 if (typeof window === "undefined") return;
 window.localStorage.removeItem(PANELS_KEY);
 window.localStorage.removeItem(LAYOUT_MODE_KEY);
}

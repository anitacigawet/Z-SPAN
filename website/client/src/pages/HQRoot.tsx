import HQPage from "./HQPage";
import HQPageV2 from "./HQPageV2";
import HQCompare from "./HQCompare";
import {
 useHQMode,
 HQModeProvider,
 type HQMode,
} from "@/components/hq/v2/HQMode";

/**
 * HQ root dispatcher — picks which HQ surface to render based on the owner's
 * stored mode preference. Three modes:
 * - v2 : the composite (default since V2-17c, 2026-06-02)
 * - v1 : the legacy painted image (?legacy=true opt-in)
 * - compare : side-by-side V1 + V2 (dev view, ?compare=true opt-in)
 *
 * Mode lives in localStorage via useHQMode(). The `urlForcedMode` prop
 * carries an explicit URL override and wins over the stored value for THIS
 * session only — without clobbering the persisted choice. This lets dev
 * links work without breaking the operator's normal mode preference.
 *
 * V2-17c moved the mode picker INTO the SettingsCloudPanel — HQRoot now
 * provides the mode context, and the panel renders the V1/V2/Compare picker
 * alongside the variant switcher + mock-inject controls.
 */
export default function HQRoot({
 onNavigate,
 urlForcedMode,
}: {
 onNavigate: (view: string, params?: unknown) => void;
 urlForcedMode?: HQMode;
}) {
 const [mode, setMode] = useHQMode(urlForcedMode);

 return (
 <HQModeProvider value={{ mode, setMode }}>
 {mode === "compare" && <HQCompare onNavigate={onNavigate} />}
 {mode === "v2" && <HQPageV2 onNavigate={onNavigate} />}
 {mode === "v1" && <HQPage onNavigate={onNavigate} />}
 </HQModeProvider>
 );
}

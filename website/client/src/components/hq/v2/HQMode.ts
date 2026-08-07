import { createContext, useCallback, useContext, useState } from "react";

// HQ mode — which version of the headquarters surface the operator wants to
// see. V2-17c graduation (2026-06-02): default flipped from "v1" to "v2"
// so first-time visitors land on the composite. `v1` survives as an opt-in
// for one release cycle via the `?legacy=true` URL flag. `compare` is the
// side-by-side dev view.
export type HQMode = "v1" | "v2" | "compare";

const STORAGE_KEY = "zspan.hq.v2.mode";

function readStored(): HQMode {
 try {
 const raw = window.localStorage.getItem(STORAGE_KEY);
 if (raw === "v1" || raw === "v2" || raw === "compare") return raw;
 } catch {
 // localStorage unavailable (private browsing); fall through to default.
 }
 return "v2";
}

/**
 * useHQMode — owner-facing toggle between V1 (painted image, legacy), V2
 * (composite, default), and compare (side-by-side). Persists in localStorage
 * so the choice survives reloads. The optional `urlForcedMode` argument lets
 * a URL flag (`?compare=true` or `?legacy=true`) override the stored
 * preference for the current load without clobbering it — useful for sharing
 * a dev preview link.
 */
export function useHQMode(urlForcedMode?: HQMode) {
 const [mode, setModeState] = useState<HQMode>(() => {
 if (urlForcedMode) return urlForcedMode;
 if (typeof window === "undefined") return "v2";
 return readStored();
 });

 const setMode = useCallback((next: HQMode) => {
 setModeState(next);
 try {
 window.localStorage.setItem(STORAGE_KEY, next);
 } catch {
 // Quota / private browsing — in-memory state still works for this session.
 }
 }, []);

 return [mode, setMode] as const;
}

/**
 * HQ mode context — provided by HQRoot, consumed inside SettingsCloudPanel.
 * V2-17c (2026-06-02): the mode picker is no longer fixed top chrome — it
 * lives alongside the other dev tweaks (variant switcher, mock-inject) in the
 * settings cloud panel.
 */
interface HQModeContextValue {
 mode: HQMode;
 setMode: (next: HQMode) => void;
}

const HQModeContext = createContext<HQModeContextValue | null>(null);

export const HQModeProvider = HQModeContext.Provider;

export function useHQModeContext(): HQModeContextValue | null {
 return useContext(HQModeContext);
}

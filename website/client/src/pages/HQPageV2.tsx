import { useEffect, useRef, useState } from "react";
import "./hq.css";
import Skybox from "@/components/hq/Skybox";
import { DEFAULT_VARIANT_ID } from "@/components/hq/skybox/StarField";
import Ganymede from "@/components/hq/skybox/Ganymede";
import V2Overlays from "@/components/hq/v2/V2Overlays";
import FogBand from "@/components/hq/v2/layers/FogBand";
import ThankYouClouds from "@/components/hq/v2/layers/ThankYouClouds";

/**
 * Local-workspace processing indicator (zspan CLI `open` mode only —
 * the local server self-identifies via /api/system/status, which
 * zspan.org never does). While the user's own pipeline runs, the HQ
 * doubles as the watch-it-work page: every step fires the skybox's
 * fiber-optic stars, and this pill in the corner names the run and
 * links to its broadcast page.
 */
function LocalProcessingIndicator({
 onNavigate,
}: {
 onNavigate: (view: string, params?: unknown) => void;
}) {
 const [localMode, setLocalMode] = useState(false);
 const [active, setActive] = useState<{
 meeting_id: number | null;
 running: boolean;
 done: boolean;
 ok: boolean | null;
 city?: string;
 meeting_date?: string;
 } | null>(null);

 useEffect(() => {
 let cancelled = false;
 fetch("/api/system/status")
 .then(r => r.json())
 .then(d => {
 if (!cancelled && d && d.mode === "local-workspace") setLocalMode(true);
 })
 .catch(() => {});
 return () => { cancelled = true; };
 }, []);

 useEffect(() => {
 if (!localMode) return;
 let cancelled = false;
 const poll = () => {
 fetch("/api/local/process/active")
 .then(r => r.json())
 .then(d => { if (!cancelled && d) setActive(d); })
 .catch(() => {});
 };
 poll();
 const timer = window.setInterval(poll, 2500);
 return () => { cancelled = true; window.clearInterval(timer); };
 }, [localMode]);

 if (!localMode || !active || active.meeting_id == null) return null;

 const where = [active.city, active.meeting_date].filter(Boolean).join(" · ");
 const base: React.CSSProperties = {
 position: "fixed",
 right: 18,
 bottom: 18,
 zIndex: 40,
 display: "flex",
 alignItems: "center",
 gap: 10,
 padding: "10px 16px",
 borderRadius: 999,
 background: "rgba(10, 10, 14, 0.9)",
 boxShadow: "0 6px 24px rgba(0,0,0,0.5)",
 fontSize: 12.5,
 lineHeight: 1.35,
 };

 if (active.running) {
 return (
 <button
 type="button"
 onClick={() =>
 onNavigate("broadcast", { meetingId: active.meeting_id })
 }
 title={`Processing ${where || "a meeting"} — open its broadcast page`}
 style={{
 ...base,
 border: "1px solid rgba(34,197,94,0.45)",
 color: "rgba(255,255,255,0.9)",
 cursor: "pointer",
 }}
 >
 <span
 style={{
 width: 8,
 height: 8,
 borderRadius: "50%",
 background: "#22C55E",
 boxShadow: "0 0 10px rgba(34,197,94,0.9)",
 animation: "hq-pulse 1.6s ease-in-out infinite",
 flexShrink: 0,
 }}
 />
 <span>
 Processing {where || "a meeting"} — open its broadcast page
 </span>
 </button>
 );
 }
 if (active.done && active.ok) {
 return (
 <button
 type="button"
 onClick={() =>
 onNavigate("broadcast", { meetingId: active.meeting_id })
 }
 style={{
 ...base,
 border: "1px solid rgba(34,197,94,0.45)",
 color: "#86EFAC",
 cursor: "pointer",
 }}
 >
 Broadcast ready — {where || "your meeting"} · watch it →
 </button>
 );
 }
 if (active.done && active.ok === false) {
 return (
 <button
 type="button"
 onClick={() =>
 onNavigate("broadcast", { meetingId: active.meeting_id })
 }
 style={{
 ...base,
 border: "1px solid rgba(248,113,113,0.45)",
 color: "rgba(252,165,165,0.9)",
 cursor: "pointer",
 }}
 >
 Processing hit trouble — open {where || "the meeting"} for the log →
 </button>
 );
 }
 return null;
}

// HQ V2 — the COMPOSITE approach (James 2026-06-01 pivot).
//
// The code-built rebuild (V2-1 through V2-16b) was thrown out as
// "terrible" after two rounds of push-back from James. What he kept
// from it: the dusk sky gradient on .scene-wrap (warm horizon band
// matching V1's painted dusk). What he wanted: V1's painted building
// — the part he liked all along — composited over that gradient.
//
// The transparent-sky PNG comes from running `rembg` on the original
// zspan-hq.png — produces zspan-hq-transparent.png (RGBA, 1376×768,
// painted dusk sky replaced with alpha=0, building + plaza intact).
// The V2 sky gradient on .scene-wrap shows through where the painted
// sky was; the building stays at V1 fidelity because it IS the V1
// art, just with its sky cut out.
//
// The old V2 layer components (Mountains, BuildingSilhouette,
// BuildingDepth, TierWindows, ZSpanSign, BillboardFrames, GroundFloor,
// GeneratorOutbuilding, PressVignette, Plaza) stay on disk under
// `components/hq/v2/layers/` but are not imported anywhere — quarantined
// per James's instruction so the chunk-by-chunk history is preserved
// in case we ever revisit the from-primitives approach.
export default function HQPageV2({
 onNavigate,
}: {
 onNavigate: (view: string, params?: unknown) => void;
}) {
 // Skybox lives ABOVE the scene-wrap in flow, matching V1's HQPage.
 // Setter is wired so the SettingsCloudPanel (opened from the FogBand
 // click) can swap the StarField variant just like V1's CloudDivider.
 const [skyboxVariant, setSkyboxVariant] = useState<string>(DEFAULT_VARIANT_ID);

 // look-up-hint — V2-17b (2026-06-02). HQPageV2 auto-scrolls to the
 // building view on mount, so a fresh user has no signal that there's a
 // sky / FogBand / settings world above. The chevron hint surfaces that
 // affordance until the first scroll, then hides. Mirrors V1's pattern.
 const [hasScrolled, setHasScrolled] = useState(false);

 // `?bg=only` mounts the composite without overlays — the naked photo
 // for hunting pixel-level artifacts that production UI would normally
 // hide behind tickers, dept zones, infra annotations, etc. James 2026-06-02.
 const naked =
 typeof window !== "undefined" &&
 new URLSearchParams(window.location.search).get("bg") === "only";

 // Sky visibility gate (perf pass 2026-07-02): the page lands scrolled to
 // the building, leaving the entire sky offscreen — yet the StarField rAF,
 // the FogBand's two animated turbulence filters and the thank-you drift
 // all kept running. Pause them offscreen; resume on
 // scroll-up. Zero quality change while visible.
 //
 // NOT an IntersectionObserver: at common aspect ratios the document's
 // max-scroll puts the sky's bottom edge at exactly viewport y=0 (edge-
 // touching counts as intersecting per spec), so IO read the sky as
 // permanently visible. A scroll-driven bottom-edge compute with a 40px
 // threshold is
 // deterministic; same-value setState makes per-tick calls free.
 const [skyVisible, setSkyVisible] = useState(true);
 const skySentinelRef = useRef<HTMLDivElement | null>(null);
 useEffect(() => {
 const el = skySentinelRef.current;
 if (!el) return;
 const compute = () => {
 const bottom = el.getBoundingClientRect().bottom;
 setSkyVisible(bottom > 40);
 };
 compute();
 window.addEventListener("scroll", compute, { passive: true });
 window.addEventListener("resize", compute, { passive: true });
 return () => {
 window.removeEventListener("scroll", compute);
 window.removeEventListener("resize", compute);
 };
 }, []);

 // Land at the building view (scene-wrap) on mount + listen for first
 // scroll so the look-up-hint can dismiss itself.
 //
 // Bug fix 2026-07-02 (caught during the thank-you-sky pass):
 // these were one effect with [hasScrolled] as its dependency — so the
 // visitor's FIRST upward scroll flipped the state, re-ran the effect, and
 // the scrollTo yanked them straight back down to the building. Splitting
 // mount-scroll (once) from the listener (stable) removes the yank.
 useEffect(() => {
 window.scrollTo({
 top: document.documentElement.scrollHeight,
 behavior: "instant",
 });
 }, []);
 useEffect(() => {
 const onScroll = () => setHasScrolled(true);
 window.addEventListener("scroll", onScroll, { passive: true });
 return () => window.removeEventListener("scroll", onScroll);
 }, []);

 return (
 <div
 className={`hq-root hq-root--v2${skyVisible ? "" : " sky-offscreen"}`}
 >
 <div ref={skySentinelRef}>
 <Skybox variantId={skyboxVariant} visible={skyVisible} />
 </div>
 {/* "Thank you for visiting!" farewell sky — cloud-lettered text +
 * civic-palette long-exposure streaks + golden octagon spirals
 * (V2-fog-2 realized from the operator's Gemini mock, 2026-07-02).
 * pointer-events:none throughout; FogBand keeps the Settings click. */}
 {!naked && <ThankYouClouds />}
 {/* Hypnotic SF-fog band at the Skybox/scene-wrap seam — hides the
 * horizon line + carries the Settings click target (whole band is
 * clickable per James 2026-06-02). Skipped in `?bg=only` naked
 * mode so the underlying composite stays inspectable. */}
 {!naked && (
 <FogBand
 variantId={skyboxVariant}
 onVariantChange={setSkyboxVariant}
 />
 )}

 {/* Ganymede — the moon in the top-right of the skybox. */}
 {!naked && <Ganymede />}

 <div className="scene-wrap">
 <div className="scene">
 {/* 2026-07-03: the rembg composite (zspan-hq-transparent.png) was
 * replaced by the operator's full-scene repaint — painted sky,
 * tickers, press board, shack and all, at 2x resolution
 * (2750x1536, same 1.79 aspect). No background strip needed;
 * lossless WebP. The old composite + fixup pipeline stay on
 * disk for the V1 page + history. */}
 <img
 className="bg"
 src="/hq/zspan-hq-scene.webp?v=2"
 alt="Z-SPAN Headquarters at dusk (painted scene)"
 />
 {!naked && <V2Overlays onNavigate={onNavigate} />}
 </div>
 </div>

 {/* "scroll up to see the sky" affordance — only the chevron itself
 * shows. Hides after the first user scroll. V2-17b (2026-06-02). */}
 {!naked && (
 <div
 className={`look-up-hint ${hasScrolled ? "hidden" : ""}`}
 aria-label="Scroll up to see the sky"
 >
 <span className="chev">↑</span>
 </div>
 )}

 {/* Local-workspace only: the corner pill naming the live pipeline
 * run. zspan.org never identifies as local-workspace, so this
 * renders nothing there. */}
 {!naked && <LocalProcessingIndicator onNavigate={onNavigate} />}
 </div>
 );
}

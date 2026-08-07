import { useEffect, useMemo, useState } from "react";
import "./hq.css";
import { useHQDataState } from "@/utils/hqData";
import { HQ_BILLBOARDS } from "@/utils/hqBillboards";
import type { DeptZoneSpec, Rect } from "@/components/hq/hqHelpers";
import TopChrome from "@/components/hq/TopChrome";
import Billboard from "@/components/hq/Billboard";
import Entrance from "@/components/hq/Entrance";
import DeptZone from "@/components/hq/DeptZone";
import InfraPanel from "@/components/hq/InfraPanel";
import PressScreen from "@/components/hq/PressScreen";
import Skybox from "@/components/hq/Skybox";
import CloudDivider from "@/components/hq/skybox/CloudDivider";
import Ganymede from "@/components/hq/skybox/Ganymede";
import { DEFAULT_VARIANT_ID } from "@/components/hq/skybox/StarField";

// Overlay coordinates — percentages of the underlying 1376×768 art, so they
// scale with the image. Tuned against client/public/hq/zspan-hq.png.
//
// 2026-05-31 (HQ V1 chunk 6): upper + lower band coords re-aligned to the
// actual painted "BREAKING NEWS: CITYWIDE UPDATE" billboards in the photo.
// Previous coords (top 45.0/60.8, left 17.0, width 64.5, height 7.6) sat
// too high + too wide + too tall — the cyan LED text frame was visibly
// offset above and beyond the photo's painted billboard. New coords
// measured by hiding the ticker overlays + reading viewport pixel
// positions of the photo's billboards at scene size 1401×782.
const COORDS: Record<string, Rect> = {
 upperBand: { top: 48.6, left: 19.9, width: 58.8, height: 5.7 },
 lowerBand: { top: 62.0, left: 19.6, width: 58.5, height: 6.4 },
 entrance: { top: 86.6, left: 46.0, width: 8.1, height: 11.7 },
 pressScreen: { top: 75.4, left: 1.4, width: 17.5, height: 10.5 },
 infraPanel: { top: 71.0, left: 84.0, width: 14.8, height: 17.5 },
};

// Department zones placed by corporate seniority — important departments
// higher up, support on the ground floor — each over the building's real
// window blocks. side = which way the hover callout opens. (grid is only used
// in the legacy window-grid mode; zones render as a state-glow by default.)
//
// 2026-05-31 (HQ V1 chunk 7): vocabulary-curator + verification narrowed and
// pulled inward to fit the upper tower's NARROW silhouette (was clipping into
// the sky at left 24-37% and right 63-76% — the upper tower only spans about
// 36-65% horizontally at that height). Ingestion's vertical range nudged
// down to sit cleanly between the (newly-aligned chunk-6) billboards rather
// than overlapping the upper one. Ground-floor zones unchanged — they sit on
// the building's WIDER ground floor and weren't clipping.
const DEPT_ZONES: DeptZoneSpec[] = [
 // Penthouse — leadership
 { deptId: "pipeline-operator", top: 15.5, left: 42.5, width: 15.0, height: 9.0, side: "bottom", grid: [6, 2] },
 // Senior floor (above the upper billboard) — judgment + integrity
 // VOCAB + VERIFY pulled inward (was 24/63 width 13 → 36/57 width 7) so they
 // sit within the narrow tower silhouette instead of bleeding into the sky.
 { deptId: "vocabulary-curator", top: 34.0, left: 36.0, width: 7.0, height: 11.0, side: "right", grid: [3, 4] },
 { deptId: "disputed-quotes-reviewer", top: 27.5, left: 42.0, width: 16.0, height: 16.5, side: "bottom", grid: [6, 4] },
 { deptId: "verification", top: 34.0, left: 57.0, width: 7.0, height: 11.0, side: "left", grid: [3, 4] },
 // Intake band (between the billboards) — vertical range tightened so it
 // sits AFTER the upper billboard ends (54.3%) and before the lower starts
 // (62.0%) per the chunk-6 billboard coords.
 { deptId: "ingestion", top: 54.5, left: 38.0, width: 24.0, height: 7.0, side: "bottom", grid: [10, 2] },
 // Ground floor — production + support. Building widens here; these fit.
 { deptId: "content-scout", top: 69.5, left: 24.0, width: 13.0, height: 15.5, side: "right", grid: [5, 4] },
 { deptId: "transcription", top: 69.5, left: 37.5, width: 11.5, height: 15.5, side: "top", grid: [5, 4] },
 { deptId: "notebooklm-bridge", top: 69.5, left: 50.5, width: 11.5, height: 15.5, side: "top", grid: [5, 4] },
 { deptId: "parser-custodian", top: 69.5, left: 63.0, width: 13.0, height: 15.5, side: "left", grid: [5, 4] },
];

export default function HQPage({
 onNavigate,
}: {
 onNavigate: (view: string, params?: unknown) => void;
}) {
 const { data, isLoading, isFallback } = useHQDataState();
 const [flashing, setFlashing] = useState(false);
 const [hasScrolled, setHasScrolled] = useState(false);
 // V4 (2026-05-31): live A/B-test which fiber-optic variant lands best.
 // Delete this state + the VariantSwitcherPanel + the losing variants
 // in StarField.ts once James picks the winner.
 const [skyboxVariant, setSkyboxVariant] = useState<string>(DEFAULT_VARIANT_ID);

 // Default scroll position is the BUILDING (bottom of the page). The
 // .hq-root container is taller than the viewport but doesn't scroll
 // internally (its content exactly fills its own min-height), so the
 // WINDOW is what scrolls — fix the scroll target accordingly. The
 // chevron-only "look up" hint surfaces the scroll-up affordance
 // until the user moves the page once.
 // Bug fix 2026-07-02 (): was one effect keyed on [hasScrolled] —
 // the visitor's first upward scroll re-ran it and the scrollTo yanked them
 // back down to the building. Mount-scroll runs once; listener is stable.
 useEffect(() => {
 // Bottom of the document — shows the scene-wrap fully (building view).
 window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" });
 }, []);
 useEffect(() => {
 const onScroll = () => setHasScrolled(true);
 window.addEventListener("scroll", onScroll, { passive: true });
 return () => window.removeEventListener("scroll", onScroll);
 }, []);

 const deptById = useMemo(() => {
 const m: Record<string, (typeof data.departments)[number]> = {};
 data.departments.forEach((d) => {
 m[d.id] = d;
 });
 return m;
 }, [data.departments]);

 const coreDown = data.infrastructure.services.some(
 (s) => s.isCore && s.status === "down",
 );
 const upperBand = HQ_BILLBOARDS.find((b) => b.id === "upper-band");
 const lowerBand = HQ_BILLBOARDS.find((b) => b.id === "lower-band");

 // Brief warm flash, then enter the channel browser.
 const handleEnter = () => {
 setFlashing(true);
 setTimeout(() => {
 setFlashing(false);
 onNavigate("home");
 }, 380);
 };

 return (
 <div className={`hq-root ${isLoading ? "hq-loading" : ""}`}>
 <TopChrome
 buildingStatus={isFallback ? null : data.building.overallStatus}
 />

 <Skybox variantId={skyboxVariant} />

 {/* Cloud divider at the skybox/scene-wrap seam — hides the hairline
 cut AND hosts the settings entry point (HQ V1 chunks 1+2).
 Chunk 3 (2026-05-31): variant switcher consolidated into the
 settings panel; the always-on bottom-left VariantSwitcherPanel
 is retired. Variants are viewer-tunable per HQ_NOTES § 2 — no
 owner gate on the variant section. */}
 <CloudDivider
 variantId={skyboxVariant}
 onVariantChange={setSkyboxVariant}
 />

 {/* Ganymede — the moon in the top-right of the skybox. */}
 <Ganymede />

 {isLoading && (
 <div className="hq-status-tag hq-status-tag--loading" aria-live="polite">
 Loading live status…
 </div>
 )}
 {!isLoading && isFallback && (
 <div className="hq-status-tag hq-status-tag--mock" aria-live="polite">
 Live feed unavailable
 </div>
 )}

 <div className="scene-wrap">
 <div className="scene">
 <img className="bg" src="/hq/zspan-hq.png" alt="Z-SPAN Headquarters at dusk" />

 {coreDown && <div className="maintenance-tint on" />}

 {upperBand && (
 <Billboard rect={COORDS.upperBand} slides={upperBand.slides} accent="neon" durationSec={42} />
 )}
 {lowerBand && (
 <Billboard rect={COORDS.lowerBand} slides={lowerBand.slides} accent="amber" durationSec={48} />
 )}

 <PressScreen rect={COORDS.pressScreen} funding={data.funding} />
 <InfraPanel rect={COORDS.infraPanel} services={data.infrastructure.services} />

 {DEPT_ZONES.map((z) => (
 <DeptZone key={z.deptId} zone={z} dept={deptById[z.deptId]} />
 ))}

 <Entrance rect={COORDS.entrance} onEnter={handleEnter} />

 {/* Entrance hint — sits above the doors as a label rather than
 floating at the viewport bottom. Same scene coordinate space
 as the entrance + all overlays, so it tracks the building's
 scale on resize. */}
 <div
 className="entrance-hint"
 style={{
 top: `${COORDS.entrance.top - 4.2}%`,
 left: `${COORDS.entrance.left + COORDS.entrance.width / 2}%`,
 }}
 >
 Hover the windows · Click the doors
 </div>
 </div>
 </div>

 <div
 className={`look-up-hint ${hasScrolled ? "hidden" : ""}`}
 aria-label="Scroll up to see the sky"
 >
 <span className="chev">↑</span>
 </div>

 {/* The always-on VariantSwitcherPanel (chunk 3) AND the always-on
 MockInjectPanel (chunk 4) are both retired in HQ V1 polish
 (2026-05-31). Both consolidate into the settings cloud panel
 above (CloudDivider → SettingsCloudPanel → VariantSwitcher-
 Controls + MockInjectControls). MockInject stays owner-only;
 variants are viewer-tunable per HQ_NOTES § 2. The bottom-left
 PressScreen and the bottom-right Generator area are unblocked
 as a direct result. */}

 <div className={`enter-flash ${flashing ? "on" : ""}`} />
 </div>
 );
}

import { useState } from "react";
import { useHQDataState } from "@/utils/hqData";
import { HQ_BILLBOARDS } from "@/utils/hqBillboards";
import Billboard from "@/components/hq/Billboard";
import Entrance from "@/components/hq/Entrance";
import DeptZone from "@/components/hq/DeptZone";
import InfraPanel from "@/components/hq/InfraPanel";
import type { DeptZoneSpec } from "@/components/hq/hqHelpers";
import { OVERLAYS, DEPT_RECTS } from "./BuildingCoords";
import { useLayerVisibility } from "./LayerVisibility";
import ServiceWindowDimmer from "./ServiceWindowDimmer";
import PressBoardHover from "./PressBoardHover";
import SvgSubwayEntrance from "./SvgSubwayEntrance";
import SvgFireworkBarrel from "./SvgFireworkBarrel";
import ExternalLinkModal from "./ExternalLinkModal";
// SvgElectricPanel quarantined 2026-06-02 — James reverted to the V1
// painted generator + LEDs after both the cabinet box and the power
// substation attempts. File stays on disk but not mounted.
// PressScreen (flat panel) + SvgPodium quarantined from V2 2026-07-03 —
// the operator's full-scene repaint paints the board's broadcast content
// and the podium speaker into the base art, so the HTML versions would
// double/cover them. Both files stay on disk; V1 (HQPage) still mounts
// PressScreen over its own art. Live funding moved to PressBoardHover.

// Maintainer's external portfolio destination — surfaced by the subway
// entrance + leaving-site confirmation modal. Update this URL if the
// canonical landing target changes.
const PORTFOLIO_URL = "https://github.com/anitacigawet";

/**
 * V2-15 — mounts the V1 overlay components (Billboard ticker, DeptZones,
 * Entrance, PressScreen, InfraPanel) inside the V2 building, using V2
 * coordinates from BuildingCoords. This is the chunk that turns V2 from
 * "pretty visual" into "fully functional HQ" — every interactive
 * surface comes back, now snapped to code-built positions instead of
 * percentages of the V1 photo.
 *
 * Each component is gated by its corresponding layer visibility so the
 * operator can turn off the V2 housing AND the V1 overlay together
 * (e.g. "Billboards" hides the frames AND the tickers; "Press vignette"
 * hides the screen mount AND the funding stats).
 *
 * Per V2-15 the dept-zone coords match V1's DEPT_ZONES one-to-one
 * (DEPT_RECTS in BuildingCoords was deliberately mirrored from V1).
 * The side + grid metadata stays here since it's V1-overlay-specific.
 */

const V2_DEPT_ZONES: DeptZoneSpec[] = [
  { deptId: "pipeline-operator", ...DEPT_RECTS.pipelineOperator, side: "bottom", grid: [6, 2] },
  { deptId: "vocabulary-curator", ...DEPT_RECTS.vocabularyCurator, side: "right", grid: [3, 4] },
  { deptId: "disputed-quotes-reviewer", ...DEPT_RECTS.disputedQuotesReviewer, side: "bottom", grid: [6, 4] },
  { deptId: "verification", ...DEPT_RECTS.verification, side: "left", grid: [3, 4] },
  { deptId: "ingestion", ...DEPT_RECTS.ingestion, side: "bottom", grid: [10, 2] },
  { deptId: "content-scout", ...DEPT_RECTS.contentScout, side: "right", grid: [5, 4] },
  { deptId: "transcription", ...DEPT_RECTS.transcription, side: "top", grid: [5, 4] },
  { deptId: "synthesis", ...DEPT_RECTS.synthesis, side: "top", grid: [5, 4] },
  { deptId: "parser-custodian", ...DEPT_RECTS.parserCustodian, side: "left", grid: [5, 4] },
];

export default function V2Overlays({
  onNavigate,
}: {
  onNavigate: (view: string, params?: unknown) => void;
}) {
  const { data } = useHQDataState();
  const { visibility } = useLayerVisibility();
  const [flashing, setFlashing] = useState(false);
  const [showLeaveModal, setShowLeaveModal] = useState(false);

  const upperBand = HQ_BILLBOARDS.find((b) => b.id === "upper-band");
  const lowerBand = HQ_BILLBOARDS.find((b) => b.id === "lower-band");

  const deptById = data.departments.reduce<
    Record<string, (typeof data.departments)[number]>
  >((acc, d) => {
    acc[d.id] = d;
    return acc;
  }, {});

  // Click-the-doors flash + navigate, same shape as HQPage.handleEnter.
  const handleEnter = () => {
    setFlashing(true);
    setTimeout(() => {
      setFlashing(false);
      onNavigate("home");
    }, 380);
  };

  return (
    <>
      {/* Billboard tickers — mounted inside V2-10 frames */}
      {visibility.billboards && upperBand && (
        <Billboard rect={OVERLAYS.upperBand} slides={upperBand.slides} accent="neon" durationSec={42} />
      )}
      {visibility.billboards && lowerBand && (
        <Billboard rect={OVERLAYS.lowerBand} slides={lowerBand.slides} accent="amber" durationSec={48} />
      )}

      {/* Press smartboard — the painted scene carries the board's broadcast
       *  content + podium speaker as art (operator repaint 2026-07-03);
       *  live funding is a hover bubble in the sky above the board, same
       *  interaction grammar as the generator shack's InfraPanel. */}
      {visibility.press && (
        <PressBoardHover rect={OVERLAYS.pressScreen} funding={data.funding} />
      )}

      {/* InfraPanel — 5 LEDs baked onto the V1 painted housing top face +
       *  hover speech bubble with the full service readout. */}
      {visibility.generator && (
        <InfraPanel rect={OVERLAYS.infraPanel} services={data.infrastructure.services} />
      )}

      {/* Service-down → corresponding dept-zone windows dim. The org's
       *  degradation reads on the building face per REPROMPT_01. */}
      {visibility.building && (
        <ServiceWindowDimmer services={data.infrastructure.services} />
      )}

      {/* Whole-building maintenance tint — a global cool cast over the scene
       *  when a core service is down ("maintenance") or any service is
       *  degraded/down ("degraded"). Per REPROMPT_01: "a major outage dims
       *  the whole tower." Subtle on degraded, more pronounced on maintenance. */}
      {visibility.building && data.building.overallStatus !== "operational" && (
        <div
          className={`maintenance-tint is-${data.building.overallStatus}`}
          aria-hidden
        />
      )}

      {/* Dept zones — hover-callout hotspots over the windows. Building
       *  toggle hides them so the operator can see the bare windows. */}
      {visibility.building &&
        V2_DEPT_ZONES.map((z) => (
          <DeptZone key={z.deptId} zone={z} dept={deptById[z.deptId]} />
        ))}

      {/* Entrance — click-to-enter doors over the V2-11 lobby glow */}
      {visibility.ground && (
        <Entrance rect={OVERLAYS.entrance} onEnter={handleEnter} />
      )}

      {/* Entrance plaque — the "Hover the windows · Click the doors" cue
       *  as a gold-embossed hotel-style header board mounted on the wall
       *  just above the door lintel (operator 2026-07-02, gilded-ballroom
       *  direction — it used to float over the facade).
       *  Position + width are locked in hq.css (.hq-root .entrance-hint)
       *  from the operator's session-33 stylus-editor drag; the
       *  EntranceHintEditable component was retired the same session. */}
      {visibility.ground && (
        <div className="entrance-hint">
          Hover the windows · Click the doors
        </div>
      )}

      {/* Subway entrance — exit from the Z-SPAN ecosystem to the maintainer's
       *  external portfolio. Covers the V1 painted plaza pipe along the
       *  right ground floor (James 2026-06-02). */}
      {/* Subway entrance HIDDEN per operator 2026-07-02 (didn't like the
       *  look of it) — component + rect stay on disk per the no-delete
       *  rule; remount by restoring this block. Note: hiding re-exposes
       *  the painted ground pipe + flange it was covering. */}
      {false && (
        <SvgSubwayEntrance
          rect={OVERLAYS.subwayEntrance}
          onLeave={() => setShowLeaveModal(true)}
        />
      )}

      {/* July-4 firework barrel — seasonal; toggle off via the layer
       *  panel after the 4th. Launches double as a scroll-up lure. */}
      {visibility.fireworks && (
        <SvgFireworkBarrel rect={OVERLAYS.fireworkBarrel} />
      )}

      <ExternalLinkModal
        href={PORTFOLIO_URL}
        shown={showLeaveModal}
        onConfirm={() => {
          window.open(PORTFOLIO_URL, "_blank", "noopener,noreferrer");
          setShowLeaveModal(false);
        }}
        onCancel={() => setShowLeaveModal(false)}
      />

      <div className={`enter-flash ${flashing ? "on" : ""}`} />
    </>
  );
}

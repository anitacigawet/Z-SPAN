import { useState } from "react";
import type { FundingStatus } from "@/utils/hqData";
import type { Rect } from "@/components/hq/hqHelpers";
import { relativeFromNow } from "@/components/hq/hqHelpers";

// Press smartboard annotation for the painted-scene V2 (2026-07-03).
//
// The operator's full-scene repaint paints the smartboard's broadcast
// content (anchor portrait, copy lines, growth chart) AND the podium
// speaker directly into the base art — the hand-painted successor to the
// "custom infographic coming" placeholder the old flat panel promised.
// An opaque HTML panel would hide that art (and bisect the painted
// speaker, who overlaps the screen's lower half), so V2 moves the live
// funding readout to the generator-shack interaction grammar: invisible
// hover target over the board, speech bubble in the sky above it, dashed
// connector tying them together.
//
// V1 (HQPage) keeps the original PressScreen flat panel — its base art
// has no painted screen content to preserve.
export default function PressBoardHover({
 rect,
 funding,
}: {
 rect: Rect;
 funding: FundingStatus;
}) {
 const [hovered, setHovered] = useState(false);
 const fmtUsd = (n: number) =>
 "$" + n.toLocaleString("en-US", { maximumFractionDigits: 0 });
 const dashed = funding.restricted === true || funding.lastUpdated === null;

 return (
 <>
 {/* Invisible hover-trigger over the painted smartboard. */}
 <div
 className="press-trigger"
 style={{
 top: `${rect.top}%`,
 left: `${rect.left}%`,
 width: `${rect.width}%`,
 height: `${rect.height}%`,
 }}
 onMouseEnter={() => setHovered(true)}
 onMouseLeave={() => setHovered(false)}
 aria-label="Funding transparency"
 title={`Updated ${relativeFromNow(funding.lastUpdated)} · ${funding.source}`}
 />

 {/* Hover-revealed funding bubble, floated in the sky above the board. */}
 <div
 className={`press-bubble ${hovered ? "is-on" : ""}`}
 aria-hidden={!hovered}
 >
 <div className="press-bubble-title">
 <span>Funding · Public</span>
 {!dashed && (
 <span className="press-bubble-live">
 <span className="press-bubble-live-dot" />
 LIVE
 </span>
 )}
 </div>
 <div className="press-bubble-big">
 {dashed ? "—" : fmtUsd(funding.balanceUsd)}
 </div>
 <div className="press-bubble-row">
 <span>Burn / mo</span>
 <span className="v">
 {dashed ? "—" : fmtUsd(funding.monthlyBurnUsd)}
 </span>
 </div>
 <div className="press-bubble-row">
 <span>Runway</span>
 <span className="v">
 {dashed ? "—" : `${funding.runwayMonths.toFixed(1)} mo`}
 </span>
 </div>
 </div>

 {/* Dashed connector line from bubble down toward the board. */}
 <div className={`press-connector ${hovered ? "is-on" : ""}`} aria-hidden>
 <svg viewBox="0 0 10 100" preserveAspectRatio="none">
 <line
 x1="3"
 y1="0"
 x2="6"
 y2="100"
 stroke="rgba(81, 209, 246, 0.55)"
 strokeWidth="1.2"
 strokeDasharray="3 3"
 vectorEffect="non-scaling-stroke"
 />
 </svg>
 </div>
 </>
 );
}

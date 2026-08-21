import type { FundingStatus } from "@/utils/hqData";
import type { Rect } from "./hqHelpers";
import { relativeFromNow } from "./hqHelpers";

// The press-conference smartboard — a minimal flat-panel TV with no
// internal chrome.
//
// The daily-regenerated Cinematic infographic was NotebookLM-backed
// (via notebooklm_bridge). NotebookLM was removed per D-143 (2026-07-01),
// so this surface uniformly renders the funding-widget placeholder with
// a "custom infographic coming" strapline — per operator direction
// 2026-07-01 ("just use a placeholder now and we will make our own
// infographic in the future"). Historical PNGs on disk are ignored; the
// Z-SPAN-native replacement will replace this placeholder end-to-end.

export default function PressScreen({
  rect,
  funding,
}: {
  rect: Rect;
  funding: FundingStatus;
}) {
  const fmtUsd = (n: number) =>
    "$" + n.toLocaleString("en-US", { maximumFractionDigits: 0 });
  const dashed = funding.restricted === true || funding.lastUpdated === null;

  return (
    <>
      <div
        className="ov press-screen"
        style={{
          top: `${rect.top}%`,
          left: `${rect.left}%`,
          width: `${rect.width}%`,
          height: `${rect.height}%`,
        }}
        aria-label="Funding transparency"
        title={`Updated ${relativeFromNow(funding.lastUpdated)} · ${funding.source}`}
      >
        <div className="press-ident">Z-SPAN</div>
        {!dashed && (
          <div className="press-live">
            <span className="press-live-dot" />
            LIVE
          </div>
        )}
        <div className="label">Funding · Public</div>
        <div className="big">{dashed ? "—" : fmtUsd(funding.balanceUsd)}</div>
        <div className="row">
          <span>Burn / mo</span>
          <span className="v">{dashed ? "—" : fmtUsd(funding.monthlyBurnUsd)}</span>
        </div>
        <div className="row">
          <span>Runway</span>
          <span className="v">{dashed ? "—" : `${funding.runwayMonths.toFixed(1)} mo`}</span>
        </div>
        <div className="row" style={{ opacity: 0.55, marginTop: "0.5em" }}>
          <span style={{ fontStyle: "italic" }}>Custom infographic coming</span>
        </div>
      </div>
      {/* Smartboard mount — two tapered steel legs from the panel's bottom
       *  down to the painted stage line, matching the original board in
       *  zspan-hq.png (legs at ~20% / ~76% of the board width). The
       *  painted board was stripped from the composite (its leg stubs
       *  cleaned in the v6/v7 image pass); this is the HTML rebuild. */}
      <div
        className="ov press-legs"
        aria-hidden="true"
        style={{
          top: `${rect.top + rect.height}%`,
          left: `${rect.left}%`,
          width: `${rect.width}%`,
          height: "7.2%",
        }}
      >
        <div className="press-leg" style={{ left: "17%" }} />
        <div className="press-leg" style={{ left: "73%" }} />
      </div>
    </>
  );
}

import { Maximize2, X } from "lucide-react";
import type { GuideCardData } from "./GuideCard";
const STATE_NAME_BY_ABBR: Record<string, string> = {
    AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas",
    CA: "California", CO: "Colorado", CT: "Connecticut", DE: "Delaware",
    DC: "District of Columbia", FL: "Florida", GA: "Georgia", HI: "Hawaii",
    ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas",
    KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland",
    MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi",
    MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada",
    NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York",
    NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma",
    OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina",
    SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah",
    VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia",
    WI: "Wisconsin", WY: "Wyoming",
};
interface InlinePlayerProps {
    data: GuideCardData;
    onMaximize: () => void;
    onClose: () => void;
}
export default function InlinePlayer({ data, onMaximize, onClose, }: InlinePlayerProps) {
    const stateFullName = data.state ? STATE_NAME_BY_ABBR[data.state] ?? data.state : "";
    const titleText = data.title || `${data.city_name} — live meeting`;
    return (<section className="guide-inline-player" aria-label={`Inline player: ${titleText}`}>
      <div className="guide-inline-player-frame">
        <iframe key={data.video_id} src={`https://www.youtube.com/embed/${data.video_id}?autoplay=1`} title={titleText} className="guide-inline-player-iframe" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowFullScreen/>
      </div>
      <div className="guide-inline-player-head">
        <div className="guide-inline-player-meta">
          <span className="guide-inline-player-live">
            <span className="guide-inline-player-live-dot"/> Live
          </span>
          <div className="guide-inline-player-meta-text">
            <div className="guide-inline-player-title">{titleText}</div>
            <div className="guide-inline-player-place">
              {data.city_name}
              {data.county ? ` · ${stripCountySuffix(data.county)} County` : ""}
              {stateFullName ? ` · ${stateFullName}` : ""}
            </div>
          </div>
        </div>
      </div>
      <div className="guide-inline-player-actions">
        <button type="button" className="guide-inline-player-action" onClick={onMaximize} title="Back to full-screen" aria-label="Maximize back to full-screen player">
          <Maximize2 size={14}/>
          <span>Cinematic</span>
        </button>
        <button type="button" className="guide-inline-player-action guide-inline-player-close" onClick={onClose} title="Close player" aria-label="Close player">
          <X size={14}/>
        </button>
      </div>
    </section>);
}
function stripCountySuffix(name: string): string {
    return name.replace(/\s+county\s*$/i, "").trim();
}

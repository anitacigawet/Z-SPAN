/**
 * CinematicTakeover — full-screen player overlay for the Guide.
 *
 * Per James's "Spiritual Visual Design for the Guide" (2026-06-02) +
 * answered architecture question: card click default = cinematic
 * takeover (animated zoom into a full-screen player). Background
 * gradient + starfield stay visible behind a backdrop-filter:blur scrim.
 * Close via the × button or the Escape key.
 *
 * G-6 (next chunk) adds the "Keep browsing" button that demotes the
 * takeover to a single inline player below the card deck. For G-5,
 * that button is a placeholder shell — clicking it is a no-op until
 * G-6 wires the inline mode.
 *
 * Phase G chunk G-5 (2026-06-02).
 */
import { useEffect } from "react";
import { X } from "lucide-react";
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

interface CinematicTakeoverProps {
  data: GuideCardData;
  onClose: () => void;
  onDemoteToInline?: () => void;
}

export default function CinematicTakeover({
  data,
  onClose,
  onDemoteToInline,
}: CinematicTakeoverProps) {
  // Escape key returns to the deck. Keep listener tight on the
  // takeover's lifecycle so deck-level keyboard handlers aren't
  // intercepted when the takeover isn't open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    // Lock body scroll while the takeover is open so the user can't
    // scroll the page underneath the overlay.
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  const stateFullName = data.state ? STATE_NAME_BY_ABBR[data.state] ?? data.state : "";
  const titleText = data.title || `${data.city_name} — live meeting`;

  return (
    <div
      className="guide-takeover-overlay"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={titleText}
    >
      <div
        className="guide-takeover-frame"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          className="guide-takeover-close"
          onClick={onClose}
          aria-label="Close player"
          title="Close (Esc)"
        >
          <X size={18} />
        </button>
        <span className="guide-takeover-esc-hint" aria-hidden>
          Esc
        </span>

        <div className="guide-takeover-header">
          <div className="guide-takeover-meta">
            <span className="guide-takeover-live">
              <span className="guide-takeover-live-dot" /> Live
            </span>
            <span className="guide-takeover-state-name">{stateFullName}</span>
          </div>
          <h2 className="guide-takeover-title">{titleText}</h2>
          <div className="guide-takeover-place">
            {data.city_name}
            {data.county ? ` · ${stripCountySuffix(data.county)} County` : ""}
            {data.state ? ` · ${data.state}` : ""}
          </div>
        </div>

        <div className="guide-takeover-player-frame">
          <iframe
            key={data.video_id}
            src={`https://www.youtube.com/embed/${data.video_id}?autoplay=1`}
            title={titleText}
            className="guide-takeover-player"
            allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
            allowFullScreen
          />
        </div>

        <div className="guide-takeover-footer">
          <a
            href={data.video_url}
            target="_blank"
            rel="noopener noreferrer"
            className="guide-takeover-link"
          >
            Open on YouTube ↗
          </a>
          {onDemoteToInline && (
            <button
              type="button"
              className="guide-takeover-demote"
              onClick={onDemoteToInline}
              title="Switch to a smaller inline player so you can keep browsing"
            >
              Keep browsing
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function stripCountySuffix(name: string): string {
  return name.replace(/\s+county\s*$/i, "").trim();
}

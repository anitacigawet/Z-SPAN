/**
 * CommunityCallsToActionSection — V1-CommunityCallsToAction-1.
 *
 * Renders the new third accordion category between Key Decisions and
 * Key Quotes on BroadcastPage. Sources from the V1-RAG-3 output
 * `community_calls_to_action` (a JSON array of verbatim civic asks
 * extracted by Sonnet against the meeting's Qdrant index).
 *
 * Honest-empty discipline: when the parsed JSON yields zero calls,
 * the section hides entirely — does NOT render an empty header that
 * says "0 calls this meeting." Per [D-126](../../../01_Project_Overview/DECISIONS.md#d-126)
 * (V1 no-AI-narrative), every quote is verbatim and every render
 * decision is honest about coverage gaps.
 *
 * Visual: highway-amber (#F5A524) accent distinguishes the
 * civic-action surface from the blue news-quote surface below.
 * Mirrors the Key Quotes accordion shape — collapsed cards show
 * speaker + role + actionable_hook summary; click expands to the
 * full verbatim quote_text + optional deadline/contact metadata.
 *
 * Gated by PublicDataDisclaimerGate per S-091 C4 — same convention
 * as Key Decisions and Key Quotes.
 */
import { useMemo } from "react";
import PromptInfoIcon from "./PromptInfoIcon";
import SyncedQuote, { type QuoteWordTiming } from "./SyncedQuote";
import { PublicDataDisclaimerGate } from "./PublicDataDisclaimerGate";
import { WatermarkRibbon } from "./WatermarkRibbon";
import { isOperatorSurfaceAllowed } from "../lib/trustPlane";

interface CommunityCallToAction {
  speaker_name: string;
  speaker_role: string;
  quote_text: string;
  ask_kind?: string;
  actionable_hook?: string;
  deadline?: string | null;
  contact?: string | null;
  video_timestamp_seconds?: number | null;
  chunk_index?: number | null;
}

interface Props {
  rawContent: string | null | undefined;
  /** S-098 — used to derive the watermark token bound to this output. */
  meetingId?: number | null;
  ribbonToken?: string | null;
  registrationState?: "registered" | "pending" | null;
  karaokeWordTimings?: QuoteWordTiming[][];
  videoUrl?: string | null;
  activeQuoteId?: string | null;
  onActiveQuoteChange?: (quoteId: string | null) => void;
}

function parseCalls(raw: string | null | undefined): CommunityCallToAction[] {
  if (!raw || typeof raw !== "string") return [];
  const trimmed = raw.trim();
  if (!trimmed) return [];
  // Be tolerant of Sonnet occasionally wrapping the JSON in a code
  // fence even when the prompt says not to — strip ```json ... ```
  // wrappers before parsing.
  const stripped = trimmed
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .trim();
  try {
    const parsed = JSON.parse(stripped);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (c): c is CommunityCallToAction =>
        c &&
        typeof c === "object" &&
        typeof c.speaker_name === "string" &&
        typeof c.quote_text === "string" &&
        c.quote_text.length > 0,
    );
  } catch (err) {
    // Sonnet usually honors the "no markdown fence" prompt instruction;
    // edge-case codefence shapes (`~~~json`, `<json>` tags, etc.) fall
    // through to here. Warn so developer-side notices when a real
    // meeting's output silently hides instead of rendering.
    if (typeof console !== "undefined") {
      console.warn(
        "CommunityCallsToActionSection: failed to parse rawContent as JSON; section will hide.",
        err,
      );
    }
    return [];
  }
}

function formatMeetingTime(seconds: number): string {
  const totalSeconds = Math.round(seconds);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
  }

  return `${minutes}:${String(remainingSeconds).padStart(2, "0")}`;
}

export function CommunityCallsToActionSection({
  rawContent,
  meetingId,
  ribbonToken,
  registrationState,
  karaokeWordTimings,
  videoUrl,
  activeQuoteId,
  onActiveQuoteChange,
}: Props) {
  const calls = useMemo(() => parseCalls(rawContent), [rawContent]);

  if (calls.length === 0) return null;

  return (
    <PublicDataDisclaimerGate surfaceName="community_calls_to_action">
      <div className="mb-12">
        <h3
          className="text-[11px] font-bold uppercase tracking-widest mb-6 flex items-center gap-2 flex-wrap"
          style={{ color: "#F5A524" }}
        >
          <span>Community Calls to Action</span>
          {isOperatorSurfaceAllowed() && (
            <PromptInfoIcon
              promptName="community_calls_to_action"
              label="Community Calls to Action"
              color="#F5A524"
            />
          )}
          {typeof meetingId === "number" && (
            <span className="ml-auto inline-flex items-center" title="Z-SPAN provenance ribbon · click to verify">
              <WatermarkRibbon
                meetingId={meetingId}
                outputType="community_calls_to_action"
                ribbonToken={ribbonToken}
                registrationState={registrationState}
              />
            </span>
          )}
        </h3>
        <div className="space-y-4">
          {calls.map((c, i) => (
            <details key={`cta-${i}`} className="group">
              <summary className="cursor-pointer list-none select-none py-1">
                {c.actionable_hook && (
                  <div className="flex items-baseline gap-x-2">
                    <span className="inline-block transition-transform group-open:rotate-90 text-[#F5A524]/40 text-[10px] flex-shrink-0">
                      ▸
                    </span>
                    <span className="text-[14px] text-[#F5A524]/90 italic leading-relaxed">
                      ({c.actionable_hook})
                    </span>
                  </div>
                )}
                <div className="ml-4 mt-1 text-[12px]">
                  <span className="font-semibold text-white/90">
                    {c.speaker_name}
                  </span>
                  {c.speaker_role && (
                    <span className="text-gray-400 font-normal">
                      , {c.speaker_role}
                    </span>
                  )}
                </div>
              </summary>
              <div className="mt-2 ml-5 pb-3">
                {karaokeWordTimings?.[i]?.length && videoUrl ? (
                  <SyncedQuote
                    wordTimings={karaokeWordTimings?.[i] || []}
                    videoUrl={videoUrl}
                    isActive={activeQuoteId === `ccta-${i}`}
                    onActivate={() => onActiveQuoteChange?.(`ccta-${i}`)}
                    onDeactivate={() => onActiveQuoteChange?.(null)}
                    markerColor="#F5A524"
                  />
                ) : (
                  <p className="text-[14px] text-gray-200 leading-relaxed italic">
                    &ldquo;{c.quote_text}&rdquo;
                  </p>
                )}
                {(c.deadline || c.contact) && (
                  <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-[#E4E4E5]/75">
                    {c.deadline && (
                      <span>
                        <span className="text-[#F5A524]/70 mr-1">Deadline:</span>
                        {c.deadline}
                      </span>
                    )}
                    {c.contact && (
                      <span>
                        <span className="text-[#F5A524]/70 mr-1">Contact:</span>
                        {c.contact}
                      </span>
                    )}
                  </div>
                )}
                {(!karaokeWordTimings?.[i]?.length || !videoUrl) &&
                  typeof c.video_timestamp_seconds === "number" &&
                  Number.isFinite(c.video_timestamp_seconds) &&
                  c.video_timestamp_seconds >= 0 && (
                    <div className="mt-2">
                      {videoUrl && (
                        <p className="text-[11px] text-gray-400">
                          Approximate source marker:{" "}
                          <span className="text-[#F5A524]/75">
                            {formatMeetingTime(c.video_timestamp_seconds)}
                          </span>
                        </p>
                      )}
                      {isOperatorSurfaceAllowed() && (
                        <p className="mt-1 text-[10px] text-gray-500 font-mono">
                          {typeof c.chunk_index === "number" &&
                            `chunk ${c.chunk_index} · `}
                          t={Math.round(c.video_timestamp_seconds)}s
                        </p>
                      )}
                    </div>
                  )}
              </div>
            </details>
          ))}
        </div>
      </div>
    </PublicDataDisclaimerGate>
  );
}

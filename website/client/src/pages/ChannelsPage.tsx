/**
 * ChannelsPage — the Z-SPAN — Arizona library landing (channel-guide presentation).
 *
 * Spirit: cable-TV channel guide × Plex × Netflix episode list, but its
 * own visual language (matched to <private-predecessor-repo>'s "Kingman Insight" dark
 * theme — deep blacks, white text, subtle borders, status dots).
 *
 * Layout:
 * ┌──────────────────────────────────────────────────────────────────┐
 * │ Z-SPAN · ARIZONA [search] [auth] │
 * ├─────────────┬─────────────┬───────────────────────────────────────┤
 * │ COUNTIES │ CITIES │ EPISODES │
 * │ ● Mohave │ ● Kingman │ [card] [card] [card] [card] ... │
 * │ ○ Maricopa │ ○ Bullhead │ │
 * │ ○ Pima │ ○ Lake Hav. │ │
 * │ ... │ ○ Colorado │ │
 * └─────────────┴─────────────┴───────────────────────────────────────┘
 *
 * Selections drill down: state → county → city → episodes. Default
 * selection lands on the deepest active path so a fresh visitor sees
 * episodes immediately. Click an episode → broadcast detail.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, Menu, RefreshCw, Settings, Tv, X } from "lucide-react";
import DefinitionHint from "@/components/DefinitionHint";
import { OwnerOnly } from "../components/OwnerOnly";
import { TravelersOdometer } from "../components/TravelersOdometer";
import { useCurrentUser } from "../hooks/useCurrentUser";
import CastPanel, { CastMemberSummary } from "../components/CastPanel";
import MeetingSchedulePanel from "../components/MeetingSchedulePanel";
import CastMemberPanel from "../components/CastMemberPanel";
import { channelPosterForCity } from "../utils/channelPoster";
import { episodeCardForTitle } from "../utils/episodeCard";
import { TAG_COLOR, parseEpisodeTags } from "../utils/episodeTags";
import { FollowButton } from "../components/FollowButton";
import {
 filterVisibleEpisodes,
 isCatalogPlaceholder,
 useHidePlaceholders,
} from "../hooks/useHidePlaceholders";
import { fetchForPlane } from "../lib/planeFetch";
import { isPublicPlane } from "../lib/trustPlane";

// ── Static channel taxonomy ────────────────────────────────────────
// Z-SPAN — Arizona's pilot is Mohave County. The rest of Arizona's 15
// counties are visible-but-inactive ("Coming soon") — those are the
// flagship's own Arizona work.

// All 50 states are listed, but the non-Arizona states are NOT
// this project's roadmap — each is an OPEN SEAT for an independent state
// operator to claim under the spirit-pact. Arizona is active; the rest
// render grayed as open seats. The header renders these in a fixed-width,
// horizontally-scrollable rail.
const STATES = [
 { code: "AL", name: "Alabama", active: false },
 { code: "AK", name: "Alaska", active: false },
 { code: "AZ", name: "Arizona", active: true },
 { code: "AR", name: "Arkansas", active: false },
 { code: "CA", name: "California", active: false },
 { code: "CO", name: "Colorado", active: false },
 { code: "CT", name: "Connecticut", active: false },
 { code: "DE", name: "Delaware", active: false },
 { code: "FL", name: "Florida", active: false },
 { code: "GA", name: "Georgia", active: false },
 { code: "HI", name: "Hawaii", active: false },
 { code: "ID", name: "Idaho", active: false },
 { code: "IL", name: "Illinois", active: false },
 { code: "IN", name: "Indiana", active: false },
 { code: "IA", name: "Iowa", active: false },
 { code: "KS", name: "Kansas", active: false },
 { code: "KY", name: "Kentucky", active: false },
 { code: "LA", name: "Louisiana", active: false },
 { code: "ME", name: "Maine", active: false },
 { code: "MD", name: "Maryland", active: false },
 { code: "MA", name: "Massachusetts", active: false },
 { code: "MI", name: "Michigan", active: false },
 { code: "MN", name: "Minnesota", active: false },
 { code: "MS", name: "Mississippi", active: false },
 { code: "MO", name: "Missouri", active: false },
 { code: "MT", name: "Montana", active: false },
 { code: "NE", name: "Nebraska", active: false },
 { code: "NV", name: "Nevada", active: false },
 { code: "NH", name: "New Hampshire", active: false },
 { code: "NJ", name: "New Jersey", active: false },
 { code: "NM", name: "New Mexico", active: false },
 { code: "NY", name: "New York", active: false },
 { code: "NC", name: "North Carolina", active: false },
 { code: "ND", name: "North Dakota", active: false },
 { code: "OH", name: "Ohio", active: false },
 { code: "OK", name: "Oklahoma", active: false },
 { code: "OR", name: "Oregon", active: false },
 { code: "PA", name: "Pennsylvania", active: false },
 { code: "RI", name: "Rhode Island", active: false },
 { code: "SC", name: "South Carolina", active: false },
 { code: "SD", name: "South Dakota", active: false },
 { code: "TN", name: "Tennessee", active: false },
 { code: "TX", name: "Texas", active: false },
 { code: "UT", name: "Utah", active: false },
 { code: "VT", name: "Vermont", active: false },
 { code: "VA", name: "Virginia", active: false },
 { code: "WA", name: "Washington", active: false },
 { code: "WV", name: "West Virginia", active: false },
 { code: "WI", name: "Wisconsin", active: false },
 { code: "WY", name: "Wyoming", active: false },
];

const ARIZONA_COUNTIES = [
 { name: "Mohave", active: true },
 { name: "Maricopa", active: false },
 { name: "Pima", active: false },
 { name: "Coconino", active: false },
 { name: "Yavapai", active: false },
 { name: "Yuma", active: false },
 { name: "Pinal", active: false },
 { name: "Cochise", active: false },
 { name: "Apache", active: false },
 { name: "Navajo", active: false },
 { name: "Gila", active: false },
 { name: "Graham", active: false },
 { name: "Greenlee", active: false },
 { name: "La Paz", active: false },
 { name: "Santa Cruz", active: false },
];

const MOHAVE_CITIES = [
 { name: "Kingman", active: true },
 { name: "Bullhead City", active: true },
 { name: "Lake Havasu City", active: true },
 { name: "Colorado City", active: true },
];

// ── Types ──────────────────────────────────────────────────────────

interface Episode {
 id?: number;
 // public catalog identity. Present on facts-only coming-soon rows;
 // internal episode rows deliberately keep their existing shape.
 public_id?: string;
 availability?: string;
 local_video_class?: string;
 local_processable?: boolean;
 meeting_title: string;
 meeting_date: string;
 meeting_time?: string;
 meeting_location?: string;
 notebook_id?: string | null;
 video_url?: string;
 // Per-episode hook + tags JOINed in by /scrape/<city>. NULL when the
 // meeting hasn't been processed by the NotebookLM pipeline yet.
 episode_tagline?: string | null;
 episode_tags?: string | null;
 // Phase 3 — publish state. Public default filters server-side to
 // is_published=true; operator mode (?drafts=true) returns everything
 // and the UI renders a draft chip on the non-published rows.
 // (published_by is no longer served on catalog rows — 2026-07-09
 // identity-strip.)
 is_published?: boolean;
 published_at?: string | null;
}

interface ChannelsPageProps {
 onNavigate: (view: string, params?: any) => void;
 // TopBar channel type-ahead jump (V1-Polish-12 follow-up, 2026-06-14).
 // When the TopBarSearch dropdown picks a channel it navigates home with
 // these params; the apply-effect below selects that county/city.
 // `selectNonce` changes on every pick so re-selecting the same channel
 // after the user moved within the rail still re-fires the effect.
 selectCounty?: string;
 selectCity?: string;
 selectNonce?: number;
 // V1-Polish-24: the Z-SPAN logo + Channels nav pass a changing nonce here to
 // reset the drill-down back to the state-level "Pick a county" picker.
 resetNonce?: number;
}

// ── Helpers ────────────────────────────────────────────────────────

function formatDateShort(s: string | undefined): string {
 if (!s) return "—";
 try {
 const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s + "T00:00:00") : new Date(s);
 if (isNaN(d.getTime())) return s;
 return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
 } catch {
 return s;
 }
}

function dayOfWeek(s: string | undefined): string {
 if (!s) return "";
 try {
 const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s + "T00:00:00") : new Date(s);
 if (isNaN(d.getTime())) return "";
 return d.toLocaleDateString("en-US", { weekday: "short" }).toUpperCase();
 } catch {
 return "";
 }
}

// Strip "City Council - Apr 07, 2026" → "City Council"
function meetingTypeFromTitle(t: string): string {
 if (!t) return "Council Meeting";
 const dashIdx = t.indexOf(" - ");
 return dashIdx > 0 ? t.slice(0, dashIdx).trim() : t.trim();
}

// ── Subcomponents ──────────────────────────────────────────────────
//
// Channel-guide list row: hairline-separated, no boxed-card chrome, status
// indicated by a small dot rather than an icon column. Per James 2026-05-08:
// the previous boxed-column treatment "popped out" too much against the rest
// of the page — drop the kg-card wrapper, drop the green left-bar selection
// state, lean on typography + dots like the rest of the channel listing.
//
// `compact` mode is the slim left-rail variant rendered at city-level for
// sibling navigation. `selected` highlights the currently active sibling
// (only meaningful in compact mode; the full mode never shows the row that
// matches the current selection because the user has navigated past it).

// V1-Polish-19 (James 2026-06-14, "Part 2 option B"): data-driven per-city
// status from /api/channels/tree. Three honest states:
// "live" — has ≥1 processed broadcast (watchable) → green
// "cached" — meetings scraped, no broadcasts yet (coming) → amber
// "scaffold" — parser registered, nothing scraped → red
//
// Supersedes the original V1-UI-1 binary V1Status ("processed" | "not_v1")
// + v1StatusForCity() helper, which were removed 2026-06-19 as dead code
// per the speed-audit mechanical-class sweep (no callsites remained after
// V1-Polish-19 swapped in the 3-state). V1_PROCESSED_CITIES below is the
// only V1-UI-1-era construct still load-bearing — BroadcastPage uses it
// for the V1-flagged-city indicator separate from the dot color.
type CityStatus = "live" | "cached" | "scaffold" | "postponed";

// V1 target list per V1_PUBLIC_RELEASE_SPEC. Operator direction
// 2026-06-10: Kingman, Bullhead City, Lake Havasu City, and Colorado
// City are the cities processed for the v1 release. / V1
// SPEC, all other cities are scaffolded but not yet processed.
export const V1_PROCESSED_CITIES: ReadonlySet<string> = new Set([
 "Kingman",
 "Bullhead City",
 "Lake Havasu City",
 "Colorado City",
]);

function StatusDot({
 active,
 status,
 sizePx,
}: {
 active: boolean;
 status?: CityStatus;
 sizePx: number;
}) {
 // V1-Polish-19 3-state when a data-driven status is provided (cities):
 // live → green glow · cached → amber · scaffold → dim red.
 // Counties pass no status and fall back to the binary active dot below.
 if (status === "live") {
 return (
 <span
 className="kg-dot-active flex-shrink-0"
 style={{ width: sizePx, height: sizePx }}
 aria-hidden="true"
 />
 );
 }
 if (status === "cached") {
 return (
 <span
 className="flex-shrink-0 rounded-full"
 style={{
 width: sizePx * 0.85,
 height: sizePx * 0.85,
 background: "#f5a524",
 opacity: 0.85,
 }}
 aria-hidden="true"
 />
 );
 }
 if (status === "scaffold" || status === "postponed") {
 // V1-Polish-21 (James 2026-06-14): scaffold = "coming soon, nothing here
 // yet" → gray, not red. Red read as "broken / error"; gray reads as an
 // inert placeholder you shouldn't click into. Deliberately matches the
 // disabled-county fallback gray below so the two "nothing here yet"
 // states look identical.
 // F28 postponed (2026-06-19): cities flagged stale_known_postponed in
 // parser_index render visually identical to scaffold — public UX is
 // "coming soon, nothing here yet" regardless of whether the parser
 // exists-but-is-stale (postponed) or hasn't-been-built-yet (scaffold).
 return (
 <span
 className="bg-foreground/15 flex-shrink-0 rounded-full"
 style={{ width: sizePx * 0.6, height: sizePx * 0.6 }}
 aria-hidden="true"
 />
 );
 }
 // Fallback — preserve existing binary active/inactive (counties).
 if (active) {
 return (
 <span
 className="kg-dot-active flex-shrink-0"
 style={{ width: sizePx, height: sizePx }}
 aria-hidden="true"
 />
 );
 }
 return (
 <span
 className="rounded-full bg-foreground/15 flex-shrink-0"
 style={{ width: sizePx * 0.6, height: sizePx * 0.6 }}
 aria-hidden="true"
 />
 );
}

// V1-Polish-21/22: the human-facing label for a channel status, rendered
// uppercase by the row (LIVE / IN PROGRESS / COMING SOON). live = has
// broadcasts (green); cached = scraped, content being built (amber); scaffold
// = registered, nothing here yet (gray, inert). Sentence-case source string
// + the existing "Coming soon" label.
function statusLabel(status?: CityStatus): string {
 // Live status: the green dot ALONE signals "finished/fully live" per
 // operator-direction 2026-06-24 — no redundant text label needed
 // (the green dot already carries the signal). Cached + scaffold
 // still get text labels because amber/gray dots aren't as
 // self-explanatory.
 if (status === "live") return "";
 if (status === "cached") return "In progress";
 // scaffold + postponed both render as "Coming soon" to the public UX
 // — semantically distinct (scaffold = not-yet-built, postponed = built-
 // but-flagged-stale-pending-remediation) but identical to the reader.
 return "Coming soon";
}

// V1-Polish-22: roll a county's status up from its cities so the county dot +
// label match what you actually find inside. live if any city is live; else
// "in progress" (amber) if any city is cached (Maricopa's case — scraped, not
// yet broadcast); else "coming soon" (gray, inert — pure scaffold).
function deriveCountyStatus(
 cities: ReadonlyArray<{ status?: CityStatus }>,
): CityStatus {
 if (cities.some(c => c.status === "live")) return "live";
 if (cities.some(c => c.status === "cached")) return "cached";
 // Postponed cities count as inert (scaffold-ish) for county rollup —
 // no point bumping the county dot for cities the operator already
 // knows render as honest-empty.
 return "scaffold";
}

function ChannelListRow({
 name,
 active,
 meta,
 onClick,
 compact = false,
 selected = false,
 status,
}: {
 name: string;
 active: boolean;
 meta?: string;
 onClick?: () => void;
 compact?: boolean;
 selected?: boolean;
 status?: CityStatus;
}) {
 const clickable = active && !!onClick && !selected;

 if (compact) {
 return (
 <button
 type="button"
 disabled={!clickable}
 onClick={onClick}
 aria-current={selected ? "page" : undefined}
 className={`group w-full text-left flex items-center gap-2 py-1.5 px-2 -mx-2 rounded-md transition-colors
 ${
 selected
 ? "bg-[var(--surface)]/70 cursor-default"
 : clickable
 ? "hover:bg-[var(--surface)]/40 cursor-pointer"
 : "cursor-default"
 }`}
 >
 <StatusDot active={active} status={status} sizePx={5} />
 <span
 className={`text-[12.5px] truncate tracking-wide ${
 selected
 ? "text-white font-medium"
 : active
 ? "text-foreground/65 font-light group-hover:text-white"
 : "text-foreground/30 font-light"
 }`}
 >
 {name}
 </span>
 </button>
 );
 }

 return (
 <button
 type="button"
 disabled={!clickable}
 onClick={onClick}
 className={`group w-full text-left flex items-center justify-between gap-4 py-3.5 px-2 -mx-2 rounded-md transition-colors
 ${
 clickable
 ? "hover:bg-[var(--surface)]/50 cursor-pointer"
 : "cursor-default"
 }`}
 >
 <div className="flex items-center gap-3 min-w-0">
 <StatusDot active={active} status={status} sizePx={6} />
 <span
 className={`text-[15px] truncate tracking-wide ${
 active
 ? "text-white font-light group-hover:text-white"
 : "text-foreground/35 font-light"
 }`}
 >
 {name}
 </span>
 </div>
 <div className="flex items-center gap-3 flex-shrink-0">
 {meta && (
 <span
 className={`text-[10px] uppercase tracking-[0.18em] ${
 active ? "text-foreground/45" : "text-foreground/25"
 }`}
 >
 {meta}
 </span>
 )}
 {clickable && (
 <ArrowRight className="w-3.5 h-3.5 text-foreground/30 group-hover:text-white group-hover:translate-x-0.5 transition-all" />
 )}
 </div>
 </button>
 );
}

// ── The OUTLINE rail (chunk 1b + chunk 3, 2026-05-29) ────────
//
// The condensed, permanent form of the "Pick a county / city" picker, shown
// ONLY at city level (chunk 3, James's onboarding model): state + county are
// taught by the spacious centered Tuner; once you reach a city, that picker
// collapses into this left-hand navigator — an Obsidian / code-outline tree
// (Now Browsing → –state → ––county → –––cities).
//
// Decoupled from the channel (James's model, points 5-6): the rail navigates
// freely, but the channel on the right only swaps when you pick a new CITY.
// Two display modes:
// · drilled (default) — –state / ––county / –––cities, current city marked.
// · browsing — click a crumb to survey the state's counties without
// disturbing the channel; click the live county to
// drill back to its cities.
//
// Tone per node: current (white + green dot) · crumb (clickable path) ·
// option (clickable child) · disabled (coming-soon, grayed).

type OutlineTone = "current" | "crumb" | "option" | "disabled";

function OutlineRow({
 depth,
 label,
 tone,
 meta,
 onClick,
 status,
}: {
 depth: number;
 label: string;
 tone: OutlineTone;
 meta?: string;
 onClick?: () => void;
 // Only meaningful on the current city row — colors its dot to MATCH the
 // city-picker StatusDot (live=green / cached=amber / scaffold=gray) so the
 // dot a user saw in the picker is the same dot on the channel page
 // (V1-Polish-17 → -19 → -21). Was always green ("you are here"), which
 // contradicted the picker dot for non-live cities like Chinle.
 status?: CityStatus;
}) {
 const clickable = !!onClick;
 // Dash marker grows with depth (– / –– / –––), Obsidian-outline style.
 const marker = "–".repeat(depth + 1);
 // V1-Polish-3 → -23: one marker per row, never a dash AND a dot. Rows with a
 // status (counties in the survey + cities) show the SAME StatusDot as the
 // picker (green=live / amber=in-progress / gray=coming-soon); pure crumbs
 // (the state row + the county you're inside) keep the outline dash. Depth 0
 // shows nothing (a lone dash read as a stray dot behind the name). The
 // fixed-width slot keeps labels aligned across dot + dash rows.
 const markerSlotWidth = (depth + 1) * 7;
 const textCls =
 tone === "current"
 ? "text-white font-medium"
 : tone === "crumb"
 ? "text-foreground/55 group-hover:text-white"
 : tone === "option"
 ? "text-foreground/70 group-hover:text-white"
 : "text-foreground/30";
 return (
 <button
 type="button"
 disabled={!clickable}
 onClick={onClick}
 aria-current={tone === "current" ? "page" : undefined}
 style={{ paddingLeft: depth * 14 }}
 className={`group w-full text-left flex items-center gap-2 py-1.5 px-1 -mx-1 rounded-md transition-colors ${
 clickable ? "hover:bg-[var(--surface)]/40 cursor-pointer" : "cursor-default"
 }`}
 >
 {depth > 0 && (
 <span
 className="text-[11px] text-foreground/25 tabular-nums select-none flex-shrink-0 inline-flex items-center"
 style={{ minWidth: markerSlotWidth }}
 aria-hidden="true"
 >
 {status ? (
 // Same dot the picker shows for this channel/area (V1-Polish-23):
 // green=live · amber=in-progress · gray=coming-soon. Applies to the
 // current row too — its dot tracks its real status, not "you are
 // here".
 <StatusDot active={status !== "scaffold"} status={status} sizePx={5} />
 ) : (
 marker
 )}
 </span>
 )}
 <span className={`text-[12.5px] truncate tracking-wide ${textCls}`}>
 {label}
 </span>
 {meta && (
 <span className="ml-auto text-[9px] uppercase tracking-[0.18em] text-foreground/25 flex-shrink-0">
 {meta}
 </span>
 )}
 </button>
 );
}

/**
 * ExitRampSign — the dynamic "CURRENTLY VIEWING [STATE]" blue highway guide
 * sign that anchors the OutlineRail (2026-06-23, highway-brand pass per
 * IMAGE_PROMPTS_HIGHWAY_BRAND.md § "NOW BROWSING guide sign — Claude builds
 * this in code").
 *
 * Updated 2026-06-23 PM per James's reference images of real "EXIT 462 FOOD"
 * services signs (Texas-style): EXIT label moves to TOP-CENTER inside the
 * panel (like the "FOOD" category header on real services signs), upper-right
 * carries a right-pointing arrow (matching the real-sign convention that
 * directs traffic to the exit ramp), and the hand-cursor decoration is
 * retired. The EXIT label is now a clickable element with the same handler
 * as the Z-SPAN logo — clicking it navigates back to home. The body
 * "CURRENTLY VIEWING [STATE]" remains the survey-counties affordance.
 *
 * Blue (#3361A6) is intentional — US highway code: blue = services / motorist
 * info ("you are here / where to find what you need"), which maps the
 * breadcrumb concept cleanly. Green wordmark + blue exit sign coexist as
 * distinct sign vocabularies, not a brand inconsistency.
 */
function ExitRampSign({
 stateName,
 onSurveyCounties,
 onHome,
 surveyDisabled,
}: {
 stateName: string;
 onSurveyCounties?: () => void;
 onHome?: () => void;
 surveyDisabled?: boolean;
}) {
 const label = (stateName || "").toUpperCase();
 const handleExitClick = (e: React.MouseEvent) => {
 e.stopPropagation();
 onHome?.();
 };
 const handleBodyClick = () => {
 if (!surveyDisabled) onSurveyCounties?.();
 };
 return (
 <div
 className={`block w-full mb-2 transition-opacity ${
 surveyDisabled ? "cursor-default opacity-90" : "cursor-pointer hover:opacity-95"
 }`}
 onClick={handleBodyClick}
 role="button"
 tabIndex={surveyDisabled ? -1 : 0}
 onKeyDown={(e) => {
 if (!surveyDisabled && (e.key === "Enter" || e.key === " ")) {
 e.preventDefault();
 onSurveyCounties?.();
 }
 }}
 aria-label={
 surveyDisabled
 ? `Currently viewing ${stateName}`
 : `Currently viewing ${stateName} — click to see counties`
 }
 title={`Currently viewing ${stateName}`}
 >
 <svg
 viewBox="0 0 220 100"
 xmlns="http://www.w3.org/2000/svg"
 className="w-full h-auto"
 role="img"
 aria-hidden
 >
 {/* Outer panel — blue guide-sign field, rounded, white border */}
 <rect
 x="3"
 y="3"
 width="214"
 height="94"
 rx="8"
 fill="var(--highway-sign-blue)"
 stroke="#FFFFFF"
 strokeWidth="2.5"
 />
 {/* EXIT label — top-center, clickable to navigate home (mimics the
 "FOOD" category header on real services signs; doubles as a
 functional "back to home" affordance for users who don't catch
 the highway reference) */}
 <g
 className="exit-button group"
 onClick={handleExitClick}
 style={{ cursor: "pointer" }}
 role="link"
 aria-label="Back to Z-SPAN home"
 >
 {/* Invisible hit target — gives the click a comfortable bounding
 box around the small text */}
 <rect x="90" y="9" width="40" height="20" fill="transparent" />
 <text
 x="110"
 y="24"
 fontSize="13"
 fontFamily="Inter, sans-serif"
 fontWeight="800"
 letterSpacing="2.5"
 fill="#FFFFFF"
 textAnchor="middle"
 className="transition-opacity hover:opacity-80"
 >
 EXIT
 </text>
 </g>
 {/* Right-pointing arrow — upper-right (matches the real-sign
 convention pointing toward the exit ramp). FHWA-style chevron. */}
 <g transform="translate(190, 12)" fill="none" stroke="#FFFFFF" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
 <line x1="0" y1="9" x2="14" y2="9" />
 <polyline points="9,3 15,9 9,15" />
 </g>
 {/* "CURRENTLY VIEWING" — small eyebrow */}
 <text
 x="110"
 y="60"
 fontSize="9"
 fontFamily="Inter, sans-serif"
 fontWeight="600"
 letterSpacing="2"
 fill="#FFFFFF"
 textAnchor="middle"
 >
 CURRENTLY VIEWING
 </text>
 {/* State name — large, dominant */}
 <text
 x="110"
 y="86"
 fontSize={label.length > 12 ? "18" : label.length > 8 ? "22" : "24"}
 fontFamily="Inter, sans-serif"
 fontWeight="800"
 letterSpacing="1.5"
 fill="#FFFFFF"
 textAnchor="middle"
 >
 {label}
 </text>
 </svg>
 </div>
 );
}

function OutlineRail({
 stateName,
 counties,
 cities,
 selectedCounty,
 selectedCity,
 showingCounties,
 onShowCounties,
 onShowCities,
 onSelectCounty,
 onSelectCity,
 onHome,
}: {
 stateName: string;
 counties: ReadonlyArray<{ name: string; active: boolean; status?: CityStatus }>;
 cities: ReadonlyArray<{ name: string; active: boolean; status?: CityStatus }>;
 selectedCounty: string | null;
 selectedCity: string | null;
 showingCounties: boolean;
 onShowCounties: () => void;
 onShowCities: () => void;
 onSelectCounty: (county: string) => void;
 onSelectCity: (city: string) => void;
 onHome?: () => void;
}) {
 return (
 <nav aria-label="Channel path" className="flex flex-col gap-1">
 {/* Highway-brand exit-ramp sign replaces the prior "Now Browsing" eyebrow
 + depth-0 state row. Clicking the body surveys counties (preserved
 affordance); clicking the EXIT label at top navigates home. */}
 <ExitRampSign
 stateName={stateName}
 onSurveyCounties={onShowCounties}
 onHome={onHome}
 surveyDisabled={showingCounties}
 />
 <div
 key={showingCounties ? "counties" : "cities"}
 className="flex flex-col gap-1 animate-in fade-in-0 slide-in-from-top-1 duration-300"
 >
 {showingCounties ? (
 // Browsing the state's counties (V1-Polish-18, 2026-06-14). Clicking
 // the CURRENT county drills the rail back to its cities (channel
 // stays put); clicking a DIFFERENT active county NAVIGATES to it
 // (its "Pick a city" view). Previously every county called
 // onShowCities, which silently snapped you back to the current
 // county — invisible when only Mohave had data, a lock-in once many
 // counties went live.
 counties.map(c => (
 <OutlineRow
 key={c.name}
 depth={1}
 label={c.name}
 tone={c.active ? "option" : "disabled"}
 status={c.status}
 meta={statusLabel(c.status ?? (c.active ? "live" : "scaffold"))}
 onClick={
 c.active
 ? c.name === selectedCounty
 ? onShowCities
 : () => onSelectCounty(c.name)
 : undefined
 }
 />
 ))
 ) : (
 selectedCounty && (
 <>
 {/* County crumb — click to survey sibling counties. */}
 <OutlineRow
 depth={1}
 label={selectedCounty}
 tone="crumb"
 onClick={onShowCounties}
 />
 {cities.map(city => {
 const isCurrent = selectedCity === city.name;
 return (
 <OutlineRow
 key={city.name}
 depth={2}
 label={city.name}
 tone={
 isCurrent ? "current" : city.active ? "option" : "disabled"
 }
 status={city.status}
 meta={statusLabel(city.status)}
 onClick={
 city.active && !isCurrent
 ? () => onSelectCity(city.name)
 : undefined
 }
 />
 );
 })}
 </>
 )
 )}
 </div>
 </nav>
 );
}

// Shared shell for the state-level (counties) and county-level (cities) views.
// Eyebrow + heading + optional subheading on top, hairline-bracketed list of
// ChannelListRows below. Capped at max-w-3xl so the rows feel substantial
// rather than stretched across a 1600px canvas.

// DefinitionHint extracted to `components/DefinitionHint.tsx` on
// 2026-07-04 so the Librarian header can reuse the same
// hover-tooltip pattern with a different source (NVIDIA blog for RAG).
// The extracted component keeps "Merriam-Webster ↗" as the default
// sourceLabel so this file's two existing consumers keep working
// without changes.

function ChannelLevelView({
 heading,
 headingHint,
 subheading,
 children,
}: {
 heading: string;
 headingHint?: React.ReactNode;
 subheading?: string;
 children: React.ReactNode;
}) {
 return (
 <section className="max-w-3xl">
 <div className="mb-7">
 {/* Eyebrow removed (James 2026-06-14) — the count line above the
 heading was unnecessary. */}
 {/* items-end (2026-07-04) — operator flagged the ⓘ chip was
 hovering above the heading. items-end drops it to the
 baseline / bottom edge of the text so it sits with the
 footer of the letters instead of the cap-line. */}
 <h2 className="mb-2 flex items-end gap-2.5 text-[28px] font-light leading-tight tracking-tight text-white sm:text-[32px]">
 <span>{heading}</span>
 <span className="pb-1.5 inline-flex">{headingHint}</span>
 </h2>
 {subheading && (
 <p className="text-[13px] text-muted-foreground leading-relaxed max-w-prose">
 {subheading}
 </p>
 )}
 </div>
 <div className="divide-y divide-[var(--line)]/60 border-y border-[var(--line)]/60">
 {children}
 </div>
 </section>
 );
}

// ── Episode grouping (month → week) ──────────────────────────────
//
// Per James 2026-05-08: a flat 24-card grid felt cluttered. Group episodes
// by month (top-level "channel bar"-style header) then by week-of-month
// (sub-bar) so the channel listing reads more like a calendar with the
// Kingman Insight aesthetic.

type EpisodeWeekGroup = {
 weekNumber: number; // week-of-month number (1, 2, 3, ...)
 weekStart: Date; // Sunday at 00:00 of the week's start (used as a stable key)
 episodes: Episode[]; // sorted newest-first within the week
};

type EpisodeMonthGroup = {
 monthKey: string; // "2026-03"
 monthLabel: string; // "April" (year omitted when same as current; carried in `monthYear` for tooltips)
 monthYear: number;
 episodes: number; // total count across all weeks
 weeks: EpisodeWeekGroup[]; // newest-first
};

// Week-of-month: count Sundays from the 1st of the month. Apr 1 = Week 1,
// Apr 8 = Week 2, etc. Matches a typical wall-calendar's reading order.
function weekOfMonth(date: Date): number {
 const firstOfMonth = new Date(date.getFullYear(), date.getMonth(), 1);
 const firstSunday = new Date(firstOfMonth);
 firstSunday.setDate(firstOfMonth.getDate() - firstOfMonth.getDay());
 firstSunday.setHours(0, 0, 0, 0);
 const ourSunday = new Date(date);
 ourSunday.setDate(date.getDate() - date.getDay());
 ourSunday.setHours(0, 0, 0, 0);
 const ms = ourSunday.getTime() - firstSunday.getTime();
 return Math.round(ms / (7 * 24 * 60 * 60 * 1000)) + 1;
}

function groupByMonthAndWeek(episodes: Episode[]): EpisodeMonthGroup[] {
 const monthMap = new Map<string, Map<string, Episode[]>>();
 for (const ep of episodes) {
 if (!ep.meeting_date) continue;
 // V1-Catalog-1 (2026-06-12) — meeting_date arrives in mixed formats
 // across cities: ISO "2025-05-06" (Kingman), "April 1, 2026"
 // (Flagstaff). The canonical schema says ISO but normalize.py
 // doesn't enforce it yet. Match formatDateShort's tolerance:
 // ISO-prefixed strings keep the explicit T00:00:00 anchor to dodge
 // the local-timezone-offset wrap; everything else falls through to
 // Date.parse and we trust the runtime.
 const d = /^\d{4}-\d{2}-\d{2}/.test(ep.meeting_date)
 ? new Date(ep.meeting_date + "T00:00:00")
 : new Date(ep.meeting_date);
 if (isNaN(d.getTime())) continue;
 const monthKey = `${d.getFullYear()}-${String(d.getMonth()).padStart(2, "0")}`;
 const sunday = new Date(d);
 sunday.setDate(d.getDate() - d.getDay());
 sunday.setHours(0, 0, 0, 0);
 const weekKey = `${sunday.getFullYear()}-${String(sunday.getMonth()).padStart(2, "0")}-${String(sunday.getDate()).padStart(2, "0")}`;
 if (!monthMap.has(monthKey)) monthMap.set(monthKey, new Map());
 const weekMap = monthMap.get(monthKey)!;
 if (!weekMap.has(weekKey)) weekMap.set(weekKey, []);
 weekMap.get(weekKey)!.push(ep);
 }
 const result: EpisodeMonthGroup[] = [];
 const sortedMonthKeys = Array.from(monthMap.keys()).sort().reverse();
 for (const monthKey of sortedMonthKeys) {
 const [yStr, mStr] = monthKey.split("-");
 const monthYear = Number(yStr);
 const sample = new Date(monthYear, Number(mStr), 1);
 const monthLabel = sample.toLocaleString("en-US", { month: "long" });
 const weekMap = monthMap.get(monthKey)!;
 const sortedWeekKeys = Array.from(weekMap.keys()).sort().reverse();
 const weeks: EpisodeWeekGroup[] = sortedWeekKeys.map(wk => {
 const [wy, wm, wd] = wk.split("-").map(Number);
 const weekStart = new Date(wy, wm, wd);
 // Compute week-of-month using a date that's actually IN the month (in
 // case weekStart's Sunday is in the previous month, e.g., Apr 1 falls
 // mid-week, the week's Sunday is in March).
 const inMonthDate = weekMap.get(wk)![0]?.meeting_date;
 // V1-Catalog-1: mixed date formats again — match the regex-or-fallback
 // pattern used in the loop above so non-ISO dates parse cleanly.
 const wnDate = inMonthDate
 ? /^\d{4}-\d{2}-\d{2}/.test(inMonthDate)
 ? new Date(inMonthDate + "T00:00:00")
 : new Date(inMonthDate)
 : weekStart;
 const weekNumber = weekOfMonth(wnDate);
 const eps = weekMap
 .get(wk)!
 .slice()
 .sort((a, b) => (b.meeting_date || "").localeCompare(a.meeting_date || ""));
 return { weekNumber, weekStart, episodes: eps };
 });
 const totalEpisodes = weeks.reduce((acc, w) => acc + w.episodes.length, 0);
 result.push({ monthKey, monthLabel, monthYear, episodes: totalEpisodes, weeks });
 }
 return result;
}


// Local endpoint contract: rows are useful here only when their source can
// enter the local pipeline, or when this workspace already has a broadcast
// for them. Keep this client-side guard as defense in depth for older/mixed
// local servers; the endpoint applies the same rule before sending rows.
function isVisibleLocalEpisode(episode: Episode): boolean {
 if (episode.local_video_class === undefined) return true;
 return (
 episode.local_processable !== false ||
 !!episode.notebook_id ||
 !!episode.is_published
 );
}

function EpisodeCard({
 episode,
 onOpen,
}: {
 episode: Episode;
 onOpen: () => void;
}) {
 const isLocalRow = episode.local_video_class !== undefined;
 // Unknown future availability values stay coming-soon-conservative per the
 // /v1 contract. This branch must precede the operator-only unprocessed row:
 // both lack notebook outputs, but they are different audience states.
 if (isCatalogPlaceholder(episode)) {
 const cardSrc = episodeCardForTitle(episode.meeting_title);
 const isDefaultCard = cardSrc.endsWith("/_default.png");
 return (
 <button
 onClick={onOpen}
 className="group text-left rounded-xl border border-dashed border-[var(--line)] hover:border-[var(--line-strong)] hover:-translate-y-0.5 transition-all duration-200 overflow-hidden bg-[var(--canvas)]"
 title="Open this meeting's public facts and CLI handoff"
 >
 <div className="episode-card-face aspect-video relative overflow-hidden">
 <img
 src={cardSrc}
 alt=""
 className="absolute inset-0 w-full h-full object-cover opacity-40 grayscale transition-all duration-300 group-hover:opacity-55 group-hover:scale-[1.02]"
 onError={(e) => {
 const img = e.currentTarget;
 if (!img.src.endsWith("/episodes/_default.png")) {
 img.src = "/episodes/_default.png";
 }
 }}
 />
 <div className="absolute inset-0 bg-gradient-to-t from-[var(--canvas)] via-[var(--canvas)]/35 to-transparent pointer-events-none" />
 <span className="absolute top-2 right-2 inline-flex items-center rounded-full border border-[var(--line-strong)] bg-[var(--surface)]/80 px-2 py-0.5 text-[9px] font-medium tracking-wide text-foreground/60 backdrop-blur-sm">
 Episode coming
 </span>
 {isDefaultCard && (
 <p className="absolute top-3 left-3 right-28 text-[10px] font-semibold tracking-wide text-foreground/65 line-clamp-2">
 {meetingTypeFromTitle(episode.meeting_title)}
 </p>
 )}
 <div className="absolute inset-x-3 bottom-2 flex items-baseline justify-between gap-3">
 <p className="kg-eyebrow episode-card-weekday text-foreground/50">
 {dayOfWeek(episode.meeting_date)}
 </p>
 <p className="episode-card-date font-light text-foreground/70 tracking-wide tabular-nums">
 {formatDateShort(episode.meeting_date)}
 </p>
 </div>
 </div>
 </button>
 );
 }

 const hasBroadcast =
 episode.availability === "published" ||
 !!episode.episode_tagline ||
 !!episode.episode_tags;
 // Phase 3 — generated card content distinguishes "processed" from
 // "actually visible to the public" (is_published). On the public path,
 // the server filters to is_published=true so every visible card is also
 // published — "On Air" shows reliably. On the operator path (?drafts=true),
 // unpublished-but-processed meetings render a DRAFT chip instead.
 // is_published may be `undefined` for cached responses from older API
 // versions — treat undefined as draft. Flask serves SQLite's raw integer
 // 1, so a strict `=== true` misread every published row as draft (latent
 // since Phase 3; surfaced 2026-07-13 when the PI-5 metadata tier put honest
 // coming-soon cards beside them).
 const isPublished =
 !!episode.is_published || episode.availability === "published";
 // "Processed" requires actual generated content cached (tagline OR tags),
 // unless the catalog contract explicitly marks the episode published.
 // Empty outputs are an in-flight / partial-failure state, not a previewable
 // broadcast — operator surface 2026-06-21 (James): empty cards in the grid
 // wasted real estate and didn't communicate the real state. UNPROCESSED
 // rows render as compact horizontal pills below; only PROCESSED episodes
 // get the full episode-card treatment.
 const isProcessed =
 episode.availability === "published" ||
 (hasBroadcast && (!!episode.episode_tagline || !!episode.episode_tags));
 const isDraft = isProcessed && !isPublished;
 const isUnprocessed = !isProcessed;

 if (isLocalRow && isUnprocessed) {
 const cardSrc = episodeCardForTitle(episode.meeting_title);
 const isDefaultCard = cardSrc.endsWith("/_default.png");
 return (
 <button
 onClick={onOpen}
 className="group text-left rounded-xl border border-[var(--line)] hover:border-[var(--line-strong)] hover:-translate-y-0.5 transition-all duration-200 overflow-hidden bg-[var(--canvas)]"
 >
 <div className="episode-card-face aspect-video relative overflow-hidden">
 <img
 src={cardSrc}
 alt=""
 className="absolute inset-0 w-full h-full object-cover transition-transform duration-300 group-hover:scale-[1.02]"
 onError={(e) => {
 const img = e.currentTarget;
 if (!img.src.endsWith("/episodes/_default.png")) {
 img.src = "/episodes/_default.png";
 }
 }}
 />
 <div
 className="absolute inset-x-0 bottom-0 h-1/2 pointer-events-none"
 style={{
 background:
 "linear-gradient(to top, rgba(0,0,0,0.82) 0%, rgba(0,0,0,0.40) 55%, rgba(0,0,0,0) 100%)",
 }}
 />
 {isDefaultCard && (
 <div className="absolute top-2 left-3 right-16">
 <p className="text-[11px] font-semibold uppercase tracking-widest text-white/85 line-clamp-1 drop-shadow-md">
 {meetingTypeFromTitle(episode.meeting_title)}
 </p>
 </div>
 )}
 <div className="absolute bottom-2 left-3 right-3 flex items-baseline justify-between">
 <p className="kg-eyebrow episode-card-weekday text-white/75 drop-shadow-md">
 {dayOfWeek(episode.meeting_date)}
 </p>
 <p className="episode-card-date font-light text-white tracking-wide tabular-nums drop-shadow-md">
 {formatDateShort(episode.meeting_date)}
 </p>
 </div>
 </div>
 </button>
 );
 }

 // Compact one-line row for unprocessed episodes. Sits in the same
 // grid cell as a full card but takes ~1/4 the height. Renders the
 // (operator) amber tag in the canonical operator-only visual
 // language (#F5A524) so the row reads as "this is operator-only
 // visual real estate, not a publicly-visible card." Clickable —
 // opens the broadcast page which will itself surface its empty
 // state honestly.
 if (isUnprocessed) {
 // Split "MEETING TYPE - MON DD, YYYY" so the date can be pinned as a
 // non-shrinking flex child (James 2026-06-24 — when the meeting-type
 // portion is long like "PLANNING & ZONING COMMISSION", the previous
 // bare `truncate` would lop the date off; we want the meeting-type
 // to be the part that ellipses so the date always reads).
 //
 // When the title doesn't carry a date suffix at all (some parsers emit
 // bare "CITY COUNCIL" without the date), synthesize one from
 // episode.meeting_date so every card shows its date consistently —
 // fixes the dateless-card inconsistency James flagged 2026-06-24.
 const fullTitle = episode.meeting_title || "(untitled)";
 const lastDash = fullTitle.lastIndexOf(" - ");
 const hasDateTail = lastDash > 0 && /\b\d{4}\s*$/.test(fullTitle);
 const titleHead = hasDateTail ? fullTitle.slice(0, lastDash) : fullTitle;
 let titleTail = hasDateTail ? fullTitle.slice(lastDash) : "";
 if (!titleTail && episode.meeting_date) {
 const parts = episode.meeting_date.split("-");
 if (parts.length === 3) {
 const months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
 const monthIdx = parseInt(parts[1], 10) - 1;
 if (monthIdx >= 0 && monthIdx < 12) {
 titleTail = ` - ${months[monthIdx]} ${parseInt(parts[2], 10)}, ${parts[0]}`;
 }
 }
 }
 return (
 <button
 onClick={onOpen}
 className="group col-span-full sm:col-span-1 text-left rounded-md border border-dashed border-[var(--line)] hover:border-[#F5A524]/40 hover:bg-[#F5A524]/5 transition-all duration-150 px-3 py-2 bg-[var(--canvas)]/40"
 title="Operator-only — meeting record exists but no broadcast content has been processed yet"
 >
 {/* Title on top, status on bottom (2026-06-24 — James): the prior
 top-row ISO date duplicated the date that already lives inside
 the meeting_title string ("CITY COUNCIL - APR 21, 2026").
 Removing the duplicate + dropping the status pill to the bottom
 gives the card a cleaner identity→state hierarchy and a more
 even rectangular footprint inside the dashed border. */}
 <div className="flex items-baseline gap-0 min-w-0 text-[10px] text-foreground/40 uppercase tracking-wider">
 <span className="truncate min-w-0">{titleHead}</span>
 {titleTail && (
 <span className="flex-shrink-0 whitespace-nowrap">{titleTail}</span>
 )}
 </div>
 {/* Single amber-chrome pill carries both operator-context (the amber
 background + border IS the operator visual language) AND the
 UNPROCESSED state (red text). Consolidates the prior two-span
 UNPROCESSED + (operator) shape — James 2026-06-24: the color
 does the operator-signaling, the text does the state-signaling. */}
 <div className="flex items-center gap-2 flex-wrap text-[11px] leading-none mt-1.5">
 <span
 className="inline-flex items-center px-1.5 py-0.5 rounded border border-[#F5A524]/50 bg-[#F5A524]/10 text-[var(--alert-red)] text-[10px] font-semibold uppercase tracking-widest"
 title="Operator-only visual artifact — viewers signed in to the public will not see this row; the amber chrome is the operator-only color cue"
 >
 Unprocessed
 </span>
 </div>
 </button>
 );
 }

 const cardSrc = episodeCardForTitle(episode.meeting_title);
 // For meetings that fall through to the default placeholder (no recognized
 // meeting type in the artwork), overlay the title in a small caption so
 // the user still knows what kind of meeting they're looking at. Known
 // types already carry their identity in the artwork itself.
 const isDefaultCard = cardSrc.endsWith("/_default.png");
 return (
 <button
 onClick={onOpen}
 className="group text-left rounded-xl border border-[var(--line)] hover:border-[var(--line-strong)] hover:-translate-y-0.5 transition-all duration-200 overflow-hidden bg-[var(--canvas)]"
 >
 {/* Card == thumbnail. The placeholder artwork carries the meeting
 identity (e.g., the city-council.png art reads "CITY COUNCIL"),
 so the only overlays we add are dynamic per-episode info: the
 actual meeting day + date and the "On Air" pill when a broadcast
 is ready. Sizing stays at aspect-video for the existing grid. */}
 <div className="episode-card-face aspect-video relative overflow-hidden">
 <img
 src={cardSrc}
 alt=""
 className="absolute inset-0 w-full h-full object-cover transition-transform duration-300 group-hover:scale-[1.02]"
 onError={(e) => {
 const img = e.currentTarget;
 if (!img.src.endsWith("/episodes/_default.png")) {
 img.src = "/episodes/_default.png";
 }
 }}
 />
 {/* dark gradient bottom-third so the date text sits over the image
 without competing with the artwork */}
 <div
 className="absolute inset-x-0 bottom-0 h-1/2 pointer-events-none"
 style={{
 background:
 "linear-gradient(to top, rgba(0,0,0,0.82) 0%, rgba(0,0,0,0.40) 55%, rgba(0,0,0,0) 100%)",
 }}
 />
 {hasBroadcast && isPublished && (
 <span
 className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-[var(--success-green)]/15 border border-[var(--success-green)]/30 backdrop-blur-sm"
 title="Broadcast published and publicly visible"
 >
 <span className="kg-dot-active" style={{ width: 6, height: 6 }} />
 <span className="text-[9px] font-semibold uppercase tracking-widest text-[var(--success-green)]">
 On Air
 </span>
 </span>
 )}
 {isDraft && (
 <span
 className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-[#F5A524]/15 border border-[#F5A524]/40 backdrop-blur-sm"
 title="Broadcast processed but NOT yet published — operator-only view; the amber chrome is the operator-only color cue"
 >
 <span className="text-[9px] font-semibold uppercase tracking-widest text-[#F5A524]">
 Draft
 </span>
 </span>
 )}
 {/* Default-placeholder fallback caption (only shown when the
 meeting type isn't recognized — the artwork is generic in that
 case so we need to surface the title separately). */}
 {isDefaultCard && (
 <div className="absolute top-2 left-3 right-16">
 <p className="text-[11px] font-semibold uppercase tracking-widest text-white/85 line-clamp-1 drop-shadow-md">
 {meetingTypeFromTitle(episode.meeting_title)}
 </p>
 </div>
 )}
 {/* Tag pills — max 2 to keep the card uncluttered. Sit above the
 date row, inside the gradient mask. Mirrors the BroadcastPage
 tag style at compact scale. Falls through (no DOM) when the
 meeting has no episode_tags yet (NotebookLM not run, or no
 categorizable items in the meeting). */}
 {(() => {
 const tags = parseEpisodeTags(episode.episode_tags).slice(0, 2);
 if (tags.length === 0) return null;
 return (
 <div className="absolute bottom-9 left-3 right-3 flex flex-wrap gap-1 pointer-events-none opacity-0 group-hover:opacity-100 transition-opacity duration-200">
 {tags.map((t, ti) => {
 const c = TAG_COLOR[t.category];
 return (
 <span
 key={ti}
 className="inline-flex items-center px-1.5 py-[1px] rounded text-[8px] font-semibold uppercase tracking-wider border max-w-full truncate"
 style={{
 color: c,
 backgroundColor: `${c}1F`, // ~12% alpha — slightly stronger than detail-view for legibility over thumbnail
 borderColor: `${c}55`, // ~33% alpha
 }}
 title={t.text}
 >
 {t.text}
 </span>
 );
 })}
 </div>
 );
 })()}
 <div className="absolute bottom-2 left-3 right-3 flex items-baseline justify-between">
 <p className="kg-eyebrow episode-card-weekday text-white/75 drop-shadow-md">
 {dayOfWeek(episode.meeting_date)}
 </p>
 <p className="episode-card-date font-light text-white tracking-wide tabular-nums drop-shadow-md">
 {formatDateShort(episode.meeting_date)}
 </p>
 </div>
 </div>
 </button>
 );
}

// ── Page ───────────────────────────────────────────────────────────

export default function ChannelsPage({
 onNavigate,
 selectCounty,
 selectCity,
 selectNonce,
 resetNonce,
}: ChannelsPageProps) {
 const publicPlane = isPublicPlane();
 const currentUser = useCurrentUser();
 const [hidePlaceholders] = useHidePlaceholders();
 // Default landing: STATE level — "Pick a county" (chunk 3, James's
 // onboarding reversal 2026-05-29). A fresh visitor is taught the structure
 // by the spacious centered Tuner (state → county → city), and only once
 // they reach a city does the condensed outline rail appear. (Was: default
 // straight to Kingman/city; reversed to "pick-your-path" so the navigation
 // explains itself.)
 const [selectedState, setSelectedState] = useState("AZ");
 const [selectedCounty, setSelectedCounty] = useState<string | null>(null);
 const [selectedCity, setSelectedCity] = useState<string | null>(null);

 // V1-Catalog-1 (2026-06-12) — DB-driven catalog tree replaces the
 // hardcoded ARIZONA_COUNTIES + MOHAVE_CITIES constants below. Tree
 // is { states: [{ state, counties: [{ county, cities: [{ name, ... }] }] }] }.
 // Loaded once on mount, cached in state.
 const [channelsTree, setChannelsTree] = useState<{
 ok: boolean;
 states: Array<{
 state: string;
 counties: Array<{
 county: string;
 cities: Array<{
 name: string;
 meeting_count: number;
 broadcast_count: number;
 status: CityStatus;
 last_meeting: string | null;
 first_meeting: string | null;
 lat?: number | null;
 lng?: number | null;
 }>;
 }>;
 }>;
 } | null>(null);
 // V1-Catalog-1 — year-pager state. Years per city; current selection
 // defaults to the current calendar year, persists across sibling-city
 // switches within the rail.
 const [availableYears, setAvailableYears] = useState<string[]>([]);
 const [selectedYear, setSelectedYear] = useState<string>(
 String(new Date().getFullYear())
 );
 // City-level rail mode: false = drilled (–state → ––county → –––cities, the
 // default); true = browsing the state's county list (a rail-only survey that
 // never disturbs the channel). Resets to drilled when the channel city
 // changes (effect below).
 const [railShowsCounties, setRailShowsCounties] = useState(false);
 const [episodes, setEpisodes] = useState<Episode[]>([]);
 // metadata tier. Kept separate from the internal episode state so
 // owner draft mode remains byte-for-byte on its established data path.
 const [catalogEpisodes, setCatalogEpisodes] = useState<Episode[]>([]);
 const [loadingCatalogEpisodes, setLoadingCatalogEpisodes] = useState(false);
 const [loadingEpisodes, setLoadingEpisodes] = useState(false);
 // cache awareness — the operator should see when data is stale
 // and choose to refresh explicitly, rather than have a page-load
 // silently re-scrape Kingman's site every 6 hours.
 const [lastScraped, setLastScraped] = useState<string | null>(null);
 const [cacheAgeSeconds, setCacheAgeSeconds] = useState<number | null>(null);
 // Mobile left-rail drawer. On desktop the rail flows inside the
 // grid column; on mobile (< md) it slides in as a fixed overlay so the
 // narrow viewport gives the right side priority. Closed by default —
 // operator opens via the hamburger button to pick a sibling city.
 const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
 // — clicking a gray state opens this popover naming the how-to
 // repo doc + the contact email. Null = closed; string = state.name.
 const [openSeatState, setOpenSeatState] = useState<string | null>(null);
 const [isStale, setIsStale] = useState<boolean>(false);
 const [refreshing, setRefreshing] = useState<boolean>(false);
 // — the hosted instance carries no parser recipes, so its
 // Refresh can never succeed; the sealed answer retires the button in
 // favor of the honest sentence (2026-07-10: the operator's refresh
 // click clobbered the rendered list with the sealed answer's empty
 // events — the city "disappeared" until reload).
 const [hostedSealed, setHostedSealed] = useState<boolean>(false);
 const [refreshError, setRefreshError] = useState<string | null>(null);
 // Channel search moved to the TopBar (V1-Polish-12, 2026-06-14) — the
 // header's consolidated Channel/Meetings pill + its type-ahead state
 // were retired in favor of the primary TopBarSearch → search-page flow.

 // Cast/Episodes toggle at city-level (). viewMode persists across
 // sibling-city switches via the rail (user is "in cast mode") but resets
 // when the user navigates up via breadcrumb. selectedMember is the
 // currently-drilled member's seat_id (null = Cast hub list view).
 const [viewMode, setViewMode] = useState<"episodes" | "cast" | "schedule">("episodes");
 const [selectedMember, setSelectedMember] = useState<CastMemberSummary | null>(
 null
 );

 // Phase 3 — operator-mode toggle. Default for owners is drafts-ON
 // (operators see everything when browsing their own site without
 // needing to remember a URL param). Default for non-owners (and
 // anonymous) is drafts-OFF — they only see the publicly-published
 // meetings. Owners can explicitly opt out via ?drafts=false to
 // preview "what the public sees" without signing out.
 //
 // RBAC: non-owner viewers can't see drafts regardless of URL param.
 // The `includeDrafts` final value AND-gates on `currentUser.isOwner`.
 const [includeDraftsRaw, setIncludeDraftsRaw] = useState<boolean>(() => {
 if (typeof window === "undefined") return false;
 const param = new URLSearchParams(window.location.search).get("drafts");
 if (param === "true") return true;
 if (param === "false") return false;
 // No explicit param — default true; the RBAC AND-gate below means
 // non-owners still see only published rows.
 return true;
 });
 const includeDrafts = currentUser.isOwner && includeDraftsRaw;

 const toggleIncludeDrafts = useCallback(() => {
 if (!currentUser.isOwner) return;
 setIncludeDraftsRaw(prev => {
 const next = !prev;
 if (typeof window !== "undefined") {
 const url = new URL(window.location.href);
 // Persist the explicit choice in both directions — without this,
 // a toggle-off would revert to the drafts-ON default on refresh.
 url.searchParams.set("drafts", next ? "true" : "false");
 window.history.replaceState({}, "", url.toString());
 }
 return next;
 });
 }, [currentUser.isOwner]);

 // V1-Catalog-1 — fetch the channels tree once on mount. Drives every
 // state/county/city pick downstream. Fails-quiet — the existing
 // hardcoded fallback in counties/cities derivations covers the gap
 // until the request returns (catalog is read-only; no risk of stale
 // writes).
 useEffect(() => {
 let cancelled = false;
 fetchForPlane({
 publicPath: "/public-api/channels/tree",
 operatorPath: "/api/channels/tree",
 })
 .then(r => r.json())
 .then(d => {
 if (cancelled) return;
 if (d && d.ok) setChannelsTree(d);
 })
 .catch(() => {});
 return () => {
 cancelled = true;
 };
 }, []);

 // V1-Catalog-1 — fetch the per-city available years whenever the city
 // changes. The year-pager renders these at the bottom of the episode
 // list with the current year highlighted.
 useEffect(() => {
 if (!selectedCity) {
 setAvailableYears([]);
 return;
 }
 let cancelled = false;
 const operatorPath = `/api/cities/${encodeURIComponent(selectedCity)}/years${
 includeDrafts ? "?include_drafts=true" : ""
 }`;
 fetchForPlane({
 publicPath: `/public-api/cities/${encodeURIComponent(selectedCity)}/years`,
 operatorPath,
 })
 .then(r => r.json())
 .then(d => {
 if (cancelled) return;
 if (d && d.ok) {
 const ys: string[] = Array.isArray(d.years) ? d.years : [];
 setAvailableYears(ys);
 // Owner draft mode keeps the established newest-available anchor.
 // Public mode holds the current selection long enough for /v1 to
 // reveal a year that has facts-only meetings but no episodes yet.
 if (includeDrafts && ys.length > 0 && !ys.includes(selectedYear)) {
 setSelectedYear(ys[0]);
 }
 } else {
 setAvailableYears([]);
 }
 })
 .catch(() => {
 if (!cancelled) setAvailableYears([]);
 });
 return () => {
 cancelled = true;
 };
 // Intentionally NOT depending on selectedYear — we only refetch years
 // on city change; the user's year picks within the city don't re-fire
 // this. eslint-disable-next-line react-hooks/exhaustive-deps
 }, [selectedCity, includeDrafts]);

 // Anonymous/public-preview metadata tier. One /v1 page is deliberately the
 // complete fetch window: the server caps it at 100 rows and selectedYear is
 // the existing episode-column pager, so this never grows into an unbounded
 // city scrape. Owners in draft mode already receive these meetings through
 // the internal endpoint and skip this request entirely.
 useEffect(() => {
 if (!selectedCity || includeDrafts) {
 setCatalogEpisodes([]);
 setLoadingCatalogEpisodes(false);
 return;
 }

 let aborted = false;
 setLoadingCatalogEpisodes(true);
 const qs = new URLSearchParams({
 city: selectedCity,
 year: selectedYear || String(new Date().getFullYear()),
 });
 fetch(`/v1/catalog/meetings?${qs.toString()}`)
 .then(async response => {
 if (!response.ok) throw new Error(`Catalog request failed (${response.status})`);
 return response.json();
 })
 .then(body => {
 if (aborted) return;
 const rows = Array.isArray(body?.meetings) ? body.meetings : [];
 const comingSoon: Episode[] = rows
 .filter((row: any) => row?.availability !== "published" && row?.public_id)
 .map((row: any) => ({
 public_id: String(row.public_id),
 availability: String(row.availability || "coming_soon"),
 meeting_title: String(row.title || ""),
 meeting_date: String(row.date || ""),
 meeting_time: String(row.time || ""),
 meeting_location: String(row.location || ""),
 notebook_id: null,
 is_published: false,
 }));
 setCatalogEpisodes(comingSoon);
 setLoadingCatalogEpisodes(false);
 })
 .catch(() => {
 if (aborted) return;
 setCatalogEpisodes([]);
 setLoadingCatalogEpisodes(false);
 });

 return () => {
 aborted = true;
 };
 }, [selectedCity, selectedYear, includeDrafts]);

 // Fetch episodes for the selected city. Clear immediately on city change
 // so we never render a sibling's stale data underneath the new city's
 // breadcrumb/header — the inline dots loader covers the gap.
 //
 // (2026-05-13): the `force` parameter explicitly opts into a live
 // re-scrape against the city's site. Default (force=false) returns
 // whatever's cached, regardless of staleness, with metadata for the UI
 // to show "X hours old". The page load itself never triggers a re-scrape.
 //
 // Phase 3 (2026-05-18): `includeDrafts` from the URL ?drafts=true gets
 // forwarded; default behavior hides draft (not-yet-published) meetings.
 const loadEpisodes = useCallback(
 (city: string, force: boolean, year?: string) => {
 if (publicPlane && force) return () => {};
 if (force) setRefreshing(true);
 else setLoadingEpisodes(true);
 let aborted = false;
 // V1-Catalog-1: the year-filtered cache read goes through the
 // new /api/cities/<city>/meetings endpoint. The legacy
 // /api/calendar/events endpoint is reserved for the explicit
 // operator refresh path (force=true), since it carries the
 // live-scrape fallback semantics that the catalog read shouldn't.
 const useNewEndpoint = !force;
 const fetchPromise = useNewEndpoint
 ? (() => {
 const yearParam = year || selectedYear || String(new Date().getFullYear());
 const qs = new URLSearchParams({ year: yearParam });
 if (includeDrafts) qs.set("include_drafts", "true");
 return fetchForPlane({
 publicPath: `/public-api/cities/${encodeURIComponent(city)}/meetings?${new URLSearchParams({ year: yearParam }).toString()}`,
 operatorPath: `/api/cities/${encodeURIComponent(city)}/meetings?${qs.toString()}`,
 }).then(res => res.json());
 })()
 : fetch("/api/calendar/events", {
 method: "POST",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify({ cityName: city, refresh: force, includeDrafts }),
 }).then(res => res.json());
 fetchPromise
 .then(data => {
 if (aborted) return;
 // A refresh that couldn't happen must never replace what's on
 // screen with nothing. Sealed instance → keep the list, retire
 // the button, say why. Failed refresh → keep the list, name
 // the error, leave the button for a retry.
 if (force && (data?.registry_sealed === true || data?.source === "registry_sealed")) {
 setHostedSealed(true);
 setRefreshError(null);
 setRefreshing(false);
 return;
 }
 if (force && data?.success === false) {
 setRefreshError(
 typeof data?.error === "string" && data.error
 ? data.error
 : "the refresh didn't complete — the cached data stays",
 );
 setRefreshing(false);
 return;
 }
 setRefreshError(null);
 const events = Array.isArray(data?.events) ? data.events : [];
 setEpisodes(events);
 setLastScraped(typeof data?.last_scraped === "string" ? data.last_scraped : null);
 setCacheAgeSeconds(
 typeof data?.cache_age_seconds === "number" ? data.cache_age_seconds : null
 );
 setIsStale(data?.is_stale === true);
 setLoadingEpisodes(false);
 setRefreshing(false);
 })
 .catch(() => {
 if (aborted) return;
 setEpisodes([]);
 setLoadingEpisodes(false);
 setRefreshing(false);
 });
 return () => {
 aborted = true;
 };
 },
 [includeDrafts, publicPlane, selectedYear]
 );

 useEffect(() => {
 if (!selectedCity) {
 setEpisodes([]);
 setLastScraped(null);
 setCacheAgeSeconds(null);
 setIsStale(false);
 return;
 }
 setEpisodes([]);
 setLastScraped(null);
 setCacheAgeSeconds(null);
 setIsStale(false);
 const cancel = loadEpisodes(selectedCity, false);
 return cancel;
 }, [selectedCity, loadEpisodes]);

 // Entering a city (or switching cities) shows that city's cities in the
 // rail, never a stale county survey left over from a previous browse.
 useEffect(() => {
 setRailShowsCounties(false);
 }, [selectedCity]);

 // Session-32 (2026-07-04): all three rail-selection handlers now scroll
 // to page top after flipping local state. Rail selections don't call
 // App.tsx navigate() (they're intra-ChannelsPage state changes, not a
 // view swap), so the window.scrollTo(0,0) reset that navigate() would
 // have applied doesn't fire. Symptom operator reported: clicking a
 // different city while scrolled to the bottom of the previous city's
 // episode list left the scroll position at the old offset — the new
 // city's panel was often shorter than the previous one, so what was
 // visible ended up being the page footer instead of the new city's
 // channel banner. Behavior: "smooth" so the reset reads as a real
 // navigation, not a jarring jump.
 const onSelectState = (code: string) => {
 setSelectedState(code);
 setSelectedCounty(null);
 setSelectedCity(null);
 setEpisodes([]);
 setViewMode("episodes");
 setSelectedMember(null);
 window.scrollTo({ top: 0, behavior: "smooth" });
 };

 // 2026-07-04: county-switch preserves the city-detail viewLevel when
 // the user was already viewing a city, so cross-county clicks feel
 // symmetric with sibling-city clicks (both stay in the detail layout
 // instead of bouncing the user back to the picker). If the user was
 // NOT in a city yet, county click acts as before — county-picker
 // layout unchanged.
 const countyJumpNeedsCityRef = useRef(false);
 const onSelectCounty = (county: string) => {
 if (selectedCity !== null) countyJumpNeedsCityRef.current = true;
 setSelectedCounty(county);
 setSelectedCity(null);
 setEpisodes([]);
 setViewMode("episodes");
 setSelectedMember(null);
 window.scrollTo({ top: 0, behavior: "smooth" });
 };

 const onSelectCity = (city: string) => {
 setSelectedCity(city);
 // Preserve viewMode across sibling-city switches via the rail (if user
 // is "in cast mode" on Kingman and clicks Bullhead, they probably want
 // to see Bullhead's cast, not bounce back to Episodes). selectedMember
 // resets because it's city-specific.
 setSelectedMember(null);
 window.scrollTo({ top: 0, behavior: "smooth" });
 };

 // Inside a member profile, clicking the city crumb takes us back to the
 // Cast hub (member grid) for that city — not all the way back to county.
 const goToCityLevel = () => {
 setSelectedMember(null);
 };

 // Apply a TopBar channel-jump (V1-Polish-12 follow-up, 2026-06-14). The
 // TopBarSearch dropdown picks a county/city and navigates home with these
 // params; apply them once per pick. The signature ref guards against
 // re-applying on unrelated re-renders — so manual rail navigation (which
 // doesn't change these props) is never clobbered — while `selectNonce`
 // ensures re-picking the same channel after moving away still re-fires.
 const lastChannelPickRef = useRef<string | null>(null);
 useEffect(() => {
 if (!selectCounty) return;
 const sig = `${selectCounty}|${selectCity ?? ""}|${selectNonce ?? ""}`;
 if (lastChannelPickRef.current === sig) return;
 lastChannelPickRef.current = sig;
 setSelectedCounty(selectCounty);
 setSelectedCity(selectCity ?? null);
 setViewMode("episodes");
 setSelectedMember(null);
 setEpisodes([]);
 setRailShowsCounties(false);
 }, [selectCounty, selectCity, selectNonce]);

 // V1-Polish-24 (2026-06-14): the Z-SPAN logo + Channels nav reset the
 // drill-down to the state-level "Pick a county" picker (defaulting to
 // Arizona). They navigate home with a changing `resetToCounties` nonce;
 // without this, clicking them while already on home left ChannelsPage's
 // county/city state untouched ("nothing happens — just refreshes"). Ref-
 // guarded so it only fires on an actual click, not on unrelated re-renders.
 const lastResetRef = useRef<number | undefined>(undefined);
 useEffect(() => {
 if (resetNonce === undefined) return;
 if (lastResetRef.current === resetNonce) return;
 lastResetRef.current = resetNonce;
 setSelectedState("AZ");
 setSelectedCounty(null);
 setSelectedCity(null);
 setEpisodes([]);
 setViewMode("episodes");
 setSelectedMember(null);
 setRailShowsCounties(false);
 }, [resetNonce]);

 const activeStateName = STATES.find(s => s.code === selectedState)?.name ?? "";

 // V1-Catalog-1: derive counties + cities from the DB-driven channels
 // tree instead of the hardcoded ARIZONA_COUNTIES + MOHAVE_CITIES
 // constants. Every county/city the tree returns is `active: true` —
 // it has catalog presence (≥1 cached meeting). The V2-processed
 // distinction (V1_PROCESSED_CITIES) is a separate flag, applied
 // downstream at the StatusDot.
 //
 // Fallback to the static lists when the tree hasn't loaded yet
 // (first render) OR if the API is unavailable — keeps Mohave reachable
 // even in degraded modes.
 const treeStateNode = useMemo(() => {
 if (!channelsTree) return null;
 return channelsTree.states.find(s => s.state === activeStateName) || null;
 }, [channelsTree, activeStateName]);

 const counties: ReadonlyArray<{ name: string; active: boolean; status?: CityStatus }> =
 useMemo(() => {
 if (treeStateNode) {
 // V1-Polish-19 → -22: a county rolls its cities' status up (live /
 // in-progress / coming-soon) so its dot + label match what's inside.
 // Clickable unless it's pure scaffold (active = status !== "scaffold").
 return treeStateNode.counties.map(c => {
 const status = deriveCountyStatus(c.cities);
 return { name: c.county, active: status !== "scaffold", status };
 });
 }
 return selectedState === "AZ" ? ARIZONA_COUNTIES : [];
 }, [treeStateNode, selectedState]);

 const cities: ReadonlyArray<{ name: string; active: boolean; status?: CityStatus }> =
 useMemo(() => {
 if (treeStateNode && selectedCounty) {
 const countyNode = treeStateNode.counties.find(c => c.county === selectedCounty);
 if (countyNode) {
 return countyNode.cities.map(c => ({
 name: c.name,
 // V1-Polish-21: scaffold cities are inert — there's no data behind
 // them yet, so they render gray + non-clickable (the dimmed text +
 // missing chevron come along for free via `active`). live/cached
 // stay clickable. F28 postponed (2026-06-19) is also inert — the
 // parser is flagged stale-and-shouldn't-render, so treat the same
 // as scaffold for click-affordance purposes.
 active: c.status !== "scaffold" && c.status !== "postponed",
 status: c.status,
 }));
 }
 return [];
 }
 return selectedCounty === "Mohave" ? MOHAVE_CITIES : [];
 }, [treeStateNode, selectedCounty]);

 // Companion to onSelectCounty: when the user was already in a city
 // and switched counties, auto-select the first ACTIVE city of the
 // new county so the layout stays in the city-detail view instead
 // of bouncing back to the picker. The ref was set during
 // onSelectCounty; the `cities` memo is what recomputes for the
 // new county, so this effect fires exactly once per cross-county
 // click when the new list is ready.
 useEffect(() => {
 if (!countyJumpNeedsCityRef.current) return;
 if (cities.length === 0) return;
 const firstActive = cities.find(c => c.active);
 if (firstActive) {
 setSelectedCity(firstActive.name);
 countyJumpNeedsCityRef.current = false;
 } else {
 // New county has no active cities — nothing to auto-select;
 // clear the flag so a subsequent county click doesn't
 // spuriously re-trigger.
 countyJumpNeedsCityRef.current = false;
 }
 }, [cities]);

 // Determine which view to show: deepest active selection wins.
 const viewLevel: "state" | "county" | "city" = !selectedCounty
 ? "state"
 : !selectedCity
 ? "county"
 : "city";

 // Merge published/internal cards with the facts-only metadata tier. The
 // public_id is authoritative when both sources eventually carry it; the
 // stable date+title identity prevents today's internal row from doubling a
 // catalog row without widening the internal endpoint in this chunk.
 const sortedEpisodes = useMemo(() => {
 const merged: Episode[] = [];
 const publicIds = new Set<string>();
 const identities = new Set<string>();
 const identityFor = (episode: Episode) =>
 `${episode.meeting_date || ""}\u0000${(episode.meeting_title || "").trim().toLocaleLowerCase()}`;

 for (const episode of [...episodes, ...catalogEpisodes]) {
 if (!isVisibleLocalEpisode(episode)) continue;
 const identity = identityFor(episode);
 if (
 (episode.public_id && publicIds.has(episode.public_id)) ||
 identities.has(identity)
 ) {
 continue;
 }
 merged.push(episode);
 if (episode.public_id) publicIds.add(episode.public_id);
 identities.add(identity);
 }

 return merged.sort((a, b) => {
 const da = a.meeting_date || "";
 const db = b.meeting_date || "";
 return db.localeCompare(da);
 });
 }, [episodes, catalogEpisodes]);

 const visibleEpisodes = useMemo(
 () => filterVisibleEpisodes(sortedEpisodes, hidePlaceholders),
 [hidePlaceholders, sortedEpisodes],
 );

 // Processed = generated card content exists, or the catalog explicitly
 // marks the episode published. Drives the green/total count badge on the
 // Episodes binder tab (V1-Polish-14) — the count that used to sit in the
 // now-removed "Recent Episodes" header.
 const processedEpisodeCount = useMemo(
 () =>
 sortedEpisodes.filter(
 e =>
 e.availability === "published" ||
 !!e.episode_tagline ||
 !!e.episode_tags,
 ).length,
 [sortedEpisodes],
 );

 // The internal years endpoint is publication-filtered for public callers.
 // Add the active year when /v1 found metadata-only meetings there so the
 // pager truthfully represents the merged episode column.
 const displayedYears = useMemo(() => {
 const years = new Set(availableYears);
 if (catalogEpisodes.length > 0) years.add(selectedYear);
 return Array.from(years).sort((a, b) => b.localeCompare(a));
 }, [availableYears, catalogEpisodes.length, selectedYear]);

 return (
 <div className="min-h-screen bg-background text-foreground flex flex-col">
 {/* State tab strip + page utilities. The Z-SPAN brand chip + the
 standalone Guide button + the NotebookLM auth pill were retired
 when the universal TopBar landed (commit C critique blocker) —
 the TopBar above already carries the brand, the Guide link, and
 a health dot that subsumes the auth pill. The quick-jump search
 dropdown stays here because it's a different feature than the
 TopBar's global Search link (it's a channel/meeting picker, not
 a route). The state tab strip is canonical and stays. */}
 <header className="sticky top-11 z-40 bg-[var(--canvas)]/95 backdrop-blur border-b border-[var(--line)]">
 <div className="max-w-[1600px] mx-auto px-6 lg:px-10 py-4 flex items-center justify-between gap-6">
 <div className="flex items-center gap-4 min-w-0">
 {/* State tab strip — all 50 states in a fixed-width rail; Arizona
 active, every other state an open seat rendered grayed.
 Overflows horizontally with a thin draggable slider (kg-scroll)
 so the row never widens past its lane. */}
 <div className="hidden md:flex items-center gap-1 overflow-x-auto kg-scroll max-w-[40vw] lg:max-w-[48vw] pb-1 min-w-0">
 {STATES.map(s => (
 <button
 key={s.code}
 onClick={() =>
 s.active
 ? onSelectState(s.code)
 : /* Open seat → non-modal invitation popover.
 The rail IS the directory: gray state clicks open
 this popover naming the repo doc + the contact
 email. When the repo goes public reopening)
 the doc URL becomes live; when a seat gets claimed
 its state's click flips to the member library's URL. */
 setOpenSeatState(s.name)
 }
 className={`flex-shrink-0 whitespace-nowrap px-3 py-1.5 text-[11px] font-semibold uppercase tracking-widest rounded-md transition-colors
 ${
 selectedState === s.code
 ? "bg-[var(--surface-3)] text-white"
 : s.active
 ? "text-foreground/70 hover:text-white hover:bg-[var(--surface-3)]/40"
 : "text-foreground/30 hover:text-foreground/60 hover:bg-[var(--surface-3)]/20"
 }`}
 title={s.active ? `Browse ${s.name}` : `Open seat — run Z-SPAN ${s.name}`}
 >
 {s.name}
 </button>
 ))}
 </div>
 </div>

 <div className="flex items-center gap-3">
 {/* Phase 3 — operator-mode indicator. Only visible when
 ?drafts=true is in the URL; click X to revert to public
 view. Bright color signals "you are seeing things the
 public doesn't." */}
 {includeDrafts && (
 <button
 onClick={toggleIncludeDrafts}
 className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md border border-[#F5A524]/50 bg-[#F5A524]/10 hover:bg-[#F5A524]/20 text-[#F5A524] text-[11px] font-semibold uppercase tracking-widest transition-colors"
 title="Operator mode: drafts visible. Click to switch back to public view."
 >
 <span>Operator · drafts</span>
 <span className="text-[#F5A524]/70 text-[14px] leading-none">×</span>
 </button>
 )}
 {/* Search moved to the TopBar (V1-Polish-12, 2026-06-14) — the
 consolidated Channel/Meetings pill that used to live here is
 now the primary TopBarSearch above. The Guide button + the
 NotebookLM AuthStatusPill were retired earlier (the TopBar
 carries Guide + a health dot that subsumes the auth pill). */}
 </div>
 </div>

 </header>

 {/* open-seat popover — clicking a gray state opens this,
 naming the future repo doc + the contact email. No auto-mailto,
 no page-load prompt. Backdrop click / Esc dismisses. */}
 {openSeatState && (
 <div
 role="dialog"
 aria-modal="true"
 aria-labelledby="open-seat-title"
 className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
 onClick={() => setOpenSeatState(null)}
 onKeyDown={e => e.key === "Escape" && setOpenSeatState(null)}
 >
 <div
 className="max-w-md mx-6 rounded-lg border border-[var(--line)] bg-[var(--surface-2)] p-6 shadow-xl"
 onClick={e => e.stopPropagation()}
 >
 <h2 id="open-seat-title" className="text-lg font-semibold text-white mb-2">
 Z-SPAN {openSeatState}
 </h2>
 <p className="text-sm text-foreground/80 leading-relaxed mb-5">
 Open seat in the Z-SPAN ecosystem, see{" "}
 <span className="text-foreground/60 italic">
 GitHub link (coming soon)
 </span>{" "}
 or contact{" "}
 <a
 href="mailto:anitacigawet@pm.me"
 className="text-white underline underline-offset-2"
 >
 anitacigawet@pm.me
 </a>{" "}
 for info.
 </p>
 <button
 type="button"
 onClick={() => setOpenSeatState(null)}
 className="text-[11px] uppercase tracking-widest text-foreground/60 hover:text-white transition-colors"
 >
 Close
 </button>
 </div>
 </div>
 )}

 {/* Channel content shell (chunk 1b). Fixed two-zone layout at every
 level: a persistent OUTLINE rail on the left (the drilled path +
 the current level's children) and the Tuner on the right (Pick a
 county / Pick a city / the episodes calendar; centered in chunk 3).
 The old 0px-to-230px rail morph is retired -- the rail is always
 present; the cherished slide is the Tuner's key={viewLevel}
 drill-fade, kept exactly as it was. */}
 <div className="flex-1 max-w-[1600px] w-full mx-auto px-6 lg:px-10 py-8">
 {/* Layout depends on depth (chunk 3, James's onboarding model 2026-05-29):
 - STATE / COUNTY = the centered Tuner ALONE, no rail. The teaching
 surface: the spacious "Pick a county / city" that explains the dots
 + how the structure works (the spiritual onboarding).
 - CITY = the rail appears on the left (the condensed, permanent form
 of that picker) + the city's channel on the right. From here the
 rail navigates; the channel only swaps on a new city pick (the rail
 can survey counties freely without disturbing it). */}
 <div
 className={
 viewLevel === "city"
 ? "grid grid-cols-1 md:grid-cols-[260px_1fr] gap-x-0 md:gap-x-10 min-h-[calc(100vh-11rem)]"
 : "min-h-[calc(100vh-11rem)]"
 }
 >
 {/* The OUTLINE rail — only at city level (the condensed navigator). */}
 {viewLevel === "city" && (
 <div className="min-w-0">
 {/* px-1 (not just pr-1): the OutlineRows use -mx-1 to bleed their
 hover background into the gutter, but `overflow-y-auto` forces
 overflow-x to clip too (CSS computes overflow-x:visible →
 auto when overflow-y is auto), which was shaving the left edge
 off the depth-0 "Arizona" row. pl-1 gives the negative margin
 room so nothing clips (V1-Polish-17). */}
 <aside className="hidden md:block sticky top-24 self-start max-h-[calc(100vh-7rem)] overflow-y-auto kg-scroll px-1">
 <OutlineRail
 stateName={activeStateName}
 counties={counties}
 cities={cities}
 selectedCounty={selectedCounty}
 selectedCity={selectedCity}
 showingCounties={railShowsCounties}
 onShowCounties={() => setRailShowsCounties(true)}
 onShowCities={() => setRailShowsCounties(false)}
 onSelectCounty={onSelectCounty}
 onSelectCity={onSelectCity}
 onHome={() => onNavigate("home", { resetToCounties: Date.now() })}
 />
 </aside>

 {/* Mobile: a compact path summary that opens the outline drawer. */}
 <button
 type="button"
 onClick={() => setMobileSidebarOpen(true)}
 className="md:hidden w-full mb-5 flex items-center gap-2 px-3 py-2 rounded-md border border-[var(--line)] bg-[var(--surface)]/40 text-left"
 aria-label="Open the channel outline"
 >
 <Menu className="w-4 h-4 text-foreground/50 flex-shrink-0" aria-hidden="true" />
 <span className="text-[10px] uppercase tracking-[0.22em] text-foreground/45 flex-shrink-0">
 Now Browsing
 </span>
 <span className="text-[12px] text-white truncate">
 {[activeStateName, selectedCounty, selectedCity].filter(Boolean).join(" / ")}
 </span>
 </button>

 {/* Mobile drawer — the same OutlineRail in a left-anchored overlay. */}
 {mobileSidebarOpen && (
 <>
 <div
 className="md:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm"
 onClick={() => setMobileSidebarOpen(false)}
 aria-hidden="true"
 />
 <div className="md:hidden fixed inset-y-0 left-0 z-50 w-[280px] bg-[var(--background)] border-r border-[var(--line)] pt-6 px-5 overflow-y-auto">
 <button
 type="button"
 onClick={() => setMobileSidebarOpen(false)}
 className="absolute top-3 right-3 text-foreground/50 hover:text-white transition-colors p-1"
 aria-label="Close the channel outline"
 >
 <X className="w-5 h-5" />
 </button>
 <div className="mt-6">
 <OutlineRail
 stateName={activeStateName}
 counties={counties}
 cities={cities}
 selectedCounty={selectedCounty}
 selectedCity={selectedCity}
 showingCounties={railShowsCounties}
 onShowCounties={() => setRailShowsCounties(true)}
 onShowCities={() => setRailShowsCounties(false)}
 onSelectCounty={c => {
 onSelectCounty(c);
 setMobileSidebarOpen(false);
 }}
 onSelectCity={c => {
 onSelectCity(c);
 setMobileSidebarOpen(false);
 }}
 onHome={() => {
 onNavigate("home", { resetToCounties: Date.now() });
 setMobileSidebarOpen(false);
 }}
 />
 </div>
 </div>
 </>
 )}
 </div>
 )}

 {/* Content column. At city = the channel (right of the rail); at
 state/county = the centered Tuner. key={viewLevel} keeps the
 cherished drill-fade on level changes. */}
 <div
 key={viewLevel}
 className={`min-w-0 animate-in fade-in-0 duration-300 ${
 viewLevel === "city" ? "" : "max-w-3xl mx-auto w-full"
 }`}
 >
 {viewLevel === "state" ? (
 <ChannelLevelView
 heading="Pick a county"
 headingHint={
 <DefinitionHint
 term="County"
 definition="The largest territorial division for local government within a state of the U.S."
 sourceUrl="https://www.merriam-webster.com/dictionary/county"
 />
 }
 subheading="Since this is currently a one-man project, only a few locations are live while I manage compute. Thank you for your patience."
 >
 {counties.map(c => (
 // Follow gated to cities only (2026-07-04, operator
 // direction). Counties are drill-down containers, not
 // subscription targets — a citizen following the whole
 // county is really following "everything my county
 // does," which is the wrong resolution for a personalized
 // feed. Follow-target is city-scoped; county row stays
 // a simple drill-in.
 <div key={c.name}>
 <ChannelListRow
 name={c.name}
 active={c.active}
 status={c.status}
 meta={statusLabel(c.status ?? (c.active ? "live" : "scaffold"))}
 onClick={c.active ? () => onSelectCounty(c.name) : undefined}
 />
 </div>
 ))}
 </ChannelLevelView>
 ) : viewLevel === "county" ? (
 <ChannelLevelView
 heading={`Pick a city in ${selectedCounty}`}
 headingHint={
 <DefinitionHint
 term="City"
 definition="An inhabited place of greater size, population, or importance than a town or village."
 sourceUrl="https://www.merriam-webster.com/dictionary/city"
 />
 }
 subheading="Since this is currently a one-man project, only a few locations are live while I manage compute. Thank you for your patience."
 >
 {cities.length === 0 ? (
 <p className="px-2 py-6 text-[13px] text-muted-foreground italic normal-case tracking-normal">
 No active cities yet for {selectedCounty}.
 </p>
 ) : (
 cities.map(city => {
 // V1-Polish-19 → -21 → -22: data-driven status + meta. live →
 // "Live" (green, clickable); cached → "In progress" (amber,
 // clickable — scraped, content being built); scaffold → gray
 // + inert "Coming soon" (non-clickable, no data yet). Clicking
 // is gated on `active` (scaffold cities have no data behind
 // them).
 const cityMeta = statusLabel(city.status);
 return (
 <div key={city.name} className="flex items-center gap-2">
 <div className="flex-1 min-w-0">
 <ChannelListRow
 name={city.name}
 active={city.active}
 status={city.status}
 meta={cityMeta}
 onClick={
 city.active ? () => onSelectCity(city.name) : undefined
 }
 />
 </div>
 <FollowButton
 targetType="city"
 targetKey={city.name}
 variant="ghost"
 />
 </div>
 );
 })
 )}
 </ChannelLevelView>
 ) : (
 // City level — Episodes calendar OR Cast hub/profile ().
 // The tab toggle swaps the inner content; sibling-city rail
 // navigation preserves the active tab. Drilling into a member
 // hides the toggle (the city crumb in the breadcrumb becomes
 // the way back to the Cast hub).
 <div className="flex flex-col gap-5">
 {/* Channel-poster hero — the dusk-cinematic city landscape
 from city_intelligence/channels/. Only rendered at the
 Episodes/Cast hub (not when drilled into a member, where
 the member portrait is the visual focus). The poster
 falls back to the generic Arizona-default if the
 per-city asset is missing. */}
 {!selectedMember && selectedCity && (
 <div className="relative z-10 overflow-hidden rounded-xl border border-[var(--line)] aspect-[21/6] sm:aspect-[24/5] bg-[var(--canvas)]">
 <img
 src={channelPosterForCity(selectedCity)}
 alt=""
 aria-hidden="true"
 className="absolute inset-0 w-full h-full object-cover select-none pointer-events-none"
 onError={e => {
 const img = e.currentTarget;
 // Drop to generic Arizona poster, then hide entirely
 // if that also fails.
 if (!img.src.endsWith("/channels/_az-default-poster.png")) {
 img.src = "/channels/_az-default-poster.png";
 } else {
 img.style.display = "none";
 }
 }}
 />
 {/* Gradient + city label overlay so the poster reads as
 a channel ident, not just a backdrop. */}
 <div
 className="absolute inset-0 pointer-events-none"
 style={{
 background:
 "linear-gradient(to top, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.30) 50%, rgba(0,0,0,0) 100%)",
 }}
 />
 {/* City-name watermark only (2026-06-24). The prior
 "CHANNEL" eyebrow + bottom-right "COUNTY · STATE"
 label were both redundant — the breadcrumb rail
 on the left already names the county + state, and
 the city-name itself anchors the channel context.
 Per operator-direction: don't interrupt the
 poster image with duplicate text. */}
 <div className="absolute bottom-3 left-4 right-4 flex items-end">
 <div className="min-w-0">
 <p className="text-[18px] sm:text-[22px] font-light text-white tracking-wide truncate drop-shadow-md">
 {selectedCity}
 </p>
 </div>
 </div>

 {/* V1-Polish-14 (2026-06-14): the old "Channel page" button,
 minimized to a gear in the banner's top-right. Routes to the
 full channel page for now; intended to become the "channel
 statistics" panel (stats-for-nerds) per James. */}
 {currentUser.isOwner && (
 <button
 type="button"
 onClick={() =>
 onNavigate("city", {
 cityName: selectedCity,
 countyName: selectedCounty,
 })
 }
 className="absolute top-3 right-3 z-20 inline-flex h-7 w-7 items-center justify-center rounded-md border border-white/15 bg-black/30 text-white/60 backdrop-blur-sm transition-colors hover:bg-black/50 hover:text-white"
 title="Channel page · statistics (more coming)"
 aria-label="Channel page and statistics"
 >
 <Settings className="h-3.5 w-3.5" />
 </button>
 )}
 </div>
 )}

 {!selectedMember && (
 // V1-Polish-6 (rev2): Episodes / Cast / Schedule as notebook
 // binder index tabs that extend FROM the channel banner. The
 // strip is pulled up so its top edge tucks ~3px BEHIND the
 // banner (banner has z-10, strip z-0) — so the tabs read as
 // protruding from the banner rather than floating below it,
 // and the squared top corners are hidden by the banner. Small
 // leading inset (pl-1). Future tabs (Truth Book, …) slot into
 // the TABS list. Minimalist palette preserved.
 <div
 className="relative z-0 -mt-[23px] flex items-stretch gap-1 self-start pl-1"
 role="tablist"
 aria-label="Channel view mode"
 >
 {([
 { key: "episodes", label: "Episodes" },
 { key: "cast", label: "Cast" },
 ...(!publicPlane
 ? [{ key: "schedule" as const, label: "Schedule" }]
 : []),
 ] as const).map(tab => {
 const active = viewMode === tab.key;
 return (
 <button
 key={tab.key}
 role="tab"
 aria-selected={active}
 onClick={() => setViewMode(tab.key)}
 className={`px-4 pt-3.5 pb-2 text-[10px] font-semibold uppercase tracking-[0.18em] rounded-b-lg border border-t-0 transition-colors ${
 active
 ? "bg-[var(--surface-3)] text-white border-[var(--line-strong)]"
 : "bg-[var(--surface)]/50 text-foreground/55 border-[var(--line)] hover:text-white hover:bg-[var(--surface-3)]/60"
 }`}
 >
 {tab.label}
 {/* V1-Polish-14: the green processed / grey total count,
 consolidated here from the removed header. Only on the
 Episodes tab, and only when the city has episodes. */}
 {tab.key === "episodes" && sortedEpisodes.length > 0 && (
 <span className="ml-1.5 tabular-nums">
 {hidePlaceholders ? (
 <span className="text-emerald-400">
 {visibleEpisodes.length}
 </span>
 ) : (
 <>
 <span className="text-emerald-400">
 {processedEpisodeCount}
 </span>
 <span className="text-foreground/30">
 /{sortedEpisodes.length}
 </span>
 </>
 )}
 </span>
 )}
 </button>
 );
 })}
 </div>
 )}

 {viewMode === "cast" && selectedMember ? (
 <CastMemberPanel
 cityName={selectedCity!}
 seatId={selectedMember.seat_id || ""}
 onBack={goToCityLevel}
 onOpenTruthBook={topic =>
 onNavigate("truth-book", {
 cityName: selectedCity!,
 seatId: selectedMember.seat_id || "",
 topic,
 })
 }
 />
 ) : viewMode === "cast" ? (
 <CastPanel
 cityName={selectedCity!}
 countyName={selectedCounty}
 onSelectMember={member => setSelectedMember(member)}
 />
 ) : viewMode === "schedule" ? (
 // V1-Polish-10: Schedule is its own binder tab now (was bundled
 // under Cast). The standalone "Channel page" button is flagged
 // to retire once this view is refactored, per James 2026-06-14.
 <MeetingSchedulePanel city={selectedCity!} />
 ) : (
 <>
 {/* V1-Polish-14 (2026-06-14): the "Recent Episodes · City"
 header + its green/total count were removed — the count now
 lives on the Episodes binder tab, and the standalone
 "Channel page" button became the gear in the banner's
 top-right. */}

 {/* cache-awareness banner. Page load NEVER triggers a
 re-scrape — operator opts in via the Refresh button.
 Subtle when fresh (the operator doesn't need to know
 cache details); louder when stale (operator should
 consider refreshing). */}
 {/* V1-Polish-9: cache freshness is operator telemetry, not
 public info — "Cache stale / last scraped" reads as a
 schema dump to a non-owner. Gate the whole banner
 to the owner; anonymous/public viewers never see it. */}
 {lastScraped && currentUser.isOwner && (
 <div
 className={`flex items-center justify-between px-3 py-1.5 mt-3 text-[10px] uppercase tracking-[0.18em] rounded border ${
 isStale
 ? "border-amber-500/40 bg-amber-500/5 text-amber-200/90"
 : "border-[var(--line)] bg-[var(--surface)]/40 text-foreground/35"
 }`}
 >
 <span className="tabular-nums">
 {isStale ? "Cache stale · " : "Cache fresh · "}
 last scraped{" "}
 {cacheAgeSeconds == null || cacheAgeSeconds < 60
 ? "just now"
 : cacheAgeSeconds < 3600
 ? `${Math.round(cacheAgeSeconds / 60)}m ago`
 : cacheAgeSeconds < 86400 * 2
 ? `${Math.round(cacheAgeSeconds / 3600)}h ago`
 : `${Math.round(cacheAgeSeconds / 86400)}d ago`}
 </span>
 <OwnerOnly hideWhileLoading={true}>
 {hostedSealed ? (
 <span
 className="text-[10px] text-foreground/50"
 title="This deployment carries no parser recipes (open framework, sealed registry) — the ingestion pipeline updates this data and syncs it here."
 >
 live re-scrape isn't available on the hosted
 site — the pipeline updates this data
 </span>
 ) : (
 <>
 <button
 onClick={() =>
 selectedCity && loadEpisodes(selectedCity, true)
 }
 disabled={refreshing}
 className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] uppercase tracking-[0.18em] border border-[var(--line)] bg-[var(--surface-2)] hover:bg-[var(--surface-3)] text-foreground/70 hover:text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
 title="Re-scrape this city from its source website. Uses a network request and may take a few seconds."
 >
 <RefreshCw
 className={`w-3 h-3 ${refreshing ? "animate-spin" : ""}`}
 />
 {refreshing ? "Refreshing" : "Refresh"}
 </button>
 {refreshError && (
 <span className="text-[10px] text-[var(--alert-red)]/80">
 {refreshError}
 </span>
 )}
 </>
 )}
 </OwnerOnly>
 </div>
 )}

 {(loadingEpisodes || loadingCatalogEpisodes) && sortedEpisodes.length === 0 ? (
 // First-load state (no prior data) — show a small inline
 // dots indicator. We deliberately do NOT use a centered
 // kg-card here because for sibling-channel switches the
 // big card flashed and visually shifted the layout (per
 // James 2026-05-08). Subsequent loads dim the existing
 // calendar instead of replacing it; see the divide-y
 // wrapper below.
 <div className="py-12 text-center">
 <div className="kg-dots inline-flex">
 <span /> <span /> <span />
 </div>
 <p className="text-sm text-muted-foreground mt-4">
 Loading episodes…
 </p>
 </div>
 ) : visibleEpisodes.length === 0 ? (
 <EmptyChannelState
 title="No episodes yet"
 message="No episodes yet, only this sleeping cat. Please check back later!"
 variant="episodes"
 onBrowseOther={() => setSelectedCity(null)}
 onOpenGuide={() => onNavigate("guide")}
 />
 ) : (
 // Hairline hierarchy (channel-guide aesthetic, NOT a grid):
 // · Month divider: --line-strong (10% white) — clearest streak.
 // · Vertical rail: --line (5%) — month label ↔ weeks.
 // · Week divider: --line/40 (~2%) — super faint rhythm.
 // When swapping cities (sibling-channel click in the rail),
 // we dim the calendar to 60% opacity instead of replacing it
 // with a big loading card. The new data slides in once the
 // fetch returns — keeps layout stable, no flash.
 <div
 className={`divide-y divide-[var(--line-strong)] transition-opacity duration-200 ${
 loadingEpisodes || loadingCatalogEpisodes
 ? "opacity-60 pointer-events-none"
 : "opacity-100"
 }`}
 >
 {groupByMonthAndWeek(visibleEpisodes).map(monthGroup => (
 <section
 key={monthGroup.monthKey}
 className="grid grid-cols-[88px_1fr] sm:grid-cols-[110px_1fr] gap-x-4 sm:gap-x-6 py-5 first:pt-0"
 >
 <div className="pt-1 border-r border-[var(--line)]">
 <h3
 className="text-[22px] sm:text-[26px] font-light text-white tracking-tight leading-none"
 title={`${monthGroup.monthLabel} ${monthGroup.monthYear}`}
 >
 {monthGroup.monthLabel}
 </h3>
 {monthGroup.monthYear !== new Date().getFullYear() && (
 <p className="text-[10px] text-muted-foreground/40 mt-1 tabular-nums">
 {monthGroup.monthYear}
 </p>
 )}
 </div>

 <div className="divide-y divide-[var(--line)]/40 min-w-0">
 {monthGroup.weeks.map(week => (
 <div
 key={week.weekStart.toISOString()}
 className="grid grid-cols-[28px_1fr] sm:grid-cols-[36px_1fr] gap-x-2 sm:gap-x-3 items-start py-2.5 first:pt-0 last:pb-0"
 >
 <span
 className="text-[8px] uppercase tracking-[0.18em] text-foreground/30 pt-1.5 tabular-nums select-none"
 title={`Week ${week.weekNumber} of ${monthGroup.monthLabel}`}
 >
 wk {week.weekNumber}
 </span>
 <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 min-w-0">
 {week.episodes.map(ep => (
 <EpisodeCard
 key={ep.public_id ?? ep.id}
 episode={ep}
 onOpen={() => {
 // Route to the CLI-catalog placeholder ONLY
 // for genuine coming-soon rows. A published
 // episode (availability=undefined on the
 // legacy endpoint, but is_published) must
 // reach the real broadcast — shares the
 // EpisodeCard render signature via
 // isCatalogPlaceholder().
 if (publicPlane && ep.public_id) {
 onNavigate("broadcast", { publicId: ep.public_id });
 } else if (isCatalogPlaceholder(ep) && ep.public_id) {
 onNavigate("broadcast", { publicId: ep.public_id });
 } else if (ep.id !== undefined) {
 onNavigate("broadcast", { meetingId: ep.id });
 } else if (ep.public_id) {
 onNavigate("broadcast", { publicId: ep.public_id });
 }
 }}
 />
 ))}
 </div>
 </div>
 ))}
 </div>
 </section>
 ))}
 </div>
 )}

 {/* V1-Catalog-1 year-pager (2026-06-12). Minimalistic
 bottom-of-list nav. Default = current year; available
 years come from /api/cities/<city>/years. Hidden when
 the city has no dated data (no years to switch to). */}
 {displayedYears.length > 0 && (
 <nav
 aria-label="Year"
 className="mt-10 pt-5 border-t border-[var(--line)] flex items-center justify-center gap-4 flex-wrap"
 >
 <span className="kg-eyebrow text-[10px] text-foreground/30 mr-2 tracking-[0.18em]">
 YEAR
 </span>
 {displayedYears.map(y => {
 const isActive = y === selectedYear;
 return (
 <button
 key={y}
 onClick={() => {
 setSelectedYear(y);
 window.scrollTo({ top: 0, behavior: "smooth" });
 }}
 className={`text-[12px] tabular-nums transition-colors ${
 isActive
 ? "text-white font-medium"
 : "text-foreground/30 hover:text-foreground/60"
 }`}
 aria-current={isActive ? "true" : undefined}
 >
 {y}
 </button>
 );
 })}
 </nav>
 )}
 </>
 )}
 </div>
 )}
 </div>
 </div>

 {/* Bottom utility row — centered, Terms-of-Service-style. Non-owners
 see only the tagline (no OwnerOnly buttons), so centering keeps it
 from sitting alone on the left; the owner's operator buttons center
 just beneath it.

 TravelersOdometer is centered beneath the tagline at every width
 (2026-07-26 — it used to be absolute/right-aligned on sm+, which
 overlapped the tagline's tail once the copy grew long enough to
 reach it; see the note at its render site below). */}
 <footer className="relative mt-8 pt-6 border-t border-[var(--line)] flex flex-col items-center gap-4 text-center">
 <div className="text-[11px] text-muted-foreground leading-relaxed">
 {/* PRIOR FOOTER COPY (2026-06-21 — preserved for revert):
 <p>
 This website was made possible by{" "}
 <a href="https://www.opengovtplatform.org/government-transparency/sunshine-laws"
 target="_blank" rel="noopener noreferrer" className="zs-sunshine"
 title="Sunshine laws — the open-meeting & open-records laws that make the public record public"
 >sunshine</a>{" "}
 laws, ensuring a <span className="zs-brighter">brighter</span> tomorrow.
 </p>
 */}
 <p>
 Made possible thanks to{" "}
 <a
 href="https://www.opengovtplatform.org/government-transparency/sunshine-laws"
 target="_blank"
 rel="noopener noreferrer"
 className="zs-sunshine"
 title="Sunshine laws — the open-meeting & open-records laws that make the public record public"
 >sunshine</a>{" "}
 laws, your local{" "}
 <a
 href="https://en.wikipedia.org/wiki/Municipal_clerk#United_States"
 target="_blank"
 rel="noopener noreferrer"
 className="no-underline hover:text-foreground transition-colors"
 title="Municipal clerks — the local officials who keep the public record public"
 >municipal clerk</a>, and{" "}
 <a
 href="https://www.noaa.gov/submarine-cables"
 target="_blank"
 rel="noopener noreferrer"
 className="no-underline hover:text-foreground transition-colors"
 title="Submarine cables — the largely-invisible physical infrastructure that carries the internet between continents"
 >technology</a>, ensuring a <span className="zs-brighter">brighter</span> tomorrow.
 </p>
 {/* No copyright/attribution line here by operator decision
 2026-07-26. History: this block carried the AGPL-3.0 § 13
 source offer + § 7(b) attribution until open-sourcing was
 postponed, then "© 2026 Z-SPAN · no warranty", then
 just the copyright line — each pass shedding a clause that
 had stopped being true. The operator removed the remainder
 because the entity question behind it (which legal entity
 operates Z-SPAN) is unsettled, and an unsettled claim does
 not belong on the public page. Copyright subsists without
 notice under Berne — nothing is waived by its absence.
 Restore a line here only when the entity + license posture
 are BOTH settled; see git history for prior wordings. */}
 </div>
 {/* Session-31 (2026-07-04) — the four owner-only footer
 buttons (Terminal / Pipeline / Parsers / Settings) were
 retired in favor of consolidating every owner surface into
 the TopBar's OPERATOR dropdown. Rationale from operator:
 the buttons were duplicating navigation already available
 top-right, and cluttering a public-facing footer with
 owner-only affordances that no visitor sees. Settings
 specifically moved into the SignInPill account-pill
 dropdown, per "AI-provider settings live under the user
 identity that scopes them." */}

 {/* TravelersOdometer — centered beneath the tagline, in the slot
 the copyright line vacated (operator 2026-07-26). It was
 previously `sm:absolute sm:right-0 sm:top-6`, which overlapped
 the tagline's last words ("…a brighter tomorrow") whenever the
 copy ran wide enough to reach the chip — the absolute element
 was outside flow, so nothing reserved space for it. As a normal
 flex child of the centered footer column it can't collide with
 the copy at any width, and the footer's `gap-4` spaces it. */}
 <div>
 <TravelersOdometer />
 </div>
 </footer>
 </div>
 </div>
 );
}

// The sleeping-cat mark for the empty-episodes state — the operator's final
// illustration (2026-07-08), replacing the temporary inline-SVG placeholder
// from V1-Polish-7. The asset is the operator's hand-drawn cat converted to
// white-ink-on-transparent (luminance-keyed alpha), so it composites cleanly
// on the card background — the old /states/no-episodes.png opaque-box problem
// is solved at the asset level rather than by drawing in code. Tone is set by
// opacity on the mount (an <img> ignores text color classes).
function SleepingCat({ className }: { className?: string }) {
 return (
 <img
 src="/states/sleeping-cat.png"
 alt=""
 aria-hidden="true"
 className={className}
 draggable={false}
 />
 );
}

function EmptyChannelState({
 title,
 message,
 variant = "channel",
 onBrowseOther,
 onOpenGuide,
}: {
 title: string;
 message: string;
 // "channel" = no city/county picked (coming-soon-channel illustration)
 // "episodes" = a city picked but no meetings yet (sleeping-cat state)
 variant?: "channel" | "episodes";
 // Episodes variant only — friendly "don't dead-end" links so a visitor on
 // an empty channel always has somewhere to go (V1-Polish-7).
 onBrowseOther?: () => void;
 onOpenGuide?: () => void;
}) {
 if (variant === "episodes") {
 return (
 <div className="kg-card-2 border-dashed p-10 sm:p-12 text-center flex flex-col items-center">
 <SleepingCat className="h-28 sm:h-32 w-auto mb-5 opacity-80" />
 <p className="text-sm text-foreground/70 leading-relaxed max-w-sm mx-auto">
 {message}
 </p>
 {(onBrowseOther || onOpenGuide) && (
 <div className="mt-5 flex flex-col items-center gap-2 text-[13px]">
 {onBrowseOther && (
 <button
 type="button"
 onClick={onBrowseOther}
 className="text-foreground/70 hover:text-white underline underline-offset-4 decoration-dotted decoration-foreground/30 hover:decoration-foreground/60 transition-colors"
 >
 Other Channels
 </button>
 )}
 {onOpenGuide && (
 <button
 type="button"
 onClick={onOpenGuide}
 className="text-foreground/70 hover:text-white underline underline-offset-4 decoration-dotted decoration-foreground/30 hover:decoration-foreground/60 transition-colors"
 >
 Guide
 </button>
 )}
 </div>
 )}
 </div>
 );
 }

 // "channel" variant — coming-soon illustration + message.
 return (
 <div className="kg-card-2 border-dashed p-10 sm:p-12 text-center flex flex-col items-center">
 <picture className="block mb-4">
 <img
 src="/states/coming-soon-channel.png"
 alt=""
 aria-hidden="true"
 className="h-28 sm:h-32 w-auto opacity-80 select-none"
 onError={e => {
 // Fall back to the original lucide Tv icon if the illustration
 // file is missing (so dev/staging never renders a broken image).
 const img = e.currentTarget;
 img.style.display = "none";
 const fallback = img.nextElementSibling as HTMLElement | null;
 if (fallback) fallback.style.display = "block";
 }}
 />
 <Tv
 className="w-8 h-8 mx-auto text-muted-foreground/40"
 style={{ display: "none" }}
 />
 </picture>
 <h3 className="text-base font-semibold text-foreground/70 mb-1.5">{title}</h3>
 <p className="text-sm text-muted-foreground leading-relaxed max-w-md mx-auto">
 {message}
 </p>
 </div>
 );
}

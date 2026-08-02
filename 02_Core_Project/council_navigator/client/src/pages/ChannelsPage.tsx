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
import { filterVisibleEpisodes, isCatalogPlaceholder, useHidePlaceholders, } from "../hooks/useHidePlaceholders";
import { fetchForPlane } from "../lib/planeFetch";
import { isPublicPlane } from "../lib/trustPlane";
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
interface Episode {
    id?: number;
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
    episode_tagline?: string | null;
    episode_tags?: string | null;
    is_published?: boolean;
    published_at?: string | null;
}
interface ChannelsPageProps {
    onNavigate: (view: string, params?: any) => void;
    selectCounty?: string;
    selectCity?: string;
    selectNonce?: number;
    resetNonce?: number;
}
function formatDateShort(s: string | undefined): string {
    if (!s)
        return "—";
    try {
        const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s + "T00:00:00") : new Date(s);
        if (isNaN(d.getTime()))
            return s;
        return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    }
    catch {
        return s;
    }
}
function dayOfWeek(s: string | undefined): string {
    if (!s)
        return "";
    try {
        const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s + "T00:00:00") : new Date(s);
        if (isNaN(d.getTime()))
            return "";
        return d.toLocaleDateString("en-US", { weekday: "short" }).toUpperCase();
    }
    catch {
        return "";
    }
}
function meetingTypeFromTitle(t: string): string {
    if (!t)
        return "Council Meeting";
    const dashIdx = t.indexOf(" - ");
    return dashIdx > 0 ? t.slice(0, dashIdx).trim() : t.trim();
}
type CityStatus = "live" | "cached" | "scaffold" | "postponed";
export const V1_PROCESSED_CITIES: ReadonlySet<string> = new Set([
    "Kingman",
    "Bullhead City",
    "Lake Havasu City",
    "Colorado City",
]);
function StatusDot({ active, status, sizePx, }: {
    active: boolean;
    status?: CityStatus;
    sizePx: number;
}) {
    if (status === "live") {
        return (<span className="kg-dot-active flex-shrink-0" style={{ width: sizePx, height: sizePx }} aria-hidden="true"/>);
    }
    if (status === "cached") {
        return (<span className="flex-shrink-0 rounded-full" style={{
                width: sizePx * 0.85,
                height: sizePx * 0.85,
                background: "#f5a524",
                opacity: 0.85,
            }} aria-hidden="true"/>);
    }
    if (status === "scaffold" || status === "postponed") {
        return (<span className="bg-foreground/15 flex-shrink-0 rounded-full" style={{ width: sizePx * 0.6, height: sizePx * 0.6 }} aria-hidden="true"/>);
    }
    if (active) {
        return (<span className="kg-dot-active flex-shrink-0" style={{ width: sizePx, height: sizePx }} aria-hidden="true"/>);
    }
    return (<span className="rounded-full bg-foreground/15 flex-shrink-0" style={{ width: sizePx * 0.6, height: sizePx * 0.6 }} aria-hidden="true"/>);
}
function statusLabel(status?: CityStatus): string {
    if (status === "live")
        return "";
    if (status === "cached")
        return "In progress";
    return "Coming soon";
}
function deriveCountyStatus(cities: ReadonlyArray<{
    status?: CityStatus;
}>): CityStatus {
    if (cities.some(c => c.status === "live"))
        return "live";
    if (cities.some(c => c.status === "cached"))
        return "cached";
    return "scaffold";
}
function ChannelListRow({ name, active, meta, onClick, compact = false, selected = false, status, }: {
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
        return (<button type="button" disabled={!clickable} onClick={onClick} aria-current={selected ? "page" : undefined} className={`group w-full text-left flex items-center gap-2 py-1.5 px-2 -mx-2 rounded-md transition-colors
          ${selected
                ? "bg-[var(--surface)]/70 cursor-default"
                : clickable
                    ? "hover:bg-[var(--surface)]/40 cursor-pointer"
                    : "cursor-default"}`}>
        <StatusDot active={active} status={status} sizePx={5}/>
        <span className={`text-[12.5px] truncate tracking-wide ${selected
                ? "text-white font-medium"
                : active
                    ? "text-foreground/65 font-light group-hover:text-white"
                    : "text-foreground/30 font-light"}`}>
          {name}
        </span>
      </button>);
    }
    return (<button type="button" disabled={!clickable} onClick={onClick} className={`group w-full text-left flex items-center justify-between gap-4 py-3.5 px-2 -mx-2 rounded-md transition-colors
        ${clickable
            ? "hover:bg-[var(--surface)]/50 cursor-pointer"
            : "cursor-default"}`}>
      <div className="flex items-center gap-3 min-w-0">
        <StatusDot active={active} status={status} sizePx={6}/>
        <span className={`text-[15px] truncate tracking-wide ${active
            ? "text-white font-light group-hover:text-white"
            : "text-foreground/35 font-light"}`}>
          {name}
        </span>
      </div>
      <div className="flex items-center gap-3 flex-shrink-0">
        {meta && (<span className={`text-[10px] uppercase tracking-[0.18em] ${active ? "text-foreground/45" : "text-foreground/25"}`}>
            {meta}
          </span>)}
        {clickable && (<ArrowRight className="w-3.5 h-3.5 text-foreground/30 group-hover:text-white group-hover:translate-x-0.5 transition-all"/>)}
      </div>
    </button>);
}
type OutlineTone = "current" | "crumb" | "option" | "disabled";
function OutlineRow({ depth, label, tone, meta, onClick, status, }: {
    depth: number;
    label: string;
    tone: OutlineTone;
    meta?: string;
    onClick?: () => void;
    status?: CityStatus;
}) {
    const clickable = !!onClick;
    const marker = "–".repeat(depth + 1);
    const markerSlotWidth = (depth + 1) * 7;
    const textCls = tone === "current"
        ? "text-white font-medium"
        : tone === "crumb"
            ? "text-foreground/55 group-hover:text-white"
            : tone === "option"
                ? "text-foreground/70 group-hover:text-white"
                : "text-foreground/30";
    return (<button type="button" disabled={!clickable} onClick={onClick} aria-current={tone === "current" ? "page" : undefined} style={{ paddingLeft: depth * 14 }} className={`group w-full text-left flex items-center gap-2 py-1.5 px-1 -mx-1 rounded-md transition-colors ${clickable ? "hover:bg-[var(--surface)]/40 cursor-pointer" : "cursor-default"}`}>
      {depth > 0 && (<span className="text-[11px] text-foreground/25 tabular-nums select-none flex-shrink-0 inline-flex items-center" style={{ minWidth: markerSlotWidth }} aria-hidden="true">
          {status ? (<StatusDot active={status !== "scaffold"} status={status} sizePx={5}/>) : (marker)}
        </span>)}
      <span className={`text-[12.5px] truncate tracking-wide ${textCls}`}>
        {label}
      </span>
      {meta && (<span className="ml-auto text-[9px] uppercase tracking-[0.18em] text-foreground/25 flex-shrink-0">
          {meta}
        </span>)}
    </button>);
}
function ExitRampSign({ stateName, onSurveyCounties, onHome, surveyDisabled, }: {
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
        if (!surveyDisabled)
            onSurveyCounties?.();
    };
    return (<div className={`block w-full mb-2 transition-opacity ${surveyDisabled ? "cursor-default opacity-90" : "cursor-pointer hover:opacity-95"}`} onClick={handleBodyClick} role="button" tabIndex={surveyDisabled ? -1 : 0} onKeyDown={(e) => {
            if (!surveyDisabled && (e.key === "Enter" || e.key === " ")) {
                e.preventDefault();
                onSurveyCounties?.();
            }
        }} aria-label={surveyDisabled
            ? `Currently viewing ${stateName}`
            : `Currently viewing ${stateName} — click to see counties`} title={`Currently viewing ${stateName}`}>
      <svg viewBox="0 0 220 100" xmlns="http://www.w3.org/2000/svg" className="w-full h-auto" role="img" aria-hidden>
        
        <rect x="3" y="3" width="214" height="94" rx="8" fill="var(--highway-sign-blue)" stroke="#FFFFFF" strokeWidth="2.5"/>
        
        <g className="exit-button group" onClick={handleExitClick} style={{ cursor: "pointer" }} role="link" aria-label="Back to Z-SPAN home">
          
          <rect x="90" y="9" width="40" height="20" fill="transparent"/>
          <text x="110" y="24" fontSize="13" fontFamily="Inter, sans-serif" fontWeight="800" letterSpacing="2.5" fill="#FFFFFF" textAnchor="middle" className="transition-opacity hover:opacity-80">
            EXIT
          </text>
        </g>
        
        <g transform="translate(190, 12)" fill="none" stroke="#FFFFFF" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="0" y1="9" x2="14" y2="9"/>
          <polyline points="9,3 15,9 9,15"/>
        </g>
        
        <text x="110" y="60" fontSize="9" fontFamily="Inter, sans-serif" fontWeight="600" letterSpacing="2" fill="#FFFFFF" textAnchor="middle">
          CURRENTLY VIEWING
        </text>
        
        <text x="110" y="86" fontSize={label.length > 12 ? "18" : label.length > 8 ? "22" : "24"} fontFamily="Inter, sans-serif" fontWeight="800" letterSpacing="1.5" fill="#FFFFFF" textAnchor="middle">
          {label}
        </text>
      </svg>
    </div>);
}
function OutlineRail({ stateName, counties, cities, selectedCounty, selectedCity, showingCounties, onShowCounties, onShowCities, onSelectCounty, onSelectCity, onHome, }: {
    stateName: string;
    counties: ReadonlyArray<{
        name: string;
        active: boolean;
        status?: CityStatus;
    }>;
    cities: ReadonlyArray<{
        name: string;
        active: boolean;
        status?: CityStatus;
    }>;
    selectedCounty: string | null;
    selectedCity: string | null;
    showingCounties: boolean;
    onShowCounties: () => void;
    onShowCities: () => void;
    onSelectCounty: (county: string) => void;
    onSelectCity: (city: string) => void;
    onHome?: () => void;
}) {
    return (<nav aria-label="Channel path" className="flex flex-col gap-1">
      
      <ExitRampSign stateName={stateName} onSurveyCounties={onShowCounties} onHome={onHome} surveyDisabled={showingCounties}/>
      <div key={showingCounties ? "counties" : "cities"} className="flex flex-col gap-1 animate-in fade-in-0 slide-in-from-top-1 duration-300">
        {showingCounties ? (counties.map(c => (<OutlineRow key={c.name} depth={1} label={c.name} tone={c.active ? "option" : "disabled"} status={c.status} meta={statusLabel(c.status ?? (c.active ? "live" : "scaffold"))} onClick={c.active
                ? c.name === selectedCounty
                    ? onShowCities
                    : () => onSelectCounty(c.name)
                : undefined}/>))) : (selectedCounty && (<>
              
              <OutlineRow depth={1} label={selectedCounty} tone="crumb" onClick={onShowCounties}/>
              {cities.map(city => {
                const isCurrent = selectedCity === city.name;
                return (<OutlineRow key={city.name} depth={2} label={city.name} tone={isCurrent ? "current" : city.active ? "option" : "disabled"} status={city.status} meta={statusLabel(city.status)} onClick={city.active && !isCurrent
                        ? () => onSelectCity(city.name)
                        : undefined}/>);
            })}
            </>))}
      </div>
    </nav>);
}
function ChannelLevelView({ heading, headingHint, subheading, children, }: {
    heading: string;
    headingHint?: React.ReactNode;
    subheading?: string;
    children: React.ReactNode;
}) {
    return (<section className="max-w-3xl">
      <div className="mb-7">
        
        
        <h2 className="mb-2 flex items-end gap-2.5 text-[28px] font-light leading-tight tracking-tight text-white sm:text-[32px]">
          <span>{heading}</span>
          <span className="pb-1.5 inline-flex">{headingHint}</span>
        </h2>
        {subheading && (<p className="text-[13px] text-muted-foreground leading-relaxed max-w-prose">
            {subheading}
          </p>)}
      </div>
      <div className="divide-y divide-[var(--line)]/60 border-y border-[var(--line)]/60">
        {children}
      </div>
    </section>);
}
type EpisodeWeekGroup = {
    weekNumber: number;
    weekStart: Date;
    episodes: Episode[];
};
type EpisodeMonthGroup = {
    monthKey: string;
    monthLabel: string;
    monthYear: number;
    episodes: number;
    weeks: EpisodeWeekGroup[];
};
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
        if (!ep.meeting_date)
            continue;
        const d = /^\d{4}-\d{2}-\d{2}/.test(ep.meeting_date)
            ? new Date(ep.meeting_date + "T00:00:00")
            : new Date(ep.meeting_date);
        if (isNaN(d.getTime()))
            continue;
        const monthKey = `${d.getFullYear()}-${String(d.getMonth()).padStart(2, "0")}`;
        const sunday = new Date(d);
        sunday.setDate(d.getDate() - d.getDay());
        sunday.setHours(0, 0, 0, 0);
        const weekKey = `${sunday.getFullYear()}-${String(sunday.getMonth()).padStart(2, "0")}-${String(sunday.getDate()).padStart(2, "0")}`;
        if (!monthMap.has(monthKey))
            monthMap.set(monthKey, new Map());
        const weekMap = monthMap.get(monthKey)!;
        if (!weekMap.has(weekKey))
            weekMap.set(weekKey, []);
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
            const inMonthDate = weekMap.get(wk)![0]?.meeting_date;
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
function isVisibleLocalEpisode(episode: Episode): boolean {
    if (episode.local_video_class === undefined)
        return true;
    return (episode.local_processable !== false ||
        !!episode.notebook_id ||
        !!episode.is_published);
}
function EpisodeCard({ episode, onOpen, }: {
    episode: Episode;
    onOpen: () => void;
}) {
    const isLocalRow = episode.local_video_class !== undefined;
    if (isCatalogPlaceholder(episode)) {
        const cardSrc = episodeCardForTitle(episode.meeting_title);
        const isDefaultCard = cardSrc.endsWith("/_default.png");
        return (<button onClick={onOpen} className="group text-left rounded-xl border border-dashed border-[var(--line)] hover:border-[var(--line-strong)] hover:-translate-y-0.5 transition-all duration-200 overflow-hidden bg-[var(--canvas)]" title="Open this meeting's public facts and CLI handoff">
        <div className="episode-card-face aspect-video relative overflow-hidden">
          <img src={cardSrc} alt="" className="absolute inset-0 w-full h-full object-cover opacity-40 grayscale transition-all duration-300 group-hover:opacity-55 group-hover:scale-[1.02]" onError={(e) => {
                const img = e.currentTarget;
                if (!img.src.endsWith("/episodes/_default.png")) {
                    img.src = "/episodes/_default.png";
                }
            }}/>
          <div className="absolute inset-0 bg-gradient-to-t from-[var(--canvas)] via-[var(--canvas)]/35 to-transparent pointer-events-none"/>
          <span className="absolute top-2 right-2 inline-flex items-center rounded-full border border-[var(--line-strong)] bg-[var(--surface)]/80 px-2 py-0.5 text-[9px] font-medium tracking-wide text-foreground/60 backdrop-blur-sm">
            Episode coming
          </span>
          {isDefaultCard && (<p className="absolute top-3 left-3 right-28 text-[10px] font-semibold tracking-wide text-foreground/65 line-clamp-2">
              {meetingTypeFromTitle(episode.meeting_title)}
            </p>)}
          <div className="absolute inset-x-3 bottom-2 flex items-baseline justify-between gap-3">
            <p className="kg-eyebrow episode-card-weekday text-foreground/50">
              {dayOfWeek(episode.meeting_date)}
            </p>
            <p className="episode-card-date font-light text-foreground/70 tracking-wide tabular-nums">
              {formatDateShort(episode.meeting_date)}
            </p>
          </div>
        </div>
      </button>);
    }
    const hasBroadcast = episode.availability === "published" ||
        !!episode.episode_tagline ||
        !!episode.episode_tags;
    const isPublished = !!episode.is_published || episode.availability === "published";
    const isProcessed = episode.availability === "published" ||
        (hasBroadcast && (!!episode.episode_tagline || !!episode.episode_tags));
    const isDraft = isProcessed && !isPublished;
    const isUnprocessed = !isProcessed;
    if (isLocalRow && isUnprocessed) {
        const cardSrc = episodeCardForTitle(episode.meeting_title);
        const isDefaultCard = cardSrc.endsWith("/_default.png");
        return (<button onClick={onOpen} className="group text-left rounded-xl border border-[var(--line)] hover:border-[var(--line-strong)] hover:-translate-y-0.5 transition-all duration-200 overflow-hidden bg-[var(--canvas)]">
        <div className="episode-card-face aspect-video relative overflow-hidden">
          <img src={cardSrc} alt="" className="absolute inset-0 w-full h-full object-cover transition-transform duration-300 group-hover:scale-[1.02]" onError={(e) => {
                const img = e.currentTarget;
                if (!img.src.endsWith("/episodes/_default.png")) {
                    img.src = "/episodes/_default.png";
                }
            }}/>
          <div className="absolute inset-x-0 bottom-0 h-1/2 pointer-events-none" style={{
                background: "linear-gradient(to top, rgba(0,0,0,0.82) 0%, rgba(0,0,0,0.40) 55%, rgba(0,0,0,0) 100%)",
            }}/>
          {isDefaultCard && (<div className="absolute top-2 left-3 right-16">
              <p className="text-[11px] font-semibold uppercase tracking-widest text-white/85 line-clamp-1 drop-shadow-md">
                {meetingTypeFromTitle(episode.meeting_title)}
              </p>
            </div>)}
          <div className="absolute bottom-2 left-3 right-3 flex items-baseline justify-between">
            <p className="kg-eyebrow episode-card-weekday text-white/75 drop-shadow-md">
              {dayOfWeek(episode.meeting_date)}
            </p>
            <p className="episode-card-date font-light text-white tracking-wide tabular-nums drop-shadow-md">
              {formatDateShort(episode.meeting_date)}
            </p>
          </div>
        </div>
      </button>);
    }
    if (isUnprocessed) {
        const fullTitle = episode.meeting_title || "(untitled)";
        const lastDash = fullTitle.lastIndexOf(" - ");
        const hasDateTail = lastDash > 0 && /\b\d{4}\s*$/.test(fullTitle);
        const titleHead = hasDateTail ? fullTitle.slice(0, lastDash) : fullTitle;
        let titleTail = hasDateTail ? fullTitle.slice(lastDash) : "";
        if (!titleTail && episode.meeting_date) {
            const parts = episode.meeting_date.split("-");
            if (parts.length === 3) {
                const months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
                const monthIdx = parseInt(parts[1], 10) - 1;
                if (monthIdx >= 0 && monthIdx < 12) {
                    titleTail = ` - ${months[monthIdx]} ${parseInt(parts[2], 10)}, ${parts[0]}`;
                }
            }
        }
        return (<button onClick={onOpen} className="group col-span-full sm:col-span-1 text-left rounded-md border border-dashed border-[var(--line)] hover:border-[#F5A524]/40 hover:bg-[#F5A524]/5 transition-all duration-150 px-3 py-2 bg-[var(--canvas)]/40" title="Operator-only — meeting record exists but no broadcast content has been processed yet">
        
        <div className="flex items-baseline gap-0 min-w-0 text-[10px] text-foreground/40 uppercase tracking-wider">
          <span className="truncate min-w-0">{titleHead}</span>
          {titleTail && (<span className="flex-shrink-0 whitespace-nowrap">{titleTail}</span>)}
        </div>
        
        <div className="flex items-center gap-2 flex-wrap text-[11px] leading-none mt-1.5">
          <span className="inline-flex items-center px-1.5 py-0.5 rounded border border-[#F5A524]/50 bg-[#F5A524]/10 text-[var(--alert-red)] text-[10px] font-semibold uppercase tracking-widest" title="Operator-only visual artifact — viewers signed in to the public will not see this row; the amber chrome is the operator-only color cue">
            Unprocessed
          </span>
        </div>
      </button>);
    }
    const cardSrc = episodeCardForTitle(episode.meeting_title);
    const isDefaultCard = cardSrc.endsWith("/_default.png");
    return (<button onClick={onOpen} className="group text-left rounded-xl border border-[var(--line)] hover:border-[var(--line-strong)] hover:-translate-y-0.5 transition-all duration-200 overflow-hidden bg-[var(--canvas)]">
      
      <div className="episode-card-face aspect-video relative overflow-hidden">
        <img src={cardSrc} alt="" className="absolute inset-0 w-full h-full object-cover transition-transform duration-300 group-hover:scale-[1.02]" onError={(e) => {
            const img = e.currentTarget;
            if (!img.src.endsWith("/episodes/_default.png")) {
                img.src = "/episodes/_default.png";
            }
        }}/>
        
        <div className="absolute inset-x-0 bottom-0 h-1/2 pointer-events-none" style={{
            background: "linear-gradient(to top, rgba(0,0,0,0.82) 0%, rgba(0,0,0,0.40) 55%, rgba(0,0,0,0) 100%)",
        }}/>
        {hasBroadcast && isPublished && (<span className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-[var(--success-green)]/15 border border-[var(--success-green)]/30 backdrop-blur-sm" title="Broadcast published and publicly visible">
            <span className="kg-dot-active" style={{ width: 6, height: 6 }}/>
            <span className="text-[9px] font-semibold uppercase tracking-widest text-[var(--success-green)]">
              On Air
            </span>
          </span>)}
        {isDraft && (<span className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-[#F5A524]/15 border border-[#F5A524]/40 backdrop-blur-sm" title="Broadcast processed but NOT yet published — operator-only view; the amber chrome is the operator-only color cue">
            <span className="text-[9px] font-semibold uppercase tracking-widest text-[#F5A524]">
              Draft
            </span>
          </span>)}
        
        {isDefaultCard && (<div className="absolute top-2 left-3 right-16">
            <p className="text-[11px] font-semibold uppercase tracking-widest text-white/85 line-clamp-1 drop-shadow-md">
              {meetingTypeFromTitle(episode.meeting_title)}
            </p>
          </div>)}
        
        {(() => {
            const tags = parseEpisodeTags(episode.episode_tags).slice(0, 2);
            if (tags.length === 0)
                return null;
            return (<div className="absolute bottom-9 left-3 right-3 flex flex-wrap gap-1 pointer-events-none opacity-0 group-hover:opacity-100 transition-opacity duration-200">
              {tags.map((t, ti) => {
                    const c = TAG_COLOR[t.category];
                    return (<span key={ti} className="inline-flex items-center px-1.5 py-[1px] rounded text-[8px] font-semibold uppercase tracking-wider border max-w-full truncate" style={{
                            color: c,
                            backgroundColor: `${c}1F`,
                            borderColor: `${c}55`,
                        }} title={t.text}>
                    {t.text}
                  </span>);
                })}
            </div>);
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
    </button>);
}
export default function ChannelsPage({ onNavigate, selectCounty, selectCity, selectNonce, resetNonce, }: ChannelsPageProps) {
    const publicPlane = isPublicPlane();
    const currentUser = useCurrentUser();
    const [hidePlaceholders] = useHidePlaceholders();
    const [selectedState, setSelectedState] = useState("AZ");
    const [selectedCounty, setSelectedCounty] = useState<string | null>(null);
    const [selectedCity, setSelectedCity] = useState<string | null>(null);
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
    const [availableYears, setAvailableYears] = useState<string[]>([]);
    const [selectedYear, setSelectedYear] = useState<string>(String(new Date().getFullYear()));
    const [railShowsCounties, setRailShowsCounties] = useState(false);
    const [episodes, setEpisodes] = useState<Episode[]>([]);
    const [catalogEpisodes, setCatalogEpisodes] = useState<Episode[]>([]);
    const [loadingCatalogEpisodes, setLoadingCatalogEpisodes] = useState(false);
    const [loadingEpisodes, setLoadingEpisodes] = useState(false);
    const [lastScraped, setLastScraped] = useState<string | null>(null);
    const [cacheAgeSeconds, setCacheAgeSeconds] = useState<number | null>(null);
    const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
    const [openSeatState, setOpenSeatState] = useState<string | null>(null);
    const [isStale, setIsStale] = useState<boolean>(false);
    const [refreshing, setRefreshing] = useState<boolean>(false);
    const [hostedSealed, setHostedSealed] = useState<boolean>(false);
    const [refreshError, setRefreshError] = useState<string | null>(null);
    const [viewMode, setViewMode] = useState<"episodes" | "cast" | "schedule">("episodes");
    const [selectedMember, setSelectedMember] = useState<CastMemberSummary | null>(null);
    const [includeDraftsRaw, setIncludeDraftsRaw] = useState<boolean>(() => {
        if (typeof window === "undefined")
            return false;
        const param = new URLSearchParams(window.location.search).get("drafts");
        if (param === "true")
            return true;
        if (param === "false")
            return false;
        return true;
    });
    const includeDrafts = currentUser.isOwner && includeDraftsRaw;
    const toggleIncludeDrafts = useCallback(() => {
        if (!currentUser.isOwner)
            return;
        setIncludeDraftsRaw(prev => {
            const next = !prev;
            if (typeof window !== "undefined") {
                const url = new URL(window.location.href);
                url.searchParams.set("drafts", next ? "true" : "false");
                window.history.replaceState({}, "", url.toString());
            }
            return next;
        });
    }, [currentUser.isOwner]);
    useEffect(() => {
        let cancelled = false;
        fetchForPlane({
            publicPath: "/public-api/channels/tree",
            operatorPath: "/api/channels/tree",
        })
            .then(r => r.json())
            .then(d => {
            if (cancelled)
                return;
            if (d && d.ok)
                setChannelsTree(d);
        })
            .catch(() => { });
        return () => {
            cancelled = true;
        };
    }, []);
    useEffect(() => {
        if (!selectedCity) {
            setAvailableYears([]);
            return;
        }
        let cancelled = false;
        const operatorPath = `/api/cities/${encodeURIComponent(selectedCity)}/years${includeDrafts ? "?include_drafts=true" : ""}`;
        fetchForPlane({
            publicPath: `/public-api/cities/${encodeURIComponent(selectedCity)}/years`,
            operatorPath,
        })
            .then(r => r.json())
            .then(d => {
            if (cancelled)
                return;
            if (d && d.ok) {
                const ys: string[] = Array.isArray(d.years) ? d.years : [];
                setAvailableYears(ys);
                if (includeDrafts && ys.length > 0 && !ys.includes(selectedYear)) {
                    setSelectedYear(ys[0]);
                }
            }
            else {
                setAvailableYears([]);
            }
        })
            .catch(() => {
            if (!cancelled)
                setAvailableYears([]);
        });
        return () => {
            cancelled = true;
        };
    }, [selectedCity, includeDrafts]);
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
            .then(async (response) => {
            if (!response.ok)
                throw new Error(`Catalog request failed (${response.status})`);
            return response.json();
        })
            .then(body => {
            if (aborted)
                return;
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
            if (aborted)
                return;
            setCatalogEpisodes([]);
            setLoadingCatalogEpisodes(false);
        });
        return () => {
            aborted = true;
        };
    }, [selectedCity, selectedYear, includeDrafts]);
    const loadEpisodes = useCallback((city: string, force: boolean, year?: string) => {
        if (publicPlane && force)
            return () => { };
        if (force)
            setRefreshing(true);
        else
            setLoadingEpisodes(true);
        let aborted = false;
        const useNewEndpoint = !force;
        const fetchPromise = useNewEndpoint
            ? (() => {
                const yearParam = year || selectedYear || String(new Date().getFullYear());
                const qs = new URLSearchParams({ year: yearParam });
                if (includeDrafts)
                    qs.set("include_drafts", "true");
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
            if (aborted)
                return;
            if (force && (data?.registry_sealed === true || data?.source === "registry_sealed")) {
                setHostedSealed(true);
                setRefreshError(null);
                setRefreshing(false);
                return;
            }
            if (force && data?.success === false) {
                setRefreshError(typeof data?.error === "string" && data.error
                    ? data.error
                    : "the refresh didn't complete — the cached data stays");
                setRefreshing(false);
                return;
            }
            setRefreshError(null);
            const events = Array.isArray(data?.events) ? data.events : [];
            setEpisodes(events);
            setLastScraped(typeof data?.last_scraped === "string" ? data.last_scraped : null);
            setCacheAgeSeconds(typeof data?.cache_age_seconds === "number" ? data.cache_age_seconds : null);
            setIsStale(data?.is_stale === true);
            setLoadingEpisodes(false);
            setRefreshing(false);
        })
            .catch(() => {
            if (aborted)
                return;
            setEpisodes([]);
            setLoadingEpisodes(false);
            setRefreshing(false);
        });
        return () => {
            aborted = true;
        };
    }, [includeDrafts, publicPlane, selectedYear]);
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
    useEffect(() => {
        setRailShowsCounties(false);
    }, [selectedCity]);
    const onSelectState = (code: string) => {
        setSelectedState(code);
        setSelectedCounty(null);
        setSelectedCity(null);
        setEpisodes([]);
        setViewMode("episodes");
        setSelectedMember(null);
        window.scrollTo({ top: 0, behavior: "smooth" });
    };
    const countyJumpNeedsCityRef = useRef(false);
    const onSelectCounty = (county: string) => {
        if (selectedCity !== null)
            countyJumpNeedsCityRef.current = true;
        setSelectedCounty(county);
        setSelectedCity(null);
        setEpisodes([]);
        setViewMode("episodes");
        setSelectedMember(null);
        window.scrollTo({ top: 0, behavior: "smooth" });
    };
    const onSelectCity = (city: string) => {
        setSelectedCity(city);
        setSelectedMember(null);
        window.scrollTo({ top: 0, behavior: "smooth" });
    };
    const goToCityLevel = () => {
        setSelectedMember(null);
    };
    const lastChannelPickRef = useRef<string | null>(null);
    useEffect(() => {
        if (!selectCounty)
            return;
        const sig = `${selectCounty}|${selectCity ?? ""}|${selectNonce ?? ""}`;
        if (lastChannelPickRef.current === sig)
            return;
        lastChannelPickRef.current = sig;
        setSelectedCounty(selectCounty);
        setSelectedCity(selectCity ?? null);
        setViewMode("episodes");
        setSelectedMember(null);
        setEpisodes([]);
        setRailShowsCounties(false);
    }, [selectCounty, selectCity, selectNonce]);
    const lastResetRef = useRef<number | undefined>(undefined);
    useEffect(() => {
        if (resetNonce === undefined)
            return;
        if (lastResetRef.current === resetNonce)
            return;
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
    const treeStateNode = useMemo(() => {
        if (!channelsTree)
            return null;
        return channelsTree.states.find(s => s.state === activeStateName) || null;
    }, [channelsTree, activeStateName]);
    const counties: ReadonlyArray<{
        name: string;
        active: boolean;
        status?: CityStatus;
    }> = useMemo(() => {
        if (treeStateNode) {
            return treeStateNode.counties.map(c => {
                const status = deriveCountyStatus(c.cities);
                return { name: c.county, active: status !== "scaffold", status };
            });
        }
        return selectedState === "AZ" ? ARIZONA_COUNTIES : [];
    }, [treeStateNode, selectedState]);
    const cities: ReadonlyArray<{
        name: string;
        active: boolean;
        status?: CityStatus;
    }> = useMemo(() => {
        if (treeStateNode && selectedCounty) {
            const countyNode = treeStateNode.counties.find(c => c.county === selectedCounty);
            if (countyNode) {
                return countyNode.cities.map(c => ({
                    name: c.name,
                    active: c.status !== "scaffold" && c.status !== "postponed",
                    status: c.status,
                }));
            }
            return [];
        }
        return selectedCounty === "Mohave" ? MOHAVE_CITIES : [];
    }, [treeStateNode, selectedCounty]);
    useEffect(() => {
        if (!countyJumpNeedsCityRef.current)
            return;
        if (cities.length === 0)
            return;
        const firstActive = cities.find(c => c.active);
        if (firstActive) {
            setSelectedCity(firstActive.name);
            countyJumpNeedsCityRef.current = false;
        }
        else {
            countyJumpNeedsCityRef.current = false;
        }
    }, [cities]);
    const viewLevel: "state" | "county" | "city" = !selectedCounty
        ? "state"
        : !selectedCity
            ? "county"
            : "city";
    const sortedEpisodes = useMemo(() => {
        const merged: Episode[] = [];
        const publicIds = new Set<string>();
        const identities = new Set<string>();
        const identityFor = (episode: Episode) => `${episode.meeting_date || ""}\u0000${(episode.meeting_title || "").trim().toLocaleLowerCase()}`;
        for (const episode of [...episodes, ...catalogEpisodes]) {
            if (!isVisibleLocalEpisode(episode))
                continue;
            const identity = identityFor(episode);
            if ((episode.public_id && publicIds.has(episode.public_id)) ||
                identities.has(identity)) {
                continue;
            }
            merged.push(episode);
            if (episode.public_id)
                publicIds.add(episode.public_id);
            identities.add(identity);
        }
        return merged.sort((a, b) => {
            const da = a.meeting_date || "";
            const db = b.meeting_date || "";
            return db.localeCompare(da);
        });
    }, [episodes, catalogEpisodes]);
    const visibleEpisodes = useMemo(() => filterVisibleEpisodes(sortedEpisodes, hidePlaceholders), [hidePlaceholders, sortedEpisodes]);
    const processedEpisodeCount = useMemo(() => sortedEpisodes.filter(e => e.availability === "published" ||
        !!e.episode_tagline ||
        !!e.episode_tags).length, [sortedEpisodes]);
    const displayedYears = useMemo(() => {
        const years = new Set(availableYears);
        if (catalogEpisodes.length > 0)
            years.add(selectedYear);
        return Array.from(years).sort((a, b) => b.localeCompare(a));
    }, [availableYears, catalogEpisodes.length, selectedYear]);
    return (<div className="min-h-screen bg-background text-foreground flex flex-col">
      
      <header className="sticky top-11 z-40 bg-[var(--canvas)]/95 backdrop-blur border-b border-[var(--line)]">
        <div className="max-w-[1600px] mx-auto px-6 lg:px-10 py-4 flex items-center justify-between gap-6">
          <div className="flex items-center gap-4 min-w-0">
            
            <div className="hidden md:flex items-center gap-1 overflow-x-auto kg-scroll max-w-[40vw] lg:max-w-[48vw] pb-1 min-w-0">
              {STATES.map(s => (<button key={s.code} onClick={() => s.active
                ? onSelectState(s.code)
                :
                    setOpenSeatState(s.name)} className={`flex-shrink-0 whitespace-nowrap px-3 py-1.5 text-[11px] font-semibold uppercase tracking-widest rounded-md transition-colors
                    ${selectedState === s.code
                ? "bg-[var(--surface-3)] text-white"
                : s.active
                    ? "text-foreground/70 hover:text-white hover:bg-[var(--surface-3)]/40"
                    : "text-foreground/30 hover:text-foreground/60 hover:bg-[var(--surface-3)]/20"}`} title={s.active ? `Browse ${s.name}` : `Open seat — run Z-SPAN ${s.name}`}>
                  {s.name}
                </button>))}
            </div>
          </div>

          <div className="flex items-center gap-3">
            
            {includeDrafts && (<button onClick={toggleIncludeDrafts} className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md border border-[#F5A524]/50 bg-[#F5A524]/10 hover:bg-[#F5A524]/20 text-[#F5A524] text-[11px] font-semibold uppercase tracking-widest transition-colors" title="Operator mode: drafts visible. Click to switch back to public view.">
                <span>Operator · drafts</span>
                <span className="text-[#F5A524]/70 text-[14px] leading-none">×</span>
              </button>)}
            
          </div>
        </div>

      </header>

      
      {openSeatState && (<div role="dialog" aria-modal="true" aria-labelledby="open-seat-title" className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setOpenSeatState(null)} onKeyDown={e => e.key === "Escape" && setOpenSeatState(null)}>
          <div className="max-w-md mx-6 rounded-lg border border-[var(--line)] bg-[var(--surface-2)] p-6 shadow-xl" onClick={e => e.stopPropagation()}>
            <h2 id="open-seat-title" className="text-lg font-semibold text-white mb-2">
              Z-SPAN {openSeatState}
            </h2>
            <p className="text-sm text-foreground/80 leading-relaxed mb-5">
              Open seat in the Z-SPAN ecosystem, see{" "}
              <span className="text-foreground/60 italic">
                GitHub link (coming soon)
              </span>{" "}
              or contact{" "}
              <a href="mailto:anitacigawet@pm.me" className="text-white underline underline-offset-2">
                anitacigawet@pm.me
              </a>{" "}
              for info.
            </p>
            <button type="button" onClick={() => setOpenSeatState(null)} className="text-[11px] uppercase tracking-widest text-foreground/60 hover:text-white transition-colors">
              Close
            </button>
          </div>
        </div>)}

      
      <div className="flex-1 max-w-[1600px] w-full mx-auto px-6 lg:px-10 py-8">
        
        <div className={viewLevel === "city"
            ? "grid grid-cols-1 md:grid-cols-[260px_1fr] gap-x-0 md:gap-x-10 min-h-[calc(100vh-11rem)]"
            : "min-h-[calc(100vh-11rem)]"}>
          
          {viewLevel === "city" && (<div className="min-w-0">
              
              <aside className="hidden md:block sticky top-24 self-start max-h-[calc(100vh-7rem)] overflow-y-auto kg-scroll px-1">
                <OutlineRail stateName={activeStateName} counties={counties} cities={cities} selectedCounty={selectedCounty} selectedCity={selectedCity} showingCounties={railShowsCounties} onShowCounties={() => setRailShowsCounties(true)} onShowCities={() => setRailShowsCounties(false)} onSelectCounty={onSelectCounty} onSelectCity={onSelectCity} onHome={() => onNavigate("home", { resetToCounties: Date.now() })}/>
              </aside>

              
              <button type="button" onClick={() => setMobileSidebarOpen(true)} className="md:hidden w-full mb-5 flex items-center gap-2 px-3 py-2 rounded-md border border-[var(--line)] bg-[var(--surface)]/40 text-left" aria-label="Open the channel outline">
                <Menu className="w-4 h-4 text-foreground/50 flex-shrink-0" aria-hidden="true"/>
                <span className="text-[10px] uppercase tracking-[0.22em] text-foreground/45 flex-shrink-0">
                  Now Browsing
                </span>
                <span className="text-[12px] text-white truncate">
                  {[activeStateName, selectedCounty, selectedCity].filter(Boolean).join("  /  ")}
                </span>
              </button>

              
              {mobileSidebarOpen && (<>
                  <div className="md:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm" onClick={() => setMobileSidebarOpen(false)} aria-hidden="true"/>
                  <div className="md:hidden fixed inset-y-0 left-0 z-50 w-[280px] bg-[var(--background)] border-r border-[var(--line)] pt-6 px-5 overflow-y-auto">
                    <button type="button" onClick={() => setMobileSidebarOpen(false)} className="absolute top-3 right-3 text-foreground/50 hover:text-white transition-colors p-1" aria-label="Close the channel outline">
                      <X className="w-5 h-5"/>
                    </button>
                    <div className="mt-6">
                      <OutlineRail stateName={activeStateName} counties={counties} cities={cities} selectedCounty={selectedCounty} selectedCity={selectedCity} showingCounties={railShowsCounties} onShowCounties={() => setRailShowsCounties(true)} onShowCities={() => setRailShowsCounties(false)} onSelectCounty={c => {
                    onSelectCounty(c);
                    setMobileSidebarOpen(false);
                }} onSelectCity={c => {
                    onSelectCity(c);
                    setMobileSidebarOpen(false);
                }} onHome={() => {
                    onNavigate("home", { resetToCounties: Date.now() });
                    setMobileSidebarOpen(false);
                }}/>
                    </div>
                  </div>
                </>)}
            </div>)}

          
          <div key={viewLevel} className={`min-w-0 animate-in fade-in-0 duration-300 ${viewLevel === "city" ? "" : "max-w-3xl mx-auto w-full"}`}>
          {viewLevel === "state" ? (<ChannelLevelView heading="Pick a county" headingHint={<DefinitionHint term="County" definition="The largest territorial division for local government within a state of the U.S." sourceUrl="https://www.merriam-webster.com/dictionary/county"/>} subheading="Since this is currently a one-man project, only a few locations are live while I manage compute. Thank you for your patience.">
              {counties.map(c => (<div key={c.name}>
                  <ChannelListRow name={c.name} active={c.active} status={c.status} meta={statusLabel(c.status ?? (c.active ? "live" : "scaffold"))} onClick={c.active ? () => onSelectCounty(c.name) : undefined}/>
                </div>))}
            </ChannelLevelView>) : viewLevel === "county" ? (<ChannelLevelView heading={`Pick a city in ${selectedCounty}`} headingHint={<DefinitionHint term="City" definition="An inhabited place of greater size, population, or importance than a town or village." sourceUrl="https://www.merriam-webster.com/dictionary/city"/>} subheading="Since this is currently a one-man project, only a few locations are live while I manage compute. Thank you for your patience.">
              {cities.length === 0 ? (<p className="px-2 py-6 text-[13px] text-muted-foreground italic normal-case tracking-normal">
                  No active cities yet for {selectedCounty}.
                </p>) : (cities.map(city => {
                const cityMeta = statusLabel(city.status);
                return (<div key={city.name} className="flex items-center gap-2">
                      <div className="flex-1 min-w-0">
                        <ChannelListRow name={city.name} active={city.active} status={city.status} meta={cityMeta} onClick={city.active ? () => onSelectCity(city.name) : undefined}/>
                      </div>
                      <FollowButton targetType="city" targetKey={city.name} variant="ghost"/>
                    </div>);
            }))}
            </ChannelLevelView>) : (<div className="flex flex-col gap-5">
              
              {!selectedMember && selectedCity && (<div className="relative z-10 overflow-hidden rounded-xl border border-[var(--line)] aspect-[21/6] sm:aspect-[24/5] bg-[var(--canvas)]">
                  <img src={channelPosterForCity(selectedCity)} alt="" aria-hidden="true" className="absolute inset-0 w-full h-full object-cover select-none pointer-events-none" onError={e => {
                    const img = e.currentTarget;
                    if (!img.src.endsWith("/channels/_az-default-poster.png")) {
                        img.src = "/channels/_az-default-poster.png";
                    }
                    else {
                        img.style.display = "none";
                    }
                }}/>
                  
                  <div className="absolute inset-0 pointer-events-none" style={{
                    background: "linear-gradient(to top, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.30) 50%, rgba(0,0,0,0) 100%)",
                }}/>
                  
                  <div className="absolute bottom-3 left-4 right-4 flex items-end">
                    <div className="min-w-0">
                      <p className="text-[18px] sm:text-[22px] font-light text-white tracking-wide truncate drop-shadow-md">
                        {selectedCity}
                      </p>
                    </div>
                  </div>

                  
                  {currentUser.isOwner && (<button type="button" onClick={() => onNavigate("city", {
                        cityName: selectedCity,
                        countyName: selectedCounty,
                    })} className="absolute top-3 right-3 z-20 inline-flex h-7 w-7 items-center justify-center rounded-md border border-white/15 bg-black/30 text-white/60 backdrop-blur-sm transition-colors hover:bg-black/50 hover:text-white" title="Channel page · statistics (more coming)" aria-label="Channel page and statistics">
                      <Settings className="h-3.5 w-3.5"/>
                    </button>)}
                </div>)}

              {!selectedMember && (<div className="relative z-0 -mt-[23px] flex items-stretch gap-1 self-start pl-1" role="tablist" aria-label="Channel view mode">
                  {([
                    { key: "episodes", label: "Episodes" },
                    { key: "cast", label: "Cast" },
                    ...(!publicPlane
                        ? [{ key: "schedule" as const, label: "Schedule" }]
                        : []),
                ] as const).map(tab => {
                    const active = viewMode === tab.key;
                    return (<button key={tab.key} role="tab" aria-selected={active} onClick={() => setViewMode(tab.key)} className={`px-4 pt-3.5 pb-2 text-[10px] font-semibold uppercase tracking-[0.18em] rounded-b-lg border border-t-0 transition-colors ${active
                            ? "bg-[var(--surface-3)] text-white border-[var(--line-strong)]"
                            : "bg-[var(--surface)]/50 text-foreground/55 border-[var(--line)] hover:text-white hover:bg-[var(--surface-3)]/60"}`}>
                        {tab.label}
                        
                        {tab.key === "episodes" && sortedEpisodes.length > 0 && (<span className="ml-1.5 tabular-nums">
                            {hidePlaceholders ? (<span className="text-emerald-400">
                                {visibleEpisodes.length}
                              </span>) : (<>
                                <span className="text-emerald-400">
                                  {processedEpisodeCount}
                                </span>
                                <span className="text-foreground/30">
                                  /{sortedEpisodes.length}
                                </span>
                              </>)}
                          </span>)}
                      </button>);
                })}
                </div>)}

              {viewMode === "cast" && selectedMember ? (<CastMemberPanel cityName={selectedCity!} seatId={selectedMember.seat_id || ""} onBack={goToCityLevel} onOpenTruthBook={topic => onNavigate("truth-book", {
                    cityName: selectedCity!,
                    seatId: selectedMember.seat_id || "",
                    topic,
                })}/>) : viewMode === "cast" ? (<CastPanel cityName={selectedCity!} countyName={selectedCounty} onSelectMember={member => setSelectedMember(member)}/>) : viewMode === "schedule" ? (<MeetingSchedulePanel city={selectedCity!}/>) : (<>
                  

                  
                  
                  {lastScraped && currentUser.isOwner && (<div className={`flex items-center justify-between px-3 py-1.5 mt-3 text-[10px] uppercase tracking-[0.18em] rounded border ${isStale
                        ? "border-amber-500/40 bg-amber-500/5 text-amber-200/90"
                        : "border-[var(--line)] bg-[var(--surface)]/40 text-foreground/35"}`}>
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
                        {hostedSealed ? (<span className="text-[10px] text-foreground/50" title="This deployment carries no parser recipes (open framework, sealed registry) — the ingestion pipeline updates this data and syncs it here.">
                            live re-scrape isn't available on the hosted
                            site — the pipeline updates this data
                          </span>) : (<>
                            <button onClick={() => selectedCity && loadEpisodes(selectedCity, true)} disabled={refreshing} className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] uppercase tracking-[0.18em] border border-[var(--line)] bg-[var(--surface-2)] hover:bg-[var(--surface-3)] text-foreground/70 hover:text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed" title="Re-scrape this city from its source website. Uses a network request and may take a few seconds.">
                              <RefreshCw className={`w-3 h-3 ${refreshing ? "animate-spin" : ""}`}/>
                              {refreshing ? "Refreshing" : "Refresh"}
                            </button>
                            {refreshError && (<span className="text-[10px] text-[var(--alert-red)]/80">
                                {refreshError}
                              </span>)}
                          </>)}
                      </OwnerOnly>
                    </div>)}

                  {(loadingEpisodes || loadingCatalogEpisodes) && sortedEpisodes.length === 0 ? (<div className="py-12 text-center">
                  <div className="kg-dots inline-flex">
                    <span /> <span /> <span />
                  </div>
                  <p className="text-sm text-muted-foreground mt-4">
                    Loading episodes…
                  </p>
                </div>) : visibleEpisodes.length === 0 ? (<EmptyChannelState title="No episodes yet" message="No episodes yet, only this sleeping cat. Please check back later!" variant="episodes" onBrowseOther={() => setSelectedCity(null)} onOpenGuide={() => onNavigate("guide")}/>) : (<div className={`divide-y divide-[var(--line-strong)] transition-opacity duration-200 ${loadingEpisodes || loadingCatalogEpisodes
                        ? "opacity-60 pointer-events-none"
                        : "opacity-100"}`}>
                  {groupByMonthAndWeek(visibleEpisodes).map(monthGroup => (<section key={monthGroup.monthKey} className="grid grid-cols-[88px_1fr] sm:grid-cols-[110px_1fr] gap-x-4 sm:gap-x-6 py-5 first:pt-0">
                      <div className="pt-1 border-r border-[var(--line)]">
                        <h3 className="text-[22px] sm:text-[26px] font-light text-white tracking-tight leading-none" title={`${monthGroup.monthLabel} ${monthGroup.monthYear}`}>
                          {monthGroup.monthLabel}
                        </h3>
                        {monthGroup.monthYear !== new Date().getFullYear() && (<p className="text-[10px] text-muted-foreground/40 mt-1 tabular-nums">
                            {monthGroup.monthYear}
                          </p>)}
                      </div>

                      <div className="divide-y divide-[var(--line)]/40 min-w-0">
                        {monthGroup.weeks.map(week => (<div key={week.weekStart.toISOString()} className="grid grid-cols-[28px_1fr] sm:grid-cols-[36px_1fr] gap-x-2 sm:gap-x-3 items-start py-2.5 first:pt-0 last:pb-0">
                            <span className="text-[8px] uppercase tracking-[0.18em] text-foreground/30 pt-1.5 tabular-nums select-none" title={`Week ${week.weekNumber} of ${monthGroup.monthLabel}`}>
                              wk {week.weekNumber}
                            </span>
                            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 min-w-0">
                              {week.episodes.map(ep => (<EpisodeCard key={ep.public_id ?? ep.id} episode={ep} onOpen={() => {
                                    if (publicPlane && ep.public_id) {
                                        onNavigate("broadcast", { publicId: ep.public_id });
                                    }
                                    else if (isCatalogPlaceholder(ep) && ep.public_id) {
                                        onNavigate("broadcast", { publicId: ep.public_id });
                                    }
                                    else if (ep.id !== undefined) {
                                        onNavigate("broadcast", { meetingId: ep.id });
                                    }
                                    else if (ep.public_id) {
                                        onNavigate("broadcast", { publicId: ep.public_id });
                                    }
                                }}/>))}
                            </div>
                          </div>))}
                      </div>
                    </section>))}
                </div>)}

              
              {displayedYears.length > 0 && (<nav aria-label="Year" className="mt-10 pt-5 border-t border-[var(--line)] flex items-center justify-center gap-4 flex-wrap">
                  <span className="kg-eyebrow text-[10px] text-foreground/30 mr-2 tracking-[0.18em]">
                    YEAR
                  </span>
                  {displayedYears.map(y => {
                        const isActive = y === selectedYear;
                        return (<button key={y} onClick={() => {
                                setSelectedYear(y);
                                window.scrollTo({ top: 0, behavior: "smooth" });
                            }} className={`text-[12px] tabular-nums transition-colors ${isActive
                                ? "text-white font-medium"
                                : "text-foreground/30 hover:text-foreground/60"}`} aria-current={isActive ? "true" : undefined}>
                        {y}
                      </button>);
                    })}
                </nav>)}
                </>)}
            </div>)}
          </div>
        </div>

        
        <footer className="relative mt-8 pt-6 border-t border-[var(--line)] flex flex-col items-center gap-4 text-center">
          <div className="text-[11px] text-muted-foreground leading-relaxed">
            
            <p>
              Made possible thanks to{" "}
              <a href="https://www.opengovtplatform.org/government-transparency/sunshine-laws" target="_blank" rel="noopener noreferrer" className="zs-sunshine" title="Sunshine laws — the open-meeting & open-records laws that make the public record public">sunshine</a>{" "}
              laws, your local{" "}
              <a href="https://en.wikipedia.org/wiki/Municipal_clerk#United_States" target="_blank" rel="noopener noreferrer" className="no-underline hover:text-foreground transition-colors" title="Municipal clerks — the local officials who keep the public record public">municipal clerk</a>, and{" "}
              <a href="https://www.noaa.gov/submarine-cables" target="_blank" rel="noopener noreferrer" className="no-underline hover:text-foreground transition-colors" title="Submarine cables — the largely-invisible physical infrastructure that carries the internet between continents">technology</a>, ensuring a <span className="zs-brighter">brighter</span> tomorrow.
            </p>
            
          </div>
          

          
          <div>
            <TravelersOdometer />
          </div>
        </footer>
      </div>
    </div>);
}
function SleepingCat({ className }: {
    className?: string;
}) {
    return (<img src="/states/sleeping-cat.png" alt="" aria-hidden="true" className={className} draggable={false}/>);
}
function EmptyChannelState({ title, message, variant = "channel", onBrowseOther, onOpenGuide, }: {
    title: string;
    message: string;
    variant?: "channel" | "episodes";
    onBrowseOther?: () => void;
    onOpenGuide?: () => void;
}) {
    if (variant === "episodes") {
        return (<div className="kg-card-2 border-dashed p-10 sm:p-12 text-center flex flex-col items-center">
        <SleepingCat className="h-28 sm:h-32 w-auto mb-5 opacity-80"/>
        <p className="text-sm text-foreground/70 leading-relaxed max-w-sm mx-auto">
          {message}
        </p>
        {(onBrowseOther || onOpenGuide) && (<div className="mt-5 flex flex-col items-center gap-2 text-[13px]">
            {onBrowseOther && (<button type="button" onClick={onBrowseOther} className="text-foreground/70 hover:text-white underline underline-offset-4 decoration-dotted decoration-foreground/30 hover:decoration-foreground/60 transition-colors">
                Other Channels
              </button>)}
            {onOpenGuide && (<button type="button" onClick={onOpenGuide} className="text-foreground/70 hover:text-white underline underline-offset-4 decoration-dotted decoration-foreground/30 hover:decoration-foreground/60 transition-colors">
                Guide
              </button>)}
          </div>)}
      </div>);
    }
    return (<div className="kg-card-2 border-dashed p-10 sm:p-12 text-center flex flex-col items-center">
      <picture className="block mb-4">
        <img src="/states/coming-soon-channel.png" alt="" aria-hidden="true" className="h-28 sm:h-32 w-auto opacity-80 select-none" onError={e => {
            const img = e.currentTarget;
            img.style.display = "none";
            const fallback = img.nextElementSibling as HTMLElement | null;
            if (fallback)
                fallback.style.display = "block";
        }}/>
        <Tv className="w-8 h-8 mx-auto text-muted-foreground/40" style={{ display: "none" }}/>
      </picture>
      <h3 className="text-base font-semibold text-foreground/70 mb-1.5">{title}</h3>
      <p className="text-sm text-muted-foreground leading-relaxed max-w-md mx-auto">
        {message}
      </p>
    </div>);
}

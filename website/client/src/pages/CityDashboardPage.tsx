/**
 * City Dashboard V0 (2026-07-03) — the per-city citizen page.
 *
 * Renders the operator's painted-scene mock (public/hq/zspan-dashboard-scene.webp)
 * as the fixed backdrop, with interactive panels absolute-positioned on
 * the painted panels — same technique as HQ V2. Wired to real Z-SPAN
 * data where the pipeline already produces it; placeholders for
 * weather/market until we pick providers.
 *
 * Operator direction (2026-07-03): try it with Kingman as "my city" —
 * meaning a city selected on the user's profile (auto-selected from
 * geolocation + Google account when possible), distinct from the
 * follow mechanic. The auto-select half pauses like the other
 * not-yet-live functions; the immediate goal is seeing the surface
 * work as well as it can right now.
 *
 * → V0 hardcodes the city from URL param (defaults to Kingman); the
 * profile-driven auto-select is scoped as a follow-on. A visible chip
 * marks the pause honestly.
 *
 * See CITY_DASHBOARD_SPEC.md for the element→data mapping.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, X, Play, Volume2, SkipForward, Pause, Maximize2, Settings } from "lucide-react";
import "./city-dashboard.css";
import StylusLayoutEditor, {
 useLayoutEditMode,
 loadPersistedPanels,
 savePersistedPanels,
 type PanelRect,
} from "../components/dashboard/StylusLayoutEditor";
import {
 fetchCityDashboard,
 relativeAgo,
 youtubeIdOf,
 type CityDashboardData,
 type DashboardHeadline,
} from "../utils/cityDashboard";
import {
 fetchCityWeather,
 weatherIcon,
 type WeatherReading,
} from "../utils/weather";

type Tab = "home" | "volunteer" | "events" | "watchdog";

interface CityDashboardPageProps {
 cityName?: string;
 onNavigate: (view: string, params?: Record<string, unknown>) => void;
 onBack: () => void;
}

const DEFAULT_CITY = "Kingman";

/** Initial panel rects — the current CSS values as of this session.
 * When ?layout=1 is on, these become the editable-state seeds. */
// Locked 2026-07-03 from operator's stylus positioning session.
const INITIAL_PANELS: Record<"nav" | "weather" | "market" | "player" | "news", PanelRect> = {
 nav: { top: 15.71, left: 28.29, width: 43.05, height: 6.05 },
 weather: { top: 24.14, left: 3.90, width: 19.83, height: 38.59 },
 market: { top: 66.69, left: 3.90, width: 19.87, height: 23.18 },
 player: { top: 24.12, left: 26.37, width: 47.35, height: 53.81 },
 news: { top: 24.03, left: 76.23, width: 19.81, height: 65.87 },
};

export default function CityDashboardPage({
 cityName,
 onNavigate,
 onBack,
}: CityDashboardPageProps) {
 // City is locked to the visitor's assigned city (V0: Kingman default;
 // profile-driven auto-select scheduled for the profile-system chunk).
 // We intentionally do NOT let a URL param change it — that would let
 // anyone swap cityName=X in the address bar and scrape any city's
 // dashboard. Instead, we accept an initial value from the caller's
 // routing (so the TopBar entry still works), then strip the param
 // from the URL immediately so it can't be re-used, changed, or shared.
 const [city] = useState<string>(cityName ?? DEFAULT_CITY);
 // Note: URL scrape defense lives in App.tsx's buildUrlForNavigation —
 // it deliberately omits cityName from the URL for view=city-dashboard.
 const [data, setData] = useState<CityDashboardData | null>(null);
 const [loading, setLoading] = useState(true);
 const [error, setError] = useState<string | null>(null);
 const [tab, setTab] = useState<Tab>("home");
 const [playing, setPlaying] = useState(false);
 const [dismissed, setDismissed] = useState<Record<string, boolean>>({});
 const [weather, setWeather] = useState<WeatherReading | null>(null);
 const [weatherLoading, setWeatherLoading] = useState(true);

 const layoutEditMode = useLayoutEditMode();
 const [panels, setPanels] = useState(() => loadPersistedPanels(INITIAL_PANELS));
 // Persist every rect change so a reload keeps the operator's in-progress work.
 useEffect(() => {
 if (layoutEditMode) savePersistedPanels(panels);
 }, [panels, layoutEditMode]);
 const sceneRef = useRef<HTMLDivElement | null>(null);
 const dragState = useRef<{
 key: keyof typeof INITIAL_PANELS;
 mode: "move" | "resize";
 startX: number;
 startY: number;
 startRect: PanelRect;
 sceneW: number;
 sceneH: number;
 } | null>(null);

 /** Bind drag+resize onto an existing panel element. When ?layout=1 is
 * on, the panel's inline style overrides CSS with the state rect, gets
 * a dashed outline, becomes draggable, and picks up a resize handle. */
 const bindEditable = (key: keyof typeof INITIAL_PANELS) => {
 if (!layoutEditMode) return {};
 const r = panels[key];
 return {
 style: {
 top: `${r.top}%`,
 left: `${r.left}%`,
 right: "auto" as const,
 width: `${r.width}%`,
 height: `${r.height}%`,
 outline: "1.5px dashed rgba(127, 216, 255, 0.75)",
 outlineOffset: "-1px",
 cursor: "move" as const,
 touchAction: "none" as const,
 },
 onPointerDown: (e: React.PointerEvent<HTMLElement>) => {
 const scene = sceneRef.current;
 if (!scene) return;
 const target = e.target as HTMLElement;
 const isResize = target.classList.contains("cdash-edit-handle");
 const sr = scene.getBoundingClientRect();
 dragState.current = {
 key,
 mode: isResize ? "resize" : "move",
 startX: e.clientX,
 startY: e.clientY,
 startRect: { ...r },
 sceneW: sr.width,
 sceneH: sr.height,
 };
 (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
 e.stopPropagation();
 e.preventDefault();
 },
 onPointerMove: (e: React.PointerEvent<HTMLElement>) => {
 const s = dragState.current;
 if (!s || s.key !== key) return;
 const dx = ((e.clientX - s.startX) / s.sceneW) * 100;
 const dy = ((e.clientY - s.startY) / s.sceneH) * 100;
 setPanels(prev => {
 const cur = prev[key];
 if (s.mode === "move") {
 return {
 ...prev,
 [key]: {
 ...cur,
 top: Math.max(0, Math.min(100 - cur.height, s.startRect.top + dy)),
 left: Math.max(0, Math.min(100 - cur.width, s.startRect.left + dx)),
 },
 };
 }
 return {
 ...prev,
 [key]: {
 ...cur,
 width: Math.max(4, Math.min(100 - cur.left, s.startRect.width + dx)),
 height: Math.max(3, Math.min(100 - cur.top, s.startRect.height + dy)),
 },
 };
 });
 },
 onPointerUp: (e: React.PointerEvent<HTMLElement>) => {
 dragState.current = null;
 (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
 },
 onPointerCancel: (e: React.PointerEvent<HTMLElement>) => {
 dragState.current = null;
 (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
 },
 };
 };

 /** Small resize handle (bottom-right triangle) — rendered inside each
 * editable panel when edit mode is on. */
 const EditHandle = () =>
 layoutEditMode ? <div className="cdash-edit-handle" aria-hidden /> : null;

 useEffect(() => {
 let cancelled = false;
 setLoading(true);
 setError(null);
 setData(null);
 fetchCityDashboard(city, { headlineCount: 8 })
 .then(d => { if (!cancelled) setData(d); })
 .catch(e => { if (!cancelled) setError(e?.message ?? "load failed"); })
 .finally(() => { if (!cancelled) setLoading(false); });
 return () => { cancelled = true; };
 }, [city]);

 useEffect(() => {
 let cancelled = false;
 setWeatherLoading(true);
 setWeather(null);
 fetchCityWeather(city)
 .then(w => { if (!cancelled) setWeather(w); })
 .catch(() => { /* soft-fail — panel shows a placeholder */ })
 .finally(() => { if (!cancelled) setWeatherLoading(false); });
 return () => { cancelled = true; };
 }, [city]);

 const featured = data?.featuredMeeting ?? null;
 const featuredYT = useMemo(
 () =>
 youtubeIdOf(
 featured?.youtube_video_url ?? featured?.video_url ?? null,
 ),
 [featured],
 );

 const dismiss = (key: string) =>
 setDismissed(prev => ({ ...prev, [key]: true }));

 return (
 <div className="cdash-root">
 {/* Painted scene fixed backdrop */}
 <div className="cdash-scene" ref={sceneRef}>
 <img
 src="/hq/zspan-dashboard-scene.webp?v=1"
 alt=""
 aria-hidden
 className="cdash-scene-img"
 fetchPriority="high"
 decoding="async"
 />

 {/* --- Top nav strip (over the painted nav band) --- */}
 <div
 className="cdash-nav"
 role="tablist"
 aria-label="Dashboard sections"
 {...bindEditable("nav")}
 >
 {(["home", "volunteer", "events", "watchdog"] as Tab[]).map(t => (
 <button
 key={t}
 role="tab"
 aria-selected={tab === t}
 onClick={() => setTab(t)}
 className={`cdash-nav-item cdash-nav-${t} ${tab === t ? "is-on" : ""}`}
 >
 {t.toUpperCase()}
 </button>
 ))}
 {data?.liveState === "on" ? (
 <a
 className="cdash-nav-item cdash-nav-live is-on"
 href={data.liveVideoId
 ? `https://www.youtube.com/watch?v=${data.liveVideoId}`
 : "#"
 }
 target="_blank"
 rel="noopener noreferrer"
 >
 LIVE
 </a>
 ) : (
 <span
 className="cdash-nav-item cdash-nav-live is-off"
 title={`No live ${city} council meeting right now — this lights up when one is streaming.`}
 >
 LIVE
 </span>
 )}
 <EditHandle />
 </div>

 {/* --- Center: player window --- */}
 <section
 className="cdash-player"
 aria-label={`Latest ${city} broadcast`}
 {...bindEditable("player")}
 >
 <EditHandle />
 <div className="cdash-player-chrome">
 <span
 className="cdash-player-title"
 title="Profile-driven auto-select is scoped as a follow-on; city currently defaults to Kingman"
 >
 📍 {city.toUpperCase()} · CITY DESK
 </span>
 <span className="cdash-player-dot cdash-player-dot--min" aria-hidden />
 <span className="cdash-player-dot cdash-player-dot--max" aria-hidden />
 <button
 type="button"
 className="cdash-player-dot cdash-player-close"
 onClick={onBack}
 aria-label="Back"
 >
 <X size={10} />
 </button>
 </div>
 <div className="cdash-player-screen">
 {loading && (
 <div className="cdash-player-empty">Loading broadcast…</div>
 )}
 {!loading && !featured && (
 <div className="cdash-player-empty">
 No published broadcast on file yet for {city}.
 </div>
 )}
 {!loading && featured && featuredYT && (
 playing ? (
 <iframe
 className="cdash-player-video"
 src={`https://www.youtube.com/embed/${featuredYT}?autoplay=1&modestbranding=1&rel=0`}
 title={featured.meeting_title}
 allow="autoplay; encrypted-media; picture-in-picture"
 allowFullScreen
 />
 ) : (
 <button
 type="button"
 className="cdash-player-poster"
 onClick={() => setPlaying(true)}
 aria-label={`Play ${featured.meeting_title}`}
 >
 <img
 src={`https://i.ytimg.com/vi/${featuredYT}/hqdefault.jpg`}
 alt=""
 aria-hidden
 />
 <span className="cdash-player-poster-live">LIVE</span>
 <span className="cdash-player-play">
 <Play size={40} fill="currentColor" />
 </span>
 </button>
 )
 )}
 {!loading && featured && !featuredYT && featured.video_url && (
 <video
 className="cdash-player-video"
 src={featured.video_url}
 controls
 preload="metadata"
 />
 )}
 </div>
 {featured && (
 <>
 <div className="cdash-player-ticker" aria-label="Breaking news ticker">
 <div className="cdash-player-ticker-track">
 {/* Duplicated content so the marquee loops seamlessly. */}
 {[0, 1].map(i => (
 <span key={i}>
 <span className="cdash-player-ticker-lead">
 {featured.meeting_date &&
 Date.now() - Date.parse(featured.meeting_date + "T12:00:00") <
 7 * 24 * 3600 * 1000
 ? "BREAKING NEWS:"
 : "LATEST BROADCAST:"}
 </span>
 {" "}
 {featured.meeting_title}
 {featured.episode_tagline
 ? ` · ${featured.episode_tagline}`
 : ""}
 {data?.headlines
 .filter(h => h.meeting_id !== featured.meeting_id)
 .slice(0, 4)
 .map(h => ` · ${h.tagline}`)
 .join("")}
 </span>
 ))}
 </div>
 </div>
 <div className="cdash-player-controls">
 <button
 type="button"
 onClick={() => setPlaying(p => !p)}
 aria-label={playing ? "Pause" : "Play"}
 >
 {playing ? <Pause size={14} /> : <Play size={14} />}
 </button>
 <button type="button" aria-label="Skip to next chapter">
 <SkipForward size={14} />
 </button>
 <button type="button" aria-label="Mute">
 <Volume2 size={14} />
 </button>
 <div className="cdash-player-progress" role="progressbar" aria-label="Playback progress">
 <div className="cdash-player-progress-fill" />
 </div>
 <button
 type="button"
 aria-label="Broadcast details"
 onClick={() =>
 onNavigate("broadcast", { meetingId: featured.meeting_id })
 }
 >
 <Settings size={13} />
 </button>
 <button type="button" aria-label="Fullscreen">
 <Maximize2 size={13} />
 </button>
 </div>
 </>
 )}
 </section>

 {/* --- Left column: weather + market --- */}
 <section
 className="cdash-weather"
 aria-label="Weather update"
 {...bindEditable("weather")}
 >
 <EditHandle />
 <div className="cdash-panel-title">WEATHER UPDATE</div>
 {weatherLoading && (
 <div className="cdash-weather-empty">Fetching sky…</div>
 )}
 {!weatherLoading && !weather && (
 <div className="cdash-weather-empty">
 Weather feed unavailable right now.
 </div>
 )}
 {weather && (
 <>
 <div className="cdash-weather-row">
 <span
 className="cdash-weather-icon"
 aria-hidden
 title={weatherIcon(weather.current.weathercode, weather.current.isDaytime).label}
 >
 {weatherIcon(weather.current.weathercode, weather.current.isDaytime).emoji}
 </span>
 <span className="cdash-weather-temp">{weather.current.tempF}°</span>
 <div className="cdash-weather-side">
 <div className="cdash-weather-side-lbl">
 <span className="cdash-weather-side-icon" aria-hidden>💨</span>
 {weather.current.windMph} mph
 </div>
 <div className="cdash-weather-side-cond">
 {weatherIcon(weather.current.weathercode, weather.current.isDaytime).label}
 </div>
 </div>
 </div>
 <div className="cdash-weather-city">
 {weather.location.displayName}
 </div>
 <div className="cdash-weather-strip-title">7-DAY FORECAST</div>
 <div className="cdash-weather-strip-viewport">
 <div className="cdash-weather-strip" role="list">
 {/* Duplicated for seamless loop marquee */}
 {[0, 1].map(pass => (
 <div key={pass} className="cdash-weather-strip-run">
 {weather.forecast.map((day, i) => {
 const icon = weatherIcon(day.weathercode, true);
 return (
 <div
 key={`${pass}-${day.date}`}
 className={`cdash-weather-day ${i === 0 ? "is-today" : ""}`}
 role="listitem"
 title={`${icon.label} · precip ${day.precipProbability}%`}
 >
 <div className="cdash-weather-day-name">
 {i === 0 ? "TODAY" : day.weekday}
 </div>
 <div className="cdash-weather-day-icon" aria-hidden>
 {icon.emoji}
 </div>
 <div className="cdash-weather-day-high">
 {day.tempMaxF}°
 </div>
 <div className="cdash-weather-day-low">
 {day.tempMinF}°
 </div>
 </div>
 );
 })}
 </div>
 ))}
 </div>
 </div>
 <div className="cdash-weather-source">
 Open-Meteo · updated {relativeAgo(weather.current.updatedAt)}
 </div>
 </>
 )}
 </section>

 {!dismissed.market && (
 <section
 className="cdash-market"
 aria-label="Market watch"
 {...bindEditable("market")}
 >
 <EditHandle />
 <div className="cdash-panel-title">
 MARKET WATCH
 <button
 type="button"
 aria-label="Dismiss market panel"
 onClick={() => dismiss("market")}
 className="cdash-panel-x"
 >
 <X size={11} />
 </button>
 </div>
 <ul className="cdash-market-rows">
 <li><span className="v pos">+1.2%</span> ZSP</li>
 <li><span className="v neg">−0.5%</span> NDX</li>
 <li><span className="v pos">+2.1%</span> GOLD</li>
 </ul>
 <div className="cdash-panel-note">Z-SPAN civic index · in-world flavor</div>
 </section>
 )}

 {/* --- Right column: LATEST NEWS FEED --- */}
 <section
 className="cdash-news"
 aria-label="Latest news feed"
 {...bindEditable("news")}
 >
 <EditHandle />
 <div className="cdash-panel-title">LATEST NEWS FEED</div>
 {loading && (
 <div className="cdash-news-empty">Loading…</div>
 )}
 {error && !loading && (
 <div className="cdash-news-empty is-err">Couldn{"'"}t load: {error}</div>
 )}
 {!loading && !error && data && data.headlines.length === 0 && (
 <div className="cdash-news-empty">
 No published broadcasts for {city} yet.
 </div>
 )}
 <ul className="cdash-news-list">
 {data?.headlines.map((h: DashboardHeadline) => (
 <li key={h.meeting_id}>
 <button
 type="button"
 onClick={() =>
 onNavigate("broadcast", { meetingId: h.meeting_id })
 }
 className="cdash-news-item"
 >
 <div className="cdash-news-title">{h.tagline}</div>
 <div className="cdash-news-meta">
 {relativeAgo(h.meeting_date)} · {h.meeting_title}
 </div>
 </button>
 </li>
 ))}
 </ul>
 </section>
 </div>

 <StylusLayoutEditor
 active={layoutEditMode}
 panels={panels}
 onChange={(k, r) => setPanels(prev => ({ ...prev, [k]: r }))}
 />

 {/* --- Tab drawer: opens below the fold when tab != home --- */}
 {tab !== "home" && data && (
 <div className="cdash-drawer" role="region" aria-label={`${tab} details`}>
 <div className="cdash-drawer-inner">
 <button
 type="button"
 onClick={() => setTab("home")}
 className="cdash-drawer-back"
 >
 <ArrowLeft size={14} /> Back to broadcast
 </button>
 {tab === "volunteer" && (
 <>
 <h2>Volunteer opportunities on the record</h2>
 <p className="cdash-drawer-hint">
 Pulled from `community_calls_to_action` — verbatim calls the
 city or public raised in a council meeting.
 </p>
 {data.callsToAction.length === 0 ? (
 <p className="cdash-drawer-empty">
 Nothing on the record yet. Calls-to-action surface once
 meetings mentioning them get processed.
 </p>
 ) : (
 <ul className="cdash-drawer-list">
 {data.callsToAction.map((c, i) => (
 <li key={i}>{c}</li>
 ))}
 </ul>
 )}
 </>
 )}
 {tab === "events" && (
 <>
 <h2>Upcoming meetings in {city}</h2>
 <p className="cdash-drawer-hint">
 From the parser calendar — same source the channel browser
 uses.
 </p>
 {data.upcomingMeetings.length === 0 ? (
 <p className="cdash-drawer-empty">
 No future meetings on file. The Channels page has the
 full calendar.
 </p>
 ) : (
 <ul className="cdash-drawer-list">
 {data.upcomingMeetings.map(m => (
 <li key={m.id}>
 <span className="cdash-drawer-date">
 {m.meeting_date}
 </span>
 <span> · {m.meeting_title}</span>
 </li>
 ))}
 </ul>
 )}
 <button
 type="button"
 onClick={() =>
 onNavigate("city", { cityName: city })
 }
 className="cdash-drawer-link"
 >
 Open the full {city} channel →
 </button>
 </>
 )}
 {tab === "watchdog" && (
 <>
 <h2>The Watchdog — Z-SPAN{"'"}s Truth Book</h2>
 <p className="cdash-drawer-hint">
 Each council member{"'"}s record on file — verified quotes
 and tracked commitments. Pick a member from the city{"'"}s
 Cast panel to open their Tracking Board.
 </p>
 <button
 type="button"
 onClick={() =>
 onNavigate("city", { cityName: city })
 }
 className="cdash-drawer-link"
 >
 Open the {city} Cast panel →
 </button>
 </>
 )}
 </div>
 </div>
 )}
 </div>
 );
}

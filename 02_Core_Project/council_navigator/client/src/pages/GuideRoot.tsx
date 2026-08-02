import { useEffect, useState } from "react";
import { ArrowLeft, RefreshCw, LayoutGrid, Map as MapIcon } from "lucide-react";
import Starfield from "@/components/guide/Starfield";
import GuideCard from "@/components/guide/GuideCard";
import CinematicTakeover from "@/components/guide/CinematicTakeover";
import InlinePlayer from "@/components/guide/InlinePlayer";
import AggregateMap from "@/components/guide/AggregateMap";
import "./guide.css";
import { fetchForPlane } from "../lib/planeFetch";
const EMPTY_OVERLAY_HIDE_KEY = "zspan-guide-empty-overlay-hidden";
interface GuideRootProps {
    onNavigate: (view: string, params?: unknown) => void;
}
interface LiveStream {
    public_id?: string;
    city_name: string;
    state: string | null;
    county: string | null;
    channel_id: string | null;
    video_id: string;
    video_url: string;
    title: string | null;
    started_at: string | null;
    detected_at?: string | null;
    meeting_id?: number | null;
}
type GuideViewMode = "cards" | "map";
export default function GuideRoot({ onNavigate }: GuideRootProps) {
    const [streams, setStreams] = useState<LiveStream[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [scheduledToday, setScheduledToday] = useState(0);
    const [mode, setMode] = useState<GuideViewMode>("cards");
    const [userPickedMode, setUserPickedMode] = useState(false);
    const [hasLoadedOnce, setHasLoadedOnce] = useState(false);
    const [selected, setSelected] = useState<LiveStream | null>(null);
    const [playerMode, setPlayerMode] = useState<"cinematic" | "inline">("cinematic");
    const [emptyOverlayHidden, setEmptyOverlayHidden] = useState<boolean>(() => {
        try {
            if (typeof window !== "undefined" &&
                window.location.search.includes("reset-empty-overlay=1")) {
                localStorage.removeItem(EMPTY_OVERLAY_HIDE_KEY);
                return false;
            }
            return localStorage.getItem(EMPTY_OVERLAY_HIDE_KEY) === "true";
        }
        catch {
            return false;
        }
    });
    const handleDismissEmptyOverlay = (persistent: boolean) => {
        setEmptyOverlayHidden(true);
        if (persistent) {
            try {
                localStorage.setItem(EMPTY_OVERLAY_HIDE_KEY, "true");
            }
            catch {
            }
        }
    };
    const load = () => {
        setLoading(true);
        if (import.meta.env.DEV &&
            typeof window !== "undefined" &&
            window.location.search.includes("demo=1")) {
            setStreams([
                {
                    city_name: "Kingman",
                    state: "AZ",
                    county: "Mohave",
                    channel_id: "demo-channel-kingman",
                    video_id: "demo-kingman-1",
                    video_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    title: "City Council Regular Meeting — DEMO",
                    started_at: new Date(Date.now() - 1000 * 60 * 22).toISOString(),
                    detected_at: new Date(Date.now() - 1000 * 60 * 22).toISOString(),
                    meeting_id: 999001,
                },
                {
                    city_name: "Chicago",
                    state: "IL",
                    county: "Cook",
                    channel_id: "demo-channel-chicago",
                    video_id: "demo-chicago-1",
                    video_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    title: "City Council Regular Meeting — DEMO",
                    started_at: new Date(Date.now() - 1000 * 60 * 13).toISOString(),
                    detected_at: new Date(Date.now() - 1000 * 60 * 13).toISOString(),
                    meeting_id: 999002,
                },
            ]);
            setScheduledToday(0);
            setError(null);
            setLoading(false);
            setHasLoadedOnce(true);
            return;
        }
        fetchForPlane({ publicPath: "/public-api/guide", operatorPath: "/api/guide" })
            .then((r) => r.json())
            .then((d) => {
            if (d && d.ok) {
                setStreams(Array.isArray(d.live) ? d.live : []);
                setScheduledToday(typeof d.scheduled_today === "number" ? d.scheduled_today : 0);
                setError(null);
            }
            else {
                setError((d && d.error) || "Could not load the Guide.");
            }
        })
            .catch((e) => setError(String(e)))
            .finally(() => {
            setLoading(false);
            setHasLoadedOnce(true);
        });
    };
    useEffect(() => {
        if (!hasLoadedOnce || userPickedMode)
            return;
        setMode(streams.length >= 3 ? "cards" : "map");
    }, [hasLoadedOnce, streams.length, userPickedMode]);
    const handleModeClick = (next: GuideViewMode) => {
        setUserPickedMode(true);
        setMode(next);
    };
    useEffect(() => {
        load();
        const t = setInterval(load, 60000);
        return () => clearInterval(t);
    }, []);
    return (<div className="guide-root">
      <Starfield />
      <div className="guide-content">
        <header className="guide-chrome">
          <div className="guide-chrome-inner">
            <div className="guide-chrome-left">
              <button onClick={() => onNavigate("home")} className="guide-icon-btn guide-back-btn" title="Back to channels" aria-label="Back to channels">
                <ArrowLeft size={16}/>
              </button>
            </div>

            <div className="guide-chrome-center">
              <span className="guide-title">The Guide</span>
              {!loading && (<div className="guide-title-count-row">
                  <span className={`guide-live-dot${streams.length === 0 ? " guide-live-dot--inactive" : ""}`} aria-hidden/>
                  <span className="guide-title-count">
                    {streams.length} live now
                    {scheduledToday > 0 ? ` · ${scheduledToday} scheduled today` : ""}
                  </span>
                </div>)}
            </div>

            <div className="guide-chrome-actions">
              <button type="button" className="guide-icon-btn guide-mode-swap-btn" onClick={() => handleModeClick(mode === "cards" ? "map" : "cards")} title={mode === "cards" ? "Switch to map view" : "Switch to cards view"} aria-label={mode === "cards" ? "Switch to map view" : "Switch to cards view"}>
                {mode === "cards" ? (<MapIcon size={16}/>) : (<LayoutGrid size={16}/>)}
              </button>
              <button onClick={load} className={`guide-icon-btn guide-refresh-btn${loading ? " is-loading" : ""}`} title="Refresh the live list" aria-label="Refresh">
                <RefreshCw size={16}/>
              </button>
            </div>
          </div>
        </header>

        <main className="guide-main">
          {error && (<div className="guide-placeholder">
              <div className="guide-placeholder-eyebrow">Error</div>
              <div className="guide-placeholder-title">
                Could not load the Guide
              </div>
              <p className="guide-placeholder-sub">{error}</p>
            </div>)}

          {!error && mode === "cards" && (<>
              {loading && streams.length === 0 && (<div className="guide-deck-loading">Checking the channels…</div>)}
              {!loading && streams.length === 0 && (<ViewPlaceholder scheduledToday={scheduledToday} onNavigate={onNavigate}/>)}
              {streams.length > 0 && (<div className="guide-card-deck">
                  {streams.map((s, i) => {
                    const n = streams.length;
                    const center = (n - 1) / 2;
                    const offset = i - center;
                    const maxOffset = Math.max(1, center);
                    const norm = offset / maxOffset;
                    const tiltDeg = -norm * 22;
                    const translateZ = -Math.abs(norm) * 80;
                    return (<GuideCard key={`${s.city_name}-${s.video_id}`} data={s} tiltDeg={tiltDeg} translateZ={translateZ} onSelect={() => setSelected(s)}/>);
                })}
                </div>)}
            </>)}

          {!error && mode === "map" && (<>
              {loading && streams.length === 0 ? (<div className="guide-deck-loading">Checking the channels…</div>) : (<AggregateMap broadcasts={streams} onSelect={(b) => setSelected(streams.find((s) => s.video_id === b.video_id) ?? null)}/>)}
              {!loading && streams.length === 0 && !emptyOverlayHidden && (<div className="guide-aggregate-empty-overlay">
                  <p className="guide-aggregate-empty-text">
                    Nothing's live right now — check back later, or browse
                    the{" "}
                    <button type="button" className="guide-aggregate-empty-link" onClick={() => onNavigate("home")}>
                      channels
                    </button>
                    .
                  </p>
                  <div className="guide-aggregate-empty-actions">
                    <button type="button" className="guide-aggregate-empty-btn guide-aggregate-empty-btn--primary" onClick={() => handleDismissEmptyOverlay(false)}>
                      Okay
                    </button>
                    <button type="button" className="guide-aggregate-empty-btn" onClick={() => handleDismissEmptyOverlay(true)}>
                      Never show again
                    </button>
                  </div>
                </div>)}
            </>)}
        </main>

        <footer className="guide-bottom-eyebrow">
          Government meetings streaming live right now, across every
          channel we follow.
        </footer>
      </div>

      {selected && playerMode === "cinematic" && (<CinematicTakeover data={selected} onClose={() => setSelected(null)} onDemoteToInline={() => setPlayerMode("inline")}/>)}
      {selected && playerMode === "inline" && (<InlinePlayer data={selected} onMaximize={() => setPlayerMode("cinematic")} onClose={() => {
                setSelected(null);
                setPlayerMode("cinematic");
            }}/>)}
    </div>);
}
function ViewPlaceholder({ scheduledToday, onNavigate, }: {
    scheduledToday: number;
    onNavigate: (view: string, params?: unknown) => void;
}) {
    return (<div className="guide-placeholder">
      <div className="guide-placeholder-title">Nothing's live right now</div>
      <p className="guide-placeholder-sub">
        Council and committee meetings broadcast live on the channels we
        track — they aren't in session around the clock. When one goes live,
        it shows up here automatically.
        {scheduledToday > 0 && (<>
            {" "}There {scheduledToday === 1 ? "is" : "are"} {scheduledToday}{" "}
            meeting{scheduledToday === 1 ? "" : "s"} scheduled today —
            check back around then.
          </>)}
      </p>
      <div className="guide-placeholder-actions">
        <button type="button" onClick={() => onNavigate("home")} className="guide-placeholder-action">
          Browse the channels →
        </button>
      </div>
    </div>);
}

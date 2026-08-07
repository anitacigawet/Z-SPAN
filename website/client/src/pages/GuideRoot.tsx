/**
 * GuideRoot — Phase G cinematic rebuild of The Guide.
 *
 * Replaces the previous list-grammar `GuidePage` at the `?view=guide`
 * route. The Guide is the network's present-tense layer — what's
 * broadcasting RIGHT NOW — and reads in an Xbox-Big-Picture register
 * rather than the catalog grammar the rest of the site uses for the
 * archive.
 *
 * G-1 (this chunk) ships the foundation: the depth-gradient backdrop
 * + the starfield overlay + the view-mode toggle chrome scaffold
 * (Map / Aggregate / Table). Each view renders a placeholder pointing
 * at the chunk it lands in; the underlying live-stream data is still
 * surfaced as a plain-text fallback so the Guide stays functionally
 * reachable while the rebuild runs.
 *
 * The 11-chunk plan: [01_Project_Overview/GUIDE_CINEMATIC_REBUILD_PLAN.md].
 */
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
 // V1-UI-2 C1 — sparse-state routing. Until the user has manually
 // picked a view mode, the default routes based on broadcast count:
 // - 3+ broadcasts → Cards (deck) — enough to populate the U-curve
 // - 1-2 broadcasts → Map (US picker) — cards feel sparse
 // - 0 broadcasts → Map with empty-state messaging
 // Once the user clicks any toggle, `userPickedMode` flips true and
 // their preference sticks for the rest of the session (no further
 // auto-routing). Tracks first-load separately so the route doesn't
 // fire from the initial empty-streams state (would flash the picker
 // before the real data arrives).
 const [userPickedMode, setUserPickedMode] = useState(false);
 const [hasLoadedOnce, setHasLoadedOnce] = useState(false);
 // G-5 — cinematic takeover. `selected` is the currently-expanded
 // broadcast; null when no takeover is open. Clicking a card sets it;
 // closing returns to null.
 const [selected, setSelected] = useState<LiveStream | null>(null);
 // G-6 — "Keep browsing" toggle. Default is cinematic (full-screen
 // takeover). When the user clicks "Keep browsing" inside the
 // takeover, mode flips to "inline" and the player demotes to a
 // single inline slot below the deck. Subsequent card clicks while
 // in inline mode swap the inline player to the new broadcast (NOT
 // open a new takeover, NOT stack inline players — single slot per
 // James). Session-scoped: each fresh load lands on cinematic.
 const [playerMode, setPlayerMode] = useState<"cinematic" | "inline">(
 "cinematic",
 );
 // Empty-state overlay dismissal. 'Okay' hides for the current session
 // only (state reverts on reload); 'Never show again' persists via
 // localStorage so the overlay never renders again on subsequent loads.
 const [emptyOverlayHidden, setEmptyOverlayHidden] = useState<boolean>(() => {
 // Reset path: ?reset-empty-overlay=1 clears the localStorage gate
 // so the empty-state popup renders again. Useful for operator-side
 // testing after Never-show-again was clicked in a prior session.
 try {
 if (
 typeof window !== "undefined" &&
 window.location.search.includes("reset-empty-overlay=1")
 ) {
 localStorage.removeItem(EMPTY_OVERLAY_HIDE_KEY);
 return false;
 }
 return localStorage.getItem(EMPTY_OVERLAY_HIDE_KEY) === "true";
 } catch {
 return false;
 }
 });
 const handleDismissEmptyOverlay = (persistent: boolean) => {
 setEmptyOverlayHidden(true);
 if (persistent) {
 try {
 localStorage.setItem(EMPTY_OVERLAY_HIDE_KEY, "true");
 } catch {
 // localStorage may be unavailable (private mode); state-only dismissal
 // still applies for the current session.
 }
 }
 };

 const load = () => {
 setLoading(true);
 // Temporary ?demo=1 fixture so the operator can preview the
 // GuideCard aesthetic without a real live broadcast. Gated behind
 // import.meta.env.DEV so Vite dead-code-eliminates the block in
 // production builds (prevents anyone from triggering fake live
 // broadcasts on the public flagship by guessing the URL).
 if (
 import.meta.env.DEV &&
 typeof window !== "undefined" &&
 window.location.search.includes("demo=1")
 ) {
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
 setScheduledToday(
 typeof d.scheduled_today === "number" ? d.scheduled_today : 0,
 );
 setError(null);
 } else {
 setError((d && d.error) || "Could not load the Guide.");
 }
 })
 .catch((e) => setError(String(e)))
 .finally(() => {
 setLoading(false);
 setHasLoadedOnce(true);
 });
 };

 // G-8 — sparse-state default route. Runs once after the first load
 // completes, then only re-runs if the stream count crosses the
 // 3-threshold AND the user hasn't manually picked a view. The
 // user-toggle wins permanently as soon as they click any segment.
 useEffect(() => {
 if (!hasLoadedOnce || userPickedMode) return;
 setMode(streams.length >= 3 ? "cards" : "map");
 }, [hasLoadedOnce, streams.length, userPickedMode]);

 // Wrap setMode so user-initiated clicks lock in their preference for
 // the rest of the session. Internal auto-routes use setMode directly.
 const handleModeClick = (next: GuideViewMode) => {
 setUserPickedMode(true);
 setMode(next);
 };

 useEffect(() => {
 load();
 const t = setInterval(load, 60_000);
 return () => clearInterval(t);
 }, []);

 return (
 <div className="guide-root">
 <Starfield />
 <div className="guide-content">
 <header className="guide-chrome">
 <div className="guide-chrome-inner">
 <div className="guide-chrome-left">
 <button
 onClick={() => onNavigate("home")}
 className="guide-icon-btn guide-back-btn"
 title="Back to channels"
 aria-label="Back to channels"
 >
 <ArrowLeft size={16} />
 </button>
 </div>

 <div className="guide-chrome-center">
 <span className="guide-title">The Guide</span>
 {!loading && (
 <div className="guide-title-count-row">
 <span
 className={`guide-live-dot${streams.length === 0 ? " guide-live-dot--inactive" : ""}`}
 aria-hidden
 />
 <span className="guide-title-count">
 {streams.length} live now
 {scheduledToday > 0 ? ` · ${scheduledToday} scheduled today` : ""}
 </span>
 </div>
 )}
 </div>

 <div className="guide-chrome-actions">
 <button
 type="button"
 className="guide-icon-btn guide-mode-swap-btn"
 onClick={() => handleModeClick(mode === "cards" ? "map" : "cards")}
 title={mode === "cards" ? "Switch to map view" : "Switch to cards view"}
 aria-label={mode === "cards" ? "Switch to map view" : "Switch to cards view"}
 >
 {mode === "cards" ? (
 <MapIcon size={16} />
 ) : (
 <LayoutGrid size={16} />
 )}
 </button>
 <button
 onClick={load}
 className={`guide-icon-btn guide-refresh-btn${loading ? " is-loading" : ""}`}
 title="Refresh the live list"
 aria-label="Refresh"
 >
 <RefreshCw size={16} />
 </button>
 </div>
 </div>
 </header>

 <main className="guide-main">
 {error && (
 <div className="guide-placeholder">
 <div className="guide-placeholder-eyebrow">Error</div>
 <div className="guide-placeholder-title">
 Could not load the Guide
 </div>
 <p className="guide-placeholder-sub">{error}</p>
 </div>
 )}

 {!error && mode === "cards" && (
 <>
 {loading && streams.length === 0 && (
 <div className="guide-deck-loading">Checking the channels…</div>
 )}
 {!loading && streams.length === 0 && (
 <ViewPlaceholder
 scheduledToday={scheduledToday}
 onNavigate={onNavigate}
 />
 )}
 {streams.length > 0 && (
 <div className="guide-card-deck">
 {streams.map((s, i) => {
 // U-curve depth (G-4) — center card forward + edges
 // tilt inward and recede slightly. Subtle per James's
 // call: just enough to play into the gradient's bottom
 // crescent, NOT a Steam-Big-Picture fold. Computed
 // here (not in CSS via nth-child) because the curve
 // shape depends on the total count.
 const n = streams.length;
 const center = (n - 1) / 2;
 const offset = i - center; // negative left, positive right
 const maxOffset = Math.max(1, center);
 const norm = offset / maxOffset; // -1 → +1
 // G-4 iter 2 — Opus critique said the original 8deg/32px
 // was invisible at 3 cards. Bumped to 15deg/60px so the
 // curve reads as designed depth, not accidental tilt.
 // V1-UI-2 C2 — translateZ max bumped 60→80 + perspective
 // 2200→1600. V1-UI-2 C5 — Opus visual pass on the 3-card
 // mock render showed the curve STILL read as nearly flat
 // at desktop scale. Tilt bumped 15→22 + perspective
 // tightened to 1200 (in guide.css) so side cards
 // unmistakably tilt inward toward the focal center.
 // City + state name overlay legibility re-verified at
 // 22deg.
 const tiltDeg = -norm * 22;
 const translateZ = -Math.abs(norm) * 80;
 return (
 <GuideCard
 key={`${s.city_name}-${s.video_id}`}
 data={s}
 tiltDeg={tiltDeg}
 translateZ={translateZ}
 onSelect={() => setSelected(s)}
 />
 );
 })}
 </div>
 )}
 </>
 )}

 {!error && mode === "map" && (
 <>
 {loading && streams.length === 0 ? (
 <div className="guide-deck-loading">Checking the channels…</div>
 ) : (
 <AggregateMap
 broadcasts={streams}
 onSelect={(b) =>
 setSelected(streams.find((s) => s.video_id === b.video_id) ?? null)
 }
 />
 )}
 {!loading && streams.length === 0 && !emptyOverlayHidden && (
 <div className="guide-aggregate-empty-overlay">
 <p className="guide-aggregate-empty-text">
 Nothing's live right now — check back later, or browse
 the{" "}
 <button
 type="button"
 className="guide-aggregate-empty-link"
 onClick={() => onNavigate("home")}
 >
 channels
 </button>
 .
 </p>
 <div className="guide-aggregate-empty-actions">
 <button
 type="button"
 className="guide-aggregate-empty-btn guide-aggregate-empty-btn--primary"
 onClick={() => handleDismissEmptyOverlay(false)}
 >
 Okay
 </button>
 <button
 type="button"
 className="guide-aggregate-empty-btn"
 onClick={() => handleDismissEmptyOverlay(true)}
 >
 Never show again
 </button>
 </div>
 </div>
 )}
 </>
 )}
 </main>

 <footer className="guide-bottom-eyebrow">
 Government meetings streaming live right now, across every
 channel we follow.
 </footer>
 </div>

 {selected && playerMode === "cinematic" && (
 <CinematicTakeover
 data={selected}
 onClose={() => setSelected(null)}
 onDemoteToInline={() => setPlayerMode("inline")}
 />
 )}
 {selected && playerMode === "inline" && (
 <InlinePlayer
 data={selected}
 onMaximize={() => setPlayerMode("cinematic")}
 onClose={() => {
 setSelected(null);
 // Reset to cinematic so the NEXT fresh card click goes
 // back to the cinematic default. Mode is session-scoped
 // per James, but it's not "sticky" across player open/close
 // cycles — each open starts cinematic unless the user
 // re-demotes mid-session.
 setPlayerMode("cinematic");
 }}
 />
 )}
 </div>
 );
}

function ViewPlaceholder({
 scheduledToday,
 onNavigate,
}: {
 scheduledToday: number;
 onNavigate: (view: string, params?: unknown) => void;
}) {
 return (
 <div className="guide-placeholder">
 <div className="guide-placeholder-title">Nothing's live right now</div>
 <p className="guide-placeholder-sub">
 Council and committee meetings broadcast live on the channels we
 track — they aren't in session around the clock. When one goes live,
 it shows up here automatically.
 {scheduledToday > 0 && (
 <>
 {" "}There {scheduledToday === 1 ? "is" : "are"} {scheduledToday}{" "}
 meeting{scheduledToday === 1 ? "" : "s"} scheduled today —
 check back around then.
 </>
 )}
 </p>
 <div className="guide-placeholder-actions">
 <button
 type="button"
 onClick={() => onNavigate("home")}
 className="guide-placeholder-action"
 >
 Browse the channels →
 </button>
 </div>
 </div>
 );
}

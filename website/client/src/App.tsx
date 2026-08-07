import { useState, useRef, useEffect } from "react";
import "./App.css";
import ParserDashboard from "./pages/ParserDashboard";
import WatermarkDebugPage from "./pages/WatermarkDebugPage";
import WatermarkVerifyPage from "./pages/WatermarkVerifyPage";
import WatermarkScanPage from "./pages/WatermarkScanPage";
import AuditPage from "./pages/AuditPage";
import CorrectionsPage from "./pages/CorrectionsPage";
import RegistryPage from "./pages/RegistryPage";
import CalendarHealthPage from "./pages/CalendarHealthPage";
import V1LaunchPage from "./pages/V1LaunchPage";
import SearchPage from "./pages/SearchPage";
import HomePage from "./pages/HomePage";
import ChannelsPage from "./pages/ChannelsPage";
import CastMemberPanel from "./components/CastMemberPanel";
import CityPage from "./pages/CityPage";
import SettingsPage from "./pages/SettingsPage";
import BroadcastPage from "./pages/BroadcastPage";
import OperatorTerminal from "./pages/OperatorTerminal";
import CityLedgerPage from "./pages/CityLedgerPage";
import DisputedQuotesPage from "./pages/DisputedQuotesPage";
import VocabularyInboxPage from "./pages/VocabularyInboxPage";
import EscalationsInboxPage from "./pages/EscalationsInboxPage";
import SpeakerRosterReviewPage from "./pages/SpeakerRosterReviewPage";
import TruthBookPage from "./pages/TruthBookPage";
import CityDashboardPage from "./pages/CityDashboardPage";
import ViewContextRequired from "./components/ViewContextRequired";
import CompilerPage from "./pages/CompilerPage";
import HQRoot from "./pages/HQRoot";
import AutonomyGatePage from "./pages/AutonomyGatePage";
import GuideRoot from "./pages/GuideRoot";
import FollowingPage from "./pages/FollowingPage";
import CreatorsSignupPage from "./pages/CreatorsSignupPage";
import CreatorsLandingPage from "./pages/CreatorsLandingPage";
import { useCurrentUser } from "./hooks/useCurrentUser";
import ViewerModeFallback from "./components/ViewerModeFallback";
import { TopBar } from "./components/TopBar";
import { OperatorSearchModal } from "./components/OperatorSearchModal";
import { ReportGeneratorModal } from "./components/ReportGeneratorModal";
import { CityDeskPage } from "./pages/CityDeskPage";
import { CityDeskDemoPage } from "./pages/CityDeskDemoPage";
import { SignInPill } from "./components/SignInPill";
// TravelersOdometer moved to ChannelsPage's footer 2026-07-01 per operator —
// previously mounted at App root with `fixed bottom-right` and followed the
// user across every view; should live in the actual footer only.
import { HandTrackingProvider } from "./components/HandTrackingProvider";
import { HandTrackingToggle } from "./components/HandTrackingToggle";
import { ThemeProvider } from "./components/ui/theme-provider";
import { PublicDataDisclaimerProvider } from "./components/PublicDataDisclaimerGate";
import {
 isOperatorSurfaceAllowed,
 isPublicPlane,
} from "./lib/trustPlane";

interface NavigationState {
 view:
 | "home" // default: ChannelsPage (Z-SPAN — Arizona library landing)
 | "home-classic" // legacy stats-style landing, kept for fallback
 | "city"
 | "search"
 | "dashboard"
 | "settings"
 | "broadcast"
 | "terminal"
 | "ledger"
 | "disputed-quotes"
 | "vocabulary-inbox"
 | "escalations-inbox"
 | "speaker-roster-review"
 | "cast-member"
 | "truth-book"
 | "compiler"
 | "hq"
 | "autonomy"
 | "calendar-health"
 | "v1-launch"
 | "guide"
 | "following"
 | "creators"
 | "watermark-debug"
 | "watermark-verify"
 | "scan"
 | "audit"
 | "corrections" // RR-4 — public corrections doorbell + running logB-4)
 | "registry" // RR-5 — public registry policy +coverage listing
 | "city-desk" //— operator-preview enterprise-wrapper suite (paper theme)
 | "city-desk-demo" //— session-only try-it walkthrough (no persistence)
 | "city-dashboard"; // 2026-07-03 — per-city citizen dashboard (CITY_DASHBOARD_SPEC.md)
 params?: {
 state?: string;
 countyName?: string;
 cityName?: string;
 meetingId?: number;
 // facts-only catalog identity for unpublished placeholder pages.
 publicId?: string;
 // V1.5-OperatorSearch-1 Phase 4 — when a citation chip from the
 // operator search modal deep-links to a broadcast page, the chunk's
 // start_seconds rides along so the video auto-seeks on mount.
 seek?: number;
 seatId?: string;
 // Truth Book chunk 6 — deep-link the Record timeline to a topic lane.
 topic?: string;
 // Optional human-readable meeting label used when a corrections
 // navigation source can pre-fill the email's page reference.
 disputeContext?: string;
 // Optional row-focus hint for review surfaces (DisputedQuotesPage,
 // VocabularyInboxPage). When set, the receiving page can scroll to /
 // highlight the matching row. Carried via ?focus=N in agent-constructed
 // deep_links (e.g. /?view=disputed-quotes&focus=44).
 focus?: string;
 // HQ render-mode overrides — only meaningful when view === "hq". These
 // force one mode for this session without clobbering the operator's
 // stored preference.
 // ?compare=true → side-by-side V1+V2 (dev view)
 // ?legacy=true → V1 painted image (the pre-V2-17c default; kept as
 // an opt-in for one release cycle per V2-17c)
 // Cleared on any internal navigate() so leaving + returning to ?view=hq
 // falls back to the stored preference (default v2) unless the URL still
 // carries the override.
 compare?: boolean;
 legacy?: boolean;
 // statusOpen removed with the scrape-daemon STATUS drawer.
 // Compiler page render mode — only meaningful when view === "compiler".
 // list = V0 typed-IR pseudo-code list (default)
 // graph = CFG node-link rendering (Surface A, item 2 of build seq)
 // Parsed from ?mode=list|graph on initial URL load; CompilerPage owns
 // the in-page toggle that mutates the URL via history.replaceState so
 // the link stays shareable.
 compilerMode?: "list" | "graph";
 // Search seed — only meaningful when view === "search". Set by the
 // TopBar search (V1-Polish-12): Enter carries the typed text here so
 // SearchPage auto-runs the query on mount. Also parsed from ?query= /
 // ?q= on initial URL load so a search link is shareable.
 query?: string;
 // Channel jump nonce — only meaningful when view === "home". The
 // TopBarSearch channel type-ahead sets countyName/cityName + a fresh
 // channelPick so ChannelsPage re-applies the selection on every pick.
 channelPick?: number;
 // V1-Polish-24: the Z-SPAN logo + Channels nav set a fresh timestamp here
 // to reset ChannelsPage to the state-level "Pick a county" picker.
 resetToCounties?: number;
 };
}

// Showcase edition (VITE_ZSPAN_EDITION=showcase): the static GitHub-Pages
// bake excludes the unreviewed corrections/disputes surface. IS_SHOWCASE is
// written as an inline import.meta.env compare so Vite replaces it and the
// bundler constant-folds it to a literal — dead-code eliminating the
// CorrectionsPage import (and its copy) out of the public bundle. The
// flagship build leaves the var unset, so corrections stays (behind CF
// Access for operator review). See ops/export_static_showcase.py.
const IS_SHOWCASE = import.meta.env.VITE_ZSPAN_EDITION === "showcase";

// All recognized view names — used to validate ?view=X URL params before
// honoring them. Keep in sync with the NavigationState["view"] union above.
const VALID_VIEWS: Array<NavigationState["view"]> = [
 "home", "home-classic", "city", "search", "dashboard",
 "settings", "broadcast", "terminal", "ledger",
 "disputed-quotes", "vocabulary-inbox", "escalations-inbox", "speaker-roster-review", "cast-member",
 "truth-book", "compiler", "hq", "autonomy", "calendar-health", "v1-launch", "guide",
 "following", "creators", "watermark-debug", "watermark-verify", "scan", "audit",
 "corrections", "registry", "city-desk", "city-desk-demo", "city-dashboard",
].filter((v) => !(IS_SHOWCASE && v === "corrections")) as Array<
 NavigationState["view"]
>;

// Public-plane allowlist. The Channels landing is named `home` internally;
// its county/city drill-down stays within ChannelsPage. keeps Cast public
// as an official-record surface while its Truth Book dossier stays operator-only.
const PUBLIC_VIEWS: ReadonlySet<NavigationState["view"]> = new Set<
 NavigationState["view"]
>([
 "home",
 "search",
 "broadcast",
 "guide",
 "cast-member",
 // Session-104: Settings now has citizen preferences, follows, and account
 // details. Anonymous visitors see the page's own sign-in prompt; owners
 // additionally see the existing operator configuration section.
 "settings",
 // Session-103 (product-slice2): the Following view is the signed-in
 // visitor's personal follow list. Every row is scoped by user.id on
 // the server side, so exposing the view here does not leak cross-user
 // data. Anonymous visitors see it as sign-in-required per the view's
 // own useCurrentUser check.
 "following",
]);

function restrictNavigationToPlane(
 navigation: NavigationState,
 publicPlane: boolean,
): NavigationState {
 if (publicPlane && !PUBLIC_VIEWS.has(navigation.view)) {
 return { view: "home" };
 }
 return navigation;
}

function parseInitialNavigationFromUrl(): NavigationState | null {
 if (typeof window === "undefined" || !window.location) return null;
 const sp = new URLSearchParams(window.location.search);
 // Accept intuitive aliases for canonical view names — agent/Slack deep_links
 // sometimes use the human label (e.g. "operator" for the operator terminal,
 // whose internal view name is "terminal"). Normalize before validating so a
 // reasonable-but-non-canonical ?view= still lands on the right surface.
 const VIEW_ALIASES: Record<string, NavigationState["view"]> = { operator: "terminal" };
 //pathname routing — bare URLs `/scan` and `/audit` map to the
 // corresponding views so the sticker microtext `zspan.org/scan` lands
 // cleanly. In production Cloudflare Pages `_redirects` rewrites the
 // pathname to `/?view=scan` while preserving the address bar; this
 // fallback makes the bare paths work in local dev too.
 const pathname = (window.location.pathname || "").replace(/\/+$/, "").toLowerCase();
 const PATH_VIEWS: Record<string, NavigationState["view"]> = {
 "/scan": "scan",
 "/audit": "audit",
 "/corrections": "corrections",
 "/registry": "registry",
 };
 const pathView = PATH_VIEWS[pathname];
 const raw = pathView ?? (sp.get("view") || "").trim();
 const view = VIEW_ALIASES[raw] ?? raw;
 if (!view || !VALID_VIEWS.includes(view as NavigationState["view"])) {
 return null;
 }
 // Build params from supported query keys. Missing ones stay undefined.
 const params: NonNullable<NavigationState["params"]> = {};
 const stateParam = sp.get("state");
 if (stateParam) params.state = stateParam;
 const cityName = sp.get("cityName") || sp.get("city");
 if (cityName) params.cityName = cityName;
 const meetingId = sp.get("meetingId");
 if (meetingId && /^\d+$/.test(meetingId)) {
 params.meetingId = parseInt(meetingId, 10);
 }
 const publicId = sp.get("publicId");
 if (publicId && /^m_[A-Za-z0-9]{22}$/.test(publicId)) {
 params.publicId = publicId;
 }
 //Report-V0-1 — ?t=<seconds> seek param (YouTube convention) so
 // report-artifact citation chips deep-link to the MOMENT, not just the
 // meeting. Flows into BroadcastPage's initialSeek via params.seek —
 // the same runtime path the OperatorSearch chips already use in-app.
 const t = sp.get("t");
 if (t && /^\d+$/.test(t)) params.seek = parseInt(t, 10);
 const seatId = sp.get("seatId");
 if (seatId) params.seatId = seatId;
 const topic = sp.get("topic");
 if (topic) params.topic = topic;
 const focus = sp.get("focus");
 if (focus) params.focus = focus;
 // Search seed for ?view=search — accept ?query= or the shorter ?q=.
 const query = sp.get("query") || sp.get("q");
 if (query) params.query = query;
 // HQ render-mode overrides — only meaningful when view === "hq". Each
 // accepts the canonical "true" form; anything else (missing, "false",
 // junk) is treated as false. Stored on params so the rendered view can
 // dispatch on them.
 if (view === "hq" && sp.get("compare") === "true") {
 params.compare = true;
 }
 if (view === "hq" && sp.get("legacy") === "true") {
 params.legacy = true;
 }
 // (Former ?view=dashboard&status=open + ?view=pipeline redirect both
 // retired with the scrape-daemon STATUS drawer.)
 // Compiler mode override — list (default V0) vs graph (Surface A CFG).
 // Only honored when view === "compiler"; junk values fall through to
 // the CompilerPage default (list).
 if (view === "compiler") {
 const m = sp.get("mode");
 if (m === "list" || m === "graph") {
 params.compilerMode = m;
 }
 }
 return {
 view: view as NavigationState["view"],
 params: Object.keys(params).length ? params : undefined,
 };
}

// A2 /-era shareability (2026-07-01): the exact inverse of
// parseInitialNavigationFromUrl above. Serializes a NavigationState into
// a shareable URL so browsing WRITES the address bar (pushState in
// navigate(), replaceState in goBack()) and the browser back button works
// (popstate listener). Only round-trippable params are serialized —
// ephemeral nonces (channelPick, resetToCounties) and runtime-only props
// stay out of the URL. Keep in sync with the parser's vocabulary.
function buildUrlForNavigation(nav: NavigationState): string {
 const sp = new URLSearchParams();
 if (nav.view !== "home") sp.set("view", nav.view);
 // Session-32 (2026-07-04) — preserve operator-editor URL flags
 // through URL rewrites. The operator terminal loads the broadcast
 // in an iframe with ?peek=1 to skip the disclaimer gate; the HQ
 // stylus-layout editor uses ?layout=1 to enter drag/resize mode.
 // On mount, App fires history.replaceState via this builder to
 // normalize the URL — pre-fix, unknown params were silently stripped
 // and the second render saw a URL without them, flipping the
 // corresponding hooks back off. Now we sniff window.location.search
 // once and preserve each recognized flag in the rebuilt URL so it
 // survives every subsequent render. Safe because only pages loaded
 // WITH the flag will ever have it set.
 if (typeof window !== "undefined") {
 const currentSp = new URLSearchParams(window.location.search);
 if (currentSp.get("peek") === "1") {
 sp.set("peek", "1");
 }
 // Layout editor flag: ?layout=1 turns edit mode on, ?layout=0
 // clears the persisted state. Preserve both so a URL-driven edit
 // session survives history.replaceState. StylusLayoutEditor's
 // useLayoutEditMode hook ALSO persists to localStorage so a
 // reload without the flag still restores edit mode — this fix is
 // specifically for the initial history.replaceState during the
 // first render, before localStorage has been read.
 const layoutFlag = currentSp.get("layout");
 if (layoutFlag === "1" || layoutFlag === "0") {
 sp.set("layout", layoutFlag);
 }
 // Keep the localhost-only public-plane test override across the SPA's
 // history normalization. The trust-plane helper ignores this parameter
 // on every non-dev hostname.
 if (currentSp.get("__plane") === "public") {
 sp.set("__plane", "public");
 }
 }
 const p = nav.params;
 if (p) {
 if (p.state) sp.set("state", p.state);
 if (p.countyName) sp.set("countyName", p.countyName);
 // 2026-07-03 — city-dashboard intentionally keeps its cityName OUT of
 // the URL. The dashboard is city-locked to the visitor's assigned
 // city (V0: Kingman default; profile-driven auto-select is the
 // follow-on). Serializing cityName would let anyone swap ?cityName=X
 // in the address bar to scrape any city's data.
 if (p.cityName && nav.view !== "city-dashboard") sp.set("cityName", p.cityName);
 if (p.meetingId !== undefined && p.meetingId !== null)
 sp.set("meetingId", String(p.meetingId));
 if (p.publicId) sp.set("publicId", p.publicId);
 //— seek serializes as ?t= so a chip-followed moment is itself
 // shareable (round-trips with the parser above). Genuinely
 // round-trippable state, unlike the ephemeral nonces kept out below.
 if (typeof p.seek === "number" && p.seek > 0)
 sp.set("t", String(Math.floor(p.seek)));
 if (p.seatId) sp.set("seatId", p.seatId);
 if (p.topic) sp.set("topic", p.topic);
 if (p.focus) sp.set("focus", p.focus);
 if (p.query) sp.set("query", p.query);
 if (nav.view === "hq" && p.compare) sp.set("compare", "true");
 if (nav.view === "hq" && p.legacy) sp.set("legacy", "true");
 if (nav.view === "compiler" && p.compilerMode)
 sp.set("mode", p.compilerMode);
 }
 const qs = sp.toString();
 return qs ? `/?${qs}` : "/";
}

// Role-based switcher for the "creators" view. role='creator' lands
// on the landing page; everyone else (anonymous + light) lands on the
// signup wizard, which handles its own anonymous-vs-light branching.
function CreatorsView({
 onNavigate,
}: {
 onNavigate: (view: string, params?: any) => void;
}) {
 const { user, loading } = useCurrentUser();
 if (loading) {
 return (
 <div className="min-h-screen bg-background flex items-center justify-center">
 <div className="text-foreground/40 text-sm">Loading…</div>
 </div>
 );
 }
 if (user && user.role === "creator") {
 return <CreatorsLandingPage onNavigate={onNavigate} />;
 }
 return <CreatorsSignupPage onNavigate={onNavigate} />;
}


function App() {
 const publicPlane = isPublicPlane();
 const operatorSurfaceAllowed = isOperatorSurfaceAllowed();
 const currentUser = useCurrentUser();
 const [navigation, setNavigation] = useState<NavigationState>(() => {
 // Honor ?view=X (and contextual params) on initial load so deep links
 // from Slack escalations, the agent fleet, and any other external entry
 // point land the user on the right operator surface instead of the
 // homepage. Invalid or missing ?view= falls through to the home
 // default. Internal navigate() calls don't touch the URL — this is
 // strictly an inbound-link enabler.
 return restrictNavigationToPlane(
 parseInitialNavigationFromUrl() ?? { view: "home" },
 publicPlane,
 );
 });
 const [navHistory, setNavHistory] = useState<NavigationState[]>([]);
 const [isTransitioning, setIsTransitioning] = useState(false);
 const contentRef = useRef<HTMLDivElement>(null);

 // V1.5-OperatorSearch-1 — natural-language cross-meeting search. The
 // query string IS the open/closed state: null = closed; set = open with
 // that query as the initial intent. Owner-only at the trigger surface
 // (TopBarSearch) AND at the backend endpoint, so non-owners can never
 // open this even if they discover the prop chain.
 const [operatorSearchQuery, setOperatorSearchQuery] = useState<string | null>(
 null,
 );

 //Report-V0-1 — cited-report generation. Same open/closed-state
 // convention and the same owner-only double gate as operatorSearchQuery.
 const [reportQuery, setReportQuery] = useState<string | null>(null);

 // Owner-only views — non-owner viewers see ViewerModeFallback instead.
 // Operator identity is the Google-OAuth owner (useCurrentUser().isOwner)
 // per V1-Polish-2: viewing these requires signing in via Google as the
 // owner account, including in local dev (no more auto-owner).
 const OWNER_ONLY_VIEWS: Array<NavigationState["view"]> = [
 "terminal",
 "dashboard",
 "city-dashboard",
 // "following" moved to PUBLIC_VIEWS in (product-slice2) —
 // signed-in visitors reach their own follow list on the public plane.
 "city-desk",
 "city-desk-demo",
 "disputed-quotes",
 "vocabulary-inbox",
 "escalations-inbox",
 "autonomy",
 "calendar-health",
 "v1-launch",
 "compiler",
 "truth-book",
 // Session-29 pre-video gate (2026-07-03): the Cast attribution surface
 // and the Creator Network signup landing get owner-only visibility
 // until the strategic strip pass fires (indefinitely postponed — see
 // TEMPORARY_THOUGHTS.md § Session-29). This is a hide-not-delete step
 // so non-operator accounts (viewer, creator) see ViewerModeFallback
 // instead of the experimental interpretive surfaces during the video
 // shoot + soft launch.
 "cast-member",
 "creators",
 // Session-31 (2026-07-04) — auth-audit findings gated BOTH `hq` and
 // `speaker-roster-review` here. Session-41 (2026-07-08) RE-OPENED `hq`
 // at operator direction: the audit misread the HQ's funding/burn/runway
 // figures as public operator data — funding dollars are server-redacted
 // for non-owners (`restricted: true` per RR-8), every status string is
 // server-rendered from the curated template table (the Club-Penguin
 // safe-status redline: secrets cannot reach the public surface), and
 // billboards are client-owned display copy (`utils/hqBillboards.ts`).
 // The HQ is thepublic lobby — built to be looked at. Its one write
 // endpoint (/api/hq/traffic-events/inject) is _require_owner()-gated
 // server-side, and the mock panel hides via isOwner, so the public view
 // is read-only. `speaker-roster-review` STAYS gated — that one renders
 // a genuine operator review queue (confirm/override/anonymous), has no
 // client nav entry point, and its reasoning holds.
 "speaker-roster-review",
 ];
 const ownerView =
 operatorSurfaceAllowed && OWNER_ONLY_VIEWS.includes(navigation.view);
 // While the principal is still loading, hold on a neutral state: don't
 // render the operator view (would flash operator content to a possible
 // non-owner) and don't render the fallback (would flash "owner-only" to
 // the actual owner). Resolve once /api/auth/me confirms identity.
 const ownerGateLoading = ownerView && currentUser.loading;
 const requestedOwnerView =
 ownerView && !currentUser.loading && !currentUser.isOwner;

 // Session-31 (2026-07-04) — SIGNED-IN-required views. Lower tier
 // than OWNER_ONLY: any signed-in Google account passes, but
 // anonymous visitors get redirected home. Semantic: these surfaces
 // are inherently profile-scoped ("my city dashboard", "my
 // follows"), so showing them to anonymous is nonsense. Direct URL
 // access at /?view=city-dashboard was defaulting to Kingman for
 // everyone including anonymous — operator flagged as a leak.
 //
 // Session-32 (2026-07-04) VALID_VIEWS sweep: the semantic test —
 // "any 'my-X' or 'personalized-Y' surface belongs here" — walked
 // every VALID_VIEWS entry. Result: only city-dashboard + following
 // meet the test. Other candidates ruled out:
 // - `guide` is a public YouTube channel viewer (city guide);
 // not profile-scoped despite the personal name.
 // - `search` / `broadcast` / `ledger` are public civic surfaces
 // per project mission.
 // - `scan` / `audit` / `watermark-verify` / `watermark-debug`
 // are public verification surfaces by design.
 // - `creators` / `cast-member` / `truth-book` are OWNER_ONLY per
 // pre-launch gate (higher tier, not this one).
 // - `home` / `city` are the public browsing surfaces.
 // Any NEW view that renders "the caller's own X" (their follows,
 // their proposed queries, their dashboard, their personalization)
 // belongs here. Any view that renders civic public data does not.
 const SIGNED_IN_ONLY_VIEWS: Array<NavigationState["view"]> = [];
 const signedInView = SIGNED_IN_ONLY_VIEWS.includes(navigation.view);
 const signedInGateLoading = signedInView && currentUser.loading;
 const requestedSignedInView =
 signedInView && !currentUser.loading && !currentUser.user;

 const navigate = (view: string, params?: any) => {
 // A view name outside the render switch paints the TopBar over an
 // empty body (the 2026-07-10 "All Channels → blank page" bug:
 // navigate("channels") set a view nothing renders, and only a
 // reload healed it because the URL parser rejects unknown names).
 // Unknown names land on home loudly instead of rendering nothing.
 if (!VALID_VIEWS.includes(view as NavigationState["view"])) {
 console.warn(`navigate: unknown view "${view}" — landing on home`);
 view = "home";
 params = undefined;
 }
 if (
 publicPlane &&
 !PUBLIC_VIEWS.has(view as NavigationState["view"])
 ) {
 console.warn(`navigate: view "${view}" is unavailable on the public plane`);
 view = "home";
 params = undefined;
 }
 setIsTransitioning(true);
 setTimeout(() => {
 setNavHistory(prev => [...prev, navigation]);
 const next: NavigationState = {
 view: view as NavigationState["view"],
 params,
 };
 setNavigation(next);
 // A2 (2026-07-01): browsing now writes the address bar. Every
 // in-app navigation is a real history entry — the URL is shareable
 // and the browser back button works (popstate effect below).
 try {
 window.history.pushState(
 { zspanView: next.view },
 "",
 buildUrlForNavigation(next),
 );
 } catch {
 // history API unavailable (ancient embed context) — non-fatal.
 }
 window.scrollTo(0, 0);
 setIsTransitioning(false);
 }, 150);
 };

 const goBack = () => {
 setIsTransitioning(true);
 setTimeout(() => {
 let target: NavigationState;
 if (navHistory.length > 0) {
 const prev = navHistory[navHistory.length - 1];
 setNavHistory(h => h.slice(0, -1));
 target = prev;
 } else {
 target = { view: "home" };
 }
 target = restrictNavigationToPlane(target, publicPlane);
 setNavigation(target);
 // In-app back keeps the URL truthful without unwinding browser
 // history depth (replaceState, not back()) — browser-back and
 // in-app-back stay independent but both leave a correct URL.
 try {
 window.history.replaceState(
 { zspanView: target.view },
 "",
 buildUrlForNavigation(target),
 );
 } catch {
 /* non-fatal */
 }
 window.scrollTo(0, 0);
 setIsTransitioning(false);
 }, 150);
 };

 // A2 (2026-07-01): browser back/forward drive the SPA. popstate fires
 // on real history traversal; re-parse the URL (same parser as initial
 // load) and adopt it WITHOUT pushing — the traversal already moved the
 // history pointer. Also stamp the entry state once on mount so the
 // first back-press has a state to return to.
 useEffect(() => {
 try {
 window.history.replaceState(
 { zspanView: navigation.view },
 "",
 buildUrlForNavigation(navigation),
 );
 } catch {
 /* non-fatal */
 }
 const onPopState = () => {
 const parsed = restrictNavigationToPlane(
 parseInitialNavigationFromUrl() ?? { view: "home" as const },
 publicPlane,
 );
 setIsTransitioning(true);
 setTimeout(() => {
 setNavigation(parsed);
 window.scrollTo(0, 0);
 setIsTransitioning(false);
 }, 150);
 };
 window.addEventListener("popstate", onPopState);
 return () => window.removeEventListener("popstate", onPopState);
 // eslint-disable-next-line react-hooks/exhaustive-deps
 }, []);

 // Universal top bar — mounts on operator-gated + operator-adjacent +
 // public reader surfaces. Skipped on the cinematic HQ + Guide views
 // (their full-bleed design language conflicts with a fixed top strip;
 // each has its own integrated chrome — HQ's SettingsCloudPanel anchored
 // to cloud puffs / fog band, Guide's view-mode toggle). Also skipped on
 // CityPage + BroadcastPage which already use the full viewport via
 // their own `h-screen` flex columns, on home-classic (legacy fallback),
 // and on cast-member which mounts as a centered modal-like panel.
 const TOPBAR_SKIP_VIEWS: Array<NavigationState["view"]> = [
 "hq",
 "guide",
 "city",
 "broadcast",
 "home-classic",
 "cast-member",
 ];
 const showTopBar = !TOPBAR_SKIP_VIEWS.includes(navigation.view);

 //disclaimer-gate scopeKey: ONLY defined for episode pages
 // (BroadcastPage). The Provider's auto-fire effect opens the modal
 // whenever scopeKey changes from undefined → defined, so leaving
 // non-broadcast views with `undefined` is what keeps the gate from
 // firing on ChannelsPage, SpeakerRosterReviewPage, operator surfaces,
 // etc. Per operator 2026-06-26: *"the disclaimer is on episodes only
 // not on the entire website... only when they click into an episode
 // and see our generated data big time (which is why we need it,
 // front page and others are beautiful)"*.
 // Session-30 (2026-07-04): peek mode (?peek=1) opens the broadcast in
 // an operator-terminal iframe for a fast visual sanity check before
 // hitting [Make Public →]. The disclaimer gate firing inside the peek
 // iframe would be pure friction — the operator is doing the review
 // pass, not consuming as a public reader. Skip the scope key when peek
 // is set so the modal never opens inside the iframe.
 // RR-8 Tier C (2026-07-12): peek is owner-only. A crafted/shared ?peek=1
 // link must NOT skip the data-accuracy disclaimer for a non-owner — the
 // operator invokes peek from the (owner-gated) operator-terminal iframe,
 // where currentUser.isOwner is true. During the brief useCurrentUser load
 // isOwner is false, so a non-owner never gets a disclaimer-free window.
 const isPeekView =
 typeof window !== "undefined" &&
 new URLSearchParams(window.location.search).get("peek") === "1" &&
 currentUser.isOwner;
 const disclaimerScopeKey = navigation.view === "broadcast" && !isPeekView
 ? `broadcast/${(navigation.params as any)?.meetingId ?? (navigation.params as any)?.publicId ?? "default"}`
 : undefined;

 return (
 <ThemeProvider defaultTheme="dark" storageKey="zspan-theme">
 <PublicDataDisclaimerProvider
 scopeKey={disclaimerScopeKey}
 autoAck={isPeekView}
 >
 <HandTrackingProvider>
 <div className="min-h-screen bg-background text-foreground">
 {/* Light-account auth pill — fixed top-right at z-[60] so it
 sits above TopBar (z-40) without being affected by its
 sticky stacking context. Present on every view EXCEPT the
 Guide (James 2026-06-14: the Guide is a no-sign-in surface
 used by people who only want the directory — the pill
 shouldn't nag them). Coexists with TopBar's left + center
 clusters and OwnerOnly affordances; it answers a different
 question (which Google-OAuth user is signed in, if any). Per
 ACCOUNT_SYSTEM_SPEC chunk 2.
 V1-Polish-13 (2026-06-14): top-1.5 vertically centers the
 32px pill in the 44px (h-11) TopBar — top-3 left it 6px low
 with its bottom edge flush against the bar border; right-5
 matches the bar's px-5 gutter so left/right align. */}
 {/* Session-30 (2026-07-04): pill also hides on broadcast view.
 The right-hand Librarian column starts at the same top-right
 coordinates the fixed pill occupies; overlap-visually reads
 as chrome cluster. Per operator direction, don't render the
 pill on the broadcast page — the sign-in state is already
 reachable from the Guide + Channels landing surfaces. */}
 {/* Session-31 (2026-07-04, revised same-day) — SignInPill now
 lives INSIDE the TopBar's own flex flow (see TopBar.tsx),
 where it can't overlap the OPERATOR dropdown. TopBar-less
 surfaces may still use the standalone portal, but Guide,
 Broadcast, and HQ deliberately hide it. HQ has its own
 cinematic chrome; rendering the portal there resurrected the
 retired always-visible floating pill. */}
 {navigation.view !== "guide" &&
 navigation.view !== "broadcast" &&
 navigation.view !== "hq" &&
 !showTopBar && (
 <SignInPill onNavigate={navigate} />
 )}
 {/* V1-HandTracking-1 — bottom-of-page hand-tracking toggle +
 locked ⓘ disclosure. Inactive by default; MediaPipe SDK +
 model + webcam stream are lazy-loaded only when the user
 flips the toggle on. The full-viewport canvas overlay is
 rendered by HandTrackingProvider's nested Overlay, not
 here — this is just the button affordance. */}
 <HandTrackingToggle />
 <div
 ref={contentRef}
 className={`transition-opacity duration-200 ${isTransitioning ? "opacity-0" : "opacity-100"}`}
 >
 {ownerGateLoading || signedInGateLoading ? (
 <div className="flex min-h-screen items-center justify-center">
 <div className="text-foreground/40 text-sm">Loading…</div>
 </div>
 ) : requestedOwnerView ? (
 <ViewerModeFallback
 onBack={() => {
 setNavigation({ view: "home" });
 setNavHistory([]);
 }}
 surface={ownerSurfaceLabel(navigation.view)}
 />
 ) : requestedSignedInView ? (
 // Session-31 (2026-07-04) — anonymous visitor hit a
 // signed-in-only view (city-dashboard, following) via
 // direct URL. Redirect to home rather than show them a
 // stale sign-in nag or a shell of the surface. Their
 // top-right SignInPill remains as the entry point if
 // they want to sign in.
 (() => {
 setTimeout(() => {
 setNavigation({ view: "home" });
 setNavHistory([]);
 }, 0);
 return (
 <div className="flex min-h-screen items-center justify-center">
 <div className="text-foreground/40 text-sm">
 Redirecting…
 </div>
 </div>
 );
 })()
 ) : (
 <>
 {showTopBar && (
 <TopBar
 view={navigation.view}
 onNavigate={navigate}
 onOperatorSearch={(q) => setOperatorSearchQuery(q)}
 onGenerateReport={(q) => setReportQuery(q)}
 showSignInPill
 />
 )}
 {navigation.view === "home" && (
 <ChannelsPage
 onNavigate={navigate}
 selectCounty={navigation.params?.countyName}
 selectCity={navigation.params?.cityName}
 selectNonce={navigation.params?.channelPick}
 resetNonce={navigation.params?.resetToCounties}
 />
 )}
 {navigation.view === "guide" && <GuideRoot onNavigate={navigate} />}
 {navigation.view === "watermark-debug" && <WatermarkDebugPage />}
 {navigation.view === "watermark-verify" && <WatermarkVerifyPage />}
 {navigation.view === "scan" && <WatermarkScanPage />}
 {navigation.view === "audit" && <AuditPage />}
 {!IS_SHOWCASE && navigation.view === "corrections" && (
 <CorrectionsPage
 meetingId={navigation.params?.meetingId}
 publicId={navigation.params?.publicId}
 disputeContext={navigation.params?.disputeContext}
 />
 )}
 {navigation.view === "registry" && <RegistryPage />}
 {navigation.view === "following" && <FollowingPage onNavigate={navigate} />}
 {navigation.view === "creators" && <CreatorsView onNavigate={navigate} />}

 {navigation.view === "city-dashboard" && (
 <CityDashboardPage
 cityName={navigation.params?.cityName}
 onNavigate={navigate}
 onBack={goBack}
 />
 )}

 {navigation.view === "hq" && (
 <HQRoot
 onNavigate={navigate}
 urlForcedMode={
 navigation.params?.compare
 ? "compare"
 : navigation.params?.legacy
 ? "v1"
 : undefined
 }
 />
 )}

 {navigation.view === "home-classic" && (
 <HomePage onNavigate={navigate} />
 )}

 {navigation.view === "city" &&
 (navigation.params?.cityName && navigation.params?.countyName ? (
 <CityPage
 cityName={navigation.params.cityName}
 countyName={navigation.params.countyName}
 onBack={goBack}
 />
 ) : (
 <ViewContextRequired
 surface="City view"
 body="The city view shows a specific city's meeting list. Pick a state from Channels, then a county, then a city to enter it."
 onBack={() => navigate("home", { resetToCounties: Date.now() })}
 />
 ))}

 {navigation.view === "search" && (
 <div className="min-h-screen bg-background">
 {/* Keyed on the query so a fresh TopBar search remounts
 SearchPage and re-runs, even when already on this view. */}
 <SearchPage
 key={`search-${navigation.params?.query ?? ""}`}
 initialQuery={navigation.params?.query}
 />
 </div>
 )}

 {navigation.view === "calendar-health" && (
 <CalendarHealthPage onBack={goBack} />
 )}
 {navigation.view === "v1-launch" && (
 <V1LaunchPage onBack={goBack} />
 )}
 {navigation.view === "dashboard" && (
 <ParserDashboard onBack={goBack} />
 )}

 {navigation.view === "settings" && (
 <div className="min-h-screen bg-background">
 <SettingsPage onNavigate={navigate} />
 </div>
 )}

 {navigation.view === "broadcast" &&
 (navigation.params?.meetingId || navigation.params?.publicId ? (
 <BroadcastPage
 meetingId={navigation.params.meetingId}
 publicId={navigation.params.publicId}
 // [All Channels] goes to the channel picker, not
 // to the last item on the nav stack — otherwise
 // clicking through sibling episodes leaves the
 // history full of broadcasts and "back" cycles
 // through them instead of returning to channels.
 // Operator flagged 2026-07-04: goBack was routing
 // to the last broadcast, not to the picker.
 onBack={() => navigate("home", { resetToCounties: Date.now() })}
 onNavigate={navigate}
 initialSeek={
 typeof navigation.params?.seek === "number"
 ? navigation.params.seek
 : undefined
 }
 />
 ) : (
 <ViewContextRequired
 surface="Broadcast page"
 body="A broadcast page shows a specific meeting's outputs. Open one from a city's meeting list — pick a state, county, and city from Channels."
 onBack={() => navigate("home", { resetToCounties: Date.now() })}
 />
 ))}

 {/*City Desk — operator-preview enterprise-wrapper
 suite, PrisonBreak paper theme. Owner-only via
 OWNER_ONLY_VIEWS; all panel logic is a local mock. */}
 {navigation.view === "city-desk" && (
 <CityDeskPage onNavigate={navigate} />
 )}

 {/*iteration 3 — the try-it walkthrough. Session-only
 React state by design: nothing a demo visitor types is
 ever persisted. */}
 {navigation.view === "city-desk-demo" && (
 <CityDeskDemoPage onNavigate={navigate} />
 )}

 {navigation.view === "terminal" && (
 <OperatorTerminal onNavigate={navigate} />
 )}

 {navigation.view === "ledger" &&
 (navigation.params?.cityName ? (
 <CityLedgerPage
 cityName={navigation.params.cityName}
 onBack={goBack}
 onNavigate={navigate}
 />
 ) : (
 <ViewContextRequired
 surface="City ledger"
 body="The ledger view shows a city's tracked-claims accountability log. Open it from a city's page."
 onBack={() => navigate("home", { resetToCounties: Date.now() })}
 />
 ))}

 {navigation.view === "disputed-quotes" && (
 <DisputedQuotesPage onBack={goBack} onNavigate={navigate} />
 )}

 {(() => {
 if (navigation.view !== "cast-member") return null;
 const castCity = navigation.params?.cityName;
 const castSeat = navigation.params?.seatId;
 if (!castCity || !castSeat) return null;
 return (
 <div className="min-h-screen bg-background px-6 py-8">
 <CastMemberPanel
 cityName={castCity}
 seatId={castSeat}
 onBack={goBack}
 onOpenTruthBook={topic =>
 navigate("truth-book", {
 cityName: castCity,
 seatId: castSeat,
 topic,
 })
 }
 />
 </div>
 );
 })()}

 {navigation.view === "truth-book" &&
 (navigation.params?.cityName && navigation.params?.seatId ? (
 <TruthBookPage
 cityName={navigation.params.cityName}
 seatId={navigation.params.seatId}
 focusTopic={navigation.params.topic}
 onBack={goBack}
 onOpenMeeting={meetingId =>
 navigate("broadcast", { meetingId })
 }
 />
 ) : (
 <ViewContextRequired
 surface="TruthBook"
 body="The TruthBook is a per-representative accountability surface — votes, attendance, position tracking over time. Open it from a council member's Cast profile: pick a city from Channels, then a member."
 onBack={() => navigate("home", { resetToCounties: Date.now() })}
 />
 ))}

 {navigation.view === "compiler" &&
 (navigation.params?.meetingId ? (
 <CompilerPage
 meetingId={navigation.params.meetingId}
 initialMode={navigation.params.compilerMode ?? "list"}
 onBack={goBack}
 />
 ) : (
 <ViewContextRequired
 surface="Compiler"
 body="The Compiler shows a meeting's parliamentary structure — motions, votes, agenda transitions as a typed conversational graph. Open it from a meeting's show page: pick a city from Channels, then a meeting, then the Compiler tab."
 onBack={() => navigate("home", { resetToCounties: Date.now() })}
 />
 ))}

 {navigation.view === "vocabulary-inbox" && (
 <VocabularyInboxPage onBack={goBack} />
 )}

 {navigation.view === "escalations-inbox" && (
 <EscalationsInboxPage onBack={goBack} />
 )}

 {navigation.view === "speaker-roster-review" && (
 <SpeakerRosterReviewPage onBack={goBack} onNavigate={navigate} />
 )}

 {navigation.view === "autonomy" && (
 <AutonomyGatePage onBack={goBack} />
 )}
 </>
 )}
 </div>
 {/* V1.5-OperatorSearch-1 — modal floats above all routed content
 so closing it returns the operator to whatever view they were
 on. Owner-only at both the trigger surface (TopBarSearch) and
 the backend endpoint; the modal itself doesn't re-check
 because it can't render without operatorSearchQuery being set
 by an owner-gated affordance. */}
 {operatorSurfaceAllowed && (
 <OperatorSearchModal
 query={operatorSearchQuery}
 onClose={() => setOperatorSearchQuery(null)}
 onNavigate={navigate}
 />
 )}
 {/*Report-V0-1 — same floating-above-all-views placement and
 the same owner-only reasoning as OperatorSearchModal above. */}
 {operatorSurfaceAllowed && (
 <ReportGeneratorModal
 query={reportQuery}
 onClose={() => setReportQuery(null)}
 />
 )}
 </div>
 </HandTrackingProvider>
 </PublicDataDisclaimerProvider>
 </ThemeProvider>
 );
}

export default App;

function ownerSurfaceLabel(view: NavigationState["view"]): string {
 switch (view) {
 case "autonomy":
 return "operator-DM autonomy gate";
 case "terminal":
 return "operator terminal";
 case "dashboard":
 return "parser dashboard";
 case "settings":
 return "settings page";
 case "disputed-quotes":
 return "disputed quotes queue";
 case "vocabulary-inbox":
 return "vocabulary inbox";
 case "escalations-inbox":
 return "escalations inbox";
 case "compiler":
 return "conversational compiler";
 case "truth-book":
 return "Record";
 default:
 return "operator surface";
 }
}

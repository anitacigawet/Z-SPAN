/**
 * TopBar — universal cross-app navigation strip.
 *
 * Renders at the top of operator-gated and operator-adjacent views so the
 * operator can hop between HQ / Operator / Compiler / Settings + reach
 * the public reader surfaces (Channels / Guide / Search) without
 * leaving the page they're on.
 *
 * Three clusters separated by `|` rules:
 *   - Left:   [Z-SPAN] brand chip + public reader nav
 *   - Center: collapsed system-health dot + label + queue chip (Q:N)
 *             — click expands a popover with Server / Auth / Worker /
 *             Queue constituent rows + cache size + polling metadata.
 *             This single dot replaces the old StatusBanner (Server +
 *             Auth + Worker + Queue stacked horizontally) and the
 *             OperatorTerminal duplicate stat strip.
 *   - Right:  owner-only operator nav (HQ · Operator · Compiler · ⋯ ·
 *             Settings). Overflow `⋯` opens a popover with lower-
 *             frequency surfaces (Pipeline Monitor, Parser Dashboard,
 *             Disputed Quotes, Vocabulary Inbox, Escalations Inbox,
 *             Autonomy, Calendar Health).
 *
 * The current view's link gets an active-state ring so the operator
 * always knows where they are.
 *
 * Compiler in the right cluster routes to the most recently opened
 * compiler meeting (cached in localStorage). If nothing has been
 * compiled yet, the link is dimmed with a "open from a meeting first"
 * tooltip — Compiler needs a meetingId to be useful.
 */
import { useEffect, useRef, useState } from "react";
import { useCurrentUser } from "../hooks/useCurrentUser";
import { OwnerOnly } from "./OwnerOnly";
import { SignInPill } from "./SignInPill";
import { TopBarSearch } from "./TopBarSearch";
import { fetchForPlane } from "../lib/planeFetch";
import { isPublicPlane } from "../lib/trustPlane";

interface TopBarProps {
  // Current view name — drives active-state highlighting.
  view: string;
  // Navigate to a target view (uses the same callback shape as the rest
  // of App.tsx). Used by all link clicks.
  onNavigate: (view: string, params?: any) => void;
  // V1.5-OperatorSearch-1 — owner-only natural-language cross-meeting
  // search. TopBarSearch's owner-only dropdown affordance calls this
  // with the typed query; App.tsx opens the OperatorSearchModal. F11
  // audit-fix 2026-06-25: required (App.tsx always supplies it).
  onOperatorSearch: (query: string) => void;
  // S-122 Report-V0-1 — owner-only cited-report generation; App.tsx
  // opens the ReportGeneratorModal. Same wiring shape as onOperatorSearch.
  onGenerateReport: (query: string) => void;
  // Public-plane shells have no account or sign-in affordance.
  showSignInPill: boolean;
}

interface AuthState {
  status: "valid" | "expired" | "missing" | "unknown";
  details?: string;
}

interface ProcessingWO {
  id: number;
  city: string;
  elapsed_seconds: number | null;
}

interface SystemStatus {
  success: boolean;
  flask_up: boolean;
  auth?: AuthState;
  work_orders?: {
    stats?: { pending: number; processing: number; total: number };
    processing?: ProcessingWO[];
    queue_depth?: number;
  };
}

const POLL_INTERVAL_MS = 5000;
const COMPILER_LAST_KEY = "zspan.topbar.compiler-last-meeting-id";

const OPERATOR_MODE_VIEWS = new Set([
  "terminal", "dashboard", "city-dashboard", "city-desk", "city-desk-demo",
  "disputed-quotes", "vocabulary-inbox", "escalations-inbox", "autonomy",
  "calendar-health", "v1-launch", "compiler", "truth-book", "cast-member",
  "creators", "speaker-roster-review",
]);

export function workspaceToggleDestination(
  view: string,
  isOwner: boolean,
): { view: "home" | "workspace"; label: "View public" | "View workspace" } {
  const viewingPrivateMode =
    view === "workspace" || (isOwner && OPERATOR_MODE_VIEWS.has(view));
  return viewingPrivateMode
    ? { view: "home", label: "View public" }
    : { view: "workspace", label: "View workspace" };
}

// Cross-component menu mutual-exclusion (commit C critique blocker —
// without this, the TopBar's health popover, the TopBar's overflow menu,
// and the OperatorTerminal's per-page overflow can all open simultaneously
// and look nearly identical). Any menu that opens dispatches
// `zspan-menu-opened` with its own id; every other menu listens and
// closes itself when the originator isn't its own id.
const MENU_EVENT = "zspan-menu-opened";
export function announceMenuOpened(menuId: string): void {
  try {
    window.dispatchEvent(new CustomEvent(MENU_EVENT, { detail: { from: menuId } }));
  } catch {
    /* ignore — older browsers without CustomEvent */
  }
}
export function useCloseOnOtherMenu(myId: string, close: () => void): void {
  useEffect(() => {
    const onOpen = (e: Event) => {
      const detail = (e as CustomEvent).detail as { from?: string } | undefined;
      if (detail?.from && detail.from !== myId) close();
    };
    window.addEventListener(MENU_EVENT, onOpen);
    return () => window.removeEventListener(MENU_EVENT, onOpen);
  }, [myId, close]);
}

function formatElapsed(seconds: number | null): string {
  if (seconds == null) return "?";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return `${m}m${s.toString().padStart(2, "0")}`;
  const h = Math.floor(m / 60);
  return `${h}h${(m % 60).toString().padStart(2, "0")}m`;
}

function formatBytes(n: number): string {
  if (n >= 1024 * 1024 * 1024) return `${(n / (1024 * 1024 * 1024)).toFixed(1)}G`;
  if (n >= 1024 * 1024) return `${Math.round(n / (1024 * 1024))}M`;
  if (n >= 1024) return `${Math.round(n / 1024)}K`;
  return `${n}B`;
}

export function TopBar({
  view,
  onNavigate,
  onOperatorSearch,
  onGenerateReport,
  showSignInPill,
}: TopBarProps) {
  const publicPlane = isPublicPlane();
  // Session-31 (2026-07-04) — the Dashboard icon in the left cluster
  // maps to the owner's "my city" surface (auto-selected from profile
  // per CityDashboardPage's operator-direction comment). "My city" is
  // meaningless for anonymous visitors — they have no profile, so we
  // hide the icon unless the owner is signed in. Operator caught the leak on iPad.
  const { user, isOwner, loading: userLoading } = useCurrentUser();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [cacheBytes, setCacheBytes] = useState<number | null>(null);
  const [error, setError] = useState(false);
  const [lastCheckedAt, setLastCheckedAt] = useState<Date | null>(null);
  const [healthOpen, setHealthOpen] = useState(false);
  const [overflowOpen, setOverflowOpen] = useState(false);
  useCloseOnOtherMenu("topbar-health", () => setHealthOpen(false));
  useCloseOnOtherMenu("topbar-overflow", () => setOverflowOpen(false));
  const healthBtnRef = useRef<HTMLButtonElement | null>(null);
  const overflowBtnRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  // Compiler deep-link target — remember the most recently compiled meeting
  // so the right-cluster Compiler link has somewhere to go without making
  // the operator drill in from a meeting card.
  const [compilerLastMeetingId, setCompilerLastMeetingId] = useState<number | null>(() => {
    try {
      const raw = localStorage.getItem(COMPILER_LAST_KEY);
      const n = raw ? parseInt(raw, 10) : NaN;
      return Number.isFinite(n) && n > 0 ? n : null;
    } catch {
      return null;
    }
  });

  // Cross-tab signal — if another tab updates the compiler-last hint,
  // pick it up so the right-cluster link stays current.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== COMPILER_LAST_KEY) return;
      const n = e.newValue ? parseInt(e.newValue, 10) : NaN;
      setCompilerLastMeetingId(Number.isFinite(n) && n > 0 ? n : null);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // Poll /api/system/status — same shape StatusBanner used to consume.
  // No heartbeat POST from the TopBar (that's still OperatorTerminal's job
  // via its own session id) — we just read.
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetchForPlane({
          publicPath: "/public-api/health",
          operatorPath: "/api/system/status",
        });
        const body = await res.json();
        const data: SystemStatus = publicPlane
          ? {
              success: body?.status === "ok",
              flask_up: body?.status === "ok",
            }
          : body;
        if (cancelled) return;
        setStatus(data);
        setError(!data.flask_up);
        setLastCheckedAt(new Date());
      } catch {
        if (cancelled) return;
        setError(true);
        setStatus(null);
        setLastCheckedAt(new Date());
      }
      // Best-effort cache-size pull (Operator-page-specific endpoint —
      // when not deployed it just stays null).
      if (!publicPlane) try {
        const r = await fetch("/api/operator/source-cache-size");
        if (r.ok) {
          const j = await r.json();
          if (!cancelled && typeof j?.total_bytes === "number") {
            setCacheBytes(j.total_bytes);
          }
        }
      } catch {
        /* ignore */
      }
      // Other-session detection lives on the Operator page itself (inline
      // banner in Commit B). The TopBar's aggregate health dot stays focused
      // on Server / Auth / Worker / Queue.
    };
    poll();
    const id = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [publicPlane]);

  // Click-outside dismisses any open popover.
  useEffect(() => {
    if (!healthOpen && !overflowOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (popoverRef.current?.contains(t)) return;
      if (healthBtnRef.current?.contains(t)) return;
      if (overflowBtnRef.current?.contains(t)) return;
      setHealthOpen(false);
      setOverflowOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [healthOpen, overflowOpen]);

  // ── Derived state ────────────────────────────────────────────────
  const flaskDown = error || !status || !status.flask_up;
  const auth = status?.auth;
  const inFlight = status?.work_orders?.processing?.[0] ?? null;
  const queueDepth = status?.work_orders?.queue_depth ?? 0;

  // Single rolled-up color + label. Priority:
  //   red    = server down OR auth missing
  //   amber  = worker in-flight OR queue > 0 OR auth expired OR other session
  //   green  = everything quiet
  let aggregateColor: "red" | "amber" | "green" = "green";
  let aggregateLabel = "all systems normal";
  if (flaskDown) {
    aggregateColor = "red";
    aggregateLabel = "server down";
  } else if (auth?.status === "missing") {
    aggregateColor = "red";
    aggregateLabel = "sign-in missing";
  } else if (auth?.status === "expired") {
    aggregateColor = "amber";
    aggregateLabel = "sign-in expired";
  } else if (inFlight) {
    aggregateColor = "amber";
    aggregateLabel = `worker on WO #${inFlight.id}`;
  } else if (queueDepth > 0) {
    aggregateColor = "amber";
    aggregateLabel = `${queueDepth} in queue`;
  }

  const dotBg =
    aggregateColor === "red"
      ? "bg-red-500"
      : aggregateColor === "amber"
        ? "bg-amber-400"
        : "bg-emerald-500";
  const dotPulse =
    aggregateColor === "red" || (aggregateColor === "amber" && inFlight)
      ? "animate-pulse"
      : "";

  // Public-nav active state — Channels = home, Guide = guide, Search = search.
  const publicActive = (target: string): boolean => {
    if (target === "home") return view === "home";
    if (target === "guide") return view === "guide";
    if (target === "search") return view === "search";
    if (target === "city-dashboard") return view === "city-dashboard";
    return false;
  };

  // Owner-nav active state — HQ = hq, Operator = terminal, Compiler = compiler,
  // Settings = settings. Overflow items get their active treatment in the menu.
  const ownerActive = (target: string): boolean => {
    if (target === "hq") return view === "hq";
    if (target === "terminal") return view === "terminal";
    if (target === "compiler") return view === "compiler";
    if (target === "settings") return view === "settings";
    return false;
  };

  const linkBase =
    "px-2.5 py-1 text-[12px] uppercase tracking-widest transition-colors duration-100 whitespace-nowrap";
  const linkInactive = "text-white/55 hover:text-white";
  const linkActive = "text-white font-semibold";

  const openCompiler = () => {
    if (compilerLastMeetingId) {
      onNavigate("compiler", { meetingId: compilerLastMeetingId });
    } else {
      // No remembered last meeting (fresh account / cleared storage).
      // Navigate to the Compiler view anyway — the route renders a
      // ViewContextRequired empty-state explaining how to enter it
      // properly, instead of silently no-op'ing on the chip click.
      onNavigate("compiler");
    }
  };
  const workspaceToggle = workspaceToggleDestination(view, isOwner);

  return (
    <div
      className="sticky top-0 z-50 flex items-center gap-1 px-5 h-11 border-b border-white/10 bg-[#0A0A0C]/95 backdrop-blur text-white"
      style={{
        fontFamily:
          'ui-monospace, "SF Mono", "Menlo", "Consolas", "Liberation Mono", monospace',
      }}
    >
      {/* ── Left cluster — brand + public nav ─────────────────────── */}
      <button
        type="button"
        onClick={() => onNavigate("home", { resetToCounties: Date.now() })}
        className="flex items-center gap-2 pr-3 group"
        title="Z-SPAN — back to Channels"
      >
        {/* Highway-brand wordmark (2026-06-23). h-8 sized 2026-06-24 to match
            the 32px SignInPill (App.tsx:319 sets top-1.5 to center the 32px
            pill in the 44px TopBar) so both TopBar chips read as peer-shaped
            siblings rather than the wordmark dominating. object-contain
            preserves the PNG's 800×359 aspect ratio inside the height box. */}
        <img
          src="/brand/zspan-highway-wordmark.png"
          alt="Z-SPAN — Arizona"
          className="h-8 w-auto object-contain group-hover:opacity-90 transition-opacity"
          draggable={false}
        />
      </button>

      <nav className="flex items-center gap-0.5">
        <button
          type="button"
          onClick={() => onNavigate("home", { resetToCounties: Date.now() })}
          className={`${linkBase} ${publicActive("home") ? linkActive : linkInactive}`}
        >
          Channels
        </button>
        <button
          type="button"
          onClick={() => onNavigate("guide")}
          className={`${linkBase} ${publicActive("guide") ? linkActive : linkInactive}`}
        >
          Guide
        </button>
        {/* City Dashboard V0 — the per-city citizen page. Defaults to
         *  Kingman until the profile-driven auto-select ships.
         *
         *  Session-31 (2026-07-04): text label "DASHBOARD" replaced
         *  with an SVG 4-panel-grid glyph placeholder — will swap for
         *  a Gemini-generated brand icon later. Also gated to
         *  owner only (was visible + accessible via
         *  ?view=city-dashboard to anonymous, which is semantic
         *  nonsense — "my city" requires a profile to be "mine").
         *  Loading state renders nothing so the icon doesn't flicker
         *  in for a frame before the auth-check settles. */}
        {!userLoading && isOwner && (
          <button
            type="button"
            onClick={() => onNavigate("city-dashboard", { cityName: "Kingman" })}
            className={`${linkBase} ${publicActive("city-dashboard") ? linkActive : linkInactive} !px-2`}
            title="My city dashboard (Kingman)"
            aria-label="My city dashboard"
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="inline-block"
              aria-hidden="true"
            >
              <rect x="3" y="3" width="7" height="9" />
              <rect x="14" y="3" width="7" height="5" />
              <rect x="14" y="12" width="7" height="9" />
              <rect x="3" y="16" width="7" height="5" />
            </svg>
          </button>
        )}
      </nav>

      {/* Primary search (V1-Polish-12, 2026-06-14) — moved here from the
          ChannelsPage header pill and promoted to the network's main
          search. Google-style cycling placeholder; Enter → search page. */}
      <TopBarSearch onNavigate={onNavigate} onOperatorSearch={onOperatorSearch} onGenerateReport={onGenerateReport} />

      {/* ── Center cluster — health dot (owner-only; viewers don't need
         to read system telemetry) ────────────────────────────────── */}
      <OwnerOnly>
        <span className="mx-3 h-4 w-px bg-white/15" aria-hidden />

      <div className="relative flex items-center">
        <button
          type="button"
          ref={healthBtnRef}
          onClick={() => {
            setHealthOpen((o) => {
              const next = !o;
              if (next) announceMenuOpened("topbar-health");
              return next;
            });
          }}
          className="flex items-center gap-2 px-2.5 py-1 hover:bg-white/5 transition-colors"
          title="Click for system-health detail"
        >
          <span
            className={`inline-block w-2 h-2 rounded-full ${dotBg} ${dotPulse}`}
            style={{ minWidth: "0.5rem" }}
          />
          <span className="text-[12px] tracking-wide text-white/80">{aggregateLabel}</span>
          {queueDepth > 0 && (
            <span className="text-[11px] tracking-widest text-amber-300/90 tabular-nums">
              · Q:{queueDepth}
            </span>
          )}
        </button>

        {healthOpen && (
          <div
            ref={popoverRef}
            className="absolute top-full left-1/2 -translate-x-1/2 mt-2 w-[360px] border border-white/15 bg-[#0A0A0C] shadow-xl text-[12px] z-50"
            role="dialog"
          >
            <div className="px-4 py-3 border-b border-white/10">
              <div className="text-[10px] uppercase tracking-[0.18em] text-white/40 mb-2">
                System health
              </div>
              <HealthRow
                label="Server"
                value={flaskDown ? "Down" : "Up"}
                color={flaskDown ? "red" : "green"}
              />
              <HealthRow
                label="NotebookLM sign-in"
                value={
                  auth?.status === "valid"
                    ? "Valid"
                    : auth?.status === "expired"
                      ? "Expired"
                      : auth?.status === "missing"
                        ? "Missing"
                        : "Unknown"
                }
                color={
                  auth?.status === "valid"
                    ? "green"
                    : auth?.status === "expired"
                      ? "amber"
                      : auth?.status === "missing"
                        ? "red"
                        : "muted"
                }
                detail={auth?.details}
              />
              <HealthRow
                label="Worker"
                value={
                  inFlight
                    ? `On WO #${inFlight.id} · ${inFlight.city} · ${formatElapsed(inFlight.elapsed_seconds)}`
                    : "Idle"
                }
                color={inFlight ? "amber" : "muted"}
                pulse={!!inFlight}
              />
              <HealthRow
                label="Queue"
                value={
                  queueDepth === 0
                    ? "Empty"
                    : `${queueDepth} work order${queueDepth === 1 ? "" : "s"} waiting`
                }
                color={queueDepth > 0 ? "amber" : "muted"}
              />
            </div>
            <div className="px-4 py-2.5 border-b border-white/10 flex items-center justify-between text-[12px]">
              <span className="text-white/55">Source-clip cache</span>
              <span className="tabular-nums text-white/85">
                {cacheBytes != null ? formatBytes(cacheBytes) : "—"}
              </span>
            </div>
            <div className="px-4 py-2.5 text-[11px] text-white/45">
              <div>Refreshes every 5s</div>
              {lastCheckedAt && (
                <div className="mt-0.5 text-white/30">
                  Last checked {lastCheckedAt.toLocaleTimeString()}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      </OwnerOnly>

      {/* ── Right cluster — owner-only operator nav ────────────────
          Session-31 refactor (2026-07-04). Prior layout showed
          CITY DESK / HQ / Operator / Compiler / ⋯ / Settings as six
          separate top-bar chips (with responsive collapse of two into
          ⋯ at narrow widths). Operator direction: HQ stays as its own
          chip; every other operator surface (Console = former Operator
          terminal, Compiler, City Desk, Parser Dashboard, Pipeline
          Monitor, Disputed Quotes, Vocabulary Inbox, Escalations,
          Autonomy Board, Calendar Health) gets folded under a single
          OPERATOR dropdown so the top bar reads as [CHANNELS · GUIDE ·
          🗂 · | · dot · | · HQ · OPERATOR ▾ · <account pill>].
          Settings moved into the account-pill dropdown (see
          SignInPill.tsx). */}
      <div className="ml-auto flex items-center gap-1">
        {!userLoading && user && (
          <button
            type="button"
            onClick={() => onNavigate(workspaceToggle.view)}
            className={`${linkBase} ${view === "workspace" ? linkActive : linkInactive}`}
            title={workspaceToggle.view === "home" ? "Return to the public library" : "Open your personal workspace"}
          >
            {workspaceToggle.label}
          </button>
        )}
        <OwnerOnly>
          <span className="mr-3 h-4 w-px bg-white/15" aria-hidden />

          <nav className="flex items-center gap-0.5">
            <button
              type="button"
              onClick={() => onNavigate("hq")}
              className={`${linkBase} ${ownerActive("hq") ? linkActive : linkInactive}`}
              title="Z-SPAN HQ"
            >
              HQ
            </button>

            {/* OPERATOR dropdown — every operator-only surface lives
                under this. Click to open the menu; pick a destination. */}
            <div className="relative">
              <button
                type="button"
                ref={overflowBtnRef}
                onClick={() => {
                  setOverflowOpen((o) => {
                    const next = !o;
                    if (next) announceMenuOpened("topbar-overflow");
                    return next;
                  });
                }}
                className={`${linkBase} ${
                  view === "terminal" ||
                  view === "compiler" ||
                  view === "city-desk" ||
                  view === "dashboard" ||
                  view === "disputed-quotes" ||
                  view === "vocabulary-inbox" ||
                  view === "escalations-inbox" ||
                  view === "autonomy" ||
                  view === "calendar-health"
                    ? linkActive
                    : linkInactive
                } inline-flex items-center gap-1`}
                title="Operator — surfaces + tools"
                aria-haspopup="menu"
                aria-expanded={overflowOpen}
              >
                Operator
                <span className="text-[9px] leading-none opacity-70">▾</span>
              </button>
              {overflowOpen && (
                <div
                  ref={popoverRef}
                  className="absolute top-full right-0 mt-2 w-[240px] border border-white/15 bg-[#0A0A0C] shadow-xl py-1 z-50"
                  role="menu"
                >
                  {/* Console — the everyday operator terminal (work-order
                      queue + processing). Was the standalone "Operator"
                      chip; renamed per operator direction so the
                      dropdown parent can wear "Operator" as its label. */}
                  <OverflowItem
                    label="Console"
                    active={view === "terminal"}
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate("terminal");
                    }}
                  />
                  <OverflowItem
                    label="Compiler"
                    active={view === "compiler"}
                    onClick={() => {
                      setOverflowOpen(false);
                      if (compilerLastMeetingId) {
                        openCompiler();
                      } else {
                        onNavigate("compiler");
                      }
                    }}
                  />
                  <OverflowItem
                    label="City Desk"
                    active={view === "city-desk"}
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate("city-desk");
                    }}
                  />
                  <div className="h-px bg-white/10 mx-3 my-1" aria-hidden />
                  <OverflowItem
                    label="Parser Dashboard"
                    active={view === "dashboard"}
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate("dashboard");
                    }}
                  />
                  <OverflowItem
                    label="Pipeline Monitor"
                    active={false}
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate("dashboard", { statusOpen: true });
                    }}
                  />
                  <OverflowItem
                    label="Disputed Quotes"
                    active={view === "disputed-quotes"}
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate("disputed-quotes");
                    }}
                  />
                  <OverflowItem
                    label="Vocabulary Inbox"
                    active={view === "vocabulary-inbox"}
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate("vocabulary-inbox");
                    }}
                  />
                  <OverflowItem
                    label="Escalations Inbox"
                    active={view === "escalations-inbox"}
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate("escalations-inbox");
                    }}
                  />
                  <OverflowItem
                    label="Autonomy Board"
                    active={view === "autonomy"}
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate("autonomy");
                    }}
                  />
                  <OverflowItem
                    label="Calendar Health"
                    active={view === "calendar-health"}
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate("calendar-health");
                    }}
                  />
                </div>
              )}
            </div>

            {/* Settings retired from the TopBar 2026-07-04 — moved into
                the SignInPill account-pill dropdown per the "universal
                AI-provider settings live under the user identity that
                scopes them" reframe. */}
          </nav>
        </OwnerOnly>

        {/* Tip jar removed 2026-07-27 (operator call: premature before the
           project has real traction; ko-fi.com/zspan itself stays live).
           Restore condition: real traction. The removed element was a
           link-only Ko-fi anchor with an inline lucide-coffee SVG, anchored
           here just left of the sign-in pill — see PR #171 history for the
           exact block. */}

        {/* Session-31 (2026-07-04) — SignInPill now lives INSIDE the
           TopBar's flex flow so it can't overlap the OPERATOR
           dropdown. Previously the pill was fixed-positioned at
           `right-5 top-1.5` and collided with the top-right cluster.
           Inline placement + a small left gutter keeps the pill
           anchored to the same visual location without positioning
           conflicts. Views without a TopBar (Guide, Broadcast) render
           the pill separately via App.tsx's standalone-fixed path. */}
        {showSignInPill && (
          <>
            <span className="ml-2 h-4 w-px bg-white/15" aria-hidden />
            <div className="pl-1">
              <SignInPill onNavigate={onNavigate} layout="inline" />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Subcomponents ──────────────────────────────────────────────────

function HealthRow({
  label,
  value,
  color,
  detail,
  pulse,
}: {
  label: string;
  value: string;
  color: "red" | "amber" | "green" | "muted";
  detail?: string;
  pulse?: boolean;
}) {
  const dotColor =
    color === "red"
      ? "bg-red-500"
      : color === "amber"
        ? "bg-amber-400"
        : color === "green"
          ? "bg-emerald-500"
          : "bg-slate-500";
  return (
    <div className="flex items-center gap-2.5 py-1.5" title={detail}>
      <span
        className={`inline-block w-1.5 h-1.5 rounded-full ${dotColor} ${pulse ? "animate-pulse" : ""}`}
        style={{ minWidth: "0.375rem" }}
      />
      <span className="text-[12px] text-white/55 w-[148px] flex-none">{label}</span>
      <span className="text-[12px] text-white/90 flex-1 truncate">{value}</span>
    </div>
  );
}

function OverflowItem({
  label,
  active,
  onClick,
  className = "",
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      role="menuitem"
      className={`w-full text-left px-3 py-2 text-[12px] hover:bg-white/5 transition-colors ${
        active ? "text-white font-semibold" : "text-white/70"
      } ${className}`}
    >
      {label}
    </button>
  );
}

// Helper for pages that want to record "I'm the last compiler view" so the
// TopBar's Compiler link can deep-link back. Call from CompilerPage on mount.
export function rememberCompilerMeeting(meetingId: number): void {
  try {
    localStorage.setItem(COMPILER_LAST_KEY, String(meetingId));
  } catch {
    /* quota / disabled — ignore */
  }
}

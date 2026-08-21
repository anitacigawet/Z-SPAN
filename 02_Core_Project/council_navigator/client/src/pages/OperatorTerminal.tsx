/**
 * OperatorTerminal — Z-SPAN's Bloomberg-style backend.
 *
 * The "brains" view: a dense, monochrome, monospace data-grid for managing
 * the NotebookLM work-order queue. One operator. One action at a time.
 *
 * Aesthetic principles:
 *   - Monospace everywhere. Tabular numbers. No icons-as-decoration.
 *   - Mostly white-on-near-black. Sparse accent color (amber for in-flight,
 *     green for done, red for failed, dim for skipped).
 *   - Data density over whitespace. Tight rows. Borderless cells.
 *   - Status indicators are 4-letter codes in brackets. Dates are ISO-ish.
 *   - Every action button is a labeled bracketed verb: [PROCESS], [SCAN], [RETRY].
 *
 * Safety invariants:
 *   - Only ONE work order may be processing at a time. The UI tracks this
 *     locally (`processingId`) and disables every other action while one is
 *     in flight. Even after a refresh, any WO in state="processing" locks
 *     the panel.
 *   - We never auto-process. Every PROCESS click is an explicit human pull.
 *   - We never trigger a SCAN unprompted.
 */
import { useEffect, useMemo, useRef, useState } from "react";
// ConfirmDestructive import removed with the [BURN] modal per D-143.
// NotebookGcPanel removed per D-143 (NotebookLM removal 2026-07-01).
import EpisodeAuditCard, {
  type EpisodeAuditSummary,
} from "../components/EpisodeAuditCard";
import PublishConfirmModal from "../components/PublishConfirmModal";
import ReviewQueueSection from "../components/ReviewQueueSection";
import LibrarianAccessRequests from "../components/LibrarianAccessRequests";
import LibrarianTuningPanel from "../components/LibrarianTuningPanel";
import { fetchUnifiedQuotes } from "../utils/unifiedQuotes";
import { announceMenuOpened, useCloseOnOtherMenu } from "../components/TopBar";
import { useCurrentUser } from "../hooks/useCurrentUser";

// ── Types ──────────────────────────────────────────────────────────

interface WorkOrder {
  id: number;
  meeting_id: number;
  state: string;
  priority: number;
  notebook_id: string | null;
  youtube_video_url: string | null;
  meeting_video_url: string | null;
  requested_outputs: string | null;
  attempts: number;
  last_attempt_at: string | null;
  next_attempt_at: string | null;
  created_at: string;
  updated_at: string;
  error: string | null;
  meeting_title: string | null;
  meeting_date: string | null;
  city_name: string | null;
  county: string | null;
  // Review-gate approval (D-032). Null = not yet approved = [REVIEW] button visible.
  approved_at: string | null;
  approved_by: string | null;
  // Public visibility is independent of approval so an approved meeting can
  // be unpublished for re-review and later re-published.
  is_published: number;
  // T-004 match metadata. Null = no auto-match attempted; otherwise one of
  // 'high' | 'medium' | 'needs_review'. High matches auto-flip awaiting_video
  // to pending; medium/needs_review need operator [CONFIRM URL].
  video_url_match_confidence: string | null;
  video_url_match_method: string | null;
}

interface WorkOrderStats {
  pending?: number;
  processing?: number;
  awaiting_video?: number;
  awaiting_notebook?: number;
  completed?: number;
  failed?: number;
  skipped_too_old?: number;
  total?: number;
  [key: string]: number | undefined;
}

interface TerminalProps {
  onNavigate?: (view: string, params?: any) => void;
}

// ── Helpers ────────────────────────────────────────────────────────

// State→4-letter code, used only by the FILTER chips (the per-row state
// column is gone; row borders carry the DONE/FAIL signal, action buttons
// implicitly show whether a row is PEND vs WVID vs PROC).
// D-054: filter chips read as operator vocabulary, not DB-shaped labels.
// awaiting_notebook is a HISTORICAL state — no new WOs transition to it
// post-D-143 (NotebookLM removed 2026-07-01). Kept in the label map so
// pre-D-143 WOs still render sensibly; retirement of the state itself is
// filed as S-112.
const STATE_LABEL: Record<string, string> = {
  all: "All",
  pending: "Pending",
  processing: "Processing",
  awaiting_video: "Awaiting video",
  awaiting_notebook: "Legacy: awaiting outputs",
  completed: "Done",
  failed: "Failed",
  skipped_too_old: "Skipped",
};

// Per-chip tooltips for the FILTER row — D-054 cleanup so the operator doesn't
// have to remember the meaning of every status enum.
const STATE_TOOLTIP: Record<string, string> = {
  skipped_too_old:
    "Skipped: too old. The scanner enqueues only meetings within the recent window — older meetings are deliberately ignored to keep the queue current.",
  awaiting_video: "Waiting for autonomous video match — [MATCH ▸ this city] runs it now, or wait for the next haiku_match sweep. Per D-138, manual paste is retired.",
  awaiting_notebook: "Legacy pre-D-143 state — historical WOs waiting for the retired NotebookLM stage. No new WOs land here post-removal.",
  processing: "The worker is currently generating outputs for this meeting.",
  completed: "All outputs generated. Hit [PUBLISH] to flip it live.",
  failed: "Generation failed; retry with [RETRY].",
};

// Grid template for the work-order rows. Header + each data row share this.
const ROW_GRID = "28px minmax(200px,1fr) 180px 320px";

// ── Activity-log entry model ──────────────────────────────────────
// Each line carries a "kind" so the UI can color-code at a glance.
type LogKind = "system" | "scan" | "process" | "set-url" | "retry" | "ok" | "error" | "worker" | "warn";

interface LogEntry {
  ts: string;        // HH:MM:SS
  kind: LogKind;
  message: string;
  woId?: number;     // optional WO context
}

const KIND_COLOR: Record<LogKind, string> = {
  system:    "#71717A", // gray — UI-internal lifecycle
  scan:      "#60A5FA", // soft blue — discovery
  process:   "#F5A524", // amber — processing actions
  "set-url": "#A78BFA", // soft violet — config / human-in-loop
  retry:     "#FCD34D", // yellow — recovery
  ok:        "#22C55E", // green — success / completion
  error:     "#EF4444", // red — failure
  warn:      "#F59E0B", // orange — warning
  worker:    "#94A3B8", // slate — raw worker subprocess output (default)
};

const ACTIVITY_LOG_KEY = "zspan.operator-terminal.activity-log.v1";
const LOG_VERBOSITY_KEY = "zspan.operator-terminal.log-verbosity.v1";

// Heuristic: classify a worker stdout line by content into one of our kinds.
// The bridge logs in the format "YYYY-MM-DD HH:MM:SS [LEVEL] zspan.module: message"
// — so we sniff the level and keywords. Everything that doesn't match falls
// through to the generic "worker" kind so it still shows up.
function classifyWorkerLine(line: string): LogKind {
  const lc = line.toLowerCase();
  if (lc.includes("[error]") || lc.includes("traceback") || lc.includes("exception")) return "error";
  if (lc.includes("[warning]") || lc.includes("[warn]")) return "warn";
  if (
    lc.includes("completed") ||
    lc.includes("✓") ||
    lc.includes("[ok]") ||
    lc.includes("saved output") ||
    lc.includes("finished")
  ) return "ok";
  if (lc.includes("auth") || lc.includes("login") || lc.includes("cookie")) return "warn";
  return "worker";
}

const MONTHS_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  // Accept either YYYY-MM-DD or full ISO; render as "05/05/2026"
  // (Session-29 2026-07-03: switched from "May 05, 2026" per operator direction
  // — MM/DD/YYYY reads more scannably in a dense row than sentence-case month.)
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  if (!m) return s;
  return `${m[2]}/${m[3]}/${m[1]}`;
}

function fmtDateTime(s: string | null | undefined): string {
  if (!s) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2})/.exec(s);
  if (!m) return s;
  return `${m[2]}/${m[3]}/${m[1]} · ${m[4]}`;
}

function shortNb(id: string | null | undefined): string {
  if (!id) return "—";
  return id.length > 10 ? id.slice(0, 8) + "…" : id;
}

function meetingTypeFromTitle(t: string | null | undefined): string {
  if (!t) return "—";
  const dashIdx = t.indexOf(" - ");
  return dashIdx > 0 ? t.slice(0, dashIdx).trim() : t.trim();
}

// Whole days between a timestamp and now. Null when unparseable.
function daysSince(s: string | null | undefined): number | null {
  if (!s) return null;
  const t = Date.parse(s.replace(" ", "T"));
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.floor((Date.now() - t) / 86400000));
}

// D-054: render a work order's state the way an operator reads it — plain
// language + how long it's been there — never the raw DB enum/timestamps.
function humanizeWorkOrderStatus(
  wo: WorkOrder
): { label: string; color: string; pulse?: boolean } {
  switch (wo.state) {
    case "processing":
      return { label: "Generating…", color: "#F5A524", pulse: true };
    case "awaiting_video":
      // Session-32 (2026-07-04): "Needs a video link" was the wrong
      // framing per D-138 + [[never-frame-operator-paste-as-constraint]].
      // Primary fix landed in the `filtered` useMemo above — actionless
      // awaiting_video rows are now hidden from the default queue since
      // they're pipeline state, not operator work. This label still
      // renders when the operator explicitly filters to awaiting_video
      // for debug, or when a row shows because it's operator-actionable
      // (medium/needs_review candidate awaiting [CONFIRM URL]).
      // Matching the FILTER chip label at line 92 verbatim keeps the
      // vocabulary honest across the terminal.
      return { label: "Awaiting video", color: "#71717A" };
    case "awaiting_notebook":
      return { label: "Awaiting notebook", color: "#71717A" };
    case "failed": {
      const n = wo.attempts ?? 0;
      return {
        label: n > 0 ? `Failed · ${n} ${n === 1 ? "try" : "tries"}` : "Failed",
        color: "#EF4444",
      };
    }
    case "completed":
      if (wo.is_published) {
        return {
          label: wo.approved_at
            ? `Published · ${fmtDate(wo.approved_at)}`
            : "Published",
          color: "#22C55E",
        };
      }
      return wo.approved_at
        ? { label: "Approved · not public", color: "#F5A524" }
        : { label: "Done", color: "#22C55E" };
    case "pending": {
      const d = daysSince(wo.created_at);
      const age =
        d == null ? "" : d <= 0 ? " · today" : ` · ${d} ${d === 1 ? "day" : "days"}`;
      return { label: `Pending${age}`, color: "#9CA3AF" };
    }
    case "skipped_too_old":
      return { label: "Skipped · too old", color: "#52525B" };
    default:
      return { label: wo.state, color: "#71717A" };
  }
}

// D-054: turn a raw worker/operator log line into a plain operator sentence
// for the activity log's "curated" view. Strips the bridge-log prefix
// (timestamp · level · module path), maps known events to sentences, and
// falls back to the prefix-stripped text so unmatched lines still read
// cleanly. The full original line is always available via the row's hover.
function curateLogMessage(raw: string): string {
  const m = raw
    .replace(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*\s*/, "") // 2026-05-29 00:52:11,852
    .replace(/^\[[A-Za-z]+\]\s*/, "")                                  // [INFO] / [WARNING]
    .replace(/^(?:[a-z_]+\.[\w.]+|whisper_client|quote_align):\s*/i, "") // module path:
    .trim();
  const lc = m.toLowerCase();
  const grab = (re: RegExp): string | null => {
    const x = m.match(re);
    return x ? x[1] : null;
  };

  if (/\bwo\s*#?\d+:?\s*completed/.test(lc)) {
    const n = grab(/\((\d+)\s*outputs?\)/i);
    return n ? `Finished generating — ${n} outputs.` : "Finished generating.";
  }
  if (/finalized.*state=completed/.test(lc)) return "Marked complete.";
  if (/finalized.*state=failed/.test(lc)) return "Marked failed.";
  if (/single-shot result/.test(lc)) return "Work order finished.";
  if (/pre-flight ok|cookies loaded and verified/.test(lc)) return "Signed in to NotebookLM.";
  if (/auth status=expired|session cookies expired/.test(lc)) return "NotebookLM sign-in had expired — refreshing it.";
  if (/attempting auto-relogin|prompt detected/.test(lc)) return "Refreshing NotebookLM sign-in…";
  if (/auto-relogin succeeded/.test(lc)) return "NotebookLM sign-in refreshed.";
  if (/transcribed\s+\d+\s+words/.test(lc)) {
    const w = grab(/transcribed\s+(\d+)\s+words/i);
    return w ? `Transcribed the recording — ${w} words.` : "Transcribed the recording.";
  }
  if (/quotes persisted/.test(lc)) {
    const n = grab(/saved['":\s]*(\d+)/i);
    return n ? `Saved ${n} quotes.` : "Saved quotes.";
  }
  if (/member_attendance persisted/.test(lc)) {
    const n = grab(/saved['":\s]*(\d+)/i);
    return n ? `Recorded attendance — ${n} members.` : "Recorded attendance.";
  }
  if (/tracked_claims persisted/.test(lc)) {
    const n = grab(/saved['":\s]*(\d+)/i);
    return n ? `Tracked ${n} claims.` : "Tracked claims.";
  }
  if (/\bbuild ok/.test(lc)) return "Built the verification clips.";
  if (/\bingest ok/.test(lc)) return "Ingested the verification responses.";
  if (/\bpush ok/.test(lc)) return "Pushed to the flagship site.";
  if (/^published|broadcast is now live/.test(lc)) return "Published to the public channel.";
  // Fallback: prefix-stripped line — already free of timestamp/level/module.
  return m;
}

// Two-tap confirmation gate (D-039 + [[preventable-harms-must-be-prevented]]
// — production-leaking verbs need explicit consent, even when idempotent).
// Click 1 arms the action under a unique key (e.g. `push:42`); click 2 within
// the timeout window fires it. Arming a different key disarms the previous
// one — only one verb in the page is armed at a time, so the operator's
// intent stays unambiguous.
function useArmedConfirm(timeoutMs: number = 3000) {
  const [armedKey, setArmedKey] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    },
    []
  );

  const handleClick = (key: string, action: () => void) => {
    if (armedKey === key) {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setArmedKey(null);
      action();
      return;
    }
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    setArmedKey(key);
    timerRef.current = window.setTimeout(() => {
      setArmedKey((curr) => (curr === key ? null : curr));
      timerRef.current = null;
    }, timeoutMs);
  };

  return {
    isArmed: (key: string) => armedKey === key,
    handleClick,
  };
}

// ── Component ──────────────────────────────────────────────────────

export default function OperatorTerminal({ onNavigate }: TerminalProps) {
  // Attribution identity for publish/push/approve writes — the signed-in
  // session's email, never a hardcoded name (2026-07-09 session-49
  // audit-fix: five '"James"' literals forked vocabulary against the
  // F-6.2-normalized email form the DB's audit trail carries).
  const currentUser = useCurrentUser();
  const operatorIdentity = currentUser.user?.email ?? "operator";
  const [orders, setOrders] = useState<WorkOrder[]>([]);
  const [stats, setStats] = useState<WorkOrderStats>({});
  // Operator review-surface badge counts. Fetched alongside work orders
  // so the [DISPUTED · N], [VOCAB · N], and [ESCALATIONS · N] action-row
  // buttons reflect current backlog without an extra fetch. Defaults to
  // 0 until first load completes.
  const [badges, setBadges] = useState<{
    disputed: number;
    vocab_kingman: number;
    escalations_unack: number;
  }>({
    disputed: 0,
    vocab_kingman: 0,
    escalations_unack: 0,
  });
  // T-013 V4 — per-WO inflight-action state. Keyed by `${wo_id}:${action}`
  // so the same WO can have CLIPS + INGEST inflight independently. The
  // button reads inflightActions.has(key) to disable itself + swap its
  // label to the in-flight variant. Prevents double-spawn footguns
  // (especially on [BUILD], which fires a 10-min subprocess on cold cache).
  const [inflightActions, setInflightActions] = useState<Set<string>>(new Set());
  const inflightKey = (woId: number, action: string) => `${woId}:${action}`;
  const beginInflight = (woId: number, action: string) =>
    setInflightActions(prev => {
      const next = new Set(prev);
      next.add(inflightKey(woId, action));
      return next;
    });
  const endInflight = (woId: number, action: string) =>
    setInflightActions(prev => {
      const next = new Set(prev);
      next.delete(inflightKey(woId, action));
      return next;
    });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("all");
  const [processingId, setProcessingId] = useState<number | null>(null);
  // editingUrlId + urlInput + submitVideoUrl + canSetUrl + [SET URL] button
  // all removed per D-138 (2026-06-25) — manual video-URL paste is struck
  // from the project. haiku_match_videos.py is the autonomous floor.
  const [scanBusy, setScanBusy] = useState(false);
  const [matchBusy, setMatchBusy] = useState(false);
  // S-074 cascade-break — matcher results land in a review panel before
  // any WO state flips. Operator ticks the matches they approve, then
  // [PROMOTE SELECTED] fires the apply phase. Without this split, MATCH ALL
  // auto-promoted high-confidence rows on click → daemon (if loaded) drained
  // them through Whisper/Qdrant/Sonnet without further operator confirmation.
  type MatchPreviewRow = {
    city: string;
    meeting_id: number;
    meeting_date: string | null;
    meeting_title: string;
    had_existing_url: boolean;
    candidate: {
      confidence: "high" | "medium" | "needs_review";
      video_url: string;
      video_title: string;
      video_upload_date: string | null;
      method: string;
      reasoning: string;
    } | null;
  };
  type MatcherPreview = {
    rows: MatchPreviewRow[];
    approvals: Set<number>; // approved meeting_ids
    cities: string[]; // cities included in this run (for the header)
    ranAtIso: string;
  };
  const [matcherPreview, setMatcherPreview] = useState<MatcherPreview | null>(null);
  const [promoteBusy, setPromoteBusy] = useState(false);
  // D-030 notebook GC panel retired per D-143 (NotebookLM removal 2026-07-01).
  // Cities-with-YouTube-channel-registered. Fetched on mount; drives the
  // [MATCH ...] dropdown. Defaults to Kingman if available, else the first
  // registered city, else empty (in which case the buttons render disabled
  // with a helpful tooltip).
  const [channelCities, setChannelCities] = useState<
    Array<{ name: string; county: string | null; state: string | null }>
  >([]);
  // City filter (Commit B). Default "all" = no city scoping. Setting this to
  // a registered channel-city scopes BOTH the meeting-list filter AND the
  // [MATCH ▸ this city] button — one chip, two jobs (per Opus design proposal
  // 2026-06-04). When "all", MATCH is disabled with "Pick a city above first"
  // — the operator's intent is ambiguous otherwise. Persisted across reloads.
  const [cityFilter, setCityFilter] = useState<string>(() => {
    try {
      return localStorage.getItem("zspan.operator.city-filter") || "all";
    } catch {
      return "all";
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("zspan.operator.city-filter", cityFilter);
    } catch {
      /* ignore */
    }
  }, [cityFilter]);
  // Per-page overflow menu (CLEANUP + any future destructive ops); kept
  // separate from the universal TopBar overflow so the operator's
  // page-level destructive actions stay on the page they target. Wired
  // into the cross-component mutual-exclusion so it doesn't co-render
  // with the TopBar's health popover / overflow.
  const [pageOverflowOpen, setPageOverflowOpen] = useState(false);
  useCloseOnOtherMenu("operator-page-overflow", () => setPageOverflowOpen(false));
  // Activity log right rail — collapsed to a 24px tab by default; expands
  // on click. Persisted across reloads so the operator's preference sticks.
  const [logOpen, setLogOpen] = useState<boolean>(() => {
    try {
      return localStorage.getItem("zspan.operator.log-open") === "true";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("zspan.operator.log-open", String(logOpen));
    } catch {
      /* ignore */
    }
  }, [logOpen]);
  // OTHER SESSION inline banner — the heartbeat POST surfaces concurrent
  // operator clients (other tabs, the relay, manually-run CLI scripts) so
  // the operator can spot a colliding session before it bites. Dismissible
  // for the current session; reappears next time another session shows up.
  const [otherSessions, setOtherSessions] = useState<
    Array<{ client_kind: string; age_seconds: number; current_action: string | null }>
  >([]);
  const [otherSessionsDismissed, setOtherSessionsDismissed] = useState(false);
  const heartbeatSessionId = useRef<string>(
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `op-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  );

  // Review gate state (D-032). Approval persistence is now backed by the
  // /api/work-orders/<id>/approve endpoint — wo.approved_at !== null is the
  // authoritative "approved" flag, so the [REVIEW] button correctly hides
  // across page refreshes. Local state below only tracks which WO is
  // currently being reviewed and its parsed quotes.
  const [reviewingWo, setReviewingWo] = useState<WorkOrder | null>(null);
  // Chunk 11 (2026-05-26): which WO row currently has its overflow [⋯]
  // dropdown open. Null = no row is showing the dropdown. Single shared
  // value so opening one row's dropdown auto-closes another's.
  const [openOverflowWoId, setOpenOverflowWoId] = useState<number | null>(null);
  // Quotes Unification Refactor Chunk 8 (2026-05-26, copy revised
  // 2026-05-26): when the operator opens the publish modal, we fetch
  // the meeting's broadcast-hero quotes from the unified
  // `/api/quotes/meeting/<id>` endpoint and pass two counts to the modal:
  //   - publishHeroCount: total broadcast-hero quotes for the meeting
  //   - publishVerifiedCount: how many carry verified_status='verified'
  // The modal shows a status indicator (verified/pending) — verification
  // isn't something the operator checks via a checkbox; it's something
  // T-013 either completed or didn't. Citation-era meetings with no legacy
  // hero quotes treat that verification step as N/A; quote-bearing meetings
  // remain blocked until every hero quote is verified.
  const [publishHeroCount, setPublishHeroCount] = useState<number>(0);
  const [publishVerifiedCount, setPublishVerifiedCount] = useState<number>(0);
  const [publishQuoteCountsLoaded, setPublishQuoteCountsLoaded] = useState(false);

  // Session-30 (2026-07-04): video/broadcast peek modal. Row-title click
  // opens the meeting's broadcast page in an iframe overlay instead of
  // navigating away, so the operator can quick-check the content, close
  // the modal (X or Esc), then hit [Make Public →] without losing their
  // place in the terminal. Null = no modal; number = show the modal for
  // that meeting_id.
  const [peekMeetingId, setPeekMeetingId] = useState<number | null>(null);
  const [auditCardMeetingId, setAuditCardMeetingId] = useState<number | null>(
    null,
  );
  const [auditSummaries, setAuditSummaries] = useState<
    Record<string, EpisodeAuditSummary>
  >({});
  const auditSummaryFetchSequence = useRef(0);
  useEffect(() => {
    if (peekMeetingId === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPeekMeetingId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [peekMeetingId]);
  useEffect(() => {
    if (peekMeetingId === null) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [peekMeetingId]);

  // D-039 action-gating state. Non-null = the ConfirmDestructive modal is
  // [BURN] retired per D-143 (NotebookLM removal 2026-07-01) — nothing to
  // burn on NotebookLM's side anymore. [RETRY] handles the recoverable
  // failure path; unrecoverable failures land as `failed` for operator review.

  // Two-tap arming for the per-row [PUSH] verb. PUSH re-deploys a meeting's
  // payload + media to the flagship at zspan.org — it's idempotent but
  // production-visible, so it deserves explicit consent even though the
  // existing `ConfirmDestructive`/`PublishConfirmModal` are heavier than this
  // action warrants. Click 1 arms ("[CONFIRM PUSH]" + amber border, 3s
  // window); click 2 fires.
  const armedConfirm = useArmedConfirm();

  // Batch selection state (per James's "spreadsheet-style" design 2026-05-08).
  // Explicitly NO "process all" button — operator selects a subset, then
  // clicks [PROCESS SELECTED]. Sequential processing one at a time respects
  // the unofficial NotebookLM API's single-flight constraint. The batchQueue
  // holds the remaining ids after the current one finishes; an effect below
  // pops + dispatches the next when processingId returns to null.
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [batchQueue, setBatchQueue] = useState<number[]>([]);

  // Activity log: persisted across reloads via localStorage.
  // Newest entries first, capped at 400.
  const [logEntries, setLogEntries] = useState<LogEntry[]>(() => {
    try {
      const raw = localStorage.getItem(ACTIVITY_LOG_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.slice(0, 400) : [];
    } catch {
      return [];
    }
  });

  const refreshTimer = useRef<number | null>(null);
  // Worker-log tail offsets per WO so we resume reading from where we left off.
  const workerLogOffsets = useRef<Record<number, number>>({});
  const workerLogTimer = useRef<number | null>(null);

  // Persist log to localStorage whenever it changes.
  useEffect(() => {
    try {
      localStorage.setItem(ACTIVITY_LOG_KEY, JSON.stringify(logEntries.slice(0, 400)));
    } catch {
      /* quota / disabled — ignore */
    }
  }, [logEntries]);

  // Activity-log verbosity (D-054). "Curated" (default) hides the raw worker
  // stdout stream (kind "worker": httpx/transport/lifecycle noise) and shows
  // only meaningful events; "raw" shows the full subprocess output. Persisted.
  const [logVerbose, setLogVerbose] = useState<boolean>(() => {
    try {
      return localStorage.getItem(LOG_VERBOSITY_KEY) === "raw";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(LOG_VERBOSITY_KEY, logVerbose ? "raw" : "curated");
    } catch {
      /* ignore */
    }
  }, [logVerbose]);

  const log = (kind: LogKind, message: string, woId?: number) => {
    const ts = new Date().toISOString().slice(11, 19);
    setLogEntries(prev => [{ ts, kind, message, woId }, ...prev].slice(0, 400));
  };

  const fetchAuditSummaries = async (workOrders: WorkOrder[]) => {
    const sequence = ++auditSummaryFetchSequence.current;
    const meetingIds = Array.from(
      new Set(
        workOrders
          .filter(wo =>
            ["complete", "completed", "published"].includes(wo.state),
          )
          .map(wo => wo.meeting_id)
          .filter(meetingId => Number.isInteger(meetingId) && meetingId > 0),
      ),
    );

    if (meetingIds.length === 0) {
      setAuditSummaries({});
      return;
    }

    const batches: number[][] = [];
    for (let index = 0; index < meetingIds.length; index += 200) {
      batches.push(meetingIds.slice(index, index + 200));
    }

    try {
      const responses = await Promise.all(
        batches.map(async batch => {
          const result = await fetch(
            `/api/episode-audit/summary?meeting_ids=${batch.join(",")}`,
          );
          if (!result.ok) {
            throw new Error(`summary request failed (${result.status})`);
          }
          const body = await result.json();
          if (body?.status !== "ok" || !body.audits) {
            throw new Error("summary response was not recognized");
          }
          return body.audits as Record<string, EpisodeAuditSummary>;
        }),
      );
      if (sequence !== auditSummaryFetchSequence.current) return;

      const merged: Record<string, EpisodeAuditSummary> = {};
      for (const response of responses) {
        for (const [meetingId, summary] of Object.entries(response)) {
          const normalizedMeetingId = Number(meetingId);
          if (Number.isInteger(normalizedMeetingId) && normalizedMeetingId > 0) {
            merged[String(normalizedMeetingId)] = summary;
          }
        }
      }
      setAuditSummaries(merged);
    } catch (error) {
      console.warn("Couldn't load episode-audit summaries.", error);
      if (sequence === auditSummaryFetchSequence.current) {
        setAuditSummaries({});
      }
    }
  };

  const fetchAll = async () => {
    try {
      const [woRes, statsRes, badgesRes] = await Promise.all([
        fetch("/api/work-orders?limit=500"),
        fetch("/api/work-orders/stats"),
        fetch("/api/operator/badges"),
      ]);
      const woBody = await woRes.json();
      const statsBody = await statsRes.json();
      const badgesBody = badgesRes.ok ? await badgesRes.json().catch(() => null) : null;
      if (woBody?.success) {
        const workOrders = woBody.work_orders || [];
        setOrders(workOrders);
        void fetchAuditSummaries(workOrders);
      }
      if (statsBody?.success) setStats(statsBody.stats || {});
      if (badgesBody && typeof badgesBody.disputed_count === "number") {
        setBadges({
          disputed: badgesBody.disputed_count,
          vocab_kingman: badgesBody.vocab_pending_kingman ?? 0,
          escalations_unack: badgesBody.pending_escalations_unack ?? 0,
        });
      }
      setError(null);

      // If a WO is processing in the DB but our local lock is empty, adopt it.
      const inFlight = (woBody.work_orders || []).find((w: WorkOrder) => w.state === "processing");
      if (inFlight && processingId === null) {
        setProcessingId(inFlight.id);
      }
      // If our local lock points at one that has finished, release it.
      if (processingId !== null) {
        const cur = (woBody.work_orders || []).find((w: WorkOrder) => w.id === processingId);
        if (cur && cur.state !== "processing") {
          const finalKind: LogKind =
            cur.state === "completed" ? "ok" : cur.state === "failed" ? "error" : "system";
          setProcessingId(null);
          log(finalKind, `WO #${processingId} finalized → state=${cur.state}`, processingId);
        }
      }
    } catch (e: any) {
      setError(e?.message || "Failed to load work orders");
    } finally {
      setLoading(false);
    }
    // Heartbeat ping (D-039 follow-up) — register this tab as an active
    // operator session AND read back the board of OTHER active sessions
    // so the inline banner can warn the operator about collisions before
    // they bite. Failures are non-fatal (the banner just won't update).
    try {
      const hb = await fetch("/api/system/heartbeat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: heartbeatSessionId.current,
          client_kind: "operator_terminal",
        }),
      });
      const hbData = await hb.json();
      if (hbData && Array.isArray(hbData.sessions)) {
        setOtherSessions(hbData.sessions);
        // If a NEW session showed up after the operator dismissed the
        // banner, un-dismiss so they see the fresh collision.
        if (hbData.sessions.length === 0) {
          setOtherSessionsDismissed(false);
        }
      }
    } catch {
      /* ignore — banner just stays at its last state */
    }
  };

  useEffect(() => {
    fetchAll();
    refreshTimer.current = window.setInterval(fetchAll, 5000);
    // One-shot fetch of cities-with-YT-channels so the match dropdown can
    // populate. Refreshed only if the operator explicitly re-loads the page;
    // this list changes rarely (only when a new city's channel gets
    // registered via set_city_channel.py).
    fetch("/api/cities/with-channels")
      .then(r => r.json())
      .then(d => {
        if (!d?.cities) return;
        setChannelCities(d.cities);
      })
      .catch(() => {
        // Channel-cities endpoint unavailable — the city-filter chip just
        // stays at "all" + the [MATCH ▸ this city] button stays disabled.
      });
    return () => {
      if (refreshTimer.current !== null) window.clearInterval(refreshTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-poll faster while a WO is processing
  useEffect(() => {
    if (refreshTimer.current !== null) window.clearInterval(refreshTimer.current);
    const interval = processingId !== null ? 3000 : 5000;
    refreshTimer.current = window.setInterval(fetchAll, interval);
    return () => {
      if (refreshTimer.current !== null) window.clearInterval(refreshTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [processingId]);

  // Batch advancer (per James's "spreadsheet-style" design 2026-05-08).
  // When the currently processing WO finishes (processingId returns to
  // null) and the batchQueue has remaining ids, dispatch the next one.
  // Single-flight is preserved — we only ever fire processOne() while
  // processingId is null. Failed/timed-out WOs still advance the queue;
  // operator can see failures in the activity log and retry manually.
  useEffect(() => {
    if (processingId !== null) return;
    if (batchQueue.length === 0) return;
    const [next, ...rest] = batchQueue;
    setBatchQueue(rest);
    log("system", `BATCH advancing · ${rest.length} remaining`, next);
    processOne(next, { skipConfirm: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [processingId, batchQueue]);

  // Unqueue a WO from the batch (positions 2..N — the currently in-flight
  // one is already committed and can't be cancelled mid-call without
  // upstream API support, which the unofficial wrapper doesn't have).
  // Also untick its selectedIds checkbox so a subsequent [PROCESS SELECTED]
  // click doesn't accidentally re-queue it.
  const cancelQueued = (woId: number) => {
    setBatchQueue(prev => prev.filter(id => id !== woId));
    setSelectedIds(prev => {
      const next = new Set(prev);
      next.delete(woId);
      return next;
    });
    log("system", `UNQUEUED · WO removed from batch`, woId);
  };

  const filtered = useMemo(() => {
    let out = orders;
    if (filter === "all") {
      // Session-32 (2026-07-04): the default queue now hides pipeline-
      // internal states that carry no operator action, per D-138 +
      // [[never-frame-operator-paste-as-constraint]]. The operator
      // terminal shows the operator's work — not the pipeline's
      // bookkeeping.
      //
      // Hidden from default view:
      //   - awaiting_video WITHOUT an operator-actionable candidate.
      //     A row only earns visibility when the operator has the
      //     [CONFIRM URL] action (medium or needs_review confidence,
      //     video_url not yet set). Everything else in awaiting_video
      //     — no candidate at all (infra: haiku_match hasn't run OR
      //     found nothing OR S-037 V0 doesn't cover this archive),
      //     high-confidence auto-promoting rows, rows whose URL is
      //     already set — is pipeline state, not operator work.
      //   - awaiting_notebook — D-143 retired the NotebookLM stage
      //     entirely. Historical rows still exist in the DB; no
      //     operator action is available.
      //
      // The FILTER chip row still exposes both states so the operator
      // can opt-in to see pipeline internals for debugging. Default
      // stays scoped to actionable rows.
      out = out.filter(o => {
        if (o.state === "awaiting_notebook") return false;
        if (o.state === "awaiting_video") {
          const conf = o.video_url_match_confidence;
          const actionable =
            (conf === "medium" || conf === "needs_review") && !o.youtube_video_url;
          return actionable;
        }
        return true;
      });
    } else {
      out = out.filter(o => o.state === filter);
    }
    if (cityFilter !== "all") out = out.filter(o => o.city_name === cityFilter);
    return out;
  }, [orders, filter, cityFilter]);

  // ── Actions ───────────────────────────────────────────────────────

  const runScan = async () => {
    if (scanBusy || processingId !== null) return;
    setScanBusy(true);
    log("scan", "SCAN initiated");
    try {
      const res = await fetch("/api/work-orders/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const body = await res.json();
      if (body?.success) {
        const s = body.summary || {};
        log("ok", `SCAN ok · enqueued=${s.enqueued ?? 0} skipped=${s.skipped ?? 0} touched=${s.touched ?? 0}`);
      } else {
        log("error", `SCAN failed · ${body?.error || "unknown"}`);
      }
    } catch (e: any) {
      log("error", `SCAN error · ${e?.message || e}`);
    } finally {
      setScanBusy(false);
      fetchAll();
    }
  };

  // T-004 channel-to-video matcher trigger — RESTRUCTURED 2026-06-21 per
  // S-074 cascade-not-click. Phase 1: this fires the matcher in preview-only
  // mode (apply:false) — Haiku reasons over candidates but writes nothing.
  // Results land in `matcherPreview` state and render in the review panel.
  // Phase 2: operator ticks the rows they approve + clicks [PROMOTE SELECTED],
  // which fires /api/work-orders/promote-matches (zero LLM spend; pure DB
  // write via apply_match()). High-confidence matches at promote-time STILL
  // flip awaiting_video → pending; the only thing that changed is that the
  // operator now sees + approves which rows promote, instead of one button
  // collapsing both phases.
  //
  // Internal worker; the buttons call it. Returns the per-meeting rows so
  // [MATCH ALL] can aggregate across cities into one combined review panel.
  const _runMatchOnce = async (
    city: string
  ): Promise<MatchPreviewRow[] | null> => {
    log("scan", `MATCH-VIDEOS preview · city=${city}`);
    try {
      const res = await fetch(
        `/api/work-orders/match-videos/${encodeURIComponent(city)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ apply: false, min_confidence: "high" }),
        }
      );
      const body = await res.json();
      if (!body?.success) {
        log("error", `MATCH ${city} failed · ${body?.error || "unknown"}`);
        return null;
      }
      const results = Array.isArray(body.results) ? body.results : [];
      const rows: MatchPreviewRow[] = results.map((r: any) => {
        const top = Array.isArray(r.top_candidates) && r.top_candidates.length > 0
          ? r.top_candidates[0]
          : null;
        return {
          city,
          meeting_id: r.meeting_id,
          meeting_date: r.meeting_date,
          meeting_title: r.meeting_title || "",
          had_existing_url: !!r.had_existing_url,
          candidate: top
            ? {
                confidence: top.confidence,
                video_url: top.video_url,
                video_title: top.video_title || "",
                video_upload_date: top.video_upload_date || null,
                method: top.method || "",
                reasoning: top.reasoning || "",
              }
            : null,
        };
      });
      const high = rows.filter((r) => r.candidate?.confidence === "high").length;
      const med = rows.filter((r) => r.candidate?.confidence === "medium").length;
      const review = rows.filter(
        (r) => r.candidate?.confidence === "needs_review"
      ).length;
      const none = rows.filter((r) => !r.candidate).length;
      log(
        "ok",
        `MATCH ${city} preview · listed=${body.videos_listed} meetings=${body.meetings_inspected} (high=${high} med=${med} review=${review} none=${none})`
      );
      return rows;
    } catch (e: any) {
      log("error", `MATCH ${city} error · ${e?.message || e}`);
      return null;
    }
  };

  // [MATCH ▸ this city] reads its target from the city-filter chip (Commit B).
  // Disabled in the button itself when cityFilter === "all" — the early
  // return is a defense in case some other code path fires it anyway.
  const runMatch = async () => {
    if (matchBusy || processingId !== null) return;
    if (cityFilter === "all") return;
    setMatchBusy(true);
    try {
      const rows = await _runMatchOnce(cityFilter);
      if (rows) {
        // Pre-tick high-confidence rows; operator can untick before promote.
        const approvals = new Set(
          rows.filter((r) => r.candidate?.confidence === "high").map((r) => r.meeting_id)
        );
        setMatcherPreview({
          rows,
          approvals,
          cities: [cityFilter],
          ranAtIso: new Date().toISOString(),
        });
      }
    } finally {
      setMatchBusy(false);
    }
  };

  // Iterates every city in channelCities sequentially (never parallel —
  // the YouTube Data API quota + Haiku-via-Mac-relay both prefer serial).
  // Aggregates rows across cities into one combined review panel; operator
  // approves + promotes in one pass.
  const runMatchAll = async () => {
    if (matchBusy || processingId !== null) return;
    if (channelCities.length === 0) {
      log("error", "MATCH ALL · no cities have a YouTube channel registered");
      return;
    }
    setMatchBusy(true);
    log("system", `MATCH ALL preview · ${channelCities.length} cities`);
    let okCount = 0;
    const combined: MatchPreviewRow[] = [];
    try {
      for (const c of channelCities) {
        const rows = await _runMatchOnce(c.name);
        if (!rows) continue;
        okCount += 1;
        combined.push(...rows);
      }
      const high = combined.filter((r) => r.candidate?.confidence === "high").length;
      const med = combined.filter((r) => r.candidate?.confidence === "medium").length;
      const review = combined.filter(
        (r) => r.candidate?.confidence === "needs_review"
      ).length;
      log(
        "ok",
        `MATCH ALL preview done · ${okCount}/${channelCities.length} cities ok · candidates: high=${high} med=${med} review=${review} (no WOs flipped — review panel below)`
      );
      const approvals = new Set(
        combined.filter((r) => r.candidate?.confidence === "high").map((r) => r.meeting_id)
      );
      setMatcherPreview({
        rows: combined,
        approvals,
        cities: channelCities.map((c) => c.name),
        ranAtIso: new Date().toISOString(),
      });
    } finally {
      setMatchBusy(false);
    }
  };

  // S-074 phase 2 — commit operator-approved subset to DB. No Haiku re-fire.
  const promoteSelected = async () => {
    if (!matcherPreview || promoteBusy) return;
    const approved = matcherPreview.rows.filter(
      (r) => r.candidate && matcherPreview.approvals.has(r.meeting_id)
    );
    if (approved.length === 0) {
      log("error", "PROMOTE · no rows ticked");
      return;
    }
    setPromoteBusy(true);
    log("system", `PROMOTE initiated · ${approved.length} match(es)`);
    try {
      const res = await fetch("/api/work-orders/promote-matches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          matches: approved.map((r) => ({
            meeting_id: r.meeting_id,
            video_url: r.candidate!.video_url,
            confidence: r.candidate!.confidence,
            method: r.candidate!.method || "operator-approved",
          })),
        }),
      });
      const body = await res.json();
      if (!body?.success) {
        log("error", `PROMOTE failed · ${body?.error || "unknown"}`);
        return;
      }
      log(
        "ok",
        `PROMOTE ok · ${body.promoted}/${body.requested} committed (high-confidence rows flipped awaiting_video → pending)`
      );
      // Drop only the rows we committed so any deferred candidates stay
      // visible for a follow-up promote.
      const promotedIds = new Set(
        (body.results || [])
          .filter((r: any) => r.ok)
          .map((r: any) => r.meeting_id as number)
      );
      const remaining = matcherPreview.rows.filter(
        (r) => !promotedIds.has(r.meeting_id)
      );
      if (remaining.length === 0) {
        setMatcherPreview(null);
      } else {
        setMatcherPreview({
          ...matcherPreview,
          rows: remaining,
          approvals: new Set(
            Array.from(matcherPreview.approvals).filter((id) => !promotedIds.has(id))
          ),
        });
      }
      fetchAll();
    } catch (e: any) {
      log("error", `PROMOTE error · ${e?.message || e}`);
    } finally {
      setPromoteBusy(false);
    }
  };

  const toggleMatchApproval = (meetingId: number) => {
    setMatcherPreview((prev) => {
      if (!prev) return prev;
      const next = new Set(prev.approvals);
      if (next.has(meetingId)) next.delete(meetingId);
      else next.add(meetingId);
      return { ...prev, approvals: next };
    });
  };

  const setAllApprovals = (filter: "high" | "all" | "none") => {
    setMatcherPreview((prev) => {
      if (!prev) return prev;
      let next: Set<number>;
      if (filter === "none") {
        next = new Set();
      } else if (filter === "high") {
        next = new Set(
          prev.rows
            .filter((r) => r.candidate?.confidence === "high")
            .map((r) => r.meeting_id)
        );
      } else {
        next = new Set(
          prev.rows.filter((r) => r.candidate).map((r) => r.meeting_id)
        );
      }
      return { ...prev, approvals: next };
    });
  };

  const dismissMatchPreview = () => setMatcherPreview(null);

  // submitVideoUrl removed per D-138 (2026-06-25). Manual video-URL paste
  // struck from the project; the endpoint at /api/work-orders/<id>/set-video-url
  // now returns HTTP 410 Gone pointing at parsers/scripts/haiku_match_videos.py
  // as the autonomous replacement.

  const processOne = async (woId: number, opts?: { skipConfirm?: boolean }) => {
    if (processingId !== null) return;
    // Direct [PROCESS] clicks ask for confirmation. Callers like retryOne
    // and the batch queue pass skipConfirm=true because the operator
    // already confirmed at the parent action (clicking [RETRY] /
    // [PROCESS SELECTED]). Without skipConfirm, the browser also blocks the
    // second prompt for not being inside a user-gesture chain.
    if (!opts?.skipConfirm) {
      if (!confirm(`Process WO #${woId}? This will spawn one worker run.`)) return;
    }
    setProcessingId(woId);
    workerLogOffsets.current[woId] = 0; // restart tail from the top of the new log
    log("process", `PROCESS spawning worker (single-shot)`, woId);
    try {
      const res = await fetch(`/api/work-orders/${woId}/process`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const body = await res.json();
      if (body?.success) {
        log("process", `PROCESS spawned · ${body.cmd || ""}`, woId);
      } else {
        log("error", `PROCESS spawn failed · ${body?.error || "unknown"}`, woId);
        setProcessingId(null);
      }
    } catch (e: any) {
      log("error", `PROCESS error · ${e?.message || e}`, woId);
      setProcessingId(null);
    } finally {
      fetchAll();
    }
  };

  // [RETRY] is one-click: flip state back to pending AND immediately
  // kick off processOne. Per James 2026-05-12: previous behavior was a
  // two-step path (button reverted to [PROCESS] after retry, requiring a
  // second click), which felt wrong. Now retry → process is atomic from
  // the operator's POV; single-flight is still preserved because retryOne
  // bails early if anything else is in flight.
  const retryOne = async (woId: number) => {
    if (processingId !== null) return;
    log("retry", `RETRY resetting to pending`, woId);
    let resetOk = false;
    try {
      const r = await fetch(`/api/work-orders/${woId}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      resetOk = r.ok;
    } catch (e: any) {
      log("error", `RETRY error · ${e?.message || e}`, woId);
    }
    await fetchAll();
    if (resetOk) {
      // Pop straight into processing. D-033 makes this safe: previously-
      // succeeded outputs skip (Case A); already-on-NotebookLM Studio
      // artifacts download instead of regenerate (Case B); only truly
      // missing outputs get re-fetched. skipConfirm is needed because the
      // operator already confirmed via the [RETRY] click AND because the
      // browser blocks a second confirm() not in the user-gesture chain.
      processOne(woId, { skipConfirm: true });
    }
  };

  // burnOne + runBurn removed per D-143 (NotebookLM removal 2026-07-01) —
  // the notebook GC delete flow no longer exists.

  // ── Worker-log tail ───────────────────────────────────────────────
  // While a WO is in flight, poll its log file every 2s and stream new
  // lines into the activity log. The server returns a `next_offset` so
  // we resume reading from where we left off.
  useEffect(() => {
    if (workerLogTimer.current !== null) {
      window.clearInterval(workerLogTimer.current);
      workerLogTimer.current = null;
    }
    if (processingId === null) return;

    const woId = processingId;

    const tail = async () => {
      const since = workerLogOffsets.current[woId] ?? 0;
      try {
        const res = await fetch(`/api/work-orders/${woId}/log?since=${since}`);
        const body = await res.json();
        if (!body?.success) return;
        const next = body.next_offset ?? since;
        workerLogOffsets.current[woId] = next;
        const content: string = body.content || "";
        if (!content) return;
        const lines = content.split(/\r?\n/).filter(l => l.trim().length > 0);
        if (lines.length === 0) return;
        const newEntries: LogEntry[] = lines.map(line => {
          const ts = new Date().toISOString().slice(11, 19);
          return {
            ts,
            kind: classifyWorkerLine(line),
            // Trim leading bridge timestamp + level prefix so it's compact
            // in the column. Original survives in the title attribute.
            // Un-escape \uXXXX the worker sometimes emits for non-ASCII (→, ·)
            // so arrows/dots render as glyphs instead of literal "→".
            message: line
              .replace(/^\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\s/, "")
              .replace(/\\u([0-9a-fA-F]{4})/g, (_m, h) => String.fromCharCode(parseInt(h, 16))),
            woId,
          };
        });
        setLogEntries(prev => [...newEntries.reverse(), ...prev].slice(0, 400));
      } catch {
        /* swallow — next tick will retry */
      }
    };

    // Tick immediately then every 2s while in-flight.
    tail();
    workerLogTimer.current = window.setInterval(tail, 2000);
    return () => {
      if (workerLogTimer.current !== null) {
        window.clearInterval(workerLogTimer.current);
        workerLogTimer.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [processingId]);

  // ── Render ────────────────────────────────────────────────────────

  const STATES_FOR_FILTER = [
    "all",
    "pending",
    "processing",
    "awaiting_video",
    "awaiting_notebook",
    "completed",
    "failed",
    "skipped_too_old",
  ];

  return (
    <div
      className="w-full flex flex-col overflow-hidden bg-[#08080A] text-[#E4E4E5] font-mono text-[14px]"
      style={{
        fontFamily:
          'ui-monospace, "SF Mono", "Menlo", "Consolas", "Liberation Mono", monospace',
        // 100vh minus the 44px universal TopBar mounted by App.tsx.
        height: "calc(100vh - 2.75rem)",
      }}
    >
      {/* ── OTHER SESSION inline banner (D-039 follow-up; Opus 2026-06-04
         moved this out of the universal TopBar so the warning lives on
         the page where the collision actually matters). Dismissible for
         the current session — re-shows when a new session appears. */}
      {otherSessions.length > 0 && !otherSessionsDismissed && (
        <div className="flex-none px-8 py-2 bg-amber-400/10 border-b border-amber-400/30 flex items-center justify-between gap-4 text-[12px]">
          <div className="flex items-center gap-3 text-amber-200">
            <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
            <span className="font-semibold uppercase tracking-widest">Other session active</span>
            <span className="text-amber-100/70 normal-case tracking-normal">
              {otherSessions.map((s) => s.client_kind).join(", ")} — actions from this tab may
              collide with whatever the other session is doing.
            </span>
          </div>
          <button
            type="button"
            onClick={() => setOtherSessionsDismissed(true)}
            className="text-amber-200/60 hover:text-amber-100 uppercase tracking-widest text-[11px]"
            title="Hide this banner for the current session"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* ── Action band — left QUEUE WORK / right NEEDS ATTENTION ── */}
      {(() => {
        const pendingSelected = orders.filter(
          (o) => selectedIds.has(o.id) && o.state === "pending"
        ).length;
        const inBatch =
          batchQueue.length > 0 || (processingId !== null && pendingSelected > 0);
        const cityScoped = cityFilter !== "all";
        const attentionItems = [
          {
            count: badges.disputed,
            label: badges.disputed === 1 ? "disputed quote" : "disputed quotes",
            target: "disputed-quotes",
            color: "#A78BFA",
            tooltip:
              "Resolve disputed quotes (T-013 V4). Each row shows Gemini's verdict; operator verifies (with optional text edit) or rejects.",
          },
          {
            count: badges.vocab_kingman,
            label:
              badges.vocab_kingman === 1
                ? "vocab correction"
                : "vocab corrections",
            target: "vocabulary-inbox",
            color: "#F2A91C",
            tooltip:
              "Vocabulary Inbox (T-018). Promote Gemini-surfaced corrections into the city's canonical hints JSON. Count is Kingman pilot.",
          },
          {
            count: badges.escalations_unack,
            label: badges.escalations_unack === 1 ? "escalation" : "escalations",
            target: "escalations-inbox",
            color: "#22D3EE",
            tooltip:
              "Escalations Inbox (S-004). Agent-employee escalations that need operator attention.",
          },
        ].filter((p) => p.count > 0);
        const anyAttention = attentionItems.length > 0;

        // PROCESS SELECTED gets a "ghost" treatment at N=0 (transparent
        // background, dim border, dim text). Only flips to full amber
        // emphasis when there's actually a non-zero batch to fire. The
        // current page had this button bright-orange even at N=0 —
        // Opus flagged it as the biggest visual-hierarchy lie on the
        // page (2026-06-04).
        const processSelectedClasses =
          pendingSelected > 0
            ? "text-black bg-[#F5A524] hover:bg-[#FFB938] font-bold"
            : "text-white/35 bg-transparent border border-white/10";

        return (
          <div className="flex-none px-8 py-3 border-b border-white/10 bg-[#0A0A0C] flex items-start gap-6">
            {/* LEFT — Queue Work */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2.5 mb-1.5 text-[10px] uppercase tracking-[0.18em]">
                <span className="text-white/35">Queue Work</span>
                {processingId !== null ? (
                  <span className="flex items-center gap-1.5 text-[#F5A524] normal-case tracking-normal">
                    <span className="w-1.5 h-1.5 rounded-full bg-[#F5A524] animate-pulse" />
                    Currently processing meeting #{processingId} — actions locked
                  </span>
                ) : (
                  <span className="flex items-center gap-1.5 text-white/35 normal-case tracking-normal">
                    <span className="w-1.5 h-1.5 rounded-full bg-white/25" />
                    Worker paused
                  </span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <button
                  onClick={runScan}
                  disabled={scanBusy || processingId !== null}
                  className="text-[13px] uppercase tracking-widest text-white/80 hover:text-white border border-white/10 hover:border-white/30 px-4 py-2 disabled:opacity-30 disabled:cursor-not-allowed"
                  title="Walk recent meetings and enqueue work orders"
                >
                  {scanBusy ? "SCANNING…" : "SCAN"}
                </button>

                <button
                  onClick={runMatch}
                  disabled={
                    matchBusy ||
                    processingId !== null ||
                    !cityScoped ||
                    channelCities.length === 0
                  }
                  className="text-[13px] uppercase tracking-widest text-white/80 hover:text-white border border-white/15 hover:border-white/35 px-4 py-2 disabled:opacity-30 disabled:cursor-not-allowed"
                  title={
                    !cityScoped
                      ? "Pick a city above first (filter row)"
                      : `Preview Haiku matches for ${cityFilter}. Nothing commits until you tick rows + [PROMOTE] in the review panel.`
                  }
                >
                  {matchBusy ? "MATCHING…" : cityScoped ? `MATCH ▸ ${cityFilter.toUpperCase()}` : "MATCH ▸ THIS CITY"}
                </button>

                <button
                  onClick={runMatchAll}
                  disabled={
                    matchBusy || processingId !== null || channelCities.length === 0
                  }
                  className="text-[13px] uppercase tracking-widest text-white/80 hover:text-white border border-white/15 hover:border-white/35 px-4 py-2 disabled:opacity-30 disabled:cursor-not-allowed"
                  title={
                    channelCities.length === 0
                      ? "No cities have a YouTube channel registered yet"
                      : `Preview Haiku matches across ${channelCities.length} city channels (serial). Nothing commits until you tick rows + [PROMOTE] in the review panel.`
                  }
                >
                  {matchBusy ? "…" : `MATCH ALL · ${channelCities.length}`}
                </button>

                <button
                  onClick={() => {
                    const queue = orders
                      .filter((o) => selectedIds.has(o.id) && o.state === "pending")
                      .map((o) => o.id);
                    if (queue.length === 0) return;
                    log("system", `BATCH starting · ${queue.length} WOs queued`);
                    const [first, ...rest] = queue;
                    setBatchQueue(rest);
                    processOne(first, { skipConfirm: true });
                  }}
                  disabled={pendingSelected === 0 || processingId !== null || inBatch}
                  className={`text-[13px] uppercase tracking-widest px-4 py-2 disabled:cursor-not-allowed disabled:opacity-50 ${processSelectedClasses}`}
                  title={
                    processingId !== null
                      ? `Currently processing meeting #${processingId} — wait for it to finish or [CANCEL QUEUE]`
                      : pendingSelected === 0
                      ? "Tick at least one pending row first"
                      : "Sequentially process every selected pending WO. Single-flight enforced."
                  }
                >
                  PROCESS SELECTED · {pendingSelected}
                </button>

                {batchQueue.length > 0 && (
                  <button
                    onClick={() => {
                      const ids = [...batchQueue];
                      setBatchQueue([]);
                      setSelectedIds((prev) => {
                        const next = new Set(prev);
                        for (const id of ids) next.delete(id);
                        return next;
                      });
                      log(
                        "system",
                        `BATCH cleared · ${ids.length} WO(s) unqueued (in-flight WO unaffected)`
                      );
                    }}
                    className="text-[13px] uppercase tracking-widest text-[#3B82F6] hover:text-white border border-[#3B82F6]/45 hover:border-[#3B82F6] px-4 py-2"
                    title="Unqueue every WO waiting behind the current in-flight one. The currently processing WO stays in flight."
                  >
                    CANCEL QUEUE · {batchQueue.length}
                  </button>
                )}

                {/* Per-page overflow ⋯ — CLEANUP lives here (Opus 2026-06-04
                   moved it out of the always-visible verbs row because
                   it's destructive + low-frequency). Future destructive
                   ops land in this menu too. */}
                <div className="relative">
                  <button
                    type="button"
                    onClick={() =>
                      setPageOverflowOpen((o) => {
                        const next = !o;
                        if (next) announceMenuOpened("operator-page-overflow");
                        return next;
                      })
                    }
                    className="text-[14px] leading-none text-white/55 hover:text-white px-2.5 py-1.5"
                    title="More queue-work actions"
                    aria-label="More queue-work actions"
                  >
                    ⋯
                  </button>
                  {pageOverflowOpen && (
                    <>
                      <div
                        className="fixed inset-0 z-40"
                        onClick={() => setPageOverflowOpen(false)}
                      />
                      <div
                        className="absolute top-full left-0 mt-1.5 w-[280px] border border-white/15 bg-[#0A0A0C] shadow-xl py-1 z-50"
                        role="menu"
                      >
                        {/* Clear-stale-notebooks entry removed per D-143
                           (NotebookLM removal 2026-07-01). */}
                        <div className="px-4 py-2 text-[11px] text-white/40">
                          (No overflow actions)
                        </div>
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>

            {/* RIGHT — Needs Attention (hidden when all counts zero) */}
            {anyAttention && (
              <>
                <div className="h-12 w-px bg-white/10 flex-none mt-1" />
                <div className="flex-none">
                  <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-1.5">
                    Needs Attention
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {attentionItems.map((p) => (
                      <button
                        key={p.target}
                        onClick={() => onNavigate?.(p.target)}
                        className="flex items-center gap-2 text-[12px] px-3 py-1.5 border transition-colors hover:bg-white/5"
                        style={{
                          color: p.color,
                          borderColor: `${p.color}66`,
                        }}
                        title={p.tooltip}
                      >
                        <span className="tabular-nums font-semibold">{p.count}</span>
                        <span className="text-white/80">{p.label}</span>
                        <span className="text-white/35">→</span>
                      </button>
                    ))}
                  </div>
                </div>
              </>
            )}
          </div>
        );
      })()}

      {/* ── Review queue (moderation backlog from suggestions +
           creator-signup flags) — hides itself when empty so it
           doesn't take screen real estate when nothing is pending.
         ──────────────────────────────────────────────────────── */}
      <ReviewQueueSection />
      <LibrarianAccessRequests />
      <LibrarianTuningPanel />

      {/* ── Matcher review panel (S-074 cascade-break) ──────────
           Visible only after MATCH ▸ <city> or MATCH ALL fires in
           preview mode. Operator approves the rows to commit, then
           [PROMOTE SELECTED] writes apply_match() per row. The
           cheap reconnaissance phase stays one-click safe; the
           expensive downstream commitment becomes an explicit step.
         ──────────────────────────────────────────────────────── */}
      {matcherPreview && (() => {
        const total = matcherPreview.rows.length;
        const withCand = matcherPreview.rows.filter((r) => r.candidate).length;
        const high = matcherPreview.rows.filter(
          (r) => r.candidate?.confidence === "high"
        ).length;
        const med = matcherPreview.rows.filter(
          (r) => r.candidate?.confidence === "medium"
        ).length;
        const review = matcherPreview.rows.filter(
          (r) => r.candidate?.confidence === "needs_review"
        ).length;
        const approvedCount = matcherPreview.approvals.size;
        const confColor = (c: string) =>
          c === "high"
            ? "#22c55e"
            : c === "medium"
            ? "#eab308"
            : c === "needs_review"
            ? "#f97316"
            : "#64748b";
        const fmtDate = (iso: string | null) => {
          if (!iso) return "—";
          const d = new Date(iso + "T00:00:00");
          if (Number.isNaN(d.getTime())) return iso;
          return d.toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
            year: "numeric",
          });
        };
        // Group rows by city so MATCH ALL renders one section per city.
        const byCity = new Map<string, MatchPreviewRow[]>();
        for (const r of matcherPreview.rows) {
          if (!byCity.has(r.city)) byCity.set(r.city, []);
          byCity.get(r.city)!.push(r);
        }
        return (
          <div className="flex-none border-b border-white/10 bg-[#0A0A0C]">
            {/* Header bar — title + bulk actions + dismiss */}
            <div className="px-8 py-3 border-b border-white/10 flex items-center gap-3 flex-wrap">
              <span className="text-[11px] uppercase tracking-[0.18em] text-white/55">
                Match Preview
              </span>
              <span className="text-[12px] text-white/80 tabular-nums">
                {matcherPreview.cities.length === 1
                  ? matcherPreview.cities[0]
                  : `${matcherPreview.cities.length} cities`}
                {" · "}
                {total} meeting{total === 1 ? "" : "s"} · candidates: high={high} med={med} review={review}
                {withCand < total ? ` · no-match=${total - withCand}` : ""}
              </span>
              <span className="text-[10px] text-white/40 uppercase tracking-widest ml-1">
                Nothing committed yet — tick rows + [PROMOTE]
              </span>

              <div className="flex-1" />

              <button
                onClick={() => setAllApprovals("high")}
                className="text-[11px] uppercase tracking-widest text-white/70 hover:text-white border border-white/15 hover:border-white/35 px-3 py-1"
                title="Tick every high-confidence row (default)"
              >
                Tick high
              </button>
              <button
                onClick={() => setAllApprovals("all")}
                className="text-[11px] uppercase tracking-widest text-white/70 hover:text-white border border-white/15 hover:border-white/35 px-3 py-1"
                title="Tick every row that has any candidate (high + med + review)"
              >
                Tick all
              </button>
              <button
                onClick={() => setAllApprovals("none")}
                className="text-[11px] uppercase tracking-widest text-white/70 hover:text-white border border-white/15 hover:border-white/35 px-3 py-1"
                title="Untick everything"
              >
                Clear
              </button>

              <button
                onClick={promoteSelected}
                disabled={promoteBusy || approvedCount === 0}
                className="text-[12px] uppercase tracking-widest text-[#22c55e] hover:text-white border border-[#22c55e]/45 hover:border-[#22c55e] hover:bg-[#22c55e]/15 px-3 py-1 disabled:opacity-30 disabled:cursor-not-allowed"
                title={
                  approvedCount === 0
                    ? "Tick at least one row first"
                    : `Commit ${approvedCount} approved match(es) via apply_match(). High-confidence rows flip awaiting_video → pending. Zero LLM spend.`
                }
              >
                {promoteBusy
                  ? "PROMOTING…"
                  : `PROMOTE SELECTED · ${approvedCount}`}
              </button>

              <button
                onClick={dismissMatchPreview}
                disabled={promoteBusy}
                className="text-[11px] uppercase tracking-widest text-white/55 hover:text-white border border-white/15 hover:border-white/35 px-3 py-1 disabled:opacity-30"
                title="Discard the preview without promoting anything"
              >
                Dismiss
              </button>
            </div>

            {/* Per-city sections */}
            <div className="max-h-[60vh] overflow-y-auto">
              {Array.from(byCity.entries()).map(([city, rows]) => (
                <div key={city} className="border-b border-white/5 last:border-b-0">
                  {byCity.size > 1 && (
                    <div className="px-8 py-1.5 bg-[#08080A] text-[10px] uppercase tracking-[0.18em] text-white/45">
                      {city} · {rows.length} meeting{rows.length === 1 ? "" : "s"}
                    </div>
                  )}
                  {rows.map((r: MatchPreviewRow) => {
                    const cand = r.candidate;
                    const approved = matcherPreview.approvals.has(r.meeting_id);
                    return (
                      <div
                        key={`${r.city}-${r.meeting_id}`}
                        className={`px-8 py-2 grid grid-cols-[24px_minmax(0,1.5fr)_minmax(0,2fr)_120px] gap-3 items-start border-b border-white/5 last:border-b-0 ${
                          approved ? "bg-[#22c55e]/[0.04]" : ""
                        } ${!cand ? "opacity-50" : ""}`}
                      >
                        <input
                          type="checkbox"
                          checked={approved}
                          disabled={!cand || promoteBusy}
                          onChange={() => toggleMatchApproval(r.meeting_id)}
                          className="mt-1 accent-[#22c55e] disabled:cursor-not-allowed"
                          title={
                            !cand
                              ? "No candidate to promote"
                              : approved
                              ? "Untick to exclude from PROMOTE"
                              : "Tick to include in PROMOTE"
                          }
                        />
                        <div className="min-w-0">
                          <div className="text-[12px] text-white/85 truncate">
                            {r.meeting_title || "(untitled meeting)"}
                          </div>
                          <div className="text-[11px] text-white/40 tabular-nums">
                            {fmtDate(r.meeting_date)} · WO #{r.meeting_id}
                            {r.had_existing_url && (
                              <span className="ml-2 text-white/35">· already has video_url</span>
                            )}
                          </div>
                        </div>
                        <div className="min-w-0">
                          {cand ? (
                            <>
                              <div className="text-[12px] text-white/80 truncate">
                                <a
                                  href={cand.video_url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="hover:underline"
                                  title={cand.video_url}
                                >
                                  {cand.video_title || cand.video_url}
                                </a>
                              </div>
                              <div className="text-[11px] text-white/45">
                                uploaded {fmtDate(cand.video_upload_date)} ·{" "}
                                <span className="italic text-white/55">{cand.reasoning}</span>
                              </div>
                            </>
                          ) : (
                            <div className="text-[11px] text-white/40 italic">
                              No candidate video — Haiku returned "none"
                            </div>
                          )}
                        </div>
                        <div className="text-right">
                          {cand && (
                            <span
                              className="inline-block text-[10px] uppercase tracking-widest px-2 py-0.5 border"
                              style={{
                                color: confColor(cand.confidence),
                                borderColor: `${confColor(cand.confidence)}66`,
                              }}
                              title={
                                cand.confidence === "high"
                                  ? "Same body + date matches video uploaded 0-2 days after + clear title correspondence"
                                  : cand.confidence === "medium"
                                  ? "Probable match with one ambiguity (abbreviation or 3-5 day delay)"
                                  : "Best of poor options — operator should confirm before processing"
                              }
                            >
                              {cand.confidence}
                            </span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          </div>
        );
      })()}

      {/* ── Filter row ─────────────────────────────────────────── */}
      <div className="flex-none px-8 py-2.5 border-b border-white/10 bg-[#08080A] flex items-center gap-2 flex-wrap">
        {/* City filter chip (Opus 2026-06-04 — relocated from action band) */}
        <span className="text-[10px] uppercase tracking-[0.18em] text-white/35 mr-1">
          City
        </span>
        <div className="flex items-stretch border border-white/15 hover:border-white/30 transition-colors">
          <select
            value={cityFilter}
            onChange={(e) => setCityFilter(e.target.value)}
            className="bg-[#08080A] text-white text-[12px] uppercase tracking-widest px-2.5 py-1 focus:outline-none font-mono"
            title="Filter the meeting list to a single city; also scopes the [MATCH ▸ this city] button"
          >
            <option value="all">All cities</option>
            {channelCities.map((c) => (
              <option key={c.name} value={c.name}>
                {c.name}
              </option>
            ))}
          </select>
        </div>

        <span className="mx-2 h-4 w-px bg-white/10" aria-hidden />

        <span className="text-[10px] uppercase tracking-[0.18em] text-white/35 mr-1">
          State
        </span>
        {STATES_FOR_FILTER.map((s) => (
          <button
            key={s}
            onClick={() => setFilter(s)}
            title={STATE_TOOLTIP[s]}
            className={`text-[12px] uppercase tracking-widest px-2.5 py-1 transition-colors ${
              filter === s
                ? "bg-white/10 text-white"
                : "text-gray-500 hover:text-white"
            }`}
          >
            {STATE_LABEL[s] || s}
          </button>
        ))}
        {/* Worker idle / in-flight indicator moved up into the QUEUE WORK
           eyebrow — the filter row stays focused on filter chips so the
           operator's eye doesn't have to triage altitude when reading. */}
      </div>

      {/* ── Body: 2-column (table + log) ───────────────────────── */}
      <div className="flex flex-1 min-h-0">

        {/* Table */}
        <div className="flex-1 overflow-auto min-w-0">
          {loading && (
            <div className="p-6 text-gray-500 text-[11px] uppercase tracking-widest">
              Loading queue…
            </div>
          )}
          {error && (
            <div className="p-6 text-red-400 text-[11px] uppercase tracking-widest">
              ERROR: {error}
            </div>
          )}

          {!loading && !error && (
            <div className="px-6 py-5">
              {/* Header row — leftmost cell is the "select all visible" checkbox. */}
              <div
                className="grid items-center gap-4 px-5 pb-3 text-[11px] uppercase tracking-widest text-gray-600 border-b border-white/10 mb-2"
                style={{ gridTemplateColumns: ROW_GRID }}
              >
                <span className="flex items-center justify-center">
                  <input
                    type="checkbox"
                    title="Select all visible WOs"
                    checked={
                      filtered.length > 0 &&
                      filtered.every((o) => selectedIds.has(o.id))
                    }
                    ref={(el) => {
                      // Indeterminate state when some-but-not-all visible are selected.
                      if (el) {
                        const visibleSelected = filtered.filter((o) =>
                          selectedIds.has(o.id)
                        ).length;
                        el.indeterminate =
                          visibleSelected > 0 && visibleSelected < filtered.length;
                      }
                    }}
                    onChange={(e) => {
                      setSelectedIds((prev) => {
                        const next = new Set(prev);
                        if (e.target.checked) {
                          for (const o of filtered) next.add(o.id);
                        } else {
                          for (const o of filtered) next.delete(o.id);
                        }
                        return next;
                      });
                    }}
                    className="cursor-pointer"
                  />
                </span>
                <span>Meeting</span>
                <span>Status</span>
                <span>Actions</span>
              </div>

              {filtered.length === 0 && (
                <div className="p-6 text-gray-600 text-[12px] uppercase tracking-widest">
                  No work orders match this filter.
                </div>
              )}

              <div className="space-y-2">
                {filtered.map(wo => {
                  const isLockedByOther = processingId !== null && processingId !== wo.id;
                  const isThisProcessing = processingId === wo.id || wo.state === "processing";
                  const isDone = wo.state === "completed";
                  const isFailed = wo.state === "failed";
                  // D-039 mode awareness: when a worker is in flight on
                  // another WO, conflicting action buttons render in a
                  // visibly-disabled state (greyed + tooltip) rather than
                  // vanishing. Operator's mental model: "I CAN retry this
                  // WO, just not while another one is running." Lock
                  // tooltip surfaces which WO is the in-flight one.
                  const lockedTooltip = isLockedByOther
                    ? `Currently processing meeting #${processingId} — wait for it to finish or use [CANCEL QUEUE]`
                    : undefined;
                  const mayProcess = wo.state === "pending";
                  const canProcess = mayProcess && !isLockedByOther;
                  // Position in the batch queue (1-based). 0 = not queued.
                  // First item never appears here — that one is `processingId`
                  // and renders with the IN-FLIGHT amber styling instead. Only
                  // positions 2..N (the actually-cancelable ones) show the
                  // queued pill.
                  const queuedPosition = batchQueue.indexOf(wo.id);
                  const isQueued = queuedPosition >= 0;
                  const mayRetry = wo.state === "failed" || wo.state === "awaiting_notebook";
                  const canRetry = mayRetry && !isLockedByOther;
                  // canSetUrl removed per D-138 (2026-06-25). Manual video-URL
                  // paste struck; haiku_match_videos.py is the autonomous path.

                  // Per-row laser-border treatment.
                  //   DONE   → green
                  //   FAIL   → red
                  //   QUEUED → cyan (subtler than in-flight amber — this WO
                  //             is "yours" but hasn't started yet, and the
                  //             operator can still unqueue it).
                  const rowStyle: React.CSSProperties = isDone
                    ? {
                        borderColor: "rgba(34,197,94,0.55)",
                        boxShadow: "0 0 0 1px rgba(34,197,94,0.25), 0 0 16px rgba(34,197,94,0.15)",
                      }
                    : isFailed
                      ? {
                          borderColor: "rgba(239,68,68,0.55)",
                          boxShadow: "0 0 0 1px rgba(239,68,68,0.25), 0 0 16px rgba(239,68,68,0.12)",
                        }
                      : isQueued
                        ? {
                            borderColor: "rgba(59,130,246,0.40)",
                            boxShadow: "0 0 0 1px rgba(59,130,246,0.15)",
                          }
                        : {};

                  return (
                    <div
                      key={wo.id}
                      style={{ ...rowStyle, gridTemplateColumns: ROW_GRID }}
                      className={`grid items-start gap-4 px-5 py-3.5 rounded-md border transition-colors ${
                        isThisProcessing
                          ? "bg-[#1A1408] border-[#F5A524]/40"
                          : isDone || isFailed
                            ? "bg-transparent"
                            : "bg-transparent border-white/5 hover:bg-white/[0.02] hover:border-white/10"
                      }`}
                    >
                      <span className="flex items-center justify-center pt-0.5">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(wo.id)}
                          onChange={(e) => {
                            setSelectedIds((prev) => {
                              const next = new Set(prev);
                              if (e.target.checked) next.add(wo.id);
                              else next.delete(wo.id);
                              return next;
                            });
                          }}
                          className="cursor-pointer"
                          title={
                            wo.state === "pending"
                              ? "Select for batch process"
                              : `Selecting WOs in state '${wo.state}' is allowed but [PROCESS SELECTED] will skip non-pending rows`
                          }
                        />
                      </span>
                      {/* Session-29 (2026-07-03): title + date section is
                         click-to-open-broadcast when the WO has a meeting_id.
                         Retires the standalone [VIEW] button; the row's title
                         area IS the affordance.
                         Session-30 (2026-07-04): click now opens the broadcast
                         in an in-page iframe peek modal instead of navigating
                         away, so the operator can quick-check + close (X) +
                         hit [Make Public →] without losing terminal context.
                         Cursor + hover feedback tell the user the region is
                         clickable without training. */}
                      <div
                        className={`min-w-0 ${
                          wo.meeting_id
                            ? "cursor-pointer group"
                            : ""
                        }`}
                        onClick={
                          wo.meeting_id
                            ? () => setPeekMeetingId(wo.meeting_id!)
                            : undefined
                        }
                        role={wo.meeting_id ? "button" : undefined}
                        title={
                          wo.meeting_id
                            ? "Peek at this meeting's broadcast (X or Esc to close)"
                            : wo.meeting_title || ""
                        }
                      >
                        <div
                          className={`text-white truncate text-[13px] ${
                            wo.meeting_id && onNavigate
                              ? "group-hover:text-[#3B82F6] group-hover:underline decoration-dotted underline-offset-2 transition-colors"
                              : ""
                          }`}
                        >
                          {meetingTypeFromTitle(wo.meeting_title)}
                        </div>
                        <div className="text-gray-500 text-[12px] mt-0.5">
                          {fmtDate(wo.meeting_date)}
                        </div>
                      </div>
                      {(() => {
                        const s = humanizeWorkOrderStatus(wo);
                        return (
                          <span
                            className="flex items-center gap-2 text-[12px] pt-0.5 min-w-0"
                            style={{ color: s.color }}
                            title={`updated ${fmtDateTime(wo.updated_at)}`}
                          >
                            <span
                              className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                                s.pulse ? "animate-pulse" : ""
                              }`}
                              style={{ backgroundColor: s.color }}
                            />
                            <span className="truncate">{s.label}</span>
                          </span>
                        );
                      })()}
                      <div>
                        <div className="flex items-center gap-1.5 flex-wrap">
                          {/* Queued pill (positions 2..N in the batch). Click
                             the X to remove this WO from the queue and untick
                             its selection. The currently-in-flight WO does
                             NOT show this — it's already past the point of
                             cancellation; the IN-FLIGHT amber row treatment
                             above tells the operator that one is committed. */}
                          {isQueued && !isThisProcessing && (
                            <span
                              className="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-widest px-2 py-1 border border-[#3B82F6]/45 text-[#3B82F6] bg-[#3B82F6]/[0.06]"
                              title={`Queued at position ${queuedPosition + 2} in the current batch. Click × to unqueue.`}
                            >
                              <span className="tabular-nums">
                                queued · {queuedPosition + 2}
                              </span>
                              <button
                                onClick={() => cancelQueued(wo.id)}
                                className="hover:text-white transition-colors -mr-1 ml-0.5 text-[14px] leading-none"
                                title="Unqueue this WO from the batch (does not affect already-processing WO)"
                                aria-label={`Unqueue WO #${wo.id}`}
                              >
                                ×
                              </button>
                            </span>
                          )}
                          {wo.meeting_id &&
                            (() => {
                              const summary =
                                auditSummaries[String(wo.meeting_id)];
                              if (!summary) return null;
                              const flagCount =
                                summary.findings_count +
                                summary.deterministic_flags_count;
                              const badge =
                                summary.verdict === "no_catches"
                                  ? {
                                      label: "audit · clean",
                                      classes:
                                        "border-[#22C55E]/35 text-[#22C55E] bg-[#22C55E]/[0.06]",
                                    }
                                  : summary.verdict === "flags"
                                    ? {
                                        label: `audit · ${flagCount} flags`,
                                        classes:
                                          "border-[#F5A524]/45 text-[#F5A524] bg-[#F5A524]/[0.06]",
                                      }
                                    : summary.verdict === "incomplete"
                                      ? {
                                          label: "audit · incomplete",
                                          classes:
                                            "border-white/15 text-gray-400 bg-white/[0.03]",
                                        }
                                      : null;
                              if (!badge) return null;
                              return (
                                <button
                                  type="button"
                                  onClick={() =>
                                    setAuditCardMeetingId(wo.meeting_id)
                                  }
                                  className={`inline-flex items-center text-[10px] uppercase tracking-widest px-2 py-1 border hover:text-white transition-colors ${badge.classes}`}
                                  title="Open the episode audit review"
                                >
                                  {badge.label}
                                </button>
                              );
                            })()}
                          {/* Per D-138 (2026-06-25): manual SET URL form
                             struck. haiku_match_videos.py is autonomous floor;
                             [CONFIRM URL] below stays for medium-confidence
                             match confirmation (that's autonomous-derived, not
                             manual paste). Fragment kept to preserve action
                             button grouping inside the parent <div>. */}
                          {(
                            <>
                              {/* T-004 confidence pill — visible ONLY while
                                 the WO is awaiting_video (Session-29 2026-07-03:
                                 confidence matters pre-Done to inform the
                                 [CONFIRM URL] auto-promotion decision; on Done
                                 it's historical noise per D-054). Color: green
                                 for high (auto-promoted), blue for medium
                                 (needs confirm), amber for needs_review. */}
                              {wo.state === "awaiting_video" && wo.video_url_match_confidence && (
                                <span
                                  className="text-[10px] uppercase tracking-widest px-2 py-1 border"
                                  style={{
                                    borderColor:
                                      wo.video_url_match_confidence === "high"
                                        ? "rgba(34,197,94,0.45)"
                                        : wo.video_url_match_confidence === "medium"
                                          ? "rgba(59,130,246,0.45)"
                                          : "rgba(245,165,36,0.45)",
                                    color:
                                      wo.video_url_match_confidence === "high"
                                        ? "#22C55E"
                                        : wo.video_url_match_confidence === "medium"
                                          ? "#3B82F6"
                                          : "#F5A524",
                                  }}
                                  title={`T-004 match: ${wo.video_url_match_method || ""}`}
                                >
                                  match · {wo.video_url_match_confidence}
                                </span>
                              )}

                              {/* T-004 [CONFIRM URL] — only for awaiting_video
                                 WOs with a medium/needs_review match (high
                                 already auto-promoted). One click pulls the
                                 matched URL from meetings → WO and flips
                                 state to pending. */}
                              {wo.state === "awaiting_video" &&
                                wo.video_url_match_confidence &&
                                wo.video_url_match_confidence !== "high" &&
                                !wo.youtube_video_url && (
                                  <button
                                    onClick={async () => {
                                      log("system", `CONFIRM-MATCH spawned`, wo.id);
                                      try {
                                        const r = await fetch(
                                          `/api/work-orders/${wo.id}/confirm-match`,
                                          { method: "POST", headers: { "Content-Type": "application/json" } }
                                        );
                                        const body = await r.json().catch(() => null);
                                        if (r.ok && body?.success) {
                                          log(
                                            "ok",
                                            `CONFIRM-MATCH ok → state=pending`,
                                            wo.id
                                          );
                                          // Optimistic: update local row so the
                                          // [PROCESS] button appears immediately
                                          // without waiting for the next poll.
                                          setOrders((prev) =>
                                            prev.map((o) =>
                                              o.id === wo.id
                                                ? {
                                                    ...o,
                                                    state: "pending",
                                                    youtube_video_url:
                                                      body.youtube_video_url ?? null,
                                                  }
                                                : o
                                            )
                                          );
                                        } else {
                                          log(
                                            "error",
                                            `CONFIRM-MATCH failed · ${body?.error || `http ${r.status}`}`,
                                            wo.id
                                          );
                                        }
                                      } catch (e: any) {
                                        log("error", `CONFIRM-MATCH error · ${e?.message || e}`, wo.id);
                                      }
                                    }}
                                    className="text-[11px] uppercase tracking-widest text-black bg-[#3B82F6] hover:bg-[#5B9DF8] px-2.5 py-1 font-bold"
                                    title="Accept the T-004 matched URL and move WO to pending"
                                  >
                                    Confirm URL
                                  </button>
                                )}

                              {/* [SET URL] / [URL ✓] button REMOVED per D-138.
                                 Video URLs are assigned autonomously by
                                 haiku_match_videos.py + parser-native capture
                                 + S-037 V0 transcribe_non_youtube. Confidence
                                 pill above + [CONFIRM URL] above are the only
                                 remaining URL-related operator surfaces — both
                                 are autonomous-derived confirmation, not
                                 manual paste. */}
                              {mayProcess && (
                                <button
                                  onClick={() => processOne(wo.id)}
                                  disabled={!canProcess}
                                  className={`text-[11px] uppercase tracking-widest px-2.5 py-1 font-bold ${
                                    canProcess
                                      ? "text-black bg-[#F5A524] hover:bg-[#FFB938]"
                                      : "text-gray-500 bg-[#F5A524]/15 cursor-not-allowed"
                                  }`}
                                  title={lockedTooltip}
                                >
                                  [PROCESS]
                                </button>
                              )}
                              {mayRetry && (
                                <button
                                  onClick={() => retryOne(wo.id)}
                                  disabled={!canRetry}
                                  className={`text-[11px] uppercase tracking-widest border px-2.5 py-1 ${
                                    canRetry
                                      ? "text-gray-300 hover:text-white border-white/10 hover:border-white/30"
                                      : "text-gray-600 border-white/5 cursor-not-allowed"
                                  }`}
                                  title={
                                    lockedTooltip ??
                                    "Reset to pending and start processing immediately. D-033 keeps already-completed outputs (DB + NotebookLM-side artifacts)."
                                  }
                                >
                                  Retry
                                </button>
                              )}
                              {/* [BURN] removed per D-143 (NotebookLM removal
                                 2026-07-01). Recoverable failures now use
                                 [RETRY]; unrecoverable ones land as `failed`
                                 for operator review. */}
                              {/* [BUILD] — T-013 V4: kick off build_review_queue
                                 in the background. Synchronous (up to ~12 min
                                 on cold source.mp4 cache; <5 sec when cached).
                                 Surfaces stdout tail in the activity log.
                                 Chunk 11 (2026-05-26): gated to isDone — building
                                 a review queue pre-done has no quotes to verify.
                                 Session-29 (2026-07-03): inline render hidden
                                 behind `false &&` — this action now lives in
                                 the [⋯] overflow menu below to reduce visual
                                 noise on Done rows. Endpoint + handler code
                                 preserved for the eventual strip pass. */}
                              {false && isDone && wo.meeting_id && (() => {
                                const inflight = inflightActions.has(inflightKey(wo.id, "build"));
                                return (
                                  <button
                                    disabled={inflight}
                                    onClick={async () => {
                                      beginInflight(wo.id, "build");
                                      log("system", `BUILD queue start`, wo.id);
                                      try {
                                        const r = await fetch(
                                          `/api/work-orders/${wo.id}/build-review-queue`,
                                          { method: "POST", headers: { "Content-Type": "application/json" } }
                                        );
                                        const body = await r.json().catch(() => null);
                                        if (r.ok && body?.ok) {
                                          const tail = (body.stdout_tail || "").split("\n").filter((l: string) => l.trim()).slice(-3).join(" · ");
                                          log("ok", `BUILD ok · ${tail || "queue ready"}`, wo.id);
                                          fetchAll();  // refresh CACHE badge after source.mp4 lands
                                        } else {
                                          log(
                                            "error",
                                            `BUILD failed · exit=${body?.exit_code} · ${body?.stderr_tail?.split("\n").slice(-1)[0] || `http ${r.status}`}`,
                                            wo.id
                                          );
                                        }
                                      } catch (e: any) {
                                        log("error", `BUILD error · ${e?.message || e}`, wo.id);
                                      } finally {
                                        endInflight(wo.id, "build");
                                      }
                                    }}
                                    className="text-[11px] uppercase tracking-widest text-gray-400 hover:text-white border border-white/10 hover:border-white/30 px-2.5 py-1 disabled:opacity-40 disabled:cursor-wait"
                                    title="Build the review queue for this meeting — extracts clips for every aligned member_quote into media/review_queue/<city>/<date>/. Synchronous; cold runs take 5-10 min for the source mp4 download."
                                  >
                                    {inflight ? "[BUILDING…]" : "[BUILD]"}
                                  </button>
                                );
                              })()}
                              {/* [INGEST] — T-013 V3 round-trip: parse every
                                 RESPONSE.md James pasted Gemini's reply into,
                                 apply mechanical substitutions, set verified_
                                 status, populate the city_vocabulary_corrections
                                 dictionary, re-align text-changed quotes.
                                 Chunk 11: gated to isDone — no responses to ingest
                                 before the meeting is processed.
                                 D-090 follow-up: two-tap confirm gate. INGEST
                                 mutates DB content-correctness state (quote
                                 text, verified status, vocab dict) and is hard
                                 to revert; misclick deserves friction.
                                 Session-29 (2026-07-03): inline render hidden
                                 behind `false &&` — moved to the [⋯] overflow
                                 menu below. Code preserved. */}
                              {false && isDone && wo.meeting_id && (() => {
                                const inflight = inflightActions.has(inflightKey(wo.id, "ingest"));
                                const ingestKey = `ingest:${wo.id}`;
                                const armed = armedConfirm.isArmed(ingestKey);
                                const fireIngest = async () => {
                                  beginInflight(wo.id, "ingest");
                                  log("system", `INGEST responses start`, wo.id);
                                  try {
                                    const r = await fetch(
                                      `/api/work-orders/${wo.id}/ingest-responses`,
                                      { method: "POST", headers: { "Content-Type": "application/json" } }
                                    );
                                    const body = await r.json().catch(() => null);
                                    if (r.ok && body?.ok) {
                                      const tail = (body.stdout_tail || "").split("\n").filter((l: string) => l.trim()).slice(-6).join(" · ");
                                      log("ok", `INGEST ok · ${tail || "no responses"}`, wo.id);
                                      fetchAll();  // disputed/vocab counts may have shifted
                                    } else {
                                      log(
                                        "error",
                                        `INGEST failed · exit=${body?.exit_code} · ${body?.stderr_tail?.split("\n").slice(-1)[0] || `http ${r.status}`}`,
                                        wo.id
                                      );
                                    }
                                  } catch (e: any) {
                                    log("error", `INGEST error · ${e?.message || e}`, wo.id);
                                  } finally {
                                    endInflight(wo.id, "ingest");
                                  }
                                };
                                return (
                                  <button
                                    disabled={inflight}
                                    onClick={() => armedConfirm.handleClick(ingestKey, fireIngest)}
                                    className={
                                      inflight
                                        ? "text-[11px] uppercase tracking-widest text-gray-400 border border-white/10 px-2.5 py-1 opacity-40 cursor-wait"
                                        : armed
                                          ? "text-[11px] uppercase tracking-widest text-[#F5A524] border border-[#F5A524]/80 bg-[#F5A524]/10 px-2.5 py-1 animate-pulse"
                                          : "text-[11px] uppercase tracking-widest text-gray-400 hover:text-white border border-white/10 hover:border-white/30 px-2.5 py-1"
                                    }
                                    title={
                                      armed
                                        ? "Tap again within 3s to apply Gemini's RESPONSE.md verdicts to this meeting's quotes + vocab. Or wait — auto-disarms."
                                        : "Ingest Gemini Pro RESPONSE.md files for this meeting (T-013 V3). Applies verdicts + text corrections, populates the vocab dictionary. Two-tap gate."
                                    }
                                  >
                                    {inflight
                                      ? "[INGESTING…]"
                                      : armed
                                        ? "[CONFIRM INGEST]"
                                        : "[INGEST]"}
                                  </button>
                                );
                              })()}
                              {/* [PUSH] — D-051 flagship content pump.
                                 Gathers this meeting's payload + media files
                                 and POSTs them to the cloud Railway backend
                                 via the Cloudflare Pages reverse-proxy.
                                 Idempotent: re-pushing UPSERTs cleanly.
                                 Chunk 11: gated to isDone — no outputs to push
                                 before the meeting is processed.
                                 D-090 follow-up: two-tap confirm gate via
                                 `useArmedConfirm` — accidental click on this
                                 button is a production deploy.
                                 Session-29 (2026-07-03): inline render hidden
                                 behind `false &&` — moved to the [⋯] overflow
                                 menu below (rare maintenance action; per-meeting
                                 sync is 1-in-100 vs the operator terminal's
                                 daily use). Code preserved. */}
                              {false && isDone && wo.meeting_id && (() => {
                                const inflight = inflightActions.has(inflightKey(wo.id, "push"));
                                const pushKey = `push:${wo.id}`;
                                const armed = armedConfirm.isArmed(pushKey);
                                const firePush = async () => {
                                  beginInflight(wo.id, "push");
                                  log("system", `PUSH to flagship start`, wo.id);
                                  try {
                                    const r = await fetch(
                                      `/api/work-orders/${wo.id}/push-to-flagship`,
                                      {
                                        method: "POST",
                                        headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify({ pushed_by: operatorIdentity }),
                                      }
                                    );
                                    const body = await r.json().catch(() => null);
                                    if (r.ok && body?.success) {
                                      const mb = body.media_bytes
                                        ? (body.media_bytes / (1024 * 1024)).toFixed(1)
                                        : "0.0";
                                      const elapsed = body.elapsed_seconds ?? "?";
                                      let outputsLine = "";
                                      try {
                                        const parsed = body.flagship_response
                                          ? JSON.parse(body.flagship_response)
                                          : null;
                                        // `outputs_sent` is the LOCAL packed count (renamed from
                                        // outputs_pushed 2026-07-26 — it was never a receipt);
                                        // flagship_ack is the far side's actual answer.
                                        if (parsed?.outputs_sent != null) {
                                          outputsLine = ` · ${parsed.outputs_sent} outputs sent`;
                                        }
                                      } catch { /* response not JSON, fine */ }
                                      log(
                                        "ok",
                                        `PUSH ok${outputsLine} · ${mb} MB media · ${elapsed}s`,
                                        wo.id
                                      );
                                    } else {
                                      log(
                                        "error",
                                        `PUSH failed · ${body?.error || `http ${r.status}`}`,
                                        wo.id
                                      );
                                    }
                                  } catch (e: any) {
                                    log("error", `PUSH error · ${e?.message || e}`, wo.id);
                                  } finally {
                                    endInflight(wo.id, "push");
                                  }
                                };
                                return (
                                  <button
                                    disabled={inflight}
                                    onClick={() => armedConfirm.handleClick(pushKey, firePush)}
                                    className={
                                      inflight
                                        ? "text-[11px] uppercase tracking-widest text-gray-400 border border-white/10 px-2.5 py-1 opacity-40 cursor-wait"
                                        : armed
                                          ? "text-[11px] uppercase tracking-widest text-[#F5A524] border border-[#F5A524]/80 bg-[#F5A524]/10 px-2.5 py-1 animate-pulse"
                                          : "text-[11px] uppercase tracking-widest text-gray-400 hover:text-[#3B82F6] border border-white/10 hover:border-[#3B82F6]/50 px-2.5 py-1"
                                    }
                                    title={
                                      armed
                                        ? "Tap again within 3s to push this meeting's payload + media to the flagship at zspan.org. Or wait — auto-disarms."
                                        : "Push this meeting's payload + media files to the flagship at zspan.org (D-051). Idempotent — re-pushing overwrites cleanly. Two-tap gate."
                                    }
                                  >
                                    {inflight
                                      ? "[PUSHING…]"
                                      : armed
                                        ? "[CONFIRM PUSH]"
                                        : "[PUSH]"}
                                  </button>
                                );
                              })()}
                              {/* [VIEW] — Session-29 (2026-07-03): retired.
                                 Row-click on the meeting title now opens the
                                 broadcast page (see the row-wrapper's onClick).
                                 Code preserved behind `false &&` in case the
                                 row-click affordance needs to be undone. */}
                              {false && isDone && wo.meeting_id && onNavigate && (
                                <button
                                  onClick={() => onNavigate?.("broadcast", { meetingId: wo.meeting_id })}
                                  className="text-[11px] uppercase tracking-widest text-[#22C55E] hover:text-white border border-[#22C55E]/30 hover:border-[#22C55E] px-2.5 py-1"
                                  title="Open this meeting's broadcast page"
                                >
                                  [VIEW]
                                </button>
                              )}
                              {isDone && wo.meeting_id && !wo.is_published && (
                                <button
                                  onClick={async () => {
                                    // Quotes Unification Refactor Chunk 8
                                    // (2026-05-26): open the PublishConfirmModal.
                                    // Fetch the meeting's broadcast-hero quotes
                                    // from the unified endpoint and compute
                                    // whether T-013 has signed off on all of
                                    // them — that drives the auto-check on the
                                    // "Quote authenticity" checklist item.
                                    setReviewingWo(wo);
                                    log("system", `PUBLISH overlay opened`, wo.id);
                                    setPublishHeroCount(0);
                                    setPublishVerifiedCount(0);
                                    setPublishQuoteCountsLoaded(false);
                                    if (wo.meeting_id) {
                                      try {
                                        const resp = await fetchUnifiedQuotes(
                                          wo.meeting_id,
                                        );
                                        const heroes = resp.quotes ?? [];
                                        setPublishHeroCount(heroes.length);
                                        setPublishVerifiedCount(
                                          heroes.filter(
                                            q => q.verified_status === "verified",
                                          ).length,
                                        );
                                        setPublishQuoteCountsLoaded(true);
                                      } catch {
                                        setPublishHeroCount(0);
                                        setPublishVerifiedCount(0);
                                        log(
                                          "error",
                                          "Could not load quote verification status; publishing remains locked",
                                          wo.id,
                                        );
                                      }
                                    }
                                  }}
                                  className="text-[11px] uppercase tracking-widest text-[#3B82F6] hover:text-white border border-[#3B82F6]/30 hover:border-[#3B82F6] px-2.5 py-1"
                                  title="Open the publish confirmation overlay. Reviewing the broadcast preview + ticking both checklist items flips this meeting to is_published=1 (Phase 3 publish gate)."
                                >
                                  {wo.approved_at ? "Re-publish →" : "Make Public →"}
                                </button>
                              )}
                              {/* "Published" now lives in the Status column (D-054);
                                 the actions cell is purely actionable affordances. */}
                              {/* Chunk 11 (2026-05-26): overflow menu for
                                 rarely-used actions. [CLIPS] opens the meeting's
                                 local review_queue folder; [CLEAR] deletes the
                                 cached source.mp4. Pre-Chunk-11 both rendered
                                 inline on every row that had a meeting_id —
                                 which was every row, producing a 7+ button
                                 spillover for done WOs. Now gated to done AND
                                 collapsed by default behind [⋯]. */}
                              {isDone && wo.meeting_id && (() => {
                                const isOpen = openOverflowWoId === wo.id;
                                const clipsInflight = inflightActions.has(inflightKey(wo.id, "clips"));
                                const clearInflight = inflightActions.has(inflightKey(wo.id, "clear"));
                                return (
                                  <div className="relative">
                                    <button
                                      onClick={() => setOpenOverflowWoId(isOpen ? null : wo.id)}
                                      className="text-[11px] uppercase tracking-widest text-gray-400 hover:text-white border border-white/10 hover:border-white/30 px-2 py-1"
                                      title="More actions for this meeting"
                                      aria-expanded={isOpen}
                                    >
                                      ⋯
                                    </button>
                                    {isOpen && (
                                      <>
                                        {/* Click-outside-to-close backdrop. */}
                                        <div
                                          className="fixed inset-0 z-10"
                                          onClick={() => setOpenOverflowWoId(null)}
                                          aria-hidden="true"
                                        />
                                        <div className="absolute right-0 top-full mt-1 z-20 bg-[#0A0A0A] border border-white/20 flex flex-col gap-1 p-1.5 min-w-[200px] shadow-lg">
                                          {/* Session-29 (2026-07-03): BUILD /
                                             INGEST / PUSH inline renders were
                                             hidden as visual clutter. Preserving
                                             them here in menu form so the
                                             actions stay reachable pre-strip
                                             (strip pass indefinitely postponed
                                             per operator direction). BUILD is
                                             synchronous 5-10 min so no confirm
                                             gate needed; INGEST + PUSH mutate
                                             DB/production state so they use a
                                             native window.confirm() dialog to
                                             replicate the two-tap safety of
                                             the retired inline versions. */}
                                          {(() => {
                                            const buildInflight = inflightActions.has(inflightKey(wo.id, "build"));
                                            return (
                                              <button
                                                disabled={buildInflight}
                                                onClick={async () => {
                                                  setOpenOverflowWoId(null);
                                                  beginInflight(wo.id, "build");
                                                  log("system", "BUILD queue start", wo.id);
                                                  try {
                                                    const r = await fetch(
                                                      `/api/work-orders/${wo.id}/build-review-queue`,
                                                      { method: "POST", headers: { "Content-Type": "application/json" } }
                                                    );
                                                    const body = await r.json().catch(() => null);
                                                    if (r.ok && body?.ok) {
                                                      const tail = (body.stdout_tail || "").split("\n").filter((l: string) => l.trim()).slice(-3).join(" · ");
                                                      log("ok", `BUILD ok · ${tail || "queue ready"}`, wo.id);
                                                      fetchAll();
                                                    } else {
                                                      log("error", `BUILD failed · exit=${body?.exit_code} · ${body?.stderr_tail?.split("\n").slice(-1)[0] || `http ${r.status}`}`, wo.id);
                                                    }
                                                  } catch (e: any) {
                                                    log("error", `BUILD error · ${e?.message || e}`, wo.id);
                                                  } finally {
                                                    endInflight(wo.id, "build");
                                                  }
                                                }}
                                                className="text-[11px] uppercase tracking-widest text-left text-gray-300 hover:text-white hover:bg-white/5 px-2 py-1.5 disabled:opacity-40 disabled:cursor-wait"
                                                title="Build the review queue for this meeting — extracts clips for every aligned member_quote into media/review_queue/. Synchronous; cold runs take 5-10 min for source mp4 download."
                                              >
                                                {buildInflight ? "Building clips…" : "Build clip queue"}
                                              </button>
                                            );
                                          })()}
                                          {(() => {
                                            const ingestInflight = inflightActions.has(inflightKey(wo.id, "ingest"));
                                            return (
                                              <button
                                                disabled={ingestInflight}
                                                onClick={async () => {
                                                  setOpenOverflowWoId(null);
                                                  const ok = window.confirm(
                                                    `Apply Gemini RESPONSE.md verdicts to meeting #${wo.meeting_id}?\n\nThis mutates DB content (quote text + verified status + vocab dictionary) and is hard to revert.`
                                                  );
                                                  if (!ok) return;
                                                  beginInflight(wo.id, "ingest");
                                                  log("system", "INGEST responses start", wo.id);
                                                  try {
                                                    const r = await fetch(
                                                      `/api/work-orders/${wo.id}/ingest-responses`,
                                                      { method: "POST", headers: { "Content-Type": "application/json" } }
                                                    );
                                                    const body = await r.json().catch(() => null);
                                                    if (r.ok && body?.ok) {
                                                      const tail = (body.stdout_tail || "").split("\n").filter((l: string) => l.trim()).slice(-6).join(" · ");
                                                      log("ok", `INGEST ok · ${tail || "no responses"}`, wo.id);
                                                      fetchAll();
                                                    } else {
                                                      log("error", `INGEST failed · exit=${body?.exit_code} · ${body?.stderr_tail?.split("\n").slice(-1)[0] || `http ${r.status}`}`, wo.id);
                                                    }
                                                  } catch (e: any) {
                                                    log("error", `INGEST error · ${e?.message || e}`, wo.id);
                                                  } finally {
                                                    endInflight(wo.id, "ingest");
                                                  }
                                                }}
                                                className="text-[11px] uppercase tracking-widest text-left text-gray-300 hover:text-white hover:bg-white/5 px-2 py-1.5 disabled:opacity-40 disabled:cursor-wait"
                                                title="Ingest Gemini Pro RESPONSE.md verdicts (T-013 V3). Applies text corrections + populates vocab dictionary. Confirms via browser dialog before firing."
                                              >
                                                {ingestInflight ? "Ingesting verdicts…" : "Ingest Gemini verdicts"}
                                              </button>
                                            );
                                          })()}
                                          {(() => {
                                            const pushInflight = inflightActions.has(inflightKey(wo.id, "push"));
                                            return (
                                              <button
                                                disabled={pushInflight}
                                                onClick={async () => {
                                                  setOpenOverflowWoId(null);
                                                  const ok = window.confirm(
                                                    `Push meeting #${wo.meeting_id}'s payload + media to the flagship at zspan.org?\n\nD-051 per-meeting sync — idempotent, re-pushing overwrites cleanly.`
                                                  );
                                                  if (!ok) return;
                                                  beginInflight(wo.id, "push");
                                                  log("system", "PUSH to flagship start", wo.id);
                                                  try {
                                                    const r = await fetch(
                                                      `/api/work-orders/${wo.id}/push-to-flagship`,
                                                      {
                                                        method: "POST",
                                                        headers: { "Content-Type": "application/json" },
                                                        body: JSON.stringify({ pushed_by: operatorIdentity }),
                                                      }
                                                    );
                                                    const body = await r.json().catch(() => null);
                                                    if (r.ok && body?.success) {
                                                      const mb = body.media_bytes ? (body.media_bytes / (1024 * 1024)).toFixed(1) : "0.0";
                                                      const elapsed = body.elapsed_seconds ?? "?";
                                                      let outputsLine = "";
                                                      try {
                                                        const parsed = body.flagship_response ? JSON.parse(body.flagship_response) : null;
                                                        if (parsed?.outputs_sent != null) outputsLine = ` · ${parsed.outputs_sent} outputs sent`;
                                                      } catch { /* response not JSON, fine */ }
                                                      log("ok", `PUSH ok${outputsLine} · ${mb} MB media · ${elapsed}s`, wo.id);
                                                    } else {
                                                      log("error", `PUSH failed · ${body?.error || `http ${r.status}`}`, wo.id);
                                                    }
                                                  } catch (e: any) {
                                                    log("error", `PUSH error · ${e?.message || e}`, wo.id);
                                                  } finally {
                                                    endInflight(wo.id, "push");
                                                  }
                                                }}
                                                className="text-[11px] uppercase tracking-widest text-left text-gray-300 hover:text-white hover:bg-white/5 px-2 py-1.5 disabled:opacity-40 disabled:cursor-wait"
                                                title="Push this meeting's payload + media to the flagship at zspan.org (D-051 per-meeting sync). Idempotent. Confirms via browser dialog before firing."
                                              >
                                                {pushInflight ? "Pushing to flagship…" : "Push to flagship"}
                                              </button>
                                            );
                                          })()}
                                          <div className="border-t border-white/10 my-0.5" aria-hidden="true" />
                                          <button
                                            disabled={clipsInflight}
                                            onClick={async () => {
                                              setOpenOverflowWoId(null);
                                              beginInflight(wo.id, "clips");
                                              log("system", `CLIPS open requested`, wo.id);
                                              try {
                                                const r = await fetch(
                                                  "/api/local-fs/open-review-queue",
                                                  {
                                                    method: "POST",
                                                    headers: { "Content-Type": "application/json" },
                                                    body: JSON.stringify({ meeting_id: wo.meeting_id }),
                                                  }
                                                );
                                                const body = await r.json().catch(() => null);
                                                if (r.ok && body?.ok) {
                                                  log("ok", `CLIPS opened · ${body.path}`, wo.id);
                                                } else if (r.status === 404) {
                                                  log(
                                                    "error",
                                                    `CLIPS · no review queue built yet · click [BUILD] first`,
                                                    wo.id
                                                  );
                                                } else {
                                                  log(
                                                    "error",
                                                    `CLIPS failed · ${body?.error || `http ${r.status}`}`,
                                                    wo.id
                                                  );
                                                }
                                              } catch (e: any) {
                                                log("error", `CLIPS error · ${e?.message || e}`, wo.id);
                                              } finally {
                                                endInflight(wo.id, "clips");
                                              }
                                            }}
                                            className="text-[11px] uppercase tracking-widest text-left text-gray-300 hover:text-white hover:bg-white/5 px-2 py-1.5 disabled:opacity-40 disabled:cursor-wait"
                                            title="Open this meeting's local review_queue folder in File Explorer"
                                          >
                                            {clipsInflight ? "Opening clips…" : "Open clips folder"}
                                          </button>
                                          <button
                                            disabled={clearInflight}
                                            onClick={async () => {
                                              setOpenOverflowWoId(null);
                                              beginInflight(wo.id, "clear");
                                              log("system", `CLEAR CACHE requested`, wo.id);
                                              try {
                                                const r = await fetch(
                                                  `/api/work-orders/${wo.id}/clear-source-cache`,
                                                  { method: "POST", headers: { "Content-Type": "application/json" } }
                                                );
                                                const body = await r.json().catch(() => null);
                                                if (r.ok && body?.ok) {
                                                  if (body.deleted) {
                                                    const mb = (body.bytes_freed / (1024 * 1024)).toFixed(1);
                                                    log("ok", `CACHE cleared · freed ${mb} MB`, wo.id);
                                                    fetchAll();
                                                  } else {
                                                    log("system", `CACHE · ${body.reason || "nothing to clear"}`, wo.id);
                                                  }
                                                } else {
                                                  log("error", `CACHE clear failed · ${body?.error || `http ${r.status}`}`, wo.id);
                                                }
                                              } catch (e: any) {
                                                log("error", `CACHE clear error · ${e?.message || e}`, wo.id);
                                              } finally {
                                                endInflight(wo.id, "clear");
                                              }
                                            }}
                                            className="text-[11px] uppercase tracking-widest text-left text-gray-300 hover:text-white hover:bg-white/5 px-2 py-1.5 disabled:opacity-40 disabled:cursor-wait"
                                            title="Delete this meeting's cached source.mp4 (~45 MB). Preserves clips + audit files; next [BUILD] re-downloads from YouTube."
                                          >
                                            {clearInflight ? "Clearing cache…" : "Clear source cache"}
                                          </button>
                                          <button
                                            onClick={() => {
                                              setOpenOverflowWoId(null);
                                              if (wo.notebook_id) {
                                                navigator.clipboard?.writeText(wo.notebook_id);
                                                log("ok", `notebook id copied · ${shortNb(wo.notebook_id)}`, wo.id);
                                              } else {
                                                log("system", "no notebook id on this WO yet", wo.id);
                                              }
                                            }}
                                            className="text-[11px] uppercase tracking-widest text-left text-gray-300 hover:text-white hover:bg-white/5 px-2 py-1.5"
                                            title={wo.notebook_id ? `Copy notebook ID to clipboard (debug) · ${wo.notebook_id}` : "No notebook ID yet"}
                                          >
                                            Copy notebook id
                                          </button>
                                        </div>
                                      </>
                                    )}
                                  </div>
                                );
                              })()}
                              {/* "Generating…" now lives in the Status column (D-054). */}
                            </>
                          )}
                        </div>
                        {wo.error && (
                          <div className="text-[11px] text-red-400 mt-1.5 truncate" title={wo.error}>
                            ! {wo.error}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* Right rail — Activity Log (Opus 2026-06-04: collapsed to a 24px
           tab by default; expand on click). The polling-metadata footer
           moved into the universal TopBar's health-dot popover so the
           bottom of this rail stays focused on log entries. */}
        {logOpen ? (
          <div className="w-[400px] border-l border-white/10 bg-[#06060A] flex flex-col flex-shrink-0">
            <div className="px-5 py-3 border-b border-white/10 flex items-center justify-between gap-3">
              <button
                type="button"
                onClick={() => setLogOpen(false)}
                className="text-[12px] uppercase tracking-widest text-gray-400 hover:text-white flex items-center gap-2"
                title="Collapse the activity log"
              >
                <span aria-hidden>›</span>
                Activity Log
              </button>
              <div className="flex items-center gap-3">
                <div
                  className="flex items-center gap-1.5 text-[11px] uppercase tracking-widest"
                  title="Curated shows key events only; Raw shows the full worker log stream."
                >
                  <button
                    onClick={() => setLogVerbose(false)}
                    className={logVerbose ? "text-gray-600 hover:text-white" : "text-white"}
                  >
                    curated
                  </button>
                  <span className="text-gray-700">·</span>
                  <button
                    onClick={() => setLogVerbose(true)}
                    className={logVerbose ? "text-white" : "text-gray-600 hover:text-white"}
                  >
                    raw
                  </button>
                </div>
                <button
                  onClick={() => setLogEntries([])}
                  className="text-[11px] uppercase tracking-widest text-gray-600 hover:text-white"
                  title="Clear local activity log (does not affect worker_logs/ files on disk)"
                >
                  Clear
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-auto p-4 space-y-1">
              {(() => {
                // Curated hides the raw worker stream (kind "worker") AND
                // any transport/lifecycle noise that slipped into another
                // kind — e.g. httpx auth-flow GETs whose URLs contain
                // "login"/"signin" and get mis-tagged "warn".
                const NOISE =
                  /httpx|http request|batchexecute|generatefreeform|client (?:opened|closed)/i;
                const visibleLog = logVerbose
                  ? logEntries
                  : logEntries.filter(
                      (e) => e.kind !== "worker" && !NOISE.test(e.message)
                    );
                const hidden = logEntries.length - visibleLog.length;
                if (logEntries.length === 0) {
                  return (
                    <div className="text-gray-700 text-[12px] uppercase tracking-widest">
                      — idle —
                    </div>
                  );
                }
                return (
                  <>
                    {visibleLog.length === 0 && (
                      <div className="text-gray-700 text-[12px] uppercase tracking-widest">
                        — no key events yet —
                      </div>
                    )}
                    {visibleLog.map((entry, i) => {
                      const color = KIND_COLOR[entry.kind] || KIND_COLOR.worker;
                      const text = logVerbose
                        ? entry.message
                        : curateLogMessage(entry.message);
                      const time = logVerbose ? entry.ts : entry.ts.slice(0, 5);
                      return (
                        <div
                          key={i}
                          className="text-[12px] leading-relaxed whitespace-pre-wrap break-words"
                          title={entry.message}
                        >
                          <span className="text-gray-600 mr-2">{time}</span>
                          {entry.woId !== undefined && (
                            <span className="text-gray-500 mr-2">#{entry.woId}</span>
                          )}
                          <span style={{ color }}>{text}</span>
                        </div>
                      );
                    })}
                    {!logVerbose && hidden > 0 && (
                      <button
                        onClick={() => setLogVerbose(true)}
                        className="text-[11px] uppercase tracking-widest text-gray-600 hover:text-white pt-1"
                        title="Switch to the full worker log stream"
                      >
                        + {hidden} more in raw
                      </button>
                    )}
                  </>
                );
              })()}
            </div>
          </div>
        ) : (
          /* Collapsed: 36px-wide right-edge tab with a left-pointing
             chevron, the vertical LOG glyph, and an entry-count chip.
             The 1px cyan left border separates it from the meeting list
             so it reads as an affordance, not accidental margin. */
          <button
            type="button"
            onClick={() => setLogOpen(true)}
            className="w-9 border-l border-[#22D3EE]/30 bg-[#06060A] flex flex-col items-center justify-start py-3 gap-2 hover:bg-[#0A0A12] hover:border-[#22D3EE]/55 transition-colors flex-shrink-0 group"
            title="Expand the activity log"
            aria-label="Expand the activity log"
          >
            <span
              className="text-[14px] leading-none text-white/45 group-hover:text-white/75"
              aria-hidden
            >
              ◂
            </span>
            <span className="text-[10px] uppercase tracking-[0.18em] text-white/45 group-hover:text-white/75 [writing-mode:vertical-rl] rotate-180">
              Log
            </span>
            {logEntries.length > 0 && (
              <span
                className="mt-1 text-[10px] text-white/55 tabular-nums [writing-mode:vertical-rl] rotate-180"
                title={`${logEntries.length} entries in the activity log`}
              >
                {logEntries.length}
              </span>
            )}
          </button>
        )}
      </div>

      {/* NotebookGcPanel overlay + [BURN] ConfirmDestructive modal removed
         per D-143 (NotebookLM removal 2026-07-01). */}

      <EpisodeAuditCard
        meetingId={auditCardMeetingId}
        isPublished={Boolean(orders.find(wo => wo.meeting_id === auditCardMeetingId)?.is_published)}
        onClose={() => setAuditCardMeetingId(null)}
      />

      {/* Session-30 (2026-07-04): broadcast peek modal. Fires from
         row-title click on any WO with a meeting_id. Iframe loads the
         full broadcast page — same origin, no CORS gymnastics. Operator
         eyeballs the content, closes via X or Esc, then hits
         [Make Public →] without losing terminal context. */}
      {peekMeetingId !== null && (
        <div
          className="fixed inset-0 z-[100] bg-black/90 backdrop-blur-sm flex flex-col p-6"
          role="dialog"
          aria-modal="true"
          aria-label="Broadcast peek"
          onClick={(e) => {
            // Backdrop click closes; iframe/header clicks bubble to their
            // own handlers and never reach here.
            if (e.target === e.currentTarget) setPeekMeetingId(null);
          }}
        >
          {/* Session-30 (2026-07-04): floating panel shape (margin around
             the modal) so the backdrop shows on all sides — clearly reads
             as a modal rather than a full-page render inside an iframe.
             Amber accent on the header for extra "this is a peek, not the
             real page" affordance. Broadcast inside renders full-page
             including sidebars per operator direction (2026-07-04): he
             wants to check errors on side rails too, not just the middle
             column. */}
          <div className="flex-1 flex flex-col rounded-xl border-2 border-amber-400/40 shadow-2xl overflow-hidden bg-black">
            <div className="flex items-center justify-between px-5 py-3 border-b-2 border-amber-400/30 bg-[#141416]">
              <div className="flex items-center gap-3">
                <span className="text-[11px] uppercase tracking-[0.22em] font-bold text-amber-300">
                  Broadcast Peek
                </span>
                <span className="text-[11px] uppercase tracking-widest text-white/40">
                  meeting #{peekMeetingId}
                </span>
              </div>
              <button
                onClick={() => setPeekMeetingId(null)}
                className="text-[11px] uppercase tracking-widest text-amber-200 hover:text-white border border-amber-400/40 hover:border-amber-300 bg-amber-400/[0.05] hover:bg-amber-400/15 px-3 py-1 rounded"
                title="Close (Esc)"
                aria-label="Close broadcast peek"
              >
                × Close
              </button>
            </div>
            <iframe
              src={`/?view=broadcast&meetingId=${peekMeetingId}&peek=1`}
              className="flex-1 w-full border-0 bg-black"
              title={`Broadcast preview for meeting ${peekMeetingId}`}
            />
          </div>
        </div>
      )}

      {/* Publish confirm overlay (Quotes Unification Refactor Chunk 8,
         2026-05-26). Replaces the broken ReviewGateModal (D-032) per
         D-053: the modal no longer claims to perform verification —
         T-013 V2 is the verification chain. This is the publish-moment
         checklist + forced read of the broadcast preview. */}
      <PublishConfirmModal
        open={reviewingWo !== null}
        meeting={
          reviewingWo
            ? {
                id: reviewingWo.meeting_id ?? 0,
                title: reviewingWo.meeting_title ?? "(untitled)",
                date: reviewingWo.meeting_date ?? "",
                city: reviewingWo.city_name ?? "",
              }
            : null
        }
        heroCount={publishHeroCount}
        verifiedCount={publishVerifiedCount}
        quoteCountsLoaded={publishQuoteCountsLoaded}
        auditSummary={
          reviewingWo?.meeting_id
            ? auditSummaries[String(reviewingWo.meeting_id)]
            : undefined
        }
        previewHref={
          reviewingWo?.meeting_id
            ? `/?meetingId=${reviewingWo.meeting_id}&drafts=true&preview=true`
            : undefined
        }
        onPublish={async () => {
          if (!reviewingWo) return;
          const id = reviewingWo.id;
          const meetingId = reviewingWo.meeting_id;
          try {
            const r = await fetch(`/api/work-orders/${id}/approve`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                approved_by: operatorIdentity,
                verified_quote_ids: [],
              }),
            });
            const body = await r.json().catch(() => null);
            if (r.ok && body?.success) {
              log(
                "ok",
                `PUBLISH marker recorded (T-013 verified ${publishVerifiedCount}/${publishHeroCount} hero quotes)`,
                id
              );
              // Optimistically reflect the approval marker. Public visibility
              // remains false until the publish endpoint succeeds below.
              setOrders((prev) =>
                prev.map((o) =>
                  o.id === id
                    ? {
                        ...o,
                        approved_at: body.approved_at ?? new Date().toISOString(),
                        approved_by: body.approved_by ?? operatorIdentity,
                      }
                    : o
                )
              );

              // Phase 3 — same click also flips is_published=1. The two
              // API calls (/approve + /publish) together constitute the
              // publish action; the legacy /approve endpoint stays for now
              // as the quality-approval marker while meetings.is_published
              // independently controls visibility and button-hiding.
              if (meetingId) {
                try {
                  const pr = await fetch(`/api/meetings/${meetingId}/publish`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      published_by: operatorIdentity,
                      publish_notes: `Published via PublishConfirmModal (WO #${id}; T-013 verified ${publishVerifiedCount}/${publishHeroCount} hero quotes)`,
                    }),
                  });
                  const pbody = await pr.json().catch(() => null);
                  if (pr.ok && pbody?.success) {
                    setOrders((prev) =>
                      prev.map((o) =>
                        o.id === id ? { ...o, is_published: 1 } : o
                      )
                    );
                    log(
                      "ok",
                      `PUBLISHED · broadcast is now live on the public channel browser`,
                      id
                    );
                  } else {
                    log(
                      "error",
                      `PUBLISH flip failed · ${pbody?.error || `http ${pr.status}`} (is_published not set; retry via /api/meetings/${meetingId}/publish)`,
                      id
                    );
                  }
                } catch (e: any) {
                  log(
                    "error",
                    `PUBLISH flip error · ${e?.message || e} (is_published not set; retry via /api/meetings/${meetingId}/publish)`,
                    id
                  );
                }
              }
            } else {
              log(
                "error",
                `PUBLISH marker failed · ${body?.error || `http ${r.status}`}`,
                id
              );
            }
          } catch (e: any) {
            log("error", `PUBLISH marker error · ${e?.message || e}`, id);
          }
          setReviewingWo(null);
          setPublishHeroCount(0);
          setPublishVerifiedCount(0);
          setPublishQuoteCountsLoaded(false);
        }}
        onCancel={() => {
          if (reviewingWo) {
            log("system", `PUBLISH canceled`, reviewingWo.id);
          }
          setReviewingWo(null);
          setPublishQuoteCountsLoaded(false);
          setPublishHeroCount(0);
          setPublishVerifiedCount(0);
        }}
      />
    </div>
  );
}

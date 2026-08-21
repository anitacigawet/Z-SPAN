/**
 * BroadcastPage — the "show page" for one council meeting (channel-guide
 * presentation lineage; the project is Z-SPAN — Arizona per D-185).
 *
 * This is a faithful port of the "Kingman Insight" detail page from the
 * project's private predecessor repo (<private-predecessor-repo>, src/App.tsx)
 * with three deltas James asked for:
 *
 *   1. The "VIEW FULL VIDEO" pill is removed. In its place, a tiny
 *      segmented Summary/Full toggle floats in the top-left of the video
 *      container. Switching tabs preserves playback progress on each side.
 *   2. The "● System Active · Data Synced" sidebar footer is removed.
 *   3. The bottom-spanning AI chat is moved into a full-height right
 *      column. The Key Decisions list moves from beside the video into
 *      a column under it, so the center column reads top-to-bottom:
 *      header → video → Key Decisions → Community Calls to Action.
 *      (What's Next + Council Sentiment were later cut per D-157.)
 *
 * Everything else — color tokens, font sizes, tracking, hover states,
 * sidebar selection bar, sentiment-box accent treatment, chat bubble
 * styling — comes straight from <private-predecessor-repo>. The hex values are inlined
 * (rather than translated to our `kg-*` variables) on purpose, so the
 * visual fidelity is exact.
 */
import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import ZspanPlayer, { type ZspanPlayerHandle } from "../player/ZspanPlayer";
import PromptInfoIcon, { DecisionEvidenceDisclosure } from "../components/PromptInfoIcon";
import { EXCERPT_SOURCE } from "../components/decisionEvidence";
import DefinitionHint from "@/components/DefinitionHint";
import { CommunityCallsToActionSection } from "../components/CommunityCallsToActionSection";
import { WatermarkRibbon } from "../components/WatermarkRibbon";
import {
  PublicDataDisclaimerGate,
  useDisclaimerAcked,
} from "../components/PublicDataDisclaimerGate";
import SyncedQuote, { type QuoteWordTiming } from "../components/SyncedQuote";
import { V1_PROCESSED_CITIES } from "./ChannelsPage";
import CitationPanel from "../components/CitationPanel";
import { useCurrentUser } from "../hooks/useCurrentUser";
import { getVideoSource } from "../lib/videoSource";
import { suggestedQuestionsForTitle } from "../lib/suggestedQuestions";
import { SignInBenefitsToast } from "../components/SignInBenefitsToast";
import { InfographDownloadButton } from "../components/InfographDownloadButton";
import {
  canonicalBroadcastUrl,
  prepareInfographKeyDecisions,
} from "../lib/renderInfograph";
import {
  ByokSetupModal,
  LibrarianAccessGate,
} from "../components/ByokSetupModal";
import { ByokQueryPanel } from "../components/ByokQueryPanel";
import { useSignedOutSimQueries } from "../hooks/useSignedOutSimQueries";
import { getByokConfig, LOCAL_WORKSPACE_PROVIDER, type ByokConfig } from "../lib/byok";
import {
  KaraokeText,
  KaraokeLoadingDots,
} from "../lib/karaokeRender";
import { episodeCardForTitle } from "../utils/episodeCard";
import { fetchForPlane } from "../lib/planeFetch";
import { isPublicPlane } from "../lib/trustPlane";
import {
  type EpisodeTag,
  type TagCategory,
  TAG_COLOR,
  parseEpisodeTags,
  stripCitations,
} from "../utils/episodeTags";
import {
  Building2,
  Play,
  Send,
  BrainCircuit,
  ArrowLeft,
  Briefcase,
  Film,
  Youtube,
  Loader2,
  AlertCircle,
  Headphones,
  ChevronDown,
  Info,
  Check,
  Sparkles,
  Menu,
  X,
  Lock,
  KeyRound,
} from "lucide-react";

// Showcase edition (VITE_ZSPAN_EDITION=showcase): the static GitHub-Pages
// bake gates off the V2-deferred suggestion box.
const IS_SHOWCASE = import.meta.env.VITE_ZSPAN_EDITION === "showcase";

// Format a seek-chip label from seconds — "M:SS", or "H:MM:SS" past an hour.
// Matches the Librarian's [at MM:SS] citation chip so the key-decision
// citation reads in the same visual vocabulary across the page.
function formatSeekLabel(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0
    ? `${h}:${m.toString().padStart(2, "0")}:${sec.toString().padStart(2, "0")}`
    : `${m}:${sec.toString().padStart(2, "0")}`;
}

// ── Types ──────────────────────────────────────────────────────────

interface NotebookOutput {
  content: string | null;
  content_url: string | null;
  prompt_filename: string | null;
  prompt_version: string | null;
  generated_at: string | null;
  error: string | null;
  voided_at?: string | null;
  voided_by?: string | null;
  gate_status?: string | null;
  ribbon_token?: string | null;
  registration_state?: "registered" | "pending" | null;
  karaoke_word_timings?: QuoteWordTiming[][];
}

interface BroadcastResponse {
  success: boolean;
  public_id?: string;
  local_workspace?: boolean;
  meeting_id?: number;
  meeting_title: string | null;
  meeting_date: string | null;
  city: string | null;
  county: string | null;
  notebook_id: string | null;
  // Resolved video URL — server prefers work_orders.youtube_video_url
  // (the S-037 V0 / user-paste resolved archive URL) over the parser-emitted
  // meetings.video_url. Null when no recording is yet linked.
  video_url: string | null;
  // D-001 / D-031 / D-032: approval state. Null = not yet approved through
  // the review gate; the page renders a "pending review" placeholder unless
  // the URL has ?preview=true (operator preview bypass). The approver's
  // identity is deliberately not served (2026-07-09 identity-strip).
  approved_at: string | null;
  published_at?: string | null;
  // F-7.1 (2026-07-06): server-computed completeness verdict against the
  // publishable floor (check_publish_readiness is the single source of
  // truth server-side). `reasons` is owner-only — plain-language failure
  // reasons for operator surfaces; visitors get only the counts.
  completeness?: {
    complete: boolean;
    required_ok?: number;
    required_total?: number;
    reasons?: string[];
  } | null;
  outputs: Record<string, NotebookOutput>;
}

interface SidebarMeeting {
  id?: number;
  public_id?: string;
  meeting_title?: string;
  meeting_date?: string;
  meeting_time?: string;
  meeting_location?: string;
  notebook_id?: string | null;
  video_url?: string;
  episode_tagline?: string | null;
  episode_tags?: string | null;
}

// EpisodeTag / TagCategory / TAG_COLOR / parseEpisodeTags moved to
// utils/episodeTags.ts (hoisted 2026-05-13 for sharing with ChannelsPage's
// EpisodeCard). Imported at the top of this file.

interface BroadcastPageProps {
  meetingId?: number;
  publicId?: string;
  onBack: () => void;
  onNavigate?: (view: string, params?: any) => void;
  // V1.5-OperatorSearch-1 Phase 4 — when a citation chip from
  // OperatorSearchModal navigates here, the chunk's start_seconds is
  // passed through so the video auto-seeks once it's loaded.
  initialSeek?: number;
}

interface CatalogMeetingDetail {
  public_id: string;
  city: string;
  title: string;
  date: string;
  time: string;
  location: string;
  availability: string;
  local_processing: {
    status: string;
    source_kind: string;
  };
}

const LOCAL_PROCESSING_COPY: Record<string, string> = {
  ready: "A compatible video source is available for this meeting.",
  no_video:
    "No video source was found for this meeting. You can still open it as a factual local record.",
  unsupported_source:
    "This meeting’s video format isn’t supported by the CLI yet. You can still open it as a factual local record.",
};

function CatalogMeetingPlaceholder({
  publicId,
  onBack,
}: {
  publicId: string;
  onBack: () => void;
}) {
  const [meeting, setMeeting] = useState<CatalogMeetingDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let aborted = false;
    setLoading(true);
    setError(null);
    fetch(`/v1/catalog/meetings/${encodeURIComponent(publicId)}`)
      .then(async response => {
        if (!response.ok) throw new Error("Meeting facts could not be loaded.");
        return response.json();
      })
      .then(body => {
        if (aborted) return;
        setMeeting(body as CatalogMeetingDetail);
        setLoading(false);
      })
      .catch(err => {
        if (aborted) return;
        setMeeting(null);
        setError(err instanceof Error ? err.message : "Meeting facts could not be loaded.");
        setLoading(false);
      });
    return () => {
      aborted = true;
    };
  }, [publicId]);

  const limitation = meeting
    ? LOCAL_PROCESSING_COPY[meeting.local_processing?.status] ??
      LOCAL_PROCESSING_COPY.unsupported_source
    : "";

  return (
    <div className="min-h-screen bg-[var(--canvas)] text-foreground">
      <main className="mx-auto flex min-h-screen w-full max-w-4xl flex-col justify-center px-6 py-12 sm:px-10">
        <button
          type="button"
          onClick={onBack}
          className="mb-10 inline-flex w-fit items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-foreground/45 transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          All Channels
        </button>

        {loading && (
          <div className="flex items-center gap-3 text-foreground/45">
            <Loader2 className="h-5 w-5 animate-spin" />
            <span className="text-sm">Loading meeting facts…</span>
          </div>
        )}

        {!loading && error && (
          <section className="rounded-xl border border-[var(--line)] bg-[var(--surface)]/40 p-8">
            <p className="text-sm text-foreground/65">{error}</p>
          </section>
        )}

        {!loading && meeting?.availability === "published" && (
          <section className="rounded-xl border border-[var(--line)] bg-[var(--surface)]/40 p-8">
            <p className="kg-eyebrow mb-3 text-[var(--success-green)]">On air</p>
            <h1 className="text-2xl font-semibold text-foreground">This episode is published.</h1>
            <p className="mt-3 text-sm leading-relaxed text-foreground/60">
              Return to Channels to open the published broadcast.
            </p>
          </section>
        )}

        {!loading && meeting && meeting.availability !== "published" && (
          <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(300px,0.72fr)] lg:items-start">
            <section>
              <p className="kg-eyebrow mb-3 text-foreground/45">Episode coming</p>
              <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
                {meeting.title || "Council meeting"}
              </h1>
              <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-4 border-y border-[var(--line)] py-5 text-sm">
                <div>
                  <dt className="text-[10px] uppercase tracking-widest text-foreground/35">City</dt>
                  <dd className="mt-1 text-foreground/75">{meeting.city || "Not listed"}</dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-widest text-foreground/35">Date</dt>
                  <dd className="mt-1 text-foreground/75">{meeting.date || "Not listed"}</dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-widest text-foreground/35">Time</dt>
                  <dd className="mt-1 text-foreground/75">{meeting.time || "Not listed"}</dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-widest text-foreground/35">Location</dt>
                  <dd className="mt-1 text-foreground/75">{meeting.location || "Not listed"}</dd>
                </div>
              </dl>
              <p className="mt-6 text-sm leading-relaxed text-foreground/65">
                This meeting is in the public catalog, but its Z-SPAN episode isn’t published yet.
                Community synthesis is coming through the connected Z-SPAN CLI.
              </p>
            </section>

            <section className="rounded-xl border border-[var(--line)] bg-[var(--surface)]/45 p-5 sm:p-6">
              <p className="kg-eyebrow text-foreground/45">Process this meeting</p>
              <p className="mt-3 text-[13px] leading-relaxed text-foreground/60">{limitation}</p>
              <a
                href={`zspan://meeting/${encodeURIComponent(publicId)}`}
                className="relative mt-6 flex min-h-24 w-full items-center justify-center overflow-hidden rounded-lg bg-[var(--accent)] px-6 pb-11 pt-5 text-center text-base font-semibold text-white shadow-lg transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--surface)]"
              >
                <span>Process meeting</span>
                <span className="absolute inset-x-0 bottom-0 border-t border-amber-950/50 bg-[repeating-linear-gradient(135deg,#fbbf24_0px,#fbbf24_12px,#18181b_12px,#18181b_24px)] px-3 py-2 text-[11px] font-black uppercase tracking-[0.12em] text-white shadow-[0_-2px_8px_rgba(0,0,0,0.3)]">
                  🚧 CLI coming soon 🚧
                </span>
              </a>
            </section>
          </div>
        )}
      </main>
    </div>
  );
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  pending?: boolean;
  error?: string;
}

interface SuggestedQA {
  question: string;
  answer: string | null;
  error?: string;
}

// Video player surfaces three style variants of the same meeting:
//   "corpo"   — corporate dark-mode professional summary (V1.5 — Studio
//               prompt not yet authored by James; tab renders as
//               placeholder "awaiting generation" until the
//               `video_explainer_corpo` output type lands).
//   "summary" — the existing playful "Kawaii"-style summary video.
//   "full"    — the raw original meeting recording from YouTube.
// V1-UI-1 per D-126: "corpo" retired; "summary" is the V2-locked placeholder
// (relabeled from "Kawaii"); "full" is the canonical Original tab.
type VideoTab = "summary" | "full";
type ChatMode = "direct" | "suggested";

// ── Helpers ────────────────────────────────────────────────────────

function formatDateUpper(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s + "T00:00:00") : new Date(s);
    if (isNaN(d.getTime())) return s;
    return d
      .toLocaleDateString("en-US", { month: "short", day: "2-digit", year: "numeric" })
      .toUpperCase();
  } catch {
    return s;
  }
}

function meetingTypeFromTitle(t: string | null | undefined): string {
  if (!t) return "Council Meeting";
  const dashIdx = t.indexOf(" - ");
  return dashIdx > 0 ? t.slice(0, dashIdx).trim() : t.trim();
}

// episodeCardForTitle hoisted to utils/episodeCard.ts so ChannelsPage,
// SearchPage, etc. can share the same mapping. See that file for adding
// new committee placeholders. Imported at the top of this file alongside
// CouncilQuote.

// VideoSource classifier moved to lib/videoSource.ts (2026-06-25,
// V1.5-OperatorSearch-1 Z4) so the modal-side InlineMeetingMoment
// Player can share the same logic. BroadcastPage and the modal both
// import from the same util.
// (type + function imported at top of file)

// Parse `[at MM:SS]` karaoke citations out of a Sonnet response and
// return alternating plain-text / citation segments. Used by
// renderKaraokeAnswer to make each timecode a clickable seek target.
// parseKaraokeSegments + KaraokeText + KaraokeLoadingDots lifted to
// `client/src/lib/karaokeRender.tsx` so ByokQueryPanel (and any future
// surface that renders cited RAG output) shares the same green-pill chip +
// "..." dots loader + token-format treatment. Long-meeting MM-up-to-999
// support preserved; raw cite shape `[at MM:SS]` unchanged.

function renderInlineBold(text: string): React.ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**")) {
      return (
        <strong key={i} className="text-white font-semibold">
          {p.slice(2, -2)}
        </strong>
      );
    }
    return <span key={i}>{p}</span>;
  });
}

// V1-UI-1 follow-up per D-126: legacy NotebookLM-generated text outputs
// for some meetings cached the bot's meta-response (e.g. "Welcome! It looks
// like you're interested in...", "To generate any of our polished artifacts
// we first need to add some content to your notebook...") OR an explicit
// error string ("NotebookLM query failed after 3 attempts: Chat request was
// rate limited...") instead of real content. These poison the broadcast
// page until V1-RAG-3 ships and regenerates via Qdrant + Sonnet. This
// detector catches the known meta-response and error shapes so the render
// path can fall through to the "Awaiting RAG-generated content." placeholder
// instead of leaking the legacy artifact verbatim. Conservative: only
// matches strong NotebookLM-specific signals (greetings, notebook-self-
// references, chatbot voice, explicit error strings) to avoid false-
// positives against real content. Sample size is the first 500 chars
// because the meta-response signals always appear at the top.
function looksLikeLegacyNotebookLMArtifact(text: string): boolean {
  if (!text) return false;
  const sample = text.slice(0, 500).toLowerCase();
  const patterns: RegExp[] = [
    /notebooklm query failed/,
    /chat request was (rate limited|rejected)/,
    /^\s*(welcome[!.,:]|hi[!.,:]|hello[!.,:])/,
    /(it looks like|i see) you('|')?re (interested|working)/,
    /to your notebook/,
    /source panel on the left/,
    /polished artifacts/,
    /briefing doc.*study guide/,
    /(would you like me to|i can help find them)/,
    /upload(ing)? the (meeting transcript|agenda)/,
    /first need to add/,
  ];
  return patterns.some(p => p.test(sample));
}

function parseNumberedList(text: string): string[] {
  if (!text) return [];
  const items: string[] = [];
  const lines = text.split(/\r?\n/);
  let buffer: string[] = [];
  const flush = () => {
    if (buffer.length) {
      const joined = buffer.join(" ").trim();
      if (joined) items.push(joined);
      buffer = [];
    }
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (/^\d+[.)]\s/.test(line)) {
      flush();
      buffer.push(line.replace(/^\d+[.)]\s+/, ""));
    } else if (line) {
      buffer.push(line);
    }
  }
  flush();
  return items.slice(0, 5);
}

function stripKeyDecisionCitations(text: string): string {
  return stripCitations(text).replace(
    /\s*\[at\s+(?:(?:\d+):)?\d{1,3}:\d{2}\]/gi,
    "",
  );
}

// Splits a key-decisions sentence into segments by the <core>...</core>
// and <nuance>...</nuance> markup the key_decisions.md Round 2 addendum
// instructs Sonnet to emit. Legacy outputs without tags collapse to a
// single 'plain' segment — graceful degradation, no rendering change.
type KeyDecisionSegment = {
  type: "plain" | "core" | "nuance";
  text: string;
  charStart: number;
  charEnd: number;
  boldRanges: Array<{ charStart: number; charEnd: number }>;
};
function splitKeyDecisionByHighlights(text: string): KeyDecisionSegment[] {
  if (!text) return [];
  const re = /<(core|nuance)>([\s\S]*?)<\/\1>/g;
  const segments: KeyDecisionSegment[] = [];
  let displayCursor = 0;
  const pushSegment = (type: KeyDecisionSegment["type"], raw: string) => {
    if (!raw) return;
    const boldRanges: Array<{ charStart: number; charEnd: number }> = [];
    let clean = "";
    let rawCursor = 0;
    const boldRe = /\*\*([^*]+)\*\*/g;
    let boldMatch: RegExpExecArray | null;
    while ((boldMatch = boldRe.exec(raw)) !== null) {
      clean += raw.slice(rawCursor, boldMatch.index);
      const boldStart = displayCursor + clean.length;
      clean += boldMatch[1];
      boldRanges.push({ charStart: boldStart, charEnd: displayCursor + clean.length });
      rawCursor = boldMatch.index + boldMatch[0].length;
    }
    clean += raw.slice(rawCursor);
    segments.push({
      type,
      text: clean,
      charStart: displayCursor,
      charEnd: displayCursor + clean.length,
      boldRanges,
    });
    displayCursor += clean.length;
  };
  let lastIdx = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > lastIdx) {
      pushSegment("plain", text.slice(lastIdx, m.index));
    }
    pushSegment(m[1] as "core" | "nuance", m[2]);
    lastIdx = m.index + m[0].length;
  }
  if (lastIdx < text.length) {
    pushSegment("plain", text.slice(lastIdx));
  }
  if (segments.length === 0) pushSegment("plain", text);
  return segments;
}

// parseEpisodeTags + stripCitations moved to utils/episodeTags.ts.

// ── Component ──────────────────────────────────────────────────────

type PendingProcessApproval = {
  output_type: string;
  chunk_index: number;
  chunk_total: number;
  provider: string;
  model: string;
  key_fingerprint: string;
  full_envelope: string;
};

const PROCESS_OUTPUT_LABELS: Record<string, string> = {
  synopsis: "Synopsis",
  key_decisions: "Key decisions",
  community_calls_to_action: "Community calls to action",
  episode_tagline: "Episode headline",
};

const PROCESS_PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  gemini: "Gemini",
  anthropic: "Anthropic",
  codex: "Codex",
};

/**
 * Local-workspace process gate (zspan CLI `open` mode only — the local
 * server self-identifies; zspan.org never renders this). An
 * un-generated meeting isn't "pending review" here, it's the menu: the
 * user clicks Process, their machine fetches + transcribes the
 * recording (free, local) and synthesizes the broadcast with their own
 * key, progress streaming live. When it lands, the page becomes the
 * broadcast.
 */
function LocalProcessGate({
  meetingId,
  city,
  date,
  title,
  onBack,
}: {
  meetingId: number;
  city: string;
  date: string;
  title: string;
  onBack: () => void;
}) {
  const [phase, setPhase] = useState<"idle" | "running" | "done" | "error">("idle");
  const [lines, setLines] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [cloudReady, setCloudReady] = useState(false);
  const [needsKey, setNeedsKey] = useState(false);
  const [pastedKey, setPastedKey] = useState("");
  // The synthesis-engine choice (operator ask 2026-07-10): "key" = the
  // stored API key; "codex" = the installed Codex CLI on the user's own
  // subscription (keyless, frontier tier). Offered only when the local
  // server reports the binary actually exists.
  const [engine, setEngine] = useState<"key" | "codex">("key");
  const [codexInfo, setCodexInfo] = useState<{ available: boolean; model: string }>({
    available: false,
    model: "",
  });
  const [pendingApproval, setPendingApproval] = useState<PendingProcessApproval | null>(null);
  const [approvalSubmitting, setApprovalSubmitting] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);

  // Is the faster cloud path ready on a stored key? Presence only — the
  // key itself never travels to this page.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/local/process/setup")
      .then(r => r.json())
      .then(d => {
        if (cancelled || !d) return;
        setCloudReady(!!d.cloud_ready);
        setCodexInfo({ available: !!d.codex_available, model: d.codex_model || "" });
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  // Attach to an already-running processing pass (kicked from another
  // tab or the terminal) instead of offering a second button.
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/local/process/${meetingId}/status`)
      .then(r => r.json())
      .then(d => {
        if (cancelled || !d) return;
        if (d.running) setPhase("running");
        if (Array.isArray(d.lines) && d.lines.length) setLines(d.lines);
        setPendingApproval(d.pending_approval || null);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [meetingId]);

  useEffect(() => {
    if (phase !== "running") return;
    const timer = window.setInterval(() => {
      fetch(`/api/local/process/${meetingId}/status`)
        .then(r => r.json())
        .then(d => {
          if (!d) return;
          if (Array.isArray(d.lines)) setLines(d.lines);
          setPendingApproval(d.pending_approval || null);
          if (d.done) {
            window.clearInterval(timer);
            setPendingApproval(null);
            if (d.ok) {
              setPhase("done");
              window.setTimeout(() => window.location.reload(), 1200);
            } else {
              setPhase("error");
              setError(d.error || "processing failed — the log above has the detail");
            }
          }
        })
        .catch(() => {});
    }, 1200);
    return () => window.clearInterval(timer);
  }, [phase, meetingId]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [lines]);

  useEffect(() => {
    setApprovalError(null);
  }, [pendingApproval]);

  const lastModeRef = useRef<"local" | "cloud">("local");

  const kick = (mode: "local" | "cloud", key?: string) => {
    lastModeRef.current = mode;
    setMenuOpen(false);
    setNeedsKey(false);
    setPhase("running");
    setError(null);
    fetch(`/api/local/process/${meetingId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        synthesis_engine: engine,
        ...(key ? { openai_key: key } : {}),
      }),
    })
      .then(r => r.json())
      .then(d => {
        if (d && d.started === false) {
          setPhase("error");
          setError(d.error || "couldn't start processing");
        } else if (key) {
          setCloudReady(true);
          setPastedKey("");
        }
      })
      .catch(e => {
        setPhase("error");
        setError(String(e));
      });
  };

  const chooseCloud = () => {
    if (cloudReady) kick("cloud");
    else {
      setMenuOpen(false);
      setNeedsKey(true);
    }
  };

  const sendApproval = async (decision: "proceed" | "skip" | "abort") => {
    if (!pendingApproval || approvalSubmitting) return;
    setApprovalSubmitting(true);
    setApprovalError(null);
    try {
      const response = await fetch(`/api/local/process/${meetingId}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      const body = await response.json();
      if (response.status === 409 && body?.pending === false) {
        setPendingApproval(null);
        return;
      }
      if (!response.ok || !body?.accepted) {
        throw new Error(body?.error || "The approval could not be recorded.");
      }
      setPendingApproval(null);
    } catch (approvalPostError) {
      setApprovalError(String(approvalPostError));
    } finally {
      setApprovalSubmitting(false);
    }
  };

  return (
    <div className="h-screen w-full bg-[#0E0E10] flex flex-col items-center justify-center text-[#E4E4E5] font-sans antialiased px-8">
      <div className="max-w-xl w-full">
        <button
          onClick={onBack}
          className="group inline-flex items-center gap-1.5 text-gray-500 hover:text-white transition-colors mb-8"
          title="Back to channels"
        >
          <ArrowLeft className="w-3.5 h-3.5 group-hover:-translate-x-0.5 transition-transform" />
          <span className="text-[10px] font-semibold uppercase tracking-widest">
            All Channels
          </span>
        </button>

        <p className="text-[11px] uppercase tracking-[0.2em] text-gray-500 mb-2">
          {phase === "done" ? "Processed" : "Not processed yet"}
        </p>
        <h1 className="text-[22px] font-bold text-white mb-2">
          {city} · {date}
        </h1>
        <p className="text-[14px] text-gray-400 mb-6">{title}</p>

        {phase === "idle" && (
          <>
            <p className="text-[13px] text-gray-300 leading-relaxed mb-6">
              This meeting hasn't been generated on your machine yet.
              Processing fetches the recording, transcribes it, and
              synthesizes the broadcast with your own key — every output
              passes a deterministic grounding check against the
              transcript before it's saved.
            </p>

            <div className="relative inline-flex">
              <button
                onClick={() => kick("local")}
                className="inline-flex items-center gap-2 rounded-l-md border border-emerald-400/50 bg-emerald-400/10 px-5 py-2.5 text-[13px] font-semibold text-emerald-200 hover:bg-emerald-400/20 transition-colors"
              >
                Process this meeting
              </button>
              <button
                onClick={() => setMenuOpen(o => !o)}
                aria-label="Processing options"
                className="inline-flex items-center rounded-r-md border border-l-0 border-emerald-400/50 bg-emerald-400/10 px-2.5 text-emerald-200 hover:bg-emerald-400/20 transition-colors"
              >
                <svg width="10" height="6" viewBox="0 0 10 6" fill="none">
                  <path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              </button>

              {menuOpen && (
                <div className="absolute left-0 top-full mt-1.5 w-72 rounded-md border border-white/15 bg-[#141416] shadow-2xl z-10 overflow-hidden">
                  <button
                    onClick={() => kick("local")}
                    className="block w-full text-left px-4 py-3 hover:bg-white/5 transition-colors"
                  >
                    <span className="block text-[12px] font-semibold text-white">
                      Local — free
                    </span>
                    <span className="block text-[11px] text-gray-400">
                      Transcribes on this machine, roughly real-time. No
                      key needed for transcription.
                    </span>
                  </button>
                  <button
                    onClick={chooseCloud}
                    className="block w-full text-left px-4 py-3 hover:bg-white/5 transition-colors border-t border-white/10"
                  >
                    <span className="block text-[12px] font-semibold text-white">
                      Cloud — faster
                      {cloudReady && (
                        <span className="ml-2 text-[10px] uppercase tracking-wider text-emerald-300">
                          ready · uses your stored OpenAI key
                        </span>
                      )}
                    </span>
                    <span className="block text-[11px] text-gray-400">
                      whisper-1 transcription (~$0.36 per hour of audio on
                      your OpenAI key). Synthesis still uses your chosen
                      provider.
                    </span>
                  </button>
                  {/* The synthesis-engine choice — shown only when the
                      Codex CLI actually exists on this machine. Applies
                      to whichever transcription mode is clicked above. */}
                  {codexInfo.available && (
                    <div className="border-t border-white/10 px-4 py-3">
                      <p className="text-[10px] uppercase tracking-widest text-gray-500 mb-2">
                        Synthesize with
                      </p>
                      <div className="flex rounded-md border border-white/15 overflow-hidden">
                        <button
                          onClick={() => setEngine("key")}
                          className={`flex-1 px-2 py-1.5 text-[11px] font-semibold transition-colors ${
                            engine === "key"
                              ? "bg-emerald-400/15 text-emerald-200"
                              : "text-gray-400 hover:bg-white/5"
                          }`}
                        >
                          Your API key
                        </button>
                        <button
                          onClick={() => setEngine("codex")}
                          className={`flex-1 px-2 py-1.5 text-[11px] font-semibold transition-colors border-l border-white/15 ${
                            engine === "codex"
                              ? "bg-emerald-400/15 text-emerald-200"
                              : "text-gray-400 hover:bg-white/5"
                          }`}
                        >
                          Codex ({codexInfo.model})
                        </button>
                      </div>
                      <p className="text-[10px] text-gray-500 mt-1.5 leading-relaxed">
                        Codex runs on your ChatGPT subscription through the
                        installed CLI — the strongest tier, no API-key spend.
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>

            {needsKey && (
              <div className="mt-4 max-w-md">
                <p className="text-[12px] text-gray-300 mb-2">
                  Cloud transcription runs on an OpenAI key. It stores in
                  your local config file on this machine (same place{" "}
                  <span className="font-mono">zspan init</span> writes) and
                  is sent only to OpenAI.
                </p>
                <div className="flex gap-2">
                  <input
                    type="password"
                    value={pastedKey}
                    onChange={e => setPastedKey(e.target.value)}
                    placeholder="sk-..."
                    className="flex-1 rounded-md border border-white/15 bg-black/40 px-3 py-2 text-[12px] font-mono text-gray-200 placeholder:text-gray-600 focus:outline-none focus:border-emerald-400/50"
                  />
                  <button
                    onClick={() => pastedKey.trim() && kick("cloud", pastedKey.trim())}
                    disabled={!pastedKey.trim()}
                    className="rounded-md border border-emerald-400/50 bg-emerald-400/10 px-4 py-2 text-[12px] font-semibold text-emerald-200 hover:bg-emerald-400/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    Save & process
                  </button>
                </div>
              </div>
            )}

            <p className="text-[11px] text-gray-500 mt-4">
              You can close and return to this page — processing continues
              until it reaches a review, then waits for you without sending.
            </p>
          </>
        )}

        {(phase === "running" || phase === "done" || phase === "error") && (
          <>
            <div
              ref={logRef}
              className="rounded-md border border-white/10 bg-black/50 p-4 h-56 overflow-y-auto font-mono text-[11px] leading-relaxed text-gray-300 mb-4"
            >
              {lines.length === 0 ? (
                <span className="text-gray-500">starting…</span>
              ) : (
                lines.map((l, i) => <div key={i}>{l}</div>)
              )}
            </div>
            {phase === "running" && (
              <>
                <p className="text-[12px] text-gray-400">
                  Working — this page updates live. Long meetings take a
                  while (transcription is roughly real-time).
                </p>
                {/* The HQ doubles as the watch-it-work page: every
                    pipeline step fires the skybox's fiber-optic stars,
                    and hovering one shows the exact thing it did.
                    Processing runs on the server, so leaving this page
                    changes nothing — the HQ's corner pill links back
                    when the broadcast lands. */}
                <a
                  href="/?view=hq"
                  className="inline-flex items-center gap-1.5 mt-3 text-[12px] text-emerald-300/90 hover:text-emerald-200 transition-colors"
                >
                  <span>Or watch the HQ work in real time — every star is
                  a step of this run</span>
                  <span aria-hidden="true">→</span>
                </a>
              </>
            )}
            {phase === "done" && (
              <p className="text-[12px] text-emerald-300">
                Done — loading the broadcast…
              </p>
            )}
            {phase === "error" && (
              <>
                <p className="text-[12px] text-red-300 mb-3">{error}</p>
                <button
                  onClick={() => kick(lastModeRef.current)}
                  className="inline-flex items-center gap-2 rounded-md border border-white/20 bg-white/5 px-4 py-2 text-[12px] font-semibold text-gray-200 hover:bg-white/10 transition-colors"
                >
                  Try again
                </button>
              </>
            )}
          </>
        )}
      </div>

      {pendingApproval && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 px-4 py-6 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-labelledby="process-approval-title"
        >
          <div className="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-white/15 bg-[#141416] shadow-2xl">
            <div className="border-b border-white/10 px-5 py-4 sm:px-6">
              <p className="mb-1 text-[10px] font-semibold uppercase tracking-[0.2em] text-emerald-300">
                Review before sending · {pendingApproval.chunk_index} of {pendingApproval.chunk_total}
              </p>
              <h2 id="process-approval-title" className="text-lg font-bold text-white">
                {PROCESS_OUTPUT_LABELS[pendingApproval.output_type] || "Generated output"}
              </h2>
              <p className="mt-1 text-[12px] text-gray-400">
                {(PROCESS_PROVIDER_LABELS[pendingApproval.provider] || pendingApproval.provider)} · {pendingApproval.model} · key {pendingApproval.key_fingerprint}
              </p>
            </div>

            <div className="min-h-0 flex-1 px-5 py-4 sm:px-6">
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-widest text-gray-400">
                Everything that will be sent
              </p>
              <pre className="max-h-[55vh] w-full overflow-auto rounded-lg border border-white/10 bg-black/60 p-4 font-mono text-[11px] leading-relaxed text-gray-200">{pendingApproval.full_envelope}</pre>
              {approvalError && (
                <p className="mt-3 text-[12px] text-red-300">{approvalError}</p>
              )}
            </div>

            <div className="flex flex-col-reverse gap-2 border-t border-white/10 px-5 py-4 sm:flex-row sm:justify-end sm:px-6">
              <button
                type="button"
                disabled={approvalSubmitting}
                onClick={() => void sendApproval("abort")}
                className="rounded-md border border-red-400/40 bg-red-400/10 px-4 py-2.5 text-[12px] font-semibold text-red-200 transition-colors hover:bg-red-400/20 disabled:cursor-wait disabled:opacity-50"
              >
                Stop the run
              </button>
              <button
                type="button"
                disabled={approvalSubmitting}
                onClick={() => void sendApproval("skip")}
                className="rounded-md border border-white/20 bg-white/5 px-4 py-2.5 text-[12px] font-semibold text-gray-200 transition-colors hover:bg-white/10 disabled:cursor-wait disabled:opacity-50"
              >
                Skip this output
              </button>
              <button
                type="button"
                disabled={approvalSubmitting}
                onClick={() => void sendApproval("proceed")}
                className="rounded-md border border-emerald-400/50 bg-emerald-400/10 px-4 py-2.5 text-[12px] font-semibold text-emerald-200 transition-colors hover:bg-emerald-400/20 disabled:cursor-wait disabled:opacity-50"
              >
                {approvalSubmitting ? "Recording decision…" : "Send to provider"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

type PublishedBroadcastPageProps = Omit<BroadcastPageProps, "meetingId" | "publicId"> & {
  meetingId?: number;
  publicId?: string;
};

function PublishedBroadcastPage({
  meetingId,
  publicId,
  onBack,
  onNavigate,
  initialSeek,
}: PublishedBroadcastPageProps) {
  const publicPlane = isPublicPlane();
  const currentUser = useCurrentUser();
  // Local-workspace mode (the zspan CLI's `open` server identifies
  // itself via /api/system/status). Nothing here is "pending review" —
  // there is no operator locally; an un-generated meeting gets a
  // Process affordance instead of the flagship's review-gate copy.
  // The flagship never returns this mode, so this branch never renders
  // on zspan.org.
  const [localMode, setLocalMode] = useState(false);
  useEffect(() => {
    if (publicPlane) return;
    let cancelled = false;
    fetch("/api/system/status")
      .then(r => r.json())
      .then(d => {
        if (!cancelled && d && d.mode === "local-workspace") setLocalMode(true);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [publicPlane]);
  // Local Librarian readiness (zspan CLI `open` mode only). The local
  // server holds the user's own stored key (`zspan init`); this probe is
  // presence-only — provider + display fingerprint travel, the key never
  // does (the LocalProcessGate cloud-probe pattern). S-131: a private
  // workspace is the user's own tool, so the flagship's D-145 public
  // V2-lock doesn't apply here; zspan.org never identifies as
  // local-workspace, so none of this renders there.
  const [localLibrarian, setLocalLibrarian] = useState<{
    provider: string;
    model: string;
    fingerprint: string;
    engine: "key" | "codex";
  } | null>(null);
  useEffect(() => {
    if (!localMode) return;
    let cancelled = false;
    fetch("/api/local/librarian/setup")
      .then(r => r.json())
      .then(d => {
        if (!cancelled && d && d.ready) {
          setLocalLibrarian({
            provider: d.provider,
            model: d.model,
            fingerprint: d.fingerprint || "",
            engine: d.engine === "codex" ? "codex" : "key",
          });
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [localMode]);
  // V1-Polish-5: one-time anonymous sign-in-benefits nudge (toast).
  const [showBenefitsToast, setShowBenefitsToast] = useState(false);
  const [data, setData] = useState<BroadcastResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [publicCatalogFallback, setPublicCatalogFallback] = useState(false);
  const [voidConfirmOutput, setVoidConfirmOutput] = useState<string | null>(null);
  const [voidMutationOutput, setVoidMutationOutput] = useState<string | null>(null);
  const [voidMutationError, setVoidMutationError] = useState<string | null>(null);
  const [voidMutationErrorOutput, setVoidMutationErrorOutput] = useState<
    string | null
  >(null);
  const [expandedVoidedOutputs, setExpandedVoidedOutputs] = useState<
    Record<string, boolean>
  >({});
  // V1-UI-1 per D-126: default to "full" (Original) since "summary" is V2-locked.
  // The useEffect on data-load also forces "full" so the user lands on the
  // canonical original-video tab rather than the locked V2 placeholder.
  const [videoTab, setVideoTab] = useState<VideoTab>("full");
  // Id of the SyncedQuote karaoke player currently active across the
  // decision receipts and Community Calls to Action. Null means no active
  // player; the meeting-change effect below resets it.
  const [activeBroadcastQuoteId, setActiveBroadcastQuoteId] = useState<
    string | null
  >(null);

  const [sidebarMeetings, setSidebarMeetings] = useState<SidebarMeeting[]>([]);

  // Cached suggested-question history. Capped so repeated playback does
  // not accrete unbounded DOM; live open-ended querying is owned entirely
  // by ByokQueryPanel.
  const CHAT_HISTORY_CAP = 50;
  const capChat = (msgs: ChatMessage[]): ChatMessage[] =>
    msgs.length <= CHAT_HISTORY_CAP ? msgs : msgs.slice(-CHAT_HISTORY_CAP);
  const [chatMode, setChatMode] = useState<ChatMode>("direct");
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
  const [chatSending, setChatSending] = useState(false);

  // V1.5-BYOK-Shell-1: modal-from-lock UX. Clicking the V2/BYOK-locked
  // "Ask anything" input opens this modal; after successful validation the
  // key stays in memory for this page + byokConfig becomes non-null. V1.5-Query-1
  // is the next chunk that will swap the locked input for the active
  // Gemini-direct path once byokConfig is set.
  const [byokModalOpen, setByokModalOpen] = useState(false);
  const [byokConfig, setByokConfig] = useState<ByokConfig | null>(() => getByokConfig());
  // The config the Librarian panel actually runs on. Local workspace
  // wins when armed (stored key, loopback synthesis — no key in this
  // page); otherwise the flagship's signed-in, in-memory BYOK path.
  const librarianCanConfigure =
    currentUser.user !== null &&
    currentUser.user.librarian_access !== "banned";
  const activeByokConfig: ByokConfig | null =
    localMode && localLibrarian
      ? {
          provider: LOCAL_WORKSPACE_PROVIDER,
          key: "",
          fingerprint: localLibrarian.fingerprint,
          validatedAt: "",
        }
      : librarianCanConfigure
        ? byokConfig
        : null;
  const signedOutPublicViewer =
    publicPlane && !currentUser.loading && currentUser.user === null;
  const canonicalPublicId = publicPlane ? data?.public_id : undefined;
  // D-186 signed-out cited-answer set. Hook is anonymous-only + auth-gated;
  // returns hidden state for signed-in users so we can call it unconditionally
  // and just read the state. Chip picker + inline answer render below.
  const simQueriesState = useSignedOutSimQueries(canonicalPublicId);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const [audioOpen, setAudioOpen] = useState(false);

  // S-101 Librarian character-video (session-30, 2026-07-04). The video
  // plays while a BYOK query is in flight and rewinds to frame 0 when the
  // LLM begins producing output / finishes. librarianBusy is fed by
  // ByokQueryPanel's onSendingChange callback.
  const librarianVideoRef = useRef<HTMLVideoElement | null>(null);
  const [librarianBusy, setLibrarianBusy] = useState<boolean>(false);

  // PLAYER-1 (2026-07-07) — the S-103 embed-disabled stack (oEmbed
  // preflight + IFrame error listener + "listening" handshake +
  // degraded-seek routing) moved INSIDE the ZspanPlayer shell. The page
  // just holds the imperative handle it drives seeks through.
  const playerRef = useRef<ZspanPlayerHandle | null>(null);
  useEffect(() => {
    const v = librarianVideoRef.current;
    if (!v) return;
    if (librarianBusy) {
      v.currentTime = 0;
      void v.play().catch(() => {
        // Autoplay-block possible on some browsers with sound=off but a
        // require-gesture policy; harmless — the video just stays paused.
      });
      return;
    }
    // Idle. Always pause + seek to frame 0 (bug caught by operator
    // 2026-07-04: prior version only seeked without pausing when
    // readyState > 0, so the video kept looping after the LLM
    // response landed). Then, if readyState is still 0 because the
    // browser throttled preload="auto" on the muted+paused mount,
    // trigger a silent play-then-pause bootstrap so the first frame
    // renders as the resting state.
    v.pause();
    v.currentTime = 0;
    if (v.readyState === 0) {
      v.play().then(() => {
        v.pause();
        v.currentTime = 0;
      }).catch(() => {
        // Autoplay-blocked; runtime playback still works when the
        // operator submits a query (user-initiated context).
      });
    }
  }, [librarianBusy]);

  // Pull chat_mode from settings. /api/settings is owner-only (the
  // catchall Pages Function 403s non-owners; the user_settings.json
  // file contains BYOK secrets). Skip the fetch for viewers — they
  // get the default chat_mode ("direct"), which is fine. This also
  // avoids 403 noise in the browser console on every BroadcastPage
  // load for allowlisted-but-not-owner sessions.
  useEffect(() => {
    if (!currentUser.isOwner) return;
    let aborted = false;
    fetch("/api/settings")
      .then(res => res.json())
      .then(s => {
        if (aborted) return;
        if (s?.chat_mode === "suggested" || s?.chat_mode === "direct") {
          setChatMode(s.chat_mode);
        }
      })
      .catch(() => {});
    return () => {
      aborted = true;
    };
  }, [currentUser.isOwner]);

  // V1-Polish-5: when an anonymous viewer first opens a broadcast, float in a
  // one-time "Did you know?" sign-in nudge — once per browser session (a
  // gentle reminder, not a per-broadcast nag), never for signed-in users.
  useEffect(() => {
    if (
      currentUser.loading ||
      currentUser.user ||
      !currentUser.signInEnabled
    ) return;
    let alreadyShown = false;
    try {
      alreadyShown = sessionStorage.getItem("zspan.benefitsToastShown") === "1";
    } catch {
      /* sessionStorage unavailable — just show it once this mount */
    }
    if (alreadyShown) return;
    const t = window.setTimeout(() => {
      setShowBenefitsToast(true);
      try {
        sessionStorage.setItem("zspan.benefitsToastShown", "1");
      } catch {
        /* ignore */
      }
    }, 700);
    return () => window.clearTimeout(t);
  }, [
    currentUser.loading,
    currentUser.signInEnabled,
    currentUser.user,
  ]);

  // Phase 3 — publish-status snapshot. Loaded alongside the broadcast
  // data so the "Reviewed by X on [date]" badge can render in the
  // header. Public-facing; the anonymization layer (citation panel
  // anonymized mode) lives separately. Null until first load.
  const publishStatusIdentity = publicPlane
    ? `public:${publicId ?? ""}`
    : `operator:${meetingId ?? ""}`;
  const [publishStatus, setPublishStatus] = useState<{
    identity: string;
    is_published: boolean;
    published_at: string | null;
  } | null>(null);
  const currentPublishStatus =
    publishStatus?.identity === publishStatusIdentity ? publishStatus : null;

  useEffect(() => {
    if (publicPlane) return;
    let aborted = false;
    fetch(`/api/meetings/${meetingId}/publish-status`)
      .then(res => res.json())
      .then(body => {
        if (aborted) return;
        if (body?.success && body?.meeting) {
          setPublishStatus({
            identity: publishStatusIdentity,
            is_published: Boolean(body.meeting.is_published),
            published_at: body.meeting.published_at ?? null,
          });
        }
      })
      .catch(() => {});
    return () => {
      aborted = true;
    };
  }, [meetingId, publicPlane, publishStatusIdentity]);

  // Broadcast data
  useEffect(() => {
    let aborted = false;
    setLoading(true);
    setError(null);
    setPublicCatalogFallback(false);
    setVoidConfirmOutput(null);
    setVoidMutationError(null);
    setVoidMutationErrorOutput(null);
    setExpandedVoidedOutputs({});
    fetchForPlane({
      publicPath: `/public-api/broadcasts/${encodeURIComponent(publicId || "")}`,
      operatorPath: `/api/notebook/${meetingId}`,
    })
      .then(async res => {
        if (publicPlane && res.status === 404) {
          if (!aborted) {
            setData(null);
            setLoading(false);
            setPublicCatalogFallback(true);
          }
          return null;
        }
        return res.json();
      })
      .then((body: BroadcastResponse) => {
        if (aborted || body === null) return;
        if (!body || body.success === false) {
          setError("This meeting has no broadcast data yet.");
          setData(null);
        } else {
          setData(body);
          if (publicPlane) {
            setPublishStatus({
              identity: publishStatusIdentity,
              is_published: true,
              published_at: body.published_at ?? null,
            });
          }
          // Land on the most-polished variant available: Corpo > Kawaii
          // > Full. Corpo's data lives at outputs.video_explainer (the
          // canonical Corporate Dark Mode prompt). Kawaii's data lives at
          // outputs.video_explainer_kawaii once that prompt exists.
          const bodyOutputs = (body.outputs || {}) as Record<
            string,
            { content_url?: string | null } | undefined
          >;
          // V1-UI-1 per D-126: corpo + kawaii outputs are V2-deferred
          // (audio_overview / video_explainer / infographic all defer per
          // the Z-SPAN-controlled studio-pipeline rebuild). Default the
          // active tab to "full" (Original) so users land on the canonical
          // original-video surface; "summary" is rendered as a locked V2
          // placeholder + cannot be activated.
          setVideoTab("full");
          setChatHistory([]);
          setAudioOpen(false);
        }
        setLoading(false);
      })
      .catch(err => {
        if (aborted) return;
        setError(err.message || "Failed to load broadcast.");
        setLoading(false);
      });
    return () => {
      aborted = true;
    };
  }, [meetingId, publicId, publicPlane, publishStatusIdentity]);

  // Sidebar meeting list for the current city
  useEffect(() => {
    if (!data?.city) {
      setSidebarMeetings([]);
      return;
    }
    let aborted = false;
    const city = encodeURIComponent(data.city);
    const request = publicPlane
      ? fetch(`/public-api/cities/${city}/meetings?year=all`)
      : fetch("/api/calendar/events", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cityName: data.city }),
        });
    request
      .then(res => res.json())
      .then(body => {
        if (aborted) return;
        const events: SidebarMeeting[] = Array.isArray(body?.events) ? body.events : [];
        setSidebarMeetings(events);
      })
      .catch(() => {
        if (!aborted) setSidebarMeetings([]);
      });
    return () => {
      aborted = true;
    };
  }, [data?.city, publicPlane]);

  // Auto-scroll chat (matches <private-predecessor-repo>'s behavior)
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatHistory]);

  // Derived data
  const outputs = data?.outputs || {};

  const mutateOutputVisibility = async (
    outputType: string,
    restore: boolean,
  ) => {
    if (!data?.meeting_id || publicPlane || !currentUser.isOwner) return;
    if (!restore && voidConfirmOutput !== outputType) {
      setVoidConfirmOutput(outputType);
      setVoidMutationError(null);
      setVoidMutationErrorOutput(null);
      return;
    }

    setVoidMutationOutput(outputType);
    setVoidMutationError(null);
    setVoidMutationErrorOutput(null);
    try {
      const action = restore ? "restore" : "void";
      const response = await fetch(
        `/api/notebook/${data.meeting_id}/outputs/${encodeURIComponent(outputType)}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok || !payload?.success || !payload?.output) {
        throw new Error(payload?.error || `Could not ${action} this section.`);
      }
      setData(previous => {
        if (!previous) return previous;
        const existing = previous.outputs[outputType];
        if (!existing) return previous;
        return {
          ...previous,
          outputs: {
            ...previous.outputs,
            [outputType]: {
              ...existing,
              voided_at: payload.output.voided_at ?? null,
              voided_by: payload.output.voided_by ?? null,
            },
          },
        };
      });
      setVoidConfirmOutput(null);
      setVoidMutationErrorOutput(null);
      if (restore) {
        setExpandedVoidedOutputs(previous => ({
          ...previous,
          [outputType]: false,
        }));
      }
    } catch (mutationError) {
      setVoidMutationErrorOutput(outputType);
      setVoidMutationError(
        mutationError instanceof Error
          ? mutationError.message
          : "Could not update this section.",
      );
    } finally {
      setVoidMutationOutput(null);
    }
  };

  const renderOutputControl = (outputType: string) => {
    const output = outputs[outputType];
    if (
      publicPlane ||
      !currentUser.isOwner ||
      !data?.meeting_id ||
      !output
    ) {
      return null;
    }
    const isVoided = Boolean(output.voided_at);
    const isBusy = voidMutationOutput === outputType;
    const awaitingConfirmation =
      !isVoided && voidConfirmOutput === outputType;

    return (
      <div className="flex items-center justify-end gap-2">
        {awaitingConfirmation && (
          <button
            type="button"
            disabled={isBusy}
            onClick={() => setVoidConfirmOutput(null)}
            className="text-[9px] font-semibold uppercase tracking-[0.18em] text-white/40 transition-colors hover:text-white/70 disabled:opacity-40"
          >
            Cancel
          </button>
        )}
        <button
          type="button"
          disabled={isBusy}
          onClick={() => void mutateOutputVisibility(outputType, isVoided)}
          className={`rounded border px-2 py-1 text-[9px] font-bold uppercase tracking-[0.18em] transition-colors disabled:cursor-wait disabled:opacity-50 ${
            isVoided
              ? "border-emerald-400/35 bg-emerald-400/10 text-emerald-200 hover:bg-emerald-400/20"
              : awaitingConfirmation
                ? "border-rose-400/55 bg-rose-400/15 text-rose-100 hover:bg-rose-400/25"
                : "border-white/15 bg-white/[0.03] text-white/45 hover:border-rose-400/45 hover:text-rose-200"
          }`}
        >
          {isBusy
            ? "Updating…"
            : isVoided
              ? "Voided — restore"
              : awaitingConfirmation
                ? "Confirm void"
                : "Void"}
        </button>
      </div>
    );
  };

  const renderOperatorOutput = (
    outputType: string,
    content: ReactNode,
  ) => {
    const output = outputs[outputType];
    const operatorControlVisible =
      !publicPlane && currentUser.isOwner && Boolean(data?.meeting_id) && !!output;
    if (!operatorControlVisible) return content;

    const isVoided = Boolean(output.voided_at);
    const expanded = Boolean(expandedVoidedOutputs[outputType]);
    if (!isVoided) {
      return (
        <div>
          <div className="mb-3 flex justify-end">
            {renderOutputControl(outputType)}
          </div>
          {voidMutationError && voidMutationErrorOutput === outputType && (
            <p className="mb-3 text-right text-[11px] text-rose-300">
              {voidMutationError}
            </p>
          )}
          {content}
        </div>
      );
    }

    const voidedDate = output.voided_at
      ? new Date(output.voided_at.replace(" ", "T") + "Z").toLocaleString(
          "en-US",
          { dateStyle: "medium", timeStyle: "short" },
        )
      : "";
    return (
      <div className="mb-12 rounded-lg border border-rose-400/25 bg-rose-400/[0.04]">
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-[0.2em] text-rose-200/75">
              {PROCESS_OUTPUT_LABELS[outputType] || "Section"} voided
            </p>
            <p className="mt-1 text-[11px] text-white/35">
              Hidden from public view{voidedDate ? ` · voided ${voidedDate}` : ""}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() =>
                setExpandedVoidedOutputs(previous => ({
                  ...previous,
                  [outputType]: !expanded,
                }))
              }
              className="text-[9px] font-semibold uppercase tracking-[0.18em] text-white/45 transition-colors hover:text-white/75"
            >
              {expanded ? "Collapse content" : "Read voided content"}
            </button>
            {renderOutputControl(outputType)}
          </div>
        </div>
        {voidMutationError && voidMutationErrorOutput === outputType && (
          <p className="px-4 pb-3 text-right text-[11px] text-rose-300">
            {voidMutationError}
          </p>
        )}
        {expanded && <div className="border-t border-rose-400/15 p-4 opacity-55">{content}</div>}
      </div>
    );
  };

  const synopsisText = outputs.synopsis?.content?.trim()
    ? stripCitations(outputs.synopsis.content)
    : null;
  // Raw (citations kept) so the synopsis can render its inline [at MM:SS]
  // chips through KaraokeText, exactly like the Librarian answer does.
  // synopsisText (stripped) stays as the render gate; KaraokeText degrades
  // to plain prose when the content carries no markers (legacy synopses).
  const synopsisRaw = outputs.synopsis?.content?.trim() || null;
  const episodeTagline = outputs.episode_tagline?.content?.trim() || null;

  const keyDecisions = useMemo(() => {
    const raw = outputs.key_decisions?.content;
    if (!raw || looksLikeLegacyNotebookLMArtifact(raw)) return [];
    return parseNumberedList(stripKeyDecisionCitations(raw));
  }, [outputs.key_decisions?.content]);

  // Reading-style toggle (promoted from operator-only debug 2026-06-24;
  // relabeled "Reading style" in session-31 per D-054). Persists to
  // localStorage as a per-user preference — session-31 audit-fix. Prior
  // state was ephemeral useState only, so opening a second broadcast
  // reset the preference. Persistence key includes a v1 suffix so
  // future schema changes can trigger a graceful reset.
  const KD_INDENT_STORAGE_KEY = "zspan.reading-style.v1";
  const [kdIndentMode, setKdIndentMode] = useState<"hanging" | "plain" | "reverse">(() => {
    if (typeof window === "undefined") return "hanging";
    const raw = window.localStorage.getItem(KD_INDENT_STORAGE_KEY);
    return raw === "plain" || raw === "reverse" ? raw : "hanging";
  });
  const cycleKdIndent = () => {
    setKdIndentMode(m => {
      const next = m === "hanging" ? "plain" : m === "plain" ? "reverse" : "hanging";
      try {
        window.localStorage.setItem(KD_INDENT_STORAGE_KEY, next);
      } catch {
        // quota / privacy-mode failures — ignore, preference just won't persist
      }
      return next;
    });
  };

  const whatsNext = useMemo(() => {
    const raw = outputs.whats_next?.content;
    if (!raw || looksLikeLegacyNotebookLMArtifact(raw)) return [];
    return parseNumberedList(stripCitations(raw));
  }, [outputs.whats_next?.content]);

  // D-157 neutrality cut (session-42/43): the council_sentiment display was
  // removed (a curated mood reading is un-consensus-able editorial), and the
  // legacy heroQuotes / unified-quotes fetch was the dead residue of the cut
  // "Key Quotes" surface — both removed here. The KEPT quote path is
  // previewQuotes → routingPayload → decision-bound quotes rendered inside
  // Key Decisions (below); it does not touch the unified-quotes table.

  // Quote-discipline preview mode (2026-06-23) — when ?previewQuotes=true
  // is set on the URL, fetch the sidecar JSON of new-selection-discipline
  // quotes from /api/preview/quotes/:meetingId. Renders a debug toggle in
  // the Quotes section so James can compare OLD (current DB) vs NEW (new
  // prompt) without destructively replacing DB rows.
  interface PreviewQuoteBase {
    speaker_name: string;
    speaker_role: string | null;
    quote_text: string;
    topic_tags?: string[];
    chunk_index?: number;
    news_values?: string[];
    selection_rationale?: string;
    /** Per-word alignment from quote_align.py — populated by the
     *  align_preview_quotes.py post-extraction step. When present,
     *  the accordion expansion renders SyncedQuote for karaoke. */
    word_timings?: Array<{
      word: string;
      start_ms?: number;
      end_ms?: number;
      start?: number;
      end?: number;
    }> | null;
  }
  type PreviewQuote = PreviewQuoteBase & (
    | {
        speaker_class: "record";
        video_timestamp_seconds: number;
      }
    | {
        speaker_class: "council_member" | "staff" | "external";
        video_timestamp_seconds?: number;
      }
  );
  interface PreviewPayload {
    quotes: PreviewQuote[];
    quote_count?: number;
    extraction_started?: string;
    batches_completed?: number;
    batches_total?: number;
    elapsed_seconds?: number;
  }
  // Production cutover 2026-06-24 (D-132 follow-up): the new-discipline
  // quote + decision content is no longer behind a ?previewQuotes /
  // ?previewDecisions URL gate. The fetch always fires when meetingId
  // is set; if the sidecar exists (.preview/m<id>.json), we render the
  // new accordion shape — if it doesn't (meeting not yet processed
  // under the new pipeline), the decision-bound quotes simply stay empty
  // (the legacy UnifiedQuote / heroQuotes fallback render was removed with
  // the D-157 Key Quotes cut). No URL flag needed; data presence is the
  // cutover signal. DEBUG amber bars + OLD/NEW toggles retired.
  const sidecarIdentity = publicId
    ? `public:${publicId}`
    : meetingId !== undefined
      ? `meeting:${meetingId}`
      : "none";
  const previewQuotesIdentityRef = useRef<string | null>(null);
  const [storedPreviewQuotes, setPreviewQuotes] = useState<PreviewQuote[]>([]);
  const previewQuotes = previewQuotesIdentityRef.current === sidecarIdentity
    ? storedPreviewQuotes
    : [];
  useEffect(() => {
    previewQuotesIdentityRef.current = null;
    setPreviewQuotes([]);
    if (!meetingId && !publicId) return;
    const ctl = new AbortController();
    fetchForPlane(
      {
        publicPath: `/public-api/broadcasts/${encodeURIComponent(publicId || "")}/sidecars/quotes`,
        operatorPath: `/api/preview/quotes/${meetingId}`,
      },
      { signal: ctl.signal },
    )
      .then(async r => (r.ok ? (await r.json()) as PreviewPayload : null))
      .then(payload => {
        if (ctl.signal.aborted) return;
        previewQuotesIdentityRef.current = sidecarIdentity;
        setPreviewQuotes(payload?.quotes || []);
      })
      .catch(err => {
        if (ctl.signal.aborted || err.name === "AbortError") return;
        previewQuotesIdentityRef.current = null;
        setPreviewQuotes([]);
        console.warn("preview quotes fetch:", err);
      });
    return () => ctl.abort();
  }, [meetingId, publicId, sidecarIdentity]);

  // Decisions-discipline preview mode (2026-06-23, parallel to the quotes
  // toggle above). Sidecar lives at .preview/m<id>_decisions.json with a
  // prose_output string + audit_json array of {index, news_values, rationale}
  // entries. Rendered in the Key Decisions panel below.
  interface PreviewDecisionAudit {
    index: number;
    news_values?: string[];
    rationale?: string;
  }
  interface DecisionVerbatimSpan {
    text?: string;
    char_start?: number;
    char_end?: number;
    start_seconds?: number;
    end_seconds?: number;
    source?: string;
    label?: string;
    structure?: string;
    omission_marker?: string;
    chunk_index?: number;
    signature_id?: string;
    word_timings?: Array<{ word?: string; start?: number; end?: number }>;
  }
  interface PreviewDecisionSpans {
    index: number;
    verbatim_spans?: DecisionVerbatimSpan[];
  }
  interface PreviewDecisionsPayload {
    prose_output?: string;
    prose_list_count?: number;
    audit_json?: PreviewDecisionAudit[] | null;
    extraction_started?: string;
    elapsed_seconds?: number;
    chunks_total?: number;
    decisions?: PreviewDecisionSpans[];
  }
  const previewDecisionsIdentityRef = useRef<string | null>(null);
  const [storedPreviewDecisionsPayload, setPreviewDecisionsPayload] =
    useState<PreviewDecisionsPayload | null>(null);
  const previewDecisionsPayload =
    previewDecisionsIdentityRef.current === sidecarIdentity
      ? storedPreviewDecisionsPayload
      : null;
  useEffect(() => {
    previewDecisionsIdentityRef.current = null;
    setPreviewDecisionsPayload(null);
    if (!meetingId && !publicId) return;
    const ctl = new AbortController();
    fetchForPlane(
      {
        publicPath: `/public-api/broadcasts/${encodeURIComponent(publicId || "")}/sidecars/decisions`,
        operatorPath: `/api/preview/decisions/${meetingId}`,
      },
      { signal: ctl.signal },
    )
      .then(async r => (r.ok ? (await r.json()) as PreviewDecisionsPayload : null))
      .then(payload => {
        if (ctl.signal.aborted) return;
        previewDecisionsIdentityRef.current = sidecarIdentity;
        setPreviewDecisionsPayload(payload);
      })
      .catch(err => {
        if (ctl.signal.aborted || err.name === "AbortError") return;
        previewDecisionsIdentityRef.current = null;
        setPreviewDecisionsPayload(null);
        console.warn("preview decisions fetch:", err);
      });
    return () => ctl.abort();
  }, [meetingId, publicId, sidecarIdentity]);
  const previewDecisionsList = useMemo(() => {
    const prose = previewDecisionsPayload?.prose_output || "";
    if (!prose) return [];
    return parseNumberedList(stripKeyDecisionCitations(prose));
  }, [previewDecisionsPayload?.prose_output]);
  const effectiveKeyDecisions = useMemo(
    () => prepareInfographKeyDecisions(previewDecisionsList, keyDecisions),
    [previewDecisionsList, keyDecisions],
  );

  // Routing + recusal sidecars (2026-06-24, D-132 era post-extraction
  // stage). Routing classifies each quote into standalone / decision_bound
  // (N) / drop so the show page can nest the decision-bound ones under
  // their relevant Key Decision card. Recusals are meeting-level
  // accountability events that surface a red ❗ next to the KEY DECISIONS
  // header (NOT gated on the recusal's matter rising to a Key Decision
  // tier — per James's explicit direction, recusals are highlighted
  // period as conflict-of-interest signals).
  interface RoutingEntry {
    quote_index: number;
    bucket: "standalone" | "decision_bound" | "drop";
    decision_index?: number;
    rationale?: string;
  }
  interface RoutingPayload {
    routing?: RoutingEntry[];
    summary?: {
      standalone_count?: number;
      decision_bound_count?: number;
      drop_count?: number;
    };
  }
  interface RecusalEvent {
    speaker_name?: string;
    speaker_role?: string | null;
    rationale?: string;
    matter?: string | null;
    raw_text?: string;
    citation?: {
      source?: string;
      chunk_index?: number;
      decision_index?: number;
      video_timestamp_seconds?: number;
    };
  }
  interface RecusalsPayload {
    recusal_count?: number;
    recusals?: RecusalEvent[];
  }
  const routingIdentityRef = useRef<string | null>(null);
  const recusalsIdentityRef = useRef<string | null>(null);
  const [storedRoutingPayload, setRoutingPayload] = useState<RoutingPayload | null>(null);
  const [storedRecusalsPayload, setRecusalsPayload] = useState<RecusalsPayload | null>(null);
  const routingPayload = routingIdentityRef.current === sidecarIdentity
    ? storedRoutingPayload
    : null;
  const recusalsPayload = recusalsIdentityRef.current === sidecarIdentity
    ? storedRecusalsPayload
    : null;
  const [recusalPopoverOpen, setRecusalPopoverOpen] = useState(false);
  // Routing + recusal fetches fire on every meeting; sidecar presence
  // is the cutover signal, no URL flag required.
  useEffect(() => {
    routingIdentityRef.current = null;
    recusalsIdentityRef.current = null;
    setRoutingPayload(null);
    setRecusalsPayload(null);
    setRecusalPopoverOpen(false);
    if (!meetingId && !publicId) return;
    const ctl = new AbortController();
    fetchForPlane(
      {
        publicPath: `/public-api/broadcasts/${encodeURIComponent(publicId || "")}/sidecars/routing`,
        operatorPath: `/api/preview/routing/${meetingId}`,
      },
      { signal: ctl.signal },
    )
      .then(async r => (r.ok ? (await r.json()) as RoutingPayload : null))
      .then(p => {
        if (ctl.signal.aborted) return;
        routingIdentityRef.current = sidecarIdentity;
        setRoutingPayload(p);
      })
      .catch(err => {
        if (ctl.signal.aborted || err.name === "AbortError") return;
        routingIdentityRef.current = null;
        setRoutingPayload(null);
        console.warn("routing fetch:", err);
      });
    fetchForPlane(
      {
        publicPath: `/public-api/broadcasts/${encodeURIComponent(publicId || "")}/sidecars/recusals`,
        operatorPath: `/api/preview/recusals/${meetingId}`,
      },
      { signal: ctl.signal },
    )
      .then(async r => (r.ok ? (await r.json()) as RecusalsPayload : null))
      .then(p => {
        if (ctl.signal.aborted) return;
        recusalsIdentityRef.current = sidecarIdentity;
        setRecusalsPayload(p);
      })
      .catch(err => {
        if (ctl.signal.aborted || err.name === "AbortError") return;
        recusalsIdentityRef.current = null;
        setRecusalsPayload(null);
        setRecusalPopoverOpen(false);
        console.warn("recusals fetch:", err);
      });
    return () => ctl.abort();
  }, [meetingId, publicId, sidecarIdentity]);
  const decisionBoundQuotesByIndex = useMemo<Record<number, PreviewQuote[]>>(() => {
    const result: Record<number, PreviewQuote[]> = {};
    if (!routingPayload?.routing) return result;
    for (const r of routingPayload.routing) {
      if (r.bucket !== "decision_bound" || typeof r.decision_index !== "number") continue;
      const q = previewQuotes[r.quote_index];
      if (!q || q.speaker_class === "record") continue;
      if (!result[r.decision_index]) result[r.decision_index] = [];
      result[r.decision_index].push(q);
    }
    return result;
  }, [routingPayload, previewQuotes]);
  const hasDecisionVerbatimEvidence = Boolean(
    previewDecisionsPayload?.decisions?.some(decision =>
      decision.verbatim_spans?.some(span =>
        span.source === EXCERPT_SOURCE
        && typeof span.text === "string"
        && span.text.trim().length > 0
        && typeof span.label === "string"
        && (span.structure === "contiguous" || span.structure === "elided")
      )
    )
  );

  // Resizable column widths (2026-06-24). Default left-sidebar narrowed
  // from 340px → 280px per James's "a bit large at the moment." Right
  // column default stays 400px. Both persist to localStorage so the
  // operator's resize sticks across reloads.
  const [leftSidebarWidth, setLeftSidebarWidth] = useState<number>(() => {
    if (typeof window === "undefined") return 280;
    const saved = window.localStorage.getItem("zspan_broadcast_left_width");
    const n = saved ? parseInt(saved, 10) : NaN;
    return Number.isFinite(n) && n >= 200 && n <= 600 ? n : 280;
  });
  const [rightColumnWidth, setRightColumnWidth] = useState<number>(() => {
    if (typeof window === "undefined") return 400;
    const saved = window.localStorage.getItem("zspan_broadcast_right_width");
    const n = saved ? parseInt(saved, 10) : NaN;
    return Number.isFinite(n) && n >= 280 && n <= 700 ? n : 400;
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem("zspan_broadcast_left_width", String(leftSidebarWidth));
  }, [leftSidebarWidth]);
  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem("zspan_broadcast_right_width", String(rightColumnWidth));
  }, [rightColumnWidth]);
  const [draggingLeft, setDraggingLeft] = useState(false);
  const [draggingRight, setDraggingRight] = useState(false);
  useEffect(() => {
    if (!draggingLeft && !draggingRight) return;
    const handleMove = (e: MouseEvent) => {
      if (draggingLeft) {
        const next = Math.max(200, Math.min(600, e.clientX));
        setLeftSidebarWidth(next);
      } else if (draggingRight) {
        const next = Math.max(280, Math.min(700, window.innerWidth - e.clientX));
        setRightColumnWidth(next);
      }
    };
    const handleUp = () => { setDraggingLeft(false); setDraggingRight(false); };
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
  }, [draggingLeft, draggingRight]);

// Cross-link infrastructure (Chunk 6 polish, 2026-05-26). Fetch the
  // meeting's city cast roster so we can:
  //   1. Render speaker names in the Quotes section as clickable links to
  //      that member's Cast profile (when speaker_class='council_member'
  //      AND member_id resolves to a roster entry).
  //   2. Scan Key Decisions text for occurrences of canonical member names
  //      (or last-name forms) and wrap them as the same kind of link.
  //
  // The cast endpoint returns {members: [{id, seat_id, name, role, ...}, ...]}.
  // We build two lookup maps: by member_id (for Quotes) and by last-name
  // (for Key Decisions text scanning). Both populate from the same fetch.
  type CastMember = { id?: number; seat_id: string; name: string; role: string | null };
  const [castMembers, setCastMembers] = useState<CastMember[]>([]);
  useEffect(() => {
    const city = data?.city;
    if (!city) {
      setCastMembers([]);
      return;
    }
    const ctl = new AbortController();
    fetchForPlane(
      {
        publicPath: `/public-api/cast/${encodeURIComponent(city)}`,
        operatorPath: `/api/cast/${encodeURIComponent(city)}`,
      },
      { signal: ctl.signal },
    )
      .then(r => r.json())
      .then(payload => {
        if (ctl.signal.aborted) return;
        const members = Array.isArray(payload?.members) ? payload.members : [];
        setCastMembers(
          members
            .filter((m: any) => m && m.seat_id && m.name)
            .map((m: any) => ({
              id: m.id, seat_id: m.seat_id, name: m.name, role: m.role || null,
            })),
        );
      })
      .catch(err => {
        if (err.name === "AbortError") return;
        console.warn("cast roster fetch failed:", err);
        setCastMembers([]);
      });
    return () => ctl.abort();
  }, [data?.city]);

  // Lookup maps derived from castMembers. memberById lets the Quotes
  // section translate member_id → seat_id; namePatterns powers the Key
  // Decisions text scanner.
  const memberById = useMemo(() => {
    const m = new Map<number, CastMember>();
    castMembers.forEach(cm => {
      if (typeof cm.id === "number") m.set(cm.id, cm);
    });
    return m;
  }, [castMembers]);

  // Lookup by canonical name — preview quotes carry speaker_name as the
  // canonical roster form (per quote_extraction.md attribution rule) but
  // don't pre-resolve member_id, so the Key Quotes section uses this
  // case-insensitive name map to surface the cast-page cross-link.
  const memberByName = useMemo(() => {
    const m = new Map<string, CastMember>();
    castMembers.forEach(cm => {
      if (cm.name) m.set(cm.name.trim().toLowerCase(), cm);
    });
    return m;
  }, [castMembers]);

  // Build name-match patterns from the cast roster. Each member contributes
  // their full canonical name AND the last-word fallback (so "Councilmember
  // Stehly" in Key Decisions matches "Jamie Scott Stehly" on the roster).
  // Patterns are sorted longest-first so the regex prefers full-name matches
  // over last-name matches when both could apply.
  const memberNamePatterns = useMemo<Array<{ pattern: string; member: CastMember }>>(() => {
    const seen = new Set<string>();
    const out: Array<{ pattern: string; member: CastMember }> = [];
    for (const cm of castMembers) {
      const candidates = new Set<string>();
      candidates.add(cm.name);
      const parts = cm.name.split(/\s+/).filter(Boolean);
      if (parts.length > 1) candidates.add(parts[parts.length - 1]);
      for (const c of Array.from(candidates)) {
        const key = c.toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ pattern: c, member: cm });
      }
    }
    out.sort((a, b) => b.pattern.length - a.pattern.length);
    return out;
  }, [castMembers]);

  // Render a text string with bold markers (**foo**) + cast-name cross-links.
  // For each member-name match, wrap with a clickable span that navigates
  // to the cast-member view. Combines the bold-marker pass from
  // renderInlineBold with a member-name scan, in one walk.
  const renderInlineWithMemberLinks = (text: string, cityName: string | undefined | null): React.ReactNode => {
    if (!text) return null;
    const boldParts = text.split(/(\*\*[^*]+\*\*)/g);
    const out: React.ReactNode[] = [];
    let nodeKey = 0;
    for (const part of boldParts) {
      if (part.startsWith("**") && part.endsWith("**")) {
        out.push(
          <strong key={`b${nodeKey++}`} className="text-white font-semibold">
            {part.slice(2, -2)}
          </strong>,
        );
        continue;
      }
      // Non-bold span — scan for member name patterns
      if (!memberNamePatterns.length || !cityName || !onNavigate) {
        out.push(<span key={`p${nodeKey++}`}>{part}</span>);
        continue;
      }
      // Build a regex that matches any pattern, case-insensitive, on word boundaries
      const escaped = memberNamePatterns
        .map(p => p.pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
      const re = new RegExp(`\\b(${escaped.join("|")})\\b`, "gi");
      let lastIdx = 0;
      let match: RegExpExecArray | null;
      while ((match = re.exec(part)) !== null) {
        if (match.index > lastIdx) {
          out.push(<span key={`p${nodeKey++}`}>{part.slice(lastIdx, match.index)}</span>);
        }
        const matched = match[1];
        const lc = matched.toLowerCase();
        const entry = memberNamePatterns.find(p => p.pattern.toLowerCase() === lc);
        if (entry && cityName) {
          out.push(
            <button
              key={`l${nodeKey++}`}
              type="button"
              onClick={() => onNavigate("cast-member", {
                cityName,
                seatId: entry.member.seat_id,
              })}
              className="text-[#3B82F6] hover:text-[#60A5FA] underline-offset-2 hover:underline transition-colors cursor-pointer font-inherit"
              title={`Open ${entry.member.name}'s cast profile`}
            >
              {matched}
            </button>,
          );
        } else {
          out.push(<span key={`p${nodeKey++}`}>{matched}</span>);
        }
        lastIdx = match.index + matched.length;
      }
      if (lastIdx < part.length) {
        out.push(<span key={`p${nodeKey++}`}>{part.slice(lastIdx)}</span>);
      }
    }
    return out;
  };

  // D-157: standardized per-meeting-type suggested questions (neutral-by-
  // construction) replace the per-meeting-generated Q&A. `suggestedPairs`
  // itself stays display-only seed material — the member's BYOK panel uses
  // them as live-query seeds; public open-ended answering remains V2-locked
  // (D-145). Sets are claude_authored·awaits_review —
  // prompts/PROMPT_REVIEW_LEDGER § 2026-07-08.
  // ⚠️ D-186 (2026-07-31) supersedes-in-part D-157's "no per-meeting answers"
  // for the SIGNED-OUT surface only: three cited factual pre-generated Sonnet
  // answers now render for signed-out visitors via SignedOutSimQueryBody
  // (mounted separately below). Those use the SAME first-three of each
  // bucket's questions and store answers in the standalone episode_sim_queries
  // table; they never come through `suggestedPairs`. S-119 stays closed — the
  // sim-query selection excludes position-4 (the public-comment question) in
  // every bucket, and the sim_query_answer.md prompt carries a private-citizen
  // guard.
  const suggestedPairs = useMemo<SuggestedQA[]>(
    () =>
      suggestedQuestionsForTitle(data?.meeting_title).map(question => ({
        question,
        answer: null,
      })),
    [data?.meeting_title],
  );

  // Simulated Librarian ask (operator-directed 2026-08-05): a suggested-
  // question chip routes through the SAME chat flow a live BYOK query
  // uses — user bubble, KaraokeLoadingDots pending beat, then the
  // precomputed cited answer arrives as an assistant message. The visitor
  // experiences the Librarian's motion; only the synthesis is precomputed
  // (D-186). No live model call happens on this path.
  const simQueriesStateRef = useRef(simQueriesState.state);
  simQueriesStateRef.current = simQueriesState.state;
  const simAskTimeoutsRef = useRef<number[]>([]);
  useEffect(
    () => () => {
      for (const t of simAskTimeoutsRef.current) window.clearTimeout(t);
    },
    [],
  );
  const [askedSimIndices, setAskedSimIndices] = useState<number[]>([]);
  const simulateSuggestedAsk = (idx: number) => {
    const question = suggestedPairs[idx]?.question;
    if (!question) return;
    const pendingId = `sim-${idx}-${Date.now()}`;
    setAskedSimIndices(prev => (prev.includes(idx) ? prev : [...prev, idx]));
    setChatHistory(prev => [
      ...prev,
      { id: `${pendingId}-q`, role: "user", text: question },
      { id: pendingId, role: "assistant", text: "", pending: true },
    ]);
    // Staged reveal — long enough to read as retrieval + synthesis. The
    // answer is resolved AT reveal time (via the ref) so a click that
    // lands while the sim-queries fetch is still in flight picks up the
    // fetched answer instead of a stale miss.
    const resolveAnswer = (): string => {
      const state = simQueriesStateRef.current;
      const row =
        state.kind === "ready" ? state.simQueries[idx] : undefined;
      return row?.answer?.trim()
        ? row.answer
        : "Cited answers aren't available for this meeting.";
    };
    const delay =
      1400 + Math.min(1800, resolveAnswer().length * 6);
    const timeout = window.setTimeout(() => {
      setChatHistory(prev =>
        prev.map(m =>
          m.id === pendingId
            ? { ...m, text: resolveAnswer(), pending: false }
            : m,
        ),
      );
    }, delay);
    simAskTimeoutsRef.current.push(timeout);
  };

  // Corpo (corporate dark-mode) IS the canonical video_explainer prompt
  // (re-confirmed 2026-05-11: video_explainer.md's visual style is already
  // "Corporate Dark Mode" — see FUTURE_THOUGHTS.md § T-010).
  const corpoVideoUrl = outputs.video_explainer?.content_url || null;
  // Kawaii (playful illustrated) — secondary variant. Output type
  // `video_explainer_kawaii` to be produced once James authors the
  // matching Studio prompt (KAWAII visual style on EXPLAINER format).
  const kawaiiVideoUrl =
    (outputs as Record<string, { content_url?: string | null } | undefined>)
      .video_explainer_kawaii?.content_url || null;
  const summaryAudioUrl = outputs.audio_overview?.content_url || null;
  // Resolve the full-meeting URL preferring server-side (data.video_url,
  // populated from work_orders.youtube_video_url ?? meetings.video_url) over
  // the sidebar lookup. The server path is the load-bearing one for any
  // V1-RAG-3 indexed meeting whose row isn't in the active channel sidebar
  // (e.g. Bullhead trio m103225/m103224/m103223 — older parser IDs no longer
  // surfaced after the Codex-rewritten parser shipped).
  const resolvedVideoUrl = useMemo(() => {
    if (data?.video_url) return data.video_url;
    const meetingMatch = sidebarMeetings.find(m =>
      publicPlane ? m.public_id === publicId : m.id === meetingId,
    );
    return meetingMatch?.video_url || null;
  }, [data?.video_url, sidebarMeetings, meetingId, publicId, publicPlane]);
  const videoSource = useMemo(
    () => getVideoSource(resolvedVideoUrl),
    [resolvedVideoUrl]
  );
  // fullMeetingDirectUrl kept for downstream consumers (review-clip karaoke
  // helpers + the Open-Full-Meeting link) — they want the raw URL regardless
  // of embed kind.
  const fullMeetingDirectUrl = resolvedVideoUrl;

  // hasSummary tracks the player's overall "non-original-recording content
  // exists" state (used by the broader page chrome). Corpo and Kawaii each
  // have their own flag for the per-tab placeholder vs real video render.
  const hasCorpo = !!corpoVideoUrl;
  const hasKawaii = !!kawaiiVideoUrl;
  const hasSummary = hasCorpo || hasKawaii || !!summaryAudioUrl;
  const hasFull = !!videoSource;

  const sortedSidebar = useMemo(() => {
    return [...sidebarMeetings].sort((a, b) => {
      const da = a.meeting_date || "";
      const db = b.meeting_date || "";
      return db.localeCompare(da);
    });
  }, [sidebarMeetings]);

  // Show-page subtitle is now just the date. Notebook id moved into a
  // hover-info popover next to the date (click-to-copy for debug). Episode
  // number was dropped — meaningless to viewers, the URL has the id if
  // we ever need it.
  const subtitleDate = useMemo(
    () => (data?.meeting_date ? formatDateUpper(data.meeting_date) : null),
    [data?.meeting_date]
  );

  const [infoOpen, setInfoOpen] = useState(false);
  const [copiedNb, setCopiedNb] = useState(false);
  // Citation panel (i) drawer — opens from the top-right of the video
  // player. Slide-out panel that shows the full provenance tree for
  // this broadcast (source → transcription → extraction → verification →
  // corrections → human review → tracked claims). Anonymized public mode
  // by default; ?citation_audience=operator surfaces raw operator names.
  const [citationOpen, setCitationOpen] = useState(false);
  // Mobile sidebar visibility (md breakpoint = 768px). On desktop the
  // sidebar is always visible; on mobile it's hidden behind a hamburger
  // menu and slides in as an overlay.
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  // Auto-close the mobile sidebar overlay whenever the user navigates to a
  // different meeting (clicking an episode in the sidebar). On desktop this
  // is a no-op because the overlay never opened.
  // Also resets the active SyncedQuote karaoke player — it shouldn't
  // carry across meetings.
  useEffect(() => {
    setMobileSidebarOpen(false);
    setActiveBroadcastQuoteId(null);
  }, [meetingId]);
  const infoCloseTimer = useRef<number | null>(null);

  // Hover-with-grace pattern: opening is instant on mouseenter; closing
  // is delayed 150ms so the user can move from the icon onto the popover
  // (and back) without it disappearing. Mouseenter on either the icon or
  // the popover cancels any pending close.
  const cancelInfoClose = () => {
    if (infoCloseTimer.current !== null) {
      window.clearTimeout(infoCloseTimer.current);
      infoCloseTimer.current = null;
    }
  };
  const scheduleInfoClose = () => {
    cancelInfoClose();
    infoCloseTimer.current = window.setTimeout(() => setInfoOpen(false), 150);
  };
  useEffect(() => () => cancelInfoClose(), []);

  const copyNotebookId = async () => {
    if (!data?.notebook_id) return;
    try {
      await navigator.clipboard.writeText(data.notebook_id);
      setCopiedNb(true);
      // Keep the popover open so the user sees the green check land.
      cancelInfoClose();
      window.setTimeout(() => setCopiedNb(false), 1500);
    } catch {
      /* ignore */
    }
  };

  // Chat actions

  // playSuggestedQA (the per-meeting cached-answer replay) was removed with the
  // D-157 suggested_questions re-scope: for signed-in "suggested mode" the
  // chips render as a static read-only list of standardized per-type questions
  // (no per-meeting answers to replay), and the owner's live answering is the
  // BYOK panel above.
  // ⚠️ D-186 (2026-07-31) revives cached-answer replay for the SIGNED-OUT
  // surface only via SignedOutSimQueryBody — clicking a chip reveals a
  // pre-generated cited Sonnet answer from episode_sim_queries. That flow does
  // NOT go through this component's `chatHistory` state machine or invoke any
  // live LLM call; it fetches from /public-api/broadcasts/<public_id>/sim-queries.

  // Karaoke-citation seek. Dispatches per videoSource.kind: YouTube uses
  // postMessage against the Player API (iframe was built with enablejsapi=1);
  // direct-mp4 sets currentTime on the native <video> element; Granicus
  // MediaPlayer pages don't expose a postMessage API but the underlying
  // JWPlayer honors ?starttime=<sec> on URL load, so we rewrite the iframe
  // src — note this re-mounts the player, which the operator can see (brief
  // flash) but it's the only honest path for that surface.
  const seekVideoTo = (seconds: number) => {
    // PLAYER-1: per-kind dispatch (postMessage / currentTime / URL
    // rewrite / dead-embed external open) lives in the ZspanPlayer
    // shell + adapters now. External-link sources no-op — the citation
    // chip stays visible as a timecode reference in the answer text.
    playerRef.current?.seekTo(seconds, { andPlay: true });
  };

  const renderKeyDecision = (
    segments: KeyDecisionSegment[],
  ): React.ReactNode => {
    const displayText = segments.map(segment => segment.text).join("");

    return segments.map((segment, segmentIndex) => {
      const boundaries = new Set<number>([segment.charStart, segment.charEnd]);
      for (const bold of segment.boldRanges) {
        boundaries.add(bold.charStart);
        boundaries.add(bold.charEnd);
      }
      const points = Array.from(boundaries).sort((a, b) => a - b);
      const children: React.ReactNode[] = [];
      for (let i = 0; i < points.length - 1; i++) {
        const start = points[i];
        const end = points[i + 1];
        const slice = displayText.slice(start, end);
        const isBold = segment.boldRanges.some(bold => bold.charStart <= start && bold.charEnd >= end);
        let content: React.ReactNode = renderInlineWithMemberLinks(slice, data?.city);
        if (isBold) {
          content = <strong className="text-white font-semibold">{content}</strong>;
        }
        children.push(<Fragment key={`${start}-${end}`}>{content}</Fragment>);
      }

      if (segment.type === "core") {
        return <mark key={segmentIndex} className="kd-highlight-core">{children}</mark>;
      }
      if (segment.type === "nuance") {
        return <mark key={segmentIndex} className="kd-highlight-nuance">{children}</mark>;
      }
      return <Fragment key={segmentIndex}>{children}</Fragment>;
    });
  };

  // V1.5-OperatorSearch-1 Phase 4 — auto-seek when arriving from an
  // operator-search citation chip. The chip passes `initialSeek` as a
  // navigation param; we fire seekVideoTo once the video source is
  // resolved AND the DOM element is mounted. ~500ms wait for iframe/
  // video element to mount and start loading; the seekVideoTo's internal
  // querySelector handles the case where it isn't quite ready yet
  // (returns silently). A ref tracks "have we seeked yet for this
  // meeting load" so re-renders don't repeat the seek.
  const initialSeekDoneRef = useRef<string | null>(null);
  const disclaimerAcked = useDisclaimerAcked();
  useEffect(() => {
    if (typeof initialSeek !== "number" || !Number.isFinite(initialSeek)) return;
    if (!videoSource) return;
    // Hold the auto-seek (and its autoplay) until the disclaimer is
    // acknowledged — otherwise meeting audio starts UNDER the gate
    // modal (2026-07-07 session-38 audit finding; the pre-PLAYER-1
    // race usually lost the seek, masking this). The effect re-fires
    // when acked flips true, so the arrival still lands.
    if (!disclaimerAcked) return;
    const key = `${publicId ?? meetingId}:${initialSeek}`;
    if (initialSeekDoneRef.current === key) return;
    initialSeekDoneRef.current = key;
    const t = setTimeout(() => {
      seekVideoTo(initialSeek);
    }, 500);
    return () => clearTimeout(t);
  }, [initialSeek, videoSource, meetingId, publicId, disclaimerAcked]);

  // ── Render ────────────────────────────────────────────────────────

  if (publicCatalogFallback && publicId) {
    return <CatalogMeetingPlaceholder publicId={publicId} onBack={onBack} />;
  }

  // Approval gate (D-001 / D-031 / D-032). Public render is gated on the
  // work order's approved_at being non-null. Owner accounts auto-bypass
  // (they browse their own pre-publication site). RR-8 Tier C (2026-07-12):
  // the ?preview=true URL param no longer grants the bypass to a non-owner
  // — a shared/crafted link used to defeat the approval gate. The primary
  // notebook OUTPUTS are already server-gated on is_published, so dropping
  // the param-trust closes the client half; previewMode is now simply
  // isOwner (the param was redundant for owners and unsafe for non-owners).
  const previewMode = currentUser.isOwner;
  // Session-30 (2026-07-04) peek mode — operator terminal opens the
  // broadcast in an iframe modal for a fast visual sanity check before
  // hitting [Make Public →]. `?peek=1` strips the left channel sidebar
  // + right "Ask anything" chat column so only the actual generations
  // render — headers + video + key decisions + discussions
  // + community calls. Left/right rails aren't AI generations the
  // operator needs to verify; they'd just eat modal space.
  // RR-8 Tier C (2026-07-12): peek is owner-only (see previewMode above) —
  // the operator invokes it from the owner-gated terminal iframe. A
  // non-owner's crafted ?peek=1 no longer strips the rails.
  const isPeek =
    typeof window !== "undefined" &&
    new URLSearchParams(window.location.search).get("peek") === "1" &&
    currentUser.isOwner;
  const isApproved = !!data?.approved_at;
  // Mirrors the server's public-serving predicate: the meeting must be
  // published and have an approved work order. A canonical public_id and
  // publication timestamp are required as positive share-card inputs too.
  const isPubliclyServed = Boolean(
    data?.public_id &&
    data.approved_at &&
    currentPublishStatus?.is_published &&
    currentPublishStatus.published_at,
  );

  if (data && !isApproved && !previewMode && localMode) {
    return (
      <LocalProcessGate
        meetingId={data.meeting_id!}
        city={data.city || "—"}
        date={data.meeting_date || ""}
        title={data.meeting_title || "(untitled)"}
        onBack={onBack}
      />
    );
  }

  if (data && !isApproved && !previewMode) {
    return (
      <div className="h-screen w-full bg-[#0E0E10] flex flex-col items-center justify-center text-[#E4E4E5] font-sans antialiased px-8">
        <div className="max-w-xl w-full">
          <button
            onClick={onBack}
            className="group inline-flex items-center gap-1.5 text-gray-500 hover:text-white transition-colors mb-8"
            title="Back to channels"
          >
            <ArrowLeft className="w-3.5 h-3.5 group-hover:-translate-x-0.5 transition-transform" />
            <span className="text-[10px] font-semibold uppercase tracking-widest">
              All Channels
            </span>
          </button>

          {/* Session-31 (2026-07-04) — audit-fix. Prior copy leaked
             D-001 / D-028 / D-030 / D-031 / D-032 policy identifiers +
             "operator terminal", "[REVIEW]", "?preview=true" internals
             to public visitors as a D-054 schema-shape violation. Now:
             owner-signed-in sessions see the full internal breadcrumbs
             (they need to know which policy fired + how to unblock);
             anonymous visitors see a plain "not publicly available yet"
             message with no D-NNN identifiers. Same page, two audiences,
             appropriate context per audience. */}
          <p className="text-[11px] uppercase tracking-[0.2em] text-gray-500 mb-2">
            {currentUser.isOwner ? "Pending Human Review" : "Coming Soon"}
          </p>
          <h1 className="text-[22px] font-bold text-white mb-2">
            {data.city || "—"} · {data.meeting_date || ""}
          </h1>
          <p className="text-[14px] text-gray-400 mb-6">
            {data.meeting_title || "(untitled)"}
          </p>

          <div className="space-y-4 mb-6 text-[13px] text-gray-300 leading-relaxed">
            {currentUser.isOwner ? (
              <>
                <p>
                  This broadcast has not yet been approved through the operator
                  review gate. Per Z-SPAN's neutrality framework, no broadcast
                  renders publicly until a human has verified its outputs against
                  the source recording.
                </p>
                <p className="text-gray-400 text-[12px]">
                  Operator: open the operator terminal, find this work order, and
                  click <span className="font-mono text-[#3B82F6]">[REVIEW]</span>{" "}
                  to enter the two-gate verification flow (D-032). After approval,
                  this page renders normally.
                </p>
                <p className="text-gray-500 text-[11px]">
                  For pre-approval preview, append{" "}
                  <span className="font-mono text-gray-300">?preview=true</span>{" "}
                  to this URL.
                </p>
              </>
            ) : (
              <>
                <p>
                  This broadcast isn't publicly available yet. Every meeting on
                  Z-SPAN is reviewed by a human before it goes live — that
                  review is still in progress for this one.
                </p>
                <p className="text-gray-400 text-[12px]">
                  Check back soon, or browse other cities in the meantime.
                </p>
              </>
            )}
          </div>

          {currentUser.isOwner && (
            <div
              className="rounded-md p-4 text-[11px] text-gray-500 leading-relaxed"
              style={{ background: "rgba(255,255,255,0.03)" }}
            >
              Reference policies: D-001 (human review gate) · D-028 (verbatim
              quote provenance) · D-030 (own-surface architecture) · D-031
              (council_quotes replaces sentiment) · D-032 (two-gate review
              pattern).
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen w-full bg-[#0E0E10] flex text-[#E4E4E5] font-sans antialiased">

      {/* V1-Polish-5: one-time sign-in-benefits nudge for anonymous viewers. */}
      {showBenefitsToast && currentUser.signInEnabled && (
        <SignInBenefitsToast onDismiss={() => setShowBenefitsToast(false)} />
      )}

      {/* Mobile-only backdrop. Click closes the sidebar. */}
      {mobileSidebarOpen && (
        <div
          className="md:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm"
          onClick={() => setMobileSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* ── Left Sidebar ───────────────────────────────────────────
         Desktop: relative-positioned, always visible (340px wide column).
         Mobile (< md): fixed off-canvas; slides in via translate when
         mobileSidebarOpen=true. Same DOM tree both modes so episode
         click-through behavior is identical. */}
      <div
        style={{ width: leftSidebarWidth }}
        className={`bg-[#141416] border-r border-white/5 flex flex-col pt-6 pb-4 flex-shrink-0
                    fixed inset-y-0 left-0 z-50 transform transition-transform duration-300
                    md:relative md:translate-x-0 md:z-auto
                    ${mobileSidebarOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"}`}
      >

        {/* Sidebar header — refreshed away from <private-predecessor-repo>'s blocky
           Building2-icon-in-a-white-tile treatment toward a broadcast-bug
           feel. Back gets its own labeled affordance; the wordmark is
           typographic (no decorative icon — once we have a real Z-SPAN
           logo asset, drop it in where the wordmark sits). A tiny live
           broadcast dot punches in the city/channel line. */}
        <div className="px-6 pb-6 border-b border-white/5 mb-5">
          <div className="flex items-center justify-between mb-5">
            <button
              onClick={onBack}
              className="group inline-flex items-center gap-1.5 text-gray-500 hover:text-white transition-colors"
              title="Back to channels"
            >
              <ArrowLeft className="w-3.5 h-3.5 group-hover:-translate-x-0.5 transition-transform" />
              <span className="text-[10px] font-semibold uppercase tracking-widest">
                All Channels
              </span>
            </button>
            {/* Mobile-only close button (X). Desktop hides it because the
                sidebar is permanently visible there. */}
            <button
              type="button"
              onClick={() => setMobileSidebarOpen(false)}
              className="md:hidden text-gray-500 hover:text-white transition-colors p-1"
              aria-label="Close menu"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          <div className="flex items-center">
            {/* Highway-shield brand mark (2026-06-23) — replaces the prior
               wordmark pill + typographic fallback per James. zspan-shield.png
               is the transparent-bg version (Gemini-cleaned watermark-free
               base + transparency strip via PIL) — composites cleanly against
               the dark sidebar with no visible edge. */}
            <img
              src="/brand/zspan-shield.png"
              alt="Z-SPAN"
              className="h-12 w-12 object-contain flex-shrink-0"
              draggable={false}
            />
          </div>

          <div className="mt-3 flex items-baseline gap-2 min-w-0">
            <span className="text-[10px] font-bold uppercase tracking-widest text-gray-600 flex-shrink-0">
              CH
            </span>
            <span className="text-[12px] font-semibold uppercase tracking-[0.18em] text-gray-300 truncate">
              {data?.city ?? "Loading"}
            </span>
          </div>
          {data?.county && (
            <p className="mt-1 text-[10px] uppercase tracking-widest text-gray-600">
              {data.county} · Arizona
            </p>
          )}
        </div>

        {/* Meeting List (no scroll-area dependency — using overflow-auto) */}
        <div className="flex-1 px-4 overflow-y-auto custom-scrollbar">
          <div className="space-y-2 pb-6">
            {sortedSidebar.length === 0 ? (
              <div className="flex justify-center items-center h-32 text-gray-500 text-sm">
                No episodes cached for this channel yet.
              </div>
            ) : (
              sortedSidebar.map((meeting, index) => {
                const isSelected = publicPlane
                  ? meeting.public_id === publicId
                  : meeting.id === meetingId;
                return (
                  <button
                    key={meeting.public_id ?? meeting.id ?? index}
                    onClick={() => {
                      if (
                        publicPlane &&
                        meeting.public_id &&
                        meeting.public_id !== publicId &&
                        onNavigate
                      ) {
                        onNavigate("broadcast", { publicId: meeting.public_id });
                      } else if (meeting.id && meeting.id !== meetingId && onNavigate) {
                        onNavigate("broadcast", { meetingId: meeting.id });
                      }
                    }}
                    className={`w-full text-left p-6 rounded-xl transition-all duration-200 border border-transparent block relative
                      ${isSelected ? "bg-[#1C1C1E] shadow-xl" : "hover:bg-[#1A1A1C] hover:border-white/5"}`}
                  >
                    {isSelected && (
                      <div className="absolute left-0 top-6 bottom-6 w-1 bg-green-500 rounded-r-md" />
                    )}

                    <div className="flex text-[11px] font-semibold text-gray-500 tracking-wider uppercase mb-2">
                      {index === 0 ? (
                        <span className="flex">LATEST &bull; {formatDateUpper(meeting.meeting_date)}</span>
                      ) : (
                        <span>{formatDateUpper(meeting.meeting_date)}</span>
                      )}
                    </div>
                    <h3
                      className={`text-[15px] font-bold mb-2 leading-snug tracking-wide ${
                        isSelected ? "text-white" : "text-gray-300"
                      }`}
                    >
                      {meetingTypeFromTitle(meeting.meeting_title)}
                    </h3>
                    {(() => {
                      const tags = parseEpisodeTags(meeting.episode_tags);
                      if (tags.length > 0) {
                        return (
                          <div className="flex flex-wrap gap-1.5 mt-1.5">
                            {tags.map((t, ti) => {
                              const c = TAG_COLOR[t.category];
                              return (
                                <span
                                  key={ti}
                                  className="inline-flex items-center px-2 py-[2px] rounded-md text-[10px] font-semibold uppercase tracking-wider border"
                                  style={{
                                    color: c,
                                    backgroundColor: `${c}14`, // ~8% alpha
                                    borderColor: `${c}40`,     // ~25% alpha
                                  }}
                                >
                                  {t.text}
                                </span>
                              );
                            })}
                          </div>
                        );
                      }
                      return (
                        publicPlane ? (
                          meeting.episode_tagline ? (
                            <p className="text-[11px] text-gray-500 leading-relaxed mt-1.5 line-clamp-2">
                              {meeting.episode_tagline}
                            </p>
                          ) : null
                        ) : (
                        <p className="text-[11px] text-gray-600 italic font-medium leading-relaxed mt-1.5 tracking-wide">
                          Tags pending
                        </p>
                        )
                      );
                    })()}
                  </button>
                );
              })
            )}
          </div>
        </div>

      </div>

      {/* Resizable splitter between left sidebar + center column (desktop only). */}
      <div
        onMouseDown={() => setDraggingLeft(true)}
        className="hidden md:block w-1 cursor-col-resize bg-white/[0.02] hover:bg-white/15 active:bg-white/25 transition-colors flex-shrink-0"
        title="Drag to resize"
      />

      {/* ── Main Content + Right Chat Column ──────────────────────── */}
      <div className="flex-1 flex h-screen overflow-hidden bg-[#0F0F0F] min-w-0">

        {/* Center column — scrollable */}
        <div className="flex-1 overflow-y-auto custom-scrollbar min-w-0">
          <div className="max-w-[1100px] w-full mx-auto p-4 md:p-12">

            {/* Mobile-only hamburger that opens the left sidebar overlay.
                Desktop hides it (md: breakpoint) — the sidebar is always
                visible there, so a menu button would be pure noise. */}
            <button
              type="button"
              onClick={() => setMobileSidebarOpen(true)}
              className="md:hidden inline-flex items-center gap-2 mb-4 px-3 py-2 text-[11px] uppercase tracking-widest rounded-md border border-white/10 bg-[#141416] hover:bg-[#1a1a1c] text-gray-300 hover:text-white transition-colors"
              aria-label="Open episodes menu"
            >
              <Menu className="w-4 h-4" />
              <span>Episodes</span>
            </button>

            {loading && (
              <div className="flex items-center gap-3 text-gray-500 py-20 justify-center">
                <Loader2 className="w-5 h-5 animate-spin" />
                <span className="text-sm">Loading broadcast…</span>
              </div>
            )}

            {!loading && error && (
              <div className="rounded-2xl border border-white/5 bg-[#161618] p-12 text-center flex flex-col items-center">
                <picture className="block mb-4">
                  <img
                    src="/states/transmission-lost.png"
                    alt=""
                    aria-hidden="true"
                    className="h-32 w-auto opacity-80 select-none"
                    onError={e => {
                      const img = e.currentTarget;
                      img.style.display = "none";
                      const fallback = img.nextElementSibling as HTMLElement | null;
                      if (fallback) fallback.style.display = "block";
                    }}
                  />
                  <AlertCircle
                    className="w-8 h-8 mx-auto text-gray-500"
                    style={{ display: "none" }}
                  />
                </picture>
                <p className="text-[11px] uppercase tracking-[0.22em] text-gray-500 mb-2">
                  Transmission Lost
                </p>
                <p className="text-sm text-gray-400 mb-1">{error}</p>
                <p className="text-xs text-gray-600">
                  {publicPlane ? `Public ID: ${publicId}` : `Meeting ID: ${meetingId}`}
                </p>
              </div>
            )}

            {!loading && !error && data && (
              <>
                {/* Incomplete-broadcast band (F-7.1, 2026-07-06). Fires when
                   the server's completeness verdict says this broadcast is
                   below the publishable floor — normally impossible for
                   published meetings (publish_meeting() refuses), so in
                   practice this covers operator draft-previews and any
                   future force-through that bypasses the canonical gate.
                   Two audiences per D-054: visitors get plain language +
                   counts; owners additionally get the verdict's
                   plain-language reasons. */}
                {data.completeness && !data.completeness.complete && (
                  <div className="mb-6 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3">
                    <p className="text-[13px] text-amber-200/90 leading-relaxed">
                      This broadcast is incomplete — some sections couldn't be
                      generated
                      {typeof data.completeness.required_ok === "number" &&
                      typeof data.completeness.required_total === "number"
                        ? ` (${data.completeness.required_ok} of ${data.completeness.required_total} sections ready)`
                        : ""}
                      . The rest of this page shows what's available so far.
                    </p>
                    {currentUser.isOwner && !!data.completeness.reasons?.length && (
                      <ul className="mt-2 space-y-1 text-[12px] text-amber-200/60 list-disc list-inside">
                        {data.completeness.reasons.map((r, i) => (
                          <li key={i}>{r}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}

                {/* Header — title + date + info-popover. Synopsis paragraph
                   removed (felt like prompt-debug noise, not a viewer surface).
                   Notebook id is now hidden behind the (i) icon for debug
                   click-to-copy, not shown by default. */}
                <div className="mb-10">
                  <h2 className="text-4xl font-light tracking-wide text-white mb-3">
                    {meetingTypeFromTitle(data.meeting_title)}
                  </h2>
                  {(episodeTagline ||
                    (!publicPlane &&
                      currentUser.isOwner &&
                      outputs.episode_tagline)) &&
                    renderOperatorOutput(
                      "episode_tagline",
                      episodeTagline ? (
                        <p className="mb-3 max-w-3xl text-[15px] leading-relaxed text-gray-400">
                          {episodeTagline}
                        </p>
                      ) : (
                        <p className="mb-3 text-[12px] italic text-gray-600">
                          This generation contains no displayable headline.
                        </p>
                      ),
                    )}
                  <div className="flex items-center gap-3 flex-wrap">
                    {subtitleDate && (
                      <div className="text-[15px] text-gray-400 font-medium tracking-wide">
                        {subtitleDate}
                      </div>
                    )}
                    {/* Published badge. Identity retired 2026-07-09
                       (operator direction): the pill carries the state +
                       date only — "Published · <date>" — never a person.
                       The slot where an actor's name once rendered is the
                       future contributor-credit placeholder ("contributed
                       from <name>") when the decentralized-contribution
                       pipeline matures; until then the review discipline
                       still runs upstream (V1-Mod + T-013) and the D-006
                       publish gate flip is the mechanic. */}
                    {currentPublishStatus?.is_published &&
                      currentPublishStatus.published_at && (
                      <div
                        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-[var(--success-green)]/30 bg-[var(--success-green)]/10"
                        title={
                          // Session-31 (2026-07-04) audit-fix: the D-001 /
                          // D-032 policy identifier leak into the citizen-
                          // facing tooltip removed. Owner-only tooltip
                          // retains the full breadcrumb; public tooltip
                          // uses plain-language framing per D-054.
                          `Published` +
                          (currentPublishStatus.published_at
                            ? ` on ${currentPublishStatus.published_at}`
                            : "") +
                          (currentUser.isOwner
                            ? ". This broadcast went through the D-001 / D-032 human review gate before the publisher flipped it public."
                            : ". This broadcast was reviewed by a human before going live.")
                        }
                      >
                        <span className="kg-dot-active" style={{ width: 6, height: 6 }} />
                        <span className="text-[10px] font-semibold uppercase tracking-widest text-[var(--success-green)]">
                          Published
                          {currentPublishStatus.published_at
                            ? ` · ${new Date(currentPublishStatus.published_at.replace(" ", "T") + "Z").toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" })}`
                            : ""}
                        </span>
                      </div>
                    )}
                    {/* Legacy NotebookLM debug popover (notebook_id UUID + copy)
                       retired session-30 per operator: it duplicated info the
                       video's ⓘ citation trigger already covers with way more
                       depth (source, transcription, extraction, verification,
                       corrections, human review, tracked claims). notebook_id
                       was a NotebookLM-era artifact anyway (D-143 retired the
                       subsystem); Meeting ID surfaces via CitationPanel's
                       meeting.id field. */}
                    {/* V1-UI-1 per D-126: Audio Summary button is always
                       V2-locked at V1 posture, regardless of any legacy
                       NotebookLM-era cached audio still on disk. Rationale
                       (session-30): all studio media output is V2-deferred
                       per D-126, and letting legacy audio surface for
                       specific meetings while the button is locked for
                       everyone else was inconsistent — reader sees "coming
                       in V2" on some episodes and an active player on
                       others based only on when the meeting was processed.
                       Force the lock uniformly so the V2 posture is honest.
                       When V2 lands, unlock this by restoring the
                       summaryAudioUrl-conditional branch. */}
                    {/* Session-32 (2026-07-04) — (i) citation trigger moved
                       here from its previous top-right-of-video position.
                       Operator direction: it belongs between the date/
                       Published badge and the Audio Summary lock chip.
                       Sits in the header pill row with matching typography
                       weight so it reads as an inline utility, not a
                       floating action button. */}
                    <button
                      onClick={() => setCitationOpen(true)}
                      className="inline-flex items-center justify-center w-6 h-6 rounded-full border border-white/15 hover:border-white/40 bg-black/40 hover:bg-black/65 text-white/55 hover:text-white transition-colors"
                      aria-label="Open citation log for this broadcast"
                      title="Citation log — how this broadcast was made"
                    >
                      <span className="text-[12px] font-serif italic leading-none" aria-hidden="true">
                        i
                      </span>
                    </button>
                    <button
                      disabled
                      className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-white/5 bg-white/[0.02] text-[10px] uppercase tracking-widest text-white/30 cursor-not-allowed"
                      title="Coming in V2 — AI audio summary"
                    >
                      <Headphones className="w-3 h-3" />
                      Audio Summary
                      <Lock className="w-3 h-3 text-white/30 ml-0.5" />
                      <span className="text-[8px] uppercase tracking-widest text-white/30 leading-none -ml-0.5">v2</span>
                    </button>
                  </div>
                </div>

                {/* Video block — capped at 75% width so it sits more like a
                   show poster than a hero image. Tabs + video share the same
                   centered max-width so they stay visually attached. Header,
                   and key decisions below remain full-width. */}
                <div className="max-w-[75%] mb-12">

                {/* Browser-style tab strip — sits ABOVE the video, "emerging"
                   from the player. Session-32 (2026-07-04) — restyled to
                   mirror ChannelsPage's EPISODES/CAST/SCHEDULE binder tabs
                   (which protrude DOWN from the channel banner). Same
                   protrude aesthetic, mirrored direction. Clean gap-1
                   separation between tabs replaces the prior overlap +
                   z-index shuffling; simpler, and matches the reference
                   design operator flagged. */}
                <div
                  className="relative z-10 flex items-end gap-1 pl-1 -mb-px self-start"
                  role="tablist"
                  aria-label="Video source"
                >
                  {(() => {
                    // V1-UI-1 per D-126: "Corpo" retired (audio_overview /
                    // video_explainer / infographic all defer to V2). "Summary"
                    // (relabeled from "Kawaii") renders as a locked V2
                    // placeholder — visible to set expectations, not clickable.
                    // "Original" stays as the canonical Original-video tab,
                    // enabled when hasFull.
                    // Session-30 order swap: Original is the canonical
                    // primary tab (the enabled + active one at V1), so it
                    // reads leftmost. Summary sits second, still V2-locked.
                    const tabs: { id: VideoTab; label: string; icon: any; enabled: boolean; v2Locked: boolean }[] = [
                      { id: "full", label: "Original", icon: Youtube, enabled: hasFull, v2Locked: false },
                      { id: "summary", label: "Summary", icon: Film, enabled: false, v2Locked: true },
                    ];
                    return tabs.map((t) => {
                      const Icon = t.icon;
                      const active = videoTab === t.id;
                      return (
                        <button
                          key={t.id}
                          role="tab"
                          aria-selected={active}
                          onClick={() => t.enabled && setVideoTab(t.id)}
                          disabled={!t.enabled}
                          title={t.v2Locked ? "Coming in V2 — AI summary video" : undefined}
                          className={`flex items-center gap-1.5 px-4 pt-2 pb-3.5 rounded-t-lg border border-b-0 text-[10px] font-semibold uppercase tracking-[0.18em] transition-colors ${
                            active
                              ? "bg-[var(--surface-3)] text-white border-[var(--line-strong)]"
                              : t.enabled
                                ? "bg-[var(--surface)]/50 text-foreground/55 border-[var(--line)] hover:text-white hover:bg-[var(--surface-3)]/60"
                                : "bg-[var(--surface)]/50 text-foreground/25 border-[var(--line)] cursor-not-allowed"
                          }`}
                        >
                          <Icon className="w-3 h-3" />
                          {t.label}
                          {t.v2Locked && (
                            <>
                              <Lock className="w-3 h-3 text-white/30 ml-0.5" />
                              <span className="text-[8px] uppercase tracking-widest text-white/30 leading-none -ml-0.5">v2</span>
                            </>
                          )}
                        </button>
                      );
                    });
                  })()}
                </div>

                {/* Video — <private-predecessor-repo> aspect-[4/3] dark box. No floating tabs inside;
                   the tab strip above visually sprouts from this container's top edge. */}
                <div className="aspect-video rounded-2xl rounded-tl-none border border-white/10 bg-black flex flex-col relative overflow-hidden shadow-2xl">

                  {/* (i) citation trigger relocated session-32 (2026-07-04) —
                     now lives inline in the header pill row between the date
                     badge and the Audio Summary lock chip, matching operator
                     direction. See the header block above for the new
                     position. */}

                  {/* Summary panel (V1-UI-1 per D-126) — V2-locked placeholder.
                     Corpo + Kawaii panels retired; ALL three studio media
                     outputs (audio_overview / video_explainer / infographic)
                     defer to V2's Z-SPAN-controlled studio-pipeline rebuild.
                     This placeholder makes the deferral honest + visible
                     rather than hiding the surface. The tab itself is
                     non-clickable (enabled: false in the tabs array above),
                     so this panel only displays when the surface is forced
                     into "summary" state externally — kept for layout
                     consistency in case the future V2 build slots in here. */}
                  <div
                    className="absolute inset-0 flex items-center justify-center"
                    style={{ display: videoTab === "summary" ? "flex" : "none" }}
                  >
                    {/* Episode-card backplate (typographic, no AI-look —
                       see STYLIZATION_PASS.md). Slight darken on top so the
                       locked-V2 messaging reads cleanly on any card. */}
                    <img
                      src={episodeCardForTitle(data.meeting_title)}
                      alt=""
                      aria-hidden="true"
                      className="absolute inset-0 w-full h-full object-cover select-none pointer-events-none"
                      onError={e => {
                        const img = e.currentTarget;
                        if (!img.src.endsWith("/episodes/_default.png")) {
                          img.src = "/episodes/_default.png";
                        } else {
                          img.style.display = "none";
                        }
                      }}
                    />
                    <div className="absolute inset-0 bg-black/55 pointer-events-none" />
                    <div className="z-10 text-center space-y-4 relative px-6">
                      <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md border border-white/10 bg-white/[0.02] backdrop-blur-sm">
                        <Lock className="w-3.5 h-3.5 text-white/55" />
                        <span className="text-[10px] font-semibold text-white/65 tracking-[0.25em] uppercase">
                          AI Summary Video
                        </span>
                        <span className="text-[9px] uppercase tracking-widest text-white/45 leading-none">v2</span>
                      </div>
                      <p className="text-[12px] text-white/55 max-w-sm mx-auto leading-relaxed">
                        An AI-distilled video summary of this meeting — building
                        this carefully so the output matches the trust standard
                        the rest of the site holds itself to. Until then, the
                        Original recording is your primary.
                      </p>
                    </div>
                  </div>

                  {/* Full meeting panel */}
                  <div
                    className="absolute inset-0"
                    style={{ display: videoTab === "full" ? "block" : "none" }}
                  >
                    {/* PLAYER-1 (2026-07-07): the per-kind render dispatch
                       (YouTube iframe + embed-error overlay / mp4 <video> /
                       Granicus iframe / external panel) collapsed into the
                       ZspanPlayer shell — one surface, adapter-backed,
                       S-103 chain inside. seekVideoTo drives it via
                       playerRef. */}
                    <ZspanPlayer
                      ref={playerRef}
                      videoUrl={resolvedVideoUrl}
                      cityName={data?.city}
                      title="Full meeting recording"
                    />
                  </div>
                </div>
                </div>{/* end max-w-[75%] video block */}

                {(synopsisText ||
                  (!publicPlane && currentUser.isOwner && outputs.synopsis)) &&
                  renderOperatorOutput(
                    "synopsis",
                    <div className="mb-12">
                      <h3
                        className="text-[11px] font-bold uppercase tracking-widest mb-6 flex items-center gap-2 flex-wrap"
                        style={{ color: "var(--highway-sign-blue)" }}
                      >
                        <span>Synopsis</span>
                        {!publicPlane && (
                          <PromptInfoIcon promptName="synopsis" label="Synopsis" color="var(--highway-sign-blue)" />
                        )}
                        {data.meeting_id && (
                          <span className="inline-flex items-center" title="Z-SPAN provenance ribbon · click to verify">
                            <WatermarkRibbon
                              meetingId={data.meeting_id}
                              outputType="synopsis"
                              ribbonToken={outputs.synopsis?.ribbon_token}
                              registrationState={outputs.synopsis?.registration_state}
                            />
                          </span>
                        )}
                      </h3>
                      <p className="text-[15px] leading-relaxed text-gray-300 whitespace-pre-wrap">
                        {synopsisRaw
                          ? <KaraokeText text={synopsisRaw} onSeek={seekVideoTo} />
                          : synopsisText || "This generation contains no displayable synopsis."}
                      </p>
                    </div>,
                  )}

                {/* Key Decisions — UNDER the video */}
                {/* S-091 C4: gated by PublicDataDisclaimerGate per operator
                   2026-06-25 — "major stuff that surfaces" requires
                   disclaimer ack to view. Once acked (any surface, any
                   page), all gates silently render children. */}
                {(!publicPlane || outputs.key_decisions !== undefined) &&
                  renderOperatorOutput(
                    "key_decisions",
                    <PublicDataDisclaimerGate surfaceName="key_decisions">
                <div className="mb-12">
                  <h3
                    className="text-[11px] font-bold uppercase tracking-widest mb-6 flex items-center gap-2 flex-wrap relative"
                    style={{ color: "var(--highway-sign-blue)" }}
                  >
                    <span>Key Decisions</span>
                    {!publicPlane && (
                      <PromptInfoIcon promptName="key_decisions" label="Key Decisions" color="var(--highway-sign-blue)" />
                    )}
                    {data?.meeting_id && (
                      <span className="inline-flex items-center" title="Z-SPAN provenance ribbon · click to verify">
                        <WatermarkRibbon
                          meetingId={data.meeting_id}
                          outputType="key_decisions"
                          ribbonToken={outputs.key_decisions?.ribbon_token}
                          registrationState={outputs.key_decisions?.registration_state}
                        />
                      </span>
                    )}
                    {/* Recusal-alert icon (2026-06-24, D-132 era) — surfaces
                       at the section header when any council member recused
                       themselves from any vote during this meeting,
                       regardless of whether the underlying matter rose to
                       Key Decision tier-1 visibility. Operator-direction
                       2026-06-24: "recusal signals financial or some other
                       backdoor situation that people should be made aware
                       of." Click toggles a popover with speaker + matter +
                       rationale + citation. */}
                    {(recusalsPayload?.recusal_count ?? 0) > 0 && currentUser.isOwner && (
                      <>
                        <button
                          type="button"
                          onClick={() => setRecusalPopoverOpen(o => !o)}
                          aria-label={`${recusalsPayload?.recusal_count} recusal event${(recusalsPayload?.recusal_count ?? 0) === 1 ? "" : "s"} on this meeting`}
                          className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-rose-500/15 border border-rose-500/50 text-rose-400 hover:bg-rose-500/30 hover:border-rose-400 hover:text-rose-200 transition-colors"
                          title="Operator-only · Conflict-of-interest signal — click for details (V3 parking — S-080)"
                        >
                          <AlertCircle className="w-3.5 h-3.5" />
                        </button>
                        {recusalPopoverOpen && (
                          <div
                            className="absolute left-0 top-full mt-2 z-30 w-[420px] max-w-[90vw] rounded-lg border border-rose-500/40 bg-[#1a0e10] shadow-xl p-4 normal-case tracking-normal"
                            role="dialog"
                          >
                            <div className="flex items-center justify-between gap-3 mb-3">
                              <div className="flex items-center gap-2 flex-wrap">
                                <AlertCircle className="w-4 h-4 text-rose-400" />
                                <span className="text-[12px] font-semibold text-rose-200 uppercase tracking-widest">
                                  Recusal{(recusalsPayload?.recusal_count ?? 0) === 1 ? "" : "s"} on this meeting
                                </span>
                                <span className="text-[9px] font-mono normal-case tracking-normal text-amber-300/60 px-1.5 py-0.5 rounded border border-amber-500/30 bg-amber-500/10">
                                  operator-only · V3 park · S-080
                                </span>
                              </div>
                              <button
                                type="button"
                                onClick={() => setRecusalPopoverOpen(false)}
                                className="text-rose-300/60 hover:text-rose-200 text-sm font-bold"
                                aria-label="Close"
                              >
                                ×
                              </button>
                            </div>
                            <p className="text-[11px] text-rose-200/70 leading-relaxed mb-3">
                              A recusal is the procedurally-defined response to a disclosed conflict of interest. Surfaced at the meeting level — see <em>each event</em> below for the specific matter, speaker, and citation. <strong className="text-amber-200">Operator-only at this stage</strong> — the public-facing surface is parked at V3 per S-080.
                            </p>
                            <div className="space-y-3">
                              {(recusalsPayload?.recusals || []).map((ev, i) => (
                                <div key={i} className="rounded-md border border-rose-500/20 bg-rose-500/[0.04] p-3">
                                  <div className="text-[13px] font-semibold text-white mb-1">
                                    {ev.speaker_name || "Council member"}
                                    {ev.speaker_role && (
                                      <span className="text-white/55 font-normal ml-1.5">
                                        , {ev.speaker_role}
                                      </span>
                                    )}
                                  </div>
                                  <p className="text-[12px] text-rose-100/85 leading-relaxed mb-2">
                                    {ev.rationale}
                                  </p>
                                  {ev.raw_text && (
                                    <p className="text-[11px] text-rose-200/55 italic leading-relaxed mb-2">
                                      &ldquo;{ev.raw_text}&rdquo;
                                    </p>
                                  )}
                                  {ev.citation && (
                                    <div className="text-[10px] text-rose-300/55 font-mono">
                                      cite · {ev.citation.source}
                                      {typeof ev.citation.chunk_index === "number" && ` · chunk ${ev.citation.chunk_index}`}
                                      {typeof ev.citation.decision_index === "number" && ` · decision ${ev.citation.decision_index}`}
                                      {typeof ev.citation.video_timestamp_seconds === "number" && ` · t=${ev.citation.video_timestamp_seconds}s`}
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </>
                    )}
                    {/* Reading-style toggle — cycle three visual
                       renderings of Key Decisions body copy: hanging
                       (default outline shape) → plain (flat prose) →
                       reverse (first-line-indented). Label was
                       previously "Indent: hanging" which leaked the
                       raw CSS-value name; Sonnet UX audit flagged as
                       schema-shape leak (2026-07-04 session-31).
                       Plain-language label + tooltip that names what
                       clicking does. */}
                    <button
                      type="button"
                      onClick={cycleKdIndent}
                      className="ml-1 px-2 py-1 text-[12px] normal-case tracking-normal rounded border border-white/15 bg-white/[0.03] text-white/55 hover:border-white/35 hover:bg-white/[0.07] hover:text-white/80 transition-colors"
                      title="Change how these are laid out — cycle through three reading styles until one feels right"
                    >
                      Reading style
                    </button>
                    {/* Slice 4 (session-103): per-meeting share card for
                        signed-in visitors. In addition to the disclaimer,
                        the button requires the server-equivalent public
                        serving state and disappears in owner peek mode so
                        draft synthesis cannot cross into a shareable PNG. */}
                    {data?.public_id && (
                      <InfographDownloadButton
                        city={data.city ?? ""}
                        date={subtitleDate ?? data.meeting_date ?? ""}
                        title={data.meeting_title || "(untitled)"}
                        tagline={episodeTagline}
                        keyDecisions={effectiveKeyDecisions}
                        publicId={data.public_id}
                        publicUrl={canonicalBroadcastUrl(data.public_id)}
                        disclaimerAcknowledged={disclaimerAcked}
                        isPubliclyServed={isPubliclyServed}
                        isPeek={isPeek}
                      />
                    )}
                  </h3>

                  {!hasDecisionVerbatimEvidence && (
                    <p className="mb-6 text-[13px] text-gray-600 italic">
                      Transcript evidence isn't available for this meeting's generation — decisions summarized without inline citations.
                    </p>
                  )}

                  {/* New-discipline decisions render when the sidecar is
                     present for this meeting (production cutover
                     2026-06-24, D-132 follow-up — DEBUG toggle retired).
                     Reuses the existing splitKeyDecisionByHighlights
                     renderer so the <core>/<nuance> highlighter wash
                     stays identical to legacy production. */}
                  {previewDecisionsList.length > 0 ? (
                    (
                      <div className="space-y-8">
                        {previewDecisionsList.map((point, idx) => {
                          const segments = splitKeyDecisionByHighlights(point);
                          const audit = previewDecisionsPayload?.audit_json?.find(
                            a => a.index === idx + 1,
                          );
                          return (
                            <div key={`new-${idx}`} className="flex gap-5 items-start">
                              <img
                                src="/brand/key-decision-wax-seal.png"
                                alt=""
                                className="w-9 h-9 mt-0.5 flex-shrink-0 select-none"
                                draggable={false}
                              />
                              <div className="flex-1 min-w-0">
                                <DecisionEvidenceDisclosure
                                  evidence={(previewDecisionsPayload?.decisions || []).filter(
                                    decision => decision.index === idx + 1,
                                  )}
                                  color="var(--highway-sign-blue)"
                                >
                                  {trigger => (
                                    <p
                                      className={
                                        kdIndentMode === "hanging"
                                          ? "relative text-[15px] text-gray-200 leading-relaxed pl-[1.5rem] [text-indent:-1.5rem]"
                                          : kdIndentMode === "plain"
                                            ? "relative text-[15px] text-gray-200 leading-relaxed [text-indent:0]"
                                            : "relative text-[15px] text-gray-200 leading-relaxed [text-indent:1.5rem]"
                                      }
                                    >
                                      {renderKeyDecision(segments)}
                                      {trigger && <> {trigger}</>}
                                    </p>
                                  )}
                                </DecisionEvidenceDisclosure>
                                {/* news_values chips + audit rationale both
                                   retired session-30 (2026-07-04). Operator
                                   pass is a visual sanity check ("do the
                                   numbers and names look right?"), not a
                                   debug drill-down; the audit rationale
                                   italic surfaced internal prompt-thinking
                                   language that operator explicitly said
                                   he doesn't need for this pass. If a
                                   drill-down is warranted, the ⓘ citation
                                   log is the right surface. */}
                                {/* Discussion (N) — decision-bound quotes
                                   nested under this Key Decision, per the
                                   quote-router classification.
                                   Refactored 2026-06-24 (James) to match
                                   the KEY QUOTES rendering shape:
                                     - rationale-above-speaker layout
                                     - cast-page hyperlinking on the speaker
                                       name when memberByName resolves
                                     - SyncedQuote karaoke when word_timings
                                       + meeting URL are available
                                     - news_values + chunk citation gated
                                       to currentUser.isOwner (operator-only
                                       debug surfaces, NOT removed)
                                   Color theming stays amber per the
                                   surrounding KEY DECISIONS palette; the
                                   speaker link uses the same blue token
                                   KEY QUOTES uses so cast-link affordance
                                   reads consistently across the page. */}
                                {(() => {
                                  const quotes = decisionBoundQuotesByIndex[idx + 1] || [];
                                  if (quotes.length === 0) return null;
                                  return (
                                    <details className="mt-4 group">
                                      <summary className="cursor-pointer text-[11px] uppercase tracking-widest text-amber-200/55 hover:text-amber-100 transition-colors list-none flex items-center gap-1.5 select-none">
                                        <span className="inline-block transition-transform group-open:rotate-90 text-amber-200/40">▸</span>
                                        <span>{`Discussion (${quotes.length})`}</span>
                                      </summary>
                                      <div className="mt-3 ml-4 space-y-4 border-l border-amber-400/15 pl-4">
                                        {quotes.map((q, qi) => {
                                        const linkedMember = q.speaker_class === "council_member"
                                          ? memberByName.get((q.speaker_name || "").trim().toLowerCase()) ?? null
                                          : null;
                                        const navigateToMember = (e: React.MouseEvent) => {
                                          e.preventDefault();
                                          e.stopPropagation();
                                          if (linkedMember && data?.city && onNavigate) {
                                            onNavigate("cast-member", {
                                              cityName: data.city,
                                              seatId: linkedMember.seat_id,
                                            });
                                          }
                                        };
                                        const quoteKey = `discussion-${idx}-${qi}`;
                                        return (
                                          <details key={quoteKey} className="group/q">
                                            <summary className="cursor-pointer list-none select-none py-0.5">
                                              {q.speaker_class === "record" ? (
                                                <div className="flex items-baseline gap-x-2">
                                                  <span className="inline-block transition-transform group-open/q:rotate-90 text-amber-200/40 text-[10px] flex-shrink-0">▸</span>
                                                  <span className="text-[12px] text-amber-200/65 leading-relaxed">
                                                    In the record at {formatSeekLabel(q.video_timestamp_seconds)}
                                                  </span>
                                                </div>
                                              ) : (
                                                <>
                                                  {q.selection_rationale && (
                                                    <div className="flex items-baseline gap-x-2">
                                                      <span className="inline-block transition-transform group-open/q:rotate-90 text-amber-200/40 text-[10px] flex-shrink-0">▸</span>
                                                      <span className="text-[13px] text-amber-200/85 italic leading-relaxed">
                                                        ({q.selection_rationale})
                                                      </span>
                                                    </div>
                                                  )}
                                                  <div className="ml-4 mt-1 text-[12px]">
                                                    {linkedMember ? (
                                                      <button
                                                        type="button"
                                                        onClick={navigateToMember}
                                                        className="font-semibold underline-offset-2 hover:underline transition-colors cursor-pointer"
                                                        style={{ color: "var(--highway-sign-blue)" }}
                                                        title={`Open ${linkedMember.name}'s cast profile`}
                                                      >{q.speaker_name}</button>
                                                    ) : (
                                                      <span className="font-semibold text-white/90">{q.speaker_name}</span>
                                                    )}
                                                    {q.speaker_role && (
                                                      <span className="text-gray-400 font-normal">, {q.speaker_role}</span>
                                                    )}
                                                  </div>
                                                </>
                                              )}
                                            </summary>
                                            <div className="mt-2 ml-5 pb-2">
                                              {q.word_timings && q.word_timings.length > 0 && fullMeetingDirectUrl ? (
                                                <SyncedQuote
                                                  wordTimings={q.word_timings as any}
                                                  videoUrl={fullMeetingDirectUrl}
                                                  isActive={activeBroadcastQuoteId === quoteKey}
                                                  onActivate={() => setActiveBroadcastQuoteId(quoteKey)}
                                                  onDeactivate={() =>
                                                    setActiveBroadcastQuoteId(prev =>
                                                      prev === quoteKey ? null : prev,
                                                    )
                                                  }
                                                  markerColor="#3B82F6"
                                                />
                                              ) : (
                                                <p className="text-[13px] text-gray-200 leading-relaxed italic">
                                                  &ldquo;{q.quote_text}&rdquo;
                                                </p>
                                              )}
                                              {/* news_values chips retired session-30 (D-054 anti-pattern) */}
                                              {currentUser.isOwner && typeof q.chunk_index === "number" && (
                                                <p className="inline-flex rounded border border-[#F5A524]/30 bg-[#F5A524]/10 px-1.5 py-0.5 text-[10px] text-[#F5A524]/70 font-mono mt-1.5">
                                                  operator-only · chunk {q.chunk_index}
                                                  {typeof q.video_timestamp_seconds === "number" && ` · t=${q.video_timestamp_seconds}s`}
                                                </p>
                                              )}
                                            </div>
                                          </details>
                                        );
                                        })}
                                      </div>
                                    </details>
                                  );
                                })()}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )
                  ) : keyDecisions.length > 0 ? (
                    <div className="space-y-8">
                      {keyDecisions.map((point, idx) => {
                        const segments = splitKeyDecisionByHighlights(point);
                        return (
                          <div key={idx} className="flex gap-5 items-start">
                            {/* Wax seal marker replaces the prior 01./02. numbering
                                — position carries the ordering; the seal is the
                                presence marker. Decorative-only (alt=""). */}
                            <img
                                src="/brand/key-decision-wax-seal.png"
                                alt=""
                                className="w-9 h-9 mt-0.5 flex-shrink-0 select-none"
                                draggable={false}
                              />
                            {/* Hanging indent — first line reads as the headline
                                phrase; subsequent wrapped lines tuck inward via
                                text-indent: -1rem + padding-left: 1rem so the body
                                reads as elaboration beneath the headline.
                                Operator can toggle to plain-no-indent for A/B
                                comparison via the debug chip in the header. */}
                            <span
                              className="text-[14px] leading-relaxed text-gray-300"
                              style={
                                kdIndentMode === "hanging"
                                  ? { textIndent: "-1rem", paddingLeft: "1rem", display: "inline-block" }
                                  : kdIndentMode === "reverse"
                                  ? { textIndent: "1rem", display: "inline-block" }
                                  : { display: "inline-block" }
                              }
                            >
                              {renderKeyDecision(segments)}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    // V1-UI-1 follow-up per D-126: when the cached content is
                    // missing OR is legacy NotebookLM error/meta-response shape
                    // (parseNumberedList returns [] on non-numbered text), show
                    // a tiny placeholder instead of leaking the legacy error
                    // string. Once V1-RAG-3 lands the Qdrant + Sonnet backend
                    // swap in fetcher.py, fresh outputs overwrite the cache
                    // and this placeholder disappears naturally for new runs.
                    <p className="text-[13px] text-gray-600 italic">
                      {data.local_workspace === true &&
                      outputs.key_decisions !== undefined &&
                      outputs.key_decisions.gate_status === "empty" &&
                      !outputs.key_decisions.content?.trim()
                        ? "No formal decisions were verified for this meeting — the grounding check found none it could confirm against the transcript."
                        : "Awaiting RAG-generated content."}
                    </p>
                  )}
                </div>
                </PublicDataDisclaimerGate>,
                  )}

                {/* V1-CommunityCallsToAction-1 (2026-06-29) — verbatim
                   civic asks from officials directed at the public.
                   Renders between Key Decisions and Key Quotes per spec.
                   Honest-empty: when the parsed JSON yields zero calls
                   (procedural-only meeting, executive session, etc.),
                   the section hides entirely. Highway-amber accent
                   distinguishes the civic-action surface from the
                   blue news-quote surface below. */}
                {outputs.community_calls_to_action !== undefined &&
                  renderOperatorOutput(
                    "community_calls_to_action",
                    <CommunityCallsToActionSection
                      rawContent={outputs.community_calls_to_action?.content}
                      meetingId={data.meeting_id}
                      ribbonToken={outputs.community_calls_to_action?.ribbon_token}
                      registrationState={outputs.community_calls_to_action?.registration_state}
                      karaokeWordTimings={outputs.community_calls_to_action?.karaoke_word_timings}
                      videoUrl={fullMeetingDirectUrl}
                      activeQuoteId={activeBroadcastQuoteId}
                      onActiveQuoteChange={setActiveBroadcastQuoteId}
                    />,
                  )}

                {/* (What's Next + Council Sentiment sections both removed —
                   What's Next per James's earlier feedback, Council Sentiment
                   per the D-157 neutrality cut. Both still generate in the
                   pipeline; we just don't render them on this surface. The
                   show page now flows: video → Key Decisions → Community
                   Calls to Action.) */}

              </>
            )}

            {/* V1-locked suggestion box (2026-06-23). The suggestion form
               is V2-deferred per D-126
               (open-ended public input lands together when V2 unlocks the
               chat input + Audio Summary + Summary view-mode), so the form
               renders inline-locked with the same Lock + v2 engraving
               language as the chat input at the sidebar bottom. */}
            {!IS_SHOWCASE && data?.city && V1_PROCESSED_CITIES.has(data.city) && (
              <div className="mt-10 border-t border-white/5 pt-8">
                <div className="grid lg:grid-cols-2 gap-4 max-w-4xl">
                  {/* Left — V1-locked suggestion form (V2-deferred). */}
                  <div
                    className="rounded-lg border border-[#1A1A1D] bg-white/[0.015] p-4 opacity-60 cursor-not-allowed"
                    title="Coming in V2 — public query submissions"
                    aria-label="Suggest a query (coming in V2)"
                  >
                    <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/35 mb-2 flex items-center gap-1.5">
                      <span>Suggest a query</span>
                      <Lock className="w-3 h-3 text-white/30" />
                      <span className="text-[8px] uppercase tracking-widest text-white/30 leading-none">
                        v2
                      </span>
                    </div>
                    <textarea
                      rows={3}
                      disabled
                      readOnly
                      value=""
                      placeholder="Open-ended questions about this meeting…"
                      className="w-full rounded-md border border-[#1A1A1D] bg-white/[0.015] px-3 py-2 text-sm text-white/30 cursor-not-allowed resize-none placeholder:text-gray-700"
                    />
                    <div className="mt-2 flex items-center justify-between gap-3">
                      <span className="text-[10px] text-foreground/25">
                        Coming in V2
                      </span>
                      <button
                        type="button"
                        disabled
                        className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-xs font-medium text-white/30 cursor-not-allowed"
                      >
                        Submit
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* ── Right Column: AI chat (full height) ──────────────── */}
        {/* Resizable splitter between center column + right chat. */}
        <div
          onMouseDown={() => setDraggingRight(true)}
          className="hidden lg:block w-1 cursor-col-resize bg-white/[0.02] hover:bg-white/15 active:bg-white/25 transition-colors flex-shrink-0"
          title="Drag to resize"
        />
        <div
          style={{ width: rightColumnWidth }}
          className="hidden lg:flex flex-shrink-0 border-l border-white/5 bg-[#0F0F11] flex-col"
        >
          {/* S-091 C4: gated by PublicDataDisclaimerGate per operator
             2026-06-25 — the BYOK/notebook query surface is "the
             queryable stuff" the gate explicitly covers. Wraps the
             entire column content (header + chat input + responses);
             placeholder fills the column pre-ack. Resizable splitter
             stays OUTSIDE the wrapper so column-width adjust still
             works pre-ack. */}
          <PublicDataDisclaimerGate surfaceName="ai_chat_byok">

          {/* Librarian header — S-101 rebrand (session-30, 2026-07-04).
             Character-video hero at w-24 h-24 (96px) so the character
             carries actual presence rather than shrinking to an icon.
             mixBlendMode: screen composites over the site's dark surface
             (bg-[#0F0F11]) so the video's black background renders
             effectively transparent — no per-frame alpha preprocessing
             needed. The video plays while a query is in flight
             (librarianBusy=true from ByokQueryPanel's onSendingChange)
             and pauses+rewinds to frame 0 when the LLM starts producing
             / finishes.
             Subtitle uses the italic gray-500 body style operator liked
             from the pre-move ByokQueryPanel empty-state (moved up here
             so it's not duplicated below); the RAG DefinitionHint links
             to the NVIDIA blog under the same hover-tooltip pattern
             ChannelsPage uses for its Merriam-Webster civic vocab hints. */}
          {/* Librarian header restructured (session-30, 2026-07-04
             third pass). Full-width hero took too much column height;
             back to horizontal — video on left at w-40 h-40 (160px),
             title + tagline stack on right — matching the operator's
             red-square mockup. Auth pill still hides on broadcast view
             (see App.tsx) so no pt-16 clearance hack needed. Source mp4
             cleaned via ffmpeg crop=1080:720:0:0 (drops the Gemini
             watermark corner entirely, no drawbox black-patch trick)
             + lutyuv threshold clamping y<25 to 0 so darks stay pure
             black under mixBlendMode: screen composite (fixes the grey
             artifacts the prior v1 encode showed). */}
          <div className="px-6 py-5 border-b border-white/5 flex-shrink-0">
            <div className="flex items-center gap-4">
              <video
                ref={librarianVideoRef}
                src="/brand/librarian.mp4"
                muted
                loop
                playsInline
                preload="auto"
                className="w-40 h-40 flex-shrink-0"
                style={{ mixBlendMode: "screen", objectFit: "cover" }}
                aria-label="Librarian character illustration"
              />
              <div className="flex-1 min-w-0">
                {/* Session-30 Opus F3: tighter gap so the status dot reads
                   as bound to the "Librarian" label rather than orphaned. */}
                <div className="flex items-center gap-1.5">
                  <p className="text-[15px] font-semibold text-white/90 tracking-wide">
                    Librarian
                  </p>
                  {/* Session-32 (2026-07-04) — v3: the dot reflects
                     account-tier + client-side availability, not per-
                     meeting readiness. Operator direction: "someone even
                     seeing it means the meeting was published, we don't
                     need some deep heuristics thing for the librarian to
                     have an off/on state." Since publish-readiness is
                     enforced upstream by the publish gate, anyone viewing
                     this page is on a published broadcast. What actually
                     determines Librarian availability at the client:
                       (a) am I the owner (D-145 V1-locks the Librarian
                           to owner-only for now)
                       (b) do I have BYOK configured (the query surface
                           needs a user-side LLM key per D-133)
                     Green when both are true; grey otherwise. No
                     per-meeting introspection needed. */}
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${
                      activeByokConfig ? "bg-[#22C55E]" : "bg-gray-700"
                    }`}
                    title={
                      activeByokConfig
                        ? activeByokConfig.provider === LOCAL_WORKSPACE_PROVIDER
                          ? localLibrarian?.engine === "codex"
                            ? "Live — Librarian on duty (your Codex subscription, this machine — keyless)"
                            : "Live — Librarian on duty (your stored key, this machine)"
                          : "Live — Librarian on duty"
                        : localMode
                          ? "Run `zspan init` in the terminal (an API key or the installed Codex CLI both arm the Librarian)"
                          : currentUser.user
                            ? "Configure your API key to activate the Librarian"
                            : "Log in, then bring your own API key"
                    }
                  />
                </div>
                {/* Opus F2: tagline dropped to text-[12px] so it wraps to
                   3 lines instead of 4 — italic gray-500 kept per operator
                   preference (was Opus's alternative fix). */}
                <p className="text-[12px] text-gray-500 italic leading-relaxed mt-2">
                  Z-SPAN provides cited transcript chunks, your LLM provider handles the synthesis.
                  {" "}
                  <DefinitionHint
                    term="RAG"
                    definition="Retrieval-Augmented Generation — the AI's answer is grounded in real source material retrieved from a database (Z-SPAN's cited transcript chunks), not the model's memory alone."
                    sourceUrl="https://blogs.nvidia.com/blog/what-is-retrieval-augmented-generation/"
                    sourceLabel="NVIDIA ↗"
                    align="right"
                  />
                </p>
              </div>
            </div>
          </div>

          {/* V1.5-ByokPanel-Polish-1 (2026-06-25 follow-up): when BYOK is
              configured + chatMode is direct, ByokQueryPanel owns the entire
              area below the header — its internal chat-history + input + cost
              footer + session counter are a complete surface. The legacy
              chat-history area (cached suggestions + locked BYOK affordance)
              only renders when BYOK is NOT configured, so
              the suggestions don't stick above an active BYOK chat.
              Session-31 (2026-07-04) — added currentUser.isOwner gate to
              match the D-145 fallback at line ~2830. Previously any
              browser with an active in-memory byokConfig (owner tested key
              earlier + signed out, visitor pasted a key) would receive
              the live BYOK panel unconditionally. Sonnet UX audit caught
              this leak at tablet/desktop widths — mobile happened to
              avoid it because the panel is off-screen in the mobile
              layout, but the underlying gate was wrong at every width.
              Local-workspace mode (zspan CLI `open`) unlocks through
              activeByokConfig instead — the stored-key loopback path;
              the owner/D-145 gating above is flagship policy and the
              flagship never identifies as local-workspace. */}
          {activeByokConfig && chatMode === "direct" && librarianCanConfigure ? (
            <ByokQueryPanel
              meetingId={meetingId!}
              byokConfig={activeByokConfig}
              onOpenSettings={() => setByokModalOpen(true)}
              onCitationClick={seekVideoTo}
              suggestedQueries={suggestedPairs.slice(0, 3).map((p) => p.question)}
              onSendingChange={setLibrarianBusy}
              enforceInputGate={!currentUser.isOwner && Boolean(currentUser.user)}
            />
          ) : (
          <>
          <div className="flex-1 overflow-y-auto custom-scrollbar px-6 py-6 space-y-6">
            {chatHistory.length === 0 ? (
              <div className="space-y-5 pt-1">
                {signedOutPublicViewer && canonicalPublicId && (
                  // D-186: signed-out visitors see 3 suggested-question chips
                  // drawn from `suggestedPairs` (the D-157 standardized set,
                  // first 3 per meeting-type bucket). When precomputed cited
                  // answers exist for this meeting (via the useSignedOutSimQueries
                  // hook state above), clicking a chip reveals its answer
                  // inline via KaraokeText. When answers don't exist yet, the
                  // click shows "Cited answers aren't available for this
                  // meeting." as an inline response — never a bulk empty-state
                  // artifact. Deliberately renders INSIDE the existing chip
                  // structure the operator approved; no new component chrome.
                  <div className="space-y-3">
                    <p className="text-[10px] font-bold text-gray-500 uppercase tracking-widest px-1">
                      Questions worth asking
                    </p>
                    {/* Operator-directed 2026-08-05: clicking a chip no longer
                        reveals the answer inline — it submits through the same
                        chat flow the live Librarian uses (user bubble →
                        loading dots → cited answer), so the visitor feels the
                        real ask-and-answer motion. */}
                    <div className="flex flex-col gap-2">
                      {suggestedPairs.slice(0, 3).map((p, idx) => (
                        <button
                          key={idx}
                          type="button"
                          onClick={() => simulateSuggestedAsk(idx)}
                          className="text-left px-3 py-2 rounded-md border border-white/5 bg-[#1A1A1C] text-[12px] leading-snug text-gray-400 hover:border-[#22C55E]/35 hover:text-white transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#22C55E]/60"
                        >
                          {p.question}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {chatMode !== "direct" && (
                  <p className="text-[14px] leading-relaxed text-gray-400 font-medium max-w-[320px]">
                    These are the questions worth asking about this meeting.
                    Answering them here unlocks in V2 — for now, the answers
                    live in the record itself, one decision at a time.
                  </p>
                )}
              </div>
            ) : (
              chatHistory.map(m => (
                <div
                  key={m.id}
                  className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
                >
                  {m.role === "user" ? (
                    <div className="bg-[#2A2A2D] text-white px-5 py-3 rounded-2xl rounded-tr-sm text-[14px] font-medium max-w-[85%]">
                      {m.text}
                    </div>
                  ) : m.pending ? (
                    <KaraokeLoadingDots />
                  ) : m.error ? (
                    <div className="text-[13px] text-red-400 font-medium">{m.error}</div>
                  ) : (
                    <div className="text-[14px] leading-relaxed text-gray-300 font-medium pt-1 max-w-[95%] whitespace-pre-wrap">
                      {/* Cached answers may carry `[at MM:SS]` karaoke
                          citations. Render those as clickable seek pills;
                          otherwise strip legacy citation markers and
                          render plain text. */}
                      {m.text.includes("[at ")
                        ? <KaraokeText text={m.text} onSeek={seekVideoTo} />
                        : stripCitations(m.text)}
                    </div>
                  )}
                </div>
              ))
            )}
            {/* Operator-directed 2026-08-05: after a simulated ask answers,
                the not-yet-asked suggested questions stay reachable so the
                visitor can keep the conversation going — mirroring how a
                signed-in user can keep asking. Renders only mid-simulated-
                session (askedSimIndices non-empty), never during real BYOK
                chats, and never while an answer is still pending. */}
            {signedOutPublicViewer &&
              canonicalPublicId &&
              chatHistory.length > 0 &&
              askedSimIndices.length > 0 &&
              askedSimIndices.length < Math.min(3, suggestedPairs.length) &&
              !chatHistory.some(m => m.pending) && (
                <div className="space-y-2 pt-2">
                  <p className="text-[10px] font-bold text-gray-500 uppercase tracking-widest px-1">
                    More questions worth asking
                  </p>
                  <div className="flex flex-col gap-2">
                    {suggestedPairs.slice(0, 3).map((p, idx) =>
                      askedSimIndices.includes(idx) ? null : (
                        <button
                          key={idx}
                          type="button"
                          onClick={() => simulateSuggestedAsk(idx)}
                          className="text-left px-3 py-2 rounded-md border border-white/5 bg-[#1A1A1C] text-[12px] leading-snug text-gray-400 hover:border-[#22C55E]/35 hover:text-white transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#22C55E]/60"
                        >
                          {p.question}
                        </button>
                      ),
                    )}
                  </div>
                </div>
              )}
            <div ref={messagesEndRef} />
          </div>

          {/* Input area — direct mode is BYOK-only for signed-in members; suggested
             mode replays cached questions. There is no server-funded
             synthesis fallback: Z-SPAN retrieves cited transcript chunks,
             while the configured user provider performs synthesis. */}
          {chatMode === "direct" && !librarianCanConfigure ? (
            <LibrarianAccessGate />
          ) : chatMode === "direct" ? (
            localMode ? (
              // Local workspace, key not armed yet (`zspan open` on a
              // machine with no config.json). Not a V2 lock and not the
              // flagship's key modal — the honest local instruction: the
              // key stores via `zspan init` in the terminal, the same
              // file the Process button's cloud path writes.
              <div className="p-4 border-t border-white/5 flex-shrink-0">
                <div className="relative flex-1 h-12 rounded-full pl-5 pr-24 text-[14px] flex items-center bg-[#0E0E10] border border-[#1A1A1D] text-gray-500 select-none">
                  <span>Ask anything…</span>
                  <div className="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-1">
                    <KeyRound className="w-3.5 h-3.5 text-white/40" />
                    <span className="text-[8px] uppercase tracking-widest text-white/40 leading-none">your key</span>
                  </div>
                </div>
                <p className="text-[11px] text-gray-500 mt-2 px-1 leading-relaxed">
                  The Librarian answers with your own key, on this machine.
                  Run <span className="font-mono text-gray-400">zspan init</span>{" "}
                  in the terminal to store one, then reload this page.
                </p>
              </div>
            ) : currentUser.user ? (
              // V1.5-BYOK-Shell-1: clicking this locked surface opens the
              // BYOK modal. Reached when byokConfig is null — the user hasn't
              // brought a key yet or reloaded since entering it. When configured, the outer branch above
              // renders ByokQueryPanel instead.
              // D-145 (2026-07-01): this unlock path is owner-only now —
              // the public sees the V2 engraving in the final branch.
              <div
                className="p-4 border-t border-white/5 flex gap-3 items-center flex-shrink-0"
                aria-label="Ask anything — click to bring your own API key"
              >
                <button
                  type="button"
                  onClick={() => setByokModalOpen(true)}
                  className="relative flex-1 text-left h-12 rounded-full pl-5 pr-24 text-[14px] transition-colors cursor-pointer flex items-center bg-[#0E0E10] border border-[#1A1A1D] hover:border-white/20 text-gray-500"
                  title="Bring your own API key to unlock live queries against this meeting"
                >
                  <span>Ask anything…</span>
                  <div className="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-1">
                    <BrainCircuit className="w-4 h-4 text-gray-600" />
                    <Lock className="w-3 h-3 text-white/40" />
                    <span className="text-[8px] uppercase tracking-widest text-white/40 leading-none">BYOK</span>
                  </div>
                </button>
                <button
                  type="button"
                  onClick={() => setByokModalOpen(true)}
                  className="h-12 w-12 rounded-full shrink-0 flex items-center justify-center transition-colors bg-white/[0.04] text-white/40 hover:bg-white/[0.08] cursor-pointer"
                  title="Bring your own API key"
                >
                  <KeyRound className="w-4 h-4" />
                </button>
              </div>
            ) : (
              // D-145 (2026-07-01, Fable-5 audit F6): BYOK open-ended
              // querying re-locked to V2 for the public. Suggested
              // (pre-computed, cited, cached) queries remain the public
              // surface; open-ended returns after the in-house
              // query-safety research (S-119 — private-citizen guard et
              // al.) completes. Deliberately NOT clickable — no modal,
              // no unlock — matching the D-126 lock-engraving language.
              <div
                className="p-4 border-t border-white/5 flex gap-3 items-center flex-shrink-0"
                aria-label="Ask anything — log in"
              >
                <div className="relative flex-1 h-12 rounded-full pl-5 pr-24 text-[14px] flex items-center bg-[#0E0E10] border border-[#1A1A1D] text-gray-600 cursor-not-allowed opacity-60 select-none">
                  <span>Ask anything…</span>
                  <div className="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-1">
                    <Lock className="w-3 h-3 text-white/40" />
                    <span className="text-[8px] uppercase tracking-widest text-white/40 leading-none">v2</span>
                  </div>
                </div>
                <div
                  className="h-12 w-12 rounded-full shrink-0 flex items-center justify-center bg-white/[0.04] text-white/30 cursor-not-allowed opacity-60"
                  title="Log in, then bring your own API key."
                >
                  <Send className="w-4 h-4 ml-0.5" />
                </div>
              </div>
            )
          ) : (
            <div className="p-4 border-t border-white/5 flex-shrink-0">
              {/* D-157: standardized per-meeting-type questions render as a
                 static, read-only list. Public open-ended answering is
                 V2-locked (D-145), so these are neutral suggested queries —
                 not clickable chips that replay a per-meeting-generated answer.
                 The member's BYOK panel above uses the same set as live seeds.
                 ⚠️ D-186 (2026-07-31): a bounded per-meeting cached-answer
                 revival ships to SIGNED-OUT visitors via SignedOutSimQueryBody
                 (mounted in the scroll body above + on mobile below the video),
                 which fetches from /public-api/broadcasts/<public_id>/sim-queries.
                 THIS static list is reached only in signed-in "suggested" mode
                 (chatMode !== "direct") and remains intentionally read-only in
                 that surface — no dual clickable + static rendering for any
                 single user class. */}
              <p className="text-[10px] font-bold text-gray-500 uppercase tracking-widest mb-2 px-1">
                Questions worth asking
              </p>
              <div className="flex flex-col gap-2">
                {suggestedPairs.map((p, idx) => (
                  <div
                    key={idx}
                    className="text-left px-3 py-2 rounded-md border border-white/5 bg-[#1A1A1C] text-[12px] leading-snug text-gray-400"
                  >
                    {p.question}
                  </div>
                ))}
              </div>
              <p className="text-[10px] text-gray-600 mt-3 px-1 leading-relaxed">
                Answering these on the page unlocks in V2. For now, open any
                decision above to watch the exact moment it happened in the
                recording.
              </p>
            </div>
          )}
          </>
          )}
          </PublicDataDisclaimerGate>
        </div>
      </div>

      {/* Citation panel (slide-out drawer from the right edge). Trigger
         is the (i) icon in the top-right of the video player above.
         Renders nothing when closed (no DOM cost). */}
      <CitationPanel
        meetingId={meetingId}
        publicId={publicId}
        isOpen={citationOpen}
        onClose={() => setCitationOpen(false)}
      />

      {/* V1.5-BYOK-Shell-1 — onboarding modal triggered by clicking the
         BYOK-locked "Ask anything" surface. After successful validation
         the key remains in memory + onConfigured re-reads it into
         byokConfig state so the lock affordance switches to the
         "BYOK ✓" indicator. V1.5-Query-1 will then swap the locked surface
         entirely for the active Gemini-direct path. */}
      <ByokSetupModal
        open={byokModalOpen}
        onClose={() => {
          setByokModalOpen(false);
          // Re-read on close so a "clear key + start over" inside the modal
          // is reflected in the parent state even when the user doesn't
          // re-validate before closing.
          setByokConfig(getByokConfig());
        }}
        onConfigured={() => {
          setByokConfig(getByokConfig());
        }}
      />
    </div>
  );
}

export default function BroadcastPage({
  meetingId,
  publicId,
  onBack,
  onNavigate,
  initialSeek,
}: BroadcastPageProps) {
  if (publicId && isPublicPlane()) {
    return (
      <PublishedBroadcastPage
        publicId={publicId}
        onBack={onBack}
        onNavigate={onNavigate}
        initialSeek={initialSeek}
      />
    );
  }
  if (publicId) {
    return <CatalogMeetingPlaceholder publicId={publicId} onBack={onBack} />;
  }
  if (meetingId !== undefined) {
    return (
      <PublishedBroadcastPage
        meetingId={meetingId}
        onBack={onBack}
        onNavigate={onNavigate}
        initialSeek={initialSeek}
      />
    );
  }
  return null;
}

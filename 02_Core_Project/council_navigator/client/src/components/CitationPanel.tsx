/**
 * CitationPanel — the (i) citation log for a broadcast.
 *
 * Renders a slide-out drawer with the full provenance tree for this
 * broadcast: source, transcription, extraction, verification,
 * corrections, human review, and tracked claims. Two audience modes:
 *
 *   - "public" (default) — anonymized operator names ("An authorized
 *     Z-SPAN operator" instead of "James"). Safe for the open public
 *     broadcast page.
 *   - "operator" — raw operator names + extra fields (publish notes,
 *     applied_count on each correction). Enabled when the URL has
 *     ?citation_audience=operator. There's no UI toggle in this
 *     component on purpose — the operator path is a deploy-time
 *     decision, not a per-render switch the panel exposes.
 *
 * The (i) icon trigger lives separately in BroadcastPage; this
 * component is just the panel.
 */
import { useEffect, useState } from "react";
import { X, ChevronDown, ExternalLink, Copy, Check } from "lucide-react";
import { useCurrentUser } from "../hooks/useCurrentUser";
import { fetchForPlane } from "../lib/planeFetch";
import { isPublicPlane } from "../lib/trustPlane";

// Humanize a pipeline output_type slug for the citation panel (D-054: no raw
// snake_case field names on a public surface). Unmapped types fall back to a
// spaced, capitalized form so a new output_type never renders as a bare slug.
const OUTPUT_TYPE_LABELS: Record<string, string> = {
  synopsis: "Synopsis",
  newsletter: "Newsletter",
  key_decisions: "Key decisions",
  whats_next: "What's next",
  council_sentiment: "Council sentiment",
  tracked_claims: "Tracked claims",
  community_calls_to_action: "Community calls to action",
  episode_tagline: "Episode tagline",
  suggested_questions: "Suggested questions",
  quote_extraction: "Quote extraction",
  transcript_words: "Transcript words",
  audio_overview: "Audio overview",
  video_explainer: "Video explainer",
  infographic: "Infographic",
};
function humanizeOutputType(t: string): string {
  return (
    OUTPUT_TYPE_LABELS[t] ??
    t.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase())
  );
}

interface CitationTree {
  meeting: {
    id?: number;
    public_id?: string;
    city: string | null;
    county: string | null;
    state: string | null;
    title: string | null;
    date: string | null;
    time: string | null;
    location: string | null;
  };
  publication: {
    is_published: boolean;
    published_at: string | null;
    published_by?: string | null;
    publish_notes?: string | null;
  };
  sources: {
    primary_video: { url: string; platform: string } | null;
    agenda_url: string | null;
    agenda_packet_url: string | null;
    minutes_url: string | null;
    ecomment_url: string | null;
  };
  transcription: {
    method: string;
    generated_at: string;
    word_count: number | null;
    duration_seconds: number | null;
    primed_with_city_vocabulary?: boolean;
  } | null;
  extraction: {
    pipeline: string;
    prompt_review_ledger?: string;
    output_count: number;
    outputs: Array<{
      output_type: string;
      generated_at: string;
      prompt_filename: string | null;
      prompt_version: string | null;
      has_content: boolean;
    }>;
  };
  verification: {
    method: string;
    member_quotes: { total: number; by_status: Record<string, number> };
    auto_corrections_applied: number;
    per_quote_human_verifications: number;
  };
  corrections: {
    city_vocabulary_dictionary_size: number;
    corrections_dictionary: Array<{
      wrong: string;
      right: string;
      applied_count?: number;
      promoted_at?: string | null;
    }>;
  };
  human_review: {
    reviewer?: string | null;
    approved_at: string | null;
    policy_references?: string[];
  };
  tracked_claims?: {
    total: number;
    by_status: Record<string, number>;
  };
  audience_mode?: string;
}

interface CitationPanelProps {
  meetingId?: number;
  publicId?: string;
  isOpen: boolean;
  onClose: () => void;
}

// Format a UTC TIMESTAMP-style string into a local-friendly display.
function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    const iso = s.includes("T") ? s : s.replace(" ", "T") + "Z";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return s;
    return d.toLocaleString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return s;
  }
}

function fmtDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m ${s}s`;
  }
  return `${m}m ${s}s`;
}

function Section({
  label,
  children,
  defaultOpen = false,
  count,
}: {
  label: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
  count?: number | string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-b border-white/5">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between text-left px-5 py-3 hover:bg-white/[0.02] transition-colors group"
      >
        <div className="flex items-center gap-2.5 min-w-0">
          <span className="text-[10px] font-semibold uppercase tracking-[0.22em] text-foreground/55 group-hover:text-foreground/80 transition-colors">
            {label}
          </span>
          {count !== undefined && (
            <span className="text-[10px] text-foreground/40 tabular-nums">
              · {count}
            </span>
          )}
        </div>
        <ChevronDown
          className={`w-3.5 h-3.5 text-foreground/40 transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && <div className="px-5 pb-4 pt-1 text-[12px] text-foreground/75 leading-relaxed">{children}</div>}
    </div>
  );
}

export function CitationTrackedClaims({
  isOwner,
  trackedClaims,
}: {
  isOwner: boolean;
  trackedClaims?: CitationTree["tracked_claims"];
}) {
  if (!isOwner || !trackedClaims) return null;

  return (
    <Section label="Tracked claims" count={trackedClaims.total}>
      {trackedClaims.total === 0 ? (
        <p className="text-foreground/40 italic">
          No forward-looking claims extracted from this meeting yet.
        </p>
      ) : (
        <>
          <p className="text-foreground/50 text-[11px] leading-snug mb-2">
            Forward-looking statements made by officials in this meeting that
            someone could later check for fulfillment or contradiction. See the
            Cast page Accountability section or the city's full Ledger for each
            claim and its current status.
          </p>
          <div className="flex flex-wrap gap-2 text-[10px] uppercase tracking-widest">
            {Object.entries(trackedClaims.by_status).map(([status, n]) => (
              <span
                key={status}
                className="px-1.5 py-0.5 rounded-sm border border-white/10 tabular-nums"
              >
                {status} · {n}
              </span>
            ))}
          </div>
        </>
      )}
    </Section>
  );
}

function Row({
  k,
  v,
  mono = false,
}: {
  k: string;
  v: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex gap-3 py-1">
      <span className="text-[10px] uppercase tracking-[0.18em] text-foreground/40 flex-shrink-0 w-32 pt-0.5">
        {k}
      </span>
      <span className={`flex-1 min-w-0 ${mono ? "font-mono text-[11px]" : ""}`}>
        {v ?? <span className="text-foreground/30 italic">—</span>}
      </span>
    </div>
  );
}

function ExtLink({ href, label }: { href: string | null | undefined; label?: string }) {
  if (!href) return <span className="text-foreground/30 italic">—</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 text-[#3B82F6] hover:text-[#60A5FA] underline decoration-[#3B82F6]/30 hover:decoration-[#60A5FA]/60 transition-colors break-all"
    >
      {label || href}
      <ExternalLink className="w-3 h-3 flex-shrink-0" />
    </a>
  );
}

export default function CitationPanel({
  meetingId,
  publicId,
  isOpen,
  onClose,
}: CitationPanelProps) {
  const publicPlane = isPublicPlane();
  const [data, setData] = useState<CitationTree | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedJson, setCopiedJson] = useState(false);

  // Session-31 (2026-07-04) — auth-audit remediation. Prior code
  // honored `?citation_audience=operator` from any browser session,
  // returning de-anonymized reviewer names + raw internal
  // publish_notes + vocabulary corrections. The Flask endpoint now
  // gates the operator branch on owner cookie (returns 401 to
  // non-owners); this client-side check just avoids sending the
  // param at all when the caller isn't the owner, so we don't burn
  // a request that will 401.
  const currentUser = useCurrentUser();
  const audience =
    typeof window !== "undefined" &&
    currentUser.isOwner &&
    new URLSearchParams(window.location.search).get("citation_audience") === "operator"
      ? "operator"
      : "public";

  useEffect(() => {
    if (!isOpen) return;
    if (publicPlane && !publicId) {
      setError("This public citation does not have a public id.");
      setLoading(false);
      return;
    }
    let aborted = false;
    setLoading(true);
    setError(null);
    fetchForPlane({
      publicPath: `/public-api/broadcasts/${encodeURIComponent(publicId || "")}/citation`,
      operatorPath: `/api/citation/${meetingId}?audience=${audience}`,
    })
      .then(r => r.json())
      .then(body => {
        if (aborted) return;
        if (body?.success && body?.citation) {
          setData(body.citation);
        } else {
          setError(body?.error || "Failed to load citation log.");
        }
        setLoading(false);
      })
      .catch(e => {
        if (aborted) return;
        setError(e?.message || "Network error.");
        setLoading(false);
      });
    return () => {
      aborted = true;
    };
  }, [isOpen, meetingId, publicId, audience, publicPlane]);

  const copyJson = async () => {
    if (!data) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(data, null, 2));
      setCopiedJson(true);
      setTimeout(() => setCopiedJson(false), 1500);
    } catch {
      // best-effort
    }
  };

  if (!isOpen) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden="true"
      />
      {/* Drawer */}
      <aside
        className="fixed top-0 right-0 bottom-0 z-50 w-full max-w-md bg-[#0E0E10] border-l border-white/10 shadow-2xl flex flex-col text-foreground"
        role="dialog"
        aria-label="Citation log"
      >
        {/* Header */}
        <div className="flex items-start justify-between px-5 py-4 border-b border-white/10">
          <div className="min-w-0">
            <p className="text-[10px] uppercase tracking-[0.22em] text-foreground/45">
              Citation Log
            </p>
            <h2 className="text-base font-semibold text-white mt-1 leading-tight">
              How this broadcast was made
            </h2>
            {data && (
              <p className="text-[11px] text-foreground/45 mt-1.5 leading-snug">
                Every claim in this broadcast traces back to verifiable sources. The
                chain below shows where each piece came from, how it was
                verified, and who approved it for publication.
              </p>
            )}
            {data?.audience_mode && (
              <p className="text-[9px] uppercase tracking-[0.22em] text-foreground/30 mt-2 tabular-nums">
                {data.audience_mode}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-foreground/40 hover:text-white transition-colors p-1 -m-1 flex-shrink-0"
            aria-label="Close citation log"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto custom-scrollbar">
          {loading && (
            <div className="px-5 py-8 text-center text-foreground/50 text-[12px]">
              Loading citation log…
            </div>
          )}
          {error && (
            <div className="px-5 py-8 text-center text-red-400 text-[12px]">
              {error}
            </div>
          )}
          {data && (
            <>
              <Section label="Source" defaultOpen={true}>
                <Row k="Video" v={<ExtLink href={data.sources.primary_video?.url} />} />
                <Row k="Platform" v={data.sources.primary_video?.platform} />
                <Row k="Agenda" v={<ExtLink href={data.sources.agenda_url} label="View agenda" />} />
                <Row k="Minutes" v={<ExtLink href={data.sources.minutes_url} label="View minutes" />} />
                {data.sources.agenda_packet_url && (
                  <Row k="Agenda packet" v={<ExtLink href={data.sources.agenda_packet_url} label="View packet" />} />
                )}
                {data.sources.ecomment_url && (
                  <Row k="eComment" v={<ExtLink href={data.sources.ecomment_url} label="View public comment" />} />
                )}
              </Section>

              <Section
                label="Transcription"
                count={data.transcription?.word_count ?? undefined}
              >
                {data.transcription ? (
                  <>
                    <Row k="Method" v={data.transcription.method} />
                    <Row k="Generated" v={fmtDate(data.transcription.generated_at)} />
                    <Row k="Words" v={data.transcription.word_count?.toLocaleString() ?? "—"} />
                    <Row k="Duration" v={fmtDuration(data.transcription.duration_seconds)} />
                    {data.transcription.primed_with_city_vocabulary && (
                      <Row
                        k="Vocab priming"
                        v="Whisper primed with city-specific civic vocabulary + council member names"
                      />
                    )}
                    <p className="text-foreground/40 text-[11px] mt-2 leading-snug">
                      Whisper produces an independent word-level transcript of the source
                      audio. Each broadcast quote is anchored back to a specific
                      timestamp in this transcript via the karaoke alignment layer.
                    </p>
                  </>
                ) : (
                  <p className="text-foreground/40 italic">No transcription on file.</p>
                )}
              </Section>

              <Section label="Extraction" count={data.extraction.output_count}>
                <Row k="Pipeline" v={data.extraction.pipeline} />
                {data.extraction.prompt_review_ledger && (
                  <Row k="Prompt review" v={data.extraction.prompt_review_ledger} mono />
                )}
                <div className="mt-2 space-y-0.5 max-h-48 overflow-y-auto">
                  {data.extraction.outputs.map(o => (
                    <div
                      key={o.output_type}
                      className="flex items-center gap-2 text-[11px] py-0.5"
                    >
                      <span className="text-foreground/70 flex-shrink-0 w-40 truncate">
                        {humanizeOutputType(o.output_type)}
                      </span>
                      <span className="text-foreground/40 flex-shrink-0">
                        {fmtDate(o.generated_at).split(",")[0]}
                      </span>
                      {!o.has_content && (
                        <span className="text-amber-400/70 text-[10px] uppercase tracking-widest">
                          empty
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </Section>

              <Section
                label="Verification"
                count={data.verification.member_quotes.total}
              >
                <Row k="Method" v={data.verification.method} />
                <Row
                  k="Quote breakdown"
                  v={
                    <div className="flex flex-wrap gap-2 text-[10px] uppercase tracking-widest">
                      {Object.entries(data.verification.member_quotes.by_status).map(
                        ([status, n]) => (
                          <span
                            key={status}
                            className="px-1.5 py-0.5 rounded-sm border border-white/10 tabular-nums"
                          >
                            {status} · {n}
                          </span>
                        )
                      )}
                    </div>
                  }
                />
                <Row
                  k="Auto-corrections"
                  v={`${data.verification.auto_corrections_applied} text correction${data.verification.auto_corrections_applied === 1 ? "" : "s"} applied mechanically`}
                />
                {data.verification.per_quote_human_verifications > 0 && (
                  <Row
                    k="Per-quote human"
                    v={`${data.verification.per_quote_human_verifications} quote${data.verification.per_quote_human_verifications === 1 ? "" : "s"} spot-checked individually`}
                  />
                )}
              </Section>

              <Section
                label="Corrections"
                count={data.corrections.city_vocabulary_dictionary_size}
              >
                <p className="text-foreground/50 text-[11px] leading-snug mb-2">
                  The city's vocabulary-corrections dictionary, applied to this
                  broadcast's generated text. Each entry was surfaced by a human
                  reviewer during a prior meeting's verification round; future
                  generations apply them automatically (T-017 Layer 2).
                </p>
                <div className="space-y-1 max-h-48 overflow-y-auto">
                  {data.corrections.corrections_dictionary.length === 0 ? (
                    <p className="text-foreground/40 italic">No dictionary entries yet.</p>
                  ) : (
                    data.corrections.corrections_dictionary.map((c, i) => (
                      <div
                        key={i}
                        className="text-[11px] font-mono flex items-center gap-2 py-0.5"
                      >
                        <span className="text-red-400/70 line-through truncate flex-shrink min-w-0">
                          {c.wrong}
                        </span>
                        <span className="text-foreground/30">→</span>
                        <span className="text-emerald-400/85 truncate flex-shrink min-w-0">
                          {c.right}
                        </span>
                        {c.applied_count !== undefined && (
                          <span className="text-foreground/30 ml-auto text-[10px] tabular-nums">
                            x{c.applied_count}
                          </span>
                        )}
                      </div>
                    ))
                  )}
                </div>
              </Section>

              <Section label="Human review" defaultOpen={true}>
                {!publicPlane && (
                  <Row
                    k="Reviewer"
                    v={
                      data.human_review.reviewer || (
                        <span className="text-amber-400/85 italic">Not yet reviewed</span>
                      )
                    }
                  />
                )}
                <Row k="Approved at" v={fmtDate(data.human_review.approved_at)} />
                <Row
                  k="Published at"
                  v={fmtDate(data.publication.published_at)}
                />
                {data.publication.publish_notes && (
                  <Row k="Publish note" v={data.publication.publish_notes} />
                )}
                {!publicPlane && (
                  <Row
                    k="Policy"
                    v={
                      <div className="flex flex-wrap gap-1.5">
                        {(data.human_review.policy_references ?? []).map(p => (
                          <span
                            key={p}
                            className="text-[10px] font-mono text-foreground/60 px-1.5 py-0.5 rounded-sm border border-white/10"
                          >
                            {p}
                          </span>
                        ))}
                      </div>
                    }
                  />
                )}
                <p className="text-foreground/40 text-[11px] mt-3 leading-snug">
                  Z-SPAN's neutrality framework requires a human operator to verify
                  each broadcast against the source recording before publication.
                  No broadcast renders publicly until that gate has cleared.
                </p>
              </Section>

              <CitationTrackedClaims
                isOwner={currentUser.isOwner}
                trackedClaims={data.tracked_claims}
              />
            </>
          )}
        </div>

        {/* Footer — copy the full JSON for audit */}
        {data && (
          <div className="border-t border-white/10 px-5 py-3 flex items-center justify-between">
            <p className="text-[10px] text-foreground/40 leading-snug max-w-[60%]">
              The full citation tree is available as JSON for audit or for an
              independent verifier to re-check sources.
            </p>
            <button
              onClick={copyJson}
              className="text-[10px] uppercase tracking-widest text-foreground/55 hover:text-white border border-white/10 hover:border-white/30 px-3 py-1.5 inline-flex items-center gap-1.5 transition-colors"
            >
              {copiedJson ? (
                <>
                  <Check className="w-3 h-3 text-green-400" />
                  Copied
                </>
              ) : (
                <>
                  <Copy className="w-3 h-3" />
                  Copy JSON
                </>
              )}
            </button>
          </div>
        )}
      </aside>
    </>
  );
}

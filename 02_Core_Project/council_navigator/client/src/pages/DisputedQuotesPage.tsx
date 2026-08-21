/**
 * DisputedQuotesPage — T-013 V4 operator review surface.
 *
 * Lists every `quotes` row with verified_status='disputed' and lets the
 * operator either Verify (optionally after editing the text) or Reject.
 * Disputed quotes are hidden from public Cast page + BroadcastPage
 * (D-043 addendum) until resolution.
 *
 * Design principle (2026-05-26 redesign per operator feedback): this is
 * for a human to scan + decide quickly. NOT a database-row view. So:
 *   - One sentence of plain language for what Gemini noticed (no
 *     uppercase technical-field labels)
 *   - Quote text shown ONCE (via SyncedQuote — karaoke + play). The
 *     editable textarea below is a different surface (editing, not
 *     display) so it doesn't read as duplication.
 *   - Always-editable textarea pre-filled with current text. No
 *     two-step "click [RESOLVE] to expand."
 *   - One primary action (Verify, prominent green) + one escape
 *     (Reject, subtle red). No [Cancel] needed since there's no
 *     two-step mode to cancel.
 *   - Sans-serif body, readable sizes (14-15px), generous spacing.
 *
 * Aesthetic: flat / utilitarian, but human-oriented. The marker karaoke
 * remains reserved for the T-012 accountability surfaces (preserved
 * evidence) — not used here (review queue, not evidence display).
 *
 * Cf. the PublishConfirmModal redesign for the same principle applied
 * to publication moment. Same direction: status-as-status, not
 * checkboxes-pretending-to-verify-things-the-operator-cannot-verify.
 */
import { ReactNode, useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowUpRight, AlertTriangle, Check, X } from "lucide-react";
import SyncedQuote from "../components/SyncedQuote";

interface DisputedQuotesPageProps {
  onBack: () => void;
  onNavigate?: (view: string, params?: Record<string, unknown>) => void;
}

type WordTiming = { word: string; start_ms: number; end_ms: number };

type GeminiVerdict = {
  speaker_attribution?: string;
  speaker_attribution_notes?: string;
  text_accuracy?: string;
  text_differences?: string;
  clip_integrity?: string;
  other_concerns?: string;
};

type DisputedQuote = {
  id: number;
  member_id: number | null;
  meeting_id: number;
  meeting_title: string;
  meeting_date: string;
  city_name: string;
  speaker_name: string;
  speaker_role: string | null;
  speaker_class?: string | null;
  seat_id: string | null;
  is_broadcast_hero?: number | null;
  quote_text: string;
  quote_text_original: string | null;
  // D-054 readability polish (gpt-4o-mini, lazy-computed by
  // `parsers/quote_cleaner.py § polish_for_display`). When set, the
  // textarea pre-fills with this form so the reviewer scans a readable
  // sentence rather than the verbatim transcript. On Verify, whatever
  // is in the textarea (polished, unchanged, or operator-edited)
  // becomes the new `quote_text`; the verbatim is preserved in
  // `quote_text_original`.
  quote_text_display: string | null;
  // D-054 verdict-emphasis (gpt-4o-mini, lazy-computed by
  // `parsers/verdict_emphasis.py § extract_verdict_emphasis`). Short
  // substrings the reviewer's eye should catch — wrapped in red
  // inside the humanized verdict note above the textarea.
  verdict_emphasis_tokens: string[];
  topic_tags: string[];
  word_timings: WordTiming[] | null;
  video_timestamp_seconds: number | null;
  meeting_video_url: string | null;
  verified_status: string;
  verified_by: string | null;
  verified_at: string | null;
  gemini_verdict: GeminiVerdict | null;
  operator_resolution: unknown;
  // D-057 extension — agent counter-proposal (Disputed Quotes Reviewer
  // and future Opus judgment agents on `quotes`). When the cleaner +
  // verifier output is wrong but the agent has a clear better
  // alternative, it records the proposal here. The UI surfaces the
  // agent's text + reasoning above the textarea; the textarea pre-fills
  // with the agent's value (overriding the polished form) when present.
  // Slack ✨ reaction applies via `resolve` with the agent's value.
  agent_proposed_quote_text: string | null;
  agent_reasoning: string | null;
  agent_proposed_by: string | null;
  agent_proposed_at: string | null;
};

type ListPayload = {
  city: string | null;
  count: number;
  disputed_quotes: DisputedQuote[];
};

function formatMeetingDate(s: string | null | undefined): string {
  if (!s) return "—";
  const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s + "T00:00:00") : new Date(s);
  if (isNaN(d.getTime())) return s;
  return d.toLocaleDateString("en-US", {
    month: "short", day: "numeric", year: "numeric",
  });
}

/**
 * Wrap any matching emphasis-token substrings inside the humanized
 * verdict text with a red span so the reviewer's eye catches the
 * substantive bits at a glance. Tokens come from
 * `parsers/verdict_emphasis.py § extract_verdict_emphasis` (gpt-4o-mini,
 * lazy-computed and cached). Token list is case-sensitive verbatim
 * substring matches; we sort longest-first so "for the overtime" wins
 * the split over "the".
 *
 * If `tokens` is empty (LLM unavailable, no emphasis worth applying, or
 * cache miss), the text renders unchanged.
 */
function renderWithEmphasis(text: string, tokens: string[]): ReactNode {
  if (!tokens || tokens.length === 0) return text;
  const sorted = [...tokens].sort((a, b) => b.length - a.length);
  const escaped = sorted.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`(${escaped.join("|")})`, "g");
  const parts = text.split(re);
  const tokenSet = new Set(sorted);
  return parts.map((part, i) =>
    tokenSet.has(part) ? (
      <span
        key={i}
        className="text-[#F87171] font-semibold"
      >
        {part}
      </span>
    ) : (
      part
    )
  );
}

/**
 * Take Gemini's structured verdict + render a single plain-language
 * sentence about what's worth knowing for resolution. Skips fields that
 * say "ok" or "none". Designed to read like a short note, not a row.
 *
 * Mirrored 1:1 in `parsers/verdict_emphasis.py § humanize_verdict` so
 * the backend can pass the LLM the exact rendered form for substring
 * matching. If one moves, move the other.
 */
function humanizeVerdict(v: GeminiVerdict | null): string | null {
  if (!v) return null;
  const parts: string[] = [];
  if (v.text_differences && v.text_differences.toLowerCase().trim() !== "none") {
    parts.push(v.text_differences.trim());
  }
  if (
    v.clip_integrity &&
    v.clip_integrity.toLowerCase().trim() !== "ok" &&
    v.clip_integrity.toLowerCase().trim() !== "none"
  ) {
    // Render "cuts-mid-word" as "the clip cuts mid-word" etc.
    const human = v.clip_integrity.replace(/-/g, " ").trim();
    parts.push(`The clip ${human}.`);
  }
  if (
    v.speaker_attribution &&
    v.speaker_attribution.toLowerCase().trim() === "uncertain"
  ) {
    parts.push(
      v.speaker_attribution_notes && v.speaker_attribution_notes !== "ok"
        ? `Speaker uncertain — ${v.speaker_attribution_notes.trim()}`
        : "Speaker uncertain."
    );
  } else if (
    v.speaker_attribution &&
    v.speaker_attribution.toLowerCase().trim() === "no"
  ) {
    parts.push(
      v.speaker_attribution_notes && v.speaker_attribution_notes !== "ok"
        ? `Wrong speaker — ${v.speaker_attribution_notes.trim()}`
        : "Wrong speaker."
    );
  }
  if (v.other_concerns && v.other_concerns.toLowerCase().trim() !== "none") {
    parts.push(v.other_concerns.trim());
  }
  if (!parts.length) return null;
  return parts.join(" ");
}

export default function DisputedQuotesPage({ onBack, onNavigate }: DisputedQuotesPageProps) {
  const [payload, setPayload] = useState<ListPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cityFilter, setCityFilter] = useState<string>("");

  // Per-row edit buffer. The textarea content for each row, keyed by
  // quote id, defaulting to the quote's current text. Updated as the
  // operator types.
  const [edits, setEdits] = useState<Record<number, string>>({});

  // Disabled-during-resolve guard. Whichever row is being resolved is
  // set here; other rows stay interactive.
  const [busyId, setBusyId] = useState<number | null>(null);

  // Only one SyncedQuote plays at a time across the page.
  const [activeKaraokeId, setActiveKaraokeId] = useState<number | null>(null);

  // D-057 — per-row "Why" reasoning panel expansion. Defaults to
  // collapsed; click to expand. Mirrors VocabularyInboxPage's pattern.
  const [reasoningOpen, setReasoningOpen] = useState<Record<number, boolean>>({});

  const fetchDisputed = (city: string | null) => {
    setLoading(true);
    setError(null);
    const qs = city ? `?city=${encodeURIComponent(city)}` : "";
    fetch(`/api/disputed-quotes${qs}`)
      .then(async r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: ListPayload) => {
        setPayload(data);
        // Seed each row's edit buffer per D-057 pre-fill priority:
        //   1. agent_proposed_quote_text (when present — Opus's
        //      counter-proposal carries judgment the polished form
        //      can't, e.g. restoring a cautionary preamble the
        //      cleaner stripped per D-056)
        //   2. quote_text_display (D-054 readable-polished form for
        //      the common case where the verifier-side text is right)
        //   3. quote_text (verbatim — fallback when OPENAI_API_KEY
        //      isn't configured at ingest)
        // Re-seeded on every fetch so a stale buffer doesn't persist
        // across refreshes.
        const next: Record<number, string> = {};
        for (const q of data.disputed_quotes ?? []) {
          const agentVal = q.agent_proposed_quote_text?.trim();
          next[q.id] = agentVal && agentVal !== ""
            ? agentVal
            : q.quote_text_display ?? q.quote_text;
        }
        setEdits(next);
      })
      .catch(e => setError(e?.message ?? String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchDisputed(cityFilter || null);
  }, [cityFilter]);

  const resolve = async (q: DisputedQuote, action: "verify" | "reject") => {
    if (busyId !== null) return;
    setBusyId(q.id);
    try {
      const agentVal = q.agent_proposed_quote_text?.trim();
      const defaultText = agentVal && agentVal !== ""
        ? agentVal
        : q.quote_text_display ?? q.quote_text;
      const editedText = edits[q.id] ?? defaultText;
      const body: Record<string, unknown> = {
        action,
        resolved_by: "operator",
      };
      // Send quote_text on verify whenever the textarea differs from
      // the verbatim `quote_text`. Even when the operator clicks Verify
      // unchanged, the prefilled form (agent's counter-proposal OR
      // polished form) differs from verbatim, so this promotes it to
      // canonical (D-054 + `update_quote_verification` preserves
      // verbatim in `quote_text_original`).
      if (action === "verify" && editedText.trim() && editedText.trim() !== q.quote_text) {
        body.quote_text = editedText.trim();
      }
      const r = await fetch(`/api/disputed-quotes/${q.id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok || !data?.ok) {
        throw new Error(data?.error ?? `HTTP ${r.status}`);
      }
      // Refresh — the resolved quote drops out of the list.
      fetchDisputed(cityFilter || null);
    } catch (e: any) {
      setError(`Resolve failed for #${q.id}: ${e?.message ?? e}`);
    } finally {
      setBusyId(null);
    }
  };

  const quotes = payload?.disputed_quotes ?? [];
  const countLabel = useMemo(() => {
    if (loading) return "Loading…";
    if (!quotes.length) return "All clear";
    return `${quotes.length} to review`;
  }, [quotes.length, loading]);

  return (
    <div className="min-h-screen bg-background text-foreground font-sans">
      <header className="sticky top-0 z-40 bg-[var(--canvas)]/95 backdrop-blur border-b border-[var(--line)]">
        <div className="max-w-4xl mx-auto px-6 lg:px-10 py-5 flex items-center justify-between gap-4">
          <div className="flex items-center gap-5 min-w-0">
            <button
              onClick={onBack}
              className="group flex items-center gap-2 text-muted-foreground hover:text-foreground transition-colors"
            >
              <ArrowLeft className="w-4 h-4 group-hover:-translate-x-0.5 transition-transform" />
              <span className="text-sm font-medium">Back</span>
            </button>
            <div className="h-4 w-px bg-[var(--line)]" />
            <div className="flex items-center gap-3 min-w-0">
              <div className="bg-[#F5A524] text-black p-1.5 rounded-md flex-shrink-0">
                <AlertTriangle className="w-4 h-4" />
              </div>
              <div className="min-w-0">
                <h1 className="text-lg font-semibold text-white">Disputed quotes</h1>
                <p className="text-[12px] text-foreground/55">
                  Listen, edit if needed, verify or reject.
                </p>
              </div>
            </div>
          </div>
          <div className="hidden sm:flex items-center gap-2.5 px-3 py-1.5 rounded-md border border-[var(--line)] bg-[var(--surface)]">
            <span className="text-[13px] text-foreground/70 tabular-nums">
              {countLabel}
            </span>
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 lg:px-10 py-8">
        {/* City filter */}
        {(payload?.count ?? 0) > 0 || cityFilter ? (
          <div className="mb-6 flex items-center gap-3 text-[13px]">
            <span className="text-foreground/55">Filter by city:</span>
            <input
              value={cityFilter}
              onChange={e => setCityFilter(e.target.value)}
              placeholder="e.g. Kingman (blank = all)"
              className="bg-black border border-white/15 rounded-md px-3 py-1.5 text-[13px] text-white w-[240px] focus:outline-none focus:border-[#F5A524]"
            />
            {cityFilter && (
              <button
                onClick={() => setCityFilter("")}
                className="text-[12px] text-foreground/55 hover:text-white"
              >
                Clear
              </button>
            )}
          </div>
        ) : null}

        {error && (
          <div className="border border-red-500/40 rounded-md px-4 py-3 mb-6 text-[14px] text-red-400">
            {error}
          </div>
        )}

        {!error && !loading && quotes.length === 0 && (
          <div className="text-center py-16">
            <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-[#22C55E]/10 mb-4">
              <Check className="w-6 h-6 text-[#22C55E]" />
            </div>
            <p className="text-[16px] text-white font-medium">No disputed quotes right now.</p>
            <p className="text-[13px] text-foreground/50 mt-2 max-w-md mx-auto leading-relaxed">
              When the verification pass flags quotes as disputed, they land here.
              Resolve each one by verifying (with edits if needed) or rejecting it.
            </p>
          </div>
        )}

        <ul className="flex flex-col gap-6">
          {quotes.map(q => {
            const canKaraoke =
              Array.isArray(q.word_timings) &&
              q.word_timings.length > 0 &&
              !!q.meeting_video_url;
            const verdictNote = humanizeVerdict(q.gemini_verdict);
            const agentVal = q.agent_proposed_quote_text?.trim();
            const hasAgentProposal = !!agentVal;
            const defaultText = hasAgentProposal
              ? agentVal!
              : q.quote_text_display ?? q.quote_text;
            const editedText = edits[q.id] ?? defaultText;
            // "edited" means deviated from the pre-fill the reviewer
            // saw on load (agent's value or polished form). We don't
            // surface "edited" just because the pre-fill differs from
            // verbatim; only if the operator typed something different
            // from what they were initially shown.
            const hasEdits = editedText.trim() !== defaultText.trim();
            const isBusy = busyId === q.id;
            const isReasoningOpen = reasoningOpen[q.id] === true;
            return (
              <li
                key={q.id}
                className="border border-[var(--line)] rounded-lg bg-[var(--surface)]/40 p-6"
              >
                {/* Header — who, when, where, plus a Disputed pill */}
                <div className="flex items-start justify-between gap-4 mb-5">
                  <div className="min-w-0">
                    <p className="text-[16px] text-white font-semibold leading-tight">
                      {q.speaker_name}
                      {q.speaker_role && (
                        <span className="text-foreground/55 font-normal text-[14px] ml-2">
                          {q.speaker_role}
                        </span>
                      )}
                    </p>
                    <button
                      onClick={() => onNavigate?.("broadcast", { meetingId: q.meeting_id })}
                      className="mt-1 text-[13px] text-foreground/55 hover:text-white transition-colors inline-flex items-center gap-1.5"
                    >
                      <span>{formatMeetingDate(q.meeting_date)}</span>
                      <span className="text-foreground/35">·</span>
                      <span>{q.city_name}</span>
                      <ArrowUpRight className="w-3 h-3 ml-0.5" />
                    </button>
                  </div>
                  <span
                    className="text-[11px] px-2 py-0.5 rounded-md border flex-shrink-0 font-medium"
                    style={{
                      color: "#A78BFA",
                      borderColor: "rgba(167, 139, 250, 0.45)",
                      backgroundColor: "rgba(167, 139, 250, 0.06)",
                    }}
                  >
                    Disputed
                  </span>
                </div>

                {/* Quote display — single rendering. SyncedQuote when we
                   can karaoke; otherwise a plain blockquote. */}
                {canKaraoke ? (
                  <div className="mb-5">
                    <SyncedQuote
                      wordTimings={q.word_timings!}
                      videoUrl={q.meeting_video_url!}
                      isActive={activeKaraokeId === q.id}
                      onActivate={() => setActiveKaraokeId(q.id)}
                      onDeactivate={() =>
                        setActiveKaraokeId(prev => (prev === q.id ? null : prev))
                      }
                    />
                  </div>
                ) : (
                  <blockquote className="mb-5 pl-4 border-l-2 border-[var(--line)] text-[15px] text-white/85 leading-relaxed italic">
                    &ldquo;{q.quote_text}&rdquo;
                  </blockquote>
                )}

                {/* Plain-language note about what Gemini noticed.
                   No "GEMINI VERDICT" label, no uppercase fields.
                   Reads like a colleague's annotation. The substantive
                   bits (specific differing words, brief integrity
                   descriptors) are red-emphasized via the lazy-computed
                   `verdict_emphasis_tokens` cache. */}
                {verdictNote && (
                  <div className="mb-5 px-4 py-3 rounded-md border border-[#F5A524]/25 bg-[#F5A524]/[0.04]">
                    <p className="text-[13px] text-[#F5A524]/90 font-medium mb-1">
                      Heads-up from verification:
                    </p>
                    <p className="text-[14px] text-white/85 leading-relaxed">
                      {renderWithEmphasis(verdictNote, q.verdict_emphasis_tokens)}
                    </p>
                  </div>
                )}

                {/* D-057 — agent counter-proposal block. Renders when an
                   Opus judgment agent has recorded a defensible better
                   alternative (e.g. preserving a cautionary preamble the
                   cleaner stripped, per D-056). The textarea below
                   pre-fills with this value when present. Visual language
                   matches VocabularyInboxPage: violet marker + "Opus"
                   label + expandable "Why" for reasoning. The karaoke
                   above remains the verifier-side single source of truth
                   for the recorded audio; this block is a callout that
                   says "the agent suggests substituting this text." */}
                {hasAgentProposal && (
                  <div className="mb-5 px-4 py-3 rounded-md border border-[#A78BFA]/30 bg-[#A78BFA]/[0.04]">
                    <div className="flex items-baseline gap-2 mb-1.5 flex-wrap">
                      <p className="text-[13px] text-[#A78BFA]/95 font-medium">
                        Opus suggests
                      </p>
                      <span
                        className="text-[10px] uppercase tracking-widest text-[#A78BFA]/65"
                        title={`Counter-proposal from ${q.agent_proposed_by ?? "agent"}`}
                      >
                        {q.agent_proposed_by ?? "agent"}
                      </span>
                      {q.agent_reasoning && (
                        <button
                          type="button"
                          onClick={() =>
                            setReasoningOpen(prev => ({
                              ...prev,
                              [q.id]: !prev[q.id],
                            }))
                          }
                          className="text-[11px] text-foreground/55 hover:text-white underline underline-offset-2 ml-auto"
                        >
                          {isReasoningOpen ? "Hide reasoning" : "Why"}
                        </button>
                      )}
                    </div>
                    <p
                      className="text-[14px] text-white/85 leading-relaxed"
                      style={{
                        backgroundImage:
                          "linear-gradient(180deg, transparent 6%, #A78BFA1F 16%, #A78BFA1F 84%, transparent 94%)",
                        backgroundRepeat: "no-repeat",
                        backgroundPosition: "0 50%",
                        backgroundSize: "100% 100%",
                        borderRadius: "4px 8px 3px 7px / 9px 3px 8px 4px",
                        padding: "0.04em 0.16em",
                        margin: "0 -0.04em",
                      }}
                    >
                      &ldquo;{agentVal}&rdquo;
                    </p>
                    {isReasoningOpen && q.agent_reasoning && (
                      <p className="text-[13px] text-foreground/70 leading-relaxed mt-3 pl-3 border-l-2 border-[#A78BFA]/30">
                        {q.agent_reasoning}
                      </p>
                    )}
                  </div>
                )}

                {/* Always-editable text. Pre-filled per the D-057 priority
                   (agent's counter-proposal > polished > verbatim) so the
                   operator scans the most refined form first. Diff hint
                   shown only when the operator has typed something
                   different. No two-step "click [RESOLVE] to enter edit
                   mode" — just edit if needed. */}
                <div className="mb-4">
                  <label className="block text-[12px] text-foreground/55 mb-2">
                    Quote text {hasEdits && (
                      <span className="text-[#F5A524] ml-1">· edited</span>
                    )}
                    {hasAgentProposal && !hasEdits && (
                      <span
                        className="ml-2 text-[10px] uppercase tracking-widest text-[#A78BFA]/80"
                        title="Pre-filled from the agent's counter-proposal — edit before verifying to override"
                      >
                        from Opus
                      </span>
                    )}
                  </label>
                  <textarea
                    value={editedText}
                    onChange={e =>
                      setEdits(prev => ({ ...prev, [q.id]: e.target.value }))
                    }
                    rows={Math.max(3, Math.min(8, Math.ceil(editedText.length / 80)))}
                    className="w-full bg-black border border-white/15 rounded-md px-3 py-2.5 text-[15px] text-white leading-relaxed focus:outline-none focus:border-[#F5A524] resize-y"
                    placeholder="Edit the text to match what was actually said, or leave as-is to accept."
                  />
                  {hasEdits && (
                    <button
                      onClick={() =>
                        setEdits(prev => ({ ...prev, [q.id]: defaultText }))
                      }
                      className="mt-2 text-[12px] text-foreground/55 hover:text-white inline-flex items-center gap-1"
                    >
                      <X className="w-3 h-3" />
                      Revert
                    </button>
                  )}
                </div>

                {/* Actions — one primary (Verify) + one secondary (Reject).
                   No [Cancel] since there's nothing to cancel: editing is
                   always-on and revert-able above. */}
                <div className="flex items-center gap-3">
                  <button
                    disabled={isBusy}
                    onClick={() => resolve(q, "verify")}
                    className="inline-flex items-center gap-2 text-[14px] font-semibold text-black bg-[#22C55E] hover:bg-[#34D87B] disabled:opacity-40 disabled:cursor-wait px-4 py-2 rounded-md transition-colors"
                  >
                    <Check className="w-4 h-4" />
                    {isBusy ? "Saving…" : hasEdits ? "Save & verify" : "Verify"}
                  </button>
                  <button
                    disabled={isBusy}
                    onClick={() => resolve(q, "reject")}
                    className="text-[13px] text-[#EF4444] hover:text-white border border-[#EF4444]/40 hover:border-[#EF4444] hover:bg-[#EF4444]/10 disabled:opacity-40 px-3 py-2 rounded-md transition-colors"
                  >
                    Reject
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      </main>
    </div>
  );
}

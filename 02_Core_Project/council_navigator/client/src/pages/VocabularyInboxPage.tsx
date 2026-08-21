/**
 * VocabularyInboxPage — T-018 operator review surface.
 *
 * Per-city pending-promotion queue for Gemini-surfaced vocabulary
 * corrections accumulated in `city_vocabulary_corrections` (T-017
 * Layer 2). The operator either:
 *
 *  - PROMOTES a correction (optionally with a category) — appends the
 *    canonical term into `city_intelligence/<slug>.json` so future
 *    Whisper transcriptions + all NotebookLM prompts pick it up.
 *
 *  - REJECTS a correction — flips its auto_apply flag off, so it
 *    stops applying to new Studio outputs AND drops from the Inbox.
 *
 * The Inbox is split into two groups:
 *  - Auto-eligible: applied_count >= threshold (default 2). These
 *    crossed the recurrence bar and are likely universal corrections.
 *  - Manual-only: applied_count < threshold. Operator's judgment call.
 *
 * Aesthetic note: this is the second home for the marker stroke
 * (after T-012 accountability surfaces). Each correction shows
 * `wrong → right` with the right-side rendered in marker orange to
 * reinforce "this is being marked into the permanent record."
 */
import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  Building2,
  CheckCircle2,
  XCircle,
  BookOpen,
} from "lucide-react";

interface VocabularyInboxPageProps {
  onBack: () => void;
}

type Correction = {
  id: number;
  city_name: string;
  wrong: string;
  right: string;
  applied_count: number;
  auto_apply: number;
  first_observed_response_file: string | null;
  last_applied_at: string | null;
  created_at: string | null;
  promoted_at: string | null;
  promoted_by: string | null;
  meets_threshold: boolean;
  // D-057 — agent counter-proposal (Vocab Curator + future agents).
  // When the verifier's `right` is wrong but an agent has a clear
  // better alternative, it records the proposal here. The UI surfaces
  // both side by side; promote uses whatever's in the text box (which
  // defaults to agent_proposed_right when present).
  agent_proposed_right: string | null;
  agent_reasoning: string | null;
  agent_proposed_by: string | null;
  agent_proposed_at: string | null;
};

type InboxPayload = {
  city: string;
  threshold: number;
  auto_eligible_count: number;
  manual_only_count: number;
  auto_eligible: Correction[];
  manual_only: Correction[];
};

const CATEGORY_OPTIONS = [
  { value: "", label: "(no category)" },
  { value: "person", label: "Person" },
  { value: "street", label: "Street" },
  { value: "place", label: "Place" },
  { value: "business", label: "Business" },
  { value: "civic_term", label: "Civic term" },
  { value: "event", label: "Event" },
  { value: "other", label: "Other" },
];

// Available cities for the filter dropdown. Eventually this could come
// from /api/cast or similar; for V1 it's hardcoded to the Mohave pilot
// roster.
const CITIES = ["Kingman", "Bullhead City", "Lake Havasu City"];

export default function VocabularyInboxPage({ onBack }: VocabularyInboxPageProps) {
  const [city, setCity] = useState<string>("Kingman");
  const [threshold, setThreshold] = useState<number>(2);
  const [payload, setPayload] = useState<InboxPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Per-correction category selection — pre-filled per row.
  const [categories, setCategories] = useState<Record<number, string>>({});
  // D-057 — per-correction "Final value" text box. Pre-filled from the
  // server's value (agent counter-proposal if present, else verifier's
  // right) on first render of each row; operator can edit before
  // promoting. Keyed by correction id so editing one row doesn't affect
  // others. Empty-string entries mean "fall back to default" — only
  // populated entries override.
  const [overrides, setOverrides] = useState<Record<number, string>>({});
  // Per-correction reasoning panel expansion state (Opus-reasoning
  // indicator). Defaults to collapsed; click to expand.
  const [reasoningOpen, setReasoningOpen] = useState<Record<number, boolean>>({});
  const [busyId, setBusyId] = useState<number | null>(null);

  const fetchInbox = () => {
    setLoading(true);
    setError(null);
    fetch(
      `/api/vocabulary-inbox?city=${encodeURIComponent(city)}&threshold=${threshold}`
    )
      .then(async r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(data => setPayload(data))
      .catch(e => setError(e?.message ?? String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(fetchInbox, [city, threshold]);

  // Resolve the "what will actually land in the dictionary" value for a row,
  // following the same priority order as the Flask endpoint:
  //   text-box override (operator edit) > agent_proposed_right > verifier `right`
  const resolveFinalRight = (c: Correction): string => {
    const typed = overrides[c.id];
    if (typed !== undefined && typed.trim() !== "") return typed.trim();
    if (c.agent_proposed_right && c.agent_proposed_right.trim() !== "") {
      return c.agent_proposed_right.trim();
    }
    return c.right;
  };

  const promote = async (c: Correction) => {
    if (busyId !== null) return;
    setBusyId(c.id);
    try {
      // Send override_right when the resolved final value differs from
      // the verifier's `right` (covers both operator edits AND
      // agent-counter-proposal acceptance via the prefilled text box).
      const finalRight = resolveFinalRight(c);
      const body: Record<string, unknown> = {
        correction_id: c.id,
        category: categories[c.id] || null,
        promoted_by: "operator",
      };
      if (finalRight !== c.right) {
        body.override_right = finalRight;
      }
      const r = await fetch("/api/vocabulary-inbox/promote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok || !data?.ok) {
        throw new Error(data?.error ?? `HTTP ${r.status}`);
      }
      fetchInbox();
    } catch (e: any) {
      setError(`Promote failed for #${c.id}: ${e?.message ?? e}`);
    } finally {
      setBusyId(null);
    }
  };

  const reject = async (c: Correction) => {
    if (busyId !== null) return;
    setBusyId(c.id);
    try {
      const r = await fetch("/api/vocabulary-inbox/reject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          correction_id: c.id,
          rejected_by: "operator",
        }),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok || !data?.ok) {
        throw new Error(data?.error ?? `HTTP ${r.status}`);
      }
      fetchInbox();
    } catch (e: any) {
      setError(`Reject failed for #${c.id}: ${e?.message ?? e}`);
    } finally {
      setBusyId(null);
    }
  };

  const totalCount = useMemo(() => {
    if (loading) return "Loading…";
    if (!payload) return "—";
    const n = payload.auto_eligible_count + payload.manual_only_count;
    if (n === 0) return "Empty";
    return `${n} pending`;
  }, [payload, loading]);

  const renderRow = (c: Correction) => {
    const isBusy = busyId === c.id;
    // Source-file path lives on a `title` hover so the metadata row
    // doesn't carry filesystem noise on every row — debug-traceability
    // available when needed, invisible otherwise (D-054 tightening pass).
    const sourceTooltip = c.first_observed_response_file
      ? `Source: ${c.first_observed_response_file}`
      : undefined;
    const hasAgentProposal =
      c.agent_proposed_right !== null && c.agent_proposed_right !== undefined && c.agent_proposed_right.trim() !== "";
    const finalRight = resolveFinalRight(c);
    const overrodeVerifier = finalRight !== c.right;
    const isReasoningOpen = reasoningOpen[c.id] === true;
    return (
      <li
        key={c.id}
        className="border border-[var(--line)] rounded-md p-4 bg-[var(--surface)]/40"
      >
        <div className="flex items-start justify-between gap-4 mb-3">
          <div className="min-w-0 flex-1">
            {/* The wrong → right rendering. Right side gets the marker
                stroke to reinforce "this is being added to the record."
                Labeled "Gemini" when there's also an Opus counter-proposal
                below; unlabeled in the V1-shape single-source case. */}
            <p
              className="text-[15px] leading-snug flex flex-wrap items-baseline gap-x-2"
              title={sourceTooltip}
            >
              <span className="text-foreground/45 line-through tabular-nums text-[13px]">
                {c.wrong}
              </span>
              <span className="text-foreground/35 text-[11px]">→</span>
              <span
                className="font-semibold text-white"
                style={{
                  backgroundImage:
                    "linear-gradient(180deg, transparent 6%, #F2A91CA6 16%, #F2A91CA6 84%, transparent 94%)",
                  backgroundRepeat: "no-repeat",
                  backgroundPosition: "0 50%",
                  backgroundSize: "100% 100%",
                  borderRadius: "4px 8px 3px 7px / 9px 3px 8px 4px",
                  padding: "0.04em 0.16em",
                  margin: "0 -0.04em",
                }}
              >
                {c.right}
              </span>
              {hasAgentProposal && (
                <span
                  className="text-[10px] uppercase tracking-widest text-foreground/45 ml-1"
                  title="Verifier's proposed right value (from Gemini Pro review)"
                >
                  Gemini
                </span>
              )}
            </p>

            {/* D-057 — agent counter-proposal block. Renders below the
                verifier's pair when present. Same marker-orange visual
                language but slightly different stroke + an "Opus" tag so
                the operator immediately sees which is which. Reasoning
                is collapsed by default; click "Why" to expand. */}
            {hasAgentProposal && (
              <div className="mt-2">
                <p className="text-[15px] leading-snug flex flex-wrap items-baseline gap-x-2 pl-5">
                  <span className="text-foreground/40 text-[11px]">↪</span>
                  <span
                    className="font-semibold text-white"
                    style={{
                      backgroundImage:
                        "linear-gradient(180deg, transparent 6%, #A78BFAA6 16%, #A78BFAA6 84%, transparent 94%)",
                      backgroundRepeat: "no-repeat",
                      backgroundPosition: "0 50%",
                      backgroundSize: "100% 100%",
                      borderRadius: "4px 8px 3px 7px / 9px 3px 8px 4px",
                      padding: "0.04em 0.16em",
                      margin: "0 -0.04em",
                    }}
                  >
                    {c.agent_proposed_right}
                  </span>
                  <span
                    className="text-[10px] uppercase tracking-widest text-[#A78BFA]/85 ml-1"
                    title={`Counter-proposal from ${c.agent_proposed_by ?? "agent"}`}
                  >
                    Opus
                  </span>
                  {c.agent_reasoning && (
                    <button
                      type="button"
                      onClick={() =>
                        setReasoningOpen(prev => ({
                          ...prev,
                          [c.id]: !prev[c.id],
                        }))
                      }
                      className="text-[11px] text-foreground/55 hover:text-white underline underline-offset-2 ml-1"
                    >
                      {isReasoningOpen ? "Hide reasoning" : "Why"}
                    </button>
                  )}
                </p>
                {isReasoningOpen && c.agent_reasoning && (
                  <p className="text-[13px] text-foreground/70 leading-relaxed mt-2 pl-5 border-l-2 border-[#A78BFA]/30 ml-1">
                    {c.agent_reasoning}
                  </p>
                )}
              </div>
            )}

            <div className="flex items-center flex-wrap gap-3 mt-3 text-[12px] text-foreground/55">
              <span className="tabular-nums">
                Caught in {c.applied_count} {c.applied_count === 1 ? "verification batch" : "verification batches"}
              </span>
              {c.meets_threshold && (
                <span
                  className="text-[11px] px-2 py-0.5 border rounded-md font-medium"
                  style={{
                    color: "#22C55E",
                    borderColor: "rgba(34, 197, 94, 0.45)",
                    backgroundColor: "rgba(34, 197, 94, 0.06)",
                  }}
                  title="Crossed the auto-promotion threshold"
                >
                  Auto-eligible
                </span>
              )}
              <span className="text-foreground/40">
                First seen {c.created_at?.split(" ")[0] ?? "—"}
              </span>
            </div>
          </div>
        </div>

        {/* Editable "Final value" text box. Always present so the
            operator can hand-adjust before promoting. Pre-filled per
            priority: existing override (operator already typed) >
            agent_proposed_right > verifier's right. */}
        <div className="mt-4 mb-3">
          <label className="block text-[12px] text-foreground/55 mb-1.5">
            Final value to add to dictionary
            {overrodeVerifier && (
              <span className="ml-2 text-[10px] uppercase tracking-widest text-[#A78BFA]/80">
                overrides Gemini
              </span>
            )}
          </label>
          <input
            type="text"
            value={overrides[c.id] ?? finalRight}
            onChange={e =>
              setOverrides(prev => ({ ...prev, [c.id]: e.target.value }))
            }
            disabled={isBusy}
            className="w-full bg-black border border-white/15 rounded-md px-3 py-2 text-[14px] text-white/90 focus:outline-none focus:border-[#F2A91C] disabled:opacity-40"
            placeholder={c.right}
          />
        </div>

        <div className="flex items-end justify-between gap-3">
          <div className="flex items-center gap-2">
            <label className="text-[12px] text-foreground/55">
              Category
            </label>
            <select
              value={categories[c.id] ?? ""}
              onChange={e =>
                setCategories(prev => ({ ...prev, [c.id]: e.target.value }))
              }
              disabled={isBusy}
              className="bg-black border border-white/15 rounded-md px-2.5 py-1.5 text-[13px] text-white/85 focus:outline-none focus:border-[#F5A524] disabled:opacity-40"
            >
              {CATEGORY_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-3">
            <button
              disabled={isBusy}
              onClick={() => promote(c)}
              className="inline-flex items-center gap-2 text-[14px] font-semibold text-black bg-[#22C55E] hover:bg-[#34D87B] disabled:opacity-40 disabled:cursor-wait px-4 py-2 rounded-md transition-colors"
            >
              <CheckCircle2 className="w-4 h-4" />
              {isBusy ? "Saving…" : "Promote"}
            </button>
            <button
              disabled={isBusy}
              onClick={() => reject(c)}
              className="inline-flex items-center gap-1.5 text-[13px] text-[#EF4444] hover:text-white border border-[#EF4444]/40 hover:border-[#EF4444] hover:bg-[#EF4444]/10 disabled:opacity-40 px-3 py-2 rounded-md transition-colors"
            >
              <XCircle className="w-3.5 h-3.5" />
              Reject
            </button>
          </div>
        </div>
      </li>
    );
  };

  return (
    <div className="min-h-screen bg-background text-foreground font-sans">
      <header className="sticky top-0 z-40 bg-[var(--canvas)]/95 backdrop-blur border-b border-[var(--line)]">
        <div className="max-w-7xl mx-auto px-6 lg:px-10 py-5 flex items-center justify-between gap-4">
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
              <div className="bg-[#F2A91C] text-black p-1.5 rounded-md flex-shrink-0">
                <BookOpen className="w-4 h-4" />
              </div>
              <div className="min-w-0">
                <h1 className="text-lg font-semibold text-white truncate">
                  Vocabulary inbox · {city}
                </h1>
                <p className="text-[12px] text-foreground/55">
                  Promote terms to this city's permanent vocabulary, or reject.
                </p>
              </div>
            </div>
          </div>
          <div className="hidden sm:flex items-center gap-2.5 px-3 py-1.5 rounded-md border border-[var(--line)] bg-[var(--surface)]">
            <span className="text-[13px] text-foreground/70 tabular-nums">
              {totalCount}
            </span>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 lg:px-10 py-8">
        <div className="mb-6 flex items-center gap-5 text-[13px]">
          <div className="flex items-center gap-2">
            <span className="text-foreground/55">City</span>
            <select
              value={city}
              onChange={e => setCity(e.target.value)}
              className="bg-black border border-white/15 rounded-md px-2.5 py-1.5 text-[13px] text-white focus:outline-none focus:border-[#F5A524]"
            >
              {CITIES.map(c => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-foreground/55">Threshold</span>
            <input
              type="number"
              min={1}
              max={20}
              value={threshold}
              onChange={e => setThreshold(Math.max(1, parseInt(e.target.value || "1", 10)))}
              className="bg-black border border-white/15 rounded-md px-2.5 py-1.5 text-[13px] text-white w-[64px] focus:outline-none focus:border-[#F5A524]"
            />
            <span className="text-foreground/45 text-[12px]">
              caught {threshold}+ times = auto-eligible
            </span>
          </div>
        </div>

        {error && (
          <div className="border border-red-500/40 rounded-md px-4 py-3 mb-6 text-[14px] text-red-400">
            {error}
          </div>
        )}

        {!error && !loading && payload && payload.auto_eligible_count === 0 && payload.manual_only_count === 0 && (
          <div className="text-center py-16">
            <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-[#22C55E]/10 mb-4">
              <CheckCircle2 className="w-6 h-6 text-[#22C55E]" />
            </div>
            <p className="text-[16px] text-white font-medium">No pending corrections for {city}.</p>
            <p className="text-[13px] text-foreground/50 mt-2 max-w-md mx-auto leading-relaxed">
              When the verification pass catches a misspelling repeatedly,
              it lands here. Promoting a term adds it to this city's permanent
              vocabulary so future transcriptions and prompts use the correct
              spelling.
            </p>
          </div>
        )}

        {payload && payload.auto_eligible_count > 0 && (
          <section className="mb-10">
            <header className="mb-4 border-b border-[var(--line)] pb-2 flex items-end justify-between">
              <p className="text-[13px] font-medium text-[#22C55E]">
                Auto-eligible · caught {payload.threshold}+ times
              </p>
              <span className="text-[12px] text-foreground/55 tabular-nums">
                {payload.auto_eligible_count} {payload.auto_eligible_count === 1 ? "term" : "terms"}
              </span>
            </header>
            <ul className="flex flex-col gap-3">
              {payload.auto_eligible.map(renderRow)}
            </ul>
          </section>
        )}

        {payload && payload.manual_only_count > 0 && (
          <section>
            <header className="mb-4 border-b border-[var(--line)] pb-2 flex items-end justify-between">
              <p className="text-[13px] font-medium text-foreground/70">
                Manual-only · caught fewer than {payload.threshold} {payload.threshold === 1 ? "time" : "times"}
              </p>
              <span className="text-[12px] text-foreground/55 tabular-nums">
                {payload.manual_only_count} {payload.manual_only_count === 1 ? "term" : "terms"}
              </span>
            </header>
            <p className="text-[13px] text-foreground/55 mb-4 max-w-2xl leading-relaxed">
              Below the auto-promotion threshold. Promote one-by-one when
              you've eyeballed the correction and it's clearly canonical —
              e.g., a proper noun the speaker said correctly but the transcript
              consistently mishears.
            </p>
            <ul className="flex flex-col gap-3">
              {payload.manual_only.map(renderRow)}
            </ul>
          </section>
        )}
      </main>
    </div>
  );
}

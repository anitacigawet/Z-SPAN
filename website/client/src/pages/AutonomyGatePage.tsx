/**
 * AutonomyGatePage —orchestrator autonomy gate (dev/operator surface).
 *
 * A calm, plain-language control board for the digital-twin orchestrator's
 * graduated autonomy. Each row is one thing the AI maintainer can do, described
 * the way you'd describe a colleague's job — not a schema field. The
 * switch controls whether it may do that thing ON ITS OWN; per the manual's
 * Mode B, James can always instruct any of these regardless of the switch, so
 * one global note up top says that once and each row stays uncluttered.
 *
 * Load-bearing: writes to /api/orchestrator/autonomy; the orchestrator (when
 * built,step 3) reads `autonomous_enabled` per capability at each
 * heartbeat to know its envelope. Ungate over time by flipping a switch; leave
 * an audit note on any row you want to watch before the next unlock.
 */
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, Flame, Lock, Plus, Check, X } from "lucide-react";

interface AutonomyGatePageProps {
 onBack: () => void;
}

type Capability = {
 id: string;
 label: string;
 what: string;
 rung: number;
 instructed: "on" | "passive" | "gated" | "never";
 wall: boolean;
 autonomous_enabled: boolean;
 note: string;
};

type GatePayload = {
 ok: boolean;
 capabilities: Capability[];
 frontier_rung: number;
};

// ──ingestion metering (the calibration dial + its live readout) ──
type Calibration = {
 videos_per_day: number;
 reviewers: number;
 reviews_per_reviewer_per_day: number;
 available_balance: number | null; //budget pot ($); null = unconfigured
 cost_per_video: number;
 solvency_days: number;
 note: string;
};

type Metering = {
 city: string;
 ceilings: {
 compute_per_day: number;
 review_per_day: number;
 effective_per_day: number;
 budget_per_day: number | null; //budget ceiling (null = unconfigured)
 bound_by: string; // "compute" | "review" | "budget" | a "+"-joined tie
 };
 budget: {
 configured: boolean;
 available_balance: number | null;
 cost_per_video: number | null;
 solvency_days: number;
 budget_per_day: number | null;
 runway_days: number | null;
 };
 progress: {
 processed: number;
 ready_to_process: number;
 needs_video_url: number;
 candidate_unprocessed: number;
 excluded_too_old: number;
 other: Record<string, number>;
 };
 today: { processed_today: number; room_today: number; under_ceiling: boolean };
 next_meeting: { meeting_id: number; meeting_date: string; meeting_title: string } | null;
 days_to_drain: number | null;
};

type GovernorPayload = {
 ok: boolean;
 calibration: Calibration;
 review_ceiling: number;
 metering: Metering;
};

// The owner-facing focus city for V1 (matches the Flask DEFAULT_FOCUS_CITY).
const FOCUS_CITY = "Kingman";

const fmtRate = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

// Plain-language clause explaining WHY the effective rate is what it is.
const boundClause = (c: Metering["ceilings"]) =>
 c.bound_by.includes("budget")
 ? " — capped to keep the balance solvent"
 : c.bound_by === "review"
 ? " — held by your review capacity"
 : c.bound_by === "compute"
 ? " — set by the meetings-per-day dial"
 : "";

// Plain-language headers for each rung of the ladder. The titles carry the
// "this unlocks later" progression so individual rows don't have to.
const RUNG_META: Record<number, { title: string; blurb: string }> = {
 1: {
 title: "Live now",
 blurb: "On from day one — watching the board and waking the look-only helpers.",
 },
 2: {
 title: "Next to unlock",
 blurb: "Wakes the judgment agents. Turn on once the live-now set has run clean for a while.",
 },
 3: {
 title: "Later",
 blurb: "Touches money and the meeting pipeline. Turn on only after the agents above have earned it.",
 },
 4: {
 title: "Far off",
 blurb: "Generating stays yours to start; publishing is never automated.",
 },
};

function Toggle({
 on,
 disabled,
 onClick,
}: {
 on: boolean;
 disabled?: boolean;
 onClick: () => void;
}) {
 return (
 <button
 type="button"
 role="switch"
 aria-checked={on}
 disabled={disabled}
 onClick={onClick}
 className={`relative inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors disabled:opacity-50 disabled:cursor-wait ${
 on ? "bg-[#22C55E]" : "bg-white/15"
 }`}
 >
 <span
 className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
 on ? "translate-x-6" : "translate-x-1"
 }`}
 />
 </button>
 );
}

function Stepper({
 value,
 onChange,
 min = 0,
 max = 99,
 disabled,
}: {
 value: number;
 onChange: (v: number) => void;
 min?: number;
 max?: number;
 disabled?: boolean;
}) {
 const btn =
 "h-8 w-8 inline-flex items-center justify-center rounded-md border border-[var(--line)] text-white/80 hover:bg-white/5 disabled:opacity-40 disabled:cursor-not-allowed transition-colors text-lg leading-none";
 return (
 <div className="inline-flex items-center gap-2.5">
 <button
 type="button"
 aria-label="Decrease"
 className={btn}
 disabled={disabled || value <= min}
 onClick={() => onChange(Math.max(min, value - 1))}
 >
 −
 </button>
 <span className="tabular-nums text-white text-[18px] font-semibold w-7 text-center">
 {value}
 </span>
 <button
 type="button"
 aria-label="Increase"
 className={btn}
 disabled={disabled || value >= max}
 onClick={() => onChange(Math.min(max, value + 1))}
 >
 +
 </button>
 </div>
 );
}

// A small $ input for the budget dials (balance, cost-per-video). Commits on
// blur / Enter — never per-keystroke, so it doesn't POST + refetch on every
// digit. `allowEmpty` lets the balance clear to null ("unconfigured").
function MoneyField({
 value,
 onCommit,
 disabled,
 allowEmpty = false,
 width = "w-24",
}: {
 value: number | null;
 onCommit: (v: number | null) => void;
 disabled?: boolean;
 allowEmpty?: boolean;
 width?: string;
}) {
 const [draft, setDraft] = useState(value == null ? "" : String(value));
 useEffect(() => {
 setDraft(value == null ? "" : String(value));
 }, [value]);
 const commit = () => {
 const t = draft.trim();
 if (t === "") {
 if (allowEmpty) onCommit(null);
 else setDraft(value == null ? "" : String(value));
 return;
 }
 const n = Number(t);
 if (!Number.isNaN(n) && n >= 0) onCommit(n);
 else setDraft(value == null ? "" : String(value)); // revert invalid input
 };
 return (
 <div className="inline-flex items-center gap-1">
 <span className="text-foreground/40 text-[14px]">$</span>
 <input
 type="number"
 inputMode="decimal"
 min={0}
 step="0.01"
 value={draft}
 disabled={disabled}
 placeholder={allowEmpty ? "not set" : ""}
 onChange={e => setDraft(e.target.value)}
 onBlur={commit}
 onKeyDown={e => {
 if (e.key === "Enter") (e.target as HTMLInputElement).blur();
 }}
 className={`${width} bg-[var(--surface)]/50 border border-[var(--line)] rounded-md px-2 py-1.5 text-white text-[15px] tabular-nums text-right outline-none focus:border-[var(--line-strong)] disabled:opacity-40 disabled:cursor-not-allowed`}
 />
 </div>
 );
}

export default function AutonomyGatePage({ onBack }: AutonomyGatePageProps) {
 const [caps, setCaps] = useState<Capability[] | null>(null);
 const [frontier, setFrontier] = useState<number>(0);
 const [loading, setLoading] = useState(false);
 const [error, setError] = useState<string | null>(null);
 const [busyId, setBusyId] = useState<string | null>(null);
 const [noteEditing, setNoteEditing] = useState<Record<string, boolean>>({});
 const [noteDraft, setNoteDraft] = useState<Record<string, string>>({});
 const [gov, setGov] = useState<GovernorPayload | null>(null);
 const [govBusy, setGovBusy] = useState(false);

 const load = () => {
 setLoading(true);
 setError(null);
 fetch("/api/orchestrator/autonomy")
 .then(async r => {
 if (!r.ok) throw new Error(`HTTP ${r.status}`);
 return r.json();
 })
 .then((data: GatePayload) => {
 setCaps(data.capabilities);
 setFrontier(data.frontier_rung);
 })
 .catch(e => setError(e?.message ?? String(e)))
 .finally(() => setLoading(false));
 };

 const loadGovernor = () => {
 fetch(`/api/ingestion/governor?city=${encodeURIComponent(FOCUS_CITY)}`)
 .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
 .then((d: GovernorPayload) => setGov(d))
 .catch(() => setGov(null)); // non-fatal — the calibration section just hides
 };

 useEffect(load, []);
 useEffect(loadGovernor, []);

 const post = async (body: Record<string, unknown>) => {
 const r = await fetch("/api/orchestrator/autonomy", {
 method: "POST",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify(body),
 });
 const data = await r.json().catch(() => null);
 if (!r.ok || !data?.ok) throw new Error(data?.error ?? `HTTP ${r.status}`);
 setCaps(data.capabilities);
 setFrontier(data.frontier_rung);
 };

 const setCalibration = async (patch: Partial<Calibration>) => {
 setGovBusy(true);
 try {
 const r = await fetch("/api/ingestion/calibration", {
 method: "POST",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify(patch),
 });
 const d = await r.json().catch(() => null);
 if (!r.ok || !d?.ok) throw new Error(d?.error ?? `HTTP ${r.status}`);
 loadGovernor(); // re-fetch so the effective rate + readout recompute
 } catch (e: any) {
 setError(`Couldn't update the cadence: ${e?.message ?? e}`);
 } finally {
 setGovBusy(false);
 }
 };

 const toggle = async (c: Capability) => {
 if (c.wall || busyId) return;
 setBusyId(c.id);
 try {
 await post({ capability_id: c.id, autonomous_enabled: !c.autonomous_enabled });
 } catch (e: any) {
 setError(`Couldn't update "${c.label}": ${e?.message ?? e}`);
 } finally {
 setBusyId(null);
 }
 };

 const saveNote = async (c: Capability) => {
 setBusyId(c.id);
 try {
 await post({ capability_id: c.id, note: noteDraft[c.id] ?? "" });
 setNoteEditing(p => ({ ...p, [c.id]: false }));
 } catch (e: any) {
 setError(`Couldn't save the note: ${e?.message ?? e}`);
 } finally {
 setBusyId(null);
 }
 };

 const openNote = (c: Capability) => {
 setNoteDraft(p => ({ ...p, [c.id]: c.note ?? "" }));
 setNoteEditing(p => ({ ...p, [c.id]: true }));
 };

 const enabledCount = useMemo(
 () => (caps ? caps.filter(c => c.autonomous_enabled).length : 0),
 [caps]
 );

 const grouped = useMemo(() => {
 const g: Record<number, Capability[]> = {};
 (caps ?? []).forEach(c => {
 (g[c.rung] ??= []).push(c);
 });
 return g;
 }, [caps]);

 const renderCap = (c: Capability) => {
 const isBusy = busyId === c.id;
 const editing = noteEditing[c.id] === true;
 return (
 <li
 key={c.id}
 className="border border-[var(--line)] rounded-lg p-4 bg-[var(--surface)]/40"
 >
 <div className="flex items-start justify-between gap-4">
 <div className="min-w-0 flex-1">
 <p className="text-[15px] font-semibold text-white leading-snug">
 {c.label}
 </p>
 <p className="text-[13px] text-foreground/60 leading-relaxed mt-1">
 {c.what}
 </p>
 {c.instructed === "gated" && (
 <p className="text-[12px] text-[#F2A91C]/90 mt-2">
 You start this one — it won't run unprompted.
 </p>
 )}
 </div>
 <div className="flex-shrink-0 pt-0.5">
 {c.wall ? (
 <span className="inline-flex items-center gap-1.5 text-[12px] text-foreground/60 border border-[var(--line)] rounded-md px-2.5 py-1.5">
 <Lock className="w-3.5 h-3.5" /> Always yours
 </span>
 ) : (
 <div className="flex items-center gap-2.5">
 <span
 className={`text-[12px] tabular-nums ${
 c.autonomous_enabled ? "text-[#22C55E]" : "text-foreground/40"
 }`}
 >
 {c.autonomous_enabled ? "On its own" : "Off"}
 </span>
 <Toggle
 on={c.autonomous_enabled}
 disabled={isBusy}
 onClick={() => toggle(c)}
 />
 </div>
 )}
 </div>
 </div>

 {/* Audit note — quiet by default; the place to record "watching this
 before I unlock the next thing." */}
 <div className="mt-3 pt-3 border-t border-[var(--line)]/60">
 {editing ? (
 <div className="flex items-center gap-2">
 <input
 autoFocus
 value={noteDraft[c.id] ?? ""}
 onChange={e =>
 setNoteDraft(p => ({ ...p, [c.id]: e.target.value }))
 }
 onKeyDown={e => {
 if (e.key === "Enter") saveNote(c);
 if (e.key === "Escape")
 setNoteEditing(p => ({ ...p, [c.id]: false }));
 }}
 placeholder="e.g. saw it double-fire on 6/2 — watching before the next unlock"
 className="flex-1 bg-black border border-white/15 rounded-md px-3 py-1.5 text-[13px] text-white/90 focus:outline-none focus:border-[#F2A91C]"
 />
 <button
 onClick={() => saveNote(c)}
 disabled={isBusy}
 className="inline-flex items-center gap-1 text-[12px] font-medium text-black bg-[#22C55E] hover:bg-[#34D87B] disabled:opacity-50 px-2.5 py-1.5 rounded-md transition-colors"
 >
 <Check className="w-3.5 h-3.5" /> Save
 </button>
 <button
 onClick={() => setNoteEditing(p => ({ ...p, [c.id]: false }))}
 className="inline-flex items-center gap-1 text-[12px] text-foreground/55 hover:text-white px-2 py-1.5 rounded-md transition-colors"
 >
 <X className="w-3.5 h-3.5" /> Cancel
 </button>
 </div>
 ) : c.note ? (
 <button
 onClick={() => openNote(c)}
 className="text-left flex items-start gap-2 group"
 >
 <span className="text-[#F2A91C] leading-tight">▍</span>
 <span className="text-[13px] text-foreground/70 italic group-hover:text-white transition-colors">
 {c.note}
 </span>
 </button>
 ) : (
 <button
 onClick={() => openNote(c)}
 className="text-[12px] text-foreground/45 hover:text-foreground/80 inline-flex items-center gap-1.5 transition-colors"
 >
 <Plus className="w-3.5 h-3.5" /> Add a note
 </button>
 )}
 </div>
 </li>
 );
 };

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
 <div className="bg-[#F2A91C] text-black p-1.5 rounded-md flex-shrink-0">
 <Flame className="w-4 h-4" />
 </div>
 <div className="min-w-0">
 <h1 className="text-lg font-semibold text-white truncate">
 AI maintainer · what it can do
 </h1>
 <p className="text-[12px] text-foreground/55">
 Switch on what it's allowed to do on its own.
 </p>
 </div>
 </div>
 </div>
 <div className="hidden sm:flex items-center gap-2.5 px-3 py-1.5 rounded-md border border-[var(--line)] bg-[var(--surface)]">
 <span className="text-[13px] text-foreground/70 tabular-nums">
 {loading ? "Loading…" : `${enabledCount} on its own`}
 </span>
 </div>
 </div>
 </header>

 <main className="max-w-4xl mx-auto px-6 lg:px-10 py-8">
 {/* The one global explainer — replaces a per-row "instructed" column. */}
 <div className="border border-[var(--line)] rounded-lg p-4 bg-[var(--surface)]/30 mb-8">
 <p className="text-[14px] text-foreground/80 leading-relaxed">
 These switches control what your AI maintainer does{" "}
 <span className="text-white font-semibold">on its own</span>. Everything
 here it can also do the moment you{" "}
 <span className="text-white font-semibold">ask it in Slack</span> — the
 switches are only about acting unprompted. Unlock more over time as it
 earns your trust, and leave a note on any row you want to keep an eye on
 first.
 </p>
 </div>

 {error && (
 <div className="border border-red-500/40 rounded-md px-4 py-3 mb-6 text-[14px] text-red-400">
 {error}
 </div>
 )}

 {/*— the calibration dial + its live readout. The pace knob James
 widens as clean cycles prove the rate safe; the readout makes the
 number's consequence legible right here, next to the dial. */}
 {gov && (
 <section className="border border-[#F2A91C]/30 rounded-lg p-5 bg-[#F2A91C]/[0.04] mb-8">
 <div className="flex items-baseline justify-between gap-3">
 <h2 className="text-[15px] font-semibold text-white">How fast it runs</h2>
 <span className="text-[12px] text-foreground/45">{gov.metering.city}</span>
 </div>
 <p className="text-[13px] text-foreground/65 leading-relaxed mt-1 mb-2">
 The machine works through the civic record at a steady, safe pace —
 never a firehose. Widen this as clean cycles prove the rate is safe.
 </p>

 <div className="flex items-center justify-between gap-4 py-3 border-t border-[var(--line)]/60">
 <div className="min-w-0">
 <p className="text-[14px] text-white font-medium">Meetings per day</p>
 <p className="text-[12px] text-foreground/55 mt-0.5">
 The most it may process in a day. Start low; walk it up.
 </p>
 </div>
 <Stepper
 value={gov.calibration.videos_per_day}
 disabled={govBusy}
 min={0}
 max={50}
 onChange={v => setCalibration({ videos_per_day: v })}
 />
 </div>

 <div className="flex items-center justify-between gap-4 py-3 border-t border-[var(--line)]/60">
 <div className="min-w-0">
 <p className="text-[14px] text-white font-medium">Reviewers</p>
 <p className="text-[12px] text-foreground/55 mt-0.5">
 Each does a quick final-pass on about one meeting a day. The pace
 can't outrun the people checking it.
 </p>
 </div>
 <Stepper
 value={gov.calibration.reviewers}
 disabled={govBusy}
 min={1}
 max={50}
 onChange={v => setCalibration({ reviewers: v })}
 />
 </div>

 {/*budget / solvency dials. The balance is the pot the machine
 spends from; it never drains faster than the solvency window.
 Today this rarely binds (a meeting is <$1, and NotebookLM + Gemini
 run on consumer quota, not metered $) — but it's the hard safety
 rail against runaway spend + the funded-tier ceiling. */}
 <div className="flex items-center justify-between gap-4 py-3 border-t border-[var(--line)]/60">
 <div className="min-w-0">
 <p className="text-[14px] text-white font-medium">Budget balance</p>
 <p className="text-[12px] text-foreground/55 mt-0.5">
 The pot it spends from. The pace never drains this faster than the
 solvency window below. Leave empty if you're not capping by money yet.
 </p>
 </div>
 <MoneyField
 value={gov.calibration.available_balance}
 disabled={govBusy}
 allowEmpty
 width="w-28"
 onCommit={v => setCalibration({ available_balance: v })}
 />
 </div>

 <div className="flex items-center justify-between gap-4 py-3 border-t border-[var(--line)]/60">
 <div className="min-w-0">
 <p className="text-[14px] text-white font-medium">Cost per meeting</p>
 <p className="text-[12px] text-foreground/55 mt-0.5">
 A deliberately high estimate (transcription scales with meeting
 length). Turns the balance into a safe daily rate.
 </p>
 </div>
 <MoneyField
 value={gov.calibration.cost_per_video}
 disabled={govBusy}
 width="w-24"
 onCommit={v => setCalibration({ cost_per_video: v ?? 0 })}
 />
 </div>

 <div className="flex items-center justify-between gap-4 py-3 border-t border-[var(--line)]/60">
 <div className="min-w-0">
 <p className="text-[14px] text-white font-medium">Stay solvent for</p>
 <p className="text-[12px] text-foreground/55 mt-0.5">
 How long the balance must last. The machine won't spend faster
 than this window allows.
 </p>
 </div>
 <div className="inline-flex items-center gap-2">
 <Stepper
 value={gov.calibration.solvency_days}
 disabled={govBusy}
 min={1}
 max={365}
 onChange={v => setCalibration({ solvency_days: v })}
 />
 <span className="text-[12px] text-foreground/45">days</span>
 </div>
 </div>

 {/* Live readout — what the dial means right now. */}
 <div className="mt-4 pt-4 border-t border-[var(--line)]/60">
 <p className="text-[13px] text-white/90 leading-relaxed">
 <span className="font-semibold text-[#F2A91C]">
 {fmtRate(gov.metering.ceilings.effective_per_day)} a day
 </span>{" "}
 is the real pace right now{boundClause(gov.metering.ceilings)}.
 </p>
 <p className="text-[13px] text-foreground/70 leading-relaxed mt-1.5">
 {gov.metering.progress.processed} processed ·{" "}
 {gov.metering.progress.candidate_unprocessed} still to process ·{" "}
 {gov.metering.today.processed_today} done today ·{" "}
 {gov.metering.today.under_ceiling ? (
 <>
 room for{" "}
 <span className="text-[#22C55E]">
 {fmtRate(gov.metering.today.room_today)} more
 </span>{" "}
 today
 </>
 ) : (
 <span className="text-foreground/60">at today's limit</span>
 )}
 </p>
 {gov.metering.days_to_drain != null &&
 gov.metering.progress.candidate_unprocessed > 0 && (
 <p className="text-[13px] text-foreground/70 mt-1.5">
 About {gov.metering.days_to_drain} day
 {gov.metering.days_to_drain === 1 ? "" : "s"} to catch up at this pace.
 </p>
 )}
 {gov.metering.budget.configured ? (
 <p className="text-[13px] text-foreground/70 mt-1.5">
 Budget allows{" "}
 <span className="text-white/90">
 {fmtRate(gov.metering.budget.budget_per_day ?? 0)} a day
 </span>
 {gov.metering.budget.runway_days != null && (
 <>
 {" "}· about{" "}
 <span className="text-white/90">
 {Math.round(gov.metering.budget.runway_days)} days
 </span>{" "}
 of runway at this pace
 </>
 )}
 .
 </p>
 ) : (
 <p className="text-[12px] text-foreground/45 mt-1.5">
 No money cap set — add a balance above to track runway.
 </p>
 )}
 {gov.metering.next_meeting && (
 <p className="text-[12px] text-foreground/55 mt-2 truncate">
 Next up: {gov.metering.next_meeting.meeting_title}
 </p>
 )}
 {gov.metering.progress.needs_video_url > 0 && (
 <p className="text-[12px] text-[#F2A91C]/80 mt-1">
 {gov.metering.progress.needs_video_url} waiting on a video link from you.
 </p>
 )}
 </div>
 </section>
 )}

 {loading && !caps && (
 <p className="text-[14px] text-foreground/50 py-10 text-center">Loading…</p>
 )}

 {[1, 2, 3, 4].map(rung => {
 const items = grouped[rung];
 if (!items || items.length === 0) return null;
 const meta = RUNG_META[rung];
 return (
 <section key={rung} className="mb-8">
 <header className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-[var(--line)] pb-2">
 <h2 className="text-[14px] font-semibold text-white">
 {meta.title}
 </h2>
 <span className="text-[12px] text-foreground/45">{meta.blurb}</span>
 </header>
 <ul className="flex flex-col gap-3">{items.map(renderCap)}</ul>
 </section>
 );
 })}
 </main>
 </div>
 );
}

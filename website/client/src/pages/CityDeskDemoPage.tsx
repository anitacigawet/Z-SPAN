/**
 * CityDeskDemoPage — the try-it walkthrough demo of the City Desk
 *iteration 3).
 *
 * This is the end-to-end mock a city manager can actually drive:
 * start an agenda → type in items (their own placeholder data) → watch
 * the approvals route while the tree grows → run the mock meeting →
 * adjourn and see the minutes assemble. NOTHING is saved — pure React
 * session state, no localStorage, refresh wipes it (deliberate: the
 * demo is for trying, not using).
 *
 * The display/interaction logic here is the operator's PrisonBreak
 * CaseDetail orchestration, ported rather than redesigned (his
 * direction: "all the logic for how to work with it for a demo is
 * there and done, so we should not try to replicate that part"):
 *
 * - The split stage: the tree pane runs FULL-width while the agenda
 * is being entered and while it grows, then eases to 50% over
 * 700ms when it blooms, with the right drawer sliding in at 50%
 * width (opacity fading in over 500ms with a 200ms delay) — their
 * exact transition choreography.
 * - The growth indicator pinned top-right of the stage: hand-framed
 * status word with a pulsing dot, the hand-drawn arrow + caption,
 * and the "n/m · building" mono line underneath.
 * - The pre-bloom card under the tree: the phase-driven CTA card
 * (their upload→analyze→grow becomes enter-items→route-approvals).
 * - Sequential growth: leaves build ONE at a time with progressive
 * hatch fill and the growing callout following the active leaf
 * (their socket-driven progress becomes a demo timer).
 * - The hand-note that sits under the tree while it grows.
 * - The right drawer's tabbed sections (their Timeline/Notes/… become
 * Agenda / Meeting / Minutes / Behind it).
 *
 * Their front-page case manager became the "agenda shelf" here — same
 * idea, renamed for this desk (start a new agenda ↔ their new case).
 *
 * Access: operator-only, same gate as the desk. Entered from the City
 * Desk page's "try the walkthrough demo" button.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { CityDeskPlant, type LeafEntry, type LeafStatus } from "../components/CityDeskPlant";
import { PAPER_CSS, usePaperFonts } from "../components/cityDeskTheme";
import { CityDeskTour, hasSeenTour, type TourActions } from "../components/CityDeskTour";

type Vote = "yes" | "no" | null;

interface DemoItem {
 id: string;
 title: string;
 dept: string;
 fiscal: string;
 /** 0 = drafted only; grows to 3 (=on the agenda) during routing. */
 stage: number;
 /** 0..100 within the currently-building stage span. */
 progress: number;
}

interface DemoAgenda {
 id: string;
 name: string;
 date: string;
 items: DemoItem[];
 phase: "intake" | "routing" | "bloomed";
 votes: Record<string, Record<string, Vote>>;
 heard: string[];
 adjourned: boolean;
}

const MEMBERS = ["Mayor", "Vice Mayor", "Seat 3", "Seat 4", "Seat 5"];
const DEPTS = ["Public Works", "Utilities", "Community Services", "Police", "Fire", "Finance"];
const STAGE_LABELS = ["Drafted", "Fiscal review", "Legal sign-off", "On the agenda"];

let demoSeq = 100;

function sampleAgenda(): DemoAgenda {
 return {
 id: "demo-sample",
 name: "Regular Council Meeting",
 date: "first Tuesday, 6:00 pm",
 items: [
 { id: "A-1", title: "Trailhead maintenance contract renewal", dept: "Public Works", fiscal: "$48,500", stage: 0, progress: 0 },
 { id: "A-2", title: "Well pump replacement — emergency purchase", dept: "Utilities", fiscal: "$262,600", stage: 0, progress: 0 },
 { id: "A-3", title: "Animal shelter services agreement", dept: "Community Services", fiscal: "$120,000", stage: 0, progress: 0 },
 ],
 phase: "intake",
 votes: {},
 heard: [],
 adjourned: false,
 };
}

// ── The ported GrowthIndicator (hand-framed status, pulsing dot,
// hand-drawn arrow + caption, mono count line) ─────────────────────
function GrowthIndicator({
 label,
 dotColor,
 pulsing,
 countLine,
 showAnnotation,
 compact = false,
}: {
 label: string;
 dotColor: string;
 pulsing: boolean;
 countLine: string;
 showAnnotation: boolean;
 /** Bloomed split-stage: the pane is half-width, so the badge shrinks
 * and tucks tighter to stay off the upper leaves (Opus finding 6). */
 compact?: boolean;
}) {
 return (
 <div className="pointer-events-none absolute z-10 flex flex-col items-end" style={{ top: compact ? 8 : 18, right: compact ? 10 : 22 }} data-tour="indicator">
 <div
 style={{
 border: "1.4px solid var(--ink)",
 padding: "8px 16px 6px",
 fontFamily: '"Atkinson Hyperlegible", sans-serif',
 fontWeight: 700,
 fontSize: compact ? 14 : 18,
 lineHeight: 1,
 background: "var(--paper-deep)",
 color: "var(--ink)",
 boxShadow: pulsing ? "0 0 0 4px oklch(0.66 0.18 25 / .12)" : "none",
 transition: "box-shadow .3s",
 borderRadius: 4,
 }}
 >
 <span
 className="inline-block align-middle"
 style={{
 width: 10,
 height: 10,
 borderRadius: 9999,
 background: dotColor,
 marginRight: 10,
 animation: pulsing ? "cd-ink-blink 1s infinite" : "none",
 }}
 />
 {label}
 </div>
 {showAnnotation && (
 <>
 <svg width="150" height="58" viewBox="0 0 150 58" style={{ marginTop: -2, marginRight: 4 }}>
 <path d="M 110 6 C 90 18, 70 28, 50 40" stroke="var(--ink)" strokeWidth="1.2" fill="none" strokeLinecap="round" />
 <path d="M 52 32 L 50 40 L 58 40" stroke="var(--ink)" strokeWidth="1.2" fill="none" strokeLinecap="round" />
 <text
 x="0"
 y="54"
 fontFamily='"Atkinson Hyperlegible", sans-serif'
 fontStyle="italic"
 fontSize="13"
 fill="var(--ink)"
 >
 indicator
 </text>
 </svg>
 <div className="mono" style={{ fontSize: 10.5, color: "var(--ink-soft)", marginTop: -6, letterSpacing: "0.06em" }}>
 {countLine}
 </div>
 </>
 )}
 </div>
 );
}

interface CityDeskDemoPageProps {
 onNavigate: (view: string, params?: any) => void;
}

export function CityDeskDemoPage({ onNavigate }: CityDeskDemoPageProps) {
 usePaperFonts();

 // Session-only shelf of agendas. No persistence anywhere, on purpose.
 const [agendas, setAgendas] = useState<DemoAgenda[]>([sampleAgenda()]);
 const [openId, setOpenId] = useState<string | null>(null);
 const [newName, setNewName] = useState("");
 const [newDate, setNewDate] = useState("");

 // Detail-screen local inputs.
 const [itemTitle, setItemTitle] = useState("");
 const [itemDept, setItemDept] = useState(DEPTS[0]);
 const [itemFiscal, setItemFiscal] = useState("");
 const [drawerTab, setDrawerTab] = useState<"agenda" | "meeting" | "minutes" | "behind">("agenda");
 const [calledId, setCalledId] = useState<string | null>(null);
 const [timerLeft, setTimerLeft] = useState<number | null>(null);

 // The-Cacti-pattern guided tour: auto-starts on first visit (the seen
 // flag is the ONE operator-side write; visitor demo data stays
 // session-only), replayable via the "▶ tour" button.
 const [tourRun, setTourRun] = useState(() => !hasSeenTour());
 // Staggered sample-vote timers (tour step 7) — retained so skipping
 // the tour can't leave ghost votes ticking in after handover
 // (2026-07-02 brainstorm-audit F2).
 const voteTimersRef = useRef<number[]>([]);
 const clearVoteTimers = () => {
 voteTimersRef.current.forEach((id) => window.clearTimeout(id));
 voteTimersRef.current = [];
 };

 const agenda = agendas.find((a) => a.id === openId) ?? null;
 const patch = (fn: (a: DemoAgenda) => DemoAgenda) =>
 setAgendas((all) => all.map((a) => (a.id === openId ? fn(a) : a)));
 const patchSample = (fn: (a: DemoAgenda) => DemoAgenda) =>
 setAgendas((all) => all.map((a) => (a.id === "demo-sample" ? fn(a) : a)));

 // Everything the tour drives operates on the SAMPLE agenda only.
 const tourActions: TourActions = {
 gotoShelf: () => setOpenId(null),
 openSample: () => {
 setOpenId("demo-sample");
 setDrawerTab("agenda");
 },
 startSampleRouting: () => {
 patchSample((a) => (a.phase === "intake" ? { ...a, phase: "routing" } : a));
 window.scrollTo({ top: 0, behavior: "smooth" });
 },
 sampleIsBloomed: () =>
 agendas.find((a) => a.id === "demo-sample")?.phase === "bloomed",
 castSampleVotes: () => {
 const sample = agendas.find((a) => a.id === "demo-sample");
 const first = sample?.items[0];
 if (!first) return;
 setCalledId(first.id);
 // Staggered so the record leaf visibly fills while the tour talks.
 const casts: Array<[string, Vote]> = [
 [MEMBERS[0], "yes"],
 [MEMBERS[1], "yes"],
 [MEMBERS[2], "no"],
 [MEMBERS[3], "yes"],
 [MEMBERS[4], "yes"],
 ];
 casts.forEach(([member, v], i) => {
 const tid = window.setTimeout(() => {
 patchSample((a) => ({
 ...a,
 heard: a.heard.includes(first.id) ? a.heard : [...a.heard, first.id],
 votes: {
 ...a.votes,
 [first.id]: { ...(a.votes[first.id] || {}), [member]: v },
 },
 }));
 }, 500 + i * 450);
 voteTimersRef.current.push(tid);
 });
 },
 adjournSample: () => patchSample((a) => ({ ...a, adjourned: true })),
 resetSample: () => {
 clearVoteTimers();
 patchSample(() => sampleAgenda());
 setCalledId(null);
 setOpenId("demo-sample");
 setDrawerTab("agenda");
 },
 setDrawerTab: (tab) => setDrawerTab(tab),
 };

 // ── Sequential routing engine (their socket-driven growth, demo-
 // timed): the first not-ready item ticks progress; each full bar
 // advances one stage; item completes → next item starts. ─────────
 const routingRef = useRef<ReturnType<typeof setInterval> | null>(null);
 useEffect(() => {
 if (!agenda || agenda.phase !== "routing") return;
 routingRef.current = setInterval(() => {
 patch((a) => {
 const idx = a.items.findIndex((it) => it.stage < 3);
 if (idx === -1) {
 return { ...a, phase: "bloomed" };
 }
 const items = a.items.map((it, i) => {
 if (i !== idx) return it;
 const nextProgress = it.progress + 9;
 if (nextProgress >= 100) {
 return { ...it, stage: it.stage + 1, progress: it.stage + 1 >= 3 ? 100 : 0 };
 }
 return { ...it, progress: nextProgress };
 });
 return { ...a, items };
 });
 }, 120);
 return () => {
 if (routingRef.current) clearInterval(routingRef.current);
 routingRef.current = null;
 };
 // eslint-disable-next-line react-hooks/exhaustive-deps
 }, [agenda?.phase, openId]);

 // Comment clock (mock meeting).
 useEffect(() => {
 if (timerLeft === null || timerLeft <= 0) return;
 const t = setTimeout(() => setTimerLeft((v) => (v === null ? null : v - 1)), 1000);
 return () => clearTimeout(t);
 }, [timerLeft]);

 // ── Derivations ─────────────────────────────────────────────────────
 const isBloomed = agenda?.phase === "bloomed";
 const isRouting = agenda?.phase === "routing";

 const buildingIdx = agenda ? agenda.items.findIndex((it) => it.stage < 3) : -1;

 const leaves = useMemo<LeafEntry[]>(() => {
 if (!agenda) return [];
 const itemLeaves: LeafEntry[] = agenda.items.slice(0, 6).map((it) => {
 let status: LeafStatus;
 let progress = 0;
 if (it.stage >= 3) {
 status = "completed";
 progress = 100;
 } else if (agenda.phase === "intake") {
 status = "pending";
 } else if (it.stage === 0 && it.progress === 0) {
 status = "pending";
 } else {
 status = "building";
 progress = ((it.stage + it.progress / 100) / 3) * 100;
 }
 return {
 key: it.id,
 label: it.title.length > 34 ? it.title.slice(0, 33) + "…" : it.title,
 subs: [
 it.dept.toLowerCase(),
 it.fiscal,
 it.stage < 3 ? `next: ${STAGE_LABELS[it.stage + 1].toLowerCase()}` : "on the agenda",
 ],
 status,
 progress,
 };
 });
 while (itemLeaves.length < 6) {
 itemLeaves.push({ key: `empty-${itemLeaves.length}`, label: "open slot", subs: [], status: "skipped", progress: 0 });
 }
 const votesLogged = Object.values(agenda.votes).reduce(
 (n, v) => n + Object.values(v).filter(Boolean).length,
 0,
 );
 itemLeaves.push({
 key: "record",
 label: "The meeting record",
 subs: [`items heard: ${agenda.heard.length}`, `votes logged: ${votesLogged}`],
 status: agenda.adjourned ? "completed" : agenda.heard.length > 0 ? "building" : "pending",
 progress: agenda.items.length
 ? Math.min(100, (agenda.heard.length / agenda.items.length) * 100)
 : 0,
 });
 itemLeaves.push({
 key: "minutes",
 label: "Official minutes",
 subs: ["assembled from the meeting", "a clerk certifies"],
 status: agenda.adjourned ? "completed" : "pending",
 progress: agenda.adjourned ? 100 : 0,
 });
 return itemLeaves;
 }, [agenda]);

 const activeLeafKey = isRouting && buildingIdx >= 0 ? agenda!.items[buildingIdx].id : calledId;

 const addItem = () => {
 const t = itemTitle.trim();
 if (!t || !agenda) return;
 patch((a) => ({
 ...a,
 items:
 a.items.length >= 6
 ? a.items
 : [
 ...a.items,
 {
 id: `A-${a.items.length + 1}`,
 title: t,
 dept: itemDept,
 fiscal: itemFiscal.trim() || "no fiscal impact",
 stage: 0,
 progress: 0,
 },
 ],
 }));
 setItemTitle("");
 setItemFiscal("");
 };

 const tallyFor = (itemId: string) => {
 const v = agenda?.votes[itemId] || {};
 const yes = Object.values(v).filter((x) => x === "yes").length;
 const no = Object.values(v).filter((x) => x === "no").length;
 return { yes, no, silent: MEMBERS.length - yes - no };
 };

 const indicator = (() => {
 if (!agenda) return { label: "", dot: "var(--ink-soft)", pulsing: false, count: "", annotate: false };
 if (agenda.adjourned)
 return { label: "Adjourned", dot: "var(--bloom)", pulsing: false, count: `${agenda.heard.length} items heard · minutes drafted`, annotate: true };
 if (isBloomed)
 return { label: "Ready", dot: "var(--bloom)", pulsing: false, count: `${agenda.items.length}/${agenda.items.length} items · on the agenda`, annotate: true };
 if (isRouting)
 return {
 label: "Routing",
 dot: "var(--hot)",
 pulsing: true,
 count: `${Math.min(buildingIdx + 1, agenda.items.length)}/${agenda.items.length} items · routing`,
 annotate: true,
 };
 return { label: "Drafting", dot: "var(--amber)", pulsing: agenda.items.length > 0, count: "", annotate: false };
 })();

 // ── Screen 1 — the agenda shelf (their case manager, renamed) ───────
 if (!agenda) {
 return (
 <div className="city-desk">
 <style>{PAPER_CSS}</style>
 <div className="mx-auto max-w-5xl px-6 py-8">
 <div className="mb-2 flex flex-wrap items-center gap-4">
 <span className="ink-pill">Z-SPAN · City Desk — demo</span>
 <button type="button" onClick={() => setTourRun(true)} className="ink-btn ghost ml-auto">
 ▶ tour
 </button>
 <button type="button" onClick={() => onNavigate("city-desk")} className="ink-btn ghost">
 ← back to the desk
 </button>
 </div>
 <h1 className="mb-1 text-[42px] leading-tight">Try it: an agenda, start to finish.</h1>
 <p className="mb-3 max-w-2xl text-[16px] leading-relaxed" style={{ color: "var(--ink-soft)" }}>
 Open the sample meeting or start your own. You'll enter agenda
 items, watch their approvals route while the tree grows, run the
 mock meeting, and see the minutes assemble at adjournment.
 </p>
 <div className="note-band kalam mb-8 px-4 py-2.5 text-[14px]">
 Walkthrough demo — nothing you type here is saved anywhere.
 Refreshing the page clears it completely.
 </div>

 <div className="grid grid-cols-1 gap-4 sm:grid-cols-2" data-tour="shelf">
 {agendas.map((a) => (
 <button
 key={a.id}
 type="button"
 onClick={() => {
 setOpenId(a.id);
 setDrawerTab("agenda");
 }}
 className="ink-frame p-5 text-left transition-opacity hover:opacity-90"
 >
 <div className="text-[17px] font-bold" style={{ color: "var(--ink)" }}>
 {a.name}
 </div>
 <div className="mono mt-1 text-[12px]" style={{ color: "var(--ink-soft)" }}>
 {a.date} · {a.items.length} item{a.items.length !== 1 ? "s" : ""} ·{" "}
 {a.adjourned ? "adjourned" : a.phase === "bloomed" ? "agenda set" : a.phase === "routing" ? "routing approvals" : "drafting"}
 </div>
 </button>
 ))}

 <div className="ink-frame-soft flex flex-col gap-2 p-5">
 <div className="text-[16px] font-bold" style={{ color: "var(--ink)" }}>
 start a new agenda
 </div>
 <input
 className="ink-input"
 placeholder="meeting name (e.g. Special Session)"
 value={newName}
 onChange={(e) => setNewName(e.target.value)}
 />
 <input
 className="ink-input"
 placeholder="when (e.g. July 15, 6:00 pm)"
 value={newDate}
 onChange={(e) => setNewDate(e.target.value)}
 />
 <button
 type="button"
 className="ink-btn mt-1 self-start"
 disabled={!newName.trim()}
 onClick={() => {
 const id = `demo-${demoSeq++}`;
 setAgendas((all) => [
 ...all,
 {
 id,
 name: newName.trim(),
 date: newDate.trim() || "date to be set",
 items: [],
 phase: "intake",
 votes: {},
 heard: [],
 adjourned: false,
 },
 ]);
 setNewName("");
 setNewDate("");
 setOpenId(id);
 setDrawerTab("agenda");
 }}
 >
 open it
 </button>
 </div>
 </div>
 </div>
 <CityDeskTour run={tourRun} actions={tourActions} onFinish={() => { clearVoteTimers(); setTourRun(false); }} />
 </div>
 );
 }

 // ── Screen 2 — the split stage (their CaseDetail choreography) ──────
 return (
 <div className="city-desk">
 <style>{PAPER_CSS}</style>
 <div className="mx-auto max-w-[1500px] px-4 py-6">
 {/* Header row */}
 <div className="mb-2 flex flex-wrap items-center gap-4 px-2">
 <span className="ink-pill">Z-SPAN · City Desk — demo</span>
 <span className="text-[16px] font-bold" style={{ color: "var(--ink)" }}>
 {agenda.name}
 </span>
 <span className="mono text-[11px]" style={{ color: "var(--ink-soft)" }}>
 {agenda.date}
 </span>
 <span className="kalam ml-auto text-[12.5px]" style={{ color: "var(--disclaimer)" }}>
 demo — nothing is saved
 </span>
 <button type="button" onClick={() => setTourRun(true)} className="ink-btn ghost">
 ▶ tour
 </button>
 <button type="button" onClick={() => setOpenId(null)} className="ink-btn ghost">
 ← all agendas
 </button>
 </div>

 {/* Split stage — the ported 700ms choreography */}
 <div className="relative flex items-stretch" style={{ padding: "0 8px" }}>
 {/* Left pane: the tree */}
 <div
 className="relative flex min-w-0 flex-col items-center"
 style={{
 width: isBloomed ? "50%" : "100%",
 transition: "width 700ms ease-in-out",
 }}
 >
 <GrowthIndicator
 label={indicator.label}
 dotColor={indicator.dot}
 pulsing={indicator.pulsing}
 countLine={indicator.count}
 showAnnotation={indicator.annotate}
 compact={!!isBloomed}
 />

 <div
 className="relative mx-auto"
 data-tour="stage"
 style={{
 marginTop: 8,
 aspectRatio: "760 / 980",
 width: isBloomed ? "min(72vh, 600px)" : "min(85vh, 760px)",
 transition: "width 700ms ease-in-out",
 maxWidth: "100%",
 }}
 >
 <CityDeskPlant
 leaves={leaves}
 activeKey={activeLeafKey}
 calloutTitle={"Growing item"}
 calloutSub={"( routing approvals )"}
 hideCallout={!isRouting}
 />
 </div>

 {/* Pre-bloom card — their phase-driven CTA card, ported */}
 {agenda.phase === "intake" && (
 <div className="ink-frame -mt-2 mb-4 w-full max-w-xl p-4" data-tour="intake">
 <div className="mb-1 text-[16px] font-bold">What's on this agenda?</div>
 <p className="mb-3 text-[14px]" style={{ color: "var(--ink-soft)" }}>
 Add up to six items — each becomes a leaf. When you're ready,
 send them off and watch the approvals route.
 </p>
 {agenda.items.length > 0 && (
 <ul className="mb-3 space-y-1">
 {agenda.items.map((it) => (
 <li key={it.id} className="flex items-baseline gap-2 text-[14px]">
 <span className="mono text-[11.5px]" style={{ color: "var(--ink-soft)" }}>
 {it.id}
 </span>
 <span>{it.title}</span>
 <span className="mono ml-auto text-[10.5px]" style={{ color: "var(--ink-soft)" }}>
 {it.dept} · {it.fiscal}
 </span>
 </li>
 ))}
 </ul>
 )}
 <div className="flex flex-wrap items-center gap-2">
 <input
 className="ink-input min-w-[180px] flex-1"
 placeholder="add an item…"
 value={itemTitle}
 onChange={(e) => setItemTitle(e.target.value)}
 onKeyDown={(e) => e.key === "Enter" && addItem()}
 disabled={agenda.items.length >= 6}
 />
 <select className="ink-input" value={itemDept} onChange={(e) => setItemDept(e.target.value)}>
 {DEPTS.map((d) => (
 <option key={d}>{d}</option>
 ))}
 </select>
 <input
 className="ink-input w-28"
 placeholder="$ amount"
 value={itemFiscal}
 onChange={(e) => setItemFiscal(e.target.value)}
 />
 <button type="button" className="ink-btn ghost" onClick={addItem} disabled={!itemTitle.trim() || agenda.items.length >= 6}>
 add
 </button>
 </div>
 <button
 type="button"
 className="ink-btn mt-3"
 disabled={agenda.items.length === 0}
 onClick={() => {
 patch((a) => ({ ...a, phase: "routing" }));
 // The tree takes over the full stage — bring the
 // viewer up to it (operator feedback 2026-07-02).
 window.scrollTo({ top: 0, behavior: "smooth" });
 }}
 >
 route {agenda.items.length || "the"} item{agenda.items.length !== 1 ? "s" : ""} for approval →
 </button>
 </div>
 )}

 {/* The hand-note that sits under the tree while it grows */}
 {isRouting && (
 <div className="text-center italic" style={{ color: "var(--ink-soft)", fontSize: 14, marginTop: 4, marginBottom: 12 }}>
 the page sits here while the agenda grows — each leaf is an item.
 </div>
 )}
 </div>

 {/* Right drawer — slides in at bloom, their exact timing */}
 <div
 className="flex flex-shrink-0 overflow-hidden"
 aria-hidden={!isBloomed}
 style={{
 width: isBloomed ? "50%" : "0%",
 opacity: isBloomed ? 1 : 0,
 minHeight: isBloomed ? "min(88vh, 880px)" : 0,
 transition: "width 700ms ease-in-out, opacity 500ms ease-in-out 200ms",
 }}
 >
 <div className="ink-frame m-2 flex w-full flex-col p-4" data-tour="drawer">
 <div className="mb-3 flex flex-wrap gap-1">
 {(
 [
 ["agenda", "Agenda"],
 ["meeting", "Meeting"],
 ["minutes", "Minutes"],
 ["behind", "Behind it"],
 ] as const
 ).map(([key, label]) => (
 <button
 key={key}
 type="button"
 className={`drawer-tab ${drawerTab === key ? "active" : ""}`}
 onClick={() => setDrawerTab(key)}
 >
 {label}
 </button>
 ))}
 </div>

 <div className="min-h-0 flex-1 overflow-y-auto pr-1">
 {drawerTab === "agenda" && (
 <div className="space-y-2.5">
 <p className="text-[14px]" style={{ color: "var(--ink-soft)" }}>
 Every item routed clean — drafted, fiscally reviewed,
 legally signed off, set by the clerk. This is the agenda
 the public side publishes automatically.
 </p>
 {agenda.items.map((it) => (
 <div key={it.id} className="ink-frame-soft flex items-center gap-3 px-4 py-2.5">
 <span className="ink-pulse is-done" />
 <div className="min-w-0">
 <div className="text-[15px]">{it.title}</div>
 <div className="mono text-[11.5px]" style={{ color: "var(--ink-soft)" }}>
 {it.id} · {it.dept} · {it.fiscal}
 </div>
 </div>
 </div>
 ))}
 </div>
 )}

 {drawerTab === "meeting" && (
 <div data-tour="meeting">
 <p className="mb-3 text-[14px]" style={{ color: "var(--ink-soft)" }}>
 Call an item, run the comment clock, tap each member's
 vote as it's spoken. Watch the tall center leaf fill in
 as the record grows.
 </p>
 <div className="graph-paper ink-frame-soft p-4">
 <div className="mb-3 space-y-1">
 {agenda.items.map((it) => (
 <button
 key={it.id}
 type="button"
 onClick={() => setCalledId(it.id)}
 className="block w-full text-left text-[14px]"
 style={{
 color: calledId === it.id ? "var(--ink)" : "var(--ink-soft)",
 fontWeight: calledId === it.id ? 700 : 400,
 }}
 >
 {calledId === it.id ? "▸ " : " "}
 {it.id} — {it.title}
 </button>
 ))}
 </div>
 <div className="mb-3 flex items-center gap-3">
 <button type="button" className="ink-btn ghost" onClick={() => setTimerLeft(180)}>
 start 3:00 comment clock
 </button>
 {timerLeft !== null && (
 <span className="mono text-[19px]" style={{ color: timerLeft <= 15 ? "var(--hot)" : "var(--ink)" }}>
 {Math.floor(timerLeft / 60)}:{String(timerLeft % 60).padStart(2, "0")}
 </span>
 )}
 </div>
 {calledId ? (
 <>
 <div className="space-y-1.5">
 {MEMBERS.map((m) => {
 const v = agenda.votes[calledId]?.[m] ?? null;
 const set = (nv: Vote) =>
 patch((a) => ({
 ...a,
 heard: a.heard.includes(calledId) ? a.heard : [...a.heard, calledId],
 votes: { ...a.votes, [calledId]: { ...(a.votes[calledId] || {}), [m]: nv } },
 }));
 return (
 <div key={m} className="flex items-center gap-2.5">
 <span className="w-24 text-[14px]">{m}</span>
 <button type="button" className={`vote-box ${v === "yes" ? "checked-yes" : ""}`} onClick={() => set(v === "yes" ? null : "yes")} title="Aye">
 {v === "yes" ? "✓" : ""}
 </button>
 <button type="button" className={`vote-box ${v === "no" ? "checked-no" : ""}`} onClick={() => set(v === "no" ? null : "no")} title="Nay">
 {v === "no" ? "✗" : ""}
 </button>
 </div>
 );
 })}
 </div>
 <div className="mono mt-2 text-[11.5px]" style={{ color: "var(--ink-soft)" }}>
 {(() => {
 const t = tallyFor(calledId);
 return `tally: ${t.yes} aye · ${t.no} nay · ${t.silent} not voting`;
 })()}
 </div>
 </>
 ) : (
 <p className="kalam text-[13.5px]" style={{ color: "var(--ink-soft)" }}>
 call an item above to open its vote sheet.
 </p>
 )}
 </div>
 <button
 type="button"
 className="ink-btn mt-3"
 disabled={agenda.heard.length === 0 || agenda.adjourned}
 onClick={() => {
 patch((a) => ({ ...a, adjourned: true }));
 setDrawerTab("minutes");
 }}
 >
 {agenda.adjourned ? "adjourned ✓" : "adjourn & draft the minutes"}
 </button>
 </div>
 )}

 {drawerTab === "minutes" &&
 (agenda.adjourned ? (
 <div className="ink-frame-soft cd-print-area px-5 py-4" data-tour="minutes-area">
 <div className="mb-1 text-[16px] font-bold">DRAFT — Minutes, {agenda.name}</div>
 <div className="mono mb-2 text-[10.5px]" style={{ color: "var(--ink-soft)" }}>
 assembled from the meeting record · {agenda.date}
 </div>
 <div className="space-y-2 text-[14px] leading-relaxed" style={{ color: "var(--ink-soft)" }}>
 {agenda.heard.map((id) => {
 const it = agenda.items.find((x) => x.id === id);
 if (!it) return null;
 const t = tallyFor(id);
 return (
 <p key={id}>
 <strong style={{ color: "var(--ink)" }}>{it.id}</strong> ({it.title}, {it.fiscal}) — motion{" "}
 {t.yes > t.no ? "carried" : "failed"}{" "}
 <span className="mono text-[12px]">
 {t.yes}–{t.no}
 </span>
 {t.silent > 0 ? `, ${t.silent} not voting` : ""}.
 </p>
 );
 })}
 <p>
 <em>
 Assembled the moment the gavel fell. A clerk reviews
 and certifies before it becomes the legal record —
 hours of typing becomes a read-through.
 </em>
 </p>
 </div>
 <button
 type="button"
 className="ink-btn ghost no-print mt-3"
 onClick={() => window.print()}
 >
 print this draft
 </button>
 </div>
 ) : (
 <p className="kalam text-[14px]" style={{ color: "var(--ink-soft)" }}>
 run the meeting first — the minutes assemble themselves at
 adjournment.
 </p>
 ))}

 {drawerTab === "behind" && (
 <div className="space-y-3 text-[14px]" style={{ color: "var(--ink-soft)" }}>
 <p>
 In the live desk, each hand-off you just watched is a real
 signature: the routing rides <strong style={{ color: "var(--ink)" }}>DocuSign</strong> envelopes
 (the webhook advances the item, not a click), and staff
 identity rides <strong style={{ color: "var(--ink)" }}>ID.me</strong> with a hardware key —
 the same verification federal agencies use.
 </p>
 <p>
 The public never waits on any of this: the moment an
 agenda is set, the network side publishes it — and after
 the meeting, the broadcast, transcript, and every vote
 land there with receipts.
 </p>
 <p className="kalam" style={{ color: "var(--disclaimer)" }}>
 In this demo those connections are simulated — which is
 also why nothing you entered was saved.
 </p>
 </div>
 )}
 </div>
 </div>
 </div>
 </div>
 </div>
 <CityDeskTour run={tourRun} actions={tourActions} onFinish={() => { clearVoteTimers(); setTourRun(false); }} />
 </div>
 );
}

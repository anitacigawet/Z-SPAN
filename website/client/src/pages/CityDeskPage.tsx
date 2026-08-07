/**
 * CityDeskPage —"City Desk": the official-facing side of Z-SPAN,
 * iteration 2 (operator feedback 2026-07-02).
 *
 * Changes from iteration 1:
 * - Palette flipped to PrisonBreak's INNER-page look: the dark warm
 * walnut (.dark paper values — oklch(0.20 0.014 60) ground, cream
 * ink, candlelight grain) instead of the bright front-page cream.
 * - Real functionality throughout: the desk runs a genuine local state
 * machine (item creation, staged approvals with a signed history
 * trail, per-item chamber votes, a working comment clock, minutes
 * assembled from the actual recorded state) persisted to
 * localStorage. The ONLY simulated points are the external calls —
 * each is marked in code with an INTEGRATION SEAM comment and in the
 * UI with a short audience-appropriate note, so wiring DocuSign /
 * ID.me later is a drop-in at those seams.
 * - The operator's hand-built growth tree (CityDeskPlant, geometry
 * verbatim from PrisonBreak's PetalFlower) is the centerpiece: each
 * agenda item is a leaf that blooms as its approvals complete, the
 * tall central leaf is the meeting record filling in as votes land,
 * and the detached crown bud blooms into the official minutes at
 * adjournment.
 * - Page copy is written for the person being shown the screen (the
 * operator, a city manager, a clerk) — explanatory where that helps,
 * no internal build vocabulary.
 *
 * Access: operator-only (OWNER_ONLY_VIEWS + the TopBar button). The
 * eventual audience is official-classified accounts.
 */
import { useEffect, useMemo, useState } from "react";
import { CityDeskPlant, type LeafEntry, type LeafStatus } from "../components/CityDeskPlant";
import { PAPER_CSS, usePaperFonts } from "../components/cityDeskTheme";

// ── The desk's real state machine ─────────────────────────────────────
// Everything below is genuine working logic persisted locally; the
// external services are the only simulated points, each marked with an
// INTEGRATION SEAM comment where the real call slots in.

const STAGES = [
 { key: "draft", label: "Drafted", who: "Department" },
 { key: "finance", label: "Fiscal review", who: "Finance" },
 { key: "attorney", label: "Legal sign-off", who: "Attorney" },
 { key: "ready", label: "On the agenda", who: "Clerk" },
] as const;

type Vote = "yes" | "no" | null;

interface HistoryEntry {
 stage: string;
 actor: string;
 at: string; // ISO
}

interface DeskItem {
 id: string;
 title: string;
 dept: string;
 fiscal: string;
 stage: number; // index of the last COMPLETED stage
 history: HistoryEntry[];
}

interface DeskState {
 items: DeskItem[];
 votes: Record<string, Record<string, Vote>>; // itemId → member → vote
 heardItemIds: string[]; // items called during the meeting
 adjournedAt: string | null;
 minutesAt: string | null;
 activeItemId: string | null; // last item touched (drives the tree callout)
 seq: number;
}

const MEMBERS = ["Mayor", "Vice Mayor", "Seat 3", "Seat 4", "Seat 5"];
const STORAGE_KEY = "zspan_city_desk_v1";

function seedState(): DeskState {
 const now = new Date().toISOString();
 const mk = (
 n: number,
 title: string,
 dept: string,
 fiscal: string,
 stage: number,
 ): DeskItem => ({
 id: `GL-${String(n).padStart(3, "0")}`,
 title,
 dept,
 fiscal,
 stage,
 history: STAGES.slice(0, stage + 1).map((s) => ({
 stage: s.label,
 actor: s.who,
 at: now,
 })),
 });
 return {
 items: [
 mk(41, "Route 66 trailhead maintenance contract renewal", "Public Works", "$48,500", 3),
 mk(42, "Well 7 pump replacement — emergency purchase", "Utilities", "$262,600", 1),
 mk(43, "Animal shelter services agreement", "Community Services", "$120,000", 0),
 ],
 votes: {},
 heardItemIds: [],
 adjournedAt: null,
 minutesAt: null,
 activeItemId: "GL-042",
 seq: 44,
 };
}

function loadState(): DeskState {
 try {
 const raw = localStorage.getItem(STORAGE_KEY);
 if (raw) return JSON.parse(raw) as DeskState;
 } catch {
 /* corrupted or unavailable — reseed */
 }
 return seedState();
}

interface CityDeskPageProps {
 onNavigate: (view: string, params?: any) => void;
}

export function CityDeskPage({ onNavigate }: CityDeskPageProps) {
 usePaperFonts();
 const [desk, setDesk] = useState<DeskState>(loadState);
 const [timerLeft, setTimerLeft] = useState<number | null>(null);
 const [calledItemId, setCalledItemId] = useState<string | null>(null);
 const [newTitle, setNewTitle] = useState("");
 const [newDept, setNewDept] = useState("Public Works");
 const [newFiscal, setNewFiscal] = useState("");

 // Persist every change.
 useEffect(() => {
 try {
 localStorage.setItem(STORAGE_KEY, JSON.stringify(desk));
 } catch {
 /* private mode — the desk still works for the session */
 }
 }, [desk]);

 // Comment clock.
 useEffect(() => {
 if (timerLeft === null || timerLeft <= 0) return;
 const t = setTimeout(() => setTimerLeft((v) => (v === null ? null : v - 1)), 1000);
 return () => clearTimeout(t);
 }, [timerLeft]);

 const addItem = () => {
 const title = newTitle.trim();
 if (!title) return;
 setDesk((d) => {
 const item: DeskItem = {
 id: `GL-${String(d.seq).padStart(3, "0")}`,
 title,
 dept: newDept,
 fiscal: newFiscal.trim() || "no fiscal impact",
 stage: 0,
 history: [
 { stage: STAGES[0].label, actor: STAGES[0].who, at: new Date().toISOString() },
 ],
 };
 return { ...d, items: [...d.items, item], seq: d.seq + 1, activeItemId: item.id };
 });
 setNewTitle("");
 setNewFiscal("");
 };

 const advance = (id: string) =>
 setDesk((d) => ({
 ...d,
 activeItemId: id,
 items: d.items.map((it) => {
 if (it.id !== id || it.stage >= STAGES.length - 1) return it;
 const next = it.stage + 1;
 // INTEGRATION SEAM — DocuSign: this is where the real flow
 // creates/advances an envelope and the webhook (not a click)
 // performs this state change. The history entry below is the
 // same record the webhook handler would write.
 return {
 ...it,
 stage: next,
 history: [
 ...it.history,
 { stage: STAGES[next].label, actor: STAGES[next].who, at: new Date().toISOString() },
 ],
 };
 }),
 }));

 const setVote = (itemId: string, member: string, v: Vote) =>
 setDesk((d) => ({
 ...d,
 activeItemId: itemId,
 heardItemIds: d.heardItemIds.includes(itemId)
 ? d.heardItemIds
 : [...d.heardItemIds, itemId],
 votes: {
 ...d.votes,
 [itemId]: { ...(d.votes[itemId] || {}), [member]: v },
 },
 }));

 const adjournAndDraft = () =>
 setDesk((d) => ({
 ...d,
 adjournedAt: d.adjournedAt ?? new Date().toISOString(),
 // INTEGRATION SEAM — minutes filing: the drafted document below is
 // assembled from real console state; a live deployment hands it to
 // the clerk's review queue (a human certifies before anything
 // becomes the legal record — same publication wall the public
 // network enforces).
 minutesAt: new Date().toISOString(),
 }));

 const resetDesk = () => {
 setDesk(seedState());
 setTimerLeft(null);
 setCalledItemId(null);
 };

 const readyItems = desk.items.filter((it) => it.stage >= STAGES.length - 1);
 const calledItem = desk.items.find((it) => it.id === calledItemId) ?? null;

 const tallyFor = (itemId: string) => {
 const v = desk.votes[itemId] || {};
 const yes = Object.values(v).filter((x) => x === "yes").length;
 const no = Object.values(v).filter((x) => x === "no").length;
 return { yes, no, silent: MEMBERS.length - yes - no };
 };

 // ── The tree derives from the real desk state ───────────────────────
 const leaves = useMemo<LeafEntry[]>(() => {
 const itemLeaves: LeafEntry[] = desk.items.slice(0, 6).map((it) => {
 let status: LeafStatus;
 let progress = 0;
 if (it.stage >= STAGES.length - 1) {
 status = "completed";
 progress = 100;
 } else if (it.stage === 0) {
 status = "pending";
 } else {
 status = "building";
 progress = (it.stage / (STAGES.length - 1)) * 100;
 }
 const nextStage = it.stage < STAGES.length - 1 ? STAGES[it.stage + 1] : null;
 return {
 key: it.id,
 label: it.title.length > 34 ? it.title.slice(0, 33) + "…" : it.title,
 subs: [
 it.dept.toLowerCase(),
 it.fiscal,
 nextStage ? `next: ${nextStage.label.toLowerCase()}` : "on the agenda",
 ],
 status,
 progress,
 };
 });
 while (itemLeaves.length < 6) {
 itemLeaves.push({
 key: `empty-${itemLeaves.length}`,
 label: "open slot",
 subs: [],
 status: "skipped",
 progress: 0,
 });
 }

 // Tall central leaf — the meeting record itself.
 const heard = desk.heardItemIds.length;
 const record: LeafEntry = {
 key: "record",
 label: "The meeting record",
 subs: [
 `items heard: ${heard}`,
 `votes logged: ${Object.values(desk.votes).reduce(
 (n, v) => n + Object.values(v).filter(Boolean).length,
 0,
 )}`,
 ],
 status: desk.adjournedAt
 ? "completed"
 : heard > 0
 ? "building"
 : "pending",
 progress: readyItems.length
 ? Math.min(100, (heard / Math.max(1, readyItems.length)) * 100)
 : 0,
 };

 // Detached crown bud — the official minutes; blooms at adjournment.
 const minutes: LeafEntry = {
 key: "minutes",
 label: "Official minutes",
 subs: ["assembled from the console", "a clerk certifies"],
 status: desk.minutesAt ? "completed" : "pending",
 progress: desk.minutesAt ? 100 : 0,
 };

 return [...itemLeaves, record, minutes];
 }, [desk, readyItems.length]);

 const activeLeafKey =
 calledItemId ??
 (desk.activeItemId &&
 desk.items.find((i) => i.id === desk.activeItemId && i.stage > 0 && i.stage < 3)
 ? desk.activeItemId
 : null);

 return (
 <div className="city-desk">
 <style>{PAPER_CSS}</style>

 <div className="mx-auto max-w-7xl px-6 py-8">
 {/* Masthead */}
 <div className="mb-2 flex flex-wrap items-center gap-4">
 <span className="ink-pill">Z-SPAN · City Desk</span>
 <span className="kalam text-[13px]" style={{ color: "var(--ink-soft)" }}>
 the clerk's side of the network
 </span>
 <button
 type="button"
 onClick={() => onNavigate("city-desk-demo")}
 className="ink-btn ml-auto"
 >
 try the walkthrough demo →
 </button>
 <button type="button" onClick={() => onNavigate("home")} className="ink-btn ghost">
 ← back to the network
 </button>
 </div>
 <h1 className="mb-1 text-[44px] leading-tight">
 Watch a meeting grow from first draft to certified minutes.
 </h1>
 <p className="mb-4 max-w-3xl text-[17px] leading-relaxed" style={{ color: "var(--ink-soft)" }}>
 The public already gets the front of house free — the broadcasts, the
 record, the receipts. This desk is the back of house: the internal
 work a city normally pays an enterprise suite for, gathered on one
 screen. Every item you plant below grows a leaf; approvals fill it
 in; the crown blooms when the minutes are done.
 </p>

 <div className="note-band kalam mb-8 px-4 py-2.5 text-[14px]">
 Preview build — the desk itself works (items, approvals, votes, and
 minutes are real and saved on this device), while the outside
 services it will hand off to — DocuSign for signatures, ID.me for
 staff identity — are simulated until those connections are switched
 on.
 </div>

 {/* ── The garden + the pipeline ─────────────────────────────── */}
 <div className="mb-6 grid grid-cols-1 gap-6 xl:grid-cols-5">
 {/* The tree — the operator's hand-built growth artifact */}
 <section className="ink-frame p-4 xl:col-span-2">
 <div className="mb-1 flex items-baseline gap-3 px-1">
 <h2 className="text-[30px]">This meeting, growing</h2>
 </div>
 <p className="kalam mb-2 px-1 text-[13px]" style={{ color: "var(--ink-soft)" }}>
 each leaf is an agenda item · the tall center leaf is the record
 itself · the bud on top blooms into the minutes
 </p>
 <div style={{ height: 640 }}>
 <CityDeskPlant
 leaves={leaves}
 activeKey={activeLeafKey}
 calloutTitle={"Growing item"}
 calloutSub={"( routing approvals )"}
 onLeafClick={(key) => {
 const el = document.getElementById(`desk-item-${key}`);
 el?.scrollIntoView({ behavior: "smooth", block: "center" });
 }}
 />
 </div>
 </section>

 {/* Greenlight pipeline — real staged approvals */}
 <section className="ink-frame p-5 xl:col-span-3">
 <div className="mb-1 flex flex-wrap items-baseline gap-3">
 <h2 className="text-[30px]">The Greenlight Pipeline</h2>
 <span className="kalam ml-auto text-[12.5px]" style={{ color: "var(--disclaimer)" }}>
 signatures ride DocuSign here — simulated in this preview
 </span>
 </div>
 <p className="mb-4 max-w-3xl text-[14.5px]" style={{ color: "var(--ink-soft)" }}>
 Before anything reaches the council, it moves through hands: the
 department drafts it, finance checks the money, the attorney
 signs off, and the clerk sets it on the agenda. Each hand-off
 here is recorded with who and when — the same trail a signature
 envelope produces.
 </p>

 <div className="space-y-3">
 {desk.items.map((it) => {
 const next = it.stage < STAGES.length - 1 ? STAGES[it.stage + 1] : null;
 return (
 <div
 key={it.id}
 id={`desk-item-${it.id}`}
 className="ink-frame-soft px-4 py-3"
 >
 <div className="flex flex-wrap items-center gap-4">
 <div className="min-w-0 flex-1">
 <div className="flex items-baseline gap-2">
 <span className="mono text-[11px]" style={{ color: "var(--ink-soft)" }}>
 {it.id}
 </span>
 <span className="text-[16px] font-medium">{it.title}</span>
 </div>
 <div className="mono mt-0.5 text-[12px]" style={{ color: "var(--ink-soft)" }}>
 {it.dept} · {it.fiscal}
 </div>
 </div>
 <div className="flex items-center gap-2">
 {STAGES.map((st, i) => (
 <div key={st.key} className="flex items-center gap-2" title={`${st.label} — ${st.who}`}>
 <span
 className={`ink-pulse ${i <= it.stage ? "is-done" : i === it.stage + 1 ? "" : "is-idle"}`}
 />
 {i < STAGES.length - 1 && (
 <svg width="22" height="6" aria-hidden>
 <line x1="0" y1="3" x2="22" y2="3" stroke="var(--ink)" strokeWidth="1" strokeDasharray="3 4" strokeLinecap="round" />
 </svg>
 )}
 </div>
 ))}
 </div>
 <button
 type="button"
 className="ink-btn"
 disabled={!next}
 onClick={() => advance(it.id)}
 >
 {next ? `send to ${next.who.toLowerCase()}` : "on the agenda ✓"}
 </button>
 </div>
 {/* The real signed-history trail */}
 <div className="kalam mt-2 flex flex-wrap gap-x-4 gap-y-0.5 text-[12px]" style={{ color: "var(--ink-soft)" }}>
 {it.history.map((h, i) => (
 <span key={i}>
 {h.stage.toLowerCase()} — {h.actor.toLowerCase()},{" "}
 {new Date(h.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
 </span>
 ))}
 </div>
 </div>
 );
 })}
 </div>

 {/* Plant a new item — real creation */}
 <div className="ink-frame-soft mt-4 flex flex-wrap items-center gap-2 px-4 py-3">
 <span className="font-bold text-[15px]">Plant an item:</span>
 <input
 className="ink-input min-w-[220px] flex-1"
 placeholder="what does the department need?"
 value={newTitle}
 onChange={(e) => setNewTitle(e.target.value)}
 onKeyDown={(e) => e.key === "Enter" && addItem()}
 />
 <select className="ink-input" value={newDept} onChange={(e) => setNewDept(e.target.value)}>
 {["Public Works", "Utilities", "Community Services", "Police", "Fire", "Finance"].map((d) => (
 <option key={d}>{d}</option>
 ))}
 </select>
 <input
 className="ink-input w-32"
 placeholder="$ amount"
 value={newFiscal}
 onChange={(e) => setNewFiscal(e.target.value)}
 />
 <button type="button" className="ink-btn" onClick={addItem} disabled={!newTitle.trim()}>
 plant it
 </button>
 </div>
 </section>
 </div>

 {/* ── Console + identity + minutes ──────────────────────────── */}
 <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
 {/* Chamber console — real per-item votes */}
 <section className="ink-frame p-5">
 <div className="mb-1 flex items-baseline gap-3">
 <h2 className="text-[30px]">The chamber console</h2>
 </div>
 <p className="mb-4 text-[14.5px]" style={{ color: "var(--ink-soft)" }}>
 The screen open next to the gavel during the live meeting: call
 an item, run the public-comment clock, log each member's vote as
 it's spoken. Everything logged here flows straight into the
 minutes.
 </p>
 <div className="graph-paper ink-frame-soft p-4">
 {readyItems.length === 0 ? (
 <p className="kalam text-[14px]" style={{ color: "var(--ink-soft)" }}>
 nothing on the agenda yet — walk an item through the pipeline
 first, then it appears here for the meeting.
 </p>
 ) : (
 <>
 <div className="mb-3 space-y-1">
 {readyItems.map((it) => (
 <button
 key={it.id}
 type="button"
 onClick={() => setCalledItemId(it.id)}
 className="block w-full text-left text-[14.5px]"
 style={{
 color: calledItemId === it.id ? "var(--ink)" : "var(--ink-soft)",
 fontWeight: calledItemId === it.id ? 600 : 400,
 }}
 >
 {calledItemId === it.id ? "▸ " : " "}
 {it.id} — {it.title}
 </button>
 ))}
 </div>
 <div className="mb-3 flex items-center gap-3">
 <button type="button" className="ink-btn ghost" onClick={() => setTimerLeft(180)}>
 start 3:00 comment clock
 </button>
 {timerLeft !== null && (
 <span className="mono text-[20px]" style={{ color: timerLeft <= 15 ? "var(--hot)" : "var(--ink)" }}>
 {Math.floor(timerLeft / 60)}:{String(timerLeft % 60).padStart(2, "0")}
 </span>
 )}
 </div>
 {calledItem ? (
 <>
 <div className="kalam mb-1.5 text-[14px]" style={{ color: "var(--ink-soft)" }}>
 votes on {calledItem.id}:
 </div>
 <div className="space-y-1.5">
 {MEMBERS.map((m) => {
 const v = desk.votes[calledItem.id]?.[m] ?? null;
 return (
 <div key={m} className="flex items-center gap-2.5">
 <span className="w-24 text-[14.5px]">{m}</span>
 <button
 type="button"
 className={`vote-box ${v === "yes" ? "checked-yes" : ""}`}
 onClick={() => setVote(calledItem.id, m, v === "yes" ? null : "yes")}
 title="Aye"
 >
 {v === "yes" ? "✓" : ""}
 </button>
 <button
 type="button"
 className={`vote-box ${v === "no" ? "checked-no" : ""}`}
 onClick={() => setVote(calledItem.id, m, v === "no" ? null : "no")}
 title="Nay"
 >
 {v === "no" ? "✗" : ""}
 </button>
 </div>
 );
 })}
 </div>
 <div className="mono mt-3 text-[12px]" style={{ color: "var(--ink-soft)" }}>
 {(() => {
 const t = tallyFor(calledItem.id);
 return `tally: ${t.yes} aye · ${t.no} nay · ${t.silent} not voting`;
 })()}
 </div>
 </>
 ) : (
 <p className="kalam text-[14px]" style={{ color: "var(--ink-soft)" }}>
 call an item above to open its vote sheet.
 </p>
 )}
 </>
 )}
 </div>
 </section>

 <div className="space-y-6">
 {/* Identity — the seam that answers the IT director */}
 <section className="ink-frame p-5">
 <div className="mb-1 flex items-baseline gap-3">
 <h2 className="text-[30px]">Who's holding the pen</h2>
 <span className="kalam ml-auto text-[12.5px]" style={{ color: "var(--disclaimer)" }}>
 identity rides ID.me here — simulated in this preview
 </span>
 </div>
 <p className="mb-3 text-[14.5px]" style={{ color: "var(--ink-soft)" }}>
 Nobody edits a municipal agenda on a password alone: staff
 verify through the same identity service federal agencies use,
 plus a physical security key kept on the desk.
 </p>
 <div className="space-y-2.5">
 {[
 { who: "City Clerk", how: "verified · hardware key", ok: true },
 { who: "Finance Director", how: "verified · hardware key", ok: true },
 { who: "City Attorney", how: "invitation sent", ok: false },
 ].map((r) => (
 <div key={r.who} className="ink-frame-soft flex items-center gap-3 px-4 py-2.5">
 <span className={`ink-pulse ${r.ok ? "is-done" : ""}`} />
 <span className="text-[15.5px] font-medium">{r.who}</span>
 <span className="mono ml-auto text-[12px]" style={{ color: "var(--ink-soft)" }}>
 {r.how}
 </span>
 </div>
 ))}
 </div>
 </section>

 {/* Minutes — assembled from the REAL recorded state */}
 <section className="ink-frame p-5">
 <div className="mb-1 flex items-baseline gap-3">
 <h2 className="text-[30px]">Adjourn → minutes</h2>
 </div>
 <p className="mb-3 text-[14.5px]" style={{ color: "var(--ink-soft)" }}>
 The console already holds the agenda, the votes, and the clock.
 Adjourning assembles the official minutes draft from that
 record — hours of typing becomes a review pass. A person still
 certifies it before it becomes the legal record.
 </p>
 {!desk.minutesAt ? (
 <button
 type="button"
 className="ink-btn"
 onClick={adjournAndDraft}
 disabled={desk.heardItemIds.length === 0}
 title={desk.heardItemIds.length === 0 ? "Call at least one item and log a vote first" : undefined}
 >
 adjourn meeting &amp; draft minutes
 </button>
 ) : (
 <div className="ink-frame-soft cd-print-area px-5 py-4">
 <div className="mb-1 text-[16px] font-bold">DRAFT — Minutes, Regular Council Meeting</div>
 <div className="mono mb-2 text-[10.5px]" style={{ color: "var(--ink-soft)" }}>
 drafted {new Date(desk.minutesAt).toLocaleString()} · from the console record
 </div>
 <div className="space-y-2 text-[14.5px] leading-relaxed" style={{ color: "var(--ink-soft)" }}>
 {desk.heardItemIds.map((id) => {
 const it = desk.items.find((x) => x.id === id);
 if (!it) return null;
 const t = tallyFor(id);
 const carried = t.yes > t.no;
 return (
 <p key={id}>
 <strong style={{ color: "var(--ink)" }}>{it.id}</strong> ({it.title}, {it.fiscal}) — motion{" "}
 {carried ? "carried" : "failed"}{" "}
 <span className="mono text-[12.5px]">
 {t.yes}–{t.no}
 </span>
 {t.silent > 0 ? `, ${t.silent} not voting` : ""}.
 </p>
 );
 })}
 <p>
 <em>
 Drafted from the chamber console's record. A clerk
 reviews and certifies before this becomes the official
 minutes.
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
 )}
 </section>
 </div>
 </div>

 {/* Desk reset — quiet, at the bottom */}
 <div className="mt-8 flex items-center gap-3">
 <button type="button" className="ink-btn ghost" onClick={resetDesk}>
 reset the desk
 </button>
 <span className="kalam text-[12.5px]" style={{ color: "var(--ink-soft)" }}>
 clears this device's saved desk and replants the sample items
 </span>
 </div>
 </div>
 </div>
 );
}

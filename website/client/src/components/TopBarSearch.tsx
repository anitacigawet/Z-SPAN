/**
 * TopBarSearch — the primary consolidated search affordance.
 *
 * Lives in the TopBar's left cluster, right after the Guide link
 * (V1-Polish-12, 2026-06-14). This is the PRIMARY search for the
 * network — it replaces the ChannelsPage-header "Search" pill.
 *
 * Two behaviors, intentionally preserved together:
 * - Enter → the full meeting-search page (`onNavigate("search", …)`).
 * - Click/focus → the city CHANNEL type-ahead dropdown that used to
 * live in the ChannelsPage header. Typing filters the channel catalog;
 * clicking a match jumps straight to that channel.
 * (Per James 2026-06-14: the jump-to-channel logic stays for now —
 * a later rework is expected, but it must survive the move to the
 * TopBar.)
 *
 * Google-style idle animation: while the field is empty AND unfocused,
 * the word "Search" stays put and only the SUFFIX cycles + fades
 * ("for channels" → "for meetings" → "for minutes" → "naturally:
 * Which cities discuss data centers?" 🔒). Every suffix carries a grayed
 * padlock for non-owners to signal that search is a V2 preview. Owners
 * see the unlocked, functional field. Focus/typing stops the animation
 * and the field reads as a plain input.
 *
 * Visual idiom matches the dark TopBar it sits in (white-on-black, the
 * bar's monospace face), not the lighter `var(--surface)` token system
 * the floating SignInPill uses — each element matches its container.
 */
import { useEffect, useRef, useState } from "react";
import { Search, Lock, Sparkles } from "lucide-react";
import { useCurrentUser } from "../hooks/useCurrentUser";
import { fetchForPlane } from "../lib/planeFetch";
import { VoiceDictationButton } from "./VoiceDictationButton";

// Idle placeholder: static "Search" + a cycling suffix. `locked` marks
// each V2 preview phrase so it renders with a padlock for non-owners.
// The NL suffix keeps the "Search" prefix like
// every other phrase (James 2026-06-14): "Search naturally: Which cities
// discuss data centers?" reads as one continuous hint, not a standalone
// quoted query. The data-center example is the realistic query the
// shipped RAG must answer, so it doubles as a legitimacy claim.
type SuffixPhrase = { text: string; locked?: boolean };
const SUFFIXES: SuffixPhrase[] = [
 { text: "for channels", locked: true },
 { text: "for meetings", locked: true },
 { text: "for minutes", locked: true },
 { text: "naturally: Which cities discuss data centers?", locked: true },
];

const CYCLE_MS = 2800; // dwell time per suffix
const FADE_MS = 220; // cross-fade gap between suffixes

// Flattened channel-catalog entry for the jump-to-channel type-ahead.
type ChannelMatch = {
 kind: "city";
 name: string;
 county: string;
 state: string;
};

interface TopBarSearchProps {
 onNavigate: (view: string, params?: any) => void;
 // V1.5-OperatorSearch-1 — owner-only natural-language cross-meeting
 // search. When the viewer is the operator AND has typed at least one
 // character, the dropdown reveals an extra "Search naturally" entry
 // that calls this with the typed query. App.tsx opens the modal. F11
 // audit-fix 2026-06-25: required (always supplied from App.tsx via
 // TopBar).
 onOperatorSearch: (query: string) => void;
 //Report-V0-1 — owner-only cited-report generation. Sibling
 // entry under "Search naturally" in the dropdown; same gate, same
 // wiring shape (App.tsx opens ReportGeneratorModal with the query).
 onGenerateReport: (query: string) => void;
}

// Minimal AI-mark for the "Generate report" entry (operator direction
// 2026-07-02: a small AI-logo SVG next to it, matching the minimal
// aesthetic). Lucide stroke vocabulary — a document with a four-point
// AI spark where the text would be.
function ReportAiMark({ className }: { className?: string }) {
 return (
 <svg
 viewBox="0 0 24 24"
 fill="none"
 stroke="currentColor"
 strokeWidth="2"
 strokeLinecap="round"
 strokeLinejoin="round"
 className={className}
 aria-hidden="true"
 >
 <path d="M13 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
 <path d="M13 3v5h5" />
 <path
 d="M11 11l.9 2.4 2.4.9-2.4.9-.9 2.4-.9-2.4-2.4-.9 2.4-.9z"
 fill="currentColor"
 stroke="none"
 />
 </svg>
 );
}

// Monotonic pick counter — re-selecting the same channel after the user
// has navigated away within ChannelsPage produces identical county/city
// params, so without a changing nonce React wouldn't see the apply
// effect's deps change. This guarantees every pick re-fires it.
let pickSeq = 0;

export function TopBarSearch({ onNavigate, onOperatorSearch, onGenerateReport }: TopBarSearchProps) {
 const { isOperatorSearchPrincipal } = useCurrentUser();
 const [value, setValue] = useState("");
 const [focused, setFocused] = useState(false);
 const [open, setOpen] = useState(false);
 const [suffixIndex, setSuffixIndex] = useState(0);
 const [suffixVisible, setSuffixVisible] = useState(true);
 const [items, setItems] = useState<ChannelMatch[]>([]);
 const treeLoadedRef = useRef(false);
 const inputRef = useRef<HTMLInputElement>(null);
 const wrapRef = useRef<HTMLDivElement>(null);

 // The field is "idle" only when empty AND unfocused — the only state
 // the placeholder animates in.
 const idle = !focused && value === "";

 useEffect(() => {
 if (!idle) {
 setSuffixVisible(true);
 return;
 }
 let swap: ReturnType<typeof setTimeout> | undefined;
 const tick = setInterval(() => {
 setSuffixVisible(false); // fade current suffix out
 swap = setTimeout(() => {
 setSuffixIndex(i => (i + 1) % SUFFIXES.length);
 setSuffixVisible(true); // fade next suffix in
 }, FADE_MS);
 }, CYCLE_MS);
 return () => {
 clearInterval(tick);
 if (swap) clearTimeout(swap);
 };
 }, [idle]);

 // Lazy-load the channel catalog the first time the user engages the
 // field — avoids a /api/channels/tree fetch on every TopBar-bearing
 // page when the dropdown may never be opened.
 const ensureTree = () => {
 if (treeLoadedRef.current) return;
 treeLoadedRef.current = true;
 fetchForPlane({
 publicPath: "/public-api/channels/tree",
 operatorPath: "/api/channels/tree",
 })
 .then(r => r.json())
 .then(d => {
 if (!d || !d.ok || !Array.isArray(d.states)) return;
 const flat: ChannelMatch[] = [];
 for (const st of d.states) {
 for (const c of st.counties ?? []) {
 for (const city of c.cities ?? []) {
 flat.push({
 kind: "city",
 name: city.name,
 county: c.county,
 state: st.state,
 });
 }
 }
 }
 setItems(flat);
 })
 .catch(() => {
 treeLoadedRef.current = false; // allow a retry on next focus
 });
 };

 // Close the dropdown on an outside click.
 useEffect(() => {
 if (!open) return;
 const onDoc = (e: MouseEvent) => {
 if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
 setOpen(false);
 }
 };
 document.addEventListener("mousedown", onDoc);
 return () => document.removeEventListener("mousedown", onDoc);
 }, [open]);

 const q = value.trim().toLowerCase();
 const matches = q
 ? items.filter(i => i.name.toLowerCase().includes(q)).slice(0, 6)
 : [];

 const goMeetings = () => {
 const query = value.trim();
 onNavigate("search", query ? { query } : undefined);
 setValue("");
 setOpen(false);
 inputRef.current?.blur();
 };

 const pickChannel = (m: ChannelMatch) => {
 onNavigate("home", {
 countyName: m.county,
 cityName: m.name,
 channelPick: ++pickSeq,
 });
 setValue("");
 setOpen(false);
 inputRef.current?.blur();
 };

 const goOperatorSearch = () => {
 const query = value.trim();
 if (!query || !onOperatorSearch) return;
 onOperatorSearch(query);
 setValue("");
 setOpen(false);
 inputRef.current?.blur();
 };

 const goGenerateReport = () => {
 const query = value.trim();
 if (!query || !onGenerateReport) return;
 onGenerateReport(query);
 setValue("");
 setOpen(false);
 inputRef.current?.blur();
 };

 const current = SUFFIXES[suffixIndex];

 return (
 <div ref={wrapRef} className="relative ml-2 flex items-center">
 <Search
 className="pointer-events-none absolute left-2.5 h-3.5 w-3.5 text-white/40"
 aria-hidden="true"
 />
 {/* Idle placeholder — "Search" static, suffix fades. Hidden once
 focused/typing so the field reads as a plain input. */}
 {idle && (
 <span
 className="pointer-events-none absolute left-8 right-3 flex items-center gap-1 overflow-hidden whitespace-nowrap font-sans text-[13px] text-white/40"
 aria-hidden="true"
 >
 <span>Search</span>
 <span
 className={`flex items-center gap-1 transition-opacity duration-200 ${
 suffixVisible ? "opacity-100" : "opacity-0"
 }`}
 >
 <span>{current.text}</span>
 {/* V2 padlock on every preview suffix, dropped for owners
 since search remains fully available in operator scope.
 Non-owners see the lock + v2 badge on every rotation. */}
 {current.locked && !isOperatorSearchPrincipal && (
 <>
 <Lock className="h-3 w-3 text-white/30" />
 <span className="text-[8px] uppercase tracking-widest text-white/30 leading-none -ml-0.5">v2</span>
 </>
 )}
 </span>
 </span>
 )}
 <input
 ref={inputRef}
 type="text"
 disabled={!isOperatorSearchPrincipal}
 value={value}
 onChange={e => {
 setValue(e.target.value);
 setOpen(true);
 }}
 onFocus={() => {
 setFocused(true);
 setOpen(true);
 ensureTree();
 }}
 onBlur={() => setFocused(false)}
 onKeyDown={e => {
 if (e.key === "Enter") {
 goMeetings();
 } else if (e.key === "Escape") {
 setValue("");
 setOpen(false);
 inputRef.current?.blur();
 }
 }}
 aria-label="Search channels and meetings"
 className={`h-7 w-96 rounded-full border border-white/15 bg-white/5 pl-8 pr-3 font-sans text-[13px] text-white/90 outline-none transition-[width,border-color,background-color] duration-200 ${
 isOperatorSearchPrincipal
 ? "focus:w-[27rem] focus:border-white/30 focus:bg-white/[0.07]"
 : "cursor-not-allowed"
 }`}
 />
 {/* Dropdown — channel type-ahead + the meetings escape hatch. Shows
 while focused or while there's a query. */}
 {isOperatorSearchPrincipal && open && (focused || value) && (
 <div className="absolute left-0 top-full z-50 mt-2 w-[27rem] overflow-hidden rounded-lg border border-white/15 bg-[#0A0A0C] shadow-2xl">
 {q ? (
 <>
 {matches.length > 0 && (
 <div className="py-1">
 <div className="px-3 pb-1 pt-1.5 text-[10px] uppercase tracking-widest text-white/30">
 Channels
 </div>
 {matches.map(m => (
 <button
 key={`${m.kind}-${m.state}-${m.name}`}
 type="button"
 // preventDefault on mousedown keeps the input from
 // blurring (which would close the dropdown) before
 // the click handler runs.
 onMouseDown={e => e.preventDefault()}
 onClick={() => pickChannel(m)}
 className="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-[13px] text-white/85 transition-colors hover:bg-white/5"
 >
 <span className="truncate font-medium">{m.name}</span>
 <span className="flex-none text-[10px] uppercase tracking-wider text-white/35">
 {`city · ${m.county}`}
 </span>
 </button>
 ))}
 </div>
 )}
 <button
 type="button"
 onMouseDown={e => e.preventDefault()}
 onClick={goMeetings}
 className={`flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] text-white/70 transition-colors hover:bg-white/5 ${
 matches.length > 0 ? "border-t border-white/10" : ""
 }`}
 >
 <Search className="h-3 w-3 flex-none text-white/40" />
 <span className="truncate">
 Search all meetings for “{value.trim()}”
 </span>
 </button>
 {/* V1.5-OperatorSearch-1 affordance — owner-only natural-
 language cross-meeting search. Visible only to the
 signed-in owner; the backend endpoint enforces the
 same gate so non-owners can never reach it even if
 they tamper with the prop chain. */}
 {isOperatorSearchPrincipal && (
 <button
 type="button"
 onMouseDown={e => e.preventDefault()}
 onClick={goOperatorSearch}
 className="flex w-full items-center gap-2 border-t border-white/10 px-3 py-2 text-left text-[12px] text-amber-200/90 transition-colors hover:bg-amber-400/5"
 >
 <Sparkles className="h-3 w-3 flex-none text-amber-400/70" />
 <span className="truncate">
 Search naturally across all meetings: “{value.trim()}”
 </span>
 <span className="ml-auto flex-none text-[9px] uppercase tracking-widest text-amber-400/60">
 operator
 </span>
 </button>
 )}
 {/*Report-V0-1 — owner-only cited-report generation.
 Sits directly under "Search naturally" per operator
 direction 2026-07-02; same owner gate at this surface AND
 at every /api/report-runs endpoint. */}
 {isOperatorSearchPrincipal && (
 <button
 type="button"
 onMouseDown={e => e.preventDefault()}
 onClick={goGenerateReport}
 className="flex w-full items-center gap-2 border-t border-white/10 px-3 py-2 text-left text-[12px] text-amber-200/90 transition-colors hover:bg-amber-400/5"
 >
 <ReportAiMark className="h-3 w-3 flex-none text-amber-400/70" />
 <span className="truncate">
 Generate report: “{value.trim()}”
 </span>
 <span className="ml-auto flex-none text-[9px] uppercase tracking-widest text-amber-400/60">
 operator
 </span>
 </button>
 )}
 </>
 ) : (
 <div className="px-3 py-2.5 text-[12px] leading-relaxed text-white/40">
 Type a city to jump to its channel, or press Enter to search
 meetings.
 </div>
 )}
 </div>
 )}
 {/* Voice dictation — owner-only speech-to-text into the search
 box. Replaces the retired V-Op-2 voice-prime button
 (biometric voice-search, removed + operator
 direction 2026-07-02). Browser-local; nothing recorded. */}
 {isOperatorSearchPrincipal && (
 <VoiceDictationButton
 onTranscript={text => {
 setValue(text);
 setOpen(true);
 ensureTree();
 inputRef.current?.focus();
 }}
 />
 )}
 </div>
 );
}

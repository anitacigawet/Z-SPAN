/**
 * CastPanel — the per-city Cast hub ( V1).
 *
 * Shown inside ChannelsPage when the user is at city-level AND has toggled
 * to the "Cast" view (parallel to the Episodes calendar). Renders the
 * council members for that city as a tile grid, hairline-separated to
 * match the rest of the Kingman Insight aesthetic.
 *
 * Data source: `/api/cast/<city>` — populated from city_intelligence/*.json
 * + council_members/member_attendance/member_quotes tables. Attendance and
 * quote counts are 0 in V1 until the NotebookLM extraction prompts ()
 * are wired.
 *
 * Clicking a member tile drills into CastMemberPanel via onSelectMember.
 */
import { useEffect, useState } from "react";
import { ArrowRight } from "lucide-react";
import { fetchForPlane } from "../lib/planeFetch";
import { isPublicPlane } from "../lib/trustPlane";

export type CastMemberSummary = {
 id?: number;
 name: string;
 role: string | null;
 seat_id: string | null;
 term_started: string | null;
 term_ends: string | null;
 source_url: string | null;
 attendance_count?: number;
 quote_count?: number;
};

export type CastRoster = {
 city: string;
 county: string | null;
 state: string | null;
 verified_on: string | null;
 notes: string | null;
 // Optional — the public roster projection at
 // parsers/api_server.py:_public_cast_member intentionally strips this
 // block. Operator plane sends it. Guard every access with `?.council?.`.
 council?: {
 seats: number | null;
 term_length_years: number | null;
 next_election_date: string | null;
 next_election_seats_up: string[] | null;
 };
 members: CastMemberSummary[];
};

interface CastPanelProps {
 cityName: string;
 countyName: string | null;
 onSelectMember: (member: CastMemberSummary) => void;
}

// ── Term-end helpers ────────────────────────────────────────────────
//
// Many term_ends values come in as just "2026" (year only) from the
// city_intelligence JSON. We treat year-only as "end of that calendar
// year" for the urgency calculation — close enough for the "term ending
// soon" pill threshold (~60 days).

function termEndsDate(termEnds: string | null): Date | null {
 if (!termEnds) return null;
 if (/^\d{4}$/.test(termEnds)) {
 // year-only -> Dec 31 of that year
 return new Date(`${termEnds}-12-31T23:59:59`);
 }
 if (/^\d{4}-\d{2}$/.test(termEnds)) {
 // year-month -> last day of that month
 const [y, m] = termEnds.split("-").map(Number);
 return new Date(y, m, 0);
 }
 const d = new Date(termEnds + "T00:00:00");
 return isNaN(d.getTime()) ? null : d;
}

function termUrgency(termEnds: string | null): "ending_soon" | "this_year" | "ok" {
 const d = termEndsDate(termEnds);
 if (!d) return "ok";
 const now = new Date();
 const daysUntil = (d.getTime() - now.getTime()) / (1000 * 60 * 60 * 24);
 if (daysUntil < 0) return "ending_soon"; // already ended (term should refresh)
 if (daysUntil <= 60) return "ending_soon";
 if (daysUntil <= 365) return "this_year";
 return "ok";
}

function formatSeatLabel(seatId: string | null): string {
 if (!seatId) return "—";
 if (seatId === "mayor") return "Mayor's Seat";
 if (seatId === "vice_mayor") return "Vice Mayor's Seat";
 if (seatId.startsWith("seat_")) return `Seat ${seatId.slice(5)}`;
 return seatId;
}

// ── Member card ─────────────────────────────────────────────────────

function MemberCard({
 member,
 onClick,
}: {
 member: CastMemberSummary;
 onClick: () => void;
}) {
 const urgency = termUrgency(member.term_ends);
 const initials = (member.name || "?")
 .split(/\s+/)
 .map(p => p[0])
 .filter(Boolean)
 .slice(0, 2)
 .join("")
 .toUpperCase();

 return (
 <button
 onClick={onClick}
 className="group text-left rounded-xl border border-[var(--line)] hover:border-[var(--line-strong)] hover:-translate-y-0.5 transition-all duration-200 overflow-hidden bg-[var(--canvas)] flex flex-col"
 >
 {/* Portrait / initials block — replaces the meeting thumbnail used by
 EpisodeCard. We don't ship photos in V1 (privacy + maintenance);
 initials in a dim deep-black tile match the channel-guide aesthetic
 while leaving room for a real portrait later. */}
 <div
 className="aspect-[4/3] relative overflow-hidden flex items-center justify-center"
 style={{
 background:
 "radial-gradient(circle at 30% 30%, rgba(255,255,255,0.04) 0%, rgba(0,0,0,0.0) 60%), var(--canvas)",
 }}
 >
 <span
 className="text-[44px] sm:text-[52px] font-light text-white/30 tracking-tight select-none"
 aria-hidden="true"
 >
 {initials}
 </span>

 {/* Term-status pill (top-right) */}
 {urgency === "ending_soon" && (
 <span
 className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-[#F5A524]/12 border border-[#F5A524]/30 backdrop-blur-sm"
 title={
 member.term_ends
 ? `Term ends ${member.term_ends} — re-verify`
 : "Term end unknown"
 }
 >
 <span
 className="w-1.5 h-1.5 rounded-full bg-[#F5A524]"
 aria-hidden="true"
 />
 <span className="text-[9px] font-semibold uppercase tracking-widest text-[#F5A524]">
 Term ends
 </span>
 </span>
 )}

 {/* dark gradient bottom-third so name + role read over the gradient */}
 <div
 className="absolute inset-x-0 bottom-0 h-1/2 pointer-events-none"
 style={{
 background:
 "linear-gradient(to top, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.40) 55%, rgba(0,0,0,0) 100%)",
 }}
 />
 </div>

 {/* Bottom strip — name, role, stats. Hairline above for cohesion with
 the rest of the channel guide. */}
 <div className="border-t border-[var(--line)] px-4 py-3 flex flex-col gap-1">
 <p className="text-[10px] uppercase tracking-[0.2em] text-foreground/45 font-medium">
 {member.role ?? "Council"}
 </p>
 <h3 className="text-[15px] text-white font-medium tracking-wide truncate">
 {member.name}
 </h3>
 {/*: the per-member quote / attendance counts (an accountability-
 profiling signal) were removed from the public roster; the card now
 carries only the seat as published + the open affordance. */}
 <div className="flex items-center justify-between gap-2 mt-1">
 <p className="text-[10px] uppercase tracking-[0.18em] text-foreground/35 tabular-nums">
 {formatSeatLabel(member.seat_id)}
 </p>
 <ArrowRight className="w-3 h-3 text-foreground/30 group-hover:text-white group-hover:translate-x-0.5 transition-all" />
 </div>
 </div>
 </button>
 );
}

// ── Main panel ──────────────────────────────────────────────────────

export default function CastPanel({
 cityName,
 countyName,
 onSelectMember,
}: CastPanelProps) {
 const publicPlane = isPublicPlane();
 const [roster, setRoster] = useState<CastRoster | null>(null);
 const [loading, setLoading] = useState(false);
 const [error, setError] = useState<string | null>(null);

 useEffect(() => {
 let aborted = false;
 setLoading(true);
 setError(null);
 fetchForPlane({
 publicPath: `/public-api/cast/${encodeURIComponent(cityName)}`,
 operatorPath: `/api/cast/${encodeURIComponent(cityName)}`,
 })
 .then(res => {
 if (!res.ok) throw new Error(`HTTP ${res.status}`);
 return res.json();
 })
 .then((data: CastRoster) => {
 if (aborted) return;
 setRoster(data);
 setLoading(false);
 })
 .catch(err => {
 if (aborted) return;
 setError(err.message || "Failed to load cast");
 setLoading(false);
 });
 return () => {
 aborted = true;
 };
 }, [cityName]);

 return (
 <div className="flex flex-col gap-5">
 <header className="flex items-end justify-between gap-4">
 <div>
 <p className="kg-eyebrow mb-1">
 Cast · {cityName}
 {countyName ? ` · ${countyName} County` : ""}
 </p>
 <h2 className="text-2xl font-light text-white tracking-wide">
 {cityName}
 {roster && (
 <span className="text-muted-foreground text-base font-normal align-middle ml-2">
 · {roster.members.length} member
 {roster.members.length === 1 ? "" : "s"}
 </span>
 )}
 </h2>
 {roster?.notes && (
 <p className="text-[12px] text-muted-foreground/80 leading-relaxed max-w-prose mt-2">
 {roster.notes}
 </p>
 )}
 </div>
 {roster?.council?.next_election_date && (
 <div className="hidden md:flex flex-col items-end text-right flex-shrink-0">
 <p className="text-[10px] uppercase tracking-[0.18em] text-foreground/40">
 Next election
 </p>
 <p className="text-[14px] font-light text-white tabular-nums">
 {roster.council.next_election_date}
 </p>
 </div>
 )}
 </header>

 {loading ? (
 <div className="py-12 text-center">
 <div className="kg-dots inline-flex">
 <span /> <span /> <span />
 </div>
 <p className="text-sm text-muted-foreground mt-4">Loading cast…</p>
 </div>
 ) : error ? (
 <div className="kg-card-2 border-dashed p-12 text-center">
 <p className="text-sm text-muted-foreground">
 Could not load the cast for {cityName}: {error}
 </p>
 </div>
 ) : !roster || roster.members.length === 0 ? (
 <div className="kg-card-2 border-dashed p-12 text-center">
 <h3 className="text-base font-semibold text-foreground/70 mb-1.5">
 No cast data yet
 </h3>
 <p className="text-sm text-muted-foreground leading-relaxed max-w-md mx-auto">
 {publicPlane ? (
 <>No official roster is on file for this city yet.</>
 ) : (
 <>
 Seed this city's council in{" "}
 <code className="text-foreground/80">
 city_intelligence/{cityName.toLowerCase().replace(/\s+/g, "_")}.json
 </code>{" "}
 and restart Flask. See{" "}
 <code className="text-foreground/80">city_intelligence/RECIPE.md</code>{" "}
 for the seeding workflow.
 </>
 )}
 </p>
 </div>
 ) : (
 <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 sm:gap-4">
 {roster.members.map(m => (
 <MemberCard
 key={m.seat_id || String(m.id)}
 member={m}
 onClick={() => onSelectMember(m)}
 />
 ))}
 </div>
 )}
 </div>
 );
}

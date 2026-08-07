/**
 * MeetingSchedulePanel — H-6 "Meeting Schedule" subsection.
 *
 * Renders a city's curated `meeting_patterns[]` as a human-readable list
 * with the next 3 projected meetings per pattern shown inline. James
 * picked "both pages" + "inline projections always visible" 2026-06-03;
 * this single component mounts on CityPage AND the Cast (cast-member)
 * page so the schedule shows up wherever an operator might look for it.
 *
 * Schema-as-label discipline per [](DECISIONS.md#d-054):
 * ✅ "1st + 3rd Tuesday at 5:00 PM"
 * ❌ "weeks_of_month: [1, 3]"
 *
 * Data shape (from /api/cities/<city>/meeting-patterns):
 * { ok, city, patterns: MeetingPattern[], upcoming_by_pattern: {pid: [...]} }
 *
 * Empty states:
 * - Loading → muted "Loading meeting schedule…" line.
 * - Network error → muted "Couldn't load meeting schedule" + retry button.
 * - No patterns yet → muted "No meeting schedule on file yet" (the city
 * hasn't been through H-1 curation; honest acknowledgment).
 * - Patterns present but no upcoming projection → render the patterns
 * with "No upcoming meeting in the next 90 days" per pattern (this
 * hits the quarterly bodies between their meeting months — real
 * signal, not a bug).
 */
import { useEffect, useMemo, useState } from "react";
import { CalendarDays, RefreshCw, MapPin, Youtube } from "lucide-react";

interface Cadence {
 frequency: "weekly" | "biweekly" | "monthly_weeks" | "monthly_date" | "twice_monthly" | "adhoc";
 day_of_week?: string;
 weeks_of_month?: number[];
 anchor_date?: string;
 date_of_month?: number;
 days_of_month?: number[];
 months_of_year?: number[];
}

interface MeetingPattern {
 pattern_id: string;
 meeting_type: string;
 cadence: Cadence;
 time_local: string;
 location?: string | null;
 youtube_channel_url?: string | null;
 exceptions?: Array<{ date: string; reason?: string }>;
 source_url: string;
 verified_on: string;
 notes?: string;
}

interface UpcomingMeeting {
 pattern_id: string;
 meeting_type: string;
 date: string;
 time_local: string;
 datetime: string; // ISO
 location?: string | null;
}

interface ApiResponse {
 ok: boolean;
 city: string;
 patterns: MeetingPattern[];
 upcoming_by_pattern: Record<string, UpcomingMeeting[]>;
 note?: string;
 error?: string;
}

const ORDINAL: Record<number, string> = {
 1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th",
};

const MONTH_LABELS = [
 "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatCadence(c: Cadence): string {
 const day = c.day_of_week || "?";
 switch (c.frequency) {
 case "weekly":
 return `Every ${day}`;
 case "biweekly":
 return `Every other ${day}`;
 case "monthly_weeks": {
 const weeks = (c.weeks_of_month || []).map(w => ORDINAL[w] || `${w}th`).join(" + ");
 return weeks ? `${weeks} ${day}` : day;
 }
 case "monthly_date":
 return c.date_of_month ? `The ${ORDINAL[c.date_of_month] || c.date_of_month}` : "Monthly";
 case "twice_monthly": {
 const days = (c.days_of_month || []).map(d => ORDINAL[d] || `${d}th`).join(" + ");
 return days ? `${days} of the month` : "Twice a month";
 }
 case "adhoc":
 return "As needed";
 default:
 return "Recurring";
 }
}

function formatMonthsOfYear(months?: number[]): string {
 if (!months || months.length === 0 || months.length === 12) return "";
 return `(${months.map(m => MONTH_LABELS[m] || m).join("/")})`;
}

function formatUpcomingDate(iso: string): string {
 try {
 const d = new Date(iso);
 return d.toLocaleDateString(undefined, {
 weekday: "short",
 month: "short",
 day: "numeric",
 });
 } catch {
 return iso;
 }
}

function formatUpcomingTime(iso: string): string {
 try {
 const d = new Date(iso);
 return d.toLocaleTimeString(undefined, {
 hour: "numeric",
 minute: "2-digit",
 });
 } catch {
 return "";
 }
}

// James 2026-06-03: freshness tone for upcoming chips. The dot + subtle
// border tint signal urgency at a glance — green = within a week (eyes
// on this), amber = within the month (plan around it), red = next month
// or later (far enough out to not need attention yet). Red here is
// "later, not bad" — paired with the green/amber it reads as a recency
// gradient, not a status warning.
function freshnessTone(daysOut: number): {
 dot: string;
 border: string;
 ariaLabel: string;
} {
 if (daysOut <= 7) {
 return {
 dot: "bg-[#22d75f] shadow-[0_0_4px_rgba(34,215,95,0.55)]",
 border: "border-[#22d75f]/35",
 ariaLabel: "Within a week",
 };
 }
 if (daysOut <= 30) {
 return {
 dot: "bg-[#f5c33b] shadow-[0_0_4px_rgba(245,195,59,0.5)]",
 border: "border-[#f5c33b]/30",
 ariaLabel: "Within the month",
 };
 }
 return {
 dot: "bg-[#ff4d4f] shadow-[0_0_4px_rgba(255,77,79,0.45)]",
 border: "border-[#ff4d4f]/30",
 ariaLabel: "Next month or later",
 };
}

function daysBetween(fromISO: string, now: Date): number {
 try {
 const target = new Date(fromISO);
 const ms = target.getTime() - now.getTime();
 return Math.max(0, Math.round(ms / 86_400_000));
 } catch {
 return 0;
 }
}

interface Props {
 city: string;
 daysAhead?: number;
 upcomingPerPattern?: number;
 /** Optional className for outer wrapping (e.g., page-specific margins). */
 className?: string;
}

export default function MeetingSchedulePanel({
 city,
 daysAhead = 90,
 upcomingPerPattern = 3,
 className = "",
}: Props) {
 const [data, setData] = useState<ApiResponse | null>(null);
 const [loading, setLoading] = useState(true);
 const [error, setError] = useState<string | null>(null);

 const fetchData = useMemo(
 () => async () => {
 setLoading(true);
 setError(null);
 try {
 const qs = new URLSearchParams({
 days_ahead: String(daysAhead),
 upcoming_per_pattern: String(upcomingPerPattern),
 }).toString();
 const res = await fetch(
 `/api/cities/${encodeURIComponent(city)}/meeting-patterns?${qs}`,
 );
 const json: ApiResponse = await res.json();
 if (!json.ok) throw new Error(json.error || "request failed");
 setData(json);
 } catch (e: unknown) {
 const msg = e instanceof Error ? e.message : String(e);
 setError(msg);
 } finally {
 setLoading(false);
 }
 },
 [city, daysAhead, upcomingPerPattern],
 );

 useEffect(() => {
 fetchData();
 }, [fetchData]);

 if (loading) {
 return (
 <section className={`kg-card p-6 ${className}`}>
 <div className="flex items-center gap-2">
 <span className="kg-dots">
 <span /> <span /> <span />
 </span>
 <p className="text-[12px] text-muted-foreground">
 Loading meeting schedule…
 </p>
 </div>
 </section>
 );
 }

 if (error) {
 return (
 <section className={`kg-card p-6 ${className}`}>
 <div className="flex items-center justify-between gap-3">
 <p className="text-[12px] text-muted-foreground">
 Couldn't load meeting schedule.
 </p>
 <button
 type="button"
 onClick={fetchData}
 className="text-[11px] uppercase tracking-widest text-foreground/80 hover:text-foreground inline-flex items-center gap-1.5"
 >
 <RefreshCw className="w-3 h-3" />
 Retry
 </button>
 </div>
 </section>
 );
 }

 const patterns = data?.patterns || [];

 if (patterns.length === 0) {
 return (
 <section className={`kg-card p-6 ${className}`}>
 <div className="flex items-center gap-3">
 <CalendarDays className="w-4 h-4 text-muted-foreground" />
 <div>
 <h2 className="text-[15px] font-semibold text-white">
 Meeting Schedule
 </h2>
 <p className="text-[12px] text-muted-foreground mt-1">
 No meeting schedule on file yet for {city}.
 </p>
 </div>
 </div>
 </section>
 );
 }

 const upcoming = data?.upcoming_by_pattern || {};

 // J-6-Opus-fix #3: when all patterns share the same location, hoist it
 // to a single panel-level note + suppress per-row. Per-row location only
 // renders when it diverges from the panel default. Stops 11-row Kingman
 // from showing the same 65-char address 11 times.
 const sharedLocation = (() => {
 const seen = new Set<string>();
 let anyMissing = false;
 for (const p of patterns) {
 if (!p.location) {
 anyMissing = true;
 break;
 }
 seen.add(p.location);
 if (seen.size > 1) break;
 }
 return !anyMissing && seen.size === 1 ? Array.from(seen)[0] : null;
 })();

 // J-6-Opus-fix #2: single-pattern panel reads as half-built when stretched
 // to full width with ~70% whitespace. Constrain inner content width when
 // there's only one row; multi-row case keeps the full container width
 // because the chip rail genuinely needs it.
 const isSinglePattern = patterns.length === 1;
 const innerWrapperClass = isSinglePattern ? "max-w-2xl" : "";

 return (
 <section className={`kg-card p-6 ${className}`}>
 <div className={innerWrapperClass}>
 <header className="flex items-center justify-between gap-3 mb-4">
 <div className="flex items-center gap-2">
 <CalendarDays className="w-4 h-4 text-foreground/70" />
 <h2 className="text-[15px] font-semibold text-white">
 Meeting Schedule
 </h2>
 </div>
 {/* J-6-Opus-fix #4: schema-as-label fix — drop the
 * uppercase tracking-widest cardinality micro-caps in favor
 * of sentence-case operator vocabulary. */}
 <p className="text-[12px] text-muted-foreground">
 {patterns.length} recurring {patterns.length === 1 ? "meeting" : "meetings"}
 </p>
 </header>

 {sharedLocation && (
 <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground mb-5">
 <MapPin className="w-3 h-3" />
 <span>All meetings at {sharedLocation}</span>
 </p>
 )}

 <ul className="space-y-5">
 {patterns.map(p => {
 const cadenceLabel = formatCadence(p.cadence);
 const monthsBadge = formatMonthsOfYear(p.cadence.months_of_year);
 const cadenceSentence =
 p.cadence.frequency === "adhoc"
 ? cadenceLabel
 : `${cadenceLabel} at ${p.time_local}`;
 const next = upcoming[p.pattern_id] || [];
 // Per-row location renders only when it diverges from the shared
 // panel-level default. When there's no shared location at all,
 // every row shows its own.
 const rowLocation =
 !sharedLocation && p.location ? p.location : null;

 return (
 <li
 key={p.pattern_id}
 className="meeting-schedule-row border-l-2 border-[var(--line)] pl-4"
 >
 <div className="flex items-baseline justify-between gap-3 mb-1">
 <h3 className="text-[14px] font-semibold text-white">
 {p.meeting_type}
 </h3>
 {monthsBadge && (
 <span className="text-[10px] uppercase tracking-widest text-muted-foreground tabular-nums">
 {monthsBadge}
 </span>
 )}
 </div>
 <p className="text-[13px] text-foreground/85 mb-2">
 {cadenceSentence}
 </p>

 {/* J-6-Opus-fix #6: location + YouTube meta knock down one
 * tier (smaller, more muted) so the cadence stays the
 * primary scannable line. */}
 {(rowLocation || p.youtube_channel_url) && (
 <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground/80 mb-3">
 {rowLocation && (
 <span className="inline-flex items-center gap-1.5">
 <MapPin className="w-3 h-3" />
 <span>{rowLocation}</span>
 </span>
 )}
 {p.youtube_channel_url && (
 <a
 href={p.youtube_channel_url}
 target="_blank"
 rel="noopener noreferrer"
 className="inline-flex items-center gap-1.5 transition-opacity hover:opacity-100 opacity-90"
 >
 {/* James 2026-06-03: YouTube icon + brand word
 * go YouTube-red so the affordance reads as
 * the YouTube-grammar it is, not just another
 * generic external-link. "Stream on " stays
 * in the muted meta tier. */}
 <Youtube className="w-3 h-3 text-[#ff3b30]" />
 <span>
 Stream on{" "}
 <span className="text-[#ff3b30] font-semibold">YouTube</span>
 </span>
 </a>
 )}
 </div>
 )}

 {next.length > 0 ? (
 <div className="flex flex-wrap items-center gap-2">
 <span className="text-[10px] uppercase tracking-widest text-muted-foreground mr-1">
 Upcoming
 </span>
 {next.map(m => {
 const days = daysBetween(m.datetime, new Date());
 const tone = freshnessTone(days);
 return (
 <span
 key={`${m.pattern_id}-${m.date}`}
 className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] text-foreground/90 bg-[var(--surface-2)] border ${tone.border} tabular-nums`}
 title={`${formatUpcomingDate(m.datetime)} at ${formatUpcomingTime(m.datetime)} · in ${days} day${days === 1 ? "" : "s"} (${tone.ariaLabel.toLowerCase()})`}
 >
 {/* James 2026-06-03: freshness dot — green ≤7d,
 * amber 8-30d, red 31+d. Recency gradient,
 * not a status warning; red here means
 * "later, not urgent yet." */}
 <span
 className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${tone.dot}`}
 aria-label={tone.ariaLabel}
 />
 <span className="font-semibold">
 {formatUpcomingDate(m.datetime)}
 </span>
 <span className="text-muted-foreground">
 {formatUpcomingTime(m.datetime)}
 </span>
 </span>
 );
 })}
 </div>
 ) : (
 <p className="text-[11px] text-muted-foreground italic">
 No upcoming meeting in the next {daysAhead} days.
 </p>
 )}
 </li>
 );
 })}
 </ul>
 </div>
 </section>
 );
}

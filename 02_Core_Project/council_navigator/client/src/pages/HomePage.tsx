import { useState, useEffect, useRef } from "react";
import {
  Building2,
  ChevronRight,
  Search,
  Activity,
  BarChart3,
  Settings as SettingsIcon,
  ArrowRight,
  Newspaper,
} from "lucide-react";
import { NATIONAL_CIVICS_CATALOG_URL } from "../lib/projectMeta";
// AuthStatusPill removed per D-143 (NotebookLM removal 2026-07-01).

interface StatsData {
  total_meetings: number;
  total_cities: number;
  active_cities: number;
  counties: string[];
  top_cities: Array<{ city: string; county: string; meetings: number }>;
  meetings_by_county: Record<string, number>;
}

interface HomePageProps {
  onNavigate: (view: string, params?: any) => void;
}

// Z-SPAN: Mohave County focus for launch. Full Arizona expansion comes
// after the pilot is stable. See 01_Project_Overview/EXPANSION_FRAMEWORK.md.
const MOHAVE_CITIES = [
  "Kingman",
  "Bullhead City",
  "Lake Havasu City",
  "Colorado City",
];

// ── Safe-int coerce: never returns NaN/undefined for the AnimatedNumber.
function toSafeInt(v: unknown): number {
  if (typeof v === "number" && Number.isFinite(v)) return Math.max(0, Math.floor(v));
  if (typeof v === "string") {
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? Math.max(0, n) : 0;
  }
  return 0;
}

function AnimatedNumber({
  value,
  duration = 1200,
}: {
  value: number;
  duration?: number;
}) {
  const safe = toSafeInt(value);
  const [display, setDisplay] = useState(0);
  const ref = useRef<HTMLSpanElement>(null);
  const hasAnimated = useRef(false);

  useEffect(() => {
    if (hasAnimated.current) {
      setDisplay(safe);
      return;
    }
    if (safe === 0) {
      setDisplay(0);
      return;
    }
    hasAnimated.current = true;
    const start = performance.now();
    const animate = (now: number) => {
      const elapsed = now - start;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      const next = Math.round(eased * safe);
      setDisplay(Number.isFinite(next) ? next : 0);
      if (progress < 1) requestAnimationFrame(animate);
    };
    requestAnimationFrame(animate);
  }, [safe, duration]);

  return <span ref={ref}>{display.toLocaleString()}</span>;
}

export default function HomePage({ onNavigate }: HomePageProps) {
  const [stats, setStats] = useState<StatsData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/calendar/stats")
      .then(res => res.json())
      .then(data => {
        if (data && data.success !== false) setStats(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  // Filter top_cities to Mohave focus when stats arrive
  const mohaveCities = stats?.top_cities?.filter(c => c.county === "Mohave County") ?? [];
  const mohaveMeetings = stats?.meetings_by_county?.["Mohave County"] ?? 0;
  const totalMohaveCities = mohaveCities.length;

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Top bar */}
      <header className="sticky top-0 z-40 bg-[var(--canvas)]/95 backdrop-blur border-b border-[var(--line)]">
        <div className="max-w-7xl mx-auto px-6 lg:px-10 py-5 flex items-center justify-between">
          <div className="flex items-center gap-3.5">
            <div
              className="bg-white text-black p-2 rounded-md"
              aria-hidden="true"
            >
              <Building2 className="w-5 h-5" />
            </div>
            <div>
              <h1 className="text-base font-bold tracking-wider uppercase text-white">
                Z-SPAN
              </h1>
              <p className="kg-eyebrow mt-0.5">
                A virtual library for Arizona politics · Mohave County Pilot
              </p>
            </div>
          </div>
          {/* NotebookLM AuthStatusPill removed per D-143 (2026-07-01). */}
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-6 lg:px-10 pt-14 pb-20 kg-fade-in">
        {/* Hero */}
        <section className="mb-14">
          <p className="kg-eyebrow mb-5">Strengthening Democracy</p>
          <h2 className="text-4xl md:text-5xl font-light tracking-wide mb-5 max-w-3xl leading-tight text-white">
            City council meetings,
            <br />
            <span className="text-foreground/60">summarized for everyone.</span>
          </h2>
          <p className="text-base text-muted-foreground max-w-2xl leading-relaxed mb-8">
            Z-SPAN distills 2+ hour Mohave County city council meetings into newsletters,
            audio overviews, and short videos — so citizens can stay informed without giving up
            an evening. Source-linked, timestamped, and politically neutral.
          </p>

          {/* Quick actions */}
          <div className="flex flex-wrap gap-3">
            <button
              onClick={() => onNavigate("broadcast", { meetingId: 9 })}
              className="zs-cta-accent group inline-flex items-center gap-2.5 px-5 py-3 text-[12px] uppercase tracking-widest"
              title="Sample broadcast: Kingman City Council, Apr 7, 2026"
            >
              <Newspaper className="w-3.5 h-3.5" />
              View Sample Broadcast
              <ArrowRight className="w-3.5 h-3.5 group-hover:translate-x-0.5 transition-transform" />
            </button>
            <button
              onClick={() => onNavigate("search")}
              className="zs-cta-primary group inline-flex items-center gap-2.5 px-5 py-3 text-[12px] uppercase tracking-widest"
            >
              <Search className="w-3.5 h-3.5" />
              Search Meetings
              <ArrowRight className="w-3.5 h-3.5 group-hover:translate-x-0.5 transition-transform" />
            </button>
            <button
              onClick={() => onNavigate("pipeline")}
              className="inline-flex items-center gap-2.5 px-5 py-3 bg-[var(--surface)] hover:bg-[var(--surface-3)] border border-[var(--line)] text-[var(--foreground)] rounded-md font-semibold uppercase tracking-widest text-[11px] transition-colors"
            >
              <Activity className="w-3.5 h-3.5" />
              Pipeline Monitor
            </button>
            <button
              onClick={() => onNavigate("dashboard")}
              className="inline-flex items-center gap-2.5 px-5 py-3 bg-[var(--surface)] hover:bg-[var(--surface-3)] border border-[var(--line)] text-[var(--foreground)] rounded-md font-semibold uppercase tracking-widest text-[11px] transition-colors"
            >
              <BarChart3 className="w-3.5 h-3.5" />
              Parser Dashboard
            </button>
            <button
              onClick={() => onNavigate("settings")}
              className="inline-flex items-center gap-2.5 px-5 py-3 bg-[var(--surface)] hover:bg-[var(--surface-3)] border border-[var(--line)] text-[var(--foreground)] rounded-md font-semibold uppercase tracking-widest text-[11px] transition-colors"
            >
              <SettingsIcon className="w-3.5 h-3.5" />
              Settings
            </button>
          </div>
        </section>

        {/* Stats — Mohave focus */}
        <section className="mb-16">
          <h3 className="kg-eyebrow mb-5">Mohave County · By the Numbers</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {[
              {
                value: toSafeInt(mohaveMeetings),
                label: "Mohave Meetings Tracked",
              },
              {
                value: toSafeInt(totalMohaveCities) || 4,
                label: "Mohave Cities",
              },
              {
                value: toSafeInt(stats?.total_meetings),
                label: "Statewide Meetings (Index)",
              },
              {
                value: toSafeInt(stats?.active_cities),
                label: "Statewide Cities (Index)",
              },
            ].map((stat, i) => (
              <div
                key={i}
                className="kg-card p-6 hover:bg-[var(--surface-3)]/60 transition-colors"
              >
                <p className="text-3xl md:text-4xl font-light tabular-nums text-white">
                  {loading ? (
                    <span className="text-muted-foreground/40">—</span>
                  ) : (
                    <AnimatedNumber value={stat.value} />
                  )}
                </p>
                <p className="kg-eyebrow mt-3">{stat.label}</p>
              </div>
            ))}
          </div>
        </section>

        {/* Mohave County cities — primary navigation */}
        <section className="mb-16">
          <div className="flex items-end justify-between mb-5">
            <div>
              <h3 className="kg-eyebrow mb-2">Mohave County Cities</h3>
              <p className="text-2xl font-light text-white tracking-wide">
                Pilot coverage area
                <span className="text-muted-foreground text-sm font-normal align-middle ml-2">
                  · {MOHAVE_CITIES.length} cities
                </span>
              </p>
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            {MOHAVE_CITIES.map(cityName => {
              const cityStats = stats?.top_cities?.find(c => c.city === cityName);
              const meetingCount = toSafeInt(cityStats?.meetings);
              const hasData = meetingCount > 0;
              return (
                <button
                  key={cityName}
                  onClick={() =>
                    onNavigate("city", {
                      cityName,
                      county: "Mohave County",
                      state: "Arizona",
                    })
                  }
                  className={`group relative overflow-hidden rounded-xl p-5 text-left transition-all duration-200 border ${
                    hasData
                      ? "bg-[var(--surface-2)] border-[var(--line)] hover:border-[var(--line-strong)] hover:bg-[var(--surface-3)]/60 hover:-translate-y-0.5 cursor-pointer"
                      : "bg-[var(--surface-2)]/60 border-[var(--line)]/60 cursor-pointer hover:bg-[var(--surface-3)]/30"
                  }`}
                >
                  <div className="flex items-center justify-between mb-3">
                    <Building2 className="w-3.5 h-3.5 text-muted-foreground/70" />
                    <ChevronRight className="w-3.5 h-3.5 text-muted-foreground/50 group-hover:text-foreground group-hover:translate-x-0.5 transition-all" />
                  </div>
                  <h4 className="text-[15px] font-semibold text-white tracking-wide leading-tight mb-2">
                    {cityName}
                  </h4>
                  {hasData ? (
                    <p className="text-xs text-muted-foreground tabular-nums">
                      {meetingCount.toLocaleString()} meetings
                    </p>
                  ) : (
                    <p className="text-xs text-muted-foreground/70 italic">
                      Awaiting first scrape
                    </p>
                  )}
                </button>
              );
            })}
          </div>
        </section>

        {/* Top Mohave cities by volume — only show if we have data */}
        {mohaveCities.length > 0 && (
          <section className="mb-16">
            <h3 className="kg-eyebrow mb-5">Most Active Mohave Cities</h3>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
              {mohaveCities.slice(0, 8).map((city, idx) => (
                <button
                  key={idx}
                  onClick={() =>
                    onNavigate("city", {
                      cityName: city.city,
                      county: city.county,
                      state: "Arizona",
                    })
                  }
                  className="kg-card p-4 text-left hover:bg-[var(--surface-3)]/60 hover:-translate-y-0.5 transition-all group"
                >
                  <p className="text-sm font-semibold text-white tracking-wide truncate group-hover:text-white">
                    {city.city}
                  </p>
                  <p className="text-[11px] text-muted-foreground mt-0.5 truncate uppercase tracking-wider">
                    {city.county}
                  </p>
                  <p className="text-2xl font-light text-foreground/85 mt-3 tabular-nums">
                    {toSafeInt(city.meetings).toLocaleString()}
                  </p>
                </button>
              ))}
            </div>
          </section>
        )}

        {/* Roadmap */}
        <section>
          <div className="kg-card p-8 text-center">
            <p className="kg-eyebrow mb-3">On the Horizon</p>
            <h4 className="text-xl font-light text-white mb-3 tracking-wide">
              Mohave first. Arizona in progress. State by state from there.
            </h4>
            <p className="text-sm text-muted-foreground max-w-xl mx-auto leading-relaxed">
              Z-SPAN starts in Mohave County, expands across Arizona, and then
              carries the same human-reviewed public record into each state as
              its official sources are mapped and verified.
            </p>
          </div>
        </section>
      </div>

      <footer className="border-t border-[var(--line)] py-6 mt-8">
        <div className="max-w-7xl mx-auto px-6 lg:px-10 text-center">
          <p className="text-[11px] text-muted-foreground uppercase tracking-widest">
            Z-SPAN · Mohave County Pilot · Source-linked · Politically neutral
          </p>
          <p className="mt-2 text-[11px] text-muted-foreground">
            © 2026 James Jones · Uses the{" "}
            <a
              href={NATIONAL_CIVICS_CATALOG_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="text-amber-400 hover:text-amber-300 hover:underline underline-offset-4"
            >
              National Civics Catalog
            </a>
          </p>
        </div>
      </footer>
    </div>
  );
}

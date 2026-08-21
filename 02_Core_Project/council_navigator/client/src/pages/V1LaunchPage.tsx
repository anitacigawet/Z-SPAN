/**
 * V1LaunchPage — V1-Batch-3 progress dashboard.
 *
 * Owner-only single-glance view of the V1 launch board per
 * V1_PUBLIC_RELEASE_SPEC. Shows per-city in-window meeting counts,
 * processed counts, URL-gap counts, plus a global URL-gap board listing
 * every awaiting-URL meeting across all 4 Mohave-County cities.
 *
 * Read-only. Polls /api/v1-launch/progress on mount + when the operator
 * hits Refresh. No mutations from this page (operator pastes URLs via
 * existing OperatorTerminal flows; this page is a glance).
 */
import { useEffect, useState } from "react";
import { ArrowLeft, RefreshCw, AlertCircle, Rocket, Link2Off } from "lucide-react";

type TargetState = "not_started" | "in_progress" | "complete";

interface UrlGapMeeting {
  meeting_id: number;
  work_order_id: number;
  title: string;
  date: string;
  time: string | null;
}

interface CityProgress {
  city: string;
  in_window_meeting_count: number;
  wo_state_counts: Record<string, number>;
  completed_count: number;
  url_gap_count: number;
  url_gap_meetings: UrlGapMeeting[];
  target_state: TargetState;
}

interface Totals {
  in_window_meetings: number;
  processed: number;
  url_gap: number;
  work_orders: number;
}

interface ApiResponse {
  scan_run_at: string;
  window_start: string;
  window_end: string;
  days_back: number;
  v1_target_cities: string[];
  cities: CityProgress[];
  totals: Totals;
  error?: string;
}

const STATE_LABEL: Record<TargetState, string> = {
  not_started: "Not started",
  in_progress: "In progress",
  complete: "Complete",
};

const STATE_PALETTE: Record<TargetState, { dot: string; chip: string; bar: string }> = {
  complete: {
    dot: "bg-[#22d75f] shadow-[0_0_8px_rgba(34,215,95,0.7)]",
    chip: "border-[#22d75f]/40 text-[#a3edb9]",
    bar: "bg-[#22d75f]/80",
  },
  in_progress: {
    dot: "bg-[#f5c33b] shadow-[0_0_8px_rgba(245,195,59,0.7)]",
    chip: "border-[#f5c33b]/40 text-[#f5dca0]",
    bar: "bg-[#f5c33b]/80",
  },
  not_started: {
    dot: "bg-[#9aa8c4]",
    chip: "border-[var(--line)] text-foreground/70",
    bar: "bg-[var(--line)]",
  },
};

function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

interface Props {
  onBack?: () => void;
}

export default function V1LaunchPage({ onBack }: Props) {
  const [data, setData] = useState<ApiResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/v1-launch/progress?days_back=14");
      const json: ApiResponse = await res.json();
      if (json.error) throw new Error(json.error);
      setData(json);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const cities = data?.cities || [];
  const totals = data?.totals || {
    in_window_meetings: 0,
    processed: 0,
    url_gap: 0,
    work_orders: 0,
  };
  const allUrlGapMeetings = cities.flatMap(c =>
    c.url_gap_meetings.map(m => ({ city: c.city, ...m }))
  );

  return (
    <div className="min-h-screen bg-[var(--canvas)] text-foreground">
      <header className="sticky top-0 z-40 bg-[var(--canvas)]/95 backdrop-blur border-b border-[var(--line)]">
        <div className="max-w-7xl mx-auto px-6 lg:px-10 py-5 flex items-center justify-between gap-4">
          <div className="flex items-center gap-5 min-w-0">
            <button
              type="button"
              onClick={() => (onBack ? onBack() : window.history.back())}
              className="group flex items-center gap-2 text-muted-foreground hover:text-foreground transition-colors"
            >
              <ArrowLeft className="w-3.5 h-3.5 group-hover:-translate-x-0.5 transition-transform" />
              <span className="text-xs font-medium tracking-wide uppercase">
                Back
              </span>
            </button>
            <div className="h-4 w-px bg-[var(--line)]" />
            <div className="flex items-center gap-3 min-w-0">
              <Rocket className="w-4 h-4 text-foreground/70" />
              <div>
                <p className="kg-eyebrow">V1 Launch · Operator</p>
                <h1 className="text-[16px] font-semibold text-white">
                  Mohave Acquisition Progress
                </h1>
              </div>
            </div>
            {data && (
              <p className="text-[11px] text-muted-foreground hidden md:block">
                Window {data.window_start} → {data.window_end} · Updated {formatTimestamp(data.scan_run_at)}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={fetchData}
            className="p-2 rounded-md bg-[var(--surface-3)] hover:bg-[var(--line-strong)] text-foreground/70 hover:text-foreground transition-colors"
            title="Refresh"
            aria-label="Refresh"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 lg:px-10 py-10 space-y-6 kg-fade-in">
        <p className="text-[13px] text-muted-foreground max-w-3xl">
          V1 release target: past 2 weeks of council meetings across all 4
          Mohave-County cities, processed end-to-end. Operator pastes
          YouTube URLs for each meeting via the existing OperatorTerminal
          flows; this page is the single-glance acquisition status.
        </p>

        {/* Totals summary */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="kg-card p-5 relative overflow-hidden">
            <div className="absolute top-0 left-0 w-[3px] h-full bg-[var(--line)]" />
            <p className="kg-eyebrow mb-3">In window</p>
            <p className="text-2xl font-light text-white tabular-nums">{totals.in_window_meetings}</p>
            <p className="text-[10px] text-muted-foreground mt-1">meetings scraped</p>
          </div>
          <div className="kg-card p-5 relative overflow-hidden">
            <div className="absolute top-0 left-0 w-[3px] h-full bg-[#22d75f]/80" />
            <p className="kg-eyebrow mb-3">Processed</p>
            <p className="text-2xl font-light text-white tabular-nums">{totals.processed}</p>
            <p className="text-[10px] text-muted-foreground mt-1">complete work orders</p>
          </div>
          <div className="kg-card p-5 relative overflow-hidden">
            <div className="absolute top-0 left-0 w-[3px] h-full bg-[#f5c33b]/80" />
            <p className="kg-eyebrow mb-3">URL gap</p>
            <p className="text-2xl font-light text-white tabular-nums">{totals.url_gap}</p>
            <p className="text-[10px] text-muted-foreground mt-1">awaiting YouTube URLs</p>
          </div>
          <div className="kg-card p-5 relative overflow-hidden">
            <div className="absolute top-0 left-0 w-[3px] h-full bg-[var(--line)]" />
            <p className="kg-eyebrow mb-3">Work orders</p>
            <p className="text-2xl font-light text-white tabular-nums">{totals.work_orders}</p>
            <p className="text-[10px] text-muted-foreground mt-1">total enqueued</p>
          </div>
        </div>

        {/* Per-city cards */}
        {loading ? (
          <div className="flex items-center gap-3 py-12 justify-center">
            <span className="kg-dots"><span /><span /><span /></span>
            <p className="text-[12px] text-muted-foreground">Loading V1 progress…</p>
          </div>
        ) : error ? (
          <div className="kg-card p-6 flex items-center gap-3">
            <AlertCircle className="w-4 h-4 text-destructive" />
            <p className="text-[13px] text-foreground/85">
              Couldn't load V1 progress: {error}
            </p>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
              {cities.map(c => {
                const palette = STATE_PALETTE[c.target_state];
                return (
                  <div key={c.city} className="kg-card p-5 relative overflow-hidden">
                    <div className={`absolute top-0 left-0 w-[3px] h-full ${palette.bar}`} />
                    <div className="flex items-start justify-between gap-2 mb-3">
                      <h3 className="text-[15px] font-semibold text-white">{c.city}</h3>
                      <span className={`px-2 py-0.5 rounded-md text-[9px] uppercase tracking-widest border ${palette.chip}`}>
                        {STATE_LABEL[c.target_state]}
                      </span>
                    </div>
                    <div className="space-y-1.5 text-[12px]">
                      <div className="flex items-baseline justify-between">
                        <span className="text-muted-foreground">In window</span>
                        <span className="text-white tabular-nums">{c.in_window_meeting_count}</span>
                      </div>
                      <div className="flex items-baseline justify-between">
                        <span className="text-muted-foreground">Processed</span>
                        <span className="text-white tabular-nums">{c.completed_count}</span>
                      </div>
                      <div className="flex items-baseline justify-between">
                        <span className="text-muted-foreground">URL gap</span>
                        <span className="text-white tabular-nums">{c.url_gap_count}</span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* URL-gap board */}
            <div className="kg-card overflow-hidden">
              <div className="px-5 py-4 border-b border-[var(--line)] flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Link2Off className="w-4 h-4 text-foreground/60" />
                  <h2 className="text-[14px] font-semibold text-white">
                    URL-gap board
                  </h2>
                  <span className="text-[11px] text-muted-foreground">
                    {allUrlGapMeetings.length} meeting{allUrlGapMeetings.length === 1 ? "" : "s"} awaiting YouTube URLs
                  </span>
                </div>
                <p className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Paste URLs via OperatorTerminal
                </p>
              </div>
              {allUrlGapMeetings.length === 0 ? (
                <div className="p-10 text-center">
                  <p className="text-[13px] text-foreground/80 font-medium">
                    All work orders have URLs.
                  </p>
                  <p className="text-[11px] text-muted-foreground mt-1">
                    Pipeline is unblocked for processing.
                  </p>
                </div>
              ) : (
                <div className="divide-y divide-[var(--line)]">
                  {allUrlGapMeetings.map(m => (
                    <div
                      key={m.work_order_id}
                      className="px-5 py-3 hover:bg-[var(--surface-3)]/30 transition-colors"
                    >
                      <div className="flex items-baseline gap-3 flex-wrap">
                        <span className="text-[10px] uppercase tracking-widest text-muted-foreground tabular-nums">
                          WO #{m.work_order_id}
                        </span>
                        <span className="text-[12px] text-foreground/70">{m.city}</span>
                        <span className="text-[12px] text-muted-foreground tabular-nums">
                          {m.date}{m.time ? ` · ${m.time}` : ""}
                        </span>
                        <span className="text-[13px] text-white flex-1 min-w-0">
                          {m.title}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}

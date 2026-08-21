/**
 * CalendarHealthPage — H-7 operator-facing pattern_health dashboard.
 *
 * Surfaces every tracked (city, pattern) row's most-recent
 * reconciliation status from the H-3 weekly refresh + H-4 drift
 * escalation. Owner-only — viewers see nothing useful here, only James.
 *
 * Sort default: severity DESC (drift first, then partial, then
 * no_data, then match). Within a bucket: most-recent refresh first.
 * The backend handles the sort; this surface is a read-only table
 * with a summary header.
 *
 * Per-row "re-curate" link is intentionally deferred — H-1's
 * extraction workflow is operator-triggered (CLI today) and the
 * orchestrator will eventually recommend it. The button on this
 * surface would just deep-link to a workflow that isn't UI yet.
 */
import { useEffect, useState } from "react";
import { ArrowLeft, RefreshCw, AlertCircle, CalendarCheck, Activity } from "lucide-react";

type Status = "match" | "partial" | "drift" | "no_data";

interface PatternHealthRow {
  id: number;
  city_name: string;
  state: string | null;
  pattern_id: string;
  refreshed_at: string;
  window_start: string;
  window_end: string;
  expected_next: string | null;
  actually_scraped: string | null;
  match_status: Status;
  drift_notes: string | null;
}

interface ApiResponse {
  ok: boolean;
  rows: PatternHealthRow[];
  summary: Record<Status, number>;
  total: number;
  error?: string;
}

const STATUS_LABEL: Record<Status, string> = {
  drift: "Drift",
  partial: "Partial",
  no_data: "No data",
  match: "Match",
};

const STATUS_PALETTE: Record<Status, { dot: string; chip: string; bar: string }> = {
  drift: {
    dot: "bg-[#ff4d4f] shadow-[0_0_8px_rgba(255,77,79,0.7)]",
    chip: "border-[#ff4d4f]/40 text-[#ffb3b4]",
    bar: "bg-[#ff4d4f]/80",
  },
  partial: {
    dot: "bg-[#f5c33b] shadow-[0_0_8px_rgba(245,195,59,0.7)]",
    chip: "border-[#f5c33b]/40 text-[#f5dca0]",
    bar: "bg-[#f5c33b]/80",
  },
  no_data: {
    dot: "bg-[#9aa8c4]",
    chip: "border-[var(--line)] text-foreground/70",
    bar: "bg-[var(--line)]",
  },
  match: {
    dot: "bg-[#22d75f] shadow-[0_0_8px_rgba(34,215,95,0.7)]",
    chip: "border-[#22d75f]/40 text-[#a3edb9]",
    bar: "bg-[#22d75f]/80",
  },
};

function humanizePatternId(pid: string): string {
  return pid
    .split("_")
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function formatRefreshedAt(iso: string): string {
  try {
    const d = new Date(iso.replace(" ", "T"));
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

export default function CalendarHealthPage({ onBack }: Props) {
  const [data, setData] = useState<ApiResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/operator/pattern-health?limit=500");
      const json: ApiResponse = await res.json();
      if (!json.ok) throw new Error(json.error || "request failed");
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

  const rows = data?.rows || [];
  const summary = data?.summary || {
    match: 0,
    partial: 0,
    drift: 0,
    no_data: 0,
  };

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
              <CalendarCheck className="w-4 h-4 text-foreground/70" />
              <div>
                <p className="kg-eyebrow">Phase H · Operator</p>
                <h1 className="text-[16px] font-semibold text-white">
                  Calendar Health
                </h1>
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={fetchData}
            className="p-2 rounded-md bg-[var(--surface-3)] hover:bg-[var(--line-strong)] text-foreground/70 hover:text-foreground transition-colors"
            title="Refresh"
            aria-label="Refresh"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 lg:px-10 py-10 space-y-6 kg-fade-in">
        <p className="text-[13px] text-muted-foreground max-w-3xl">
          Each row is the most recent reconciliation between a city's
          curated meeting pattern and what the parser actually scraped
          for the projection window. Drifts surface first — they're the
          rows where the city's published calendar diverges from the
          pattern and an operator should re-curate or repair the parser.
        </p>

        {/* Summary panel */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {(Object.keys(summary) as Status[]).map(st => (
            <div key={st} className="kg-card p-5 relative overflow-hidden">
              <div className={`absolute top-0 left-0 w-[3px] h-full ${STATUS_PALETTE[st].bar}`} />
              <div className="flex items-center gap-1.5 mb-3">
                <span className={`w-2 h-2 rounded-full ${STATUS_PALETTE[st].dot}`} />
                <span className="kg-eyebrow">{STATUS_LABEL[st]}</span>
              </div>
              <p className="text-2xl font-light text-white tabular-nums">
                {summary[st]}
              </p>
            </div>
          ))}
        </div>

        {/* Table */}
        {loading ? (
          <div className="flex items-center gap-3 py-12 justify-center">
            <span className="kg-dots">
              <span /> <span /> <span />
            </span>
            <p className="text-[12px] text-muted-foreground">
              Loading pattern health…
            </p>
          </div>
        ) : error ? (
          <div className="kg-card p-6 flex items-center gap-3">
            <AlertCircle className="w-4 h-4 text-destructive" />
            <p className="text-[13px] text-foreground/85">
              Couldn't load pattern health: {error}
            </p>
          </div>
        ) : rows.length === 0 ? (
          <div className="kg-card p-10 text-center">
            <Activity className="w-8 h-8 text-muted-foreground/40 mx-auto mb-3" />
            <p className="text-[13px] text-foreground/80 font-medium">
              No pattern health rows yet.
            </p>
            <p className="text-[11px] text-muted-foreground mt-1">
              Run <code className="bg-[var(--surface-3)] px-1.5 py-0.5 rounded">refresh_city_calendars.py</code> to write the first reconciliation.
            </p>
          </div>
        ) : (
          <div className="kg-card overflow-hidden">
            <div className="px-5 py-4 border-b border-[var(--line)] flex items-center justify-between gap-3">
              <h2 className="text-[14px] font-semibold text-white">
                {rows.length} {rows.length === 1 ? "pattern" : "patterns"} tracked
              </h2>
              <p className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Sorted by severity
              </p>
            </div>
            <div className="divide-y divide-[var(--line)]">
              {rows.map(r => {
                const palette = STATUS_PALETTE[r.match_status] || STATUS_PALETTE.no_data;
                let parsedExpected: string[] = [];
                let parsedScraped: string[] = [];
                try {
                  parsedExpected = r.expected_next ? JSON.parse(r.expected_next) : [];
                } catch { /* keep empty */ }
                try {
                  parsedScraped = r.actually_scraped ? JSON.parse(r.actually_scraped) : [];
                } catch { /* keep empty */ }
                return (
                  <div
                    key={r.id}
                    className="px-5 py-4 hover:bg-[var(--surface-3)]/30 transition-colors"
                  >
                    <div className="flex items-start gap-4">
                      <div className="flex items-center gap-2.5 min-w-0 flex-1">
                        <span className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${palette.dot}`} />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-baseline gap-2 flex-wrap">
                            <span className="text-[14px] font-semibold text-white truncate">
                              {r.city_name}
                            </span>
                            <span className="text-[12px] text-muted-foreground">
                              · {humanizePatternId(r.pattern_id)}
                            </span>
                          </div>
                          <p className="text-[11px] text-muted-foreground mt-0.5">
                            Window {r.window_start} → {r.window_end} · Refreshed {formatRefreshedAt(r.refreshed_at)}
                          </p>
                        </div>
                      </div>
                      <span
                        className={`px-2.5 py-1 rounded-md text-[10px] uppercase tracking-widest border ${palette.chip} flex-shrink-0`}
                      >
                        {STATUS_LABEL[r.match_status]}
                      </span>
                    </div>
                    {(r.drift_notes || parsedExpected.length || parsedScraped.length) ? (
                      <div className="mt-3 ml-[1.625rem] space-y-2">
                        {r.drift_notes && (
                          <p className="text-[12px] text-foreground/80 leading-relaxed">
                            {r.drift_notes}
                          </p>
                        )}
                        {(parsedExpected.length > 0 || parsedScraped.length > 0) && (
                          <div className="flex flex-wrap gap-x-6 gap-y-1 text-[11px] text-muted-foreground tabular-nums">
                            {parsedExpected.length > 0 && (
                              <span>
                                <span className="font-semibold text-foreground/80">Expected:</span>{" "}
                                {parsedExpected.join(" · ")}
                              </span>
                            )}
                            {parsedScraped.length > 0 && (
                              <span>
                                <span className="font-semibold text-foreground/80">Scraped:</span>{" "}
                                {parsedScraped.join(" · ")}
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

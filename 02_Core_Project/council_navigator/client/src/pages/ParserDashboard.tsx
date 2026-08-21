/**
 * ParserDashboard — Phase I cinematic rebuild.
 *
 * Replaces the old AZ-specific sortable-card layout with a Larry-King-CNN
 * LED-grid US map where each parser = one dot at its city's geographic
 * position, colored by status (green=working / red=broken / yellow=
 * maintenance / hollow grey=pending). Hover surfaces an HQ-style detail
 * tooltip. Full-page background — the LED grid IS the dashboard, status
 * legend + summary count + test-all action overlay as floating chrome.
 *
 * Architecture per [D-083](DECISIONS.md#d-083) (Phase I closeout).
 *
 * Phase I chunk I-5 (2026-06-02).
 */
import { useState, useEffect } from "react";
import { Play, ArrowLeft, RefreshCw } from "lucide-react";
import ParserMap, {
  type ParserMarker,
  type LedStatus,
} from "@/components/parsers/ParserMap";
import { lookupCity } from "@/lib/guideGeo";
import { useServerCoordsTick } from "@/data/serverCoords";
import "@/components/parsers/ParserMap.css";
import "./parser-dashboard.css";

interface ParserStatus {
  city: string;
  county: string;
  status: "untested" | "testing" | "working" | "broken";
  meetingCount?: number;
  error?: string;
  errorType?: "http" | "dependency" | "parsing" | "unknown";
  errorDetails?: string;
  lastTested?: string;
  parserFilePresent: boolean;
}

interface ParserHealthDto {
  city: string;
  county: string;
  parser_file: boolean;
  status: "untested" | "working" | "broken";
  meeting_count: number;
  last_scanned_at: string;
}

export default function ParserDashboard({
  onBack,
}: {
  onBack?: () => void;
}) {
  const [parserStatuses, setParserStatuses] = useState<ParserStatus[]>([]);
  const [testingAll, setTestingAll] = useState(false);
  // Re-render markers when the gazetteer cache gains a new entry
  // (covers any AZ city that lazy-resolves through /api/gazetteer/lookup).
  useServerCoordsTick();

  useEffect(() => {
    const initialize = async () => {
      try {
        const response = await fetch("/api/parser-health");
        const data: { parsers?: ParserHealthDto[]; error?: string } =
          await response.json();
        if (!response.ok || !Array.isArray(data.parsers)) {
          throw new Error(data.error || "Parser health unavailable");
        }
        const statuses = data.parsers.map((parser): ParserStatus => ({
          city: parser.city,
          county: parser.county,
          status: parser.status,
          meetingCount: parser.meeting_count,
          lastTested: parser.last_scanned_at || undefined,
          parserFilePresent: parser.parser_file,
        }));
        setParserStatuses(statuses.sort((a, b) => a.city.localeCompare(b.city)));
      } catch {
        setParserStatuses([]);
      }
    };
    initialize();
  }, []);

  const saveResults = async (statuses: ParserStatus[]) => {
    try {
      const map: Record<string, unknown> = {};
      statuses.forEach(s => {
        map[s.city] = {
          status: s.status,
          meetingCount: s.meetingCount,
          error: s.error,
          errorType: s.errorType,
          errorDetails: s.errorDetails,
          lastTested: s.lastTested,
        };
      });
      await fetch("/api/parser-results/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ results: map }),
      });
    } catch {
      /* ignore */
    }
  };

  const testParser = async (cityName: string) => {
    setParserStatuses(prev =>
      prev.map(p => (p.city === cityName ? { ...p, status: "testing" as const } : p)),
    );
    try {
      const response = await fetch("/api/calendar/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cityName }),
      });
      const data = await response.json();
      const updated = (prev: ParserStatus[]) =>
        prev.map(p =>
          p.city === cityName
            ? data.success && data.count > 0
              ? {
                  ...p,
                  status: "working" as const,
                  meetingCount: data.count,
                  lastTested: new Date().toISOString(),
                }
              : {
                  ...p,
                  status: "broken" as const,
                  meetingCount: 0,
                  error: data.error || "No meetings found",
                  errorType: data.error_type,
                  errorDetails: data.error_details,
                  lastTested: new Date().toISOString(),
                }
            : p,
        );
      setParserStatuses(prev => {
        const next = updated(prev);
        saveResults(next);
        return next;
      });
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : String(error);
      setParserStatuses(prev => {
        const next = prev.map(p =>
          p.city === cityName
            ? {
                ...p,
                status: "broken" as const,
                error: message,
                errorType: "http" as const,
                lastTested: new Date().toISOString(),
              }
            : p,
        );
        saveResults(next);
        return next;
      });
    }
  };

  const testAllParsers = async () => {
    setTestingAll(true);
    for (const parser of parserStatuses) {
      await testParser(parser.city);
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    setTestingAll(false);
  };

  // (Manual per-city scrape UI retired with the daemon control surface
  // per D-169 — the /api/calendar/events + X-Scrape-Password backend
  // path per PR #46 survives; the BitTorrent parser-view redesign will
  // re-wire it from its per-city tree-node action. testParser above
  // remains for the "Run full sweep" per-city test invocation.)

  // Derive the LED status from the underlying parser status. Maintenance
  // auto-fires when a scrape SUCCEEDED but returned zero meetings — the
  // strongest "needs eye" signal we can derive without per-parser
  // baselines (per I-3 + D-083; sample_meeting_count baseline comparison
  // + operator-flag mechanism are tracked as Phase I follow-ups).
  const deriveLedStatus = (p: ParserStatus): LedStatus => {
    if (!p.parserFilePresent) return "broken";
    if (p.status === "broken") return "broken";
    if (p.status === "untested" || p.status === "testing") return "pending";
    if (p.status === "working" && (!p.meetingCount || p.meetingCount === 0)) {
      return "maintenance";
    }
    return "working";
  };

  const ledMarkers: ParserMarker[] = parserStatuses.map(p => {
    const coords = lookupCity("AZ", p.county, p.city);
    return {
      id: `AZ|${p.county}|${p.city}`,
      label: p.city,
      lng: coords?.lng ?? null,
      lat: coords?.lat ?? null,
      status: deriveLedStatus(p),
      county: p.county,
      state: "AZ",
      meetingCount: p.meetingCount,
      lastTested: p.lastTested,
      error: p.error,
    };
  });

  // Counts derived from the LED-status taxonomy (post-derivation) so the
  // legend matches what's actually rendered on the grid.
  const ledCounts = ledMarkers.reduce(
    (acc, m) => {
      acc[m.status] += 1;
      return acc;
    },
    { working: 0, broken: 0, maintenance: 0, pending: 0 } as Record<LedStatus, number>,
  );

  return (
    <div className="parser-dashboard">
      {/* Full-bleed Leaflet US map — real state borders + city labels
       *  give geographic context so the AZ cluster is anchored
       *  spatially. Replaces the pure-SVG LED grid (I-5) per James
       *  2026-06-02 — same LED dot styling, real map underneath. */}
      <div className="parser-dashboard-grid-bg">
        <ParserMap parserMarkers={ledMarkers} />
      </div>

      {/* Floating chrome — title + counts (top-left), legend (top-right),
       *  test-all action (bottom-right). All translucent so the grid
       *  reads through. */}
      <header className="parser-dashboard-chrome-top">
        <div className="parser-dashboard-title-block">
          <button
            type="button"
            className="parser-dashboard-back-btn"
            onClick={() => (onBack ? onBack() : window.history.back())}
            aria-label="Back"
          >
            <ArrowLeft size={14} />
            <span>Back</span>
          </button>
          <div className="parser-dashboard-title-stack">
            <div className="parser-dashboard-eyebrow">Parser Health</div>
            <div className="parser-dashboard-title">Per-jurisdiction scrapers</div>
            <div className="parser-dashboard-subtitle">
              {ledMarkers.length} parsers tracked · hover any dot for detail
            </div>
          </div>
        </div>

        <ul className="parser-dashboard-legend" aria-label="Status legend">
          <li className="parser-dashboard-legend-item">
            <span className="parser-dashboard-legend-dot parser-dashboard-legend-dot--working" />
            <span className="parser-dashboard-legend-label">Working</span>
            <span className="parser-dashboard-legend-count">{ledCounts.working}</span>
          </li>
          <li className="parser-dashboard-legend-item">
            <span className="parser-dashboard-legend-dot parser-dashboard-legend-dot--broken" />
            <span className="parser-dashboard-legend-label">Broken</span>
            <span className="parser-dashboard-legend-count">{ledCounts.broken}</span>
          </li>
          <li className="parser-dashboard-legend-item">
            <span className="parser-dashboard-legend-dot parser-dashboard-legend-dot--maintenance" />
            <span className="parser-dashboard-legend-label">Maintenance</span>
            <span className="parser-dashboard-legend-count">{ledCounts.maintenance}</span>
          </li>
          <li className="parser-dashboard-legend-item">
            <span className="parser-dashboard-legend-dot parser-dashboard-legend-dot--pending" />
            <span className="parser-dashboard-legend-label">Pending</span>
            <span className="parser-dashboard-legend-count">{ledCounts.pending}</span>
          </li>
          {/* STATUS chip retired with the scrape daemon per D-169. */}
        </ul>
      </header>

      <div className="parser-dashboard-chrome-bottom">
        <button
          type="button"
          className="parser-dashboard-test-all"
          onClick={testAllParsers}
          disabled={testingAll}
        >
          {testingAll ? (
            <>
              <RefreshCw size={14} className="parser-dashboard-test-all-spin" />
              <span>Sweeping all parsers…</span>
            </>
          ) : (
            <>
              <Play size={14} />
              <span>Run full sweep</span>
            </>
          )}
        </button>
      </div>

      {/* StatusDrawer + ScrapeStatusPanel + Manual city scrapes list all
       *  retired with the scrape daemon per D-169. The BitTorrent parser-
       *  view redesign will re-surface per-city on-demand scrape via its
       *  tree-node action (which will hit the same /api/calendar/events +
       *  X-Scrape-Password backend path per PR #46). */}
    </div>
  );
}

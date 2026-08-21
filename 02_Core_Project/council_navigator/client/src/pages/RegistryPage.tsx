/**
 * Public explanation of Z-SPAN's application/source-catalog boundary plus
 * the live Z-SPAN coverage listing. The reusable official-source map lives
 * in the separately licensed catalog; this page's city statuses remain application truth
 * from GET /api/coverage.
 */
import { useEffect, useMemo, useState } from "react";
import { NATIONAL_CIVICS_CATALOG_URL } from "../lib/projectMeta";
import { fetchForPlane } from "../lib/planeFetch";

// Showcase edition (VITE_ZSPAN_EDITION=showcase): the static bake gates off
// the corrections surface, so the "corrections door" link below (which
// would dead-link) is dropped too. Inline import.meta.env compare so the
// bundler folds + DCEs it. Flagship leaves the var unset → link stays.
const IS_SHOWCASE = import.meta.env.VITE_ZSPAN_EDITION === "showcase";
type CoverageRow = {
  city: string;
  county: string | null;
  state: string;
  status: string;
  published_count: number;
  latest_published_date: string | null;
};

const STATUS_STYLE: Record<string, string> = {
  covered: "border-[var(--success-green)]/40 text-[var(--success-green)]",
  monitored: "border-white/25 text-gray-300",
  "needs repair": "border-[#F5A524]/40 text-[#F5A524]",
  postponed: "border-white/20 text-gray-400",
  "no video source": "border-white/20 text-gray-400",
  "assessment pending": "border-white/10 text-gray-500",
};

const STATE_NAMES: Record<string, string> = {
  AZ: "Arizona",
  NV: "Nevada",
  UT: "Utah",
  VA: "Virginia",
};

export default function RegistryPage() {
  const [rows, setRows] = useState<CoverageRow[] | null>(null);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchForPlane({
      publicPath: "/public-api/coverage",
      operatorPath: "/api/coverage",
    })
      .then((r) => r.json())
      .then((data) => {
        if (cancelled) return;
        if (data?.success) setRows(data.cities ?? []);
        else setLoadError(true);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const grouped = useMemo(() => {
    if (!rows) return [];
    const byState = new Map<string, CoverageRow[]>();
    for (const row of rows) {
      const key = row.state || "—";
      if (!byState.has(key)) byState.set(key, []);
      byState.get(key)!.push(row);
    }
    return Array.from(byState.entries());
  }, [rows]);

  const coveredCount = rows?.filter((r) => r.status === "covered").length ?? 0;

  return (
    <div className="min-h-screen bg-[#0A0A0A] text-white">
      <div className="max-w-2xl mx-auto px-5 py-10">
        <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-4">
          Z-SPAN · Sources and software
        </div>
        <h1 className="text-[24px] font-semibold leading-tight mb-4">
          What is published, and what stays inside Z-SPAN
        </h1>

        <p className="text-[14px] text-gray-300 leading-relaxed mb-4">
          Z-SPAN is a source-available application. Its code is published
          under PolyForm Noncommercial 1.0.0, so people can inspect it and
          use, modify, or redistribute it for purposes the license allows.
        </p>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
          Collection-level meeting sources live in a separate public catalog,
          connected to the governments or named civic bodies that publish them
          and the places they cover. That source map can be useful well beyond
          this one application without mixing in Z-SPAN's own episodes.
        </p>

        <h2 className="text-[16px] font-semibold mb-3">
          The public civic source catalog
        </h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-4">
          The{" "}
          <a
            href={NATIONAL_CIVICS_CATALOG_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="text-amber-400 hover:text-amber-300 hover:underline underline-offset-4"
          >
            National Civics Catalog
          </a>{" "}
          records meeting calendars, document indexes, video pages, APIs, and
          feeds state by state, beginning with Arizona. Inclusion identifies the
          named publisher and source relationship; it does not imply government
          status, endorsement, or authority over a place.
          It uses PolyForm Noncommercial 1.0.0, so another person or project
          can inspect it, improve it, mirror it, or build on it for purposes
          the license allows.
        </p>
        <ul className="text-[14px] text-gray-300 leading-relaxed mb-8 space-y-3">
          <li>
            <span className="text-white font-medium">Source locations.</span>{" "}
            The catalog points to collection-level pages and machine-readable
            endpoints published by the named body or its authorized service.
            It does not copy meeting records into the catalog.
          </li>
          <li>
            <span className="text-white font-medium">Provenance.</span>{" "}
            Each entry identifies the publisher, its relationship to the
            source, the places the source covers, and, when known, the date it
            was last checked.
          </li>
          <li>
            <span className="text-white font-medium">Honest status.</span>{" "}
            A working source, a confirmed empty source, and a source blocked
            by its host are different conditions and are recorded separately.
          </li>
        </ul>

        <h2 className="text-[16px] font-semibold mb-3">
          What the catalog does not contain
        </h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-4">
          The catalog is a reusable map of public sources. Z-SPAN's
          per-jurisdiction parser implementations live in the Z-SPAN
          application repository, where they turn differently shaped source
          sites into one consistent record. Keeping that code out of the
          catalog lets the source map remain useful without choosing one
          implementation.
        </p>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
          It also does not contain Z-SPAN episodes, transcripts, summaries,
          key decisions, or other records created by Z-SPAN. Those belong to
          the application and its publication process, not to the reusable
          source catalog.
        </p>

        <h2 className="text-[16px] font-semibold mb-3">
          How source corrections work
        </h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-4">
          Anyone may propose a source addition or correction in the source
          catalog. Before it enters a release, the source is checked against the
          named publisher, its first-party or authorized host, and the catalog's
          documented shape. Z-SPAN then decides separately whether and how to
          use that source in its own application.
        </p>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
          This keeps the public map broadly useful without giving a catalog
          contribution control over Z-SPAN's application, processing, or
          publication decisions.
        </p>

        <h2 className="text-[16px] font-semibold mb-3">
          Coverage stays publicly visible
        </h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-4">
          The listing below shows Z-SPAN's own current coverage status. It is
          separate from the source catalog: a jurisdiction can have verified
          public sources before Z-SPAN has prepared and published a broadcast.
        </p>

        {/* ── The live coverage listing (S-124) ── */}
        <div className="border border-white/10 rounded-lg p-5 mb-8 bg-[#141416]">
          <div className="flex items-baseline justify-between gap-3 mb-3 flex-wrap">
            <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72]">
              Coverage listing
            </div>
            {rows && (
              <div className="text-[11px] text-gray-500">
                {rows.length} jurisdictions · {coveredCount} with
                published broadcasts
              </div>
            )}
          </div>
          <p className="text-[12px] text-gray-500 leading-relaxed mb-4">
            The label on each jurisdiction tells you where it stands:{" "}
            <span className="text-[var(--success-green)]">covered</span> means
            published broadcasts exist;{" "}
            <span className="text-gray-300">monitored</span> means we're
            tracking its calendar but nothing's published yet;{" "}
            <span className="text-amber-400">needs repair</span> means its
            source or normalization path needs attention; and{" "}
            <span className="text-gray-400">postponed</span>,{" "}
            <span className="text-gray-400">no video source</span>, and{" "}
            <span className="text-gray-400">assessment pending</span> are
            honest not-yet states.
          </p>
          {loadError && (
            <p className="text-[13px] text-gray-500">
              The listing couldn't load just now — refresh to retry.
            </p>
          )}
          {!loadError && rows === null && (
            <p className="text-[13px] text-gray-500">Loading…</p>
          )}
          {!loadError &&
            grouped.map(([state, stateRows]) => (
              <div key={state} className="mb-5 last:mb-0">
                <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-gray-400 mb-2">
                  {STATE_NAMES[state] ?? state}{" "}
                  <span className="text-gray-600 normal-case tracking-normal">
                    · {stateRows.length} jurisdictions
                  </span>
                </div>
                <ul className="space-y-1.5">
                  {stateRows.map((row) => (
                    <li
                      key={`${row.state}-${row.city}`}
                      className="flex items-center gap-2 flex-wrap text-[13px]"
                    >
                      <span
                        className={`inline-block border rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${
                          STATUS_STYLE[row.status] ?? STATUS_STYLE["assessment pending"]
                        }`}
                      >
                        {row.status}
                      </span>
                      <span className="text-white">{row.city}</span>
                      <span className="text-gray-600 text-[11px]">
                        {row.county}
                      </span>
                      {row.published_count > 0 && (
                        <span className="text-gray-500 text-[11px]">
                          {row.published_count} broadcast
                          {row.published_count === 1 ? "" : "s"}
                          {row.latest_published_date
                            ? ` · latest ${row.latest_published_date}`
                            : ""}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
        </div>

        <h2 className="text-[16px] font-semibold mb-3">
          If Z-SPAN changes or ends
        </h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
          The source catalog does not depend on Z-SPAN continuing to operate.
          Its released source map and the application can be inspected,
          preserved, modified, and redistributed for purposes allowed by their
          PolyForm Noncommercial licenses. The Z-SPAN name and other trademarks
          remain separate from those copyright grants.
        </p>

        <h2 className="text-[16px] font-semibold mb-3">
          The licensing picture, in one breath
        </h2>
        <div className="border border-white/10 rounded-lg overflow-hidden mb-8">
          <table className="w-full text-[13px]">
            <tbody>
              {(
                [
                  ["Z-SPAN application code", "PolyForm Noncommercial 1.0.0"],
                  ["National Civics Catalog", "PolyForm Noncommercial 1.0.0"],
                  ["Per-jurisdiction parser implementation", "Part of the Z-SPAN application repository"],
                  ["Z-SPAN episodes and processed records", "Not part of the source catalog"],
                  ["Underlying government records", "Not licensed by this catalog; subject to applicable law and source terms"],
                ] as [string, string][]
              ).map(([layer, license]) => (
                <tr key={layer} className="border-b border-white/5 last:border-b-0">
                  <td className="px-3 py-2.5 text-gray-300 align-top w-1/2">{layer}</td>
                  <td className="px-3 py-2.5 text-gray-400 align-top">{license}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {!IS_SHOWCASE && (
        <p className="text-[12px] text-gray-500 leading-relaxed">
          Questions about any of this are welcome — write to us through{" "}
          <a
            href="/?view=corrections"
            className="text-amber-400 hover:text-amber-300 hover:underline underline-offset-4"
          >
            the corrections door
          </a>{" "}
          or the{" "}
          <a
            href={`${NATIONAL_CIVICS_CATALOG_URL}/issues`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-amber-400 hover:text-amber-300 hover:underline underline-offset-4"
          >
            source catalog's issue tracker
          </a>
          .
        </p>
        )}
      </div>
    </div>
  );
}

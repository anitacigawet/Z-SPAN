/**
 * RR-4 — the public corrections page (S-043 B-4, "the institutional
 * doorbell"). Copy is rendered VERBATIM from
 * 01_Project_Overview/CORRECTIONS_POLICY_DRAFT.md (the calm version) so
 * that what the operator reviews pre-flip is exactly what ships — James
 * approves every word before the perimeter opens.
 *
 * Intake is temporarily closed. The page's second half still renders the
 * PUBLIC corrections log from GET /api/corrections — "visibly, not
 * silently" is the policy's promise, and an honestly-empty log is itself
 * a trust signal.
 */
import { useEffect, useState } from "react";
import { fetchForPlane } from "../lib/planeFetch";

type CorrectionRow = {
  id?: number;
  meeting_id?: number | null;
  public_id?: string;
  corrected_surface: string | null;
  status: "under_review" | "corrected" | "record_stands" | "disputed_ambiguous";
  summary_public: string | null;
  reported_at: string;
  resolved_at: string | null;
  city_name: string | null;
  meeting_date: string | null;
  meeting_title: string | null;
};

const STATUS_LABELS: Record<CorrectionRow["status"], { label: string; cls: string }> = {
  under_review: {
    label: "Under review",
    cls: "border-white/20 text-gray-300",
  },
  corrected: {
    label: "Corrected",
    cls: "border-[#F5A524]/50 text-[#F5A524]",
  },
  record_stands: {
    label: "Record stands",
    cls: "border-white/20 text-gray-300",
  },
  disputed_ambiguous: {
    label: "Disputed — both readings published",
    cls: "border-white/30 text-gray-200",
  },
};

export default function CorrectionsPage({
  disputeContext,
}: {
  meetingId?: number;
  publicId?: string;
  disputeContext?: string;
}) {
  const [rows, setRows] = useState<CorrectionRow[] | null>(null);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchForPlane({
      publicPath: "/public-api/corrections",
      operatorPath: "/api/corrections",
    })
      .then((r) => r.json())
      .then((data) => {
        if (cancelled) return;
        if (data?.success) setRows(data.corrections ?? []);
        else setLoadError(true);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="min-h-screen bg-[#0A0A0A] text-white">
      <div className="max-w-2xl mx-auto px-5 py-10">
        <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-4">
          Z-SPAN · Corrections
        </div>
        <h1 className="text-[24px] font-semibold leading-tight mb-4">Corrections</h1>

        {disputeContext && (
          <div className="border border-[#F5A524]/25 rounded-lg p-4 mb-6 bg-[#F5A524]/[0.04]">
            <p className="text-[13px] text-gray-300 leading-relaxed">
              You're here about{" "}
              <span className="text-white font-medium">{disputeContext}</span>.
              Correction intake is temporarily closed.
            </p>
          </div>
        )}

        {/* ── Policy copy with the current intake-closure notice ── */}
        <p className="text-[14px] text-gray-300 leading-relaxed mb-6">
          Z-SPAN's job is to present an accurate, neutral record of what
          happened in public meetings. When something we've published is
          wrong, we want to know, and we'll fix it.
        </p>

        <h2 className="text-[16px] font-semibold mb-3">
          If you think something is inaccurate
        </h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
          Corrections intake is temporarily closed. This page will be
          updated when a new contact channel is available.
        </p>

        <h2 className="text-[16px] font-semibold mb-3">What happens next</h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-4">
          This project is run by one person with a careful process, so
          here's an honest promise instead of a corporate one:{" "}
          <span className="text-white font-medium">
            you'll get a human acknowledgment within a few days
          </span>
          , and the review starts from the source recording — the same one
          linked on every page. We check the claim against what was
          actually said, not against what anyone prefers had been said.
        </p>
        <ul className="text-[14px] text-gray-300 leading-relaxed mb-8 space-y-3">
          <li>
            <span className="text-white font-medium">If we got it wrong</span>
            , we correct the page, and the correction is noted on the page
            itself — visibly, not silently. Corrections are part of the
            record here, not something we're embarrassed by. A running log
            of corrections stays public.
          </li>
          <li>
            <span className="text-white font-medium">
              If the record supports what we published
            </span>
            , we'll explain why, with the timestamp so you can check it
            yourself. You don't have to take our word for anything — that's
            the point of this whole project.
          </li>
          <li>
            <span className="text-white font-medium">
              If it's genuinely ambiguous
            </span>{" "}
            (audio is unclear, context cuts both ways), we'll say so on the
            page. "Disputed, here's both readings" is an honest state and
            we're comfortable publishing it.
          </li>
        </ul>

        <h2 className="text-[16px] font-semibold mb-3">
          If you're an elected official or city staff
        </h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
          Same door, same process — and genuinely: we'd rather hear from
          you early than have an error stand. Every extracted quote links
          to the moment in your meeting's recording, so reviewing a concern
          together usually takes minutes. Corrections don't come with
          commentary or a news cycle attached. We fix things and move on.
        </p>

        <h2 className="text-[16px] font-semibold mb-3">
          If you appear in a public recording and have concerns
        </h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
          Public meetings are public records, and Z-SPAN presents them as
          the city published them. We also understand that speaking at your
          city council meeting is not the same as expecting to be indexed
          on the internet. This project is young and we're working out the
          right way to honor both truths. If you appear in a recording and
          something about how it's presented here concerns you, write to
          the same address — a human reads every message, and we'll talk it
          through case by case while our policy matures.
        </p>

        <h2 className="text-[16px] font-semibold mb-3">What this page is not</h2>
        <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
          We don't remove accurate records of public proceedings because
          someone dislikes them — that would defeat the purpose of a public
          record. And we don't adjudicate politics: corrections here are
          about <em>what was said and decided</em>, never about whether it
          was right.
        </p>

        {/* ── The running public log ── */}
        <div className="border border-white/10 rounded-lg p-5 mb-8 bg-[#141416]">
          <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-3">
            Corrections log
          </div>
          {loadError && (
            <p className="text-[13px] text-gray-500">
              The log couldn't load just now — refresh to retry.
            </p>
          )}
          {!loadError && rows === null && (
            <p className="text-[13px] text-gray-500">Loading…</p>
          )}
          {!loadError && rows !== null && rows.length === 0 && (
            <p className="text-[13px] text-gray-400 leading-relaxed">
              No corrections yet. When we make one, it appears here — with
              what was wrong and what changed.
            </p>
          )}
          {!loadError && rows !== null && rows.length > 0 && (
            <ul className="space-y-4">
              {rows.map((row) => {
                const pill = STATUS_LABELS[row.status] ?? STATUS_LABELS.under_review;
                const where =
                  row.corrected_surface ||
                  [row.city_name, row.meeting_date, row.meeting_title]
                    .filter(Boolean)
                    .join(" · ");
                return (
                  <li
                    key={row.public_id ?? row.id ?? `${row.reported_at}-${row.corrected_surface}`}
                    className="border-b border-white/5 pb-3 last:border-b-0 last:pb-0"
                  >
                    <div className="flex items-center gap-2 mb-1 flex-wrap">
                      <span
                        className={`inline-block border rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${pill.cls}`}
                      >
                        {pill.label}
                      </span>
                      <span className="text-[11px] text-gray-500">
                        reported {String(row.reported_at).slice(0, 10)}
                        {row.resolved_at
                          ? ` · resolved ${String(row.resolved_at).slice(0, 10)}`
                          : ""}
                      </span>
                    </div>
                    {where && (
                      <div className="text-[13px] text-white font-medium mb-0.5">
                        {where}
                      </div>
                    )}
                    {row.summary_public && (
                      <p className="text-[13px] text-gray-300 leading-relaxed">
                        {row.summary_public}
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <p className="text-[12px] text-gray-500 leading-relaxed italic">
          Everything on this site links back to the city's own published
          recording. The fastest way to check us is to click through and
          listen.
        </p>
      </div>
    </div>
  );
}

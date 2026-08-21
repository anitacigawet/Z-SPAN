/**
 * EscalationsInboxPage — S-004 operator review surface for agent escalations.
 *
 * Lists every `pending_escalations` row with `acknowledged_at IS NULL`. When
 * an employee-agent encounters a case its manual doesn't cover, it escalates
 * via parsers/slack_notifier — the message goes to Slack AND is preserved in
 * the table as the canonical record. This page is the operator's recovery
 * surface: review each escalation, follow the deep link to the affected
 * operator surface to act, then acknowledge here to clear the badge.
 *
 * Design (D-054): plain language, single source of truth per row, primary
 * action prominent. Each escalation is one card; the agent's reasoning
 * renders as sentences, not labeled JSON fields. Severity is a small
 * decorative pill (visual furniture) — the body content does the work.
 */
import { useEffect, useState } from "react";
import {
  ArrowLeft,
  ArrowUpRight,
  CheckCircle2,
  Inbox,
  RefreshCw,
} from "lucide-react";

interface EscalationsInboxPageProps {
  onBack: () => void;
}

type Severity = "info" | "decision" | "blocked" | "error";

type Escalation = {
  id: number;
  agent_role: string;
  severity: Severity;
  summary: string;
  what_i_see: string[];
  what_id_do: string[];
  deep_link: string | null;
  audit_row: string | null;
  created_at: string;
  delivered_to_slack: number;
  delivered_at: string | null;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
};

type InboxPayload = {
  unacknowledged_count: number;
  undelivered_count: number;
  escalations: Escalation[];
};

// Severity → visual furniture. These pills are decorative category markers
// the eye learns to skip after the first glance; the body content carries
// the real signal. Per James's user-level CLAUDE guidance, uppercase pills
// are OK in this role.
const SEVERITY_STYLES: Record<Severity, { label: string; className: string }> = {
  info: {
    label: "INFO",
    className: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  },
  decision: {
    label: "DECISION",
    className: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  },
  blocked: {
    label: "BLOCKED",
    className: "bg-red-500/15 text-red-300 border-red-500/30",
  },
  error: {
    label: "ERROR",
    className: "bg-red-600/20 text-red-200 border-red-600/40",
  },
};

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export default function EscalationsInboxPage({ onBack }: EscalationsInboxPageProps) {
  const [payload, setPayload] = useState<InboxPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const fetchInbox = () => {
    setLoading(true);
    setError(null);
    fetch("/api/operator/pending-escalations")
      .then(async r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(data => setPayload(data))
      .catch(e => setError(e?.message ?? String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(fetchInbox, []);

  const acknowledge = async (e: Escalation) => {
    if (busyId !== null) return;
    setBusyId(e.id);
    try {
      const r = await fetch(
        `/api/operator/pending-escalations/${e.id}/acknowledge`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ acknowledged_by: "operator" }),
        }
      );
      const data = await r.json().catch(() => null);
      if (!r.ok || !data?.ok) {
        throw new Error(data?.error ?? `HTTP ${r.status}`);
      }
      // Optimistic remove from the visible list; re-fetch to refresh count.
      setPayload(prev =>
        prev
          ? {
              ...prev,
              escalations: prev.escalations.filter(x => x.id !== e.id),
              unacknowledged_count: Math.max(0, prev.unacknowledged_count - 1),
            }
          : prev
      );
    } catch (err: any) {
      setError(err?.message ?? String(err));
    } finally {
      setBusyId(null);
    }
  };

  const escalations = payload?.escalations ?? [];
  const unackCount = payload?.unacknowledged_count ?? 0;
  const undeliveredCount = payload?.undelivered_count ?? 0;

  return (
    <div className="min-h-screen bg-[#0A0A0A] text-white font-sans">
      <header className="sticky top-0 z-40 bg-[#0A0A0A]/95 backdrop-blur border-b border-white/10">
        <div className="max-w-5xl mx-auto px-6 py-5 flex items-center justify-between gap-4">
          <div className="flex items-center gap-4">
            <button
              onClick={onBack}
              className="flex items-center gap-2 text-gray-400 hover:text-white transition-colors"
            >
              <ArrowLeft className="w-4 h-4" />
              <span className="text-xs font-medium tracking-wide uppercase">Back</span>
            </button>
            <div className="h-4 w-px bg-white/10" />
            <h1 className="text-lg font-semibold">Pending escalations</h1>
            {unackCount > 0 && (
              <span className="px-2 py-0.5 rounded text-xs bg-amber-500/20 text-amber-300 border border-amber-500/40">
                {unackCount} awaiting
              </span>
            )}
          </div>
          <button
            onClick={fetchInbox}
            disabled={loading}
            className="flex items-center gap-2 text-xs text-gray-400 hover:text-white disabled:opacity-50"
            title="Refresh"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
            <span className="tracking-wide uppercase">Refresh</span>
          </button>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-8">
        <p className="text-gray-400 text-[14px] mb-6 leading-relaxed">
          When an agent hits a case its manual doesn&apos;t cover, it posts here.
          Read the agent&apos;s reasoning, follow the deep link to act on the
          underlying surface, then acknowledge to clear the badge.
          {undeliveredCount > 0 && (
            <span className="block mt-2 text-amber-300/80">
              {undeliveredCount} of these never reached Slack — webhook may be
              unreachable.
            </span>
          )}
        </p>

        {error && (
          <div className="mb-6 px-4 py-3 rounded border border-red-500/40 bg-red-500/10 text-red-300 text-sm">
            {error}
          </div>
        )}

        {loading && !payload && (
          <div className="text-gray-500 text-sm">Loading…</div>
        )}

        {!loading && escalations.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-20 text-gray-500">
            <Inbox className="w-10 h-10 mb-3 opacity-50" />
            <p className="text-[15px]">No pending escalations.</p>
            <p className="text-[13px] mt-1">All agent surfaces are clear.</p>
          </div>
        )}

        <div className="space-y-4">
          {escalations.map(e => {
            const sev = SEVERITY_STYLES[e.severity] ?? SEVERITY_STYLES.info;
            const isBusy = busyId === e.id;
            return (
              <article
                key={e.id}
                className="border border-white/10 rounded-lg bg-[#111111] p-5"
              >
                <header className="flex items-start justify-between gap-4 mb-3">
                  <div className="flex items-center gap-3 flex-wrap">
                    <span
                      className={`px-2 py-0.5 rounded text-[10px] tracking-widest border ${sev.className}`}
                    >
                      {sev.label}
                    </span>
                    <span className="text-[13px] text-gray-300 font-medium">
                      {e.agent_role}
                    </span>
                    <span className="text-[12px] text-gray-500">
                      {formatTimestamp(e.created_at)}
                    </span>
                    {!e.delivered_to_slack && (
                      <span
                        className="text-[11px] text-amber-400/80"
                        title="Not delivered to Slack"
                      >
                        slack failed
                      </span>
                    )}
                  </div>
                </header>

                <p className="text-[15px] text-white leading-relaxed mb-4">
                  {e.summary}
                </p>

                {e.what_i_see.length > 0 && (
                  <div className="mb-3">
                    <div className="text-[12px] text-gray-500 mb-1">What I see</div>
                    <ul className="text-[14px] text-gray-200 space-y-1 list-disc pl-5">
                      {e.what_i_see.map((line, i) => (
                        <li key={i}>{line}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {e.what_id_do.length > 0 && (
                  <div className="mb-3">
                    <div className="text-[12px] text-gray-500 mb-1">
                      What I&apos;d do if forced
                    </div>
                    <ul className="text-[14px] text-gray-200 space-y-1 list-disc pl-5">
                      {e.what_id_do.map((line, i) => (
                        <li key={i}>{line}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {(e.deep_link || e.audit_row) && (
                  <div className="flex items-center gap-4 mb-4 text-[12px] text-gray-500 flex-wrap">
                    {e.deep_link && (
                      <a
                        href={e.deep_link}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center gap-1 text-blue-400 hover:text-blue-300"
                      >
                        Open surface
                        <ArrowUpRight className="w-3 h-3" />
                      </a>
                    )}
                    {e.audit_row && (
                      <span className="font-mono">{e.audit_row}</span>
                    )}
                  </div>
                )}

                <div className="flex items-center justify-end pt-2 border-t border-white/5">
                  <button
                    onClick={() => acknowledge(e)}
                    disabled={isBusy}
                    className="flex items-center gap-2 px-4 py-2 rounded bg-green-500/15 text-green-300 border border-green-500/40 hover:bg-green-500/25 disabled:opacity-50 text-[13px]"
                  >
                    <CheckCircle2 className="w-4 h-4" />
                    {isBusy ? "Acknowledging…" : "Acknowledge"}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      </main>
    </div>
  );
}

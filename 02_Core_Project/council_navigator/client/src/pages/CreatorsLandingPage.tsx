/**
 * CreatorsLandingPage — post-promotion landing surface for role='creator'.
 *
 * Per ACCOUNT_SYSTEM_SPEC chunk 8 + chunk 7's GET /api/creators/me/status
 * endpoint. Shows the user's active agreement summary + download summary
 * (V0 = total + most-recent timestamp only per redline decision 2; full
 * per-creator history dashboard deferred to V1+).
 *
 * V0 has no actual asset-repository surface yet — the landing page is
 * the placeholder for "you're a creator, here's what that means". The
 * static-asset repository download surface is a future chunk.
 */
import { useEffect, useState, type ReactElement } from "react";

import { useCurrentUser } from "../hooks/useCurrentUser";

interface CreatorsLandingPageProps {
  onNavigate: (view: string, params?: any) => void;
}

interface AgreementShape {
  id: number;
  tos_version: string;
  disclaimer_version: string;
  disclaimer_acknowledged_at: string;
  signed_at: string;
  revoked_at: string | null;
  revoked_reason: string | null;
}

interface CreatorStatus {
  user: {
    user_id: number;
    email: string;
    display_name: string | null;
    role: string;
  };
  active_agreement: AgreementShape | null;
  download_summary: {
    total_downloads: number;
    most_recent_at: string | null;
  };
}

function formatTimestamp(s: string | null): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return s;
  }
}

export default function CreatorsLandingPage({ onNavigate }: CreatorsLandingPageProps): ReactElement {
  const { user, loading: userLoading } = useCurrentUser();
  const [status, setStatus] = useState<CreatorStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (userLoading || !user) return;
    let active = true;
    setLoading(true);
    fetch("/api/creators/me/status", {
      credentials: "include",
      cache: "no-store",
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((body) => {
        if (!active) return;
        if (body?.success) {
          setStatus(body);
        } else {
          setError(body?.error || "Could not load creator status");
        }
        setLoading(false);
      })
      .catch((err) => {
        if (!active) return;
        setError(err?.message || "Network error");
        setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [user, userLoading]);

  if (userLoading || loading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <div className="text-foreground/40 text-sm">Loading…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen bg-background px-6 py-16">
        <div className="mx-auto max-w-md text-center space-y-3">
          <h1 className="text-xl font-light text-white">Couldn't load your creator status</h1>
          <p className="text-sm text-foreground/55">{error}</p>
          <button
            type="button"
            onClick={() => onNavigate("home")}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/0 px-4 py-1.5 text-xs text-white/85 hover:border-white/40 transition"
          >
            Back to Channels
          </button>
        </div>
      </div>
    );
  }

  const a = status?.active_agreement || null;
  const d = status?.download_summary || { total_downloads: 0, most_recent_at: null };

  return (
    <div className="min-h-screen bg-background px-6 py-12">
      <div className="mx-auto max-w-2xl">
        <header className="mb-8">
          <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/40 mb-2">
            Creator Network
          </div>
          <h1 className="text-2xl font-light tracking-tight text-white">
            You're a Z-SPAN creator
          </h1>
          <p className="mt-2 text-sm text-foreground/55">
            Welcome, {status?.user.display_name || status?.user.email}.
          </p>
        </header>

        <section className="rounded-xl border border-white/10 bg-white/[0.02] p-6 space-y-4 mb-6">
          <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/45">
            Active agreement
          </div>
          {a ? (
            <dl className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <dt className="text-[10px] uppercase tracking-wider text-foreground/40">
                  TOS version
                </dt>
                <dd className="text-white">{a.tos_version}</dd>
              </div>
              <div>
                <dt className="text-[10px] uppercase tracking-wider text-foreground/40">
                  Disclaimer version
                </dt>
                <dd className="text-white">{a.disclaimer_version}</dd>
              </div>
              <div>
                <dt className="text-[10px] uppercase tracking-wider text-foreground/40">
                  Signed
                </dt>
                <dd className="text-foreground/80">{formatTimestamp(a.signed_at)}</dd>
              </div>
              <div>
                <dt className="text-[10px] uppercase tracking-wider text-foreground/40">
                  Acknowledged disclaimer
                </dt>
                <dd className="text-foreground/80">
                  {formatTimestamp(a.disclaimer_acknowledged_at)}
                </dd>
              </div>
            </dl>
          ) : (
            <p className="text-sm text-foreground/55">No active agreement on record.</p>
          )}
        </section>

        <section className="rounded-xl border border-white/10 bg-white/[0.02] p-6 space-y-4 mb-6">
          <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/45">
            Download activity
          </div>
          <dl className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <dt className="text-[10px] uppercase tracking-wider text-foreground/40">
                Total downloads
              </dt>
              <dd className="text-white">{d.total_downloads}</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase tracking-wider text-foreground/40">
                Most recent
              </dt>
              <dd className="text-foreground/80">{formatTimestamp(d.most_recent_at)}</dd>
            </div>
          </dl>
          <p className="text-[11px] text-foreground/40 leading-relaxed">
            The static-asset repository download surface is on the V1+ roadmap.
            Today this page confirms your agreement is on file + serves as the
            landing point for future creator features.
          </p>
        </section>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => onNavigate("home")}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/0 px-4 py-1.5 text-xs font-medium text-white/85 hover:border-white/40 hover:bg-white/5 transition"
          >
            Back to Channels
          </button>
          <button
            type="button"
            onClick={() => onNavigate("following")}
            className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/0 px-4 py-1.5 text-xs font-medium text-white/70 hover:text-white/95 hover:border-white/30 transition"
          >
            Following
          </button>
        </div>
      </div>
    </div>
  );
}

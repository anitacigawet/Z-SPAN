import { useEffect, useState, type ReactElement } from "react";
import { Cloud, Database, ExternalLink, KeyRound, Laptop, RefreshCw, Trash2 } from "lucide-react";

import { ByokSetupModal } from "../components/ByokSetupModal";
import { useCurrentUser } from "../hooks/useCurrentUser";
import { getByokMetadata } from "../lib/byok";
import {
  clearBrowserWorkspaceEntries,
  listBrowserWorkspaceEntries,
  type BrowserWorkspaceEntry,
} from "../lib/browserWorkspace";

interface WorkspaceReceipt {
  kind: "analysis" | "generation" | "contribution";
  public_id: string;
  meeting_public_id: string;
  status: string;
  created_at: string;
}

function receiptStatus(status: string): string {
  switch (status) {
    case "registered": return "Saved to your account";
    case "superseded": return "Replaced by a newer generation";
    case "received_unverified": return "Received for private review";
    case "accepted": return "Accepted after review";
    case "rejected": return "Needs correction";
    case "retrieval_recorded": return "Meeting evidence recorded";
    case "retrieval_failed": return "Meeting evidence request did not finish";
    default: return "Status unavailable";
  }
}

export default function WorkspacePage({
  onNavigate,
}: {
  onNavigate: (view: string, params?: unknown) => void;
}): ReactElement {
  const { user } = useCurrentUser();
  const [setupOpen, setSetupOpen] = useState(false);
  const [localEntries, setLocalEntries] = useState<BrowserWorkspaceEntry[]>([]);
  const [receipts, setReceipts] = useState<WorkspaceReceipt[]>([]);
  const [localError, setLocalError] = useState<string | null>(null);
  const [receiptError, setReceiptError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const metadata = getByokMetadata();

  useEffect(() => {
    if (!user) return;
    let active = true;
    setLoading(true);
    setLocalError(null);
    setReceiptError(null);
    Promise.allSettled([
      listBrowserWorkspaceEntries(user.user_id),
      fetch("/api/workspace/receipts", {
        credentials: "include",
        cache: "no-store",
      })
        .then(async response => {
          if (!response.ok) throw new Error(`Receipt request failed (${response.status}).`);
          const body = await response.json();
          if (!Array.isArray(body.receipts)) throw new Error("Receipt response was incomplete.");
          return body.receipts as WorkspaceReceipt[];
        }),
    ]).then(([localResult, serverResult]) => {
      if (!active) return;
      if (localResult.status === "fulfilled") setLocalEntries(localResult.value);
      else {
        setLocalEntries([]);
        setLocalError("This browser could not open its device-local workspace.");
      }
      if (serverResult.status === "fulfilled") setReceipts(serverResult.value);
      else {
        setReceipts([]);
        setReceiptError("Z-SPAN could not load your account receipts.");
      }
      setLoading(false);
    });
    return () => { active = false; };
  }, [user, refreshKey]);

  if (!user) return <div />;

  return (
    <main className="mx-auto max-w-6xl px-5 py-10 sm:px-8 lg:px-12">
      <header className="mb-10 max-w-3xl">
        <p className="kg-eyebrow mb-3">Your workspace</p>
        <h1 className="text-4xl font-light tracking-tight text-white sm:text-5xl">
          Work with the library your way.
        </h1>
        <p className="mt-4 text-base leading-relaxed text-foreground/60">
          Use your own AI key in the browser, or run the complete Z-SPAN pipeline on your computer. Your key stays only in browser memory and is never stored by Z-SPAN. Your working files stay on your device; Z-SPAN keeps only the account-linked receipts and private contribution intake needed to confirm what arrived.
        </p>
      </header>

      <div className="grid gap-5 lg:grid-cols-2">
        <section className="kg-card p-6 sm:p-7" aria-labelledby="browser-workspace-title">
          <div className="flex items-start justify-between gap-4">
            <div>
              <KeyRound className="mb-4 h-6 w-6 text-[var(--active)]" aria-hidden="true" />
              <h2 id="browser-workspace-title" className="text-2xl font-light text-white">Browser workspace</h2>
              <p className="mt-2 text-sm leading-relaxed text-foreground/55">
                Ask cited questions about indexed meetings with your provider key. Answers are saved in this browser, not on Z-SPAN's servers.
              </p>
            </div>
          </div>
          <div className="mt-6 rounded-lg border border-[var(--line)] bg-black/15 p-4 text-sm">
            <p className="text-white">{metadata ? `Last used: ${metadata.provider}` : "No provider selected yet"}</p>
            <p className="mt-1 text-xs text-foreground/45">
              API keys are memory-only and must be entered again after a reload.
            </p>
          </div>
          <div className="mt-5 flex flex-wrap gap-3">
            <button type="button" onClick={() => setSetupOpen(true)} className="min-h-11 rounded-full bg-white px-5 text-sm font-semibold text-black transition hover:bg-white/85">
              {metadata ? "Enter key" : "Choose an AI provider"}
            </button>
            <button type="button" onClick={() => onNavigate("home")} className="min-h-11 rounded-full border border-[var(--line)] px-5 text-sm text-white transition hover:border-[var(--line-strong)]">
              Find a meeting
            </button>
          </div>
        </section>

        <section className="kg-card p-6 sm:p-7" aria-labelledby="cli-workspace-title">
          <Laptop className="mb-4 h-6 w-6 text-amber-300" aria-hidden="true" />
          <h2 id="cli-workspace-title" className="text-2xl font-light text-white">Complete local workspace</h2>
          <p className="mt-2 text-sm leading-relaxed text-foreground/55">
            The CLI downloads, transcribes, checks, and organizes meetings locally in <span className="font-mono text-foreground/75">~/.zspan/workspace.db</span>. It uses your key and can run local Whisper without charging Z-SPAN.
          </p>
          <div className="mt-5 overflow-x-auto rounded-lg border border-[var(--line)] bg-black/25 p-4 font-mono text-xs leading-7 text-foreground/75">
            <div>pip install https://github.com/anitacigawet/Z-SPAN/releases/download/zspan-cli-v0/zspan_cli-0.1.0-py3-none-any.whl</div>
            <div>zspan init</div>
            <div>zspan pick</div>
            <div>zspan process</div>
            <div>zspan open</div>
          </div>
          <a href="https://github.com/anitacigawet/Z-SPAN/blob/main/QUICKSTART_WINDOWS.md" target="_blank" rel="noreferrer" className="mt-5 inline-flex min-h-11 items-center gap-2 rounded-full border border-[var(--line)] px-5 text-sm text-white transition hover:border-[var(--line-strong)]">
            Open the setup guide <ExternalLink className="h-4 w-4" aria-hidden="true" />
          </a>
        </section>
      </div>

      <section className="mt-8 grid gap-5 lg:grid-cols-[1.25fr_.75fr]">
        <div className="kg-card p-6 sm:p-7">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="kg-eyebrow">Saved on this device</p>
              <h2 className="mt-2 text-xl font-light text-white">Browser analyses</h2>
            </div>
            {localEntries.length > 0 && (
              <button type="button" onClick={() => void clearBrowserWorkspaceEntries(user.user_id).then(() => setRefreshKey(key => key + 1)).catch(() => setLocalError("This browser could not clear its device-local workspace."))} className="inline-flex min-h-11 items-center gap-2 rounded-full border border-[var(--line)] px-4 text-xs text-foreground/60 hover:text-white">
                <Trash2 className="h-4 w-4" aria-hidden="true" /> Clear this device
              </button>
            )}
          </div>
          {localError && <p role="alert" className="mt-5 text-sm text-red-300/80">{localError}</p>}
          {loading ? <p className="mt-5 text-sm text-foreground/45">Loading your workspace…</p> : !localError && localEntries.length ? (
            <div className="mt-5 space-y-3">
              {localEntries.slice(0, 20).map(entry => (
                <article key={entry.id} className="rounded-lg border border-[var(--line)] p-4">
                  <p className="text-sm font-medium text-white">{entry.query}</p>
                  <p className="mt-2 line-clamp-3 text-sm leading-relaxed text-foreground/55">{entry.answer}</p>
                  <p className="mt-3 text-xs text-foreground/35">Meeting {entry.meetingId} · {new Date(entry.createdAt).toLocaleString()} · {entry.provider}</p>
                </article>
              ))}
            </div>
          ) : !localError ? <p className="mt-5 text-sm leading-relaxed text-foreground/45">Answers you create with your key will appear here after you use the Librarian on a meeting page.</p> : null}
        </div>

        <aside className="kg-card p-6 sm:p-7">
          <Database className="mb-4 h-5 w-5 text-foreground/55" aria-hidden="true" />
          <h2 className="text-xl font-light text-white">Received by Z-SPAN</h2>
          <p className="mt-2 text-sm leading-relaxed text-foreground/50">Small account-linked receipts confirm which browser requests, CLI generations, and private contributions reached Z-SPAN. Your answers and working files stay on your device.</p>
          <button type="button" onClick={() => setRefreshKey(key => key + 1)} className="mt-4 inline-flex min-h-11 items-center gap-2 text-xs text-foreground/55 hover:text-white">
            <RefreshCw className="h-4 w-4" aria-hidden="true" /> Refresh receipts
          </button>
          {receiptError && <p role="alert" className="mt-3 text-sm text-red-300/80">{receiptError}</p>}
          <div className="mt-3 space-y-3">
            {receipts.slice(0, 12).map(receipt => (
              <div key={`${receipt.kind}-${receipt.public_id}`} className="rounded-lg border border-[var(--line)] p-3">
                <div className="flex items-center gap-2 text-xs text-white"><Cloud className="h-3.5 w-3.5" aria-hidden="true" />{receipt.kind === "contribution" ? "Private contribution received" : receipt.kind === "analysis" ? "Browser request received" : "Generation registered"}</div>
                <p className="mt-1 text-[11px] text-foreground/40">{receiptStatus(receipt.status)} · {new Date(receipt.created_at).toLocaleString()}</p>
              </div>
            ))}
            {!loading && !receiptError && receipts.length === 0 && <p className="text-sm text-foreground/40">Nothing has reached Z-SPAN from this account yet.</p>}
          </div>
        </aside>
      </section>

      <ByokSetupModal open={setupOpen} onClose={() => setSetupOpen(false)} onConfigured={() => setRefreshKey(key => key + 1)} />
    </main>
  );
}

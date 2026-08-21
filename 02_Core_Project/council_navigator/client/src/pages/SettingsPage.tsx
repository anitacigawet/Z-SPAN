import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import { Trash2, Save } from "lucide-react";

import { FollowingList } from "../components/FollowingList";
import { ByokSetupModal } from "../components/ByokSetupModal";
import { useCurrentUser, type CurrentUser } from "../hooks/useCurrentUser";
import { useHidePlaceholders } from "../hooks/useHidePlaceholders";
import { getByokMetadata } from "../lib/byok";

interface SettingsPageProps {
  onNavigate: (view: string, params?: any) => void;
}

interface CitizenSettingsProps extends SettingsPageProps {
  user: CurrentUser;
}

function buildSignInHref(): string {
  if (typeof window === "undefined") return "/login?next=%2F";
  const next = `${window.location.pathname}${window.location.search}` || "/";
  return `/login?next=${encodeURIComponent(next)}`;
}

function AiProviderSettings({ onNavigate }: SettingsPageProps): ReactElement {
  const [byokOpen, setByokOpen] = useState(false);
  const [providerRevision, setProviderRevision] = useState(0);
  const byokMetadata = getByokMetadata();

  return (
    <section className="kg-card p-5 space-y-4" aria-labelledby="ai-provider-heading">
      <div>
        <p className="kg-eyebrow" id="ai-provider-heading">Your AI provider</p>
        <p className="mt-2 text-sm leading-relaxed text-foreground/55">
          Bring your own Google, OpenAI, or Anthropic key for meeting analysis. The key stays only in browser memory and is never stored by Z-SPAN.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={() => setByokOpen(true)} className="min-h-11 rounded-full bg-white px-5 text-sm font-semibold text-black transition hover:bg-white/85">
          {byokMetadata ? "Enter key again" : "Choose a provider"}
        </button>
        <button type="button" onClick={() => onNavigate("workspace")} className="min-h-11 rounded-full border border-[var(--line)] px-5 text-sm text-white transition hover:border-[var(--line-strong)]">
          Open workspace
        </button>
        <span className="text-xs text-foreground/40">
          {byokMetadata ? `Last used: ${byokMetadata.provider}` : "No provider selected"}
        </span>
      </div>
      <ByokSetupModal
        key={providerRevision}
        open={byokOpen}
        onClose={() => setByokOpen(false)}
        onConfigured={() => setProviderRevision(value => value + 1)}
      />
    </section>
  );
}

async function signOut(event: FormEvent<HTMLFormElement>): Promise<void> {
  event.preventDefault();
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    // Reload even if the request failed so the account state is rechecked.
  } finally {
    if (typeof window !== "undefined") {
      window.location.reload();
    }
  }
}

export function CitizenSettings({
  user,
  onNavigate,
}: CitizenSettingsProps): ReactElement {
  const [hidePlaceholders, setHidePlaceholders] = useHidePlaceholders();

  return (
    <div className="space-y-8">
      <section
        className="kg-card p-5 space-y-3"
        aria-labelledby="preferences-heading"
      >
        <p className="kg-eyebrow" id="preferences-heading">
          Preferences
        </p>
        <label className="flex items-start gap-3 rounded-md border border-[var(--line)] p-3 cursor-pointer transition-colors hover:border-[var(--line-strong)]">
          <input
            type="checkbox"
            checked={hidePlaceholders}
            onChange={event => setHidePlaceholders(event.target.checked)}
            className="mt-0.5"
          />
          <span className="min-w-0">
            <span className="block text-[13px] font-semibold text-white tracking-tight">
              Hide &quot;Episode coming&quot; cards
            </span>
            <span className="block text-[11px] text-muted-foreground mt-1 leading-relaxed">
              Show only meetings with a published episode. Uncheck this if you
              use the CLI and want to see catalog placeholders.
            </span>
          </span>
        </label>
      </section>

      <section className="space-y-4" aria-labelledby="following-heading">
        <div>
          <p className="kg-eyebrow" id="following-heading">
            Following
          </p>
        </div>
        <FollowingList onNavigate={onNavigate} />
      </section>

      <AiProviderSettings onNavigate={onNavigate} />

      <section
        className="kg-card p-5 space-y-4"
        aria-labelledby="account-heading"
      >
        <p className="kg-eyebrow" id="account-heading">
          Account
        </p>
        <dl className="grid gap-3 text-sm sm:grid-cols-[8rem_1fr]">
          <dt className="text-foreground/45">Display name</dt>
          <dd className="text-white break-words">
            {user.display_name || "Not set"}
          </dd>
          <dt className="text-foreground/45">Email</dt>
          <dd className="text-white break-all">{user.email}</dd>
        </dl>
        <form
          action="/api/auth/logout"
          method="post"
          onSubmit={event => void signOut(event)}
        >
          <button
            type="submit"
            className="inline-flex rounded-md border border-[var(--line)] px-3 py-1.5 text-xs font-medium text-foreground/65 transition-colors hover:border-[var(--line-strong)] hover:text-white"
          >
            Sign out
          </button>
        </form>
      </section>
    </div>
  );
}

function OwnerSettings(): ReactElement {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // The OpenAI key is NOT part of the retired Navigator provider toggle — three
  // live consumers resolve it independently: the whisper-1 transcription
  // fallback, the gpt-4o-mini quote cleaner (T-011), and ingest_validator.
  // Blank input = leave the stored key untouched; the server never returns it.
  const [openaiKey, setOpenaiKey] = useState("");
  const [openaiKeyConfigured, setOpenaiKeyConfigured] = useState(false);
  const [openaiKeyHint, setOpenaiKeyHint] = useState("");

  // Z-SPAN broadcast-page chat mode (D-021 lineage; NotebookLM retired per D-143).
  //   "direct"    — show-page chat runs live retrieval + synthesis over the
  //                 meeting's indexed transcript.
  //   "suggested" — only pre-cached Q&A chips are exposed; safe for public hosts.
  const [chatMode, setChatMode] = useState<"direct" | "suggested">("direct");

  const [message, setMessage] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  const loadStatus = () => {
    setLoading(true);
    fetch("/api/settings")
      .then(res => res.json())
      .then(data => {
        setOpenaiKeyConfigured(Boolean(data.openai_key_configured));
        setOpenaiKeyHint(data.openai_key_hint || "");
        setOpenaiKey("");
        if (data.chat_mode === "suggested" || data.chat_mode === "direct") {
          setChatMode(data.chat_mode);
        }
        setLoading(false);
      })
      .catch(() => {
        setMessage({
          type: "error",
          text: "Could not reach the settings API. Is the Flask server running?",
        });
        setLoading(false);
      });
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chat_mode: chatMode,
          // Omitted when blank so saving other settings never clears the key.
          ...(openaiKey.trim() ? { openai_api_key: openaiKey.trim() } : {}),
        }),
      });
      const data = await res.json();
      if (data.success) {
        setMessage({ type: "success", text: "Settings saved." });
        loadStatus();
      } else {
        setMessage({ type: "error", text: data.error || "Save failed." });
      }
    } catch (e: any) {
      setMessage({ type: "error", text: e.message || "Save failed." });
    } finally {
      setSaving(false);
    }
  };

  const handleClear = async () => {
    if (!confirm("Remove all saved settings?")) return;
    setSaving(true);
    try {
      const res = await fetch("/api/settings", { method: "DELETE" });
      const data = await res.json();
      if (data.success) {
        setMessage({ type: "success", text: "All settings cleared." });
        loadStatus();
      } else {
        setMessage({ type: "error", text: data.error || "Could not clear." });
      }
    } catch (e: any) {
      setMessage({ type: "error", text: e.message || "Could not clear." });
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="pt-4" aria-labelledby="operator-settings-heading">
      <div className="mb-8">
        <p className="kg-eyebrow mb-3">Operator</p>
        <h3
          className="text-2xl font-light tracking-wide text-white mb-2"
          id="operator-settings-heading"
        >
          Operator settings
        </h3>
      </div>

      {loading ? (
        <div className="flex flex-col items-center py-20 gap-3">
          <span className="kg-dots">
            <span /> <span /> <span />
          </span>
          <p className="kg-eyebrow">Loading settings…</p>
        </div>
      ) : (
        <div className="space-y-5 pb-20">
          {/* OpenAI key — transcription fallback + quote cleaning */}
          <div className="kg-card p-5 space-y-3">
            <div>
              <p className="kg-eyebrow mb-1">OpenAI Key</p>
              <p className="text-[12px] text-muted-foreground leading-relaxed">
                Used for two things: transcribing a meeting when the local
                Whisper node isn't available, and tidying up quote punctuation.
                Leave blank to keep the key already on file.
              </p>
            </div>
            <input
              type="password"
              value={openaiKey}
              onChange={e => setOpenaiKey(e.target.value)}
              placeholder={
                openaiKeyConfigured
                  ? `A key is on file (${openaiKeyHint}) — type a new one to replace it`
                  : "No key on file"
              }
              autoComplete="off"
              spellCheck={false}
              className="w-full bg-transparent border border-[var(--line)] rounded-md px-3 py-2 text-sm text-white placeholder:text-muted-foreground/70 focus:border-[var(--line-strong)] outline-none transition-colors"
            />
          </div>

          {/* Z-SPAN broadcast chat mode */}
          <div className="kg-card p-5 space-y-3">
            <div>
              <p className="kg-eyebrow mb-1">Broadcast Chat Mode</p>
              <p className="text-[12px] text-muted-foreground leading-relaxed">
                Controls how the AI panel on the broadcast detail page behaves.
              </p>
            </div>
            <div className="space-y-2">
              <label className="flex items-start gap-3 p-3 rounded-md border border-[var(--line)] hover:border-[var(--line-strong)] cursor-pointer transition-colors">
                <input
                  type="radio"
                  name="chat-mode"
                  value="direct"
                  checked={chatMode === "direct"}
                  onChange={() => setChatMode("direct")}
                  className="mt-1"
                />
                <div className="min-w-0">
                  <p className="text-[13px] font-semibold text-white tracking-tight">
                    Direct (dev / local)
                  </p>
                  <p className="text-[11px] text-muted-foreground mt-1 leading-relaxed">
                    Typing into the broadcast chat runs a live retrieval +
                    synthesis pass over this meeting&apos;s indexed transcript.
                    Use this when only you (or trusted operators) can hit the
                    site.
                  </p>
                </div>
              </label>
              <label className="flex items-start gap-3 p-3 rounded-md border border-[var(--line)] hover:border-[var(--line-strong)] cursor-pointer transition-colors">
                <input
                  type="radio"
                  name="chat-mode"
                  value="suggested"
                  checked={chatMode === "suggested"}
                  onChange={() => setChatMode("suggested")}
                  className="mt-1"
                />
                <div className="min-w-0">
                  <p className="text-[13px] font-semibold text-white tracking-tight">
                    Suggested questions only (public host)
                  </p>
                  <p className="text-[11px] text-muted-foreground mt-1 leading-relaxed">
                    Visitors only see pre-cached Q&A chips generated during
                    processing. Clicks replay cached answers — no live API
                    calls, no abuse surface. Safe to expose publicly.
                  </p>
                </div>
              </label>
            </div>
          </div>

          {/* Actions */}
          <div className="flex items-center gap-3 pt-2">
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex-1 px-6 py-3 bg-white hover:bg-gray-200 text-black rounded-md text-[14px] font-semibold transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
            >
              {saving ? (
                <span className="kg-dots scale-75">
                  <span /> <span /> <span />
                </span>
              ) : (
                <Save className="w-4 h-4" />
              )}
              Save all settings
            </button>
            <button
              onClick={handleClear}
              disabled={saving}
              className="px-5 py-3 bg-[var(--surface-2)] hover:bg-[var(--surface-3)] border border-[var(--line)] text-muted-foreground hover:text-destructive rounded-md text-[13px] font-medium transition-colors flex items-center gap-2"
            >
              <Trash2 className="w-3.5 h-3.5" /> Clear
            </button>
          </div>

          {message && (
            <div
              className={`text-sm px-4 py-3 rounded-md border ${
                message.type === "success"
                  ? "bg-[var(--surface-2)] border-[var(--active)]/40 text-[var(--active)]"
                  : "bg-[var(--surface-2)] border-destructive/40 text-destructive"
              }`}
            >
              {message.text}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export default function SettingsPage({
  onNavigate,
}: SettingsPageProps): ReactElement {
  const { user, loading } = useCurrentUser();

  if (loading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <div className="text-foreground/40 text-sm">Loading…</div>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="min-h-screen bg-background px-6 py-16">
        <div className="mx-auto max-w-md text-center space-y-5">
          <h1 className="text-2xl font-light tracking-tight text-white">
            Log in to manage your settings
          </h1>
          <p className="text-sm text-foreground/55 leading-relaxed">
            Personalize your civic feed and manage the people and places you
            follow.
          </p>
          <a
            href={buildSignInHref()}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-4 py-2 text-sm font-medium text-white hover:border-white/40 hover:bg-white/10 transition"
          >
            Log in
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-3xl mx-auto px-6 lg:px-10 py-10 kg-fade-in">
      <header className="mb-8">
        <p className="kg-eyebrow">Your account</p>
      </header>

      <div className="space-y-10 pb-20">
        <CitizenSettings user={user} onNavigate={onNavigate} />
        {user.is_owner && <OwnerSettings />}
      </div>
    </div>
  );
}

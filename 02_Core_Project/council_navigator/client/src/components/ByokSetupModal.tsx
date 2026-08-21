/**
 * V1.5-BYOK-Shell-1 — BYOK onboarding modal.
 *
 * Triggered by clicking the V2/BYOK-locked "Ask anything" input on
 * BroadcastPage (per the modal-from-lock UX agreed 2026-06-24 — modal
 * lives where the user already wants it, no separate /?view=byok-setup
 * route + no new TopBar nav surface).
 *
 * Scope: Gemini-2.5-Flash live; OpenAI / Anthropic / audio / infographic
 * shown as "Coming soon" placeholders per BYOK_ARCHITECTURE_SPEC § 2.4 +
 * the per-feature chunks (V1.5-Relay-1 / V1.5-Audio-Summary-1 /
 * V1.5-Infographic-1).
 *
 * Storage: the key is held only in lib/byok.ts's in-memory cache. Safe
 * provider metadata survives in localStorage so a reload can preselect the
 * last provider while asking the user to re-enter the credential.
 *
 * Key custody narrative shown in the UI:
 *   - For Gemini-direct: the key goes straight from your browser to
 *     google's servers; Z-SPAN never sees it in steady state
 *   - The one-shot validation ping holds the key in volatile request
 *     memory for ~100ms only; never persisted server-side
 */

import React, { useEffect, useState } from "react";
import { Lock, KeyRound, Check, AlertTriangle, ExternalLink, Loader2, Settings, ChevronDown, ChevronRight } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  ByokConfig,
  ByokMetadata,
  ByokSettings,
  validateByokKey,
  saveByokConfig,
  getByokConfig,
  getByokMetadata,
  clearByokConfig,
  getByokSettings,
  saveByokSettings,
  DEFAULT_BYOK_SETTINGS,
} from "@/lib/byok";
import { useCurrentUser } from "@/hooks/useCurrentUser";

interface ByokSetupModalProps {
  open: boolean;
  onClose: () => void;
  /** Fires after a successful save so the parent can re-read getByokConfig()
   *  and swap its locked UI for the unlocked path. */
  onConfigured?: (config: ByokConfig) => void;
}

const PROVIDER_OPTIONS = [
  {
    id: "google-gemini-2.5-flash",
    label: "Google Gemini 2.5 Flash",
    subtitle: "Free tier ~1,500 req/day · ~$0.0006/query · key-blind by construction",
    available: true,
    badge: null,
  },
  {
    id: "openai-gpt-4o-mini",
    label: "OpenAI GPT-4o-mini",
    subtitle: "Paid · ~$0.0011/query · via /api/byok/relay (bytes-blind in transit)",
    available: true,
    badge: null,
  },
  {
    id: "openai-gpt-4o",
    label: "OpenAI GPT-4o (full)",
    subtitle: "Paid · ~$0.019/query · premium tier, more nuanced citations",
    available: true,
    badge: null,
  },
  {
    id: "anthropic-claude-3-haiku",
    label: "Anthropic Claude 3 Haiku",
    subtitle: "Paid · ~$0.002/query · via /api/byok/relay",
    available: true,
    badge: null,
  },
  {
    id: "anthropic-claude-3-5-sonnet",
    label: "Anthropic Claude 3.5 Sonnet",
    subtitle: "Paid · ~$0.05/query · highest-quality reasoning",
    available: true,
    badge: null,
  },
  {
    id: "audio_summary",
    label: "Audio summary generation",
    subtitle: "Custom audio pipeline (Z-SPAN-curated templates + your provider)",
    available: false,
    badge: "V1.5-Audio-Summary-1",
  },
  {
    id: "infographic",
    label: "Interactive infographic generation",
    subtitle: "HTML/CSS/JS via Z-SPAN-curated library + your provider; not opaque image-gen",
    available: false,
    badge: "V1.5-Infographic-1",
  },
];

type LibrarianAccess = "none" | "requested" | "granted" | "banned";

export function LibrarianAccessGate() {
  const currentUser = useCurrentUser();
  const [accessState, setAccessState] = useState<LibrarianAccess>(
    currentUser.user?.librarian_access ?? "none",
  );
  const [requestingAccess, setRequestingAccess] = useState(false);
  const [requestAccessError, setRequestAccessError] = useState("");

  useEffect(() => {
    setAccessState(currentUser.user?.librarian_access ?? "none");
  }, [currentUser.user?.librarian_access]);

  const handleRequestAccess = async () => {
    setRequestingAccess(true);
    setRequestAccessError("");
    try {
      const response = await fetch("/api/librarian/request-access", {
        method: "POST",
        credentials: "include",
      });
      const body = (await response.json().catch(() => ({}))) as {
        status?: LibrarianAccess;
        message?: string;
      };
      if (body.status === "banned") {
        setAccessState("banned");
        return;
      }
      if (!response.ok || !body.status) {
        throw new Error(body.message || "The access request couldn't be sent.");
      }
      setAccessState(body.status);
      currentUser.refresh();
    } catch (error) {
      setRequestAccessError(
        error instanceof Error
          ? error.message
          : "The access request couldn't be sent.",
      );
    } finally {
      setRequestingAccess(false);
    }
  };

  if (currentUser.loading) return null;

  if (!currentUser.user) {
    if (!currentUser.signInEnabled) return null;

    const next =
      typeof window === "undefined"
        ? "/"
        : `${window.location.pathname}${window.location.search}`;
    const loginHref = `/login?next=${encodeURIComponent(
      next || "/",
    )}`;
    // Keep the signed-out action in the same compact input-shaped surface as
    // the Librarian controls, while describing the member action plainly.
    return (
      <div className="p-4 border-t border-white/5 flex-shrink-0">
        <a
          href={loginHref}
          className="relative flex-1 h-12 rounded-full pl-5 pr-5 text-[14px] flex items-center bg-[#0E0E10] border border-[#1A1A1D] text-gray-500 hover:border-[var(--line-strong)] hover:text-white transition-colors"
        >
          <span>
            Log in to use your own AI provider
          </span>
        </a>
      </div>
    );
  }

  if (accessState === "requested") {
    return (
      <div className="p-4 border-t border-white/5 flex-shrink-0">
        <div className="relative flex-1 h-12 rounded-full pl-5 pr-5 text-[14px] flex items-center bg-[#0E0E10] border border-[#1A1A1D] text-gray-500 select-none">
          <span>Access requested — waiting for approval</span>
        </div>
      </div>
    );
  }

  if (accessState === "banned") {
    return (
      <div className="p-4 border-t border-white/5 flex-shrink-0">
        <div className="relative flex-1 h-12 rounded-full pl-5 pr-5 text-[14px] flex items-center bg-[#0E0E10] border border-[#1A1A1D] text-gray-500 select-none">
          <span>Librarian access unavailable for this account</span>
        </div>
      </div>
    );
  }

  return (
    <div className="p-4 border-t border-white/5 flex-shrink-0">
      <button
        type="button"
        onClick={() => void handleRequestAccess()}
        disabled={requestingAccess}
        className="relative w-full h-12 rounded-full pl-5 pr-5 text-[14px] text-left flex items-center bg-[#0E0E10] border border-[#1A1A1D] text-gray-500 transition-colors hover:border-[var(--line-strong)] disabled:opacity-40 disabled:cursor-not-allowed"
      >
        <span>
          {requestingAccess ? "Requesting…" : "Request Librarian access"}
        </span>
      </button>
      {requestAccessError && (
        <p className="text-[12px] text-red-300/90 mt-2 px-1">
          {requestAccessError}
        </p>
      )}
    </div>
  );
}

export function ByokSetupModal({ open, onClose, onConfigured }: ByokSetupModalProps) {
  const [existing, setExisting] = useState<ByokMetadata | null>(() =>
    open ? getByokMetadata() : null,
  );
  const [providerId, setProviderId] = useState<string>(
    existing?.provider || PROVIDER_OPTIONS[0].id,
  );
  const [apiKey, setApiKey] = useState<string>("");
  const [status, setStatus] = useState<
    "idle" | "validating" | "valid" | "invalid"
  >("idle");
  const [errorMsg, setErrorMsg] = useState<string>("");
  const [fingerprint, setFingerprint] = useState<string>("");
  const [modelCount, setModelCount] = useState<number | undefined>(undefined);
  // V1.5-Query-1: settings — sliders for max_output_tokens + temperature.
  // Persist on every change so the user's preference survives a modal close.
  const [settings, setSettings] = useState<ByokSettings>(() => getByokSettings());
  const [advancedOpen, setAdvancedOpen] = useState<boolean>(false);
  const keyIsActive = open && getByokConfig() !== null;

  // Refresh once at each closed → open transition. This preselects the last
  // validated provider after a reload without fighting changes the user makes
  // while the modal is already open.
  useEffect(() => {
    if (!open) return;
    const metadata = getByokMetadata();
    setExisting(metadata);
    if (metadata) setProviderId(metadata.provider);
  }, [open]);

  const updateSettings = (patch: Partial<ByokSettings>) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    saveByokSettings(next);
  };

  const handleValidate = async () => {
    if (!apiKey || apiKey.length < 12) {
      setErrorMsg("API key looks too short. Paste the full key.");
      setStatus("invalid");
      return;
    }
    setStatus("validating");
    setErrorMsg("");
    const result = await validateByokKey(providerId, apiKey);

    if (result.valid) {
      const config: ByokConfig = {
        provider: providerId,
        key: apiKey,
        fingerprint: result.fingerprint || "",
        validatedAt: new Date().toISOString(),
        modelCount: result.model_count,
      };
      saveByokConfig(config);
      setExisting({
        provider: config.provider,
        fingerprint: config.fingerprint,
        validatedAt: config.validatedAt,
        ...(typeof config.modelCount === "number"
          ? { modelCount: config.modelCount }
          : {}),
      });
      setFingerprint(config.fingerprint);
      setModelCount(config.modelCount);
      setStatus("valid");
      if (onConfigured) onConfigured(config);
      // Auto-close after 1.5s so the user sees the success state
      setTimeout(() => {
        onClose();
        setStatus("idle");
        setApiKey("");
      }, 1500);
    } else {
      setStatus("invalid");
      setErrorMsg(result.error || "Validation failed for unknown reason.");
    }
  };

  const handleClear = () => {
    clearByokConfig();
    setExisting(null);
    setApiKey("");
    setStatus("idle");
    setFingerprint("");
    setErrorMsg("");
    if (onConfigured) onConfigured({} as ByokConfig); // signal parent to re-check
  };

  const selectedOption =
    PROVIDER_OPTIONS.find((o) => o.id === providerId) || PROVIDER_OPTIONS[0];

  // Per-provider key-input hints — placeholder shape + helper-link target +
  // privacy note all swap with the selected provider so the user isn't asked
  // to paste an OpenAI key into an `AIza...` field (the V1.5-Query-1 polish
  // gap that surfaced 2026-06-24 during the operator-side smoke test).
  const PROVIDER_KEY_HINTS: Record<string, {
    placeholder: string;
    helperPrefix: string;
    helperUrl: string;
    helperLabel: string;
    privacyNote: string;
  }> = {
    "google-gemini-2.5-flash": {
      placeholder: "AIza... (Google AI Studio key)",
      helperPrefix: "Get a free key at",
      helperUrl: "https://aistudio.google.com/app/apikey",
      helperLabel: "aistudio.google.com/app/apikey",
      privacyNote: "Z-SPAN never stores your key. Your queries go straight from your browser to Google.",
    },
    "openai-gpt-4o-mini": {
      placeholder: "sk-... (OpenAI API key)",
      helperPrefix: "Get a key at",
      helperUrl: "https://platform.openai.com/api-keys",
      helperLabel: "platform.openai.com/api-keys",
      privacyNote: "Z-SPAN never stores your key. Queries route through /api/byok/relay (bytes-blind in transit) to OpenAI.",
    },
    "openai-gpt-4o": {
      placeholder: "sk-... (OpenAI API key)",
      helperPrefix: "Get a key at",
      helperUrl: "https://platform.openai.com/api-keys",
      helperLabel: "platform.openai.com/api-keys",
      privacyNote: "Z-SPAN never stores your key. Queries route through /api/byok/relay (bytes-blind in transit) to OpenAI.",
    },
    "anthropic-claude-3-haiku": {
      placeholder: "sk-ant-... (Anthropic API key)",
      helperPrefix: "Get a key at",
      helperUrl: "https://console.anthropic.com/settings/keys",
      helperLabel: "console.anthropic.com/settings/keys",
      privacyNote: "Z-SPAN never stores your key. Queries route through /api/byok/relay (bytes-blind in transit) to Anthropic.",
    },
    "anthropic-claude-3-5-sonnet": {
      placeholder: "sk-ant-... (Anthropic API key)",
      helperPrefix: "Get a key at",
      helperUrl: "https://console.anthropic.com/settings/keys",
      helperLabel: "console.anthropic.com/settings/keys",
      privacyNote: "Z-SPAN never stores your key. Queries route through /api/byok/relay (bytes-blind in transit) to Anthropic.",
    },
  };
  const providerKeyHint = PROVIDER_KEY_HINTS[providerId] || PROVIDER_KEY_HINTS["google-gemini-2.5-flash"];

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-xl bg-[#0E0E10] border-white/10 text-white">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-white">
            <KeyRound className="w-5 h-5 text-[#22C55E]" />
            Bring your own API key
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-5 pt-2">
          {/* Existing-config notice */}
          {existing && status === "idle" && (
            <div className="text-[13px] bg-white/[0.03] border border-white/10 rounded-md p-3 flex items-start gap-2">
              <Check className="w-4 h-4 text-[#22C55E] mt-0.5 flex-shrink-0" />
              <div className="flex-1">
                <div className="text-white/80">
                  {keyIsActive
                    ? "Your key is active in memory for this page only. Re-enter it after a reload."
                    : "Your key is not stored. Re-enter it to use the Librarian on this page."}
                </div>
                <div className="text-[11px] text-white/40 mt-1">
                  Provider: {existing.provider} · Fingerprint:{" "}
                  <span className="font-mono">{existing.fingerprint}</span>
                </div>
                <button
                  type="button"
                  onClick={handleClear}
                  className="mt-2 text-[11px] text-white/50 hover:text-white/80 underline"
                >
                  {keyIsActive
                    ? "Clear key and provider details"
                    : "Clear saved provider details"}
                </button>
              </div>
            </div>
          )}

          {/* Provider picker */}
          <div>
            <label className="block text-[12px] uppercase tracking-widest text-white/40 mb-2">
              Provider
            </label>
            <div className="space-y-1.5">
              {PROVIDER_OPTIONS.map((opt) => (
                <button
                  type="button"
                  key={opt.id}
                  disabled={!opt.available}
                  onClick={() => opt.available && setProviderId(opt.id)}
                  className={`w-full text-left p-3 rounded-md border transition-colors ${
                    opt.available
                      ? providerId === opt.id
                        ? "border-[#22C55E]/60 bg-[#22C55E]/10"
                        : "border-white/10 hover:border-white/30"
                      : "border-white/5 bg-white/[0.01] opacity-50 cursor-not-allowed"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-[13px] text-white">
                        {opt.label}
                      </div>
                      <div className="text-[11px] text-white/40 mt-0.5">
                        {opt.subtitle}
                      </div>
                    </div>
                    {opt.badge && (
                      <div className="text-[9px] uppercase tracking-widest text-white/30 ml-3 flex items-center gap-1">
                        <Lock className="w-3 h-3" />
                        {opt.badge}
                      </div>
                    )}
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* API key input */}
          {selectedOption.available && (
            <div>
              <label className="block text-[12px] uppercase tracking-widest text-white/40 mb-2">
                API key
              </label>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => {
                  setApiKey(e.target.value);
                  if (status !== "idle") setStatus("idle");
                }}
                placeholder={providerKeyHint.placeholder}
                className="w-full bg-[#0A0A0C] border border-white/10 h-10 rounded-md px-3 text-[13px] text-white placeholder:text-white/30 focus:outline-none focus:border-[#22C55E]/40 font-mono"
                disabled={status === "validating"}
              />
              <div className="text-[11px] text-white/40 mt-2">
                {providerKeyHint.helperPrefix}{" "}
                <a
                  href={providerKeyHint.helperUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[#22C55E] underline inline-flex items-center gap-1"
                >
                  {providerKeyHint.helperLabel}
                  <ExternalLink className="w-3 h-3" />
                </a>{" "}
                · {providerKeyHint.privacyNote}
              </div>
            </div>
          )}

          {/* Status surface */}
          {status === "validating" && (
            <div className="flex items-center gap-2 text-[13px] text-white/60">
              <Loader2 className="w-4 h-4 animate-spin" />
              Pinging provider to validate the key…
            </div>
          )}
          {status === "valid" && (
            <div className="text-[13px] bg-[#22C55E]/10 border border-[#22C55E]/30 rounded-md p-3 flex items-start gap-2">
              <Check className="w-4 h-4 text-[#22C55E] mt-0.5 flex-shrink-0" />
              <div>
                <div className="text-white/90">Key valid ✓</div>
                <div className="text-[11px] text-white/50 mt-1">
                  Fingerprint:{" "}
                  <span className="font-mono">{fingerprint}</span>
                  {typeof modelCount === "number" && ` · ${modelCount} models accessible`}
                </div>
              </div>
            </div>
          )}
          {status === "invalid" && (
            <div className="text-[13px] bg-red-500/10 border border-red-500/30 rounded-md p-3 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 text-red-400 mt-0.5 flex-shrink-0" />
              <div>
                <div className="text-white/90">Validation failed</div>
                <div className="text-[11px] text-white/50 mt-1">
                  {errorMsg}
                </div>
              </div>
            </div>
          )}
          {/* V1.5-Query-1: advanced settings (max_tokens + temperature).
             Visible regardless of validation state so users can tighten
             the per-query cap before they ever paste a key. Persists on
             every slider change. */}
          <div className="border-t border-white/5 pt-3">
            <button
              type="button"
              onClick={() => setAdvancedOpen(!advancedOpen)}
              className="flex items-center gap-1.5 text-[11px] uppercase tracking-widest text-white/40 hover:text-white/70 transition-colors"
            >
              {advancedOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
              <Settings className="w-3 h-3" />
              Query settings
            </button>
            {advancedOpen && (
              <div className="mt-3 space-y-4 pl-1">
                {/* max_output_tokens slider */}
                <div>
                  <div className="flex justify-between items-baseline mb-1">
                    <label className="text-[11px] text-white/60">Max response tokens</label>
                    <span className="text-[11px] text-white/40 font-mono">{settings.max_output_tokens}</span>
                  </div>
                  <input
                    type="range"
                    min={256}
                    max={4096}
                    step={128}
                    value={settings.max_output_tokens}
                    onChange={(e) => updateSettings({ max_output_tokens: parseInt(e.target.value) })}
                    className="w-full accent-[#22C55E]"
                  />
                  <div className="text-[10px] text-white/30 mt-1">
                    Caps each query's response length. Lower = cheaper per query. Worst-case at GPT-4o + 4096: ~$0.04/query.
                  </div>
                </div>
                {/* temperature slider */}
                <div>
                  <div className="flex justify-between items-baseline mb-1">
                    <label className="text-[11px] text-white/60">Temperature</label>
                    <span className="text-[11px] text-white/40 font-mono">{settings.temperature.toFixed(2)}</span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={settings.temperature}
                    onChange={(e) => updateSettings({ temperature: parseFloat(e.target.value) })}
                    className="w-full accent-[#22C55E]"
                  />
                  <div className="text-[10px] text-white/30 mt-1">
                    0.0 = deterministic answers from the chunks. 1.0 = more creative phrasing. Default 0.2 matches Z-SPAN's neutral civic-news register.
                  </div>
                </div>
                {/* top_k slider — V-Op-2-Tune-1c. Retrieval breadth knob. */}
                <div>
                  <div className="flex justify-between items-baseline mb-1">
                    <label className="text-[11px] text-white/60">Top-K (retrieval breadth)</label>
                    <span className="text-[11px] text-white/40 font-mono">{settings.top_k}</span>
                  </div>
                  <input
                    type="range"
                    min={5}
                    max={50}
                    step={1}
                    value={settings.top_k}
                    onChange={(e) => updateSettings({ top_k: parseInt(e.target.value, 10) })}
                    className="w-full accent-[#22C55E]"
                  />
                  <div className="text-[10px] text-white/30 mt-1">
                    How many transcript chunks the retrieval layer feeds the LLM before it answers. Higher K = more context = more comprehensive but more tokens consumed. Default 12 covers most queries; bump to 25+ when a short utterance you know exists isn't surfacing.
                  </div>
                </div>
                {/* Spend-protection note */}
                <div className="text-[10px] text-white/40 bg-white/[0.02] border border-white/5 rounded p-2 leading-relaxed">
                  <span className="text-white/60">Spend protection:</span> Z-SPAN UI rate-limits your queries (min {settings.min_seconds_between_queries}s between sends, {settings.per_minute_query_cap}/min cap, warn-confirm at ${settings.per_session_spend_warn_usd.toFixed(2)} per session) to prevent accidental burns. These limits don't stop a malicious actor who has stolen your key — if your key is compromised, rotate it in your provider's dashboard.
                </div>
              </div>
            )}
          </div>

          {/* Validate button */}
          {selectedOption.available && (
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={onClose}
                className="px-4 py-2 rounded-md text-[13px] text-white/60 hover:text-white/90"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleValidate}
                disabled={status === "validating" || !apiKey}
                className="px-5 py-2 rounded-md text-[13px] bg-[#22C55E]/20 hover:bg-[#22C55E]/40 text-[#22C55E] border border-[#22C55E]/30 disabled:opacity-30 disabled:cursor-not-allowed flex items-center gap-2"
              >
                {status === "validating" && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                Validate + save
              </button>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

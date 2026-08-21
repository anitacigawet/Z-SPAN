/**
 * V1.5-BYOK-Shell-1 — client-side BYOK key store.
 *
 * API keys are session-scoped: the secret lives only in this module's
 * volatile cache. localStorage keeps display-safe provider metadata so the
 * re-entry form can preserve context across reloads without persisting the
 * credential itself.
 *
 * The key NEVER touches Z-SPAN's servers in steady state. Gemini direct-
 * browser-call goes from the user's browser straight to
 * generativelanguage.googleapis.com with the key in the URL parameter or
 * Authorization header. The only Z-SPAN-side touch is the one-shot
 * /api/byok/validate-key ping at onboarding (key held in volatile request
 * memory for ~100ms, never persisted server-side).
 */

const STORAGE_KEY = "zspan_byok_v1";

/** zspan CLI `open` mode (S-131 private workspace). The local server
 *  holds the user's stored key (`zspan init` → ~/.zspan/config.json) and
 *  synthesizes loopback-direct to the provider — the key never travels
 *  to this page, so the ByokConfig carries this sentinel provider id
 *  with an empty key. The flagship never issues this id, so the local
 *  path below can never fire on zspan.org. */
export const LOCAL_WORKSPACE_PROVIDER = "local-workspace";

export interface ByokConfig {
  provider: string; // matches BYOK_ARCHITECTURE_SPEC provider id, e.g. "google-gemini-2.5-flash"
  key: string;
  fingerprint: string; // first4...last4 from validate-key response; safe to display
  validatedAt: string; // ISO-8601 timestamp of the successful validation
  modelCount?: number; // models the key has access to (from validate-key response)
}

export type ByokMetadata = Omit<ByokConfig, "key">;

let volatileApiKey = "";

function clearVolatileApiKey(): void {
  volatileApiKey = "";
}

// `pagehide` also covers bfcache navigation. The browser destroys the rest of
// the page state on a normal unload; this explicit reset makes the module cache
// fail closed if the document is retained for a possible back navigation.
if (
  typeof window !== "undefined" &&
  typeof window.addEventListener === "function"
) {
  window.addEventListener("pagehide", clearVolatileApiKey);
  window.addEventListener("beforeunload", clearVolatileApiKey);
}

function metadataFromStored(value: unknown): ByokMetadata | null {
  if (
    typeof value === "object" &&
    value !== null &&
    typeof (value as Record<string, unknown>).provider === "string" &&
    typeof (value as Record<string, unknown>).fingerprint === "string" &&
    typeof (value as Record<string, unknown>).validatedAt === "string"
  ) {
    const parsed = value as Record<string, unknown>;
    return {
      provider: parsed.provider as string,
      fingerprint: parsed.fingerprint as string,
      validatedAt: parsed.validatedAt as string,
      ...(typeof parsed.modelCount === "number"
        ? { modelCount: parsed.modelCount }
        : {}),
    };
  }
  return null;
}

/** Read display-safe provider metadata from localStorage. Legacy records are
 *  migrated on read by deleting their persisted key immediately. A legacy
 *  key is never copied into volatile memory: the user must re-enter it. */
export function getByokMetadata(): ByokMetadata | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    const metadata = metadataFromStored(parsed);
    const hasLegacyKey =
      typeof parsed === "object" &&
      parsed !== null &&
      Object.prototype.hasOwnProperty.call(parsed, "key");
    if (!metadata) {
      if (hasLegacyKey) window.localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    const serializedMetadata = JSON.stringify(metadata);
    if (hasLegacyKey || raw !== serializedMetadata) {
      window.localStorage.setItem(STORAGE_KEY, serializedMetadata);
    }
    return metadata;
  } catch {
    // A corrupt record is not useful configuration and may still contain an
    // unparseable legacy secret. Remove it instead of repeatedly retaining it.
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Storage may itself be unavailable (privacy mode / denied access).
    }
    return null;
  }
}

// Run the legacy-secret migration as soon as this module loads, even on pages
// that only import BYOK helpers without immediately requesting a config.
if (typeof window !== "undefined") getByokMetadata();

/** Return a usable config only while its key remains in volatile memory. */
export function getByokConfig(): ByokConfig | null {
  const metadata = getByokMetadata();
  if (!metadata || !volatileApiKey) return null;
  return { ...metadata, key: volatileApiKey };
}

/** Cache the key for this page and persist only display-safe metadata. */
export function saveByokConfig(config: ByokConfig): void {
  volatileApiKey = config.key;
  if (typeof window === "undefined") return;
  const metadata: ByokMetadata = {
    provider: config.provider,
    fingerprint: config.fingerprint,
    validatedAt: config.validatedAt,
    ...(typeof config.modelCount === "number"
      ? { modelCount: config.modelCount }
      : {}),
  };
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(metadata));
}

/** Clear both the volatile key and persisted provider metadata. */
export function clearByokConfig(): void {
  clearVolatileApiKey();
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(STORAGE_KEY);
}

/** True iff this page currently has a usable in-memory key. */
export function isByokConfigured(): boolean {
  return getByokConfig() !== null;
}

/**
 * Server-side validation ping wrapper. POSTs to /api/byok/validate-key
 * with {provider, api_key} and returns the parsed response. Doesn't
 * persist on its own — caller decides whether to save (typically: only
 * if `valid === true`).
 *
 * Response shape (validate-key endpoint):
 *   { valid: bool, provider: str, fingerprint: str,
 *     model_count?: int, error?: str }
 */
export interface ValidateKeyResponse {
  valid: boolean;
  provider?: string;
  fingerprint?: string;
  model_count?: number;
  error?: string;
}

export async function validateByokKey(
  provider: string,
  apiKey: string,
): Promise<ValidateKeyResponse> {
  try {
    const resp = await fetch("/api/byok/validate-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, api_key: apiKey }),
    });
    return (await resp.json()) as ValidateKeyResponse;
  } catch (e) {
    return {
      valid: false,
      error: `network error: ${e instanceof Error ? e.message : String(e)}`,
    };
  }
}

// ─────────────────────────────────────────────────────────────────
// V1.5-Query-1 — settings, cost calculation, query orchestration
// ─────────────────────────────────────────────────────────────────

const SETTINGS_KEY = "zspan_byok_settings_v1";

export interface ByokSettings {
  /** Per-query cap on response tokens. Default 1024, range 256-4096. Even
   *  at GPT-4o the max-tokens cap × output rate caps per-query cost at ~$0.04. */
  max_output_tokens: number;
  /** 0.0-1.0 (some providers allow up to 2.0). Default 0.2 — matches the
   *  project's neutral civic-news register. */
  temperature: number;
  /** Retrieval breadth — how many chunks the Qdrant /query endpoint
   *  returns before the LLM sees them. Higher K = more context = more
   *  comprehensive answers, but more synthesis tokens + potentially more
   *  noise. Default 12, range 5-50. (V-Op-2-Tune-1c 2026-06-26.) */
  top_k: number;
  /** Debounce countdown on Send button. Prevents accidental rapid-fire. */
  min_seconds_between_queries: number;
  /** Soft per-minute cap; exceeding triggers a 30s cooldown toast. */
  per_minute_query_cap: number;
  /** Per-session cumulative spend (USD) at which a confirm dialog fires
   *  before the next query. User-visible safety net. */
  per_session_spend_warn_usd: number;
}

export const DEFAULT_BYOK_SETTINGS: ByokSettings = {
  max_output_tokens: 1024,
  temperature: 0.2,
  top_k: 12,
  // 10s default debounce per operator direction 2026-06-24 (queries must
  // have logical pauses between them, not fire back-to-back). User can
  // lower in advanced
  // settings if they're doing rapid evaluation work; ceiling stays via the
  // per-minute cap regardless.
  min_seconds_between_queries: 10,
  per_minute_query_cap: 20,
  per_session_spend_warn_usd: 0.5,
};

export function getByokSettings(): ByokSettings {
  if (typeof window === "undefined") return DEFAULT_BYOK_SETTINGS;
  try {
    const raw = window.localStorage.getItem(SETTINGS_KEY);
    if (!raw) return DEFAULT_BYOK_SETTINGS;
    const parsed = JSON.parse(raw);
    return { ...DEFAULT_BYOK_SETTINGS, ...parsed };
  } catch {
    return DEFAULT_BYOK_SETTINGS;
  }
}

export function saveByokSettings(settings: ByokSettings): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
}

/** Per-million-token rates per provider (USD). Used for cost display.
 *  Reflects 2026-06-24 published pricing; bump when providers change rates. */
export const PROVIDER_RATES: Record<string, { input: number; output: number }> = {
  "google-gemini-2.5-flash": { input: 0.075, output: 0.3 },
  "openai-gpt-4o-mini": { input: 0.15, output: 0.6 },
  "openai-gpt-4o": { input: 2.5, output: 10.0 },
  "anthropic-claude-3-haiku": { input: 0.25, output: 1.25 },
  "anthropic-claude-3-5-sonnet": { input: 3.0, output: 15.0 },
};

export function calculateCost(
  provider: string,
  inputTokens: number,
  outputTokens: number,
): number {
  const rate = PROVIDER_RATES[provider];
  if (!rate) return 0;
  return (
    (inputTokens / 1_000_000) * rate.input +
    (outputTokens / 1_000_000) * rate.output
  );
}

export function formatCost(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.0001) return "<$0.0001";
  if (usd < 1) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

interface RagSearchChunk {
  chunk_index: number;
  vector_id: string;
  body: string;
  start_seconds: number;
  end_seconds: number;
  speaker_turns: unknown;
  score: number;
}

interface RagSearchResponse {
  success: boolean;
  error?: string;
  reason_code?: string;
  meeting_id: number;
  query: string;
  chunks: RagSearchChunk[];
  provenance: {
    run_id: string;
    vector_ids: string[];
    prompt_template_hash: string;
    prompt_template_version: string;
    query_hash: string;
    timestamp_utc: string;
  };
  recommended_system_prompt: string;
  synthesis_envelope?: {
    system_prompt: string;
    user_message: string;
    envelope_hash: string;
    envelope_version: string;
    expires_at_utc: string;
    run_id: string;
  };
}

export interface ByokQueryResult {
  answer: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  provider: string;
  model: string;
  runId: string;
  vectorIds: string[];
  chunks: Array<{
    chunk_index: number;
    timecode: string;
    start_seconds: number;
    /** V1.5-BYOK-Verbatim-1 (2026-07-04) — the retrieved chunk's
     *  verbatim text body. Preserved on the result so ByokQueryPanel
     *  can pass it to KaraokeText for verbatim-substring highlighting
     *  (click-to-hear on 8+ consecutive-token matches). Empty string
     *  when the chunk had no body (honest-empty path). */
    body: string;
  }>;
}

function formatTimecode(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
}

/** Legacy client-side message construction remains authoritative for Gemini
 * direct-browser synthesis and the local-workspace loopback path. OpenAI and
 * Anthropic relay requests consume the server-built envelope instead. */
export function buildUserMessage(meetingId: number, query: string, chunks: RagSearchChunk[]): string {
  const chunksBlock = chunks
    .map((c) => `[chunk_index=${c.chunk_index} timecode=${formatTimecode(c.start_seconds)} start_seconds=${c.start_seconds.toFixed(1)}]\n${c.body}`)
    .join("\n\n");
  return `CURRENT QUESTION: ${query}\n\nRETRIEVED CONTEXT — chunks from meeting_id=${meetingId}:\n---\n${chunksBlock}\n---`;
}

type SynthesisEnvelope = NonNullable<
  RagSearchResponse["synthesis_envelope"]
>;

function requireSynthesisEnvelope(
  value: RagSearchResponse["synthesis_envelope"],
): SynthesisEnvelope {
  if (
    !value ||
    typeof value.system_prompt !== "string" ||
    value.system_prompt.length === 0 ||
    typeof value.user_message !== "string" ||
    value.user_message.length === 0 ||
    typeof value.envelope_hash !== "string" ||
    !/^[0-9a-f]{64}$/.test(value.envelope_hash) ||
    typeof value.envelope_version !== "string" ||
    value.envelope_version.length === 0 ||
    typeof value.expires_at_utc !== "string" ||
    !value.expires_at_utc.endsWith("Z") ||
    typeof value.run_id !== "string" ||
    value.run_id.length === 0
  ) {
    throw new Error(
      "rag-search returned a malformed synthesis envelope; no provider request was sent",
    );
  }
  return value;
}

/** Options shared between the one-shot and streaming query paths.
 *  Adding `onDelta` selects the streaming path; omitting it preserves the
 *  one-shot behavior for callers that want the full answer as a single
 *  string. */
export interface ByokQueryOptions {
  signal?: AbortSignal;
  /** V1.5-BYOK-Stream-1 (2026-07-04) — when provided, executeByokQuery
   *  uses SSE and fires this callback with each incremental token. The
   *  returned promise still resolves with the full accumulated answer +
   *  final token/cost metadata (safe to overwrite state that was updated
   *  incrementally via onDelta — same string). Omit to keep the historic
   *  one-shot fetch-then-json shape. */
  onDelta?: (delta: string) => void;
}

/** End-to-end BYOK query orchestration:
 *  1. POST /api/rag-search to get chunks + system prompt + provenance (Mac-side)
 *  2. Route to provider — Gemini direct (browser→Google) or relay (browser→Z-SPAN→OpenAI/Anthropic)
 *  3. Return the answer + cost + provenance for inline render
 *
 *  The `signal` option lets the caller abort mid-flight (Cancel button).
 *  The `onDelta` option (V1.5-BYOK-Stream-1) switches to SSE so tokens
 *  render as they arrive.
 */
export async function executeByokQuery(
  meetingId: number,
  query: string,
  config: ByokConfig,
  settings: ByokSettings,
  options: ByokQueryOptions = {},
): Promise<ByokQueryResult> {
  // Step 1: rag-search
  const ragResp = await fetch(`/api/rag-search/${meetingId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      top_k: settings.top_k,
      include_provenance: true,
      provider: config.provider,
    }),
    signal: options.signal,
  });
  if (!ragResp.ok) {
    let errorBody: { error?: unknown; reason_code?: unknown } | null =
      null;
    try {
      errorBody = (await ragResp.json()) as {
        error?: unknown;
        reason_code?: unknown;
      };
    } catch {
      // Preserve the existing generic HTTP error when no JSON body exists.
    }
    if (typeof errorBody?.error === "string") {
      const suffix =
        typeof errorBody.reason_code === "string"
          ? ` (${errorBody.reason_code})`
          : "";
      throw new Error(`${errorBody.error}${suffix}`);
    }
    throw new Error(`rag-search failed: HTTP ${ragResp.status}`);
  }
  const ragData = (await ragResp.json()) as RagSearchResponse;
  if (!ragData.success) {
    if (typeof ragData.error === "string") {
      const suffix =
        typeof ragData.reason_code === "string"
          ? ` (${ragData.reason_code})`
          : "";
      throw new Error(`${ragData.error}${suffix}`);
    }
    throw new Error(`rag-search returned success=false`);
  }
  if (!ragData.chunks || ragData.chunks.length === 0) {
    return {
      answer:
        config.provider === LOCAL_WORKSPACE_PROVIDER
          ? "This meeting isn't indexed in your workspace yet — open its page and press Process, then ask again."
          : "No chunks retrieved for this meeting. It may not be indexed in Qdrant yet — V1-Mohave-1 ingestion is operator-gated outside the 13 Mohave-deep showcase meetings.",
      inputTokens: 0,
      outputTokens: 0,
      costUsd: 0,
      provider: config.provider,
      model: "",
      runId: ragData.provenance?.run_id || "",
      vectorIds: [],
      chunks: [],
    };
  }

  // Local workspace (zspan CLI `open`): the loopback server makes the
  // provider call with the stored key — one-shot, no SSE. The onDelta
  // single fire below keeps streaming callers' pending→text flip.
  if (config.provider === LOCAL_WORKSPACE_PROVIDER) {
    const userMessage = buildUserMessage(meetingId, query, ragData.chunks);
    return callLocalWorkspace(ragData, userMessage, settings, options);
  }

  const isGemini = config.provider.startsWith("google-gemini");
  const streaming = typeof options.onDelta === "function";

  if (isGemini) {
    const userMessage = buildUserMessage(meetingId, query, ragData.chunks);
    if (streaming) {
      return callGeminiStreaming(
        ragData,
        userMessage,
        config,
        settings,
        options,
      );
    }
    return callGeminiDirect(ragData, userMessage, config, settings, options);
  }

  const envelope = requireSynthesisEnvelope(ragData.synthesis_envelope);
  if (streaming) {
    return callRelayStreaming(
      ragData,
      envelope,
      config,
      settings,
      options,
    );
  }
  return callViaRelay(ragData, envelope, config, settings, options);
}

async function callLocalWorkspace(
  ragData: RagSearchResponse,
  userMessage: string,
  settings: ByokSettings,
  options: ByokQueryOptions,
): Promise<ByokQueryResult> {
  // The system prompt + user message travel to the loopback server —
  // never a key (it's already stored on the machine). The answer comes
  // back whole; the local hop is same-machine so there's no relay
  // discipline to inherit (S-131: the "server" is the user's own tool).
  const resp = await fetch("/api/local/librarian/synthesize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      system_prompt: ragData.recommended_system_prompt,
      user_message: userMessage,
      max_tokens: settings.max_output_tokens,
      temperature: settings.temperature,
    }),
    signal: options.signal,
  });
  let data: {
    success?: boolean;
    answer?: string;
    provider_id?: string;
    model?: string;
    input_tokens?: number;
    output_tokens?: number;
    error?: string;
  };
  try {
    data = await resp.json();
  } catch {
    throw new Error(`local synthesis failed: HTTP ${resp.status}`);
  }
  if (!resp.ok || !data?.success) {
    throw new Error(data?.error || `local synthesis failed: HTTP ${resp.status}`);
  }

  const answer = data.answer || "";
  // One-shot delivery: fire the single delta so the panel's first-token
  // handling (dots → text, Librarian animation stop) behaves as it does
  // on the streaming paths.
  if (answer) options.onDelta?.(answer);

  const inputTokens = data.input_tokens || 0;
  const outputTokens = data.output_tokens || 0;
  const providerId = data.provider_id || LOCAL_WORKSPACE_PROVIDER;
  return {
    answer,
    inputTokens,
    outputTokens,
    // Cost maps through the same rate table where the provider+model
    // pair is known (e.g. openai-gpt-4o-mini); unknown pairs read $0.
    costUsd: calculateCost(providerId, inputTokens, outputTokens),
    provider: providerId,
    model: data.model || "",
    // A workspace attesting to itself proves nothing (S-134) — no
    // run_id, which is exactly what hides the verify-run link.
    runId: "",
    vectorIds: [],
    chunks: ragData.chunks.map((c) => ({
      chunk_index: c.chunk_index,
      timecode: formatTimecode(c.start_seconds),
      start_seconds: c.start_seconds,
      body: c.body,
    })),
  };
}

async function callGeminiDirect(
  ragData: RagSearchResponse,
  userMessage: string,
  config: ByokConfig,
  settings: ByokSettings,
  options: { signal?: AbortSignal },
): Promise<ByokQueryResult> {
  // Direct browser → generativelanguage.googleapis.com. Key in URL param
  // per Google's documented pattern; never touches Z-SPAN servers.
  const modelId = config.provider.replace("google-", ""); // "google-gemini-2.5-flash" → "gemini-2.5-flash"
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${modelId}:generateContent?key=${encodeURIComponent(config.key)}`;
  const payload = {
    systemInstruction: { parts: [{ text: ragData.recommended_system_prompt }] },
    contents: [{ role: "user", parts: [{ text: userMessage }] }],
    generationConfig: {
      maxOutputTokens: settings.max_output_tokens,
      temperature: settings.temperature,
    },
  };
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: options.signal,
  });
  if (!resp.ok) {
    let msg = `Gemini API HTTP ${resp.status}`;
    try {
      const errBody = await resp.json();
      msg = errBody?.error?.message || msg;
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }
  const data = await resp.json();
  const answer = data?.candidates?.[0]?.content?.parts?.[0]?.text || "";
  const usage = data?.usageMetadata || {};
  const inputTokens = usage.promptTokenCount || 0;
  const outputTokens = usage.candidatesTokenCount || 0;
  return {
    answer,
    inputTokens,
    outputTokens,
    costUsd: calculateCost(config.provider, inputTokens, outputTokens),
    provider: config.provider,
    model: modelId,
    runId: ragData.provenance.run_id,
    vectorIds: ragData.provenance.vector_ids,
    chunks: ragData.chunks.map((c) => ({
      chunk_index: c.chunk_index,
      timecode: formatTimecode(c.start_seconds),
      start_seconds: c.start_seconds,
      body: c.body,
    })),
  };
}

async function callViaRelay(
  ragData: RagSearchResponse,
  envelope: SynthesisEnvelope,
  config: ByokConfig,
  settings: ByokSettings,
  options: { signal?: AbortSignal },
): Promise<ByokQueryResult> {
  // Resolve provider-specific model id. config.provider carries the
  // BYOK_ARCHITECTURE_SPEC matrix id; the actual API model name differs.
  const modelMap: Record<string, string> = {
    "openai-gpt-4o-mini": "gpt-4o-mini",
    "openai-gpt-4o": "gpt-4o",
    "anthropic-claude-3-haiku": "claude-3-haiku-20240307",
    "anthropic-claude-3-5-sonnet": "claude-3-5-sonnet-20241022",
  };
  const model = modelMap[config.provider] || config.provider;

  const resp = await fetch("/api/byok/relay", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: config.provider,
      api_key: config.key,
      model,
      system_prompt: envelope.system_prompt,
      user_message: envelope.user_message,
      envelope_version: envelope.envelope_version,
      run_id: envelope.run_id,
      max_tokens: settings.max_output_tokens,
      temperature: settings.temperature,
    }),
    signal: options.signal,
  });
  const data = await resp.json();
  if (!resp.ok || data?.error) {
    throw new Error(data?.error?.message || `relay failed: HTTP ${resp.status}`);
  }

  // Parse per-provider response shape
  let answer = "";
  let inputTokens = 0;
  let outputTokens = 0;
  if (config.provider.startsWith("openai")) {
    answer = data?.choices?.[0]?.message?.content || "";
    inputTokens = data?.usage?.prompt_tokens || 0;
    outputTokens = data?.usage?.completion_tokens || 0;
  } else if (config.provider.startsWith("anthropic")) {
    answer = (data?.content || [])
      .filter((b: { type?: string }) => b.type === "text")
      .map((b: { text?: string }) => b.text)
      .join("\n");
    inputTokens = data?.usage?.input_tokens || 0;
    outputTokens = data?.usage?.output_tokens || 0;
  }

  return {
    answer,
    inputTokens,
    outputTokens,
    costUsd: calculateCost(config.provider, inputTokens, outputTokens),
    provider: config.provider,
    model,
    runId: ragData.provenance.run_id,
    vectorIds: ragData.provenance.vector_ids,
    chunks: ragData.chunks.map((c) => ({
      chunk_index: c.chunk_index,
      timecode: formatTimecode(c.start_seconds),
      start_seconds: c.start_seconds,
      body: c.body,
    })),
  };
}

// ─────────────────────────────────────────────────────────────────
// V1.5-BYOK-Stream-1 — SSE variants (2026-07-04)
// ─────────────────────────────────────────────────────────────────
//
// The one-shot paths above stay for callers that don't pass `onDelta`.
// These streaming variants use Server-Sent Events so the browser renders
// tokens as they arrive. Gemini uses its native `:streamGenerateContent`
// endpoint direct from the browser (CORS-friendly); OpenAI + Anthropic
// route through the Flask/Express SSE relay at /api/byok/relay-stream.
//
// The uniform terminal sentinel is `data: [DONE]` — Gemini doesn't emit
// it natively; the relay does (Python side synthesizes for Anthropic).
// Both are handled by readSSE().

interface SSEEvent {
  eventName: string | null;
  data: string;
}

/** Parse an SSE-formatted Response body, dispatching each `data:` line to
 *  `onEvent`. Handles CRLF, comment lines, `event:` name prefixes, and
 *  event-boundary blank lines per the SSE spec. Yields cooperatively
 *  during a long stream by awaiting reader.read().
 *
 *  This is a simplified reader: it assumes each SSE event carries a
 *  single `data:` line (OpenAI + Anthropic + Gemini all follow this
 *  pattern). For multi-line data events we'd need to buffer them until
 *  the blank-line boundary. If a provider ever ships multi-line data,
 *  extend here. */
async function readSSE(
  response: Response,
  onEvent: (event: SSEEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName: string | null = null;
  try {
    while (true) {
      if (signal?.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let lineEnd: number;
      while ((lineEnd = buffer.indexOf("\n")) !== -1) {
        const raw = buffer.slice(0, lineEnd);
        buffer = buffer.slice(lineEnd + 1);
        const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
        if (line === "") {
          eventName = null; // event boundary
          continue;
        }
        if (line.startsWith(":")) continue; // SSE comment
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
          continue;
        }
        if (line.startsWith("data:")) {
          // Trim exactly one leading space (SSE spec) but preserve any
          // internal whitespace the delta may carry.
          const data = line.slice(5).replace(/^ /, "");
          onEvent({ eventName, data });
          continue;
        }
        // Ignore id: / retry: lines — not used by any current provider.
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* reader may already be released after abort */
    }
  }
}

async function callGeminiStreaming(
  ragData: RagSearchResponse,
  userMessage: string,
  config: ByokConfig,
  settings: ByokSettings,
  options: ByokQueryOptions,
): Promise<ByokQueryResult> {
  const modelId = config.provider.replace("google-", "");
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${modelId}:streamGenerateContent?alt=sse&key=${encodeURIComponent(config.key)}`;
  const payload = {
    systemInstruction: { parts: [{ text: ragData.recommended_system_prompt }] },
    contents: [{ role: "user", parts: [{ text: userMessage }] }],
    generationConfig: {
      maxOutputTokens: settings.max_output_tokens,
      temperature: settings.temperature,
    },
  };
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(payload),
    signal: options.signal,
  });
  if (!resp.ok) {
    let msg = `Gemini API HTTP ${resp.status}`;
    try {
      const errBody = await resp.json();
      msg = errBody?.error?.message || msg;
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }

  let answer = "";
  let inputTokens = 0;
  let outputTokens = 0;

  await readSSE(
    resp,
    ({ data }) => {
      if (data === "[DONE]") return;
      try {
        const chunk = JSON.parse(data);
        const parts = chunk?.candidates?.[0]?.content?.parts;
        if (Array.isArray(parts)) {
          for (const part of parts) {
            const text = part?.text;
            if (typeof text === "string" && text.length > 0) {
              answer += text;
              options.onDelta?.(text);
            }
          }
        }
        // Gemini streaming carries cumulative usage on later events;
        // last-write-wins gives the final counts.
        const usage = chunk?.usageMetadata;
        if (usage) {
          if (typeof usage.promptTokenCount === "number") {
            inputTokens = usage.promptTokenCount;
          }
          if (typeof usage.candidatesTokenCount === "number") {
            outputTokens = usage.candidatesTokenCount;
          }
        }
      } catch {
        /* skip malformed JSON — spec-friendly graceful degradation */
      }
    },
    options.signal,
  );

  return {
    answer,
    inputTokens,
    outputTokens,
    costUsd: calculateCost(config.provider, inputTokens, outputTokens),
    provider: config.provider,
    model: modelId,
    runId: ragData.provenance.run_id,
    vectorIds: ragData.provenance.vector_ids,
    chunks: ragData.chunks.map((c) => ({
      chunk_index: c.chunk_index,
      timecode: formatTimecode(c.start_seconds),
      start_seconds: c.start_seconds,
      body: c.body,
    })),
  };
}

async function callRelayStreaming(
  ragData: RagSearchResponse,
  envelope: SynthesisEnvelope,
  config: ByokConfig,
  settings: ByokSettings,
  options: ByokQueryOptions,
): Promise<ByokQueryResult> {
  const modelMap: Record<string, string> = {
    "openai-gpt-4o-mini": "gpt-4o-mini",
    "openai-gpt-4o": "gpt-4o",
    "anthropic-claude-3-haiku": "claude-3-haiku-20240307",
    "anthropic-claude-3-5-sonnet": "claude-3-5-sonnet-20241022",
  };
  const model = modelMap[config.provider] || config.provider;

  const resp = await fetch("/api/byok/relay-stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({
      provider: config.provider,
      api_key: config.key,
      model,
      system_prompt: envelope.system_prompt,
      user_message: envelope.user_message,
      envelope_version: envelope.envelope_version,
      run_id: envelope.run_id,
      max_tokens: settings.max_output_tokens,
      temperature: settings.temperature,
    }),
    signal: options.signal,
  });

  if (!resp.ok) {
    // Non-2xx = JSON error from Flask (403 owner-gate, 400 bad request,
    // 504 upstream timeout). Parse + throw so the caller renders the
    // error uniformly with the one-shot path.
    let msg = `relay-stream failed: HTTP ${resp.status}`;
    try {
      const errBody = await resp.json();
      msg = errBody?.error?.message || msg;
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }

  const isOpenAI = config.provider.startsWith("openai");
  let answer = "";
  let inputTokens = 0;
  let outputTokens = 0;
  let relayError: string | null = null;

  await readSSE(
    resp,
    ({ eventName, data }) => {
      if (data === "[DONE]") return;
      if (eventName === "relay_error") {
        try {
          const errBody = JSON.parse(data);
          relayError = errBody?.error?.message || "relay error";
        } catch {
          relayError = "relay error (unparseable payload)";
        }
        return;
      }
      try {
        const chunk = JSON.parse(data);
        if (isOpenAI) {
          const delta = chunk?.choices?.[0]?.delta?.content;
          if (typeof delta === "string" && delta.length > 0) {
            answer += delta;
            options.onDelta?.(delta);
          }
          // Final usage arrives via stream_options.include_usage in a
          // trailing chunk with empty choices[].
          const usage = chunk?.usage;
          if (usage) {
            if (typeof usage.prompt_tokens === "number") {
              inputTokens = usage.prompt_tokens;
            }
            if (typeof usage.completion_tokens === "number") {
              outputTokens = usage.completion_tokens;
            }
          }
        } else {
          // Anthropic — dispatch by SSE event name.
          if (eventName === "content_block_delta") {
            const text = chunk?.delta?.text;
            if (typeof text === "string" && text.length > 0) {
              answer += text;
              options.onDelta?.(text);
            }
          } else if (eventName === "message_start") {
            const usage = chunk?.message?.usage;
            if (usage && typeof usage.input_tokens === "number") {
              inputTokens = usage.input_tokens;
            }
          } else if (eventName === "message_delta") {
            const usage = chunk?.usage;
            if (usage && typeof usage.output_tokens === "number") {
              outputTokens = usage.output_tokens;
            }
          }
        }
      } catch {
        /* skip malformed JSON */
      }
    },
    options.signal,
  );

  if (relayError) {
    throw new Error(relayError);
  }

  return {
    answer,
    inputTokens,
    outputTokens,
    costUsd: calculateCost(config.provider, inputTokens, outputTokens),
    provider: config.provider,
    model,
    runId: ragData.provenance.run_id,
    vectorIds: ragData.provenance.vector_ids,
    chunks: ragData.chunks.map((c) => ({
      chunk_index: c.chunk_index,
      timecode: formatTimecode(c.start_seconds),
      start_seconds: c.start_seconds,
      body: c.body,
    })),
  };
}

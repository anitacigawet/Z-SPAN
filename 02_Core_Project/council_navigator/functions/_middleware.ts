// Cloudflare Pages middleware — traffic-event tee for the HQ skybox viz.
//
// Runs on EVERY canonical public- or operator-host request (static page-views
// plus API traffic). After the matched function or static asset handler
// returns its response, this middleware fires a
// non-blocking POST to ${BACKEND_URL}/api/hq/traffic-events/ingest with
// the request meta + classification. The Worker's response goes out to
// the user immediately; the tee runs in waitUntil() in the background.
//
// Bot classification comes from cf.botManagement (Cloudflare Bot
// Management) when available — verifiedBot for known good crawlers
// (Googlebot, Bingbot), score thresholds for likely_bot vs human. On
// free Pages plans botManagement may be absent; we degrade to "unknown"
// rather than guess.
//
// Required env vars (set in the Pages dashboard, Production scope):
//   BACKEND_URL                   — the Railway hostname (already set for
//                                   the existing api catchall proxy)
//   Z_SPAN_TRAFFIC_INGEST_TOKEN   — shared secret matching the Flask side's
//                                   _validate_traffic_ingest_token. Mint
//                                   per-deploy; rotate freely (token-only
//                                   gate, no other auth on the endpoint).
//   ZSPAN_EDGE_TOKEN              — shared Pages-to-origin credential. The
//                                   traffic tee carries it just like the API
//                                   proxy does.
//
// IMPORTANT: this middleware runs BEFORE api/[[catchall]].ts because
// _middleware.ts is the outermost wrapper. The catchall's operator-host
// identity gate still fires for /api/*; the tee just records the attempt.

interface Env {
  BACKEND_URL: string;
  Z_SPAN_TRAFFIC_INGEST_TOKEN?: string;
  ZSPAN_EDGE_TOKEN?: string;
}

// ── canonical-host redirects ──────────────────────────────────────────
//
// Cloudflare Pages auto-generates `<project>.pages.dev` and preview aliases.
// Keep those aliases and www on the canonical apex so there is one public
// origin for paths, query strings, caching, and indexing.
//
// The canonical-host fix is here, at the edge, in code we control: any
// request whose Host ends in `.pages.dev`, plus www.zspan.org, gets a 302
// to the apex custom domain with the path + query preserved.
//
// SAFETY: this keys only on the `.pages.dev` suffix or exact www hostname.
// The apex and operator host cannot match either branch.
const CANONICAL_ORIGIN = "https://zspan.org";

function canonicalHostRedirect(request: Request): Response | null {
  let host = "";
  try {
    host = new URL(request.url).hostname.toLowerCase().replace(/\.$/, "");
  } catch {
    return null; // unparseable URL — let the normal path handle it
  }
  if (!host.endsWith(".pages.dev") && host !== "www.zspan.org") return null;

  const incoming = new URL(request.url);
  const target = CANONICAL_ORIGIN + incoming.pathname + incoming.search;
  return new Response(null, {
    status: 302,
    headers: {
      Location: target,
      // Don't let intermediaries cache the redirect aggressively — keep it
      // reversible if the canonical host ever changes.
      "Cache-Control": "no-store",
      // Belt-and-suspenders: ask crawlers not to index the hostname alias.
      "X-Robots-Tag": "noindex",
    },
  });
}

// Mirrors EXCLUDED_PATHS in parsers/traffic_events.py. The SSE stream
// itself + the polling endpoints would otherwise dominate the viz with
// monitoring noise — they are the platform watching itself.
const EXCLUDED_PATHS: readonly string[] = [
  "/api/hq/traffic-events",
  "/api/operator/badges",
  "/api/orchestrator/autonomy",
  "/api/ingestion/governor",
  "/api/hq/status",
];

function isExcluded(path: string): boolean {
  const p = path.split("?", 1)[0];
  return EXCLUDED_PATHS.some((prefix) => p.startsWith(prefix));
}

// Mirrors classify_path in parsers/traffic_events.py — keep the two in
// sync so the same URL classifies the same way regardless of which feed
// reported it.
function classifyPath(path: string): string {
  const p = path.split("?", 1)[0];
  if (!p || p === "/") return "other";
  if (p.startsWith("/api/guide") || p.startsWith("/guide")) return "guide";
  if (
    p.startsWith("/api/operator") ||
    p.startsWith("/api/orchestrator") ||
    p.startsWith("/api/ingestion") ||
    p.startsWith("/api/hq") ||
    p.startsWith("/api/work-orders") ||
    p.startsWith("/api/sync")
  ) {
    return "admin";
  }
  if (
    p.startsWith("/broadcast") ||
    p.startsWith("/api/notebook") ||
    p.startsWith("/api/quotes") ||
    p.startsWith("/api/cast") ||
    p.startsWith("/api/truth-book")
  ) {
    return "broadcast";
  }
  if (
    p === "/public-api" ||
    p.startsWith("/public-api/") ||
    p === "/v1" ||
    p.startsWith("/v1/")
  ) {
    return "api";
  }
  if (p.startsWith("/api/")) return "api";
  if (
    p.startsWith("/media/") ||
    p.startsWith("/static/") ||
    p.startsWith("/assets/")
  ) {
    return "static";
  }
  return "other";
}

// Cloudflare's incoming-request CF object carries the bot-management
// signals when Bot Management is enabled on the zone. Score is 1-99
// (lower = more bot-like); verifiedBot is true for known-good crawlers.
// On free plans the field may be absent — degrade to "unknown" rather
// than guess (the alternative is over-reporting bots, which would
// permanently red-stain the viz for hobby instances).
interface BotManagement {
  verifiedBot?: boolean;
  score?: number;
}

function classifyBot(
  request: Request,
): "human" | "verified_bot" | "likely_bot" | "unknown" {
  // The cf object isn't part of the standard Request type — cast.
  const cf = (request as Request & { cf?: { botManagement?: BotManagement } })
    .cf;
  const bm = cf?.botManagement;
  if (!bm) return "unknown";
  if (bm.verifiedBot) return "verified_bot";
  if (typeof bm.score === "number") {
    if (bm.score < 30) return "likely_bot";
    if (bm.score >= 80) return "human";
  }
  return "unknown";
}

async function fireIngest(
  env: Env,
  event: Record<string, unknown>,
): Promise<void> {
  if (
    !env.BACKEND_URL ||
    !env.Z_SPAN_TRAFFIC_INGEST_TOKEN ||
    !env.ZSPAN_EDGE_TOKEN
  ) {
    return;
  }
  const url = `${env.BACKEND_URL.replace(/\/$/, "")}/api/hq/traffic-events/ingest`;
  try {
    // 5s timeout via AbortSignal so a slow/down backend never lets the
    // waitUntil() promise live forever — Pages bounds waitUntil lifetimes
    // already, but defense-in-depth.
    await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Zspan-Traffic-Ingest-Token": env.Z_SPAN_TRAFFIC_INGEST_TOKEN,
        "X-Zspan-Edge-Token": env.ZSPAN_EDGE_TOKEN,
      },
      body: JSON.stringify({ events: [event] }),
      signal: AbortSignal.timeout(5000),
    });
  } catch {
    // Never throw out of waitUntil. A failed tee = one missing star.
  }
}

export const onRequest: PagesFunction<Env> = async (context) => {
  // FIRST: canonicalize *.pages.dev and www before serving content or
  // recording traffic for the non-canonical hostname.
  const blocked = canonicalHostRedirect(context.request);
  if (blocked) return blocked;

  // Pass through first — the user's response is never blocked by the tee.
  const response = await context.next();

  try {
    const url = new URL(context.request.url);
    if (!isExcluded(url.pathname)) {
      const event = {
        ts: new Date().toISOString(),
        status: response.status,
        path_class: classifyPath(url.pathname),
        bot_classification: classifyBot(context.request),
        // source field is set by the receiver (Flask forces 'cloudflare'
        // regardless of the body), so we don't send it here.
      };
      context.waitUntil(fireIngest(context.env, event));
    }
  } catch {
    // Defensive — never let a tee bug break the response path.
  }

  return response;
};

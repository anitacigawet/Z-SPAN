// Cloudflare Worker — the api.zspan.org public API origin (D-164, PI-4).
//
// A SECOND edge ingress in front of the SAME Railway service the zspan.org
// Pages Function already proxies to. This origin serves ONLY the public /v1
// contract (PUBLIC_INTERFACE_SPEC § 2-3) and fails closed on everything else:
//
// Dispatch order (path-first contract; DIV-008):
//   1. Unknown path → 404 for every method.
//   2. OPTIONS on a valid path → 204 + CORS without consuming rate limit.
//   3. All other valid-path requests consume the per-client rate limit.
//   4. Non-GET/HEAD → 405.
//   5. Validate origin config, then proxy with the edge token.
//
// The limiter allows 60 requests per 10 seconds per CF-Connecting-IP. Query
// parameters are canonicalized per route, the Railway origin is HTTPS-host
// allowlisted before the token is attached, redirects are rejected, and
// origin construction/fetch failures fail closed.
//
// Owner/operator routes are NOT reachable through this origin by design —
// they live exclusively at zspan.org/api/* behind the Pages catchall's
// OWNER_ONLY_PREFIXES + Cloudflare Access. Widening this allowlist happens
// per-route, explicitly, never namespace-wide (spec § 6.6).
//
// Required Worker env vars (set in the CF dashboard at deploy):
//   BACKEND_URL       — the Railway hostname (same value the Pages catchall uses)
//   ZSPAN_EDGE_TOKEN  — the shared origin-shield secret; Railway rejects
//                       direct hits without it (server/index.ts:240). Setting
//                       it here keeps api.zspan.org inside the same shielded
//                       boundary rather than becoming a bypass of it.
//
// Deploy (operator-present session — CF panel/DNS are Chrome-MCP-with-James
// actions per the standing instruction):
//   1. `wrangler deploy` from this directory is the authoritative deploy path.
//      Dashboard script-paste does NOT create the rate-limit binding; if the
//      script is pasted, create CATALOG_RATE_LIMITER manually in the dashboard.
//   2. Set the two env vars (Secrets) on the Worker.
//   3. SHADOW TEST first: hit the workers.dev URL — /v1/catalog/* must 200,
//      /api/* must 404, POST must 405, OPTIONS on a non-/v1 path must 404
//      (not 204 — DIV-008), and Railway direct-hit must stay 403.
//   4. Only then add the api.zspan.org custom domain / DNS record. The
//      public DNS activation rides the un-gate sequencing (dead last).

interface Env {
  BACKEND_URL: string;
  CATALOG_RATE_LIMITER: RateLimit;
  ZSPAN_EDGE_TOKEN: string;
}

// The explicit public allowlist. Per-route, never namespace-wide.
const V1_ALLOWED_PREFIXES: readonly string[] = ["/v1/catalog/"];

const ORIGIN_HOST = "z-span-production.up.railway.app";
const RAILWAY_HOST_SUFFIX = ".up.railway.app";
const MEETINGS_QUERY_KEYS = ["state", "county", "city", "year", "cursor"] as const;
const MAX_QUERY_VALUE_LENGTH = 128;
const MAX_QUERY_LENGTH = 512;

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
};

function failClosed(status: number, message: string): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      ...CORS_HEADERS,
    },
  });
}

function rateLimited(): Response {
  return new Response(JSON.stringify({ error: "rate limited" }), {
    status: 429,
    headers: {
      "Content-Type": "application/json",
      "Retry-After": "10",
      "Cache-Control": "no-store",
      "Access-Control-Expose-Headers": "Retry-After",
      ...CORS_HEADERS,
    },
  });
}

function canonicalQuery(url: URL): string {
  if (url.pathname !== "/v1/catalog/meetings") return "";

  const canonical = new URLSearchParams();
  for (const key of MEETINGS_QUERY_KEYS) {
    const value = url.searchParams.get(key);
    if (!value || value.length > MAX_QUERY_VALUE_LENGTH) continue;

    const candidate = new URLSearchParams(canonical);
    candidate.set(key, value);
    if (candidate.toString().length > MAX_QUERY_LENGTH) break;
    canonical.set(key, value);
  }
  return canonical.toString();
}

function configuredOrigin(rawOrigin: string): URL | null {
  try {
    const origin = new URL(rawOrigin);
    const allowedHost =
      origin.hostname === ORIGIN_HOST || origin.hostname.endsWith(RAILWAY_HOST_SUFFIX);
    if (
      origin.protocol !== "https:" ||
      origin.username !== "" ||
      origin.password !== "" ||
      !allowedHost
    ) {
      return null;
    }
    return origin;
  } catch {
    return null;
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // Fail closed on everything outside the explicit /v1 allowlist FIRST —
    // for every method, OPTIONS included. This origin has no other surface,
    // so an unknown path never earns even a CORS preflight (DIV-008: the
    // allowlist gates method handling, not the reverse — a bare 204 on an
    // unknown path both leaks "OPTIONS is handled here" and contradicts the
    // fail-closed contract in the header comment).
    const allowed = V1_ALLOWED_PREFIXES.some((p) => url.pathname.startsWith(p));
    if (!allowed) {
      // 404, not a redirect — api.zspan.org never reveals what lives on other origins.
      return failClosed(404, "not found");
    }

    // CORS preflight for browser consumers of the anonymous catalog — only
    // reachable now that the path is known to be on the allowlist.
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const clientKey = request.headers.get("CF-Connecting-IP") ?? "missing-client-ip";
    const { success } = await env.CATALOG_RATE_LIMITER.limit({ key: clientKey });
    if (!success) {
      return rateLimited();
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return failClosed(405, "method not allowed");
    }

    if (!env.BACKEND_URL || !env.ZSPAN_EDGE_TOKEN) {
      // Refuse to operate half-configured rather than proxying nowhere.
      return failClosed(503, "origin not configured");
    }

    const origin = configuredOrigin(env.BACKEND_URL);
    if (!origin) {
      return failClosed(503, "origin not configured");
    }

    const headers = new Headers();
    headers.set("Accept", "application/json");
    headers.set("X-Zspan-Edge-Token", env.ZSPAN_EDGE_TOKEN);

    let upstreamResponse: Response;
    try {
      const query = canonicalQuery(url);
      const upstream = new URL(url.pathname + (query ? `?${query}` : ""), origin);
      upstreamResponse = await fetch(upstream.toString(), {
        method: request.method,
        headers,
        redirect: "manual",
      });
    } catch {
      return failClosed(502, "origin unreachable");
    }

    if (upstreamResponse.status >= 300 && upstreamResponse.status < 400) {
      return failClosed(502, "unexpected origin redirect");
    }

    // Preserve status + the cache policy Flask sets; add CORS. Body streams.
    const responseHeaders = new Headers(CORS_HEADERS);
    for (const name of ["content-type", "cache-control"]) {
      const value = upstreamResponse.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    return new Response(upstreamResponse.body, {
      status: upstreamResponse.status,
      headers: responseHeaders,
    });
  },
};

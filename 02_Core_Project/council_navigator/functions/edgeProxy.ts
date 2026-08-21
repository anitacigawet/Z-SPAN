export interface EdgeProxyEnv {
  BACKEND_URL: string;
  OWNER_EMAIL?: string;
  ZSPAN_EDGE_TOKEN?: string;
  ZSPAN_SYNC_TOKEN?: string;
  // CF Access identity-verification config. Both must be set for the
  // Pages Function to independently verify Cf-Access-Jwt-Assertion via
  // Cloudflare's team JWKS. If either is unset, resolveAccessEmail
  // fails closed (returns null) rather than falling back to trusting
  // unverified assertions. See sol pen-test Finding #10.
  ACCESS_TEAM_DOMAIN?: string; // e.g. "zspan.cloudflareaccess.com"
  ACCESS_APP_AUD?: string;     // per-app AUD from CF Access dashboard
}

export type EdgeTrustPlane = "operator" | "public";

export function requestTrustPlane(request: Request): EdgeTrustPlane {
  const hostname = new URL(request.url).hostname
    .trim()
    .toLowerCase()
    .replace(/\.$/, "");
  return hostname === "operator.zspan.org" ? "operator" : "public";
}

export function jsonError(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

// ── CF Access JWT verification ────────────────────────────────────────
// Per sol pen-test Finding #10: resolveAccessEmail used to trust the
// friendly cf-access-authenticated-user-email header directly, or
// atob-decode the JWT payload with zero signature/issuer/audience/exp
// verification. If any operator/preview route ever permitted a request
// to reach this Function without Access's header sanitation, a forged
// claim became owner identity. This module now independently verifies
// the JWT via Cloudflare's team JWKS + WebCrypto and fails closed on
// any error.

type JWK = JsonWebKey & { kid?: string; kty: string; use?: string; alg?: string };

interface JWKSCacheEntry {
  keys: Map<string, CryptoKey>;
  fetchedAt: number;
}

const JWKS_TTL_MS = 10 * 60 * 1000; // 10 minutes; CF rotates infrequently
const jwksCache = new Map<string, JWKSCacheEntry>();

// Test-only: reset the JWKS cache between test cases.
export function _resetJwksCacheForTests(): void {
  jwksCache.clear();
}

async function fetchJWKS(teamDomain: string): Promise<Map<string, CryptoKey>> {
  const cached = jwksCache.get(teamDomain);
  if (cached && Date.now() - cached.fetchedAt < JWKS_TTL_MS) {
    return cached.keys;
  }
  const url = `https://${teamDomain}/cdn-cgi/access/certs`;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`JWKS fetch failed: HTTP ${response.status}`);
  }
  const body = (await response.json()) as { keys?: JWK[] };
  if (!body.keys || !Array.isArray(body.keys)) {
    throw new Error("JWKS response missing keys array");
  }
  const keys = new Map<string, CryptoKey>();
  for (const jwk of body.keys) {
    if (!jwk.kid || jwk.kty !== "RSA") continue;
    const key = await crypto.subtle.importKey(
      "jwk",
      jwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    keys.set(jwk.kid, key);
  }
  jwksCache.set(teamDomain, { keys, fetchedAt: Date.now() });
  return keys;
}

function base64UrlToBytes(input: string): Uint8Array {
  const b64 = input.replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob(padded);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function base64UrlToString(input: string): string {
  return new TextDecoder().decode(base64UrlToBytes(input));
}

interface AccessClaims {
  iss?: string;
  aud?: string | string[];
  exp?: number;
  nbf?: number;
  email?: string;
  identity_nonce?: string;
}

/**
 * Verify a Cf-Access-Jwt-Assertion and return the email claim.
 * Fails closed (returns null) on any error: missing config, malformed
 * JWT, signature mismatch, wrong iss/aud, expired, nbf in future,
 * JWKS unreachable, kid not in JWKS. Never throws — errors are logged
 * via console.warn.
 */
export async function verifyAccessJWT(
  jwt: string,
  env: Pick<EdgeProxyEnv, "ACCESS_TEAM_DOMAIN" | "ACCESS_APP_AUD">,
): Promise<string | null> {
  const teamDomain = (env.ACCESS_TEAM_DOMAIN || "").trim();
  const appAud = (env.ACCESS_APP_AUD || "").trim();
  if (!teamDomain || !appAud) {
    console.warn(
      "cf-access verify: ACCESS_TEAM_DOMAIN or ACCESS_APP_AUD not set; failing closed",
    );
    return null;
  }
  try {
    const parts = jwt.split(".");
    if (parts.length !== 3) return null;
    const [headerB64, payloadB64, signatureB64] = parts;

    const headerText = base64UrlToString(headerB64);
    const header = JSON.parse(headerText) as { alg?: string; kid?: string };
    if (header.alg !== "RS256" || !header.kid) return null;

    const keys = await fetchJWKS(teamDomain);
    const key = keys.get(header.kid);
    if (!key) return null;

    const signingInput = new TextEncoder().encode(`${headerB64}.${payloadB64}`);
    const signature = base64UrlToBytes(signatureB64);
    const ok = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      signature,
      signingInput,
    );
    if (!ok) return null;

    const claims = JSON.parse(base64UrlToString(payloadB64)) as AccessClaims;
    const expectedIssuer = `https://${teamDomain}`;
    if (claims.iss !== expectedIssuer) return null;

    const audOk = Array.isArray(claims.aud)
      ? claims.aud.includes(appAud)
      : claims.aud === appAud;
    if (!audOk) return null;

    const nowSec = Math.floor(Date.now() / 1000);
    if (typeof claims.exp !== "number" || claims.exp <= nowSec) return null;
    if (typeof claims.nbf === "number" && claims.nbf > nowSec) return null;

    return typeof claims.email === "string" && claims.email ? claims.email : null;
  } catch (err) {
    console.warn(
      "cf-access verify failed:",
      err instanceof Error ? err.message : String(err),
    );
    return null;
  }
}

/**
 * Read the verified email from the Cf-Access-Jwt-Assertion header.
 * Sol pen-test Finding #10: the friendly cf-access-authenticated-user-email
 * header fallback has been removed — it was spoofable if any route ever
 * reached this Function without Access header sanitation. Only the signed
 * JWT, independently verified against CF's team JWKS, is trusted now.
 */
export async function resolveAccessEmail(
  headers: Headers,
  env: Pick<EdgeProxyEnv, "ACCESS_TEAM_DOMAIN" | "ACCESS_APP_AUD">,
): Promise<string | null> {
  const jwt = headers.get("cf-access-jwt-assertion");
  if (!jwt) return null;
  return verifyAccessJWT(jwt, env);
}

function proxyHeaders(
  request: Request,
  edgeToken: string | undefined,
  stripPublicCredentials: boolean
): Headers {
  const cfConnectingIp = request.headers.get("cf-connecting-ip")?.trim() || "";
  const headers = new Headers(request.headers);
  headers.delete("host");
  // Session-103 (post-Slice-1 fix): the origin's compute_redirect_uri
  // picks the OAuth callback by inspecting X-Forwarded-Host, and
  // Flask's _forwarded_host_url() reads BOTH X-Forwarded-Host +
  // X-Forwarded-Proto. If we don't synthesize these from the trusted
  // Request.url, Flask sees the Railway internal host and falls
  // through to localhost:3000 — which is what caused public sign-in
  // to redirect to the dev-only OAuth client after we removed the
  // ZSPAN_OAUTH_REDIRECT_URI hardcode. Sol Round-1 flagged this seam
  // explicitly. Delete any caller-supplied copies first so nothing
  // upstream can spoof the values.
  headers.delete("x-forwarded-host");
  headers.delete("x-forwarded-proto");
  headers.delete("x-zspan-origin-host");
  headers.delete("x-zspan-origin-proto");
  const _incoming = new URL(request.url);
  headers.set("x-forwarded-host", _incoming.host);
  headers.set("x-forwarded-proto", _incoming.protocol.replace(/:$/, ""));
  // Session-104 (post-PR-#205 diagnostic): Railway's ingress rewrites
  // X-Forwarded-Host to the internal `z-span-production.up.railway.app`
  // hostname between here and Express, so the standard header alone can't
  // survive. Send a non-standard companion pair Railway won't touch —
  // Express reads these FIRST in proxyAuthToFlask and normalizes them
  // back to X-Forwarded-Host before Flask sees the request. Values are
  // synthesized from Request.url same as the standard pair above.
  headers.set("x-zspan-origin-host", _incoming.host);
  headers.set("x-zspan-origin-proto", _incoming.protocol.replace(/:$/, ""));
  for (const key of Array.from(headers.keys())) {
    if (key.toLowerCase().startsWith("cf-")) headers.delete(key);
  }
  // The origin and Flask may trust only the copy derived here from
  // Cloudflare's connecting-IP metadata. Drop all client-controlled aliases.
  headers.delete("x-forwarded-for");
  headers.delete("x-zspan-client-ip");
  if (cfConnectingIp) headers.set("x-zspan-client-ip", cfConnectingIp);
  // Never trust a client-supplied copy of the shared origin credential.
  headers.delete("x-zspan-edge-token");

  if (stripPublicCredentials) {
    headers.delete("cookie");
    headers.delete("authorization");
    headers.delete("agent-role");
    headers.delete("x-zspan-agent-role");
  }

  if (edgeToken) headers.set("x-zspan-edge-token", edgeToken);
  return headers;
}

export async function proxyToBackend(
  request: Request,
  env: EdgeProxyEnv,
  pathAndQuery: string,
  stripPublicCredentials = false
): Promise<Response> {
  if (!env.BACKEND_URL) {
    return jsonError(
      {
        success: false,
        error: "flagship misconfigured: BACKEND_URL not set on Pages Function",
      },
      500
    );
  }

  const target = `${env.BACKEND_URL.replace(/\/$/, "")}${pathAndQuery}`;
  const proxied = new Request(target, {
    method: request.method,
    headers: proxyHeaders(
      request,
      env.ZSPAN_EDGE_TOKEN,
      stripPublicCredentials
    ),
    body:
      request.method === "GET" || request.method === "HEAD"
        ? undefined
        : request.body,
    redirect: "manual",
  });

  try {
    return await fetch(proxied);
  } catch (err) {
    return jsonError(
      {
        success: false,
        error: "flagship backend unreachable",
        details: err instanceof Error ? err.message : String(err),
      },
      502
    );
  }
}

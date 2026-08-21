import { isIP } from "node:net";

export const ZSPAN_CLIENT_IP_HEADER = "X-Zspan-Client-Ip";

export type FlaskProxyRequest = {
  headers: Record<string, any>;
  ip?: string;
  socket?: { remoteAddress?: string };
};

const HEALTHCHECK_BYPASS_PATHS = new Set(["/healthz"]);
const INSECURE_NO_EDGE_TOKEN_VALUES = new Set(["1", "true"]);
const INSECURE_LOCAL_DEV_GATE_BYPASS = Symbol(
  "insecure-local-dev-origin-gate-bypass"
);
let didWarnInsecureNoEdgeToken = false;

export type OriginGateCredential =
  | string
  | typeof INSECURE_LOCAL_DEV_GATE_BYPASS;

function normalizedIp(value: unknown): string {
  if (typeof value !== "string") return "";
  const candidate = value.trim().replace(/^::ffff:/, "");
  return isIP(candidate) ? candidate : "";
}

export function requireEdgeToken(
  value: string | undefined
): OriginGateCredential {
  // A configured token always wins. The local-only escape hatch is consulted
  // only when the shared credential is absent, so it can never bypass a gate
  // that has been configured.
  if (value) return value;

  const allowInsecureNoEdgeToken = INSECURE_NO_EDGE_TOKEN_VALUES.has(
    process.env.ZSPAN_ALLOW_INSECURE_NO_EDGE_TOKEN ?? ""
  );
  if (process.env.NODE_ENV !== "production" && allowInsecureNoEdgeToken) {
    if (!didWarnInsecureNoEdgeToken) {
      console.warn(
        "\n" +
          "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n" +
          "WARNING: Z-SPAN ORIGIN GATE IS DISABLED FOR LOCAL DEVELOPMENT.\n" +
          "ZSPAN_ALLOW_INSECURE_NO_EDGE_TOKEN must never be set in production.\n" +
          "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
      );
      didWarnInsecureNoEdgeToken = true;
    }
    return INSECURE_LOCAL_DEV_GATE_BYPASS;
  }

  throw new Error(
    "ZSPAN_EDGE_TOKEN is required: the origin edge gate fails closed " +
      "when the shared credential is unset."
  );
}

export function originGateAllows(
  path: string,
  presentedToken: string | undefined,
  expectedToken: OriginGateCredential
): boolean {
  if (expectedToken === INSECURE_LOCAL_DEV_GATE_BYPASS) return true;
  // Only /healthz bypasses — Railway's load balancer probes it unauthenticated.
  // /media/* used to bypass here, which let raw Railway-hostname requests walk
  // the numeric-ID space with `Access-Control-Allow-Origin: *`. Closed per sol
  // pen-test Finding #4: browser flow still works because Cloudflare
  // Pages/Workers inject X-Zspan-Edge-Token on every proxied request.
  if (HEALTHCHECK_BYPASS_PATHS.has(path)) {
    return true;
  }
  return Boolean(
    presentedToken &&
      typeof expectedToken === "string" &&
      presentedToken === expectedToken
  );
}

/**
 * Derive the abuse-prevention IP that Flask may trust from this local proxy.
 * The edge overwrites X-Zspan-Client-Ip with CF-Connecting-IP. Trust it only
 * when the request also carries the valid edge token; never consult XFF.
 */
export function clientIpForFlask(req: FlaskProxyRequest): string {
  const expectedEdgeToken = process.env.ZSPAN_EDGE_TOKEN;
  const presentedEdgeToken = req.headers["x-zspan-edge-token"];
  if (
    expectedEdgeToken &&
    typeof presentedEdgeToken === "string" &&
    presentedEdgeToken === expectedEdgeToken
  ) {
    const forwarded = req.headers["x-zspan-client-ip"];
    const rawForwarded = Array.isArray(forwarded) ? forwarded[0] : forwarded;
    const trustedIp = normalizedIp(rawForwarded);
    if (trustedIp) return trustedIp;
  }

  return (
    normalizedIp(req.ip) ||
    normalizedIp(req.socket?.remoteAddress) ||
    "127.0.0.1"
  );
}

export function flaskProxyHeaders(
  req: FlaskProxyRequest,
  initial: Record<string, string> = {}
): Record<string, string> {
  const headers = { ...initial };
  // Always overwrite; an inbound X-Zspan-Client-Ip is never forwarded.
  headers[ZSPAN_CLIENT_IP_HEADER] = clientIpForFlask(req);
  return headers;
}

// ── Session-104: OAuth origin-host resolution ─────────────────────────
//
// Railway's ingress rewrites X-Forwarded-Host between the CF Pages
// Function and Express to the internal `z-span-production.up.railway.app`
// hostname. To let Flask's compute_redirect_uri() see the true
// browser-facing hostname (zspan.org / operator.zspan.org / lab.zspan.org),
// the Pages Function synthesizes a non-standard companion pair
// (X-ZSPAN-Origin-Host + X-ZSPAN-Origin-Proto) that Railway leaves
// untouched. This helper reads the companion pair FIRST — with an
// allowlist so an attacker-controlled value can't steer OAuth to an
// unregistered host — then falls back to X-Forwarded-Host, then to
// req.headers.host, then to the caller-supplied default.

/** Hostnames Flask's compute_redirect_uri knows how to resolve. Anything
 *  outside this set is treated as untrusted at every host-header rung. */
const KNOWN_ORIGIN_HOSTS: ReadonlySet<string> = new Set([
  "zspan.org",
  "operator.zspan.org",
  "lab.zspan.org",
  "localhost:3000",
  "127.0.0.1:3000",
]);

const KNOWN_ORIGIN_PROTOS: ReadonlySet<string> = new Set(["http", "https"]);

function firstString(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (Array.isArray(value) && typeof value[0] === "string") return value[0].trim();
  return "";
}

/**
 * Resolve the browser-facing host + protocol Express should stamp on the
 * X-Forwarded-Host/Proto headers it sends to Flask for OAuth routes.
 *
 * Precedence (each layer only wins if its value validates):
 *   1. X-ZSPAN-Origin-Host / X-ZSPAN-Origin-Proto — set only by the CF
 *      Pages Function from Request.url; allowlisted here in case Railway
 *      or anyone else ever injects one.
 *   2. X-Forwarded-Host / X-Forwarded-Proto — Railway may have rewritten
 *      the host to an internal name, but this is the historical fallback
 *      for setups without the CF Pages Function in front.
 *   3. req.headers.host — bare Host header (dev / direct-origin case).
 *   4. defaultHost / defaultProto — caller-supplied floor.
 */
export function pickAuthOriginHost(
  req: FlaskProxyRequest,
  defaultHost: string,
  defaultProto: string = "http",
): { host: string; proto: string } {
  const originHost = firstString(
    req.headers["x-zspan-origin-host"]
  ).toLowerCase();
  const originProto = firstString(req.headers["x-zspan-origin-proto"]).toLowerCase();
  const trustedOriginHost = originHost && KNOWN_ORIGIN_HOSTS.has(originHost) ? originHost : "";
  const trustedOriginProto =
    trustedOriginHost &&
    originProto &&
    KNOWN_ORIGIN_PROTOS.has(originProto)
      ? originProto
      : "";

  const xfh = firstString(req.headers["x-forwarded-host"]).toLowerCase();
  const xfp = firstString(req.headers["x-forwarded-proto"]).toLowerCase();
  const trustedXfh = xfh && KNOWN_ORIGIN_HOSTS.has(xfh) ? xfh : "";
  const trustedXfp =
    trustedXfh && xfp && KNOWN_ORIGIN_PROTOS.has(xfp) ? xfp : "";

  const hostHeader = firstString(req.headers.host).toLowerCase();
  const trustedHostHeader =
    hostHeader && KNOWN_ORIGIN_HOSTS.has(hostHeader) ? hostHeader : "";

  const host =
    trustedOriginHost || trustedXfh || trustedHostHeader || defaultHost;
  const proto = trustedOriginProto || trustedXfp || defaultProto;

  return { host, proto };
}

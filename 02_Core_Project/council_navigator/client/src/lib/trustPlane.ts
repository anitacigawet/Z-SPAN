export type TrustPlane = "public" | "operator" | "dev";

const PUBLIC_HOSTS = new Set(["zspan.org", "www.zspan.org"]);
const DEV_HOSTS = new Set(["localhost", "127.0.0.1"]);
const OPERATOR_HOST = "operator.zspan.org";
const DEV_OVERRIDE_KEY = "zspanPlaneOverride";

function classifyHostname(hostname: string): TrustPlane {
  const normalized = hostname.trim().toLowerCase().replace(/\.$/, "");

  if (PUBLIC_HOSTS.has(normalized) || normalized.endsWith(".pages.dev")) {
    return "public";
  }
  if (normalized === OPERATOR_HOST) return "operator";
  if (
    DEV_HOSTS.has(normalized) ||
    normalized === "local" ||
    normalized.endsWith(".local")
  ) {
    return "dev";
  }

  // Unknown hosts fail closed to the least-privileged renderer.
  return "public";
}

function hasPublicDevOverride(): boolean {
  try {
    const params = new URLSearchParams(window.location.search);
    if (params.get("__plane") === "public") return true;
  } catch {
    // An unavailable URL API must not broaden access.
  }

  try {
    return window.localStorage.getItem(DEV_OVERRIDE_KEY) === "public";
  } catch {
    return false;
  }
}

/**
 * Resolve the renderer's trust plane from the current hostname.
 *
 * Local development may force the public experience with
 * `?__plane=public` or `localStorage.zspanPlaneOverride = "public"`.
 * That override is considered only after the hostname has independently
 * resolved to `dev`; it is ignored on every deployed or unknown hostname.
 */
export function getTrustPlane(): TrustPlane {
  if (typeof window === "undefined" || !window.location) return "public";

  const hostnamePlane = classifyHostname(window.location.hostname);
  if (hostnamePlane === "dev" && hasPublicDevOverride()) return "public";
  return hostnamePlane;
}

export function getApiBase(plane: TrustPlane): string {
  return plane === "public" ? "/public-api" : "/api";
}

export function isPublicPlane(): boolean {
  return getTrustPlane() === "public";
}

export function isOperatorSurfaceAllowed(): boolean {
  const plane = getTrustPlane();
  return plane === "operator" || plane === "dev";
}

/**
 * D-180 public-host admission contract.
 *
 * Pages functions may proxy a request only after it matches one of these
 * method/path/query entries.  Flask DTO projections live in
 * parsers/public_dto.py; neither boundary has an implicit fallback.
 */

export type PublicEdgeRoute = Readonly<{
  method: "GET";
  pathPattern: string;
  allowedQueryKeys: readonly string[];
}>;

export const PUBLIC_EDGE_ROUTES: readonly PublicEdgeRoute[] = [
  {
    method: "GET",
    pathPattern: "/public-api/channels/tree",
    allowedQueryKeys: ["state"],
  },
  {
    method: "GET",
    pathPattern: "/public-api/catalog/contribute/:source_id.md",
    allowedQueryKeys: ["state"],
  },
  {
    method: "GET",
    pathPattern: "/public-api/cities/:city/years",
    allowedQueryKeys: [],
  },
  {
    method: "GET",
    pathPattern: "/public-api/cities/:city/meetings",
    allowedQueryKeys: ["year"],
  },
  {
    method: "GET",
    pathPattern: "/public-api/calendar/county/:county/meetings",
    allowedQueryKeys: ["state"],
  },
  {
    method: "GET",
    pathPattern: "/public-api/calendar/search",
    allowedQueryKeys: [
      "q",
      "county",
      "state",
      "date_from",
      "date_to",
      "limit",
      "offset",
    ],
  },
  {
    method: "GET",
    pathPattern: "/public-api/calendar/stats",
    allowedQueryKeys: [],
  },
  { method: "GET", pathPattern: "/public-api/health", allowedQueryKeys: [] },
  {
    method: "GET",
    pathPattern: "/public-api/broadcasts/:public_id",
    allowedQueryKeys: [],
  },
  {
    method: "GET",
    pathPattern: "/public-api/broadcasts/:public_id/sim-queries",
    allowedQueryKeys: [],
  },
  {
    method: "GET",
    pathPattern: "/public-api/broadcasts/:public_id/sidecars/:type",
    allowedQueryKeys: [],
  },
  {
    method: "GET",
    pathPattern: "/public-api/broadcasts/:public_id/citation",
    allowedQueryKeys: [],
  },
  {
    method: "GET",
    pathPattern: "/public-api/cast/:city",
    allowedQueryKeys: [],
  },
  {
    method: "GET",
    pathPattern: "/public-api/cast/:city/:seat_id",
    allowedQueryKeys: [],
  },
  { method: "GET", pathPattern: "/public-api/guide", allowedQueryKeys: [] },
  { method: "GET", pathPattern: "/public-api/travelers", allowedQueryKeys: [] },
  {
    method: "GET",
    pathPattern: "/public-api/youtube/embed-check",
    allowedQueryKeys: ["video_id"],
  },
  {
    method: "GET",
    pathPattern: "/v1/catalog/jurisdictions",
    allowedQueryKeys: [],
  },
  {
    method: "GET",
    pathPattern: "/v1/catalog/meetings",
    allowedQueryKeys: ["state", "county", "city", "year", "cursor"],
  },
  {
    method: "GET",
    pathPattern: "/v1/catalog/meetings/:public_id",
    allowedQueryKeys: [],
  },
];

export type FunctionTrustPlane = "operator" | "public" | "shared";

/**
 * Every current Pages entry point is deliberately classified.  The inventory
 * test reads functions/ from disk and requires this map to remain exhaustive.
 */
export const FUNCTION_ENTRYPOINT_TRUST_PLANE: Readonly<
  Record<string, FunctionTrustPlane>
> = {
  "_middleware.ts": "shared",
  "api/[[catchall]].ts": "operator",
  "api/me.ts": "operator",
  "public-api/[[catchall]].ts": "public",
  "v1/[[catchall]].ts": "public",
};

function matchesPathPattern(pathPattern: string, pathname: string): boolean {
  const patternSegments = pathPattern.split("/");
  const pathSegments = pathname.split("/");
  if (patternSegments.length !== pathSegments.length) return false;

  return patternSegments.every((segment, index) => {
    if (segment.startsWith(":")) return pathSegments[index].length > 0;
    return segment === pathSegments[index];
  });
}

export function matchPublicRoute(
  method: string,
  pathname: string,
  searchParams: URLSearchParams
): PublicEdgeRoute | null {
  for (const route of PUBLIC_EDGE_ROUTES) {
    if (method !== route.method) continue;
    if (!matchesPathPattern(route.pathPattern, pathname)) continue;
    const allowed = new Set(route.allowedQueryKeys);
    for (const key of searchParams.keys()) {
      if (!allowed.has(key)) return null;
    }
    return route;
  }
  return null;
}

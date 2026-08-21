import {
  PUBLIC_EDGE_ROUTES,
  matchPublicRoute,
} from "../publicSurfaceContract";
import {
  type EdgeProxyEnv,
  jsonError,
  proxyToBackend,
} from "../edgeProxy";

export const onRequest: PagesFunction<EdgeProxyEnv> = async (context) => {
  const incoming = new URL(context.request.url);
  const matched = matchPublicRoute(
    context.request.method,
    incoming.pathname,
    incoming.searchParams,
  );
  if (
    !matched ||
    !PUBLIC_EDGE_ROUTES.includes(matched) ||
    !matched.pathPattern.startsWith("/v1/")
  ) {
    return jsonError({ success: false, error: "not found" }, 404);
  }

  const query = new URLSearchParams();
  for (const key of matched.allowedQueryKeys) {
    for (const value of incoming.searchParams.getAll(key)) {
      query.append(key, value);
    }
  }
  const filteredQuery = query.toString();
  const suffix = filteredQuery ? `?${filteredQuery}` : "";
  return proxyToBackend(
    context.request,
    context.env,
    incoming.pathname + suffix,
    true,
  );
};

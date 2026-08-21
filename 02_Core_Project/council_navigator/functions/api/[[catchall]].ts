// Cloudflare Pages Function — fail-closed reverse proxy for /api/*.
// The public hostname admits only the app-OAuth routes listed below; the
// complete operator API remains restricted to the exact operator hostname.

import {
  type EdgeProxyEnv,
  jsonError,
  proxyToBackend,
  requestTrustPlane,
  resolveAccessEmail,
} from "../edgeProxy";

// D-179 boundary revision, operator-authorized 2026-07-27 — public sign-in;
// The exact official-client routes below are separately admitted.
export const PUBLIC_AUTH_ROUTES: readonly {
  method: string;
  pathname: string;
}[] = [
  { method: "GET", pathname: "/api/auth/google/login" },
  { method: "GET", pathname: "/api/auth/google/callback" },
  { method: "GET", pathname: "/api/auth/me" },
  { method: "POST", pathname: "/api/auth/logout" },
  { method: "POST", pathname: "/api/auth/password/register" },
  { method: "POST", pathname: "/api/auth/password/login" },
  { method: "POST", pathname: "/api/auth/password/forgot" },
  { method: "POST", pathname: "/api/auth/password/reset" },
];

// Exact official-client surface. These routes are reachable on the public
// hostname because the desktop client uses zspan.org as its flagship. Flask
// independently verifies the signed CLI handoff or opaque bearer token on
// every data-bearing call; no operator route is admitted by this list.
export const PUBLIC_CLI_ROUTES: readonly {
  method: string;
  pathname: string;
}[] = [
  { method: "GET", pathname: "/api/auth/cli/start" },
  { method: "GET", pathname: "/api/auth/cli/finish" },
  { method: "POST", pathname: "/api/auth/cli/finish" },
  { method: "GET", pathname: "/api/auth/cli/cancel" },
  { method: "POST", pathname: "/api/auth/cli/exchange" },
  { method: "POST", pathname: "/api/auth/cli/revoke" },
  { method: "GET", pathname: "/api/auth/cli/me" },
  { method: "POST", pathname: "/api/generations/register" },
  { method: "POST", pathname: "/api/contributions/submit" },
];

// Signed-in app features admitted on the public plane; Flask enforces
// cookie auth + the D-145 grant gate — 2026-07-27 Librarian request-access.
// Session-103 (product-slice2): /api/follows added so the follow-a-city
// primitive works on the public plane. Flask's `_require_user()` scopes
// every row by user.id from the session cookie; a signed-in visitor only
// sees + mutates their own follows.
export const PUBLIC_ACCOUNT_ROUTES: readonly {
  method: string;
  pathname: string;
}[] = [
  { method: "POST", pathname: "/api/librarian/request-access" },
  { method: "POST", pathname: "/api/invitations/status" },
  { method: "POST", pathname: "/api/invitations/redeem" },
  { method: "POST", pathname: "/api/byok/validate-key" },
  { method: "POST", pathname: "/api/byok/relay" },
  { method: "POST", pathname: "/api/byok/relay-stream" },
  { method: "GET", pathname: "/api/follows" },
  { method: "POST", pathname: "/api/follows" },
  { method: "DELETE", pathname: "/api/follows" },
  { method: "GET", pathname: "/api/workspace/receipts" },
];

// Bearer-token unsubscribe is intentionally public. GET is read-only so
// scanner prefetches cannot mutate preferences; POST verifies the signed token
// before disabling email for that token's user.
export const PUBLIC_UNSUBSCRIBE_ROUTES: readonly {
  method: string;
  pathname: string;
}[] = [
  { method: "GET", pathname: "/api/unsubscribe" },
  { method: "POST", pathname: "/api/unsubscribe" },
];

export const onRequest: PagesFunction<EdgeProxyEnv> = async (context) => {
  if (requestTrustPlane(context.request) !== "operator") {
    const incoming = new URL(context.request.url);
    const admitted = [
      ...PUBLIC_AUTH_ROUTES,
      ...PUBLIC_CLI_ROUTES,
      ...PUBLIC_ACCOUNT_ROUTES,
      ...PUBLIC_UNSUBSCRIBE_ROUTES,
    ].some(
      ({ method, pathname }) =>
        context.request.method === method && incoming.pathname === pathname,
    );
    if (admitted) {
      return proxyToBackend(
        context.request,
        context.env,
        incoming.pathname + incoming.search,
      );
    }
    return jsonError({ success: false, error: "not found" }, 404);
  }

  const incoming = new URL(context.request.url);

  // Service-token path for /api/sync/*. Recognizes the shared X-Sync-Token
  // header so the Mac's flagship_sync can push meeting data to Railway.
  // Flask independently validates the same header on the receiving end,
  // so this is defense-in-depth, not a substitute. If the header is
  // missing or does not match, the request falls through to the existing
  // owner-identity resolution so the authenticated browser continues to
  // work on /api/sync/* as well.
  if (incoming.pathname.startsWith("/api/sync/")) {
    const expected = (context.env.ZSPAN_SYNC_TOKEN || "").trim();
    const provided = (
      context.request.headers.get("x-sync-token") || ""
    ).trim();
    if (expected && provided && provided === expected) {
      return proxyToBackend(
        context.request,
        context.env,
        incoming.pathname + incoming.search,
      );
    }
  }

  const owner = (context.env.OWNER_EMAIL || "").trim().toLowerCase();
  if (!owner) {
    return jsonError(
      {
        success: false,
        error: "flagship misconfigured: OWNER_EMAIL not set on Pages Function",
      },
      503,
    );
  }

  const email = await resolveAccessEmail(context.request.headers, context.env);
  if (!email) {
    return jsonError(
      { success: false, error: "unauthenticated (no Cf-Access identity)" },
      401,
    );
  }
  if (email.toLowerCase() !== owner) {
    return jsonError(
      {
        success: false,
        error: "forbidden: this surface is restricted to the flagship owner",
      },
      403,
    );
  }

  return proxyToBackend(
    context.request,
    context.env,
    incoming.pathname + incoming.search,
  );
};

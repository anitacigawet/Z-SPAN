// Cloudflare Pages Function — exposes the authenticated user's identity to
// the React frontend so it can decide what UI to render.
//
// Cloudflare Access forwards user identity on every authenticated request
// in one of two shapes:
//   1. `cf-access-authenticated-user-email` header (the legacy / friendly
//      form). Some Pages configurations forward this directly.
//   2. `cf-access-jwt-assertion` header — a signed JWT containing the
//      user's identity in its payload claims (email, sub, identity_nonce).
//
// Our Pages config falls into case (2): the JWT is forwarded but the
// friendly header is not. We try the friendly header first for
// resilience, then fall back to parsing the JWT payload. Signature
// verification is delegated to Cloudflare Access at the edge — if a
// JWT reaches this Function with a signed assertion, Access already
// validated it upstream.
//
// We read the email, compare it against the OWNER_EMAIL env var, and
// return a small JSON shape the frontend uses to gate owner-only
// affordances (operator terminal, parser dashboard, settings, vocab
// inbox, etc.).
//
// Required env var (set in Cloudflare Pages dashboard):
//   OWNER_EMAIL  — the email that should see the owner UI. Comparison is
//                  case-insensitive against the Cf-Access-injected email.
//
// Local dev: when no Cf-Access identity is present (running `pnpm dev`
// against a local Flask), this endpoint will not be hit at all — the
// Pages Function only runs on the deployed Pages instance. The React
// hook in `lib/useFlagshipUser.ts` treats a 404 / network error as
// "local dev mode" and defaults to owner. James's local workflow stays
// unchanged.
//
// See DECISIONS.md § D-049 (dual-track flagship + self-host), § D-051
// (flagship RBAC + Cf-Access JWT decoding), and the 2026-05-22
// stuffs.txt notes for the RBAC requirement origin.

import {
  jsonError,
  requestTrustPlane,
  resolveAccessEmail,
} from "../edgeProxy";

interface Env {
  OWNER_EMAIL?: string;
  ACCESS_TEAM_DOMAIN?: string;
  ACCESS_APP_AUD?: string;
}

interface MeResponse {
  authenticated: boolean;
  email: string | null;
  isOwner: boolean;
  // When `authenticated` is true but `isOwner` is false, the user is an
  // allowlisted viewer — they passed Cloudflare Access but they're not
  // the owner. The frontend shows them the read-only channel browser.
  reason?: string;
}

export const onRequest: PagesFunction<Env> = async (context) => {
  if (requestTrustPlane(context.request) !== "operator") {
    return jsonError({ success: false, error: "not found" }, 404);
  }

  const ownerEnv = context.env.OWNER_EMAIL || '';
  const owner = ownerEnv.trim().toLowerCase();

  const email = await resolveAccessEmail(context.request.headers, context.env);

  if (!email) {
    const body: MeResponse = {
      authenticated: false,
      email: null,
      isOwner: false,
      reason: 'no-access-identity',
    };
    return jsonResponse(body, 200);
  }

  if (!owner) {
    // OWNER_EMAIL not configured — fail closed but tell the frontend why.
    // James will see "viewer mode" rendering even on the owner account and
    // know to set the env var.
    const body: MeResponse = {
      authenticated: true,
      email,
      isOwner: false,
      reason: 'owner-email-not-configured',
    };
    return jsonResponse(body, 200);
  }

  const isOwner = email.toLowerCase() === owner;
  const body: MeResponse = {
    authenticated: true,
    email,
    isOwner,
  };
  return jsonResponse(body, 200);
};

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    },
  });
}

// useCurrentUser — the light-account principal hook.
//
// Reads /api/auth/me, which returns whichever Google-OAuth-authenticated
// user the request's `zspan_session` cookie identifies (or {authenticated:
// false} for anonymous visitors).
//
// Single source of truth for "who is the viewer, and are they the
// operator." Returns the Google-OAuth principal (the public reader who
// signed in via Google) plus `isOwner` — true when their email matches
// the server's owner_email. Per V1-Polish-2 (2026-06-14) operator gating
// (OwnerOnly + the owner-only view gate) keys off THIS hook, not the
// retired `useFlagshipUser`/Cf-Access hook. On the flagship, Cloudflare
// Access remains a server-side perimeter (functions/api/[[catchall]].ts
// deny-list) — but the UI's notion of "operator" is now the Google owner.
//
// Per ACCOUNT_SYSTEM_SPEC.md chunk 2 + V1-Polish-2.

import { useCallback, useEffect, useState } from "react";

export interface CurrentUserFollow {
 target_type: "city" | "county" | "meeting";
 target_key: string;
 created_at: string;
}

export interface CurrentUser {
 user_id: number;
 email: string;
 display_name: string | null;
 avatar_url: string | null;
 role: "light" | "creator" | "verified-creator";
 // True when this signed-in Google account is the configured operator
 // (email matches owner_email server-side). The single source of truth
 // for "is the current viewer the operator" since V1-Polish-2 — the
 // operator view requires signing in via Google as the owner account.
 is_owner: boolean;
 // V1.5-OperatorSearch-1 — strictly wider than is_owner: includes
 // owners + secondary test accounts on the operator_search_allowlist.
 // Gates ONLY the operator-search affordance; other owner-only
 // surfaces continue to gate strictly on is_owner.
 is_operator_search_principal: boolean;
 librarian_access: "none" | "requested" | "granted" | "banned";
 follows: CurrentUserFollow[];
 // Session-105 — per-city topic decoration prefs, keyed by canonical
 // city name (same casing as `follows[].target_key` for city rows).
 // Empty object when the user has enabled no per-city topics. NEVER
 // affects which meetings notify — enrichment only.
 city_topics: Record<string, string[]>;
}

export interface CurrentUserState {
 user: CurrentUser | null;
 loading: boolean;
 // Server-controlled Google sign-in maintenance switch. Defaults true
 // when an older API response omits the field.
 signInEnabled: boolean;
 // Convenience: true when the signed-in account is the operator/owner.
 // Equivalent to `!!user?.is_owner`. While `loading`, this is false —
 // operator surfaces must hide until the principal is confirmed so they
 // never flash to an anonymous viewer.
 isOwner: boolean;
 // V1.5-OperatorSearch-1 — strictly wider than `isOwner`. Gates the
 // operator-search dropdown affordance + modal only.
 isOperatorSearchPrincipal: boolean;
 // Distinct from `user === null` — `null` after a successful fetch
 // means "anonymous"; `null` while loading is "we don't know yet".
 // Components that render different UI for "signed in" vs. "signed
 // out" should keep both branches collapsed during `loading` to avoid
 // flicker.
 refresh: () => void;
}

type CachedCurrentUser = {
 user: CurrentUser | null;
 signInEnabled: boolean;
};

const INITIAL: CachedCurrentUser & { loading: boolean } = {
 user: null,
 loading: true,
 signInEnabled: true,
};

// Module-level cache + inflight dedup so multiple hook consumers don't
// each refetch. The /me endpoint is cheap but not free.
let cached: CachedCurrentUser | null = null;
let inflight: Promise<CachedCurrentUser> | null = null;

async function fetchMe(): Promise<CachedCurrentUser> {
 if (inflight) return inflight;

 inflight = (async () => {
 try {
 const res = await fetch("/api/auth/me", {
 method: "GET",
 credentials: "include",
 cache: "no-store",
 });
 if (!res.ok) {
 cached = { user: null, signInEnabled: true };
 return cached;
 }
 const body = (await res.json()) as {
 authenticated: boolean;
 user: CurrentUser | null;
 sign_in_enabled?: boolean;
 };
 const user = body.authenticated && body.user
 ? { ...body.user, city_topics: body.user.city_topics ?? {} }
 : null;
 cached = {
 user,
 signInEnabled: body.sign_in_enabled !== false,
 };
 return cached;
 } catch {
 cached = { user: null, signInEnabled: true };
 return cached;
 } finally {
 inflight = null;
 }
 })();

 return inflight;
}

export function invalidateCurrentUserCache(): void {
 cached = null;
}

export function useCurrentUser(): CurrentUserState {
 const [state, setState] = useState(() => {
 if (cached) return { ...cached, loading: false };
 return INITIAL;
 });

 const refresh = useCallback(() => {
 invalidateCurrentUserCache();
 setState((s) => ({ ...s, loading: true }));
 fetchMe().then((r) => setState({ ...r, loading: false }));
 }, []);

 useEffect(() => {
 if (cached) {
 setState({ ...cached, loading: false });
 return;
 }
 let active = true;
 fetchMe().then((r) => {
 if (active) setState({ ...r, loading: false });
 });
 return () => {
 active = false;
 };
 }, []);

 return {
 ...state,
 isOwner: !!state.user?.is_owner,
 isOperatorSearchPrincipal:
 !!state.user?.is_operator_search_principal,
 refresh,
 };
}

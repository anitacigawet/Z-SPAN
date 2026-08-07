// OwnerOnly — inline gate that renders children only when the current
// viewer is the operator: signed in via Google as the configured owner
// account (useCurrentUser().isOwner).
//
// Since V1-Polish-2 (2026-06-14) the operator identity is the Google-OAuth
// principal whose email matches the server's owner_email — NOT the old
// Cf-Access / local-dev-auto-owner default (the retired useFlagshipUser
// hook). Operator surfaces now require signing in as the owner everywhere,
// including local dev; anonymous + every non-owner viewer gets the clean
// public surface. On the flagship, Cloudflare Access stays as the
// server-side perimeter (functions/api/[[catchall]].ts deny-list).
//
// Always hides while the principal is still loading so operator
// affordances never flash to an anonymous viewer.
//
// Pairs with the owner-only view gate in App.tsx for view-level gating.

import { ReactNode } from 'react';
import { useCurrentUser } from '../hooks/useCurrentUser';

interface OwnerOnlyProps {
 children: ReactNode;
 // Optional fallback to render in the non-owner case (e.g., a viewer-mode
 // empty-state, a "this is an owner-only surface" placeholder). Default
 // is null (silent hide).
 fallback?: ReactNode;
 // Retained for API compatibility with existing call sites. OwnerOnly now
 // always hides while loading (the safe default that prevents operator-UI
 // flash to anonymous viewers), so this flag is a no-op.
 hideWhileLoading?: boolean;
}

export function OwnerOnly({ children, fallback = null }: OwnerOnlyProps) {
 const { isOwner, loading } = useCurrentUser();

 if (loading || !isOwner) {
 return <>{fallback}</>;
 }
 return <>{children}</>;
}

// Convenience inverse for the rare "show only to viewers / non-owners" case.
export function ViewerOnly({
 children,
 fallback = null,
}: {
 children: ReactNode;
 fallback?: ReactNode;
}) {
 const { isOwner, loading } = useCurrentUser();
 if (loading || isOwner) {
 return <>{fallback}</>;
 }
 return <>{children}</>;
}

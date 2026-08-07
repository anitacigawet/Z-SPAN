/**
 * SignInPill — the light-account auth affordance.
 *
 * Anonymous visitor: renders a "Sign in with Google" button that hits
 * `/api/auth/google/login?next=<current path>` (the Flask endpoint 302s
 * to Google's consent screen, then back to /api/auth/google/callback,
 * then to the original path).
 *
 * Signed-in user: renders the display name + a small dropdown with a
 * "Sign out" action.
 *
 * Per ACCOUNT_SYSTEM_SPEC.md chunk 2. The button + hook just need to
 * EXIST for chunk 2's acceptance gate; richer integration (where the
 * pill appears on which surfaces) lands with chunk 3 and beyond.
 */
import { useEffect, useRef, useState, type ReactElement } from "react";
import { createPortal } from "react-dom";

import {
 invalidateCurrentUserCache,
 useCurrentUser,
} from "../hooks/useCurrentUser";
import {
 SIGNIN_PILL_SHIMMER_EVENT,
 SignInPillShimmer,
} from "./SignInPillShimmer";
import { firstNameForChip } from "../utils/userDisplayName";

function buildLoginHref(): string {
 if (typeof window === "undefined") return "/api/auth/google/login";
 const next = `${window.location.pathname}${window.location.search}`;
 return `/api/auth/google/login?next=${encodeURIComponent(next || "/")}`;
}

async function signOut(): Promise<void> {
 try {
 await fetch("/api/auth/logout", {
 method: "POST",
 credentials: "include",
 headers: { "Content-Type": "application/json" },
 });
 } catch {
 /* ignore — we'll still wipe the local cache + reload */
 }
 invalidateCurrentUserCache();
 if (typeof window !== "undefined") {
 window.location.reload();
 }
}

interface SignInPillProps {
 /** Optional SPA navigation callback. When provided, the Following
 * menu item uses the in-app router; otherwise it falls back to a
 * full-reload `<a href>`. */
 onNavigate?: (view: string, params?: any) => void;
 /** Layout mode. Default `standalone-fixed` renders the pill wrapped
 * in a `fixed right-5 top-1.5` container portaled to document.body
 * (used on views without a TopBar so the pill still floats at the
 * viewport corner). `inline` renders the button/menu with no
 * positioning wrapper — for embedding inside the TopBar's own flex
 * flow so the pill can't overlap its neighbors. Session-31
 * (2026-07-04) — operator caught the fixed pill overlapping the
 * new OPERATOR dropdown; inline placement inside TopBar is the fix. */
 layout?: "standalone-fixed" | "inline";
}

export function SignInPill({ onNavigate, layout = "standalone-fixed" }: SignInPillProps = {}): ReactElement | null {
 const { user, loading, signInEnabled } = useCurrentUser();
 const [menuOpen, setMenuOpen] = useState(false);
 const menuRef = useRef<HTMLDivElement | null>(null);

 // Shimmer plumbing — listen for the toast's dispatched event, measure
 // the pill's bounding rect, render the shimmer overlay once, then let
 // it unmount itself when the lap finishes. The ref attaches to the
 // anonymous <a> below; signed-in users skip the shimmer entirely.
 const anchorRef = useRef<HTMLAnchorElement | null>(null);
 const [shimmerRect, setShimmerRect] = useState<DOMRect | null>(null);

 useEffect(() => {
 const handler = () => {
 if (anchorRef.current) {
 setShimmerRect(anchorRef.current.getBoundingClientRect());
 }
 };
 window.addEventListener(SIGNIN_PILL_SHIMMER_EVENT, handler);
 return () => window.removeEventListener(SIGNIN_PILL_SHIMMER_EVENT, handler);
 }, []);

 useEffect(() => {
 if (!menuOpen) return;
 const onClick = (e: MouseEvent) => {
 if (
 menuRef.current &&
 !menuRef.current.contains(e.target as Node)
 ) {
 setMenuOpen(false);
 }
 };
 document.addEventListener("mousedown", onClick);
 return () => document.removeEventListener("mousedown", onClick);
 }, [menuOpen]);

 if (loading) {
 return null;
 }
 if (!user && !signInEnabled) {
 return null;
 }

 // Wrapper handles the fixed-position anchoring on views without a
 // TopBar. When embedded IN the TopBar (layout === "inline"), no
 // wrapper is applied — the pill sits in the TopBar's flex flow and
 // shares layout space with its neighbors.
 const wrapClass = "fixed right-5 top-1.5 z-[60]";
 const inline = layout === "inline";

 const anonymousBody = (
 <>
 <a
 ref={anchorRef}
 href={buildLoginHref()}
 className="inline-flex items-center gap-2 whitespace-nowrap rounded-full border border-[var(--line)] bg-[var(--surface)]/40 pl-3 pr-3 sm:pr-3.5 py-1.5 text-[12px] font-medium text-foreground/80 backdrop-blur transition-colors hover:border-[var(--line-strong)] hover:bg-[var(--surface)]/70 hover:text-white"
 aria-label="Sign in with Google"
 >
 <span aria-hidden="true">
 {/* Google "G" mark — inline so we don't ship an asset for the
 single-pixel button. Public-domain rendering of the
 standard 4-color G. */}
 <svg
 width="14"
 height="14"
 viewBox="0 0 18 18"
 xmlns="http://www.w3.org/2000/svg"
 >
 <path
 fill="#4285F4"
 d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844a4.14 4.14 0 0 1-1.796 2.716v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"
 />
 <path
 fill="#34A853"
 d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"
 />
 <path
 fill="#FBBC05"
 d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"
 />
 <path
 fill="#EA4335"
 d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"
 />
 </svg>
 </span>
 {/* Session-31 mobile fix (2026-07-04) — the full label
 overflowed at 375px iPhone width and wrapped onto 3 lines,
 overlapping the search mic. `hidden sm:inline` collapses the
 text at ≤640px so the pill becomes G-icon-only there;
 `sm:inline` restores the label at tablet+. */}
 <span className="hidden sm:inline">Sign in with Google</span>
 <span className="sm:hidden">Sign in</span>
 </a>
 {shimmerRect && (
 <SignInPillShimmer
 rect={shimmerRect}
 onComplete={() => setShimmerRect(null)}
 />
 )}
 </>
 );

 if (!user) {
 if (inline) return anonymousBody;
 const wrapped = <div className={wrapClass}>{anonymousBody}</div>;
 if (typeof document !== "undefined") {
 return createPortal(wrapped, document.body);
 }
 return wrapped;
 }

 const chipName = firstNameForChip(user.display_name, user.email);
 const signedInBody = (
 <div ref={menuRef} className="relative">
 <button
 type="button"
 onClick={() => setMenuOpen((o) => !o)}
 className="inline-flex items-center gap-2 whitespace-nowrap rounded-full border border-[var(--line)] bg-[var(--surface)]/40 pl-1 pr-1 sm:pl-3 sm:pr-3.5 py-1 sm:py-1.5 text-[12px] font-medium text-foreground/80 backdrop-blur transition-colors hover:border-[var(--line-strong)] hover:bg-[var(--surface)]/70 hover:text-white"
 aria-haspopup="menu"
 aria-expanded={menuOpen}
 >
 {user.avatar_url ? (
 <img
 src={user.avatar_url}
 alt=""
 className="h-5 w-5 rounded-full"
 referrerPolicy="no-referrer"
 />
 ) : (
 <span className="flex h-5 w-5 items-center justify-center rounded-full bg-white/20 text-[10px] font-semibold">
 {chipName.slice(0, 1).toUpperCase()}
 </span>
 )}
 {/* Session-31 mobile fix (2026-07-04) — display name / email
 label hides at ≤640px (avatar-only mode) so the pill fits
 next to the OPERATOR dropdown at iPhone width. Owner still
 reaches the dropdown by tapping the avatar; name shows again
 at tablet+. */}
 <span className="hidden sm:inline max-w-[12rem] truncate">
 {chipName}
 </span>
 </button>
 {menuOpen && (
 <div
 role="menu"
 className="absolute right-0 z-50 mt-2 min-w-[14rem] rounded-lg border border-white/10 bg-zinc-900/95 p-1 shadow-xl backdrop-blur"
 >
 <div className="px-3 py-2 text-[11px] text-white/50">
 Signed in as
 <div className="truncate text-xs text-white/80">{user.email}</div>
 <div className="mt-1 text-[10px] uppercase tracking-wider text-white/40">
 {user.role}
 </div>
 </div>
 {user.is_owner && (onNavigate ? (
 <button
 type="button"
 role="menuitem"
 className="block w-full rounded-md px-3 py-1.5 text-left text-xs text-white/90 transition hover:bg-white/10"
 onClick={() => {
 setMenuOpen(false);
 onNavigate("following");
 }}
 >
 Following ({user.follows?.length ?? 0})
 </button>
 ) : (
 <a
 href="/?view=following"
 role="menuitem"
 className="block w-full rounded-md px-3 py-1.5 text-left text-xs text-white/90 transition hover:bg-white/10"
 onClick={() => setMenuOpen(false)}
 >
 Following ({user.follows?.length ?? 0})
 </a>
 ))}
 {/* Session-104: citizen settings now live alongside the owner-only
 configuration section, so every signed-in account can enter. */}
 {onNavigate ? (
 <button
 type="button"
 role="menuitem"
 className="block w-full rounded-md px-3 py-1.5 text-left text-xs text-white/90 transition hover:bg-white/10"
 onClick={() => {
 setMenuOpen(false);
 onNavigate("settings");
 }}
 >
 Settings
 </button>
 ) : (
 <a
 href="/?view=settings"
 role="menuitem"
 className="block w-full rounded-md px-3 py-1.5 text-left text-xs text-white/90 transition hover:bg-white/10"
 onClick={() => setMenuOpen(false)}
 >
 Settings
 </a>
 )}
 <button
 type="button"
 role="menuitem"
 onClick={() => void signOut()}
 className="block w-full rounded-md px-3 py-1.5 text-left text-xs text-white/90 transition hover:bg-white/10"
 >
 Sign out
 </button>
 </div>
 )}
 </div>
 );

 if (inline) return signedInBody;
 const wrapped = <div className={wrapClass}>{signedInBody}</div>;
 if (typeof document !== "undefined") {
 return createPortal(wrapped, document.body);
 }
 return wrapped;
}

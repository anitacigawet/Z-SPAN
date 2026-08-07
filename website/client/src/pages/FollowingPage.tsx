/**
 * FollowingPage — the signed-in user's followed cities / counties /
 * topics / meetings, grouped by type with inline unfollow.
 *
 * Per ACCOUNT_SYSTEM_SPEC chunk 3.
 *
 * Routing:
 * - Anonymous → SignInPrompt link to /api/auth/google/login?next=/?view=following
 * - Signed-in + zero follows → empty-state with hint to follow from
 * the Channels page
 * - Signed-in + follows → grouped lists; meetings have an "Open"
 * link that navigates to the BroadcastPage; cities/counties show
 * a "Browse Channels" link as the entry point (full deep-nav
 * deferred — county→city lookup would need taxonomy export).
 */
import type { ReactElement } from "react";

import { useCurrentUser } from "../hooks/useCurrentUser";
import { CreatorPromotionBanner } from "../components/CreatorPromotionBanner";
import { FollowingList } from "../components/FollowingList";

interface FollowingPageProps {
 onNavigate: (view: string, params?: any) => void;
}

function buildSignInHref(): string {
 if (typeof window === "undefined") return "/api/auth/google/login?next=%2F";
 const next = `${window.location.pathname}${window.location.search}` || "/";
 return `/api/auth/google/login?next=${encodeURIComponent(next)}`;
}

export default function FollowingPage({
 onNavigate,
}: FollowingPageProps): ReactElement {
 const { user, loading: userLoading } = useCurrentUser();

 if (userLoading) {
 return (
 <div className="min-h-screen bg-background flex items-center justify-center">
 <div className="text-foreground/40 text-sm">Loading…</div>
 </div>
 );
 }

 if (!user) {
 return (
 <div className="min-h-screen bg-background px-6 py-16">
 <div className="mx-auto max-w-md text-center space-y-5">
 <h1 className="text-2xl font-light tracking-tight text-white">
 Sign in to see what you're following
 </h1>
 <p className="text-sm text-foreground/55 leading-relaxed">
 Follow cities, counties, and individual meetings to build a
 personalized view of what's happening in your civic feed.
 </p>
 <a
 href={buildSignInHref()}
 className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-4 py-2 text-sm font-medium text-white hover:border-white/40 hover:bg-white/10 transition"
 >
 Sign in with Google
 </a>
 </div>
 </div>
 );
 }

 return (
 <div className="min-h-screen bg-background px-6 py-12">
 <div className="mx-auto max-w-3xl">
 <CreatorPromotionBanner onNavigate={onNavigate} className="mb-6" />
 <header>
 <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/40 mb-2">
 Following
 </div>
 <h1 className="text-3xl font-light tracking-tight text-white">
 {user.display_name || user.email}
 </h1>
 </header>

 <FollowingList onNavigate={onNavigate} />
 </div>
 </div>
 );
}

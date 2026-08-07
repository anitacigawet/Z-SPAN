/**
 * FollowButton — toggleable follow affordance for a single target.
 *
 * States:
 * - Anonymous (or still-loading) → renders nothing. The persistent per-row
 * "Sign in to follow" nag was removed (V1-Polish-5); it duplicated the
 * global top-right Sign-in pill. Anonymous visitors are nudged once,
 * gently, by the BroadcastPage sign-in-benefits toast.
 * - Signed in + not following → "+ Follow" button.
 * - Signed in + following → "✓ Following" button (click to unfollow).
 *
 * Per ACCOUNT_SYSTEM_SPEC chunk 3 + V1-Polish-5.
 */
import { useCallback, useState, type ReactElement } from "react";

import { useCurrentUser } from "../hooks/useCurrentUser";
import { useFollows, type FollowTargetType } from "../hooks/useFollows";

interface FollowButtonProps {
 targetType: FollowTargetType;
 targetKey: string;
 /** Short label for the target — surfaces in the aria-label so screen
 * readers say "Follow Kingman" instead of "Follow city kingman". */
 targetLabel?: string;
 /** Layout variant. "pill" is the default chip used in lists; "ghost"
 * is a less-emphasized inline button for dense surfaces. */
 variant?: "pill" | "ghost";
 /** Optional className to compose with the variant base styles. */
 className?: string;
}

const BASE_PILL =
 "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition disabled:opacity-50";
const BASE_GHOST =
 "inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium transition disabled:opacity-50";

export function FollowButton({
 targetType,
 targetKey,
 targetLabel,
 variant = "pill",
 className = "",
}: FollowButtonProps): ReactElement | null {
 const { user, loading: userLoading } = useCurrentUser();
 const { isFollowing, follow, unfollow, loading: followsLoading } =
 useFollows();
 const [busy, setBusy] = useState(false);

 const following = isFollowing(targetType, targetKey);
 const disabled = busy || followsLoading;

 // Hooks must run unconditionally (Rules of Hooks) — keep useCallback
 // ABOVE the userLoading / !user early returns below. A prior version
 // declared it AFTER those returns, so a loading→signed-in transition
 // changed the rendered hook count and threw "Rendered more hooks than
 // during the previous render" the moment a user was actually signed in.
 const onClick = useCallback(
 async (e: React.MouseEvent) => {
 e.stopPropagation();
 e.preventDefault();
 if (disabled) return;
 setBusy(true);
 try {
 if (following) {
 await unfollow(targetType, targetKey);
 } else {
 await follow(targetType, targetKey);
 }
 } finally {
 setBusy(false);
 }
 },
 [disabled, following, follow, unfollow, targetType, targetKey],
 );

 // Anonymous + still-loading viewers get NO follow affordance. The
 // persistent per-row "Sign in to follow" nag was removed (V1-Polish-5) —
 // it duplicated the global top-right Sign-in pill. Anonymous visitors are
 // nudged once, gently, by the BroadcastPage sign-in-benefits toast.
 if (userLoading || !user) return null;

 const label = targetLabel ?? targetKey;
 const base = variant === "ghost" ? BASE_GHOST : BASE_PILL;

 const followingStyles =
 "border-emerald-400/30 bg-emerald-400/10 text-emerald-200 hover:border-emerald-400/60";
 const notFollowingStyles =
 "border-white/10 bg-white/0 text-white/70 hover:border-white/30 hover:text-white/95";

 return (
 <button
 type="button"
 onClick={onClick}
 disabled={disabled}
 aria-pressed={following}
 aria-label={following ? `Unfollow ${label}` : `Follow ${label}`}
 className={`${base} ${following ? followingStyles : notFollowingStyles} ${className}`}
 >
 <span aria-hidden="true">{following ? "✓" : "+"}</span>
 <span>{following ? "Following" : "Follow"}</span>
 </button>
 );
}

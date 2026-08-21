/**
 * CreatorPromotionBanner — operator-facing entry point for the Creator
 * Network signup surface.
 *
 * Per ACCOUNT_SYSTEM_SPEC chunk 7 + the D-095 redline: V0 keeps the
 * Creator Network operator-facing only (no public-facing entry point
 * until Prong A clears). This banner is OwnerOnly-wrapped so it
 * surfaces solely on the operator's session. Renders only when the
 * signed-in user has role='light' (already-creator users see
 * CreatorsLandingPage at /creators instead).
 *
 * Drop this component on any operator-side surface where the
 * "promote light → creator" affordance should be reachable — e.g.,
 * OperatorTerminal, FollowingPage when viewed by the operator, the
 * HQ lobby, etc.
 */
import { useCurrentUser } from "../hooks/useCurrentUser";
import { OwnerOnly } from "./OwnerOnly";

interface CreatorPromotionBannerProps {
  onNavigate: (view: string, params?: any) => void;
  className?: string;
}

export function CreatorPromotionBanner({
  onNavigate,
  className = "",
}: CreatorPromotionBannerProps) {
  const { user, loading } = useCurrentUser();

  // Don't render anything if we don't yet know who's signed in, if
  // nobody is signed in, or if the signed-in user is already a creator.
  if (loading || !user || user.role !== "light") return null;

  return (
    <OwnerOnly>
      <div
        className={`flex items-center justify-between gap-4 rounded-lg border border-white/10 bg-white/[0.03] px-4 py-3 ${className}`}
      >
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-wider text-foreground/45">
            Creator Network · operator entry
          </div>
          <div className="text-sm text-white mt-0.5">
            Promote this light account to creator
          </div>
          <div className="text-[11px] text-foreground/45 mt-1">
            Operator-only in V0. Opens the TOS + narrated-disclaimer signup wizard.
          </div>
        </div>
        <button
          type="button"
          onClick={() => onNavigate("creators")}
          className="inline-flex items-center gap-2 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3 py-1.5 text-xs font-medium text-emerald-100 hover:border-emerald-400/60 transition flex-shrink-0"
        >
          Open signup
        </button>
      </div>
    </OwnerOnly>
  );
}

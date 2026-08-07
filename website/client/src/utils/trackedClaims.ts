// Tracked Claims — shared types + status display config used by
// both the Cast page "Accountability" section and the City Ledger page.

export type TrackedClaimType =
 | "assurance"
 | "commitment"
 | "prediction"
 | "promise";

export type TrackedClaimStatus =
 | "active"
 | "fulfilled"
 | "broken"
 | "unclear"
 | "withdrawn";

export type TrackedClaimWordTiming = {
 word: string;
 start_ms: number;
 end_ms: number;
};

export interface TrackedClaim {
 id?: number;
 member_id?: number;
 meeting_id?: number;
 meeting_public_id?: string;
 meeting_date: string;
 meeting_title: string;
 meeting_video_url: string | null;
 video_url?: string | null;
 // Speaker fields — present on the City Ledger payload via JOIN to
 // council_members, absent on the per-member Cast payload (the member
 // is the page context).
 speaker_name?: string;
 seat_id?: string | null;
 speaker_role?: string | null;
 claim_type: string | null;
 claim_text: string;
 expected_outcome: string | null;
 time_horizon_months: number | null;
 topic_tags: string[];
 confidence: string | null;
 context: string | null;
 word_timings: TrackedClaimWordTiming[] | null;
 status: TrackedClaimStatus;
 status_updated_at: string | null;
 status_updated_by: string | null;
 status_evidence: string | null;
 extracted_at?: string;
}

// Status badge display — labels + foreground/border colors. The
// background is always transparent; border + text carry the signal.
// Colors are tuned for the dark Z-SPAN palette.
export const TRACKED_CLAIM_STATUS_DISPLAY: Record<
 TrackedClaimStatus,
 { label: string; fg: string; border: string }
> = {
 active: {
 label: "Active",
 fg: "#F2A91C",
 border: "rgba(242, 169, 28, 0.45)",
 },
 fulfilled: {
 label: "Fulfilled",
 fg: "#34D399",
 border: "rgba(52, 211, 153, 0.45)",
 },
 broken: {
 label: "Broken",
 fg: "#F87171",
 border: "rgba(248, 113, 113, 0.45)",
 },
 unclear: {
 label: "Unclear",
 fg: "#A78BFA",
 border: "rgba(167, 139, 250, 0.45)",
 },
 withdrawn: {
 label: "Withdrawn",
 fg: "#9CA3AF",
 border: "rgba(156, 163, 175, 0.45)",
 },
};

// Claim-type display — short label for the small chip alongside status.
export const TRACKED_CLAIM_TYPE_DISPLAY: Record<string, string> = {
 assurance: "Assurance",
 commitment: "Commitment",
 prediction: "Prediction",
 promise: "Promise",
};

// The marker color used everywhere a tracked claim's karaoke renders.
// Z-SPAN evidence-grade aesthetic per the marker decision.
export const TRACKED_CLAIM_MARKER_COLOR = "#F2A91C";

// Compute whether a claim is "aged past horizon" — still active and the
// extracted_at + time_horizon_months window has elapsed. Used by the
// Ledger page to surface the next-review feed. The backend also exposes
// this as a `?aged=true` filter, but the per-card display affordance
// uses this client-side check so card-level badges stay correct after
// a status flip without a refetch.
export function isAgedPastHorizon(claim: TrackedClaim): boolean {
 if (claim.status !== "active") return false;
 if (claim.time_horizon_months == null) return false;
 if (!claim.extracted_at) return false;
 const extracted = Date.parse(claim.extracted_at);
 if (Number.isNaN(extracted)) return false;
 const deadlineMs = extracted + claim.time_horizon_months * 30 * 24 * 3600 * 1000;
 return Date.now() > deadlineMs;
}

export function formatTimeHorizon(months: number | null): string {
 if (months == null) return "no horizon";
 if (months === 1) return "1 month";
 if (months < 12) return `${months} months`;
 if (months === 12) return "1 year";
 if (months % 12 === 0) return `${months / 12} years`;
 return `${months} months`;
}

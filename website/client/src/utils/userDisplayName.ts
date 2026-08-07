/**
 * Compact account-chip label derived from a stored Google profile.
 *
 * Full display names remain intact in the database and on roomy profile
 * surfaces. Top-bar chips use the first whitespace-delimited name so the
 * identity reads cleanly without truncating a surname into an ellipsis.
 */
export function firstNameForChip(
 displayName: string | null | undefined,
 email: string | null | undefined,
): string {
 const firstName = (displayName || "").trim().split(/\s+/)[0];
 return firstName || (email || "").trim() || "?";
}

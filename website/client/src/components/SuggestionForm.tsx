/**
 * SuggestionForm — login-gated user query against a processed episode.
 *
 * Per V1_PUBLIC_RELEASE_SPEC.md V1-UI-3 + the input_moderation
 * "suggestion_query" surface (max 500 chars, 50/day per user, no
 * URLs). Mount on BroadcastPage for processed-city meetings only.
 *
 * States:
 * - Anonymous → "Sign in to suggest a query" link to OAuth login.
 * - Signed in (idle) → textarea + char counter + submit.
 * - Submitting → spinner.
 * - Submitted-accepted → "Thanks — your query is in the queue."
 * - Submitted-flagged → same accepted UX (flagged means the
 * operator review queue catches it; user sees acceptance).
 * - Rejected → inline error with the moderation reason.
 */
import { useCallback, useMemo, useState, type ReactElement } from "react";

import { useCurrentUser } from "../hooks/useCurrentUser";

const QUERY_MAX_CHARS = 500;

interface SuggestionFormProps {
 meetingId: number;
 meetingTitle?: string;
 className?: string;
}

function buildSignInHref(): string {
 if (typeof window === "undefined") return "/api/auth/google/login";
 const next = `${window.location.pathname}${window.location.search}`;
 return `/api/auth/google/login?next=${encodeURIComponent(next || "/")}`;
}

type Status =
 | { kind: "idle" }
 | { kind: "submitting" }
 | { kind: "accepted"; flagged: boolean }
 | { kind: "rejected"; reason: string };

export function SuggestionForm({
 meetingId,
 meetingTitle,
 className = "",
}: SuggestionFormProps): ReactElement | null {
 const { user, loading } = useCurrentUser();
 const [query, setQuery] = useState("");
 const [status, setStatus] = useState<Status>({ kind: "idle" });

 const remaining = useMemo(
 () => Math.max(0, QUERY_MAX_CHARS - query.length),
 [query.length],
 );
 const overLimit = query.length > QUERY_MAX_CHARS;
 const submittable = query.trim().length > 0 && !overLimit;

 const submit = useCallback(async () => {
 setStatus({ kind: "submitting" });
 try {
 const res = await fetch("/api/suggestions", {
 method: "POST",
 credentials: "include",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify({ meeting_id: meetingId, query: query.trim() }),
 });
 const body = await res.json().catch(() => null);
 if (res.ok && body?.success) {
 setStatus({
 kind: "accepted",
 flagged: !!body.operator_review_needed,
 });
 setQuery("");
 return;
 }
 setStatus({
 kind: "rejected",
 reason:
 body?.reason || body?.error || `Server returned ${res.status}`,
 });
 } catch (err: any) {
 setStatus({
 kind: "rejected",
 reason: err?.message || "Network error",
 });
 }
 }, [meetingId, query]);

 // Hide while auth state is loading so the anonymous link doesn't
 // briefly flash for already-signed-in users.
 if (loading) return null;

 const heading = (
 <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/45 mb-2">
 Suggest a query
 {meetingTitle ? <span className="text-foreground/30"> · about this episode</span> : null}
 </div>
 );

 if (!user) {
 return (
 <div
 className={`rounded-lg border border-white/10 bg-white/[0.02] p-4 ${className}`}
 >
 {heading}
 <p className="text-sm text-foreground/65 leading-relaxed">
 Sign in to send a query about this episode. Submissions are reviewed
 before they're acted on.
 </p>
 <a
 href={buildSignInHref()}
 className="mt-3 inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3 py-1.5 text-xs font-medium text-white hover:border-white/40 hover:bg-white/10 transition"
 >
 Sign in with Google
 </a>
 </div>
 );
 }

 if (status.kind === "accepted") {
 return (
 <div
 className={`rounded-lg border border-emerald-400/30 bg-emerald-400/5 p-4 ${className}`}
 >
 {heading}
 <p className="text-sm text-emerald-100">
 Thanks — your query is in the queue.
 {status.flagged && (
 <span className="text-emerald-200/70">
 {" "}It was flagged for an extra operator review pass.
 </span>
 )}
 </p>
 <button
 type="button"
 onClick={() => setStatus({ kind: "idle" })}
 className="mt-3 text-[11px] text-foreground/55 hover:text-white transition"
 >
 Submit another
 </button>
 </div>
 );
 }

 const submitting = status.kind === "submitting";
 const rejectedReason = status.kind === "rejected" ? status.reason : null;

 return (
 <div
 className={`rounded-lg border border-white/10 bg-white/[0.02] p-4 ${className}`}
 >
 {heading}
 <textarea
 value={query}
 onChange={(e) => setQuery(e.target.value)}
 rows={3}
 maxLength={QUERY_MAX_CHARS}
 placeholder="What would you like to know about this meeting?"
 disabled={submitting}
 className="w-full rounded-md border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-white outline-none focus:border-white/40 disabled:opacity-50 resize-none"
 />
 <div className="mt-2 flex items-center justify-between gap-3">
 <span
 className={`text-[10px] ${overLimit ? "text-rose-300" : "text-foreground/40"}`}
 >
 {remaining} character{remaining === 1 ? "" : "s"} remaining
 </span>
 <button
 type="button"
 onClick={submit}
 disabled={!submittable || submitting}
 className="inline-flex items-center gap-2 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3 py-1.5 text-xs font-medium text-emerald-100 transition disabled:opacity-30 disabled:cursor-not-allowed hover:border-emerald-400/60"
 >
 {submitting ? "Submitting…" : "Submit"}
 </button>
 </div>
 {rejectedReason && (
 <div className="mt-3 rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
 {rejectedReason}
 </div>
 )}
 </div>
 );
}

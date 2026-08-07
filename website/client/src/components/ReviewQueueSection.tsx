/**
 * ReviewQueueSection — operator surface for the moderation backlog.
 *
 * Consumes operator_review_needed=1 rows from four feeds:
 * - suggestions (V1-UI-3 query submissions flagged by the
 * suggestion_query moderation surface)
 * - creator_signups (Chunk 8 signups flagged by the creator_signup
 * moderation surface)
 * - adversarial_findings hardening-review findings from the Antigravity-Jules-Gemini-Pro side,
 * defensive observations + suggested mitigations)
 * - repository_pending (V1-Repo-1 repository_assets rows in
 * pending_owner_review / — the
 * deposit gate that keeps non-approved assets
 * out of the creator-facing repository)
 *
 * Each row shows the submitter / asset, the flagged content or asset
 * metadata, when it landed, and a pair (or trio) of single-tap actions.
 * The component hides itself entirely when every feed is empty so the
 * rest of the operator surface keeps its density.
 *
 *: every visible label is sentence-case operator vocabulary.
 * No `operator_review_needed=1`-style schema strings reach the user;
 * the schema lives in the database, not on the screen.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { OwnerOnly } from "./OwnerOnly";
import RepositoryActionModal, {
 type RepositoryActionKind,
} from "./RepositoryActionModal";

interface SuggestionRow {
 id: number;
 user_id: number;
 display_name: string | null;
 email: string;
 user_role: string;
 meeting_id: number;
 query_text: string;
 normalized_text: string | null;
 moderation_reason: string | null;
 submitted_at: string;
}

interface CreatorSignupRow {
 id: number;
 user_id: number;
 display_name: string | null;
 email: string;
 user_role: string;
 tos_version: string;
 disclaimer_version: string;
 moderation_reason: string | null;
 moderation_normalized_text: string | null;
 submitted_at: string;
 revoked_at: string | null;
}

interface HardeningFindingRow {
 id: number;
 run_id: number;
 run_label: string;
 runner_identity: string;
 run_date: string;
 surface_id: string;
 severity: "low" | "medium" | "high";
 defensive_observation: string;
 suggested_mitigation: string;
 submitted_at: string;
}

interface RepositoryAssetRow {
 id: number;
 source_type: string;
 source_id: number;
 source_meeting_id: number;
 asset_type: string;
 asset_metadata: Record<string, any> | null;
 repository_status: string;
 queued_at: string;
 filter_reason: string | null;
 city_name: string | null;
 meeting_date: string | null;
 meeting_title: string | null;
}

interface ReviewQueueResponse {
 success: boolean;
 suggestions: SuggestionRow[];
 creator_signups: CreatorSignupRow[];
 adversarial_findings: HardeningFindingRow[];
 repository_pending: RepositoryAssetRow[];
 counts: {
 suggestions: number;
 creator_signups: number;
 adversarial_findings: number;
 repository_pending: number;
 total: number;
 };
}

type QueueType = "suggestions" | "creator_signups" | "adversarial_findings";
type Action = "dismiss" | "reject" | "revoke" | "triaged" | "resolved";
type RepositoryAction = "approve" | "reject" | "withdraw";

const SEVERITY_BADGE: Record<HardeningFindingRow["severity"], { label: string; tone: string }> = {
 low: { label: "Low severity", tone: "text-sky-200 border-sky-300/30 bg-sky-300/10" },
 medium: { label: "Medium severity", tone: "text-amber-200 border-amber-300/40 bg-amber-300/10" },
 high: { label: "High severity", tone: "text-rose-200 border-rose-300/40 bg-rose-300/10" },
};

const RELATIVE_DIVISORS: Array<{ ms: number; suffix: string }> = [
 { ms: 1000 * 60, suffix: "minute" },
 { ms: 1000 * 60 * 60, suffix: "hour" },
 { ms: 1000 * 60 * 60 * 24, suffix: "day" },
];

function relativeWhen(iso: string | null | undefined): string {
 if (!iso) return "—";
 const t = Date.parse(iso.replace(" ", "T"));
 if (Number.isNaN(t)) return iso;
 const delta = Date.now() - t;
 if (delta < 60_000) return "just now";
 for (let i = RELATIVE_DIVISORS.length - 1; i >= 0; i--) {
 const { ms, suffix } = RELATIVE_DIVISORS[i];
 const n = Math.floor(delta / ms);
 if (n >= 1) return `${n} ${suffix}${n === 1 ? "" : "s"} ago`;
 }
 return "just now";
}

function submitterLabel(displayName: string | null, email: string): string {
 const name = (displayName || "").trim();
 if (name) return name;
 return email;
}

function moderationLabel(reason: string | null | undefined): string {
 if (!reason) return "Flagged for review";
 const lc = reason.toLowerCase();
 if (lc === "flagged") return "Flagged by content classifier";
 if (lc === "rate_limited") return "Hit rate limit";
 if (lc === "suspicious") return "Suspicious pattern";
 return `Flagged: ${reason}`;
}

// Human-readable asset label.: keep schema words out of the UI.
// Combines the asset class with hints from the source (a clip extracted
// from a member quote reads differently than a generic clip).
function describeAsset(assetType: string, sourceType: string): string {
 const a = (assetType || "").toLowerCase();
 const s = (sourceType || "").toLowerCase();
 if (a === "audio") return "Audio briefing";
 if (a === "video") return "Video explainer";
 if (a === "infographic") return "Infographic";
 if (a === "summary") return "Summary";
 if (a === "clip") return s === "member_quote" ? "Quote clip" : "Clip";
 return "Asset";
}

function assetMetadataPreview(
 metadata: Record<string, any> | null,
): string | null {
 if (!metadata) return null;
 // Prefer the operator-readable keys in priority order; fall back to
 // raw if a producer dumped something unstructured.
 const m = metadata as Record<string, unknown>;
 for (const key of ["title", "headline", "summary", "preview", "raw"]) {
 const v = m[key];
 if (typeof v === "string" && v.trim()) return v.trim();
 }
 return null;
}

interface SectionProps {
 className?: string;
}

export default function ReviewQueueSection({ className = "" }: SectionProps) {
 const [data, setData] = useState<ReviewQueueResponse | null>(null);
 const [loading, setLoading] = useState(true);
 const [error, setError] = useState<string | null>(null);
 const [acting, setActing] = useState<Set<string>>(new Set());
 // Session-30 (2026-07-04): default collapsed. Per Decision 4,
 // this queue is Creator Network deposit-gate machinery, orthogonal
 // to broadcast publishing. With the Creator Network deferred, the review
 // queue is legacy background — surface it collapsed so the operator's
 // eye lands on the publish-relevant rows instead. Expand toggle still
 // works for when the operator wants to look.
 const [expanded, setExpanded] = useState<boolean>(false);

 const refresh = useCallback(async () => {
 setError(null);
 try {
 const res = await fetch("/api/operator/review-queue", {
 credentials: "include",
 });
 if (res.status === 401) {
 // Not signed in — silent no-op (the operator surface still
 // mounts; section hides itself per the render below).
 setData({
 success: false,
 suggestions: [],
 creator_signups: [],
 adversarial_findings: [],
 repository_pending: [],
 counts: {
 suggestions: 0,
 creator_signups: 0,
 adversarial_findings: 0,
 repository_pending: 0,
 total: 0,
 },
 });
 setLoading(false);
 return;
 }
 const body = (await res.json()) as ReviewQueueResponse;
 setData(body);
 } catch (err: any) {
 setError(err?.message || "Failed to load the review queue");
 } finally {
 setLoading(false);
 }
 }, []);

 useEffect(() => {
 void refresh();
 }, [refresh]);

 const resolve = useCallback(
 async (queueType: QueueType, rowId: number, action: Action) => {
 const key = `${queueType}:${rowId}`;
 setActing((prev) => new Set(prev).add(key));
 try {
 const res = await fetch(
 `/api/operator/review-queue/${queueType}/${rowId}/resolve`,
 {
 method: "POST",
 credentials: "include",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify({ action }),
 },
 );
 if (!res.ok) {
 const body = await res.json().catch(() => ({}));
 throw new Error(
 body?.error || `HTTP ${res.status}: failed to resolve`,
 );
 }
 // Optimistic remove; refresh in the background for source-of-truth.
 setData((prev) => {
 if (!prev) return prev;
 const next = { ...prev };
 if (queueType === "suggestions") {
 next.suggestions = prev.suggestions.filter((r) => r.id !== rowId);
 } else if (queueType === "creator_signups") {
 next.creator_signups = prev.creator_signups.filter(
 (r) => r.id !== rowId,
 );
 } else {
 next.adversarial_findings = prev.adversarial_findings.filter(
 (r) => r.id !== rowId,
 );
 }
 next.counts = {
 suggestions: next.suggestions.length,
 creator_signups: next.creator_signups.length,
 adversarial_findings: next.adversarial_findings.length,
 repository_pending: next.repository_pending.length,
 total:
 next.suggestions.length +
 next.creator_signups.length +
 next.adversarial_findings.length +
 next.repository_pending.length,
 };
 return next;
 });
 void refresh();
 } catch (err: any) {
 setError(err?.message || "Could not resolve that item");
 } finally {
 setActing((prev) => {
 const next = new Set(prev);
 next.delete(key);
 return next;
 });
 }
 },
 [refresh],
 );

 // Modal state for reject / withdraw reason capture. Approve bypasses
 // the modal and runs the network call directly.
 const [modalAction, setModalAction] = useState<RepositoryActionKind | null>(
 null,
 );
 const [modalAssetId, setModalAssetId] = useState<number | null>(null);
 const [modalContext, setModalContext] = useState<string | null>(null);

 // V1-Repo-1 — / repository deposit gate. Network call that
 // actually mutates the row. Approve dispatches without a reason; the
 // modal feeds reject + withdraw the operator's typed reason.
 const performRepositoryAction = useCallback(
 async (
 assetId: number,
 action: RepositoryAction,
 reason: string,
 ) => {
 const key = `repository_assets:${assetId}`;
 setActing((prev) => new Set(prev).add(key));
 try {
 const res = await fetch(
 `/api/operator/repository-queue/${assetId}/${action}`,
 {
 method: "POST",
 credentials: "include",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify(reason ? { reason } : {}),
 },
 );
 if (!res.ok) {
 const body = await res.json().catch(() => ({}));
 throw new Error(
 body?.error || `HTTP ${res.status}: action failed`,
 );
 }
 // Optimistic remove from pending list; refresh for source-of-truth.
 setData((prev) => {
 if (!prev) return prev;
 const next = { ...prev };
 next.repository_pending = prev.repository_pending.filter(
 (r) => r.id !== assetId,
 );
 next.counts = {
 suggestions: next.suggestions.length,
 creator_signups: next.creator_signups.length,
 adversarial_findings: next.adversarial_findings.length,
 repository_pending: next.repository_pending.length,
 total:
 next.suggestions.length +
 next.creator_signups.length +
 next.adversarial_findings.length +
 next.repository_pending.length,
 };
 return next;
 });
 void refresh();
 } catch (err: any) {
 setError(err?.message || "Couldn't approve or reject this generation");
 } finally {
 setActing((prev) => {
 const n = new Set(prev);
 n.delete(key);
 return n;
 });
 }
 },
 [refresh],
 );

 // Entry point from the queue's button row. Approve dispatches
 // immediately; reject + withdraw open the in-aesthetic reason modal.
 const repositoryAction = useCallback(
 (
 assetId: number,
 action: RepositoryAction,
 contextLabel: string | null,
 ) => {
 if (action === "approve") {
 void performRepositoryAction(assetId, "approve", "");
 return;
 }
 setModalAction(action);
 setModalAssetId(assetId);
 setModalContext(contextLabel);
 },
 [performRepositoryAction],
 );

 const closeRepositoryModal = useCallback(() => {
 setModalAction(null);
 setModalAssetId(null);
 setModalContext(null);
 }, []);

 const confirmRepositoryModal = useCallback(
 (reason: string) => {
 if (modalAssetId == null || modalAction == null) return;
 const assetId = modalAssetId;
 const action = modalAction;
 closeRepositoryModal();
 void performRepositoryAction(assetId, action, reason);
 },
 [modalAssetId, modalAction, closeRepositoryModal, performRepositoryAction],
 );

 const modalBusy =
 modalAssetId != null
 ? acting.has(`repository_assets:${modalAssetId}`)
 : false;

 const total = data?.counts.total ?? 0;
 const suggestionCount = data?.counts.suggestions ?? 0;
 const creatorCount = data?.counts.creator_signups ?? 0;
 const hardeningCount = data?.counts.adversarial_findings ?? 0;
 const repositoryCount = data?.counts.repository_pending ?? 0;

 const headerLine = useMemo(() => {
 if (total === 0) return "No items waiting for review.";
 const parts: string[] = [];
 if (suggestionCount)
 parts.push(`${suggestionCount} suggestion${suggestionCount === 1 ? "" : "s"}`);
 if (creatorCount)
 parts.push(
 `${creatorCount} creator signup${creatorCount === 1 ? "" : "s"}`,
 );
 if (hardeningCount)
 parts.push(
 `${hardeningCount} hardening finding${hardeningCount === 1 ? "" : "s"}`,
 );
 if (repositoryCount)
 parts.push(
 `${repositoryCount} generation${repositoryCount === 1 ? "" : "s"}`,
 );
 return parts.join(" · ");
 }, [
 total,
 suggestionCount,
 creatorCount,
 hardeningCount,
 repositoryCount,
 ]);

 // Hide entirely when empty. Operator's attention is precious; empty
 // queues should never take screen real estate.
 if (!loading && total === 0 && !error) return null;

 return (
 <OwnerOnly>
 <section
 className={`flex-none px-8 py-3 border-b border-amber-400/30 bg-amber-400/5 ${className}`}
 aria-label="Operator review queue"
 >
 <header className="flex items-center justify-between gap-4 mb-2">
 <div className="flex items-center gap-3">
 <span className="text-[10px] uppercase tracking-[0.18em] text-amber-200/80">
 Review queue
 </span>
 <span className="text-[12px] text-amber-100/90">{headerLine}</span>
 </div>
 <div className="flex items-center gap-2">
 <button
 type="button"
 onClick={() => void refresh()}
 className="text-[11px] uppercase tracking-widest text-amber-100/70 hover:text-amber-100 border border-amber-400/30 hover:border-amber-400/60 px-2.5 py-1"
 title="Re-fetch the review queue from the server"
 >
 [REFRESH]
 </button>
 <button
 type="button"
 onClick={() => setExpanded((v) => !v)}
 className="text-[11px] uppercase tracking-widest text-amber-100/70 hover:text-amber-100 border border-amber-400/30 hover:border-amber-400/60 px-2.5 py-1"
 aria-expanded={expanded}
 >
 {expanded ? "COLLAPSE" : "EXPAND"}
 </button>
 </div>
 </header>

 {error && (
 <div className="mb-2 text-[12px] text-red-300/90">{error}</div>
 )}

 {expanded && (
 <div className="flex flex-col gap-3">
 {data && data.suggestions.length > 0 && (
 <div>
 <div className="text-[10px] uppercase tracking-[0.16em] text-white/40 mb-1">
 Suggestions on Mohave broadcasts
 </div>
 <ul className="flex flex-col gap-1.5">
 {data.suggestions.map((row) => {
 const key = `suggestions:${row.id}`;
 const busy = acting.has(key);
 return (
 <li
 key={row.id}
 className="flex flex-col gap-1 px-3 py-2 border border-white/10 bg-[#0E0B07]"
 >
 <div className="flex items-center justify-between gap-3 text-[12px]">
 <div className="flex items-center gap-2 min-w-0">
 <span className="text-white/85 font-medium truncate">
 {submitterLabel(row.display_name, row.email)}
 </span>
 <span className="text-white/40 text-[11px] tracking-wide">
 · Broadcast #{row.meeting_id} · {relativeWhen(row.submitted_at)}
 </span>
 </div>
 <div className="flex items-center gap-1.5 flex-none">
 <button
 type="button"
 disabled={busy}
 onClick={() => void resolve("suggestions", row.id, "dismiss")}
 className="text-[11px] uppercase tracking-widest text-white/80 hover:text-white border border-white/20 hover:border-white/40 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 title="Mark this submission as reviewed and clear it from the queue."
 >
 {busy ? "…" : "DISMISS"}
 </button>
 <button
 type="button"
 disabled={busy}
 onClick={() => void resolve("suggestions", row.id, "reject")}
 className="text-[11px] uppercase tracking-widest text-red-200/85 hover:text-red-100 border border-red-300/30 hover:border-red-300/60 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 title="Reject this submission as out-of-bounds. The row stays in the audit trail."
 >
 REJECT
 </button>
 </div>
 </div>
 <div className="text-[12px] text-white/75 italic leading-snug">
 “{row.query_text}”
 </div>
 <div className="text-[10px] uppercase tracking-wider text-amber-200/70">
 {moderationLabel(row.moderation_reason)}
 </div>
 </li>
 );
 })}
 </ul>
 </div>
 )}

 {data && data.creator_signups.length > 0 && (
 <div>
 <div className="text-[10px] uppercase tracking-[0.16em] text-white/40 mb-1">
 Creator Network signups
 </div>
 <ul className="flex flex-col gap-1.5">
 {data.creator_signups.map((row) => {
 const key = `creator_signups:${row.id}`;
 const busy = acting.has(key);
 return (
 <li
 key={row.id}
 className="flex flex-col gap-1 px-3 py-2 border border-white/10 bg-[#0E0B07]"
 >
 <div className="flex items-center justify-between gap-3 text-[12px]">
 <div className="flex items-center gap-2 min-w-0">
 <span className="text-white/85 font-medium truncate">
 {submitterLabel(row.display_name, row.email)}
 </span>
 <span className="text-white/40 text-[11px] tracking-wide">
 · {row.user_role} · signed up {relativeWhen(row.submitted_at)}
 </span>
 </div>
 <div className="flex items-center gap-1.5 flex-none">
 <button
 type="button"
 disabled={busy}
 onClick={() => void resolve("creator_signups", row.id, "dismiss")}
 className="text-[11px] uppercase tracking-widest text-white/80 hover:text-white border border-white/20 hover:border-white/40 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 title="Confirm the signup is legitimate. The creator keeps the role."
 >
 {busy ? "…" : "DISMISS"}
 </button>
 <button
 type="button"
 disabled={busy}
 onClick={() => void resolve("creator_signups", row.id, "revoke")}
 className="text-[11px] uppercase tracking-widest text-red-200/85 hover:text-red-100 border border-red-300/30 hover:border-red-300/60 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 title="Revoke the creator role. User reverts to light account; audit trail preserved."
 >
 [REVOKE]
 </button>
 </div>
 </div>
 {row.moderation_normalized_text && (
 <div className="text-[12px] text-white/75 italic leading-snug">
 “{row.moderation_normalized_text}”
 </div>
 )}
 <div className="text-[10px] uppercase tracking-wider text-amber-200/70">
 {moderationLabel(row.moderation_reason)}
 </div>
 </li>
 );
 })}
 </ul>
 </div>
 )}

 {data && data.adversarial_findings.length > 0 && (
 <div>
 <div className="text-[10px] uppercase tracking-[0.16em] text-white/40 mb-1">
 Hardening findings · defensive observations
 </div>
 <ul className="flex flex-col gap-1.5">
 {data.adversarial_findings.map((row) => {
 const key = `adversarial_findings:${row.id}`;
 const busy = acting.has(key);
 const sev = SEVERITY_BADGE[row.severity] ?? SEVERITY_BADGE.medium;
 return (
 <li
 key={row.id}
 className="flex flex-col gap-1 px-3 py-2 border border-white/10 bg-[#0E0B07]"
 >
 <div className="flex items-center justify-between gap-3 text-[12px]">
 <div className="flex items-center gap-2 min-w-0 flex-wrap">
 <span
 className={`inline-flex items-center gap-1 rounded-sm border px-1.5 py-[1px] text-[10px] uppercase tracking-wider ${sev.tone}`}
 title={`${sev.label} per the runner's assessment`}
 >
 {row.surface_id} · {row.severity}
 </span>
 <span className="text-white/70 text-[11px] tracking-wide truncate">
 {row.run_label} · {row.runner_identity}
 </span>
 <span className="text-white/40 text-[11px] tracking-wide">
 · {relativeWhen(row.submitted_at)}
 </span>
 </div>
 <div className="flex items-center gap-1.5 flex-none">
 <button
 type="button"
 disabled={busy}
 onClick={() => void resolve("adversarial_findings", row.id, "triaged")}
 className="text-[11px] uppercase tracking-widest text-white/80 hover:text-white border border-white/20 hover:border-white/40 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 title="Mark as triaged. Acknowledged but no immediate code change planned; finding stays in the audit table with a status of triaged."
 >
 {busy ? "…" : "TRIAGED"}
 </button>
 <button
 type="button"
 disabled={busy}
 onClick={() => void resolve("adversarial_findings", row.id, "resolved")}
 className="text-[11px] uppercase tracking-widest text-emerald-200/85 hover:text-emerald-100 border border-emerald-300/30 hover:border-emerald-300/60 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 title="Mark as resolved. Mitigation has landed; the operator note can reference the commit hash + summary."
 >
 [RESOLVED]
 </button>
 </div>
 </div>
 <div className="text-[12px] text-white/75 italic leading-snug">
 {row.defensive_observation}
 </div>
 <div className="text-[11px] text-white/55 leading-snug">
 <span className="text-white/35 uppercase tracking-widest text-[10px] mr-1">
 Suggested mitigation:
 </span>
 {row.suggested_mitigation}
 </div>
 </li>
 );
 })}
 </ul>
 </div>
 )}

 {data && data.repository_pending.length > 0 && (
 <div>
 <div className="text-[10px] uppercase tracking-[0.16em] text-white/40 mb-1">
 Generations pending your approval
 </div>
 <ul className="flex flex-col gap-1.5">
 {data.repository_pending.map((row) => {
 const key = `repository_assets:${row.id}`;
 const busy = acting.has(key);
 const assetLabel = describeAsset(
 row.asset_type,
 row.source_type,
 );
 const meetingLabel =
 row.city_name && row.meeting_date
 ? `${row.city_name} · ${row.meeting_date}`
 : row.meeting_title ||
 `Meeting #${row.source_meeting_id}`;
 const preview = assetMetadataPreview(row.asset_metadata);
 return (
 <li
 key={row.id}
 className="flex flex-col gap-1 px-3 py-2 border border-white/10 bg-[#0E0B07]"
 >
 <div className="flex items-center justify-between gap-3 text-[12px]">
 <div className="flex items-center gap-2 min-w-0 flex-wrap">
 <span
 className="inline-flex items-center rounded-sm border border-sky-300/30 bg-sky-300/10 px-1.5 py-[1px] text-[10px] uppercase tracking-wider text-sky-200"
 title={`Asset class: ${assetLabel}`}
 >
 {assetLabel}
 </span>
 <span className="text-white/85 font-medium truncate">
 {meetingLabel}
 </span>
 <span className="text-white/40 text-[11px] tracking-wide">
 · queued {relativeWhen(row.queued_at)}
 </span>
 </div>
 <div className="flex items-center gap-1.5 flex-none">
 <button
 type="button"
 disabled={busy}
 onClick={() =>
 repositoryAction(
 row.id,
 "approve",
 `${assetLabel} · ${meetingLabel}`,
 )
 }
 className="text-[11px] uppercase tracking-widest text-emerald-200/85 hover:text-emerald-100 border border-emerald-300/30 hover:border-emerald-300/60 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 title="Approve this generation for the Creator Network repository. Once the creator-facing surface is live, approved generations become downloadable. (Creator Network scrap postponed — see TEMPORARY_THOUGHTS Session-29.)"
 >
 {busy ? "…" : "APPROVE"}
 </button>
 <button
 type="button"
 disabled={busy}
 onClick={() =>
 repositoryAction(
 row.id,
 "reject",
 `${assetLabel} · ${meetingLabel}`,
 )
 }
 className="text-[11px] uppercase tracking-widest text-red-200/85 hover:text-red-100 border border-red-300/30 hover:border-red-300/60 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 title="Send this asset back to draft state. The reason you give lands in the public-readable filter log."
 >
 REJECT
 </button>
 </div>
 </div>
 {preview && (
 <div className="text-[12px] text-white/75 italic leading-snug line-clamp-2">
 “{preview}”
 </div>
 )}
 </li>
 );
 })}
 </ul>
 </div>
 )}

 {loading && (
 <div className="text-[12px] text-white/50">Loading the queue…</div>
 )}
 </div>
 )}
 </section>
 <RepositoryActionModal
 open={modalAction != null}
 action={modalAction}
 contextLabel={modalContext}
 busy={modalBusy}
 onCancel={closeRepositoryModal}
 onConfirm={confirmRepositoryModal}
 />
 </OwnerOnly>
 );
}

/**
 * PublishConfirmModal — the publish-time confirmation overlay (Quotes
 * Unification Refactor Chunk 8, 2026-05-26; copy rewrite 2026-05-26).
 *
 * Architectural framing to land in Chunk 10):
 *
 * The OLD ReviewGateModal claimed verification ("you will verify each
 * verbatim quote against the source recording") but provided no audio /
 * video / source tools to actually verify with. The contradiction made
 * the modal a theatrical surface rather than a real safeguard.
 *
 * This replacement is HONEST about what it is and what it isn't:
 *
 * - It is NOT a verification step. The verification chain ( V2:
 * Whisper alignment + Gemini Pro batch review + human clip-by-clip
 * attestation) happens upstream. By the time a meeting reaches this
 * modal, hero-quote verification has either finished or it hasn't.
 *
 * - When verification IS complete, the modal shows a status indicator
 * ("Quotes have been verified"). No checkbox to pretend-tick.
 *
 * - When verification is NOT complete, the modal blocks publish entirely
 * with a clear "finish verifying first" message. The operator does NOT
 * get to override by clicking a checkbox.
 *
 * - The actual checklist items are just for non-quote content the
 * operator must eyeball on the broadcast preview before publishing:
 * the Studio outputs (audio / video / infographic) and the text
 * content (synopsis + key decisions).
 *
 * (human review gate permanent) is satisfied by:
 * - Upstream verification for quote accuracy
 * - The forced read of the broadcast preview + two-item acknowledgment
 * for non-quote content
 *
 * Copy guidance: plain language, sans-serif, no engineering jargon. This
 * is a journalistic moment — the operator is deciding "yes, this goes
 * public" — not a terminal command.
 */
import { useEffect, useState } from "react";
import { CheckCircle2, AlertCircle, ExternalLink } from "lucide-react";
import type { EpisodeAuditSummary } from "./EpisodeAuditCard";

export interface PublishConfirmMeeting {
 id: number;
 title: string;
 date: string;
 city: string;
}

interface PublishConfirmModalProps {
 open: boolean;
 meeting: PublishConfirmMeeting | null;
 /** Result of querying the unified `quotes` table at modal-open time.
 * heroCount: total broadcast-hero quotes; verifiedCount: how many of
 * those carry verified_status='verified'. The modal renders a clean
 * status when verifiedCount === heroCount. A zero heroCount means this
 * legacy verification step does not apply; citation-backed decisions are
 * validated server-side. */
 heroCount: number;
 verifiedCount: number;
 /** False while the quote-count request is pending or if it failed. */
 quoteCountsLoaded: boolean;
 /** Optional href that opens the broadcast preview in a new tab. */
 previewHref?: string;
 /** Latest non-gating episode-auditor summary, when one exists. */
 auditSummary?: EpisodeAuditSummary;
 onPublish: () => void;
 onCancel: () => void;
}

type ChecklistKey = "studio" | "text";

export function canPublishBroadcast(
 heroCount: number,
 verifiedCount: number,
 allChecklistTicked: boolean,
 quoteCountsLoaded = true,
): boolean {
 const quoteVerificationSatisfied =
 quoteCountsLoaded &&
 (heroCount === 0 || (heroCount > 0 && verifiedCount === heroCount));
 return quoteVerificationSatisfied && allChecklistTicked;
}

export default function PublishConfirmModal({
 open,
 meeting,
 heroCount,
 verifiedCount,
 quoteCountsLoaded,
 previewHref,
 auditSummary,
 onPublish,
 onCancel,
}: PublishConfirmModalProps) {
 const [ticked, setTicked] = useState<Set<ChecklistKey>>(new Set());

 // Reset every time the modal closes. The friction is the feature —
 // every entry re-prompts.
 useEffect(() => {
 if (!open) setTicked(new Set());
 }, [open]);

 // ESC closes
 useEffect(() => {
 if (!open) return;
 const onKey = (e: KeyboardEvent) => {
 if (e.key === "Escape") onCancel();
 };
 window.addEventListener("keydown", onKey);
 return () => window.removeEventListener("keydown", onKey);
 }, [open, onCancel]);

 if (!open || !meeting) return null;

 const toggle = (key: ChecklistKey) => {
 setTicked(prev => {
 const next = new Set(prev);
 if (next.has(key)) next.delete(key);
 else next.add(key);
 return next;
 });
 };

 // Legacy hero-quote verification is N/A when the citation-era contract
 // produces no hero quotes. Quote-bearing meetings still require every
 // hero quote to carry verified_status='verified'.
 const quoteVerificationSatisfied =
 quoteCountsLoaded &&
 (heroCount === 0 || (heroCount > 0 && verifiedCount === heroCount));
 const pendingCount = Math.max(heroCount - verifiedCount, 0);
 const allChecklistTicked = ticked.has("studio") && ticked.has("text");
 const canPublish = canPublishBroadcast(
 heroCount,
 verifiedCount,
 allChecklistTicked,
 quoteCountsLoaded,
 );

 return (
 <div
 className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:p-8 font-sans"
 style={{ background: "rgba(0,0,0,0.78)" }}
 role="dialog"
 aria-modal="true"
 aria-labelledby="publish-confirm-title"
 onClick={e => {
 if (e.target === e.currentTarget) onCancel();
 }}
 >
 <div
 className="max-w-xl w-full rounded-2xl overflow-hidden font-sans"
 style={{
 background: "#0F141B",
 border: "1px solid rgba(255,255,255,0.08)",
 boxShadow: "0 24px 60px rgba(0,0,0,0.6)",
 fontFamily:
 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
 }}
 >
 {/* Header — title in sans, meeting metadata readable */}
 <div className="px-7 py-5 border-b border-white/5">
 <p className="text-[11px] uppercase tracking-[0.18em] text-[#3B82F6] font-semibold mb-2">
 Publish broadcast
 </p>
 <h2
 id="publish-confirm-title"
 className="text-[19px] text-white font-semibold leading-tight"
 >
 {meeting.title}
 </h2>
 <p className="text-[14px] text-gray-400 mt-1">
 {meeting.city} · {meeting.date}
 </p>
 </div>

 {/* Verification status — informational, not a checkbox */}
 <div
 className="px-7 py-4 border-b border-white/5 flex items-start gap-3"
 style={{
 background: quoteVerificationSatisfied
 ? "rgba(34, 197, 94, 0.06)"
 : "rgba(245, 165, 36, 0.06)",
 }}
 >
 {quoteVerificationSatisfied ? (
 <CheckCircle2 className="w-5 h-5 mt-0.5 flex-shrink-0" style={{ color: "#22C55E" }} />
 ) : (
 <AlertCircle className="w-5 h-5 mt-0.5 flex-shrink-0" style={{ color: "#F5A524" }} />
 )}
 <div className="flex-1 min-w-0">
 {!quoteCountsLoaded ? (
 <>
 <p className="text-[14px] text-white font-semibold">
 Checking legacy hero quotes
 </p>
 <p className="text-[13px] text-gray-400 mt-0.5 leading-relaxed">
 Publish remains locked until the quote-verification status loads.
 </p>
 </>
 ) : heroCount === 0 ? (
 <>
 <p className="text-[14px] text-white font-semibold">
 No legacy hero quotes
 </p>
 <p className="text-[13px] text-gray-400 mt-0.5 leading-relaxed">
 Citation-backed decisions are validated server-side; hero-quote
 verification does not apply to this meeting.
 </p>
 </>
 ) : quoteVerificationSatisfied ? (
 <>
 <p className="text-[14px] text-white font-semibold">
 Quotes have been verified
 </p>
 <p className="text-[13px] text-gray-400 mt-0.5 leading-relaxed">
 All {heroCount} broadcast quotes passed the verification chain
 (Whisper transcript + Gemini review + human attestation).
 </p>
 </>
 ) : (
 <>
 <p className="text-[14px] text-white font-semibold">
 {pendingCount} of {heroCount} quotes still need verification
 </p>
 <p className="text-[13px] text-gray-400 mt-0.5 leading-relaxed">
 Finish the Gemini verification pass first ([BUILD] →
 run the Gemini Pro batch → [INGEST]). The Publish
 button stays disabled until every quote is verified —
 this isn't something to confirm with a checkbox.
 </p>
 </>
 )}
 </div>
 </div>

 {/* Preview link */}
 {previewHref && (
 <div className="px-7 py-4 border-b border-white/5">
 <a
 href={previewHref}
 target="_blank"
 rel="noopener noreferrer"
 className="inline-flex items-center gap-1.5 text-[14px] text-[#3B82F6] hover:text-[#60A5FA] hover:underline underline-offset-4"
 >
 Open the broadcast preview
 <ExternalLink className="w-3.5 h-3.5" />
 </a>
 <p className="text-[13px] text-gray-400 mt-2 leading-relaxed">
 Look it over — the tagline, key decisions and their citation
 evidence, community calls to action, and the video player. Then
 come back and confirm the two things below.
 </p>
 </div>
 )}

 {/* Two-item checklist — plain language */}
 <div className="px-7 py-5 space-y-4">
 {auditSummary && (
 <p className="text-[12px] text-gray-400 leading-relaxed border-b border-white/5 pb-4">
 {auditSummary.verdict === "no_catches"
 ? "The episode auditor found no catches on its last pass."
 : auditSummary.verdict === "flags"
 ? `The episode auditor flagged ${auditSummary.findings_count + auditSummary.deterministic_flags_count} item(s) on its last pass — worth a look before publishing (badge on the meeting row opens the detail).`
 : "The last audit pass didn't complete."}
 </p>
 )}
 <label className="flex gap-3 cursor-pointer items-start group">
 <input
 type="checkbox"
 checked={ticked.has("studio")}
 onChange={() => toggle("studio")}
 className="mt-1 w-4 h-4 rounded border-white/20 bg-white/[0.04] checked:bg-[#3B82F6] checked:border-[#3B82F6] cursor-pointer"
 />
 <div className="flex-1 min-w-0">
 <p className="text-[14px] text-white font-semibold">
 The broadcast surfaces look right.
 </p>
 <p className="text-[13px] text-gray-400 mt-0.5 leading-relaxed">
 The tagline, key decisions and citation evidence, community
 calls to action, and video player match the meeting.
 Nothing from public commenters slipped into them, including the
 verbatim citation excerpts. No editorial language — no{" "}
 &ldquo;controversial,&rdquo; &ldquo;narrowly,&rdquo;{" "}
 &ldquo;wisely,&rdquo; etc.
 </p>
 </div>
 </label>

 <label className="flex gap-3 cursor-pointer items-start group">
 <input
 type="checkbox"
 checked={ticked.has("text")}
 onChange={() => toggle("text")}
 className="mt-1 w-4 h-4 rounded border-white/20 bg-white/[0.04] checked:bg-[#3B82F6] checked:border-[#3B82F6] cursor-pointer"
 />
 <div className="flex-1 min-w-0">
 <p className="text-[14px] text-white font-semibold">
 Key decisions and the tagline read clean.
 </p>
 <p className="text-[13px] text-gray-400 mt-0.5 leading-relaxed">
 Names, dollar amounts, ordinances, and vote outcomes match
 what actually happened in the meeting. Nothing reads like
 someone's opinion crept in.
 </p>
 </div>
 </label>
 </div>

 {/* Footer */}
 <div className="px-7 py-4 border-t border-white/5 bg-white/[0.02] flex items-center justify-between gap-4">
 <p className="text-[11px] text-gray-500">
 Z-SPAN policy: · · NEUTRALITY_FRAMEWORK
 </p>
 <div className="flex items-center gap-3">
 <button
 type="button"
 onClick={onCancel}
 className="text-[13px] text-gray-300 hover:text-white border border-white/10 hover:border-white/30 px-4 py-2 rounded-md transition-colors"
 >
 Cancel
 </button>
 <button
 type="button"
 onClick={onPublish}
 disabled={!canPublish}
 className="text-[13px] text-white font-semibold px-5 py-2 rounded-md transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
 style={{
 background: canPublish ? "#3B82F6" : "rgba(59,130,246,0.18)",
 border: `1px solid ${canPublish ? "#3B82F6" : "rgba(59,130,246,0.35)"}`,
 }}
 title={
 !quoteCountsLoaded
 ? "Cannot publish — quote verification status has not loaded"
 : !quoteVerificationSatisfied
 ? `Cannot publish — ${pendingCount} of ${heroCount} quotes still need verification`
 : !allChecklistTicked
 ? "Confirm both items above to enable Publish"
 : "Publish this broadcast to public mode"
 }
 >
 Publish to public
 </button>
 </div>
 </div>
 </div>
 </div>
 );
}

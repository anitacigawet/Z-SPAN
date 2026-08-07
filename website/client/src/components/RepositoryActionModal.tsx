/**
 * RepositoryActionModal — reason capture for repository_assets actions.
 *
 * Replaces window.prompt() for reject + withdraw on the operator
 * repository queue (V1-Repo-1 follow-up). In-aesthetic Z-SPAN panel,
 * multi-line textarea, character counter to MAX_REASON_CHARS, Cancel
 * + Confirm. Keyboard: Esc cancels; Cmd/Ctrl+Enter submits when the
 * reason is non-empty.
 *
 * Approve doesn't need a reason — the queue can call onConfirm("")
 * directly without opening the modal.
 */
import { useEffect, useRef, useState } from "react";

const MAX_REASON_CHARS = 500;

export type RepositoryActionKind = "reject" | "withdraw";

interface ModalCopy {
 eyebrow: string;
 title: string;
 description: string;
 placeholder: string;
 confirmLabel: string;
 confirmTone: string;
}

const COPY: Record<RepositoryActionKind, ModalCopy> = {
 reject: {
 eyebrow: "Repository review",
 title: "Reject this asset",
 description:
 "The asset moves back to draft state and your reason lands in the public-readable filter log. Be specific — the log is how the project demonstrates structural neutrality.",
 placeholder: "Why is this asset being rejected? (PII, off-topic, etc.)",
 confirmLabel: "Confirm reject",
 confirmTone:
 "text-red-100 border-red-300/60 hover:border-red-300/80 bg-red-500/10 hover:bg-red-500/20",
 },
 withdraw: {
 eyebrow: "Repository review",
 title: "Withdraw this approved asset",
 description:
 "The asset is currently visible to creators. Withdrawing makes it unavailable + records your reason in the public-readable filter log so the change has a structural explanation.",
 placeholder:
 "Why is this previously-approved asset being withdrawn? (PII surfaced, correction, etc.)",
 confirmLabel: "Confirm withdraw",
 confirmTone:
 "text-red-100 border-red-300/60 hover:border-red-300/80 bg-red-500/10 hover:bg-red-500/20",
 },
};

interface Props {
 open: boolean;
 action: RepositoryActionKind | null;
 /** Short context line shown above the input — typically the asset
 * description so the operator can sanity-check what they're acting on
 * without leaving the modal. */
 contextLabel?: string | null;
 busy?: boolean;
 onCancel: () => void;
 onConfirm: (reason: string) => void;
}

export default function RepositoryActionModal({
 open,
 action,
 contextLabel,
 busy = false,
 onCancel,
 onConfirm,
}: Props) {
 const [reason, setReason] = useState("");
 const textareaRef = useRef<HTMLTextAreaElement | null>(null);
 const previouslyFocused = useRef<HTMLElement | null>(null);

 // Reset textarea + remember the trigger element so we can return
 // focus when the modal closes. Mount-time only — the parent toggles
 // `open` to control visibility.
 useEffect(() => {
 if (open) {
 previouslyFocused.current = document.activeElement as HTMLElement | null;
 setReason("");
 // Defer focus until the textarea is actually rendered.
 requestAnimationFrame(() => {
 textareaRef.current?.focus();
 });
 } else if (previouslyFocused.current) {
 previouslyFocused.current.focus();
 previouslyFocused.current = null;
 }
 }, [open]);

 // Esc cancels; Cmd/Ctrl+Enter submits when non-empty. The handler
 // attaches to the document so it works regardless of focus position
 // inside the modal.
 useEffect(() => {
 if (!open) return;
 const handler = (e: KeyboardEvent) => {
 if (e.key === "Escape") {
 e.preventDefault();
 if (!busy) onCancel();
 } else if (
 (e.key === "Enter" || e.key === "Return") &&
 (e.metaKey || e.ctrlKey)
 ) {
 e.preventDefault();
 const trimmed = reason.trim();
 if (trimmed && !busy) onConfirm(trimmed);
 }
 };
 document.addEventListener("keydown", handler);
 return () => document.removeEventListener("keydown", handler);
 }, [open, busy, reason, onCancel, onConfirm]);

 if (!open || !action) return null;
 const copy = COPY[action];
 const trimmed = reason.trim();
 const canConfirm = trimmed.length > 0 && !busy;
 const overLimit = reason.length > MAX_REASON_CHARS;
 const charCounter = `${reason.length} / ${MAX_REASON_CHARS}`;

 return (
 <div
 role="dialog"
 aria-modal="true"
 aria-labelledby="repository-action-modal-title"
 className="fixed inset-0 z-50 flex items-center justify-center"
 >
 <button
 type="button"
 aria-label="Cancel and close"
 onClick={() => {
 if (!busy) onCancel();
 }}
 className="absolute inset-0 bg-black/70"
 />
 <div className="relative z-10 w-full max-w-lg mx-4 rounded-lg border border-white/15 bg-[#11100C] shadow-2xl">
 <div className="px-5 py-4 border-b border-white/10">
 <div className="text-[10px] uppercase tracking-[0.18em] text-amber-200/80">
 {copy.eyebrow}
 </div>
 <h2
 id="repository-action-modal-title"
 className="text-base font-light tracking-tight text-white mt-1"
 >
 {copy.title}
 </h2>
 {contextLabel && (
 <div className="mt-2 text-[12px] text-white/55 truncate">
 {contextLabel}
 </div>
 )}
 </div>
 <div className="px-5 py-4 space-y-3">
 <p className="text-[13px] leading-relaxed text-foreground/75">
 {copy.description}
 </p>
 <div>
 <label
 htmlFor="repository-action-reason"
 className="sr-only"
 >
 Reason
 </label>
 <textarea
 id="repository-action-reason"
 ref={textareaRef}
 value={reason}
 onChange={(e) => setReason(e.target.value)}
 placeholder={copy.placeholder}
 rows={4}
 maxLength={MAX_REASON_CHARS + 20}
 disabled={busy}
 className="w-full rounded-md border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-white outline-none focus:border-white/40 resize-y min-h-[88px]"
 />
 <div
 className={`mt-1 flex items-center justify-between text-[10px] uppercase tracking-wider ${
 overLimit ? "text-red-300" : "text-foreground/40"
 }`}
 >
 <span>
 {overLimit
 ? `Trim to ${MAX_REASON_CHARS} characters`
 : "Esc cancels · Cmd/Ctrl+Enter submits"}
 </span>
 <span>{charCounter}</span>
 </div>
 </div>
 </div>
 <div className="px-5 py-3 border-t border-white/10 flex items-center justify-between gap-3">
 <button
 type="button"
 disabled={busy}
 onClick={() => onCancel()}
 className="text-[11px] uppercase tracking-widest text-white/70 hover:text-white border border-white/20 hover:border-white/40 px-3 py-1.5 disabled:opacity-30 disabled:cursor-not-allowed"
 >
 [CANCEL]
 </button>
 <button
 type="button"
 disabled={!canConfirm || overLimit}
 onClick={() => {
 if (canConfirm && !overLimit) onConfirm(trimmed);
 }}
 className={`text-[11px] uppercase tracking-widest px-3 py-1.5 border disabled:opacity-30 disabled:cursor-not-allowed ${copy.confirmTone}`}
 >
 {busy ? "[…]" : `[${copy.confirmLabel.toUpperCase()}]`}
 </button>
 </div>
 </div>
 </div>
 );
}

/**
 * ConfirmDestructive — visible-friction confirmation modal for destructive
 * operator actions.
 *
 * Per `DECISIONS.md` (UI visibility principle): resource-consuming
 * or irreversible actions require explicit operator consent. The two-gate
 * `ReviewGateModal` is reserved for publication approval /;
 * this lighter single-gate modal covers the everyday destructive verbs
 * (BURN, force-regenerate, bulk-delete, etc.) where:
 *
 * - The action's intent is unambiguous (the operator clicked the
 * bracketed verb deliberately).
 * - A `window.confirm()` would work but breaks visual consistency and
 * doesn't render structured context (which notebook, which meeting).
 * - The ReviewGateModal's policy-citation framing would be overkill.
 *
 * Use site pattern:
 *
 * const [confirmState, setConfirmState] = useState<{...} | null>(null);
 *
 * <ConfirmDestructive
 * open={confirmState !== null}
 * title="BURN NOTEBOOK"
 * subtitle={confirmState && `WO #${wo.id} · ${meeting.title}`}
 * description="Deletes this notebook from NotebookLM and restarts the WO with a fresh one."
 * context={[
 * { label: "Notebook", value: notebookId },
 * { label: "Meeting", value: meeting.title },
 * ]}
 * warning="All existing outputs for this meeting will be discarded."
 * confirmLabel="[BURN]"
 * onConfirm={confirmState?.run}
 * onCancel={() => setConfirmState(null)}
 * />
 *
 * The component owns no business logic — it's a render surface for the
 * caller's confirmation moment. The caller stores the pending action in
 * a state-shaped struct and invokes it on `onConfirm`.
 */
import { useEffect } from "react";
import { AlertTriangle } from "lucide-react";

interface ContextRow {
 label: string;
 value: string;
}

export interface ConfirmDestructiveProps {
 open: boolean;
 /** Action label, displayed prominently. Convention: bracketed UPPERCASE verb. */
 title: string;
 /** Optional secondary line under the title (e.g., "WO #123 · Kingman · Apr 21"). */
 subtitle?: string;
 /** Short prose describing what the action does. */
 description: string;
 /** Optional key/value rows shown as a structured context table. */
 context?: ContextRow[];
 /** Optional irreversibility warning line, rendered with a danger accent. */
 warning?: string;
 /** Defaults to "[CONFIRM]". */
 confirmLabel?: string;
 /** Defaults to "[CANCEL]". */
 cancelLabel?: string;
 onConfirm: () => void;
 onCancel: () => void;
}

export default function ConfirmDestructive({
 open,
 title,
 subtitle,
 description,
 context,
 warning,
 confirmLabel = "[CONFIRM]",
 cancelLabel = "[CANCEL]",
 onConfirm,
 onCancel,
}: ConfirmDestructiveProps) {
 // ESC key cancels — standard modal behavior; doesn't fire confirm by
 // accident because the operator must explicitly click the destructive
 // button (no Enter-to-confirm to prevent the muscle-memory footgun
 // where rapid-fire confirm dialogs all get accepted).
 useEffect(() => {
 if (!open) return;
 const onKey = (e: KeyboardEvent) => {
 if (e.key === "Escape") {
 e.preventDefault();
 onCancel();
 }
 };
 window.addEventListener("keydown", onKey);
 return () => window.removeEventListener("keydown", onKey);
 }, [open, onCancel]);

 if (!open) return null;

 return (
 <div
 className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 px-4"
 role="dialog"
 aria-modal="true"
 aria-labelledby="confirm-destructive-title"
 >
 <div
 className="w-full max-w-lg rounded-md border border-white/10"
 style={{
 background: "#0E0E10",
 boxShadow: "0 0 60px rgba(0,0,0,0.7)",
 }}
 >
 {/* Header */}
 <div className="px-6 py-5 border-b border-white/5">
 <div className="flex items-center gap-2 mb-2">
 <AlertTriangle className="w-4 h-4 text-[#EF4444]" />
 <p className="text-[10px] uppercase tracking-[0.2em] text-[#EF4444] font-semibold">
 Destructive Action
 </p>
 </div>
 <h2
 id="confirm-destructive-title"
 className="text-[15px] font-bold text-white tracking-wide"
 >
 {title}
 </h2>
 {subtitle && (
 <p className="text-[12px] text-gray-400 mt-0.5 truncate">
 {subtitle}
 </p>
 )}
 </div>

 {/* Body */}
 <div className="px-6 py-5">
 <p className="text-[12.5px] text-gray-300 leading-relaxed mb-4">
 {description}
 </p>

 {context && context.length > 0 && (
 <div className="mb-4 border border-white/5 rounded-sm">
 {context.map((row, i) => (
 <div
 key={i}
 className="grid grid-cols-[max-content_1fr] gap-x-4 px-3 py-1.5 text-[11.5px] border-b border-white/5 last:border-b-0"
 >
 <span className="uppercase tracking-[0.12em] text-gray-500">
 {row.label}
 </span>
 <span className="text-gray-200 break-all font-mono text-[11px]">
 {row.value}
 </span>
 </div>
 ))}
 </div>
 )}

 {warning && (
 <div
 className="text-[12px] text-[#FCA5A5] leading-relaxed pl-3 mb-1"
 style={{ borderLeft: "2px solid #EF4444" }}
 >
 {warning}
 </div>
 )}
 </div>

 {/* Footer */}
 <div className="px-6 py-4 border-t border-white/5 flex flex-wrap items-center justify-end gap-2">
 <button
 onClick={onCancel}
 className="text-[11px] uppercase tracking-widest text-gray-400 hover:text-white border border-white/10 hover:border-white/30 px-3 py-1.5"
 >
 {cancelLabel}
 </button>
 <button
 onClick={onConfirm}
 className="text-[11px] uppercase tracking-widest font-bold px-3 py-1.5 text-white border border-[#EF4444] bg-[#EF4444]/15 hover:bg-[#EF4444]/30 transition-colors"
 autoFocus
 >
 {confirmLabel}
 </button>
 </div>
 </div>
 </div>
 );
}

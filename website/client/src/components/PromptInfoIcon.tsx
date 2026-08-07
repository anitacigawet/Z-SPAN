import {
 type ReactNode,
 useEffect,
 useId,
 useMemo,
 useRef,
 useState,
} from "react";
import {
 EXCERPT_SOURCE,
 paragraphizeVerbatimWords,
 transitionDecisionEvidenceState,
 type DecisionEvidence,
 type DecisionEvidenceState,
} from "./decisionEvidence";

type PromptModeProps = {
 mode?: "prompt";
 promptName: string;
 label: string;
 color?: string;
};

type DecisionEvidenceDisclosureProps = {
 evidence: DecisionEvidence[];
 color?: string;
 children: (trigger: ReactNode) => ReactNode;
};

export function DecisionEvidenceDisclosure({
 evidence: rawEvidence,
 color,
 children,
}: DecisionEvidenceDisclosureProps) {
 const evidence = useMemo(() => rawEvidence
 .map((decision) => ({
 index: decision.index,
 spans: (decision.verbatim_spans || []).filter((span) =>
 span.source === EXCERPT_SOURCE
 && typeof span.text === "string"
 && span.text.length > 0
 && typeof span.label === "string"
 && (span.structure === "contiguous" || span.structure === "elided")
 ),
 }))
 .filter((decision) => decision.spans.length > 0), [rawEvidence]);
 const [state, setState] = useState<DecisionEvidenceState>("closed");
 const triggerRef = useRef<HTMLButtonElement | null>(null);
 const dialogId = `decision-evidence-${useId().replace(/:/g, "")}`;

 if (evidence.length === 0) {
 return <>{children(null)}</>;
 }

 const open = state === "open";
 const title = "Verbatim transcript source";
 const triggerLabel = `${open ? "Hide" : "Show"} verbatim transcript source for this decision`;
 const trigger = (
 <button
 ref={triggerRef}
 type="button"
 className="decision-evidence-trigger inline-flex items-center justify-center w-4 h-4 rounded-full border text-[10px] leading-none transition-colors hover:bg-white/10"
 style={{
 color: color || "currentColor",
 borderColor: "currentColor",
 opacity: 0.55,
 }}
 aria-label={triggerLabel}
 aria-expanded={open}
 aria-controls={dialogId}
 onClick={() => setState((current) => transitionDecisionEvidenceState(current))}
 >
 i
 </button>
 );

 return (
 <div
 className="decision-evidence-host w-full min-w-0"
 data-state={state}
 onKeyDown={(event) => {
 if (
 event.key === "Escape"
 && open
 && event.currentTarget.contains(document.activeElement)
 ) {
 event.preventDefault();
 setState("closed");
 triggerRef.current?.focus();
 }
 }}
 >
 {children(trigger)}
 <div
 id={dialogId}
 data-state={state}
 hidden={!open}
 className="decision-evidence-disclosure"
 >
 <div className="decision-evidence-header">
 <span className="decision-evidence-title">{title}</span>
 <span className="decision-evidence-badge">Words unchanged</span>
 </div>
 <div className="decision-evidence-body">
 {evidence.map((decision) => {
 const wordCount = decision.spans.reduce(
 (count, span) => count + (span.text || "").trim().split(/\s+/).filter(Boolean).length,
 0,
 );
 const showCollapse = decision.spans.length === 2 || wordCount > 180;
 return (
 <section key={decision.index} className="decision-evidence-item">
 {decision.spans.map((span, spanIndex) => {
 const paragraphs = paragraphizeVerbatimWords(
 span.word_timings || [],
 1.5,
 span.text || "",
 ) || [span.text || ""];
 const previous = decision.spans[spanIndex - 1];
 const gapMinutes = previous
 && typeof previous.end_seconds === "number"
 && typeof span.start_seconds === "number"
 && Number.isFinite(previous.end_seconds)
 && Number.isFinite(span.start_seconds)
 ? Math.round((span.start_seconds - previous.end_seconds) / 60)
 : 0;
 return (
 <div key={spanIndex} className="decision-evidence-passage">
 {spanIndex > 0 && (
 <div className="decision-evidence-divider">
 <span className="decision-evidence-divider-rule" />
 <span className="decision-evidence-divider-text">
 Verbatim transcript resumes about {gapMinutes} minutes later
 </span>
 <span className="decision-evidence-divider-rule" />
 </div>
 )}
 <blockquote className="decision-evidence-blockquote">
 {paragraphs.map((paragraph, paragraphIndex) => (
 <p key={paragraphIndex} className="decision-evidence-paragraph">
 {paragraph}
 </p>
 ))}
 </blockquote>
 </div>
 );
 })}
 {showCollapse && (
 <button
 type="button"
 className="decision-evidence-collapse"
 onClick={() => {
 setState("closed");
 triggerRef.current?.focus();
 }}
 >
 Collapse transcript source
 </button>
 )}
 </section>
 );
 })}
 </div>
 </div>
 </div>
 );
}

export default function PromptInfoIcon(props: PromptModeProps) {
 const [open, setOpen] = useState(false);
 const [body, setBody] = useState<string | null>(null);
 const [path, setPath] = useState<string | null>(null);
 const [error, setError] = useState<string | null>(null);
 const [loading, setLoading] = useState(false);
 const wrapRef = useRef<HTMLSpanElement | null>(null);

 useEffect(() => {
 if (!open || body !== null) return;
 setLoading(true);
 setError(null);
 fetch(`/api/prompts/${encodeURIComponent(props.promptName)}`)
 .then((response) => response.json())
 .then((payload) => {
 if (payload?.success) {
 setBody(payload.body || "");
 setPath(payload.path || null);
 } else {
 setError(payload?.error || "fetch failed");
 }
 })
 .catch((reason) => setError(reason?.message || "network error"))
 .finally(() => setLoading(false));
 }, [open, props, body]);

 useEffect(() => {
 if (!open) return;
 const onDown = (event: MouseEvent) => {
 if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
 setOpen(false);
 }
 };
 const onKey = (event: KeyboardEvent) => {
 if (event.key === "Escape") setOpen(false);
 };
 document.addEventListener("mousedown", onDown);
 document.addEventListener("keydown", onKey);
 return () => {
 document.removeEventListener("mousedown", onDown);
 document.removeEventListener("keydown", onKey);
 };
 }, [open]);

 const title = `Show the exact prompt used to generate ${props.label}`;

 return (
 <span ref={wrapRef} className="relative inline-flex">
 <button
 type="button"
 onClick={() => setOpen((value) => !value)}
 className="inline-flex items-center justify-center w-4 h-4 rounded-full border text-[10px] leading-none transition-colors hover:bg-white/10"
 style={{
 color: props.color || "currentColor",
 borderColor: "currentColor",
 opacity: 0.55,
 }}
 title={title}
 aria-label={title}
 aria-expanded={open}
 >
 i
 </button>
 {open && (
 <div
 role="dialog"
 aria-label={`${props.label} prompt`}
 className="absolute z-50 top-full mt-2 border border-white/15 bg-[#0A0A0C] shadow-2xl normal-case tracking-normal"
 style={{
 maxHeight: "min(60vh, 32rem)",
 left: 0,
 width: "min(48rem, 90vw)",
 }}
 >
 <div className="flex items-baseline justify-between gap-3 px-4 py-2.5 border-b border-white/10">
 <div className="flex flex-col">
 <span className="text-[12px] text-white/45">Prompt for</span>
 <span className="text-[14px] text-white/90 font-semibold tracking-tight">
 {props.label}
 </span>
 {path && (
 <span className="text-[11px] text-white/35 font-mono mt-0.5">
 {path}
 </span>
 )}
 </div>
 <button
 type="button"
 onClick={() => setOpen(false)}
 className="text-[12px] normal-case text-white/55 hover:text-white border border-white/15 hover:border-white/35 px-2.5 py-1 rounded"
 title="Close (Esc)"
 >
 Close
 </button>
 </div>
 <div
 className="overflow-y-auto px-4 py-3"
 style={{ maxHeight: "calc(min(60vh, 32rem) - 64px)" }}
 >
 {loading && (
 <div className="py-3 text-[12px] text-white/55 italic">
 Loading prompt…
 </div>
 )}
 {error && (
 <div className="py-3 text-[12px] text-[#EF4444]">
 Failed to load: {error}
 </div>
 )}
 {!loading && !error && body !== null && (
 <pre
 className="text-[13px] text-white/80 font-mono whitespace-pre-wrap leading-relaxed"
 style={{ tabSize: 2 }}
 >
 {body}
 </pre>
 )}
 </div>
 </div>
 )}
 </span>
 );
}

import { useEffect, useState } from "react";

export interface EpisodeAuditSummary {
 verdict: "no_catches" | "flags" | "incomplete";
 run_status: string;
 findings_count: number;
 open_findings_count: number;
 suggestions_count: number;
 deterministic_flags_count: number;
 created_at: string;
}

interface DeterministicCheck {
 status?: string;
 [key: string]: unknown;
}

export interface EpisodeAuditRun {
 run_id?: string | number;
 verdict: "no_catches" | "flags" | "incomplete";
 run_status: string;
 findings_count: number;
 deterministic_flags_count: number;
 started_at_utc?: string;
 created_at?: string;
 duration_seconds?: number | null;
 report: {
 llm: {
 findings?: string[];
 open_findings?: string[];
 suggestions?: string[];
 verdict_line?: string;
 proposals?: EpisodeAuditProposal[];
 no_safe_proposals?: NoSafeProposal[];
 };
 deterministic: {
 entropy?: DeterministicCheck;
 entity_consistency?: DeterministicCheck;
 locator_existence?: DeterministicCheck;
 quote_existence?: DeterministicCheck;
 provenance?: DeterministicCheck;
 valid_empty?: DeterministicCheck;
 };
 };
}

export type AuditResponse =
 | { status: "ok"; run: EpisodeAuditRun }
 | { status: "none"; meeting_id?: number };

interface EpisodeAuditCardProps {
 meetingId: number | null;
 onClose: () => void;
 isPublished?: boolean;
}

interface EpisodeAuditProposal {
 id: string | number;
 finding_number: string | number;
 target_output: string;
 before: string;
 after: string;
 fix_rationale: string;
 validated: boolean;
 apply_gated: boolean;
 checks?: unknown;
 parse_ok?: boolean;
 delimiters_ok?: boolean;
}

interface NoSafeProposal {
 finding_number: string | number;
 reason: string;
}

const CHECK_LABELS = {
 entropy: "Transcript machine-loop scan",
 entity_consistency: "Cross-output name check",
 locator_existence: "Timecode citations",
 quote_existence: "Quoted evidence",
 valid_empty: "Legitimately-empty outputs",
} as const;

function cleanMarkdown(value: string): string {
 return value
 .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
 .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
 .replace(/<[^>]+>/g, " ")
 .replace(/^\s{0,3}(?:#{1,6}|>|[-+*])\s+/gm, "")
 .replace(/[*_~`]/g, "")
 .replace(/\s+\n/g, "\n")
 .trim();
}

function countArray(value: unknown): number {
 return Array.isArray(value) ? value.length : 0;
}

function humanizeOutput(value: unknown): string {
 return String(value ?? "")
 .replace(/[_-]+/g, " ")
 .replace(/\s+/g, " ")
 .trim();
}

function checkStatus(value: unknown): "passed" | "failed" | null {
 if (typeof value === "boolean") return value ? "passed" : "failed";
 if (typeof value !== "string") return null;
 const normalized = value.toLowerCase();
 if (["passed", "pass", "ok", "verified", "true", "completed"].includes(normalized)) {
 return "passed";
 }
 if (["failed", "fail", "false", "error", "invalid"].includes(normalized)) {
 return "failed";
 }
 return null;
}

function proposalCheckNames(
 checks: unknown,
 validated: boolean,
): { passed: string[]; failed: string[] } {
 const passed: string[] = [];
 const failed: string[] = [];
 const add = (name: unknown, status: "passed" | "failed") => {
 const label = humanizeOutput(name);
 if (!label) return;
 const target = status === "passed" ? passed : failed;
 if (!target.includes(label)) target.push(label);
 };

 if (Array.isArray(checks)) {
 for (const check of checks) {
 if (typeof check === "string") {
 add(check, validated ? "passed" : "failed");
 continue;
 }
 if (!check || typeof check !== "object") continue;
 const record = check as Record<string, unknown>;
 const status =
 checkStatus(record.passed) ??
 checkStatus(record.status) ??
 (validated ? "passed" : "failed");
 add(record.name ?? record.check ?? record.id, status);
 }
 } else if (checks && typeof checks === "object") {
 for (const [name, value] of Object.entries(
 checks as Record<string, unknown>,
 )) {
 const record =
 value && typeof value === "object"
 ? (value as Record<string, unknown>)
 : null;
 const status =
 checkStatus(value) ??
 checkStatus(record?.passed) ??
 checkStatus(record?.status);
 if (status) add(name, status);
 }
 }

 return { passed, failed };
}

function responseOutcome(body: unknown): string {
 if (!body || typeof body !== "object") return "";
 const record = body as Record<string, unknown>;
 const outcomes: string[] = [];
 for (const value of [record.status, record.code, record.result, record.error]) {
 if (typeof value === "string") outcomes.push(value);
 if (value && typeof value === "object") {
 const nested = value as Record<string, unknown>;
 if (typeof nested.code === "string") outcomes.push(nested.code);
 if (typeof nested.status === "string") outcomes.push(nested.status);
 }
 }
 return (
 outcomes.find(value =>
 [
 "applied",
 "already_applied",
 "adapter_deferred",
 "cas_conflict",
 "validation_failed",
 ].includes(value),
 ) ??
 outcomes[0] ??
 ""
 );
}

function responseFailedChecks(body: unknown): string[] {
 if (!body || typeof body !== "object") return [];
 const record = body as Record<string, unknown>;
 const details =
 record.details && typeof record.details === "object"
 ? (record.details as Record<string, unknown>)
 : {};
 const raw = record.failed_checks ?? details.failed_checks;
 if (Array.isArray(raw)) {
 return raw.map(humanizeOutput).filter(Boolean);
 }
 return proposalCheckNames(record.checks ?? details.checks, false).failed;
}

function uncheckableSentence(
 key: keyof typeof CHECK_LABELS,
 check: DeterministicCheck | undefined,
): string | null {
 return check?.status === "uncheckable"
 ? `${CHECK_LABELS[key]}: could not run.`
 : null;
}

export function buildDeterministicSentences(
 checks: EpisodeAuditRun["report"]["deterministic"],
): string[] {
 const sentences: string[] = [];

 const entity = checks.entity_consistency;
 const entityUnavailable = uncheckableSentence("entity_consistency", entity);
 if (entityUnavailable) {
 sentences.push(entityUnavailable);
 } else if (entity) {
 const collisions = Array.isArray(entity.variant_collisions)
 ? entity.variant_collisions
 : [];
 if (collisions.length === 0) {
 sentences.push("Cross-output name check: no spelling conflicts.");
 } else {
 const details = collisions.map(rawCollision => {
 const collision =
 rawCollision && typeof rawCollision === "object"
 ? (rawCollision as Record<string, unknown>)
 : {};
 const outputs =
 collision.outputs && typeof collision.outputs === "object"
 ? (collision.outputs as Record<string, unknown>)
 : {};
 const involvedOutputs = Array.from(
 new Set(
 Object.values(outputs)
 .flatMap(value => (Array.isArray(value) ? value : []))
 .map(humanizeOutput),
 ),
 );
 const outputNote =
 involvedOutputs.length > 0 ? ` (${involvedOutputs.join(", ")})` : "";
 const spellings = Array.isArray(collision.spellings)
 ? collision.spellings.map(value => String(value ?? ""))
 : [];
 return `"${spellings[0] ?? ""}" vs "${spellings[1] ?? ""}"${outputNote}`;
 });
 sentences.push(
 `Cross-output name check: ${collisions.length} conflict${collisions.length === 1 ? "" : "s"} — ${details.join("; ")}.`,
 );
 }
 }

 const locators = checks.locator_existence;
 const locatorUnavailable = uncheckableSentence("locator_existence", locators);
 if (locatorUnavailable) {
 sentences.push(locatorUnavailable);
 } else if (locators) {
 const checked =
 typeof locators.citations_checked === "number"
 ? locators.citations_checked
 : 0;
 const outside = countArray(locators.out_of_range);
 sentences.push(
 outside === 0
 ? `Timecode citations: ${checked} checked, all inside the meeting.`
 : `Timecode citations: ${checked} checked; ${outside} point${outside === 1 ? "" : "s"} outside the meeting's timeline.`,
 );
 }

 const quotes = checks.quote_existence;
 const quoteUnavailable = uncheckableSentence("quote_existence", quotes);
 if (quoteUnavailable) {
 sentences.push(quoteUnavailable);
 } else if (quotes) {
 const checked = countArray(quotes.quotes_checked);
 const missing = countArray(quotes.llm_evidence_not_found);
 sentences.push(
 missing === 0
 ? `Quoted evidence: ${checked} passages checked; all matched.`
 : `Quoted evidence: ${checked} passages checked; ${missing} could not be matched verbatim.`,
 );
 }

 const entropy = checks.entropy;
 const entropyUnavailable = uncheckableSentence("entropy", entropy);
 if (entropyUnavailable) {
 sentences.push(entropyUnavailable);
 } else if (entropy) {
 const regionCount = countArray(entropy.regions);
 const lowCount =
 typeof entropy.low_entropy_window_count === "number"
 ? entropy.low_entropy_window_count
 : regionCount;
 sentences.push(
 regionCount === 0 || lowCount === 0
 ? "Transcript machine-loop scan: clean."
 : `Transcript machine-loop scan: ${regionCount} low-entropy region${regionCount === 1 ? "" : "s"} noted.`,
 );
 }

 const provenance = checks.provenance;
 if (provenance) {
 sentences.push(
 provenance.status === "recorded"
 ? "Generation provenance: recorded."
 : "Generation provenance: not recorded for this meeting (pre-instrumentation).",
 );
 }

 const validEmpty = checks.valid_empty;
 const validEmptyUnavailable = uncheckableSentence("valid_empty", validEmpty);
 if (validEmptyUnavailable) {
 sentences.push(validEmptyUnavailable);
 } else if (validEmpty) {
 const outputs = Array.isArray(validEmpty.valid_empty)
 ? validEmpty.valid_empty.map(humanizeOutput)
 : [];
 if (outputs.length > 0) {
 sentences.push(`Legitimately-empty outputs: ${outputs.join(", ")}.`);
 }
 }

 return sentences;
}

function ProseBlock({ text }: { text: string }) {
 const lines = cleanMarkdown(text)
 .split(/\r?\n/)
 .map(line => line.trim());
 const headline = lines.find(line => line.length > 0) ?? "";
 const headlineIndex = lines.indexOf(headline);
 const body = lines
 .slice(headlineIndex + 1)
 .join("\n")
 .split(/\n\s*\n/)
 .map(paragraph => paragraph.trim())
 .filter(Boolean);

 return (
 <div className="space-y-2">
 <p className="font-semibold text-white">{headline}</p>
 {body.map((paragraph, index) => (
 <p
 key={`${index}-${paragraph.slice(0, 20)}`}
 className="text-gray-300 whitespace-pre-line leading-relaxed"
 >
 {paragraph}
 </p>
 ))}
 </div>
 );
}

function ProseList({ items }: { items: string[] }) {
 return (
 <ol className="space-y-4">
 {items.map((item, index) => (
 <li
 key={`${index}-${item.slice(0, 30)}`}
 className="grid grid-cols-[1.5rem_1fr] gap-2 text-[13px]"
 >
 <span className="text-gray-500 tabular-nums">{index + 1}.</span>
 <ProseBlock text={item} />
 </li>
 ))}
 </ol>
 );
}

export type ProposalPhase =
 | "idle"
 | "confirming"
 | "rejecting"
 | "applied"
 | "rejected"
 | "deferred"
 | "adapter_deferred"
 | "validation_failed"
 | "cas_conflict"
 | "apply_error"
 | "disposition_error";

interface ProposalUiState {
 phase: ProposalPhase;
 busy: boolean;
 reason: string;
 failedChecks: string[];
}

const INITIAL_PROPOSAL_STATE: ProposalUiState = {
 phase: "idle",
 busy: false,
 reason: "",
 failedChecks: [],
};

export function nextApprovalPhase(isPublished: boolean): ProposalPhase {
 return isPublished ? "confirming" : "idle";
}

export async function applyEpisodeAuditFix({
 meetingId,
 runId,
 proposalId,
}: {
 meetingId?: number;
 runId?: string | number;
 proposalId: string | number;
}): Promise<Pick<ProposalUiState, "phase" | "failedChecks">> {
 try {
 const response = await fetch(
 `/api/episode-audit/${meetingId}/apply-fix`,
 {
 method: "POST",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify({
 run_id: runId,
 proposal_id: proposalId,
 }),
 },
 );
 const body = (await response.json().catch(() => null)) as unknown;
 const outcome = responseOutcome(body);

 if (
 response.ok &&
 (outcome === "applied" || outcome === "already_applied")
 ) {
 return { phase: "applied", failedChecks: [] };
 }
 if (response.status === 409 && outcome === "adapter_deferred") {
 return { phase: "adapter_deferred", failedChecks: [] };
 }
 if (response.status === 409 && outcome === "cas_conflict") {
 return { phase: "cas_conflict", failedChecks: [] };
 }
 if (response.status === 422) {
 return {
 phase: "validation_failed",
 failedChecks: responseFailedChecks(body),
 };
 }
 return { phase: "apply_error", failedChecks: [] };
 } catch {
 return { phase: "apply_error", failedChecks: [] };
 }
}

export async function submitEpisodeAuditDisposition({
 meetingId,
 runId,
 proposalId,
 disposition,
 reason = "",
}: {
 meetingId?: number;
 runId?: string | number;
 proposalId: string | number;
 disposition: "rejected" | "deferred";
 reason?: string;
}): Promise<"rejected" | "deferred" | "blocked" | "error"> {
 const trimmedReason = reason.trim();
 if (disposition === "rejected" && !trimmedReason) return "blocked";

 try {
 const response = await fetch(
 `/api/episode-audit/${meetingId}/disposition`,
 {
 method: "POST",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify({
 run_id: runId,
 proposal_id: proposalId,
 disposition,
 ...(disposition === "rejected" ? { reason: trimmedReason } : {}),
 }),
 },
 );
 return response.ok ? disposition : "error";
 } catch {
 return "error";
 }
}

export function PublishedRecordConfirm({
 onApply,
 onCancel,
}: {
 onApply: () => void;
 onCancel: () => void;
}) {
 return (
 <div className="flex flex-wrap items-center gap-2 text-[11px] text-amber-200">
 <span>This edits a published public record.</span>
 <button
 type="button"
 onClick={onApply}
 className="rounded border border-amber-300/40 px-2.5 py-1 font-semibold text-amber-100 hover:border-amber-200"
 >
 Apply anyway
 </button>
 <button
 type="button"
 onClick={onCancel}
 className="px-2 py-1 text-gray-400 hover:text-white"
 >
 Cancel
 </button>
 </div>
 );
}

function ProposalPanel({
 proposal,
 meetingId,
 runId,
 isPublished,
}: {
 proposal: EpisodeAuditProposal;
 meetingId?: number;
 runId?: string | number;
 isPublished: boolean;
}) {
 const [ui, setUi] = useState<ProposalUiState>(INITIAL_PROPOSAL_STATE);
 const targetOutput = humanizeOutput(proposal.target_output);
 const proposalChecks = proposalCheckNames(
 proposal.checks,
 proposal.validated,
 );
 const updateUi = (patch: Partial<ProposalUiState>) => {
 setUi(current => ({ ...current, ...patch }));
 };

 const applyFix = async () => {
 updateUi({ busy: true, phase: "idle", failedChecks: [] });
 const result = await applyEpisodeAuditFix({
 meetingId,
 runId,
 proposalId: proposal.id,
 });
 updateUi({ busy: false, ...result });
 };

 const submitDisposition = async (
 disposition: "rejected" | "deferred",
 ) => {
 const reason = ui.reason.trim();
 if (disposition === "rejected" && !reason) return;
 updateUi({ busy: true });
 const result = await submitEpisodeAuditDisposition({
 meetingId,
 runId,
 proposalId: proposal.id,
 disposition,
 reason,
 });
 if (result === "error") {
 updateUi({ busy: false, phase: "disposition_error" });
 return;
 }
 if (result === "blocked") {
 updateUi({ busy: false });
 return;
 }
 updateUi({ busy: false, phase: result });
 };

 const beginApprove = () => {
 const phase = nextApprovalPhase(isPublished);
 if (phase === "confirming") {
 updateUi({ phase });
 } else {
 void applyFix();
 }
 };

 const statusMessage =
 ui.phase === "adapter_deferred"
 ? `Fix validated — applying to ${targetOutput} arrives with its adapter`
 : ui.phase === "validation_failed"
 ? "Validation failed at apply time — content may have changed; re-run the audit."
 : ui.phase === "cas_conflict"
 ? "Content changed underneath — re-run the audit."
 : ui.phase === "apply_error"
 ? "Couldn't apply — try again."
 : ui.phase === "disposition_error"
 ? "Couldn't update disposition — try again."
 : null;

 const renderActions = () => {
 if (ui.phase === "applied") {
 return (
 <div className="space-y-1.5">
 <button
 type="button"
 disabled
 className="rounded border border-emerald-400/30 bg-emerald-400/10 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-emerald-300"
 >
 ✓ Applied
 </button>
 <p className="text-[11px] text-gray-400">
 The next audit run covers the corrected episode.
 </p>
 </div>
 );
 }
 if (ui.phase === "rejected") {
 return <p className="text-[11px] text-red-300">✗ Rejected</p>;
 }
 if (ui.phase === "deferred") {
 return <p className="text-[11px] text-gray-400">Deferred</p>;
 }
 if (ui.phase === "confirming") {
 return (
 <PublishedRecordConfirm
 onApply={() => void applyFix()}
 onCancel={() => updateUi({ phase: "idle" })}
 />
 );
 }
 if (ui.phase === "rejecting") {
 return (
 <div className="flex items-center gap-2">
 <input
 autoFocus
 type="text"
 value={ui.reason}
 disabled={ui.busy}
 onChange={event => updateUi({ reason: event.target.value })}
 onKeyDown={event => {
 if (event.key !== "Enter") return;
 event.preventDefault();
 void submitDisposition("rejected");
 }}
 placeholder="Reason required — press Enter"
 aria-label="Rejection reason"
 className="min-w-0 flex-1 rounded border border-white/15 bg-black/20 px-2.5 py-1.5 text-[11px] text-white placeholder:text-gray-600 focus:border-red-300/50 focus:outline-none"
 />
 <button
 type="button"
 disabled={ui.busy}
 onClick={() => updateUi({ phase: "idle", reason: "" })}
 className="text-[11px] text-gray-500 hover:text-white disabled:opacity-50"
 >
 Cancel
 </button>
 </div>
 );
 }

 const canApprove = proposal.validated && !proposal.apply_gated;
 return (
 <div className="flex flex-wrap items-center gap-2">
 {canApprove && (
 <button
 type="button"
 disabled={ui.busy}
 onClick={beginApprove}
 className="rounded bg-emerald-500 px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-black hover:bg-emerald-400 disabled:cursor-wait disabled:opacity-50"
 >
 {ui.busy ? "APPLYING…" : "APPROVE FIX"}
 </button>
 )}
 <button
 type="button"
 disabled={ui.busy}
 onClick={() => updateUi({ phase: "rejecting" })}
 className="rounded border border-red-300/25 px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-red-300 hover:border-red-300/60 disabled:cursor-wait disabled:opacity-50"
 >
 Reject
 </button>
 <button
 type="button"
 disabled={ui.busy}
 onClick={() => void submitDisposition("deferred")}
 className="rounded border border-white/15 px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-gray-300 hover:border-white/35 hover:text-white disabled:cursor-wait disabled:opacity-50"
 >
 Defer
 </button>
 </div>
 );
 };

 return (
 <div className="mt-3 rounded-lg border border-white/10 bg-white/[0.025] p-3.5 space-y-3">
 <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-gray-400">
 Proposed fix · {targetOutput}
 </p>
 <div className="space-y-2 text-[12px] leading-relaxed">
 <p className="rounded bg-red-400/[0.07] px-2.5 py-2 text-red-200/75 line-through decoration-red-300/70">
 {cleanMarkdown(proposal.before)}
 </p>
 <p className="rounded bg-emerald-400/[0.09] px-2.5 py-2 text-emerald-100">
 {cleanMarkdown(proposal.after)}
 </p>
 </div>
 <p className="text-[12px] leading-relaxed text-gray-300">
 {cleanMarkdown(proposal.fix_rationale)}
 </p>
 {proposalChecks.passed.length > 0 && (
 <p className="text-[10px] text-gray-500">
 verified: {proposalChecks.passed.join(" · ")}
 </p>
 )}
 {!proposal.validated && (
 <div className="space-y-1 text-[11px] text-amber-200/80">
 <p>Could not be machine-verified — review manually</p>
 {proposalChecks.failed.length > 0 && (
 <p className="text-gray-500">
 failed: {proposalChecks.failed.join(" · ")}
 </p>
 )}
 </div>
 )}
 {proposal.validated && proposal.apply_gated && (
 <p className="text-[11px] text-gray-500">
 Fix validated — applying to {targetOutput} arrives with its adapter
 </p>
 )}
 {statusMessage && (
 <div className="space-y-1 text-[11px] text-amber-200/80">
 <p>{statusMessage}</p>
 {ui.failedChecks.length > 0 && (
 <p className="text-gray-500">
 failed: {ui.failedChecks.join(" · ")}
 </p>
 )}
 </div>
 )}
 {renderActions()}
 </div>
 );
}

function FindingList({
 items,
 proposals,
 noSafeProposals,
 meetingId,
 runId,
 isPublished,
}: {
 items: string[];
 proposals: EpisodeAuditProposal[];
 noSafeProposals: NoSafeProposal[];
 meetingId?: number;
 runId?: string | number;
 isPublished: boolean;
}) {
 return (
 <ol className="space-y-4">
 {items.map((item, index) => {
 const findingNumber = String(index + 1);
 const matchedProposals = proposals.filter(
 proposal =>
 proposal.parse_ok !== false &&
 String(proposal.finding_number) === findingNumber,
 );
 const matchedNoSafe = noSafeProposals.filter(
 proposal => String(proposal.finding_number) === findingNumber,
 );

 return (
 <li
 key={`${index}-${item.slice(0, 30)}`}
 className="grid grid-cols-[1.5rem_1fr] gap-2 text-[13px]"
 >
 <span className="text-gray-500 tabular-nums">{index + 1}.</span>
 <div>
 <ProseBlock text={item} />
 {matchedProposals.map(proposal => (
 <ProposalPanel
 key={String(proposal.id)}
 proposal={proposal}
 meetingId={meetingId}
 runId={runId}
 isPublished={isPublished}
 />
 ))}
 {matchedNoSafe.map((entry, noSafeIndex) => (
 <p
 key={`${entry.finding_number}-${noSafeIndex}`}
 className="mt-3 text-[11px] text-gray-500"
 >
 No safe automatic fix — {cleanMarkdown(entry.reason)}
 </p>
 ))}
 </div>
 </li>
 );
 })}
 </ol>
 );
}

function verdictHeading(run: EpisodeAuditRun): string {
 if (run.verdict === "no_catches") return "No catches";
 if (run.verdict === "incomplete") return "Audit incomplete";
 const flags = run.findings_count + run.deterministic_flags_count;
 return `${flags} flag${flags === 1 ? "" : "s"}`;
}

function formatRunLine(run: EpisodeAuditRun): string {
 const timestamp = run.started_at_utc || run.created_at;
 const dateText = timestamp
 ? new Date(timestamp).toLocaleString(undefined, {
 dateStyle: "medium",
 timeStyle: "short",
 })
 : "Time not recorded";
 const duration =
 typeof run.duration_seconds === "number"
 ? `${run.duration_seconds.toFixed(1)} seconds`
 : "duration not recorded";
 return `${dateText} · ${duration}`;
}

export function EpisodeAuditCardBody({
 response,
 meetingId,
 isPublished = false,
}: {
 response: AuditResponse;
 meetingId?: number;
 isPublished?: boolean;
}) {
 if (response.status === "none") {
 return (
 <p className="px-6 py-8 text-[14px] text-gray-400">
 No audit has been run for this meeting yet.
 </p>
 );
 }

 const run = response.run;
 const findings = run.report.llm.findings ?? [];
 const openFindings = run.report.llm.open_findings ?? [];
 const suggestions = run.report.llm.suggestions ?? [];
 const proposals = run.report.llm.proposals ?? [];
 const noSafeProposals = run.report.llm.no_safe_proposals ?? [];
 const deterministicSentences = buildDeterministicSentences(
 run.report.deterministic,
 );

 return (
 <>
 <div className="px-6 py-5 border-b border-white/5">
 <h2
 id="episode-audit-title"
 className="text-[19px] text-white font-semibold"
 >
 Episode audit — {verdictHeading(run)}
 </h2>
 {run.report.llm.verdict_line && (
 <p className="text-[14px] text-gray-300 mt-2 leading-relaxed">
 {cleanMarkdown(run.report.llm.verdict_line)}
 </p>
 )}
 <p className="text-[11px] text-gray-500 mt-2">
 {formatRunLine(run)}
 </p>
 </div>

 <div className="px-6 py-5 space-y-6 overflow-y-auto">
 <section>
 <h3 className="text-[11px] uppercase tracking-[0.18em] text-amber-300 font-semibold mb-3">
 Findings
 </h3>
 {findings.length > 0 ? (
 <FindingList
 items={findings}
 proposals={proposals}
 noSafeProposals={noSafeProposals}
 meetingId={meetingId}
 runId={run.run_id}
 isPublished={isPublished}
 />
 ) : (
 <p className="text-[13px] text-gray-500">No findings.</p>
 )}
 </section>

 {openFindings.length > 0 && (
 <details className="border-t border-white/5 pt-4">
 <summary className="cursor-pointer text-[12px] text-gray-300 hover:text-white">
 Open findings ({openFindings.length})
 </summary>
 <div className="mt-4">
 <ProseList items={openFindings} />
 </div>
 </details>
 )}

 {suggestions.length > 0 && (
 <details className="border-t border-white/5 pt-4">
 <summary className="cursor-pointer text-[12px] text-gray-300 hover:text-white">
 Suggestions ({suggestions.length})
 </summary>
 <div className="mt-4">
 <ProseList items={suggestions} />
 </div>
 </details>
 )}

 <section className="border-t border-white/5 pt-4">
 <h3 className="text-[11px] uppercase tracking-[0.18em] text-gray-400 font-semibold mb-3">
 Deterministic checks
 </h3>
 <ul className="space-y-2 text-[13px] text-gray-300 leading-relaxed">
 {deterministicSentences.map(sentence => (
 <li key={sentence}>{sentence}</li>
 ))}
 </ul>
 </section>
 </div>
 </>
 );
}

export default function EpisodeAuditCard({
 meetingId,
 onClose,
 isPublished = false,
}: EpisodeAuditCardProps) {
 const [response, setResponse] = useState<AuditResponse | null>(null);
 const [failed, setFailed] = useState(false);

 useEffect(() => {
 if (meetingId === null) return;
 const controller = new AbortController();
 setResponse(null);
 setFailed(false);

 void fetch(`/api/episode-audit/${meetingId}`, {
 signal: controller.signal,
 })
 .then(async result => {
 if (!result.ok) throw new Error(`Audit request failed (${result.status})`);
 const body = (await result.json()) as AuditResponse;
 if (body.status !== "ok" && body.status !== "none") {
 throw new Error("Audit response was not recognized");
 }
 setResponse(body);
 })
 .catch(error => {
 if (error instanceof DOMException && error.name === "AbortError") return;
 setFailed(true);
 });

 return () => controller.abort();
 }, [meetingId]);

 useEffect(() => {
 if (meetingId === null) return;
 const onKey = (event: KeyboardEvent) => {
 if (event.key === "Escape") onClose();
 };
 const previousOverflow = document.body.style.overflow;
 document.body.style.overflow = "hidden";
 window.addEventListener("keydown", onKey);
 return () => {
 document.body.style.overflow = previousOverflow;
 window.removeEventListener("keydown", onKey);
 };
 }, [meetingId, onClose]);

 if (meetingId === null) return null;

 return (
 <div
 className="fixed inset-0 z-[110] bg-black/90 backdrop-blur-sm flex items-center justify-center p-4 sm:p-8 font-sans"
 role="dialog"
 aria-modal="true"
 aria-labelledby="episode-audit-title"
 onClick={event => {
 if (event.target === event.currentTarget) onClose();
 }}
 >
 <div className="max-w-2xl w-full max-h-full flex flex-col rounded-xl border border-white/10 shadow-2xl overflow-hidden bg-[#0F141B]">
 <div className="flex items-center justify-between px-6 py-3 border-b border-white/5 bg-[#141416]">
 <span className="text-[11px] uppercase tracking-[0.2em] text-gray-400">
 Meeting #{meetingId}
 </span>
 <button
 type="button"
 onClick={onClose}
 className="text-[11px] uppercase tracking-widest text-gray-300 hover:text-white border border-white/10 hover:border-white/30 px-3 py-1 rounded"
 title="Close (Esc)"
 aria-label="Close episode audit"
 >
 × Close
 </button>
 </div>

 {failed ? (
 <p className="px-6 py-8 text-[14px] text-gray-300">
 Couldn't load the audit — try again.
 </p>
 ) : response ? (
 <EpisodeAuditCardBody
 response={response}
 meetingId={meetingId}
 isPublished={isPublished}
 />
 ) : (
 <p className="px-6 py-8 text-[14px] text-gray-400 flex items-center gap-2">
 <span className="w-3 h-3 rounded-full border border-gray-500 border-t-transparent animate-spin" />
 Loading the episode audit…
 </p>
 )}
 </div>
 </div>
 );
}

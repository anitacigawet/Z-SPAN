import { useCallback, useEffect, useState } from "react";

type LibrarianAccess = "requested" | "granted" | "banned";
type Decision = "grant" | "deny" | "ban";

interface AccessRequest {
 id: number;
 email: string;
 display_name: string | null;
 librarian_access: LibrarianAccess;
 active_auto_ban: boolean;
 abuse_evidence: {
 refused_count: number;
 duplicate_suppressed_count: number;
 burst_count: number;
 window_started_at: string;
 window_ended_at: string;
 samples: Array<{
 reason_code: string;
 matched_rule_id: string;
 }>;
 thresholds: Record<string, number> | null;
 } | null;
}

interface AccessRequestsResponse {
 success: boolean;
 requests: AccessRequest[];
}

function stateLabel(request: AccessRequest): string {
 if (request.active_auto_ban) {
 return "Auto-banned: rejected-query flood";
 }
 return (
 request.librarian_access.charAt(0).toUpperCase() +
 request.librarian_access.slice(1)
 );
}

const SMALL_COUNTS = [
 "zero",
 "one",
 "two",
 "three",
 "four",
 "five",
 "six",
 "seven",
 "eight",
 "nine",
 "ten",
];

function abuseEvidenceProse(request: AccessRequest): string | null {
 const evidence = request.abuse_evidence;
 if (!request.active_auto_ban || !evidence) {
 return null;
 }
 const started = Date.parse(
 `${evidence.window_started_at.replace(" ", "T")}Z`,
 );
 const ended = Date.parse(`${evidence.window_ended_at.replace(" ", "T")}Z`);
 const elapsedSeconds =
 Number.isFinite(started) && Number.isFinite(ended)
 ? Math.max(1, Math.ceil((ended - started) / 1000))
 : 0;
 const duration =
 elapsedSeconds < 3600
 ? `${Math.max(1, Math.ceil(elapsedSeconds / 60))} ${
 elapsedSeconds <= 60 ? "minute" : "minutes"
 }`
 : `${Math.max(1, Math.ceil(elapsedSeconds / 3600))} ${
 elapsedSeconds <= 3600 ? "hour" : "hours"
 }`;
 const burstCount =
 SMALL_COUNTS[evidence.burst_count] ?? String(evidence.burst_count);
 return (
 `Refused ${evidence.refused_count} malformed questions in ${duration} ` +
 `across ${burstCount} ${
 evidence.burst_count === 1 ? "burst" : "bursts"
 }.`
 );
}

export default function LibrarianAccessRequests() {
 const [requests, setRequests] = useState<AccessRequest[]>([]);
 const [loading, setLoading] = useState(true);
 const [error, setError] = useState("");
 const [actingOn, setActingOn] = useState<number | null>(null);

 const refresh = useCallback(async () => {
 setError("");
 try {
 const response = await fetch("/api/librarian/access-requests", {
 credentials: "include",
 });
 const body = (await response.json().catch(() => ({}))) as Partial<
 AccessRequestsResponse
 > & { error?: string };
 if (!response.ok || !body.success || !Array.isArray(body.requests)) {
 throw new Error(body.error || "Failed to load Librarian access requests.");
 }
 setRequests(body.requests);
 } catch (caught) {
 setError(
 caught instanceof Error
 ? caught.message
 : "Failed to load Librarian access requests.",
 );
 } finally {
 setLoading(false);
 }
 }, []);

 useEffect(() => {
 void refresh();
 }, [refresh]);

 const decide = useCallback(
 async (userId: number, action: Decision) => {
 setActingOn(userId);
 setError("");
 try {
 const response = await fetch(
 `/api/librarian/access-requests/${userId}/decide`,
 {
 method: "POST",
 credentials: "include",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify({ action }),
 },
 );
 const body = (await response.json().catch(() => ({}))) as {
 success?: boolean;
 error?: string;
 };
 if (!response.ok || !body.success) {
 throw new Error(body.error || "The Librarian access decision failed.");
 }
 await refresh();
 } catch (caught) {
 setError(
 caught instanceof Error
 ? caught.message
 : "The Librarian access decision failed.",
 );
 } finally {
 setActingOn(null);
 }
 },
 [refresh],
 );

 return (
 <section
 className="flex-none px-8 py-3 border-b border-amber-400/30 bg-amber-400/5"
 aria-label="Librarian access requests"
 >
 <header className="flex items-center justify-between gap-4 mb-2">
 <span className="text-[10px] uppercase tracking-[0.18em] text-amber-200/80">
 Librarian access
 </span>
 <button
 type="button"
 onClick={() => void refresh()}
 disabled={loading}
 className="text-[11px] uppercase tracking-widest text-amber-100/70 hover:text-amber-100 border border-amber-400/30 hover:border-amber-400/60 px-2.5 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 >
 Refresh
 </button>
 </header>

 {error && <p className="mb-2 text-[12px] text-red-300/90">{error}</p>}
 {loading ? (
 <p className="text-[12px] text-white/45">Loading access requests…</p>
 ) : requests.length === 0 ? (
 <p className="text-[12px] text-white/45">
 No Librarian access requests.
 </p>
 ) : (
 <ul className="flex flex-col gap-1.5">
 {requests.map((request) => {
 const busy = actingOn === request.id;
 const evidenceProse = abuseEvidenceProse(request);
 return (
 <li
 key={request.id}
 className="flex items-center justify-between gap-4 px-3 py-2 border border-white/10 bg-[#0E0B07]"
 >
 <div className="min-w-0">
 <div className="text-[12px] text-white/85 truncate">
 {request.display_name?.trim() || request.email}
 </div>
 <div className="text-[11px] text-white/40 truncate">
 {request.email} · {stateLabel(request)}
 </div>
 {evidenceProse && (
 <div className="mt-1 text-[11px] text-amber-100/65">
 {evidenceProse}
 </div>
 )}
 </div>
 <div className="flex flex-none items-center gap-1.5">
 {(
 [
 [
 "grant",
 request.active_auto_ban ? "Restore" : "Grant",
 ],
 ["deny", "Deny"],
 ["ban", "Ban"],
 ] as const
 ).map(([action, label]) => (
 <button
 key={action}
 type="button"
 onClick={() => void decide(request.id, action)}
 disabled={actingOn !== null}
 className={
 action === "ban"
 ? "text-[11px] text-red-200/85 hover:text-red-100 border border-red-300/30 hover:border-red-300/60 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 : "text-[11px] text-white/80 hover:text-white border border-white/20 hover:border-white/40 px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed"
 }
 >
 {busy ? "Working…" : label}
 </button>
 ))}
 </div>
 </li>
 );
 })}
 </ul>
 )}
 </section>
 );
}

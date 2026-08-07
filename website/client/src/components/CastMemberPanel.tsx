/**
 * CastMemberPanel — per-member view.
 *
 * (neutrality output cut): this surface no longer
 * renders a Z-SPAN-authored dossier — the attendance percentage, the
 * per-member verified-quote reels, and the accountability / tracked-claims
 * ledger were all removed. A curated per-person profile is editorial:
 * deciding "what's notable about this person" is a viewpoint, and the
 * decentralized-consensus audit can only verify outputs that
 * converge — it can't verify a curated dossier. So the surface now presents
 * the OFFICIAL record and points at it: the seat as the city publishes it,
 * plus links to the city's own government site and the source where the
 * seat was verified. Z-SPAN presents; it does not profile.
 *
 * Hide-not-delete: the per-member dossier still generates in the pipeline
 * and stays reachable to the operator through the owner-only Record
 * (TruthBook); only the presented surface is cut. The extraction pipeline,
 * the DB tables, and the /api/cast dossier fields are untouched.
 */
import { useEffect, useState } from "react";
import { ArrowLeft, ExternalLink } from "lucide-react";
import { OwnerOnly } from "./OwnerOnly";
import { fetchForPlane } from "../lib/planeFetch";

type MemberDetail = {
 city: string;
 county: string | null;
 state: string | null;
 //: the city's own official record, from city_intelligence
 // primary_source_url. Null when the recon file has no primary source yet.
 city_official_url: string | null;
 member: {
 id?: number;
 name: string;
 role: string | null;
 seat_id: string | null;
 term_started: string | null;
 term_ends: string | null;
 // The source where this seat/roster was verified (an agenda or the
 // official roster page). Per-member official *bio* links are a future
 // recon enrichment (A2 in the decision) — not on file yet.
 source_url: string | null;
 };
};

interface CastMemberPanelProps {
 cityName: string;
 seatId: string;
 onBack: () => void;
 // Owner-only Record (TruthBook) entry — the operator's per-member dossier
 // lives there (owner-only. The button renders only for the
 // owner; the public never sees it.
 onOpenTruthBook?: (topic?: string) => void;
}

function formatTerm(start: string | null, end: string | null): string {
 if (!start && !end) return "Term dates not on file";
 const s = start ?? "?";
 const e = end ?? "?";
 return `Term: ${s} – ${e}`;
}

function formatSeatLabel(seatId: string | null): string {
 if (!seatId) return "—";
 if (seatId === "mayor") return "Mayor's Seat";
 if (seatId === "vice_mayor") return "Vice Mayor's Seat";
 if (seatId.startsWith("seat_")) return `Seat ${seatId.slice(5)}`;
 return seatId;
}

export function SourceCitationDisclosure({ sourceUrl }: { sourceUrl: string }) {
 return (
 <details className="group text-[13px] text-foreground/75">
 <summary className="inline-flex cursor-pointer list-none items-center gap-2 transition-colors hover:text-white">
 <span>Where this seat was verified</span>
 <span
 aria-hidden="true"
 className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-current text-[10px] leading-none opacity-55 transition-colors group-hover:bg-white/10"
 >
 i
 </span>
 </summary>
 <code className="mt-2 block select-text break-all rounded border border-white/10 bg-black/20 px-3 py-2 text-[11px] font-normal text-foreground/60">
 {sourceUrl}
 </code>
 </details>
 );
}

// ── Main panel ──────────────────────────────────────────────────────

export default function CastMemberPanel({
 cityName,
 seatId,
 onBack,
 onOpenTruthBook,
}: CastMemberPanelProps) {
 const [detail, setDetail] = useState<MemberDetail | null>(null);
 const [loading, setLoading] = useState(false);
 const [error, setError] = useState<string | null>(null);

 useEffect(() => {
 let aborted = false;
 setLoading(true);
 setError(null);
 fetchForPlane({
 publicPath: `/public-api/cast/${encodeURIComponent(cityName)}/${encodeURIComponent(seatId)}`,
 operatorPath: `/api/cast/${encodeURIComponent(cityName)}/${encodeURIComponent(seatId)}`,
 })
 .then(res => {
 if (!res.ok) throw new Error(`HTTP ${res.status}`);
 return res.json();
 })
 .then((data: MemberDetail) => {
 if (aborted) return;
 setDetail(data);
 setLoading(false);
 })
 .catch(err => {
 if (aborted) return;
 setError(err.message || "Failed to load member");
 setLoading(false);
 });
 return () => {
 aborted = true;
 };
 }, [cityName, seatId]);

 if (loading) {
 return (
 <div className="py-16 text-center">
 <div className="kg-dots inline-flex">
 <span /> <span /> <span />
 </div>
 <p className="text-sm text-muted-foreground mt-4">
 Loading member record…
 </p>
 </div>
 );
 }

 if (error || !detail) {
 return (
 <div className="kg-card-2 border-dashed p-12 text-center">
 <h3 className="text-base font-semibold text-foreground/70 mb-1.5">
 Could not load this member
 </h3>
 <p className="text-sm text-muted-foreground mt-2">
 {error ?? "Unknown error"}
 </p>
 <button
 onClick={onBack}
 className="mt-4 inline-flex items-center gap-2 px-3 py-1.5 text-[11px] uppercase tracking-widest rounded-md border border-[var(--line)] bg-[var(--surface)] hover:bg-[var(--surface-3)] text-foreground/80 hover:text-white transition-colors"
 >
 <ArrowLeft className="w-3.5 h-3.5" /> Back to cast
 </button>
 </div>
 );
 }

 const { member } = detail;
 const initials = (member.name || "?")
 .split(/\s+/)
 .map(p => p[0])
 .filter(Boolean)
 .slice(0, 2)
 .join("")
 .toUpperCase();

 return (
 <div className="flex flex-col gap-8">
 {/* Back link */}
 <button
 onClick={onBack}
 className="self-start group flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-foreground/55 hover:text-white transition-colors"
 >
 <ArrowLeft className="w-3.5 h-3.5 group-hover:-translate-x-0.5 transition-transform" />
 Back to {cityName} cast
 </button>

 {/* Hero — the seat as the city publishes it. */}
 <header className="grid grid-cols-[88px_1fr] sm:grid-cols-[120px_1fr] gap-x-5 sm:gap-x-7 items-start">
 <div
 className="aspect-square rounded-lg border border-[var(--line)] flex items-center justify-center select-none"
 style={{
 background:
 "radial-gradient(circle at 30% 30%, rgba(255,255,255,0.05) 0%, rgba(0,0,0,0.0) 60%), var(--canvas)",
 }}
 aria-hidden="true"
 >
 <span className="text-[36px] sm:text-[44px] font-light text-white/35 tracking-tight">
 {initials}
 </span>
 </div>

 <div className="min-w-0">
 <p className="kg-eyebrow mb-1">
 {member.role ?? "Council"} · {formatSeatLabel(member.seat_id)}
 </p>
 <h1 className="text-[28px] sm:text-[34px] font-light text-white tracking-tight leading-tight">
 {member.name}
 </h1>
 <p className="text-[12px] uppercase tracking-[0.18em] text-foreground/45 mt-2">
 {formatTerm(member.term_started, member.term_ends)}
 </p>
 </div>
 </header>

 {/* The official record — links, not a profile. */}
 <section className="max-w-2xl">
 <header className="mb-3 border-b border-[var(--line)] pb-2">
 <p className="kg-eyebrow">The official record</p>
 </header>
 <p className="text-[13px] text-foreground/70 leading-relaxed">
 Z-SPAN doesn't write its own profile of {member.name}. What you see
 above is the seat as {cityName} publishes it. For the substance —
 how this member voted, what they said — open any meeting on this
 channel: every decision links back to the exact moment in the
 recording.
 </p>

 <div className="mt-5 flex flex-col gap-2.5">
 {detail.city_official_url && (
 <a
 href={detail.city_official_url}
 target="_blank"
 rel="noopener noreferrer"
 className="group inline-flex items-center gap-2 text-[13px] text-foreground/75 hover:text-white transition-colors"
 >
 <ExternalLink className="w-3.5 h-3.5 text-foreground/45 group-hover:text-white transition-colors" />
 <span>{cityName} — official government site</span>
 </a>
 )}
 {member.source_url && (
 <SourceCitationDisclosure sourceUrl={member.source_url} />
 )}
 {!detail.city_official_url && !member.source_url && (
 <p className="text-[12px] text-foreground/40 leading-relaxed">
 No official source link is on file for this city yet.
 </p>
 )}
 </div>

 {/* Owner-only entry to the per-member Record (TruthBook) — the
 dossier stays reachable to the operator, cut from the public
 surface */}
 {onOpenTruthBook && (
 <OwnerOnly>
 <button
 onClick={() => onOpenTruthBook()}
 className="mt-5 text-[12px] text-[#3B82F6] hover:underline whitespace-nowrap"
 title="Open this member's full Record — every verified quote and tracked commitment on a shared timeline (operator-only"
 >
 Open the Record ↗
 </button>
 </OwnerOnly>
 )}
 </section>
 </div>
 );
}

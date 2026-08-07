/**
 * Unified quotes utility — Quotes Unification Refactor (2026-05-26).
 * See 01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md for the architecture.
 *
 * Frontend interface to the new `/api/quotes/meeting/:meetingId` endpoint,
 * which returns either:
 * - Rows from the unified `quotes` table (source = "quotes_table"), or
 * - Parsed legacy council_quotes JSON projected into the same shape
 * (source = "council_quotes_legacy") for meetings that haven't been
 * re-extracted under the unified prompt yet.
 *
 * Both sources produce the same `UnifiedQuote` shape so BroadcastPage /
 * Cast page can render uniformly. Chunk 9 removes the legacy fallback.
 */

export interface UnifiedQuoteWordTiming {
 word: string;
 start_ms: number;
 end_ms: number;
}

export type SpeakerClass = "council_member" | "staff" | "external";
export type VerifiedStatus = "pending" | "verified" | "disputed" | "rejected";

export interface UnifiedQuote {
 /** Real numeric id from the quotes table, OR a synthetic "legacy-<meeting>-<idx>"
 * string when sourced from the legacy council_quotes JSON blob. */
 id: number | string;
 meeting_id: number;
 /** Resolved member_id when speaker_class='council_member' AND the name
 * matched a canonical roster entry. Null for staff/external speakers
 * or when the council-member name didn't resolve (migration leftover). */
 member_id: number | null;
 speaker_name: string;
 speaker_role: string | null;
 speaker_class: SpeakerClass;
 quote_text: string;
 /** Pre--V3-correction form of quote_text, if a correction was
 * applied. Null otherwise. */
 quote_text_original: string | null;
 /** Parsed JSON array from topic_tags column. Empty array if no tags. */
 topic_tags: string[];
 minutes_page_ref: string | null;
 context: string | null;
 is_broadcast_hero: 0 | 1;
 /** Derived from word_timings[0].start_ms when alignment ran; otherwise
 * may carry NotebookLM's approximate value or null. */
 video_timestamp_seconds: number | null;
 word_timings: UnifiedQuoteWordTiming[] | null;
 verified_status: VerifiedStatus;
 verified_by: string | null;
 verified_at: string | null;
 gemini_correction_notes: unknown | null;
 proof_clip_url: string | null;
 proof_clip_sha256: string | null;
 content_hash: string | null;
 extracted_at: string | null;
 updated_at: string | null;
}

export interface UnifiedQuotesResponse {
 success: boolean;
 source: "quotes_table" | "council_quotes_legacy" | "empty";
 quotes: UnifiedQuote[];
 count: number;
 error?: string;
}

/**
 * Fetch broadcast-hero quotes for a meeting. Defaults to hero-only;
 * pass includeAll=true for the full set (used by operator surfaces).
 *
 * Returns a uniform UnifiedQuotesResponse regardless of whether the
 * backend served from the canonical table or the legacy fallback.
 */
export async function fetchUnifiedQuotes(
 meetingId: number,
 options: { includeAll?: boolean; signal?: AbortSignal } = {},
): Promise<UnifiedQuotesResponse> {
 const params = options.includeAll ? "?include_all=true" : "";
 const response = await fetch(`/api/quotes/meeting/${meetingId}${params}`, {
 signal: options.signal,
 });
 if (!response.ok) {
 return {
 success: false,
 source: "empty",
 quotes: [],
 count: 0,
 error: `HTTP ${response.status}`,
 };
 }
 const data = (await response.json()) as UnifiedQuotesResponse;
 return data;
}

/**
 * Lightweight predicate for "this quote can be played inline as karaoke."
 * Requires both word_timings AND a meeting video URL (caller passes the
 * latter since it lives at the meeting level, not the quote level).
 */
export function canKaraokePlay(
 quote: UnifiedQuote,
 meetingVideoUrl: string | null | undefined,
): boolean {
 return (
 Array.isArray(quote.word_timings) &&
 quote.word_timings.length > 0 &&
 !!meetingVideoUrl
 );
}

/**
 * Map a verified_status enum value to a small UI descriptor — color +
 * label suitable for inline rendering next to the quote attribution line.
 * Returns null for `pending` (no visible badge needed; pending is the
 * default and shouldn't visually call attention).
 */
export interface VerificationBadge {
 label: string;
 color: string;
 bgColor: string;
 tooltip: string;
}

export function verificationBadgeFor(
 status: VerifiedStatus,
): VerificationBadge | null {
 switch (status) {
 case "verified":
 return {
 label: "Verified",
 color: "#22C55E",
 bgColor: "rgba(34, 197, 94, 0.12)",
 tooltip:
 "Triple-source verified via : Whisper transcript alignment + Gemini Pro review + human attestation",
 };
 case "disputed":
 return {
 label: "Disputed",
 color: "#F5A524",
 bgColor: "rgba(245, 165, 36, 0.12)",
 tooltip:
 "Flagged at review as having attribution/accuracy concerns; awaiting operator resolution",
 };
 case "rejected":
 return {
 label: "Rejected",
 color: "#EF4444",
 bgColor: "rgba(239, 68, 68, 0.12)",
 tooltip:
 "Marked as inaccurate or misattributed at review; not surfaced on public surfaces",
 };
 case "pending":
 default:
 return null;
 }
}

/**
 * Render-ready key for React lists. Use this rather than the raw `id`
 * since legacy-sourced quotes have synthetic string IDs.
 */
export function quoteKey(quote: UnifiedQuote): string {
 return String(quote.id);
}

/**
 * Truth Book Lite (D-059 Layer 1) — frontend types + fetch helper for the
 * per-person research surface. Mirrors the GET /api/truth-book/:city/:seat
 * response assembled in parsers/database.py § get_truth_book_for_member.
 * See 01_Project_Overview/TRUTH_BOOK_LITE_SPEC.md.
 */
import type { VerifiedStatus } from "./unifiedQuotes";

export interface TruthBookWordTiming {
  word: string;
  start_ms: number;
  end_ms: number;
}

export interface TruthBookQuoteEntry {
  type: "quote";
  quote_id: number;
  meeting_id: number;
  meeting_date: string | null;
  meeting_title: string | null;
  meeting_video_url: string | null;
  text: string | null;
  context: string | null;
  topic_tags: string[];
  word_timings: TruthBookWordTiming[] | null;
  video_timestamp_seconds: number | null;
  verified_status: VerifiedStatus;
  verified_by: string | null;
  verified_at: string | null;
  proof_clip_sha256: string | null;
  is_broadcast_hero: 0 | 1 | null;
  speaker_role: string | null;
}

export interface TruthBookClaimEntry {
  type: "claim";
  claim_id: number;
  claim_type: string | null;
  status: string | null;
  topic_tags: string[];
  meeting_id: number;
  meeting_date: string | null;
  meeting_title: string | null;
  meeting_video_url: string | null;
  claim_text: string | null;
  expected_outcome: string | null;
  time_horizon_months: number | null;
  confidence: string | null;
  context: string | null;
  word_timings: TruthBookWordTiming[] | null;
  video_timestamp_seconds: number | null;
  status_updated_at: string | null;
  extracted_at: string | null;
  /** Forward-looking: the meeting where this claim resolved. The backend
   * emits null until the tracked_claims resolution-link column lands
   * (TRUTH_BOOK_LITE_SPEC chunk 5 draws the claim→resolution connector). */
  resolved_meeting_id: number | null;
}

export interface TruthBookLane {
  topic: string;
  label: string;
  entries: TruthBookQuoteEntry[];
}

export interface TruthBookMember {
  id: number;
  name: string;
  role: string | null;
  seat_id: string;
  term_started: string | null;
  term_ends: string | null;
  source_url: string | null;
}

export interface TruthBookResponse {
  city: string;
  county: string | null;
  state: string | null;
  member: TruthBookMember;
  time_range: { earliest: string | null; latest: string | null };
  lanes: TruthBookLane[];
  claims: TruthBookClaimEntry[];
}

export async function fetchTruthBook(
  cityName: string,
  seatId: string,
  options: { signal?: AbortSignal } = {},
): Promise<TruthBookResponse> {
  const res = await fetch(
    `/api/truth-book/${encodeURIComponent(cityName)}/${encodeURIComponent(seatId)}`,
    { signal: options.signal },
  );
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && typeof body.error === "string") detail = body.error;
    } catch {
      /* error body wasn't JSON (e.g. an upstream HTML 404 page) */
    }
    throw new Error(detail);
  }
  return (await res.json()) as TruthBookResponse;
}

/**
 * Total quote entries across all lanes. A quote tagged with multiple
 * featured topics is counted once per lane it appears in (matching the
 * swimlane render), so this is a lane-occupancy total, not a distinct-quote
 * count.
 */
export function totalLaneEntries(lanes: TruthBookLane[]): number {
  return lanes.reduce((sum, lane) => sum + lane.entries.length, 0);
}

// ── V1-RAG-3 per-member retrieval (the V3 preview surface, S-071) ──────
//
// Mirrors POST /api/member-rag/<city>/<seat> body {topic, top_k?}.
// Pure retrieval, no synthesis — each result is one Qdrant chunk plus its
// karaoke timecode and the member aliases that surfaced it. UI renders the
// chunks alongside the existing extracted-quote lanes so the operator can
// compare extracted-data vs RAG-retrieved-data side by side.

export interface MemberRagChunk {
  meeting_id: number;
  meeting_title: string | null;
  meeting_date: string | null;
  meeting_video_url: string | null;
  chunk_index: number;
  start_seconds: number;
  end_seconds: number;
  score: number;
  body: string;
  matched_aliases: string[];
}

export interface MemberRagMember {
  name: string;
  role: string | null;
  seat_id: string;
  term_started: string | null;
  term_ends: string | null;
}

export interface MemberRagResponse {
  success: true;
  city: string;
  seat_id: string;
  member: MemberRagMember;
  aliases: string[];
  topic: { id: string; label: string; hint: string };
  results: MemberRagChunk[];
  meetings_queried: number;
  chunks_retrieved: number;
  chunks_matched: number;
}

export async function fetchMemberRag(
  cityName: string,
  seatId: string,
  topic: string,
  options: { signal?: AbortSignal; topK?: number } = {},
): Promise<MemberRagResponse> {
  const res = await fetch(
    `/api/member-rag/${encodeURIComponent(cityName)}/${encodeURIComponent(seatId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, top_k: options.topK ?? 12 }),
      signal: options.signal,
    },
  );
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && typeof body.error === "string") detail = body.error;
    } catch {
      /* error body wasn't JSON */
    }
    throw new Error(detail);
  }
  return (await res.json()) as MemberRagResponse;
}

/** Format a seconds offset as `M:SS` for the chunk timecode display. */
export function formatChunkTimecode(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

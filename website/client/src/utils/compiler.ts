/**
 * Conversational Compiler V0Track A) — frontend types + fetch helper
 * for the Hex-Rays UX consumer. Mirrors the GET /api/compiler/:meetingId
 * response assembled in parsers/api_server.py § get_compiler_view.
 *
 * V0 renders existing tracked_claims as Commit_P nodes in the IR pseudo-code.
 * Track B (the actual parser pipeline) is a separate workstream; when it
 * lands, the same endpoint shape will carry parser-generated claims with
 * full Commit_P fields. The frontend doesn't need to care which produced
 * each claim — the typed-IR rendering is the same either way.
 *
 * See FUTURE_THOUGHTSfor the architectural vision.
 */

export interface CompilerWordTiming {
 word: string;
 start_ms: number;
 end_ms: number;
}

export interface CompilerMeeting {
 id: number;
 city_name: string;
 meeting_title: string | null;
 meeting_date: string | null;
}

/** A tracked_claims row enriched with the joined council_members fields.
 * In V0 these are the existing tracked_claims rows; in production these
 * are populated by NotebookLM via prompts/tracked_claims.md (sidecar-
 * persisted by notebooklm_bridge/fetcher.py), with the 3 m101091 sandbox
 * rows hand-seeded via notebooklm_bridge/scripts/seed_tracked_claims_m101091.py.
 * The frontend renders them as Commit_P nodes in the typed-IR pseudo-code. */
export interface CompilerClaim {
 id: number;
 member_id: number;
 claim_type: string | null;
 claim_text: string;
 expected_outcome: string | null;
 time_horizon_months: number | null;
 topic_tags: string[];
 confidence: string | null;
 context: string | null;
 word_timings: CompilerWordTiming[] | null;
 status: string;
 status_updated_at: string | null;
 extracted_at: string | null;
 /** From council_members JOIN; null when the claim's member_id no longer
 * resolves (deleted member, schema drift). */
 speaker_name: string | null;
 speaker_title: string | null;
}

/** A row from `transcript_nodes` — the parser-pipeline output that
 * extends Track A's V0 hand-seeded `tracked_claims` (Commit_P only).
 * Each node carries a `node_type` discriminator and a `typed_fields`
 * payload shaped per the SPEC § Node types table. Surface 2026-06-05
 * via Track B's NotebookLM prompts (motions.md, votes.md, future
 * sibling prompts) — see CONVERSATIONAL_COMPILER_SPEC.md § Decision
 * #8a + § IR schema V0. */
export type CompilerNodeType =
 | "Motion"
 | "Vote"
 | "Utterance"
 | "Second"
 | "Commit_P"
 | "AgendaTransition"
 | "Contradiction";

export interface CompilerNode {
 id: number;
 ordinal: number;
 node_type: CompilerNodeType;
 speaker_id: number | null;
 /** Canonical-roster-joined name when speaker_id resolves; otherwise the
 * denormalized speaker_name from the row (e.g., for body actions or
 * non-council speakers without a roster match). */
 speaker_name: string | null;
 speaker_title: string | null;
 transcript_span_text: string;
 /** Node-type-specific JSON payload. Per SPEC § Node types:
 * Motion: {summary_sentence, motion_text, motion_type,
 * agenda_item, context}
 * Vote: {summary_sentence, motion_reference, vote_result,
 * vote_method, per_member_votes, tally,
 * agenda_item, context}
 * Commit_P (TBD): matches CompilerClaim's projected field set
 * AgendaTransition: {summary_sentence, agenda_item_number,
 * agenda_item_title}
 * etc. */
 typed_fields: Record<string, unknown>;
 parser_model: string;
 parser_confidence: number | null;
 parser_ran_at: string | null;
 /** Audio range in SECONDS — backfilled by parsers/node_timing.py
 * (the post-extraction timing pass). Null when the alignment
 * couldn't find a confident position (typically because the node's
 * text is too short to anchor — e.g., a Vote whose responds_to
 * Motion wasn't itself timed). Used by TranscriptPane for token
 * coloring (SPEC build seq item 6) and by CompilerPage's transcript-
 * scroll → IR-node mapping. */
 audio_offset_seconds: number | null;
 audio_duration_seconds: number | null;
 /** Self-FK for SPEC Decision #2 layered abstraction — assigned by
 * parsers/edge_inference.py::backfill_parent_node_ids based on the
 * agenda-item key match against AgendaTransition nodes. */
 parent_node_id: number | null;
}

/** Edge type vocabulary per CONVERSATIONAL_COMPILER_SPEC § Edge types.
 * V0 fires `responds_to` (Vote → Motion procedural response) and
 * `satisfies` (Vote-passed → Commit_P operationalized commitment); the
 * other three (`references`, `entails`, `contradicts`) are reserved for
 * later inference passes. */
export type CompilerEdgeType =
 | "responds_to"
 | "satisfies"
 | "references"
 | "entails"
 | "contradicts";

/** A transcript_edges row with both endpoints already resolved to
 * frontend focus keys by the API. `source_focus_key` and
 * `target_focus_key` are the same string vocabulary CompilerPage uses
 * for the focus state — `"claim:<id>"` when the endpoint is a Commit_P
 * with a tracked_claims projection (the canonical surface for Commit_P
 * per SPEC § Relationship to existing tracked_claims), `"node:<id>"`
 * for every other transcript_nodes endpoint (Motion / Vote / etc.).
 * The frontend doesn't need to bridge the two id spaces — it just
 * addresses both endpoints by focus key. */
export interface CompilerEdge {
 id: number;
 source_node_id: number;
 target_node_id: number;
 source_focus_key: string;
 target_focus_key: string;
 edge_type: CompilerEdgeType;
 parser_confidence: number | null;
 parser_ran_at: string | null;
}

export interface CompilerResponse {
 meeting: CompilerMeeting;
 claims: CompilerClaim[];
 /** Track B parser-pipeline nodes. May be empty if Track B hasn't
 * run for the meeting yet. EXCLUDES Commit_P transcript_nodes when
 * those have a tracked_claims projection — the canonical surface for
 * Commit_P stays the claims array. */
 nodes: CompilerNode[];
 /** Edges inferred by the constraint-checker pass (V0:
 * `parsers/edge_inference.py`). May be empty when no Vote→Motion
 * matching has run for the meeting. */
 edges: CompilerEdge[];
}

export async function fetchCompilerView(
 meetingId: number,
 options: { signal?: AbortSignal } = {},
): Promise<CompilerResponse> {
 const res = await fetch(
 `/api/compiler/${encodeURIComponent(meetingId)}`,
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
 return (await res.json()) as CompilerResponse;
}

/** A Whisper word-level transcript row — produced by OpenAI Whisper,
 * persisted by _fetch_transcript_words in notebooklm_bridge/fetcher.py
 * into notebook_outputs.transcript_words, exposed via the
 * /api/compiler/<id>/transcript endpoint (Decision #7a). */
export interface CompilerTranscriptWord {
 word: string;
 start: number;
 end: number;
}

export interface CompilerTranscriptResponse {
 meeting_id: number;
 words: CompilerTranscriptWord[];
 duration_seconds: number | null;
 language: string | null;
}

/** Fetch the meeting's persisted Whisper word-level transcript for
 * Surface A's left full-transcript pane (SPEC build seq item 4).
 *
 * Throws on 404 (no transcript_words row exists yet for this meeting)
 * with `error.message === "no transcript available for this meeting"`
 * — caller decides whether to render an empty state or a "transcription
 * pending" hint based on the meeting's pipeline state. */
export async function fetchCompilerTranscript(
 meetingId: number,
 options: { signal?: AbortSignal } = {},
): Promise<CompilerTranscriptResponse> {
 const res = await fetch(
 `/api/compiler/${encodeURIComponent(meetingId)}/transcript`,
 { signal: options.signal },
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
 return (await res.json()) as CompilerTranscriptResponse;
}

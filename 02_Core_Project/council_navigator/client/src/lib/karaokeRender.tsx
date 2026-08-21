/**
 * Shared karaoke-citation render primitives for any surface that displays
 * RAG-generated answers with timestamp citation tokens.
 *
 * The existing chat-passthrough surface in BroadcastPage.tsx pioneered the
 * green-pill chip + animated "..." dots pattern. V1.5-Query-1 originally
 * rolled its own render, which produced a visually weaker surface (plain
 * green text vs the proper chip, Loader2 spinner vs the dots, literal
 * "[at " brackets in the rendered output). This module extracts the
 * canonical patterns so every surface that renders cited RAG output stays
 * visually consistent.
 *
 * Citation seek: the seek behavior is owned by BroadcastPage's seekVideoTo()
 * which already handles YouTube iframe postMessage / direct-MP4
 * <video>.currentTime / Granicus-iframe via DOM queries. Callers pass that
 * function as the `onSeek` prop on KaraokeText; this module stays
 * source-agnostic.
 */
import React from "react";

export type KaraokeSegment =
  | { kind: "text"; value: string }
  | { kind: "cite"; mm: number; ss: number; raw: string };

/**
 * Split canonical `[at H:MM:SS]` tokens and legacy flat-minute
 * `[at MM:SS]` tokens out of a text body, returning interleaved text +
 * citation segments in order. Canonical citations avoid ambiguous long-
 * meeting locators; legacy support keeps older generated content clickable.
 */
export function parseKaraokeSegments(text: string): KaraokeSegment[] {
  const re = /\[at (?:(0|[1-9]\d*):([0-5]\d):([0-5]\d)|(\d{1,3}):([0-5]\d))\]/g;
  const segs: KaraokeSegment[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) segs.push({ kind: "text", value: text.slice(last, m.index) });
    const mm = m[1] !== undefined
      ? parseInt(m[1], 10) * 60 + parseInt(m[2], 10)
      : parseInt(m[4], 10);
    const ss = m[1] !== undefined ? parseInt(m[3], 10) : parseInt(m[5], 10);
    segs.push({
      kind: "cite",
      mm,
      ss,
      raw: m[0],
    });
    last = m.index + m[0].length;
  }
  if (last < text.length) segs.push({ kind: "text", value: text.slice(last) });
  return segs;
}

/**
 * V1.5-BYOK-Verbatim-1 (2026-07-04) — verbatim-highlighter chunk shape.
 *
 * When ByokQueryPanel finalizes a turn (stream complete + metadata
 * landed), it passes the retrieved chunks from that turn's rag-search
 * response so KaraokeText can wrap verbatim substrings of the LLM
 * answer in a click-to-hear highlighter mark. Each match seeks the
 * video to the chunk's start_seconds via onVerbatimClick.
 *
 * The chunks are only needed for verbatim detection; if omitted, the
 * component renders identically to its pre-verbatim shape.
 */
export interface VerbatimChunk {
  chunk_index: number;
  body: string;
  start_seconds: number;
}

interface KaraokeTextProps {
  text: string;
  onSeek?: (seconds: number) => void;
  /** Retrieved chunks whose bodies to scan for verbatim substrings.
   *  When provided AND non-empty, text segments (between `[at MM:SS]`
   *  chips) get highlighter marks wrapped around 8+ consecutive-token
   *  matches to any chunk body. */
  chunks?: VerbatimChunk[];
  /** Fires when the citizen clicks a verbatim-highlighted phrase.
   *  Receives the chunk's start_seconds so the caller can seek the
   *  video. In practice ByokQueryPanel wires this to the same
   *  seekVideoTo() handler that owns the `[at MM:SS]` chip clicks. */
  onVerbatimClick?: (seconds: number) => void;
  /** Minimum consecutive-token match length to qualify as verbatim.
   *  Default 8 — long enough to filter out common phrases but short
   *  enough to catch typical council-record fragments (resolution
   *  numbers + dollar amounts + specific project names). */
  verbatimMinTokens?: number;
}

// ─────────────────────────────────────────────────────────────────
// Verbatim-substring detection (V1.5-BYOK-Verbatim-1)
// ─────────────────────────────────────────────────────────────────

interface TokenPos {
  /** Normalized form used for matching — lowercased, non-word chars stripped. */
  norm: string;
  /** Character start offset in the original text (before normalization). */
  charStart: number;
  /** Character end offset (exclusive). */
  charEnd: number;
}

/**
 * Split text into tokens carrying their original character positions.
 * Normalization drops case + trailing punctuation so "6-1" matches "6-1,"
 * and "council." matches "council" — the citizen-visible answer often
 * cleans punctuation vs. verbatim transcript, but semantically identical.
 *
 * Tokens with an empty normalized form (pure-punctuation runs) are
 * skipped; they'd match anything and pollute the ngram index.
 */
function tokenizeWithPositions(text: string): TokenPos[] {
  const tokens: TokenPos[] = [];
  const re = /\S+/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const raw = m[0];
    const norm = raw.toLowerCase().replace(/[^a-z0-9$]/g, "");
    if (norm.length === 0) continue;
    tokens.push({
      norm,
      charStart: m.index,
      charEnd: m.index + raw.length,
    });
  }
  return tokens;
}

interface VerbatimRange {
  /** Character index in the source text where the match starts. */
  charStart: number;
  /** Character index (exclusive) where the match ends. */
  charEnd: number;
  /** Seconds to seek to in the video (the source chunk's start). */
  startSeconds: number;
  /** Which chunk provided the match — useful for debugging + attribution. */
  chunkIndex: number;
}

/**
 * Find all verbatim substrings of `text` that appear in any of `chunks`,
 * of at least `minTokens` consecutive tokens. Returns non-overlapping
 * ranges sorted by charStart.
 *
 * Algorithm: for each chunk, build a Map<ngram, chunkPos> keyed on
 * minTokens-length n-grams. Slide over the text's tokens; on an n-gram
 * hit, greedily extend the match forward until tokens diverge. This is
 * O(m + n) per chunk pair, so ~5-10K ops total for typical answer/chunk
 * sizes.
 *
 * When multiple chunks contribute overlapping matches at the same
 * position, the FIRST-seen match wins (chunks iterated in retrieval
 * order = highest cosine-score first). Later matches that overlap are
 * dropped.
 *
 * Chip-token pollution edge case (session-32 park): the scan operates
 * over the full answer text INCLUDING `[at MM:SS]` chip characters, so a
 * verbatim run could in theory span a chip boundary if the chunk body
 * happens to contain tokens aligning with the chip's normalized form
 * ("at" + digits). In practice the minTokens=8 floor + the chip's
 * distinctive tokenization make this essentially impossible — no real
 * transcript chunk contains eight consecutive tokens where the middle
 * of the run is "at 12 34" or similar. Unverified in production data;
 * if it surfaces, the fix is: split `text` into non-chip segments before
 * scanning (parseKaraokeSegments already produces that split), and run
 * findVerbatimRanges on each text segment independently. Char positions
 * would then be segment-local + need re-baselining before rendering.
 * NOT landing preemptively because the current per-full-text scan is
 * simpler and the edge case is theoretical.
 */
export function findVerbatimRanges(
  text: string,
  chunks: VerbatimChunk[],
  minTokens: number = 8,
): VerbatimRange[] {
  if (!text || chunks.length === 0 || minTokens < 2) return [];
  const textTokens = tokenizeWithPositions(text);
  if (textTokens.length < minTokens) return [];

  const candidates: VerbatimRange[] = [];
  for (const chunk of chunks) {
    if (!chunk.body) continue;
    const chunkTokens = tokenizeWithPositions(chunk.body);
    if (chunkTokens.length < minTokens) continue;

    // Build the n-gram → first-position map for this chunk.
    const ngramMap = new Map<string, number>();
    for (let j = 0; j + minTokens <= chunkTokens.length; j++) {
      const ngram = chunkTokens.slice(j, j + minTokens).map((t) => t.norm).join(" ");
      if (!ngramMap.has(ngram)) ngramMap.set(ngram, j);
    }

    let i = 0;
    while (i + minTokens <= textTokens.length) {
      const ngram = textTokens.slice(i, i + minTokens).map((t) => t.norm).join(" ");
      const chunkPos = ngramMap.get(ngram);
      if (chunkPos === undefined) {
        i++;
        continue;
      }
      // Extend greedily forward from the n-gram tail.
      let extend = minTokens;
      while (
        i + extend < textTokens.length &&
        chunkPos + extend < chunkTokens.length &&
        textTokens[i + extend].norm === chunkTokens[chunkPos + extend].norm
      ) {
        extend++;
      }
      candidates.push({
        charStart: textTokens[i].charStart,
        charEnd: textTokens[i + extend - 1].charEnd,
        startSeconds: chunk.start_seconds,
        chunkIndex: chunk.chunk_index,
      });
      i += extend; // skip past the matched run before checking again
    }
  }

  if (candidates.length === 0) return [];
  candidates.sort((a, b) => a.charStart - b.charStart);
  // Drop overlaps: keep the first, discard any subsequent that starts
  // before the previous ends. First-seen = highest-cosine-chunk seen
  // first (retrieval order preserved by caller).
  const result: VerbatimRange[] = [];
  let lastEnd = -1;
  for (const cand of candidates) {
    if (cand.charStart < lastEnd) continue;
    result.push(cand);
    lastEnd = cand.charEnd;
  }
  return result;
}

/**
 * Render a single text segment with verbatim ranges highlighted. Each
 * range becomes a <mark className="kd-highlight-nuance"> button that
 * fires onSeek(startSeconds) on click. Text outside ranges renders
 * plain.
 */
function renderTextSegmentWithHighlights(
  key: number,
  text: string,
  ranges: VerbatimRange[],
  onSeek?: (seconds: number) => void,
): React.ReactElement {
  if (ranges.length === 0) return <span key={key}>{text}</span>;
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  ranges.forEach((r, i) => {
    if (r.charStart > cursor) {
      parts.push(<span key={`t-${i}`}>{text.slice(cursor, r.charStart)}</span>);
    }
    const inner = text.slice(r.charStart, r.charEnd);
    parts.push(
      <mark
        key={`v-${i}`}
        className="kd-highlight-nuance cursor-pointer hover:brightness-125 transition-[filter]"
        role="button"
        tabIndex={0}
        onClick={() => onSeek?.(r.startSeconds)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onSeek?.(r.startSeconds);
          }
        }}
        title={`Play from the source moment · chunk ${r.chunkIndex}`}
      >
        {inner}
      </mark>,
    );
    cursor = r.charEnd;
  });
  if (cursor < text.length) {
    parts.push(<span key="t-tail">{text.slice(cursor)}</span>);
  }
  return <span key={key}>{parts}</span>;
}

/**
 * Render the chip's MM:SS / H:MM:SS label from the parsed mm + ss
 * segment. The parser allows mm up to 999 because the LLM is instructed
 * to emit flat minutes past 60 (e.g. `[at 129:27]` for 2h 9m 27s), but
 * rendering that as "129:27" in a chip is unreadable — collapse to
 * `H:MM:SS` whenever total time crosses one hour so the chip matches the
 * shape SyncedQuote / MeetingExcerptPlayer already use across the rest
 * of the surface. Sub-hour chips stay as `M:SS` since that's the natural
 * shape for shorter meetings.
 */
export function formatChipLabel(mm: number, ss: number): string {
  const totalSeconds = mm * 60 + ss;
  if (totalSeconds < 3600) {
    return `${mm}:${ss.toString().padStart(2, "0")}`;
  }
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
}

/**
 * Render text containing `[at MM:SS]` citation tokens, replacing each
 * citation with a clickable green pill chip that fires onSeek(seconds)
 * when clicked. Visual treatment matches BroadcastPage's existing
 * chat-passthrough surface.
 *
 * V1.5-BYOK-Verbatim-1 (2026-07-04) — when `chunks` is provided,
 * additionally scans text segments (between chips) for 8+ consecutive-
 * token verbatim substrings against the chunk bodies and wraps them
 * in a click-to-hear highlighter <mark>. Both layers coexist: the
 * green pill jumps to the LLM-emitted timecode; the orange highlight
 * jumps to the chunk's start_seconds. Verbatim highlighting is memoized
 * on (text, chunks, minTokens) so it doesn't recompute per keystroke
 * when the caller re-renders during a streaming response.
 */
export function KaraokeText({
  text,
  onSeek,
  chunks,
  onVerbatimClick,
  verbatimMinTokens,
}: KaraokeTextProps): React.ReactElement {
  const segs = parseKaraokeSegments(text);
  const minTokens = verbatimMinTokens ?? 8;
  const verbatimRanges = React.useMemo(() => {
    if (!chunks || chunks.length === 0) return [];
    return findVerbatimRanges(text, chunks, minTokens);
  }, [text, chunks, minTokens]);

  // Verbatim ranges are computed against the full answer text; each
  // text segment (between chips) needs the subset of ranges that fall
  // inside its own character window, rebased to segment-local offsets.
  let segTextCursor = 0;

  return (
    <>
      {segs.map((seg, i) => {
        if (seg.kind === "cite") {
          // Chip characters don't advance the verbatim cursor (matches
          // are computed against the full text including chips, so the
          // char positions already reflect their presence).
          segTextCursor += seg.raw.length;
          return (
            <button
              key={i}
              type="button"
              // The seek math (mm*60+ss) correctly handles past-hour mm
              // values per the parser's 3-digit allowance — only the label
              // shape needs the hour collapse.
              onClick={() => onSeek?.(seg.mm * 60 + seg.ss)}
              className="inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded-md bg-[#22C55E]/15 hover:bg-[#22C55E]/30 text-[#22C55E] text-[11px] font-mono font-bold transition-colors cursor-pointer"
              title={`Seek video to ${formatChipLabel(seg.mm, seg.ss)}`}
            >
              {formatChipLabel(seg.mm, seg.ss)}
            </button>
          );
        }
        // seg.kind === "text"
        const segStart = segTextCursor;
        const segEnd = segStart + seg.value.length;
        segTextCursor = segEnd;
        if (verbatimRanges.length === 0) {
          return <span key={i}>{seg.value}</span>;
        }
        // Rebase overlapping ranges to segment-local offsets, clipping
        // any range that crosses the segment boundary (shouldn't happen
        // — findVerbatimRanges doesn't cross chip characters because
        // the chip text isn't part of any chunk body — but the clip is
        // cheap insurance).
        const localRanges: VerbatimRange[] = [];
        for (const r of verbatimRanges) {
          if (r.charEnd <= segStart || r.charStart >= segEnd) continue;
          localRanges.push({
            charStart: Math.max(0, r.charStart - segStart),
            charEnd: Math.min(seg.value.length, r.charEnd - segStart),
            startSeconds: r.startSeconds,
            chunkIndex: r.chunkIndex,
          });
        }
        return renderTextSegmentWithHighlights(i, seg.value, localRanges, onVerbatimClick);
      })}
    </>
  );
}

/**
 * The "..." pending-response indicator — three bouncing dots, staggered
 * by 150ms, that any surface waiting on a RAG round-trip can drop in.
 */
export function KaraokeLoadingDots(): React.ReactElement {
  return (
    <div className="flex gap-2 items-center h-6">
      <span
        className="w-2 h-2 rounded-full bg-gray-500 animate-bounce"
        style={{ animationDelay: "0ms" }}
      />
      <span
        className="w-2 h-2 rounded-full bg-gray-500 animate-bounce"
        style={{ animationDelay: "150ms" }}
      />
      <span
        className="w-2 h-2 rounded-full bg-gray-500 animate-bounce"
        style={{ animationDelay: "300ms" }}
      />
    </div>
  );
}

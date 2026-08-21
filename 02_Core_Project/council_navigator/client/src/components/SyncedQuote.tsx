/**
 * SyncedQuote — the karaoke synced-transcript player for Cast page quotes.
 *
 * The V2 headline feature per DECISIONS.md § D-040 / D-041: each featured
 * quote renders with a speaker icon; click expands an inline player
 * below the quote that auto-seeks to the quote's start, plays through
 * to its end, and highlights one word at a time in sync with the
 * audio. The operator's 2026-05-16 framing: captions in the
 * accessibility sense, applied to civic accountability.
 *
 * Per D-041, this UI ALSO replaces the originally-planned Gemini
 * multimodal verification pass — the human reviewer watches the
 * karaoke (eyes confirm speaker, ears confirm words, highlight cursor
 * confirms timing) and approves with one click.
 *
 * PLAYER-1 C2 (2026-07-07): internals ported onto the unified player
 * layer — the private YT-IFrame-API loader, player typings, and RAF
 * word-highlight loop moved to player/youtubeApi.ts, player/adapters.ts,
 * and player/KaraokeStrip.tsx. External props and UX are unchanged for
 * the four consumers (BroadcastPage, TruthBookPage, DisputedQuotesPage,
 * CastMemberPanel). One capability upgrade rides the port: quotes on
 * direct-MP4 meetings (Granicus direct archives) now karaoke-play too —
 * the strip needs only a readable clock, and the html5 adapter has one.
 * Word-level highlight modes (flat + marker) live in KaraokeStrip.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Play, X } from "lucide-react";
import { getVideoSource } from "../lib/videoSource";
import { createAdapter, type PlayerAdapter } from "../player/adapters";
import KaraokeStrip, { type QuoteWordTiming } from "../player/KaraokeStrip";

export type { QuoteWordTiming };

interface SyncedQuoteProps {
  /** Per-word timings from `member_quotes.word_timings`. Must be non-empty. */
  wordTimings: QuoteWordTiming[];
  /** Source video URL for the meeting (YouTube in any standard form, or
   *  a direct .mp4 — anything with a readable playback clock plays). */
  videoUrl: string;
  /** True when this is the one quote with an active player on the page.
   *  Parent state ensures only one is active at a time. */
  isActive: boolean;
  /** Called when the user clicks the play button. Parent should set the
   *  active-quote state to this row's id. */
  onActivate: () => void;
  /** Called when playback ends, errors, or the user clicks the close
   *  button. Parent should clear the active-quote state. */
  onDeactivate: () => void;
  /** Tailwind class applied to the active word's background when running
   *  in "flat" highlight mode. Default is the Cast page's amber
   *  (`bg-[#F5A524]/35`). Ignored when `markerColor` is set. */
  activeWordClassName?: string;
  /** When set, switches to "highlighter marker" mode — see
   *  player/KaraokeStrip.tsx. Pass a 6-char hex like `"#F2A91C"`
   *  (legalese orange). Used by the BroadcastPage Council Quotes section. */
  markerColor?: string;
}

function formatTimestamp(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export default function SyncedQuote({
  wordTimings,
  videoUrl,
  isActive,
  onActivate,
  onDeactivate,
  activeWordClassName = "bg-[#F5A524]/35",
  markerColor,
}: SyncedQuoteProps) {
  const playerHostRef = useRef<HTMLDivElement | null>(null);
  const adapterRef = useRef<PlayerAdapter | null>(null);

  const source = useMemo(() => getVideoSource(videoUrl), [videoUrl]);
  // Karaoke needs a readable clock — YouTube and direct MP4 qualify;
  // the Granicus cross-origin iframe and external links don't.
  const canPlay =
    !!source && (source.kind === "youtube" || source.kind === "mp4");

  // First/last word boundaries — the played span.
  const startSeconds = (wordTimings[0]?.start_ms ?? 0) / 1000;
  const endSeconds =
    (wordTimings[wordTimings.length - 1]?.end_ms ?? wordTimings[0]?.start_ms ?? 0) /
    1000;

  // ── Mount adapter when activated ───────────────────────────────
  useEffect(() => {
    if (!isActive || !canPlay || !source) return;
    const host = playerHostRef.current;
    if (!host) return;

    const adapter = createAdapter(source, { title: "Synced quote player" });
    adapterRef.current = adapter;
    adapter.onEnded(() => onDeactivate());
    adapter.onError(() => {
      // Failed videos shouldn't trap the operator. Bail out.
      onDeactivate();
    });
    adapter.mount(host, { startSeconds, endSeconds, autoplay: true });

    return () => {
      adapterRef.current = null;
      adapter.destroy();
      host.innerHTML = "";
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActive, source?.raw, startSeconds, endSeconds]);

  const getCurrentTimeMs = useCallback(() => {
    const t = adapterRef.current?.getCurrentTime();
    return typeof t === "number" ? t * 1000 : null;
  }, []);

  const handlePassedEnd = useCallback(() => {
    adapterRef.current?.pause();
    onDeactivate();
  }, [onDeactivate]);

  // ── Render ─────────────────────────────────────────────────────

  if (wordTimings.length === 0) {
    return null;
  }

  return (
    <div className="flex flex-col gap-2">
      <KaraokeStrip
        wordTimings={wordTimings}
        getCurrentTimeMs={getCurrentTimeMs}
        running={isActive}
        onPassedEnd={handlePassedEnd}
        markerColor={markerColor}
        activeWordClassName={activeWordClassName}
      />

      {/* Player + controls — only mounted when active */}
      {isActive ? (
        <div className="flex flex-col gap-2">
          <div
            ref={playerHostRef}
            className="aspect-video w-full max-w-md rounded-sm overflow-hidden bg-black border border-white/10"
          />
          <button
            onClick={onDeactivate}
            className="self-start inline-flex items-center gap-1.5 text-[10px] uppercase tracking-[0.18em] text-foreground/55 hover:text-white transition-colors border border-white/10 hover:border-white/30 px-2 py-1"
            aria-label="Close player"
          >
            <X className="w-3 h-3" />
            Close
          </button>
        </div>
      ) : (
        <button
          onClick={onActivate}
          disabled={!canPlay}
          className={`self-start inline-flex items-center gap-1.5 text-[10px] uppercase tracking-[0.18em] border px-2 py-1 transition-colors ${
            canPlay
              ? "text-foreground/55 hover:text-white border-white/10 hover:border-white/30"
              : "text-foreground/25 border-white/5 cursor-not-allowed"
          }`}
          title={
            canPlay
              ? `Play synced audio from ${formatTimestamp(
                  wordTimings[0].start_ms
                )}`
              : "No playable video URL on file for this meeting"
          }
          aria-label="Play synced audio"
        >
          <Play className="w-3 h-3" />
          Play at {formatTimestamp(wordTimings[0].start_ms)}
        </button>
      )}
    </div>
  );
}

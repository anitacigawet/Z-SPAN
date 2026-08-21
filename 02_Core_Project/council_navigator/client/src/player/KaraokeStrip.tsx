/**
 * KaraokeStrip — word-level highlight synced to a playback clock
 * (PLAYER-1 C2).
 *
 * Extracted from SyncedQuote so the karaoke rendering is a shell
 * feature over ANY clock-bearing adapter — YouTube and direct-MP4
 * uniformly — instead of being welded to one component's private
 * YT.Player. The strip never touches video bytes: it reads a
 * `getCurrentTimeMs()` callback each animation frame and paints words.
 * That is the whole zero-egress karaoke contract — a readable clock,
 * nothing else.
 *
 * Two highlight modes (verbatim from SyncedQuote):
 *   flat   — solid translucent class on the active word (Cast default).
 *   marker — the "finger sliding across the words" glyph glow: the text
 *            itself lights up (no background box) with a ~10px luminance
 *            edge that glides across the active word as it's spoken +
 *            a colored halo riding that edge; past words stay bright,
 *            future words sit dim. Synced to the clock (NOT a CSS
 *            animation — pausing/seeking moves the edge). Within-word
 *            progress is RAF-driven via direct DOM mutation of
 *            --karaoke-progress on the active span (no 60fps React
 *            re-renders); re-renders fire on word changes only (~1Hz).
 *            The per-word paint lives in index.css (.karaoke-word*).
 *
 * Between words (silence gaps) the previous active word stays lit to
 * avoid flicker. When the clock passes the last word's end + graceMs,
 * `onPassedEnd` fires once per activation (span consumers stop their
 * players there). A null clock (adapter not ready / clockless) simply
 * leaves the highlight where it was — the RAF keeps ticking cheaply.
 */

import { useEffect, useRef, useState } from "react";

export interface QuoteWordTiming {
  word: string;
  start_ms: number;
  end_ms: number;
}

interface KaraokeStripProps {
  wordTimings: QuoteWordTiming[];
  /** Live playback clock in milliseconds; null when not readable. */
  getCurrentTimeMs: () => number | null;
  /** RAF runs only while true; highlight resets when it flips false. */
  running: boolean;
  /** Fires once per activation when the clock passes the last word's
   *  end + graceMs. */
  onPassedEnd?: () => void;
  graceMs?: number;
  /** Marker mode when set (6-char hex, e.g. "#F2A91C"). */
  markerColor?: string;
  /** Flat-mode active-word class. Both literal strings appear in source
   *  so Tailwind JIT generates the CSS. */
  activeWordClassName?: string;
  /** Wrapper <p> classes. */
  className?: string;
  /** Render the surrounding typographic quotes (SyncedQuote look). */
  quoted?: boolean;
}

export default function KaraokeStrip({
  wordTimings,
  getCurrentTimeMs,
  running,
  onPassedEnd,
  graceMs = 500,
  markerColor,
  activeWordClassName = "bg-[#F5A524]/35",
  className = "text-[14px] text-white/85 leading-snug italic",
  quoted = true,
}: KaraokeStripProps) {
  const [activeIndex, setActiveIndex] = useState<number>(-1);
  const rafRef = useRef<number | null>(null);
  const wordSpanRefs = useRef<Array<HTMLSpanElement | null>>([]);
  const paintedIndexRef = useRef(-1);
  const passedEndFiredRef = useRef(false);
  // Clock-smoothing anchors (see readSmoothedClockMs in the effect): the
  // last raw clock sample + the wall-clock time it arrived, so frames
  // between coarse samples can interpolate forward.
  const clockAnchorMsRef = useRef<number | null>(null);
  const clockAnchorAtRef = useRef<number>(0);

  useEffect(() => {
    if (!running) {
      setActiveIndex(-1);
      passedEndFiredRef.current = false;
      paintedIndexRef.current = -1;
      clockAnchorMsRef.current = null;
      return;
    }

    const timings = wordTimings;
    const endMs =
      timings.length > 0 ? timings[timings.length - 1].end_ms : 0;
    const stopMs = endMs + graceMs;

    // The raw adapter clock — especially YouTube's getCurrentTime() —
    // only advances in coarse ~250ms steps, so reading it directly each
    // animation frame freezes the highlight for ~15 frames then jumps:
    // the choppy karaoke. Smooth it — anchor on each fresh raw sample,
    // then glide forward with performance.now() between samples.
    // MAX_AHEAD caps the glide so a paused/buffering clock (raw frozen)
    // can't drift past the true position, and a seek (raw jumps to a new
    // value) re-anchors on the next frame. Direct-MP4 already ticks
    // finely — there raw changes every frame, so this is a no-op and the
    // real win is on YouTube.
    const MAX_AHEAD_MS = 350;
    const readSmoothedClockMs = (): number | null => {
      const raw = getCurrentTimeMs();
      const now =
        typeof performance !== "undefined" ? performance.now() : Date.now();
      if (raw === null) return clockAnchorMsRef.current;
      if (
        clockAnchorMsRef.current === null ||
        raw !== clockAnchorMsRef.current
      ) {
        clockAnchorMsRef.current = raw;
        clockAnchorAtRef.current = now;
        return raw;
      }
      const elapsed = Math.min(now - clockAnchorAtRef.current, MAX_AHEAD_MS);
      return raw + elapsed;
    };

    const tick = () => {
      const currentTimeMs = readSmoothedClockMs();
      if (currentTimeMs === null) {
        // Clock not readable yet — keep waiting.
        rafRef.current = requestAnimationFrame(tick);
        return;
      }

      // Membership is end-exclusive, so the final in-word frame will
      // normally stop just shy of 100%. Complete the previously painted
      // word explicitly once its end passes — whether the clock moves
      // straight into the next word or into a silence gap. The gap still
      // holds that previous word lit, now with a finished wipe.
      if (markerColor && paintedIndexRef.current >= 0) {
        const paintedIndex = paintedIndexRef.current;
        const paintedTiming = timings[paintedIndex];
        if (paintedTiming && currentTimeMs >= paintedTiming.end_ms) {
          wordSpanRefs.current[paintedIndex]?.style.setProperty(
            "--karaoke-progress",
            "100%",
          );
        }
      }

      if (currentTimeMs > stopMs) {
        if (!passedEndFiredRef.current) {
          passedEndFiredRef.current = true;
          setActiveIndex(-1);
          onPassedEnd?.();
        }
        return;
      }

      // Linear scan — quote spans are short (typically 50-150 words);
      // O(n) per frame at 60fps is trivial.
      let nextIndex = -1;
      for (let i = 0; i < timings.length; i++) {
        if (
          currentTimeMs >= timings[i].start_ms &&
          currentTimeMs < timings[i].end_ms
        ) {
          nextIndex = i;
          break;
        }
      }
      // Between words: keep the previous active to avoid flicker.
      if (nextIndex >= 0) {
        setActiveIndex((prev) => (prev === nextIndex ? prev : nextIndex));

        if (markerColor) {
          const t = timings[nextIndex];
          const dur = Math.max(1, t.end_ms - t.start_ms);
          const progress = Math.max(
            0,
            Math.min(1, (currentTimeMs - t.start_ms) / dur),
          );
          wordSpanRefs.current[nextIndex]?.style.setProperty(
            "--karaoke-progress",
            `${progress * 100}%`,
          );
          paintedIndexRef.current = nextIndex;
        }
      }

      rafRef.current = requestAnimationFrame(tick);
    };

    passedEndFiredRef.current = false;
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [running, wordTimings, getCurrentTimeMs, onPassedEnd, graceMs, markerColor]);

  if (wordTimings.length === 0) return null;

  // Marker mode — the "finger sliding across the words" glyph glow. The
  // per-word visual (a luminance wipe + a glyph-clipped halo riding the
  // moving edge, NO background box) lives in index.css (.karaoke-word*,
  // keyed on --karaoke-progress + --karaoke-accent). This component only
  // tags each word's state class and drives --karaoke-progress on the
  // indexed span from the RAF above. Replaces the prior ink-stroke box,
  // which collapsed to an OS-text-selection block at 14px italic and
  // snapped on word-by-word (2026-07-12 Claude<->Codex think-tank).
  return (
    <p
      className={className}
      style={
        markerColor
          ? ({ ["--karaoke-accent" as never]: markerColor } as React.CSSProperties)
          : undefined
      }
    >
      {quoted && <span aria-hidden="true">&ldquo;</span>}
      {wordTimings.map((t, i) => {
        const isActive = i === activeIndex;
        const isPast = activeIndex >= 0 && i < activeIndex;

        if (markerColor) {
          // Word state → CSS class. activeIndex < 0 (pre-playback or
          // passed-end) → no state class, so the whole quote stays at the
          // base 0.85 rather than dimming to the future shade. The active
          // span carries data-glow-word (the ::after halo duplicates it);
          // the indexed ref lets the RAF initialize and drive progress
          // without a React render overwriting the live value.
          const isFuture = activeIndex >= 0 && i > activeIndex;
          const stateClass =
            activeIndex < 0
              ? ""
              : isActive
                ? "karaoke-word--active"
                : isPast
                  ? "karaoke-word--past"
                  : isFuture
                    ? "karaoke-word--future"
                    : "";
          return (
            <span key={i}>
              <span
                ref={(node) => {
                  wordSpanRefs.current[i] = node;
                }}
                className={`karaoke-word ${stateClass}`.trimEnd()}
                data-glow-word={t.word}
                data-start-ms={t.start_ms}
                data-end-ms={t.end_ms}
              >
                {t.word}
              </span>
              {i < wordTimings.length - 1 ? " " : ""}
            </span>
          );
        }

        // Flat mode (Cast page default): solid translucent class.
        return (
          <span key={i}>
            <span
              className={
                isActive
                  ? `rounded-sm px-0.5 -mx-0.5 ${activeWordClassName} text-white transition-colors duration-100`
                  : "transition-colors duration-200"
              }
              data-start-ms={t.start_ms}
              data-end-ms={t.end_ms}
            >
              {t.word}
            </span>
            {i < wordTimings.length - 1 ? " " : ""}
          </span>
        );
      })}
      {quoted && <span aria-hidden="true">&rdquo;</span>}
    </p>
  );
}

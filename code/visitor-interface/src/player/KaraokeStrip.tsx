import { useEffect, useRef, useState } from "react";
export interface QuoteWordTiming {
    word: string;
    start_ms: number;
    end_ms: number;
}
interface KaraokeStripProps {
    wordTimings: QuoteWordTiming[];
    getCurrentTimeMs: () => number | null;
    running: boolean;
    onPassedEnd?: () => void;
    graceMs?: number;
    markerColor?: string;
    activeWordClassName?: string;
    className?: string;
    quoted?: boolean;
}
export default function KaraokeStrip({ wordTimings, getCurrentTimeMs, running, onPassedEnd, graceMs = 500, markerColor, activeWordClassName = "bg-[#F5A524]/35", className = "text-[14px] text-white/85 leading-snug italic", quoted = true, }: KaraokeStripProps) {
    const [activeIndex, setActiveIndex] = useState<number>(-1);
    const rafRef = useRef<number | null>(null);
    const wordSpanRefs = useRef<Array<HTMLSpanElement | null>>([]);
    const paintedIndexRef = useRef(-1);
    const passedEndFiredRef = useRef(false);
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
        const endMs = timings.length > 0 ? timings[timings.length - 1].end_ms : 0;
        const stopMs = endMs + graceMs;
        const MAX_AHEAD_MS = 350;
        const readSmoothedClockMs = (): number | null => {
            const raw = getCurrentTimeMs();
            const now = typeof performance !== "undefined" ? performance.now() : Date.now();
            if (raw === null)
                return clockAnchorMsRef.current;
            if (clockAnchorMsRef.current === null ||
                raw !== clockAnchorMsRef.current) {
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
                rafRef.current = requestAnimationFrame(tick);
                return;
            }
            if (markerColor && paintedIndexRef.current >= 0) {
                const paintedIndex = paintedIndexRef.current;
                const paintedTiming = timings[paintedIndex];
                if (paintedTiming && currentTimeMs >= paintedTiming.end_ms) {
                    wordSpanRefs.current[paintedIndex]?.style.setProperty("--karaoke-progress", "100%");
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
            let nextIndex = -1;
            for (let i = 0; i < timings.length; i++) {
                if (currentTimeMs >= timings[i].start_ms &&
                    currentTimeMs < timings[i].end_ms) {
                    nextIndex = i;
                    break;
                }
            }
            if (nextIndex >= 0) {
                setActiveIndex((prev) => (prev === nextIndex ? prev : nextIndex));
                if (markerColor) {
                    const t = timings[nextIndex];
                    const dur = Math.max(1, t.end_ms - t.start_ms);
                    const progress = Math.max(0, Math.min(1, (currentTimeMs - t.start_ms) / dur));
                    wordSpanRefs.current[nextIndex]?.style.setProperty("--karaoke-progress", `${progress * 100}%`);
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
    if (wordTimings.length === 0)
        return null;
    return (<p className={className} style={markerColor
            ? ({ ["--karaoke-accent" as never]: markerColor } as React.CSSProperties)
            : undefined}>
      {quoted && <span aria-hidden="true">&ldquo;</span>}
      {wordTimings.map((t, i) => {
            const isActive = i === activeIndex;
            const isPast = activeIndex >= 0 && i < activeIndex;
            if (markerColor) {
                const isFuture = activeIndex >= 0 && i > activeIndex;
                const stateClass = activeIndex < 0
                    ? ""
                    : isActive
                        ? "karaoke-word--active"
                        : isPast
                            ? "karaoke-word--past"
                            : isFuture
                                ? "karaoke-word--future"
                                : "";
                return (<span key={i}>
              <span ref={(node) => {
                        wordSpanRefs.current[i] = node;
                    }} className={`karaoke-word ${stateClass}`.trimEnd()} data-glow-word={t.word} data-start-ms={t.start_ms} data-end-ms={t.end_ms}>
                {t.word}
              </span>
              {i < wordTimings.length - 1 ? " " : ""}
            </span>);
            }
            return (<span key={i}>
            <span className={isActive
                    ? `rounded-sm px-0.5 -mx-0.5 ${activeWordClassName} text-white transition-colors duration-100`
                    : "transition-colors duration-200"} data-start-ms={t.start_ms} data-end-ms={t.end_ms}>
              {t.word}
            </span>
            {i < wordTimings.length - 1 ? " " : ""}
          </span>);
        })}
      {quoted && <span aria-hidden="true">&rdquo;</span>}
    </p>);
}

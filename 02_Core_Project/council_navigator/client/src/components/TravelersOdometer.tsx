/**
 * TravelersOdometer — V1-Odometer-1 ChannelsPage-footer travelers counter.
 *
 * Highway-aesthetic mechanical odometer chip. "Travelers" framing per the
 * spec is civic-warm — the people who have joined Z-SPAN, not "members"
 * or "users." Per the highway brand palette in
 * IMAGE_PROMPTS_HIGHWAY_BRAND.md: deep guide-sign green (#0C7A43), warm
 * amber (#F5A524) accent, near-white (#E4E4E5) lettering on near-black
 * (#0E0E10) chip. Flat — no gloss, no glow, no gradient.
 *
 * Mechanical-odometer cue: digits are padded to a fixed width with
 * leading zeros visibly dimmed, mirroring a real dashboard odometer
 * where the unused leading digits are darker. On tick-up the green
 * border briefly flashes amber as a subtle live-broadcast accent.
 *
 * Positioning (revised 2026-07-01 per operator): the chip mounts INLINE
 * inside ChannelsPage's `<footer>` element, right-aligned in the same
 * row as the sunshine-laws/municipal-clerk/technology message — NOT
 * fixed/floating across every page like V0 shipped. Persisting across
 * every view was the wrong default; it belongs in the website's actual
 * footer alongside other footer-class content.
 *
 * Honest-empty: if the backend can't return a count (network error,
 * 500, etc.), the chip simply does not render — never shows "—" or
 * "loading…" indefinitely. Composes with [[grey-dot-state-honesty]] /
 * D-128 honest-state discipline.
 */
import { useEffect, useRef, useState } from "react";
import { fetchForPlane } from "../lib/planeFetch";

interface TravelersResponse {
  success: boolean;
  count?: number;
}

const REFRESH_INTERVAL_MS = 60_000;
const TICK_FLASH_MS = 1_200;
const DIGIT_PAD = 5; // 00042 — odometer-style leading zeros up to 99,999

export function TravelersOdometer() {
  const [count, setCount] = useState<number | null>(null);
  const [tickFlash, setTickFlash] = useState(false);
  const lastCountRef = useRef<number | null>(null);
  const flashTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function fetchCount() {
      try {
        const res = await fetchForPlane(
          {
            publicPath: "/public-api/travelers",
            operatorPath: "/api/travelers",
          },
          { credentials: "include" },
        );
        if (!res.ok) return;
        const data = (await res.json()) as TravelersResponse;
        if (cancelled) return;
        if (!data.success || typeof data.count !== "number") return;

        if (
          lastCountRef.current !== null &&
          data.count > lastCountRef.current
        ) {
          setTickFlash(true);
          if (flashTimeoutRef.current !== null) {
            window.clearTimeout(flashTimeoutRef.current);
          }
          flashTimeoutRef.current = window.setTimeout(
            () => setTickFlash(false),
            TICK_FLASH_MS,
          );
        }
        lastCountRef.current = data.count;
        setCount(data.count);
      } catch {
        // honest-fail: leave whatever count we last had; never show a
        // placeholder dash that lies about live state.
      }
    }

    fetchCount();
    const intervalId = window.setInterval(fetchCount, REFRESH_INTERVAL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      if (flashTimeoutRef.current !== null) {
        window.clearTimeout(flashTimeoutRef.current);
      }
    };
  }, []);

  if (count === null) return null;

  const padded = String(count).padStart(DIGIT_PAD, "0");
  const leadingZeroCount = padded.length - String(count).length;

  return (
    <div
      className="select-none"
      aria-label={`${count.toLocaleString()} travelers have joined Z-SPAN`}
      title={`${count.toLocaleString()} travelers have joined Z-SPAN`}
    >
      <div
        className={`inline-flex items-center gap-2 rounded-md bg-[#0E0E10]/95 px-2.5 py-1.5 shadow-sm transition-colors duration-700 ease-out ${
          tickFlash ? "ring-1 ring-[#F5A524]" : ""
        }`}
      >
        {/* Highway guide-sign green status dot */}
        <span
          className="inline-block h-1.5 w-1.5 rounded-full bg-[#0C7A43]"
          aria-hidden="true"
        />
        {/* Mechanical-odometer digit row */}
        <span className="font-mono text-[12px] leading-none tracking-[0.18em] tabular-nums text-[#E4E4E5]">
          {padded.split("").map((ch, i) => (
            <span key={i} className={i < leadingZeroCount ? "text-[#E4E4E5]/25" : ""}>
              {ch}
            </span>
          ))}
        </span>
        {/* Category eyebrow per D-054 — visual furniture, not content */}
        <span className="text-[9px] uppercase tracking-[0.2em] text-[#71717A]">
          travelers
        </span>
      </div>
    </div>
  );
}

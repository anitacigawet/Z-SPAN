/**
 * SignInBenefitsToast — a gentle, one-time "Did you know?" card that invites
 * an anonymous viewer to sign in, shown when they first open a broadcast.
 *
 * Floats in from the top-right, runs a visible countdown (from 7) in its
 * top-right corner, then floats away — so it reads as a non-annoying nudge,
 * not a persistent nag. Hovering the countdown swaps it to an X dismiss
 * button AND pauses the countdown; mouse-leave resumes both (countdown
 * resumes from where it paused).
 *
 * Instead of repeating the login link in the body, the
 * card carries a small up-arrow cue pointing at the actual SignInPill
 * directly above it in the TopBar — and on mount dispatches a custom
 * event that triggers a one-lap pixie-dust shimmer around the pill
 * (SignInPillShimmer.tsx, intentional-detail family per james_project_lens).
 */
import { useEffect, useState } from "react";
import { X } from "lucide-react";

import { SIGNIN_PILL_SHIMMER_EVENT } from "./SignInPillShimmer";

const COUNTDOWN_FROM = 7; // seconds
const EXIT_MS = 320; // must match the leave transition duration below

type Benefit = { label: string; locked?: "byok" };

// Session-103 (product-slice4): Infographic generation is a free signed-in
// feature now — the client-side Canvas share-card renders straight from
// the meeting's existing outputs (title, date, tagline, key decisions)
// with no provider key required. Audio updates stays under the BYOK badge
// (D-133 lineage — Tier 2 ElevenLabs/OpenAI-TTS still applies for that
// one). Notifications + "and more" are free/storage-tier as before.
const BENEFITS: Benefit[] = [
  { label: "Infographic" },
  { label: "Notifications" },
  { label: "Audio updates", locked: "byok" },
  { label: "and more" },
];

export function SignInBenefitsToast({ onDismiss }: { onDismiss: () => void }) {
  // `shown` drives the enter/leave transition; `remaining` is the countdown;
  // `paused` freezes the countdown while hovered; `hoverDismiss` swaps the
  // visible countdown chip with the X icon on hover.
  const [shown, setShown] = useState(false);
  const [remaining, setRemaining] = useState(COUNTDOWN_FROM);
  const [paused, setPaused] = useState(false);
  const [hoverDismiss, setHoverDismiss] = useState(false);

  // Animate in on mount AND fire the one-lap shimmer around the
  // SignInPill in the TopBar. The shimmer event is dispatched a beat
  // after mount so the toast's slide-in and the pill's outline trace
  // run in sync visually (the viewer's eye lands on the pill area just
  // as the trace starts).
  useEffect(() => {
    const rafId = requestAnimationFrame(() => setShown(true));
    const shimmerTimer = window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent(SIGNIN_PILL_SHIMMER_EVENT));
    }, 180);
    return () => {
      cancelAnimationFrame(rafId);
      window.clearTimeout(shimmerTimer);
    };
  }, []);

  // Begin the slide-out, then unmount after the transition finishes.
  const leave = () => {
    setShown(false);
    window.setTimeout(onDismiss, EXIT_MS);
  };

  // Countdown tick. When it reaches 0, float away. Paused on hover.
  useEffect(() => {
    if (paused) return;
    if (remaining <= 0) {
      leave();
      return;
    }
    const t = window.setTimeout(() => setRemaining((r) => r - 1), 1000);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remaining, paused]);

  return (
    <div
      role="status"
      aria-live="polite"
      className={`fixed right-4 top-16 z-[70] w-[19rem] transition-all duration-300 ease-out ${
        shown ? "translate-x-0 opacity-100" : "translate-x-6 opacity-0"
      }`}
    >
      <div className="relative rounded-lg border border-[var(--line)] bg-[var(--surface-2)]/95 p-3 shadow-2xl backdrop-blur">
        {/* Top row — countdown↔X swap (left) + up-arrow cue (right)
            pointing at the SignInPill in the TopBar directly above the
            toast's right edge. Arrow positioned right-side because the
            SignInPill is right-aligned in the TopBar above us; arrow
            on the LEFT of the toast pointed at empty space (caught by
            the 2026-06-24 brainstorm-audit). The up-arrow itself is a
            visual cue, not a link — the actual button is right up there. */}
        <div className="mb-2 flex items-center justify-between">
          <button
            type="button"
            onClick={leave}
            onMouseEnter={() => {
              setHoverDismiss(true);
              setPaused(true);
            }}
            onMouseLeave={() => {
              setHoverDismiss(false);
              setPaused(false);
            }}
            onFocus={() => {
              setHoverDismiss(true);
              setPaused(true);
            }}
            onBlur={() => {
              setHoverDismiss(false);
              setPaused(false);
            }}
            aria-label={
              hoverDismiss
                ? "Dismiss"
                : `Auto-dismisses in ${remaining} seconds — hover to cancel`
            }
            className="inline-flex h-5 w-5 items-center justify-center rounded-full border border-[var(--line)] bg-[var(--surface)] text-[10px] tabular-nums text-foreground/60 transition-colors hover:text-white hover:border-[var(--line-strong)]"
          >
            {hoverDismiss ? (
              <X className="h-3 w-3" />
            ) : (
              <span aria-hidden="true">{remaining}</span>
            )}
          </button>
          <span
            aria-label="Log in — up there"
            title="Log in — up there"
            className="inline-flex h-5 w-5 items-center justify-center rounded-full text-foreground/55 transition-colors hover:text-white"
          >
            {/* Animated up-chevron — gentle 1.6s bob in sync with the
                shimmer's first lap so the viewer's eye picks up the
                direction without it being a banner.  */}
            <svg
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="none"
              xmlns="http://www.w3.org/2000/svg"
              style={{
                animation: "zspan-toast-uparrow-bob 1.8s ease-in-out 1",
              }}
            >
              <path
                d="M2.5 7.5 L6 4 L9.5 7.5"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
        </div>

        <p className="text-[13px] font-semibold tracking-tight text-white">
          Did you know?
        </p>
        <p className="mt-0.5 text-[12px] text-foreground/70">
          Users who log in get access to
        </p>

        <ul className="mt-2 space-y-1">
          {BENEFITS.map((b) => {
            const isLocked = b.locked === "byok";
            return (
              <li
                key={b.label}
                className={`flex items-center gap-2 text-[12px] ${
                  isLocked ? "text-foreground/45" : "text-foreground/85"
                }`}
              >
                <span
                  className="kg-dot-active inline-block flex-shrink-0"
                  style={{
                    width: 4,
                    height: 4,
                    opacity: isLocked ? 0.45 : 1,
                  }}
                  aria-hidden="true"
                />
                <span>{b.label}</span>
                {isLocked && (
                  <span
                    title="Requires your own provider API key (Tier 2 paid BYOK — D-133)"
                    aria-label="Requires your own provider API key"
                    className="ml-auto rounded border border-foreground/15 px-1.5 py-px text-[9px] uppercase tracking-wider text-foreground/45"
                  >
                    BYOK
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      {/* Module-level keyframes for the up-arrow bob. Inlined here so
          the toast is self-contained; the shimmer ships its own
          stylesheet. */}
      <style>{`
        @keyframes zspan-toast-uparrow-bob {
          0%   { transform: translateY(0);    opacity: 0; }
          15%  { opacity: 1; }
          40%  { transform: translateY(-3px); opacity: 1; }
          70%  { transform: translateY(0);    opacity: 1; }
          100% { transform: translateY(0);    opacity: 0.55; }
        }
      `}</style>
    </div>
  );
}

/**
 * CityDeskTour — the guided walkthrough over the City Desk demo,
 * ported from the operator's The-Cacti onboarding (S-123 iteration 4).
 *
 * What's theirs (kept faithfully): the step schema with per-step
 * auto-advance reading times; the spotlight ring (measured element +
 * 9999px box-shadow dim, retry-find while the target mounts,
 * scrollIntoView, re-measure on resize/scroll); the narration card that
 * places itself below/above the ring (or centers when there's no
 * target) with a pause-on-hover progress bar, Back/Next/Skip and the
 * "n / m" counter; the versioned first-visit auto-start with a replay
 * button. Their framer-motion card transition became a small CSS
 * animation so this stays dependency-free.
 *
 * What's extended (needed because this tour walks a live state machine,
 * not routes): each step may carry an `action` the demo executes on
 * entry (open the sample agenda, start routing, cast the sample votes,
 * adjourn), and `advanceWhen` — a state predicate polled while the step
 * is up, so the tour can start the tree growing, narrate over it, and
 * roll on the moment it blooms. That's what makes it play like the
 * 30-second video: the demo performs itself while the tour narrates.
 *
 * All visitor-entered demo data stays session-only; the ONE thing this
 * writes is the operator-side seen-it flag (same pattern as The-Cacti's
 * cacti-onboarded key).
 */
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ArrowRight, X } from "lucide-react";

export const TOUR_VERSION = "v1";
export const TOUR_STORAGE_KEY = "zspan_city_desk_tour";

export function hasSeenTour(): boolean {
  try {
    return localStorage.getItem(TOUR_STORAGE_KEY) === TOUR_VERSION;
  } catch {
    return true; // storage unavailable → don't force the tour
  }
}

function persistSeen() {
  try {
    localStorage.setItem(TOUR_STORAGE_KEY, TOUR_VERSION);
  } catch {
    /* fine — it just won't be remembered */
  }
}

/** Actions the demo page lends the tour. Every one operates ONLY on the
 *  sample agenda. */
export interface TourActions {
  gotoShelf: () => void;
  openSample: () => void;
  startSampleRouting: () => void;
  sampleIsBloomed: () => boolean;
  castSampleVotes: () => void;
  adjournSample: () => void;
  resetSample: () => void;
  setDrawerTab: (tab: "agenda" | "meeting" | "minutes" | "behind") => void;
}

interface TourStep {
  id: string;
  highlightSelector?: string;
  title: string;
  narration: string;
  /** Auto-advance delay; null = wait for Next (or advanceWhen). */
  readingTimeMs: number | null;
  /** Runs when the step becomes current. */
  action?: (a: TourActions) => void;
  /** Polled ~4×/s; advances the moment it returns true. */
  advanceWhen?: (a: TourActions) => boolean;
  isFinale?: boolean;
}

const STEPS: TourStep[] = [
  {
    id: "welcome",
    title: "The City Desk, in about a minute",
    narration:
      "This is the working side of the network — where a meeting goes from first draft to certified minutes. Sit back; the desk will drive itself. Skip anytime.",
    readingTimeMs: 7000,
    action: (a) => a.gotoShelf(),
  },
  {
    id: "shelf",
    highlightSelector: '[data-tour="shelf"]',
    title: "Every meeting starts as a file on the shelf",
    narration:
      "Open one that's underway, or start a fresh agenda with nothing but a name. We'll open the sample meeting.",
    readingTimeMs: 7000,
  },
  {
    id: "stage",
    highlightSelector: '[data-tour="stage"]',
    title: "The agenda grows like a plant",
    narration:
      "Each item on the agenda is a leaf. The tall center leaf is the meeting record itself, and the bud at the crown will become the official minutes.",
    readingTimeMs: 8500,
    action: (a) => a.openSample(),
  },
  {
    id: "intake",
    highlightSelector: '[data-tour="intake"]',
    title: "Items go on in plain words",
    narration:
      "A department head types what they need — the thing, the department, the dollar amount. No forms training, no vendor manual.",
    readingTimeMs: 7500,
  },
  {
    id: "routing",
    // The spotlight belongs on the TREE being grown, not the status
    // badge (operator feedback 2026-07-02 — "the focal focus is on the
    // tour box itself instead of the actual tree").
    highlightSelector: '[data-tour="stage"]',
    title: "Now watch the approvals route",
    narration:
      "Drafted, fiscally reviewed, legally signed off, set by the clerk — each hand-off recorded with who and when. In the live desk these are real signatures; the leaf fills in as they land.",
    readingTimeMs: null,
    action: (a) => a.startSampleRouting(),
    advanceWhen: (a) => a.sampleIsBloomed(),
  },
  {
    id: "drawer",
    highlightSelector: '[data-tour="drawer"]',
    title: "Fully routed — the desk opens",
    narration:
      "The tree steps aside and the working drawer slides in. From here the clerk runs the meeting itself.",
    readingTimeMs: 7000,
  },
  {
    id: "meeting",
    highlightSelector: '[data-tour="meeting"]',
    title: "The live meeting, one screen",
    narration:
      "Call an item, run the public-comment clock, tap each vote as it's spoken. Watch the tall center leaf fill in — that's the record building itself.",
    readingTimeMs: 9000,
    action: (a) => {
      a.setDrawerTab("meeting");
      a.castSampleVotes();
    },
  },
  {
    id: "minutes",
    highlightSelector: '[data-tour="minutes-area"]',
    title: "Adjourn — and the minutes exist",
    narration:
      "The moment the gavel falls, the draft minutes assemble from what actually happened: every item, every tally. Hours of typing becomes a read-through, and the crown blooms.",
    readingTimeMs: 9000,
    action: (a) => {
      a.adjournSample();
      a.setDrawerTab("minutes");
    },
  },
  {
    id: "finale",
    title: "Your turn",
    narration:
      "That's the whole arc — draft to certified record on one desk, with the public side published automatically. The sample just reset; try it with your own items. Nothing you type here is saved.",
    readingTimeMs: null,
    isFinale: true,
    action: (a) => a.resetSample(),
  },
];

const RING_PAD = 8;
const CARD_W = 360;
const GAP = 16;

interface CityDeskTourProps {
  run: boolean;
  actions: TourActions;
  onFinish: () => void;
}

export function CityDeskTour({ run, actions, onFinish }: CityDeskTourProps) {
  const [stepIndex, setStepIndex] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const [progress, setProgress] = useState(0);
  const elapsedRef = useRef(0);
  const pausedRef = useRef(false);
  const actionsRef = useRef(actions);
  actionsRef.current = actions;

  const step = run ? STEPS[stepIndex] : null;

  const finish = useCallback(() => {
    persistSeen();
    onFinish();
    setStepIndex(0);
  }, [onFinish]);

  const next = useCallback(() => {
    setStepIndex((i) => {
      if (i >= STEPS.length - 1) {
        persistSeen();
        onFinish();
        return 0;
      }
      return i + 1;
    });
  }, [onFinish]);

  const prev = useCallback(() => setStepIndex((i) => Math.max(0, i - 1)), []);

  // Run the step's action once on entry.
  useEffect(() => {
    if (!run || !step) return;
    step.action?.(actionsRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, step?.id]);

  // State-predicate advance (the routing step).
  useEffect(() => {
    if (!run || !step?.advanceWhen) return;
    const id = window.setInterval(() => {
      if (step.advanceWhen!(actionsRef.current)) {
        window.clearInterval(id);
        // A beat so the bloom transition is seen before the card moves.
        window.setTimeout(next, 1200);
      }
    }, 250);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, step?.id, next]);

  // Locate + measure the highlighted element (retries while it mounts).
  useEffect(() => {
    if (!run || !step?.highlightSelector) {
      setRect(null);
      return;
    }
    let cancelled = false;
    let tries = 0;
    const selector = step.highlightSelector;
    const find = () => {
      if (cancelled) return;
      const el = document.querySelector(selector);
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        window.setTimeout(() => {
          if (!cancelled) setRect(el.getBoundingClientRect());
        }, 280);
      } else if (tries < 20) {
        tries += 1;
        window.setTimeout(find, 150);
      } else {
        setRect(null); // give up gracefully → centered card
      }
    };
    find();
    return () => {
      cancelled = true;
    };
  }, [run, step?.id, step?.highlightSelector]);

  // Keep the ring aligned on resize/scroll — plus a gentle interval so
  // the ring stays glued through the demo's OWN animations (the 700ms
  // bloom pane-shrink moves the stage while no scroll event fires).
  useEffect(() => {
    if (!run || !step?.highlightSelector) return;
    const selector = step.highlightSelector;
    const remeasure = () => {
      const el = document.querySelector(selector);
      if (el) setRect(el.getBoundingClientRect());
    };
    window.addEventListener("resize", remeasure);
    window.addEventListener("scroll", remeasure, true);
    const id = window.setInterval(remeasure, 300);
    return () => {
      window.removeEventListener("resize", remeasure);
      window.removeEventListener("scroll", remeasure, true);
      window.clearInterval(id);
    };
  }, [run, step?.id, step?.highlightSelector]);

  // Auto-advance with pause-on-hover.
  useEffect(() => {
    setProgress(0);
    elapsedRef.current = 0;
    if (!run || !step || step.readingTimeMs == null) return;
    const duration = step.readingTimeMs;
    const tick = 50;
    const id = window.setInterval(() => {
      if (pausedRef.current) return;
      elapsedRef.current += tick;
      const p = Math.min(1, elapsedRef.current / duration);
      setProgress(p);
      if (p >= 1) {
        window.clearInterval(id);
        next();
      }
    }, tick);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, step?.id, next]);

  if (!run || !step) return null;

  const isCentered = !step.highlightSelector || !rect;
  const isFinale = !!step.isFinale;

  // Estimated card height for viewport-fit decisions (title + 3-4 lines
  // + controls). Slight over-estimate is safe — it only biases toward
  // the roomier placement.
  const CARD_H_EST = 240;

  let cardStyle: CSSProperties;
  if (isCentered) {
    cardStyle = {
      position: "fixed",
      left: "50%",
      top: "50%",
      transform: "translate(-50%, -50%)",
      width: CARD_W,
    };
  } else {
    const ringBottom = rect.bottom + RING_PAD;
    const ringTop = rect.top - RING_PAD;
    const centerX = rect.left + rect.width / 2;
    const left = Math.min(
      Math.max(GAP, centerX - CARD_W / 2),
      window.innerWidth - CARD_W - GAP,
    );
    // Operator feedback 2026-07-02: cards were escaping the viewport
    // (top-clipped above the ring, bottom-anchored past the fold). Fit
    // check both placements explicitly; when NEITHER side has room
    // (e.g. the ring is the full-height stage), clamp inside the
    // viewport near the ring's bottom-left instead of overflowing.
    const fitsBelow = ringBottom + GAP + CARD_H_EST <= window.innerHeight - GAP;
    const fitsAbove = ringTop - GAP - CARD_H_EST >= GAP;
    if (fitsBelow) {
      cardStyle = { position: "fixed", left, top: ringBottom + GAP, width: CARD_W };
    } else if (fitsAbove) {
      cardStyle = {
        position: "fixed",
        left,
        top: ringTop - GAP,
        width: CARD_W,
        transform: "translateY(-100%)",
      };
    } else {
      cardStyle = {
        position: "fixed",
        left,
        top: Math.max(GAP, Math.min(window.innerHeight - CARD_H_EST - GAP, ringBottom - CARD_H_EST)),
        width: CARD_W,
      };
    }
  }

  const overlay = (
    <div className="city-desk fixed inset-0 z-[100]" style={{ pointerEvents: "none", minHeight: 0, background: "none" }}>
      <style>{`
        @keyframes cd-tour-card-in {
          from { opacity: 0; transform: translateY(12px); }
          to   { opacity: 1; transform: translateY(0); }
        }
      `}</style>

      {/* Dim + spotlight ring */}
      {isCentered ? (
        <div className="absolute inset-0" style={{ pointerEvents: "auto", background: "rgba(0,0,0,0.75)" }} />
      ) : (
        <div
          style={{
            position: "fixed",
            left: rect.left - RING_PAD,
            top: rect.top - RING_PAD,
            width: rect.width + RING_PAD * 2,
            height: rect.height + RING_PAD * 2,
            borderRadius: 8,
            boxShadow:
              "0 0 0 9999px rgba(0,0,0,0.72), 0 0 0 2px oklch(0.93 0.015 85), 0 0 24px 4px oklch(0.68 0.13 138 / 0.35)",
            transition: "all 0.3s ease",
            pointerEvents: "none",
          }}
        />
      )}

      {/* Narration card — paper-framed */}
      <div
        key={step.id}
        style={{
          ...cardStyle,
          pointerEvents: "auto",
          background: "oklch(0.24 0.015 65)",
          border: "1.4px solid oklch(0.93 0.015 85)",
          borderRadius: 6,
          overflow: "hidden",
          boxShadow: "0 12px 36px -18px rgb(0 0 0 / 0.7)",
          animation: "cd-tour-card-in 0.28s ease-out",
        }}
        onMouseEnter={() => {
          pausedRef.current = true;
        }}
        onMouseLeave={() => {
          pausedRef.current = false;
        }}
      >
        {/* Auto-advance progress bar */}
        {step.readingTimeMs != null && (
          <div style={{ height: 3, width: "100%", background: "oklch(0.93 0.015 85 / 0.12)" }}>
            <div
              style={{
                height: "100%",
                width: `${progress * 100}%`,
                background: "oklch(0.68 0.13 138)",
              }}
            />
          </div>
        )}
        {/* Waiting-on-the-tree shimmer bar for predicate steps */}
        {step.readingTimeMs == null && step.advanceWhen && (
          <div style={{ height: 3, width: "100%", background: "oklch(0.93 0.015 85 / 0.12)" }}>
            <div
              style={{
                height: "100%",
                width: "35%",
                background: "oklch(0.78 0.15 75)",
                animation: "cd-ink-blink 1.2s ease-in-out infinite",
              }}
            />
          </div>
        )}

        <div style={{ padding: 16 }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 }}>
            <h3
              style={{
                fontFamily: '"Atkinson Hyperlegible", sans-serif',
                fontWeight: 700,
                fontSize: 16,
                lineHeight: 1.25,
                color: "oklch(0.93 0.015 85)",
                margin: 0,
              }}
            >
              {step.title}
            </h3>
            <button
              onClick={finish}
              aria-label="Skip tour"
              style={{
                padding: 4,
                marginRight: -4,
                borderRadius: 4,
                color: "oklch(0.72 0.014 80)",
                background: "transparent",
                border: "none",
                cursor: "pointer",
              }}
            >
              <X size={16} />
            </button>
          </div>
          <p
            style={{
              fontFamily: '"Atkinson Hyperlegible", sans-serif',
              fontSize: 14,
              lineHeight: 1.55,
              color: "oklch(0.72 0.014 80)",
              margin: "8px 0 0",
            }}
          >
            {step.narration}
          </p>

          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 16 }}>
            <span
              className="mono"
              style={{ fontSize: 10, letterSpacing: "0.08em", color: "oklch(0.72 0.014 80 / 0.7)" }}
            >
              {stepIndex + 1} / {STEPS.length}
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {stepIndex > 0 && !step.advanceWhen && (
                <button
                  onClick={prev}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                    padding: "6px 10px",
                    borderRadius: 4,
                    fontFamily: '"Atkinson Hyperlegible", sans-serif',
                    fontSize: 12,
                    color: "oklch(0.72 0.014 80)",
                    background: "transparent",
                    border: "none",
                    cursor: "pointer",
                  }}
                >
                  <ArrowLeft size={14} />
                  Back
                </button>
              )}
              <button
                onClick={next}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                  padding: "8px 14px",
                  borderRadius: 4,
                  fontFamily: '"Atkinson Hyperlegible", sans-serif',
                  fontWeight: 600,
                  fontSize: 13,
                  lineHeight: 1,
                  color: "oklch(0.20 0.014 60)",
                  background: "oklch(0.93 0.015 85)",
                  border: "1.4px solid oklch(0.93 0.015 85)",
                  cursor: "pointer",
                }}
              >
                {isFinale
                  ? "hand me the desk"
                  : stepIndex === 0
                    ? "take the tour"
                    : step.advanceWhen
                      ? "skip ahead"
                      : "next"}
                {!isFinale && <ArrowRight size={14} />}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );

  return createPortal(overlay, document.body);
}

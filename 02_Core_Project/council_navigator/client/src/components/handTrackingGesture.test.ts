/** Pure gesture, mapping, filtering, release, and coast tests. No DOM/webcam. */
import { describe, expect, it } from "vitest";
import {
  computePinchRatio,
  isScrollableStyle,
  mapComfortBox,
  OneEuroFilter,
  estimateReleaseVelocity,
  coastStep,
  reduceGesture,
  createGestureContext,
  DEFAULT_COMFORT_BOX,
  DEFAULT_PARAMS,
  type GestureContext,
  type GestureEffect,
  type GestureInput,
  type Landmark,
  type MotionSample,
} from "./handTrackingGesture";

const P = DEFAULT_PARAMS;

function landmarksWithPinch(fingertipGap: number): Landmark[] {
  const lm: Landmark[] = Array.from({ length: 21 }, () => ({
    x: 0,
    y: 0,
    z: 0,
  }));
  lm[0] = { x: 0.5, y: 0.6, z: 0 };
  lm[5] = { x: 0.45, y: 0.4, z: 0 };
  lm[9] = { x: 0.5, y: 0.4, z: 0 };
  lm[17] = { x: 0.6, y: 0.42, z: 0 };
  lm[4] = { x: 0.5, y: 0.5, z: 0 };
  lm[8] = { x: 0.5 + fingertipGap, y: 0.5, z: 0 };
  return lm;
}

type Frame = Partial<GestureInput> & { now: number };
function frame(f: Frame): GestureInput {
  return {
    freshObservation: true,
    observationAtMs: f.now,
    hasHand: true,
    pinchRatio: 0.7,
    cursorX: 400,
    cursorY: 300,
    clickableUnderCursor: false,
    blockedDirection: null,
    scrollTargetValid: true,
    ...f,
  };
}
const closed = (f: Frame) => frame({ pinchRatio: 0.2, ...f });
const open = (f: Frame) => frame({ pinchRatio: 0.7, ...f });
const gone = (f: Frame) =>
  frame({ hasHand: false, pinchRatio: Infinity, ...f });
const stale = (f: Frame) => frame({ ...f, freshObservation: false });

interface Step {
  phase: string;
  reason: string;
  effect: GestureEffect;
  ctx: GestureContext;
}
function drive(frames: GestureInput[], start = createGestureContext()) {
  let ctx = start;
  const steps: Step[] = [];
  for (const input of frames) {
    const result = reduceGesture(ctx, input, P);
    ctx = result.ctx;
    steps.push({
      phase: ctx.phase,
      reason: result.reason,
      effect: result.effect,
      ctx,
    });
  }
  return { ctx, steps };
}
const effectKinds = (steps: Step[]) => steps.map(step => step.effect.kind);
const clickCount = (steps: Step[]) =>
  steps.filter(step => step.effect.kind === "click").length;

function flickFrames(clickableUnderCursor = false): GestureInput[] {
  return [
    closed({ now: 0, cursorY: 400, clickableUnderCursor }),
    closed({ now: 30, cursorY: 370, clickableUnderCursor }),
    closed({ now: 60, cursorY: 340, clickableUnderCursor }),
    closed({ now: 90, cursorY: 310, clickableUnderCursor }),
    open({ now: 120, cursorY: 280, clickableUnderCursor }),
    open({ now: 150, cursorY: 280, clickableUnderCursor }),
    open({ now: 180, cursorY: 280, clickableUnderCursor }),
    open({ now: 210, cursorY: 280, clickableUnderCursor }),
  ];
}

describe("computePinchRatio", () => {
  it("reads a near fingertip pair as closed", () => {
    expect(computePinchRatio(landmarksWithPinch(0.02))).toBeLessThan(
      P.pinchCloseRatio
    );
  });

  it("reads a separated fingertip pair as open", () => {
    expect(computePinchRatio(landmarksWithPinch(0.2))).toBeGreaterThan(
      P.pinchOpenRatio
    );
  });

  it("returns Infinity for missing landmarks", () => {
    expect(computePinchRatio([])).toBe(Number.POSITIVE_INFINITY);
  });
});

describe("isScrollableStyle", () => {
  it("accepts scrolling overflow with real overflow", () => {
    expect(isScrollableStyle("auto", 2000, 800)).toBe(true);
    expect(isScrollableStyle("scroll", 2000, 800)).toBe(true);
    expect(isScrollableStyle("overlay", 2000, 800)).toBe(true);
  });

  it("rejects non-scrolling styles and content without overflow", () => {
    expect(isScrollableStyle("visible", 2000, 800)).toBe(false);
    expect(isScrollableStyle("hidden", 2000, 800)).toBe(false);
    expect(isScrollableStyle("auto", 800, 800)).toBe(false);
  });
});

describe("mapComfortBox", () => {
  it("maps the comfort-box center to viewport center", () => {
    const b = DEFAULT_COMFORT_BOX;
    const mapped = mapComfortBox(
      (b.xMin + b.xMax) / 2,
      (b.yMin + b.yMax) / 2,
      b
    );
    expect(mapped.x).toBeCloseTo(0.5);
    expect(mapped.y).toBeCloseTo(0.5);
  });

  it("hard-clamps all four box corners to viewport corners", () => {
    const b = DEFAULT_COMFORT_BOX;
    expect(mapComfortBox(b.xMin, b.yMin, b)).toEqual({ x: 0, y: 0 });
    expect(mapComfortBox(b.xMax, b.yMin, b)).toEqual({ x: 1, y: 0 });
    expect(mapComfortBox(b.xMin, b.yMax, b)).toEqual({ x: 0, y: 1 });
    expect(mapComfortBox(b.xMax, b.yMax, b)).toEqual({ x: 1, y: 1 });
  });

  it("creates a deterministic 3% edge shelf slightly inside each bound", () => {
    const b = DEFAULT_COMFORT_BOX;
    const xShelf = (b.overscan / (1 + 2 * b.overscan)) * (b.xMax - b.xMin);
    const yShelf = (b.overscan / (1 + 2 * b.overscan)) * (b.yMax - b.yMin);
    expect(mapComfortBox(b.xMin + xShelf * 0.9, 0.5, b).x).toBe(0);
    expect(mapComfortBox(b.xMax - xShelf * 0.9, 0.5, b).x).toBe(1);
    expect(mapComfortBox(0.5, b.yMin + yShelf * 0.9, b).y).toBe(0);
    expect(mapComfortBox(0.5, b.yMax - yShelf * 0.9, b).y).toBe(1);
  });

  it("is memoryless: repeated inputs produce identical outputs", () => {
    const outputs = Array.from({ length: 20 }, () =>
      mapComfortBox(0.413, 0.637, DEFAULT_COMFORT_BOX)
    );
    expect(outputs.every(value => JSON.stringify(value) === JSON.stringify(outputs[0]))).toBe(true);
  });
});

function variance(values: number[]): number {
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  return values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / values.length;
}

describe("OneEuroFilter", () => {
  it("reduces stationary noise more than the old 0.35 EMA", () => {
    const raw = Array.from({ length: 120 }, (_, i) =>
      500 + Math.sin(i * 2.17) * 5 + Math.cos(i * 0.71) * 2
    );
    const oneEuro = new OneEuroFilter();
    const filtered = raw.map((value, i) => oneEuro.filter(value, i * 33));
    let ema = raw[0];
    const emaValues = raw.map(value => (ema += (value - ema) * 0.35));
    expect(variance(filtered.slice(15))).toBeLessThan(variance(emaValues.slice(15)));
  });

  it("adapts to a fast transit with less lag than a stationary cutoff", () => {
    const adaptive = new OneEuroFilter();
    const fixed = new OneEuroFilter(1.2, 0, 1);
    let raw = 0;
    let adaptiveValue = 0;
    let fixedValue = 0;
    for (let i = 0; i <= 30; i += 1) {
      raw = i * 16;
      adaptiveValue = adaptive.filter(raw, i * 16);
      fixedValue = fixed.filter(raw, i * 16);
    }
    expect(raw - adaptiveValue).toBeLessThan(raw - fixedValue);
    expect(raw - adaptiveValue).toBeLessThan(90);
  });

  it("ends near the same trajectory under irregular 30Hz and 60Hz cadence", () => {
    const sample = (steps: number[]) => {
      const filter = new OneEuroFilter();
      let t = 0;
      let result = 0;
      let i = 0;
      while (t < 1000) {
        result = filter.filter(t * 0.6, t);
        t = Math.min(1000, t + steps[i++ % steps.length]);
      }
      return filter.filter(600, 1000);
    };
    expect(Math.abs(sample([31, 35, 33]) - sample([15, 18, 16, 17]))).toBeLessThan(8);
  });

  it("snaps exactly to raw after reset and ignores non-advancing time", () => {
    const filter = new OneEuroFilter();
    filter.filter(10, 0);
    const prior = filter.filter(20, 16);
    expect(filter.filter(100, 16)).toBe(prior);
    expect(filter.filter(100, -10)).toBe(prior);
    filter.reset();
    expect(filter.filter(777, 1000)).toBe(777);
  });
});

describe("estimateReleaseVelocity", () => {
  it("qualifies constant upward motion with positive scroll-space velocity", () => {
    const samples = [0, 30, 60, 90, 120].map((atMs, i) => ({
      atMs,
      y: 400 - i * 30,
    }));
    expect(estimateReleaseVelocity(samples, P.drag)).toEqual({
      qualified: true,
      scrollVelocityPxPerSec: 1000,
    });
  });

  it("rejects one teleport segment without changing the normal estimate", () => {
    const samples = [
      { atMs: 0, y: 400 },
      { atMs: 30, y: 370 },
      { atMs: 60, y: 1000 },
      { atMs: 90, y: 310 },
      { atMs: 120, y: 280 },
    ];
    expect(estimateReleaseVelocity(samples, P.drag)).toEqual({
      qualified: true,
      scrollVelocityPxPerSec: 1000,
    });
  });

  it("follows the terminal window after a direction reversal", () => {
    const oldUp = [0, 30, 60, 90, 120].map((atMs, i) => ({
      atMs,
      y: 400 - i * 30,
    }));
    const terminalDown = [150, 180, 210, 240, 270].map((atMs, i) => ({
      atMs,
      y: 280 + i * 30,
    }));
    const estimate = estimateReleaseVelocity([...oldUp, ...terminalDown], P.drag);
    expect(estimate.qualified).toBe(true);
    expect(estimate.scrollVelocityPxPerSec).toBe(-1000);
  });

  it("does not qualify a single-frame stationary jitter spike", () => {
    const samples = [
      { atMs: 0, y: 300 },
      { atMs: 30, y: 300 },
      { atMs: 60, y: 360 },
      { atMs: 90, y: 300 },
      { atMs: 120, y: 300 },
    ];
    expect(estimateReleaseVelocity(samples, P.drag).qualified).toBe(false);
  });

  it("does not qualify a slow, short release", () => {
    const samples = [0, 30, 60, 90, 120].map((atMs, i) => ({
      atMs,
      y: 300 - i * 3,
    }));
    expect(estimateReleaseVelocity(samples, P.drag)).toEqual({
      qualified: false,
      scrollVelocityPxPerSec: 0,
    });
  });
});

describe("click and intent", () => {
  it("quick release on a clickable clicks once at the pinch-close anchor", () => {
    const { ctx, steps } = drive([
      closed({ now: 0, cursorX: 410, cursorY: 305, clickableUnderCursor: true }),
      open({ now: 16, cursorX: 414, cursorY: 307 }),
      open({ now: 32, cursorX: 414, cursorY: 307 }),
      open({ now: 48, cursorX: 414, cursorY: 307 }),
      open({ now: 64, cursorX: 414, cursorY: 307 }),
    ]);
    expect(steps.find(step => step.effect.kind === "click")?.effect).toEqual({
      kind: "click",
      x: 410,
      y: 305,
    });
    expect(clickCount(steps)).toBe(1);
    expect(ctx.phase).toBe("REARM_REQUIRED");
  });

  it("quick release in free space never clicks", () => {
    const { steps } = drive([
      closed({ now: 0 }),
      open({ now: 16 }),
      open({ now: 32 }),
      open({ now: 48 }),
      open({ now: 64 }),
    ]);
    expect(clickCount(steps)).toBe(0);
    expect(steps.at(-1)?.reason).toBe("release_no_click");
  });

  it("movement intent is monotonic even over a clickable", () => {
    const { ctx, steps } = drive([
      closed({ now: 0, cursorY: 300, clickableUnderCursor: true }),
      closed({ now: 16, cursorY: 270, clickableUnderCursor: true }),
      ...Array.from({ length: 4 }, (_, i) =>
        open({ now: 32 + i * 16, cursorY: 270, clickableUnderCursor: true })
      ),
    ]);
    expect(effectKinds(steps)).toContain("dragStart");
    expect(clickCount(steps)).toBe(0);
    expect(["REARM_REQUIRED", "COASTING"]).toContain(ctx.phase);
  });

  it("rearm requires fresh confirmed-open evidence", () => {
    const seed = drive([
      closed({ now: 0 }),
      ...Array.from({ length: P.releaseConfirmFrames }, (_, i) =>
        open({ now: 16 + i * 16 })
      ),
    ]).ctx;
    const held = drive(
      [closed({ now: 100 }), closed({ now: 116 }), closed({ now: 132 })],
      seed
    );
    expect(held.ctx.phase).toBe("REARM_REQUIRED");
    const rearmed = drive(
      Array.from({ length: P.rearmConfirmFrames }, (_, i) =>
        open({ now: 150 + i * 16 })
      ),
      held.ctx
    );
    expect(rearmed.ctx.phase).toBe("IDLE_OPEN");
  });
});

describe("sensor-clock evidence and sampling", () => {
  it("stale open ticks do not advance release confirmation", () => {
    const dragging = drive([
      closed({ now: 0, cursorY: 300 }),
      closed({ now: 20, cursorY: 270 }),
      open({ now: 40, cursorY: 250 }),
    ]);
    expect(dragging.ctx.phase).toBe("DRAG_RELEASE_DEBOUNCE");
    const staleRelease = drive(
      Array.from({ length: 8 }, (_, i) =>
        stale({ now: 50 + i * 16, pinchRatio: 0.7, cursorY: 250 })
      ),
      dragging.ctx
    );
    expect(staleRelease.ctx.phase).toBe("DRAG_RELEASE_DEBOUNCE");
    expect(staleRelease.ctx.openFrames).toBe(1);
    expect(effectKinds(staleRelease.steps)).not.toContain("scrollStop");
  });

  it("stale open ticks do not complete rearm", () => {
    const rearm = {
      ...createGestureContext(),
      phase: "REARM_REQUIRED" as const,
      openFrames: 1,
    };
    const result = drive(
      Array.from({ length: 8 }, (_, i) =>
        stale({ now: i * 16, pinchRatio: 0.7 })
      ),
      rearm
    );
    expect(result.ctx.phase).toBe("REARM_REQUIRED");
    expect(result.ctx.openFrames).toBe(1);
  });

  it("stale no-hand ticks do not consume tracking-loss grace", () => {
    const dragging = drive([
      closed({ now: 0, cursorY: 300 }),
      closed({ now: 20, cursorY: 270 }),
    ]).ctx;
    const result = drive(
      Array.from({ length: P.handLossGraceFrames + 3 }, (_, i) =>
        stale({ now: 40 + i * 16, hasHand: false, pinchRatio: Infinity })
      ),
      dragging
    );
    expect(result.ctx.phase).toBe("DRAGGING");
    expect(result.ctx.missedFrames).toBe(0);
  });

  it("stale flutter between fresh closed observations does not accumulate open evidence", () => {
    const result = drive([
      closed({ now: 0 }),
      ...Array.from({ length: 8 }, (_, i) =>
        stale({ now: 16 + i * 16, pinchRatio: 0.7 })
      ),
      closed({ now: 160 }),
    ]);
    expect(result.ctx.phase).toBe("PRESS_CLICK_ELIGIBLE");
    expect(result.ctx.openFrames).toBe(0);
  });

  it("never inserts stale ticks into drag motionSamples", () => {
    const start = drive([
      closed({ now: 0, cursorY: 300 }),
      closed({ now: 30, cursorY: 270 }),
    ]);
    const initialCount = start.ctx.motionSamples.length;
    const result = drive(
      [
        stale({ now: 40, cursorY: 270, pinchRatio: 0.2 }),
        stale({ now: 50, cursorY: 270, pinchRatio: 0.2 }),
        closed({ now: 60, cursorY: 250 }),
        stale({ now: 70, cursorY: 250, pinchRatio: 0.2 }),
      ],
      start.ctx
    );
    expect(result.ctx.motionSamples.length).toBe(initialCount + 1);
  });
});

describe("direct drag and release debounce", () => {
  it("dragStart carries the anchor and promoting-frame cursorY", () => {
    const { steps } = drive([
      closed({ now: 0, cursorX: 420, cursorY: 330 }),
      closed({ now: 16, cursorX: 420, cursorY: 300 }),
    ]);
    expect(steps.at(-1)?.effect).toEqual({
      kind: "dragStart",
      anchorX: 420,
      anchorY: 330,
      cursorY: 300,
    });
  });

  it("dragTo is exactly deterministic from the visible cursorY", () => {
    const start = drive([
      closed({ now: 0, cursorY: 330 }),
      closed({ now: 16, cursorY: 300 }),
    ]).ctx;
    const { steps } = drive(
      [closed({ now: 32, cursorY: 277 }), closed({ now: 48, cursorY: 277 })],
      start
    );
    expect(steps[0].effect).toEqual({ kind: "dragTo", cursorY: 277 });
    expect(steps[1].effect).toEqual(steps[0].effect);
  });

  it("open flutter keeps dragging; fresh reclose discards the latch without a new start", () => {
    const { ctx, steps } = drive([
      closed({ now: 0, cursorY: 400 }),
      closed({ now: 30, cursorY: 370 }),
      closed({ now: 60, cursorY: 340 }),
      closed({ now: 90, cursorY: 310 }),
      open({ now: 120, cursorY: 280 }),
      open({ now: 150, cursorY: 275 }),
      closed({ now: 180, cursorY: 270 }),
    ]);
    expect(steps[4].effect).toEqual({ kind: "dragTo", cursorY: 280 });
    expect(steps[5].effect).toEqual({ kind: "dragTo", cursorY: 275 });
    expect(steps[6].reason).toBe("drag_reengage");
    expect(ctx.phase).toBe("DRAGGING");
    expect(ctx.releaseVelocity).toBe(0);
    expect(effectKinds(steps).filter(kind => kind === "dragStart")).toHaveLength(1);
  });

  it("slow release stops and rearms without entering coast", () => {
    const { ctx, steps } = drive([
      closed({ now: 0, cursorY: 300 }),
      closed({ now: 400, cursorY: 300 }),
      open({ now: 416, cursorY: 300 }),
      open({ now: 432, cursorY: 300 }),
      open({ now: 448, cursorY: 300 }),
      open({ now: 464, cursorY: 300 }),
    ]);
    expect(ctx.phase).toBe("REARM_REQUIRED");
    expect(steps.at(-1)?.effect.kind).toBe("scrollStop");
    expect(steps.some(step => step.phase === "COASTING")).toBe(false);
  });

  it("qualified flick uses the estimate latched on the first open frame", () => {
    const { ctx, steps } = drive(flickFrames());
    const firstOpen = steps.find(step => step.reason === "drag_release_candidate")!;
    expect(firstOpen.ctx.releaseVelocity).toBe(1000);
    expect(ctx.phase).toBe("COASTING");
    expect(ctx.coastVelocity).toBe(firstOpen.ctx.releaseVelocity);
    expect(ctx.motionSamples.at(-1)?.y).toBe(280);
  });
});

describe("coast math and reducer", () => {
  it("decays velocity and displacement monotonically per equal tick", () => {
    let velocity = 1800;
    const velocities: number[] = [];
    const deltas: number[] = [];
    for (let i = 0; i < 8; i += 1) {
      const step = coastStep(velocity, 16, P.drag.coastTauMs);
      velocity = step.v1;
      velocities.push(Math.abs(step.v1));
      deltas.push(Math.abs(step.deltaPx));
    }
    expect(velocities.every((v, i) => i === 0 || v < velocities[i - 1])).toBe(true);
    expect(deltas.every((v, i) => i === 0 || v < deltas[i - 1])).toBe(true);
  });

  it("is rate independent at 30/60/120Hz and approaches v0*tau", () => {
    const integrate = (dtMs: number) => {
      let v = 2000;
      let total = 0;
      for (let elapsed = 0; elapsed < 10000; elapsed += dtMs) {
        const dt = Math.min(dtMs, 10000 - elapsed);
        const step = coastStep(v, dt, P.drag.coastTauMs);
        total += step.deltaPx;
        v = step.v1;
      }
      return total;
    };
    const totals = [1000 / 30, 1000 / 60, 1000 / 120].map(integrate);
    expect(Math.max(...totals) - Math.min(...totals)).toBeLessThan(1e-8);
    expect(totals[0]).toBeCloseTo(2000 * (P.drag.coastTauMs / 1000), 4);
  });

  it("stops with scrollStop once velocity falls below the threshold", () => {
    const seed: GestureContext = {
      ...createGestureContext(),
      phase: "COASTING",
      coastVelocity: 45,
      coastLastAtMs: 0,
    };
    const result = drive([open({ now: 16 })], seed);
    expect(result.ctx.phase).toBe("REARM_REQUIRED");
    expect(result.steps[0].effect.kind).toBe("scrollStop");
  });

  it("matching blocked direction kills coast with no bounce", () => {
    const seed: GestureContext = {
      ...createGestureContext(),
      phase: "COASTING",
      coastVelocity: 1000,
      coastLastAtMs: 0,
    };
    const result = drive([open({ now: 16, blockedDirection: "end" })], seed);
    expect(result.steps[0].effect.kind).toBe("scrollStop");
    expect(result.ctx.coastVelocity).toBe(0);
    expect(effectKinds(result.steps)).not.toContain("coastBy");
  });

  it("fresh missing-hand observations never stop an active coast", () => {
    const seed: GestureContext = {
      ...createGestureContext(),
      phase: "COASTING",
      coastVelocity: 2000,
      coastLastAtMs: 0,
    };
    const result = drive(
      Array.from({ length: P.handLossGraceFrames + 3 }, (_, i) =>
        gone({ now: (i + 1) * 16 })
      ),
      seed
    );
    expect(result.ctx.phase).toBe("COASTING");
    expect(result.ctx.missedFrames).toBeGreaterThanOrEqual(P.handLossGraceFrames);
    expect(result.steps.every(step => step.effect.kind === "coastBy")).toBe(true);
  });
});

describe("re-grab, identity, cleanup, and click exclusion", () => {
  it("fresh close during coast re-grabs directly with zero click and zero coast", () => {
    const coast = drive(flickFrames());
    const result = drive(
      [closed({ now: 226, cursorY: 280, clickableUnderCursor: true })],
      coast.ctx
    );
    expect(result.ctx.phase).toBe("DRAGGING");
    expect(result.steps[0].effect).toEqual({ kind: "dragRegrab", cursorY: 280 });
    expect(result.ctx.coastVelocity).toBe(0);
    expect(result.steps.some(step => step.phase === "PRESS_CLICK_ELIGIBLE")).toBe(false);
    expect(clickCount([...coast.steps, ...result.steps])).toBe(0);
  });

  it("keeps one container identity across drag, debounce, coast, re-grab, and release", () => {
    const sequence = [
      ...flickFrames(),
      closed({ now: 226, cursorY: 280 }),
      closed({ now: 256, cursorY: 280 }),
      open({ now: 286, cursorY: 280 }),
      open({ now: 316, cursorY: 280 }),
      open({ now: 346, cursorY: 280 }),
      open({ now: 376, cursorY: 280 }),
    ];
    const { steps } = drive(sequence);
    expect(effectKinds(steps).filter(kind => kind === "dragStart")).toHaveLength(1);
    expect(effectKinds(steps).filter(kind => kind === "dragRegrab")).toHaveLength(1);
    expect(effectKinds(steps).filter(kind => kind === "scrollStop")).toHaveLength(1);
  });

  it.each([
    ["coast decay", { phase: "COASTING" as const, coastVelocity: 45, coastLastAtMs: 0 }, open({ now: 16 })],
    ["coast boundary", { phase: "COASTING" as const, coastVelocity: 1000, coastLastAtMs: 0 }, open({ now: 16, blockedDirection: "end" })],
    ["invalid target", { phase: "DRAGGING" as const }, closed({ now: 16, scrollTargetValid: false })],
  ])("cleanup exit %s emits scrollStop exactly once", (_name, patch, input) => {
    const seed = { ...createGestureContext(), ...patch };
    const result = drive([input], seed);
    expect(effectKinds(result.steps).filter(kind => kind === "scrollStop")).toHaveLength(1);
  });

  it("sustained drag tracking loss emits scrollStop exactly once", () => {
    const start = drive([
      closed({ now: 0, cursorY: 300 }),
      closed({ now: 30, cursorY: 270 }),
    ]).ctx;
    const result = drive(
      Array.from({ length: P.handLossGraceFrames + 4 }, (_, i) =>
        gone({ now: 50 + i * 16 })
      ),
      start
    );
    expect(effectKinds(result.steps).filter(kind => kind === "scrollStop")).toHaveLength(1);
  });

  it("drag, coast, and coast re-grab over clickables never synthesize click", () => {
    const coast = drive(flickFrames(true));
    const regrab = drive(
      [
        closed({ now: 226, cursorY: 280, clickableUnderCursor: true }),
        closed({ now: 256, cursorY: 250, clickableUnderCursor: true }),
        open({ now: 286, cursorY: 240, clickableUnderCursor: true }),
        open({ now: 316, cursorY: 240, clickableUnderCursor: true }),
        open({ now: 346, cursorY: 240, clickableUnderCursor: true }),
        open({ now: 376, cursorY: 240, clickableUnderCursor: true }),
      ],
      coast.ctx
    );
    expect(clickCount([...coast.steps, ...regrab.steps])).toBe(0);
  });
});

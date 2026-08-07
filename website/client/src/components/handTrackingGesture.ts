/**
 * Pure hand-tracking mechanics for HandTrackingProvider.
 *
 * Camera observations own evidence and cursor sampling; duplicate RAF ticks may
 * advance only render-clock coast decay. The visible avatar is mapped through a
 * deterministic comfort box, filtered once in screen pixels, and used for every
 * draw, hit-test, drag, and velocity decision. Pinch motion directly drags one
 * locked scroll container; a qualified release can continue as inertial coast.
 */

// --- Landmark math --------------------------------------------------------

export interface Landmark {
 x: number;
 y: number;
 z: number;
}

export function computePinchRatio(landmarks: Landmark[]): number {
 const thumb = landmarks[4];
 const index = landmarks[8];
 const p0 = landmarks[0];
 const p5 = landmarks[5];
 const p9 = landmarks[9];
 const p17 = landmarks[17];
 if (!thumb || !index || !p0 || !p5 || !p9 || !p17) {
 return Number.POSITIVE_INFINITY;
 }

 const dx = thumb.x - index.x;
 const dy = thumb.y - index.y;
 const dz = (thumb.z - index.z) * 0.5;
 const fingertipDistance = Math.sqrt(dx * dx + dy * dy + dz * dz);
 const palmWidth = Math.hypot(
 p5.x - p17.x,
 p5.y - p17.y,
 (p5.z - p17.z) * 0.5
 );
 const palmLength = Math.hypot(p0.x - p9.x, p0.y - p9.y, (p0.z - p9.z) * 0.5);
 return fingertipDistance / Math.max(palmWidth, palmLength, 1e-6);
}

// --- Cursor mapping + filtering ------------------------------------------

export interface ComfortBox {
 xMin: number;
 xMax: number;
 yMin: number;
 yMax: number;
 overscan: number;
}

export const DEFAULT_COMFORT_BOX: ComfortBox = {
 xMin: 0.22,
 xMax: 0.78,
 yMin: 0.26,
 yMax: 0.82,
 overscan: 0.03,
};

export function mapComfortBox(
 nx: number,
 ny: number,
 box: ComfortBox
): { x: number; y: number } {
 const mapAxis = (value: number, min: number, max: number) => {
 const t = (value - min) / Math.max(max - min, Number.EPSILON);
 const mapped = -box.overscan + t * (1 + box.overscan * 2);
 return Math.max(0, Math.min(1, mapped));
 };
 return {
 x: mapAxis(nx, box.xMin, box.xMax),
 y: mapAxis(ny, box.yMin, box.yMax),
 };
}

export class OneEuroFilter {
 private previousValue: number | null = null;
 private previousFiltered: number | null = null;
 private previousDerivative = 0;
 private previousTimestampMs: number | null = null;

 constructor(
 private readonly minCutoff = 1.2,
 private readonly beta = 0.006,
 private readonly dCutoff = 1.0
 ) {}

 filter(value: number, timestampMs: number): number {
 if (
 this.previousValue === null ||
 this.previousFiltered === null ||
 this.previousTimestampMs === null
 ) {
 this.previousValue = value;
 this.previousFiltered = value;
 this.previousDerivative = 0;
 this.previousTimestampMs = timestampMs;
 return value;
 }

 const dt = (timestampMs - this.previousTimestampMs) / 1000;
 if (dt <= 0) return this.previousFiltered;

 const derivative = (value - this.previousValue) / dt;
 const derivativeAlpha = OneEuroFilter.alpha(this.dCutoff, dt);
 const filteredDerivative =
 this.previousDerivative +
 derivativeAlpha * (derivative - this.previousDerivative);
 const cutoff = this.minCutoff + this.beta * Math.abs(filteredDerivative);
 const valueAlpha = OneEuroFilter.alpha(cutoff, dt);
 const filtered =
 this.previousFiltered + valueAlpha * (value - this.previousFiltered);

 this.previousValue = value;
 this.previousFiltered = filtered;
 this.previousDerivative = filteredDerivative;
 this.previousTimestampMs = timestampMs;
 return filtered;
 }

 reset(): void {
 this.previousValue = null;
 this.previousFiltered = null;
 this.previousDerivative = 0;
 this.previousTimestampMs = null;
 }

 private static alpha(cutoff: number, dt: number): number {
 const safeCutoff = Math.max(cutoff, Number.EPSILON);
 const tau = 1 / (2 * Math.PI * safeCutoff);
 return 1 / (1 + tau / dt);
 }
}

// --- Scroll-container classification -------------------------------------

export function isScrollableStyle(
 overflowY: string,
 scrollHeight: number,
 clientHeight: number
): boolean {
 const scrolls =
 overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay";
 return scrolls && scrollHeight > clientHeight + 1;
}

// --- Drag release + coast math -------------------------------------------

export interface DragParams {
 gain: number;
 velocityWindowMs: number;
 velocityMaxSamples: number;
 velocityMinSamples: number;
 velocityMinSpanMs: number;
 flickThresholdPxPerSec: number;
 minReleaseDisplacementPx: number;
 directionAgreementMin: number;
 teleportRejectPxPerSec: number;
 madGateFloorPxPerSec: number;
 maxCoastVelocityPxPerSec: number;
 coastTauMs: number;
 coastStopPxPerSec: number;
 coastDtCapMs: number;
}

export interface MotionSample {
 atMs: number;
 y: number;
}

function median(values: number[]): number {
 const sorted = [...values].sort((a, b) => a - b);
 const middle = Math.floor(sorted.length / 2);
 return sorted.length % 2 === 0
 ? (sorted[middle - 1] + sorted[middle]) / 2
 : sorted[middle];
}

function terminalSamples(samples: MotionSample[], p: DragParams): MotionSample[] {
 if (samples.length === 0) return [];
 const newestAt = samples[samples.length - 1].atMs;
 return samples
 .filter(sample => newestAt - sample.atMs <= p.velocityWindowMs)
 .slice(-p.velocityMaxSamples);
}

export function estimateReleaseVelocity(
 samples: MotionSample[],
 p: DragParams
): { qualified: boolean; scrollVelocityPxPerSec: number } {
 const windowed = terminalSamples(samples, p);
 if (
 windowed.length < p.velocityMinSamples ||
 windowed.at(-1)!.atMs - windowed[0].atMs < p.velocityMinSpanMs
 ) {
 return { qualified: false, scrollVelocityPxPerSec: 0 };
 }

 const velocities: number[] = [];
 for (let i = 1; i < windowed.length; i += 1) {
 const dtMs = windowed[i].atMs - windowed[i - 1].atMs;
 if (dtMs < 8 || dtMs > 50) continue;
 const velocity = ((windowed[i].y - windowed[i - 1].y) / dtMs) * 1000;
 if (Math.abs(velocity) <= p.teleportRejectPxPerSec) velocities.push(velocity);
 }
 if (velocities.length === 0) {
 return { qualified: false, scrollVelocityPxPerSec: 0 };
 }

 const initialMedian = median(velocities);
 const mad = median(velocities.map(v => Math.abs(v - initialMedian)));
 const gate = Math.max(3 * mad, p.madGateFloorPxPerSec);
 const survivors = velocities.filter(v => Math.abs(v - initialMedian) <= gate);
 if (survivors.length === 0) {
 return { qualified: false, scrollVelocityPxPerSec: 0 };
 }

 const handVelocity = median(survivors);
 const sign = Math.sign(handVelocity);
 const agreeing = survivors.filter(v => sign !== 0 && Math.sign(v) === sign).length;
 const agreement = agreeing / survivors.length;
 const displacement = Math.abs(windowed.at(-1)!.y - windowed[0].y);
 const uncapped = -handVelocity * p.gain;
 const scrollVelocityPxPerSec = Math.max(
 -p.maxCoastVelocityPxPerSec,
 Math.min(p.maxCoastVelocityPxPerSec, uncapped)
 );
 const qualified =
 displacement >= p.minReleaseDisplacementPx &&
 agreement >= p.directionAgreementMin &&
 Math.abs(scrollVelocityPxPerSec) >= p.flickThresholdPxPerSec;
 return { qualified, scrollVelocityPxPerSec: qualified ? scrollVelocityPxPerSec : 0 };
}

export function coastStep(
 v0: number,
 dtMs: number,
 tauMs: number
): { deltaPx: number; v1: number } {
 const dt = Math.max(0, dtMs);
 const tau = Math.max(tauMs, Number.EPSILON);
 const decay = Math.exp(-dt / tau);
 return {
 deltaPx: v0 * (tau / 1000) * (1 - decay),
 v1: v0 * decay,
 };
}

// --- Gesture FSM ----------------------------------------------------------

export type GesturePhase =
 | "IDLE_OPEN"
 | "PRESS_CLICK_ELIGIBLE"
 | "DRAGGING"
 | "DRAG_RELEASE_DEBOUNCE"
 | "COASTING"
 | "REARM_REQUIRED";

export interface GestureParams {
 pinchCloseRatio: number;
 pinchOpenRatio: number;
 releaseConfirmFrames: number;
 rearmConfirmFrames: number;
 scrollHoldDelayMs: number;
 scrollIntentThresholdPx: number;
 handLossGraceFrames: number;
 clickCooldownMs: number;
 drag: DragParams;
}

export const DEFAULT_PARAMS: GestureParams = {
 pinchCloseRatio: 0.42,
 pinchOpenRatio: 0.5,
 releaseConfirmFrames: 4,
 rearmConfirmFrames: 3,
 scrollHoldDelayMs: 350,
 scrollIntentThresholdPx: 22,
 handLossGraceFrames: 15,
 clickCooldownMs: 250,
 drag: {
 gain: 1.0,
 velocityWindowMs: 120,
 velocityMaxSamples: 8,
 velocityMinSamples: 3,
 velocityMinSpanMs: 50,
 flickThresholdPxPerSec: 650,
 minReleaseDisplacementPx: 24,
 directionAgreementMin: 0.75,
 teleportRejectPxPerSec: 5000,
 madGateFloorPxPerSec: 300,
 maxCoastVelocityPxPerSec: 3200,
 coastTauMs: 550,
 coastStopPxPerSec: 45,
 coastDtCapMs: 50,
 },
};

export interface GestureContext {
 phase: GesturePhase;
 anchorX: number;
 anchorY: number;
 closeAtMs: number;
 startedOnClickable: boolean;
 openFrames: number;
 missedFrames: number;
 lastClickAtMs: number;
 lastReason: string;
 motionSamples: MotionSample[];
 releaseVelocity: number;
 coastVelocity: number;
 coastLastAtMs: number;
}

export function createGestureContext(): GestureContext {
 return {
 phase: "IDLE_OPEN",
 anchorX: 0,
 anchorY: 0,
 closeAtMs: 0,
 startedOnClickable: false,
 openFrames: 0,
 missedFrames: 0,
 lastClickAtMs: Number.NEGATIVE_INFINITY,
 lastReason: "init",
 motionSamples: [],
 releaseVelocity: 0,
 coastVelocity: 0,
 coastLastAtMs: 0,
 };
}

export interface GestureInput {
 now: number;
 freshObservation: boolean;
 observationAtMs: number;
 hasHand: boolean;
 pinchRatio: number;
 cursorX: number;
 cursorY: number;
 clickableUnderCursor: boolean;
 blockedDirection: "start" | "end" | null;
 scrollTargetValid: boolean;
}

export type GestureEffect =
 | { kind: "none" }
 | { kind: "click"; x: number; y: number }
 | { kind: "dragStart"; anchorX: number; anchorY: number; cursorY: number }
 | { kind: "dragTo"; cursorY: number }
 | { kind: "coastBy"; deltaPx: number; velocity: number }
 | { kind: "dragRegrab"; cursorY: number }
 | { kind: "scrollStop" };

export interface GestureResult {
 ctx: GestureContext;
 effect: GestureEffect;
 reason: string;
}

function appendMotionSample(
 samples: MotionSample[],
 sample: MotionSample,
 p: DragParams
): MotionSample[] {
 return terminalSamples([...samples, sample], p);
}

export function reduceGesture(
 prev: GestureContext,
 input: GestureInput,
 P: GestureParams
): GestureResult {
 const {
 now,
 freshObservation,
 observationAtMs,
 hasHand,
 pinchRatio,
 cursorX,
 cursorY,
 clickableUnderCursor,
 blockedDirection,
 scrollTargetValid,
 } = input;
 const openHand = hasHand && pinchRatio > P.pinchOpenRatio;
 const closedHand = hasHand && pinchRatio < P.pinchCloseRatio;
 const missedFrames = freshObservation
 ? hasHand
 ? 0
 : prev.missedFrames + 1
 : prev.missedFrames;
 const openFrames = freshObservation
 ? openHand
 ? prev.openFrames + 1
 : 0
 : prev.openFrames;
 const ctx: GestureContext = { ...prev, missedFrames, openFrames };
 const done = (
 phase: GesturePhase,
 effect: GestureEffect,
 reason: string,
 extra?: Partial<GestureContext>
 ): GestureResult => {
 const nextCtx = { ...ctx, ...extra, phase, lastReason: reason };
 return { ctx: nextCtx, effect, reason };
 };

 const hasLockedTarget =
 prev.phase === "DRAGGING" ||
 prev.phase === "DRAG_RELEASE_DEBOUNCE" ||
 prev.phase === "COASTING";
 if (hasLockedTarget && !scrollTargetValid) {
 return done("REARM_REQUIRED", { kind: "scrollStop" }, "scroll_target_lost", {
 coastVelocity: 0,
 releaseVelocity: 0,
 });
 }

 if (
 prev.phase !== "COASTING" &&
 missedFrames >= P.handLossGraceFrames
 ) {
 if (prev.phase === "IDLE_OPEN" || prev.phase === "REARM_REQUIRED") {
 return done(prev.phase, { kind: "none" }, "idle_no_hand");
 }
 const wasDragging =
 prev.phase === "DRAGGING" || prev.phase === "DRAG_RELEASE_DEBOUNCE";
 return done(
 "REARM_REQUIRED",
 wasDragging ? { kind: "scrollStop" } : { kind: "none" },
 "tracking_lost",
 { releaseVelocity: 0, coastVelocity: 0 }
 );
 }

 switch (prev.phase) {
 case "IDLE_OPEN":
 if (closedHand) {
 return done("PRESS_CLICK_ELIGIBLE", { kind: "none" }, "pinch_close", {
 anchorX: cursorX,
 anchorY: cursorY,
 closeAtMs: now,
 startedOnClickable: clickableUnderCursor,
 motionSamples: freshObservation
 ? [{ atMs: observationAtMs, y: cursorY }]
 : [],
 releaseVelocity: 0,
 coastVelocity: 0,
 });
 }
 return done("IDLE_OPEN", { kind: "none" }, "idle");

 case "PRESS_CLICK_ELIGIBLE": {
 const moved = Math.hypot(cursorX - prev.anchorX, cursorY - prev.anchorY);
 const elapsed = now - prev.closeAtMs;
 if (openFrames >= P.releaseConfirmFrames) {
 const canClick =
 prev.startedOnClickable &&
 moved < P.scrollIntentThresholdPx &&
 elapsed < P.scrollHoldDelayMs &&
 now - prev.lastClickAtMs >= P.clickCooldownMs;
 if (canClick) {
 return done(
 "REARM_REQUIRED",
 { kind: "click", x: prev.anchorX, y: prev.anchorY },
 "click",
 { lastClickAtMs: now, motionSamples: [] }
 );
 }
 return done("REARM_REQUIRED", { kind: "none" }, "release_no_click", {
 motionSamples: [],
 });
 }
 if (moved >= P.scrollIntentThresholdPx || elapsed >= P.scrollHoldDelayMs) {
 const samples = freshObservation && hasHand
 ? appendMotionSample(
 prev.motionSamples,
 { atMs: observationAtMs, y: cursorY },
 P.drag
 )
 : prev.motionSamples;
 return done(
 "DRAGGING",
 {
 kind: "dragStart",
 anchorX: prev.anchorX,
 anchorY: prev.anchorY,
 cursorY,
 },
 moved >= P.scrollIntentThresholdPx ? "drag_intent_move" : "drag_intent_hold",
 { motionSamples: samples }
 );
 }
 return done("PRESS_CLICK_ELIGIBLE", { kind: "none" }, "press_hold");
 }

 case "DRAGGING": {
 const samples = freshObservation && hasHand
 ? appendMotionSample(
 prev.motionSamples,
 { atMs: observationAtMs, y: cursorY },
 P.drag
 )
 : prev.motionSamples;
 if (freshObservation && openHand) {
 const estimate = estimateReleaseVelocity(samples, P.drag);
 return done(
 "DRAG_RELEASE_DEBOUNCE",
 { kind: "dragTo", cursorY },
 "drag_release_candidate",
 {
 motionSamples: samples,
 releaseVelocity: estimate.qualified
 ? estimate.scrollVelocityPxPerSec
 : 0,
 }
 );
 }
 return done(
 "DRAGGING",
 { kind: "dragTo", cursorY },
 hasHand ? "dragging" : "drag_hand_gap",
 { motionSamples: samples }
 );
 }

 case "DRAG_RELEASE_DEBOUNCE": {
 const samples = freshObservation && hasHand
 ? appendMotionSample(
 prev.motionSamples,
 { atMs: observationAtMs, y: cursorY },
 P.drag
 )
 : prev.motionSamples;
 if (freshObservation && closedHand) {
 return done("DRAGGING", { kind: "dragTo", cursorY }, "drag_reengage", {
 motionSamples: samples,
 releaseVelocity: 0,
 });
 }
 if (openFrames >= P.releaseConfirmFrames) {
 if (prev.releaseVelocity !== 0) {
 return done("COASTING", { kind: "none" }, "flick_release", {
 motionSamples: samples,
 coastVelocity: prev.releaseVelocity,
 coastLastAtMs: now,
 });
 }
 return done("REARM_REQUIRED", { kind: "scrollStop" }, "slow_release", {
 motionSamples: [],
 releaseVelocity: 0,
 coastVelocity: 0,
 });
 }
 return done(
 "DRAG_RELEASE_DEBOUNCE",
 { kind: "dragTo", cursorY },
 "drag_release_debounce",
 { motionSamples: samples }
 );
 }

 case "COASTING": {
 if (freshObservation && closedHand) {
 return done(
 "DRAGGING",
 { kind: "dragRegrab", cursorY },
 "coast_regrab",
 {
 motionSamples: [{ atMs: observationAtMs, y: cursorY }],
 releaseVelocity: 0,
 coastVelocity: 0,
 }
 );
 }
 const blockedInDirection =
 (prev.coastVelocity > 0 && blockedDirection === "end") ||
 (prev.coastVelocity < 0 && blockedDirection === "start");
 if (blockedInDirection) {
 return done("REARM_REQUIRED", { kind: "scrollStop" }, "coast_boundary", {
 coastVelocity: 0,
 releaseVelocity: 0,
 });
 }
 const dtMs = Math.min(
 P.drag.coastDtCapMs,
 Math.max(0, now - prev.coastLastAtMs)
 );
 const step = coastStep(prev.coastVelocity, dtMs, P.drag.coastTauMs);
 if (Math.abs(step.v1) < P.drag.coastStopPxPerSec) {
 return done("REARM_REQUIRED", { kind: "scrollStop" }, "coast_stopped", {
 coastVelocity: 0,
 releaseVelocity: 0,
 });
 }
 return done(
 "COASTING",
 { kind: "coastBy", deltaPx: step.deltaPx, velocity: step.v1 },
 hasHand ? "coasting" : "coasting_no_hand",
 { coastVelocity: step.v1, coastLastAtMs: now }
 );
 }

 case "REARM_REQUIRED":
 if (freshObservation && openFrames >= P.rearmConfirmFrames) {
 return done("IDLE_OPEN", { kind: "none" }, "rearmed", {
 motionSamples: [],
 releaseVelocity: 0,
 coastVelocity: 0,
 });
 }
 return done("REARM_REQUIRED", { kind: "none" }, "awaiting_open");

 default:
 return done("IDLE_OPEN", { kind: "none" }, "reset");
 }
}

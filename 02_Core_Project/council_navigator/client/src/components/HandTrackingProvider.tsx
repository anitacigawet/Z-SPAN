/**
 * HandTrackingProvider — V1-HandTracking-1 core.
 *
 * Browser-side hand tracking via MediaPipe HandLandmarker (Oculus-style
 * pointer interaction). 21-landmark inference runs locally in
 * WebAssembly + WebGPU; the model downloads once from Google's
 * storage.googleapis.com and after that NO camera frame leaves the
 * device. See the locked ⓘ disclosure copy in HandTrackingToggle.tsx
 * for the user-facing privacy framing.
 *
 * The Provider exposes a context (enabled / pointerMode / status /
 * setEnabled / setPointerMode) so the toggle UI is decoupled from the
 * inference engine. When enabled flips on, the Provider lazy-loads the
 * MediaPipe SDK + the hand_landmarker.task model + requests the webcam,
 * then renders a full-viewport canvas overlay (pointer-events:none) on top of
 * the app. One amber avatar is the complete visual vocabulary: dot mode maps
 * the palm through a comfort box; laser mode projects a palm-axis ray and adds
 * its beam. Both use a One-Euro filter on the one screen coordinate shared by
 * drawing, hit-testing, direct dragging, and release-velocity measurement.
 *
 * Click synthesis uses document.elementFromPoint() + dispatchEvent(new
 * MouseEvent('click', ...)) so existing UI components (buttons, links,
 * accordions, citation chips, etc.) work without any modification.
 *
 * Pinch-and-move directly drags the container resolved at drag promotion. A
 * qualified release coasts with closed-form exponential decay; pinching during
 * coast re-grabs the same container without passing through click eligibility.
 *
 * Architecture notes:
 *   - MediaPipe + webcam are torn down on disable to free GPU + camera
 *     light (not just paused — the user expects "off means off").
 *   - The preview fades after first acquisition, but its video remains mounted
 *     and playing as MediaPipe's inference source for the full session.
 *   - The webcam stream is mirrored (flipX) so the user's pointer
 *     follows their natural hand motion rather than a flipped mirror.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type {
  HandLandmarker,
  HandLandmarkerResult,
} from "@mediapipe/tasks-vision";
import {
  computePinchRatio,
  reduceGesture,
  createGestureContext,
  isScrollableStyle,
  mapComfortBox,
  OneEuroFilter,
  DEFAULT_COMFORT_BOX,
  DEFAULT_PARAMS,
  type GestureContext,
  type GestureEffect,
} from "./handTrackingGesture";

// --- Public types ---------------------------------------------------------

export type PointerMode = "dot" | "laser";

export type HandTrackingStatus =
  | "off"
  | "loading-model"
  | "requesting-camera"
  | "active"
  | "error";

interface HandTrackingContextValue {
  enabled: boolean;
  pointerMode: PointerMode;
  status: HandTrackingStatus;
  errorMessage: string | null;
  setEnabled: (v: boolean) => void;
  setPointerMode: (m: PointerMode) => void;
}

const HandTrackingContext = createContext<HandTrackingContextValue | null>(
  null
);

export function useHandTracking(): HandTrackingContextValue {
  const ctx = useContext(HandTrackingContext);
  if (!ctx) {
    throw new Error(
      "useHandTracking must be called inside <HandTrackingProvider>"
    );
  }
  return ctx;
}

// --- Constants ------------------------------------------------------------

const STORAGE_KEY = "zspan_hand_tracking_v1";

// Pinch, drag, release, and coast mechanics live in the pure gesture core.

// Positional cursor gain: expand hand movement around the viewport center so
// the user reaches screen edges without pushing their hand to the camera-frame
// edges. Positional, not velocity — slow aiming stays predictable.
const CURSOR_POSITION_GAIN = 1.15;

interface TrackingLandmark {
  x: number;
  y: number;
  z: number;
}

interface CachedObservation {
  landmarks: TrackingLandmark[] | null;
  cursor: { x: number; y: number };
  palmPosition: { x: number; y: number } | null;
  indexTipZ: number;
  pinchRatio: number;
  observationAtMs: number;
}

const EMPTY_OBSERVATION: CachedObservation = {
  landmarks: null,
  cursor: { x: 0, y: 0 },
  palmPosition: null,
  indexTipZ: 0,
  pinchRatio: Number.POSITIVE_INFINITY,
  observationAtMs: 0,
};

// --- Diagnostic HUD snapshot ---------------------------------------------
interface HudSnapshot {
  phase: string;
  reason: string;
  pinchRatio: number;
  hasHand: boolean;
  openFrames: number;
  missedFrames: number;
  cursorX: number;
  cursorY: number;
  anchorY: number;
  pressDelta: number;
  lastClickDelta: number;
  observationRate: number;
  rafRate: number;
  releaseVelocity: number;
  coastVelocity: number;
  sampleCount: number;
  reacquired: boolean;
  requestedTop: number;
  actualTop: number;
  scrollMax: number;
  atBoundary: boolean;
  container: string;
  clickable: boolean;
}
const EMPTY_HUD: HudSnapshot = {
  phase: "IDLE_OPEN",
  reason: "init",
  pinchRatio: 0,
  hasHand: false,
  openFrames: 0,
  missedFrames: 0,
  cursorX: 0,
  cursorY: 0,
  anchorY: 0,
  pressDelta: 0,
  lastClickDelta: 0,
  observationRate: 0,
  rafRate: 0,
  releaseVelocity: 0,
  coastVelocity: 0,
  sampleCount: 0,
  reacquired: false,
  requestedTop: 0,
  actualTop: 0,
  scrollMax: 0,
  atBoundary: false,
  container: "-",
  clickable: false,
};

// CDN paths. WASM bundle comes from npm via jsdelivr (the canonical
// MediaPipe recipe); the trained model comes from Google's
// storage.googleapis.com — that's what the disclosure copy is referring
// to with "downloads once from Google."
//
// ⚠️ WASM_BASE version is HARDCODED to match the installed
// @mediapipe/tasks-vision npm version. When `pnpm update` bumps the
// npm package, also bump the @x.x.xx string in this URL to match — the
// WASM ABI is not guaranteed compatible across minor versions. Future
// follow-up: derive from package.json at build time so this stays in
// sync automatically.
const WASM_BASE =
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/wasm";
const MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task";

// --- Persistence helpers --------------------------------------------------

interface StoredPrefs {
  pointerMode?: PointerMode;
}

function loadPrefs(): StoredPrefs {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null ? parsed : {};
  } catch {
    return {};
  }
}

function savePrefs(prefs: StoredPrefs): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    /* ignore quota / privacy-mode failures */
  }
}

// --- Provider -------------------------------------------------------------

export function HandTrackingProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabledState] = useState(false);
  const [pointerMode, setPointerModeState] = useState<PointerMode>(() => {
    const prefs = loadPrefs();
    return prefs.pointerMode === "laser" ? "laser" : "dot";
  });
  const [status, setStatus] = useState<HandTrackingStatus>("off");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const setEnabled = useCallback((v: boolean) => {
    setEnabledState(v);
    if (!v) {
      setStatus("off");
      setErrorMessage(null);
    }
  }, []);

  const setPointerMode = useCallback((m: PointerMode) => {
    setPointerModeState(m);
    savePrefs({ pointerMode: m });
  }, []);

  return (
    <HandTrackingContext.Provider
      value={{
        enabled,
        pointerMode,
        status,
        errorMessage,
        setEnabled,
        setPointerMode,
      }}
    >
      {children}
      {enabled && (
        <HandTrackingOverlay
          pointerMode={pointerMode}
          onStatusChange={setStatus}
          onError={msg => {
            setErrorMessage(msg);
            setStatus("error");
          }}
        />
      )}
    </HandTrackingContext.Provider>
  );
}

// --- Overlay (runs only when enabled) -------------------------------------

function HandTrackingOverlay({
  pointerMode,
  onStatusChange,
  onError,
}: {
  pointerMode: PointerMode;
  onStatusChange: (s: HandTrackingStatus) => void;
  onError: (msg: string) => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const landmarkerRef = useRef<HandLandmarker | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const rafRef = useRef<number | null>(null);
  const [previewAcquired, setPreviewAcquired] = useState(false);
  // Gesture state lives in one explicit FSM context (handTrackingGesture.ts),
  // replacing the prior tangle of pinched / pinchDrag / release / suppress refs
  // that formed an implicit, untraceable state machine — the reason 10+ tuning
  // passes never converged (see the 2026-07-12 think-tank synthesis).
  const gestureCtxRef = useRef<GestureContext>(createGestureContext());
  // The scroll container is resolved + LOCKED at drag promotion and never
  // switched mid-gesture. Null unless dragging/coasting. `el` is the scroller — a nested
  // overflow-y-auto column, or document.scrollingElement for the window. (The
  // old code always drove window.scrollTo(), which moved nothing on pages whose
  // shell is `h-screen overflow-hidden`, e.g. BroadcastPage — the dominant
  // "scroll is non-functional" cause the think-tank verified.)
  // savedBehavior stashes the container's scroll-behavior so we can restore it
  // on release (App.css sets html{scroll-behavior:smooth}, which would animate-
  // fight our per-frame scrollTop writes — scroll-behavior isn't inherited, so
  // overriding the resolved container covers both window + nested cases).
  const scrollTargetRef = useRef<{
    el: HTMLElement;
    min: number;
    savedBehavior: string;
    grabCursorY: number;
    grabScrollTop: number;
  } | null>(null);
  const blockedDirectionRef = useRef<"start" | "end" | null>(null);
  // Sensor-clock state. MediaPipe and every camera-derived calculation advance
  // only when video.currentTime advances; render ticks reuse this observation.
  const lastProcessedVideoTimeRef = useRef<number | null>(null);
  const observationRef = useRef<CachedObservation>({ ...EMPTY_OBSERVATION });
  const cursorFiltersRef = useRef({
    dot: { x: new OneEuroFilter(), y: new OneEuroFilter() },
    laser: { x: new OneEuroFilter(), y: new OneEuroFilter() },
  });
  const lastHandObservationAtRef = useRef<number | null>(null);
  const consecutiveNoHandRef = useRef(0);
  const hadHandRef = useRef(false);
  const filtersAwaitingReacquireRef = useRef(false);
  const reacquiredUntilRef = useRef(0);
  const lastClickDeltaRef = useRef(0);
  const observationTimesRef = useRef<number[]>([]);
  const rafTimesRef = useRef<number[]>([]);
  // Diagnostic HUD store — mutated every frame, sampled into React state at
  // ~10Hz (never a re-render per RAF). Shown when the URL has ?hthud. Highest-
  // leverage debugging surface: turns the operator's next live test into an
  // observation instead of another blind threshold guess.
  const hudRef = useRef<HudSnapshot>({ ...EMPTY_HUD });
  const hudTrailRef = useRef<Array<{ reason: string; at: number }>>([]);
  const [hudSnap, setHudSnap] = useState<HudSnapshot | null>(null);
  const hudEnabled =
    typeof window !== "undefined" &&
    new URLSearchParams(window.location.search).has("hthud");
  // Mode is read by the RAF callback. useRef instead of useState avoids
  // re-running the entire init effect when the user toggles dot↔laser.
  const pointerModeRef = useRef<PointerMode>(pointerMode);
  useEffect(() => {
    pointerModeRef.current = pointerMode;
  }, [pointerMode]);

  // Sample the per-frame HUD store into React state at ~10Hz (never per RAF, so
  // the overlay's inference loop is never taxed by re-renders).
  useEffect(() => {
    if (!hudEnabled) return;
    const id = window.setInterval(() => {
      setHudSnap({ ...hudRef.current });
    }, 100);
    return () => window.clearInterval(id);
  }, [hudEnabled]);

  useEffect(() => {
    let cancelled = false;

    // Execute exactly one reducer-authored effect per frame. All applications
    // use live DOM bounds; dragging rebases at a clamp so reversal has no hidden
    // overshoot debt, while coast reports a boundary into the next reducer tick.
    function applyGestureEffect(
      effect: GestureEffect,
      ctx2d: CanvasRenderingContext2D,
      cur: { x: number; y: number }
    ): {
      requestedTop: number;
      actualTop: number;
      atBoundary: boolean;
    } | null {
      const applyDragTo = (cursorY: number) => {
        const tgt = scrollTargetRef.current;
        if (!tgt) return null;
        const liveMax = Math.max(0, tgt.el.scrollHeight - tgt.el.clientHeight);
        const requested =
          tgt.grabScrollTop +
          (tgt.grabCursorY - cursorY) * DEFAULT_PARAMS.drag.gain;
        const clamped = Math.max(tgt.min, Math.min(liveMax, requested));
        tgt.el.scrollTop = clamped;
        if (clamped !== requested) {
          tgt.grabScrollTop = clamped;
          tgt.grabCursorY = cursorY;
        }
        return {
          requestedTop: requested,
          actualTop: tgt.el.scrollTop,
          atBoundary: clamped <= tgt.min + 0.5 || clamped >= liveMax - 0.5,
        };
      };

      switch (effect.kind) {
        case "click": {
          synthesizeClick(effect.x, effect.y);
          lastClickDeltaRef.current = Math.hypot(
            cur.x - effect.x,
            cur.y - effect.y
          );
          ctx2d.strokeStyle = "rgba(245, 165, 36, 0.9)";
          ctx2d.lineWidth = 3;
          ctx2d.beginPath();
          ctx2d.arc(effect.x, effect.y, 18, 0, Math.PI * 2);
          ctx2d.stroke();
          return null;
        }
        case "dragStart": {
          const t = resolveScrollContainer(effect.anchorX, effect.anchorY);
          const savedBehavior = t.el.style.scrollBehavior;
          t.el.style.scrollBehavior = "auto";
          scrollTargetRef.current = {
            el: t.el,
            min: t.min,
            savedBehavior,
            grabCursorY: effect.anchorY,
            grabScrollTop: t.el.scrollTop,
          };
          return applyDragTo(effect.cursorY);
        }
        case "dragTo":
          return applyDragTo(effect.cursorY);
        case "coastBy": {
          const tgt = scrollTargetRef.current;
          if (!tgt) return null;
          const liveMax = Math.max(0, tgt.el.scrollHeight - tgt.el.clientHeight);
          const requested = tgt.el.scrollTop + effect.deltaPx;
          const next = Math.max(tgt.min, Math.min(liveMax, requested));
          blockedDirectionRef.current =
            next <= tgt.min && effect.deltaPx < 0
              ? "start"
              : next >= liveMax && effect.deltaPx > 0
                ? "end"
                : null;
          tgt.el.scrollTop = next;
          return {
            requestedTop: requested,
            actualTop: tgt.el.scrollTop,
            atBoundary: blockedDirectionRef.current !== null,
          };
        }
        case "dragRegrab": {
          const tgt = scrollTargetRef.current;
          if (!tgt) return null;
          tgt.grabScrollTop = tgt.el.scrollTop;
          tgt.grabCursorY = effect.cursorY;
          blockedDirectionRef.current = null;
          return null;
        }
        case "scrollStop": {
          const tgt = scrollTargetRef.current;
          if (tgt) tgt.el.style.scrollBehavior = tgt.savedBehavior;
          scrollTargetRef.current = null;
          blockedDirectionRef.current = null;
          return null;
        }
        default:
          return null;
      }
    }

    async function init() {
      try {
        onStatusChange("loading-model");
        // Dynamic import keeps MediaPipe out of the initial bundle —
        // only paid when the user actually toggles hand tracking on.
        const { FilesetResolver, HandLandmarker } = await import(
          "@mediapipe/tasks-vision"
        );
        if (cancelled) return;

        const vision = await FilesetResolver.forVisionTasks(WASM_BASE);
        if (cancelled) return;

        const handLandmarker = await HandLandmarker.createFromOptions(vision, {
          baseOptions: {
            modelAssetPath: MODEL_URL,
            delegate: "GPU",
          },
          runningMode: "VIDEO",
          numHands: 1,
        });
        if (cancelled) {
          handLandmarker.close();
          return;
        }
        landmarkerRef.current = handLandmarker;

        onStatusChange("requesting-camera");
        // Secure-context guard: navigator.mediaDevices is undefined on
        // non-secure origins (http:// on anything other than localhost —
        // e.g., LAN IP dev like http://192.168.x.y:3000, or an HTTP
        // tunnel host). Surface a plain-English cause instead of the
        // raw "cannot read property of undefined reading 'getUserMedia'"
        // TypeError that would otherwise bubble up.
        if (!navigator.mediaDevices?.getUserMedia) {
          throw new Error(
            window.isSecureContext
              ? "This browser doesn't expose the camera API. Try Chrome or Firefox."
              : "Camera access needs a secure origin (HTTPS or localhost). This page is served over HTTP; open it via https://… or http://localhost:3000/."
          );
        }
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { width: 640, height: 480, facingMode: "user" },
          audio: false,
        });
        if (cancelled) {
          stream.getTracks().forEach(t => t.stop());
          return;
        }
        streamRef.current = stream;

        const video = videoRef.current;
        if (!video) {
          // Defensive — React attaches refs before effects, so this
          // shouldn't fire. If it does, throw so the catch block runs
          // cleanup instead of silently leaving the camera hot.
          throw new Error("Video element unmounted mid-initialization.");
        }
        video.srcObject = stream;
        await video.play();
        if (cancelled) return;

        onStatusChange("active");
        startInferenceLoop();
      } catch (err: any) {
        if (cancelled) return;
        const msg =
          err?.name === "NotAllowedError"
            ? "Camera permission denied. Click the camera icon in your browser bar to re-enable."
            : err?.message || "Hand tracking failed to initialize.";
        onError(msg);
      }
    }

    function startInferenceLoop() {
      const tick = () => {
        if (cancelled) return;
        const video = videoRef.current;
        const canvas = canvasRef.current;
        const landmarker = landmarkerRef.current;
        if (
          !video ||
          !canvas ||
          !landmarker ||
          video.readyState < 2 ||
          video.videoWidth === 0
        ) {
          rafRef.current = requestAnimationFrame(tick);
          return;
        }

        // Match canvas backing-store to viewport dimensions so the
        // overlay rasterizes at device-pixel sharpness.
        const dpr = window.devicePixelRatio || 1;
        const w = window.innerWidth;
        const h = window.innerHeight;
        if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
          canvas.width = w * dpr;
          canvas.height = h * dpr;
          canvas.style.width = `${w}px`;
          canvas.style.height = `${h}px`;
        }
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          rafRef.current = requestAnimationFrame(tick);
          return;
        }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);

        const freshObservation =
          lastProcessedVideoTimeRef.current === null ||
          video.currentTime > lastProcessedVideoTimeRef.current;

        if (freshObservation) {
          lastProcessedVideoTimeRef.current = video.currentTime;
          const observationAtMs = performance.now();

          let result: HandLandmarkerResult | null = null;
          try {
            result = landmarker.detectForVideo(video, observationAtMs);
          } catch {
            // Transient inference errors (e.g. WebGPU context loss); the next
            // fresh camera frame will re-try.
          }

          const landmarks =
            result?.landmarks && result.landmarks.length > 0
              ? result.landmarks[0]
              : null;

          if (!landmarks) {
            consecutiveNoHandRef.current += 1;
            if (consecutiveNoHandRef.current === 3) {
              for (const mode of ["dot", "laser"] as const) {
                cursorFiltersRef.current[mode].x.reset();
                cursorFiltersRef.current[mode].y.reset();
              }
              filtersAwaitingReacquireRef.current = hadHandRef.current;
            }
            observationRef.current = {
              ...EMPTY_OBSERVATION,
              cursor: observationRef.current.cursor,
              observationAtMs,
            };
          } else {
            consecutiveNoHandRef.current = 0;
            const gapExceeded =
              lastHandObservationAtRef.current !== null &&
              observationAtMs - lastHandObservationAtRef.current > 100;
            if (gapExceeded) {
              for (const mode of ["dot", "laser"] as const) {
                cursorFiltersRef.current[mode].x.reset();
                cursorFiltersRef.current[mode].y.reset();
              }
              filtersAwaitingReacquireRef.current = hadHandRef.current;
            }
            if (filtersAwaitingReacquireRef.current) {
              reacquiredUntilRef.current = observationAtMs + 1000;
              filtersAwaitingReacquireRef.current = false;
            }
            if (!hadHandRef.current) {
              hadHandRef.current = true;
              setPreviewAcquired(true);
            }
            lastHandObservationAtRef.current = observationAtMs;

            const p0 = landmarks[0]; // wrist
            const p5 = landmarks[5]; // index MCP
            const p9 = landmarks[9]; // middle MCP
            const p13 = landmarks[13]; // ring MCP
            const p17 = landmarks[17]; // pinky MCP
            const p8 = landmarks[8]; // laser depth styling only

            const palmN = {
              x: (p0.x + p5.x + p9.x + p13.x + p17.x) / 5,
              y: (p0.y + p5.y + p9.y + p13.y + p17.y) / 5,
            };
            const palmPosition = {
              x: (1 - palmN.x) * w,
              y: palmN.y * h,
            };
            const currentMode = pointerModeRef.current;
            let rawScreenX: number;
            let rawScreenY: number;
            if (currentMode === "dot") {
              const mapped = mapComfortBox(
                palmN.x,
                palmN.y,
                DEFAULT_COMFORT_BOX
              );
              rawScreenX = (1 - mapped.x) * w;
              rawScreenY = mapped.y * h;
            } else {
              const rawDx = p9.x - p0.x;
              const rawDy = p9.y - p0.y;
              const rawLen = Math.hypot(rawDx, rawDy) || 1e-6;
              const projectedNX = palmN.x + (rawDx / rawLen) * 0.32;
              const projectedNY = palmN.y + (rawDy / rawLen) * 0.32;
              const gainedNX =
                0.5 + (projectedNX - 0.5) * CURSOR_POSITION_GAIN;
              const gainedNY =
                0.5 + (projectedNY - 0.5) * CURSOR_POSITION_GAIN;
              rawScreenX = (1 - Math.max(0, Math.min(1, gainedNX))) * w;
              rawScreenY = Math.max(0, Math.min(1, gainedNY)) * h;
            }
            const filters = cursorFiltersRef.current[currentMode];
            const cursor = {
              x: filters.x.filter(rawScreenX, observationAtMs),
              y: filters.y.filter(rawScreenY, observationAtMs),
            };
            observationRef.current = {
              landmarks,
              cursor,
              palmPosition,
              indexTipZ: p8.z,
              pinchRatio: computePinchRatio(landmarks),
              observationAtMs,
            };
          }
        }

        // Rendering and the reducer run on every RAF tick. When the camera has
        // not advanced, both consume the most recent cached observation.
        const observation = observationRef.current;
        const landmarks = observation.landmarks;
        const hasHand = landmarks !== null;
        const framePinchRatio = observation.pinchRatio;
        const cur = observation.cursor;

        const nowMs = performance.now();
        // Clock-rate windows are HUD diagnostics only — keep their per-frame
        // allocation off the production path.
        if (hudEnabled) {
          rafTimesRef.current = [...rafTimesRef.current, nowMs].filter(
            t => nowMs - t <= 1000
          );
          if (freshObservation) {
            observationTimesRef.current = [
              ...observationTimesRef.current,
              nowMs,
            ].filter(t => nowMs - t <= 1000);
          }
        }
        const clickableUnderCursor = hasHand
          ? findClickableAtPoint(cur.x, cur.y) !== null
          : false;
        const previousPhase = gestureCtxRef.current.phase;
        const targetRequired =
          previousPhase === "DRAGGING" ||
          previousPhase === "DRAG_RELEASE_DEBOUNCE" ||
          previousPhase === "COASTING";
        const blockedDirection =
          previousPhase === "COASTING"
            ? blockedDirectionRef.current
            : null;
        blockedDirectionRef.current = null;
        const gesture = reduceGesture(
          gestureCtxRef.current,
          {
            now: nowMs,
            freshObservation,
            observationAtMs: observation.observationAtMs,
            hasHand,
            pinchRatio: framePinchRatio,
            cursorX: cur.x,
            cursorY: cur.y,
            clickableUnderCursor,
            blockedDirection,
            scrollTargetValid:
              !targetRequired ||
              Boolean(scrollTargetRef.current?.el.isConnected),
          },
          DEFAULT_PARAMS
        );
        gestureCtxRef.current = gesture.ctx;
        const tele = applyGestureEffect(gesture.effect, ctx, cur);

        // One avatar only. Laser adds only its palm-to-cursor beam; grip state
        // is communicated by contracting and brightening that same cursor dot.
        if (hasHand && observation.palmPosition) {
          if (pointerModeRef.current === "laser") {
            const handPos = observation.palmPosition;
            const zNorm = Math.max(-0.2, Math.min(0.2, observation.indexTipZ));
            const depthFactor = (0.2 - zNorm) / 0.4;
            const beamAlpha = 0.4 + depthFactor * 0.45;
            const grad = ctx.createLinearGradient(
              handPos.x,
              handPos.y,
              cur.x,
              cur.y
            );
            grad.addColorStop(0, `rgba(245, 165, 36, ${beamAlpha * 0.25})`);
            grad.addColorStop(1, `rgba(245, 165, 36, ${beamAlpha})`);
            ctx.strokeStyle = grad;
            ctx.lineWidth = 1.5 + depthFactor * 2.5;
            ctx.lineCap = "round";
            ctx.beginPath();
            ctx.moveTo(handPos.x, handPos.y);
            ctx.lineTo(cur.x, cur.y);
            ctx.stroke();
          }

          const gripped =
            gesture.ctx.phase === "PRESS_CLICK_ELIGIBLE" ||
            gesture.ctx.phase === "DRAGGING" ||
            gesture.ctx.phase === "DRAG_RELEASE_DEBOUNCE";
          if (gesture.ctx.phase === "IDLE_OPEN" && clickableUnderCursor) {
            ctx.strokeStyle = "rgba(245, 165, 36, 0.28)";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.arc(cur.x, cur.y, 12, 0, Math.PI * 2);
            ctx.stroke();
          }
          ctx.fillStyle = gripped
            ? "rgba(255, 190, 66, 1)"
            : "rgba(245, 165, 36, 0.95)";
          ctx.strokeStyle = gripped
            ? "rgba(255, 224, 154, 0.95)"
            : "rgba(14, 14, 16, 0.8)";
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(cur.x, cur.y, gripped ? 6 : 8, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();

          if (
            hudEnabled &&
            (gesture.ctx.phase === "PRESS_CLICK_ELIGIBLE" ||
              gesture.ctx.phase === "DRAGGING" ||
              gesture.ctx.phase === "DRAG_RELEASE_DEBOUNCE")
          ) {
            const ax = gesture.ctx.anchorX;
            const ay = gesture.ctx.anchorY;
            ctx.strokeStyle = "rgba(103, 232, 249, 0.75)";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(ax - 5, ay);
            ctx.lineTo(ax + 5, ay);
            ctx.moveTo(ax, ay - 5);
            ctx.lineTo(ax, ay + 5);
            ctx.stroke();
          }
        }

        // Diagnostic HUD store (only when ?hthud). Mutated every frame; sampled
        // into React state at ~10Hz by the interval below.
        if (hudEnabled) {
          const st = gesture.ctx;
          if (gesture.reason !== hudTrailRef.current[0]?.reason) {
            hudTrailRef.current = [
              { reason: gesture.reason, at: nowMs },
              ...hudTrailRef.current,
            ].slice(0, 12);
          }
          const tgt = scrollTargetRef.current;
          const liveMax = tgt
            ? Math.max(0, tgt.el.scrollHeight - tgt.el.clientHeight)
            : 0;
          const pressDelta =
            st.phase === "PRESS_CLICK_ELIGIBLE"
              ? Math.hypot(cur.x - st.anchorX, cur.y - st.anchorY)
              : 0;
          hudRef.current = {
            phase: st.phase,
            reason: gesture.reason,
            pinchRatio: Number.isFinite(framePinchRatio) ? framePinchRatio : 0,
            hasHand,
            openFrames: st.openFrames,
            missedFrames: st.missedFrames,
            cursorX: cur.x,
            cursorY: cur.y,
            anchorY: st.anchorY,
            pressDelta,
            lastClickDelta: lastClickDeltaRef.current,
            observationRate: observationTimesRef.current.length,
            rafRate: rafTimesRef.current.length,
            releaseVelocity: st.releaseVelocity,
            coastVelocity: st.coastVelocity,
            sampleCount: st.motionSamples.length,
            reacquired: nowMs < reacquiredUntilRef.current,
            requestedTop: tele?.requestedTop ?? (tgt ? tgt.el.scrollTop : 0),
            actualTop: tele?.actualTop ?? (tgt ? tgt.el.scrollTop : 0),
            scrollMax: liveMax,
            atBoundary:
              tele?.atBoundary ??
              Boolean(
                tgt &&
                  (tgt.el.scrollTop <= 0.5 || tgt.el.scrollTop >= liveMax - 0.5)
              ),
            container: tgt ? describeContainer(tgt.el) : "-",
            clickable: clickableUnderCursor,
          };
        }

        rafRef.current = requestAnimationFrame(tick);
      };
      rafRef.current = requestAnimationFrame(tick);
    }

    init();

    return () => {
      cancelled = true;
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      if (streamRef.current) {
        streamRef.current.getTracks().forEach(t => t.stop());
        streamRef.current = null;
      }
      if (videoRef.current) {
        videoRef.current.srcObject = null;
      }
      if (landmarkerRef.current) {
        try {
          landmarkerRef.current.close();
        } catch {
          /* ignore */
        }
        landmarkerRef.current = null;
      }
      // Restore the locked container's scroll-behavior if we tore down mid-scroll.
      if (scrollTargetRef.current) {
        scrollTargetRef.current.el.style.scrollBehavior =
          scrollTargetRef.current.savedBehavior;
        scrollTargetRef.current = null;
      }
      gestureCtxRef.current = createGestureContext();
      lastProcessedVideoTimeRef.current = null;
      observationRef.current = { ...EMPTY_OBSERVATION };
      for (const mode of ["dot", "laser"] as const) {
        cursorFiltersRef.current[mode].x.reset();
        cursorFiltersRef.current[mode].y.reset();
      }
      lastHandObservationAtRef.current = null;
      consecutiveNoHandRef.current = 0;
      hadHandRef.current = false;
      filtersAwaitingReacquireRef.current = false;
      blockedDirectionRef.current = null;
    };
    // pointerMode is read via ref so toggling doesn't re-init the entire
    // pipeline. Errors + status callbacks are stable from the parent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      {/* Acquire-then-fade preview. The visual container fades after the first
         fresh hand observation; the video itself stays mounted and playing as
         the inference source. ?hthud keeps it visible for diagnosis.
         Positioned bottom-left ABOVE the toggle with a generous gap
         (bottom-16 = 64px from viewport-bottom) so the toggle button
         at bottom-3 has 24px of breathing room and clicks can never
         land on the video. pointer-events-none applied at each nesting
         level (parent wrapper, inner box, video) since CSS
         pointer-events doesn't inherit to descendants — a stray tap
         on the video was intercepting the click intended for the
         toggle underneath, so the button appeared to "not close." */}
      <div className="pointer-events-none fixed left-3 bottom-16 z-[72] flex flex-col items-start gap-1">
        <div
          className="pointer-events-none relative overflow-hidden rounded-md border border-white/15 bg-[#0A0A0C]/95 backdrop-blur shadow-lg"
          style={{
            width: 160,
            height: 120,
            opacity: previewAcquired && !hudEnabled ? 0 : 1,
            transition: "opacity 600ms ease",
          }}
        >
          <video
            ref={videoRef}
            className="pointer-events-none h-full w-full object-cover"
            style={{ transform: "scaleX(-1)" }}
            playsInline
            muted
            autoPlay
            aria-hidden="true"
          />
        </div>
      </div>
      {/* Fullscreen click-overlay canvas — hosts the one amber avatar, optional
         laser beam, diagnostic anchor crosshair, and click-flash ring.
         pointer-events:none so the underlying app remains interactive
         while tracking is on. */}
      <canvas
        ref={canvasRef}
        className="pointer-events-none fixed inset-0 z-[70]"
        aria-hidden="true"
      />
      {/* Diagnostic HUD — opt-in via ?hthud in the URL. Live gesture-state +
         scroll telemetry so the next live test is an OBSERVATION, not another
         blind threshold guess (the think-tank's highest-leverage item). */}
      {hudEnabled && hudSnap && (
        <div
          className="pointer-events-none fixed right-3 top-3 z-[73] rounded-md border border-white/15 bg-black/85 px-3 py-2 font-mono text-[10px] leading-[1.35] text-emerald-200 shadow-lg"
          style={{ width: 250 }}
        >
          <div className="mb-1 font-semibold text-amber-300">
            hand-tracking HUD · {hudSnap.phase}
          </div>
          <HudRow k="reason" v={hudSnap.reason} />
          <HudRow
            k="hand"
            v={hudSnap.hasHand ? "yes" : `no (${hudSnap.missedFrames})`}
          />
          <HudRow k="pinch" v={hudSnap.pinchRatio.toFixed(3)} />
          <HudRow k="openF" v={String(hudSnap.openFrames)} />
          <HudRow
            k="cursor"
            v={`${hudSnap.cursorX.toFixed(0)}, ${hudSnap.cursorY.toFixed(0)}`}
          />
          <HudRow k="anchorY" v={hudSnap.anchorY.toFixed(0)} />
          <HudRow
            k="anchor→cursor"
            v={`${hudSnap.pressDelta.toFixed(1)} · click ${hudSnap.lastClickDelta.toFixed(1)}`}
          />
          <HudRow
            k="clocks"
            v={`obs/s ${hudSnap.observationRate.toFixed(1)} · raf/s ${hudSnap.rafRate.toFixed(1)}`}
          />
          <HudRow k="releaseVel" v={hudSnap.releaseVelocity.toFixed(0)} />
          <HudRow k="coastVel" v={hudSnap.coastVelocity.toFixed(0)} />
          <HudRow k="samples" v={String(hudSnap.sampleCount)} />
          <HudRow k="reacquired" v={hudSnap.reacquired ? "YES" : "no"} />
          <HudRow
            k="scrollTop"
            v={`${hudSnap.actualTop.toFixed(0)} / ${hudSnap.scrollMax.toFixed(0)}`}
          />
          <HudRow k="reqTop" v={hudSnap.requestedTop.toFixed(0)} />
          <HudRow k="boundary" v={hudSnap.atBoundary ? "YES" : "no"} />
          <HudRow k="container" v={hudSnap.container} />
          <HudRow k="clickable" v={hudSnap.clickable ? "yes" : "no"} />
          <div className="mt-1 border-t border-white/10 pt-1 text-white/55">
            params · gain {DEFAULT_PARAMS.drag.gain.toFixed(1)} · flick{" "}
            {DEFAULT_PARAMS.drag.flickThresholdPxPerSec} · tau{" "}
            {DEFAULT_PARAMS.drag.coastTauMs}
          </div>
          <div className="mt-1 text-white/40">
            {hudTrailRef.current
              .slice(0, 6)
              .map(t => t.reason)
              .join(" ← ")}
          </div>
        </div>
      )}
    </>
  );
}

// --- Scroll-container resolution ------------------------------------------

/**
 * Walk up from the grab point to the nearest actually-scrollable ancestor and
 * lock onto it, falling back to document.scrollingElement (the window scroller).
 * Fixes the dominant "scroll does nothing" bug: pages like BroadcastPage pin the
 * document to `h-screen overflow-hidden` and scroll inside a nested column, so
 * the old window.scrollTo() moved nothing there.
 */
function resolveScrollContainer(
  clientX: number,
  clientY: number
): { el: HTMLElement; min: number } {
  let el: Element | null = document.elementFromPoint(clientX, clientY);
  while (el instanceof HTMLElement) {
    const style = window.getComputedStyle(el);
    if (isScrollableStyle(style.overflowY, el.scrollHeight, el.clientHeight)) {
      return {
        el,
        min: 0,
      };
    }
    el = el.parentElement;
  }
  const doc =
    (document.scrollingElement as HTMLElement | null) ??
    document.documentElement;
  return {
    el: doc,
    min: 0,
  };
}

/** Short human label for the HUD: "window" or a nested "tag.class". */
function describeContainer(el: HTMLElement): string {
  if (el === document.scrollingElement || el === document.documentElement) {
    return "window";
  }
  const cls = (el.className || "")
    .toString()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .join(".");
  return el.tagName.toLowerCase() + (cls ? "." + cls : "");
}

// --- Diagnostic HUD row ---------------------------------------------------

function HudRow({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-white/45">{k}</span>
      <span className="text-emerald-200">{v}</span>
    </div>
  );
}

// --- Click synthesis ------------------------------------------------------

function findClickableAtPoint(
  clientX: number,
  clientY: number
): Element | null {
  const target = document.elementFromPoint(clientX, clientY);
  return target ? findClickableAncestor(target) : null;
}

function synthesizeClick(clientX: number, clientY: number): void {
  // Walk up to a focusable / clickable ancestor when the literal
  // element-at-point is a text node or non-interactive child. This
  // mirrors how browsers themselves resolve pointer-event targets.
  const finalTarget = findClickableAtPoint(clientX, clientY);
  if (!finalTarget) return;
  const eventInit: MouseEventInit = {
    bubbles: true,
    cancelable: true,
    view: window,
    clientX,
    clientY,
    button: 0,
  };
  if (typeof window.PointerEvent === "function") {
    const pointerInit: PointerEventInit = {
      ...eventInit,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    };
    finalTarget.dispatchEvent(
      new PointerEvent("pointerdown", { ...pointerInit, buttons: 1 })
    );
    finalTarget.dispatchEvent(
      new MouseEvent("mousedown", { ...eventInit, buttons: 1 })
    );
    finalTarget.dispatchEvent(
      new PointerEvent("pointerup", { ...pointerInit, buttons: 0 })
    );
  } else {
    finalTarget.dispatchEvent(
      new MouseEvent("mousedown", { ...eventInit, buttons: 1 })
    );
  }
  finalTarget.dispatchEvent(
    new MouseEvent("mouseup", { ...eventInit, buttons: 0 })
  );
  finalTarget.dispatchEvent(new MouseEvent("click", eventInit));
}

function findClickableAncestor(el: Element): Element | null {
  let current: Element | null = el;
  while (current) {
    const tag = current.tagName;
    if (
      tag === "A" ||
      tag === "BUTTON" ||
      tag === "INPUT" ||
      tag === "SELECT" ||
      tag === "TEXTAREA" ||
      tag === "LABEL" ||
      current.getAttribute("role") === "button" ||
      (current as HTMLElement).onclick !== null
    ) {
      return current;
    }
    current = current.parentElement;
  }
  return null;
}

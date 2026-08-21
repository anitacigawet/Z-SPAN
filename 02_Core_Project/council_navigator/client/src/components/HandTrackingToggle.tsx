/**
 * HandTrackingToggle — V1-HandTracking-1 UI affordance.
 *
 * The bottom-of-page hand-icon entry-point + the small ⓘ chip that
 * surfaces the operator-locked privacy disclosure (verbatim copy per
 * the V1-HandTracking-1 spec in TASKS.md). When hand tracking is on, a
 * tiny inline pill exposes the dot/laser pointer-mode sub-toggle next
 * to the main button.
 *
 * Visual pattern reuses the PromptInfoIcon shape per
 * [[prompt-provenance-info-icon-pattern]] — tiny circle with 'i',
 * hover-or-click trigger, popover typography matching the post-d71ee9a
 * refinement. The disclosure body itself is operator-voiced and
 * preserved VERBATIM — including "your devices browser" without
 * apostrophe correction. Any future tweaks to the copy land via an
 * operator-explicit edit, not Claude-side polish.
 */
import { useEffect, useRef, useState } from "react";
import { Hand } from "lucide-react";
import { useHandTracking, type PointerMode } from "./HandTrackingProvider";

// Build-time-resolved link to the live source for HandTrackingProvider
// on main. The "[here]" link in the locked disclosure copy targets this
// URL so curious visitors can audit our integration end-to-end.
//
// ⚠️ HARDCODED to `anitacigawet/Z-SPAN`. When the repo transfers to an
// entity org per S-044 LLC formation, update this URL or the [here]
// link in the operator-locked disclosure copy breaks silently. The
// disclosure copy itself is operator-voiced verbatim; the LINK is the
// only mutable piece.
const PROVIDER_SOURCE_URL =
  "https://github.com/anitacigawet/Z-SPAN/blob/main/02_Core_Project/council_navigator/client/src/components/HandTrackingProvider.tsx";

const MEDIAPIPE_DOCS_URL =
  "https://developers.google.com/edge/mediapipe/solutions/vision/hand_landmarker";

export function HandTrackingToggle() {
  const {
    enabled,
    pointerMode,
    status,
    errorMessage,
    setEnabled,
    setPointerMode,
  } = useHandTracking();

  const handIconColor =
    status === "active"
      ? "#F5A524"
      : status === "error"
        ? "#D83933"
        : enabled
          ? "#E4E4E5"
          : "#71717A";

  return (
    // z-[75] — sits ABOVE the video preview panel (z-72) so the toggle
    // always receives its own click even if the preview grew or shifted.
    // Prior z-55 was BELOW the preview; iPad taps within the 8px gap
    // between the two occasionally landed on the video instead. Also
    // moved the preview up (bottom-16 in the Provider) so the vertical
    // gap grew from 8px → 24px.
    <div className="pointer-events-auto fixed bottom-3 left-5 z-[75] flex items-center gap-1.5 select-none">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setEnabled(!enabled);
        }}
        className="flex h-7 w-7 items-center justify-center rounded-md border bg-[#0E0E10]/95 shadow-sm backdrop-blur-sm transition-colors duration-200 hover:bg-[#0E0E10]"
        style={{ borderColor: enabled ? "#F5A524" : "#0C7A43" }}
        aria-pressed={enabled}
        aria-label={enabled ? "Disable hand tracking" : "Enable hand tracking"}
        title={
          status === "error" && errorMessage
            ? errorMessage
            : enabled
              ? "Disable hand tracking"
              : "Enable hand tracking"
        }
      >
        <Hand size={14} color={handIconColor} strokeWidth={2} />
      </button>
      {enabled && (
        <PointerModePill mode={pointerMode} onChange={setPointerMode} />
      )}
      {/* Session-33 (2026-07-04) — the ⓘ moved AFTER the dot/laser pill
         (was between the hand-toggle and the pill) so accidental hovers
         while reaching for the toggle don't pop the disclosure. Combined
         with dropping onMouseEnter below — the popover now only opens
         on click. */}
      <HandTrackingInfoIcon />
      {/* Session-31 (2026-07-04) — status feedback panel replaces the
         tiny inline "loading…" pill. Operator direction: when hand
         tracking doesn't work, the citizen had no info — the button
         just silently failed. New panel is a compact status readout
         showing loading progress + error details + debug info, sitting
         above the preview box near the toggle button. */}
      {enabled && status !== "active" && (
        <HandTrackingStatusPanel status={status} errorMessage={errorMessage} />
      )}
    </div>
  );
}

// --- Status feedback panel -----------------------------------------------

function HandTrackingStatusPanel({
  status,
  errorMessage,
}: {
  status: "off" | "loading-model" | "requesting-camera" | "active" | "error";
  errorMessage: string | null;
}) {
  const label =
    status === "loading-model"
      ? "Loading MediaPipe model…"
      : status === "requesting-camera"
        ? "Requesting camera permission…"
        : status === "error"
          ? "Hand tracking failed"
          : "Off";
  const color =
    status === "error" ? "#D83933" : status === "loading-model" || status === "requesting-camera" ? "#F5A524" : "#71717A";

  const [debugOpen, setDebugOpen] = useState(false);

  const diagnostics = (): string => {
    const ua = typeof navigator !== "undefined" ? navigator.userAgent : "unknown";
    const secure = typeof window !== "undefined" ? window.isSecureContext : false;
    const perms = typeof navigator !== "undefined" && "permissions" in navigator ? "available" : "unavailable";
    const mediaDevices = typeof navigator !== "undefined" && navigator.mediaDevices ? "available" : "unavailable";
    const gpu = typeof navigator !== "undefined" && "gpu" in navigator ? "yes" : "no";
    return [
      `status=${status}`,
      `error=${errorMessage ?? "(none)"}`,
      `secure_context=${secure}`,
      `permissions_api=${perms}`,
      `media_devices=${mediaDevices}`,
      `webgpu=${gpu}`,
      `viewport=${typeof window !== "undefined" ? `${window.innerWidth}x${window.innerHeight}` : "unknown"}`,
      `ua=${ua}`,
    ].join("\n");
  };

  const copyDiagnostics = () => {
    if (typeof navigator === "undefined" || !navigator.clipboard) return;
    void navigator.clipboard.writeText(diagnostics());
  };

  return (
    <div className="flex items-center gap-1.5 rounded-md border border-white/15 bg-[#0A0A0C]/95 px-2 py-1 backdrop-blur shadow-sm">
      <span
        className="inline-block h-1.5 w-1.5 rounded-full animate-pulse"
        style={{ backgroundColor: color }}
        aria-hidden="true"
      />
      <span className="text-[10px] uppercase tracking-[0.14em]" style={{ color }}>
        {label}
      </span>
      {status === "error" && (
        <>
          <button
            type="button"
            onClick={() => setDebugOpen((o) => !o)}
            className="text-[10px] uppercase tracking-[0.14em] text-white/50 hover:text-white/85 underline decoration-dotted underline-offset-2"
            aria-expanded={debugOpen}
          >
            {debugOpen ? "hide" : "debug"}
          </button>
          {debugOpen && (
            <div
              role="dialog"
              className="absolute bottom-full left-0 mb-2 w-[min(360px,90vw)] border border-white/15 bg-[#0A0A0C] shadow-2xl"
            >
              <div className="border-b border-white/10 px-3 py-2">
                <span className="text-[10px] uppercase tracking-[0.18em] text-white/45">
                  Hand-tracking diagnostics
                </span>
              </div>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words px-3 py-2 text-[10px] leading-relaxed text-white/80 font-mono">
{diagnostics()}
              </pre>
              <div className="border-t border-white/10 px-3 py-2 flex items-center justify-between text-[10px]">
                <span className="text-white/45">
                  Paste this into a bug report if the feature won't work.
                </span>
                <button
                  type="button"
                  onClick={copyDiagnostics}
                  className="text-[#F5A524] underline decoration-dotted underline-offset-2 hover:text-white"
                >
                  Copy
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// --- Pointer-mode sub-toggle ---------------------------------------------

function PointerModePill({
  mode,
  onChange,
}: {
  mode: PointerMode;
  onChange: (m: PointerMode) => void;
}) {
  return (
    <div
      className="flex h-7 items-center overflow-hidden rounded-md border bg-[#0E0E10]/95 text-[10px] uppercase tracking-[0.18em] shadow-sm backdrop-blur-sm"
      style={{ borderColor: "#0C7A43" }}
      role="radiogroup"
      aria-label="Pointer style"
    >
      {/* The two modes are semantically different instruments (2026-07-13
         ownership rework): dot maps your palm through a comfort box — the
         cursor IS your hand; laser projects a ray for lean-back pointing.
         Tooltips carry the distinction; the pill labels stay compact. */}
      <button
        type="button"
        role="radio"
        aria-checked={mode === "dot"}
        onClick={() => onChange("dot")}
        title="Dot — your hand is the cursor"
        className={`h-full px-2 transition-colors ${
          mode === "dot" ? "bg-[#0C7A43]/40 text-[#E4E4E5]" : "text-[#71717A] hover:text-[#E4E4E5]"
        }`}
      >
        dot
      </button>
      <span className="h-full w-px bg-[#0C7A43]/40" aria-hidden="true" />
      <button
        type="button"
        role="radio"
        aria-checked={mode === "laser"}
        onClick={() => onChange("laser")}
        title="Laser — point from a distance"
        className={`h-full px-2 transition-colors ${
          mode === "laser" ? "bg-[#0C7A43]/40 text-[#E4E4E5]" : "text-[#71717A] hover:text-[#E4E4E5]"
        }`}
      >
        laser
      </button>
    </div>
  );
}

// --- ⓘ disclosure ---------------------------------------------------------

function HandTrackingInfoIcon() {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current) return;
      if (!wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span ref={wrapRef} className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full border text-[10px] leading-none transition-colors hover:bg-white/10"
        style={{
          color: "#E4E4E5",
          borderColor: "#E4E4E5",
          opacity: 0.55,
        }}
        aria-label="About hand tracking — privacy disclosure"
        aria-expanded={open}
      >
        i
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Hand tracking privacy disclosure"
          className="absolute bottom-full left-0 z-[60] mb-2 w-[min(420px,90vw)] border bg-[#0A0A0C] shadow-2xl"
          style={{ borderColor: "rgba(228,228,229,0.15)" }}
        >
          <div className="border-b border-white/10 px-4 py-2.5">
            <span className="text-[11px] uppercase tracking-[0.18em] text-white/45">
              About hand tracking
            </span>
          </div>
          <div className="space-y-3 px-4 py-3 text-[13px] leading-relaxed text-white/85">
            {/* Operator-locked verbatim copy. Do NOT polish: "your devices
                browser" (no apostrophe) is operator-voice and stays.
                Any change goes through operator-explicit edit. */}
            <p>
              This magical feature is accomplished through the{" "}
              <a
                href={MEDIAPIPE_DOCS_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[#F5A524] underline decoration-[#F5A524]/40 underline-offset-2 hover:decoration-[#F5A524]"
              >
                MediaPipe HandLandmarker API
              </a>{" "}
              and is processed 100% locally on your device, using existing
              WebAssembly and WebGPU functionality.
            </p>
            <p>
              The model downloads once from Google, and after that, the hand
              tracking runs entirely on your devices browser.
            </p>
            <p>
              The source code for our integration of this functionality can be
              found{" "}
              <em>
                <a
                  href={PROVIDER_SOURCE_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[#F5A524] underline decoration-[#F5A524]/40 underline-offset-2 hover:decoration-[#F5A524]"
                >
                  here
                </a>
              </em>
              , and{" "}
              <em>
                at no point does Google or Z-SPAN, in any way shape or form,
                receive a single frame from your camera
              </em>
              .
            </p>
          </div>
        </div>
      )}
    </span>
  );
}

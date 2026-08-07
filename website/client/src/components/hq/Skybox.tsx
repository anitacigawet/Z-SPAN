import { useCallback, useEffect, useRef, useState } from "react";
import type { TrafficEvent } from "@/utils/skyboxStream";
import { useTrafficEventStream } from "@/utils/skyboxStream";
import { StarField, DEFAULT_VARIANT_ID, type HeldStarInfo } from "./skybox/StarField";
import { drawBackgroundStars } from "./skybox/backgroundStars";

/**
 * The vertical-scroll skybox region above the HQ building.
 *
 * Layered depth (back to front):
 * 1. .skybox-bg — main vertical gradient: HQ warm-dusk at the
 * horizon up to OLED black at the zenith.
 * 2. .skybox-nebula — subtle violet ellipse mid-upper sky (screen
 * blend); gives the void a hint of structure
 * without competing with shooting stars.
 * 3. .skybox-stars-bg — one-shot procedural background star field;
 * static depth, drawn once on mount/resize.
 * 4. .skybox-canvas — the long-exposure shooting-star renderer
 * (StarField), driven by the SSE stream.
 * 5. .skybox-haze — atmospheric haze at the bottom where the sky
 * meets the building roof. Sells the seam.
 */
export default function Skybox({
 variantId = DEFAULT_VARIANT_ID,
 visible = true,
}: {
 variantId?: string;
 /** Visibility gate (perf pass 2026-07-02): the page lands scrolled to
 * the building, leaving this whole region offscreen — but the
 * StarField rAF accumulator kept rendering. When false, the field
 * stops and SSE spawns are dropped (a shooting star nobody can see
 * doesn't need to exist). Zero quality change when visible. */
 visible?: boolean;
} = {}) {
 const canvasRef = useRef<HTMLCanvasElement | null>(null);
 const bgCanvasRef = useRef<HTMLCanvasElement | null>(null);
 const fieldRef = useRef<StarField | null>(null);
 const visibleRef = useRef(visible);

 // Shooting-star renderer lifecycle. Constructed once; variant changes
 // are pushed via setVariant() (the field doesn't need to be torn down
 // for an aesthetic swap — in-flight stars finish under their old colors,
 // new spawns pick up the new variant).
 useEffect(() => {
 const canvas = canvasRef.current;
 if (!canvas) return;
 const field = new StarField(canvas, variantId);
 fieldRef.current = field;
 field.start();
 return () => {
 field.stop();
 fieldRef.current = null;
 };
 // eslint-disable-next-line react-hooks/exhaustive-deps
 }, []);

 // V4 (2026-05-31): push variant changes to the existing field instead
 // of reconstructing it — preserves in-flight stars + the SSE subscription.
 useEffect(() => {
 fieldRef.current?.setVariant(variantId);
 }, [variantId]);

 // Visibility gate — stop the rAF loop while the sky is scrolled away;
 // clean stop/start restart on return so there's never a double loop.
 useEffect(() => {
 visibleRef.current = visible;
 const field = fieldRef.current;
 if (!field) return;
 field.stop();
 if (visible) field.start();
 }, [visible]);

 // Static background stars — render once on mount + on each resize.
 useEffect(() => {
 const bg = bgCanvasRef.current;
 if (!bg) return;
 const ctx = bg.getContext("2d");
 if (!ctx) return;

 const render = (): void => {
 const dpr = window.devicePixelRatio || 1;
 const rect = bg.getBoundingClientRect();
 bg.width = Math.max(1, Math.floor(rect.width * dpr));
 bg.height = Math.max(1, Math.floor(rect.height * dpr));
 ctx.setTransform(1, 0, 0, 1, 0, 0);
 ctx.scale(dpr, dpr);
 ctx.clearRect(0, 0, rect.width, rect.height);
 drawBackgroundStars(ctx, rect.width, rect.height);
 };

 render();
 window.addEventListener("resize", render);
 return () => window.removeEventListener("resize", render);
 }, []);

 // Stable callback so the SSE hook doesn't reconnect every render.
 // Spawns are dropped while offscreen — otherwise they'd pile up in the
 // stopped field and burst all at once when the visitor scrolls up.
 const onEvent = useCallback((evt: TrafficEvent) => {
 if (!visibleRef.current) return;
 fieldRef.current?.spawn(evt);
 }, []);

 useTrafficEventStream(onEvent);

 // Catch-a-star (local workspace mode): payload-carrying stars freeze
 // under the cursor and show what they are — the exact transcription
 // segment / retrieval query / synthesis receipts / gate verdict the
 // pipeline just performed. The field hit-tests per FRAME against the
 // last pointer position, so a star flying into a resting cursor is
 // caught too; the listener callback is how a catch reaches React.
 // Flagship stars carry no payload, so on zspan.org the cursor passes
 // straight through and nothing ever renders.
 const [held, setHeld] = useState<HeldStarInfo | null>(null);
 useEffect(() => {
 fieldRef.current?.setHoldListener(setHeld);
 return () => fieldRef.current?.setHoldListener(null);
 }, []);
 const onSkyMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
 const canvas = canvasRef.current;
 if (!canvas) return;
 const rect = canvas.getBoundingClientRect();
 fieldRef.current?.setPointer(e.clientX - rect.left, e.clientY - rect.top);
 }, []);
 const onSkyMouseLeave = useCallback(() => {
 fieldRef.current?.clearPointer();
 }, []);

 return (
 <div
 className="skybox"
 onMouseMove={onSkyMouseMove}
 onMouseLeave={onSkyMouseLeave}
 style={held ? { cursor: "pointer" } : undefined}
 >
 <div className="skybox-bg" aria-hidden="true" />
 <div className="skybox-nebula" aria-hidden="true" />
 <canvas
 ref={bgCanvasRef}
 className="skybox-stars-bg"
 aria-hidden="true"
 />
 <canvas
 ref={canvasRef}
 className="skybox-canvas"
 aria-label="Live traffic visualization — shooting stars cross the sky as visitors arrive"
 />
 {held && <CaughtStarPanel info={held} />}
 </div>
 );
}

/**
 * The caught star's payload, pinned where it froze — the DeptZone
 * worker-detail pattern applied to the sky. Reads as a colleague's
 * note, not a schema row: the label line says what happened, the
 * detail block carries the actual material (decoded words, the
 * retrieval query, chunk receipts, a gate's failure list).
 */
function CaughtStarPanel({ info }: { info: HeldStarInfo }) {
 const { evt } = info;
 const failed = evt.status >= 400;
 // Keep the panel inside the sky: open leftward past mid-canvas, and
 // below the star when caught near the top edge.
 const openLeft = typeof window !== "undefined" && info.x > window.innerWidth * 0.55;
 const kindWord =
 evt.kind === "librarian" ? "the Librarian"
 : evt.kind === "watcher" ? "the sky itself"
 : evt.kind || "activity";
 return (
 <div
 style={{
 position: "absolute",
 left: info.x,
 top: Math.max(info.y, 96),
 transform: openLeft
 ? "translate(calc(-100% - 18px), -50%)"
 : "translate(18px, -50%)",
 maxWidth: 380,
 padding: "12px 14px",
 borderRadius: 8,
 border: `1px solid ${failed ? "rgba(248,113,113,0.4)" : "rgba(255,255,255,0.18)"}`,
 background: "rgba(10, 10, 14, 0.92)",
 boxShadow: "0 6px 28px rgba(0,0,0,0.55)",
 pointerEvents: "none",
 zIndex: 5,
 }}
 >
 <p
 style={{
 margin: 0,
 fontSize: 10,
 letterSpacing: "0.14em",
 textTransform: "uppercase",
 color: failed ? "rgba(248,113,113,0.85)" : "rgba(255,255,255,0.45)",
 }}
 >
 {failed ? `${kindWord} · refused` : kindWord}
 </p>
 <p
 style={{
 margin: "6px 0 0",
 fontSize: 13,
 lineHeight: 1.45,
 color: "rgba(255,255,255,0.92)",
 fontWeight: 600,
 }}
 >
 {evt.label || "activity"}
 </p>
 {evt.detail && (
 <p
 style={{
 margin: "7px 0 0",
 fontSize: 12,
 lineHeight: 1.55,
 color: "rgba(255,255,255,0.62)",
 whiteSpace: "pre-wrap",
 overflowWrap: "anywhere",
 }}
 >
 {evt.detail}
 </p>
 )}
 </div>
 );
}

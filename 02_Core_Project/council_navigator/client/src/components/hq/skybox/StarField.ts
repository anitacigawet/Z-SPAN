/**
 * Canvas-2D fiber-optic shooting-star renderer for the HQ skybox.
 *
 * V3 (James's review 2026-05-29): "literal fiber-optic" = a thin WIRE
 * with a bright point of light at the end, not a glowing bulb with a
 * vague aura.
 *
 * V4 (James's review 2026-05-31 + design-variant workflow): V3's
 * wire-not-bulb intent is preserved, but V3 went too far toward
 * "geometric streak" and lost the fiber-optic feel. V4 introduces a
 * VARIANT system so 4 distinct aesthetic interpretations of "fiber-
 * optic" can be A/B-tested via the owner-only `VariantSwitcherPanel`
 * in the HQ. Each variant configures: trail color gradient along the
 * length, 3-layer tip glow (outer atmospheric bloom + inner tight
 * halo + pinpoint core), tip blend mode (source-over vs additive
 * "lighter"), trail width taper, per-star hue + brightness jitter.
 *
 * Red ("rejected") stars stay V3-shape (white-red + bounce + shockwave) --
 * variants apply only to the normal (white) traffic stars. The bouncing
 * red is a distinct semantic visual and doesn't get the fiber-optic
 * remix.
 *
 * Trajectory math (the parallel-arcs flow from photo 4): narrow vx
 * range + small initial upward bias + gentle gravity so all stars
 * trace similar gentle parabolic arcs and exit the right edge in a
 * vertical band.
 */

import type { TrafficEvent } from "@/utils/skyboxStream";
import { isRedEvent } from "@/utils/skyboxStream";

type StarColor = "white" | "red";
type StarState = "flowing" | "bouncing";
type StarPoint = { x: number; y: number };

// Per-star precomputed colors after variant + jitter applied at spawn.
type StarVariantColors = {
  trail_head: string; // "R, G, B"
  trail_mid: string;
  trail_tail: string;
  tip_core: string;
  tip_halo: string;
  tip_bloom: string;
  brightness_mul: number; // 1.0 = baseline
};

type Star = {
  x: number;
  y: number;
  vx: number;
  vy: number;
  age: number;
  color: StarColor;
  state: StarState;
  bounceX: number;
  impactX: number;
  impactY: number;
  impactAge: number;
  history: StarPoint[];
  lastSampleAge: number;
  variantColors: StarVariantColors | null; // null for red stars
  /** The event that spawned this star. Local-workspace events carry a
   *  payload (kind/label/detail) — those stars are catchable: hovering
   *  freezes them mid-flight while the popup shows what they are. */
  evt: TrafficEvent;
  held: boolean;
};

/** What hoverAt() hands the popup layer: the caught star's event +
 *  its current canvas position (CSS pixels). */
export type HeldStarInfo = {
  evt: TrafficEvent;
  x: number;
  y: number;
};

// ── Variant system (V4) ───────────────────────────────────────────────

export type VariantConfig = {
  id: string;
  display_name: string;
  philosophy: string;
  trail_colors: { head_rgb: string; mid_rgb: string; tail_rgb: string };
  tip_colors: { core_rgb: string; halo_rgb: string; bloom_rgb: string };
  bloom: {
    core_radius_px: number;
    inner_halo_radius_px: number;
    outer_bloom_radius_px: number;
    outer_bloom_alpha_peak: number;
    blend_mode: GlobalCompositeOperation; // "source-over" | "lighter"
  };
  trail: {
    width_head_px: number;
    width_tail_px: number;
    alpha_curve: "linear" | "quadratic" | "cubic";
    alpha_peak_at_head: number;
  };
  jitter_per_star: { hue_shift_range_deg: number; brightness_jitter: number };
};

// The 4 variant specs from the 2026-05-31 design-exploration workflow.
// Each is a meaningfully-distinct aesthetic interpretation of "literal
// fiber-optic." See agents/_skybox_variants/ (TODO) or the workflow
// transcript for the philosophy notes.
export const VARIANTS: VariantConfig[] = [
  {
    id: "pure_white_bloom",
    display_name: "Pure White Bloom",
    philosophy:
      "Isolate the single hypothesis: is the missing piece JUST a wider, very-faint atmospheric bloom outside the tip? Hold every other V3 variable constant — pure white, no jitter, no color shift, no additive blending — so any improvement is unambiguously attributable to bloom alone.",
    trail_colors: { head_rgb: "255, 255, 255", mid_rgb: "255, 255, 255", tail_rgb: "255, 255, 255" },
    tip_colors: { core_rgb: "255, 255, 255", halo_rgb: "255, 255, 255", bloom_rgb: "255, 255, 255" },
    bloom: {
      core_radius_px: 1.2,
      inner_halo_radius_px: 5,
      outer_bloom_radius_px: 13,
      outer_bloom_alpha_peak: 0.14,
      blend_mode: "source-over",
    },
    trail: { width_head_px: 1.4, width_tail_px: 1.4, alpha_curve: "quadratic", alpha_peak_at_head: 0.85 },
    jitter_per_star: { hue_shift_range_deg: 0, brightness_jitter: 0 },
  },
  {
    id: "cyan_bundle",
    display_name: "Cyan Bundle",
    philosophy:
      "A cool fiber-optic bundle viewed end-on: each strand is its own cyan-blue wavelength, and where strands overlap the light additively compounds into the bright bundle-tip glow of reference image 2. Discipline at the single-star scale, drama at the cluster scale.",
    trail_colors: { head_rgb: "210, 240, 255", mid_rgb: "120, 210, 240", tail_rgb: "100, 220, 200" },
    tip_colors: { core_rgb: "235, 250, 255", halo_rgb: "120, 210, 255", bloom_rgb: "60, 140, 220" },
    bloom: {
      core_radius_px: 1.2,
      inner_halo_radius_px: 4.5,
      outer_bloom_radius_px: 10,
      outer_bloom_alpha_peak: 0.22,
      blend_mode: "lighter",
    },
    trail: { width_head_px: 1.4, width_tail_px: 0.7, alpha_curve: "quadratic", alpha_peak_at_head: 0.85 },
    jitter_per_star: { hue_shift_range_deg: 15, brightness_jitter: 0.15 },
  },
  {
    id: "rainbow_strand",
    display_name: "Rainbow Strand",
    philosophy:
      "A prism dragged through the dark — each strand carries a wavelength traveling from warm tail to cool head, like white light pulled apart across the fiber. Thirty degrees of per-strand hue makes overlapping fibers shimmer additively instead of fusing into uniform white.",
    trail_colors: { head_rgb: "200, 235, 255", mid_rgb: "190, 165, 230", tail_rgb: "255, 165, 130" },
    tip_colors: { core_rgb: "245, 250, 255", halo_rgb: "165, 215, 255", bloom_rgb: "150, 200, 255" },
    bloom: {
      core_radius_px: 1.4,
      inner_halo_radius_px: 6,
      outer_bloom_radius_px: 22,
      outer_bloom_alpha_peak: 0.18,
      blend_mode: "lighter",
    },
    trail: { width_head_px: 1.5, width_tail_px: 0.5, alpha_curve: "quadratic", alpha_peak_at_head: 0.85 },
    jitter_per_star: { hue_shift_range_deg: 30, brightness_jitter: 0.18 },
  },
  {
    id: "cool_plasma",
    display_name: "Cool Plasma",
    philosophy:
      "Cinematic sci-fi fiber-optic: the wire stays disciplined (taper 1.6→0.4px, electric-cyan head fading to deep cobalt tail) while the tip pushes into plasma — near-white-hot pinpoint, saturated electric-blue halo, 15px atmospheric bloom with additive blending so overlapping fibers visibly intensify each other. Dramatic but not gaudy.",
    trail_colors: { head_rgb: "140, 220, 255", mid_rgb: "70, 150, 230", tail_rgb: "30, 70, 170" },
    tip_colors: { core_rgb: "245, 252, 255", halo_rgb: "120, 200, 255", bloom_rgb: "60, 140, 240" },
    bloom: {
      core_radius_px: 1.3,
      inner_halo_radius_px: 5,
      outer_bloom_radius_px: 15,
      outer_bloom_alpha_peak: 0.22,
      blend_mode: "lighter",
    },
    trail: { width_head_px: 1.6, width_tail_px: 0.4, alpha_curve: "quadratic", alpha_peak_at_head: 0.9 },
    jitter_per_star: { hue_shift_range_deg: 5, brightness_jitter: 0.08 },
  },
  {
    id: "pure_white_plasma",
    display_name: "Pure White Plasma",
    philosophy:
      "James's hybrid call (2026-05-31): Pure White Bloom's neutral white color across the entire fiber (no hue, no jitter) wearing Cool Plasma's bloom physics — 15px additive outer bloom, 'lighter' blend so overlapping fibers visibly intensify each other, tapered 1.6→0.4px wire. The white discipline says 'this isn't a color-coded star'; the bloom shape says 'this is bright fiber-optic punctuation.' Named after the midway-switch render state during the V4 A/B that James liked best.",
    trail_colors: { head_rgb: "255, 255, 255", mid_rgb: "255, 255, 255", tail_rgb: "255, 255, 255" },
    tip_colors: { core_rgb: "255, 255, 255", halo_rgb: "255, 255, 255", bloom_rgb: "255, 255, 255" },
    bloom: {
      core_radius_px: 1.3,
      inner_halo_radius_px: 5,
      outer_bloom_radius_px: 15,
      outer_bloom_alpha_peak: 0.22,
      blend_mode: "lighter",
    },
    trail: { width_head_px: 1.6, width_tail_px: 0.4, alpha_curve: "quadratic", alpha_peak_at_head: 0.9 },
    jitter_per_star: { hue_shift_range_deg: 0, brightness_jitter: 0 },
  },
];

export const DEFAULT_VARIANT_ID = "pure_white_plasma";

function getVariant(id: string): VariantConfig {
  return VARIANTS.find((v) => v.id === id) ?? VARIANTS[0];
}

// ── Color helpers (RGB ↔ HSL, hue rotation, alpha curves) ─────────────

function parseRgb(s: string): [number, number, number] {
  const parts = s.split(",").map((p) => parseInt(p.trim(), 10));
  return [parts[0] || 0, parts[1] || 0, parts[2] || 0];
}

function clampByte(n: number): number {
  return Math.max(0, Math.min(255, Math.round(n)));
}

function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
  const rN = r / 255;
  const gN = g / 255;
  const bN = b / 255;
  const max = Math.max(rN, gN, bN);
  const min = Math.min(rN, gN, bN);
  const l = (max + min) / 2;
  let h = 0;
  let s = 0;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === rN) h = ((gN - bN) / d + (gN < bN ? 6 : 0)) * 60;
    else if (max === gN) h = ((bN - rN) / d + 2) * 60;
    else h = ((rN - gN) / d + 4) * 60;
  }
  return [h, s, l];
}

function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  if (s === 0) {
    const v = l * 255;
    return [clampByte(v), clampByte(v), clampByte(v)];
  }
  const hue2rgb = (p: number, q: number, t: number): number => {
    let tt = t;
    if (tt < 0) tt += 1;
    if (tt > 1) tt -= 1;
    if (tt < 1 / 6) return p + (q - p) * 6 * tt;
    if (tt < 1 / 2) return q;
    if (tt < 2 / 3) return p + (q - p) * (2 / 3 - tt) * 6;
    return p;
  };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const hN = (((h % 360) + 360) % 360) / 360;
  const r = hue2rgb(p, q, hN + 1 / 3);
  const g = hue2rgb(p, q, hN);
  const b = hue2rgb(p, q, hN - 1 / 3);
  return [clampByte(r * 255), clampByte(g * 255), clampByte(b * 255)];
}

// Shift the hue of an "R, G, B" string by N degrees + multiply brightness.
// Returns "R, G, B" string. Used at star spawn to bake per-star jitter into
// the precomputed variant colors (no per-frame HSL conversion cost).
function shiftHueAndBrightness(
  rgbStr: string,
  hueShiftDeg: number,
  brightnessMul: number,
): string {
  const [r, g, b] = parseRgb(rgbStr);
  if (hueShiftDeg === 0 && brightnessMul === 1) return rgbStr;
  const [h, s, l] = rgbToHsl(r, g, b);
  const newH = h + hueShiftDeg;
  const newL = Math.max(0, Math.min(1, l * brightnessMul));
  const [r2, g2, b2] = hslToRgb(newH, s, newL);
  return `${r2}, ${g2}, ${b2}`;
}

// Linear interpolation between two "R, G, B" strings.
function lerpRgb(a: string, b: string, t: number): string {
  const [r1, g1, b1] = parseRgb(a);
  const [r2, g2, b2] = parseRgb(b);
  const tt = Math.max(0, Math.min(1, t));
  return `${clampByte(r1 + (r2 - r1) * tt)}, ${clampByte(g1 + (g2 - g1) * tt)}, ${clampByte(b1 + (b2 - b1) * tt)}`;
}

// Per-star variant colors with jitter applied. Computed once at spawn.
function precomputeStarColors(variant: VariantConfig): StarVariantColors {
  const hueRange = variant.jitter_per_star.hue_shift_range_deg;
  const brightRange = variant.jitter_per_star.brightness_jitter;
  const hueShift = hueRange === 0 ? 0 : (Math.random() - 0.5) * hueRange;
  const brightnessMul =
    brightRange === 0 ? 1 : 1 + (Math.random() - 0.5) * brightRange;
  return {
    trail_head: shiftHueAndBrightness(variant.trail_colors.head_rgb, hueShift, brightnessMul),
    trail_mid: shiftHueAndBrightness(variant.trail_colors.mid_rgb, hueShift, brightnessMul),
    trail_tail: shiftHueAndBrightness(variant.trail_colors.tail_rgb, hueShift, brightnessMul),
    tip_core: shiftHueAndBrightness(variant.tip_colors.core_rgb, hueShift, brightnessMul),
    tip_halo: shiftHueAndBrightness(variant.tip_colors.halo_rgb, hueShift, brightnessMul),
    tip_bloom: shiftHueAndBrightness(variant.tip_colors.bloom_rgb, hueShift, brightnessMul),
    brightness_mul: brightnessMul,
  };
}

// Trail alpha curve: t in [0, 1] (0 = tail, 1 = head).
function alphaAt(t: number, curve: "linear" | "quadratic" | "cubic", peak: number): number {
  switch (curve) {
    case "linear":
      return t * peak;
    case "quadratic":
      return t * t * peak;
    case "cubic":
      return t * t * t * peak;
  }
}

// ── Physics + render constants (unchanged from V3 trajectory math) ────

const VX_BASE = 285;
const VX_JITTER = 45;
const VY_INITIAL_BIAS = -6;
const VY_INITIAL_JITTER = 4;
const GRAVITY = 18;
const SPAWN_X_OFFSET = -60;
const SPAWN_Y_TOP_FRAC = 0.04;
const SPAWN_Y_BAND_FRAC = 0.55;

const MAX_STARS = 280;
const DT_CAP = 0.05;

const BOUNCE_X_FRAC = 0.82;
const BOUNCE_X_JITTER_FRAC = 0.04;

const HISTORY_SAMPLE_INTERVAL_S = 0.022;
const TRAIL_DURATION_S = 1.4;
const MAX_HISTORY = Math.ceil(TRAIL_DURATION_S / HISTORY_SAMPLE_INTERVAL_S);

// AT-field shield lifetime. V3.6 (James 2026-05-31): restored from
// V3.4's 0.55s (V3.5 had trimmed it to 0.45s when only the orange
// burst remained — the hexagonal artifact needs the longer hold to
// read as an Eva AT-field rather than a single flash).
const SHIELD_LIFETIME_S = 0.55;

// Red ("rejected") stars stay V3-shape (no variant remix).
const RED_TRAIL_WIDTH = 1.4;
const RED_TIP_CORE_RADIUS = 1.2;
const RED_TIP_GLOW_RADIUS = 5;

export class StarField {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private stars: Star[] = [];
  private rafId: number | null = null;
  private lastTime = 0;
  private dpr = 1;
  private logicalW = 0;
  private logicalH = 0;
  private variantId: string;
  private variant: VariantConfig;

  constructor(canvas: HTMLCanvasElement, variantId: string = DEFAULT_VARIANT_ID) {
    this.canvas = canvas;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("StarField: canvas 2d context unavailable");
    this.ctx = ctx;
    this.variantId = variantId;
    this.variant = getVariant(variantId);
    this.resize();
    window.addEventListener("resize", this.resize);
  }

  start(): void {
    if (this.rafId !== null) return;
    this.lastTime = performance.now();
    this.rafId = requestAnimationFrame(this.tick);
  }

  stop(): void {
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    window.removeEventListener("resize", this.resize);
  }

  /** V4: switch the active aesthetic variant. Future stars use the new
   * spec; in-flight stars keep their precomputed colors so the on-screen
   * transition is gradual (old stars fade out under the new spec). */
  setVariant(variantId: string): void {
    this.variantId = variantId;
    this.variant = getVariant(variantId);
  }

  getVariantId(): string {
    return this.variantId;
  }

  spawn(evt: TrafficEvent): void {
    if (this.stars.length >= MAX_STARS) this.stars.shift();
    const color: StarColor = isRedEvent(evt) ? "red" : "white";
    const spawnY =
      this.logicalH * SPAWN_Y_TOP_FRAC +
      Math.random() * this.logicalH * SPAWN_Y_BAND_FRAC;
    this.stars.push({
      x: SPAWN_X_OFFSET,
      y: spawnY,
      vx: VX_BASE + (Math.random() - 0.5) * VX_JITTER,
      vy: VY_INITIAL_BIAS + (Math.random() - 0.5) * VY_INITIAL_JITTER,
      age: 0,
      color,
      state: "flowing",
      bounceX:
        this.logicalW *
        (BOUNCE_X_FRAC + (Math.random() - 0.5) * BOUNCE_X_JITTER_FRAC),
      impactX: 0,
      impactY: 0,
      impactAge: 0,
      history: [],
      lastSampleAge: 0,
      variantColors: color === "white" ? precomputeStarColors(this.variant) : null,
      evt,
      held: false,
    });
  }

  get count(): number {
    return this.stars.length;
  }

  // ── Catch-a-star (local-workspace payload inspection) ────────────────
  //
  // Only stars whose event carries a payload are catchable — locally
  // that's most of them; flagship stars (contentless by design) fly
  // through the cursor untouched. The hit-test runs per FRAME against
  // the last pointer position, not per mousemove — so a star that
  // flies INTO a resting cursor stops too ("see it stop"), not just
  // one the cursor chases down. Catching freezes the star mid-flight;
  // moving away releases it back into its arc.

  private static readonly HIT_RADIUS_PX = 22;
  private pointer: StarPoint | null = null;
  private heldStar: Star | null = null;
  private holdListener: ((info: HeldStarInfo | null) => void) | null = null;

  /** The popup layer's subscription: fires with the caught star's
   *  payload + frozen position when a catch happens, null on release. */
  setHoldListener(cb: ((info: HeldStarInfo | null) => void) | null): void {
    this.holdListener = cb;
  }

  /** Track the pointer in CSS-pixel canvas coordinates. */
  setPointer(x: number, y: number): void {
    this.pointer = { x, y };
  }

  /** Pointer left the sky — release any catch and stop testing. */
  clearPointer(): void {
    this.pointer = null;
    this.releaseHold();
  }

  /** Let every held star fly again. Notifies the listener. */
  releaseHold(): void {
    for (const s of this.stars) s.held = false;
    if (this.heldStar !== null) {
      this.heldStar = null;
      this.holdListener?.(null);
    }
  }

  private updateHold(): void {
    if (this.pointer === null) return;
    let best: Star | null = null;
    let bestD2 = StarField.HIT_RADIUS_PX * StarField.HIT_RADIUS_PX;
    for (const s of this.stars) {
      if (!s.evt.detail && !s.evt.label) continue;
      const dx = s.x - this.pointer.x;
      const dy = s.y - this.pointer.y;
      const d2 = dx * dx + dy * dy;
      if (d2 <= bestD2) {
        best = s;
        bestD2 = d2;
      }
    }
    if (best === this.heldStar) return;  // steady state — no re-notify
    if (best === null) {
      this.releaseHold();
      return;
    }
    for (const s of this.stars) s.held = s === best;
    this.heldStar = best;
    this.holdListener?.({ evt: best.evt, x: best.x, y: best.y });
  }

  private resize = (): void => {
    this.dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.floor(rect.width * this.dpr));
    this.canvas.height = Math.max(1, Math.floor(rect.height * this.dpr));
    this.logicalW = rect.width;
    this.logicalH = rect.height;
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.scale(this.dpr, this.dpr);
  };

  private tick = (now: number): void => {
    const dt = Math.min(DT_CAP, (now - this.lastTime) / 1000);
    this.lastTime = now;

    this.ctx.clearRect(0, 0, this.logicalW, this.logicalH);

    for (let i = this.stars.length - 1; i >= 0; i--) {
      const s = this.stars[i];

      // A held star is frozen mid-flight — position, velocity, and age
      // all pause so the trail + tip render exactly as caught, and the
      // star resumes its arc from the same instant on release.
      if (!s.held) {
        s.x += s.vx * dt;
        s.vy += GRAVITY * dt;
        s.y += s.vy * dt;
        s.age += dt;
      }

      if (s.state === "flowing" && s.color === "red" && s.x >= s.bounceX) {
        s.state = "bouncing";
        s.impactX = s.x;
        s.impactY = s.y;
        s.impactAge = 0;
        s.vx = -s.vx;
      }
      if (s.state === "bouncing" && !s.held) {
        s.impactAge += dt;
      }

      if (s.age - s.lastSampleAge >= HISTORY_SAMPLE_INTERVAL_S) {
        s.history.push({ x: s.x, y: s.y });
        if (s.history.length > MAX_HISTORY) s.history.shift();
        s.lastSampleAge = s.age;
      }

      if (
        s.x > this.logicalW + 60 ||
        s.x < -120 ||
        s.y > this.logicalH + 60
      ) {
        if (s === this.heldStar) this.releaseHold();
        this.stars.splice(i, 1);
        continue;
      }

      if (s.color === "white" && s.variantColors) {
        this.drawTrailVariant(s, s.variantColors);
        this.drawTipVariant(s, s.variantColors);
      } else {
        this.drawTrailRed(s);
        this.drawTipRed(s);
      }
      if (s.state === "bouncing" && s.impactAge < SHIELD_LIFETIME_S) {
        this.drawShield(s);
      }
    }

    this.updateHold();

    this.rafId = requestAnimationFrame(this.tick);
  };

  // ── White star rendering (variant-aware) ────────────────────────────

  private drawTrailVariant(s: Star, c: StarVariantColors): void {
    if (s.history.length < 2) return;
    const ctx = this.ctx;
    const variant = this.variant;
    const widthHead = variant.trail.width_head_px;
    const widthTail = variant.trail.width_tail_px;
    const curve = variant.trail.alpha_curve;
    const peak = variant.trail.alpha_peak_at_head;

    const pts: StarPoint[] = [];
    for (const p of s.history) pts.push(p);
    pts.push({ x: s.x, y: s.y });
    const n = pts.length;

    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    for (let i = 1; i < n; i++) {
      const t = i / (n - 1); // 0 at tail, 1 at head
      const alpha = alphaAt(t, curve, peak);
      const width = widthTail + (widthHead - widthTail) * t;
      // Color: lerp tail→mid for first half, mid→head for second.
      const segColor =
        t <= 0.5 ? lerpRgb(c.trail_tail, c.trail_mid, t * 2) : lerpRgb(c.trail_mid, c.trail_head, (t - 0.5) * 2);
      ctx.strokeStyle = `rgba(${segColor}, ${alpha})`;
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(pts[i - 1].x, pts[i - 1].y);
      ctx.lineTo(pts[i].x, pts[i].y);
      ctx.stroke();
    }
  }

  private drawTipVariant(s: Star, c: StarVariantColors): void {
    const ctx = this.ctx;
    const variant = this.variant;
    const bloom = variant.bloom;
    const prevComposite = ctx.globalCompositeOperation;

    // 3 layers, painted outer → inner so the pinpoint stays crisp on top.
    // The OUTER bloom + INNER halo use the variant's blend mode (so e.g.
    // "lighter" makes overlapping bundles additively brighten). The CORE
    // pinpoint always uses source-over so it stays sharp on top.

    // 1. Outer atmospheric bloom (V4's added piece — the "missing bloom").
    ctx.globalCompositeOperation = bloom.blend_mode;
    const outer = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, bloom.outer_bloom_radius_px);
    outer.addColorStop(0, `rgba(${c.tip_bloom}, ${bloom.outer_bloom_alpha_peak})`);
    outer.addColorStop(0.55, `rgba(${c.tip_bloom}, ${bloom.outer_bloom_alpha_peak * 0.35})`);
    outer.addColorStop(1, `rgba(${c.tip_bloom}, 0)`);
    ctx.fillStyle = outer;
    ctx.beginPath();
    ctx.arc(s.x, s.y, bloom.outer_bloom_radius_px, 0, Math.PI * 2);
    ctx.fill();

    // 2. Inner tight halo (V3's original 5px-ish halo).
    const inner = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, bloom.inner_halo_radius_px);
    inner.addColorStop(0, `rgba(${c.tip_halo}, 0.55)`);
    inner.addColorStop(0.55, `rgba(${c.tip_halo}, 0.18)`);
    inner.addColorStop(1, `rgba(${c.tip_halo}, 0)`);
    ctx.fillStyle = inner;
    ctx.beginPath();
    ctx.arc(s.x, s.y, bloom.inner_halo_radius_px, 0, Math.PI * 2);
    ctx.fill();

    // 3. Pinpoint core (source-over for crisp on-top).
    ctx.globalCompositeOperation = "source-over";
    const core = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, bloom.core_radius_px);
    core.addColorStop(0, `rgba(${c.tip_core}, 1)`);
    core.addColorStop(0.5, `rgba(${c.tip_core}, 0.9)`);
    core.addColorStop(1, `rgba(${c.tip_core}, 0)`);
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(s.x, s.y, bloom.core_radius_px, 0, Math.PI * 2);
    ctx.fill();

    ctx.globalCompositeOperation = prevComposite;
  }

  // ── Red star rendering (V3-shape, unchanged) ────────────────────────

  private drawTrailRed(s: Star): void {
    if (s.history.length < 2) return;
    const ctx = this.ctx;
    const colorTriplet = "255, 110, 110";

    const pts: StarPoint[] = [];
    for (const p of s.history) pts.push(p);
    pts.push({ x: s.x, y: s.y });
    const n = pts.length;

    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.lineWidth = RED_TRAIL_WIDTH;

    for (let i = 1; i < n; i++) {
      const t = i / (n - 1);
      const alpha = t * t * 0.85;
      ctx.strokeStyle = `rgba(${colorTriplet}, ${alpha})`;
      ctx.beginPath();
      ctx.moveTo(pts[i - 1].x, pts[i - 1].y);
      ctx.lineTo(pts[i].x, pts[i].y);
      ctx.stroke();
    }
  }

  private drawTipRed(s: Star): void {
    const ctx = this.ctx;
    const colorTriplet = "255, 130, 130";

    const glow = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, RED_TIP_GLOW_RADIUS);
    glow.addColorStop(0, `rgba(${colorTriplet}, 0.55)`);
    glow.addColorStop(0.55, `rgba(${colorTriplet}, 0.18)`);
    glow.addColorStop(1, `rgba(${colorTriplet}, 0)`);
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(s.x, s.y, RED_TIP_GLOW_RADIUS, 0, Math.PI * 2);
    ctx.fill();

    const core = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, RED_TIP_CORE_RADIUS);
    core.addColorStop(0, `rgba(255, 255, 255, 1)`);
    core.addColorStop(0.5, `rgba(255, 255, 255, 0.9)`);
    core.addColorStop(1, `rgba(255, 255, 255, 0)`);
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(s.x, s.y, RED_TIP_CORE_RADIUS, 0, Math.PI * 2);
    ctx.fill();
  }

  /**
   * Draw the AT-field shield artifact at the bot's impact point.
   *
   * Reference: End of Evangelion's Lance of Longinus / AT-field aesthetic.
   * Five concentric hexagons rotated relative to one another for a spiral-
   * inward feel, bright orange-amber edges with shadowBlur halos, and a
   * central radial glow at the impact origin.
   *
   * V3.6 (James 2026-05-31): restored the V3.4 hexagonal complex. V3.5
   * over-corrected James's intricacy/color critique as "remove the shape
   * entirely"; the actual ask was "same Eva AT-field shape, orange
   * instead of violet." This version is V3.4 with a violet→orange
   * palette swap — geometry, motion, and lifecycle unchanged.
   *
   * Three-phase life cycle over SHIELD_LIFETIME_S:
   *   0.00–0.18: ease-out scale-in + alpha-in (artifact materializes)
   *   0.18–0.55: hold at full visibility with continuous slow rotation
   *   0.55–1.00: slight expand + alpha fade (artifact dissolves)
   *
   * The orange impact burst (first ~32% of lifetime) layers underneath
   * the persistent hexagonal artifact — V3.4's original "burst as
   * impact, field as persistent shield" pattern.
   */
  private drawShield(s: Star): void {
    const ctx = this.ctx;
    const t = s.impactAge / SHIELD_LIFETIME_S;

    let scale: number;
    let alpha: number;
    if (t < 0.18) {
      const u = t / 0.18;
      const eased = 1 - Math.pow(1 - u, 3);
      scale = eased;
      alpha = eased;
    } else if (t < 0.55) {
      scale = 1;
      alpha = 1;
    } else {
      const u = (t - 0.55) / 0.45;
      scale = 1 + u * 0.22;
      alpha = 1 - u;
    }

    ctx.save();
    ctx.translate(s.impactX, s.impactY);

    // Orange impact burst: two expanding rings + bright central flash,
    // all gone by ~32% of the shield's life so the hexagonal artifact
    // dominates the persistent phase.
    if (t < 0.32) {
      const u = t / 0.32;
      for (let ring = 0; ring < 2; ring++) {
        const ringT = Math.max(0, u - ring * 0.18);
        if (ringT > 0 && ringT < 1) {
          const ringR = ringT * 28;
          const ringAlpha = (1 - ringT) * 0.55;
          const ringGrad = ctx.createRadialGradient(
            0, 0, Math.max(0, ringR * 0.72),
            0, 0, ringR,
          );
          ringGrad.addColorStop(0, "rgba(255, 140, 60, 0)");
          ringGrad.addColorStop(0.65, `rgba(255, 175, 85, ${ringAlpha})`);
          ringGrad.addColorStop(1, "rgba(255, 100, 40, 0)");
          ctx.fillStyle = ringGrad;
          ctx.beginPath();
          ctx.arc(0, 0, ringR, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      const flashAlpha = (1 - u) * 0.85;
      const flashR = 7 + u * 6;
      const flashGrad = ctx.createRadialGradient(0, 0, 0, 0, 0, flashR);
      flashGrad.addColorStop(0, `rgba(255, 230, 150, ${flashAlpha})`);
      flashGrad.addColorStop(0.4, `rgba(255, 160, 60, ${flashAlpha * 0.6})`);
      flashGrad.addColorStop(1, "rgba(255, 100, 30, 0)");
      ctx.fillStyle = flashGrad;
      ctx.beginPath();
      ctx.arc(0, 0, flashR, 0, Math.PI * 2);
      ctx.fill();
    }

    // Continuous slow rotation — keeps the artifact alive, never static.
    ctx.rotate(s.impactAge * 0.9);

    // Warm-orange halo around all hex strokes — the "glowing edges" of
    // the Eva AT-field, recolored from V3.4's violet.
    ctx.shadowColor = "rgba(255, 150, 70, 0.85)";
    ctx.shadowBlur = 4;

    // V3.7 (James 2026-05-31): all geometry halved from V3.6 — same
    // shape + motion + lifecycle, just a tighter footprint so the
    // shield reads as punctuation rather than dominating the bounce.
    const HEX_COUNT = 5;
    const HEX_BASE_RADII = [21, 16, 12, 8.5, 5.5];
    const HEX_ROTATIONS = [
      0,
      Math.PI / 9,
      -Math.PI / 9,
      Math.PI / 15,
      -Math.PI / 12,
    ];
    const HEX_LINE_WIDTHS = [1.3, 1.1, 0.9, 0.8, 0.7];
    const HEX_BRIGHTNESS = [0.6, 0.72, 0.85, 0.95, 1.0];

    for (let i = 0; i < HEX_COUNT; i++) {
      const r = HEX_BASE_RADII[i] * scale;
      ctx.save();
      ctx.rotate(HEX_ROTATIONS[i]);
      ctx.strokeStyle = `rgba(255, 210, 140, ${alpha * HEX_BRIGHTNESS[i] * 0.92})`;
      ctx.lineWidth = HEX_LINE_WIDTHS[i];

      ctx.beginPath();
      for (let k = 0; k < 6; k++) {
        const ang = (k / 6) * Math.PI * 2 - Math.PI / 2; // first vertex at top
        const px = Math.cos(ang) * r;
        const py = Math.sin(ang) * r;
        if (k === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      ctx.closePath();
      ctx.stroke();
      ctx.restore();
    }

    // Central bright glow — the impact origin, where the Lance would
    // strike. White-warm core fading into the shield's orange.
    ctx.shadowBlur = 0;
    const glowR = 4.5 * scale;
    const glow = ctx.createRadialGradient(0, 0, 0, 0, 0, glowR);
    glow.addColorStop(0, `rgba(255, 245, 220, ${alpha})`);
    glow.addColorStop(0.4, `rgba(255, 200, 130, ${alpha * 0.7})`);
    glow.addColorStop(1, `rgba(255, 130, 50, 0)`);
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(0, 0, glowR, 0, Math.PI * 2);
    ctx.fill();

    ctx.restore();
  }
}

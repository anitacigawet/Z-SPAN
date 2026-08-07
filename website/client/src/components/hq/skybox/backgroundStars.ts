/**
 * One-shot procedural background star field for the skybox.
 *
 * Drawn once on mount + on resize — these stars are static depth, not
 * the shooting-star foreground (which the StarField canvas accumulates).
 * The placement is seeded deterministically from the canvas size so the
 * field stays stable across resizes/HMR within a session.
 *
 * Density is low enough not to compete with the fiber-optic shooting
 * stars; brightness has long-tail variation (most stars dim, a few
 * brighter with a small halo) so the field reads as real sky, not as
 * uniform noise.
 *
 * Stars populate only the upper ~85% of the sky — the lower band blends
 * into the HQ horizon haze without competing with it.
 */
export function drawBackgroundStars(
 ctx: CanvasRenderingContext2D,
 width: number,
 height: number,
): void {
 // Deterministic seed so the same field renders each load/resize within
 // a session (avoids the visually-jarring "field re-randomizes on every
 // window resize" issue).
 let seed = Math.floor(width * 73 + height * 19 + 12345);
 const rand = (): number => {
 // LCG — Numerical Recipes constants. Good enough for visual seeding.
 seed = (seed * 1664525 + 1013904223) | 0;
 return ((seed >>> 0) % 1000000) / 1000000;
 };

 // ~1 star per 4500 px² → e.g. ~170 stars on 1280×600. Low enough that
 // bright shooting stars dominate, dense enough to read as "sky."
 const count = Math.max(40, Math.floor((width * height) / 4500));

 ctx.globalCompositeOperation = "source-over";

 for (let i = 0; i < count; i++) {
 const x = rand() * width;
 // Upper 85% only — lower band is reserved for the horizon haze.
 const y = rand() * height * 0.85;

 // Long-tail brightness: most dim, occasional bright.
 const r = rand();
 const radius = 0.3 + r * r * 1.1; // r² → mostly small, occasional larger
 const brightness = 0.14 + rand() * 0.55;

 // Faint cool tint for variety — most stars are pure white with a slight
 // blue-shift, a few have a warmer tint.
 const warmth = rand();
 const colorR = warmth < 0.2 ? 255 : 230 + Math.floor(rand() * 25);
 const colorG = warmth < 0.2 ? 240 + Math.floor(rand() * 15) : 240;
 const colorB = 255;

 // Small halo on the brightest few — gives some sparkle without
 // turning the field into a galaxy poster.
 if (radius > 1.0 && brightness > 0.55) {
 const haloR = radius * 3.5;
 const halo = ctx.createRadialGradient(x, y, 0, x, y, haloR);
 halo.addColorStop(
 0,
 `rgba(${colorR}, ${colorG}, ${colorB}, ${brightness * 0.28})`,
 );
 halo.addColorStop(
 1,
 `rgba(${colorR}, ${colorG}, ${colorB}, 0)`,
 );
 ctx.fillStyle = halo;
 ctx.beginPath();
 ctx.arc(x, y, haloR, 0, Math.PI * 2);
 ctx.fill();
 }

 ctx.fillStyle = `rgba(${colorR}, ${colorG}, ${colorB}, ${brightness})`;
 ctx.beginPath();
 ctx.arc(x, y, radius, 0, Math.PI * 2);
 ctx.fill();
 }
}

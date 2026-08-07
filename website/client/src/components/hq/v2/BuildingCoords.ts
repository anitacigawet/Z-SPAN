import type { CSSProperties } from "react";

// V2 building coordinate system — single source of truth for the layer
// components shipping in V2-5 → V2-14. Mirrors the tier breakdown in
// HQ_V2_BACKGROUND_REBUILD_PLAN.md § 3 (the stepped silhouette diagram).
//
// All values are percentages of the V2 .scene container. For visual parity
// with V1 we inherit the 1376×768 aspect ratio (HQPage.tsx COORDS uses the
// same baseline). V2-17 (graduation) can shift to a more responsive layout
// once the layer pieces prove out.
//
// Convention: top = distance from top edge (smaller = higher up); left =
// distance from left edge; width/height in viewport % too. The y-axis grows
// downward, matching CSS. Building tiers run TOP-DOWN (tier 0 = roof).

export interface Rect {
 top: number;
 left: number;
 width: number;
 height: number;
}

/**
 * Tier envelopes — the outer rectangle each structural tier occupies on the
 * building. Stepped silhouette: penthouse is narrowest, ground floor is
 * widest. Billboard bands (tier 2.5 + 3.5) span WIDER than the tier above/
 * below them since they mount across the building's face.
 *
 * These envelopes are the parameters the V2-6 BuildingSilhouette path uses
 * to compute its polygon outline, and the V2-8 TierWindows grids use to
 * lay out their cells. V2-15 (overlay re-target) snaps V1 overlays onto
 * coords derived from these tiers.
 */
export const TIERS = {
 // Tier 0 — ROOF — Z-SPAN sign + antenna + beacon
 roof: { top: 6.5, left: 39.0, width: 22.0, height: 8.5 } satisfies Rect,
 // Tier 1 — PENTHOUSE — PIPELINE OPS dept; narrowest tower section
 penthouse: { top: 14.0, left: 41.5, width: 17.0, height: 11.5 } satisfies Rect,
 // Tier 2 — UPPER TOWER — VOCAB · DISPUTES · VERIFY band
 upper: { top: 25.0, left: 32.0, width: 36.0, height: 22.5 } satisfies Rect,
 // Tier 2.5 — UPPER BILLBOARD band — wider than tier 2/3 (spans the face)
 upperBillboard: { top: 47.5, left: 19.5, width: 60.0, height: 6.5 } satisfies Rect,
 // Tier 3 — MID TOWER — INGEST band; narrower than the billboards above/below
 mid: { top: 53.5, left: 35.0, width: 30.0, height: 7.5 } satisfies Rect,
 // Tier 3.5 — LOWER BILLBOARD band
 lowerBillboard: { top: 61.5, left: 19.5, width: 60.0, height: 6.5 } satisfies Rect,
 // Tier 4 — GROUND FLOOR — 4 dept zones + entrance (widest tier of the
 // building proper); SCOUT WHISPER RAG PARSER + ENTRANCE
 ground: { top: 67.5, left: 14.0, width: 72.0, height: 19.5 } satisfies Rect,
 // Tier 5 — PLAZA — generator outbuilding (left) + press vignette (right)
 // + foreground street/people. Spans full width since outbuildings hang
 // off the building proper.
 plaza: { top: 87.0, left: 0.0, width: 100.0, height: 13.0 } satisfies Rect,
} as const;

export type TierKey = keyof typeof TIERS;

/**
 * Dept-zone rectangles within tiers — mirror V1's DEPT_ZONES (HQPage.tsx)
 * so the V2 re-target in V2-15 doesn't have to relearn where each agent
 * lives in the building. Names use camelCase here for export-friendliness;
 * V1's DeptZoneSpec uses kebab-case `deptId` — see V2-15 for the mapping.
 */
// PLACEMENT GOTCHA (2026-07-02): all rects here are percentages of the
// .scene box, and the scene is HEIGHT-fitted and centered — at a 1440px
// viewport it renders ~1083px wide with a ~178px left offset. Overlay
// percentages are NOT viewport fractions; computing a rect from screen
// pixels requires solving the scene box first (two observed positions
// suffice). The firework barrel landed inside the tower twice before
// this was understood — see commit cd1f5b9's war story.
export const DEPT_RECTS = {
 // left 42.5→45.0 / width 15→12.5 (2026-07-02): the open dept box poked
 // past the penthouse's left silhouette into the sky.
 pipelineOperator: { top: 15.5, left: 45.0, width: 12.5, height: 9.0 } satisfies Rect,
 vocabularyCurator: { top: 34.0, left: 36.0, width: 7.0, height: 11.0 } satisfies Rect,
 disputedQuotesReviewer: { top: 27.5, left: 42.0, width: 16.0, height: 16.5 } satisfies Rect,
 verification: { top: 34.0, left: 57.0, width: 7.0, height: 11.0 } satisfies Rect,
 ingestion: { top: 54.5, left: 38.0, width: 24.0, height: 7.0 } satisfies Rect,
 contentScout: { top: 69.5, left: 24.0, width: 13.0, height: 15.5 } satisfies Rect,
 transcription: { top: 69.5, left: 37.5, width: 11.5, height: 15.5 } satisfies Rect,
 // (was notebooklmBridge — renamed with the dept retirement, 2026-07-02)
 synthesis: { top: 69.5, left: 50.5, width: 11.5, height: 15.5 } satisfies Rect,
 parserCustodian: { top: 69.5, left: 63.0, width: 13.0, height: 15.5 } satisfies Rect,
} as const;

/**
 * Standalone overlay rects mirroring V1's COORDS — V2-15 re-targets each
 * V1 overlay component (Billboard, Entrance, PressScreen, InfraPanel) at
 * these coords once the V2 layers are in place.
 *
 * 2026-07-03 re-solve: rects below were re-measured against the operator's
 * full-scene repaint (zspan-hq-scene.webp, 2750x1536 — exactly 2x the old
 * canvas, same layout). Grid-overlaid crops of the painting gave the pixel
 * bounds; percentages are px/2750 and px/1536. The painting reproduces the
 * old page layout so closely that DEPT_RECTS and TIERS needed no changes.
 */
export const OVERLAYS = {
 // Painted band housings run x550..2227/y734..863 (upper) and
 // x525..2238/y939..1076 (lower) — both taller + wider than the old
 // composite's bands, so the HTML tickers grow to cover them.
 upperBand: { top: 47.8, left: 20.0, width: 61.0, height: 8.4 } satisfies Rect,
 lowerBand: { top: 61.1, left: 19.1, width: 62.3, height: 8.9 } satisfies Rect,
 // Painted glass storefront x1237..1556 (door pair 1340..1450).
 entrance: { top: 86.6, left: 45.0, width: 11.6, height: 11.7 } satisfies Rect,
 // The painted smartboard incl. frame (x110..455, y1207..1451). Since the
 // repaint includes the board's broadcast content + podium speaker as art,
 // this rect is now the HOVER TRIGGER for PressBoardHover (V2) — the V1
 // flat-panel PressScreen no longer mounts in V2.
 pressScreen: { top: 78.6, left: 4.0, width: 12.5, height: 15.9 } satisfies Rect,
 // Unmounted in V2 since 2026-07-03 — the repaint paints the podium
 // speaker. Rect kept for V1 parity + potential remount.
 podium: { top: 85.0, left: 7.0, width: 6.5, height: 7.0 } satisfies Rect,
 infraPanel: { top: 70.0, left: 82.5, width: 17.0, height: 21.5 } satisfies Rect,
 subwayEntrance: { top: 84.5, left: 62.0, width: 14.5, height: 13.5 } satisfies Rect,
 // July-4 firework barrel — the painted scene's clear strip is the gap
 // between the smartboard's right edge (x455) and the tower's left wall
 // (x555); barrel base lands on the sidewalk line above the parked car.
 fireworkBarrel: { top: 52.0, left: 17.45, width: 1.85, height: 44.3 } satisfies Rect,
} as const;

/**
 * Building bounding rect — the outermost silhouette envelope. The V2-6
 * BuildingSilhouette path lives inside this. Used by V2-5 Mountains to
 * decide how far behind to render the desert ranges, and by V2-7
 * BuildingDepth to scope the ledge highlights.
 */
export const BUILDING_BOUNDS: Rect = {
 top: 6.5,
 left: 12.0,
 width: 76.0,
 height: 80.5,
};

/**
 * Horizon line — where the building meets the ground. Mountains (V2-5)
 * silhouette behind the building above this line; plaza (V2-14) elements
 * sit below it. Same percent-of-scene-height convention as the rects above.
 */
export const HORIZON_PCT = 87.0;

/**
 * Helper — convert a Rect to a `style` object for absolute positioning
 * against the V2 .scene container. Same pattern as V1's overlay components
 * (Billboard, Entrance, etc.).
 */
export function rectToStyle(rect: Rect): CSSProperties {
 return {
 position: "absolute",
 top: `${rect.top}%`,
 left: `${rect.left}%`,
 width: `${rect.width}%`,
 height: `${rect.height}%`,
 };
}

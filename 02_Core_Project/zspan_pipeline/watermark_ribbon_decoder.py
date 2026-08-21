"""
S-098 Phase 2 V0 + 2.5 — visible provenance ribbon decoder.

Given an image containing a Z-SPAN watermark ribbon (20 colored blocks
× 2 bits each, 4 brand colors: civic-blue / alert-red / success-green /
highway-amber), find the ribbon + recover the 40-bit token + base32-
encode + chain to `/api/watermark-lookup`.

Two anchor mechanisms (in order):

  1. **Frame-shape anchor (Phase 2.5)** — `_find_ribbon_frame_cv`
     uses OpenCV Canny + contour finding to locate the ribbon's
     rounded-rectangle outline directly. Filters candidates by
     aspect ratio (~8.6:1 wide horizontal rectangle) and verifies
     the left edge carries microtext-shaped content. Once a frame
     is found, the inner colored-block strip is sampled at known
     geometric ratios within the frame. **Robust against amber/gold
     frame border** (the saturation anchor isn't).
  2. **Saturation-cluster anchor (Phase 2 V0)** — `_find_ribbon_strip`
     scans HSV-saturated rows for the densest horizontal cluster.
     Fast and dependency-free; works when the frame border is
     low-saturation white or absent. Used as the fallback when
     the frame anchor doesn't find a candidate.

Decoding (after either anchor locates the block strip):
  - Sample each of the 20 block positions for its dominant RGB color
    (median of the central 40% of each block).
  - Classify each sample to its nearest brand palette entry → 2 bits.
  - Pack 40 bits MSB-first → 8 base32 chars → token.

The 4-color palette has perceptual distance vastly higher than the
font-watermark Inter↔IBM Plex Sans pair scrapped in Phase 1, so simple
nearest-color classification works without ML training. This is by
design — the visible ribbon trades the invisibility property for
tractable decoding + visible-defense signal (per the session-13/14
hidden-perfect-security-backfires-on-false-flag finding).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image

try:  # cv2 is required for the frame-shape anchor; saturation fallback works without
    import cv2  # type: ignore
    import numpy as np  # type: ignore
    _HAVE_CV2 = True
except ImportError:  # pragma: no cover — graceful degradation if cv2 missing
    _HAVE_CV2 = False

logger = logging.getLogger(__name__)

# Mirrors WatermarkRibbon.tsx PALETTE. RGB tuples for nearest-color
# classification. The 2-bit code is the index in this list.
BRAND_PALETTE = [
    (0x1A, 0x3A, 0x7C),  # 0b00 civic-blue (deep)
    (0xEF, 0x44, 0x44),  # 0b01 alert-red (was highway-sign-blue; swapped for
                         #      contrast — see WatermarkRibbon.tsx palette
                         #      note for the rationale)
    (0x22, 0xC5, 0x5E),  # 0b10 success-green
    (0xF5, 0xA5, 0x24),  # 0b11 highway-amber
]
BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

# Geometric ratios of the inner colored-block strip WITHIN the SVG frame.
# Computed from WatermarkRibbon.tsx constants:
#   FRAME_PADDING_X=4, FRAME_PADDING_Y=3, FRAME_LABEL_FONT_SIZE=6,
#   FRAME_LABEL="zspan.org/scan" (14 chars, S-102 sticker-URL),
#   labelWidth = 14 * 6 * 0.62 + 10 = 62.08px (the +10 is the
#       label-to-strip margin so microtext doesn't touch the first block),
#   inner block strip = 20 blocks × (5+2 gap) - 2 = 138px wide × 14px tall,
#   frame total ≈ 208.08 × 20 px.
# Inner strip starts at x≈66.08/208.08 = 31.8% from left; runs to 98.1%.
# Inner strip starts at y=3/20 = 15% from top; runs to 85%.
# **THESE RATIOS ARE COUPLED TO `FRAME_LABEL` IN WatermarkRibbon.tsx —
#  any change to the microtext string length REBREAKS THE DECODER.**
# S-102 (2026-06-30) post-mortem: the "Z-SPAN" → "zspan.org/scan" swap
# silently broke camera-frame + upload decoding because the old ratios
# (0.204, 0.978) were calibrated for the 6-char label. iPhone batch
# capture caught 15/15 frames returning honest-empty "no ribbon found"
# despite the frame-anchor detecting the ribbon shape on every frame —
# the geometric inner-strip extraction was sampling between microtext
# and first block + then drifting progressively past real block
# positions. Sibling defense added below (column-saturation scan
# refinement) so future label changes degrade rather than break.
INNER_X_START_RATIO = 0.318
INNER_X_END_RATIO = 0.981
INNER_Y_START_RATIO = 0.15
INNER_Y_END_RATIO = 0.85

# Aspect ratio target for candidate ranking. The empirical aspect of
# captured ribbons clusters around 8-10 even though the SVG geometry
# is 10.4 (the OpenCV contour detector often latches onto the block
# strip's inner edges, which give a tighter rectangle than the full
# frame). Keeping the historical 8.92 target works fine in practice —
# all candidates with aspect 5-18 are considered + the verify-ribbon-
# interior + 20-block-strip-extraction filter selects the right one.
FRAME_ASPECT_TARGET = 8.92
FRAME_ASPECT_MIN = 5.0
FRAME_ASPECT_MAX = 18.0

# Safety: max allowed squared RGB distance from a sampled block to its
# nearest palette color. Calibration:
#   - Smallest pairwise palette distance² is amber↔red ≈ 10,533, so a
#     sample exactly between two palette colors would be ~2,600 away
#     from each — well below this threshold.
#   - Anti-aliased / JPEG-noisy palette pixels measure ≤ ~2,500 in
#     practice.
#   - Black-pixel-against-amber is ~88,500; dark-UI-gray-against-blue
#     is ~19,000; white-against-amber is ~56,000 — all way above.
# Anything above this threshold signals "this block wasn't sampled
# from a real palette color" → fail-loud (return token=None) rather
# than silently round to a nearest palette and ship a wrong token.
MAX_BLOCK_DIST_SQ = 4_000


def _rgb_dist(a: tuple, b: tuple) -> float:
    """Squared Euclidean distance in RGB. Good enough for our 4-color
    palette where the hues are perceptually distant."""
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _classify_color(rgb: tuple) -> int:
    """Map an observed RGB tuple to the nearest brand-palette index (2-bit value)."""
    return min(range(len(BRAND_PALETTE)), key=lambda i: _rgb_dist(rgb, BRAND_PALETTE[i]))


# S-102 post-iPhone-batch-test additions — phone cameras overexpose
# saturated colors + auto-white-balance against laptop backlight,
# shifting the captured RGB far from the canonical palette even when
# the underlying hue is right. The 4 brand-palette colors are ~90°
# apart in hue (0° red / 37° amber / 142° green / 220° blue), so
# hue-distance classification survives the camera shift cleanly.
import colorsys

# Pre-compute palette hues in 0-360° space + saturation-as-HLS-S so
# the per-sample classifier doesn't repeat the conversion.
_PALETTE_HUES: list[float] = []
for _r, _g, _b in BRAND_PALETTE:
    _h, _l, _s = colorsys.rgb_to_hls(_r / 255.0, _g / 255.0, _b / 255.0)
    _PALETTE_HUES.append(_h * 360.0)

# Tolerance for "this hue matches palette color N." Calibrated against
# the iPhone 11 Pro batch (dbg-mr0qphhr): captures showed hues at
# canonical-palette-color ± 30-40° due to camera white-balance + JPEG
# ringing on saturated colors. 40° tolerance recovers 13/15 of that
# batch while staying well below the half-gap-to-ambiguity threshold
# (the smallest pairwise palette-hue gap is amber↔red at 37°, so any
# threshold ≥ 18.5° has unique-nearest determinism for clean samples;
# 40° absorbs the camera shift without crossing into ambiguous-
# classification territory).
MAX_HUE_DISTANCE_DEG = 40.0

# Saturation/value floor for "this sample is a real palette block."
# Pure grayscale UI background passes any hue (hue is undefined for
# achromatic samples + defaults to 0); the saturation gate rejects
# those without depending on color-shift-sensitive RGB distance.
MIN_SATURATION_FOR_CLASSIFY = 0.20
MIN_VALUE_FOR_CLASSIFY = 0.15


def _hue_distance(a_deg: float, b_deg: float) -> float:
    """Shortest distance between two hues on the 0-360° circle."""
    d = abs(a_deg - b_deg) % 360.0
    return d if d <= 180.0 else 360.0 - d


def _classify_by_hue(rgb: tuple) -> tuple[int, float, float, float]:
    """Return (palette_index, hue_distance_to_nearest, saturation, value).
    Caller decides whether to accept based on the distance + sat/value
    floors above."""
    h, l, s = colorsys.rgb_to_hls(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    sample_hue = h * 360.0
    # HLS lightness ≠ HSV value; both work as "is this near-black" signals
    # but for the achromatic-rejection check the HLS lightness is what
    # corresponds to brightness perceived. Use lightness as the "value"
    # floor.
    distances = [_hue_distance(sample_hue, ph) for ph in _PALETTE_HUES]
    best = min(range(len(_PALETTE_HUES)), key=lambda i: distances[i])
    return best, distances[best], s, l


def _bits_to_token(bits: list[int]) -> str:
    """Pack 40 bits into 8 base32 chars."""
    if len(bits) != 40:
        raise ValueError(f"expected 40 bits; got {len(bits)}")
    result = []
    for i in range(0, 40, 5):
        v = 0
        for b in range(5):
            v = (v << 1) | bits[i + b]
        result.append(BASE32_ALPHABET[v])
    return "".join(result)


# ── Ribbon detection ────────────────────────────────────────────────────────


def _find_ribbon_frame_cv(img_rgb) -> tuple[int, int, int, int] | None:
    """Phase 2.5 primary anchor: OpenCV edge + contour detection of the
    ribbon's rectangular frame outline.

    Robust to amber/gold frame border (the saturation-cluster anchor
    isn't — the amber border would pollute the saturation scan).

    Returns (x0, y0, x1, y1) of the detected frame outer bbox, or None
    if no candidate passes the aspect-ratio + size + interior-color checks.
    """
    if not _HAVE_CV2:
        return None

    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)

    # Bilateral filter preserves edges while smoothing low-amplitude noise.
    # The frame border can be faint (low alpha over dark UI) so we don't
    # want aggressive blur; we want JPEG ringing suppressed.
    blurred = cv2.bilateralFilter(gray, 5, 50, 50)

    # Canny edges. Low thresholds because the frame border may be subtle
    # (white-translucent over dark) AND we want amber-edge sensitivity.
    edges = cv2.Canny(blurred, 20, 80)

    # Dilate slightly to close small gaps in the rectangular outline.
    # Kernel kept at 2x2 so we don't merge frame with inner blocks.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    edges = cv2.dilate(edges, kernel, iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    h_img, w_img = img_rgb.shape[:2]
    candidates: list[tuple[int, int, int, int, float, float]] = []

    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)

        # Size sanity:
        #  - frame is at least 40px wide and 6px tall (retina blurs make
        #    very small ribbons unrecoverable anyway)
        #  - frame is at most the image bounds minus a 2px margin
        if w < 40 or h < 6:
            continue
        if w >= w_img - 1 or h >= h_img - 1:
            continue

        # Boundary guard: the frame outline of an intact ribbon never
        # touches the image edge. If it does, the screenshot has been
        # cropped through the ribbon, and the inner-strip geometry will
        # be wrong → skip this candidate so the saturation fallback can
        # try (and its distance-to-palette guard will then fail-loud if
        # the partial strip can't be classified confidently).
        if x <= 0 or y <= 0 or (x + w) >= w_img or (y + h) >= h_img:
            continue

        ar = w / max(h, 1)
        if ar < FRAME_ASPECT_MIN or ar > FRAME_ASPECT_MAX:
            continue

        # Score favors aspect-ratio closeness to target AND larger size.
        # Larger size wins ties since real ribbons render at meaningful
        # pixel sizes on phone screens.
        ar_penalty = abs(ar - FRAME_ASPECT_TARGET)
        score = (w * h) / (1.0 + ar_penalty * 0.5)
        candidates.append((x, y, w, h, ar, score))

    if not candidates:
        return None

    candidates.sort(key=lambda c: -c[5])

    # Walk best→worst and accept the first that has real brand-palette
    # colors in its interior block-strip region. This is the load-bearing
    # discriminator — random page rectangles fail this check.
    for x, y, w, h, _ar, _score in candidates:
        if _verify_ribbon_interior(img_rgb, x, y, w, h):
            return (x, y, x + w, y + h)

    return None


def _verify_ribbon_interior(img_rgb, x: int, y: int, w: int, h: int) -> bool:
    """Check the bbox's interior block-strip region for high concentration
    of saturated brand-palette colors. A real ribbon has ≥30% saturated
    pixels in the strip; random page rectangles fail this check."""
    ix0 = max(x, x + int(w * INNER_X_START_RATIO))
    iy0 = max(y, y + int(h * INNER_Y_START_RATIO))
    ix1 = min(x + w, x + int(w * INNER_X_END_RATIO))
    iy1 = min(y + h, y + int(h * INNER_Y_END_RATIO))
    if ix1 <= ix0 or iy1 <= iy0:
        return False

    inner = img_rgb[iy0:iy1, ix0:ix1]
    if inner.size == 0:
        return False

    hsv = cv2.cvtColor(inner, cv2.COLOR_RGB2HSV)
    saturated = ((hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 60)).sum()
    sat_fraction = saturated / (inner.shape[0] * inner.shape[1])
    return sat_fraction >= 0.30


def _strip_from_frame(img_rgb, frame_bbox: tuple[int, int, int, int]) -> tuple["Image.Image", tuple[int, int, int, int]]:
    """Given a detected frame bbox, compute the inner block-strip bbox
    using the known geometric ratios + return it as a PIL crop ready
    for color sampling."""
    x0, y0, x1, y1 = frame_bbox
    fw = x1 - x0
    fh = y1 - y0
    sx0 = x0 + int(fw * INNER_X_START_RATIO)
    sy0 = y0 + int(fh * INNER_Y_START_RATIO)
    sx1 = x0 + int(fw * INNER_X_END_RATIO)
    sy1 = y0 + int(fh * INNER_Y_END_RATIO)
    # Clamp to image bounds defensively
    h_img, w_img = img_rgb.shape[:2]
    sx0 = max(0, sx0)
    sy0 = max(0, sy0)
    sx1 = min(w_img, sx1)
    sy1 = min(h_img, sy1)
    strip_pixels = img_rgb[sy0:sy1, sx0:sx1]
    strip = Image.fromarray(strip_pixels)
    return strip, (sx0, sy0, sx1, sy1)


def _find_ribbon_strip(img: Image.Image) -> tuple[Image.Image, tuple] | None:
    """Find the bounding box of the ribbon in `img` by scanning for the
    densest horizontal cluster of high-saturation non-grayscale pixels.

    Returns `(cropped_ribbon_image, (left, top, right, bottom))` or None
    if no candidate cluster is found.
    """
    rgb = img.convert("RGB")
    hsv = rgb.convert("HSV")
    w, h = hsv.size

    hsv_data = list(hsv.getdata())

    # Per-row count of saturated pixels (S > 100 + V > 60). Ribbon rows
    # will have ~20 contiguous saturated runs; UI background rows have
    # near-zero saturation.
    row_counts = [0] * h
    row_xmin = [w] * h
    row_xmax = [0] * h
    for y in range(h):
        row_base = y * w
        for x in range(w):
            _hh, s, v = hsv_data[row_base + x]
            if s > 100 and v > 60:
                row_counts[y] += 1
                if x < row_xmin[y]:
                    row_xmin[y] = x
                if x > row_xmax[y]:
                    row_xmax[y] = x

    # Find the densest run of consecutive rows with high saturated-pixel counts.
    best = None  # (count_sum, y_start, y_end)
    cur_start = None
    cur_sum = 0
    threshold = 30  # ribbon row must have ≥30 saturated px
    for y in range(h):
        if row_counts[y] >= threshold:
            if cur_start is None:
                cur_start = y
                cur_sum = 0
            cur_sum += row_counts[y]
        else:
            if cur_start is not None and (best is None or cur_sum > best[0]):
                best = (cur_sum, cur_start, y - 1)
            cur_start = None
    if cur_start is not None and (best is None or cur_sum > best[0]):
        best = (cur_sum, cur_start, h - 1)
    if not best:
        return None

    _, y_start, y_end = best
    x_min = min(row_xmin[y] for y in range(y_start, y_end + 1) if row_xmin[y] < w)
    x_max = max(row_xmax[y] for y in range(y_start, y_end + 1))
    # Small margin so the sampler has room.
    pad = 2
    bbox = (max(0, x_min - pad), max(0, y_start - pad),
            min(w, x_max + 1 + pad), min(h, y_end + 1 + pad))
    return rgb.crop(bbox), bbox


def _sample_block_colors(ribbon: Image.Image, num_blocks: int = 20) -> list[tuple]:
    """Sample the dominant RGB at each block position along the ribbon's
    width. Assumes the ribbon is horizontal + tightly cropped.

    Sampling region per block is the central ~40% X × central ~50% Y of
    each block's allotted slice. Per-channel MEDIAN (not mean) makes the
    sampler robust to a minority of off-color pixels — the inter-block
    gaps (background-dark) and anti-aliased block-edge pixels would
    otherwise dim the average enough to push the sample past the
    distance-to-palette safety gate on tight crops. Median votes the
    majority block color even when up to ~40% of the sample window
    contains gap/edge contamination."""
    w, h = ribbon.size
    if w == 0 or h == 0:
        return []
    pixels = list(ribbon.getdata())
    block_w = w / num_blocks
    sampled = []
    y_start = int(h * 0.25)
    y_end = int(h * 0.75) + 1
    if y_end <= y_start:
        y_end = y_start + 1
    for i in range(num_blocks):
        x_start = int(i * block_w + block_w * 0.3)
        x_end = int(i * block_w + block_w * 0.7)
        if x_end <= x_start:
            x_end = x_start + 1
        x_end = min(x_end, w)
        r_vals: list[int] = []
        g_vals: list[int] = []
        b_vals: list[int] = []
        for y in range(y_start, y_end):
            base = y * w
            for x in range(x_start, x_end):
                p = pixels[base + x]
                r_vals.append(p[0])
                g_vals.append(p[1])
                b_vals.append(p[2])
        if not r_vals:
            sampled.append((0, 0, 0))
            continue
        r_vals.sort(); g_vals.sort(); b_vals.sort()
        mid = len(r_vals) // 2
        sampled.append((r_vals[mid], g_vals[mid], b_vals[mid]))
    return sampled


def decode_ribbon_image(img: Image.Image) -> dict:
    """Top-level: find ribbon → sample blocks → classify → decode token.

    Two anchor mechanisms tried in order (multi-anchor fallback for
    partially-obscured ribbons):
      1. Frame-shape (OpenCV) — robust to amber/gold frame border.
         Skipped if cv2 isn't installed.
      2. Saturation-cluster (PIL) — fast, dependency-free; works on
         white-frame V0 + amber-blocks-only crops.

    Returns:
        {
            "token": "<8 chars>" or None,
            "bbox": [left, top, right, bottom] or None,
            "blocks": [{"sampled_rgb": [r,g,b], "classified": <0..3>}, ...],
            "stats": {"detected": bool, "block_count": int, "anchor": "frame"|"saturation"|None},
        }
    """
    rgb = img.convert("RGB")
    anchor: str | None = None
    ribbon: Image.Image | None = None
    bbox: tuple[int, int, int, int] | None = None

    # Primary anchor: frame-shape via OpenCV (Phase 2.5).
    if _HAVE_CV2:
        rgb_arr = np.array(rgb)
        frame = _find_ribbon_frame_cv(rgb_arr)
        if frame is not None:
            ribbon, bbox = _strip_from_frame(rgb_arr, frame)
            anchor = "frame"

    # Fallback anchor: saturation-cluster (Phase 2 V0). Always available.
    if ribbon is None:
        found = _find_ribbon_strip(rgb)
        if found is not None:
            ribbon, raw_bbox = found
            bbox = raw_bbox
            anchor = "saturation"

    if ribbon is None or bbox is None:
        return {
            "token": None,
            "bbox": None,
            "blocks": [],
            "stats": {"detected": False, "anchor": None, "note": "no ribbon detected"},
        }

    samples = _sample_block_colors(ribbon)
    if len(samples) != 20:
        return {
            "token": None,
            "bbox": list(bbox),
            "blocks": [{"sampled_rgb": list(s), "classified": _classify_color(s)} for s in samples],
            "stats": {
                "detected": True,
                "block_count": len(samples),
                "anchor": anchor,
                "note": "expected 20 blocks",
            },
        }

    # Two-track classification (S-102):
    #
    # Track 1 — hue-based (LOAD-BEARING for camera captures). Phone
    #   cameras drift RGB heavily (white-balance, exposure, JPEG ringing
    #   on saturated colors) but preserve the underlying hue. Hue
    #   classification with a 30° tolerance + saturation/value floors
    #   handles iPhone-class capture cleanly while rejecting achromatic
    #   page pixels.
    # Track 2 — RGB-distance (kept for diagnostic visibility). When the
    #   capture is clean (screenshot upload, not camera), RGB distances
    #   are small + this track confirms the hue track agrees.
    #
    # The hue track is authoritative for accept/reject. The audit-log
    # lookup is the final correctness gate — a wrong token returns
    # honest-empty rather than silently shipping false provenance.
    classified_rgb = [_classify_color(s) for s in samples]
    rgb_distances = [_rgb_dist(s, BRAND_PALETTE[c]) for s, c in zip(samples, classified_rgb)]

    hue_classifications = [_classify_by_hue(s) for s in samples]
    classified = [hc[0] for hc in hue_classifications]
    hue_dists = [hc[1] for hc in hue_classifications]
    sats = [hc[2] for hc in hue_classifications]
    lits = [hc[3] for hc in hue_classifications]

    def _block_fails(i: int) -> str | None:
        if sats[i] < MIN_SATURATION_FOR_CLASSIFY:
            return "low-saturation"
        if lits[i] < MIN_VALUE_FOR_CLASSIFY:
            return "near-black"
        if hue_dists[i] > MAX_HUE_DISTANCE_DEG:
            return "hue-off-palette"
        return None

    failures = [(i, _block_fails(i)) for i in range(20)]
    out_of_palette = [i for i, reason in failures if reason]

    blocks_payload = [
        {
            "sampled_rgb": list(s),
            "classified": c,
            "hue_distance_deg": round(hue_dists[i], 1),
            "saturation": round(sats[i], 2),
            "lightness": round(lits[i], 2),
            "rgb_dist_sq": int(rgb_distances[i]),
            "rgb_classified": classified_rgb[i],
            "fail_reason": _block_fails(i),
        }
        for i, (s, c) in enumerate(zip(samples, classified))
    ]

    if out_of_palette:
        reason_breakdown: dict[str, int] = {}
        for _i, reason in failures:
            if reason:
                reason_breakdown[reason] = reason_breakdown.get(reason, 0) + 1
        return {
            "token": None,
            "bbox": list(bbox),
            "blocks": blocks_payload,
            "stats": {
                "detected": True,
                "block_count": 20,
                "anchor": anchor,
                "note": f"{len(out_of_palette)} block(s) failed hue-classification gate",
                "out_of_palette_indices": out_of_palette,
                "fail_reasons": reason_breakdown,
                "max_hue_distance_deg": round(max(hue_dists), 1),
            },
        }

    bits = []
    for c in classified:
        bits.append((c >> 1) & 1)
        bits.append(c & 1)
    token = _bits_to_token(bits)
    return {
        "token": token,
        "bbox": list(bbox),
        "blocks": blocks_payload,
        "stats": {
            "detected": True,
            "block_count": 20,
            "anchor": anchor,
            "max_hue_distance_deg": round(max(hue_dists), 1),
        },
    }


def decode_ribbon_bytes(data: bytes) -> dict:
    img = Image.open(io.BytesIO(data))
    return decode_ribbon_image(img)


def decode_ribbon_file(path: str | Path) -> dict:
    img = Image.open(path)
    return decode_ribbon_image(img)


if __name__ == "__main__":
    # Smoke test: capture the debug page ribbon area + decode.
    import sys
    from playwright.sync_api import sync_playwright

    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3000/?view=watermark-debug"
    print(f"Capturing {url} via Playwright (full-page)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 900, "height": 1600})
            page.goto(url, wait_until="networkidle", timeout=20000)
            page.wait_for_selector("svg", timeout=10000)
            png = page.screenshot(full_page=True)
        finally:
            browser.close()
    print(f"Captured {len(png)} bytes. Decoding ribbon...")
    result = decode_ribbon_bytes(png)
    print(f"  Detected: {result['stats']}")
    print(f"  Token:    {result['token']}")
    print(f"  bbox:     {result['bbox']}")
    if result.get("blocks"):
        print("  Per-block (sampled RGB → classified bits):")
        for i, b in enumerate(result["blocks"]):
            print(f"    block {i:2d}: rgb={b['sampled_rgb']} → {b['classified']:02b}")

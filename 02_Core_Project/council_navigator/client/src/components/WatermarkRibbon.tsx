/**
 * S-098 Phase 2 V0 — visible provenance ribbon.
 *
 * Renders a server-registered 40-bit token as a thin horizontal strip of
 * 20 colored blocks × 2 bits each, using Z-SPAN brand colors. Looks like a
 * design ribbon, NOT a barcode — it's an intentional brand element that
 * doubles as a verification anchor.
 *
 * Click → opens /?view=watermark-verify&token=XXX in a new tab; the
 * verifier auto-runs and shows the audit-row verdict for that output.
 *
 * Future scope:
 *   - Phone-camera ribbon decoder (color-classify each block from a
 *     captured frame). Substantially easier than font-watermark pixel
 *     decode because the brand colors are perceptually distinct.
 *   - When the font-watermark approach matures (ML pixel classifier or
 *     contextual-alt OpenType), the ribbon retires to a wax-stamp-style
 *     positioning anchor + the cryptographic provenance migrates back
 *     into the typography per the operator's note.
 */
const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

// 4-color palette mapped to 2-bit values. Picked from index.css brand
// tokens with the constraint that all 4 are perceptually MAXIMALLY
// distinct so a phone camera classifier survives lighting variance +
// JPEG compression + bad-camera color smudging. The earlier palette
// had civic-blue + highway-sign-blue as adjacent values — both blues,
// hard to distinguish under bad-camera conditions. Replaced
// highway-sign-blue with alert-red so all 4 hues span the color wheel
// (deep blue / green / red / amber — ~90° apart in hue).
const PALETTE: Record<number, string> = {
  0b00: "#1A3A7C", // civic-blue (deep)
  0b01: "#EF4444", // alert-red — was highway-sign-blue (too close to civic-blue)
  0b10: "#22C55E", // success-green
  0b11: "#F5A524", // highway-amber
};

/** Convert an 8-char base32 token into the 20 × 2-bit sequence used by
 *  the ribbon renderer. */
function tokenToBlockBits(token: string): number[] {
  const upper = token.toUpperCase();
  if (upper.length !== 8) throw new Error(`token must be 8 chars; got ${upper.length}`);
  const bits: number[] = [];
  for (const ch of upper) {
    const idx = BASE32_ALPHABET.indexOf(ch);
    if (idx === -1) throw new Error(`invalid base32 char: ${ch}`);
    // 5 bits per base32 char, MSB first
    for (let b = 4; b >= 0; b--) bits.push((idx >> b) & 1);
  }
  // Group into 20 pairs of 2 bits each
  const blocks: number[] = [];
  for (let i = 0; i < 40; i += 2) {
    blocks.push((bits[i] << 1) | bits[i + 1]);
  }
  return blocks;
}

type Props = {
  meetingId: number;
  outputType: string;
  ribbonToken?: string | null;
  registrationState?: "registered" | "pending" | null;
  /** Block width in px. Default 5 — ribbon ends up ~140px wide
   *  including gaps; gives bad cameras more pixels per block. */
  blockWidth?: number;
  /** Ribbon block-strip height in px. Default 14. */
  height?: number;
  /** Gap between blocks in px. Default 2 — wider visual separator so
   *  adjacent blocks never blur into each other under camera noise. */
  gap?: number;
  className?: string;
};

// Frame is a fixed-pattern container around the variable colored blocks.
// Mirrors the security-ribbon-on-a-dollar-bill discipline: consistent
// outer shape + microtext makes the WHOLE thing easy for a downstream
// detector to find in arbitrary screenshots, while the colored blocks
// inside still carry the per-output payload.
//
// FRAME_BORDER + FRAME_LABEL_COLOR are highway-amber (Phase 2.5+
// restored). The Phase 2.5 frame detector in `watermark_ribbon_decoder
// .py § _find_ribbon_frame_cv` uses OpenCV edge/contour detection
// + interior-color verification to locate the frame SHAPE first, then
// samples the inner block strip via known geometric ratios — so the
// saturated-amber border no longer pollutes the saturation cluster
// heuristic that the original V0 fallback uses. (The saturation
// fallback now only fires when the frame anchor finds no candidate,
// and on amber-frame ribbons the frame anchor wins consistently.)
const FRAME_BORDER = "rgba(245, 165, 36, 0.85)";       // highway-amber
const FRAME_BORDER_WIDTH = 1;
const FRAME_CORNER_RADIUS = 3;
const FRAME_PADDING_X = 4;
const FRAME_PADDING_Y = 3;
// S-102 sticker-as-civic-tag — microtext IS the cold-stranger discovery
// URL. Anyone seeing a ribbon (on a screen, on paper, on a sticker stuck
// to a streetlight) reads the microtext and types it into a phone to
// verify the ribbon. The OpenCV anchor is geometric (amber-frame +
// amber-microtext-band + interior-brand-color), not OCR — so text
// content is free to change without affecting detection.
const FRAME_LABEL = "zspan.org/scan";                  // amber microtext anchor (left edge)
const FRAME_LABEL_FONT_SIZE = 6;
const FRAME_LABEL_COLOR = "rgba(245, 165, 36, 0.95)";  // highway-amber, slightly brighter for legibility

const VERIFY_BASE =
  typeof window !== "undefined"
    ? `${window.location.origin}/?view=watermark-verify`
    : "/?view=watermark-verify";

export function WatermarkRibbon({
  meetingId,
  outputType,
  ribbonToken,
  registrationState,
  blockWidth = 5,
  height = 14,
  gap = 2,
  className,
}: Props) {
  const token = ribbonToken?.trim().toUpperCase() || null;
  const tokenIsValid = Boolean(
    token
      && token.length === 8
      && token.split("").every((ch) => BASE32_ALPHABET.includes(ch)),
  );

  if (!tokenIsValid || !token) {
    return null;
  }

  const blocks = tokenToBlockBits(token);
  const blocksWidth = (blockWidth + gap) * 20 - gap;

  // Label sits on the left edge of the frame as microtext — fixed-pattern
  // visual signature the detector can lock onto. The +10 trailing margin
  // gives the microtext breathing room from the first colored block so
  // letterforms don't touch the block edge at 6px font size.
  const labelWidth = FRAME_LABEL.length * FRAME_LABEL_FONT_SIZE * 0.62 + 10;

  const innerX = FRAME_PADDING_X + labelWidth;
  const innerY = FRAME_PADDING_Y;
  const innerW = blocksWidth;
  const innerH = height;
  const frameW = innerX + innerW + FRAME_PADDING_X;
  const frameH = innerH + FRAME_PADDING_Y * 2;

  const href = `${VERIFY_BASE}&token=${encodeURIComponent(token)}`;
  const tooltip = `Z-SPAN provenance · click to verify (token ${token})`;

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={className}
      data-meeting-id={meetingId}
      data-output-type={outputType}
      data-registration-state={registrationState || "registered"}
      title={tooltip}
      aria-label={tooltip}
      style={{
        display: "inline-flex",
        alignItems: "center",
        textDecoration: "none",
        cursor: "pointer",
      }}
    >
      <svg
        width={frameW}
        height={frameH}
        viewBox={`0 0 ${frameW} ${frameH}`}
        xmlns="http://www.w3.org/2000/svg"
        style={{ display: "block" }}
      >
        {/* Frame outline — consistent anchor for downstream detection */}
        <rect
          x={FRAME_BORDER_WIDTH / 2}
          y={FRAME_BORDER_WIDTH / 2}
          width={frameW - FRAME_BORDER_WIDTH}
          height={frameH - FRAME_BORDER_WIDTH}
          rx={FRAME_CORNER_RADIUS}
          ry={FRAME_CORNER_RADIUS}
          fill="none"
          stroke={FRAME_BORDER}
          strokeWidth={FRAME_BORDER_WIDTH}
        />

        {/* Microtext label — left-edge "Z-SPAN" anchor signature */}
        <text
          x={FRAME_PADDING_X + 1}
          y={frameH / 2}
          dominantBaseline="middle"
          fontSize={FRAME_LABEL_FONT_SIZE}
          fontFamily='"Inter", system-ui, sans-serif'
          fontWeight={700}
          letterSpacing="0.5"
          fill={FRAME_LABEL_COLOR}
        >
          {FRAME_LABEL}
        </text>

        {/* Variable payload — the 20 colored blocks */}
        <g transform={`translate(${innerX}, ${innerY})`}>
          {blocks.map((value, i) => (
            <rect
              key={i}
              x={i * (blockWidth + gap)}
              y={0}
              width={blockWidth}
              height={innerH}
              fill={PALETTE[value]}
            />
          ))}
        </g>
      </svg>
    </a>
  );
}

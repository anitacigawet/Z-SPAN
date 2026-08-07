/**
 * renderInfograph — client-side share-card renderer.
 *
 * The earlier design pass recommended native Canvas 2D over html-to-image
 * for the tonight-shipped per-meeting infographic: the card has a fixed
 * layout + a handful of text blocks, so a general DOM screenshot engine
 * would be unnecessary weight (new dependency, CSS/font capture caveats,
 * cross-origin image tainting on the QR, offscreen-DOM layout flakiness).
 * Native Canvas ships zero-dep and produces a deterministic PNG.
 *
 * The card is 1200×630 — the standard social-share aspect ratio, so an
 * operator or a signed-in visitor can paste the download directly into
 * Twitter/X, Bluesky, Threads, etc., without a downstream crop cycle.
 *
 * QR uses the already-installed `qrcode` package (package.json:61), no
 * new dep. The caller constructs `publicUrl` from the server-validated
 * public_id via `canonicalBroadcastUrl`; ambient browser origins, query
 * parameters, and fragments never enter the QR or printed provenance URL.
 */
import QRCode from "qrcode";

export const CANONICAL_ORIGIN = "https://zspan.org";

export interface InfographInput {
 city: string;
 date: string; // free-form; caller does formatting
 title: string;
 tagline: string | null;
 keyDecisions: string[]; // top 3 used; more truncated
 publicUrl: string; // output of canonicalBroadcastUrl — feeds QR + printed URL
}

const CANVAS_W = 1200;
const CANVAS_H = 630;

// Z-SPAN civic palette (matches the site CSS tokens; hardcoded here since
// Canvas can't read CSS variables directly).
const COLOR_BG = "#0b0d10"; // near-black surface
const COLOR_ACCENT = "#5eead4"; // civic teal — matches --civic-teal
const COLOR_TEXT = "#f5f7fa";
const COLOR_MUTED = "#8b95a3";
const COLOR_RULE = "#242a33";

const PADDING = 56;
const QR_SIZE = 128;
const QR_TILE_PADDING = 16;
const QR_TILE_SIZE = QR_SIZE + QR_TILE_PADDING * 2;
const QR_DARK = "#0b0d10";
const QR_LIGHT = "#ffffff";

/**
 * Build the one public URL shape permitted in a share card. `publicId`
 * comes from the public DTO, never from location state.
 */
export function canonicalBroadcastUrl(publicId: string): string {
 return `${CANONICAL_ORIGIN}/?view=broadcast&publicId=${encodeURIComponent(publicId)}`;
}

/**
 * Match the page's decision-source precedence and remove presentation
 * markup before text crosses into Canvas, where it would otherwise print
 * literally. A non-empty verified sidecar wins over the legacy output.
 */
export function prepareInfographKeyDecisions(
 previewDecisions: readonly string[],
 legacyDecisions: readonly string[],
): string[] {
 const source = previewDecisions.length > 0 ? previewDecisions : legacyDecisions;
 return source
 .map(decision =>
 decision
 .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
 .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
 .replace(/<\/?(?:core|nuance)>/gi, "")
 .replace(/<[^>]*>/g, "")
 .replace(/[*_~`]+/g, "")
 .replace(/\s+/g, " ")
 .trim(),
 )
 .filter(Boolean);
}

async function loadQrDataUrl(url: string): Promise<string> {
 return QRCode.toDataURL(url, {
 width: QR_SIZE,
 margin: 4,
 color: {
 dark: QR_DARK,
 light: QR_LIGHT,
 },
 errorCorrectionLevel: "M",
 });
}

async function loadImage(dataUrl: string): Promise<HTMLImageElement> {
 return new Promise((resolve, reject) => {
 const img = new Image();
 img.onload = () => resolve(img);
 img.onerror = reject;
 img.src = dataUrl;
 });
}

/**
 * Wrap `text` to `maxLines` lines within `maxWidth`. Long words get their
 * own line rather than overflowing the canvas edge. Returns the wrapped
 * lines; the caller decides layout position and font.
 */
function wrapText(
 ctx: CanvasRenderingContext2D,
 text: string,
 maxWidth: number,
 maxLines: number,
): string[] {
 const words = text.split(/\s+/).filter(Boolean);
 const lines: string[] = [];
 let current = "";
 for (const word of words) {
 const candidate = current ? `${current} ${word}` : word;
 if (ctx.measureText(candidate).width <= maxWidth) {
 current = candidate;
 } else {
 if (current) lines.push(current);
 current = word;
 if (lines.length === maxLines - 1) {
 // Last visible line — greedily fill + ellipsize what remains.
 const remaining = words.slice(words.indexOf(word)).join(" ");
 const truncated = truncateToWidth(ctx, remaining, maxWidth);
 lines.push(truncated);
 return lines;
 }
 }
 }
 if (current) lines.push(current);
 return lines.slice(0, maxLines);
}

function truncateToWidth(
 ctx: CanvasRenderingContext2D,
 text: string,
 maxWidth: number,
): string {
 if (ctx.measureText(text).width <= maxWidth) return text;
 const ellipsis = "…";
 let lo = 0;
 let hi = text.length;
 while (lo < hi) {
 const mid = Math.floor((lo + hi + 1) / 2);
 const candidate = text.slice(0, mid) + ellipsis;
 if (ctx.measureText(candidate).width <= maxWidth) {
 lo = mid;
 } else {
 hi = mid - 1;
 }
 }
 return text.slice(0, lo) + ellipsis;
}

function fillRoundedRect(
 ctx: CanvasRenderingContext2D,
 x: number,
 y: number,
 width: number,
 height: number,
 radius: number,
): void {
 ctx.beginPath();
 ctx.moveTo(x + radius, y);
 ctx.lineTo(x + width - radius, y);
 ctx.quadraticCurveTo(x + width, y, x + width, y + radius);
 ctx.lineTo(x + width, y + height - radius);
 ctx.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
 ctx.lineTo(x + radius, y + height);
 ctx.quadraticCurveTo(x, y + height, x, y + height - radius);
 ctx.lineTo(x, y + radius);
 ctx.quadraticCurveTo(x, y, x + radius, y);
 ctx.closePath();
 ctx.fill();
}

/**
 * Render a Z-SPAN meeting share card to a PNG Blob. Returns null when
 * the browser can't produce a Canvas Blob (rare — jsdom, ancient WebViews).
 * `input.publicUrl` is guaranteed by the caller to be the canonical
 * `https://zspan.org/?view=broadcast&publicId=...` URL.
 */
export async function renderInfograph(input: InfographInput): Promise<Blob | null> {
 const canvas = document.createElement("canvas");
 canvas.width = CANVAS_W;
 canvas.height = CANVAS_H;
 const ctx = canvas.getContext("2d");
 if (!ctx) return null;

 // Background
 ctx.fillStyle = COLOR_BG;
 ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);

 // Top accent bar
 ctx.fillStyle = COLOR_ACCENT;
 ctx.fillRect(0, 0, CANVAS_W, 6);

 // Header row — Z-SPAN wordmark
 ctx.fillStyle = COLOR_TEXT;
 ctx.font = "700 22px system-ui, -apple-system, 'Segoe UI', sans-serif";
 ctx.textBaseline = "top";
 ctx.fillText("Z-SPAN", PADDING, PADDING);

 ctx.fillStyle = COLOR_MUTED;
 ctx.font = "400 14px system-ui, -apple-system, 'Segoe UI', sans-serif";
 ctx.fillText("A virtual library for Arizona politics", PADDING + 88, PADDING + 6);

 // City · Date eyebrow
 const cityDate = `${input.city.toUpperCase()} · ${input.date.toUpperCase()}`;
 ctx.fillStyle = COLOR_ACCENT;
 ctx.font = "600 13px system-ui, -apple-system, 'Segoe UI', sans-serif";
 ctx.fillText(cityDate, PADDING, PADDING + 60);

 // Meeting title (large)
 ctx.fillStyle = COLOR_TEXT;
 ctx.font = "700 44px system-ui, -apple-system, 'Segoe UI', sans-serif";
 const titleLines = wrapText(ctx, input.title, CANVAS_W - PADDING * 2, 2);
 let y = PADDING + 92;
 for (const line of titleLines) {
 ctx.fillText(line, PADDING, y);
 y += 54;
 }

 // Episode tagline (italic, medium)
 if (input.tagline) {
 ctx.fillStyle = COLOR_MUTED;
 ctx.font = "italic 400 22px system-ui, -apple-system, 'Segoe UI', sans-serif";
 const taglineLines = wrapText(ctx, input.tagline, CANVAS_W - PADDING * 2, 2);
 for (const line of taglineLines) {
 ctx.fillText(line, PADDING, y);
 y += 30;
 }
 y += 8;
 } else {
 y += 12;
 }

 // Rule
 ctx.strokeStyle = COLOR_RULE;
 ctx.lineWidth = 1;
 ctx.beginPath();
 ctx.moveTo(PADDING, y);
 ctx.lineTo(CANVAS_W - PADDING, y);
 ctx.stroke();
 y += 24;

 // Key Decisions header + top 3
 ctx.fillStyle = COLOR_ACCENT;
 ctx.font = "600 13px system-ui, -apple-system, 'Segoe UI', sans-serif";
 ctx.fillText("KEY DECISIONS", PADDING, y);
 y += 26;

 const topDecisions = input.keyDecisions.slice(0, 3);
 if (topDecisions.length === 0) {
 ctx.fillStyle = COLOR_MUTED;
 ctx.font = "italic 400 16px system-ui, -apple-system, 'Segoe UI', sans-serif";
 ctx.fillText("No key-decision summary available.", PADDING, y);
 y += 24;
 } else {
 ctx.font = "400 16px system-ui, -apple-system, 'Segoe UI', sans-serif";
 for (const decision of topDecisions) {
 ctx.fillStyle = COLOR_ACCENT;
 ctx.fillText("•", PADDING, y);
 ctx.fillStyle = COLOR_TEXT;
 const bulletLines = wrapText(
 ctx,
 decision,
 CANVAS_W - PADDING * 2 - QR_TILE_SIZE - 32 - 18, // reserve QR tile
 2,
 );
 let by = y;
 for (const line of bulletLines) {
 ctx.fillText(line, PADDING + 18, by);
 by += 22;
 }
 y = by + 6;
 }
 }

 // QR code — bottom-right corner
 try {
 const qrData = await loadQrDataUrl(input.publicUrl);
 const qrImg = await loadImage(qrData);
 const tileX = CANVAS_W - PADDING - QR_TILE_SIZE;
 const tileY = CANVAS_H - PADDING - QR_TILE_SIZE - 40;
 const qrX = tileX + QR_TILE_PADDING;
 const qrY = tileY + QR_TILE_PADDING;
 ctx.fillStyle = QR_LIGHT;
 fillRoundedRect(ctx, tileX, tileY, QR_TILE_SIZE, QR_TILE_SIZE, 12);
 ctx.drawImage(qrImg, qrX, qrY, QR_SIZE, QR_SIZE);
 ctx.fillStyle = COLOR_MUTED;
 ctx.font = "400 11px system-ui, -apple-system, 'Segoe UI', sans-serif";
 ctx.textAlign = "center";
 ctx.fillText(
 "scan for broadcast",
 tileX + QR_TILE_SIZE / 2,
 tileY + QR_TILE_SIZE + 8,
 );
 ctx.textAlign = "left";
 } catch {
 // QR failed — omit; the URL still appears in the provenance line below.
 }

 // Provenance line — bottom, above the 6px accent
 const provenance =
 "AI-assisted summary · human reviewed · verify against the cited recording";
 ctx.fillStyle = COLOR_MUTED;
 ctx.font = "400 12px system-ui, -apple-system, 'Segoe UI', sans-serif";
 ctx.fillText(provenance, PADDING, CANVAS_H - PADDING - 20);

 // URL line
 ctx.fillStyle = COLOR_ACCENT;
 ctx.font = "600 12px system-ui, -apple-system, 'Segoe UI', sans-serif";
 ctx.fillText(input.publicUrl, PADDING, CANVAS_H - PADDING - 4);

 // Bottom accent bar
 ctx.fillStyle = COLOR_ACCENT;
 ctx.fillRect(0, CANVAS_H - 6, CANVAS_W, 6);

 return new Promise((resolve) => {
 canvas.toBlob((blob) => resolve(blob), "image/png", 0.95);
 });
}

/**
 * Trigger a browser download of `blob` as `filename`. No-op in server-side
 * rendering contexts (SSR / tests without a DOM).
 */
export function downloadBlob(blob: Blob, filename: string): void {
 if (typeof document === "undefined") return;
 const url = URL.createObjectURL(blob);
 const a = document.createElement("a");
 a.href = url;
 a.download = filename;
 document.body.appendChild(a);
 a.click();
 document.body.removeChild(a);
 // Revoke on next tick so Safari can still resolve the click before we
 // pull the URL out from under it.
 setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * A stable filename shape for the download so operators can tell which
 * meeting a saved card belongs to.
 */
export function infographFilename(city: string, date: string, publicId: string): string {
 const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
 return `zspan-${slug(city)}-${slug(date)}-${publicId}.png`;
}

/**
 *watermark base32 payload helpers.
 *
 * The font-watermark approach (Phases 0-1) was scrapped at close
 * per operator direction: hidden-perfect security backfires when no one
 * knows it exists. The visible ribbon (WatermarkRibbon.tsx) is the V0
 * surface; this file retains only format conversion helpers used by decoders
 * and diagnostics. Verifiable tokens are minted server-side and registry-
 * backed; this module must never derive one from public meeting metadata.
 *
 * Token shape: 40 bits encoded as 8 base32 chars.
 */

export const PAYLOAD_BITS = 40;
export const PAYLOAD_BYTES = 5;

const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

export function payloadToBase32Token(payload: Uint8Array): string {
 if (payload.length !== PAYLOAD_BYTES) {
 throw new Error(`Payload must be ${PAYLOAD_BYTES} bytes; got ${payload.length}`);
 }
 let result = "";
 for (let i = 0; i < PAYLOAD_BITS; i += 5) {
 let v = 0;
 for (let b = 0; b < 5; b++) {
 const bitIndex = i + b;
 const byteIndex = Math.floor(bitIndex / 8);
 const bitInByte = bitIndex % 8;
 const bit = (payload[byteIndex] >> (7 - bitInByte)) & 1;
 v = (v << 1) | bit;
 }
 result += BASE32_ALPHABET[v];
 }
 return result;
}

export function base32TokenToPayload(token: string): Uint8Array {
 const upper = token.toUpperCase();
 if (upper.length !== 8) {
 throw new Error(`Token must be 8 chars; got ${upper.length}`);
 }
 const bits: number[] = [];
 for (const ch of upper) {
 const idx = BASE32_ALPHABET.indexOf(ch);
 if (idx === -1) throw new Error(`Invalid base32 char: ${ch}`);
 for (let b = 4; b >= 0; b--) bits.push((idx >> b) & 1);
 }
 const payload = new Uint8Array(PAYLOAD_BYTES);
 for (let i = 0; i < PAYLOAD_BITS; i++) {
 if (bits[i]) payload[Math.floor(i / 8)] |= 1 << (7 - (i % 8));
 }
 return payload;
}

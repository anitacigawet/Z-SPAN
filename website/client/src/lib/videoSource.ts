/**
 * videoSource — classifier + types for embedding a meeting recording.
 *
 * Extracted from BroadcastPage.tsx (2026-06-25, V1.5-OperatorSearch-1 Z4)
 * so the modal-side InlineMeetingMomentPlayer can use the same logic
 * without duplicating it. BroadcastPage now imports from this module.
 *
 * Four kinds, in match priority order:
 *
 * - "youtube" — youtube.com/watch?v= / youtu.be/ / .com/embed/ /
 * .com/live/. Renders in an iframe with
 * ?enablejsapi=1 so postMessage seekTo works.
 * - "mp4" — direct .mp4 URL (Granicus archive). Renders in a
 * native <video> element; seek via currentTime.
 * - "granicus-iframe" — Granicus MediaPlayer.php page (<city>.granicus.com).
 * Renders as iframe; seek = ?starttime URL rewrite.
 * - "external-link" — anything else. No embed; caller renders an
 * "Open Full Meeting" link instead.
 */

export type VideoSource =
 | { kind: "youtube"; embedUrl: string; raw: string; videoId: string }
 | { kind: "mp4"; embedUrl: string; raw: string }
 | { kind: "granicus-iframe"; embedUrl: string; raw: string; clipId: string }
 | { kind: "external-link"; embedUrl: null; raw: string }
 | null;

export function getVideoSource(url: string | null | undefined): VideoSource {
 if (!url) return null;
 try {
 const yt = url.match(
 /(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/live\/)([\w-]{11})/,
 );
 if (yt) {
 return {
 kind: "youtube",
 embedUrl: `https://www.youtube.com/embed/${yt[1]}?rel=0&modestbranding=1&enablejsapi=1`,
 raw: url,
 videoId: yt[1],
 };
 }
 if (/\.mp4(\?|$)/i.test(url)) {
 return { kind: "mp4", embedUrl: url, raw: url };
 }
 const granicus = url.match(
 /\/\/([\w.-]+\.granicus\.com)\/MediaPlayer\.php\?.*?clip_id=(\d+)/i,
 );
 if (granicus) {
 return {
 kind: "granicus-iframe",
 embedUrl: url,
 raw: url,
 clipId: granicus[2],
 };
 }
 return { kind: "external-link", embedUrl: null, raw: url };
 } catch {
 return { kind: "external-link", embedUrl: null, raw: url };
 }
}

/**
 * Append/replace ?starttime=<seconds> on a Granicus MediaPlayer URL.
 * Preserves all other query params. Used both by BroadcastPage's
 * seekVideoTo and by the modal-side InlineMeetingMomentPlayer.
 */
export function granicusUrlWithStartTime(baseUrl: string, seconds: number): string {
 const cleaned = baseUrl.replace(/[&?]starttime=\d+/i, "");
 const sep = cleaned.includes("?") ? "&" : "?";
 return `${cleaned}${sep}starttime=${Math.floor(seconds)}`;
}

/**
 * MeetingExcerptPlayer — portable inline player for a single meeting
 * excerpt.
 *
 * Mirrors the BroadcastPage video-source dispatch (YouTube iframe +
 * direct-MP4 <video> + Granicus MediaPlayer iframe) so any page that
 * wants to inject a "play this segment inline" affordance — speaker-
 * roster review, future quote-review surfaces, anywhere we'd otherwise
 * deep-link the operator into BroadcastPage — gets the audio/video
 * locally without losing their place.
 *
 * The shape mirrors SyncedQuote's parent-managed activation pattern
 * (one player at a time across the page), but doesn't require word_timings
 * — the speaker-roster cluster excerpts are pyannote turns with start/end
 * seconds, not RAG-aligned word arrays. When the parent flips `isActive`
 * to true, the player mounts inline; flipping back to false unmounts
 * cleanly so we never have multiple iframes humming in the background.
 *
 * Source kinds come from the shared lib/videoSource classifier
 * (PLAYER-1 C3, 2026-07-07 — this file's private duplicate classifier
 * was deleted; one classifier now serves every player surface). The
 * rendering stays DELIBERATELY declarative — URL-built embeds, no
 * player/ adapter mount — because an excerpt player needs neither a
 * readable clock nor programmatic control, and the URL params do the
 * span natively:
 * - youtube → <iframe …/embed/<id>?start=N&end=M&autoplay=1>
 * (the IFrame Player URL params honor start/end without any JS).
 * - mp4 → <video src="<url>#t=N,M"> — the HTML5
 * media-fragment standard; browsers seek to N + stop at M
 * (onTimeUpdate safety net below for engines that ignore the end).
 * - granicus-iframe → <iframe src="<url>?starttime=N"> via the
 * shared granicusUrlWithStartTime; no native end param so playback
 * runs on until the operator closes the player.
 * - external-link → open-in-new-tab affordance (NOT an in-app
 * redirect — the operator's spot on the calling page is preserved).
 */
import { useEffect, useMemo, useState } from "react";
import { Play, X, ExternalLink } from "lucide-react";
import { getVideoSource, granicusUrlWithStartTime } from "../lib/videoSource";

interface MeetingExcerptPlayerProps {
 videoUrl: string | null | undefined;
 startSeconds: number;
 endSeconds: number;
 isActive: boolean;
 onActivate: () => void;
 onDeactivate: () => void;
 /** Optional label shown next to the activate-button. */
 buttonLabel?: string;
}

function buildYouTubeEmbed(videoId: string, start: number, end: number): string {
 const startSec = Math.max(0, Math.floor(start));
 const endSec = Math.max(startSec + 1, Math.ceil(end));
 const params = new URLSearchParams({
 start: String(startSec),
 end: String(endSec),
 autoplay: "1",
 rel: "0",
 modestbranding: "1",
 playsinline: "1",
 });
 return `https://www.youtube.com/embed/${encodeURIComponent(videoId)}?${params.toString()}`;
}

function buildMp4Src(src: string, start: number, end: number): string {
 const startSec = Math.max(0, start);
 const endSec = Math.max(startSec + 0.5, end);
 // Strip any existing media fragment so we don't double-stack #t= ranges
 // when the same excerpt is reactivated.
 const stripped = src.replace(/#t=[^&]*$/i, "");
 return `${stripped}#t=${startSec.toFixed(2)},${endSec.toFixed(2)}`;
}

export default function MeetingExcerptPlayer({
 videoUrl,
 startSeconds,
 endSeconds,
 isActive,
 onActivate,
 onDeactivate,
 buttonLabel = "Listen",
}: MeetingExcerptPlayerProps) {
 // Whitespace-only URLs count as no-source (pre-C3 behavior preserved).
 const source = useMemo(
 () => getVideoSource(videoUrl?.trim() || null),
 [videoUrl],
 );
 // Track whether the operator has ever activated this excerpt in the
 // current page-life — used only for the MP4 path's auto-stop, where the
 // <video> element's onTimeUpdate handler watches the timestamp.
 const [mountKey, setMountKey] = useState(0);
 useEffect(() => {
 if (isActive) setMountKey(k => k + 1);
 }, [isActive]);

 if (!source) {
 return (
 <span style={{
 fontSize: 11, color: "var(--text-secondary, #8b95a8)",
 fontStyle: "italic", whiteSpace: "nowrap", padding: "0 10px",
 }}>
 no video on file
 </span>
 );
 }

 if (source.kind === "external-link") {
 // Permissive fallback — open in a new tab so the calling page is
 // preserved. NOT an in-app navigation: the operator's row state
 // stays where it was.
 return (
 <a
 href={source.raw}
 target="_blank"
 rel="noopener noreferrer"
 style={{
 display: "inline-flex", alignItems: "center", gap: 5,
 background: "rgba(148, 163, 184, 0.08)",
 color: "var(--text-secondary, #8b95a8)",
 border: "none",
 borderLeft: "1px solid rgba(148, 163, 184, 0.12)",
 fontSize: 12, padding: "0 14px",
 whiteSpace: "nowrap", textDecoration: "none",
 }}
 title="Open video in a new tab (no inline player available for this source)"
 >
 <ExternalLink size={12} />
 Open video
 </a>
 );
 }

 if (!isActive) {
 return (
 <button
 onClick={onActivate}
 title="Play this segment inline"
 style={{
 display: "inline-flex", alignItems: "center", gap: 5,
 background: "rgba(74, 144, 226, 0.12)",
 color: "rgb(96, 165, 250)",
 border: "none",
 borderLeft: "1px solid rgba(148, 163, 184, 0.12)",
 fontSize: 12, padding: "0 14px",
 cursor: "pointer", whiteSpace: "nowrap",
 fontWeight: 500,
 }}
 >
 <Play size={12} fill="currentColor" />
 {buttonLabel}
 </button>
 );
 }

 // Active — render the inline player. The close button calls
 // onDeactivate so the parent unmounts us cleanly.
 return (
 <div style={{
 flexBasis: "100%", marginTop: 8,
 border: "1px solid rgba(74, 144, 226, 0.25)",
 borderRadius: 4,
 background: "rgba(0, 0, 0, 0.3)",
 overflow: "hidden",
 position: "relative",
 }}>
 <button
 onClick={onDeactivate}
 title="Close inline player"
 style={{
 position: "absolute", top: 6, right: 6, zIndex: 2,
 background: "rgba(0, 0, 0, 0.55)",
 color: "rgb(226, 232, 240)",
 border: "1px solid rgba(148, 163, 184, 0.3)",
 borderRadius: 4, padding: "4px 6px",
 cursor: "pointer", lineHeight: 0,
 }}
 >
 <X size={12} />
 </button>

 {source.kind === "youtube" && (
 <div style={{
 position: "relative", width: "100%", paddingBottom: "56.25%",
 }}>
 <iframe
 key={mountKey}
 src={buildYouTubeEmbed(source.videoId, startSeconds, endSeconds)}
 title="Meeting excerpt"
 allow="autoplay; encrypted-media"
 allowFullScreen
 style={{
 position: "absolute", top: 0, left: 0,
 width: "100%", height: "100%", border: "none",
 }}
 />
 </div>
 )}

 {source.kind === "mp4" && (
 <video
 key={mountKey}
 src={buildMp4Src(source.embedUrl, startSeconds, endSeconds)}
 controls
 autoPlay
 onTimeUpdate={(e) => {
 // Safety-net auto-stop. The #t=N,M media fragment SHOULD make
 // the browser stop at M, but Safari + some embedded HLS proxies
 // ignore the end. Watch currentTime and pause once we cross
 // endSeconds. Pause-not-deactivate so the operator can scrub
 // back inside the same segment without re-clicking Listen.
 const el = e.currentTarget;
 if (el.currentTime >= endSeconds && !el.paused) {
 el.pause();
 }
 }}
 style={{
 display: "block", width: "100%", maxHeight: 360,
 background: "black",
 }}
 />
 )}

 {source.kind === "granicus-iframe" && (
 <div>
 <div style={{
 position: "relative", width: "100%", paddingBottom: "56.25%",
 }}>
 <iframe
 key={mountKey}
 src={granicusUrlWithStartTime(source.embedUrl, startSeconds)}
 title="Meeting excerpt (Granicus)"
 allow="autoplay; encrypted-media"
 allowFullScreen
 style={{
 position: "absolute", top: 0, left: 0,
 width: "100%", height: "100%", border: "none",
 background: "black",
 }}
 />
 </div>
 <div style={{
 fontSize: 11, color: "var(--text-secondary, #8b95a8)",
 padding: "6px 10px",
 background: "rgba(148, 163, 184, 0.05)",
 fontStyle: "italic",
 }}>
 Granicus MediaPlayer doesn&rsquo;t auto-stop at the segment end —
 pause when you&rsquo;ve identified the voice.
 </div>
 </div>
 )}
 </div>
 );
}

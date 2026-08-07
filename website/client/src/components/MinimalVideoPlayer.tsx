/**
 * MinimalVideoPlayer — strictly broadcast-minimal video player for Z-SPAN.
 *
 * Design intent (per James): only the controls a citizen actually needs to
 * watch a meeting summary. No volume slider, no time display, no settings
 * cog, no playback-speed menu. A thin progress bar at the bottom, two
 * icons, and that's it. Click anywhere on the video surface to play/pause.
 *
 * Visible affordances (always):
 * - thin progress bar (bottom edge, hairline)
 *
 * Visible affordances (on hover, fade after 1.5s of inactivity):
 * - play / pause icon (bottom-left)
 * - fullscreen icon (bottom-right)
 *
 * Click anywhere else on the video surface toggles play/pause.
 */
import { useEffect, useRef, useState } from "react";
import { Play, Pause, Maximize2, Minimize2 } from "lucide-react";

interface Props {
 src: string;
 className?: string;
}

export default function MinimalVideoPlayer({ src, className = "" }: Props) {
 const videoRef = useRef<HTMLVideoElement>(null);
 const containerRef = useRef<HTMLDivElement>(null);
 const hideControlsTimer = useRef<number | null>(null);

 const [playing, setPlaying] = useState(false);
 const [progress, setProgress] = useState(0); // 0..1
 const [hovering, setHovering] = useState(false);
 const [fullscreen, setFullscreen] = useState(false);
 const [showControls, setShowControls] = useState(true);

 // ── Sync internal state with the underlying <video> ──────────────
 useEffect(() => {
 const v = videoRef.current;
 if (!v) return;
 const onPlay = () => setPlaying(true);
 const onPause = () => setPlaying(false);
 const onTimeUpdate = () => {
 if (v.duration > 0) setProgress(v.currentTime / v.duration);
 };
 v.addEventListener("play", onPlay);
 v.addEventListener("pause", onPause);
 v.addEventListener("timeupdate", onTimeUpdate);
 return () => {
 v.removeEventListener("play", onPlay);
 v.removeEventListener("pause", onPause);
 v.removeEventListener("timeupdate", onTimeUpdate);
 };
 }, []);

 // ── Track fullscreen state ───────────────────────────────────────
 useEffect(() => {
 const onFs = () => setFullscreen(!!document.fullscreenElement);
 document.addEventListener("fullscreenchange", onFs);
 return () => document.removeEventListener("fullscreenchange", onFs);
 }, []);

 // ── Auto-hide controls after 1.5s idle while playing ─────────────
 useEffect(() => {
 if (hideControlsTimer.current !== null) {
 window.clearTimeout(hideControlsTimer.current);
 hideControlsTimer.current = null;
 }
 if (!playing) {
 setShowControls(true);
 return;
 }
 if (!hovering) {
 setShowControls(false);
 return;
 }
 setShowControls(true);
 hideControlsTimer.current = window.setTimeout(() => {
 setShowControls(false);
 }, 1500);
 return () => {
 if (hideControlsTimer.current !== null) {
 window.clearTimeout(hideControlsTimer.current);
 }
 };
 }, [playing, hovering, progress]);

 const togglePlay = () => {
 const v = videoRef.current;
 if (!v) return;
 if (v.paused) v.play().catch(() => {});
 else v.pause();
 };

 const toggleFullscreen = () => {
 const c = containerRef.current;
 if (!c) return;
 if (!document.fullscreenElement) {
 c.requestFullscreen().catch(() => {});
 } else {
 document.exitFullscreen().catch(() => {});
 }
 };

 const seekTo = (e: React.MouseEvent<HTMLDivElement>) => {
 const v = videoRef.current;
 const bar = e.currentTarget;
 if (!v || !bar || v.duration === 0) return;
 const rect = bar.getBoundingClientRect();
 const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
 v.currentTime = ratio * v.duration;
 };

 return (
 <div
 ref={containerRef}
 className={`relative bg-black overflow-hidden group ${className}`}
 onMouseEnter={() => setHovering(true)}
 onMouseLeave={() => setHovering(false)}
 onMouseMove={() => setHovering(true)}
 >
 <video
 ref={videoRef}
 src={src}
 className="w-full h-full"
 onClick={togglePlay}
 playsInline
 />

 {/* Center play affordance — only when paused */}
 {!playing && (
 <button
 onClick={togglePlay}
 className="absolute inset-0 flex items-center justify-center transition-opacity"
 aria-label="Play"
 >
 <span className="w-16 h-16 rounded-full bg-black/40 backdrop-blur-md border border-white/20 flex items-center justify-center group-hover:bg-black/60 transition-colors">
 <Play className="w-6 h-6 text-white ml-1 fill-white" />
 </span>
 </button>
 )}

 {/* Bottom control strip */}
 <div
 className={`absolute left-0 right-0 bottom-0 transition-opacity duration-200 ${
 showControls ? "opacity-100" : "opacity-0 pointer-events-none"
 }`}
 >
 {/* Progress bar — clickable for scrubbing. Hairline, full width. */}
 <div
 onClick={seekTo}
 className="h-[3px] bg-white/10 cursor-pointer group/bar"
 role="slider"
 aria-label="Seek"
 aria-valuemin={0}
 aria-valuemax={100}
 aria-valuenow={Math.round(progress * 100)}
 >
 <div
 className="h-full bg-white/85 group-hover/bar:bg-white transition-colors"
 style={{ width: `${progress * 100}%` }}
 />
 </div>

 {/* Icon row */}
 <div className="flex items-center justify-between px-3 py-2 bg-gradient-to-t from-black/70 to-transparent">
 <button
 onClick={togglePlay}
 className="w-8 h-8 flex items-center justify-center text-white/85 hover:text-white transition-colors"
 aria-label={playing ? "Pause" : "Play"}
 >
 {playing ? (
 <Pause className="w-4 h-4" />
 ) : (
 <Play className="w-4 h-4 ml-0.5 fill-current" />
 )}
 </button>

 <button
 onClick={toggleFullscreen}
 className="w-8 h-8 flex items-center justify-center text-white/85 hover:text-white transition-colors"
 aria-label={fullscreen ? "Exit fullscreen" : "Enter fullscreen"}
 >
 {fullscreen ? (
 <Minimize2 className="w-4 h-4" />
 ) : (
 <Maximize2 className="w-4 h-4" />
 )}
 </button>
 </div>
 </div>
 </div>
 );
}

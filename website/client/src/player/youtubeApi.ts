/**
 * youtubeApi — shared loader + minimal typings for the YouTube IFrame
 * Player API.
 *
 * PLAYER-1 (2026-07-07): lifted from SyncedQuote so every consumer of a
 * YouTube playback surface (the ZspanPlayer youtube adapter, SyncedQuote's
 * span player) shares ONE deduped script load and one set of type stubs.
 *
 * The API loads via a global script that sets `window.YT` when ready.
 * Multiple components racing to load the same script would each install a
 * different `onYouTubeIframeAPIReady` handler; we dedupe with a
 * module-level promise and chain any pre-existing handler.
 *
 * Types are intentionally minimal — we use a handful of methods and don't
 * want @types/youtube as a dependency. Kept LOCAL (no `declare global`)
 * so this module never collides with other files' Window augmentations.
 */

export interface YTPlayer {
 destroy: () => void;
 seekTo: (seconds: number, allowSeekAhead?: boolean) => void;
 playVideo: () => void;
 pauseVideo: () => void;
 stopVideo: () => void;
 getCurrentTime: () => number;
 getPlayerState: () => number;
}

export interface YTPlayerOptions {
 videoId: string;
 playerVars?: Record<string, string | number>;
 events?: {
 onReady?: (e: { target: YTPlayer }) => void;
 onStateChange?: (e: { data: number; target: YTPlayer }) => void;
 onError?: (e: { data: number }) => void;
 };
}

export interface YTNamespace {
 Player: new (element: HTMLElement | string, opts: YTPlayerOptions) => YTPlayer;
 PlayerState: { ENDED: 0; PLAYING: 1; PAUSED: 2; BUFFERING: 3; CUED: 5 };
}

type YTWindow = Window & {
 YT?: YTNamespace;
 onYouTubeIframeAPIReady?: () => void;
};

let _ytLoadPromise: Promise<YTNamespace> | null = null;

export function loadYouTubeAPI(): Promise<YTNamespace> {
 if (typeof window === "undefined") {
 return Promise.reject(new Error("SSR — no window"));
 }
 const w = window as YTWindow;
 if (w.YT && w.YT.Player) {
 return Promise.resolve(w.YT);
 }
 if (_ytLoadPromise) return _ytLoadPromise;

 _ytLoadPromise = new Promise((resolve) => {
 const prev = w.onYouTubeIframeAPIReady;
 w.onYouTubeIframeAPIReady = () => {
 prev?.();
 if (w.YT) resolve(w.YT);
 };

 const already = document.querySelector(
 'script[src="https://www.youtube.com/iframe_api"]',
 );
 if (!already) {
 const script = document.createElement("script");
 script.src = "https://www.youtube.com/iframe_api";
 script.async = true;
 document.head.appendChild(script);
 }
 });

 return _ytLoadPromise;
}

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
        onReady?: (e: {
            target: YTPlayer;
        }) => void;
        onStateChange?: (e: {
            data: number;
            target: YTPlayer;
        }) => void;
        onError?: (e: {
            data: number;
        }) => void;
    };
}
export interface YTNamespace {
    Player: new (element: HTMLElement | string, opts: YTPlayerOptions) => YTPlayer;
    PlayerState: {
        ENDED: 0;
        PLAYING: 1;
        PAUSED: 2;
        BUFFERING: 3;
        CUED: 5;
    };
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
    if (_ytLoadPromise)
        return _ytLoadPromise;
    _ytLoadPromise = new Promise((resolve) => {
        const prev = w.onYouTubeIframeAPIReady;
        w.onYouTubeIframeAPIReady = () => {
            prev?.();
            if (w.YT)
                resolve(w.YT);
        };
        const already = document.querySelector('script[src="https://www.youtube.com/iframe_api"]');
        if (!already) {
            const script = document.createElement("script");
            script.src = "https://www.youtube.com/iframe_api";
            script.async = true;
            document.head.appendChild(script);
        }
    });
    return _ytLoadPromise;
}

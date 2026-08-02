import type { VideoSource } from "../lib/videoSource";
import { granicusUrlWithStartTime } from "../lib/videoSource";
import { loadYouTubeAPI, type YTPlayer } from "./youtubeApi";
export interface AdapterMountOpts {
    startSeconds?: number;
    endSeconds?: number;
    autoplay?: boolean;
}
export interface PlayerError {
    code?: number;
}
export interface PlayerAdapter {
    readonly kind: NonNullable<VideoSource>["kind"];
    readonly hasClock: boolean;
    mount(host: HTMLElement, opts?: AdapterMountOpts): void;
    destroy(): void;
    play(): void;
    pause(): void;
    seekTo(seconds: number, opts?: {
        andPlay?: boolean;
    }): void;
    getCurrentTime(): number | null;
    onError(cb: (err: PlayerError) => void): void;
    onEnded(cb: () => void): void;
}
class YouTubeAdapter implements PlayerAdapter {
    readonly kind = "youtube" as const;
    readonly hasClock = true;
    private player: YTPlayer | null = null;
    private ready = false;
    private cancelled = false;
    private errorCb: ((err: PlayerError) => void) | null = null;
    private errorFired = false;
    private endedCb: (() => void) | null = null;
    private pendingSeek: {
        seconds: number;
        andPlay?: boolean;
    } | null = null;
    constructor(private videoId: string) { }
    mount(host: HTMLElement, opts: AdapterMountOpts = {}): void {
        this.cancelled = false;
        loadYouTubeAPI().then((YT) => {
            if (this.cancelled)
                return;
            const inner = document.createElement("div");
            inner.className = "w-full h-full";
            host.innerHTML = "";
            host.appendChild(inner);
            const playerVars: Record<string, string | number> = {
                rel: 0,
                modestbranding: 1,
                playsinline: 1,
            };
            if (typeof opts.startSeconds === "number") {
                playerVars.start = Math.floor(opts.startSeconds);
            }
            if (typeof opts.endSeconds === "number") {
                playerVars.end = Math.ceil(opts.endSeconds + 1);
            }
            if (opts.autoplay)
                playerVars.autoplay = 1;
            this.player = new YT.Player(inner, {
                videoId: this.videoId,
                playerVars,
                events: {
                    onReady: (e) => {
                        if (this.cancelled)
                            return;
                        this.ready = true;
                        if (this.pendingSeek) {
                            const { seconds, andPlay } = this.pendingSeek;
                            this.pendingSeek = null;
                            e.target.seekTo(seconds, true);
                            if (andPlay)
                                e.target.playVideo();
                            return;
                        }
                        if (typeof opts.startSeconds === "number") {
                            e.target.seekTo(opts.startSeconds, true);
                            if (opts.autoplay)
                                e.target.playVideo();
                        }
                    },
                    onStateChange: (e) => {
                        if (e.data === 0)
                            this.endedCb?.();
                    },
                    onError: (e) => this.fireError({ code: e.data }),
                },
            });
        });
    }
    private fireError(err: PlayerError) {
        if (this.errorFired)
            return;
        this.errorFired = true;
        this.errorCb?.(err);
    }
    destroy(): void {
        this.cancelled = true;
        const p = this.player;
        this.player = null;
        if (p && typeof p.destroy === "function") {
            try {
                p.destroy();
            }
            catch {
            }
        }
    }
    play(): void {
        try {
            this.player?.playVideo();
        }
        catch {
        }
    }
    pause(): void {
        try {
            this.player?.pauseVideo();
        }
        catch {
        }
    }
    seekTo(seconds: number, opts?: {
        andPlay?: boolean;
    }): void {
        if (!this.ready || !this.player) {
            this.pendingSeek = { seconds, andPlay: opts?.andPlay };
            return;
        }
        try {
            this.player.seekTo(seconds, true);
            if (opts?.andPlay)
                this.player.playVideo();
        }
        catch {
        }
    }
    getCurrentTime(): number | null {
        try {
            const t = this.player?.getCurrentTime();
            return typeof t === "number" && Number.isFinite(t) ? t : null;
        }
        catch {
            return null;
        }
    }
    onError(cb: (err: PlayerError) => void): void {
        this.errorCb = cb;
    }
    onEnded(cb: () => void): void {
        this.endedCb = cb;
    }
}
class Html5Adapter implements PlayerAdapter {
    readonly kind = "mp4" as const;
    readonly hasClock = true;
    private video: HTMLVideoElement | null = null;
    private endSeconds: number | null = null;
    private errorCb: ((err: PlayerError) => void) | null = null;
    private errorFired = false;
    private endedCb: (() => void) | null = null;
    private onTimeUpdate: (() => void) | null = null;
    constructor(private src: string) { }
    mount(host: HTMLElement, opts: AdapterMountOpts = {}): void {
        const video = document.createElement("video");
        video.src = this.src;
        video.controls = true;
        video.preload = "metadata";
        video.className = "w-full h-full bg-black";
        video.setAttribute("data-z-player", "1");
        const track = document.createElement("track");
        track.kind = "captions";
        video.appendChild(track);
        if (typeof opts.startSeconds === "number") {
            const s = opts.startSeconds;
            video.addEventListener("loadedmetadata", () => {
                video.currentTime = s;
                if (opts.autoplay)
                    void video.play().catch(() => { });
            }, { once: true });
        }
        else if (opts.autoplay) {
            video.autoplay = true;
        }
        if (typeof opts.endSeconds === "number") {
            this.endSeconds = opts.endSeconds;
            this.onTimeUpdate = () => {
                if (this.endSeconds !== null && video.currentTime >= this.endSeconds) {
                    video.pause();
                }
            };
            video.addEventListener("timeupdate", this.onTimeUpdate);
        }
        video.addEventListener("error", () => {
            if (!this.errorFired) {
                this.errorFired = true;
                this.errorCb?.({});
            }
        }, { once: true });
        video.addEventListener("ended", () => this.endedCb?.());
        host.innerHTML = "";
        host.appendChild(video);
        this.video = video;
    }
    destroy(): void {
        const v = this.video;
        this.video = null;
        if (v) {
            if (this.onTimeUpdate)
                v.removeEventListener("timeupdate", this.onTimeUpdate);
            try {
                v.pause();
                v.removeAttribute("src");
                v.load();
            }
            catch {
            }
            v.remove();
        }
    }
    play(): void {
        void this.video?.play().catch(() => {
        });
    }
    pause(): void {
        this.video?.pause();
    }
    seekTo(seconds: number, opts?: {
        andPlay?: boolean;
    }): void {
        if (!this.video)
            return;
        this.video.currentTime = seconds;
        if (opts?.andPlay)
            this.play();
    }
    getCurrentTime(): number | null {
        const t = this.video?.currentTime;
        return typeof t === "number" && Number.isFinite(t) ? t : null;
    }
    onError(cb: (err: PlayerError) => void): void {
        this.errorCb = cb;
    }
    onEnded(cb: () => void): void {
        this.endedCb = cb;
    }
}
class GranicusIframeAdapter implements PlayerAdapter {
    readonly kind = "granicus-iframe" as const;
    readonly hasClock = false;
    private iframe: HTMLIFrameElement | null = null;
    constructor(private baseUrl: string, private title: string) { }
    mount(host: HTMLElement, opts: AdapterMountOpts = {}): void {
        const iframe = document.createElement("iframe");
        iframe.title = this.title;
        iframe.src =
            typeof opts.startSeconds === "number" && opts.startSeconds > 0
                ? granicusUrlWithStartTime(this.baseUrl, opts.startSeconds)
                : this.baseUrl;
        iframe.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
        iframe.allowFullscreen = true;
        iframe.className = "w-full h-full";
        iframe.setAttribute("data-z-player", "1");
        host.innerHTML = "";
        host.appendChild(iframe);
        this.iframe = iframe;
    }
    destroy(): void {
        this.iframe?.remove();
        this.iframe = null;
    }
    play(): void { }
    pause(): void { }
    seekTo(seconds: number): void {
        if (!this.iframe)
            return;
        this.iframe.src = granicusUrlWithStartTime(this.baseUrl, seconds);
    }
    getCurrentTime(): number | null {
        return null;
    }
    onError(): void {
    }
    onEnded(): void {
    }
}
class ExternalLinkAdapter implements PlayerAdapter {
    readonly kind = "external-link" as const;
    readonly hasClock = false;
    mount(): void {
    }
    destroy(): void { }
    play(): void { }
    pause(): void { }
    seekTo(): void { }
    getCurrentTime(): number | null {
        return null;
    }
    onError(): void { }
    onEnded(): void { }
}
export function createAdapter(source: NonNullable<VideoSource>, opts?: {
    title?: string;
}): PlayerAdapter {
    switch (source.kind) {
        case "youtube":
            return new YouTubeAdapter(source.videoId);
        case "mp4":
            return new Html5Adapter(source.embedUrl);
        case "granicus-iframe":
            return new GranicusIframeAdapter(source.embedUrl, opts?.title ?? "Meeting recording (Granicus)");
        case "external-link":
            return new ExternalLinkAdapter();
    }
}

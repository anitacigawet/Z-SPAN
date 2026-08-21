/**
 * player/adapters — the source-adapter layer under ZspanPlayer (PLAYER-1).
 *
 * One interface — play · pause · seekTo · getCurrentTime · onError —
 * over every way a meeting recording can reach the viewer. Bytes always
 * flow source → viewer directly (zero Z-SPAN egress by construction);
 * the adapter only needs a READABLE PLAYBACK CLOCK for karaoke, never
 * the video bytes.
 *
 * Adapter kinds mirror lib/videoSource's classifier:
 *
 *   youtube          — YouTube IFrame Player API. Their surface, our
 *                      chrome + a real clock (getCurrentTime) + real
 *                      error events (101/150 embed-disabled etc.), which
 *                      replaces the old raw-iframe + postMessage-bridge +
 *                      "listening"-handshake stack.
 *   html5            — native <video> for direct MP4 (Granicus direct
 *                      archives). Fully ours; clock is currentTime.
 *   granicus-iframe  — Granicus MediaPlayer.php page. Plays fine; the
 *                      cross-origin iframe exposes NO clock, so seek is
 *                      a ?starttime URL rewrite and hasClock is false
 *                      (no karaoke until a direct MP4 resolves).
 *   external-link    — no embeddable surface at all. The shell renders
 *                      an open-externally panel; seek is a no-op.
 *
 * Mount options carry span playback (start/end) for excerpt-style
 * consumers (SyncedQuote's quote spans, MeetingExcerptPlayer) so the
 * span logic lives once, per-adapter.
 */

import type { VideoSource } from "../lib/videoSource";
import { granicusUrlWithStartTime } from "../lib/videoSource";
import { loadYouTubeAPI, type YTPlayer } from "./youtubeApi";

export interface AdapterMountOpts {
  /** Seek here as soon as the surface is ready. */
  startSeconds?: number;
  /** Span playback: stop/pause at this offset (adapter-enforced where
   *  the platform supports it; clock-bearing consumers can also enforce
   *  it themselves via onTime loops). */
  endSeconds?: number;
  autoplay?: boolean;
}

export interface PlayerError {
  /** YouTube error code when known (101/150 = embed disabled;
   *  100 = removed; 2/5 = unplayable). Absent for non-YT failures. */
  code?: number;
}

export interface PlayerAdapter {
  readonly kind: NonNullable<VideoSource>["kind"];
  /** True when getCurrentTime returns a live playback clock — the only
   *  thing karaoke needs. */
  readonly hasClock: boolean;
  /** Create the playback surface inside `host`. Idempotent-hostile:
   *  call once; destroy() before re-mounting. */
  mount(host: HTMLElement, opts?: AdapterMountOpts): void;
  destroy(): void;
  play(): void;
  pause(): void;
  seekTo(seconds: number, opts?: { andPlay?: boolean }): void;
  /** Seconds, or null when the clock isn't readable (no surface yet,
   *  clockless adapter). */
  getCurrentTime(): number | null;
  /** Playback-surface failure (S-103 class). Fires at most once. */
  onError(cb: (err: PlayerError) => void): void;
  /** Playback reached the end of the media (not the span end — span
   *  consumers watch the clock for that). No-op on clockless adapters. */
  onEnded(cb: () => void): void;
}

// ── YouTube ─────────────────────────────────────────────────────────

class YouTubeAdapter implements PlayerAdapter {
  readonly kind = "youtube" as const;
  readonly hasClock = true;
  private player: YTPlayer | null = null;
  private ready = false;
  private cancelled = false;
  private errorCb: ((err: PlayerError) => void) | null = null;
  private errorFired = false;
  private endedCb: (() => void) | null = null;
  /** Seek requested before the player was ready (e.g. a ?t= deep-link
   *  arrival racing the IFrame API bootstrap) — applied at onReady so
   *  the arrival is never lost. */
  private pendingSeek: { seconds: number; andPlay?: boolean } | null = null;

  constructor(private videoId: string) {}

  mount(host: HTMLElement, opts: AdapterMountOpts = {}): void {
    this.cancelled = false;
    loadYouTubeAPI().then((YT) => {
      if (this.cancelled) return;
      // The YT constructor REPLACES the element. Create an inner node
      // each time so the React-managed host stays intact and can
      // re-host a new iframe on the next mount.
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
        // +1s headroom; span consumers auto-stop earlier off the clock.
        playerVars.end = Math.ceil(opts.endSeconds + 1);
      }
      if (opts.autoplay) playerVars.autoplay = 1;

      this.player = new YT.Player(inner, {
        videoId: this.videoId,
        playerVars,
        events: {
          onReady: (e) => {
            if (this.cancelled) return;
            this.ready = true;
            if (this.pendingSeek) {
              const { seconds, andPlay } = this.pendingSeek;
              this.pendingSeek = null;
              e.target.seekTo(seconds, true);
              if (andPlay) e.target.playVideo();
              return;
            }
            // `start` rounds down to whole seconds; span consumers want
            // the precise offset.
            if (typeof opts.startSeconds === "number") {
              e.target.seekTo(opts.startSeconds, true);
              if (opts.autoplay) e.target.playVideo();
            }
          },
          onStateChange: (e) => {
            // YT.PlayerState.ENDED === 0
            if (e.data === 0) this.endedCb?.();
          },
          onError: (e) => this.fireError({ code: e.data }),
        },
      });
    });
  }

  private fireError(err: PlayerError) {
    if (this.errorFired) return;
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
      } catch {
        // best-effort
      }
    }
  }

  play(): void {
    try {
      this.player?.playVideo();
    } catch {
      /* not ready yet */
    }
  }

  pause(): void {
    try {
      this.player?.pauseVideo();
    } catch {
      /* not ready yet */
    }
  }

  seekTo(seconds: number, opts?: { andPlay?: boolean }): void {
    if (!this.ready || !this.player) {
      this.pendingSeek = { seconds, andPlay: opts?.andPlay };
      return;
    }
    try {
      this.player.seekTo(seconds, true);
      if (opts?.andPlay) this.player.playVideo();
    } catch {
      /* transient player-state hiccup; drop rather than throw */
    }
  }

  getCurrentTime(): number | null {
    try {
      const t = this.player?.getCurrentTime();
      return typeof t === "number" && Number.isFinite(t) ? t : null;
    } catch {
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

// ── HTML5 (direct MP4) ──────────────────────────────────────────────

class Html5Adapter implements PlayerAdapter {
  readonly kind = "mp4" as const;
  readonly hasClock = true;
  private video: HTMLVideoElement | null = null;
  private endSeconds: number | null = null;
  private errorCb: ((err: PlayerError) => void) | null = null;
  private errorFired = false;
  private endedCb: (() => void) | null = null;
  private onTimeUpdate: (() => void) | null = null;

  constructor(private src: string) {}

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
      video.addEventListener(
        "loadedmetadata",
        () => {
          video.currentTime = s;
          if (opts.autoplay) void video.play().catch(() => {});
        },
        { once: true },
      );
    } else if (opts.autoplay) {
      video.autoplay = true;
    }

    if (typeof opts.endSeconds === "number") {
      // Span playback: pause at the end bound (media-fragment #t=N,M
      // stops but keeps controls odd across browsers; timeupdate is
      // deterministic).
      this.endSeconds = opts.endSeconds;
      this.onTimeUpdate = () => {
        if (this.endSeconds !== null && video.currentTime >= this.endSeconds) {
          video.pause();
        }
      };
      video.addEventListener("timeupdate", this.onTimeUpdate);
    }

    video.addEventListener(
      "error",
      () => {
        if (!this.errorFired) {
          this.errorFired = true;
          this.errorCb?.({});
        }
      },
      { once: true },
    );
    video.addEventListener("ended", () => this.endedCb?.());

    host.innerHTML = "";
    host.appendChild(video);
    this.video = video;
  }

  destroy(): void {
    const v = this.video;
    this.video = null;
    if (v) {
      if (this.onTimeUpdate) v.removeEventListener("timeupdate", this.onTimeUpdate);
      try {
        v.pause();
        v.removeAttribute("src");
        v.load();
      } catch {
        // best-effort
      }
      v.remove();
    }
  }

  play(): void {
    void this.video?.play().catch(() => {
      /* autoplay-blocked is fine; user clicks play */
    });
  }

  pause(): void {
    this.video?.pause();
  }

  seekTo(seconds: number, opts?: { andPlay?: boolean }): void {
    if (!this.video) return;
    this.video.currentTime = seconds;
    if (opts?.andPlay) this.play();
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

// ── Granicus MediaPlayer iframe ─────────────────────────────────────

class GranicusIframeAdapter implements PlayerAdapter {
  readonly kind = "granicus-iframe" as const;
  /** The cross-origin JWPlayer page exposes no clock — karaoke stays
   *  off for this source until a direct MP4 resolves for the meeting. */
  readonly hasClock = false;
  private iframe: HTMLIFrameElement | null = null;

  constructor(
    private baseUrl: string,
    private title: string,
  ) {}

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

  /** No programmatic control over the cross-origin JWPlayer; play/pause
   *  are the user's clicks inside the iframe. */
  play(): void {}
  pause(): void {}

  seekTo(seconds: number): void {
    if (!this.iframe) return;
    // Seek = reload the page player at the offset, preserving all other
    // query params (the pre-PLAYER-1 BroadcastPage behavior).
    this.iframe.src = granicusUrlWithStartTime(this.baseUrl, seconds);
  }

  getCurrentTime(): number | null {
    return null;
  }

  onError(): void {
    /* no error signal crosses the iframe boundary */
  }

  onEnded(): void {
    /* no playback lifecycle crosses the iframe boundary */
  }
}

// ── External link (no embeddable surface) ───────────────────────────

class ExternalLinkAdapter implements PlayerAdapter {
  readonly kind = "external-link" as const;
  readonly hasClock = false;

  mount(): void {
    /* nothing to mount — the shell renders the open-externally panel */
  }
  destroy(): void {}
  play(): void {}
  pause(): void {}
  /** No seek vocabulary exists for arbitrary vendor pages; citation
   *  chips stay visible as timecode references (pre-PLAYER-1 behavior). */
  seekTo(): void {}
  getCurrentTime(): number | null {
    return null;
  }
  onError(): void {}
  onEnded(): void {}
}

// ── Factory ─────────────────────────────────────────────────────────

export function createAdapter(
  source: NonNullable<VideoSource>,
  opts?: { title?: string },
): PlayerAdapter {
  switch (source.kind) {
    case "youtube":
      return new YouTubeAdapter(source.videoId);
    case "mp4":
      return new Html5Adapter(source.embedUrl);
    case "granicus-iframe":
      return new GranicusIframeAdapter(
        source.embedUrl,
        opts?.title ?? "Meeting recording (Granicus)",
      );
    case "external-link":
      return new ExternalLinkAdapter();
  }
}

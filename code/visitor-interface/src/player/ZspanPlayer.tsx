import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, } from "react";
import { Youtube } from "lucide-react";
import { getVideoSource } from "../lib/videoSource";
import { fetchForPlane } from "../lib/planeFetch";
import { createAdapter, type PlayerAdapter } from "./adapters";
import KaraokeStrip, { type QuoteWordTiming } from "./KaraokeStrip";
export interface ZspanPlayerHandle {
    seekTo(seconds: number, opts?: {
        andPlay?: boolean;
    }): void;
    play(): void;
    pause(): void;
    getCurrentTime(): number | null;
}
interface ZspanPlayerProps {
    videoUrl: string | null | undefined;
    cityName?: string | null;
    title?: string;
    className?: string;
    autoplay?: boolean;
    startSeconds?: number;
    endSeconds?: number;
    externalLabel?: string;
    karaoke?: {
        wordTimings: QuoteWordTiming[];
        markerColor?: string;
        activeWordClassName?: string;
        className?: string;
    };
}
const ZspanPlayer = forwardRef<ZspanPlayerHandle, ZspanPlayerProps>(function ZspanPlayer({ videoUrl, cityName, title = "Full meeting recording", className = "", autoplay = false, startSeconds, endSeconds, externalLabel = "The full meeting recording is hosted on the city's video platform.", karaoke, }, ref) {
    const hostRef = useRef<HTMLDivElement | null>(null);
    const adapterRef = useRef<PlayerAdapter | null>(null);
    const [embedError, setEmbedError] = useState(false);
    const source = useMemo(() => getVideoSource(videoUrl), [videoUrl]);
    const hasClock = !!source && (source.kind === "youtube" || source.kind === "mp4");
    const getCurrentTimeMs = useCallback(() => {
        const t = adapterRef.current?.getCurrentTime();
        return typeof t === "number" ? t * 1000 : null;
    }, []);
    useEffect(() => {
        setEmbedError(false);
        if (!source || source.kind === "external-link")
            return;
        const host = hostRef.current;
        if (!host)
            return;
        const adapter = createAdapter(source, { title });
        adapterRef.current = adapter;
        adapter.onError(() => setEmbedError(true));
        adapter.mount(host, { startSeconds, endSeconds, autoplay });
        return () => {
            adapterRef.current = null;
            adapter.destroy();
            host.innerHTML = "";
        };
    }, [source?.raw]);
    useEffect(() => {
        if (!source || source.kind !== "youtube" || !source.videoId)
            return;
        let cancelled = false;
        fetchForPlane({
            publicPath: `/public-api/youtube/embed-check?video_id=${encodeURIComponent(source.videoId)}`,
            operatorPath: `/api/youtube/embed-check?video_id=${encodeURIComponent(source.videoId)}`,
        })
            .then((r) => (r.ok ? r.json() : null))
            .then((body) => {
            if (cancelled || !body)
                return;
            if (body.embeddable === false)
                setEmbedError(true);
        })
            .catch(() => {
        });
        return () => {
            cancelled = true;
        };
    }, [source?.raw, source?.kind]);
    useImperativeHandle(ref, () => ({
        seekTo(seconds: number, opts?: {
            andPlay?: boolean;
        }) {
            if (!source)
                return;
            if (source.kind === "youtube" && embedError) {
                const t = Math.max(0, Math.floor(seconds));
                window.open(`https://youtu.be/${source.videoId}?t=${t}`, "_blank", "noopener,noreferrer");
                return;
            }
            adapterRef.current?.seekTo(seconds, opts);
        },
        play() {
            adapterRef.current?.play();
        },
        pause() {
            adapterRef.current?.pause();
        },
        getCurrentTime() {
            return adapterRef.current?.getCurrentTime() ?? null;
        },
    }), [source, embedError]);
    if (!source) {
        return (<div className={`flex items-center justify-center h-full text-center text-gray-500 text-sm ${className}`}>
          Source recording link not available for this meeting.
        </div>);
    }
    if (source.kind === "external-link") {
        return (<div className={`flex items-center justify-center h-full text-center px-6 ${className}`}>
          <div>
            <Youtube className="w-8 h-8 mx-auto mb-3 text-gray-500"/>
            <p className="text-sm text-gray-400 mb-3">{externalLabel}</p>
            <a href={source.raw} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2 px-4 py-2 bg-white text-black rounded-md text-[11px] font-semibold uppercase tracking-widest hover:bg-gray-200">
              Open Full Meeting
            </a>
          </div>
        </div>);
    }
    return (<div className={`relative w-full h-full ${className}`}>
        <div ref={hostRef} className="w-full h-full"/>
        {karaoke && hasClock && !embedError && (<div className="absolute inset-x-0 bottom-0 px-4 py-2 bg-gradient-to-t from-black/85 to-transparent pointer-events-none">
            <KaraokeStrip wordTimings={karaoke.wordTimings} getCurrentTimeMs={getCurrentTimeMs} running markerColor={karaoke.markerColor} activeWordClassName={karaoke.activeWordClassName} className={karaoke.className ??
                "text-[13px] text-white/90 leading-snug text-center"} quoted={false}/>
          </div>)}
        {source.kind === "youtube" && embedError && (<div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/85 text-center px-6 backdrop-blur-sm" role="alert">
            <div className="text-[13px] uppercase tracking-widest text-amber-300/80">
              Embedding disabled
            </div>
            <p className="max-w-md text-[15px] leading-relaxed text-white/90">
              {cityName ? <>{cityName}</> : <>This city</>} has disabled YouTube
              embedding; citations will open in a new tab instead of here.
            </p>
            <a href={`https://youtu.be/${source.videoId}`} target="_blank" rel="noopener noreferrer" className="mt-2 inline-flex items-center gap-2 rounded border border-amber-400/50 bg-amber-400/10 px-4 py-2 text-[13px] font-medium text-amber-200 transition hover:border-amber-400 hover:bg-amber-400/20">
              Open on YouTube ↗
            </a>
          </div>)}
      </div>);
});
export default ZspanPlayer;

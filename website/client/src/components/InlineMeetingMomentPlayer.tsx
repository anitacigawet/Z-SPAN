/**
 * InlineMeetingMomentPlayer — embedded video at a specific moment.
 *
 * Built for V1.5-OperatorSearch-1 Z4 (2026-06-25): when the operator
 * expands a citation chip in the modal and clicks "Play this moment"
 * on one of the chunk sub-cards, this component renders inline below
 * the button and starts playing the video seeked to the chunk's
 * start_seconds.
 *
 * Click-to-play (no autoplay surprise): the parent CitationCard
 * mounts this component only after the user explicitly clicks Play.
 *
 * PLAYER-1 C3 (2026-07-07): ported onto the player/ adapter layer.
 * The per-kind branches (raw YouTube iframe + a 1s-delayed postMessage
 * seek racing the player bootstrap; <video> loadedmetadata seek;
 * Granicus ?starttime rewrite) collapse into one adapter mount with
 * { startSeconds, autoplay } — the YouTube adapter's ready-queued seek
 * replaces the race hack outright. External-link and no-source chrome
 * unchanged.
 */
import { useEffect, useMemo, useRef } from "react";
import { ExternalLink } from "lucide-react";
import { getVideoSource } from "../lib/videoSource";
import { createAdapter } from "../player/adapters";

interface InlineMeetingMomentPlayerProps {
 videoUrl: string | null | undefined;
 seek: number;
}

export function InlineMeetingMomentPlayer({
 videoUrl,
 seek,
}: InlineMeetingMomentPlayerProps) {
 const source = useMemo(() => getVideoSource(videoUrl), [videoUrl]);
 const hostRef = useRef<HTMLDivElement>(null);

 const embeddable =
 !!source && source.kind !== "external-link";

 useEffect(() => {
 if (!source || !embeddable) return;
 const host = hostRef.current;
 if (!host) return;
 const adapter = createAdapter(source, { title: "Meeting moment" });
 adapter.mount(host, { startSeconds: seek, autoplay: true });
 return () => {
 adapter.destroy();
 host.innerHTML = "";
 };
 // eslint-disable-next-line react-hooks/exhaustive-deps
 }, [source?.raw, seek]);

 if (!source) {
 return (
 <div className="mt-2 rounded-md border border-white/10 bg-black/30 p-3 text-[11px] text-white/40">
 No video archive registered for this meeting.
 </div>
 );
 }

 if (source.kind === "external-link") {
 return (
 <div className="mt-2 flex items-center gap-2 rounded-md border border-white/10 bg-black/30 p-3 text-[11px] text-white/55">
 <span>
 Video archive isn't embed-supported here. Open in a new tab:
 </span>
 <a
 href={source.raw}
 target="_blank"
 rel="noopener noreferrer"
 className="inline-flex items-center gap-1 text-emerald-300 transition-colors hover:text-emerald-200"
 >
 <span>Open recording</span>
 <ExternalLink className="h-3 w-3" />
 </a>
 </div>
 );
 }

 return (
 <div className="mt-2 overflow-hidden rounded-md border border-white/10 bg-black">
 <div className="relative aspect-video w-full">
 <div ref={hostRef} className="absolute inset-0 h-full w-full" />
 </div>
 </div>
 );
}

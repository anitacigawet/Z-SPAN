/**
 * SSE consumer + types for the HQ skybox traffic-event viz.
 *
 * The Flask backbone (parsers/traffic_events.py) defines the canonical event
 * shape; this file is the frontend's mirror. Keep the two in sync.
 *
 * EventSource auto-reconnects on drop (default 3s) — no manual retry needed.
 */
import { useEffect } from "react";

export type TrafficEvent = {
 ts: string;
 status: number;
 path_class: "broadcast" | "guide" | "api" | "static" | "admin" | "other";
 bot_classification: "human" | "verified_bot" | "likely_bot" | "unknown";
 source: "flask" | "cloudflare" | "mock" | "local";
 /** Local-workspace mode only (zspan CLI `open`): what this star IS —
 * the exact pipeline step or request the local server performed.
 * `kind` names the step family (transcription / retrieval / synthesis /
 * gate / librarian / request…), `label` is the one-line human summary,
 * `detail` is the payload (the decoded segment, the retrieval query,
 * the chunk receipts, the gate's failure list). Flagship events NEVER
 * carry these — visitor traffic stays contentless by design; the only
 * person who can read a payload star is the machine's own user. */
 kind?: string;
 label?: string;
 detail?: string;
};

/**
 * Subscribe to the live traffic-event stream. `onEvent` MUST be stable across
 * renders (memoize via useCallback with empty deps + ref pattern) — every
 * change reconnects the EventSource.
 */
export function useTrafficEventStream(
 onEvent: (evt: TrafficEvent) => void,
): void {
 useEffect(() => {
 const es = new EventSource("/api/hq/traffic-events");
 const onMessage = (e: MessageEvent) => {
 try {
 const evt = JSON.parse(e.data) as TrafficEvent;
 onEvent(evt);
 } catch {
 // Malformed event payload — skip silently.
 }
 };
 es.addEventListener("message", onMessage);
 return () => {
 es.removeEventListener("message", onMessage);
 es.close();
 };
 }, [onEvent]);
}

/**
 * Color rule (mirrors traffic_events.py docs):
 * - red: status >= 400 OR bot_classification == "likely_bot"
 * - white: everything else
 */
export function isRedEvent(evt: TrafficEvent): boolean {
 return evt.status >= 400 || evt.bot_classification === "likely_bot";
}

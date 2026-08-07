/**
 * ParserMap — Leaflet-based US map for the parser dashboard.
 *
 * I-6 redesign per James 2026-06-02: the pure-SVG LED-grid approach
 * (I-1 through I-5) gave the broadcast-status-wall aesthetic but lost geographic
 * accuracy at scale — 78 AZ parsers blurred into a cluster blob with
 * no spatial context to anchor them. The fix is to use the Guide's
 * AggregateMap pattern (dark CartoDB tiles + state borders + city
 * labels) as the background, with the LED-style status-colored dots
 * overlaid at their actual lat/lng positions via Leaflet markers.
 *
 * The LED visual register (green=working / red=broken / yellow=
 * maintenance / hollow grey=pending + amber bloom halo) is preserved
 * — just ported from SVG <circle> elements to Leaflet DivIcons. The
 * real state borders + city labels from the dark CartoDB tiles give
 * the operator unambiguous "this cluster is in Arizona" context that
 * decorative SVG continent dots couldn't.
 *
 * Phase I chunk I-6 (2026-06-02).
 */
import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "./ParserMap.css";

// Continental US bounds — matches AggregateMap (G-7) so the parser
// view and the Guide aggregate view share the same default framing.
const US_BOUNDS: L.LatLngBoundsLiteral = [
 [24.396308, -125.0],
 [49.384358, -66.93457],
];

export type LedStatus = "working" | "broken" | "maintenance" | "pending";

export interface ParserMarker {
 /** Stable React key — typically `${state}|${county}|${city}` */
 id: string;
 /** Display label — typically the city name */
 label: string;
 /** Geographic position. If null, the marker is skipped (city not in
 * the gazetteer + county lookup also failed). */
 lng: number | null;
 lat: number | null;
 status: LedStatus;
 /** Optional metadata surfaced in the hover tooltip. */
 county?: string;
 state?: string;
 meetingCount?: number;
 lastTested?: string;
 error?: string;
}

interface TooltipState {
 marker: ParserMarker;
 /** Container-relative pixel coords for the marker's center. */
 x: number;
 y: number;
}

export default function ParserMap({
 parserMarkers = [],
}: {
 parserMarkers?: ParserMarker[];
}) {
 const mapHostRef = useRef<HTMLDivElement | null>(null);
 const mapRef = useRef<L.Map | null>(null);
 const markersLayerRef = useRef<L.LayerGroup | null>(null);
 const [tooltip, setTooltip] = useState<TooltipState | null>(null);

 // Stable ref to the latest tooltip-setter so marker event handlers
 // attached on render N still call the current setState on render N+M.
 const setTooltipRef = useRef(setTooltip);
 setTooltipRef.current = setTooltip;

 // Lazy-init the Leaflet map on first effect call + always render
 // markers. Combined into ONE effect (vs the separate init+markers
 // split that the first cut tried) because React strict mode's
 // double-mount cycle was causing a ref-race where the markers
 // effect read mapRef.current as null even though the layer ref was
 // populated. Coupling them in one effect means init and markers
 // always see consistent ref state.
 useEffect(() => {
 const host = mapHostRef.current;
 if (!host) return;

 // First call — set up the map.
 if (!mapRef.current) {
 const map = L.map(host, {
 zoomControl: true,
 attributionControl: false,
 zoomSnap: 0.25,
 minZoom: 3,
 maxZoom: 9,
 });
 L.tileLayer(
 "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
 {
 subdomains: "abcd",
 maxZoom: 18,
 attribution: "&copy; OpenStreetMap &copy; CARTO",
 },
 ).addTo(map);
 L.control.zoom({ position: "topright" }).addTo(map);
 L.control
 .attribution({ position: "bottomright", prefix: false })
 .addAttribution("&copy; OpenStreetMap &copy; CARTO")
 .addTo(map);
 mapRef.current = map;
 markersLayerRef.current = L.layerGroup().addTo(map);
 map.fitBounds(US_BOUNDS, { padding: [20, 20], animate: false });
 // Clear tooltip on pan/zoom so it doesn't drift away from its anchor.
 const clearTip = () => setTooltipRef.current(null);
 map.on("zoomstart", clearTip);
 map.on("movestart", clearTip);
 }

 // Every call — re-render the markers (cheap; the layer group's
 // clearLayers is O(1) and we re-add the same set with new data).
 const map = mapRef.current;
 const layer = markersLayerRef.current;
 if (!map || !layer) return;
 layer.clearLayers();
 for (const m of parserMarkers) {
 if (m.lng === null || m.lat === null) continue;
 const icon = L.divIcon({
 className: `parser-map-marker parser-map-marker--${m.status}`,
 html: `<span class="parser-map-marker-dot"></span>`,
 iconSize: [22, 22],
 iconAnchor: [11, 11],
 });
 const marker = L.marker([m.lat, m.lng], { icon, keyboard: false });
 marker.on("mouseover", () => {
 const lp = map.latLngToContainerPoint([m.lat as number, m.lng as number]);
 setTooltipRef.current({ marker: m, x: lp.x, y: lp.y });
 });
 marker.on("mouseout", () => {
 setTooltipRef.current((cur) => (cur?.marker.id === m.id ? null : cur));
 });
 marker.addTo(layer);
 }
 }, [parserMarkers]);

 // Cleanup-only effect — destroys the Leaflet map on real unmount.
 // Separate from the main effect so its cleanup only fires on
 // genuine unmount (not on parserMarkers changes).
 useEffect(() => {
 return () => {
 if (mapRef.current) {
 mapRef.current.remove();
 mapRef.current = null;
 markersLayerRef.current = null;
 }
 };
 }, []);

 return (
 <div className="parser-map-wrap">
 <div ref={mapHostRef} className="parser-map-host" />
 {tooltip && (
 <div
 className={`parser-map-tooltip parser-map-tooltip--${tooltip.marker.status}`}
 style={{
 left: `${tooltip.x}px`,
 top: `${tooltip.y}px`,
 }}
 role="status"
 aria-live="polite"
 >
 <div className="parser-map-tooltip-head">
 <span className="parser-map-tooltip-city">
 {tooltip.marker.label}
 </span>
 <span className="parser-map-tooltip-status">
 {tooltip.marker.status}
 </span>
 </div>
 {tooltip.marker.county && (
 <div className="parser-map-tooltip-county">
 {stripCountySuffix(tooltip.marker.county)} County
 {tooltip.marker.state ? ` · ${tooltip.marker.state}` : ""}
 </div>
 )}
 <div className="parser-map-tooltip-rule" />
 <div className="parser-map-tooltip-detail">
 {tooltip.marker.status === "broken" && tooltip.marker.error && (
 <div className="parser-map-tooltip-row parser-map-tooltip-row--error">
 Error: {tooltip.marker.error}
 </div>
 )}
 {tooltip.marker.status === "working" &&
 tooltip.marker.meetingCount != null && (
 <div className="parser-map-tooltip-row">
 {tooltip.marker.meetingCount} meeting
 {tooltip.marker.meetingCount === 1 ? "" : "s"} scraped
 </div>
 )}
 {tooltip.marker.status === "maintenance" && (
 <div className="parser-map-tooltip-row">
 Scrape succeeded but returned zero meetings — selectors
 may have drifted. Needs investigation.
 </div>
 )}
 {tooltip.marker.status === "pending" && (
 <div className="parser-map-tooltip-row parser-map-tooltip-row--muted">
 Not yet tested. Run the dashboard's sweep to fill this in.
 </div>
 )}
 {tooltip.marker.lastTested && (
 <div className="parser-map-tooltip-row parser-map-tooltip-row--muted">
 Tested {formatRelativeTime(tooltip.marker.lastTested)}
 </div>
 )}
 </div>
 </div>
 )}
 </div>
 );
}

function stripCountySuffix(name: string): string {
 return name.replace(/\s+county\s*$/i, "").trim();
}

function formatRelativeTime(iso: string): string {
 const t = Date.parse(iso);
 if (Number.isNaN(t)) return iso;
 const diffMs = Math.max(0, Date.now() - t);
 const diffMin = Math.floor(diffMs / 60_000);
 if (diffMin < 1) return "just now";
 if (diffMin < 60) return `${diffMin}m ago`;
 const hours = Math.floor(diffMin / 60);
 if (hours < 24) return `${hours}h ago`;
 const days = Math.floor(hours / 24);
 return `${days}d ago`;
}

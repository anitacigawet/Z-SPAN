/**
 * GuideCard — one live broadcast = one card.
 *
 * Per James's "Spiritual Visual Design for the Guide" (2026-06-02): each
 * card IS a Leaflet snapshot of the meeting's county, in the same dark
 * CartoDB-tile + dashed-amber-polygon aesthetic as the Traveling project's
 * Mohave-County reference image. Strip everything except the active city
 * + state name + a small meeting-type label.
 *
 * Leaflet interaction is fully disabled — this is a snapshot, not a
 * navigable map. The live tile texture comes through so the card feels
 * atmospheric (vs. a static SVG outline), but the user can't zoom or pan.
 *
 * Click handler lands in G-5 (cinematic takeover). For G-3, the card is
 * pure visual — focus-styling + cursor:pointer hint at clickability but
 * onClick is a no-op until G-5 wires the takeover.
 *
 * Phase G chunk G-3 (2026-06-02).
 */
import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { lookupCounty, lookupCity } from "@/lib/guideGeo";
import { useServerCoordsTick } from "@/data/serverCoords";

export interface GuideCardData {
 city_name: string;
 state: string | null;
 county: string | null;
 video_id: string;
 video_url: string;
 title: string | null;
 started_at: string | null;
}

const STATE_NAME_BY_ABBR: Record<string, string> = {
 AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas",
 CA: "California", CO: "Colorado", CT: "Connecticut", DE: "Delaware",
 DC: "District of Columbia", FL: "Florida", GA: "Georgia", HI: "Hawaii",
 ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas",
 KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland",
 MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi",
 MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada",
 NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York",
 NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma",
 OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina",
 SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah",
 VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia",
 WI: "Wisconsin", WY: "Wyoming",
};

interface GuideCardProps {
 data: GuideCardData;
 onSelect?: (data: GuideCardData) => void;
 /** Degrees of Y-axis rotation for the U-curve depth (G-4).
 * Computed by the parent based on card position. 0 = facing camera. */
 tiltDeg?: number;
 /** Z-axis translation (px) for the U-curve depth (G-4). Negative
 * values recede. Computed by the parent based on card position. */
 translateZ?: number;
}

export default function GuideCard({
 data,
 onSelect,
 tiltDeg = 0,
 translateZ = 0,
}: GuideCardProps) {
 const mapHostRef = useRef<HTMLDivElement | null>(null);
 const mapRef = useRef<L.Map | null>(null);

 // Re-render when the gazetteer cache gains a new entry (covers the
 // demo Chicago fixture + any first-render of an unknown city).
 useServerCoordsTick();
 const countyFeature = lookupCounty(data.state, data.county);
 const cityCoords = lookupCity(data.state, data.county, data.city_name);
 const stateFullName = data.state ? STATE_NAME_BY_ABBR[data.state] ?? data.state : "";

 useEffect(() => {
 const host = mapHostRef.current;
 if (!host || mapRef.current) return;

 // Snapshot-only map — no zoom controls, no panning, no scroll-zoom,
 // no double-click-zoom. The card is a frozen view of the county.
 const map = L.map(host, {
 zoomControl: false,
 attributionControl: false,
 dragging: false,
 scrollWheelZoom: false,
 doubleClickZoom: false,
 boxZoom: false,
 keyboard: false,
 touchZoom: false,
 zoomSnap: 0,
 // tap option is iOS-specific; cast through to bypass the d.ts gap
 ...({ tap: false } as L.MapOptions),
 });

 L.tileLayer(
 "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
 {
 subdomains: "abcd",
 maxZoom: 18,
 attribution: "&copy; OpenStreetMap &copy; CARTO",
 },
 ).addTo(map);

 mapRef.current = map;

 return () => {
 map.remove();
 mapRef.current = null;
 };
 }, []);

 // Apply county polygon + pin + bounds whenever the data changes.
 useEffect(() => {
 const map = mapRef.current;
 if (!map) return;

 // Clear prior overlay layers (polygons + markers), keep the tile layer.
 map.eachLayer((layer) => {
 if (layer instanceof L.TileLayer) return;
 map.removeLayer(layer);
 });

 let bounds: L.LatLngBounds | null = null;

 if (countyFeature) {
 const layer = L.geoJSON(countyFeature, {
 style: {
 color: "#d9a55a",
 weight: 1.4,
 opacity: 0.85,
 fillColor: "#d9a55a",
 fillOpacity: 0.05,
 dashArray: "6 4",
 },
 interactive: false,
 });
 layer.addTo(map);
 bounds = layer.getBounds();
 }

 if (cityCoords) {
 const pinIcon = L.divIcon({
 className: "guide-card-pin",
 html: `
 <span class="guide-card-pin-pulse"></span>
 <span class="guide-card-pin-pulse guide-card-pin-pulse--late"></span>
 <span class="guide-card-pin-dot"></span>
 `,
 iconSize: [14, 14],
 iconAnchor: [7, 7],
 });
 L.marker([cityCoords.lat, cityCoords.lng], {
 icon: pinIcon,
 interactive: false,
 keyboard: false,
 }).addTo(map);

 // If we had no polygon, fit to the pin with a small padding.
 if (!bounds) {
 bounds = L.latLngBounds(
 [cityCoords.lat - 0.5, cityCoords.lng - 0.5],
 [cityCoords.lat + 0.5, cityCoords.lng + 0.5],
 );
 }
 }

 if (bounds) {
 // Generous padding (was [16, 16], bumped per G-3 Opus critique that
 // flagged the edge-to-edge fit as killing the "county floating in
 // space" feeling of the Traveling reference) + a maxZoom cap so
 // small counties don't zoom in past the readable scale.
 map.fitBounds(bounds, {
 padding: [32, 32],
 animate: false,
 maxZoom: 9,
 });
 } else {
 // Fallback: continental US bounds so the card shows SOMETHING
 // instead of a blank dark square.
 map.fitBounds(
 [
 [24.5, -125],
 [49.5, -66],
 ],
 { padding: [8, 8], animate: false },
 );
 }
 }, [countyFeature, cityCoords]);

 const handleClick = () => {
 if (onSelect) onSelect(data);
 };

 const titleText = data.title || `${data.city_name} — live meeting`;

 // G-4 — receding cards (negative translateZ) get a heavier atmospheric
 // shadow so the eye reads the perspective. Center card (translateZ=0)
 // keeps just the base card border. V1-UI-2 C2 — divisor bumped 60→80 to
 // match the parent's new translateZ range, so the recede-shadow scale
 // stays normalized to [0, 1] instead of clipping early at 0.75.
 const recedeNorm = Math.min(1, Math.abs(translateZ) / 80);
 const cardStyle: React.CSSProperties = {
 transform: `translateZ(${translateZ}px) rotateY(${tiltDeg}deg)`,
 boxShadow:
 recedeNorm > 0.01
 ? `0 ${10 + recedeNorm * 16}px ${24 + recedeNorm * 28}px -10px rgba(0, 0, 0, ${0.3 + recedeNorm * 0.4})`
 : undefined,
 };

 return (
 <button
 type="button"
 className="guide-card"
 style={cardStyle}
 onClick={handleClick}
 aria-label={`${titleText} — ${data.city_name}, ${data.state ?? ""}`}
 >
 <div className="guide-card-map">
 <div ref={mapHostRef} className="guide-card-map-host" />
 {stateFullName && (
 <div className="guide-card-state-name" aria-hidden>
 {stateFullName}
 </div>
 )}
 <div className="guide-card-live-chip" aria-hidden>
 <span className="guide-card-live-dot" />
 Live
 </div>
 </div>
 <div className="guide-card-meta">
 <div className="guide-card-meta-title">{titleText}</div>
 <div className="guide-card-meta-place">
 {data.city_name}
 {data.county ? ` · ${stripCountySuffix(data.county)} County` : ""}
 </div>
 </div>
 </button>
 );
}

function stripCountySuffix(name: string): string {
 return name.replace(/\s+county\s*$/i, "").trim();
}

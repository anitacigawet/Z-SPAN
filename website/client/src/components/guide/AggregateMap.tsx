/**
 * AggregateMap — the US-wide view of every active broadcast.
 *
 * Third peer view alongside Map (cards) and Table. Single Leaflet of
 * the continental US rendered as GeoJSON country polygons (no raster
 * tile layer — per V1-design-1 image-1+3 references, the country
 * landmasses float against the dark celestial backdrop with no gray
 * ocean fill). Every active broadcast's county highlighted in the
 * same dashed-amber style as the per-card GuideCard polygons + a
 * pulse-dot at each active city. Click a dot or a county to open that
 * broadcast (cinematic or inline, matching the current playerMode
 * state in GuideRoot).
 *
 * Sparse-state default per G-8: when fewer than 3 broadcasts are live,
 * GuideRoot lands the user here automatically. The empty state
 * ("No broadcasts live right now") is overlaid by GuideRoot, not
 * this component.
 *
 * Phase G chunk G-7 (2026-06-02); CartoDB tile layer replaced with
 * GeoJSON country polygons V1-design-1 redo (2026-06-19).
 */
import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { lookupCounty, lookupCity } from "@/lib/guideGeo";
import { useServerCoordsTick } from "@/data/serverCoords";
import type { GuideCardData } from "./GuideCard";

// Continental US bounds — kept as the aggregate view's fallback frame even
// though the flagship is Arizona-only (other states are open
// seats for independent operators; the wide frame costs nothing and the
// map zooms to actual data). Alaska + Hawaii aren't represented; inset
// panels would be a follow-up chunk if ever needed.
const US_BOUNDS: L.LatLngBoundsLiteral = [
 [24.396308, -125.0],
 [49.384358, -66.93457],
];

interface AggregateMapProps {
 broadcasts: GuideCardData[];
 onSelect: (data: GuideCardData) => void;
}

export default function AggregateMap({
 broadcasts,
 onSelect,
}: AggregateMapProps) {
 const mapHostRef = useRef<HTMLDivElement | null>(null);
 const mapRef = useRef<L.Map | null>(null);
 // Subscribe to the gazetteer cache so pins re-render once lazy lookups
 // (e.g., the demo Chicago fixture) land. Each new entry bumps the tick.
 const coordsTick = useServerCoordsTick();
 // Keep a stable reference to the latest onSelect so the click handler
 // attached to markers always invokes the current callback. (Markers
 // are recreated when broadcasts change, but stale closures during the
 // overlap window can fire — this pattern avoids that.)
 const onSelectRef = useRef(onSelect);
 onSelectRef.current = onSelect;

 // Init Leaflet once.
 useEffect(() => {
 const host = mapHostRef.current;
 if (!host || mapRef.current) return;

 const map = L.map(host, {
 // zoomControl:false — we explicitly add a right-positioned zoom
 // below; default zoomControl would render a duplicate on the
 // left.
 zoomControl: false,
 attributionControl: false,
 zoomSnap: 0.25,
 minZoom: 3,
 maxZoom: 12,
 });
 // Pre-fit to US bounds so the map has a center/zoom before the
 // label-overlay effects fire (Leaflet's getZoom/getBounds throw
 // before setView/fitBounds runs once). The broadcast-overlay
 // effect below re-fits to actual broadcasts when streams arrive.
 map.fitBounds(US_BOUNDS, { padding: [60, 120], animate: false });

 // CartoDB dark_nolabels base layer — same beautiful dark cartographic
 // detail as the GuideCard (Escape Kingman aesthetic) but WITHOUT
 // the basemap's own city/state labels (we render our own labels at
 // appropriate zoom tiers). A heavy brightness filter (applied in
 // guide.css) crushes the gray ocean toward black while preserving
 // the subtle road / boundary detail the operator called beautiful.
 const tileLayer = L.tileLayer(
 "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
 {
 subdomains: "abcd",
 maxZoom: 18,
 attribution: "&copy; OpenStreetMap &copy; CARTO",
 },
 );
 // Tag the tile layer as _isCountryBase so the broadcast-overlay
 // cleanup below doesn't strip it on every broadcast list update.
 (tileLayer as L.Layer & { _isCountryBase?: boolean })._isCountryBase = true;
 tileLayer.addTo(map);

 L.control
 .zoom({ position: "topright" })
 .addTo(map);
 L.control
 .attribution({ position: "bottomright", prefix: false })
 .addAttribution("&copy; OpenStreetMap &copy; CARTO")
 .addTo(map);

 mapRef.current = map;

 return () => {
 map.remove();
 mapRef.current = null;
 };
 }, []);

 // Apply broadcast overlays whenever the list changes.
 useEffect(() => {
 const map = mapRef.current;
 if (!map) return;

 // Clear prior broadcast overlays, keep the country base layer +
 // country labels (both tagged _isCountryBase during init).
 map.eachLayer((layer) => {
 if ((layer as L.Layer & { _isCountryBase?: boolean })._isCountryBase)
 return;
 map.removeLayer(layer);
 });

 // Auto-fit viewport to where the broadcasts actually are. Per G-7
 // Opus critique: a national-frame initial view buried the AZ cluster
 // in dead center-frame; user had to hunt left to find the action.
 // Fitting to broadcast bounds keeps the cluster optical-center +
 // spreads fused pulses apart enough to be individually clickable.
 // Falls back to continental US when no broadcasts (sparse state).
 const broadcastBounds = L.latLngBounds([]);

 for (const b of broadcasts) {
 const countyFeat = lookupCounty(b.state, b.county);
 const cityCoords = lookupCity(b.state, b.county, b.city_name);

 if (countyFeat) {
 const layer = L.geoJSON(countyFeat, {
 style: {
 color: "#d9a55a",
 weight: 1.4,
 opacity: 0.85,
 fillColor: "#d9a55a",
 fillOpacity: 0.08,
 dashArray: "6 4",
 },
 onEachFeature: (_feat, leafLayer) => {
 leafLayer.on("click", () => onSelectRef.current(b));
 },
 });
 layer.addTo(map);
 broadcastBounds.extend(layer.getBounds());
 }

 if (cityCoords) {
 broadcastBounds.extend([cityCoords.lat, cityCoords.lng]);
 const pinIcon = L.divIcon({
 className: "guide-aggregate-pin",
 html: `
 <span class="guide-aggregate-pin-pulse"></span>
 <span class="guide-aggregate-pin-pulse guide-aggregate-pin-pulse--late"></span>
 <span class="guide-aggregate-pin-dot"></span>
 `,
 iconSize: [16, 16],
 iconAnchor: [8, 8],
 });
 const marker = L.marker([cityCoords.lat, cityCoords.lng], {
 icon: pinIcon,
 keyboard: false,
 });
 marker.bindTooltip(
 `
 <div class="guide-aggregate-tip-title">${escapeHtml(
 b.title || `${b.city_name} — live`,
 )}</div>
 <div class="guide-aggregate-tip-place">${escapeHtml(
 [b.city_name, b.county, b.state].filter(Boolean).join(" · "),
 )}</div>
 `,
 {
 direction: "top",
 offset: [0, -10],
 className: "guide-aggregate-tip",
 opacity: 1,
 },
 );
 marker.on("click", () => onSelectRef.current(b));
 marker.addTo(map);
 }
 }

 if (broadcastBounds.isValid()) {
 // Generous padding (96px) so the broadcast cluster doesn't crowd
 // the map edges + maxZoom: 6 cap so a single broadcast doesn't
 // zoom to county-level (the aggregate view's job is regional
 // context, not single-broadcast focus — the GuideCard already
 // does that).
 map.fitBounds(broadcastBounds, {
 padding: [96, 96],
 maxZoom: 6,
 animate: false,
 });
 } else {
 // Sparse-state fallback — no broadcasts to fit; show the country
 // with generous padding so the landmasses sit in the middle of
 // the container rather than touching its edges. The host's mask
 // already feathers the rectangle's edges; the padding here keeps
 // country shapes off the feather zone so they read crisp.
 map.fitBounds(US_BOUNDS, { padding: [60, 120], animate: false });
 }
 }, [broadcasts, coordsTick]);

 return (
 <div className="guide-aggregate-wrap">
 <div ref={mapHostRef} className="guide-aggregate-host" />
 </div>
 );
}

function escapeHtml(s: string): string {
 return s
 .replace(/&/g, "&amp;")
 .replace(/</g, "&lt;")
 .replace(/>/g, "&gt;")
 .replace(/"/g, "&quot;")
 .replace(/'/g, "&#39;");
}

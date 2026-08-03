import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { lookupCounty, lookupCity } from "@/lib/guideGeo";
import { useServerCoordsTick } from "@/data/serverCoords";
import type { GuideCardData } from "./GuideCard";
const US_BOUNDS: L.LatLngBoundsLiteral = [
    [24.396308, -125.0],
    [49.384358, -66.93457],
];
interface AggregateMapProps {
    broadcasts: GuideCardData[];
    onSelect: (data: GuideCardData) => void;
}
export default function AggregateMap({ broadcasts, onSelect, }: AggregateMapProps) {
    const mapHostRef = useRef<HTMLDivElement | null>(null);
    const mapRef = useRef<L.Map | null>(null);
    const coordsTick = useServerCoordsTick();
    const onSelectRef = useRef(onSelect);
    onSelectRef.current = onSelect;
    useEffect(() => {
        const host = mapHostRef.current;
        if (!host || mapRef.current)
            return;
        const map = L.map(host, {
            zoomControl: false,
            attributionControl: false,
            zoomSnap: 0.25,
            minZoom: 3,
            maxZoom: 12,
        });
        map.fitBounds(US_BOUNDS, { padding: [60, 120], animate: false });
        const tileLayer = L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
            subdomains: "abcd",
            maxZoom: 18,
            attribution: "&copy; OpenStreetMap &copy; CARTO",
        });
        (tileLayer as L.Layer & {
            _isCountryBase?: boolean;
        })._isCountryBase = true;
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
    useEffect(() => {
        const map = mapRef.current;
        if (!map)
            return;
        map.eachLayer((layer) => {
            if ((layer as L.Layer & {
                _isCountryBase?: boolean;
            })._isCountryBase)
                return;
            map.removeLayer(layer);
        });
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
                marker.bindTooltip(`
          <div class="guide-aggregate-tip-title">${escapeHtml(b.title || `${b.city_name} — live`)}</div>
          <div class="guide-aggregate-tip-place">${escapeHtml([b.city_name, b.county, b.state].filter(Boolean).join(" · "))}</div>
        `, {
                    direction: "top",
                    offset: [0, -10],
                    className: "guide-aggregate-tip",
                    opacity: 1,
                });
                marker.on("click", () => onSelectRef.current(b));
                marker.addTo(map);
            }
        }
        if (broadcastBounds.isValid()) {
            map.fitBounds(broadcastBounds, {
                padding: [96, 96],
                maxZoom: 6,
                animate: false,
            });
        }
        else {
            map.fitBounds(US_BOUNDS, { padding: [60, 120], animate: false });
        }
    }, [broadcasts, coordsTick]);
    return (<div className="guide-aggregate-wrap">
      <div ref={mapHostRef} className="guide-aggregate-host"/>
    </div>);
}
function escapeHtml(s: string): string {
    return s
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

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
    tiltDeg?: number;
    translateZ?: number;
}
export default function GuideCard({ data, onSelect, tiltDeg = 0, translateZ = 0, }: GuideCardProps) {
    const mapHostRef = useRef<HTMLDivElement | null>(null);
    const mapRef = useRef<L.Map | null>(null);
    useServerCoordsTick();
    const countyFeature = lookupCounty(data.state, data.county);
    const cityCoords = lookupCity(data.state, data.county, data.city_name);
    const stateFullName = data.state ? STATE_NAME_BY_ABBR[data.state] ?? data.state : "";
    useEffect(() => {
        const host = mapHostRef.current;
        if (!host || mapRef.current)
            return;
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
            ...({ tap: false } as L.MapOptions),
        });
        L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
            subdomains: "abcd",
            maxZoom: 18,
            attribution: "&copy; OpenStreetMap &copy; CARTO",
        }).addTo(map);
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
            if (layer instanceof L.TileLayer)
                return;
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
            if (!bounds) {
                bounds = L.latLngBounds([cityCoords.lat - 0.5, cityCoords.lng - 0.5], [cityCoords.lat + 0.5, cityCoords.lng + 0.5]);
            }
        }
        if (bounds) {
            map.fitBounds(bounds, {
                padding: [32, 32],
                animate: false,
                maxZoom: 9,
            });
        }
        else {
            map.fitBounds([
                [24.5, -125],
                [49.5, -66],
            ], { padding: [8, 8], animate: false });
        }
    }, [countyFeature, cityCoords]);
    const handleClick = () => {
        if (onSelect)
            onSelect(data);
    };
    const titleText = data.title || `${data.city_name} — live meeting`;
    const recedeNorm = Math.min(1, Math.abs(translateZ) / 80);
    const cardStyle: React.CSSProperties = {
        transform: `translateZ(${translateZ}px) rotateY(${tiltDeg}deg)`,
        boxShadow: recedeNorm > 0.01
            ? `0 ${10 + recedeNorm * 16}px ${24 + recedeNorm * 28}px -10px rgba(0, 0, 0, ${0.3 + recedeNorm * 0.4})`
            : undefined,
    };
    return (<button type="button" className="guide-card" style={cardStyle} onClick={handleClick} aria-label={`${titleText} — ${data.city_name}, ${data.state ?? ""}`}>
      <div className="guide-card-map">
        <div ref={mapHostRef} className="guide-card-map-host"/>
        {stateFullName && (<div className="guide-card-state-name" aria-hidden>
            {stateFullName}
          </div>)}
        <div className="guide-card-live-chip" aria-hidden>
          <span className="guide-card-live-dot"/>
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
    </button>);
}
function stripCountySuffix(name: string): string {
    return name.replace(/\s+county\s*$/i, "").trim();
}

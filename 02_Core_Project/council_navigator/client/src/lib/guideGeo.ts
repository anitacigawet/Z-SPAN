/**
 * guideGeo — county polygon + city coordinate lookup for the Guide
 * cinematic rebuild.
 *
 * County polygons come from the bundled us-atlas TopoJSON
 * (counties-10m.json, ~822 KB — pre-built from US Census Cartographic
 * Boundary Files by Mike Bostock). Lat/lng coordinates so they project
 * cleanly into Leaflet (Web Mercator). All 3,143 counties available
 * immediately — no per-city onboarding work as Z-SPAN expands.
 *
 * City coordinates come from `client/src/data/zspanCities.ts` (hand-
 * curated for Z-SPAN-tracked cities, with county-centroid fallback for
 * cities not in the gazetteer yet).
 *
 * The county TopoJSON is statically imported so Vite bundles it with
 * the Guide page chunk. If bundle weight becomes a concern, switch to
 * dynamic import (`await import('us-atlas/counties-10m.json')`) at the
 * G-3 GuideCard mount site.
 *
 * Phase G chunk G-2 (2026-06-02).
 */
import { feature } from "topojson-client";
import type { Feature, FeatureCollection, Geometry, Polygon, MultiPolygon } from "geojson";
import type { Topology, GeometryCollection } from "topojson-specification";
import countiesTopo from "us-atlas/counties-10m.json";
import { lookupServerCoord, type CityCoords } from "@/data/serverCoords";

// US state FIPS → USPS abbreviation. First 2 chars of a county FIPS
// id give the state, which lets us narrow county-name matches (there
// are 31 "Washington" counties across the US, for example — we MUST
// disambiguate by state).
const STATE_FIPS_BY_ABBR: Record<string, string> = {
  AL: "01", AK: "02", AZ: "04", AR: "05", CA: "06", CO: "08", CT: "09",
  DE: "10", DC: "11", FL: "12", GA: "13", HI: "15", ID: "16", IL: "17",
  IN: "18", IA: "19", KS: "20", KY: "21", LA: "22", ME: "23", MD: "24",
  MA: "25", MI: "26", MN: "27", MS: "28", MO: "29", MT: "30", NE: "31",
  NV: "32", NH: "33", NJ: "34", NM: "35", NY: "36", NC: "37", ND: "38",
  OH: "39", OK: "40", OR: "41", PA: "42", RI: "44", SC: "45", SD: "46",
  TN: "47", TX: "48", UT: "49", VT: "50", VA: "51", WA: "53", WV: "54",
  WI: "55", WY: "56",
};

// Materialize the county FeatureCollection once at module load. The
// `feature()` call from topojson-client expands all arcs into GeoJSON
// coordinates — fine for the ~3,143-county scale (under 30 MB in
// memory once expanded, but only the queried features are visited
// downstream so the working set stays small).
const countyCollection = feature(
  countiesTopo as unknown as Topology<{ counties: GeometryCollection }>,
  (countiesTopo as unknown as Topology<{ counties: GeometryCollection }>).objects.counties,
) as FeatureCollection<Polygon | MultiPolygon, { name: string }>;

// Indexed lookup table built once: "stateAbbr|countyName" → Feature.
// Normalized: trailing " County" stripped from incoming names, both sides
// lowercased + trimmed. Built lazily on first call so module-load cost
// stays minimal (~10ms for the index build at ~3,143 entries).
let countyIndex: Map<string, Feature<Polygon | MultiPolygon, { name: string }>> | null = null;

function buildCountyIndex(): Map<string, Feature<Polygon | MultiPolygon, { name: string }>> {
  const m = new Map<string, Feature<Polygon | MultiPolygon, { name: string }>>();
  const abbrByFips: Record<string, string> = {};
  for (const [abbr, fips] of Object.entries(STATE_FIPS_BY_ABBR)) abbrByFips[fips] = abbr;

  for (const feat of countyCollection.features) {
    const id = String(feat.id ?? "");
    if (id.length !== 5) continue;
    const stateFips = id.slice(0, 2);
    const stateAbbr = abbrByFips[stateFips];
    if (!stateAbbr) continue;
    const countyName = feat.properties?.name;
    if (!countyName) continue;
    const key = `${stateAbbr}|${normalizeCounty(countyName)}`.toLowerCase();
    m.set(key, feat);
  }
  return m;
}

function normalizeCounty(name: string): string {
  return name.replace(/\s+county\s*$/i, "").trim();
}

/**
 * Look up a county's GeoJSON polygon by state abbreviation + county name.
 * Names are normalized (trailing " County" stripped, case-insensitive).
 * Returns null if no match (unknown state, misspelled county, etc.) —
 * the caller should render a fallback (e.g., no polygon, just a pin).
 */
export function lookupCounty(
  stateAbbr: string | null,
  countyName: string | null,
): Feature<Polygon | MultiPolygon, { name: string }> | null {
  if (!stateAbbr || !countyName) return null;
  if (!countyIndex) countyIndex = buildCountyIndex();
  const key = `${stateAbbr}|${normalizeCounty(countyName)}`.toLowerCase();
  return countyIndex.get(key) ?? null;
}

/**
 * Compute the geographic centroid of a polygon or multipolygon. Used as
 * a fallback for cities not in the hand-curated gazetteer — better than
 * dropping the pin off-screen, accurate enough for "this is roughly
 * where in the county the meeting is" purposes.
 */
export function polygonCentroid(geom: Polygon | MultiPolygon): CityCoords {
  // For a MultiPolygon, use the centroid of the largest polygon by
  // perimeter (avoids islands skewing the centroid). For a Polygon,
  // use the outer ring's mean lat/lng. Naïve but proportionate for
  // the level of precision needed (mid-county pin placement).
  const rings: number[][][] =
    geom.type === "Polygon"
      ? [geom.coordinates[0]]
      : geom.coordinates.map((poly) => poly[0]);

  // Pick the ring with the most points as a perimeter proxy.
  let chosen = rings[0];
  for (const r of rings) if (r.length > chosen.length) chosen = r;

  let lngSum = 0;
  let latSum = 0;
  for (const [lng, lat] of chosen) {
    lngSum += lng;
    latSum += lat;
  }
  return {
    lat: latSum / chosen.length,
    lng: lngSum / chosen.length,
  };
}

/**
 * Look up a city's coordinates via the universal gazetteer substrate
 * (per S-067 2026-06-19). Two-layer chain:
 *   1. `serverCoords` cache — populated bulk-from `/api/channels/tree`
 *      for every parser-registered city, or lazy-from
 *      `/api/gazetteer/lookup` for ad-hoc cities (e.g., the demo
 *      Chicago fixture). Both paths terminate in the same Census
 *      Places Gazetteer on the server. Cache misses trigger a lazy
 *      fetch; subscribers via `useServerCoordsTick()` re-render when
 *      the fetch lands.
 *   2. County polygon centroid — last-resort while the lazy fetch is
 *      in flight, or when the gazetteer has no entry for the city.
 *
 * Returns null only when even the county lookup fails (unknown state,
 * misspelled county) — at which point we have no map to render anyway.
 */
export function lookupCity(
  stateAbbr: string | null,
  countyName: string | null,
  cityName: string | null,
): CityCoords | null {
  const server = lookupServerCoord(stateAbbr, countyName, cityName);
  if (server) return server;
  const county = lookupCounty(stateAbbr, countyName);
  if (!county || !county.geometry) return null;
  return polygonCentroid(county.geometry);
}

/**
 * Reverse the state FIPS → abbr lookup so callers can ask "what counties
 * are in this state?" — handy for the G-7 aggregate view that may want
 * to render all counties in a state band.
 */
export function isKnownState(stateAbbr: string): boolean {
  return stateAbbr in STATE_FIPS_BY_ABBR;
}

// Re-export type-only so consumers don't need to reach into the data dir.
export type { CityCoords };

// Re-export the full collection for callers that need the raw set
// (e.g., G-7 aggregate view rendering all active-county polygons in
// one Leaflet pass). Most callers should use lookupCounty() instead.
export { countyCollection as ALL_COUNTIES };

// Smoke test — at module load (dev only), verify Mohave + Kingman.
// Stripped in production builds; cheap to keep in dev as a tripwire.
if (import.meta.env.DEV) {
  const mohave = lookupCounty("AZ", "Mohave");
  const kingman = lookupCity("AZ", "Mohave County", "Kingman");
  if (!mohave) {
    // eslint-disable-next-line no-console
    console.warn("[guideGeo] Mohave County lookup returned null — bundled TopoJSON may be broken");
  }
  if (!kingman || Math.abs(kingman.lat - 35.1894) > 0.01) {
    // eslint-disable-next-line no-console
    console.warn("[guideGeo] Kingman lookup returned unexpected coords:", kingman);
  }
}

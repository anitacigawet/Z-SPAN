/**
 * Server-resolved city coordinates — the universal coordinate substrate
 * for every Z-SPAN map pin. Two paths populate the in-memory cache:
 *
 * 1. Bulk: `/api/channels/tree` returns per-city lat/lng for every
 * city in `parser_index.json`. Auto-fired once at module import;
 * typically populated before the Guide page mounts.
 * 2. Lazy: when `lookupServerCoord()` is called for a city not in
 * the cache (e.g., the demo Chicago fixture, an ad-hoc preview),
 * it kicks off a one-off `/api/gazetteer/lookup?city=X&state=Y`
 * fetch in the background. When that resolves, the cache updates
 * and any subscriber via `useServerCoordsTick()` re-renders.
 *
 * Both paths terminate in the same Census Places Gazetteer on the
 * server (`parsers/gazetteer.py`). Contributors never touch coordinates.
 *
 * Per — universal coordinate path.
 */

import { useEffect, useState } from "react";
import { fetchForPlane } from "../lib/planeFetch";
import { isPublicPlane } from "../lib/trustPlane";

export interface CityCoords {
 lat: number;
 lng: number;
}

type StateName = string;
type CountyName = string;
type CityName = string;
type Key = `${StateName}|${CountyName}|${CityName}`;

const COORDS: Map<Key, CityCoords> = new Map();
let _kickoffPromise: Promise<void> | null = null;

// Subscribers re-render whenever the cache gains a new entry.
const _listeners: Set<() => void> = new Set();
let _tick = 0;

function _commit(key: Key, coords: CityCoords): void {
 if (COORDS.has(key)) return;
 COORDS.set(key, coords);
 _tick++;
 _listeners.forEach((listener) => listener());
}

// In-flight lazy lookups so we don't double-fetch the same city before
// the first response lands.
const _inflight: Map<Key, Promise<void>> = new Map();

// Negative-result cache: cities the gazetteer endpoint returned 404 for
// (genuinely absent from the Census Places gazetteer). Without this, a
// cache-miss city that 404s leaves no record — COORDS never gets the key
// and _inflight clears on completion — so it re-fires the /api/gazetteer
// fetch on every subsequent re-render. AZ V1 doesn't trigger this (all 88
// parser_index cities are backfilled + the demo cities resolve), but it's
// a latent repeated-404 leak for any queried-but-absent city.
// brainstorm-audit finding F1, 2026-06-19.)
const _knownAbsent: Set<Key> = new Set();

const STATE_ABBR_BY_NAME: Record<string, string> = {
 Arizona: "AZ",
 Alabama: "AL",
 Alaska: "AK",
 Arkansas: "AR",
 California: "CA",
 Colorado: "CO",
 Connecticut: "CT",
 Delaware: "DE",
 "District of Columbia": "DC",
 Florida: "FL",
 Georgia: "GA",
 Hawaii: "HI",
 Idaho: "ID",
 Illinois: "IL",
 Indiana: "IN",
 Iowa: "IA",
 Kansas: "KS",
 Kentucky: "KY",
 Louisiana: "LA",
 Maine: "ME",
 Maryland: "MD",
 Massachusetts: "MA",
 Michigan: "MI",
 Minnesota: "MN",
 Mississippi: "MS",
 Missouri: "MO",
 Montana: "MT",
 Nebraska: "NE",
 Nevada: "NV",
 "New Hampshire": "NH",
 "New Jersey": "NJ",
 "New Mexico": "NM",
 "New York": "NY",
 "North Carolina": "NC",
 "North Dakota": "ND",
 Ohio: "OH",
 Oklahoma: "OK",
 Oregon: "OR",
 Pennsylvania: "PA",
 "Rhode Island": "RI",
 "South Carolina": "SC",
 "South Dakota": "SD",
 Tennessee: "TN",
 Texas: "TX",
 Utah: "UT",
 Vermont: "VT",
 Virginia: "VA",
 Washington: "WA",
 "West Virginia": "WV",
 Wisconsin: "WI",
 Wyoming: "WY",
};

function normalizeKey(
 stateAbbr: string | null,
 countyName: string | null,
 cityName: string | null,
): Key | null {
 if (!stateAbbr || !countyName || !cityName) return null;
 const county = countyName.replace(/\s+county\s*$/i, "").trim();
 return `${stateAbbr.toUpperCase()}|${county.toLowerCase()}|${cityName.toLowerCase()}` as Key;
}

interface ChannelsTreeCity {
 name: string;
 lat: number | null;
 lng: number | null;
}
interface ChannelsTreeCounty {
 county: string;
 cities: ChannelsTreeCity[];
}
interface ChannelsTreeState {
 state: string;
 counties: ChannelsTreeCounty[];
}
interface ChannelsTreeResponse {
 ok?: boolean;
 states?: ChannelsTreeState[];
}

/**
 * Kick off the server-coordinates fetch. Idempotent — only the first
 * call performs the network request; subsequent calls return the same
 * Promise. Safe to call from any component's effect.
 */
export function kickoffServerCoords(): Promise<void> {
 if (_kickoffPromise) return _kickoffPromise;
 _kickoffPromise = (async () => {
 try {
 const r = await fetchForPlane({
 publicPath: "/public-api/channels/tree",
 operatorPath: "/api/channels/tree",
 });
 if (!r.ok) return;
 const data = (await r.json()) as ChannelsTreeResponse;
 const states = data.states ?? [];
 for (const stateNode of states) {
 const abbr = STATE_ABBR_BY_NAME[stateNode.state];
 if (!abbr) continue;
 for (const countyNode of stateNode.counties) {
 for (const city of countyNode.cities) {
 if (
 typeof city.lat !== "number" ||
 typeof city.lng !== "number"
 )
 continue;
 const key = normalizeKey(abbr, countyNode.county, city.name);
 if (!key) continue;
 _commit(key, { lat: city.lat, lng: city.lng });
 }
 }
 }
 } catch {
 // Network failure on the coords fetch is non-fatal — lookupCity
 // falls back to polygonCentroid, which is the prior behavior.
 }
 })();
 return _kickoffPromise;
}

/** Synchronous lookup. Returns null on cache miss; if the city is unknown
 * to the cache AND we have city + state, fires an async fetch against
 * `/api/gazetteer/lookup` in the background so the next render hits.
 * Subscribers via `useServerCoordsTick()` will re-render when the lazy
 * fetch lands.
 */
export function lookupServerCoord(
 stateAbbr: string | null,
 countyName: string | null,
 cityName: string | null,
): CityCoords | null {
 const key = normalizeKey(stateAbbr, countyName, cityName);
 if (!key) return null;
 const hit = COORDS.get(key);
 if (hit) return hit;
 // Known-absent (a prior lazy fetch 404'd) — don't re-fire. Caller
 // falls back to polygon centroid permanently for this city.
 if (_knownAbsent.has(key)) return null;
 // Cache miss — fire a lazy gazetteer fetch (dedup'd) so the next
 // render lands a precise pin. Don't block the current call.
 if (!isPublicPlane() && stateAbbr && cityName && !_inflight.has(key)) {
 const promise = (async () => {
 try {
 const params = new URLSearchParams({
 city: cityName,
 state: stateAbbr.toUpperCase(),
 });
 const r = await fetch(`/api/gazetteer/lookup?${params.toString()}`);
 if (!r.ok) {
 // 404 = genuinely absent from the gazetteer. Record it so we
 // don't re-fetch on every subsequent re-render.
 if (r.status === 404) _knownAbsent.add(key);
 return;
 }
 const data = (await r.json()) as { lat?: number; lng?: number };
 if (typeof data.lat === "number" && typeof data.lng === "number") {
 _commit(key, { lat: data.lat, lng: data.lng });
 } else {
 // 200 but no usable coords — treat as absent to avoid re-fire.
 _knownAbsent.add(key);
 }
 } catch {
 // Network error (not a definitive 404) — leave key un-cached so
 // a later render can retry; transient failures shouldn't poison
 // the city permanently.
 } finally {
 _inflight.delete(key);
 }
 })();
 _inflight.set(key, promise);
 }
 return null;
}

/** React hook — subscribe to cache updates. Components that render pins
 * via `lookupCity()` should include the returned tick in their effect
 * deps so they re-render when the lazy gazetteer fetch lands. */
export function useServerCoordsTick(): number {
 const [, setLocal] = useState(_tick);
 useEffect(() => {
 const listener = () => setLocal(_tick);
 _listeners.add(listener);
 // Sync up if the cache advanced between render and effect.
 if (_tick !== undefined) setLocal(_tick);
 return () => {
 _listeners.delete(listener);
 };
 }, []);
 return _tick;
}

/** Diagnostic — how many cities are cached. */
export function serverCoordsSize(): number {
 return COORDS.size;
}

// Auto-fire on module import so the cache starts populating as early
// as possible. Components consuming `lookupCity` don't need to call
// this themselves.
kickoffServerCoords();

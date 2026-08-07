/**
 * Open-Meteo weather integration for the City Dashboard (2026-07-03).
 *
 * Free, no API key required, browser-CORS-friendly.
 * Docs: https://open-meteo.com/en/docs
 *
 * Two calls:
 * 1. Geocode city name → lat/lon + display name
 * 2. Forecast: current conditions + 7-day daily strip
 *
 * Both results cache in localStorage per city for the session (12h TTL)
 * so we don't hammer the API on every dashboard mount.
 */

export interface CityLocation {
 latitude: number;
 longitude: number;
 displayName: string; // e.g. "Kingman, Arizona, United States"
 admin1?: string; // state
 country?: string;
 timezone?: string;
}

export interface WeatherDay {
 date: string; // YYYY-MM-DD
 weekday: string; // e.g. "MON"
 weathercode: number;
 tempMaxF: number;
 tempMinF: number;
 precipProbability: number; // 0..100
}

export interface WeatherReading {
 location: CityLocation;
 current: {
 tempF: number;
 windMph: number;
 weathercode: number;
 isDaytime: boolean;
 updatedAt: string;
 };
 forecast: WeatherDay[];
 cachedAt: string;
}

const CACHE_TTL_MS = 12 * 60 * 60 * 1000; // 12h

interface CachedWeather {
 reading: WeatherReading;
 cachedAt: number;
}

function cacheKey(city: string): string {
 return `zspan.weather.v1.${city.toLowerCase()}`;
}

function readCache(city: string): WeatherReading | null {
 try {
 const raw = window.localStorage.getItem(cacheKey(city));
 if (!raw) return null;
 const { reading, cachedAt } = JSON.parse(raw) as CachedWeather;
 if (Date.now() - cachedAt > CACHE_TTL_MS) return null;
 return reading;
 } catch {
 return null;
 }
}

function writeCache(city: string, reading: WeatherReading): void {
 try {
 window.localStorage.setItem(
 cacheKey(city),
 JSON.stringify({ reading, cachedAt: Date.now() } satisfies CachedWeather),
 );
 } catch {
 /* private-mode / quota — ignore */
 }
}

/** Geocode a US city name (defaults to state disambiguation later). */
async function geocodeCity(city: string): Promise<CityLocation | null> {
 const url = new URL("https://geocoding-api.open-meteo.com/v1/search");
 url.searchParams.set("name", city);
 url.searchParams.set("count", "1");
 url.searchParams.set("country", "US");
 url.searchParams.set("language", "en");
 url.searchParams.set("format", "json");
 const res = await fetch(url.toString());
 if (!res.ok) return null;
 const data = (await res.json()) as {
 results?: Array<{
 latitude: number;
 longitude: number;
 name: string;
 admin1?: string;
 country?: string;
 timezone?: string;
 }>;
 };
 const r = data.results?.[0];
 if (!r) return null;
 return {
 latitude: r.latitude,
 longitude: r.longitude,
 displayName: [r.name, r.admin1].filter(Boolean).join(", "),
 admin1: r.admin1,
 country: r.country,
 timezone: r.timezone,
 };
}

const WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];

/** Fetch current + 7-day forecast for a given lat/lon. */
async function fetchForecast(loc: CityLocation): Promise<WeatherReading | null> {
 const url = new URL("https://api.open-meteo.com/v1/forecast");
 url.searchParams.set("latitude", loc.latitude.toString());
 url.searchParams.set("longitude", loc.longitude.toString());
 url.searchParams.set(
 "daily",
 "temperature_2m_max,temperature_2m_min,weathercode,precipitation_probability_max",
 );
 url.searchParams.set(
 "current",
 "temperature_2m,wind_speed_10m,weather_code,is_day",
 );
 url.searchParams.set("temperature_unit", "fahrenheit");
 url.searchParams.set("wind_speed_unit", "mph");
 url.searchParams.set("timezone", loc.timezone ?? "auto");
 url.searchParams.set("forecast_days", "7");

 const res = await fetch(url.toString());
 if (!res.ok) return null;
 const data = (await res.json()) as {
 current: {
 temperature_2m: number;
 wind_speed_10m: number;
 weather_code: number;
 is_day: 0 | 1;
 time: string;
 };
 daily: {
 time: string[];
 temperature_2m_max: number[];
 temperature_2m_min: number[];
 weathercode: number[];
 precipitation_probability_max: number[];
 };
 };

 const forecast: WeatherDay[] = data.daily.time.map((iso, i) => {
 const d = new Date(iso + "T12:00:00");
 return {
 date: iso,
 weekday: WEEKDAYS[d.getDay()],
 weathercode: data.daily.weathercode[i],
 tempMaxF: Math.round(data.daily.temperature_2m_max[i]),
 tempMinF: Math.round(data.daily.temperature_2m_min[i]),
 precipProbability: data.daily.precipitation_probability_max[i] ?? 0,
 };
 });

 return {
 location: loc,
 current: {
 tempF: Math.round(data.current.temperature_2m),
 windMph: Math.round(data.current.wind_speed_10m),
 weathercode: data.current.weather_code,
 isDaytime: data.current.is_day === 1,
 updatedAt: data.current.time,
 },
 forecast,
 cachedAt: new Date().toISOString(),
 };
}

/** Public: fetch a city's live weather, with session cache. */
export async function fetchCityWeather(
 city: string,
): Promise<WeatherReading | null> {
 const cached = readCache(city);
 if (cached) return cached;
 const loc = await geocodeCity(city);
 if (!loc) return null;
 const reading = await fetchForecast(loc);
 if (!reading) return null;
 writeCache(city, reading);
 return reading;
}

/** Emoji + label from Open-Meteo weather codes.
 * Reference: https://open-meteo.com/en/docs (WMO weather codes) */
export function weatherIcon(code: number, isDay = true): {
 emoji: string;
 label: string;
} {
 if (code === 0) return { emoji: isDay ? "☀️" : "🌙", label: "Clear" };
 if (code <= 2) return { emoji: isDay ? "🌤️" : "🌙", label: "Partly cloudy" };
 if (code === 3) return { emoji: "☁️", label: "Overcast" };
 if (code >= 45 && code <= 48) return { emoji: "🌫️", label: "Fog" };
 if (code >= 51 && code <= 57) return { emoji: "🌦️", label: "Drizzle" };
 if (code >= 61 && code <= 67) return { emoji: "🌧️", label: "Rain" };
 if (code >= 71 && code <= 77) return { emoji: "🌨️", label: "Snow" };
 if (code >= 80 && code <= 82) return { emoji: "🌧️", label: "Showers" };
 if (code >= 85 && code <= 86) return { emoji: "🌨️", label: "Snow showers" };
 if (code >= 95 && code <= 99) return { emoji: "⛈️", label: "Thunder" };
 return { emoji: "🌡️", label: "Unknown" };
}

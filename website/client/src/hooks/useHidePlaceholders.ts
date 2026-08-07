import { useSyncExternalStore } from "react";

export const HIDE_PLACEHOLDERS_STORAGE_KEY = "zspan.hide_placeholders";

const DEFAULT_HIDE_PLACEHOLDERS = true;
const subscribers = new Set<() => void>();
let fallbackValue = DEFAULT_HIDE_PLACEHOLDERS;

type CatalogEpisode = {
 availability?: string;
};

// The catalog-row signature is the PRESENCE of `availability` (a /v1-only
// field) with a non-published value — NOT public_id, which also rides internal
// published rows. Card rendering, routing, and visibility filtering must all
// share this one predicate so those paths cannot drift apart.
export function isCatalogPlaceholder(episode: CatalogEpisode): boolean {
 return (
 episode.availability !== undefined && episode.availability !== "published"
 );
}

export function filterVisibleEpisodes<T extends CatalogEpisode>(
 episodes: T[],
 hidePlaceholders: boolean
): T[] {
 return hidePlaceholders
 ? episodes.filter(episode => !isCatalogPlaceholder(episode))
 : episodes;
}

function valueFromStorage(raw: string | null): boolean {
 return raw !== "0";
}

function readHidePlaceholders(): boolean {
 if (typeof window === "undefined") return DEFAULT_HIDE_PLACEHOLDERS;

 try {
 fallbackValue = valueFromStorage(
 window.localStorage.getItem(HIDE_PLACEHOLDERS_STORAGE_KEY)
 );
 } catch {
 // localStorage may be unavailable in private browsing.
 }
 return fallbackValue;
}

function subscribe(callback: () => void): () => void {
 subscribers.add(callback);

 const onStorage = (event: StorageEvent) => {
 if (event.key !== HIDE_PLACEHOLDERS_STORAGE_KEY && event.key !== null) {
 return;
 }
 fallbackValue =
 event.key === null
 ? DEFAULT_HIDE_PLACEHOLDERS
 : valueFromStorage(event.newValue);
 callback();
 };

 if (typeof window !== "undefined") {
 window.addEventListener("storage", onStorage);
 }

 return () => {
 subscribers.delete(callback);
 if (typeof window !== "undefined") {
 window.removeEventListener("storage", onStorage);
 }
 };
}

export function setHidePlaceholders(next: boolean): void {
 fallbackValue = next;
 try {
 window.localStorage.setItem(
 HIDE_PLACEHOLDERS_STORAGE_KEY,
 next ? "1" : "0"
 );
 } catch {
 // Keep the preference in memory when localStorage is unavailable.
 }
 subscribers.forEach(callback => callback());
}

export function useHidePlaceholders() {
 const hidePlaceholders = useSyncExternalStore(
 subscribe,
 readHidePlaceholders,
 () => DEFAULT_HIDE_PLACEHOLDERS
 );

 return [hidePlaceholders, setHidePlaceholders] as const;
}

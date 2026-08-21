/**
 * channelPoster — resolve a city name to its 16:9 channel-poster PNG.
 *
 * Assets live in `client/public/channels/<city-slug>-poster.png` with a
 * generic Arizona fallback at `_az-default-poster.png`. Per-city posters
 * are dusk-cinematic landscape illustrations (see
 * 01_Project_Overview/IMAGE_PROMPTS.md §4–§8 for the generation briefs).
 *
 * Returns the public URL; callers should use an <img onError> fallback
 * in case the file is missing on disk so dev/staging never breaks.
 */

const PER_CITY: ReadonlyArray<[RegExp, string]> = [
  [/^\s*kingman\s*$/i,            "/channels/kingman-poster.png"],
  [/^\s*bullhead\s+city\s*$/i,    "/channels/bullhead-city-poster.png"],
  [/^\s*lake\s+havasu(?:\s+city)?\s*$/i, "/channels/lake-havasu-poster.png"],
];

const ARIZONA_FALLBACK = "/channels/_az-default-poster.png";

export function channelPosterForCity(
  cityName: string | null | undefined
): string {
  if (!cityName) return ARIZONA_FALLBACK;
  for (const [pattern, path] of PER_CITY) {
    if (pattern.test(cityName)) return path;
  }
  return ARIZONA_FALLBACK;
}

/**
 * episodeCardForTitle — resolve a meeting title to the placeholder thumbnail
 * PNG path.
 *
 * Tier 1: meeting-type matches (e.g., "City Council", "Golf Commission")
 *         → /episodes/<slug>.png (typographic per-committee cards).
 * Tier 2: anything that doesn't match a known committee falls through to
 *         one of four cinematic council-chamber illustrations under
 *         /states/episode-fallback-{1..4}.png. The variant is picked
 *         deterministically from a hash of the title so adjacent rows
 *         don't look identical AND the same meeting always renders the
 *         same artwork across page loads.
 *
 * Adding a new committee placeholder:
 *   1. Generate the PNG (see 01_Project_Overview/IMAGE_PROMPTS.md)
 *   2. Drop into client/public/episodes/<slug>.png
 *   3. Add a row to _EPISODE_CARD_RULES below — most-specific patterns
 *      first since first-match-wins.
 *
 * The pattern set should track the meeting-type vocabulary in
 * parsers/meeting_vocabulary.yaml — a meeting that classifies as
 * `committee_golf` should resolve to `/episodes/golf-commission.png`,
 * `planning` -> `/episodes/planning-zoning.png`, etc.
 */

const _EPISODE_CARD_RULES: Array<[RegExp, string]> = [
  [/planning\s*&?\s*zoning/i,                       "/episodes/planning-zoning.png"],
  [/airport.*industrial/i,                          "/episodes/airport-industrial-park.png"],
  [/transit/i,                                      "/episodes/transit-advisory.png"],
  [/golf/i,                                         "/episodes/golf-commission.png"],
  [/municipal\s+utility|utility\s+commission/i,     "/episodes/municipal-utility.png"],
  [/economic\s+development/i,                       "/episodes/economic-development.png"],
  [/city\s+council/i,                               "/episodes/city-council.png"],
];

// Per James 2026-05-13: the photographic council-chamber fallbacks
// (`/states/episode-fallback-{1..4}.png`) are retired in favor of the
// typographic `_default.png`. James prefers the typographic look across
// every episode card — the photographic variants didn't sit visually
// alongside the typographic committee cards. The 4 fallback PNGs stay
// on disk (still used elsewhere as empty-state illustrations); they
// just no longer route as episode-card fallbacks.
//
// When new typographic committee cards land in `/episodes/<slug>.png`,
// extend `_EPISODE_CARD_RULES` above and they'll route automatically
// before reaching this default. Currently-falling-through committees:
// Clean City Commission, Heritage Preservation, Parks/Aquatics/Recreation,
// City Council Work Session, Tri-City Council, etc.
const _DEFAULT_EPISODE_CARD = "/episodes/_default.png";

export function episodeCardForTitle(title: string | null | undefined): string {
  if (!title) return _DEFAULT_EPISODE_CARD;
  for (const [pattern, path] of _EPISODE_CARD_RULES) {
    if (pattern.test(title)) return path;
  }
  return _DEFAULT_EPISODE_CARD;
}

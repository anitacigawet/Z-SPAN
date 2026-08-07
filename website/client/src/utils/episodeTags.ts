/**
 * Shared episode-tag parsing + color mapping.
 *
 * The `episode_tags` NotebookLM output is a plaintext block in the format:
 *
 * TAG: Walapai Foothills annexation | CATEGORY: zoning
 * TAG: Flying Fortress Parkway | CATEGORY: infrastructure
 * ...
 *
 * `parseEpisodeTags()` extracts the rows into `{text, category}` objects.
 * `TAG_COLOR` maps each category to a hex base; the consumer renders pills
 * with that hex at low alpha for background/border and full saturation for
 * the text — gives a muted broadcast-graphic feel rather than candy colors.
 *
 * Used by both BroadcastPage (large pill row on the detail view) and the
 * ChannelsPage EpisodeCard (compact 1–2 pills overlay on the calendar
 * thumbnail).
 */

export type TagCategory =
 | "zoning"
 | "infrastructure"
 | "utilities"
 | "budget"
 | "public_safety"
 | "environment"
 | "community"
 | "legislation"
 | "personnel"
 | "transit"
 | "hearing"
 | "miscellaneous";

export interface EpisodeTag {
 text: string;
 category: TagCategory;
}

export const TAG_COLOR: Record<TagCategory, string> = {
 zoning: "#60A5FA", // blue
 infrastructure: "#F59E0B", // amber
 utilities: "#06B6D4", // cyan
 budget: "#22C55E", // green
 public_safety: "#EF4444", // red
 environment: "#10B981", // emerald
 community: "#A78BFA", // violet
 legislation: "#FCD34D", // yellow
 personnel: "#94A3B8", // slate
 transit: "#FB923C", // orange
 hearing: "#EC4899", // pink
 miscellaneous: "#71717A", // gray
};

const VALID_CATEGORIES = new Set<TagCategory>([
 "zoning",
 "infrastructure",
 "utilities",
 "budget",
 "public_safety",
 "environment",
 "community",
 "legislation",
 "personnel",
 "transit",
 "hearing",
 "miscellaneous",
]);

export function stripCitations(t: string): string {
 // NotebookLM output sometimes has trailing citations like " [1, 3-5]".
 // Strip them so the tag text reads cleanly.
 return t.replace(/\s*\[\d+(?:[-,\s\d]*)\]/g, "");
}

export function parseEpisodeTags(
 raw: string | null | undefined
): EpisodeTag[] {
 if (!raw) return [];
 const cleaned = stripCitations(raw);
 const lines = cleaned.split(/\r?\n/);
 const tags: EpisodeTag[] = [];
 for (const line of lines) {
 // TAG: <text> | CATEGORY: <cat>
 const m = line.match(/TAG:\s*(.+?)\s*\|\s*CATEGORY:\s*([a-z_]+)/i);
 if (!m) continue;
 const text = m[1].trim().replace(/^[\-•]\s*/, "");
 const cat = m[2].toLowerCase().trim();
 if (!text) continue;
 const category = (VALID_CATEGORIES.has(cat as TagCategory)
 ? cat
 : "miscellaneous") as TagCategory;
 tags.push({ text, category });
 if (tags.length >= 5) break;
 }
 return tags;
}

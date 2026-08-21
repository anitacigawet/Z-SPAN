/**
 * topicTags — controlled vocabulary for Cast-page quote filtering.
 *
 * Per James 2026-05-11 (T-007 V1), the Cast page surfaces ONLY these
 * five topic categories as filter buttons. Quotes extracted by
 * NotebookLM can still carry richer tags; this list defines what shows
 * up on the user-facing UI.
 *
 * MUST stay in sync with `parsers/topic_tags.py`. When adding or
 * removing a tag here, update the Python file too — there's no
 * compile-time check that enforces parity.
 */

export type TopicTagId =
  | "data_centers"
  | "water_rights"
  | "diversity_inclusion"
  | "lgbtq"
  | "education";

export interface TopicTagDef {
  id: TopicTagId;
  label: string;
  hint: string;
}

export const TOPIC_TAGS: ReadonlyArray<TopicTagDef> = [
  {
    id: "data_centers",
    label: "Data Centers",
    hint:
      "Data-center proposals, hyperscaler expansion, water/power demands of such facilities, related zoning or incentive votes.",
  },
  {
    id: "water_rights",
    label: "Water Rights",
    hint:
      "Colorado River allocations, well permits, groundwater conservation, drought response, water-supply infrastructure.",
  },
  {
    id: "diversity_inclusion",
    label: "Diversity & Inclusion",
    hint:
      "DEI policy, civic-access programs, language services, accessibility accommodations, equity in city services.",
  },
  {
    id: "lgbtq",
    label: "LGBTQ",
    hint:
      "LGBTQ-related ordinances, official recognition or proclamations, public-comment exchanges on LGBTQ topics.",
  },
  {
    id: "education",
    label: "Education",
    hint:
      "School-board liaison items, library funding or policy, civic-education partnerships, after-school programs.",
  },
];

export const TOPIC_TAG_IDS: ReadonlyArray<TopicTagId> = TOPIC_TAGS.map(t => t.id);

export const TOPIC_LABELS = Object.fromEntries(
  TOPIC_TAGS.map(tag => [tag.id, tag.label]),
) as Readonly<Record<TopicTagId, string>>;

export const OTHER_TAG_ID = "other";

export function topicLabel(tag: string): string {
  const def = TOPIC_TAGS.find(t => t.id === tag);
  return def ? def.label : tag.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

export function isFeaturedTag(tag: string): boolean {
  return TOPIC_TAG_IDS.includes(tag as TopicTagId);
}

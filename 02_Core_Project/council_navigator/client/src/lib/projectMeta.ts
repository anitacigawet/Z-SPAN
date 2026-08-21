/**
 * projectMeta — single source of truth for project-level metadata used
 * by public-facing surfaces.
 *
 * Currently houses:
 *   - PROJECT_PUBLIC_LAUNCH_DATE (null until V1 public release; operator
 *     flips at deploy time)
 *   - DISCLAIMER_VERSION (bump on every material disclaimer-copy
 *     revision; triggers full re-acknowledgment per user)
 *   - DISCLAIMER body content + acknowledgment sentence
 *   - Timing constants for the public vs operator paths
 *   - localStorage discipline constants
 *   - Canonical application and National Civics Catalog links
 *   - Disclaimer contact links
 *
 * Per S-091 C3, ALL constants below are single-source-of-truth for the
 * `<PublicDataDisclaimerGate>` component. Do not inline-duplicate
 * anywhere in code or markdown. The canonical disclaimer-copy spec
 * lives at `01_Project_Overview/S091_DISCLAIMER_COPY_LOCKED.md` and
 * THIS file is its TypeScript binding.
 */

// ─────────────────────────────────────────────────────────────────
// Project lifecycle
// ─────────────────────────────────────────────────────────────────

/**
 * Public launch date for the SITE (zspan.org going public), explicitly
 * decoupled from repository releases. Null until the operator flips it.
 * Used by `formatTimeSinceLaunch` to render the *"It has
 * currently been around for X"* fragment in paragraph 1 of the
 * disclaimer.
 *
 * Pre-launch fallback (null or future-dated): the entire time-since
 * fragment is omitted; paragraph 1 reads as the pre-launch variant.
 */
export const PROJECT_PUBLIC_LAUNCH_DATE: Date | null = null;

/** Public project links shared by visitor-facing surfaces. */
export const ZSPAN_REPOSITORY_URL =
  "https://github.com/anitacigawet/Z-SPAN" as const;
export const NATIONAL_CIVICS_CATALOG_URL =
  "https://github.com/anitacigawet/national-civics-catalog" as const;

export function nationalCivicsCatalogStateUrl(stateCode: string): string {
  return `${NATIONAL_CIVICS_CATALOG_URL}/tree/main/data/states/${stateCode.toLowerCase()}`;
}

/**
 * Natural-language time-since-launch renderer.
 *
 * Returns:
 *   - null when PROJECT_PUBLIC_LAUNCH_DATE is null or in the future
 *   - "1 week" / "8 weeks" for 0-13 weeks since launch
 *   - "1 month" / "5 months" for 13 weeks-18 months
 *   - "1 year" / "2 years and 3 months" for 18+ months
 */
export function formatTimeSinceLaunch(now: Date = new Date()): string | null {
  if (!PROJECT_PUBLIC_LAUNCH_DATE) return null;
  if (PROJECT_PUBLIC_LAUNCH_DATE > now) return null;
  const ms = now.getTime() - PROJECT_PUBLIC_LAUNCH_DATE.getTime();
  const days = Math.floor(ms / (1000 * 60 * 60 * 24));
  const weeks = Math.floor(days / 7);
  const months = Math.floor(days / 30);
  if (weeks < 13) {
    return weeks <= 1 ? "1 week" : `${weeks} weeks`;
  }
  if (months < 18) {
    return months <= 1 ? "1 month" : `${months} months`;
  }
  const years = Math.floor(days / 365);
  const remainingMonths = Math.max(0, months - years * 12);
  if (remainingMonths === 0) {
    return years === 1 ? "1 year" : `${years} years`;
  }
  return `${years} year${years > 1 ? "s" : ""} and ${remainingMonths} month${remainingMonths > 1 ? "s" : ""}`;
}

// ─────────────────────────────────────────────────────────────────
// Disclaimer (S-091 / C1 LOCKED)
// ─────────────────────────────────────────────────────────────────

/**
 * Version string for the public-data disclaimer. Bump on every
 * material copy revision; triggers a full re-acknowledgment cycle
 * for every user (stale localStorage acks become invalid because the
 * key embeds this version). Per D-095 fleet-media convention.
 */
export const DISCLAIMER_VERSION = "v3-civic-focus" as const;

/**
 * localStorage key for the disclaimer acknowledgment. Versioned so a
 * DISCLAIMER_VERSION bump silently re-prompts (the new key won't exist
 * in the user's localStorage and the gate fires fresh).
 */
export const DISCLAIMER_LOCALSTORAGE_KEY = `zspan_public_disclaimer_ack_${DISCLAIMER_VERSION}`;

/**
 * Timing constants for the two-stage gate. Public-path values are
 * the user-facing defaults; operator-path values collapse to 1s each
 * so operator can cycle through the gate during debugging while still
 * seeing the surface render (per the operator's requirement that the
 * gate stay visible in the operator's own view, never fully bypassed).
 */
export const DISCLAIMER_TIMINGS = {
  public: {
    stage1DwellMs: 10_000, // [Click here] button disabled this long
    stage2KaraokeMs: 2_000, // total karaoke duration for the ack sentence
  },
  operator: {
    stage1DwellMs: 1_000,
    stage2KaraokeMs: 1_000,
  },
} as const;

/**
 * Disclaimer link slots.
 *   - github: canonical repo URL (wired 2026-07-15).
 *   - officialContact1 / officialContact2: "official finds a mistake"
 *     channels. Interim-wired to the public corrections page (RR-4 — the real
 *     report-a-mistake channel) so no visitor hits a broken {placeholder}
 *     href. OPEN COPY DECISION (James): whether these should be two distinct
 *     channels (corrections page + corrections@ email once the mailbox is
 *     live). The disclaimer copy now uses a single named corrections-page
 *     link (officialContact1); officialContact2 stays reserved for a future
 *     distinct channel and is currently unused (2026-07-15 visitor-QA).
 */
export const DISCLAIMER_PLACEHOLDERS = {
  officialContact1: "/corrections",
  officialContact2: "/corrections",
  // The former generic GitHub slot remains absent because the disclaimer
  // no longer uses GitHub as a catch-all contribution affordance. App and
  // source-catalog links use the explicit constants above.
} as const;

/**
 * Acknowledgment sentence rendered at stage 2 of the gate. Per operator
 * 2026-06-25 (post-visual-iteration): two sentences across a line break,
 * with "is not 100% accurate" rendered in red for emphasis. Renders via
 * the etch-reveal pattern (`.zs-etch-revealed` / `.zs-etch-in` in
 * index.css); red words also carry `.zs-etch-red` modifier for the
 * red color variant.
 *
 * Structured as segments (instead of a single string) so the renderer
 * can flow per-word reveal across newlines AND apply per-segment color
 * classes. Plain-text DISCLAIMER_ACK_SENTENCE below is derived from
 * the segments for accessibility / analytics use cases.
 */
export interface DisclaimerAckSegment {
  /** Plain text payload — split into words for the per-word etch reveal. */
  text?: string;
  /** Apply the red color variant to the words in this segment. */
  red?: boolean;
  /** When true, emit a line break here (no words). */
  newline?: boolean;
}

export const DISCLAIMER_ACK_SEGMENTS: DisclaimerAckSegment[] = [
  // Session-30 revision (2026-07-04): collapsed to a single sentence.
  // The "experimental project" opener retired — the STATE 1 reading
  // surface already establishes the AI-project framing upfront, so the
  // ack sentence focuses solely on the not-authoritative acceptance.
  // Red segment gains a "nor claiming to be" clause and now renders
  // bold in addition to red (see .zs-etch-revealed.zs-etch-red in
  // index.css — bold applied globally to the red variant since it's
  // the emphasis color for this animation).
  { text: "I understand the data presented on this website " },
  { text: "may not be 100% accurate", red: true },
  { text: ", and I accept the consequence of treating it as such." },
];

/**
 * Plain-text rendering of the segments above. Used for accessibility
 * (aria-label fallbacks, screen-reader narration) + any future analytics
 * surface that wants to record what acknowledgment text the user agreed
 * to at their version.
 */
export const DISCLAIMER_ACK_SENTENCE = DISCLAIMER_ACK_SEGMENTS
  .map((s) => (s.newline ? "\n" : (s.text ?? "")))
  .join("");

/**
 * Persistence posture for the acknowledgment.
 *
 * Per operator 2026-06-25 (post-visual-iteration): the gate must re-fire
 * on EVERY page load AND on every navigation to a different episode,
 * regardless of prior acknowledgment. The operator knows this is
 * overkill and wants it anyway — the point is to stress the seriousness
 * of the disclaimer to every visitor, refresh included, operator's own
 * sessions included. This means:
 *   - DISCLAIMER_ACK_PERSIST_ACROSS_RELOADS = false → on every full
 *     page load, the Provider initializes `acked = false`, gate fires
 *     fresh.
 *   - The Provider also watches a scopeKey prop and resets ack when
 *     it changes — passed by App.tsx based on navigation view +
 *     meetingId, so navigating between BroadcastPages re-fires the
 *     gate even within a single page-load session.
 *
 * To re-enable persistence at a later stage (the operator has floated
 * relaxing the re-fire — removing it or shortening the trigger once
 * accounts exist; undecided), flip the constant below to true + re-introduce
 * localStorage read in the Provider's useState initializer (the write
 * side is removed entirely for now since there's nothing to write
 * when the read isn't honored).
 */
export const DISCLAIMER_ACK_PERSIST_ACROSS_RELOADS = false;

/**
 * Paragraph 1 of the disclaimer body, with the time-since-launch
 * fragment conditionally interpolated based on PROJECT_PUBLIC_LAUNCH_DATE.
 *
 * Pre-launch (date null or future): direct sentence, no fragment.
 * Post-launch: includes "It has currently been around for X and will
 * only improve as time goes on —" between the project-description and
 * the mission-statement clauses.
 */
export function getDisclaimerParagraph1(now: Date = new Date()): string {
  const timeSince = formatTimeSinceLaunch(now);
  if (!timeSince) {
    return "Z-SPAN was created as a solo experimental project, using Claude AI, to strengthen democratic systems by making city council meetings more visible and easier to follow in daily life.";
  }
  return `Z-SPAN was created as a solo experimental project, using Claude AI. It has currently been around for ${timeSince} and will only improve as time goes on — to strengthen democratic systems by making city council meetings more visible and easier to follow in daily life.`;
}

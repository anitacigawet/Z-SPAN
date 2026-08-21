/**
 * City Dashboard V0 (2026-07-03) — data helpers.
 *
 * The dashboard is the per-city citizen face. Per CITY_DASHBOARD_SPEC.md,
 * these helpers pull only the data the pipeline already produces + gate
 * the LIVE pill on the city's registered YouTube channel actually being
 * live right now (operator rule: the LIVE pill renders ONLY when the
 * city's own council meeting is actually streaming — never as decor).
 */

export interface MeetingSummary {
  id: number;
  meeting_id: number;
  meeting_date: string | null;
  meeting_title: string;
  video_url: string | null;
  youtube_video_url?: string | null;
  is_published?: 0 | 1 | null;
  episode_tagline?: string | null;
}

export interface DashboardHeadline {
  meeting_id: number;
  meeting_date: string | null;
  meeting_title: string;
  tagline: string;
  is_published: boolean;
}

export interface CityDashboardData {
  city: string;
  featuredMeeting: MeetingSummary | null;
  headlines: DashboardHeadline[];
  upcomingMeetings: MeetingSummary[];
  callsToAction: string[];
  liveState: "off" | "on" | "unknown";
  liveVideoId?: string | null;
  updatedAt: string;
}

interface RawMeetingRow {
  id: number;
  meeting_id: number | string;
  meeting_date: string | null;
  meeting_title: string;
  video_url: string | null;
  youtube_video_url?: string | null;
  is_published?: 0 | 1 | null;
  episode_tagline?: string | null;
}

interface NotebookOutput {
  content: string | null;
  content_url?: string | null;
  error?: string | null;
  status?: string | null;
}

interface NotebookResponse {
  meeting_id: number;
  city: string;
  meeting_title: string;
  meeting_date: string | null;
  video_url: string | null;
  outputs: Record<string, NotebookOutput | null>;
}

/** Pull the city's most-recent meetings from the Flask calendar endpoint. */
async function fetchRecentMeetings(
  cityName: string,
  limit: number,
): Promise<MeetingSummary[]> {
  // include_drafts=true so we surface headlines from processed-but-not-yet-published
  // meetings (the operator's V0 sees the full processed record; the D-006
  // publish gate still governs what leaves the flagship, not what the
  // dashboard experiment renders locally).
  // year=all so we don't limit to current year.
  const res = await fetch(
    `/api/cities/${encodeURIComponent(cityName)}/meetings?limit=${limit}&include_drafts=true&year=all`,
  );
  if (!res.ok) throw new Error(`meetings HTTP ${res.status}`);
  const data = (await res.json()) as { events?: RawMeetingRow[] };
  const rows = data.events ?? [];
  return rows.map(r => ({
    id: r.id,
    meeting_id: typeof r.meeting_id === "number" ? r.meeting_id : r.id,
    meeting_date: r.meeting_date,
    meeting_title: r.meeting_title,
    video_url: r.video_url ?? null,
    youtube_video_url: r.youtube_video_url ?? null,
    is_published: r.is_published ?? null,
    episode_tagline: r.episode_tagline ?? null,
  }));
}

/** Fetch a meeting's notebook outputs; returns null on any failure. */
async function fetchNotebook(meetingId: number): Promise<NotebookResponse | null> {
  try {
    const res = await fetch(`/api/notebook/${meetingId}`);
    if (!res.ok) return null;
    return (await res.json()) as NotebookResponse;
  } catch {
    return null;
  }
}

/** Extract the plain-text headline from an episode_tagline output. */
function taglineFromNotebook(nb: NotebookResponse | null): string | null {
  if (!nb) return null;
  const out = nb.outputs?.episode_tagline;
  if (!out || !out.content) return null;
  // taglines are usually one line — trim + cap.
  const raw = out.content.trim();
  if (!raw) return null;
  const line = raw.split(/\r?\n/).find(l => l.trim().length > 0) ?? raw;
  return line.length > 140 ? line.slice(0, 137) + "…" : line;
}

/** Pull `community_calls_to_action` when present — used by VOLUNTEER. */
function callsFromNotebook(nb: NotebookResponse | null): string[] {
  if (!nb) return [];
  const out = nb.outputs?.community_calls_to_action;
  if (!out || !out.content) return [];
  return out.content
    .split(/\r?\n/)
    .map(l => l.replace(/^[-•\d.\s]+/, "").trim())
    .filter(l => l.length > 0)
    .slice(0, 5);
}

/** Assemble the dashboard payload for a city — parallel fetches. */
export async function fetchCityDashboard(
  cityName: string,
  options: { headlineCount?: number } = {},
): Promise<CityDashboardData> {
  const headlineCount = options.headlineCount ?? 10;
  const meetings = await fetchRecentMeetings(cityName, 30);

  // The player rides the newest published meeting with any video URL.
  const featured =
    meetings.find(
      m =>
        m.is_published === 1 &&
        (m.video_url ?? m.youtube_video_url ?? "").length > 0,
    ) ?? null;

  // Upcoming (future-dated) meetings for EVENTS.
  const today = new Date().toISOString().slice(0, 10);
  const upcoming = meetings
    .filter(m => m.meeting_date && m.meeting_date >= today)
    .sort((a, b) =>
      (a.meeting_date ?? "").localeCompare(b.meeting_date ?? ""),
    )
    .slice(0, 6);

  // Notebook fan-out for headlines: newest meetings first, capped.
  const candidates = meetings
    .slice()
    .sort((a, b) =>
      (b.meeting_date ?? "").localeCompare(a.meeting_date ?? ""),
    )
    .slice(0, headlineCount * 2);

  const notebooks = await Promise.all(
    candidates.map(async m => ({
      meeting: m,
      nb: await fetchNotebook(m.meeting_id),
    })),
  );

  const headlines: DashboardHeadline[] = [];
  const callsSet = new Set<string>();
  for (const { meeting, nb } of notebooks) {
    const tagline = taglineFromNotebook(nb);
    if (tagline && headlines.length < headlineCount) {
      headlines.push({
        meeting_id: meeting.meeting_id,
        meeting_date: meeting.meeting_date,
        meeting_title: meeting.meeting_title,
        tagline,
        is_published: meeting.is_published === 1,
      });
    }
    for (const c of callsFromNotebook(nb)) callsSet.add(c);
  }

  // LIVE probe — check /api/guide (the S-015 read-only live-streams cache
  // guide_detector.py populates) for a stream on this city. Any failure
  // stays "unknown"; a hit fills liveState + liveVideoId.
  let liveState: "off" | "on" | "unknown" = "unknown";
  let liveVideoId: string | null = null;
  try {
    const res = await fetch("/api/guide");
    if (res.ok) {
      const g = (await res.json()) as { ok?: boolean; live?: Array<{ city?: string; video_id?: string }> };
      if (g?.ok) {
        const cityLc = cityName.toLowerCase();
        const hit = (g.live ?? []).find(
          s => (s.city ?? "").toLowerCase() === cityLc,
        );
        if (hit && hit.video_id) {
          liveState = "on";
          liveVideoId = hit.video_id;
        } else {
          liveState = "off";
        }
      }
    }
  } catch {
    /* leave liveState as "unknown" */
  }

  return {
    city: cityName,
    featuredMeeting: featured,
    headlines,
    upcomingMeetings: upcoming,
    callsToAction: Array.from(callsSet).slice(0, 8),
    liveState,
    liveVideoId,
    updatedAt: new Date().toISOString(),
  };
}

/** Parse a YouTube video id from an embed-capable URL, or null if not YouTube. */
export function youtubeIdOf(url: string | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    if (u.hostname.endsWith("youtube.com")) {
      const v = u.searchParams.get("v");
      if (v) return v;
      // /embed/<id>
      const m = u.pathname.match(/^\/embed\/([\w-]{6,})/);
      if (m) return m[1];
    }
    if (u.hostname === "youtu.be") {
      const m = u.pathname.match(/^\/([\w-]{6,})/);
      if (m) return m[1];
    }
  } catch {
    /* not a URL */
  }
  return null;
}

/** Human "N min/h/d ago" for the news feed timestamps. */
export function relativeAgo(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
  if (Number.isNaN(d.getTime())) return "";
  const diff = Date.now() - d.getTime();
  const mins = Math.round(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  const days = Math.round(hrs / 24);
  if (days < 30) return `${days} day${days === 1 ? "" : "s"} ago`;
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

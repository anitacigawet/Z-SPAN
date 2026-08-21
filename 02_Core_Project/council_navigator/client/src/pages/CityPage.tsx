import { useState, useEffect } from "react";
import {
  ArrowLeft,
  Building2,
  Play,
  ExternalLink,
  Download,
  Users,
  X,
  Mail,
  Phone,
  User,
  Calendar,
  MapPin,
  Clock,
  AlertCircle,
} from "lucide-react";
import { downloadICalFile, getGoogleCalendarUrl } from "../utils/icalendar";
import MeetingSchedulePanel from "../components/MeetingSchedulePanel";
import { FollowButton } from "../components/FollowButton";
import { fetchForPlane } from "../lib/planeFetch";
import { isPublicPlane } from "../lib/trustPlane";

interface Meeting {
  id?: number;
  public_id?: string;
  meeting_title: string;
  meeting_date: string;
  meeting_time: string;
  meeting_location: string;
  agenda_url: string;
  minutes_url: string;
  video_url: string;
  meeting_status: string;
}

interface CouncilMember {
  name: string;
  title: string;
  email?: string | null;
  phone?: string | null;
  ward?: string | null;
  photo_url?: string | null;
}

interface CityPageProps {
  cityName: string;
  countyName: string;
  onBack: () => void;
}

// Convert common video URLs to embeddable iframe URLs.
// Returns null when the URL cannot be safely embedded.
function getEmbedUrl(url: string): string | null {
  if (!url) return null;
  try {
    const yt = url.match(
      /(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/v\/)([\w-]{11})/
    );
    if (yt) return `https://www.youtube.com/embed/${yt[1]}?rel=0&modestbranding=1`;

    const vimeo = url.match(/vimeo\.com\/(\d+)/);
    if (vimeo) return `https://player.vimeo.com/video/${vimeo[1]}`;
  } catch {
    /* fall through */
  }
  return null;
}

function formatDateLong(dateStr: string): string {
  if (!dateStr) return "Date TBD";
  try {
    const d = /^\d{4}-\d{2}-\d{2}/.test(dateStr)
      ? new Date(dateStr + "T00:00:00")
      : new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    return d.toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
}

function formatDateShort(dateStr: string): string {
  if (!dateStr) return "TBD";
  try {
    const d = /^\d{4}-\d{2}-\d{2}/.test(dateStr)
      ? new Date(dateStr + "T00:00:00")
      : new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    return d
      .toLocaleDateString("en-US", {
        month: "short",
        day: "2-digit",
        year: "numeric",
      })
      .toUpperCase();
  } catch {
    return dateStr;
  }
}

export default function CityPage({
  cityName,
  countyName,
  onBack,
}: CityPageProps) {
  const publicPlane = isPublicPlane();
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showVideo, setShowVideo] = useState(false);
  const [showCouncil, setShowCouncil] = useState(false);
  const [councilMembers, setCouncilMembers] = useState<CouncilMember[]>([]);
  const [councilLoading, setCouncilLoading] = useState(false);

  // Load meetings when city changes
  useEffect(() => {
    setLoading(true);
    setError(null);
    setSelectedIdx(0);
    const request = publicPlane
      ? fetch(`/public-api/cities/${encodeURIComponent(cityName)}/meetings?year=all`)
      : fetch("/api/calendar/events", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cityName }),
        });
    request
      .then(res => res.json())
      .then(data => {
        if (data.error) {
          setError(data.error);
        } else {
          // Sort newest first so "Latest" is at top of sidebar
          const sorted = [...(data.events || [])].sort((a: Meeting, b: Meeting) => {
            const da = new Date(a.meeting_date).getTime() || 0;
            const db = new Date(b.meeting_date).getTime() || 0;
            return db - da;
          });
          setMeetings(sorted);
        }
        setLoading(false);
      })
      .catch(() => {
        setError("Failed to load meetings");
        setLoading(false);
      });
  }, [cityName, publicPlane]);

  // Lazy-load council members the first time the panel opens
  useEffect(() => {
    if (!showCouncil || councilMembers.length > 0 || councilLoading) return;
    setCouncilLoading(true);
    fetchForPlane({
      publicPath: `/public-api/cast/${encodeURIComponent(cityName)}`,
      operatorPath: `/api/calendar/council/${encodeURIComponent(cityName)}`,
    })
      .then(res => res.json())
      .then(data =>
        setCouncilMembers(
          (data.members || []).map((member: any) => ({
            ...member,
            title: member.title ?? member.role ?? "Council",
          })),
        ),
      )
      .catch(() => {})
      .finally(() => setCouncilLoading(false));
  }, [showCouncil, cityName, councilMembers.length, councilLoading]);

  const selectedMeeting = meetings[selectedIdx];

  const embedUrl = selectedMeeting ? getEmbedUrl(selectedMeeting.video_url) : null;

  return (
    <div className="h-screen w-full bg-background flex text-foreground antialiased overflow-hidden">
      {/* ---------------------- Sidebar ---------------------- */}
      <aside className="w-[340px] bg-[var(--surface)] border-r border-[var(--line)] flex flex-col">
        {/* Sidebar header */}
        <div className="px-6 pt-7 pb-6 border-b border-[var(--line)]">
          <button
            onClick={onBack}
            className="group flex items-center gap-2 text-muted-foreground hover:text-foreground transition-colors mb-5 text-xs"
          >
            <ArrowLeft className="w-3.5 h-3.5 group-hover:-translate-x-0.5 transition-transform" />
            <span className="font-medium tracking-wide uppercase">Back</span>
          </button>
          <div className="flex items-center gap-3.5">
            <div className="bg-white text-black p-2 rounded-md flex-shrink-0">
              <Building2 className="w-5 h-5" />
            </div>
            <div className="min-w-0">
              <h1 className="text-base font-bold tracking-wider text-white uppercase truncate">
                {cityName}
              </h1>
              <p className="kg-eyebrow mt-1 truncate">
                {countyName} · Council Archive
              </p>
            </div>
          </div>
        </div>

        {/* Meeting list */}
        <div className="flex-1 overflow-y-auto kg-scroll px-3 py-4">
          {loading ? (
            <div className="flex flex-col items-center justify-center pt-16 gap-3">
              <span className="kg-dots">
                <span /> <span /> <span />
              </span>
              <p className="kg-eyebrow">Loading meetings…</p>
            </div>
          ) : error ? (
            <div className="px-3 py-6 text-center">
              <AlertCircle className="w-5 h-5 mx-auto mb-2 text-destructive/70" />
              <p className="text-xs text-muted-foreground">{error}</p>
            </div>
          ) : meetings.length === 0 ? (
            <div className="px-3 py-6 text-center">
              <Calendar className="w-5 h-5 mx-auto mb-2 text-muted-foreground/40" />
              <p className="text-xs text-muted-foreground">
                No meetings on file for {cityName}.
              </p>
            </div>
          ) : (
            <div className="space-y-1">
              {meetings.map((m, i) => {
                const isSelected = i === selectedIdx;
                return (
                  <div key={i} className="relative">
                    <button
                      onClick={() => setSelectedIdx(i)}
                      className={`w-full text-left p-5 rounded-xl transition-all duration-200 border block relative ${
                        isSelected
                          ? "kg-row-selected border-transparent shadow-xl"
                          : "border-transparent hover:bg-[var(--surface-3)]/50 hover:border-[var(--line)]"
                      }`}
                    >
                      <div className="kg-eyebrow mb-2">
                        {/* H-6 Opus-fix #5: "Latest" alone competes with
                         *  the new Meeting Schedule panel above which
                         *  shows upcoming dates. Disambiguate to
                         *  "Latest archived" so the user knows the
                         *  sidebar is recordings, not the next meeting. */}
                        {i === 0 ? "Latest archived · " : ""}
                        {formatDateShort(m.meeting_date)}
                      </div>
                      <h3
                        className={`text-[14px] font-semibold mb-1.5 leading-snug tracking-wide ${
                          isSelected ? "text-white" : "text-foreground/70"
                        }`}
                      >
                        {m.meeting_title || "City Council Meeting"}
                      </h3>
                      <p className="text-[12px] text-muted-foreground leading-relaxed line-clamp-2">
                        {m.meeting_location || m.meeting_time || "Council session"}
                      </p>
                    </button>
                    {/* FollowButton only when meeting has a persisted id —
                     *  un-persisted meetings (scraped but never seen by
                     *  the bridge) have no stable identifier we can follow. */}
                    {m.id != null && (
                      <div className="pointer-events-none absolute right-3 top-3 z-10">
                        <div className="pointer-events-auto">
                          <FollowButton
                            targetType="meeting"
                            targetKey={String(m.id)}
                            targetLabel={m.meeting_title || `Meeting on ${m.meeting_date}`}
                            variant="ghost"
                          />
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Sidebar footer */}
        <div className="px-4 pb-4 pt-2 border-t border-[var(--line)] space-y-2">
          <button
            onClick={() => setShowCouncil(true)}
            className="w-full flex items-center gap-3 px-4 py-2.5 rounded-lg border border-[var(--line)] bg-[var(--surface-2)] hover:bg-[var(--surface-3)] text-foreground/80 hover:text-foreground text-xs font-medium transition-colors"
          >
            <Users className="w-3.5 h-3.5" />
            <span className="tracking-wide">Council Members</span>
          </button>
          <div className="bg-[var(--canvas)] border border-[var(--line)] rounded-lg px-4 py-2.5 flex items-center gap-3">
            <div className="kg-dot-active" />
            <span className="text-[12px] font-medium text-muted-foreground">
              System Active · Data Synced
            </span>
          </div>
        </div>
      </aside>

      {/* ---------------------- Main ---------------------- */}
      <main className="flex-1 flex flex-col h-screen overflow-hidden bg-[var(--canvas)]">
        <div className="flex-1 overflow-y-auto kg-scroll">
          <div className="max-w-[1200px] w-full mx-auto px-8 lg:px-12 py-10 kg-fade-in">
            {/* H-6: city-level Meeting Schedule subsection — always
             *  visible on CityPage above the meeting selection (or its
             *  empty state). CityPage auto-selects the latest meeting on
             *  mount, so a "no meeting selected" empty state rarely
             *  shows; pinning the schedule above the main content keeps
             *  it discoverable. The same component mounts in the
             *  ChannelsPage Cast view, per James 2026-06-03 ("both pages,
             *  inline projections always visible"). */}
            {!publicPlane && (
              <div className="mb-8">
                <MeetingSchedulePanel city={cityName} />
              </div>
            )}

            {!selectedMeeting ? (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <Calendar className="w-10 h-10 text-muted-foreground/30 mb-4" />
                {/* H-6 Opus-fix #1: when meetings list is empty but the
                 *  schedule panel above shows upcoming dates, the
                 *  "Select a meeting from the sidebar" copy contradicts
                 *  what the user sees. Swap the copy to acknowledge the
                 *  schedule. */}
                <h2 className="text-lg text-foreground/80 font-medium mb-2">
                  {meetings.length === 0
                    ? publicPlane
                      ? "No published meetings yet"
                      : "Upcoming meetings shown above"
                    : "Select a meeting from the sidebar"}
                </h2>
                <p className="text-sm text-muted-foreground">
                  {meetings.length === 0
                    ? `No archived recordings on file yet for ${cityName}.`
                    : "Pick any meeting from the sidebar."}
                </p>
              </div>
            ) : (
              <>
                {/* Header */}
                <div className="flex justify-between items-start gap-6 mb-10">
                  <div className="min-w-0">
                    <h2 className="text-3xl lg:text-4xl font-light tracking-wide text-white mb-3 leading-tight">
                      {selectedMeeting.meeting_title || "City Council Meeting"}
                    </h2>
                    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm text-muted-foreground font-medium">
                      <span className="flex items-center gap-1.5">
                        <Calendar className="w-3.5 h-3.5" />
                        {formatDateLong(selectedMeeting.meeting_date)}
                      </span>
                      {selectedMeeting.meeting_time && (
                        <span className="flex items-center gap-1.5">
                          <Clock className="w-3.5 h-3.5" />
                          {selectedMeeting.meeting_time}
                        </span>
                      )}
                      {selectedMeeting.meeting_location && (
                        <span className="flex items-center gap-1.5">
                          <MapPin className="w-3.5 h-3.5" />
                          {selectedMeeting.meeting_location}
                        </span>
                      )}
                    </div>
                  </div>
                  {selectedMeeting.video_url && (
                    <a
                      href={selectedMeeting.video_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="hidden sm:inline-flex bg-white hover:bg-gray-200 text-black font-semibold tracking-widest uppercase text-[11px] px-5 py-3.5 rounded-md shadow-lg items-center gap-2 transition-colors flex-shrink-0"
                    >
                      View Full Video
                      <ExternalLink className="w-3.5 h-3.5" />
                    </a>
                  )}
                </div>

                <div className="space-y-6">
                  {/* Video Player */}
                  <div className="aspect-[4/3] rounded-2xl border border-[var(--line)] bg-black flex flex-col relative overflow-hidden shadow-2xl">
                    {showVideo && embedUrl ? (
                      <iframe
                        src={embedUrl}
                        title="Meeting video"
                        className="absolute inset-0 w-full h-full"
                        frameBorder={0}
                        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                        allowFullScreen
                      />
                    ) : (
                      <>
                        <div className="absolute inset-0 bg-gradient-to-t from-black via-black/40 to-transparent" />
                        <div className="flex-1 flex flex-col items-center justify-center z-10 space-y-5 px-6 text-center">
                          {selectedMeeting.video_url ? (
                            embedUrl ? (
                              <button
                                onClick={() => setShowVideo(true)}
                                className="w-20 h-20 rounded-full bg-white/10 hover:bg-white/20 transition-all flex items-center justify-center backdrop-blur-md group"
                              >
                                <Play className="w-8 h-8 text-white ml-1 fill-white opacity-80 group-hover:opacity-100" />
                              </button>
                            ) : (
                              <a
                                href={selectedMeeting.video_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="w-20 h-20 rounded-full bg-white/10 hover:bg-white/20 transition-all flex items-center justify-center backdrop-blur-md group"
                                title="Open video on official site"
                              >
                                <ExternalLink className="w-7 h-7 text-white opacity-80 group-hover:opacity-100" />
                              </a>
                            )
                          ) : (
                            <div className="w-20 h-20 rounded-full bg-white/5 flex items-center justify-center">
                              <Play className="w-8 h-8 text-white/30 ml-1" />
                            </div>
                          )}
                          <span className="kg-eyebrow">
                            {selectedMeeting.video_url
                              ? embedUrl
                                ? "Watch Meeting Recording"
                                : "Open Recording on Official Site"
                              : "Video Not Yet Posted"}
                          </span>
                        </div>
                      </>
                    )}
                  </div>

                  <div>
                    {selectedMeeting.meeting_date && (
                      <div className="flex flex-wrap gap-2 pt-1">
                        <button
                          onClick={() =>
                            downloadICalFile(selectedMeeting, cityName)
                          }
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-[var(--surface-2)] border border-[var(--line)] hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors"
                        >
                          <Download className="w-3 h-3" /> .ics
                        </button>
                        <a
                          href={getGoogleCalendarUrl(selectedMeeting, cityName)}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-[var(--surface-2)] border border-[var(--line)] hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors"
                        >
                          <ExternalLink className="w-3 h-3" /> Google Cal
                        </a>
                        {selectedMeeting.agenda_url && (
                          <a
                            href={selectedMeeting.agenda_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-[var(--surface-2)] border border-[var(--line)] hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors"
                          >
                            <ExternalLink className="w-3 h-3" /> Agenda
                          </a>
                        )}
                        {selectedMeeting.minutes_url && (
                          <a
                            href={selectedMeeting.minutes_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-[var(--surface-2)] border border-[var(--line)] hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors"
                          >
                            <ExternalLink className="w-3 h-3" /> Minutes
                          </a>
                        )}
                      </div>
                    )}
                  </div>
                </div>

              </>
            )}
          </div>
        </div>
      </main>

      {/* Council members modal */}
      {showCouncil && (
        <div
          className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4 kg-fade-in"
          onClick={() => setShowCouncil(false)}
        >
          <div
            className="bg-[var(--surface-2)] border border-[var(--line)] rounded-2xl shadow-2xl max-w-3xl w-full max-h-[80vh] overflow-y-auto kg-scroll"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-6 py-5 border-b border-[var(--line)] sticky top-0 bg-[var(--surface-2)]">
              <div className="flex items-center gap-3">
                <Users className="w-4 h-4 text-foreground/70" />
                <h3 className="text-sm font-semibold uppercase tracking-wider">
                  {cityName} Council
                </h3>
              </div>
              <button
                onClick={() => setShowCouncil(false)}
                className="p-1.5 rounded-md hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-6">
              {councilLoading ? (
                <div className="flex flex-col items-center py-12 gap-3">
                  <span className="kg-dots">
                    <span /> <span /> <span />
                  </span>
                  <p className="kg-eyebrow">Loading council members…</p>
                </div>
              ) : councilMembers.length === 0 ? (
                <div className="text-center py-10">
                  <p className="text-sm text-muted-foreground">
                    No council member data available for {cityName}.
                  </p>
                </div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {councilMembers.map((member, idx) => (
                    <div
                      key={idx}
                      className="bg-[var(--canvas)] rounded-xl p-4 border border-[var(--line)] hover:border-[var(--line-strong)] transition-colors"
                    >
                      <div className="flex items-start gap-3 mb-3">
                        <div className="w-9 h-9 rounded-full bg-[var(--surface-3)] flex items-center justify-center flex-shrink-0">
                          <User className="w-4 h-4 text-foreground/60" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <h5 className="text-sm font-semibold text-foreground truncate">
                            {member.name}
                          </h5>
                          <p className="text-[11px] text-muted-foreground mt-0.5 uppercase tracking-wider">
                            {member.title}
                            {member.ward && (
                              <span> · Ward {member.ward}</span>
                            )}
                          </p>
                        </div>
                      </div>
                      <div className="flex gap-2">
                        {member.email && (
                          <a
                            href={`mailto:${member.email}`}
                            className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-md bg-[var(--surface-3)] hover:bg-[var(--line-strong)] border border-[var(--line)] text-foreground/80 hover:text-foreground text-xs font-medium transition-colors"
                          >
                            <Mail className="w-3 h-3" />
                            <span>Email</span>
                          </a>
                        )}
                        {member.phone && (
                          <a
                            href={`tel:${member.phone}`}
                            className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-md bg-[var(--surface-3)] hover:bg-[var(--line-strong)] border border-[var(--line)] text-foreground/80 hover:text-foreground text-xs font-medium transition-colors"
                          >
                            <Phone className="w-3 h-3" />
                            <span className="truncate">{member.phone}</span>
                          </a>
                        )}
                        {!member.email && !member.phone && (
                          <p className="text-[11px] text-muted-foreground/70 text-center py-1">
                            No contact info available
                          </p>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <p className="text-[11px] text-muted-foreground/70 mt-5 text-center">
                Council data sourced via AI research. Verify details on the
                official city website.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

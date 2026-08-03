import { useState, useEffect, useCallback, useRef } from "react";
import { Search, Calendar, MapPin, X, Clock, ChevronLeft, ChevronRight, Download, ExternalLink, Play, } from "lucide-react";
import { downloadICalFile, getGoogleCalendarUrl } from "../utils/icalendar";
import { fetchForPlane } from "../lib/planeFetch";
interface SearchResult {
    id?: number;
    public_id?: string;
    city: string;
    county: string;
    state: string;
    meeting_title: string;
    meeting_date: string;
    meeting_time: string;
    meeting_location: string;
    meeting_status: string;
    agenda_url: string;
    minutes_url: string;
    video_url: string;
    summary?: string;
    calendar_url?: string;
}
interface SearchResponse {
    success: boolean;
    results: SearchResult[];
    total: number;
    limit: number;
    offset: number;
    has_more: boolean;
}
interface Stats {
    total_cities: number;
    active_cities?: number;
    total_meetings: number;
    meetings_by_county: Record<string, number>;
    top_cities: Array<{
        city: string;
        county: string;
        meetings: number;
    }>;
}
const COUNTIES = [
    "Apache County",
    "Cochise County",
    "Coconino County",
    "Gila County",
    "Graham County",
    "Greenlee County",
    "La Paz County",
    "Maricopa County",
    "Mohave County",
    "Navajo County",
    "Pima County",
    "Pinal County",
    "Santa Cruz County",
    "Yavapai County",
    "Yuma County",
];
const RESULTS_PER_PAGE = 25;
function formatDate(dateStr: string) {
    if (!dateStr)
        return "Date TBD";
    try {
        const d = new Date(dateStr);
        if (isNaN(d.getTime()))
            return dateStr;
        return d.toLocaleDateString("en-US", {
            weekday: "short",
            year: "numeric",
            month: "short",
            day: "numeric",
        });
    }
    catch {
        return dateStr;
    }
}
interface SearchPageProps {
    initialQuery?: string;
}
export default function SearchPage({ initialQuery }: SearchPageProps = {}) {
    const [searchQuery, setSearchQuery] = useState(initialQuery ?? "");
    const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
    const [loading, setLoading] = useState(false);
    const [selectedCounty, setSelectedCounty] = useState<string>("");
    const [dateFrom, setDateFrom] = useState("");
    const [dateTo, setDateTo] = useState("");
    const [totalResults, setTotalResults] = useState(0);
    const [currentPage, setCurrentPage] = useState(1);
    const [hasMore, setHasMore] = useState(false);
    const [stats, setStats] = useState<Stats | null>(null);
    const [hasSearched, setHasSearched] = useState(false);
    const [searchTime, setSearchTime] = useState(0);
    useEffect(() => {
        fetchForPlane({
            publicPath: "/public-api/calendar/stats",
            operatorPath: "/api/calendar/stats",
        })
            .then(res => res.json())
            .then(data => setStats(data))
            .catch(() => { });
    }, []);
    const handleSearch = useCallback(async (page: number = 1) => {
        setLoading(true);
        setHasSearched(true);
        const startTime = performance.now();
        const params = new URLSearchParams();
        if (searchQuery.trim())
            params.set("q", searchQuery.trim());
        if (selectedCounty)
            params.set("county", selectedCounty);
        if (dateFrom)
            params.set("date_from", dateFrom);
        if (dateTo)
            params.set("date_to", dateTo);
        params.set("limit", String(RESULTS_PER_PAGE));
        params.set("offset", String((page - 1) * RESULTS_PER_PAGE));
        try {
            const suffix = params.toString();
            const response = await fetchForPlane({
                publicPath: `/public-api/calendar/search?${suffix}`,
                operatorPath: `/api/calendar/search?${suffix}`,
            });
            const data: SearchResponse = await response.json();
            setSearchResults(data.results);
            setTotalResults(data.total);
            setHasMore(data.has_more);
            setCurrentPage(page);
            setSearchTime(Math.round(performance.now() - startTime));
        }
        catch {
            setSearchResults([]);
            setTotalResults(0);
        }
        finally {
            setLoading(false);
        }
    }, [searchQuery, selectedCounty, dateFrom, dateTo]);
    const didAutoRun = useRef(false);
    useEffect(() => {
        if (didAutoRun.current)
            return;
        if (initialQuery && initialQuery.trim()) {
            didAutoRun.current = true;
            handleSearch(1);
        }
    }, [initialQuery, handleSearch]);
    const handleKeyPress = (e: React.KeyboardEvent) => {
        if (e.key === "Enter")
            handleSearch(1);
    };
    const clearFilters = () => {
        setSelectedCounty("");
        setDateFrom("");
        setDateTo("");
        setSearchQuery("");
        setSearchResults([]);
        setHasSearched(false);
        setTotalResults(0);
    };
    const totalPages = Math.ceil(totalResults / RESULTS_PER_PAGE);
    const datePresets = [
        {
            label: "Upcoming",
            fn: () => {
                const today = new Date().toISOString().split("T")[0];
                setDateFrom(today);
                setDateTo("");
            },
        },
        {
            label: "This Month",
            fn: () => {
                const now = new Date();
                const start = new Date(now.getFullYear(), now.getMonth(), 1)
                    .toISOString()
                    .split("T")[0];
                const end = new Date(now.getFullYear(), now.getMonth() + 1, 0)
                    .toISOString()
                    .split("T")[0];
                setDateFrom(start);
                setDateTo(end);
            },
        },
        {
            label: "Next 30 Days",
            fn: () => {
                const today = new Date();
                const future = new Date(today.getTime() + 30 * 86400000);
                setDateFrom(today.toISOString().split("T")[0]);
                setDateTo(future.toISOString().split("T")[0]);
            },
        },
        {
            label: "Past 30 Days",
            fn: () => {
                const today = new Date();
                const past = new Date(today.getTime() - 30 * 86400000);
                setDateFrom(past.toISOString().split("T")[0]);
                setDateTo(today.toISOString().split("T")[0]);
            },
        },
        {
            label: "Past 90 Days",
            fn: () => {
                const today = new Date();
                const past = new Date(today.getTime() - 90 * 86400000);
                setDateFrom(past.toISOString().split("T")[0]);
                setDateTo(today.toISOString().split("T")[0]);
            },
        },
    ];
    const handleDownloadICS = (result: SearchResult) => {
        downloadICalFile({
            meeting_title: result.meeting_title,
            meeting_date: result.meeting_date,
            meeting_time: result.meeting_time || "",
            meeting_location: result.meeting_location || "",
            agenda_url: result.agenda_url || "",
            minutes_url: result.minutes_url || "",
            video_url: result.video_url || "",
        }, result.city);
    };
    const handleGoogleCalendar = (result: SearchResult) => {
        window.open(getGoogleCalendarUrl({
            meeting_title: result.meeting_title,
            meeting_date: result.meeting_date,
            meeting_time: result.meeting_time || "",
            meeting_location: result.meeting_location || "",
            agenda_url: result.agenda_url || "",
            minutes_url: result.minutes_url || "",
            video_url: result.video_url || "",
        }, result.city), "_blank");
    };
    return (<div className="max-w-5xl mx-auto px-6 lg:px-10 py-10 kg-fade-in">
      <div className="mb-8">
        <p className="kg-eyebrow mb-3">Find a Meeting</p>
        <h2 className="text-3xl font-light tracking-wide text-white mb-3">
          Search every council meeting in Arizona
        </h2>
        <p className="text-sm text-muted-foreground max-w-2xl">
          Filter by county and date, or search free-text across titles,
          locations, and cities.
        </p>
      </div>

      
      <div className="kg-card p-6 mb-8">
        
        <div className="relative mb-4">
          <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground"/>
          <input type="text" value={searchQuery} onChange={e => setSearchQuery(e.target.value)} onKeyDown={handleKeyPress} placeholder="Search by meeting title, city, county, or location…" className="w-full pl-11 pr-4 py-3.5 rounded-lg bg-[var(--canvas)] border border-[var(--line)] text-white placeholder-muted-foreground/60 text-sm focus:outline-none focus:border-[var(--line-strong)] focus:ring-1 focus:ring-white/10 transition-colors"/>
        </div>

        
        <div className="flex flex-wrap gap-2 mb-4">
          {datePresets.map(preset => (<button key={preset.label} onClick={preset.fn} className="px-3 py-1.5 rounded-md bg-[var(--canvas)] hover:bg-[var(--surface-3)] border border-[var(--line)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors">
              {preset.label}
            </button>))}
        </div>

        
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-5">
          <div>
            <label className="kg-eyebrow block mb-2">County</label>
            <select value={selectedCounty} onChange={e => setSelectedCounty(e.target.value)} className="w-full px-3 py-2.5 rounded-md bg-[var(--canvas)] border border-[var(--line)] text-foreground/85 text-sm focus:outline-none focus:border-[var(--line-strong)] transition-colors appearance-none cursor-pointer">
              <option value="">All Counties</option>
              {COUNTIES.map(county => (<option key={county} value={county}>
                  {county}
                </option>))}
            </select>
          </div>
          <div>
            <label className="kg-eyebrow block mb-2">From</label>
            <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} className="w-full px-3 py-2.5 rounded-md bg-[var(--canvas)] border border-[var(--line)] text-foreground/85 text-sm focus:outline-none focus:border-[var(--line-strong)] transition-colors"/>
          </div>
          <div>
            <label className="kg-eyebrow block mb-2">To</label>
            <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} className="w-full px-3 py-2.5 rounded-md bg-[var(--canvas)] border border-[var(--line)] text-foreground/85 text-sm focus:outline-none focus:border-[var(--line-strong)] transition-colors"/>
          </div>
        </div>

        
        <div className="flex gap-3">
          <button onClick={() => handleSearch(1)} disabled={loading} className="flex-1 bg-white hover:bg-gray-200 text-black px-6 py-3 rounded-md font-semibold uppercase tracking-widest text-[11px] transition-colors disabled:opacity-50 flex items-center justify-center gap-2">
            {loading ? (<span className="kg-dots scale-75">
                <span /> <span /> <span />
              </span>) : (<Search className="w-3.5 h-3.5"/>)}
            {loading ? "Searching" : "Search"}
          </button>
          {(selectedCounty || dateFrom || dateTo || searchQuery) && (<button onClick={clearFilters} className="bg-[var(--canvas)] hover:bg-[var(--surface-3)] border border-[var(--line)] text-muted-foreground hover:text-foreground px-5 py-3 rounded-md font-semibold uppercase tracking-widest text-[11px] transition-colors flex items-center gap-2">
              <X className="w-3.5 h-3.5"/> Clear
            </button>)}
        </div>
      </div>

      
      {!hasSearched && stats && (<div className="space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div className="kg-card p-5">
              <p className="kg-eyebrow mb-2">Total Meetings</p>
              <p className="text-3xl font-light text-white tabular-nums">
                {stats.total_meetings.toLocaleString()}
              </p>
              <p className="text-xs text-muted-foreground mt-1">
                across {stats.active_cities} cities
              </p>
            </div>
            <div className="kg-card p-5">
              <p className="kg-eyebrow mb-2">Counties</p>
              <p className="text-3xl font-light text-white tabular-nums">
                {Object.keys(stats.meetings_by_county).length}
              </p>
              <p className="text-xs text-muted-foreground mt-1">
                with meeting data
              </p>
            </div>
            <div className="kg-card p-5">
              <p className="kg-eyebrow mb-2">Top City</p>
              <p className="text-2xl font-light text-white truncate">
                {stats.top_cities[0]?.city || "N/A"}
              </p>
              <p className="text-xs text-muted-foreground mt-1 tabular-nums">
                {stats.top_cities[0]?.meetings || 0} meetings
              </p>
            </div>
          </div>
        </div>)}

      
      {hasSearched && (<div>
          <div className="kg-card p-5 mb-3 flex items-center justify-between flex-wrap gap-3">
            <div>
              <p className="kg-eyebrow">Results</p>
              <p className="text-2xl font-light text-white mt-1 tabular-nums">
                {totalResults.toLocaleString()}{" "}
                <span className="text-base text-muted-foreground font-normal">
                  {totalResults === 1 ? "match" : "matches"}
                </span>
                {searchTime > 0 && (<span className="ml-3 text-xs text-muted-foreground tracking-wider uppercase">
                    · {searchTime}ms
                  </span>)}
              </p>
            </div>
            {totalPages > 1 && (<div className="flex items-center gap-2">
                <button onClick={() => handleSearch(currentPage - 1)} disabled={currentPage <= 1 || loading} className="p-2 rounded-md bg-[var(--canvas)] border border-[var(--line)] text-muted-foreground hover:text-foreground disabled:opacity-30 transition-colors">
                  <ChevronLeft className="w-4 h-4"/>
                </button>
                <span className="text-xs text-muted-foreground tracking-wide uppercase">
                  Page {currentPage} of {totalPages}
                </span>
                <button onClick={() => handleSearch(currentPage + 1)} disabled={!hasMore || loading} className="p-2 rounded-md bg-[var(--canvas)] border border-[var(--line)] text-muted-foreground hover:text-foreground disabled:opacity-30 transition-colors">
                  <ChevronRight className="w-4 h-4"/>
                </button>
              </div>)}
          </div>

          {searchResults.length > 0 ? (<div className="kg-card overflow-hidden">
              <div className="divide-y divide-[var(--line)]">
                {searchResults.map((result, index) => (<div key={result.public_id ?? result.id ?? index} className="p-5 hover:bg-[var(--surface-3)]/30 transition-colors">
                    <h4 className="text-sm font-semibold text-white mb-2">
                      {result.meeting_title || "Untitled Meeting"}
                    </h4>
                    <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-muted-foreground text-xs mb-3">
                      <span className="flex items-center gap-1.5">
                        <MapPin className="w-3 h-3"/>
                        {result.city}, {result.county}
                      </span>
                      <span className="flex items-center gap-1.5">
                        <Calendar className="w-3 h-3"/>
                        {formatDate(result.meeting_date)}
                      </span>
                      {result.meeting_time && (<span className="flex items-center gap-1.5">
                          <Clock className="w-3 h-3"/> {result.meeting_time}
                        </span>)}
                    </div>
                    {result.meeting_location && (<p className="text-xs text-muted-foreground/70 mb-3">
                        {result.meeting_location}
                      </p>)}
                    <div className="flex flex-wrap gap-1.5">
                      {result.agenda_url && (<a href={result.agenda_url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-[var(--canvas)] border border-[var(--line)] hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors">
                          <ExternalLink className="w-3 h-3"/> Agenda
                        </a>)}
                      {result.minutes_url && (<a href={result.minutes_url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-[var(--canvas)] border border-[var(--line)] hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors">
                          <ExternalLink className="w-3 h-3"/> Minutes
                        </a>)}
                      {result.video_url && (<a href={result.video_url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-[var(--canvas)] border border-[var(--line)] hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors">
                          <Play className="w-3 h-3"/> Video
                        </a>)}
                      <button onClick={() => handleDownloadICS(result)} className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-[var(--canvas)] border border-[var(--line)] hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors">
                        <Download className="w-3 h-3"/> .ics
                      </button>
                      <button onClick={() => handleGoogleCalendar(result)} className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-[var(--canvas)] border border-[var(--line)] hover:bg-[var(--surface-3)] text-muted-foreground hover:text-foreground text-[11px] font-medium uppercase tracking-wider transition-colors">
                        <ExternalLink className="w-3 h-3"/> Google Cal
                      </button>
                    </div>
                  </div>))}
              </div>
            </div>) : (<div className="kg-card p-12 text-center">
              <p className="text-sm text-muted-foreground">
                No meetings found{searchQuery && ` matching "${searchQuery}"`}
                {selectedCounty && ` in ${selectedCounty}`}.
              </p>
            </div>)}

          {totalPages > 1 && (<div className="mt-5 flex justify-center gap-2">
              <button onClick={() => handleSearch(currentPage - 1)} disabled={currentPage <= 1 || loading} className="px-4 py-2 rounded-md bg-[var(--surface-2)] border border-[var(--line)] text-muted-foreground hover:text-foreground disabled:opacity-30 transition-colors text-xs uppercase tracking-wider font-medium flex items-center gap-1.5">
                <ChevronLeft className="w-3.5 h-3.5"/> Previous
              </button>
              <span className="px-3 py-2 text-muted-foreground text-xs uppercase tracking-wider">
                Page {currentPage} of {totalPages}
              </span>
              <button onClick={() => handleSearch(currentPage + 1)} disabled={!hasMore || loading} className="px-4 py-2 rounded-md bg-[var(--surface-2)] border border-[var(--line)] text-muted-foreground hover:text-foreground disabled:opacity-30 transition-colors text-xs uppercase tracking-wider font-medium flex items-center gap-1.5">
                Next <ChevronRight className="w-3.5 h-3.5"/>
              </button>
            </div>)}
        </div>)}
    </div>);
}

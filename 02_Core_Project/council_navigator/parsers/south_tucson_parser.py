"""
South Tucson — real scrape of /meetings/recent (table with agendas, minutes, media).
Index `calendar_url` often points at /calendar; the document grid lives on meetings/recent.
"""
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://www.southtucsonaz.gov"
DEFAULT_CALENDAR = f"{BASE}/calendar"
RECENT_MEETINGS = f"{BASE}/meetings/recent"

def _list_url(url):
    """Compatibility delegate for callers of the retired helper."""
    return _current_list_url(url or DEFAULT_CALENDAR)


def scrape_calendar(url=None):
    """Compatibility export backed only by the bounded current implementation."""
    return _scrape_calendar_current(url)


# The public export above delegates to this single bounded implementation.
from datetime import date as _date, datetime as _datetime
import re
from urllib.parse import urlparse as _urlparse

from polite_http import make_session


_SOURCE_HOSTS = {"southtucsonaz.gov", "www.southtucsonaz.gov"}
_MEDIA_HOSTS = _SOURCE_HOSTS | {"dropbox.com", "www.dropbox.com"}
_MAX_BYTES = 5_000_000
_CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
_COUNCIL_RE = re.compile(r"\b(?:town|city)\s+council\b", re.IGNORECASE)
_NON_COUNCIL_BODY_RE = re.compile(
    r"\b(?:planning\s+(?:and\s+zoning\s+)?commission|board\s+of\s+adjustment|"
    r"parks?\s+(?:and\s+recreation\s+)?board|library\s+board)\b",
    re.IGNORECASE,
)
_CANONICAL_FIELDS = (
    "meeting_title",
    "meeting_date",
    "meeting_time",
    "meeting_location",
    "meeting_status",
    "agenda_url",
    "minutes_url",
    "video_url",
    "agenda_packet_url",
    "ecomment_url",
    "meeting_id",
)


def _scrape_calendar_current(url=None):
    """Read the current official table or return an explicit blocked empty."""
    target = _current_list_url(url or DEFAULT_CALENDAR)
    with make_session() as session:
        with session.get(target, timeout=35, stream=True, allow_redirects=True) as response:
            final_host = (_urlparse(response.url).hostname or "").lower()
            if final_host not in _SOURCE_HOSTS:
                raise ValueError(f"South Tucson redirect reached disallowed host: {final_host}")
            body = bytearray()
            for chunk in response.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > _MAX_BYTES:
                    raise ValueError(f"South Tucson response exceeded {_MAX_BYTES} bytes")
            text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
            if _cloudflare_challenge(response.status_code, text):
                logger.warning("health_empty_kind=source_blocked")
                logger.warning(
                    "South Tucson source blocked: official /meetings/recent returned a Cloudflare "
                    "managed challenge; failure_shape=honest-empty missing_scope=current meetings"
                )
                return []
            if response.status_code in {401, 403, 429}:
                logger.warning("health_empty_kind=source_blocked")
            response.raise_for_status()

    soup = BeautifulSoup(text, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("South Tucson meeting-table fingerprint is absent")
    rows = table.find_all("tr")
    if len(rows) < 2:
        if "no results" in table.get_text(" ", strip=True).lower():
            logger.info("South Tucson official table explicitly reports no meeting results")
            logger.warning("health_empty_kind=confirmed_empty")
            return []
        raise ValueError("South Tucson table has no rows and no explicit empty-state marker")

    headers = [_clean_text(cell.get_text(" ", strip=True)).lower() for cell in rows[0].find_all(["th", "td"])]
    month_floor = _date.today().replace(day=1)
    meetings = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            logger.warning("South Tucson row dropped: expected at least two cells; html=%r", str(row)[:300])
            continue
        meeting_date, meeting_time = _parse_date_time(_clean_text(cells[0].get_text(" ", strip=True)))
        if not meeting_date:
            logger.warning("South Tucson row dropped: unparseable date text=%r", cells[0].get_text(" ", strip=True))
            continue
        if _date.fromisoformat(meeting_date) < month_floor:
            continue
        title = _clean_text(cells[1].get_text(" ", strip=True))
        if not title:
            logger.warning("South Tucson row dropped: missing title for date=%s", meeting_date)
            continue
        row_text = _clean_text(row)
        if not _COUNCIL_RE.search(row_text):
            if _NON_COUNCIL_BODY_RE.search(row_text):
                logger.warning(
                    "South Tucson row dropped: date=%s title=%r reason=explicit_non_council_body",
                    meeting_date,
                    title,
                )
                continue
            raise RuntimeError(
                "South Tucson current meeting row is governing-body ambiguous; "
                f"explicit Town/City Council evidence is required: {row_text[:300]!r}"
            )

        links = {"agenda_url": "", "minutes_url": "", "video_url": "", "agenda_packet_url": ""}
        detail_url = ""
        for index, cell in enumerate(cells):
            heading = headers[index] if index < len(headers) else ""
            for anchor in cell.select("a[href]"):
                href = _safe_current_url(anchor.get("href", ""), target)
                if not href:
                    logger.warning(
                        "South Tucson row URL dropped: title=%r heading=%r href=%r",
                        title,
                        heading,
                        anchor.get("href", ""),
                    )
                    continue
                if "packet" in heading:
                    links["agenda_packet_url"] = links["agenda_packet_url"] or href
                elif "agenda" in heading:
                    links["agenda_url"] = links["agenda_url"] or href
                elif "minutes" in heading:
                    links["minutes_url"] = links["minutes_url"] or href
                elif "video" in heading or "audio" in heading:
                    links["video_url"] = links["video_url"] or href
                elif "view" in heading:
                    detail_url = detail_url or href

        if _CANCELLED_RE.search(title):
            status = "Cancelled"
        elif links["minutes_url"]:
            status = "Minutes Available"
        elif links["agenda_url"] or links["agenda_packet_url"]:
            status = "Agenda Available"
        else:
            status = "Scheduled"
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": status,
            "agenda_url": links["agenda_url"],
            "minutes_url": links["minutes_url"],
            "video_url": links["video_url"],
            "agenda_packet_url": links["agenda_packet_url"],
            "ecomment_url": "",
            "meeting_id": _current_meeting_id(detail_url),
        }
        meetings.append({field: meeting[field] for field in _CANONICAL_FIELDS})

    _assert_schema(meetings)
    logger.warning(
        "South Tucson field absence: the list surface does not provide a reliable per-row location or e-comment link"
    )
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    logger.info("South Tucson scrape complete: current_month_forward=%d floor=%s", len(meetings), month_floor)
    return meetings


def _current_list_url(url):
    parsed = _urlparse(url)
    if (parsed.hostname or "").lower() not in _SOURCE_HOSTS:
        raise ValueError(f"South Tucson source host is not allowlisted: {parsed.hostname}")
    return url if "/meetings/recent" in parsed.path else RECENT_MEETINGS


def _cloudflare_challenge(status_code, text):
    lowered = text.lower()
    return status_code == 403 and "just a moment" in lowered and "challenges.cloudflare.com" in lowered


def _parse_date_time(value):
    parts = [part.strip() for part in value.split("|", 1)]
    try:
        meeting_date = _datetime.strptime(parts[0], "%b %d, %Y").date().isoformat()
    except ValueError:
        logger.warning("South Tucson date extraction failed: text=%r", value[:240])
        return "", ""
    if len(parts) == 1:
        logger.info("South Tucson meeting time absent: date=%s reason=no_time_segment", meeting_date)
        return meeting_date, ""
    match = re.search(
        r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?(?=\s|$|\s*-)",
        parts[1],
        re.IGNORECASE,
    )
    if not match:
        if re.search(r"\b(?:a\.?m\.?|p\.?m\.?)\b", parts[1], re.IGNORECASE):
            logger.warning(
                "South Tucson time extraction failed: date=%s text=%r",
                meeting_date,
                parts[1][:240],
            )
        else:
            logger.info("South Tucson meeting time absent: date=%s reason=no_visible_time", meeting_date)
        return meeting_date, ""
    return meeting_date, f"{int(match.group(1))}:{match.group(2) or '00'} {match.group(3).upper()}M"


def _safe_current_url(href, base_url):
    candidate = urljoin(base_url, href.strip())
    parsed = _urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in _MEDIA_HOSTS:
        return ""
    return candidate


def _current_meeting_id(url):
    match = re.search(r"/meeting/([^/?#]+)", url, re.IGNORECASE)
    if match:
        return match.group(1)
    logger.warning("South Tucson meeting_id absent: detail_url=%r reason=no_meeting_path_id", url)
    return ""


def _clean_text(value):
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return " ".join(text.replace("\ufffd", " ").split())


def _assert_schema(meetings):
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != _CANONICAL_FIELDS:
            raise ValueError(f"South Tucson row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"South Tucson row {index} contains a non-string value")

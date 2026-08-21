"""Current-year, current-month-forward parser for Superior's official council archive."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import logging
import re
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session

logger = logging.getLogger(__name__)

DEFAULT_URL = (
    "https://superioraz.gov/index.php/government/town-council/"
    "town-council-meeting-agendas/"
)
OFFICIAL_HOSTS = {"superioraz.gov", "www.superioraz.gov"}
MAX_RESPONSE_BYTES = 2_000_000
_CANCEL_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?<!\d)(0?[1-9]|1[0-2])[\s/_-]+(0?[1-9]|[12]\d|3[01])[\s/_-]+(20\d{2})(?!\d)"
)


@dataclass(frozen=True)
class _Fetch:
    status: int
    url: str
    text: str
    headers: dict[str, str]


@dataclass
class _Meeting:
    meeting_date: str
    meeting_title: str
    meeting_id: str = ""
    agenda_url: str = ""
    minutes_url: str = ""
    agenda_packet_url: str = ""
    cancelled: bool = False
    priorities: dict[str, int] = field(default_factory=dict)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _validate_official_url(url: str, label: str, base_url: str = DEFAULT_URL) -> str:
    absolute = urljoin(base_url, url.strip())
    if urlparse(absolute).scheme != "https" or _host(absolute) not in OFFICIAL_HOSTS:
        raise RuntimeError(f"Superior {label} left the official HTTPS host: {url!r}")
    return absolute


def _fetch_bounded(session, url: str) -> _Fetch:
    _validate_official_url(url, "request")
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        if urlparse(response.url).scheme != "https" or _host(response.url) not in OFFICIAL_HOSTS:
            raise RuntimeError(f"Superior request redirected to a disallowed host: {response.url}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"Superior response exceeded the {MAX_RESPONSE_BYTES}-byte safety cap"
                )
        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        logger.info(
            "Superior fetched status=%s bytes=%s url=%s",
            response.status_code,
            len(body),
            response.url,
        )
        return _Fetch(
            status=response.status_code,
            url=response.url,
            text=text,
            headers={key.lower(): value for key, value in response.headers.items()},
        )


def _blocker(fetch: _Fetch) -> str:
    lowered = fetch.text[:50_000].lower()
    title_soup = BeautifulSoup(fetch.text, "html.parser")
    title = title_soup.title.get_text(" ", strip=True).lower() if title_soup.title else ""
    if (
        fetch.headers.get("cf-mitigated", "").lower() == "challenge"
        or "challenges.cloudflare.com" in lowered
        or "__cf_chl" in lowered
        or title.startswith("just a moment")
    ):
        return "Cloudflare challenge"
    if title in {"access denied", "forbidden"} or "access denied" in lowered[:5_000]:
        return "access denied"
    if fetch.status in {401, 403, 429}:
        return f"HTTP {fetch.status}"
    return ""


def _require_accessible(fetch: _Fetch, label: str) -> None:
    blocker = _blocker(fetch)
    if blocker:
        logger.warning("health_empty_kind=source_blocked")
        raise RuntimeError(
            f"Superior official {label} is blocked by {blocker}; this is not a successful empty source"
        )
    if fetch.status != 200:
        raise RuntimeError(
            f"Superior official {label} returned HTTP {fetch.status}; this is not a successful empty source"
        )


def _find_year_folder(html: str, base_url: str, year: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    if "town council meeting agendas" not in page_text.lower():
        raise RuntimeError("Superior archive index lost its Town Council Meeting Agendas fingerprint")

    candidates: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        href = anchor["href"]
        if str(year) not in text and not re.search(rf"/{year}(?:-\d+)?(?:[/?#]|$)", href):
            continue
        absolute = _validate_official_url(href, "year-folder link", base_url)
        if "/town-council-meeting-agendas/" in urlparse(absolute).path:
            candidates.add(absolute)
    if len(candidates) != 1:
        raise RuntimeError(
            f"Superior archive index exposed {len(candidates)} official {year} folder candidates; "
            "expected exactly one"
        )
    folder = next(iter(candidates))
    logger.info("Superior current-year folder witnessed from index: %s", folder)
    return folder


def _extract_date(text: str) -> date | None:
    matches = {
        date(int(year), int(month), int(day))
        for month, day, year in _DATE_RE.findall(unquote(text[:800]))
    }
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        logger.warning("Superior document exposed ambiguous dates and was dropped: %r", text)
    elif not matches:
        logger.warning("Superior document exposed no supported date and was dropped: %r", text)
    return None


def _field(text: str) -> tuple[str, int]:
    lowered = re.sub(r"[^a-z0-9]+", " ", text.lower())
    amended = int("amended" in lowered or "revised" in lowered or "corrected" in lowered)
    if "minutes" in lowered:
        return "minutes_url", 30 + amended
    if "packet" in lowered:
        return "agenda_packet_url", 20 + amended
    if "agenda" in lowered:
        return "agenda_url", 10 + amended
    return "", 0


def _title(text: str) -> str:
    lowered = text.lower()
    if "work session" in lowered or "works session" in lowered:
        title = "Town Council Work Session"
    elif "special" in lowered:
        title = "Town Council Special Meeting"
    else:
        title = "Town Council Meeting"
    if _CANCEL_RE.search(text):
        title += " (Cancelled)"
    return title


def _parse_year_folder(html: str, folder_url: str, cutoff: date) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    if str(cutoff.year) not in page_text or "documents" not in page_text.lower():
        raise RuntimeError("Superior current-year folder lost its year/Documents fingerprint")

    groups: dict[tuple[str, str], _Meeting] = {}
    recognized = 0
    historical = 0
    undated_documents = 0
    newest: date | None = None
    for anchor in soup.find_all("a", href=True):
        text = BeautifulSoup(anchor.decode_contents(), "html.parser").get_text(" ", strip=True)
        combined = f"{text} {anchor['href']}"
        field_name, priority = _field(combined)
        if not field_name:
            continue
        meeting_date = _extract_date(combined)
        if meeting_date is None:
            undated_documents += 1
            continue
        if meeting_date.year != cutoff.year:
            continue
        recognized += 1
        newest = max(newest, meeting_date) if newest else meeting_date
        if meeting_date < cutoff:
            historical += 1
            continue
        document_url = _validate_official_url(
            anchor["href"],
            "document link",
            folder_url.rstrip("/") + "/",
        )
        title = _title(combined)
        key = (meeting_date.isoformat(), title)
        group = groups.setdefault(
            key,
            _Meeting(
                meeting_date=key[0],
                meeting_title=title,
                cancelled=bool(_CANCEL_RE.search(combined)),
            ),
        )
        old_priority = group.priorities.get(field_name, -1)
        if priority < old_priority:
            logger.warning(
                "Superior retained higher-priority %s for %s and dropped %s",
                field_name,
                key,
                document_url,
            )
            continue
        if getattr(group, field_name) and getattr(group, field_name) != document_url:
            logger.warning("Superior replaced %s for %s with stronger/equal evidence", field_name, key)
        setattr(group, field_name, document_url)
        group.priorities[field_name] = priority

    if recognized == 0:
        raise RuntimeError(
            f"Superior {cutoff.year} folder had no date-bearing agenda/minutes document evidence"
        )

    meetings: list[dict[str, str]] = []
    for key in sorted(groups):
        group = groups[key]
        status = (
            "Cancelled"
            if group.cancelled
            else "Minutes Available"
            if group.minutes_url
            else "Agenda Available"
            if group.agenda_url or group.agenda_packet_url
            else "Scheduled"
        )
        meetings.append(
            {
                "meeting_title": group.meeting_title,
                "meeting_date": group.meeting_date,
                "meeting_time": "",
                "meeting_location": "",
                "meeting_status": status,
                "agenda_url": group.agenda_url,
                "minutes_url": group.minutes_url,
                "video_url": "",
                "agenda_packet_url": group.agenda_packet_url,
                "ecomment_url": "",
                "meeting_id": group.meeting_id,
            }
        )

    logger.warning(
        "Superior archive exposes document IDs, not stable meeting IDs, and does not expose "
        "per-row meeting time or location; emitting those canonical fields empty"
    )
    logger.info(
        "Superior year-folder audit: recognized=%s historical_dropped=%s undated_documents=%s current_meetings=%s "
        "newest_source_date=%s cutoff=%s",
        recognized,
        historical,
        undated_documents,
        len(meetings),
        newest,
        cutoff,
    )
    if not meetings:
        if undated_documents:
            raise RuntimeError(
                "Superior folder contained undated agenda/minutes links, so an official zero cannot be witnessed"
            )
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Superior proved an honest current-month empty from its accessible %s folder; "
            "newest source date=%s",
            cutoff.year,
            newest,
        )
    return meetings


def scrape_calendar(calendar_url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Fetch only the archive index and its derived current-year folder."""
    parsed = urlparse(calendar_url)
    if parsed.scheme != "https" or _host(calendar_url) not in OFFICIAL_HOSTS:
        raise ValueError("Superior calendar URL must use HTTPS on the official town host")
    cutoff = date.today().replace(day=1)
    try:
        with make_session() as session:
            index_fetch = _fetch_bounded(session, DEFAULT_URL)
            _require_accessible(index_fetch, "agenda index")
            folder_url = _find_year_folder(index_fetch.text, index_fetch.url, cutoff.year)
            folder_fetch = _fetch_bounded(session, folder_url)
            _require_accessible(folder_fetch, f"{cutoff.year} agenda folder")
    except requests.exceptions.SSLError as exc:
        logger.warning("health_empty_kind=source_blocked")
        raise RuntimeError("Superior official agenda archive failed verified TLS") from exc
    return _parse_year_folder(folder_fetch.text, folder_fetch.url, cutoff)

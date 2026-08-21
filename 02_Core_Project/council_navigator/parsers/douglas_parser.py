from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from polite_http import make_session


logger = logging.getLogger(__name__)

CALENDAR_URL = "https://www.douglasaz.gov/AgendaCenter/City-Council-2"
ALLOWED_HOSTS = {"www.douglasaz.gov", "douglasaz.gov", "meetings.municode.com"}
SOURCE_HOSTS = {"www.douglasaz.gov", "douglasaz.gov"}
BLOCKED_STATUSES = {401, 403, 429}
MAX_RESPONSE_BYTES = 3_000_000
YEARS_TO_FETCH = 2
CANONICAL_FIELDS = (
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
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
YEAR_CHANGE_RE = re.compile(r"changeYear\((\d{4}),\s*(\d+)\b")


class AgendaRowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, object]] = []
        self._row: dict[str, object] | None = None
        self._current_link: dict[str, str] | None = None
        self._strong_text: list[str] | None = None
        self._td_classes = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name.lower(): value or "" for name, value in attrs}
        if tag == "tr" and "catAgendaRow" in attr_map.get("class", ""):
            self._row = {
                "row_id": attr_map.get("id", ""),
                "links": [],
                "strong_texts": [],
                "strong_labels": [],
            }
            self._td_classes = ""
            return

        if self._row is None:
            return

        if tag == "td":
            self._td_classes = attr_map.get("class", "")
        elif tag == "a":
            self._current_link = {
                "href": attr_map.get("href", ""),
                "text": "",
                "name": attr_map.get("name", ""),
                "aria_label": attr_map.get("aria-label", ""),
                "section": self._td_classes,
            }
            links = self._row["links"]
            assert isinstance(links, list)
            links.append(self._current_link)
        elif tag == "strong":
            self._strong_text = []
            label = attr_map.get("aria-label", "")
            if label:
                labels = self._row["strong_labels"]
                assert isinstance(labels, list)
                labels.append(label)

    def handle_data(self, data: str) -> None:
        if self._row is None:
            return
        if self._current_link is not None:
            self._current_link["text"] += data
        if self._strong_text is not None:
            self._strong_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._row is None:
            return
        if tag == "a":
            self._current_link = None
        elif tag == "strong" and self._strong_text is not None:
            strong_texts = self._row["strong_texts"]
            assert isinstance(strong_texts, list)
            strong_texts.append(_clean_text("".join(self._strong_text)))
            self._strong_text = None
        elif tag == "td":
            self._td_classes = ""
        elif tag == "tr":
            self.rows.append(self._row)
            self._row = None
            self._current_link = None
            self._strong_text = None
            self._td_classes = ""


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _fetch_text(session, url: str) -> tuple[int, str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _hostname(url) not in SOURCE_HOSTS:
        raise ValueError(f"Douglas source URL is not allowlisted: {url!r}")
    with session.get(
        url,
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=(10, 30),
        stream=True,
        allow_redirects=True,
    ) as response:
        final_host = _hostname(response.url)
        if final_host not in SOURCE_HOSTS:
            raise ValueError(
                f"Redirect to disallowed host: {final_host} (started from {url})"
            )
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")
        return (
            response.status_code,
            bytes(body).decode(response.encoding or "utf-8", errors="replace"),
            response.url,
        )


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _emit_url(href: str, base_url: str, field: str, row_id: str) -> str:
    if not href:
        return ""
    stripped = href.strip()
    lowered = stripped.lower()
    bad_prefixes = (
        "javascript:",
        "data:",
        "vbscript:",
        "file:",
        "mailto:",
        "ftp:",
        "gopher:",
    )
    if lowered.startswith(bad_prefixes) or lowered in {"#", ""}:
        logger.warning(
            "dropped %s URL for row %s: rejected non-http href %r",
            field,
            row_id,
            href,
        )
        return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        logger.warning(
            "dropped %s URL for row %s: disallowed scheme in %r",
            field,
            row_id,
            absolute,
        )
        return ""
    if host not in ALLOWED_HOSTS:
        logger.warning(
            "dropped %s URL for row %s: disallowed host %r in %r",
            field,
            row_id,
            host,
            absolute,
        )
        return ""
    return absolute


def _validate_civicplus_surface(html: str) -> None:
    required = (
        "AgendaCenterContent",
        "/AgendaCenter/ViewFile/Agenda/",
        "catAgendaRow",
        "changeYear(",
    )
    missing = [marker for marker in required if marker not in html]
    if missing:
        raise ValueError(f"Douglas page no longer matches CivicPlus Agenda Center: {missing}")
    if "CivicClerk" in html or "/api/v1/Meetings" in html:
        raise ValueError("Douglas page exposed CivicClerk markers; parser expects CivicPlus")


def _find_city_council_category_id(html: str) -> str:
    input_re = re.compile(
        r'<input\b(?=[^>]*\bname="chkCategoryID")(?=[^>]*\bvalue="(\d+)")[^>]*>',
        re.IGNORECASE,
    )
    for match in input_re.finditer(html):
        category_id = match.group(1)
        after = html[match.end() : match.end() + 350]
        if "City Council" in _clean_text(after):
            return category_id
    raise ValueError("Could not find CivicPlus City Council category id")


def _latest_years_for_category(html: str, category_id: str, limit: int = YEARS_TO_FETCH) -> list[int]:
    years = {
        int(year)
        for year, cat_id in YEAR_CHANGE_RE.findall(html)
        if cat_id == category_id
    }
    if not years:
        raise ValueError(f"No CivicPlus year controls found for category {category_id}")
    return sorted(years, reverse=True)[:limit]


def _parse_rows(html: str, base_url: str) -> list[dict[str, str]]:
    parser = AgendaRowParser()
    parser.feed(html)
    meetings: list[dict[str, str]] = []

    for raw_row in parser.rows:
        meeting = _build_meeting(raw_row, base_url)
        if meeting:
            meetings.append(meeting)

    return meetings


def _build_meeting(raw_row: dict[str, object], base_url: str) -> dict[str, str] | None:
    row_id = str(raw_row.get("row_id") or "")
    links = raw_row.get("links")
    strong_labels = raw_row.get("strong_labels")
    strong_texts = raw_row.get("strong_texts")
    if not isinstance(links, list):
        logger.warning("dropped row %s: parser did not capture links", row_id)
        return None

    agenda_link = _agenda_link(links)
    if not agenda_link:
        logger.warning("dropped row %s: no agenda link found", row_id)
        return None

    minutes_link = _first_link(links, "/AgendaCenter/ViewFile/Minutes/")
    packet_link = _packet_link(links)
    media_link = _media_link(links)

    meeting_id = _meeting_id_from_link(agenda_link)
    date_text = _date_text(strong_labels, strong_texts, row_id)
    meeting_date = _parse_meeting_date(date_text, row_id)
    title = _title_from_link(agenda_link)
    meeting_time = _meeting_time_from_title(title)
    agenda_url = _emit_url(agenda_link.get("href", ""), base_url, "agenda_url", row_id)
    minutes_url = (
        _emit_url(minutes_link.get("href", ""), base_url, "minutes_url", row_id)
        if minutes_link
        else ""
    )
    agenda_packet_url = (
        _emit_url(packet_link.get("href", ""), base_url, "agenda_packet_url", row_id)
        if packet_link
        else ""
    )
    video_url = (
        _emit_url(media_link.get("href", ""), base_url, "video_url", row_id)
        if media_link
        else ""
    )

    meeting = {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": "",
        "meeting_status": _meeting_status(title, agenda_url, minutes_url, agenda_packet_url),
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": video_url,
        "agenda_packet_url": agenda_packet_url,
        "ecomment_url": "",
        "meeting_id": meeting_id,
    }
    logger.info(
        "emitted row=%s id=%s date=%s time=%r title=%r agenda=%s minutes=%s packet=%s media=%s status=%s",
        row_id,
        meeting_id,
        meeting_date,
        meeting_time,
        title,
        bool(agenda_url),
        bool(minutes_url),
        bool(agenda_packet_url),
        bool(video_url),
        meeting["meeting_status"],
    )
    return meeting


def _agenda_link(links: list[object]) -> dict[str, str] | None:
    for link in _typed_links(links):
        href = link.get("href", "")
        text = _clean_text(link.get("text", "")).lower()
        if "/agendacenter/viewfile/agenda/" not in href.lower():
            continue
        if "packet=true" in href.lower() or text in {"html", "pdf", "packet", "previous versions"}:
            continue
        return link
    for link in _typed_links(links):
        href = link.get("href", "")
        if "/agendacenter/viewfile/agenda/" in href.lower() and "packet=true" not in href.lower():
            return link
    return None


def _packet_link(links: list[object]) -> dict[str, str] | None:
    for link in _typed_links(links):
        href = link.get("href", "")
        text = _clean_text(link.get("text", "")).lower()
        label = _clean_text(link.get("aria_label", "")).lower()
        if "/agendacenter/viewfile/agenda/" in href.lower() and (
            "packet=true" in href.lower() or text == "packet" or label.endswith(". packet")
        ):
            return link
    return None


def _media_link(links: list[object]) -> dict[str, str] | None:
    for link in _typed_links(links):
        href = link.get("href", "")
        label = _clean_text(
            f"{link.get('text', '')} {link.get('aria_label', '')} {href}"
        )
        if re.search(r"\b(video|recording|watch)\b", label, re.IGNORECASE) and href:
            return link
    return None


def _first_link(links: list[object], href_part: str) -> dict[str, str] | None:
    for link in _typed_links(links):
        href = link.get("href", "")
        if href_part.lower() in href.lower():
            return link
    return None


def _typed_links(links: list[object]) -> list[dict[str, str]]:
    typed: list[dict[str, str]] = []
    for link in links:
        if isinstance(link, dict):
            typed.append({str(key): str(value) for key, value in link.items()})
    return typed


def _meeting_id_from_link(link: dict[str, str]) -> str:
    if link.get("name", "").isdigit():
        return link["name"]
    match = re.search(r"-(\d+)(?:$|[?#])", link.get("href", ""))
    return match.group(1) if match else ""


def _date_text(strong_labels: object, strong_texts: object, row_id: str) -> str:
    if isinstance(strong_labels, list):
        for label in strong_labels:
            cleaned = _clean_text(str(label))
            if cleaned.lower().startswith("agenda for "):
                return cleaned[len("Agenda for ") :]
    if isinstance(strong_texts, list) and strong_texts:
        return _clean_text(str(strong_texts[0]))
    raise ValueError(f"Could not find meeting date evidence for row {row_id}")


def _parse_meeting_date(date_text: str, row_id: str) -> str:
    cleaned = _clean_text(date_text)
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Could not parse meeting date {cleaned!r} for row {row_id}")


def _title_from_link(link: dict[str, str]) -> str:
    title = _clean_text(link.get("text", ""))
    if title:
        return title
    label = _clean_text(link.get("aria_label", ""))
    if "," in label:
        middle = label.split(",", 1)[1].rsplit(".", 1)[0]
        return _clean_text(middle)
    return ""


def _meeting_time_from_title(title: str) -> str:
    match = re.search(
        r"(?:@|\bat\b)\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?(?=\s|$|[^\w.])",
        title[:300],
        re.IGNORECASE,
    )
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = "AM" if match.group(3).lower() == "a" else "PM"
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        logger.warning("dropped meeting_time from title %r: out-of-range clock value", title)
        return ""
    return f"{hour}:{minute:02d} {meridiem}"


def _meeting_status(
    title: str,
    agenda_url: str,
    minutes_url: str,
    agenda_packet_url: str,
) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _dedupe_sort(meetings: list[dict[str, str]]) -> list[dict[str, str]]:
    by_id: dict[str, dict[str, str]] = {}
    for meeting in meetings:
        key = meeting["meeting_id"] or f"{meeting['meeting_date']}|{meeting['meeting_title']}"
        by_id[key] = meeting
    return sorted(
        by_id.values(),
        key=lambda item: (item["meeting_date"], int(item["meeting_id"] or "0")),
    )


def _validate_schema(meetings: list[dict[str, str]]) -> None:
    expected = set(CANONICAL_FIELDS)
    for index, meeting in enumerate(meetings):
        if set(meeting) != expected:
            raise ValueError(f"Meeting {index} has invalid fields: {sorted(meeting)}")
        non_strings = [key for key, value in meeting.items() if not isinstance(value, str)]
        if non_strings:
            raise ValueError(f"Meeting {index} has non-string fields: {non_strings}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meeting["meeting_date"]):
            raise ValueError(f"Meeting {index} has non-ISO date: {meeting['meeting_date']!r}")
        for field in (
            "agenda_url",
            "minutes_url",
            "video_url",
            "agenda_packet_url",
            "ecomment_url",
        ):
            value = meeting[field]
            if value and not value.startswith(("http://", "https://")):
                raise ValueError(f"Meeting {index} has invalid {field}: {value!r}")


def scrape_calendar(
    url: str = CALENDAR_URL,
    *,
    today: date | None = None,
) -> list[dict[str, str]]:
    expected_path = "/agendacenter/city-council-2"
    if expected_path not in urlparse(url).path.lower():
        raise ValueError(f"Douglas parser requires the official City Council category URL: {url!r}")
    with make_session() as session:
        status, html, final_url = _fetch_text(session, url)
    if status in BLOCKED_STATUSES:
        logger.warning(
            "Douglas official City Council Agenda Center blocked the neutral paced request: status=%d url=%s",
            status,
            final_url,
        )
        logger.warning("health_empty_kind=source_blocked")
        return []
    if status != 200:
        raise RuntimeError(f"Douglas Agenda Center returned HTTP {status}: {final_url}")
    _validate_civicplus_surface(html)
    category_id = _find_city_council_category_id(html)
    if category_id != "2":
        raise ValueError(f"Douglas City Council category id drifted from witnessed id 2 to {category_id!r}")
    month_floor = (today or date.today()).replace(day=1)

    logger.info(
        "douglas CivicPlus Agenda Center surface verified; category=%s month_floor=%s",
        category_id,
        month_floor.isoformat(),
    )

    parsed_meetings = _parse_rows(html, url)
    if not parsed_meetings:
        raise ValueError("Douglas City Council category exposed no parseable rows or witnessed empty state")
    result = _dedupe_sort(
        [
            meeting
            for meeting in parsed_meetings
            if date.fromisoformat(meeting["meeting_date"]) >= month_floor
        ]
    )
    _validate_schema(result)

    if not result:
        logger.warning(
            "Douglas City Council category is accessible but has no rows from %s onward",
            month_floor.isoformat(),
        )
        logger.warning("health_empty_kind=confirmed_empty")
        return []

    missing_time = sum(1 for meeting in result if meeting["meeting_time"] == "")
    missing_location = sum(1 for meeting in result if meeting["meeting_location"] == "")
    missing_ecomment = sum(1 for meeting in result if meeting["ecomment_url"] == "")
    if missing_time:
        logger.warning(
            "field_absence meeting_time: %s/%s rows; CivicPlus row markup exposes amendment/posting timestamps, not meeting times",
            missing_time,
            len(result),
        )
    if missing_location:
        logger.warning(
            "field_absence meeting_location: %s/%s rows; no per-row location signal in Agenda Center table",
            missing_location,
            len(result),
        )
    if missing_ecomment:
        logger.warning(
            "field_absence ecomment_url: %s/%s rows; no per-row eComment link in Agenda Center table",
            missing_ecomment,
            len(result),
        )

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    result = scrape_calendar(CALENDAR_URL)
    print(len(result))
    print(json.dumps(result[:3], indent=2))

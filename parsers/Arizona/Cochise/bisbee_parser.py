from __future__ import annotations

import json
import logging
import re
import ssl
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPSHandler, Request, build_opener


logger = logging.getLogger(__name__)

CALENDAR_URL = "https://www.bisbeeaz.gov/agendacenter"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
ALLOWED_HOSTS = {"www.bisbeeaz.gov", "bisbeeaz.gov"}
MAX_RESPONSE_BYTES = 2_000_000
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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name.lower(): value or "" for name, value in attrs}
        if tag == "tr" and "catAgendaRow" in attr_map.get("class", ""):
            self._row = {
                "row_id": attr_map.get("id", ""),
                "links": [],
                "strong_texts": [],
                "strong_labels": [],
            }
            return

        if self._row is None:
            return

        if tag == "a":
            self._current_link = {
                "href": attr_map.get("href", ""),
                "text": "",
                "name": attr_map.get("name", ""),
                "aria_label": attr_map.get("aria-label", ""),
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
        elif tag == "tr":
            self.rows.append(self._row)
            self._row = None
            self._current_link = None
            self._strong_text = None


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _fetch_text(url: str, *, post_data: dict[str, str] | None = None) -> str:
    data = urlencode(post_data).encode("utf-8") if post_data else None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if post_data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    request = Request(url, data=data, headers=headers, method="POST" if data else "GET")

    try:
        with _http_opener().open(request, timeout=30) as response:
            final_host = _hostname(response.geturl())
            if final_host not in ALLOWED_HOSTS:
                raise ValueError(
                    f"Redirect to disallowed host: {final_host} (started from {url})"
                )
            body = bytearray()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")
            encoding = response.headers.get_content_charset() or "utf-8"
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc

    return bytes(body).decode(encoding, errors="replace")


def _http_opener():
    verify_paths = ssl.get_default_verify_paths()
    if verify_paths.cafile or not Path("/etc/ssl/cert.pem").exists():
        return build_opener()
    context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return build_opener(HTTPSHandler(context=context))


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
        "Agenda Center",
        "AgendaCenterContent",
        "/AgendaCenter/ViewFile/Agenda/",
        "catAgendaRow",
    )
    missing = [marker for marker in required if marker not in html]
    if missing:
        raise ValueError(f"Bisbee page no longer matches CivicPlus Agenda Center: {missing}")


def _find_city_council_category_id(html: str) -> str:
    input_re = re.compile(
        r'<input\b(?=[^>]*\bname="chkCategoryID")(?=[^>]*\bvalue="(\d+)")[^>]*>',
        re.IGNORECASE,
    )
    for match in input_re.finditer(html):
        category_id = match.group(1)
        after = html[match.end() : match.end() + 300]
        if "City Council" in _clean_text(after):
            return category_id
    raise ValueError("Could not find CivicPlus City Council category id")


def _latest_years_for_category(html: str, category_id: str, limit: int = 2) -> list[int]:
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
        return None

    agenda_link = _first_link(links, "/AgendaCenter/ViewFile/Agenda/")
    if not agenda_link:
        logger.warning("dropped row %s: no agenda link found", row_id)
        return None

    minutes_link = _first_link(links, "/AgendaCenter/ViewFile/Minutes/")
    meeting_id = _meeting_id_from_link(agenda_link)
    date_text = _date_text(strong_labels, strong_texts, row_id)
    meeting_date = _parse_meeting_date(date_text, row_id)
    title = _title_from_link(agenda_link)
    agenda_url = _emit_url(agenda_link.get("href", ""), base_url, "agenda_url", row_id)
    minutes_url = (
        _emit_url(minutes_link.get("href", ""), base_url, "minutes_url", row_id)
        if minutes_link
        else ""
    )

    meeting = {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": "",
        "meeting_location": "",
        "meeting_status": _meeting_status(title, agenda_url, minutes_url),
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
        "meeting_id": meeting_id,
    }
    logger.info(
        "emitted row=%s id=%s date=%s title=%r agenda=%s minutes=%s status=%s",
        row_id,
        meeting_id,
        meeting_date,
        title,
        bool(agenda_url),
        bool(minutes_url),
        meeting["meeting_status"],
    )
    return meeting


def _first_link(links: list[object], href_part: str) -> dict[str, str] | None:
    for link in links:
        if not isinstance(link, dict):
            continue
        href = str(link.get("href", ""))
        if href_part.lower() in href.lower():
            return {str(key): str(value) for key, value in link.items()}
    return None


def _meeting_id_from_link(link: dict[str, str]) -> str:
    if link.get("name", "").isdigit():
        return link["name"]
    match = re.search(r"-(\d+)(?:$|[?#])", link.get("href", ""))
    return match.group(1) if match else ""


def _date_text(
    strong_labels: object,
    strong_texts: object,
    row_id: str,
) -> str:
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


def _meeting_status(title: str, agenda_url: str, minutes_url: str) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url:
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
        reverse=True,
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


def scrape_calendar(url: str) -> list[dict[str, str]]:
    html = _fetch_text(url)
    _validate_civicplus_surface(html)
    category_id = _find_city_council_category_id(html)
    years = _latest_years_for_category(html, category_id)

    logger.info(
        "bisbee CivicPlus Agenda Center surface verified; category=%s years=%s",
        category_id,
        years,
    )

    meetings = _parse_rows(html, url)
    update_url = urljoin(url, "/AgendaCenter/UpdateCategoryList")
    for year in years:
        if str(year) in html and any(row["meeting_date"].startswith(str(year)) for row in meetings):
            continue
        year_html = _fetch_text(
            update_url,
            post_data={"year": str(year), "catID": category_id},
        )
        meetings.extend(_parse_rows(year_html, url))

    result = _dedupe_sort(meetings)
    _validate_schema(result)

    if not result:
        raise ValueError("Bisbee CivicPlus parser returned zero meetings")

    missing_time = sum(1 for meeting in result if meeting["meeting_time"] == "")
    missing_location = sum(1 for meeting in result if meeting["meeting_location"] == "")
    if missing_time:
        logger.warning(
            "field_absence meeting_time: %s/%s rows; CivicPlus row markup exposes posted time only",
            missing_time,
            len(result),
        )
    if missing_location:
        logger.warning(
            "field_absence meeting_location: %s/%s rows; no per-row location signal in Agenda Center table",
            missing_location,
            len(result),
        )

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    result = scrape_calendar(CALENDAR_URL)
    print(len(result))
    print(json.dumps(result[:3], indent=2))

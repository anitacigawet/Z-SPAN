
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.bensonaz.gov/AgendaCenter"
ALLOWED_HOSTS = {"bensonaz.gov", "www.bensonaz.gov"}
MAX_RESPONSE_BYTES = 4_000_000
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
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"([0-9]{1,2}),\s+([0-9]{4})",
    re.IGNORECASE,
)
NON_MEETING_DOCUMENT_RE = re.compile(
    r"\bcouncil packet\b|\baction minutes\b|\bpublic hearing notice\b",
    re.IGNORECASE,
)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Read Benson's current City Council rows from one AgendaCenter page."""
    target = url or DEFAULT_URL
    if url and urlparse(url).path.rstrip("/").casefold() != "/agendacenter":
        if _host(url) not in ALLOWED_HOSTS:
            raise ValueError(f"Benson input URL host is not allowlisted: {_host(url)}")
        logger.warning(
            "registry_url_stale supplied_url=%s replacement_url=%s reason=legacy_page_returns_404",
            url,
            DEFAULT_URL,
        )
        target = DEFAULT_URL
    with make_session() as session:
        html = _fetch_text_bounded(session, target)
    soup = BeautifulSoup(html, "html.parser")

    heading = next(
        (candidate for candidate in soup.find_all("h2") if _clean_text(candidate) == "City Council"),
        None,
    )
    panel_id = str(heading.get("aria-controls", "")) if heading else ""
    panel = soup.find(id=panel_id) if panel_id else None
    if not heading or not panel or not panel.find("tr", class_="catAgendaRow"):
        logger.warning(
            "vendor_fingerprint_failed expected=City_Council_heading_plus_catAgendaRow panel_id=%r",
            panel_id,
        )
        raise ValueError("Benson CivicPlus City Council AgendaCenter surface drifted")
    logger.info(
        "vendor_fingerprint witness=City_Council_heading_plus_catAgendaRow panel_id=%s",
        panel_id,
    )
    logger.warning(
        "field_absence fields=meeting_time,meeting_location,ecomment_url "
        "reason=agenda_center_rows_expose_no_per_row_signal"
    )

    current_floor = date.today().replace(day=1).isoformat()
    meetings: list[dict[str, str]] = []
    rows_seen = rows_dropped = 0
    untrusted_rows = 0
    for row in panel.find_all("tr", class_="catAgendaRow"):
        rows_seen += 1
        primary = row.select_one("td:first-of-type p a[href*='/AgendaCenter/ViewFile/Agenda/']")
        row_text = _clean_text(row)
        if primary is None:
            rows_dropped += 1
            untrusted_rows += 1
            logger.warning("drop_row reason=primary_agenda_link_missing row=%r", row_text[:240])
            continue
        raw_title = _clean_text(primary)
        meeting_date = _extract_date(raw_title) or _extract_date(row_text)
        if not meeting_date:
            rows_dropped += 1
            untrusted_rows += 1
            logger.warning("drop_row reason=meeting_date_unparseable title=%r", raw_title)
            continue
        if meeting_date < current_floor:
            rows_dropped += 1
            logger.info(
                "drop_row reason=before_current_month title=%r date=%s current_floor=%s",
                raw_title,
                meeting_date,
                current_floor,
            )
            continue
        if NON_MEETING_DOCUMENT_RE.search(raw_title):
            rows_dropped += 1
            logger.warning(
                "drop_row reason=document_companion_not_meeting title=%r date=%s",
                raw_title,
                meeting_date,
            )
            continue

        agenda_url = _emit_url(primary.get("href", ""), target, "agenda_url", raw_title)
        if not agenda_url:
            rows_dropped += 1
            untrusted_rows += 1
            logger.warning("drop_row reason=agenda_url_rejected title=%r", raw_title)
            continue
        minutes_anchor = row.select_one("td.minutes a[href]")
        minutes_url = (
            _emit_url(minutes_anchor.get("href", ""), target, "minutes_url", raw_title)
            if minutes_anchor
            else ""
        )
        media_anchor = row.select_one("td.media a[href]")
        if media_anchor:
            logger.warning(
                "drop_media reason=canonical_schema_has_no_audio_field title=%r href=%r",
                raw_title,
                media_anchor.get("href", ""),
            )

        meeting_id = str(primary.get("name", "") or "")
        if not meeting_id.isdigit():
            id_match = re.search(r"-([0-9]+)(?:[/?]|$)", urlparse(agenda_url).path)
            meeting_id = id_match.group(1) if id_match else ""
        if not meeting_id:
            logger.warning("meeting_id_absent title=%r agenda_url=%s", raw_title, agenda_url)

        title = _title_from_label(raw_title)
        status = (
            "Cancelled"
            if CANCELLED_RE.search(title)
            else "Minutes Available"
            if minutes_url
            else "Agenda Available"
        )
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": "",
            "meeting_location": "",
            "meeting_status": status,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": "",
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})

    _assert_schema(meetings)
    logger.info(
        "scrape_summary rows_seen=%d rows_accepted=%d rows_dropped=%d untrusted_rows=%d current_floor=%s",
        rows_seen,
        len(meetings),
        rows_dropped,
        untrusted_rows,
        current_floor,
    )
    if not meetings:
        if untrusted_rows:
            raise RuntimeError(
                "Benson City Council panel contained malformed rows, so an official zero cannot be witnessed"
            )
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Benson witnessed zero current-month-forward City Council rows in the official panel"
        )
    return meetings


def _fetch_text_bounded(session: requests.Session, url: str) -> str:
    try:
        response_context = session.get(url, timeout=30, stream=True, allow_redirects=True)
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Benson official AgendaCenter source failed verified TLS")
        raise
    with response_context as response:
        if getattr(response, "status_code", None) in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        final_host = _host(response.url)
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Benson redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Benson response exceeded {MAX_RESPONSE_BYTES} bytes")
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _emit_url(href: str, base_url: str, field: str, row_label: str) -> str:
    absolute = urljoin(base_url, str(href or "").strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or _host(absolute) not in ALLOWED_HOSTS:
        logger.warning(
            "drop_url field=%s row=%r href=%r reason=scheme_or_host_not_allowlisted",
            field,
            row_label,
            href,
        )
        return ""
    return absolute


def _extract_date(text: str) -> str:
    match = DATE_RE.search(text[:500])
    if not match:
        logger.warning("date_extraction_empty reason=no_supported_date text=%r", text[:240])
        return ""
    try:
        return datetime.strptime(" ".join(match.groups()), "%B %d %Y").date().isoformat()
    except ValueError as exc:
        logger.warning(
            "date_extraction_empty reason=invalid_calendar_date raw=%r error=%s",
            match.group(0),
            exc,
        )
        return ""


def _title_from_label(label: str) -> str:
    without_date = DATE_RE.sub("", label, count=1)
    descriptor = re.sub(r"\s*-?\s*Agenda\s*$", "", without_date, flags=re.IGNORECASE)
    descriptor = descriptor.strip(" -\u2013\u2014") or "Meeting"
    return f"City Council {descriptor}"


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != CANONICAL_FIELDS:
            raise ValueError(f"Benson row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Benson row {index} contains a non-string value")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_URL), indent=2))

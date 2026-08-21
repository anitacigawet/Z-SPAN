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

DEFAULT_URL = "https://townofhaydenaz.gov/meetings/"
ALLOWED_HOSTS = {"townofhaydenaz.gov", "www.townofhaydenaz.gov"}
MAX_RESPONSE_BYTES = 2_000_000
CANONICAL_FIELDS = (
    "meeting_title", "meeting_date", "meeting_time", "meeting_location",
    "meeting_status", "agenda_url", "minutes_url", "video_url",
    "agenda_packet_url", "ecomment_url", "meeting_id",
)
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"([0-9]{1,2})\s*,?\s*([0-9]{4})",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
NON_COUNCIL_RE = re.compile(
    r"\b(?:public\s+hearing|committee|commission|board|authority)\b",
    re.IGNORECASE,
)
COUNCIL_RE = re.compile(r"\b(?:town|city)?\s*council\b", re.IGNORECASE)
DEFAULT_COUNCIL_RE = re.compile(
    r"^(?:(?:regular|special)(?:\s+council)?(?:\s+meeting)?|"
    r"(?:budget\s+)?work\s*session)$",
    re.IGNORECASE,
)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Return current-month-forward Hayden Town Council meetings."""
    target = _validated_source_url(url or DEFAULT_URL)
    with make_session() as session:
        html = _fetch_text_bounded(session, target)
    soup = BeautifulSoup(html, "html.parser")
    page_title = _clean_text(soup.title)
    paragraphs = soup.find_all("p")
    dated_indices = [
        index for index, paragraph in enumerate(paragraphs)
        if DATE_RE.search(_clean_text(paragraph)[:500])
    ]
    if "Meetings" not in page_title or "Town of Hayden" not in page_title or not dated_indices:
        logger.warning(
            "vendor_fingerprint_failed expected=Town_of_Hayden_Meetings_plus_dated_paragraphs "
            "title=%r dated_paragraphs=%d",
            page_title, len(dated_indices),
        )
        raise RuntimeError("Hayden meetings-page fingerprint drifted")
    logger.info(
        "vendor_fingerprint witness=Town_of_Hayden_Meetings_plus_dated_paragraphs count=%d",
        len(dated_indices),
    )
    logger.warning(
        "field_absence fields=meeting_time,meeting_location,video_url,ecomment_url "
        "reason=meeting_list_exposes_no_same_row_signal"
    )

    floor = date.today().replace(day=1).isoformat()
    meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    rows_seen = rows_dropped = historical = 0
    for position, paragraph_index in enumerate(dated_indices):
        end = dated_indices[position + 1] if position + 1 < len(dated_indices) else len(paragraphs)
        paragraph = paragraphs[paragraph_index]
        label = _clean_text(paragraph)
        match = DATE_RE.search(label[:500])
        if match is None:
            continue
        rows_seen += 1
        meeting_date = _date_from_match(match, label)
        if not meeting_date:
            rows_dropped += 1
            continue
        if meeting_date < floor:
            historical += 1
            continue

        descriptor = label[match.end():]
        descriptor = re.sub(r"(?:Amended\s+)?Notice\s*&\s*Agenda", " ", descriptor, flags=re.IGNORECASE)
        descriptor = re.sub(r"\bMinutes?\b", " ", descriptor, flags=re.IGNORECASE)
        descriptor = " ".join(descriptor.strip(" -:\u2013\u2014").split())
        scope_descriptor = CANCELLED_RE.sub(" ", descriptor)
        scope_descriptor = re.sub(r"\bdue\s+to\s+lack\s+of\s+quorum\b", " ", scope_descriptor, flags=re.IGNORECASE)
        scope_descriptor = " ".join(scope_descriptor.strip(" -~").split())
        if NON_COUNCIL_RE.search(scope_descriptor):
            rows_dropped += 1
            logger.warning(
                "drop_row reason=explicit_non_council_body date=%s title=%r",
                meeting_date, descriptor,
            )
            continue
        if COUNCIL_RE.search(scope_descriptor):
            logger.info(
                "governing_body_witness=explicit_council date=%s title=%r",
                meeting_date, descriptor,
            )
        elif DEFAULT_COUNCIL_RE.fullmatch(scope_descriptor):
            logger.info(
                "governing_body_witness=official_default_session_vocabulary "
                "date=%s title=%r subordinate_body_signal=absent",
                meeting_date, descriptor,
            )
        else:
            raise RuntimeError(
                "Hayden current meeting label is governing-body ambiguous: "
                f"{label!r}"
            )

        segment = paragraphs[paragraph_index:end]
        anchors = [anchor for item in segment for anchor in item.find_all("a", href=True)]
        agenda_url = minutes_url = packet_url = ""
        for anchor in anchors:
            link_label = _clean_text(anchor)
            if re.search(r"(?:Amended\s+)?Notice\s*&\s*Agenda", link_label, re.IGNORECASE):
                agenda_url = agenda_url or _emit_url(anchor.get("href", ""), target, "agenda_url", label)
            elif re.search(r"\bMinutes?\b", link_label, re.IGNORECASE):
                minutes_url = minutes_url or _emit_url(anchor.get("href", ""), target, "minutes_url", label)
            elif link_label:
                logger.warning(
                    "drop_link reason=unclassified_document_label row=%r label=%r href=%r",
                    label, link_label, anchor.get("href"),
                )
        if not agenda_url:
            logger.info("field_absent field=agenda_url row=%r reason=no_same_entry_agenda_link", label)
        if not minutes_url:
            logger.info("field_absent field=minutes_url row=%r reason=no_same_entry_minutes_link", label)

        title = descriptor or "Town Council Meeting"
        key = (meeting_date, title.casefold())
        if key in seen:
            rows_dropped += 1
            logger.warning("drop_row reason=duplicate date=%s title=%r", meeting_date, title)
            continue
        seen.add(key)
        status = _status(title, agenda_url, packet_url, minutes_url)
        meeting_id = _document_id(agenda_url or minutes_url)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": "",
            "meeting_location": "",
            "meeting_status": status,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": "",
            "agenda_packet_url": packet_url,
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})

    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    logger.info(
        "scrape_summary rows_seen=%d rows_accepted=%d rows_dropped=%d historical_ignored=%d current_floor=%s",
        rows_seen, len(meetings), rows_dropped, historical, floor,
    )
    return meetings


def _validated_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in ALLOWED_HOSTS:
        raise ValueError("Hayden source URL must use HTTPS on the official town host")
    return url


def _fetch_text_bounded(session: requests.Session, url: str) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        if _host(response.url) not in ALLOWED_HOSTS:
            raise ValueError(f"Hayden redirect reached disallowed host: {_host(response.url)}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Hayden response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _date_from_match(match: re.Match[str], label: str) -> str:
    try:
        return datetime.strptime(" ".join(match.groups()), "%B %d %Y").date().isoformat()
    except ValueError:
        logger.warning("meeting_date_unparseable label=%r raw=%r", label, match.group(0))
        return ""


def _emit_url(href: str, base_url: str, field: str, row_label: str) -> str:
    absolute = urljoin(base_url, str(href or "").strip())
    if urlparse(absolute).scheme not in {"http", "https"} or _host(absolute) not in ALLOWED_HOSTS:
        logger.warning(
            "drop_url field=%s row=%r href=%r reason=scheme_or_host_not_allowlisted",
            field, row_label, href,
        )
        return ""
    return absolute


def _document_id(url: str) -> str:
    if not url:
        return ""
    stem = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    match = re.search(r"(?:^|[-_])([0-9]{3,})(?:[-_.]|$)", stem)
    return match.group(1) if match else ""


def _status(title: str, agenda: str, packet: str, minutes: str) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes:
        return "Minutes Available"
    if agenda or packet:
        return "Agenda Available"
    return "Scheduled"


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _assert_schema(rows: list[dict[str, str]]) -> None:
    for index, row in enumerate(rows):
        if tuple(row) != CANONICAL_FIELDS or any(not isinstance(value, str) for value in row.values()):
            raise ValueError(f"Hayden row {index} violates canonical schema")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_URL), indent=2))

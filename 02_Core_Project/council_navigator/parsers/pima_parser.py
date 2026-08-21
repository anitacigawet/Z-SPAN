"""Current-month-forward Town Council rows from Pima's Revize Document Center."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from polite_http import make_session
from requests.utils import requote_uri


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.pimatown.az.gov/town_council/agendas_and_minutes.php"
PIMA_HOSTS = {"pimatown.az.gov", "www.pimatown.az.gov"}
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
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
BLOCKING_HTTP_STATUSES = {401, 403, 407, 423, 429, 451}
BLOCK_PAGE_RE = re.compile(r"\b(?:access denied|attention required|just a moment|captcha)\b", re.IGNORECASE)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
DISPLAY_DATE_RE = re.compile(r"([A-Z][a-z]+\s+\d{1,2},\s*\d{4})")
MONTH_DAY_RE = re.compile(r"([A-Z][a-z]+\s+\d{1,2})(?!\s*,?\s*\d)")
DOCUMENT_SUFFIX_RE = re.compile(
    r"\b(?:agenda(?:\s+packet)?|packet|minutes?)\b(?:\s*\.pdf)?",
    re.IGNORECASE,
)
NOTICE_RE = re.compile(r"\bnotice\b", re.IGNORECASE)
MAX_RESPONSE_BYTES = 2_000_000
REQUEST_TIMEOUT = 45


class SourceBlockedError(RuntimeError):
    """Raised only when a recognizable upstream block page was witnessed."""


def scrape_calendar(url: str) -> list[dict[str, str]]:
    """Return Pima Town Council documents grouped into current-forward meetings."""
    source_url = _validated_source_url(url or DEFAULT_URL)
    current_floor = date.today().replace(day=1)
    session = make_session()
    counters: Counter[str] = Counter()

    logger.warning(
        "field_absence field=meeting_time reason=revize_document_center_exposes_no_per_row_meeting_time_signal"
    )
    logger.warning(
        "field_absence field=meeting_location reason=revize_document_center_exposes_no_per_row_location_signal"
    )
    logger.warning(
        "field_absence field=video_url reason=revize_document_center_exposes_no_video_signal"
    )
    logger.warning(
        "field_absence field=ecomment_url reason=revize_document_center_exposes_no_ecomment_signal"
    )
    logger.warning(
        "field_absence field=meeting_id reason=revize_document_center_exposes_no_vendor_meeting_identifier"
    )

    try:
        html = _fetch_html_bounded(session, source_url)
        meetings = _parse_document_center(
            html,
            source_url,
            current_floor=current_floor,
            counters=counters,
        )
    except requests.HTTPError as exc:
        if not _is_witnessed_http_blocker(exc):
            raise
        _log_source_blocked(exc=exc)
        return []
    except SourceBlockedError as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "source_blocked phase=html_fingerprint failure_shape=honest-empty "
            "missing_data_scope=all_current_month_forward_meetings error=%r",
            exc,
        )
        return []

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.info("scrape_summary counters=%s", dict(sorted(counters.items())))
        return []

    meetings.sort(key=lambda item: (item["meeting_date"], item["meeting_title"].casefold()))
    _assert_schema(meetings)
    logger.info("scrape_summary counters=%s", dict(sorted(counters.items())))
    return meetings


def _parse_document_center(
    html: str,
    source_url: str,
    *,
    current_floor: date,
    counters: Counter[str],
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    _validate_document_center(soup, source_url)
    current_year = str(current_floor.year)
    grouped: dict[tuple[str, str], dict[str, str]] = {}
    current_year_sections_seen: set[str] = set()
    forward_years_seen: set[str] = set()

    for category in soup.select("div.outer-cat.cat"):
        category_name = next(iter(category.stripped_strings), "").strip()
        if category_name not in {"Agendas", "Minutes"}:
            continue
        for year_section in category.select("div.inner-cat.cat"):
            year_label = next(iter(year_section.stripped_strings), "").strip()
            if not re.fullmatch(r"\d{4}", year_label) or int(year_label) < current_floor.year:
                continue
            forward_years_seen.add(year_label)
            if year_label == current_year:
                current_year_sections_seen.add(category_name)
            for item in year_section.select("ul.file-group > li"):
                anchor = item.find("a", href=True)
                if anchor is None:
                    continue
                label = _clean_text(anchor.get_text(" ", strip=True))
                href = str(anchor.get("href", "") or "")
                if NOTICE_RE.search(label):
                    counters["notice_documents_dropped"] += 1
                    logger.info(
                        "drop_notice_document category=%s label=%r href=%r",
                        category_name,
                        label,
                        href,
                    )
                    continue

                matched_date, meeting_day = _meeting_day_from_label(
                    label,
                    witnessed_year=year_label,
                    category_name=category_name,
                )
                if meeting_day < current_floor:
                    counters["documents_dropped_before_current_floor"] += 1
                    continue

                field = _document_field(category_name, label, href)
                document_url = _emit_url(
                    href,
                    source_url,
                    field=field,
                    row_label=label,
                )
                if not document_url:
                    raise ValueError(
                        f"Pima current document has no safe emitted URL: category={category_name!r}, label={label!r}"
                    )
                title = _meeting_title(label, matched_date)
                key = (meeting_day.isoformat(), title.casefold())
                if key not in grouped:
                    grouped[key] = {
                        "meeting_title": title,
                        "meeting_date": meeting_day.isoformat(),
                        "meeting_time": "",
                        "meeting_location": "",
                        "meeting_status": "Scheduled",
                        "agenda_url": "",
                        "minutes_url": "",
                        "video_url": "",
                        "agenda_packet_url": "",
                        "ecomment_url": "",
                        "meeting_id": "",
                    }
                if grouped[key][field] and grouped[key][field] != document_url:
                    raise ValueError(
                        f"Pima meeting exposes multiple {field} values: key={key!r}, "
                        f"first={grouped[key][field]!r}, second={document_url!r}"
                    )
                grouped[key][field] = document_url
                counters[f"{field}_documents_seen"] += 1

    if current_year_sections_seen != {"Agendas", "Minutes"}:
        raise ValueError(
            f"Pima Document Center did not expose both {current_year} sections: {sorted(current_year_sections_seen)}"
        )

    meetings: list[dict[str, str]] = []
    for meeting in grouped.values():
        meeting["meeting_status"] = _derive_status(
            meeting["meeting_title"],
            meeting["agenda_url"],
            meeting["minutes_url"],
            meeting["agenda_packet_url"],
        )
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})
        counters["rows_accepted"] += 1
    logger.info(
        "vendor_fingerprint witness=revize_document_center_agendas_minutes_current_year_sections "
        "source=%s current_year=%s forward_years=%r current_rows=%d",
        source_url,
        current_year,
        sorted(forward_years_seen),
        len(meetings),
    )
    return meetings


def _document_field(category_name: str, label: str, href: str) -> str:
    if category_name == "Minutes":
        return "minutes_url"
    if "packet" in f"{label} {urlparse(href).path}".casefold():
        return "agenda_packet_url"
    return "agenda_url"


def _meeting_day_from_label(
    label: str,
    *,
    witnessed_year: str,
    category_name: str,
) -> tuple[str, date]:
    full_match = DISPLAY_DATE_RE.search(label)
    if full_match:
        matched_date = full_match.group(1)
        meeting_day = datetime.strptime(
            re.sub(r"\s+", " ", matched_date),
            "%B %d, %Y",
        ).date()
        if str(meeting_day.year) != witnessed_year:
            raise ValueError(
                f"Pima {category_name} document date conflicts with its {witnessed_year} section: {label!r}"
            )
        return matched_date, meeting_day

    month_day_match = MONTH_DAY_RE.search(label)
    if not month_day_match:
        raise ValueError(
            f"Pima {witnessed_year} {category_name} document has no meeting date in its label: {label!r}"
        )
    matched_date = month_day_match.group(1)
    logger.info(
        "meeting_year_from_witnessed_section category=%s label=%r month_day=%r year=%s",
        category_name,
        label,
        matched_date,
        witnessed_year,
    )
    meeting_day = datetime.strptime(
        f"{matched_date}, {witnessed_year}",
        "%B %d, %Y",
    ).date()
    return matched_date, meeting_day


def _meeting_title(label: str, matched_date: str) -> str:
    title = label.replace(matched_date, " ")
    title = title.replace("_", " ")
    title = DOCUMENT_SUFFIX_RE.sub(" ", title)
    title = re.sub(r"\s+", " ", title).strip(" -_.")
    return title or "Town Council Meeting"


def _validate_document_center(soup: BeautifulSoup, source_url: str) -> None:
    title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    h1 = soup.find("h1")
    h1_text = _clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    document_center = soup.select_one("article#document-center, article.document-center")
    category_labels = {
        next(iter(category.stripped_strings), "").strip()
        for category in soup.select("div.outer-cat.cat")
    }
    if (
        not title.startswith("Agendas and Minutes")
        or h1_text != "Agendas and Minutes"
        or document_center is None
        or not {"Agendas", "Minutes"}.issubset(category_labels)
    ):
        page_text = _clean_text(soup.get_text(" ", strip=True))[:1000]
        if BLOCK_PAGE_RE.search(f"{title} {page_text}"):
            raise SourceBlockedError(f"recognized block page at {source_url}: title={title!r}")
        raise ValueError(
            "Pima Document Center fingerprint drift: "
            f"title={title!r}, h1={h1_text!r}, document_center={document_center is not None}, "
            f"categories={sorted(category_labels)}"
        )


def _derive_status(title: str, agenda_url: str, minutes_url: str, agenda_packet_url: str) -> str:
    if CANCELLED_RE.search(title[:500]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _emit_url(href: str, base_url: str, *, field: str, row_label: str) -> str:
    value = href.strip()
    if not value:
        return ""
    if value.lower().startswith(BAD_SCHEMES) or value == "#":
        logger.warning("drop_url_bad_scheme field=%s row=%r href=%r", field, row_label, href)
        return ""
    absolute = requote_uri(urljoin(base_url, value))
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        logger.warning("drop_url_non_https field=%s row=%r href=%r absolute=%r", field, row_label, href, absolute)
        return ""
    if not _host_allowed(host, PIMA_HOSTS):
        logger.warning(
            "drop_url_disallowed_host field=%s row=%r href=%r host=%r allowed=%r",
            field,
            row_label,
            href,
            host,
            sorted(PIMA_HOSTS),
        )
        return ""
    return absolute


def _fetch_html_bounded(session: requests.Session, url: str) -> str:
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = (urlparse(response.url).hostname or "").lower()
        if not _host_allowed(final_host, PIMA_HOSTS):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _validated_source_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not _host_allowed(host, PIMA_HOSTS):
        raise ValueError(f"Pima source URL must be HTTPS on pimatown.az.gov: {url!r}")
    return url


def _host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


def _is_witnessed_http_blocker(exc: requests.HTTPError) -> bool:
    return exc.response is not None and exc.response.status_code in BLOCKING_HTTP_STATUSES


def _log_source_blocked(*, exc: requests.HTTPError) -> None:
    response = exc.response
    logger.warning("health_empty_kind=source_blocked")
    logger.warning(
        "source_blocked phase=http_fetch status=%s final_url=%s failure_shape=honest-empty "
        "missing_data_scope=all_current_month_forward_meetings",
        response.status_code if response is not None else 0,
        response.url if response is not None else "",
    )


def _clean_text(value: str) -> str:
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != CANONICAL_FIELDS:
            raise ValueError(f"Row {index} schema mismatch: {tuple(meeting)}")
        for field, value in meeting.items():
            if not isinstance(value, str):
                raise ValueError(f"Row {index} field {field} is not str: {type(value).__name__}")
        for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
            value = meeting[field]
            if value and not value.startswith("https://"):
                raise ValueError(f"Row {index} field {field} has invalid URL: {value}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    result = scrape_calendar(DEFAULT_URL)
    print(json.dumps({"count": len(result), "samples": result[:5]}, indent=2))

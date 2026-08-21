from __future__ import annotations

import html
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from polite_http import make_session

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://www.navajonationcouncil.org/wp-json/tribe/events/v1/events?per_page=50"
EVENTS_API_PATH = "/wp-json/tribe/events/v1/events"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_PAGES = 4
ALLOWED_SOURCE_HOSTS = {
    "navajonationcouncil.org",
    "www.navajonationcouncil.org",
}
EXPECTED_TOP_LEVEL_KEYS = {"events", "rest_url", "total", "total_pages"}
EXPECTED_EVENT_KEYS = {
    "id",
    "title",
    "url",
    "start_date",
    "end_date",
    "start_date_details",
    "end_date_details",
    "venue",
    "status",
}
RECOGNIZED_VENDOR_STATUSES = {"publish"}
BAD_URL_SCHEMES = (
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "mailto:",
    "ftp:",
    "gopher:",
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
FULL_COUNCIL_RE = re.compile(
    r"\bNavajo Nation Council\b.{0,80}\bSession\b",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")

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


class ArchitecturalBlocker(RuntimeError):
    """Expected external blocker: network, redirect, JSON, or vendor shape."""


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = TAG_RE.sub(" ", text)
    return " ".join(text.split())


def _registered_domain(url_or_host: str) -> str:
    parsed = urlparse(url_or_host)
    host = parsed.hostname or url_or_host.split(":")[0]
    host = host.strip().lower().rstrip(".")
    if not host:
        return "navajonationcouncil.org"
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _host_allowed(host: str, registered_domain: str) -> bool:
    clean_host = host.lower().split(":")[0].rstrip(".")
    return clean_host == registered_domain or clean_host.endswith("." + registered_domain)


def _is_events_api_url(url: str) -> bool:
    return urlparse(url).path.rstrip("/") == EVENTS_API_PATH


def _replace_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        query[key] = [value]
    encoded = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, encoded, parsed.fragment))


def _ensure_per_page(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "per_page" in query:
        return url
    return _replace_query_params(url, {"per_page": "50"})


def _build_api_url(calendar_url: str | None) -> tuple[str, str]:
    source_url = (calendar_url or DEFAULT_API_URL).strip()
    parsed = urlparse(source_url)

    source_host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or source_host not in ALLOWED_SOURCE_HOSTS:
        raise ValueError(f"Window Rock source URL is not an approved HTTPS host: {source_url!r}")

    if _is_events_api_url(source_url):
        api_url = _ensure_per_page(source_url)
        logger.warning(
            "window_rock_api_route=direct_api input_url=%s api_url=%s checked_path=%s",
            source_url,
            api_url,
            EVENTS_API_PATH,
        )
        return api_url, _registered_domain(source_url)

    if parsed.scheme and parsed.netloc:
        api_url = urlunparse((parsed.scheme, parsed.netloc, EVENTS_API_PATH, "", "per_page=50", ""))
        logger.warning(
            "window_rock_api_route=derived_from_calendar_page input_url=%s api_url=%s checked_path=%s",
            source_url,
            api_url,
            EVENTS_API_PATH,
        )
        return api_url, _registered_domain(source_url)

    raise ValueError(f"Unable to derive Window Rock events API from {source_url!r}")


def _page_url(api_url: str, page: int) -> str:
    return _replace_query_params(api_url, {"page": str(page)})


def _fetch_text_bounded(session: object, url: str, registered_domain: str) -> str:
    try:
        response_context = session.get(
            url,
            timeout=15,
            stream=True,
            allow_redirects=True,
            headers={"Accept": "application/json"},
        )
    except Exception as exc:  # requests exception classes are not available without requests installed.
        raise ArchitecturalBlocker(f"request_failed url={url} error={exc}") from exc

    with response_context as response:
        final_host = urlparse(response.url).hostname or ""
        if final_host.lower() not in ALLOWED_SOURCE_HOSTS:
            raise ArchitecturalBlocker(
                "redirect_disallowed "
                f"started_url={url} final_url={response.url} final_host={final_host} "
                f"allowed_registered_domain={registered_domain}"
            )

        try:
            response.raise_for_status()
        except Exception as exc:
            raise ArchitecturalBlocker(f"http_status_failed url={url} error={exc}") from exc

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ArchitecturalBlocker(
                    f"response_too_large url={url} bytes_seen={size} max_bytes={MAX_RESPONSE_BYTES}"
                )
            chunks.append(chunk)

        encoding = response.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")


def _fetch_json_page(session: object, url: str, registered_domain: str) -> dict:
    body = _fetch_text_bounded(session, url, registered_domain)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        snippet = body[:120].replace("\n", " ")
        raise ArchitecturalBlocker(
            f"json_decode_failed url={url} error={exc} body_prefix={snippet!r}"
        ) from exc
    if not isinstance(data, dict):
        raise ArchitecturalBlocker(f"fingerprint_mismatch url={url} expected=top_level_object seen={type(data).__name__}")
    return data


def _validate_fingerprint(data: dict, url: str) -> None:
    missing_top = sorted(EXPECTED_TOP_LEVEL_KEYS - set(data))
    wrong_types: list[str] = []
    if "events" in data and not isinstance(data["events"], list):
        wrong_types.append(f"events:{type(data['events']).__name__}")
    if "total" in data and not isinstance(data["total"], int):
        wrong_types.append(f"total:{type(data['total']).__name__}")
    if "total_pages" in data and not isinstance(data["total_pages"], int):
        wrong_types.append(f"total_pages:{type(data['total_pages']).__name__}")

    if missing_top or wrong_types:
        raise ArchitecturalBlocker(
            "fingerprint_mismatch "
            f"url={url} expected_top_keys={sorted(EXPECTED_TOP_LEVEL_KEYS)} "
            f"missing_top_keys={missing_top} wrong_types={wrong_types} seen_top_keys={sorted(data.keys())}"
        )

    for index, event in enumerate(data["events"], start=1):
        if not isinstance(event, dict):
            raise ArchitecturalBlocker(
                f"fingerprint_mismatch url={url} event_index={index} expected=event_object seen={type(event).__name__}"
            )
        missing_event = sorted(EXPECTED_EVENT_KEYS - set(event))
        if missing_event:
            raise ArchitecturalBlocker(
                "fingerprint_mismatch "
                f"url={url} event_index={index} expected_event_keys={sorted(EXPECTED_EVENT_KEYS)} "
                f"missing_event_keys={missing_event} seen_event_keys={sorted(event.keys())}"
            )
        if not isinstance(event.get("venue"), list):
            raise ArchitecturalBlocker(
                f"fingerprint_mismatch url={url} event_index={index} expected=venue_list "
                f"seen={type(event.get('venue')).__name__}"
            )

    logger.info(
        "window_rock_vendor_fingerprint=matched url=%s checked_top_keys=%s checked_event_keys=%s events_seen=%d",
        url,
        sorted(EXPECTED_TOP_LEVEL_KEYS),
        sorted(EXPECTED_EVENT_KEYS),
        len(data["events"]),
    )


def _emit_url(raw_url: object, base_url: str, registered_domain: str, field: str, event_id: str) -> str:
    if not raw_url:
        logger.info("window_rock_url_absent field=%s event_id=%s reason=no_source_value", field, event_id)
        return ""

    href = str(raw_url).strip()
    low = href.lower().lstrip()
    for bad_scheme in BAD_URL_SCHEMES:
        if low.startswith(bad_scheme):
            logger.warning(
                "window_rock_url_rejected field=%s event_id=%s rejected_url=%r reason=bad_scheme",
                field,
                event_id,
                href,
            )
            return ""

    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme != "https":
        logger.warning(
            "window_rock_url_rejected field=%s event_id=%s rejected_url=%r absolute_url=%r reason=bad_scheme_after_join",
            field,
            event_id,
            href,
            absolute,
        )
        return ""

    emit_host = parsed.hostname or ""
    if not _host_allowed(emit_host, registered_domain):
        logger.warning(
            "window_rock_url_rejected field=%s event_id=%s rejected_url=%r absolute_url=%r "
            "reason=disallowed_host allowed_registered_domain=%s",
            field,
            event_id,
            href,
            absolute,
            registered_domain,
        )
        return ""

    return absolute


def _extract_location(event: dict, title: str, counters: Counter) -> str:
    event_id = str(event.get("id", ""))
    venue = event.get("venue")

    if isinstance(venue, list):
        for item_index, item in enumerate(venue):
            if not isinstance(item, dict):
                logger.warning(
                    "window_rock_location_structured_rejected event_id=%s item_index=%d reason=venue_item_not_object seen_type=%s",
                    event_id,
                    item_index,
                    type(item).__name__,
                )
                continue
            venue_name = _clean_text(item.get("venue"))
            if venue_name:
                counters["location_from_structured_field_count"] += 1
                logger.info(
                    "window_rock_location_source=structured_field event_id=%s emitted_location=%r",
                    event_id,
                    venue_name,
                )
                return venue_name
            logger.info(
                "window_rock_location_structured_empty event_id=%s item_index=%d reason=missing_or_empty_venue_key",
                event_id,
                item_index,
            )
    else:
        logger.warning(
            "window_rock_location_structured_rejected event_id=%s reason=venue_not_list seen_type=%s",
            event_id,
            type(venue).__name__,
        )

    separators = [f" {chr(8211)} ", f" {chr(8212)} ", " - "]
    for separator in separators:
        marker = title.rfind(separator)
        if marker == -1:
            continue
        candidate = title[marker + len(separator) :].strip()
        if candidate:
            counters["location_from_title_heuristic_count"] += 1
            logger.info(
                "window_rock_location_source=title_suffix event_id=%s separator=%r emitted_location=%r",
                event_id,
                separator.encode("unicode_escape").decode("ascii"),
                candidate,
            )
            return candidate

    counters["location_absent_count"] += 1
    logger.info(
        "window_rock_location_absent event_id=%s reason=no_structured_venue_and_no_dash_suffix_in_title title=%r",
        event_id,
        title,
    )
    return ""


def _parse_start_datetime(raw_start_date: object, event_id: str) -> datetime | None:
    if not isinstance(raw_start_date, str) or not raw_start_date.strip():
        logger.warning(
            "window_rock_row_dropped event_id=%s reason=missing_start_date rejected_value=%r",
            event_id,
            raw_start_date,
        )
        return None
    try:
        return datetime.strptime(raw_start_date, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.warning(
            "window_rock_row_dropped event_id=%s reason=unparseable_start_date rejected_value=%r expected_format=%s",
            event_id,
            raw_start_date,
            "%Y-%m-%d %H:%M:%S",
        )
        return None


def _classify_status(title: str, agenda_url: str, agenda_packet_url: str, minutes_url: str, counters: Counter) -> str:
    if CANCELLED_RE.search(title[:500]):
        counters["cancelled_count"] += 1
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _meeting_subject(title: str) -> str:
    """Remove only the final location suffix while preserving DAY prefixes."""
    pieces = re.split(r"\s+[\u2013\u2014-]\s+", title)
    return " - ".join(pieces[:-1]) if len(pieces) > 1 else title


def _build_meeting(
    event: dict,
    api_url: str,
    registered_domain: str,
    counters: Counter,
    month_floor: date,
    window_end: date,
) -> dict[str, str] | None:
    event_id = str(event.get("id", ""))
    vendor_status = str(event.get("status", "")).strip()
    if vendor_status not in RECOGNIZED_VENDOR_STATUSES:
        counters["unknown_vendor_status_count"] += 1
        logger.warning(
            "window_rock_unknown_vendor_status event_id=%s status=%r recognized_statuses=%s",
            event_id,
            vendor_status,
            sorted(RECOGNIZED_VENDOR_STATUSES),
        )

    start_dt = _parse_start_datetime(event.get("start_date"), event_id)
    if start_dt is None:
        counters["rows_dropped"] += 1
        counters["drop_reason_missing_or_bad_start_date"] += 1
        return None

    title = _clean_text(event.get("title"))
    if not title:
        counters["rows_dropped"] += 1
        counters["drop_reason_missing_title"] += 1
        logger.warning("window_rock_row_dropped event_id=%s reason=missing_title", event_id)
        return None
    meeting_day = start_dt.date()
    if meeting_day < month_floor or meeting_day > window_end:
        counters["rows_dropped"] += 1
        counters["drop_reason_outside_window"] += 1
        logger.warning(
            "window_rock_row_dropped event_id=%s reason=outside_current_window "
            "meeting_date=%s floor=%s end=%s title=%r",
            event_id,
            meeting_day.isoformat(),
            month_floor.isoformat(),
            window_end.isoformat(),
            title,
        )
        return None

    subject = _meeting_subject(title)
    if not FULL_COUNCIL_RE.search(subject):
        counters["rows_dropped"] += 1
        counters["drop_reason_non_flagship_body"] += 1
        if "navajo nation council" in subject.casefold():
            counters["ambiguous_full_council_titles"] += 1
            logger.warning(
                "window_rock_row_dropped event_id=%s reason=ambiguous_full_council_title "
                "subject=%r full_title=%r",
                event_id,
                subject,
                title,
            )
        else:
            logger.warning(
                "window_rock_row_dropped event_id=%s reason=non_flagship_governing_body "
                "subject=%r",
                event_id,
                subject,
            )
        return None

    source_event_url = _emit_url(
        event.get("url"), api_url, registered_domain, "source_event_url_audit_only", event_id
    )
    logger.info(
        "window_rock_source_event_url_not_emitted event_id=%s source_url=%r "
        "reason=no_canonical_source_page_field",
        event_id,
        source_event_url,
    )
    agenda_url = ""
    minutes_url = ""
    video_url = ""
    agenda_packet_url = ""
    ecomment_url = ""
    location = _extract_location(event, title, counters)
    meeting_status = _classify_status(title, agenda_url, agenda_packet_url, minutes_url, counters)

    meeting = {
        "meeting_title": title,
        "meeting_date": start_dt.strftime("%Y-%m-%d"),
        "meeting_time": start_dt.strftime("%I:%M %p").lstrip("0"),
        "meeting_location": location,
        "meeting_status": meeting_status,
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": video_url,
        "agenda_packet_url": agenda_packet_url,
        "ecomment_url": ecomment_url,
        "meeting_id": event_id,
    }

    for field in CANONICAL_FIELDS:
        if not isinstance(meeting[field], str):
            meeting[field] = str(meeting[field])
    return meeting


def scrape_calendar(url: str) -> list[dict]:
    logger.warning(
        "window_rock_structural_absence fields=%s reason=the_events_calendar_rest_api_exposes_no_separate_documents",
        ["minutes_url", "video_url", "agenda_packet_url", "ecomment_url"],
    )

    api_url, registered_domain = _build_api_url(url)
    month_floor = date.today().replace(day=1)
    next_year_floor = month_floor.replace(year=month_floor.year + 1)
    window_end = next_year_floor - timedelta(days=1)
    api_url = _replace_query_params(
        api_url,
        {
            "start_date": month_floor.isoformat(),
            "end_date": window_end.isoformat(),
            "per_page": "50",
        },
    )
    counters: Counter = Counter()
    meetings: list[dict] = []

    try:
        with make_session() as session:
            first_url = _page_url(api_url, 1)
            first_page = _fetch_json_page(session, first_url, registered_domain)
            _validate_fingerprint(first_page, first_url)

            total_pages = first_page["total_pages"]
            total = first_page["total"]
            if total_pages > MAX_TOTAL_PAGES:
                raise ArchitecturalBlocker(
                    "total_pages_exceeds_cap "
                    f"total_pages={total_pages} cap={MAX_TOTAL_PAGES} api_url={api_url}"
                )

            pages = [first_page]
            for page_number in range(2, total_pages + 1):
                page_url = _page_url(api_url, page_number)
                logger.info(
                    "window_rock_fetching_additional_page page=%d total_pages=%d page_url=%s",
                    page_number,
                    total_pages,
                    page_url,
                )
                page_data = _fetch_json_page(session, page_url, registered_domain)
                _validate_fingerprint(page_data, page_url)
                pages.append(page_data)

            for page_index, page_data in enumerate(pages, start=1):
                events = page_data["events"]
                counters["rows_seen"] += len(events)
                logger.info(
                    "window_rock_page_iteration page=%d events_seen=%d total_pages=%d api_total=%d",
                    page_index,
                    len(events),
                    total_pages,
                    total,
                )
                for event in events:
                    meeting = _build_meeting(
                        event,
                        api_url,
                        registered_domain,
                        counters,
                        month_floor,
                        window_end,
                    )
                    if meeting is None:
                        continue
                    meetings.append(meeting)
                    counters["rows_emitted"] += 1

    except ArchitecturalBlocker as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("window_rock_architectural_blocker %s", exc)
        return []

    if counters["ambiguous_full_council_titles"]:
        raise ValueError(
            "Window Rock official calendar exposed an unrecognized full-Council title; "
            "refusing to guess the flagship scope"
        )

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Window Rock official Tribe Events API was accessible but exposed no "
            "Navajo Nation Council session in %s through %s",
            month_floor.isoformat(),
            window_end.isoformat(),
        )

    logger.warning(
        "window_rock_scrape_summary rows_seen=%d rows_emitted=%d rows_dropped=%d cancelled_count=%d "
        "location_from_structured_field_count=%d location_from_title_heuristic_count=%d location_absent_count=%d "
        "unknown_vendor_status_count=%d",
        counters["rows_seen"],
        counters["rows_emitted"],
        counters["rows_dropped"],
        counters["cancelled_count"],
        counters["location_from_structured_field_count"],
        counters["location_from_title_heuristic_count"],
        counters["location_absent_count"],
        counters["unknown_vendor_status_count"],
    )
    return meetings


__all__ = ["scrape_calendar"]

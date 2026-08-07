from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


FIELD_NAMES = (
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

BASE_URL = "https://gilbertaz.databankcloud.com/GilbertAgendaOnline"
GET_MORE_PATH = "/GilbertAgendaOnline/Meetings/GetMoreFullTextResults"
ALLOWED_HOSTS = {"gilbertaz.databankcloud.com"}
MAX_BYTES = 10_000_000
MAX_CONTINUATION_POSTS = 10
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
BAD_URL_SCHEMES = (
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "mailto:",
    "ftp:",
    "gopher:",
)

logger = logging.getLogger(__name__)


def emit_url(
    href: str,
    base: str,
    allowed_hosts: set[str] | None,
    field_name: str = "url",
) -> str:
    if not href:
        return ""

    raw_href = href.strip()
    lower_href = raw_href.lower()
    for bad_scheme in BAD_URL_SCHEMES:
        if lower_href.startswith(bad_scheme):
            logger.warning(
                "dropped URL field=%s rejected_input=%r reason=bad_scheme",
                field_name,
                href,
            )
            return ""

    absolute = urljoin(base, raw_href)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning(
            "dropped URL field=%s rejected_input=%r reason=non_http_scheme absolute=%r",
            field_name,
            href,
            absolute,
        )
        return ""

    emit_host = (parsed.netloc.split(":", 1)[0] or "").lower()
    base_host = (urlparse(base).netloc.split(":", 1)[0] or "").lower()
    if allowed_hosts is None:
        host_allowed = emit_host == base_host or emit_host.endswith("." + base_host)
    else:
        host_allowed = emit_host == base_host or emit_host in allowed_hosts

    if not host_allowed:
        logger.warning(
            "dropped URL field=%s rejected_input=%r reason=disallowed_host host=%s",
            field_name,
            href,
            emit_host,
        )
        return ""

    return absolute


def _fetch_text_bounded(
    session: requests.Session,
    url: str,
    allowed_hosts: set[str],
    max_bytes: int = MAX_BYTES,
) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).netloc.split(":", 1)[0] or "").lower()
        if final_host not in allowed_hosts:
            raise ValueError(
                f"Redirect to disallowed host: {final_host} (started from {url})"
            )
        response.raise_for_status()

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")

        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _post_json_bounded(
    session: requests.Session,
    url: str,
    payload: dict[str, str],
    allowed_hosts: set[str],
    max_bytes: int = MAX_BYTES,
) -> dict[str, Any]:
    with session.post(
        url,
        data=payload,
        timeout=30,
        stream=True,
        allow_redirects=True,
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE_URL},
    ) as response:
        final_host = (urlparse(response.url).netloc.split(":", 1)[0] or "").lower()
        if final_host not in allowed_hosts:
            raise ValueError(
                f"Redirect to disallowed host: {final_host} (started from {url})"
            )
        response.raise_for_status()

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")

        return json.loads(bytes(body).decode(response.encoding or "utf-8"))


def _validate_vendor_fingerprint(html: str, url: str) -> bool:
    witnesses = []
    if "OnBase Agenda Online" in html:
        witnesses.append("OnBase Agenda Online")
    if "Hyland Software" in html:
        witnesses.append("Hyland Software")
    if "OnBase_Logo.png" in html:
        witnesses.append("OnBase_Logo.png")
    if "meeting-row" in html:
        witnesses.append("meeting-row client-render token")
    if "showSearchResults(new SearchResults" in html:
        witnesses.append("Hyland SearchResults JSON")

    if witnesses:
        logger.info("vendor fingerprint confirmed url=%s witnesses=%s", url, witnesses)
        return True

    logger.warning(
        "vendor fingerprint mismatch url=%s expected=Hyland OnBase Agenda Online "
        "witnesses=[] action=honest_empty",
        url,
    )
    return False


def _extract_json_argument(html: str) -> dict[str, Any] | None:
    marker = "showSearchResults(new SearchResults("
    start = html.find(marker)
    if start == -1:
        logger.warning(
            "initial Hyland SearchResults JSON not found action=honest_empty"
        )
        return None

    object_start = html.find("{", start + len(marker))
    if object_start == -1:
        logger.warning(
            "initial Hyland SearchResults call lacked JSON object action=honest_empty"
        )
        return None

    depth = 0
    in_string = False
    escape_next = False
    for index in range(object_start, len(html)):
        char = html[index]
        if in_string:
            if escape_next:
                escape_next = False
            elif char == "\\":
                escape_next = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                raw_json = html[object_start : index + 1]
                return json.loads(raw_json)

    logger.warning("unterminated Hyland SearchResults JSON action=honest_empty")
    return None


def _request_verification_token(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    token = soup.select_one(
        '#getMoreResultsForm input[name="__RequestVerificationToken"]'
    )
    if not token:
        logger.warning(
            "GetMoreFullTextResults form token absent; continuation disabled"
        )
        return ""
    return token.get("value", "").strip()


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)


def _parse_datetime(meeting: dict[str, Any], counters: Counter[str]) -> tuple[str, str]:
    time_string = _clean_text(meeting.get("TimeString"))
    if time_string:
        collapsed = re.sub(r"\s+", " ", time_string).strip()
        for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p"):
            try:
                parsed = datetime.strptime(collapsed, fmt)
                return parsed.strftime("%Y-%m-%d"), parsed.strftime("%I:%M %p").lstrip("0")
            except ValueError:
                continue
        logger.warning(
            "meeting datetime parse failed meeting_id=%s field=TimeString rejected_value=%r",
            meeting.get("ID", ""),
            time_string,
        )
        counters["time_parse_failed"] += 1

    iso_time = _clean_text(meeting.get("Time"))
    if iso_time:
        try:
            parsed = datetime.fromisoformat(iso_time)
            return parsed.strftime("%Y-%m-%d"), parsed.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            logger.warning(
                "meeting datetime parse failed meeting_id=%s field=Time rejected_value=%r",
                meeting.get("ID", ""),
                iso_time,
            )
            counters["time_parse_failed"] += 1

    counters["time_absent"] += 1
    return "", ""


def _meeting_link(meeting_id: str, doc_type: int, field_name: str) -> str:
    if not meeting_id:
        logger.warning(
            "dropped URL field=%s reason=missing_meeting_id source=Hyland_ViewMeeting",
            field_name,
        )
        return ""
    return emit_url(
        f"/GilbertAgendaOnline/Meetings/ViewMeeting?id={meeting_id}&doctype={doc_type}",
        BASE_URL,
        ALLOWED_HOSTS,
        field_name,
    )


def _packet_link(
    meeting: dict[str, Any],
    unique_name_key: str,
    doc_type: int,
    field_name: str,
) -> str:
    meeting_id = _clean_text(meeting.get("ID"))
    unique_name = _clean_text(meeting.get(unique_name_key))
    if not meeting_id or not unique_name:
        logger.warning(
            "dropped URL field=%s reason=missing_hyland_packet_evidence "
            "meeting_id=%r unique_name_key=%s unique_name=%r",
            field_name,
            meeting_id,
            unique_name_key,
            unique_name,
        )
        return ""

    href = (
        f"/GilbertAgendaOnline/Documents/DownloadFile/{unique_name}.pdf"
        f"?documentType={doc_type}&meetingId={meeting_id}&isAttachment=true"
    )
    return emit_url(href, BASE_URL, ALLOWED_HOSTS, field_name)


def _video_link(meeting: dict[str, Any], counters: Counter[str]) -> str:
    if not meeting.get("HasMedia"):
        counters["video_url_absent_no_media"] += 1
        return ""

    media = meeting.get("Media")
    if not isinstance(media, dict):
        logger.warning(
            "dropped URL field=video_url reason=HasMedia_without_Media_object meeting_id=%s",
            meeting.get("ID", ""),
        )
        counters["video_url_drop_missing_media_object"] += 1
        return ""

    if not (media.get("IsLive") or media.get("IsOnDemand")):
        logger.warning(
            "dropped URL field=video_url reason=unknown_media_state meeting_id=%s media=%r",
            meeting.get("ID", ""),
            media,
        )
        counters["video_url_drop_unknown_media_state"] += 1
        return ""

    meeting_id = _clean_text(meeting.get("ID"))
    doc_type = _clean_text(meeting.get("LatestDocumentType"))
    if not meeting_id or not doc_type:
        logger.warning(
            "dropped URL field=video_url reason=missing_media_link_evidence "
            "meeting_id=%r latest_document_type=%r",
            meeting_id,
            doc_type,
        )
        counters["video_url_drop_missing_id_or_doctype"] += 1
        return ""

    return emit_url(
        f"/GilbertAgendaOnline/Meetings/ViewMeeting?id={meeting_id}&doctype={doc_type}",
        BASE_URL,
        ALLOWED_HOSTS,
        "video_url",
    )


def _ecomment_link(meeting: dict[str, Any], counters: Counter[str]) -> str:
    possible_keys = [
        key
        for key in meeting
        if "comment" in str(key).lower() or "ecomment" in str(key).lower()
    ]
    if possible_keys:
        logger.warning(
            "dropped URL field=ecomment_url reason=unclassified_hyland_comment_keys "
            "meeting_id=%s keys=%s",
            meeting.get("ID", ""),
            possible_keys,
        )
        counters["ecomment_url_drop_unclassified_keys"] += 1
    else:
        counters["ecomment_url_absent_no_source_key"] += 1
    return ""


def _status_for(title: str, agenda_url: str, agenda_packet_url: str, minutes_url: str) -> str:
    if CANCELLED_RE.search(title[:500]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _meeting_from_hyland_json(
    meeting: dict[str, Any],
    counters: Counter[str],
) -> dict[str, str] | None:
    meeting_id = _clean_text(meeting.get("ID"))
    title = _clean_text(meeting.get("Name"))
    meeting_type = _clean_text(meeting.get("MeetingTypeName"))
    if meeting_type:
        title = f"{meeting_type} - {title}" if title else meeting_type

    meeting_date, meeting_time = _parse_datetime(meeting, counters)
    if not title:
        logger.warning(
            "dropped row reason=missing_title meeting_id=%r source=Hyland SearchResults",
            meeting_id,
        )
        counters["row_drop_missing_title"] += 1
        return None
    if not meeting_date:
        logger.warning(
            "dropped row reason=missing_or_unparseable_date meeting_id=%r title=%r",
            meeting_id,
            title,
        )
        counters["row_drop_missing_date"] += 1
        return None

    location = _clean_text(meeting.get("Location"))
    if not location:
        counters["meeting_location_absent_no_row_location"] += 1

    agenda_url = ""
    if meeting.get("IsAgendaAvailable"):
        agenda_url = _meeting_link(meeting_id, 1, "agenda_url")
    else:
        counters["agenda_url_absent_flag_false"] += 1

    agenda_packet_url = ""
    if meeting.get("IsAgendaPacketAvailable"):
        agenda_packet_url = _packet_link(
            meeting,
            "AgendaPacketUniqueName",
            5,
            "agenda_packet_url",
        )
    else:
        counters["agenda_packet_url_absent_flag_false"] += 1

    minutes_url = ""
    if meeting.get("IsMinutesAvailable"):
        minutes_url = _meeting_link(meeting_id, 2, "minutes_url")
    else:
        counters["minutes_url_absent_flag_false"] += 1

    if meeting.get("IsMinutesPacketAvailable"):
        logger.warning(
            "Hyland minutes packet present but canonical schema has no minutes_packet_url "
            "meeting_id=%s action=minutes_url_keeps_minutes_view_link",
            meeting_id,
        )
        counters["minutes_packet_present_no_canonical_field"] += 1

    if meeting.get("IsSummaryAvailable"):
        logger.warning(
            "Hyland summary present but canonical schema has no summary_url "
            "meeting_id=%s action=ignored",
            meeting_id,
        )
        counters["summary_present_no_canonical_field"] += 1

    video_url = _video_link(meeting, counters)
    ecomment_url = _ecomment_link(meeting, counters)
    status = _status_for(title, agenda_url, agenda_packet_url, minutes_url)

    row = {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": location,
        "meeting_status": status,
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": video_url,
        "agenda_packet_url": agenda_packet_url,
        "ecomment_url": ecomment_url,
        "meeting_id": meeting_id,
    }
    return {field: str(row[field] or "") for field in FIELD_NAMES}


def _continuation_payload(
    token: str,
    search_results: dict[str, Any],
    last_meeting_id: str,
) -> dict[str, str]:
    meeting_type_ids = search_results.get("MeetingTypeIDs") or []
    if isinstance(meeting_type_ids, str):
        mtids = meeting_type_ids
    else:
        mtids = ",".join(str(value) for value in meeting_type_ids)

    return {
        "__RequestVerificationToken": token,
        "dropid": str(search_results.get("DateRangeOptionID") or "4"),
        "mtids": mtids,
        "searchTerm": str(search_results.get("Keywords") or ""),
        "dropsv": str(search_results.get("DateRangeCustomStartDate") or ""),
        "dropev": str(search_results.get("DateRangeCustomEndDate") or ""),
        "lastMeetingIdSearched": last_meeting_id,
    }


def _collect_search_results(
    session: requests.Session,
    initial_results: dict[str, Any],
    token: str,
    counters: Counter[str],
) -> list[dict[str, Any]]:
    meetings = list(initial_results.get("Meetings") or [])
    seen_ids = {_clean_text(meeting.get("ID")) for meeting in meetings}
    current_results = initial_results
    get_more_url = urljoin(BASE_URL, GET_MORE_PATH)

    if current_results.get("NoMoreResults"):
        counters["page_fetch_no_more_results_initial"] += 1
        return meetings
    if not token:
        counters["page_fetch_drop_missing_token"] += 1
        return meetings

    for page_index in range(1, MAX_CONTINUATION_POSTS + 1):
        if not meetings:
            logger.warning(
                "GetMoreFullTextResults skipped reason=no_initial_meetings page_index=%s",
                page_index,
            )
            counters["page_fetch_drop_no_initial_meetings"] += 1
            break

        last_meeting_id = _clean_text(meetings[-1].get("ID"))
        payload = _continuation_payload(token, current_results, last_meeting_id)
        try:
            next_results = _post_json_bounded(
                session,
                get_more_url,
                payload,
                ALLOWED_HOSTS,
            )
        except Exception:
            logger.warning(
                "GetMoreFullTextResults failed page_index=%s last_meeting_id=%s",
                page_index,
                last_meeting_id,
                exc_info=True,
            )
            counters["page_fetch_drop_exception"] += 1
            break

        batch = list(next_results.get("Meetings") or [])
        batch_ids = [_clean_text(meeting.get("ID")) for meeting in batch]
        new_batch = [meeting for meeting in batch if _clean_text(meeting.get("ID")) not in seen_ids]

        logger.info(
            "GetMoreFullTextResults page_index=%s batch_size=%s new_rows=%s "
            "no_more_results=%s last_meeting_id_sent=%s",
            page_index,
            len(batch),
            len(new_batch),
            next_results.get("NoMoreResults"),
            last_meeting_id,
        )

        if batch and not new_batch:
            logger.warning(
                "GetMoreFullTextResults returned repeated batch page_index=%s "
                "last_meeting_id_sent=%s batch_ids=%s action=stop_to_avoid_loop",
                page_index,
                last_meeting_id,
                batch_ids,
            )
            counters["page_fetch_drop_repeated_batch"] += 1
            break

        meetings.extend(new_batch)
        seen_ids.update(_clean_text(meeting.get("ID")) for meeting in new_batch)
        current_results = next_results
        if next_results.get("NoMoreResults") or not batch:
            break
    else:
        logger.warning(
            "GetMoreFullTextResults reached cap max_posts=%s action=partial_results",
            MAX_CONTINUATION_POSTS,
        )
        counters["page_fetch_drop_cap_reached"] += 1

    return meetings


def scrape_calendar(url: str) -> list[dict]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    try:
        html = _fetch_text_bounded(session, url, ALLOWED_HOSTS)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else ""
        if status == 403:
            logger.warning(
                "architectural blocker fetching Gilbert Hyland url=%s status=403 "
                "action=honest_empty",
                url,
                exc_info=True,
            )
            return []
        raise

    if not _validate_vendor_fingerprint(html, url):
        return []

    initial_results = _extract_json_argument(html)
    if initial_results is None:
        return []

    counters: Counter[str] = Counter()
    token = _request_verification_token(html)
    source_meetings = _collect_search_results(session, initial_results, token, counters)

    parsed_meetings: list[dict[str, str]] = []
    rows_seen = 0
    rows_dropped = 0
    drop_reasons: Counter[str] = Counter()

    for meeting in source_meetings:
        rows_seen += 1
        try:
            parsed = _meeting_from_hyland_json(meeting, counters)
        except Exception:
            rows_dropped += 1
            drop_reasons["row_exception"] += 1
            logger.warning(
                "dropped row reason=row_exception meeting_id=%s",
                meeting.get("ID", ""),
                exc_info=True,
            )
            continue

        if parsed is None:
            rows_dropped += 1
            drop_reasons["row_helper_returned_none"] += 1
            continue

        parsed_meetings.append(parsed)

    logger.warning(
        "Gilbert Hyland scrape complete rows_seen=%s rows_accepted=%s rows_dropped=%s "
        "drop_reasons=%s field_absence_and_page_counters=%s",
        rows_seen,
        len(parsed_meetings),
        rows_dropped,
        dict(drop_reasons),
        dict(counters),
    )
    return parsed_meetings


if __name__ == "__main__":
    scraped = scrape_calendar(BASE_URL)
    print(f"meeting count: {len(scraped)}")
    for row in scraped[:2]:
        print(row)

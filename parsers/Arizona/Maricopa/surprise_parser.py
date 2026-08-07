import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

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

DEFAULT_CALENDAR_URL = "https://surpriseaz.portal.civicclerk.com/"
EVENTS_API_URL = "https://surpriseaz.api.civicclerk.com/v1/Events"
API_BASE_URL = "https://surpriseaz.api.civicclerk.com/v1"

ALLOWED_HOSTS = {
    "surpriseaz.portal.civicclerk.com",
    "surpriseaz.api.civicclerk.com",
    "cpmedia.azureedge.net",
}
ALLOWED_HOST_SUFFIXES = (".civicclerk.com",)
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")

CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
MAX_BYTES = 10_000_000
PAGE_LIMIT = 20
EVENT_LIMIT = 200

DOCUMENT_TYPE_TO_FIELD = {
    "agenda": "agenda_url",
    "agenda packet": "agenda_packet_url",
    "minutes": "minutes_url",
}
DOCUMENT_TYPE_TO_FILE_TYPE = {
    "agenda": 1,
    "agenda packet": 2,
    "minutes": 4,
}


class Trail:
    def __init__(self) -> None:
        self.rows_seen = 0
        self.rows_accepted = 0
        self.rows_dropped = 0
        self.drop_reasons: Counter[str] = Counter()
        self.empty_fields: Counter[str] = Counter()
        self.samples: dict[str, list[str]] = defaultdict(list)

    def sample(self, key: str, row_id: str, detail: str) -> None:
        bucket = self.samples[key]
        if len(bucket) < 10:
            bucket.append(f"{row_id}: {detail}")

    def dropped_row(self, row_id: str, reason: str, detail: str) -> None:
        self.rows_dropped += 1
        self.drop_reasons[reason] += 1
        self.sample(f"drop:{reason}", row_id, detail)

    def empty_field(self, field: str, row_id: str, reason: str) -> None:
        key = f"{field}:{reason}"
        self.empty_fields[key] += 1
        self.sample(f"empty:{key}", row_id, reason)

    def warn_summary(self) -> None:
        logger.warning(
            "scrape_calendar: rows_seen=%d rows_accepted=%d rows_dropped=%d drop_reasons=%s",
            self.rows_seen,
            self.rows_accepted,
            self.rows_dropped,
            dict(self.drop_reasons),
        )
        for key, count in sorted(self.empty_fields.items()):
            logger.warning(
                "scrape_calendar: empty_field %s count=%d first_samples=%s",
                key,
                count,
                self.samples.get(f"empty:{key}", []),
            )
        for key, samples in sorted(self.samples.items()):
            if key.startswith("drop:"):
                logger.warning("scrape_calendar: %s first_samples=%s", key, samples)


def _host_allowed(host: str) -> bool:
    return host in ALLOWED_HOSTS or any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def _fetch_json_bounded(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str] | None = None,
    max_bytes: int = MAX_BYTES,
    timeout: int = 30,
) -> dict[str, Any]:
    with session.get(
        url,
        params=params,
        timeout=timeout,
        stream=True,
        verify=True,
        allow_redirects=True,
    ) as response:
        response.raise_for_status()
        final_host = (urlparse(response.url).netloc.split(":")[0] or "").lower()
        if not _host_allowed(final_host):
            raise ValueError(f"Redirect to disallowed host: {final_host} (from {url})")
        body = b""
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body += chunk
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")
        return json.loads(body.decode(response.encoding or "utf-8"))


def emit_url(href: Any, base_url: str, *, field: str, row_id: str) -> str:
    if href in (None, ""):
        return ""
    raw = str(href).strip()
    low = raw.lower().lstrip()
    for bad in BAD_SCHEMES:
        if low.startswith(bad):
            logger.warning("emit_url: row=%s field=%s dropped %r (bad scheme)", row_id, field, href)
            return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "emit_url: row=%s field=%s dropped %r (non-http scheme after urljoin)",
            row_id,
            field,
            href,
        )
        return ""
    emit_host = (parsed.netloc.split(":")[0] or "").lower()
    if not _host_allowed(emit_host):
        logger.warning(
            "emit_url: row=%s field=%s dropped %r (host %s not in allowlist)",
            row_id,
            field,
            href,
            emit_host,
        )
        return ""
    return absolute


def _validate_vendor_fingerprint(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"CivicClerk fingerprint failed: expected dict, got {type(payload).__name__}")
    if "@odata.context" not in payload or "value" not in payload:
        raise ValueError("CivicClerk fingerprint failed: missing @odata.context or value key")
    context = str(payload.get("@odata.context") or "")
    if "civicclerk" not in context.lower():
        raise ValueError(f"CivicClerk fingerprint failed: @odata.context lacks civicclerk token: {context[:200]}")
    logger.warning("CivicClerk fingerprint confirmed via @odata.context=%s", context[:200])


def _sanitize_text(value: Any, *, field: str, row_id: str) -> str:
    if value in (None, ""):
        return ""
    cleaned = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    if str(value).strip() and not cleaned:
        logger.warning("_sanitize_text: row=%s field=%s dropped non-empty value after HTML stripping", row_id, field)
    return cleaned


def _parse_event_datetime(value: Any, *, row_id: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        logger.warning("_parse_event_datetime: row=%s missing startDateTime/eventDate", row_id)
        return "", ""
    raw = value.strip()
    try:
        normalized = raw[:-1] if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        logger.warning("_parse_event_datetime: row=%s failed to parse %r", row_id, value)
        return "", ""
    hour = dt.hour % 12 or 12
    return dt.strftime("%Y-%m-%d"), f"{hour}:{dt.minute:02d} {dt.strftime('%p')}"


def _format_location(location: Any, *, row_id: str, trail: Trail) -> str:
    if not isinstance(location, dict):
        trail.empty_field("meeting_location", row_id, "eventLocation absent")
        return ""
    parts = []
    for key in ("address2", "address1", "city", "state", "zipCode"):
        value = _sanitize_text(location.get(key), field=f"eventLocation.{key}", row_id=row_id)
        if value:
            parts.append(value)
    if not parts:
        trail.empty_field("meeting_location", row_id, "eventLocation present without text fields")
        return ""
    return ", ".join(parts)


def _document_stream_url(file_id: Any) -> str:
    if isinstance(file_id, bool):
        return ""
    try:
        numeric_file_id = int(file_id)
    except (TypeError, ValueError):
        return ""
    if numeric_file_id <= 0:
        return ""
    return f"{API_BASE_URL}/Meetings/GetMeetingFileStream(fileId={numeric_file_id},plainText=false)"


def _extract_documents(event: dict[str, Any], *, row_id: str, trail: Trail) -> dict[str, str]:
    urls = {"agenda_url": "", "minutes_url": "", "agenda_packet_url": ""}
    published_files = event.get("publishedFiles")
    if not isinstance(published_files, list) or not published_files:
        if event.get("hasAgenda") is True:
            logger.warning("row=%s hasAgenda=true but publishedFiles is absent/empty", row_id)
        for field in urls:
            trail.empty_field(field, row_id, "no publishedFiles evidence")
        return urls

    sorted_files = sorted(
        (item for item in published_files if isinstance(item, dict)),
        key=lambda item: (item.get("sort") if isinstance(item.get("sort"), int) else 999, item.get("fileId") or 0),
    )
    non_dict_count = len(published_files) - len(sorted_files)
    if non_dict_count:
        logger.warning("row=%s dropped %d non-dict publishedFiles entries", row_id, non_dict_count)

    seen_fields: set[str] = set()
    for item in sorted_files:
        raw_type = _sanitize_text(item.get("type"), field="publishedFiles.type", row_id=row_id)
        type_key = raw_type.lower()
        field = DOCUMENT_TYPE_TO_FIELD.get(type_key)
        if field is None:
            logger.warning(
                "row=%s dropped published file fileId=%r type=%r fileType=%r (unmapped document type)",
                row_id,
                item.get("fileId"),
                raw_type,
                item.get("fileType"),
            )
            continue

        expected_file_type = DOCUMENT_TYPE_TO_FILE_TYPE[type_key]
        if item.get("fileType") != expected_file_type:
            logger.warning(
                "row=%s document type/fileType mismatch type=%r fileType=%r expected=%r",
                row_id,
                raw_type,
                item.get("fileType"),
                expected_file_type,
            )

        if field in seen_fields:
            logger.warning(
                "row=%s dropped duplicate %s fileId=%r name=%r",
                row_id,
                field,
                item.get("fileId"),
                item.get("name"),
            )
            continue

        candidate = item.get("streamUrl") or _document_stream_url(item.get("fileId"))
        if not candidate:
            logger.warning(
                "row=%s field=%s dropped fileId=%r url=%r (no witnessed stream URL evidence)",
                row_id,
                field,
                item.get("fileId"),
                item.get("url"),
            )
            continue
        emitted = emit_url(candidate, API_BASE_URL + "/", field=field, row_id=row_id)
        if emitted:
            urls[field] = emitted
            seen_fields.add(field)
        else:
            logger.warning("row=%s field=%s dropped candidate=%r after URL hygiene", row_id, field, candidate)

    for field, value in urls.items():
        if not value:
            trail.empty_field(field, row_id, "no same-row published file of matching type")
    if event.get("hasAgenda") is True and not (urls["agenda_url"] or urls["agenda_packet_url"]):
        logger.warning("row=%s hasAgenda=true but no agenda or packet URL emitted", row_id)
    return urls


def _extract_video_url(event: dict[str, Any], *, row_id: str, trail: Trail) -> str:
    candidates = [
        ("mediaStreamPath", event.get("mediaStreamPath")),
        ("mediaSourcePathMp4", event.get("mediaSourcePathMp4")),
        ("externalMediaUrl", event.get("externalMediaUrl")),
    ]
    seen: set[str] = set()
    for source_field, candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(str(candidate))
        emitted = emit_url(candidate, API_BASE_URL + "/", field="video_url", row_id=row_id)
        if emitted:
            return emitted
        logger.warning("row=%s video candidate from %s rejected: %r", row_id, source_field, candidate)
    if event.get("youtubeVideoId") and not seen:
        logger.warning("row=%s youtubeVideoId present but no full video URL field was exposed", row_id)
    if event.get("hasMedia") is True:
        trail.empty_field("video_url", row_id, "hasMedia true but no valid media URL emitted")
    else:
        trail.empty_field("video_url", row_id, "hasMedia false/no media evidence")
    return ""


def _extract_ecomment_url(event: dict[str, Any], *, row_id: str, trail: Trail) -> str:
    for key in ("eCommentUrl", "ecommentUrl", "publicCommentUrl", "publicCommentsUrl"):
        value = event.get(key)
        if value:
            emitted = emit_url(value, API_BASE_URL + "/", field="ecomment_url", row_id=row_id)
            if emitted:
                return emitted
            logger.warning("row=%s ecomment candidate from %s rejected: %r", row_id, key, value)
    if event.get("publicCommentsEnabled") is True:
        logger.warning("row=%s publicCommentsEnabled=true but no eComment URL field was exposed", row_id)
        trail.empty_field("ecomment_url", row_id, "publicCommentsEnabled true without URL evidence")
    else:
        trail.empty_field("ecomment_url", row_id, "publicCommentsEnabled false/no ecomment evidence")
    return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _status(
    event: dict[str, Any],
    *,
    title: str,
    row_id: str,
    agenda_url: str,
    agenda_packet_url: str,
    minutes_url: str,
) -> str:
    is_cancelled = _truthy(event.get("isCancelled")) or _truthy(event.get("isCanceled"))
    title_cancelled = bool(CANCELLED_RE.search(title[:300]))
    file_cancel_signal = any(
        CANCELLED_RE.search(str(item.get("name") or "")[:300])
        for item in event.get("publishedFiles") or []
        if isinstance(item, dict)
    )
    if file_cancel_signal and not (is_cancelled or title_cancelled):
        logger.warning(
            "row=%s found cancellation wording in publishedFiles name, but event title/API cancellation evidence is absent",
            row_id,
        )

    vendor_publish_status = event.get("isPublished")
    if vendor_publish_status not in (None, "", "Published"):
        logger.warning(
            "row=%s encountered non-canonical isPublished vocabulary %r; status will be derived from row documents",
            row_id,
            vendor_publish_status,
        )

    if is_cancelled or title_cancelled:
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _empty_row() -> dict[str, str]:
    return {field: "" for field in FIELD_NAMES}


def _finalize_row(row: dict[str, Any], *, row_id: str) -> dict[str, str]:
    finalized = {field: "" if row.get(field) is None else str(row.get(field, "")) for field in FIELD_NAMES}
    extra = set(row) - set(FIELD_NAMES)
    if extra:
        raise ValueError(f"row={row_id} has unexpected fields: {sorted(extra)}")
    return finalized


def _api_url_from_input(url: str) -> str:
    parsed = urlparse(url or DEFAULT_CALENDAR_URL)
    host = (parsed.netloc.split(":")[0] or "").lower()
    if host.endswith(".api.civicclerk.com") and parsed.path.rstrip("/").endswith("/v1/Events"):
        return urljoin(f"{parsed.scheme}://{host}", parsed.path.rstrip("/"))
    if host.endswith(".portal.civicclerk.com"):
        tenant = host.split(".", 1)[0]
        return f"https://{tenant}.api.civicclerk.com/v1/Events"
    logger.warning("scrape_calendar: input URL %r is not a CivicClerk portal/API URL; using Surprise default API", url)
    return EVENTS_API_URL


def _load_events(session: requests.Session, api_url: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    params: dict[str, str] | None = {"$top": str(EVENT_LIMIT), "$orderby": "startDateTime desc, eventName desc"}
    next_url: str | None = api_url
    pages = 0
    while next_url and len(events) < EVENT_LIMIT:
        pages += 1
        if pages > PAGE_LIMIT:
            logger.warning(
                "scrape_calendar: page cap reached page_limit=%d rows_loaded=%d next_url=%s",
                PAGE_LIMIT,
                len(events),
                next_url,
            )
            break
        try:
            payload = _fetch_json_bounded(session, next_url, params=params)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            if pages == 1 and status in (401, 403):
                logger.warning("scrape_calendar: CivicClerk API blocked with HTTP %s; returning honest-empty", status)
                return []
            logger.warning(
                "scrape_calendar: page fetch failed page=%d status=%s rows_loaded=%d error=%s",
                pages,
                status,
                len(events),
                exc,
            )
            break
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            if pages == 1:
                logger.warning("scrape_calendar: first page fetch failed (%s); returning honest-empty", exc)
                return []
            logger.warning("scrape_calendar: later page fetch failed page=%d rows_loaded=%d error=%s", pages, len(events), exc)
            break

        _validate_vendor_fingerprint(payload)
        values = payload.get("value")
        if not isinstance(values, list):
            raise ValueError(f"CivicClerk events payload value is not a list: {type(values).__name__}")
        if not values:
            logger.warning("scrape_calendar: page=%d returned 0 events next_url=%s", pages, next_url)
            break
        logger.warning(
            "scrape_calendar: fetched page=%d count=%d first_id=%s last_id=%s",
            pages,
            len(values),
            values[0].get("id") if isinstance(values[0], dict) else "",
            values[-1].get("id") if isinstance(values[-1], dict) else "",
        )
        for value in values:
            if isinstance(value, dict):
                events.append(value)
            else:
                logger.warning("scrape_calendar: dropped non-dict event entry type=%s", type(value).__name__)
            if len(events) >= EVENT_LIMIT:
                break
        next_url = payload.get("@odata.nextLink")
        params = None
    return events


def scrape_calendar(url: str) -> list[dict]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
    )

    api_url = _api_url_from_input(url)
    events = _load_events(session, api_url)
    trail = Trail()
    rows: list[dict[str, str]] = []

    for event in events:
        row_id = str(event.get("id") or "")
        trail.rows_seen += 1
        if not row_id:
            row_id = f"seen-{trail.rows_seen}"
            trail.dropped_row(row_id, "missing-id", "event id absent")
            logger.warning("row=%s dropped event because id is absent", row_id)
            continue

        title = _sanitize_text(event.get("eventName"), field="eventName", row_id=row_id)
        if not title:
            trail.dropped_row(row_id, "missing-title", f"eventName={event.get('eventName')!r}")
            logger.warning("row=%s dropped event because eventName is absent/empty", row_id)
            continue

        date_value = event.get("startDateTime") or event.get("eventDate")
        meeting_date, meeting_time = _parse_event_datetime(date_value, row_id=row_id)
        if not meeting_date:
            trail.dropped_row(row_id, "missing-date", f"startDateTime/eventDate={date_value!r}")
            logger.warning("row=%s dropped event because no ISO meeting_date could be parsed", row_id)
            continue
        if not meeting_time:
            trail.empty_field("meeting_time", row_id, "datetime field parsed without time")

        docs = _extract_documents(event, row_id=row_id, trail=trail)
        video_url = _extract_video_url(event, row_id=row_id, trail=trail)
        ecomment_url = _extract_ecomment_url(event, row_id=row_id, trail=trail)
        location = _format_location(event.get("eventLocation"), row_id=row_id, trail=trail)
        status = _status(
            event,
            title=title,
            row_id=row_id,
            agenda_url=docs["agenda_url"],
            agenda_packet_url=docs["agenda_packet_url"],
            minutes_url=docs["minutes_url"],
        )

        row = _empty_row()
        row.update(
            {
                "meeting_title": title,
                "meeting_date": meeting_date,
                "meeting_time": meeting_time,
                "meeting_location": location,
                "meeting_status": status,
                "agenda_url": docs["agenda_url"],
                "minutes_url": docs["minutes_url"],
                "video_url": video_url,
                "agenda_packet_url": docs["agenda_packet_url"],
                "ecomment_url": ecomment_url,
                "meeting_id": row_id,
            }
        )
        rows.append(_finalize_row(row, row_id=row_id))
        trail.rows_accepted += 1

    trail.warn_summary()
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    output = scrape_calendar(DEFAULT_CALENDAR_URL)
    print(f"Found {len(output)} meetings.")
    for meeting in output[:2]:
        print(json.dumps(meeting, indent=2))

"""Bounded current/future adapter for official CivicClerk event feeds.

City wrappers supply the exact governing-body phrase witnessed for their
jurisdiction.  The adapter keeps CivicClerk transport, schema validation, URL
hygiene, and honest-empty evidence in one place without broadening body scope.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from polite_http import make_session


MAX_REQUESTS = 14
MAX_EVENTS = 200
MAX_RESPONSE_BYTES = 12_000_000
CHUNK_SIZE = 65_536
BLOCKED_STATUSES = {401, 403, 407, 423, 429, 451}
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
MEETING_SIGNAL_RE = re.compile(r"\b(?:meeting|session|hearing)\b", re.IGNORECASE)
NON_MEETING_COUNCIL_RE = re.compile(
    r"\b(?:notice of (?:possible )?quorum|quorum notice|upcoming agenda items?|"
    r"public notices?|calendar)\b",
    re.IGNORECASE,
)
KNOWN_OTHER_BODY_RE = re.compile(
    r"\b(?:board|commission|committee|authority|district|task force|"
    r"planning(?: and| &) zoning|airport advisory|tourism advisory|"
    r"industrial development|library advisory|parks and recreation|"
    r"utility enterprises|retirement system)\b",
    re.IGNORECASE,
)
FIELDS = (
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
DOCUMENT_FIELDS = {
    "agenda": "agenda_url",
    "agenda packet": "agenda_packet_url",
    "minutes": "minutes_url",
    "video": "video_url",
}
MEDIA_KEYS = (
    "externalMediaUrl",
    "mediaSourcePath",
    "mediaStreamPath",
    "mediaSourcePathMp4",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FetchResult:
    events: list[Any]
    authoritative_window: bool
    source_blocked: bool = False


def scrape_civicclerk_current(
    portal_url: str,
    *,
    city_label: str,
    governing_body_phrase: str,
    exact_allowed_titles: frozenset[str] = frozenset(),
    additional_output_hosts: frozenset[str] = frozenset(),
    today: date | None = None,
) -> list[dict[str, str]]:
    """Return council-only CivicClerk events from this calendar month forward."""
    tenant, portal_host, api_host = _source_identity(portal_url)
    api_root = f"https://{api_host}/"
    api_url = urljoin(api_root, "v1/Events")
    floor = (today or date.today()).replace(day=1)
    normalized_phrase = _normalize_title(governing_body_phrase)
    normalized_exact = frozenset(_normalize_title(title) for title in exact_allowed_titles)
    if not normalized_phrase:
        raise ValueError(f"{city_label} CivicClerk governing-body phrase is empty")

    with make_session() as session:
        fetched = _fetch_current_events(
            session,
            api_url=api_url,
            api_host=api_host,
            floor=floor,
            city_label=city_label,
        )
    if fetched.source_blocked:
        logger.warning("health_empty_kind=source_blocked")
        return []

    output_hosts = {
        api_host,
        portal_host,
        "cpmedia.azureedge.net",
        "youtu.be",
        "youtube.com",
        "www.youtube.com",
    } | {host.casefold() for host in additional_output_hosts}
    stats: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    ambiguous_titles: list[str] = []

    for position, event in enumerate(fetched.events, start=1):
        stats["rows_seen"] += 1
        if not isinstance(event, dict):
            stats["rows_dropped_non_object"] += 1
            logger.warning(
                "%s CivicClerk row dropped: position=%d reason=non_object type=%s",
                city_label,
                position,
                type(event).__name__,
            )
            continue

        meeting_id = _clean(event.get("id") or event.get("eventId"))
        title = _clean(event.get("eventName") or event.get("title") or event.get("name"))
        row_id = meeting_id or f"position-{position}"
        meeting_date, meeting_time = _event_datetime(event, row_id, city_label)
        meeting_day = date.fromisoformat(meeting_date)
        if meeting_day < floor:
            stats["rows_dropped_before_floor"] += 1
            logger.warning(
                "%s CivicClerk row dropped despite current query: id=%s date=%s floor=%s title=%r",
                city_label,
                row_id,
                meeting_date,
                floor.isoformat(),
                title,
            )
            continue
        stats["rows_current_month_forward"] += 1

        scope, category_values = _classify_event_scope(
            event,
            title,
            governing_body_phrase=normalized_phrase,
            exact_allowed_titles=normalized_exact,
        )
        if scope == "other":
            stats["rows_dropped_known_other_body"] += 1
            logger.warning(
                "%s CivicClerk current row dropped: reason=known_other_body id=%s title=%r categories=%r",
                city_label,
                row_id,
                title,
                category_values,
            )
            continue
        if scope == "ambiguous":
            stats["rows_dropped_ambiguous_title"] += 1
            ambiguous_titles.append(title)
            logger.warning(
                "%s CivicClerk current row withheld: reason=ambiguous_body_signal id=%s title=%r categories=%r",
                city_label,
                row_id,
                title,
                category_values,
            )
            continue
        stats["rows_governing_body"] += 1

        if not meeting_id:
            raise ValueError(f"{city_label} CivicClerk governing-body event lacks vendor id: {row_id}")
        if meeting_id in seen_ids:
            stats["rows_dropped_duplicate_id"] += 1
            logger.warning("%s CivicClerk duplicate event dropped: id=%s title=%r", city_label, meeting_id, title)
            continue
        seen_ids.add(meeting_id)

        documents = _documents(
            event.get("publishedFiles"),
            api_root=api_root,
            allowed_hosts=output_hosts,
            city_label=city_label,
            row_id=row_id,
            stats=stats,
        )
        video_url = documents["video_url"] or _event_url(
            event,
            MEDIA_KEYS,
            field="video_url",
            api_root=api_root,
            allowed_hosts=output_hosts,
            city_label=city_label,
            row_id=row_id,
            stats=stats,
        )
        ecomment_url = _event_url(
            event,
            ("eCommentUrl", "ecommentUrl", "publicCommentUrl", "publicCommentsUrl"),
            field="ecomment_url",
            api_root=api_root,
            allowed_hosts=output_hosts,
            city_label=city_label,
            row_id=row_id,
            stats=stats,
        )
        if not ecomment_url and event.get("publicCommentsEnabled") is True:
            stats["ecomment_enabled_without_url"] += 1
            logger.warning(
                "%s CivicClerk ecomment URL absent: id=%s enabled=true but no stable URL was exposed",
                city_label,
                row_id,
            )

        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": _location(event.get("eventLocation"), stats),
            "meeting_status": _status(title, documents),
            "agenda_url": documents["agenda_url"],
            "minutes_url": documents["minutes_url"],
            "video_url": video_url,
            "agenda_packet_url": documents["agenda_packet_url"],
            "ecomment_url": ecomment_url,
            "meeting_id": meeting_id,
        }
        _validate_meeting(meeting, city_label)
        meetings.append(meeting)
        stats["rows_emitted"] += 1
        logger.info("%s CivicClerk meeting emitted: id=%s fields=%s", city_label, meeting_id, meeting)

    if ambiguous_titles:
        raise ValueError(
            f"{city_label} CivicClerk exposed current rows with unreviewed body titles: "
            f"{ambiguous_titles[:10]!r}"
        )

    meetings.sort(key=lambda row: (row["meeting_date"], row["meeting_time"], row["meeting_id"]))
    logger.info(
        "%s CivicClerk bounded scrape summary tenant=%s floor=%s authoritative_window=%s stats=%s",
        city_label,
        tenant,
        floor.isoformat(),
        fetched.authoritative_window,
        dict(stats),
    )
    if not meetings:
        if not fetched.authoritative_window:
            raise ValueError(
                f"{city_label} CivicClerk unfiltered fallback could not prove an official current-window zero"
            )
        logger.warning("health_empty_kind=confirmed_empty")
    return meetings


def _source_identity(portal_url: str) -> tuple[str, str, str]:
    parsed = urlparse(portal_url)
    portal_host = (parsed.hostname or "").lower()
    suffix = ".portal.civicclerk.com"
    if parsed.scheme != "https" or not portal_host.endswith(suffix):
        raise ValueError(f"CivicClerk source URL is not an official HTTPS portal: {portal_url!r}")
    tenant = portal_host[: -len(suffix)]
    if not tenant or "." in tenant or not re.fullmatch(r"[a-z0-9-]+", tenant):
        raise ValueError(f"CivicClerk source URL has invalid tenant host: {portal_url!r}")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"CivicClerk source URL must be the tenant portal root: {portal_url!r}")
    return tenant, portal_host, f"{tenant}.api.civicclerk.com"


def _fetch_current_events(
    session,
    *,
    api_url: str,
    api_host: str,
    floor: date,
    city_label: str,
) -> _FetchResult:
    filter_fields: tuple[str | None, ...] = ("startDateTime", "eventDate", None)
    request_count = 0
    for filter_field in filter_fields:
        params = {
            "$orderby": (
                f"{filter_field} asc" if filter_field is not None else "eventDate desc"
            ),
            "$top": str(MAX_EVENTS),
            "$format": "json",
        }
        if filter_field:
            params["$filter"] = f"{filter_field} ge {floor.isoformat()}T00:00:00Z"
        page_url = api_url
        page_params = params
        events: list[Any] = []
        for page_number in range(1, MAX_REQUESTS + 1):
            request_count += 1
            if request_count > MAX_REQUESTS:
                raise ValueError(
                    f"{city_label} CivicClerk exceeded the {MAX_REQUESTS}-request hard cap"
                )
            status, final_url, body = _fetch_bounded(
                session,
                page_url,
                api_host,
                page_params,
            )
            if status in BLOCKED_STATUSES:
                logger.warning(
                    "%s CivicClerk source blocked paced request: status=%d url=%s request=%d",
                    city_label,
                    status,
                    final_url,
                    request_count,
                )
                return _FetchResult([], authoritative_window=False, source_blocked=True)
            if status == 400 and filter_field is not None and page_number == 1:
                logger.warning(
                    "%s CivicClerk tenant rejected OData field=%s; trying next bounded query shape",
                    city_label,
                    filter_field,
                )
                break
            if status != 200:
                raise RuntimeError(f"{city_label} CivicClerk API returned HTTP {status}: {final_url}")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{city_label} CivicClerk API returned invalid JSON") from exc
            page_events, next_link = _validate_payload(payload, api_host, city_label)
            if len(events) + len(page_events) > MAX_EVENTS:
                raise ValueError(
                    f"{city_label} CivicClerk row cap exceeded: "
                    f"{len(events) + len(page_events)} > {MAX_EVENTS}"
                )
            events.extend(page_events)
            logger.info(
                "%s CivicClerk page accepted: filter_field=%s page=%d page_rows=%d "
                "total_rows=%d request=%d/%d",
                city_label,
                filter_field or "none",
                page_number,
                len(page_events),
                len(events),
                request_count,
                MAX_REQUESTS,
            )
            if not next_link:
                if filter_field is None:
                    events = _current_rows_from_unfiltered_payload(
                        events,
                        floor=floor,
                        city_label=city_label,
                    )
                return _FetchResult(events, authoritative_window=filter_field is not None)
            if len(events) >= MAX_EVENTS:
                raise ValueError(
                    f"{city_label} CivicClerk pagination reached the {MAX_EVENTS}-row cap "
                    "with another page still advertised"
                )
            page_url = _pagination_url(next_link, api_host, city_label)
            page_params = {}
        else:
            raise ValueError(
                f"{city_label} CivicClerk pagination exceeded the {MAX_REQUESTS}-page hard cap"
            )
    raise RuntimeError(f"{city_label} CivicClerk exhausted the {MAX_REQUESTS}-request query cap")


def _fetch_bounded(
    session,
    url: str,
    api_host: str,
    params: dict[str, str],
) -> tuple[int, str, str]:
    with session.get(
        url,
        params=params,
        timeout=(10, 30),
        stream=True,
        allow_redirects=True,
    ) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != api_host:
            raise ValueError(f"CivicClerk redirect reached disallowed host: {final_host!r}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"CivicClerk response exceeded {MAX_RESPONSE_BYTES} bytes: {url}")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _validate_payload(payload: Any, api_host: str, city_label: str) -> tuple[list[Any], str]:
    if not isinstance(payload, dict):
        raise ValueError(f"{city_label} CivicClerk fingerprint drifted: payload={type(payload).__name__}")
    context = _clean(payload.get("@odata.context"))
    expected_context = f"https://{api_host}/v1/$metadata#events"
    if context.casefold() != expected_context.casefold():
        raise ValueError(f"{city_label} CivicClerk fingerprint drifted: context={context!r}")
    events = payload.get("value")
    if not isinstance(events, list):
        raise ValueError(f"{city_label} CivicClerk fingerprint drifted: value is not a list")
    if len(events) > MAX_EVENTS:
        raise ValueError(f"{city_label} CivicClerk page row cap exceeded: {len(events)} > {MAX_EVENTS}")
    next_link = _clean(payload.get("@odata.nextLink"))
    logger.info("%s CivicClerk fingerprint witnessed: context=%s rows=%d", city_label, context, len(events))
    return events, next_link


def _pagination_url(value: str, api_host: str, city_label: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != api_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.casefold().rstrip("/") != "/v1/events"
        or parsed.fragment
    ):
        raise ValueError(f"{city_label} CivicClerk advertised an unsafe nextLink: {value!r}")
    return value


def _current_rows_from_unfiltered_payload(
    events: list[Any],
    *,
    floor: date,
    city_label: str,
) -> list[Any]:
    """Prove that a rejected-filter fallback is a complete current tail.

    CivicClerk tenants that reject OData filters historically return a finite,
    date-ordered ``value`` list.  A fallback is trustworthy only when it ends
    below the current-month floor (proving the current tail is present) or the
    API explicitly returns fewer than the hard row cap.  A full capped page
    whose oldest row is still current could conceal later rows and fails loud.
    """
    dated: list[tuple[date, Any]] = []
    for position, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            dated.append((date.max, event))
            continue
        raw = _clean(event.get("startDateTime") or event.get("eventDate"))
        if not raw:
            raise ValueError(
                f"{city_label} CivicClerk unfiltered fallback row lacks datetime: position={position}"
            )
        try:
            parsed = datetime.fromisoformat(raw[:-1] if raw.endswith("Z") else raw).date()
        except ValueError as exc:
            raise ValueError(
                f"{city_label} CivicClerk unfiltered fallback row has invalid datetime: "
                f"position={position} value={raw!r}"
            ) from exc
        dated.append((parsed, event))
    if len(dated) == MAX_EVENTS and all(meeting_day >= floor for meeting_day, _ in dated):
        raise ValueError(
            f"{city_label} CivicClerk unfiltered fallback filled the {MAX_EVENTS}-row cap "
            "without crossing the month floor"
        )
    current = [event for meeting_day, event in dated if meeting_day >= floor]
    logger.warning(
        "%s CivicClerk used bounded unfiltered fallback: rows=%d current_rows=%d floor=%s",
        city_label,
        len(events),
        len(current),
        floor.isoformat(),
    )
    return current


def _classify_title(
    title: str,
    *,
    governing_body_phrase: str,
    exact_allowed_titles: frozenset[str],
) -> str:
    normalized = _normalize_title(title)
    if not normalized:
        return "ambiguous"
    if normalized in exact_allowed_titles:
        return "allow"
    padded = f" {normalized} "
    if f" {governing_body_phrase} " in padded:
        if NON_MEETING_COUNCIL_RE.search(normalized):
            return "ambiguous"
        if normalized == governing_body_phrase or MEETING_SIGNAL_RE.search(normalized):
            return "allow"
        return "ambiguous"
    if " council " in padded or normalized.startswith("council ") or normalized.endswith(" council"):
        return "ambiguous"
    if KNOWN_OTHER_BODY_RE.search(normalized):
        return "other"
    return "ambiguous"


def _classify_event_scope(
    event: dict[str, Any],
    title: str,
    *,
    governing_body_phrase: str,
    exact_allowed_titles: frozenset[str],
) -> tuple[str, tuple[str, ...]]:
    """Cross-check title scope against CivicClerk's explicit body category."""
    title_scope = _classify_title(
        title,
        governing_body_phrase=governing_body_phrase,
        exact_allowed_titles=exact_allowed_titles,
    )
    categories = tuple(
        dict.fromkeys(
            normalized
            for key in ("eventCategoryName", "categoryName")
            if (normalized := _normalize_title(_clean(event.get(key))))
        )
    )
    if not categories:
        return title_scope, categories
    category_allows = governing_body_phrase in categories
    if category_allows:
        if title_scope == "other":
            return "ambiguous", categories
        if title_scope == "allow" or MEETING_SIGNAL_RE.search(_normalize_title(title)):
            return "allow", categories
        return "ambiguous", categories
    if title_scope == "allow":
        return "ambiguous", categories
    return "other", categories


def _event_datetime(event: dict[str, Any], row_id: str, city_label: str) -> tuple[str, str]:
    raw = _clean(event.get("startDateTime") or event.get("eventDate"))
    if not raw:
        raise ValueError(f"{city_label} CivicClerk event lacks datetime: id={row_id}")
    try:
        parsed = datetime.fromisoformat(raw[:-1] if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise ValueError(f"{city_label} CivicClerk event has invalid datetime: id={row_id} value={raw!r}") from exc
    return parsed.date().isoformat(), parsed.strftime("%I:%M %p").lstrip("0")


def _documents(
    value: Any,
    *,
    api_root: str,
    allowed_hosts: set[str],
    city_label: str,
    row_id: str,
    stats: Counter[str],
) -> dict[str, str]:
    result = {field: "" for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url")}
    if value in (None, ""):
        stats["published_files_absent"] += 1
        return result
    if not isinstance(value, list):
        raise ValueError(f"{city_label} CivicClerk publishedFiles is not a list: id={row_id}")
    for position, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            stats["documents_dropped_non_object"] += 1
            logger.warning(
                "%s CivicClerk document dropped: id=%s position=%d reason=non_object",
                city_label,
                row_id,
                position,
            )
            continue
        document_type = _normalize_title(_clean(item.get("type")))
        field = DOCUMENT_FIELDS.get(document_type)
        if field is None:
            stats["documents_dropped_unmapped_type"] += 1
            logger.warning(
                "%s CivicClerk document dropped: id=%s position=%d reason=unmapped_type type=%r url=%r",
                city_label,
                row_id,
                position,
                item.get("type"),
                item.get("url"),
            )
            continue
        raw_url = item.get("url") or item.get("streamUrl")
        emitted = _safe_url(raw_url, api_root, allowed_hosts, city_label, row_id, field)
        if not emitted:
            continue
        if result[field]:
            stats["documents_dropped_duplicate_field"] += 1
            logger.warning(
                "%s CivicClerk document dropped: id=%s reason=duplicate_field field=%s kept=%r dropped=%r",
                city_label,
                row_id,
                field,
                result[field],
                emitted,
            )
            continue
        result[field] = emitted
    return result


def _event_url(
    event: dict[str, Any],
    keys: tuple[str, ...],
    *,
    field: str,
    api_root: str,
    allowed_hosts: set[str],
    city_label: str,
    row_id: str,
    stats: Counter[str],
) -> str:
    for key in keys:
        raw = event.get(key)
        if raw in (None, ""):
            continue
        emitted = _safe_url(raw, api_root, allowed_hosts, city_label, row_id, field)
        if emitted:
            return emitted
    stats[f"{field}_absent"] += 1
    return ""


def _safe_url(
    value: Any,
    base_url: str,
    allowed_hosts: set[str],
    city_label: str,
    row_id: str,
    field: str,
) -> str:
    raw = "" if value in (None, "") else str(value).strip()
    if not raw:
        logger.warning("%s CivicClerk URL dropped: id=%s field=%s reason=empty", city_label, row_id, field)
        return ""
    lowered = raw.casefold()
    if lowered.startswith(("//", "javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:")):
        logger.warning(
            "%s CivicClerk URL dropped: id=%s field=%s reason=scheme rejected=%r",
            city_label,
            row_id,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in allowed_hosts:
        logger.warning(
            "%s CivicClerk URL dropped: id=%s field=%s reason=host rejected=%r",
            city_label,
            row_id,
            field,
            raw,
        )
        return ""
    return absolute


def _location(value: Any, stats: Counter[str]) -> str:
    if value in (None, ""):
        stats["meeting_location_absent"] += 1
        return ""
    if isinstance(value, str):
        cleaned = _clean(value)
        if not cleaned:
            stats["meeting_location_absent"] += 1
        return cleaned
    if not isinstance(value, dict):
        stats["meeting_location_invalid"] += 1
        return ""
    parts: list[str] = []
    for key in ("name", "address1", "address2", "city", "state", "zipCode"):
        part = _clean(value.get(key))
        if part and part.casefold() not in {seen.casefold() for seen in parts}:
            parts.append(part)
    if not parts:
        stats["meeting_location_absent"] += 1
    return ", ".join(parts)


def _status(title: str, documents: dict[str, str]) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if documents["minutes_url"]:
        return "Minutes Available"
    if documents["agenda_url"] or documents["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _validate_meeting(meeting: dict[str, str], city_label: str) -> None:
    if tuple(meeting) != FIELDS:
        raise ValueError(f"{city_label} CivicClerk schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise TypeError(f"{city_label} CivicClerk emitted a non-string field")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    raw = str(value)
    if "<" in raw or ">" in raw:
        raw = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return " ".join(unescape(raw).split())


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

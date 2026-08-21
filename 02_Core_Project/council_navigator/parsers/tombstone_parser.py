import json
import logging
import re
from collections import Counter
from datetime import date
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://cityoftombstoneaz.gov/wp-json/"
SITE_HOST = "cityoftombstoneaz.gov"
ALLOWED_FETCH_HOSTS = {SITE_HOST}
ALLOWED_EMIT_HOSTS = {
    SITE_HOST,
    "cdn.townweb.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
}
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
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
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"(?<!\d)([0-9]{1,2})(?::([0-9]{2}))?\s*([AP])\.?M\.?(?=\s|$|[^\w.])", re.IGNORECASE)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
MEDIA_ID_RE = re.compile(r"^\d+$")
MAX_RESPONSE_BYTES = 10_000_000
REQUEST_TIMEOUT = 30
MAX_MEDIA_BATCHES = 2
MEDIA_BATCH_SIZE = 100
BLOCKING_HTTP_STATUSES = {401, 403, 407, 423, 429, 451}


def _is_witnessed_source_blocker(exc: requests.HTTPError) -> bool:
    response = exc.response
    return response is not None and response.status_code in BLOCKING_HTTP_STATUSES


def _log_witnessed_source_blocker(*, phase: str, exc: requests.HTTPError) -> None:
    response = exc.response
    status_code = response.status_code if response is not None else 0
    final_url = response.url if response is not None else ""
    logger.warning("health_empty_kind=source_blocked")
    logger.warning(
        "source_blocked phase=%s status=%s final_url=%s failure_shape=honest-empty "
        "missing_data_scope=all_current_month_forward_meetings",
        phase,
        status_code,
        final_url,
    )


def scrape_calendar(url: str) -> list[dict[str, str]]:
    """Scrape current-month-forward Tombstone council meetings from TownWeb."""
    api_base = _api_base(url or DEFAULT_URL)
    session = make_session()
    counters: Counter[str] = Counter()
    current_floor = date.today().replace(day=1).isoformat()

    logger.warning(
        "field_absence field=meeting_location reason=twd_meeting_repository_schema_exposes_no_per_row_location_signal"
    )
    logger.warning("field_absence field=ecomment_url reason=twd_meeting_repository_schema_exposes_no_ecomment_signal")

    try:
        category_id = _discover_city_council_category(session, api_base)
    except requests.HTTPError as exc:
        if not _is_witnessed_source_blocker(exc):
            raise
        _log_witnessed_source_blocker(phase="category_discovery", exc=exc)
        return []

    first_page_url = _repository_url(api_base, category_id, page=1, after=current_floor)
    try:
        first_page, first_headers = _fetch_json_bounded_with_retries(session, first_page_url, allowed_hosts=ALLOWED_FETCH_HOSTS)
    except requests.HTTPError as exc:
        if not _is_witnessed_source_blocker(exc):
            raise
        _log_witnessed_source_blocker(phase="repository_page_1", exc=exc)
        return []

    if not isinstance(first_page, list):
        raise ValueError(
            "Tombstone repository page 1 returned an unexpected "
            f"{type(first_page).__name__} payload"
        )

    if not first_page:
        raw_total = str(first_headers.get("X-WP-Total", "") or "").strip()
        if raw_total != "0":
            raise ValueError(
                "Tombstone repository returned an empty list without "
                f"X-WP-Total=0 (observed {raw_total!r})"
            )
        logger.info(
            "vendor_fingerprint witness=townweb_repository_and_city_council_taxonomy "
            "api_base=%s category_id=%s current_floor=%s first_page_rows=0 x_wp_total=0",
            api_base,
            category_id,
            current_floor,
        )
        counters["pages_fetched"] += 1
        logger.warning("health_empty_kind=confirmed_empty")
        logger.info("scrape_summary counters=%s", dict(sorted(counters.items())))
        return []

    total_pages = _parse_total_pages(first_headers)
    logger.info(
        "vendor_fingerprint witness=townweb_repository_and_city_council_taxonomy api_base=%s category_id=%s current_floor=%s first_page_rows=%d x_wp_total=%r x_wp_totalpages=%d",
        api_base,
        category_id,
        current_floor,
        len(first_page),
        first_headers.get("X-WP-Total", ""),
        total_pages,
    )
    counters["pages_fetched"] += 1
    if total_pages > 1:
        logger.warning(
            "architectural_drift reason=current_window_exceeds_single_bounded_page current_floor=%s total_pages=%d page_size=100",
            current_floor,
            total_pages,
        )
        raise ValueError("Tombstone current-month repository exceeds the bounded one-page contract")

    posts: list[dict[str, Any]] = []
    for post in first_page:
        counters["rows_seen"] += 1
        if not isinstance(post, dict):
            counters["rows_dropped_unexpected_shape"] += 1
            logger.warning("drop_row_unexpected_shape shape=%s value=%r", type(post).__name__, post)
            continue

        post_id = str(post.get("id", "") or "")
        title = _clean_text(_nested_rendered_title(post.get("title")))
        row_label = f"post_id={post_id} title={title!r}"
        if not title:
            counters["rows_dropped_empty_title"] += 1
            logger.warning("drop_row_empty_title post_id=%s", post_id)
            continue
        if str(post.get("status", "") or "") != "publish":
            counters["rows_dropped_unexpected_wp_status"] += 1
            logger.warning("drop_row_unexpected_wp_status %s wp_status=%r", row_label, post.get("status", ""))
            continue

        meeting_date = str(post.get("meeting_date", "") or "").strip()
        if not DATE_RE.fullmatch(meeting_date):
            counters["rows_dropped_invalid_date"] += 1
            logger.warning("drop_row_invalid_meeting_date %s raw_date=%r", row_label, meeting_date)
            continue
        if meeting_date < current_floor:
            counters["rows_dropped_before_current_floor"] += 1
            logger.warning(
                "drop_row_before_current_floor %s meeting_date=%s current_floor=%s",
                row_label,
                meeting_date,
                current_floor,
            )
            continue
        posts.append(post)

    if not posts:
        raise ValueError(
            "Tombstone repository returned rows but none satisfied the validated "
            f"current-window row contract; counters={dict(sorted(counters.items()))}"
        )

    media_map = _fetch_media_map(session, api_base, posts, counters)

    meetings: list[dict[str, str]] = []
    for post in posts:
        post_id = str(post.get("id", "") or "")
        title = _clean_text(_nested_rendered_title(post.get("title")))
        meeting_date = str(post.get("meeting_date", "") or "").strip()
        agenda_url = _resolve_media_from_map(
            post.get("agenda", ""), media_map,
            field="agenda_url",
            post_id=post_id,
            title=title,
            counters=counters,
        )
        minutes_url = _resolve_media_from_map(
            post.get("meeting_minutes", ""), media_map,
            field="minutes_url",
            post_id=post_id,
            title=title,
            counters=counters,
        )
        agenda_packet_url = _resolve_media_from_map(
            post.get("agenda_pack", ""), media_map,
            field="agenda_packet_url",
            post_id=post_id,
            title=title,
            counters=counters,
        )
        video_url = _resolve_url_or_media_from_map(
            post.get("video", ""), api_base, media_map,
            field="video_url",
            post_id=post_id,
            title=title,
            counters=counters,
        )

        additional_url = str(post.get("additional_url", "") or "").strip()
        if additional_url:
            counters["additional_urls_not_emitted"] += 1
            logger.warning(
                "drop_additional_url_no_canonical_field post_id=%s title=%r raw_value=%r",
                post_id,
                title,
                additional_url,
            )

        status = _derive_status(title, agenda_url, minutes_url, agenda_packet_url)
        counters[f"status_{status.replace(' ', '_').lower()}"] += 1

        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": _extract_time(title, post_id),
            "meeting_location": "",
            "meeting_status": status,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
            "agenda_packet_url": agenda_packet_url,
            "ecomment_url": "",
            "meeting_id": post_id,
        }
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})
        counters["rows_accepted"] += 1

    _assert_schema(meetings)
    logger.info("scrape_summary counters=%s", dict(sorted(counters.items())))
    return meetings


def _discover_city_council_category(session: requests.Session, api_base: str) -> str:
    category_url = urljoin(
        api_base,
        "wp/v2/twd_repository_cat?slug=city-council&per_page=1&_fields=id,slug,name,count",
    )
    categories, _headers = _fetch_json_bounded_with_retries(
        session,
        category_url,
        allowed_hosts=ALLOWED_FETCH_HOSTS,
    )

    if not isinstance(categories, list):
        raise ValueError(
            "Tombstone taxonomy endpoint returned an unexpected "
            f"{type(categories).__name__} payload"
        )

    slug_match = None
    name_match = None
    for category in categories:
        if not isinstance(category, dict):
            logger.warning("skip_category_unexpected_shape shape=%s value=%r", type(category).__name__, category)
            continue

        slug = str(category.get("slug", "") or "")
        name = str(category.get("name", "") or "")
        if slug == "city-council":
            slug_match = category
            break
        if name.casefold() == "city council":
            name_match = category

    selected = slug_match or name_match
    if not selected:
        raise ValueError(
            "Tombstone taxonomy endpoint did not expose the City Council category "
            f"across {len(categories)} rows"
        )

    category_id = str(selected.get("id", "") or "")
    if not MEDIA_ID_RE.fullmatch(category_id):
        raise ValueError(
            f"Tombstone City Council category id was not numeric: {selected!r}"
        )

    logger.info(
        "category_discovered id=%s slug=%r name=%r count=%r",
        category_id,
        selected.get("slug", ""),
        selected.get("name", ""),
        selected.get("count", ""),
    )
    return category_id


def _repository_url(api_base: str, category_id: str, *, page: int, after: str) -> str:
    query = urlencode(
        {
            "twd_repository_cat": category_id,
            "per_page": 100,
            "page": page,
            "after": f"{after}T00:00:00",
            "orderby": "date",
            "order": "asc",
            "_fields": (
                "id,date,status,title,meeting_date,agenda,meeting_minutes,"
                "agenda_pack,video,additional_url"
            ),
        }
    )
    return urljoin(api_base, f"wp/v2/twd_repository?{query}")


def _fetch_media_map(
    session: requests.Session,
    api_base: str,
    posts: list[dict[str, Any]],
    counters: Counter[str],
) -> dict[str, str]:
    media_ids: list[str] = []
    for post in posts:
        for field in ("agenda", "meeting_minutes", "agenda_pack", "video"):
            raw = post.get(field, "")
            if isinstance(raw, dict):
                direct = str(raw.get("source_url", raw.get("url", raw.get("guid", ""))) or "").strip()
                if direct:
                    continue
                value = str(raw.get("ID", raw.get("id", "")) or "").strip()
            else:
                value = str(raw or "").strip()
            if value and MEDIA_ID_RE.fullmatch(value) and value not in media_ids:
                media_ids.append(value)

    maximum = MAX_MEDIA_BATCHES * MEDIA_BATCH_SIZE
    if len(media_ids) > maximum:
        logger.warning(
            "media_resolution_capped total_ids=%d cap=%d dropped_ids=%r",
            len(media_ids),
            maximum,
            media_ids[maximum:maximum + 10],
        )
        counters["media_ids_dropped_by_cap"] += len(media_ids) - maximum
        media_ids = media_ids[:maximum]

    resolved: dict[str, str] = {}
    for batch_number, start in enumerate(range(0, len(media_ids), MEDIA_BATCH_SIZE), start=1):
        batch = media_ids[start:start + MEDIA_BATCH_SIZE]
        query = urlencode(
            {
                "include": ",".join(batch),
                "per_page": len(batch),
                "_fields": "id,source_url",
            }
        )
        batch_url = urljoin(api_base, f"wp/v2/media?{query}")
        try:
            payload, _headers = _fetch_json_bounded_with_retries(
                session,
                batch_url,
                allowed_hosts=ALLOWED_FETCH_HOSTS,
            )
        except Exception as exc:
            logger.warning(
                "media_batch_fetch_failed batch=%d ids=%r url=%s error=%r",
                batch_number,
                batch[:10],
                batch_url,
                exc,
            )
            counters["media_batches_failed"] += 1
            continue
        if not isinstance(payload, list):
            logger.warning(
                "media_batch_unexpected_shape batch=%d shape=%s",
                batch_number,
                type(payload).__name__,
            )
            counters["media_batches_unexpected_shape"] += 1
            continue
        counters["media_batches_fetched"] += 1
        for item in payload:
            if not isinstance(item, dict):
                logger.warning("media_item_unexpected_shape batch=%d value=%r", batch_number, item)
                continue
            media_id = str(item.get("id", "") or "")
            source_url = _emit_url(
                str(item.get("source_url", "") or ""),
                api_base,
                field="media_source_url",
                post_id="batch",
                title="Tombstone media batch",
            )
            if media_id and source_url:
                resolved[media_id] = source_url

    unresolved = [media_id for media_id in media_ids if media_id not in resolved]
    if unresolved:
        logger.warning(
            "media_ids_unresolved count=%d first_10=%r",
            len(unresolved),
            unresolved[:10],
        )
        counters["media_ids_unresolved"] += len(unresolved)
    return resolved


def _resolve_media_from_map(
    raw_value: Any,
    media_map: dict[str, str],
    *,
    field: str,
    post_id: str,
    title: str,
    counters: Counter[str],
) -> str:
    if isinstance(raw_value, dict):
        direct = str(raw_value.get("source_url", raw_value.get("guid", "")) or "").strip()
        if direct:
            return _emit_url(direct, DEFAULT_URL, field=field, post_id=post_id, title=title)
        value = str(raw_value.get("ID", raw_value.get("id", "")) or "").strip()
    else:
        value = str(raw_value or "").strip()
    if not value:
        counters[f"{field}_absent"] += 1
        return ""
    if not MEDIA_ID_RE.fullmatch(value):
        counters[f"{field}_dropped_unknown_shape"] += 1
        logger.warning(
            "drop_media_unknown_shape field=%s post_id=%s title=%r raw_value=%r",
            field,
            post_id,
            title,
            raw_value,
        )
        return ""
    resolved = media_map.get(value, "")
    if not resolved:
        counters[f"{field}_unresolved_media_id"] += 1
        logger.warning(
            "drop_unresolved_media_id field=%s post_id=%s title=%r media_id=%s",
            field,
            post_id,
            title,
            value,
        )
    return resolved


def _resolve_url_or_media_from_map(
    raw_value: Any,
    api_base: str,
    media_map: dict[str, str],
    *,
    field: str,
    post_id: str,
    title: str,
    counters: Counter[str],
) -> str:
    if isinstance(raw_value, dict):
        direct = str(raw_value.get("source_url", raw_value.get("url", raw_value.get("guid", ""))) or "").strip()
        if direct:
            return _emit_url(direct, api_base, field=field, post_id=post_id, title=title)
        raw_value = raw_value.get("ID", raw_value.get("id", ""))
    value = str(raw_value or "").strip()
    if not value:
        counters[f"{field}_absent"] += 1
        return ""
    if value.lower().startswith(("http://", "https://")):
        return _emit_url(value, api_base, field=field, post_id=post_id, title=title)
    return _resolve_media_from_map(
        value,
        media_map,
        field=field,
        post_id=post_id,
        title=title,
        counters=counters,
    )


def _parse_total_pages(headers: requests.structures.CaseInsensitiveDict[str]) -> int:
    raw_total_pages = str(headers.get("X-WP-TotalPages", "") or "").strip()
    if not raw_total_pages:
        logger.warning("pagination_header_missing header=X-WP-TotalPages defaulting_to_1")
        return 1

    try:
        total_pages = int(raw_total_pages)
    except ValueError:
        logger.warning("pagination_header_invalid header=X-WP-TotalPages value=%r defaulting_to_1", raw_total_pages)
        return 1

    if total_pages < 1:
        logger.warning("pagination_header_invalid header=X-WP-TotalPages value=%r defaulting_to_1", raw_total_pages)
        return 1
    return total_pages


def _resolve_media_id_field(
    session: requests.Session,
    api_base: str,
    raw_value: Any,
    *,
    field: str,
    post_id: str,
    title: str,
    media_cache: dict[str, str],
    counters: Counter[str],
) -> str:
    if raw_value is None:
        value = ""
    elif isinstance(raw_value, dict):
        attachment_id = str(raw_value.get("ID", raw_value.get("id", "")) or "").strip()
        if attachment_id and MEDIA_ID_RE.fullmatch(attachment_id):
            counters[f"{field}_media_object_shape"] += 1
            logger.info("media_object_branch field=%s post_id=%s title=%r media_id=%s", field, post_id, title, attachment_id)
            return _resolve_media_source_url(
                session,
                api_base,
                attachment_id,
                field=field,
                post_id=post_id,
                title=title,
                media_cache=media_cache,
                counters=counters,
            )

        guid = str(raw_value.get("source_url", raw_value.get("guid", "")) or "").strip()
        if guid:
            counters[f"{field}_media_object_direct_url_shape"] += 1
            logger.info("media_object_direct_url_branch field=%s post_id=%s title=%r raw_value=%r", field, post_id, title, guid)
            return _emit_url(guid, api_base, field=field, post_id=post_id, title=title)

        counters[f"{field}_dropped_media_object_missing_id_url"] += 1
        logger.warning("drop_media_object_missing_id_url field=%s post_id=%s title=%r raw_value=%r", field, post_id, title, raw_value)
        return ""
    else:
        value = str(raw_value or "").strip()

    if not value:
        counters[f"{field}_absent"] += 1
        return ""
    if not MEDIA_ID_RE.fullmatch(value):
        counters[f"{field}_dropped_non_numeric_media_id"] += 1
        logger.warning("drop_media_non_numeric_id field=%s post_id=%s title=%r raw_value=%r", field, post_id, title, raw_value)
        return ""

    logger.info("media_id_branch field=%s post_id=%s title=%r media_id=%s", field, post_id, title, value)
    return _resolve_media_source_url(
        session,
        api_base,
        value,
        field=field,
        post_id=post_id,
        title=title,
        media_cache=media_cache,
        counters=counters,
    )


def _resolve_url_or_media_field(
    session: requests.Session,
    api_base: str,
    raw_value: Any,
    *,
    field: str,
    post_id: str,
    title: str,
    media_cache: dict[str, str],
    counters: Counter[str],
) -> str:
    if raw_value is None:
        value = ""
    elif isinstance(raw_value, dict):
        attachment_id = str(raw_value.get("ID", raw_value.get("id", "")) or "").strip()
        if attachment_id and MEDIA_ID_RE.fullmatch(attachment_id):
            counters[f"{field}_media_object_shape"] += 1
            logger.info("media_object_branch field=%s post_id=%s title=%r media_id=%s", field, post_id, title, attachment_id)
            return _resolve_media_source_url(
                session,
                api_base,
                attachment_id,
                field=field,
                post_id=post_id,
                title=title,
                media_cache=media_cache,
                counters=counters,
            )

        direct_url = str(raw_value.get("source_url", raw_value.get("url", raw_value.get("guid", ""))) or "").strip()
        if direct_url:
            counters[f"{field}_media_object_direct_url_shape"] += 1
            logger.info("media_object_direct_url_branch field=%s post_id=%s title=%r raw_value=%r", field, post_id, title, direct_url)
            return _emit_url(direct_url, api_base, field=field, post_id=post_id, title=title)

        counters[f"{field}_dropped_media_object_missing_id_url"] += 1
        logger.warning("drop_url_or_media_object_missing_id_url field=%s post_id=%s title=%r raw_value=%r", field, post_id, title, raw_value)
        return ""
    else:
        value = str(raw_value or "").strip()

    if not value:
        counters[f"{field}_absent"] += 1
        return ""

    if value.lower().startswith(("http://", "https://")):
        counters[f"{field}_direct_url_shape"] += 1
        logger.info("direct_url_branch field=%s post_id=%s title=%r raw_value=%r", field, post_id, title, raw_value)
        return _emit_url(value, api_base, field=field, post_id=post_id, title=title)

    if MEDIA_ID_RE.fullmatch(value):
        counters[f"{field}_media_id_shape"] += 1
        logger.info("media_id_branch field=%s post_id=%s title=%r media_id=%s", field, post_id, title, value)
        return _resolve_media_source_url(
            session,
            api_base,
            value,
            field=field,
            post_id=post_id,
            title=title,
            media_cache=media_cache,
            counters=counters,
        )

    counters[f"{field}_dropped_unknown_shape"] += 1
    logger.warning("drop_url_or_media_unknown_shape field=%s post_id=%s title=%r raw_value=%r", field, post_id, title, raw_value)
    return ""


def _resolve_media_source_url(
    session: requests.Session,
    api_base: str,
    media_id: str,
    *,
    field: str,
    post_id: str,
    title: str,
    media_cache: dict[str, str],
    counters: Counter[str],
) -> str:
    if media_id in media_cache:
        counters[f"{field}_media_cache_hit"] += 1
        return media_cache[media_id]

    media_url = urljoin(api_base, f"wp/v2/media/{media_id}")
    try:
        media_data, _headers = _fetch_json_bounded_with_retries(session, media_url, allowed_hosts=ALLOWED_FETCH_HOSTS)
    except Exception as exc:
        counters[f"{field}_media_fetch_failed"] += 1
        logger.warning(
            "drop_media_fetch_failed field=%s post_id=%s title=%r media_id=%s url=%s error=%r",
            field,
            post_id,
            title,
            media_id,
            media_url,
            exc,
        )
        media_cache[media_id] = ""
        return ""

    if not isinstance(media_data, dict):
        counters[f"{field}_media_unexpected_shape"] += 1
        logger.warning(
            "drop_media_unexpected_shape field=%s post_id=%s title=%r media_id=%s shape=%s",
            field,
            post_id,
            title,
            media_id,
            type(media_data).__name__,
        )
        media_cache[media_id] = ""
        return ""

    source_url = str(media_data.get("source_url", "") or "").strip()
    if not source_url:
        counters[f"{field}_media_missing_source_url"] += 1
        logger.warning("drop_media_missing_source_url field=%s post_id=%s title=%r media_id=%s", field, post_id, title, media_id)
        media_cache[media_id] = ""
        return ""

    emitted = _emit_url(source_url, api_base, field=field, post_id=post_id, title=title)
    if emitted:
        counters[f"{field}_media_resolved"] += 1
    media_cache[media_id] = emitted
    return emitted


def _emit_url(href: str, base_url: str, *, field: str, post_id: str, title: str) -> str:
    value = href.strip()
    if not value:
        return ""

    lowered = value.lower()
    if lowered.startswith(BAD_SCHEMES) or value == "#":
        logger.warning("drop_url_bad_scheme field=%s post_id=%s title=%r href=%r", field, post_id, title, href)
        return ""

    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning("drop_url_bad_scheme field=%s post_id=%s title=%r href=%r absolute=%r", field, post_id, title, href, absolute)
        return ""

    host = _host(absolute)
    if not _host_allowed(host, ALLOWED_EMIT_HOSTS):
        logger.warning(
            "drop_url_disallowed_host field=%s post_id=%s title=%r href=%r host=%r allowed=%r",
            field,
            post_id,
            title,
            href,
            host,
            sorted(ALLOWED_EMIT_HOSTS),
        )
        return ""

    return absolute


def _derive_status(title: str, agenda_url: str, minutes_url: str, agenda_packet_url: str) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _extract_time(title: str, post_id: str) -> str:
    match = TIME_RE.search(title[:500])
    if not match:
        logger.info("meeting_time absent in title post_id=%s title=%r", post_id, title)
        return ""
    hour = int(match.group(1))
    if not 1 <= hour <= 12:
        logger.warning(
            "meeting_time_dropped_invalid_hour post_id=%s title=%r raw=%r",
            post_id,
            title,
            match.group(0),
        )
        return ""
    minute = match.group(2) or "00"
    return f"{hour}:{minute} {match.group(3).upper()}M"


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != CANONICAL_FIELDS:
            raise ValueError(f"Row {index} schema mismatch: {tuple(meeting)}")
        for field, value in meeting.items():
            if not isinstance(value, str):
                raise ValueError(f"Row {index} field {field} is not str: {type(value).__name__}")
        for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
            value = meeting[field]
            if value and not value.startswith(("http://", "https://")):
                raise ValueError(f"Row {index} field {field} has invalid URL: {value}")


def _fetch_json_bounded(
    session: requests.Session,
    url: str,
    *,
    allowed_hosts: set[str],
) -> tuple[Any, requests.structures.CaseInsensitiveDict[str]]:
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = _host(response.url)
        if not _host_allowed(final_host, allowed_hosts):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")

        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        return json.loads(text), response.headers


def _fetch_json_bounded_with_retries(
    session: requests.Session,
    url: str,
    *,
    allowed_hosts: set[str],
    attempts: int = 3,
) -> tuple[Any, requests.structures.CaseInsensitiveDict[str]]:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_json_bounded(session, url, allowed_hosts=allowed_hosts)
        except requests.HTTPError as exc:
            last_exc = exc
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code not in {429, 500, 502, 503, 504} or attempt == attempts:
                raise
            logger.warning("fetch_retry url=%s attempt=%d status_code=%s error=%r", url, attempt, status_code, exc)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt == attempts:
                raise
            logger.warning("fetch_retry url=%s attempt=%d error=%r", url, attempt, exc)

    if last_exc:
        raise last_exc
    raise RuntimeError(f"Fetch failed without exception: {url}")


def _api_base(url: str) -> str:
    parsed = urlparse(url or DEFAULT_URL)
    if not parsed.scheme or not parsed.netloc:
        parsed = urlparse(DEFAULT_URL)

    root = f"{parsed.scheme}://{parsed.netloc}/"
    if "/wp-json/" in parsed.path:
        prefix = parsed.path.split("/wp-json/", 1)[0].strip("/")
        if prefix:
            return urljoin(root, f"{prefix}/wp-json/")
        return urljoin(root, "wp-json/")
    return urljoin(root, "wp-json/")


def _nested_rendered_title(title_value: Any) -> str:
    if isinstance(title_value, dict):
        return str(title_value.get("rendered", "") or "")
    return str(title_value or "")


def _clean_text(value: Any) -> str:
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _host(url: str) -> str:
    return (urlparse(url).netloc.split(":")[0] or "").lower()


def _host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    scraped = scrape_calendar(DEFAULT_URL)
    print(json.dumps({"count": len(scraped), "samples": scraped[:5]}, indent=2))

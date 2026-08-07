import json
import logging
import re
import time
from collections import Counter
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


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
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
MEDIA_ID_RE = re.compile(r"^\d+$")
MAX_RESPONSE_BYTES = 10_000_000
REQUEST_TIMEOUT = 30


def scrape_calendar(url: str) -> list[dict[str, str]]:
    """Scrape Tombstone City Council meetings from TownWeb's WP REST repository."""
    api_base = _api_base(url or DEFAULT_URL)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
    counters: Counter[str] = Counter()
    media_cache: dict[str, str] = {}

    logger.warning(
        "field_absence fields=meeting_time,meeting_location reason=twd_meeting_repository_schema_exposes_no_per_row_time_or_location_signal"
    )
    logger.warning("field_absence field=ecomment_url reason=twd_meeting_repository_schema_exposes_no_ecomment_signal")

    category_id = _discover_city_council_category(session, api_base)
    if not category_id:
        logger.warning("architectural_blocker reason=city_council_category_not_discovered api_base=%s", api_base)
        return []

    first_page_url = _repository_url(api_base, category_id, page=1)
    try:
        first_page, first_headers = _fetch_json_bounded_with_retries(session, first_page_url, allowed_hosts=ALLOWED_FETCH_HOSTS)
    except Exception as exc:
        logger.warning("architectural_blocker reason=repository_page_1_fetch_failed url=%s error=%r", first_page_url, exc)
        return []

    if not isinstance(first_page, list):
        logger.warning(
            "architectural_blocker reason=repository_page_1_unexpected_shape url=%s shape=%s",
            first_page_url,
            type(first_page).__name__,
        )
        return []

    total_pages = _parse_total_pages(first_headers)
    logger.info(
        "vendor_fingerprint api_base=%s category_id=%s first_page_rows=%d x_wp_total=%r x_wp_totalpages=%d",
        api_base,
        category_id,
        len(first_page),
        first_headers.get("X-WP-Total", ""),
        total_pages,
    )

    posts = list(first_page)
    counters["pages_fetched"] += 1
    for page in range(2, total_pages + 1):
        page_url = _repository_url(api_base, category_id, page=page)
        try:
            page_data, _headers = _fetch_json_bounded_with_retries(session, page_url, allowed_hosts=ALLOWED_FETCH_HOSTS)
        except Exception as exc:
            counters["pages_failed"] += 1
            logger.warning("page_fetch_failed page=%d url=%s error=%r", page, page_url, exc)
            continue

        if not isinstance(page_data, list):
            counters["pages_unexpected_shape"] += 1
            logger.warning(
                "page_fetch_unexpected_shape page=%d url=%s shape=%s",
                page,
                page_url,
                type(page_data).__name__,
            )
            continue

        counters["pages_fetched"] += 1
        posts.extend(page_data)

    meetings: list[dict[str, str]] = []
    additional_url_drops: list[str] = []

    for post in posts:
        counters["rows_seen"] += 1
        if not isinstance(post, dict):
            counters["rows_dropped_unexpected_shape"] += 1
            logger.warning("drop_row_unexpected_shape shape=%s value=%r", type(post).__name__, post)
            continue

        post_id = str(post.get("id", "") or "")
        title = _clean_text(_nested_rendered_title(post.get("title")))
        row_label = f"post_id={post_id} title={title!r}"
        if not title:
            counters["rows_with_empty_title"] += 1
            logger.warning("empty_meeting_title post_id=%s emitted_empty=true", post_id)

        wp_status = str(post.get("status", "") or "")
        if wp_status != "publish":
            counters["rows_dropped_unexpected_wp_status"] += 1
            logger.warning("drop_row_unexpected_wp_status %s wp_status=%r", row_label, wp_status)
            continue

        meeting_date = str(post.get("meeting_date", "") or "").strip()
        if not DATE_RE.fullmatch(meeting_date):
            counters["rows_with_invalid_date"] += 1
            logger.warning("invalid_meeting_date %s raw_date=%r emitted_empty=true", row_label, meeting_date)
            meeting_date = ""

        agenda_url = _resolve_media_id_field(
            session,
            api_base,
            post.get("agenda", ""),
            field="agenda_url",
            post_id=post_id,
            title=title,
            media_cache=media_cache,
            counters=counters,
        )
        minutes_url = _resolve_media_id_field(
            session,
            api_base,
            post.get("meeting_minutes", ""),
            field="minutes_url",
            post_id=post_id,
            title=title,
            media_cache=media_cache,
            counters=counters,
        )
        agenda_packet_url = _resolve_media_id_field(
            session,
            api_base,
            post.get("agenda_pack", ""),
            field="agenda_packet_url",
            post_id=post_id,
            title=title,
            media_cache=media_cache,
            counters=counters,
        )
        video_url = _resolve_url_or_media_field(
            session,
            api_base,
            post.get("video", ""),
            field="video_url",
            post_id=post_id,
            title=title,
            media_cache=media_cache,
            counters=counters,
        )

        additional_url = _resolve_url_or_media_field(
            session,
            api_base,
            post.get("additional_url", ""),
            field="additional_url",
            post_id=post_id,
            title=title,
            media_cache=media_cache,
            counters=counters,
        )
        if additional_url:
            counters["additional_urls_not_emitted"] += 1
            additional_url_drops.append(f"{post_id} {title}: {additional_url}")
            logger.warning(
                "drop_additional_url_no_canonical_field post_id=%s title=%r resolved_url=%s",
                post_id,
                title,
                additional_url,
            )

        status = _derive_status(title, agenda_url, minutes_url, agenda_packet_url)
        counters[f"status_{status.replace(' ', '_').lower()}"] += 1

        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": "",
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

    if additional_url_drops:
        logger.warning(
            "additional_url_no_canonical_field count=%d first_10=%r",
            len(additional_url_drops),
            additional_url_drops[:10],
        )

    logger.info("scrape_summary counters=%s", dict(sorted(counters.items())))
    return meetings


def _discover_city_council_category(session: requests.Session, api_base: str) -> str:
    category_url = urljoin(api_base, "wp/v2/twd_repository_cat?per_page=100")
    try:
        categories, _headers = _fetch_json_bounded_with_retries(session, category_url, allowed_hosts=ALLOWED_FETCH_HOSTS)
    except Exception as exc:
        logger.warning("vendor_fingerprint_failed category_url=%s error=%r", category_url, exc)
        return ""

    if not isinstance(categories, list):
        logger.warning("vendor_fingerprint_failed category_url=%s shape=%s", category_url, type(categories).__name__)
        return ""

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
        logger.warning(
            "vendor_fingerprint_failed reason=city_council_category_missing category_count=%d",
            len(categories),
        )
        return ""

    category_id = str(selected.get("id", "") or "")
    if not MEDIA_ID_RE.fullmatch(category_id):
        logger.warning("vendor_fingerprint_failed reason=category_id_not_numeric category=%r", selected)
        return ""

    logger.info(
        "category_discovered id=%s slug=%r name=%r count=%r",
        category_id,
        selected.get("slug", ""),
        selected.get("name", ""),
        selected.get("count", ""),
    )
    return category_id


def _repository_url(api_base: str, category_id: str, *, page: int) -> str:
    return urljoin(api_base, f"wp/v2/twd_repository?twd_repository_cat={category_id}&per_page=100&page={page}")


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

        time.sleep(0.5 * attempt)

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

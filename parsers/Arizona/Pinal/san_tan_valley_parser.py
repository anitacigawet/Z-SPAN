import json
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_URL = "https://azleague.org/543/Town-of-San-Tan-Valley"
MAX_RESPONSE_BYTES = 10_000_000
FETCH_ALLOWED_HOSTS = {"azleague.org", "www.azleague.org"}
EMIT_ALLOWED_HOSTS = {
    "azleague.org",
    "www.azleague.org",
    "santanvalley.gov",
    "www.santanvalley.gov",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
}
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:")
URL_FIELDS = ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url")
SCHEMA_FIELDS = (
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
MONTHS = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}

DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"([0-9]{1,2}),\s+([0-9]{4})",
    re.IGNORECASE,
)
# Dotted suffix test cases: "5:30 a.m.", "5:30 p.m.", "5:30am", "5:30 AM".
TIME_RE = re.compile(r"([0-9]{1,2}):([0-9]{2})\s*([AP])\.?M\.?(?=\s|$|[^\w.])", re.IGNORECASE)
CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
AGENDA_RE = re.compile(r"\bagenda\b", re.IGNORECASE)
MINUTES_RE = re.compile(r"\bminutes?\b", re.IGNORECASE)
PACKET_RE = re.compile(r"\bpacket\b", re.IGNORECASE)
LOCATION_RE = re.compile(r"\b(?:location|located at|held at|address):\s*([^;\n\r]+)", re.IGNORECASE)
DOC_ID_RE = re.compile(r"/DocumentCenter/View/([0-9]+)(?:/|$)", re.IGNORECASE)

logger = logging.getLogger(__name__)


def _clean_text(value: object) -> str:
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())


def _fetch_text_bounded(session: requests.Session, url: str, max_bytes: int = MAX_RESPONSE_BYTES) -> str:
    """Fetch HTML with a hard response-size cap and final-host validation."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    with session.get(url, headers=headers, timeout=30, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = _host(response.url)
        if final_host not in FETCH_ALLOWED_HOSTS:
            logger.warning(
                "fetch dropped because redirect final host is disallowed: start_url=%s final_url=%s final_host=%s",
                url,
                response.url,
                final_host,
            )
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        body = b""
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body += chunk
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")
        return body.decode(response.encoding or "utf-8", errors="replace")


def _host(url: str) -> str:
    return (urlparse(url).netloc.split(":")[0] or "").lower()


def _validate_vendor_fingerprint(soup: BeautifulSoup) -> None:
    """Require a markup witness for the CivicPlus DocumentCenter surface."""
    html = str(soup)
    has_doccenter = "/DocumentCenter/View/" in html
    has_civicplus = "CivicPlus" in html
    if has_doccenter and has_civicplus:
        logger.info(
            "vendor fingerprint witness: CivicPlus byline plus DocumentCenter URL pattern /DocumentCenter/View/"
        )
        return
    if has_doccenter:
        logger.info("vendor fingerprint witness: DocumentCenter URL pattern /DocumentCenter/View/")
        return
    logger.warning("vendor fingerprint missing: no /DocumentCenter/View/ token found in page markup")
    raise ValueError("CivicPlus DocumentCenter surface not confirmed by markup")


def _emit_url(
    href: str,
    base_url: str,
    field_name: str,
    row_label: str,
    session: requests.Session,
    redirect_cache: dict[str, str],
) -> str:
    """Absolutize and validate one extracted URL before emitting it."""
    raw = (href or "").strip()
    if not raw:
        return ""

    lowered = raw.lower().lstrip()
    for bad_scheme in BAD_SCHEMES:
        if lowered.startswith(bad_scheme):
            logger.warning(
                "url rejected: row=%s field=%s href=%s reason=bad_scheme_%s",
                row_label,
                field_name,
                raw,
                bad_scheme.rstrip(":"),
            )
            return ""

    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "url rejected: row=%s field=%s href=%s absolute=%s reason=unsupported_scheme",
            row_label,
            field_name,
            raw,
            absolute,
        )
        return ""

    original_host = _host(absolute)
    if original_host not in EMIT_ALLOWED_HOSTS:
        logger.warning(
            "url rejected: row=%s field=%s url=%s reason=host_not_allowlisted host=%s",
            row_label,
            field_name,
            absolute,
            original_host,
        )
        return ""

    final_url = redirect_cache.get(absolute)
    if final_url is None:
        final_url = _validate_emit_redirect_host(absolute, field_name, row_label, session)
        redirect_cache[absolute] = final_url
    if not final_url:
        return ""
    return absolute


def _validate_emit_redirect_host(
    url: str,
    field_name: str,
    row_label: str,
    session: requests.Session,
) -> str:
    """GET, not HEAD, so CivicPlus DocumentCenter PDFs are not false-rejected."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Range": "bytes=0-0",
    }
    try:
        with session.get(url, headers=headers, timeout=30, stream=True, allow_redirects=True) as response:
            final_url = response.url
            final_host = _host(final_url)
            if final_host not in EMIT_ALLOWED_HOSTS:
                logger.warning(
                    "url dropped after redirect: row=%s field=%s url=%s final_url=%s final_host=%s",
                    row_label,
                    field_name,
                    url,
                    final_url,
                    final_host,
                )
                return ""
            response.raise_for_status()
            return final_url
    except requests.RequestException as exc:
        logger.warning(
            "url rejected: row=%s field=%s url=%s reason=get_validation_failed error=%s",
            row_label,
            field_name,
            url,
            exc,
        )
        return ""


def _find_agenda_section_start(soup: BeautifulSoup) -> object:
    heading = soup.find(
        lambda tag: tag.name == "h2" and "agenda and events" in _clean_text(tag).lower()
    )
    if heading:
        logger.info("section boundary witness: found h2 heading %r", _clean_text(heading))
        return heading
    logger.warning("Agenda and Events h2 section heading not found")
    raise ValueError("Agenda and Events section not found")


def _section_blocks(heading: object) -> list[tuple[str, object, list[object]]]:
    """Collect meeting-shaped blocks after the Agenda and Events heading."""
    blocks: list[tuple[str, object, list[object]]] = []
    siblings = list(heading.next_siblings)
    index = 0
    while index < len(siblings):
        node = siblings[index]
        if getattr(node, "name", None) is None:
            index += 1
            continue

        text = _clean_text(node)
        name = str(getattr(node, "name", "")).lower()
        if name == "h2":
            if _parse_date(text, f"h2:{text}"):
                content: list[object] = []
                index += 1
                while index < len(siblings):
                    follower = siblings[index]
                    if str(getattr(follower, "name", "")).lower() == "h2":
                        break
                    content.append(follower)
                    index += 1
                blocks.append(("h2", node, content))
                logger.info("accepted h2 meeting block candidate: title=%s", text)
                continue

            if blocks or text.lower() in {"presentations", "recruitments"}:
                logger.info("section boundary stop: h2=%r", text)
                break
            logger.warning("dropped h2 inside agenda section: title=%r reason=no_parseable_date", text)
            index += 1
            continue

        if name in {"p", "li", "ul", "ol", "div"}:
            if _block_has_meeting_signal(node):
                blocks.append(("row", node, []))
                logger.info("accepted paragraph/list meeting block candidate: text=%s", text[:160])
            elif text or node.find("a"):
                logger.warning(
                    "dropped agenda-section block: text=%r reason=no_meeting_date_signal",
                    text[:220],
                )
        index += 1

    logger.info("section block summary: blocks_seen=%s", len(blocks))
    return blocks


def _block_has_meeting_signal(node: object) -> bool:
    text = _clean_text(node)
    if not DATE_RE.search(text[:1500]):
        return False
    return bool(node.find("a", href=True))


def _parse_date(text: str, row_label: str) -> str:
    match = DATE_RE.search((text or "")[:1500])
    if not match:
        if text:
            logger.warning("meeting_date extraction returned empty: row=%s reason=no_month_day_year text=%r", row_label, text[:220])
        return ""
    month = MONTHS[match.group(1).lower()]
    day = int(match.group(2))
    year = match.group(3)
    return f"{year}-{month}-{day:02d}"


def _extract_time(text: str, row_label: str) -> str:
    capped = (text or "")[:1500]
    match = TIME_RE.search(capped)
    if not match:
        if "am" in capped.lower() or "pm" in capped.lower() or "a.m" in capped.lower() or "p.m" in capped.lower():
            logger.warning("meeting_time extraction returned empty: row=%s reason=unparsed_time_signal text=%r", row_label, capped[:220])
        else:
            logger.info("meeting_time absent by visible text: row=%s", row_label)
        return ""
    hour = int(match.group(1))
    minute = match.group(2)
    suffix = match.group(3).upper() + "M"
    if hour < 1 or hour > 12:
        logger.warning("meeting_time extraction returned empty: row=%s reason=hour_out_of_range text=%r", row_label, match.group(0))
        return ""
    return f"{hour}:{minute} {suffix}"


def _extract_location(text: str, row_label: str) -> str:
    capped = (text or "")[:1500]
    match = LOCATION_RE.search(capped)
    if not match:
        logger.info("meeting_location absent by visible text: row=%s", row_label)
        return ""
    location = _clean_text(match.group(1)).strip(" .")
    if not location:
        logger.warning("meeting_location extraction returned empty: row=%s reason=empty_location_after_label text=%r", row_label, capped[:220])
    return location


def _primary_anchors(block_kind: str, node: object, content: list[object]) -> list[object]:
    if block_kind == "h2":
        return [node]
    anchors = list(node.find_all("a", href=True))
    primaries = []
    for anchor in anchors:
        text = _clean_text(anchor)
        lowered = text.lower()
        if not DATE_RE.search(text):
            continue
        if MINUTES_RE.search(text) or "video" in lowered or "poll results" in lowered or "supporting document" in lowered:
            continue
        primaries.append(anchor)
    return primaries


def _row_title_from_primary(primary: object, block_text: str, row_label: str) -> str:
    source = _clean_text(primary) or block_text
    date_match = DATE_RE.search(source)
    if not date_match:
        logger.warning("meeting_title extraction returned empty: row=%s reason=no_date_in_primary text=%r", row_label, source[:220])
        return ""
    before_date = source[: date_match.start()].strip(" -\u2013\u2014")
    before_date = re.sub(r"\bAgenda\s*(?:Only)?\b\s*$", "", before_date, flags=re.IGNORECASE).strip(" -")
    before_date = re.sub(r"\bMeeting\s+Agenda\b\s*$", "Meeting", before_date, flags=re.IGNORECASE).strip(" -")
    if not before_date:
        logger.warning("meeting_title extraction returned empty: row=%s reason=empty_before_date text=%r", row_label, source[:220])
        return ""
    return before_date


def _row_link_scope(block_kind: str, node: object, content: list[object]) -> list[object]:
    nodes = [node] + content if block_kind == "h2" else [node]
    anchors = []
    for item in nodes:
        if hasattr(item, "find_all"):
            anchors.extend(item.find_all("a", href=True))
    return anchors


def _anchors_for_primary(
    block_kind: str,
    all_anchors: list[object],
    primaries: list[object],
    primary: object,
) -> list[object]:
    if block_kind == "h2" or len(primaries) == 1:
        return all_anchors
    primary_indexes = [index for index, anchor in enumerate(all_anchors) if any(anchor is item for item in primaries)]
    current_index = next((index for index, anchor in enumerate(all_anchors) if anchor is primary), -1)
    if current_index < 0:
        logger.warning(
            "link scope fell back to full block: primary=%r reason=primary_anchor_not_found",
            _clean_text(primary),
        )
        return all_anchors
    next_indexes = [index for index in primary_indexes if index > current_index]
    end_index = next_indexes[0] if next_indexes else len(all_anchors)
    scoped = all_anchors[current_index:end_index]
    logger.info(
        "link scope selected: primary=%r anchor_count=%s",
        _clean_text(primary),
        len(scoped),
    )
    return scoped


def _classify_link(anchor: object, block_text: str, row_title: str) -> str:
    href = anchor.get("href", "")
    host = _host(href)
    text = _clean_text(anchor)
    combined = f"{text} {block_text[:500]}"
    path = urlparse(href).path.lower()
    if host in {"youtube.com", "www.youtube.com", "youtu.be"} or "youtube.com" in href.lower() or "youtu.be" in href.lower():
        return "video_url"
    if "/documentcenter/view/" not in path:
        logger.warning(
            "url classification unclassified: row=%s href=%s text=%r reason=not_documentcenter_or_youtube",
            row_title,
            href,
            text,
        )
        return ""
    if PACKET_RE.search(text):
        return "agenda_packet_url"
    if AGENDA_RE.search(text):
        return "agenda_url"
    if MINUTES_RE.search(text):
        return "minutes_url"
    if PACKET_RE.search(combined):
        return "agenda_packet_url"
    if AGENDA_RE.search(combined):
        return "agenda_url"
    if MINUTES_RE.search(combined):
        return "minutes_url"
    logger.warning(
        "DocumentCenter link classification unclassified: row=%s href=%s text=%r",
        row_title,
        href,
        text,
    )
    return ""


def _meeting_id_from_links(anchors: list[object], row_label: str) -> str:
    for anchor in anchors:
        href = anchor.get("href", "")
        match = DOC_ID_RE.search(urlparse(href).path)
        if match:
            return match.group(1)
    logger.info("meeting_id absent: row=%s reason=no_documentcenter_id", row_label)
    return ""


def _status_for_row(meeting_title: str, agenda_url: str, minutes_url: str, agenda_packet_url: str) -> str:
    if CANCELLED_RE.search(meeting_title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _empty_row() -> dict:
    return {field: "" for field in SCHEMA_FIELDS}


def _build_rows_from_block(
    block_kind: str,
    node: object,
    content: list[object],
    base_url: str,
    session: requests.Session,
    redirect_cache: dict[str, str],
) -> tuple[list[dict], int]:
    block_text = _clean_text(node)
    if block_kind == "h2":
        block_text = " ".join([block_text] + [_clean_text(item) for item in content]).strip()
    primaries = _primary_anchors(block_kind, node, content)
    if not primaries:
        logger.warning("dropped meeting block: text=%r reason=no_primary_anchor_or_h2", block_text[:220])
        return [], 1

    rows = []
    drops = 0
    all_anchors = _row_link_scope(block_kind, node, content)
    for primary in primaries:
        row_label = _clean_text(primary) or _clean_text(node)
        row_anchors = _anchors_for_primary(block_kind, all_anchors, primaries, primary)
        title = _row_title_from_primary(primary, block_text, row_label)
        date = _parse_date(row_label or block_text, row_label)
        if not title or not date:
            logger.warning(
                "dropped meeting: row=%s reason=missing_required_title_or_date title=%r date=%r",
                row_label,
                title,
                date,
            )
            drops += 1
            continue

        row = _empty_row()
        row["meeting_title"] = title
        row["meeting_date"] = date
        row["meeting_time"] = _extract_time(row_label, row_label)
        row["meeting_location"] = _extract_location(block_text, row_label)
        row["meeting_id"] = _meeting_id_from_links([primary], row_label) or _meeting_id_from_links(row_anchors, row_label)

        for anchor in row_anchors:
            field_name = _classify_link(anchor, block_text, title)
            href = anchor.get("href", "")
            if not field_name:
                _emit_url(href, base_url, "unclassified_url", title, session, redirect_cache)
                continue
            emitted = _emit_url(href, base_url, field_name, title, session, redirect_cache)
            if not emitted:
                continue
            if row[field_name]:
                logger.warning(
                    "url dropped: row=%s field=%s url=%s reason=field_already_populated existing=%s",
                    title,
                    field_name,
                    emitted,
                    row[field_name],
                )
                continue
            row[field_name] = emitted
            logger.info("url emitted: row=%s field=%s url=%s", title, field_name, emitted)

        row["meeting_status"] = _status_for_row(
            row["meeting_title"],
            row["agenda_url"],
            row["minutes_url"],
            row["agenda_packet_url"],
        )
        logger.info("meeting emitted: title=%s date=%s status=%s", title, date, row["meeting_status"])
        rows.append(row)
    return rows, drops


def _assert_schema(rows: list[dict]) -> None:
    for index, row in enumerate(rows):
        keys = tuple(row.keys())
        if keys != SCHEMA_FIELDS:
            raise ValueError(f"Row {index} schema mismatch: {keys}")
        for field, value in row.items():
            if not isinstance(value, str):
                raise ValueError(f"Row {index} field {field} is not str: {type(value)!r}")
        for field in URL_FIELDS:
            value = row[field]
            if value and not value.startswith(("http://", "https://")):
                raise ValueError(f"Row {index} field {field} has invalid URL: {value}")


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Scrape San Tan Valley's CivicPlus DocumentCenter agenda-and-events page."""
    session = requests.Session()
    html = _fetch_text_bounded(session, calendar_url)
    soup = BeautifulSoup(html, "html.parser")
    _validate_vendor_fingerprint(soup)
    section_heading = _find_agenda_section_start(soup)
    blocks = _section_blocks(section_heading)

    rows: list[dict] = []
    rows_dropped = 0
    redirect_cache: dict[str, str] = {}
    for block_kind, node, content in blocks:
        block_rows, block_drops = _build_rows_from_block(
            block_kind,
            node,
            content,
            calendar_url,
            session,
            redirect_cache,
        )
        rows.extend(block_rows)
        rows_dropped += block_drops

    if not rows:
        logger.warning("no meetings emitted from Agenda and Events section")
    _assert_schema(rows)
    logger.info(
        "run summary: rows_seen=%s rows_accepted=%s rows_dropped=%s",
        len(blocks),
        len(rows),
        rows_dropped,
    )
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_URL), indent=2))

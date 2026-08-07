from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://mammothaz.gov/council-agendas"
HTML_MAX_BYTES = 1_000_000
BUNDLE_MAX_BYTES = 10_000_000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

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

ALLOWED_HOSTS = {
    "mammothaz.gov",
    "www.mammothaz.gov",
    "townofmammoth.us",
    "www.townofmammoth.us",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
}
BLOCKED_HOSTS = {"townofmammoth.lovable.app"}
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")

YEAR_GROUP_RE = re.compile(r'\{year:"(\d{4})",items:\[([^\]]{1,100000})\]\}')
ITEM_RE = re.compile(r'\{label:"([^"]{0,500})",href:"([^"]{1,1500}\.pdf)"\}')
PDF_HREF_RE = re.compile(r'"([^"]{1,1500}\.pdf)"')
VITE_SCRIPT_RE = re.compile(r"/assets/[^\"']+\.js")
CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]{1,200}>")

YMD_RE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})[-_ ](0?[1-9]|1[0-2])[-_ ](0?[1-9]|[12]\d|3[01])(?!\d)")
MDY_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[-_ ](0?[1-9]|[12]\d|3[01])[-_ ](20\d{2}|19\d{2}|\d{2})(?!\d)")
COMPACT_MDY_RE = re.compile(r"(?<!\d)(0[1-9]|1[0-2])([0-3]\d)(20\d{2}|19\d{2}|\d{2})(?!\d)")
COMPACT_YMD_RE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})(0[1-9]|1[0-2])([0-3]\d)(?!\d)")
PARTIAL_MD_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[-_ ](0?[1-9]|[12]\d|3[01])(?![-_ ]?\d)")
YEAR_RE = re.compile(r"\b(20\d{2}|19\d{2})\b")

MONTH_RE = re.compile(
    r"\b("
    r"January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    r")[-_ ]+([0-3]?\d)(?:st|nd|rd|th)?[,]?[-_ ]+(20\d{2}|19\d{2}|\d{2})\b",
    re.IGNORECASE,
)

MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


@dataclass(frozen=True)
class AgendaPdf:
    """Agenda PDF evidence extracted from the Mammoth Vite bundle."""

    label: str
    href: str
    year: str
    source: str
    ordinal: int


def _host_from_url(url: str) -> str:
    return (urlparse(url).netloc.split(":")[0] or "").lower()


def _ensure_fetch_host(url: str) -> None:
    host = _host_from_url(url)
    if host in BLOCKED_HOSTS or host not in ALLOWED_HOSTS:
        raise ValueError(f"Input URL host is not allowed for Mammoth parser: {host}")


def _fetch_text_bounded(
    session: requests.Session,
    url: str,
    max_bytes: int,
    allowed_hosts: set[str],
    step: str,
) -> str:
    """Fetch text with response-size and redirect-host checks."""
    _ensure_fetch_host(url)
    with session.get(url, timeout=30, stream=True, allow_redirects=True, verify=True) as response:
        final_host = _host_from_url(response.url)
        if final_host in BLOCKED_HOSTS or final_host not in allowed_hosts:
            raise ValueError(f"{step} redirected to disallowed host: {final_host} from {url}")
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"{step} response from {url} exceeded {max_bytes} bytes")
    encoding = response.encoding or "utf-8"
    logger.info("%s fetched %s bytes from %s", step, len(body), response.url)
    return bytes(body).decode(encoding, errors="replace")


def emit_url(raw_href: str, base_url: str, field: str, row_id: str) -> str:
    """Validate and absolutize an extracted URL for parser output."""
    if not raw_href:
        logger.warning("Dropping URL for field=%s row_id=%s: empty raw href", field, row_id)
        return ""

    stripped = raw_href.strip()
    lowered = stripped.lower().lstrip()
    for bad_scheme in BAD_SCHEMES:
        if lowered.startswith(bad_scheme):
            logger.warning(
                "Dropping URL for field=%s row_id=%s raw=%r reason=bad scheme",
                field,
                row_id,
                raw_href,
            )
            return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning(
            "Dropping URL for field=%s row_id=%s raw=%r absolute=%r reason=scheme not allowed",
            field,
            row_id,
            raw_href,
            absolute,
        )
        return ""

    host = (parsed.netloc.split(":")[0] or "").lower()
    if host in BLOCKED_HOSTS:
        logger.warning(
            "Dropping URL for field=%s row_id=%s raw=%r absolute=%r reason=blocked host",
            field,
            row_id,
            raw_href,
            absolute,
        )
        return ""
    if host not in ALLOWED_HOSTS:
        logger.warning(
            "Dropping URL for field=%s row_id=%s raw=%r absolute=%r reason=host not allowed",
            field,
            row_id,
            raw_href,
            absolute,
        )
        return ""

    logger.info("Emitting URL for field=%s row_id=%s url=%s", field, row_id, absolute)
    return absolute


def _find_vite_bundle_src(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        src = str(script.get("src") or "")
        script_type = str(script.get("type") or "")
        if script_type == "module" and VITE_SCRIPT_RE.search(src):
            logger.info(
                "Mammoth site vendor: custom Vite SPA, bundle-extraction strategy, "
                "confirmed by Vite script tag at runtime: %s",
                src,
            )
            return src

    for script in soup.find_all("script"):
        src = str(script.get("src") or "")
        if VITE_SCRIPT_RE.search(src):
            logger.info(
                "Mammoth site vendor: custom Vite SPA, bundle-extraction strategy, "
                "confirmed by Vite script tag at runtime: %s",
                src,
            )
            return src

    logger.warning("No Vite bundle script tag found in Mammoth HTML shell")
    return ""


def _clean_text(value: str) -> str:
    if not value:
        return ""
    if HTML_TAG_RE.search(value):
        value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _extract_pdf_items(bundle_text: str) -> list[AgendaPdf]:
    items: list[AgendaPdf] = []
    seen: set[str] = set()
    year_groups = list(YEAR_GROUP_RE.finditer(bundle_text))
    logger.info("Mammoth bundle fingerprint: detected %s year-group blocks", len(year_groups))

    ordinal = 0
    for group_match in year_groups:
        year = group_match.group(1)
        group_body = group_match.group(2)
        group_count = 0
        for item_match in ITEM_RE.finditer(group_body):
            label = _clean_text(item_match.group(1))
            href = item_match.group(2)
            if "/documents/council-agendas/" not in href:
                continue
            row_id = _derive_meeting_id(href, "")
            logger.info(
                "Bundle PDF found source=year-group year=%s row_id=%s label=%r href=%r",
                year,
                row_id,
                label,
                href,
            )
            if href in seen:
                logger.warning("Dropping duplicate agenda PDF row_id=%s href=%r", row_id, href)
                continue
            seen.add(href)
            ordinal += 1
            group_count += 1
            items.append(AgendaPdf(label=label, href=href, year=year, source="year-group", ordinal=ordinal))
        logger.info("Year group %s yielded %s council agenda PDFs", year, group_count)

    for item_match in ITEM_RE.finditer(bundle_text):
        label = _clean_text(item_match.group(1))
        href = item_match.group(2)
        if "/documents/council-agendas/" not in href or href in seen:
            continue
        row_id = _derive_meeting_id(href, "")
        logger.warning(
            "Agenda PDF found outside detected year groups row_id=%s label=%r href=%r",
            row_id,
            label,
            href,
        )
        seen.add(href)
        ordinal += 1
        items.append(AgendaPdf(label=label, href=href, year="", source="ungrouped-item", ordinal=ordinal))

    for href_match in PDF_HREF_RE.finditer(bundle_text):
        href = href_match.group(1)
        if "/documents/council-agendas/" not in href or href in seen:
            continue
        row_id = _derive_meeting_id(href, "")
        logger.warning("Unlabeled agenda PDF found in bundle row_id=%s href=%r", row_id, href)
        seen.add(href)
        ordinal += 1
        items.append(AgendaPdf(label="", href=href, year="", source="raw-pdf", ordinal=ordinal))

    year_group_detected = bool(year_groups)
    logger.info(
        "Mammoth bundle fingerprint: pdf_links=%s year_group_detected=%s",
        len(items),
        year_group_detected,
    )
    if not items:
        logger.warning("Zero PDF agenda links found in JS bundle - bundle structure may have changed.")
        logger.warning(
            "Could not extract agenda PDFs from JS bundle - bundle structure may have changed or PDFs are dynamically loaded."
        )
    return items


def _normalize_year(year: str) -> str:
    if len(year) == 2:
        numeric = int(year)
        return f"20{numeric:02d}" if numeric < 70 else f"19{numeric:02d}"
    return year


def _valid_iso(year: str, month: str, day: str) -> str:
    normalized_year = _normalize_year(year)
    try:
        parsed = datetime.strptime(
            f"{int(normalized_year):04d}-{int(month):02d}-{int(day):02d}",
            "%Y-%m-%d",
        )
    except ValueError:
        return ""
    return parsed.strftime("%Y-%m-%d")


def _parse_date_from_text(value: str) -> str:
    capped = value[:2000]

    for match in YMD_RE.finditer(capped):
        parsed = _valid_iso(match.group(1), match.group(2), match.group(3))
        if parsed:
            return parsed

    for match in MDY_RE.finditer(capped):
        parsed = _valid_iso(match.group(3), match.group(1), match.group(2))
        if parsed:
            return parsed

    for match in COMPACT_YMD_RE.finditer(capped):
        parsed = _valid_iso(match.group(1), match.group(2), match.group(3))
        if parsed:
            return parsed

    for match in COMPACT_MDY_RE.finditer(capped):
        parsed = _valid_iso(match.group(3), match.group(1), match.group(2))
        if parsed:
            return parsed

    for match in MONTH_RE.finditer(capped):
        month = MONTHS.get(match.group(1).lower(), 0)
        if not month:
            continue
        parsed = _valid_iso(match.group(3), str(month), match.group(2))
        if parsed:
            return parsed

    return ""


def _parse_partial_date_with_year(filename: str, label: str, year: str) -> str:
    context_year = year
    if not context_year:
        year_match = YEAR_RE.search(label)
        context_year = year_match.group(1) if year_match else ""
    if not context_year:
        return ""

    for match in PARTIAL_MD_RE.finditer(filename[:1000]):
        parsed = _valid_iso(context_year, match.group(1), match.group(2))
        if parsed:
            return parsed
    return ""


def _parse_date_from_filename(filename: str) -> str:
    """Parse an ISO date from a PDF filename, or return an empty string."""
    decoded = unquote(filename)
    parsed = _parse_date_from_text(decoded)
    if parsed:
        return parsed
    logger.warning("Could not parse date from filename=%r", filename)
    return ""


def _parse_meeting_date(item: AgendaPdf, filename: str, row_id: str) -> str:
    meeting_date = _parse_date_from_filename(filename)
    if meeting_date:
        logger.info("Date parsed from filename row_id=%s filename=%r date=%s", row_id, filename, meeting_date)
        return meeting_date

    combined = f"{filename} {item.label} {item.year}".strip()
    meeting_date = _parse_date_from_text(combined)
    if meeting_date:
        logger.info("Date parsed from surrounding context row_id=%s context=%r date=%s", row_id, combined, meeting_date)
        return meeting_date

    meeting_date = _parse_partial_date_with_year(filename, item.label, item.year)
    if meeting_date:
        logger.info(
            "Date parsed from filename month-day plus context year row_id=%s filename=%r year=%r date=%s",
            row_id,
            filename,
            item.year,
            meeting_date,
        )
        return meeting_date

    logger.warning("PDF URL could not be date-parsed row_id=%s filename=%r label=%r", row_id, filename, item.label)
    return ""


def _extract_time(label: str, filename: str, row_id: str) -> str:
    logger.warning(
        "No same-row time evidence in label/filename; emitting empty row_id=%s label=%r filename=%r",
        row_id,
        label,
        filename,
    )
    return ""


def _derive_meeting_id(href: str, iso_date: str) -> str:
    path = urlparse(href).path
    filename = unquote(PurePosixPath(path).name)
    stem = filename.rsplit(".", 1)[0] if filename else ""
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")
    if sanitized:
        return sanitized
    return iso_date


def _title_from_label(label: str) -> str:
    clean_label = _clean_text(label)
    lowered = clean_label.lower()
    if "public hearing" in lowered and "special" in lowered:
        return "Town Council Public Hearing and Special Meeting"
    if "public hearing" in lowered:
        return "Town Council Public Hearing"
    if "study session" in lowered:
        return "Town Council Study Session"
    if "special" in lowered:
        return "Town Council Special Meeting"
    if "notice" in lowered:
        return "Town Council Meeting Notice"
    return "Town Council Meeting"


def _meeting_status(title: str, agenda_url: str, agenda_packet_url: str, minutes_url: str) -> str:
    if CANCELLED_RE.search(title[:500]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _normalize_meeting(meeting: dict[str, str]) -> dict[str, str]:
    return {field: str(meeting.get(field, "") or "") for field in FIELDS}


def _build_meeting(item: AgendaPdf, base_url: str) -> dict[str, str]:
    raw_filename = PurePosixPath(urlparse(item.href).path).name
    filename = unquote(raw_filename)
    preliminary_id = _derive_meeting_id(item.href, "")
    agenda_url = emit_url(item.href, base_url, "agenda_url", preliminary_id)
    meeting_date = _parse_meeting_date(item, filename, preliminary_id)
    meeting_id = _derive_meeting_id(item.href, meeting_date)
    title = _title_from_label(item.label)
    meeting_time = _extract_time(item.label, filename, meeting_id)
    minutes_url = ""
    video_url = ""
    agenda_packet_url = ""
    ecomment_url = ""
    status = _meeting_status(title, agenda_url, agenda_packet_url, minutes_url)

    meeting = _normalize_meeting(
        {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": status,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
            "agenda_packet_url": agenda_packet_url,
            "ecomment_url": ecomment_url,
            "meeting_id": meeting_id,
        }
    )
    logger.info(
        "Emitted meeting row_id=%s source=%s label=%r date=%r status=%s agenda_url=%s",
        meeting_id,
        item.source,
        item.label,
        meeting_date,
        status,
        agenda_url,
    )
    return meeting


def scrape_calendar(url: str) -> list[dict]:
    """Scrape Mammoth council agenda PDFs from the site's Vite JavaScript bundle."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    try:
        html = _fetch_text_bounded(session, url, HTML_MAX_BYTES, ALLOWED_HOSTS, "HTML shell fetch")
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch Mammoth HTML shell: {exc}") from exc

    bundle_src = _find_vite_bundle_src(html)
    if not bundle_src:
        logger.warning("Cannot continue Mammoth scrape because no bundle URL was found")
        return []

    bundle_url = emit_url(bundle_src, url, "bundle_url", "vite-script")
    if not bundle_url:
        logger.warning("Cannot continue Mammoth scrape because bundle URL failed validation: %r", bundle_src)
        return []

    try:
        bundle_text = _fetch_text_bounded(session, bundle_url, BUNDLE_MAX_BYTES, ALLOWED_HOSTS, "JS bundle fetch")
    except Exception as exc:
        logger.warning("Failed to fetch Mammoth JS bundle %s: %s", bundle_url, exc)
        return []

    pdf_items = _extract_pdf_items(bundle_text)
    if pdf_items:
        years = sorted({item.year for item in pdf_items if item.year})
        if years and max(years) < "2020":
            logger.warning("Only pre-2020 agenda PDFs found in Mammoth bundle; archive may have migrated")

    meetings: list[dict] = []
    for item in pdf_items:
        if not item.href:
            logger.warning("Dropping agenda item ordinal=%s reason=empty href label=%r", item.ordinal, item.label)
            continue
        meetings.append(_build_meeting(item, url))

    logger.info("Mammoth scrape complete rows_emitted=%s", len(meetings))
    return meetings


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    url = sys.argv[1] if len(sys.argv) > 1 else "https://mammothaz.gov/council-agendas"
    result = scrape_calendar(url)
    print(json.dumps(result, indent=2))
    print(f"# Total meetings: {len(result)}", file=sys.stderr)

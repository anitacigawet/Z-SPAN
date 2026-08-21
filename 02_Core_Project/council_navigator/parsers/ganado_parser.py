from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from polite_http import make_session


DEFAULT_URL = "https://ganado.navajochapters.org/870-2/"
FETCH_HOSTS = {"ganado.navajochapters.org"}
MAX_RESPONSE_BYTES = 2_000_000
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
LABEL_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})\s+(PLANNING|REGULAR|REGUALR)\s+Meeting\s+Agenda\b",
    re.IGNORECASE,
)
FULL_MONTH_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"[-_ .]+([0-9]{1,2})[-_, .]+([0-9]{4})\b",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"\b(0?[1-9]|1[0-2])[-_.](0?[1-9]|[12]\d|3[01])[-_.](\d{4})\b")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)

logger = logging.getLogger(__name__)

def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Read current Ganado Chapter planning and regular meeting agendas."""
    _validate_source_url(url)
    with make_session() as session:
        html = _fetch_text_bounded(session, url)
    soup = BeautifulSoup(html, "html.parser")
    modules = _meeting_modules(soup)
    link_map = _link_options(soup)
    _validate_fingerprint(soup, modules, link_map)

    current_floor_date = date.today().replace(day=1)
    current_floor = current_floor_date.isoformat()
    meetings: list[dict[str, str]] = []
    latest_label_month = ""
    mapped_witnesses = 0
    for module_class, label, label_match in modules:
        month_number = datetime.strptime(label_match.group(1), "%B").month
        label_year = int(label_match.group(2))
        label_month = f"{label_year:04d}-{month_number:02d}"
        latest_label_month = max(latest_label_month, label_month)
        agenda_candidate = link_map.get(module_class, "")
        if agenda_candidate:
            mapped_witnesses += 1
        if (label_year, month_number) < (current_floor_date.year, current_floor_date.month):
            continue
        if not agenda_candidate:
            raise RuntimeError(
                "Ganado current/future agenda module lacks its class-matched official URL: "
                f"class={module_class!r} label={label!r}"
            )
        agenda_url = _emit_url(agenda_candidate, url, label)
        if not agenda_url:
            raise RuntimeError(f"Ganado current/future agenda URL was rejected: label={label!r}")
        meeting_date = _extract_exact_date(agenda_url)
        if not meeting_date:
            raise RuntimeError(
                "Ganado current/future agenda lacks an exact day-level date; "
                f"the month label is not used as a placeholder: {label!r}"
            )
        if meeting_date < current_floor:
            continue
        normalized_title = re.sub(r"\bAgenda\b", "", label, flags=re.IGNORECASE)
        normalized_title = re.sub(r"\bREGUALR\b", "REGULAR", normalized_title, flags=re.IGNORECASE)
        normalized_title = " ".join(normalized_title.split())
        meeting = _empty_row()
        meeting.update(
            {
                "meeting_title": normalized_title,
                "meeting_date": meeting_date,
                "meeting_status": (
                    "Cancelled" if CANCELLED_RE.search(label) else "Agenda Available"
                ),
                "agenda_url": agenda_url,
                "meeting_id": PurePosixPath(urlparse(agenda_url).path).stem,
            }
        )
        meetings.append(meeting)

    if mapped_witnesses == 0:
        raise ValueError("Ganado Divi class-to-URL mapping produced no witnessed pairs")
    meetings.sort(key=lambda item: (item["meeting_date"], item["meeting_title"]))
    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Ganado official Chapter Agenda surface is accessible with no current-month-forward rows; "
            "latest_label_month=%s current_floor=%s class_mappings=%d",
            latest_label_month,
            current_floor,
            mapped_witnesses,
        )
    return meetings


def _fetch_text_bounded(session: object, url: str) -> str:
    with session.get(url, timeout=35, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").casefold()
        if final_host not in FETCH_HOSTS:
            raise ValueError(f"Ganado redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Ganado response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _validate_source_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in FETCH_HOSTS:
        raise ValueError("Ganado source must use HTTPS on the official chapter host")


def _meeting_modules(soup: BeautifulSoup) -> list[tuple[str, str, re.Match[str]]]:
    modules: list[tuple[str, str, re.Match[str]]] = []
    for module in soup.select("div.et_pb_module.et_pb_code"):
        label = _clean_text(module)
        label_match = LABEL_RE.fullmatch(label)
        if not label_match:
            continue
        module_class = next(
            (name for name in module.get("class", []) if re.fullmatch(r"et_pb_code_\d+", name)),
            "",
        )
        if not module_class:
            raise ValueError(f"Ganado meeting module lacks its Divi code class: {label!r}")
        modules.append((module_class, label, label_match))
    return modules


def _link_options(soup: BeautifulSoup) -> dict[str, str]:
    script_text = next(
        (
            script.string or script.get_text()
            for script in soup.find_all("script")
            if "et_link_options_data" in (script.string or script.get_text())
        ),
        "",
    )
    match = re.search(r"var\s+et_link_options_data\s*=\s*(\[.*?\]);", script_text, re.DOTALL)
    if not match:
        raise ValueError("Ganado et_link_options_data fingerprint is absent")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("Ganado et_link_options_data JSON is malformed") from exc
    if not isinstance(payload, list):
        raise ValueError("Ganado et_link_options_data is not a list")
    options: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Ganado et_link_options_data contains a non-object item")
        class_name = str(item.get("class") or "")
        href = str(item.get("url") or "")
        if re.fullmatch(r"et_pb_code_\d+", class_name) and href:
            if class_name in options and options[class_name] != href:
                raise ValueError(f"Ganado Divi class maps to multiple URLs: {class_name}")
            options[class_name] = href
    return options


def _validate_fingerprint(
    soup: BeautifulSoup,
    modules: list[tuple[str, str, re.Match[str]]],
    link_map: dict[str, str],
) -> None:
    title = _clean_text(soup.title)
    visible = _clean_text(soup)
    if "Agenda Page" not in title or "Ganado Chapter Agenda" not in visible:
        raise ValueError("Ganado Chapter Agenda fingerprint drifted")
    if not modules or not link_map:
        raise ValueError("Ganado Divi meeting modules or link mapping are absent")
    if not any(module_class in link_map for module_class, _, _ in modules):
        raise ValueError("Ganado Divi module classes do not intersect the URL mapping")
    logger.info(
        "vendor fingerprint witness=Ganado_Chapter_Agenda_plus_Divi_code_modules_plus_class_URL_map"
    )


def _extract_exact_date(value: str) -> str:
    match = FULL_MONTH_DATE_RE.search(value[:1200])
    if match:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(" ".join(match.groups()), fmt).date().isoformat()
            except ValueError:
                continue
        logger.warning("Ganado agenda date is invalid: value=%r", value[:300])
        return ""
    match = NUMERIC_DATE_RE.search(value[:1200])
    if match:
        try:
            return date(int(match.group(3)), int(match.group(1)), int(match.group(2))).isoformat()
        except ValueError:
            logger.warning("Ganado numeric agenda date is invalid: value=%r", value[:300])
            return ""
    logger.warning("Ganado agenda date extraction returned empty: value=%r", value[:300])
    return ""


def _emit_url(href: str, base_url: str, row_label: str) -> str:
    absolute = urljoin(base_url, str(href or "").strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").casefold() not in FETCH_HOSTS:
        logger.warning(
            "Ganado URL dropped: row=%r href=%r reason=scheme_or_host_not_allowlisted",
            row_label,
            href,
        )
        return ""
    return absolute


def _empty_row() -> dict[str, str]:
    return {field: "" for field in FIELD_NAMES}


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != FIELD_NAMES:
            raise ValueError(f"Ganado row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Ganado row {index} contains a non-string value")

# Example usage (for testing purposes)
if __name__ == '__main__':
    CALENDAR_URL = "https://ganado.navajochapters.org/870-2/"
    scraped_meetings = scrape_calendar(CALENDAR_URL)

    if scraped_meetings:
        print(f"Found {len(scraped_meetings)} meetings.")
        # Print the first few meetings for verification
        for i, meeting in enumerate(scraped_meetings[:5]):
            print(f"\n--- Meeting {i+1} ---")
            for key, value in meeting.items():
                print(f"{key}: {value}")
    else:
        print("No meetings found or an error occurred.")

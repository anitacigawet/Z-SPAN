
"""Florence, Arizona Town Council calendar parser."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import json
import logging
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

from polite_http import make_session


DEFAULT_API_URL = "https://www.florenceaz.gov/wp-json/tribe/events/v1/events"
COUNCIL_PAGE_URL = "https://www.florenceaz.gov/town-council/"
TOWN_COUNCIL_CATEGORY_ID = "1145"
ALLOWED_SOURCE_HOSTS = {"florenceaz.gov", "www.florenceaz.gov"}
ALLOWED_DOCUMENT_HOSTS = ALLOWED_SOURCE_HOSTS | {"youtu.be", "youtube.com", "www.youtube.com"}
MAX_API_BYTES = 1_000_000
MAX_COUNCIL_PAGE_BYTES = 3_000_000
MAX_API_PAGES = 10
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)

# Florence's server currently sends only its leaf certificate and omits this
# public Sectigo intermediate. Adding the intermediate to the normal trust
# context repairs the chain while preserving hostname, expiry, signature, and
# root verification. Fingerprint (SHA-256):
# 8C:54:C3:34:B6:6B:A4:E4:26:77:2A:F4:A3:F9:13:6C:
# 19:A1:AE:C7:29:FD:B2:8C:53:5C:07:A5:A4:EF:22:E0
FLORENCE_SECTIGO_INTERMEDIATE_PEM = """-----BEGIN CERTIFICATE-----
MIIGTDCCBDSgAwIBAgIQOXpmzCdWNi4NqofKbqvjsTANBgkqhkiG9w0BAQwFADBf
MQswCQYDVQQGEwJHQjEYMBYGA1UEChMPU2VjdGlnbyBMaW1pdGVkMTYwNAYDVQQD
Ey1TZWN0aWdvIFB1YmxpYyBTZXJ2ZXIgQXV0aGVudGljYXRpb24gUm9vdCBSNDYw
HhcNMjEwMzIyMDAwMDAwWhcNMzYwMzIxMjM1OTU5WjBgMQswCQYDVQQGEwJHQjEY
MBYGA1UEChMPU2VjdGlnbyBMaW1pdGVkMTcwNQYDVQQDEy5TZWN0aWdvIFB1Ymxp
YyBTZXJ2ZXIgQXV0aGVudGljYXRpb24gQ0EgRFYgUjM2MIIBojANBgkqhkiG9w0B
AQEFAAOCAY8AMIIBigKCAYEAljZf2HIz7+SPUPQCQObZYcrxLTHYdf1ZtMRe7Yeq
RPSwygz16qJ9cAWtWNTcuICc++p8Dct7zNGxCpqmEtqifO7NvuB5dEVexXn9RFFH
12Hm+NtPRQgXIFjx6MSJcNWuVO3XGE57L1mHlcQYj+g4hny90aFh2SCZCDEVkAja
EMMfYPKuCjHuuF+bzHFb/9gV8P9+ekcHENF2nR1efGWSKwnfG5RawlkaQDpRtZTm
M64TIsv/r7cyFO4nSjs1jLdXYdz5q3a4L0NoabZfbdxVb+CUEHfB0bpulZQtH1Rv
38e/lIdP7OTTIlZh6OYL6NhxP8So0/sht/4J9mqIGxRFc0/pC8suja+wcIUna0HB
pXKfXTKpzgis+zmXDL06ASJf5E4A2/m+Hp6b84sfPAwQ766rI65mh50S0Di9E3Pn
2WcaJc+PILsBmYpgtmgWTR9eV9otfKRUBfzHUHcVgarub/XluEpRlTtZudU5xbFN
xx/DgMrXLUAPaI60fZ6wA+PTAgMBAAGjggGBMIIBfTAfBgNVHSMEGDAWgBRWc1hk
lfmSGrASKgRieaFAFYghSTAdBgNVHQ4EFgQUaMASFhgOr872h6YyV6NGUV3LBycw
DgYDVR0PAQH/BAQDAgGGMBIGA1UdEwEB/wQIMAYBAf8CAQAwHQYDVR0lBBYwFAYI
KwYBBQUHAwEGCCsGAQUFBwMCMBsGA1UdIAQUMBIwBgYEVR0gADAIBgZngQwBAgEw
VAYDVR0fBE0wSzBJoEegRYZDaHR0cDovL2NybC5zZWN0aWdvLmNvbS9TZWN0aWdv
UHVibGljU2VydmVyQXV0aGVudGljYXRpb25Sb290UjQ2LmNybDCBhAYIKwYBBQUH
AQEEeDB2ME8GCCsGAQUFBzAChkNodHRwOi8vY3J0LnNlY3RpZ28uY29tL1NlY3Rp
Z29QdWJsaWNTZXJ2ZXJBdXRoZW50aWNhdGlvblJvb3RSNDYucDdjMCMGCCsGAQUF
BzABhhdodHRwOi8vb2NzcC5zZWN0aWdvLmNvbTANBgkqhkiG9w0BAQwFAAOCAgEA
YtOC9Fy+TqECFw40IospI92kLGgoSZGPOSQXMBqmsGWZUQ7rux7cj1du6d9rD6C8
ze1B2eQjkrGkIL/OF1s7vSmgYVafsRoZd/IHUrkoQvX8FZwUsmPu7amgBfaY3g+d
q1x0jNGKb6I6Bzdl6LgMD9qxp+3i7GQOnd9J8LFSietY6Z4jUBzVoOoz8iAU84OF
h2HhAuiPw1ai0VnY38RTI+8kepGWVfGxfBWzwH9uIjeooIeaosVFvE8cmYUB4TSH
5dUyD0jHct2+8ceKEtIoFU/FfHq/mDaVnvcDCZXtIgitdMFQdMZaVehmObyhRdDD
4NQCs0gaI9AAgFj4L9QtkARzhQLNyRf87Kln+YU0lgCGr9HLg3rGO8q+Y4ppLsOd
unQZ6ZxPNGIfOApbPVf5hCe58EZwiWdHIMn9lPP6+F404y8NNugbQixBber+x536
WrZhFZLjEkhp7fFXf9r32rNPfb74X/U90Bdy4lzp3+X1ukh1BuMxA/EEhDoTOS3l
7ABvc7BYSQubQ2490OcdkIzUh3ZwDrakMVrbaTxUM2p24N6dB+ns2zptWCva6jzW
r8IWKIMxzxLPv5Kt3ePKcUdvkBU/smqujSczTzzSjIoR5QqQA6lN1ZRSnuHIWCvh
JEltkYnTAH41QJ6SAWO66GrrUESwN/cgZzL4JLEqz1Y=
-----END CERTIFICATE-----"""

logger = logging.getLogger(__name__)


def scrape_calendar(calendar_url: str | None = None) -> list[dict]:
    """Return Florence Town Council meetings from this month forward."""
    month_start = date.today().replace(day=1).isoformat()
    api_url = _build_api_url(calendar_url or DEFAULT_API_URL, month_start)

    with make_session(additional_ca_pem=FLORENCE_SECTIGO_INTERMEDIATE_PEM) as session:
        events = _fetch_council_events(session, api_url)
        document_rows = _fetch_document_rows(session, COUNCIL_PAGE_URL, month_start)

    meetings: list[dict] = []
    stats: Counter[str] = Counter()
    for event in events:
        title = _clean_fragment(str(event.get("title") or ""))
        if "town council" not in title.lower():
            stats["non_council_events_dropped"] += 1
            continue
        meeting = _meeting_from_event(event, title, document_rows, stats)
        _validate_meeting(meeting)
        meetings.append(meeting)

    meetings.sort(key=lambda row: (row["meeting_date"], row["meeting_time"], row["meeting_id"]))
    logger.info("Florence current-intake scrape summary: %s emitted=%d", dict(stats), len(meetings))
    if not meetings:
        if events:
            raise ValueError(
                "Florence Town Council category exposed events but none retained explicit Town Council title evidence"
            )
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning("Florence official Town Council API category witnessed zero current events")
    return meetings


def _build_api_url(url: str, month_start: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_SOURCE_HOSTS:
        raise ValueError(f"Florence API URL is not an allowed official HTTPS host: {url!r}")
    if "/wp-json/tribe/events/v1/events" not in parsed.path:
        raise ValueError(f"Florence API URL has an unexpected path: {parsed.path!r}")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "per_page": "50",
            "start_date": month_start,
            "categories": TOWN_COUNCIL_CATEGORY_ID,
        }
    )
    query.pop("page", None)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _fetch_council_events(session, api_url: str) -> list[dict]:
    first = _fetch_json_bounded(session, api_url)
    total_pages = first.get("total_pages")
    if not isinstance(total_pages, int) or total_pages < 0:
        raise ValueError("Florence events API omitted a valid total_pages value")
    if total_pages > MAX_API_PAGES:
        raise ValueError(f"Florence events API requested {total_pages} pages; cap is {MAX_API_PAGES}")

    events = _require_event_list(first)
    for page in range(2, total_pages + 1):
        parsed = urlparse(api_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["page"] = str(page)
        page_url = urlunparse(parsed._replace(query=urlencode(query)))
        events.extend(_require_event_list(_fetch_json_bounded(session, page_url)))
    return events


def _fetch_json_bounded(session, url: str) -> dict:
    with _source_response(session, url) as response:
        response.raise_for_status()
        _validate_source_url(response.url)
        body = _read_bounded(response, MAX_API_BYTES, url)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Florence events API returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("Florence events API returned a non-object payload")
    return payload


def _require_event_list(payload: dict) -> list[dict]:
    events = payload.get("events")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise TypeError("Florence events API omitted its events list")
    return events


def _fetch_document_rows(session, url: str, month_start: str) -> dict[str, list[dict[str, str]]]:
    with _source_response(session, url) as response:
        response.raise_for_status()
        _validate_source_url(response.url)
        html = _read_bounded(response, MAX_COUNCIL_PAGE_BYTES, url).decode(
            response.encoding or "utf-8", errors="replace"
        )
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#agendas")
    if table is None:
        raise ValueError("Florence Town Council page no longer contains table#agendas")

    rows_by_date: dict[str, list[dict[str, str]]] = {}
    for row in table.select("tbody tr"):
        date_cell = row.select_one("td[data-order]")
        if date_cell is None:
            logger.warning("Florence document row dropped because data-order date is missing")
            continue
        meeting_date = str(date_cell.get("data-order") or "").strip()
        try:
            date.fromisoformat(meeting_date)
        except ValueError:
            logger.warning("Florence document row dropped due invalid date=%r", meeting_date)
            continue
        if meeting_date < month_start:
            continue
        rows_by_date.setdefault(meeting_date, []).append(_parse_document_row(row, url))
    return rows_by_date


def _source_response(session, url: str):
    try:
        response = session.get(url, timeout=(10, 30), stream=True, allow_redirects=True)
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Florence official source failed verified TLS: url=%s", url)
        raise
    status = getattr(response, "status_code", None)
    if status in {401, 403, 429}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Florence official source blocked the bounded polite request: status=%s url=%s",
            status,
            url,
        )
    return response


def _parse_document_row(row: Tag, base_url: str) -> dict[str, str]:
    result = {
        "session_label": "",
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
    }
    cells = row.select("td")
    if not cells:
        return result
    cell_lines = [line.strip() for line in cells[0].get_text("\n", strip=True).splitlines() if line.strip()]
    result["session_label"] = next(
        (line for line in cell_lines if line.lower().endswith("session")),
        "",
    )
    if len(cells) < 2:
        logger.warning("Florence document row has no document cell for date=%r", cells[0].get("data-order"))
        return result
    for link in cells[1].select("a[href]"):
        label = _clean_fragment(link.get_text(" ", strip=True)).lower()
        field = {
            "agenda": "agenda_url",
            "agenda packet": "agenda_packet_url",
            "minutes": "minutes_url",
            "action minutes": "minutes_url",
            "video recording": "video_url",
        }.get(label)
        if field is None:
            continue
        emitted = _emit_document_url(str(link.get("href") or ""), base_url, field)
        if not emitted:
            continue
        if field == "minutes_url" and result[field] and label != "action minutes":
            continue
        result[field] = emitted
    return result


def _meeting_from_event(
    event: dict,
    title: str,
    document_rows: dict[str, list[dict[str, str]]],
    stats: Counter[str],
) -> dict:
    raw_start = str(event.get("start_date") or "")
    try:
        start = datetime.strptime(raw_start, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"Florence event {event.get('id')!r} has invalid start_date={raw_start!r}") from exc

    meeting_date = start.date().isoformat()
    documents = _select_document_row(title, meeting_date, document_rows.get(meeting_date, []), stats)
    location = _format_location(event.get("venue"))
    status = _canonical_status(title, documents)
    stats[f"status_{status.lower().replace(' ', '_')}"] += 1
    for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url"):
        if documents[field]:
            stats[f"{field}_emitted"] += 1

    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": start.strftime("%I:%M %p").lstrip("0"),
        "meeting_location": location,
        "meeting_status": status,
        "agenda_url": documents["agenda_url"],
        "minutes_url": documents["minutes_url"],
        "video_url": documents["video_url"],
        "agenda_packet_url": documents["agenda_packet_url"],
        "ecomment_url": "",
        "meeting_id": str(event.get("id") or ""),
    }


def _select_document_row(
    title: str,
    meeting_date: str,
    rows: list[dict[str, str]],
    stats: Counter[str],
) -> dict[str, str]:
    empty = {
        "session_label": "",
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
    }
    if not rows:
        stats["events_without_document_row"] += 1
        return empty
    if len(rows) == 1:
        return rows[0]
    title_lower = title.lower()
    matching = [
        row
        for row in rows
        if row["session_label"]
        and row["session_label"].lower().split(" session")[0] in title_lower
    ]
    if len(matching) == 1:
        return matching[0]
    stats["ambiguous_document_rows"] += 1
    logger.warning(
        "Florence documents left empty because date=%s title=%r matched %d rows",
        meeting_date,
        title,
        len(rows),
    )
    return empty


def _format_location(raw_venue) -> str:
    if not isinstance(raw_venue, dict):
        return ""
    parts = [
        str(raw_venue.get("venue") or "").strip(),
        str(raw_venue.get("address") or "").strip(),
        str(raw_venue.get("city") or "").strip(),
        " ".join(
            part
            for part in (
                str(raw_venue.get("state") or "").strip(),
                str(raw_venue.get("zip") or "").strip(),
            )
            if part
        ),
    ]
    return ", ".join(part for part in parts if part)


def _canonical_status(title: str, documents: dict[str, str]) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if documents["minutes_url"]:
        return "Minutes Available"
    if documents["agenda_url"] or documents["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _emit_document_url(href: str, base_url: str, field: str) -> str:
    absolute = urljoin(base_url, href.strip())
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_DOCUMENT_HOSTS:
        logger.warning("Florence dropped %s URL due disallowed scheme/host: %r", field, href)
        return ""
    return absolute


def _read_bounded(response, max_bytes: int, started_url: str) -> bytes:
    body = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ValueError(f"Response from {started_url} exceeded {max_bytes} bytes")
    return bytes(body)


def _validate_source_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_SOURCE_HOSTS:
        raise ValueError(f"Florence source redirected to a disallowed URL: {url!r}")


def _clean_fragment(value: str) -> str:
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _validate_meeting(meeting: dict) -> None:
    expected = {
        "meeting_title", "meeting_date", "meeting_time", "meeting_location",
        "meeting_status", "agenda_url", "minutes_url", "video_url",
        "agenda_packet_url", "ecomment_url", "meeting_id",
    }
    if set(meeting) != expected:
        raise ValueError(f"Florence parser emitted wrong fields: {sorted(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise TypeError("Florence parser emitted a non-string field")
    if not meeting["meeting_id"]:
        raise ValueError("Florence event is missing its stable event ID")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    for row in scrape_calendar():
        print(row)

"""South Tucson — custom website meeting parser."""
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://www.southtucsonaz.gov"
DEFAULT_CALENDAR = f"{BASE}/calendar"
RECENT_MEETINGS = f"{BASE}/meetings/recent"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _list_url(url):
    if not url:
        return RECENT_MEETINGS
    if "southtucsonaz.gov" not in url:
        return url
    if "/meetings/recent" in url:
        return url
    # The city calendar omits the document grid; meeting records are exposed at
    # /meetings/recent.
    if "/calendar" in url or url.rstrip("/") == BASE:
        return RECENT_MEETINGS
    return url


def scrape_calendar(url=None):
    target = _list_url(url or DEFAULT_CALENDAR)
    try:
        response = requests.get(target, timeout=25, headers=HEADERS)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("South Tucson meetings fetch failed for %s: %s", target, exc)
        raise

    soup = BeautifulSoup(response.content, "html.parser")
    table = soup.find("table")
    if not table:
        message = "South Tucson meetings response is missing the expected table"
        logger.warning(message)
        raise RuntimeError(message)

    rows = table.find_all("tr")
    if len(rows) < 2:
        message = (
            "South Tucson meetings table contains no recognizable meeting rows; "
            "the page exposes no explicit empty-state marker"
        )
        logger.warning(message)
        raise RuntimeError(message)

    headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
    meetings = []

    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        when = cells[0].get_text(strip=True)
        title = cells[1].get_text(strip=True)
        if not title:
            continue

        meeting_date = when
        meeting_time = ""
        if "|" in when:
            parts = [p.strip() for p in when.split("|", 1)]
            meeting_date = parts[0]
            meeting_time = parts[1] if len(parts) > 1 else ""

        agenda_url = minutes_url = video_url = detail_url = ""

        for i, cell in enumerate(cells):
            if i >= len(headers):
                break
            hdr = headers[i]
            link = cell.find("a", href=True)
            if not link:
                continue
            href = urljoin(BASE, link["href"])

            if "agenda" in hdr and "packet" not in hdr:
                agenda_url = agenda_url or href
            elif "packet" in hdr:
                agenda_url = agenda_url or href
            elif "minutes" in hdr:
                minutes_url = minutes_url or href
            elif "video" in hdr or "audio" in hdr:
                video_url = video_url or href
            elif "view" in hdr:
                detail_url = href

        for link in row.find_all("a", href=True):
            href = urljoin(BASE, link["href"])
            if "/meeting/" in href and "dropbox.com" not in href:
                detail_url = detail_url or href
            if "dropbox.com" in href:
                video_url = video_url or href

        if not agenda_url and detail_url:
            agenda_url = detail_url

        meetings.append(
            {
                "Meeting Title/Name": title,
                "Meeting Date": meeting_date,
                "Meeting Time": meeting_time,
                "Agenda URL": agenda_url,
                "Minutes URL": minutes_url,
                "Video URL": video_url,
                "Meeting Location": "South Tucson City Hall",
                "Meeting Status": "",
            }
        )

    if not meetings:
        message = (
            "South Tucson meetings table yielded no valid meeting rows; "
            "cannot distinguish an empty calendar from source drift"
        )
        logger.warning(message)
        raise RuntimeError(message)
    return meetings

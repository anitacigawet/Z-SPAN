import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://www.welltonaz.gov"
DEFAULT_URL = f"{BASE}/agendacenter"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _parse_row_text(cell_text):
    """TD0 holds 'Feb 3, 2026 Town of Wellton Regular ...' possibly with Amended line."""
    text = " ".join(cell_text.split())
    dm = re.search(r"([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", text)
    if not dm:
        return "", text
    meeting_date = dm.group(1)
    rest = text[dm.end() :].strip()
    tail = re.search(r"\b(?:AM|PM|am|pm)\s+(.+)$", rest)
    if tail and "amended" in rest.lower():
        title = tail.group(1).strip()
    else:
        rest = re.sub(
            r"^(?:\?\?)?\s*Amended\s+.*?\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\s*",
            "",
            rest,
            flags=re.I | re.DOTALL,
        )
        title = rest.strip() or text
    return meeting_date, title


def scrape_calendar(url=None):
    target = url or DEFAULT_URL
    try:
        response = requests.get(target, timeout=25, headers=HEADERS)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Wellton agenda-center fetch failed for %s: %s", target, exc)
        raise

    soup = BeautifulSoup(response.content, "html.parser")
    table = soup.find("table")
    if not table:
        message = "Wellton agenda-center response is missing the expected table"
        logger.warning(message)
        raise RuntimeError(message)

    meetings = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue

        cell0 = tds[0].get_text(" ", strip=True)
        meeting_date, title = _parse_row_text(cell0)
        if not title:
            continue
        low = title.lower()
        if low == "agenda minutes download" or (
            "agenda" in low and "minutes" in low and "download" in low and len(low) < 40
        ):
            continue

        agenda_url = ""
        minutes_url = ""
        for a in tr.find_all("a", href=True):
            href = a["href"]
            if "ViewFile/Agenda" in href:
                agenda_url = urljoin(BASE, href)
            elif "ViewFile/Minutes" in href:
                minutes_url = urljoin(BASE, href)

        if not agenda_url and not minutes_url:
            continue

        meetings.append(
            {
                "Meeting Title/Name": title,
                "Meeting Date": meeting_date,
                "Meeting Time": "",
                "Agenda URL": agenda_url,
                "Minutes URL": minutes_url,
                "Video URL": "",
                "Meeting Location": "Wellton Town Hall",
                "Meeting Status": "",
            }
        )

    if not meetings:
        message = (
            "Wellton agenda-center table yielded no recognizable meeting documents; "
            "cannot distinguish an empty calendar from source drift"
        )
        logger.warning(message)
        raise RuntimeError(message)
    return meetings

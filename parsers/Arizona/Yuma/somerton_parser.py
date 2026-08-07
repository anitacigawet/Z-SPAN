import logging
import re
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://cityofsomerton.civicweb.net"
MEETING_TYPE_LIST = f"{BASE}/Portal/MeetingTypeList.aspx"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

DATE_TAIL = re.compile(r"[-\u2013]\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s*$")


def _row_from_link(list_href, label):
    info_url = urljoin(BASE, list_href)
    meeting_date = ""
    meeting_time = ""
    m = DATE_TAIL.search(label)
    if m:
        meeting_date = m.group(1)
        title = label[: m.start()].strip()
    else:
        title = label

    return {
        "Meeting Title/Name": title or label,
        "Meeting Date": meeting_date,
        "Meeting Time": meeting_time,
        "Agenda URL": info_url,
        "Minutes URL": "",
        "Video URL": "",
        "Meeting Location": "City Hall, Council Chambers 143 N. State Ave., Somerton, AZ 85350",
        "Meeting Status": "",
    }


def scrape_calendar(url=None):
    """
    MeetingTypeList.aspx includes server-rendered links to MeetingInformation.aspx?Id=...
    (the schedule page alone is mostly JS; the type list is scrape-friendly).
    """
    try:
        response = requests.get(MEETING_TYPE_LIST, timeout=25, headers=HEADERS)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Somerton meeting-list fetch failed for %s: %s", MEETING_TYPE_LIST, exc)
        raise

    soup = BeautifulSoup(response.content, "html.parser")
    seen_ids = set()
    meetings = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "MeetingInformation.aspx" not in href or "Id=" not in href:
            continue
        q = parse_qs(urlparse(href).query)
        ids = q.get("Id") or q.get("id")
        if not ids:
            continue
        mid = ids[0]
        if mid in seen_ids:
            continue
        seen_ids.add(mid)

        label = a.get_text(strip=True)
        if not label:
            continue

        meetings.append(_row_from_link(href, label))

    if not meetings:
        message = (
            "Somerton meeting-list page yielded no recognizable meeting links; "
            "cannot distinguish an empty calendar from source drift"
        )
        logger.warning(message)
        raise RuntimeError(message)
    return meetings

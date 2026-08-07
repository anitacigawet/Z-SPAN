"""Pima — Revize Document Center meeting parser."""
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_URL = "https://www.pimatown.az.gov/town_council/agendas_and_minutes.php"
BASE = "https://www.pimatown.az.gov"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

MEETING_KEYWORDS = (
    "agenda",
    "minute",
    "packet",
    "council",
    "meeting",
    "special",
    "workshop",
    "session",
)


def _basename(href):
    path = urlparse(href).path
    name = path.split("/")[-1] if path else href
    return name.split("?")[0]


def scrape_calendar(calendar_url=None):
    url = calendar_url or DEFAULT_URL
    try:
        response = requests.get(url, timeout=25, headers=HEADERS)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Pima fetch error: {e}")
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    meetings = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" not in href.lower():
            continue
        low = href.lower()
        if not any(k in low for k in MEETING_KEYWORDS):
            continue

        full = urljoin(url, href)
        name = _basename(full)
        title = a.get_text(strip=True) or name

        agenda_url = ""
        minutes_url = ""
        nlow = name.lower()
        if "minute" in nlow:
            minutes_url = full
        elif "agenda" in nlow or "packet" in nlow or "meeting" in nlow:
            agenda_url = full
        else:
            agenda_url = full

        meetings.append(
            {
                "Meeting Title/Name": title,
                "Meeting Date": "",
                "Meeting Time": "",
                "Meeting Location": "Pima Town Hall",
                "Agenda URL": agenda_url,
                "Minutes URL": minutes_url,
                "Video URL": "",
                "Meeting Status": "",
            }
        )

    return meetings


if __name__ == "__main__":
    m = scrape_calendar()
    print(f"Found {len(m)} document rows")

import logging
from bs4 import BeautifulSoup
import re
import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _towncloud_from_city_portal(portal_url):
    """Official site links to TownCloud; table data loads there."""
    try:
        r = requests.get(portal_url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if "towncloud.io" in h:
                return h
    except requests.RequestException as exc:
        logger.warning("Springerville portal resolution failed for %s: %s", portal_url, exc)
    return None


def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://www.springervilleaz.gov/agendas"

    fetch_url = calendar_url
    if calendar_url and "springervilleaz.gov" in calendar_url and "towncloud.io" not in calendar_url:
        resolved = _towncloud_from_city_portal(calendar_url)
        if resolved:
            fetch_url = resolved

    try:
        response = requests.get(fetch_url, timeout=15, headers=HEADERS)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("Springerville agenda fetch failed for %s: %s", fetch_url, exc)
        raise

    soup = BeautifulSoup(response.content, "html.parser")
    meetings = []

    table = soup.find("table", {"id": "agenda-datatable"})
    if not table:
        message = "Springerville agenda response is missing the expected TownCloud table"
        logger.warning(message)
        raise RuntimeError(message)

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        title = cells[0].get_text(strip=True)
        if not title:
            continue
        date_time_str = cells[1].get_text(strip=True)

        date_match = re.search(r"(\w+ \d{1,2}, \d{4})", date_time_str)
        time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:am|pm))", date_time_str, re.IGNORECASE)

        date_str = date_match.group(1) if date_match else None
        time_str = time_match.group(1) if time_match else None

        meeting = {
            "title": title,
            "date": date_str,
            "time": time_str,
            "location": None,
            "agenda_url": None,
            "minutes_url": None,
            "video_url": None
        }

        # Agenda URL
        agenda_link = cells[2].find("a", {"title": "Download the agenda PDF"})
        if agenda_link:
            meeting["agenda_url"] = f"https://towncloud.io{agenda_link['href']}"

        # Minutes URL
        minutes_link = cells[3].find("a", {"title": "Download the minutes PDF"})
        if minutes_link:
            meeting["minutes_url"] = f"https://towncloud.io{minutes_link['href']}"

        meetings.append(meeting)

    if not meetings:
        message = "Springerville TownCloud table contained no server-rendered meeting rows"
        logger.warning(message)
        raise RuntimeError(message)

    return meetings


if __name__ == "__main__":
    meetings = scrape_calendar()
    for meeting in meetings:
        print(meeting)
    print(f"Found {len(meetings)} meetings.")

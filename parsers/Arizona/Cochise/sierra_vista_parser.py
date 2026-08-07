"""Sierra Vista — CivicEngage meeting parser."""
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def scrape_calendar(calendar_url=None):
    if not calendar_url:
        calendar_url = "https://www.sierravistaaz.gov/our-city/advanced-components/list-detail-pages/calendar-list"

    try:
        response = requests.get(calendar_url, timeout=15, headers=HEADERS)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Sierra Vista calendar fetch failed for %s: %s", calendar_url, exc)
        raise

    soup = BeautifulSoup(response.content, "html.parser")
    if soup.find("table") is None:
        message = "Sierra Vista calendar response is missing the expected table"
        logger.warning(message)
        raise RuntimeError(message)
    meetings = []

    for row in soup.select("table tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        title = cells[0].get_text(strip=True)
        if not title or len(title) < 3:
            continue
        date_cell = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        link = row.find("a", href=True)
        agenda = ""
        if link:
            href = link["href"]
            if href.startswith("http"):
                agenda = href
            else:
                agenda = urljoin(calendar_url, href)

        meetings.append(
            {
                "Meeting Title/Name": title,
                "Meeting Date": date_cell,
                "Meeting Time": "",
                "Meeting Location": "",
                "Agenda URL": agenda,
                "Minutes URL": "",
                "Video URL": "",
                "Meeting Status": "",
            }
        )

    if not meetings:
        message = (
            "Sierra Vista calendar table yielded no recognizable meeting rows; "
            "cannot distinguish an empty calendar from source drift"
        )
        logger.warning(message)
        raise RuntimeError(message)
    return meetings


if __name__ == "__main__":
    print(scrape_calendar())

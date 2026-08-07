#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.maranaaz.gov/Council/Public-Notices"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def scrape_calendar(url=None):
    """Scrape Marana's Public Notices HTML."""
    target = url or DEFAULT_URL
    try:
        response = requests.get(target, timeout=20, headers=HEADERS)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Marana public-notices fetch failed for %s: %s", target, exc)
        raise

    if len(response.text) < 2000:
        message = (
            "Marana public-notices response was unexpectedly short: "
            f"{len(response.text)} bytes"
        )
        logger.warning(message)
        raise RuntimeError(message)

    soup = BeautifulSoup(response.content, "html.parser")
    meetings = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or len(text) < 5:
            continue
        low = text.lower()
        if "agenda" not in low and "council" not in low and "meeting" not in low:
            continue
        if not href.startswith("http"):
            href = urljoin(target, href)
        meetings.append(
            {
                "Meeting Title/Name": text,
                "Meeting Date": "",
                "Meeting Time": "",
                "Agenda URL": href,
                "Minutes URL": "",
                "Video URL": "",
                "Meeting Location": "Marana Municipal Complex",
                "Meeting Status": "",
            }
        )

    if not meetings:
        message = (
            "Marana public-notices page contained no recognizable meeting links; "
            "cannot distinguish an empty calendar from source drift"
        )
        logger.warning(message)
        raise RuntimeError(message)
    return meetings

if __name__ == '__main__':
    meetings = scrape_calendar("https://www.maranaaz.gov/Council/Public-Notices")
    print(f"Found {len(meetings)} meetings.")
    for meeting in meetings:
        print(meeting)

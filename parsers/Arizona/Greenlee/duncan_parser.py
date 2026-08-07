import logging
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def scrape_calendar(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("Duncan agendas-and-minutes fetch failed for %s: %s", url, exc)
        raise

    soup = BeautifulSoup(response.content, "html.parser")
    meetings = []

    for a in soup.find_all("a", href=True):
        link_text = a.get_text().strip()
        link_url = a['href']

        # Look for a date in the format MM.DD.YYYY
        date_match = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', link_text)
        if not date_match:
            continue

        month, day, year = date_match.groups()
        if int(year) > datetime.now().year + 5:
            continue
        meeting_date = f"{year}-{month}-{day}"

        meeting_title = 'Town Meeting'
        if "special" in link_text.lower():
            meeting_title = "Special Meeting"
        elif "public hearing" in link_text.lower():
            meeting_title = "Public Hearing"
        elif "regular" in link_text.lower():
            meeting_title = "Regular Meeting"


        agenda_url = None
        minutes_url = None

        if "agenda" in link_text.lower():
            agenda_url = urljoin(url, link_url) if link_url and not link_url.startswith("http") else link_url
        if "minutes" in link_text.lower():
            minutes_url = urljoin(url, link_url) if link_url and not link_url.startswith("http") else link_url

        # Check if a meeting with this date already exists
        existing_meeting = next((m for m in meetings if m['Meeting Date'] == meeting_date), None)

        if existing_meeting:
            if agenda_url and not existing_meeting['Agenda URL']:
                existing_meeting['Agenda URL'] = agenda_url
            if minutes_url and not existing_meeting['Minutes URL']:
                existing_meeting['Minutes URL'] = minutes_url
        else:
            meetings.append({
                'Meeting Title/Name': meeting_title,
                'Meeting Date': meeting_date,
                'Meeting Time': '',
                'Agenda URL': agenda_url,
                'Minutes URL': minutes_url,
                'Video URL': '',
                'Meeting Location': '',
                'Meeting Status': ''
            })

    if not meetings:
        message = (
            "Duncan agendas-and-minutes page yielded no recognizable dated documents; "
            "cannot distinguish an empty archive from source drift"
        )
        logger.warning(message)
        raise RuntimeError(message)
    return meetings

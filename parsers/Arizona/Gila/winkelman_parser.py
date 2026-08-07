import logging
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def scrape_calendar(url):
    """
    Scrapes the Winkelman Town Council Meeting Minutes page for meeting records.
    The page lists links to meeting minutes, which are used to derive the meeting date and minutes URL.
    """
    meetings = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("Winkelman meeting-minutes fetch failed for %s: %s", url, exc)
        raise

    soup = BeautifulSoup(response.content, 'html.parser')

    # The links are simple <a> tags with the date as the text.
    # Parse date-bearing anchor text as meeting records.
    # The date format appears to be MM/DD/YY or MM/DD/YYYY.
    date_formats = ['%m/%d/%y', '%m/%d/%Y']

    # Find all links that are likely meeting minutes links.
    # Based on the page structure, they seem to be direct links with date text.
    for a_tag in soup.find_all('a', href=True):
        link_text = a_tag.get_text(strip=True)
        
        # Clean up text that might contain non-date characters (e.g., extra spaces or hidden characters)
        cleaned_text = re.sub(r'\s+', ' ', link_text).strip()
        
        meeting_date_str = None
        # Try to find a date pattern in the text
        match = re.search(r'(\d{1,2}/\d{1,2}/\d{2,4})', cleaned_text)
        if match:
            meeting_date_str = match.group(1)
        
        if not meeting_date_str:
            # The site also renders some dates space-separated, e.g. "06 12 23".
            match_space = re.search(r'(\d{1,2}\s+\d{1,2}\s+\d{2,4})', cleaned_text)
            if match_space:
                # Convert space-separated date to slash-separated for standard parsing
                meeting_date_str = match_space.group(1).replace(' ', '/')

        if meeting_date_str:
            meeting_date = None
            for fmt in date_formats:
                try:
                    meeting_date = datetime.strptime(meeting_date_str, fmt).strftime('%Y-%m-%d')
                    break
                except ValueError:
                    continue

            if meeting_date:
                # Construct the full URL
                minutes_url = a_tag['href']
                if not minutes_url.startswith('http'):
                    # Handle relative URLs, though they seem to be absolute on this site
                    minutes_url = urljoin(url, minutes_url)

                # Construct the meeting title
                title = f"Town Council Meeting Minutes - {meeting_date_str}"

                meeting = {
                    'Meeting Title/Name': title,
                    'Meeting Date': meeting_date,
                    'Meeting Time': None,
                    'Meeting Location': None,
                    'Agenda URL': None,
                    'Minutes URL': minutes_url,
                    'Video URL': None,
                    'Agenda Packet URL': None,
                    'Meeting Status': 'Confirmed',
                    'eComment/Public Comment URL': None,
                    'Meeting ID': None,
                }
                meetings.append(meeting)

    if not meetings:
        message = (
            "Winkelman meeting-minutes page yielded no recognizable dated documents; "
            "cannot distinguish an empty archive from source drift"
        )
        logger.warning(message)
        raise RuntimeError(message)
    return meetings

if __name__ == '__main__':
    CALENDAR_URL = "https://winkelmanaz.gov/meeting-minutes/"
    print(f"Scraping meetings from: {CALENDAR_URL}")
    meeting_list = scrape_calendar(CALENDAR_URL)

    if meeting_list:
        print(f"Found {len(meeting_list)} meetings.")
        for i, meeting in enumerate(meeting_list[:5]):
            print(f"\n--- Meeting {i+1} ---")
            for key, value in meeting.items():
                print(f"{key}: {value}")
    else:
        print("No meetings found or an error occurred.")

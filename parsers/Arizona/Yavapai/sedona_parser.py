import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
from datetime import datetime

# Base URL for the calendar
BASE_URL = "https://www.sedonaaz.gov/i-want-to/advanced-components-not-displayed/calendar-meeting-list"
# The 'All' view URL with pagination.
ALL_MEETINGS_URL = BASE_URL + "/-toggle-all"
PAGINATION_URL_TEMPLATE = ALL_MEETINGS_URL + "/-npage-{}"
CITY_NAME = "Sedona"

def get_page_soup(url):
    """Fetches a page and returns a BeautifulSoup object."""
    try:
        # Using a different User-Agent to avoid being blocked
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': BASE_URL
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return BeautifulSoup(response.content, 'html.parser')
    except requests.exceptions.RequestException as e:
        print(f"Error fetching {url}: {e}")
        return None

def extract_meeting_data(row):
    """Extracts data from a single table row (tr)."""
    cols = row.find_all('td')
    
    # The 'All' view table has 5 columns: Event, Date/Time, Agenda, Minutes, Other
    if len(cols) < 5:
        return None # Skip header or malformed rows

    # 1. Meeting Title/Name (from the link in the first column)
    title_tag = cols[0].find('a')
    meeting_title = title_tag.text.strip() if title_tag else cols[0].text.strip()
    
    # 2. Meeting Date and Time
    date_time_text = cols[1].text.strip()
    
    # Extract date (MM/DD/YYYY)
    date_match = re.search(r'\d{2}/\d{2}/\d{4}', date_time_text)
    meeting_date = date_match.group(0) if date_match else None
    
    # Extract time range or single time (e.g., 3:00 p.m. or 10:30 a.m. - 11:30 a.m.)
    time_part = date_time_text.replace(meeting_date if meeting_date else '', '').strip()
    meeting_time = time_part if time_part else None
    
    # 3. Agenda URL
    agenda_link = cols[2].find('a')
    agenda_url = urljoin(BASE_URL, agenda_link['href']) if agenda_link else None

    # 4. Minutes URL
    minutes_link = cols[3].find('a')
    minutes_url = urljoin(BASE_URL, minutes_link['href']) if minutes_link else None

    # 5. Other URL (could be Video, Packet, etc.)
    other_link = cols[4].find('a')
    other_url = urljoin(BASE_URL, other_link['href']) if other_link else None
    
    # Standardize the output dictionary
    meeting = {
        "Meeting Title/Name": meeting_title,
        "Meeting Date": meeting_date,
        "Meeting Time": meeting_time,
        "Meeting Location": None, 
        "Agenda URL": agenda_url,
        "Minutes URL": minutes_url,
        "Video URL": None, 
        "Agenda Packet URL": None, 
        "Meeting Status": None, 
        "eComment/Public Comment URL": None, 
        "Meeting ID": None, 
        "notes": None,
    }
    
    # Attempt to map 'Other URL' to Video or Agenda Packet based on link text/title
    if other_url:
        link_text = other_link.text.strip().lower()
        if "video" in link_text or "webcast" in link_text:
            meeting["Video URL"] = other_url
        elif "packet" in link_text or "agenda packet" in link_text:
            meeting["Agenda Packet URL"] = other_url
        else:
            # Preserve unclassified "Other" links in notes.
            meeting["notes"] = f"Found unclassified 'Other URL': {other_url}"

    return meeting

def scrape_calendar(url=None):
    """
    Scrapes all available meetings from the City of Sedona calendar.
    The calendar uses a custom HTML table structure with URL-based pagination.
    """
    all_meetings = []
    
    # The site blocks automated page discovery, so bound pagination using the
    # manually observed archive size.
    # based on manual inspection (2836 items / 30 per page = 95 pages).
    TOTAL_PAGES = 95
    
    # 1. Loop through all pages
    for page_num in range(1, TOTAL_PAGES + 1):
        current_url = PAGINATION_URL_TEMPLATE.format(page_num)
        
        print(f"Scraping page {page_num}/{TOTAL_PAGES} from {current_url}...")
        soup = get_page_soup(current_url)
        
        if not soup:
            # Stop if there's a network error
            break

        # Find the main table container
        table_container = soup.find('div', id=lambda x: x and x.startswith('events_widget_'))
        
        if not table_container:
            print(f"Could not find the main table container on page {page_num}. Stopping.")
            break

        # Find all table rows (tr) in the body of the table
        rows = table_container.find('tbody').find_all('tr') if table_container.find('tbody') else []
        
        if not rows:
            print(f"No meetings found on page {page_num}. Assuming end of list.")
            break

        for row in rows:
            meeting = extract_meeting_data(row)
            if meeting:
                all_meetings.append(meeting)

    return all_meetings

if __name__ == '__main__':
    print(f"Starting scraper for {CITY_NAME}...")
    meetings = scrape_calendar()
    
    print(f"\n--- Scraped {len(meetings)} meetings ---")
    if meetings:
        print("\nSample Meeting 1:")
        for k, v in meetings[0].items():
            print(f"  {k}: {v}")
        
        if len(meetings) > 100:
            print("\nSample Meeting 100:")
            for k, v in meetings[99].items():
                print(f"  {k}: {v}")
    
    # Importers use scrape_calendar as the parser entry point.

import requests
from bs4 import BeautifulSoup
from datetime import datetime
import re

def scrape_calendar(url):
    """
    Scrapes the city council calendar from the SuiteOne Media iframe URL,
    which is embedded in the main page. The calendar appears to be a custom
    implementation by SuiteOne Media.

    Args:
        url: The URL of the main calendar page (not used directly for scraping).

    Returns:
        A list of meeting dictionaries.
    """
    # The actual calendar data is hosted on a SuiteOne Media iframe.
    SUITEONE_URL = "https://winslowaz.suiteonemedia.com/"
    meetings = []
    
    try:
        # Fetch the main SuiteOne Media page, which contains both upcoming and recent events
        response = requests.get(SUITEONE_URL)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching the SuiteOne Media URL: {e}")
        return meetings

    soup = BeautifulSoup(response.text, 'html.parser')

    # Find all tables containing event data (Upcoming and Recent)
    # The tables are identified by their class names.
    tables = soup.find_all('table', class_=['upcomingEventsTable', 'recentEventsTable'])
    
    BASE_URL = "https://winslowaz.suiteonemedia.com"
    
    def make_absolute(relative_url):
        """Converts relative URLs to absolute URLs."""
        if relative_url and relative_url.startswith('/'):
            return BASE_URL + relative_url
        return relative_url

    for table in tables:
        # Iterate over all rows in the table body
        tbody = table.find('tbody')
        if not tbody:
            continue
            
        for row in tbody.find_all('tr'):
            cols = row.find_all('td')
            
            # A valid meeting row should have at least 7 columns
            if len(cols) < 7:
                continue

            # Column 1: Meeting Title
            title_col = cols[0]
            title_tag = title_col.find('a')
            
            # Filter for City Council meetings
            if not title_tag or 'City Council' not in title_tag.text:
                continue
                
            meeting_title = title_tag.text.strip()
            
            # Column 2: Date | Time
            date_time_text = cols[1].text.strip()
            meeting_date = None
            meeting_time = None
            meeting_status = None
            
            parts = date_time_text.split('|')
            if len(parts) == 2:
                date_str = parts[0].strip()
                time_str = parts[1].strip()
                
                # Parse date
                try:
                    # Example: Jan 13, 2026
                    dt_object = datetime.strptime(date_str, '%b %d, %Y')
                    meeting_date = dt_object.strftime('%Y-%m-%d')
                except ValueError:
                    pass

                # Time is already in a good format
                meeting_time = time_str
            
            # Check for meeting status (e.g., CANCELLED)
            if 'CANCELLED' in meeting_title.upper():
                meeting_status = 'Cancelled'
            
            # Columns 3-7: Links (Agenda, Packet, Minutes, Documents, Media)
            # The columns are: Agenda, Packet, Minutes, Documents, Media
            agenda_url = cols[2].find('a')['href'] if cols[2].find('a') else None
            packet_url = cols[3].find('a')['href'] if cols[3].find('a') else None
            minutes_url = cols[4].find('a')['href'] if cols[4].find('a') else None
            documents_url = cols[5].find('a')['href'] if cols[5].find('a') else None
            media_url = cols[6].find('a')['href'] if cols[6].find('a') else None
            
            # Determine Agenda Packet URL
            agenda_packet_url = packet_url
            # If the dedicated 'Packet' column is empty, check the 'Documents' column
            if not agenda_packet_url and documents_url:
                agenda_packet_url = documents_url
            
            # Construct the meeting dictionary
            meeting = {
                'meeting_title': meeting_title,
                'meeting_date': meeting_date,
                'meeting_time': meeting_time,
                'meeting_location': None, # Location is not available in this table view
                'agenda_url': make_absolute(agenda_url),
                'minutes_url': make_absolute(minutes_url),
                'video_url': make_absolute(media_url),
                'agenda_packet_url': make_absolute(agenda_packet_url),
                'meeting_status': meeting_status,
                'ecomment_public_comment_url': None,
                'meeting_id': None
            }
            
            # Emit only rows with a date.
            if meeting_date:
                meetings.append(meeting)

    # The SuiteOne Media page seems to load all data on the main page, so no need for a second request.
    # Deduplicate defensively even though the row logic should already prevent repeats.
    unique_meetings = {}
    for m in meetings:
        key = (m['meeting_title'], m['meeting_date'], m['meeting_time'])
        unique_meetings[key] = m
        
    return list(unique_meetings.values())

if __name__ == '__main__':
    calendar_url = 'https://www.winslowaz.gov/page/boards-agendas-minutes'
    scraped_meetings = scrape_calendar(calendar_url)
    
    # Output the result in a format that can be easily checked
    print(f"Found {len(scraped_meetings)} meetings.")
    
    for i, meeting in enumerate(scraped_meetings[:5]):
        print(f"\nMeeting {i+1}:")
        for key, value in meeting.items():
            print(f"  {key}: {value}")

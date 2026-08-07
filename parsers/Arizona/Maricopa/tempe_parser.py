import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
from datetime import datetime

# The base URL for the calendar
BASE_URL = "https://tempe.hylandcloud.com/AgendaOnline/"
# The search results URL for 'Last Year' (dropid=4). This is the widest range available
# without complex browser automation for custom date ranges.
SEARCH_RESULTS_URL = urljoin(BASE_URL, "Meetings/Search?dropid=4")

def scrape_calendar(url=SEARCH_RESULTS_URL):
    """
    Scrapes the Tempe City Council calendar from the Hyland OnBase Agenda Online platform.

    A requests.Session preserves the cookies required by the search-results
    page, whose meetings are exposed as `tr.meeting-row` elements.

    Args:
        url (str): The URL of the meeting search results page.

    Returns:
        list: A list of dictionaries, each representing a meeting.
    """
    meetings = []
    session = requests.Session()
    
    # Use a common User-Agent to mimic a browser
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        # 1. Visit the base URL to establish a session/get cookies
        session.get(BASE_URL, headers=headers, timeout=10)
        
        # 2. Visit the search results URL using the established session
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL {url}: {e}")
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    
    # Meetings are contained in <tr> elements with class 'meeting-row'
    meeting_rows = soup.find_all('tr', class_='meeting-row')
    
    if not meeting_rows:
        print("No meeting rows found. The scraper may need adjustment or the page content is not as expected.")
        return []

    for row in meeting_rows:
        meeting = {
            "meeting_title": None,
            "meeting_date": None,
            "meeting_time": None,
            "meeting_location": None,
            "agenda_url": None,
            "minutes_url": None,
            "video_url": None,
            "agenda_packet_url": None,
            "meeting_status": None,
            "ecomment_public_comment_url": None,
            "meeting_id": None,
        }

        # Extract Meeting ID from the tr's data attribute
        meeting_id = row.get('data-meeting-id')
        if meeting_id:
            meeting["meeting_id"] = meeting_id

        # Find all table data cells (td) in the row
        tds = row.find_all('td')
        
        # The data is structured in specific <td> elements based on the 'data-sortable-type' attribute
        
        # 1. Meeting Title/Name (data-sortable-type="mtgName")
        title_td = row.find('td', {'data-sortable-type': 'mtgName'})
        if title_td:
            meeting["meeting_title"] = title_td.text.strip()
        
        # 2. Meeting Date and Time (data-sortable-type="mtgTime")
        date_time_td = row.find('td', {'data-sortable-type': 'mtgTime'})
        if date_time_td:
            date_time_text = date_time_td.text.strip()
            # Clean up the date string, removing extra whitespace and newlines
            date_time_text = re.sub(r'\s+', ' ', date_time_text).strip()
            
            dt_object = None
            # The format is typically "M/D/YYYY H:MM:SS PM/AM" or "M/D/YYYY H:MM PM/AM"
            try:
                # Try with seconds
                dt_object = datetime.strptime(date_time_text, '%m/%d/%Y %I:%M:%S %p')
            except ValueError:
                try:
                    # Try without seconds
                    dt_object = datetime.strptime(date_time_text, '%m/%d/%Y %I:%M %p')
                except ValueError:
                    pass # Keep dt_object as None

            if dt_object:
                meeting["meeting_date"] = dt_object.strftime('%Y-%m-%d')
                meeting["meeting_time"] = dt_object.strftime('%H:%M:%S')
            else:
                # Fallback: try to split and assign if parsing failed
                parts = date_time_text.split(' ')
                if len(parts) >= 2:
                    meeting["meeting_date"] = parts[0]
                    meeting["meeting_time"] = parts[1]
                else:
                    meeting["meeting_date"] = date_time_text
                    meeting["meeting_time"] = None

        # 3. Meeting Location (data-sortable-type="mtgLocation")
        location_td = row.find('td', {'data-sortable-type': 'mtgLocation'})
        if location_td:
            meeting["meeting_location"] = location_td.text.strip()

        # 4. Links
        # The links are in the last <td> element of the row (or the one without a data-sortable-type)
        links_td = row.find('td', class_=lambda x: x and 'visible-lg' in x and 'hidden-xs' in x and 'hidden-sm' in x)
        if not links_td:
            # Fallback for smaller screen size columns
            links_td = row.find('td', class_=lambda x: x and 'visible-sm' in x and 'hidden-xs' in x)
        if not links_td:
            # Final fallback: check all tds for links
            links_td = row.find_all('td')[-1] if row.find_all('td') else None

        if links_td and meeting_id:
            # Helper function to extract URL based on link ID prefix
            def get_link_url_by_id_prefix(prefix):
                # Find a link whose ID starts with the prefix and ends with the meeting_id
                link_element = links_td.find('a', id=lambda x: x and x.startswith(f'{prefix}_{meeting_id}'))
                if link_element and 'href' in link_element.attrs:
                    # Resolve the relative href against the platform base URL.
                    return urljoin(BASE_URL, link_element['href'])
                return None

            # Extract all required links
            meeting["agenda_url"] = get_link_url_by_id_prefix("lnkMeetingAgenda")
            meeting["agenda_packet_url"] = get_link_url_by_id_prefix("lnkAgendaPacket")
            
            # Minutes: link with ID prefix 'lnkMinutesPacket' or 'lnkMeetingSummary'
            meeting["minutes_url"] = get_link_url_by_id_prefix("lnkMinutesPacket") or get_link_url_by_id_prefix("lnkMeetingSummary")
            
            # Check the platform's conventional link IDs for optional video and
            # eComment URLs even when they are not otherwise visible in the row.
            meeting["video_url"] = get_link_url_by_id_prefix("lnkMeetingVideo")
            meeting["ecomment_public_comment_url"] = get_link_url_by_id_prefix("lnkEComment")

        # 5. Meeting Status
        if meeting["meeting_title"] and "CANCELED" in meeting["meeting_title"].upper():
            meeting["meeting_status"] = "CANCELED"
        elif meeting["meeting_date"]:
            try:
                # Use the parsed date for comparison
                meeting_dt = datetime.strptime(meeting["meeting_date"], '%Y-%m-%d')
                if meeting_dt.date() < datetime.now().date():
                    meeting["meeting_status"] = "Complete"
                else:
                    meeting["meeting_status"] = "Scheduled"
            except ValueError:
                pass # Keep as None if date parsing failed

        # Only add if a title was found (to filter out empty rows)
        if meeting["meeting_title"]:
            meetings.append(meeting)

    return meetings

if __name__ == '__main__':
    scraped_meetings = scrape_calendar()
    
    print(f"Total meetings found: {len(scraped_meetings)}")
    print("--- Sample Meetings (First 5) ---")
    for i, meeting in enumerate(scraped_meetings[:5]):
        print(f"Meeting {i+1}:")
        for key, value in meeting.items():
            if value:
                print(f"  {key}: {value}")
        print("-" * 20)

    if len(scraped_meetings) > 0:
        print(f"Scraper successfully extracted {len(scraped_meetings)} meetings.")
        print("Note: The scraper is limited to the 'Last Year' range due to the platform's search limitations.")
    else:
        print("Scraper failed to extract any meetings.")

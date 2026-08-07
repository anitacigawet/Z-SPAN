import requests
from bs4 import BeautifulSoup
from datetime import datetime
import re

BASE_URL = "https://www.sanluisaz.gov"
# CivicPlus calendars often require a wide date range to show all events in list view.
# The 'view=list' and 'showPastEvents=true' parameters are crucial.
CALENDAR_URL = f"{BASE_URL}/calendar.aspx?CID=28&view=list&showPastEvents=true&startDate=01/01/1900&endDate=12/31/2100"

def scrape_calendar(url):
    """
    Scrapes the San Luis, AZ city council calendar for all meeting data.
    The calendar is a CivicPlus calendar, which requires a specific URL format
    to display all historical and future events in a list view.
    """
    meetings = []
    
    try:
        # Fetch the list view page with past events included and a wide date range
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # CivicPlus list views commonly use this div as the event container.
        list_container = soup.find('div', id='ctl00_ctl00_MainContent_ModuleContent_ctl00_divList')
        
        # Fallback to a common CivicPlus class if the ID is not found
        if not list_container:
            list_container = soup.find('div', class_='list-view-events')
            
        # If still not found, use the whole soup.
        if not list_container:
            list_container = soup
            
        # Events use linked h3 titles followed by a details block.
        event_blocks = list_container.find_all('h3')
        
        # Treat linked h3 elements followed by date-bearing sibling content as
        # event blocks, excluding navigation and footer headings.
        
        valid_event_blocks = []
        for h3 in event_blocks:
            title_link = h3.find('a', href=True)
            if title_link:
                # Check if the next sibling contains date/time/location information
                next_sibling = h3.find_next_sibling()
                if next_sibling and re.search(r'\w+\s+\d{1,2},\s+\d{4}', next_sibling.get_text()):
                    valid_event_blocks.append(h3)
                    
        event_blocks = valid_event_blocks
            
        for block in event_blocks:
            meeting = {
                "Meeting Title/Name": None,
                "Meeting Date": None,
                "Meeting Time": None,
                "Meeting Location": None,
                "Agenda URL": None,
                "Minutes URL": None,
                "Video URL": None,
                "Agenda Packet URL": None,
                "Meeting Status": None,
                "eComment/Public Comment URL": None,
                "Meeting ID": None,
            }
            
            # 1. Extract Title and Details URL
            title_element = block.find('a', href=True)
            if title_element:
                meeting["Meeting Title/Name"] = title_element.get_text(strip=True)
                details_href = title_element['href']
                
                # Extract Meeting ID from the details URL
                match = re.search(r'EID=(\d+)', details_href)
                if match:
                    meeting["Meeting ID"] = match.group(1)
            else:
                continue
            
            # 2. Extract Date, Time, Location, and Links from the next sibling element
            date_time_location_block = block.find_next_sibling()
            
            # Skip non-tag siblings (like NavigableString)
            while date_time_location_block and date_time_location_block.name is None:
                date_time_location_block = date_time_location_block.find_next_sibling()
            
            if not date_time_location_block:
                continue # Should not happen for valid blocks
        
            text = date_time_location_block.get_text(strip=True)
            
            # Replace non-breaking spaces and other odd characters with a regular space
            text = text.replace('\xa0', ' ').replace('\u2009', ' ')
            
            # Regex to find date and time (up to the first time component)
            # Canonical meeting_time uses the start of a displayed time range.
            date_time_match = re.search(r'(\w+\s+\d{1,2},\s+\d{4}),\s*([\d:]+\s*[AP]M)', text)
            
            if date_time_match:
                date_str = date_time_match.group(1)
                time_str = date_time_match.group(2)
                
                # Clean up date string and format
                try:
                    date_obj = datetime.strptime(date_str, '%B %d, %Y')
                    meeting["Meeting Date"] = date_obj.strftime('%Y-%m-%d')
                except ValueError:
                    meeting["Meeting Date"] = date_str # Keep original if parsing fails
                    
                meeting["Meeting Time"] = time_str
            
            # Extract Location (text after '@')
            location_match = re.search(r'@\s*(.*)', text)
            if location_match:
                # Strip CivicPlus's "More Details" link text from the location.
                location_text = location_match.group(1).strip()
                # Remove "More Details" and any surrounding whitespace/newlines
                location_text = re.sub(r'\s*More Details\s*', '', location_text, flags=re.IGNORECASE).strip()
                meeting["Meeting Location"] = location_text
            
            # Check for Agenda/Minutes links in the same block
            links = date_time_location_block.find_all('a', href=True)
            for link in links:
                link_text = link.get_text(strip=True).lower()
                href = link['href']
                full_url = f"{BASE_URL}/{href}" if href.startswith('/') else href
                
                if 'agenda' in link_text and not meeting["Agenda URL"]:
                    meeting["Agenda URL"] = full_url
                elif 'minutes' in link_text and not meeting["Minutes URL"]:
                    meeting["Minutes URL"] = full_url
                elif 'video' in link_text and not meeting["Video URL"]:
                    meeting["Video URL"] = full_url
                elif 'packet' in link_text and not meeting["Agenda Packet URL"]:
                    meeting["Agenda Packet URL"] = full_url
            
            meetings.append(meeting)
            
    except requests.exceptions.RequestException as e:
        print(f"An error occurred while fetching the main calendar page: {e}")
        
    return meetings

if __name__ == '__main__':
    # meetings = scrape_calendar(CALENDAR_URL)
    # print(f"Scraped {len(meetings)} meetings.")
    # for m in meetings[:5]:
    #     print(m)
    pass

# The scraper is designed to handle the CivicPlus calendar structure.
# It uses a wide date range to ensure all historical and future events are captured.

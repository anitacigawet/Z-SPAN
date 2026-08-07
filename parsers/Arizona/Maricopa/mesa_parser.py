import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin

# Base URL for the Legistar calendar
BASE_URL = "https://mesa.legistar.com/Calendar.aspx"
CITY_NAME = "Mesa"

def extract_meeting_data(row, base_url, headers):
    """Extracts data from a single table row."""
    cells = row.find_all('td')
    if not cells:
        return None

    meeting = {
        'city_name': CITY_NAME,
        'meeting_title': None,
        'meeting_date': None,
        'meeting_time': None,
        'meeting_location': None,
        'agenda_url': None,
        'minutes_url': None,
        'video_url': None,
        'agenda_packet_url': None,
        'meeting_status': 'Scheduled', # Default status
        'ecomment_url': None,
        'meeting_id': None,
    }

    # Mapping of header text to standardized field name
    col_map = {
        'Name': 'meeting_title',
        'Meeting Date': 'meeting_date',
        'Meeting Time': 'meeting_time',
        'Meeting Location': 'meeting_location',
        'Agenda': 'agenda_url',
        'Agenda Packet': 'agenda_packet_url',
        'Minutes': 'minutes_url',
        'Video': 'video_url',
        'Meeting Details': 'meeting_details_url',
    }
    
    # Create an index map from headers to standardized keys
    index_map = {i: col_map[h] for i, h in enumerate(headers) if h in col_map}

    for i, cell in enumerate(cells):
        if i in index_map:
            key = index_map[i]
            
            # 1. Extract text content for simple fields
            if key in ['meeting_date', 'meeting_time', 'meeting_location']:
                meeting[key] = cell.text.strip()
            
            # 2. Extract link URLs for document fields and meeting title
            else:
                link = cell.find('a', href=True)
                
                if key == 'meeting_title':
                    # Meeting title is the text of the link in the 'Name' column
                    if link:
                        meeting[key] = link.text.strip()
                    else:
                        meeting[key] = cell.text.strip()
                    
                elif link and link.text.strip() not in ['Not available', '']:
                    url_path = link['href']
                    meeting[key] = urljoin(base_url, url_path)
                    
                    # Extract Meeting ID from Meeting Details URL
                    if key == 'meeting_details_url':
                        match = re.search(r'MeetingDetail\.aspx\?ID=(\d+)', url_path)
                        if match:
                            meeting['meeting_id'] = match.group(1)
                            
                    # Update status based on available links
                    if key == 'agenda_url':
                        meeting['meeting_status'] = 'Agenda Available'
                    elif key == 'minutes_url':
                        meeting['meeting_status'] = 'Minutes Available'
                    
                    # Check for "Results Minutes" text in the Minutes cell
                    if key == 'minutes_url' and cell.text.strip() == 'Results Minutes':
                        meeting['meeting_status'] = 'Results Minutes'

    # Final check for required fields
    if meeting['meeting_title'] and meeting['meeting_date']:
        return meeting
    return None

def get_form_data(soup):
    """Extracts required ASP.NET form data from the soup object."""
    return {
        '__VIEWSTATE': soup.find('input', {'name': '__VIEWSTATE'}).get('value', ''),
        '__VIEWSTATEGENERATOR': soup.find('input', {'name': '__VIEWSTATEGENERATOR'}).get('value', ''),
        '__EVENTVALIDATION': soup.find('input', {'name': '__EVENTVALIDATION'}).get('value', '')
    }

def scrape_calendar(url):
    """
    Scrapes all meeting data from the Legistar calendar page.
    """
    all_meetings = []
    session = requests.Session()
    current_url = url
    page_count = 0
    
    # 1. Initial GET request to get the necessary ASP.NET form fields
    try:
        response = session.get(current_url, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error on initial GET request: {e}")
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    form_data = get_form_data(soup)

    # 2. Single POST request to select "All Years" and click "Search Calendar"
    # This is the most robust way to simulate the form submission for this RadComboBox.
    payload = {
        **form_data,
        'ctl00$ContentPlaceHolder1$lstYears$Input': 'All Years',
        'ctl00$ContentPlaceHolder1$lstYears$text': 'All Years',
        'ctl00$ContentPlaceHolder1$lstYears$SelectedValue': '', # Empty string for "All Years" is a common Legistar pattern
        'ctl00$ContentPlaceHolder1$btnSearch': 'Search Calendar',
    }

    try:
        print("POST: Submitting form to view 'All Years' and search...")
        response = session.post(current_url, data=payload, timeout=30)
        response.raise_for_status()
        content = response.content
    except requests.RequestException as e:
        print(f"Error on 'All Years' POST request: {e}")
        return []

    # 3. Pagination loop to scrape all pages
    while True:
        page_count += 1
        print(f"Scraping page {page_count}...")
        soup = BeautifulSoup(content, 'html.parser')

        # Check for total records to confirm filter was applied
        record_info = soup.find('div', class_='rgInfoPart')
        if page_count == 1 and record_info:
            print(f"Record info: {record_info.text.strip()}")
            # If the record count is still low, the filter failed.
            if 'Page 1 of 1' in record_info.text and len(all_meetings) < 100:
                print("Filter failed to apply. Stopping.")
                break

        # Get table headers
        table = soup.find('table', class_='rgMasterTable')
        if not table:
            print("Could not find the main meeting table.")
            break
        headers = [th.text.strip() for th in table.find('thead').find_all('th')]

        # Extract meetings from the current page
        for row in table.find('tbody').find_all('tr', recursive=False):
            meeting = extract_meeting_data(row, url, headers)
            if meeting:
                all_meetings.append(meeting)

        # --- Pagination Control ---
        # Re-extract form data for the next postback
        form_data = get_form_data(soup)
        
        # Find the link for the next page number
        grid_div = soup.find('div', id='ctl00_ContentPlaceHolder1_gridCalendar')
        pager = grid_div.find('div', class_='rgWrap rgArrPart1') if grid_div else None
        
        event_target = None
        event_argument = ''
        
        if pager:
            # Look for the link with the next page number
            next_page_num = page_count + 1
            next_page_link = pager.find('a', text=str(next_page_num))
            
            if next_page_link:
                match = re.search(r"javascript:__doPostBack\('([^']+)',\s*'([^']*)'\)", next_page_link['href'])
                if match:
                    event_target = match.group(1)
                    event_argument = match.group(2)
            
            # Fallback: Check for the 'Next Pages' link (the '...' link)
            if not event_target:
                next_pages_link = pager.find('a', title='Next Pages')
                if next_pages_link:
                    match = re.search(r"javascript:__doPostBack\('([^']+)',\s*'([^']*)'\)", next_pages_link['href'])
                    if match:
                        event_target = match.group(1)
                        event_argument = match.group(2)

        if not event_target:
            print("No more pages found.")
            break

        # Prepare payload for the next page request
        payload = {
            **form_data,
            '__EVENTTARGET': event_target,
            '__EVENTARGUMENT': event_argument,
        }

        try:
            print(f"Posting for page {page_count + 1}...")
            response = session.post(current_url, data=payload, timeout=30)
            response.raise_for_status()
            content = response.content
        except requests.RequestException as e:
            print(f"Error on pagination POST request: {e}")
            break

    return all_meetings

if __name__ == '__main__':
    print(f"Starting scraper for {CITY_NAME}...")
    meetings_data = scrape_calendar(BASE_URL)
    print(f"\nScraped a total of {len(meetings_data)} meetings.")
    if meetings_data:
        print("\n--- Sample Meeting --- ")
        # Print the most recent meeting found
        for key, value in meetings_data[0].items():
            print(f"{key}: {value}")

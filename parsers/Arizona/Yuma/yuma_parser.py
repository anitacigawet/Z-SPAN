import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

def scrape_calendar(url):
    """
    Scrapes all available meetings from a Legistar calendar page.

    Args:
        url (str): The URL of the Legistar calendar page.

    Returns:
        list: A list of dictionaries, each representing a meeting.
    """
    session = requests.Session()
    meetings = []
    
    # 1. Initial GET request to get the page and extract form data
    try:
        response = session.get(url)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error during initial GET request: {e}")
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    form = soup.find('form', id='aspnetForm')
    if not form:
        print("Could not find the main ASP.NET form.")
        return []

    # Extract hidden form fields required for postback
    viewstate = form.find('input', {'name': '__VIEWSTATE'})['value']
    eventvalidation = form.find('input', {'name': '__EVENTVALIDATION'})['value']
    
    # Trigger the ASP.NET year-dropdown postback to request all years. The
    # dropdown list ID is ctl00$ContentPlaceHolder1$lstYears.
    
    # 2. POST request to select 'All' years and get the full list
    # The target for the postback is the dropdown list itself.
    post_data = {
        '__EVENTTARGET': 'ctl00$ContentPlaceHolder1$lstYears',
        '__EVENTARGUMENT': '',
        '__VIEWSTATE': viewstate,
        '__EVENTVALIDATION': eventvalidation,
        'ctl00$ContentPlaceHolder1$lstYears': '', # The value for 'All' years
        'ctl00$ContentPlaceHolder1$txtSearch': '',
        'ctl00$ContentPlaceHolder1$lstBodies': '0', # '0' typically means all bodies/departments
        'ctl00$ContentPlaceHolder1$chkOptions$0': 'on', # Include notes
        'ctl00$ContentPlaceHolder1$chkOptions$1': 'on', # Include closed captions
    }

    try:
        # The Legistar page often requires a second POST to load the full table
        # after changing the year filter. The first POST only updates the page state.
        response = session.post(url, data=post_data)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error during POST request to select 'All' years: {e}")
        return []

    # 3. Parse the resulting HTML table
    soup = BeautifulSoup(response.content, 'html.parser')
    grid = soup.find('table', class_='rgMasterTable')
    
    if not grid:
        # If rgMasterTable is not found, try to find the grid by its ID
        grid = soup.find('div', id='ctl00_ContentPlaceHolder1_gridCalendar')
        if not grid:
            print("Could not find the meeting grid table.")
            return []

    # The table rows start from the second row (index 1) as the first is the header
    rows = grid.find_all('tr', class_=re.compile(r'rgRow|rgAltRow'))
    
    base_url = re.match(r'(https?://[^/]+)', url).group(0)

    for row in rows:
        cells = row.find_all('td')
        if len(cells) < 10: # Minimum expected number of columns
            continue

        # Helper function to get text and link from a cell
        def get_cell_data(cell_index):
            cell = cells[cell_index]
            link = cell.find('a', href=True)
            text = cell.get_text(strip=True)
            href = link['href'] if link and link['href'] not in ('#', 'javascript:void(0);') else None
            full_url = base_url + '/' + href.lstrip('/') if href else None
            return text, full_url

        # Column indices (based on typical Legistar table structure):
        # 0: Name (Title)
        # 1: Meeting Date
        # 2: Meeting Time
        # 3: Meeting Location
        # 4: Meeting Details (Status/ID often in the details page)
        # 5: Agenda
        # 6: Accessible Agenda
        # 7: Agenda Packet
        # 8: Minutes
        # 9: Accessible Minutes
        # 10: Video
        # 11: eComment/Public Comment (sometimes in a separate column or details page)

        # 0. Meeting Title/Name
        title_text, _ = get_cell_data(0)
        
        # 1. Meeting Date
        date_text, _ = get_cell_data(1)
        
        # 2. Meeting Time
        time_text, _ = get_cell_data(2)
        
        # 3. Meeting Location
        location_text, _ = get_cell_data(3)
        
        # 4. Meeting Details (often contains a link to a page with more info)
        _, details_url = get_cell_data(4)
        
        # 5. Agenda URL
        agenda_text, agenda_url = get_cell_data(5)
        agenda_url = agenda_url if agenda_text.lower() not in ('not available', 'agenda') else None
        
        # 7. Agenda Packet URL
        packet_text, packet_url = get_cell_data(7)
        packet_url = packet_url if packet_text.lower() not in ('not available', 'agenda packet') else None
        
        # 8. Minutes URL
        minutes_text, minutes_url = get_cell_data(8)
        minutes_url = minutes_url if minutes_text.lower() not in ('not available', 'minutes') else None
        
        # 10. Video URL
        video_text, video_url = get_cell_data(10)
        video_url = video_url if video_text.lower() not in ('not available', 'video') else None
        
        # Standardized field names
        meeting = {
            'Meeting Title/Name': title_text,
            'Meeting Date': date_text,
            'Meeting Time': time_text if time_text and time_text.lower() != 'not available' else None,
            'Meeting Location': location_text if location_text and location_text.lower() != 'not available' else None,
            'Agenda URL': agenda_url,
            'Minutes URL': minutes_url,
            'Video URL': video_url,
            'Agenda Packet URL': packet_url,
            # Legistar does not typically provide these fields directly in the calendar view
            'Meeting Status': None,
            'eComment/Public Comment URL': None,
            'Meeting ID': None,
        }
        
        # Attempt to extract Meeting ID from the details URL if available
        if details_url:
            match = re.search(r'MeetingID=(\d+)', details_url)
            if match:
                meeting['Meeting ID'] = match.group(1)

        meetings.append(meeting)

    return meetings

# if __name__ == '__main__':
#     CALENDAR_URL = "https://yuma-az.legistar.com/Calendar.aspx"
#     all_meetings = scrape_calendar(CALENDAR_URL)
#     print(f"Found {len(all_meetings)} meetings.")
#     # print(json.dumps(all_meetings[:5], indent=2))

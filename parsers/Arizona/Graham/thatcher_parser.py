"""Thatcher — CivicEngage meeting parser."""
import requests
from bs4 import BeautifulSoup
import json

def scrape_calendar(url):
    '''
    Scrapes all meeting data from the Thatcher, AZ city council calendar.

    Args:
        url (str): The URL of the calendar page.

    Returns:
        list: A list of dictionaries, where each dictionary represents a meeting.
    '''
    meetings = []
    page_num = 1
    # Use a generic User-Agent
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36'
    }
    
    # The URL to get all meetings is the base URL with the /-toggle-all suffix
    base_all_url = f"{url}/-toggle-all"

    while True:
        # Page 1 is just the base_all_url. Subsequent pages are base_all_url/-npage-X
        paginated_url = f"{base_all_url}/-npage-{page_num}" if page_num > 1 else base_all_url
        print(f"Fetching page: {page_num} from {paginated_url}")
        
        try:
            response = requests.get(paginated_url, headers=headers, timeout=10)
            response.raise_for_status()  # Raise an exception for bad status codes
        except requests.exceptions.RequestException as e:
            print(f"Error fetching URL for page {page_num}: {e}")
            # Stop pagination after an HTTP failure.
            break

        soup = BeautifulSoup(response.content, "html.parser")
        # The meeting list is inside a div with a specific ID, and each row has a class
        meeting_rows = soup.select("#events_widget_46_0_38 .cat_list_row")

        # No rows marks the end of pagination.
        if not meeting_rows:
            break

        for row in meeting_rows:
            meeting = {
                'meeting_title': None,
                'meeting_date': None,
                'meeting_time': None,
                'meeting_location': None,
                'agenda_url': None,
                'minutes_url': None,
                'video_url': None,
                'agenda_packet_url': None,
                'meeting_status': None,
                'ecomment_url': None,
                'meeting_id': None,
            }

            # Columns: 0: Event, 1: Date/Time, 2: Agenda, 3: Minutes, 4: Other
            columns = row.select('.cat_list_item')
            
            # 1. Meeting Title/Name (Column 0)
            title_element = columns[0].select_one('[href*="/-item-"]') if len(columns) > 0 else None
            if title_element:
                meeting['meeting_title'] = title_element.text.strip()
                # The detail page URL can be used as a unique ID
                meeting['meeting_id'] = title_element['href'].split('/')[-1]

            # 2. Date/Time (Column 1)
            if len(columns) > 1:
                datetime_element = columns[1]
                datetime_str = datetime_element.text.strip()
                # The format is "MM/DD/YYYY HH:MM AM/PM - HH:MM AM/PM" or just "MM/DD/YYYY"
                parts = datetime_str.split(' ')
                meeting['meeting_date'] = parts[0]
                if len(parts) > 1:
                    # Time is everything after the date
                    time_str = ' '.join(parts[1:]).strip()
                    # Clean up the time string if it contains a range
                    if ' - ' in time_str:
                        # meeting_time uses the start of the displayed range.
                        meeting['meeting_time'] = time_str.split(' - ')[0].strip()
                    else:
                        meeting['meeting_time'] = time_str
                
            # 3. Agenda URL (Column 2)
            if len(columns) > 2:
                agenda_col = columns[2]
                agenda_link = agenda_col.select_one('a')
                if agenda_link:
                    # Construct absolute URL
                    meeting['agenda_url'] = url.split('/government')[0] + agenda_link['href']
            
            # 4. Minutes URL (Column 3)
            if len(columns) > 3:
                minutes_col = columns[3]
                minutes_link = minutes_col.select_one('a')
                if minutes_link:
                    # Construct absolute URL
                    meeting['minutes_url'] = url.split('/government')[0] + minutes_link['href']
                            
            # 5. Agenda Packet/Video URL (Column 4 - "Other")
            if len(columns) > 4:
                other_col = columns[4]
                # The vendor's first "Other" link is the agenda packet.
                other_link = other_col.select_one('a')
                if other_link:
                    # Construct absolute URL
                    meeting['agenda_packet_url'] = url.split('/government')[0] + other_link['href']


            meetings.append(meeting)

        # Check for the "Next »" link to determine if there are more pages
        # This check is only for robustness, the loop structure handles pagination via page_num
        next_page_link = soup.select_one('#Pager_widget_46_0_38 a[href*="Next"]')
        if next_page_link:
            page_num += 1
        else:
            break

    return meetings

if __name__ == '__main__':
    calendar_url = "https://www.thatcher.az.gov/government/advanced-components/list-detail-pages/calendar-meeting-list"
    scraped_meetings = scrape_calendar(calendar_url)
    
    # Map vendor fields into canonical names.
    standardized_meetings = []
    for m in scraped_meetings:
        standardized_meetings.append({
            'Meeting Title/Name': m['meeting_title'],
            'Meeting Date': m['meeting_date'],
            'Meeting Time': m['meeting_time'],
            'Meeting Location': m['meeting_location'],
            'Agenda URL': m['agenda_url'],
            'Minutes URL': m['minutes_url'],
            'Video URL': m['video_url'],
            'Agenda Packet URL': m['agenda_packet_url'],
            'Meeting Status': m['meeting_status'],
            'eComment/Public Comment URL': m['ecomment_url'],
            'Meeting ID': m['meeting_id'],
        })

    print(json.dumps(standardized_meetings[:5], indent=4))
    print(f"Total meetings found: {len(standardized_meetings)}")

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import urljoin
import json
import sys

# Tucson uses OnBase Agenda Online (Hyland Cloud)
BASE_URL = "https://tucsonaz.hylandcloud.com/221agendaonline"
SEARCH_URL = f"{BASE_URL}/Meetings/Search"

def scrape_calendar(calendar_url=None):
    """
    Scrapes meeting data from Tucson's OnBase Agenda Online system.
    Returns a list of meeting dictionaries.
    """
    # The caller's calendar_url is unused because Hyland exposes fixed search endpoints.
    meetings = []
    
    # Search for meetings in the last year and upcoming
    # The dropid parameter: 0=Last Year, 3=Recent/Upcoming, 6=This Year
    for dropid in [0, 3, 6]:  # Last Year, Recent/Upcoming, This Year
        try:
            url = f"{SEARCH_URL}?dropid={dropid}"
            print(f"Fetching meetings from: {url}", file=sys.stderr)
            
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Find all year sections
            year_links = soup.find_all('a', id=lambda x: x and x.startswith('lnkMeetingYear'))
            
            # If no year sections, look for meeting rows directly
            meeting_sections = soup.find_all('div', class_='meeting-section')
            if not meeting_sections:
                # Try alternative structure - look for tables with meeting data
                meeting_sections = soup.find_all('table', class_=lambda x: x and 'meeting' in x.lower() if x else False)
            
            # Parse meeting rows
            # OnBase uses a table structure with specific IDs
            meeting_rows = soup.find_all('tr', id=lambda x: x and 'rowMeeting' in x if x else False)
            
            if not meeting_rows:
                # Alternative: find all rows that contain meeting data
                # Look for rows with agenda links
                all_rows = soup.find_all('tr')
                for row in all_rows:
                    agenda_link = row.find('a', id=lambda x: x and 'lnkMeetingAgenda_' in x if x else False)
                    if agenda_link:
                        meeting_rows.append(row)
            
            for row in meeting_rows:
                try:
                    meeting = {}
                    
                    # Extract meeting name/title from the first column
                    name_cell = row.find('td', class_=lambda x: x and 'meeting-name' in x.lower() if x else False)
                    if not name_cell:
                        # Try finding by text content
                        cells = row.find_all('td')
                        if len(cells) > 0:
                            name_cell = cells[0]
                    
                    if name_cell:
                        meeting_name = name_cell.get_text(strip=True)
                        meeting['Meeting Title/Name'] = meeting_name
                    
                    # Extract meeting type from the second column
                    type_cell = row.find('td', class_=lambda x: x and 'meeting-type' in x.lower() if x else False)
                    if not type_cell and len(cells) > 1:
                        type_cell = cells[1]
                    
                    # Extract meeting date and time from the third column
                    date_cell = row.find('td', class_=lambda x: x and 'meeting-date' in x.lower() if x else False)
                    if not date_cell and len(cells) > 2:
                        date_cell = cells[2]
                    
                    if date_cell:
                        date_text = date_cell.get_text(strip=True)
                        # Parse date like "2/18/2026 11:30:00 AM"
                        try:
                            dt = datetime.strptime(date_text, '%m/%d/%Y %I:%M:%S %p')
                            meeting['Meeting Date'] = dt.strftime('%Y-%m-%d')
                            meeting['Meeting Time'] = dt.strftime('%I:%M %p')
                        except:
                            try:
                                # Try without time
                                dt = datetime.strptime(date_text, '%m/%d/%Y')
                                meeting['Meeting Date'] = dt.strftime('%Y-%m-%d')
                                meeting['Meeting Time'] = ''
                            except:
                                print(f"Could not parse date: {date_text}", file=sys.stderr)
                                continue
                    
                    # Extract agenda URL
                    agenda_link = row.find('a', id=lambda x: x and 'lnkMeetingAgenda_' in x if x else False)
                    if agenda_link and 'href' in agenda_link.attrs:
                        meeting['Agenda URL'] = urljoin(BASE_URL, agenda_link['href'])
                    else:
                        meeting['Agenda URL'] = ''
                    
                    # Extract minutes/summary URL
                    minutes_link = row.find('a', id=lambda x: x and 'lnkMinutes_' in x if x else False)
                    if minutes_link and 'href' in minutes_link.attrs:
                        meeting['Minutes URL'] = urljoin(BASE_URL, minutes_link['href'])
                    else:
                        meeting['Minutes URL'] = ''
                    
                    # Extract agenda packet URL (download link)
                    packet_link = row.find('a', id=lambda x: x and 'lnkMeetingAgendaDoc_' in x if x else False)
                    if packet_link and 'href' in packet_link.attrs:
                        meeting['Agenda Packet URL'] = urljoin(BASE_URL, packet_link['href'])
                    else:
                        meeting['Agenda Packet URL'] = ''
                    
                    # Set default values
                    meeting['Meeting Location'] = 'Tucson City Hall'
                    meeting['Video URL'] = ''
                    meeting['Meeting Status'] = 'Past' if meeting.get('Minutes URL') else 'Scheduled'
                    meeting['eComment URL'] = ''
                    
                    # Emit only rows with a date.
                    if meeting.get('Meeting Date'):
                        meetings.append(meeting)
                        
                except Exception as e:
                    print(f"Error parsing meeting row: {e}", file=sys.stderr)
                    continue
                    
        except requests.exceptions.RequestException as e:
            print(f"Error fetching {url}: {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"Unexpected error: {e}", file=sys.stderr)
            continue
    
    # Remove duplicates based on date and title
    seen = set()
    unique_meetings = []
    for meeting in meetings:
        key = (meeting.get('Meeting Date'), meeting.get('Meeting Title/Name'))
        if key not in seen:
            seen.add(key)
            unique_meetings.append(meeting)
    
    return unique_meetings

if __name__ == '__main__':
    meetings = scrape_calendar()
    print(json.dumps(meetings, indent=2))
    print(f"\nTotal meetings found: {len(meetings)}", file=sys.stderr)

import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

def parse_meeting_string(meeting_string):
    """
    Parses a meeting string like "City Council Meeting - January 20, 2026 at 5:30 p.m."
    to extract the title, date, and time.
    """
    # 1. Extract the date and time from the end of the string
    date_pattern = r'([A-Za-z]+ \d{1,2}(?:st|nd|rd|th)?(?:,)? \d{4})'
    time_pattern = r'(?:at\s*)?(\d{1,2}:\d{2}(?:\s*(?:a\.?m\.?|p\.?m\.?))?)'
    
    # Full pattern: [title part] [separator] [date] [separator] [time]
    # Capture everything before the date as the title.
    full_pattern = re.compile(r'(.*?)[\s-]*' + date_pattern + r'[\s-]*' + time_pattern + r'?', re.IGNORECASE)
    
    match = full_pattern.search(meeting_string)
    
    title = meeting_string.strip()
    date_str = ""
    time_str = ""
    
    if match:
        # Group 1 is the title part before the date
        # Group 2 is the date
        # Group 3 is the time (optional)
        
        title_part = match.group(1).strip(' -').strip()
        date_str = match.group(2).replace('st', '').replace('nd', '').replace('rd', '').replace('th', '').strip()
        time_str = match.group(3).strip() if match.group(3) else ""
        
        # Clean up title
        title = title_part
    
    # Fallback for strings that are just a date
    if not date_str:
        date_match = re.search(date_pattern, meeting_string)
        if date_match:
            date_str = date_match.group(1).replace('st', '').replace('nd', '').replace('rd', '').replace('th', '').strip()
            title = meeting_string.replace(date_match.group(0), '').strip(' -').strip()
            time_str = ""
        
    # Clean up title by removing common prefixes
    title = re.sub(r'^(City Council Meeting|Special City Council Meeting|Quorum Notice|Cancelled|Quroum Notice|City Council Work Session|Proposed Graffiti Ordinance|Public Hearing|Cemetery Work Session)\s*[\s-]*', '', title, flags=re.IGNORECASE).strip()
    
    # Handle empty title
    if not title:
        title = "City Council Event"

    # Handle meeting status
    meeting_status = 'Cancelled' if 'Cancelled' in meeting_string else ''
    
    return {
        'meeting_title': title,
        'meeting_date': date_str,
        'meeting_time': time_str,
        'meeting_status': meeting_status
    }

def scrape_calendar(url):
    """
    Scrapes the Willcox, AZ City Council meeting calendar.
    """
    meetings = []
    try:
        # Fetch the page content
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
    except requests.exceptions.RequestException as e:
        print(f"Error fetching the page: {e}")
        return []

    # The meetings are in an accordion structure, which is a common Squarespace block.
    # The main container for a single meeting is an <li> element with class 'accordion-item'.
    accordion_items = soup.find_all('li', class_='accordion-item')
    
    for item in accordion_items:
        # 1. Extract the meeting title, date, time, and status from the button
        # The button is inside a div with class 'accordion-item__title'
        button = item.find('button')
        if not button:
            continue
            
        meeting_string = button.get_text(strip=True)
        
        # Filter out non-meeting/quorum items if necessary, though the structure is specific
        if not re.search(r'(City Council|Quorum Notice|Work Session|Public Hearing)', meeting_string, re.IGNORECASE):
            continue
            
        parsed_data = parse_meeting_string(meeting_string)
        
        # Initialize meeting dictionary
        meeting = {
            'meeting_title': parsed_data['meeting_title'],
            'meeting_date': parsed_data['meeting_date'],
            'meeting_time': parsed_data['meeting_time'],
            'meeting_location': '', # Not explicitly available in the main string
            'agenda_url': '',
            'minutes_url': '',
            'video_url': '',
            'agenda_packet_url': '',
            'meeting_status': parsed_data['meeting_status'],
            'ecomment_url': '',
            'meeting_id': '',
        }
        
        # 2. Extract links from the accordion content
        # The content div is a sibling of the title div, but it's a div with class 'accordion-item__dropdown'
        content_div = item.find('div', class_='accordion-item__dropdown')
        
        if content_div:
            # Find all links within the content div
            links = content_div.find_all('a', href=True)
            
            for link in links:
                link_text = link.get_text(strip=True).lower()
                link_url = link['href']
                
                # Check for full URL and prepend base URL if relative
                if not link_url.startswith('http'):
                    # Assuming the base URL is the main domain, not the calendar page
                    base_url_match = re.match(r'(https?://[^/]+)', url)
                    if base_url_match:
                        base_url = base_url_match.group(1)
                        link_url = base_url + link_url
                    else:
                        # Skip links when the base URL cannot be determined.
                        continue
                
                if 'agenda' in link_text and not meeting['agenda_url']:
                    meeting['agenda_url'] = link_url
                elif 'packet' in link_text and not meeting['agenda_packet_url']:
                    meeting['agenda_packet_url'] = link_url
                elif 'minutes' in link_text and not meeting['minutes_url']:
                    meeting['minutes_url'] = link_url
                elif 'video' in link_text and not meeting['video_url']:
                    meeting['video_url'] = link_url
        
        meetings.append(meeting)

    return meetings

if __name__ == '__main__':
    calendar_url = "https://willcox.az.gov/city-council-meetings-agendas-resolutions-1"
    
    print(f"Scraping calendar for {calendar_url}...")
    meetings_data = scrape_calendar(calendar_url)
    
    print(f"Found {len(meetings_data)} meetings.")
    
    for i, meeting in enumerate(meetings_data[:5]):
        print(f"\n--- Meeting {i+1} ---")
        for key, value in meeting.items():
            if value:
                print(f"{key}: {value}")

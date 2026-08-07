import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

def scrape_calendar(url):
    """
    Scrapes the Patagonia, AZ city council calendar from a custom HTML page.

    The page lists meeting documents (Agenda, Minutes, Video, Notice) as links.
    The scraper groups these links by meeting date and type to form a single
    meeting record.
    """
    try:
        # 1. Fetch the page content
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL {url}: {e}")
        return []

    # 2. Parse the HTML
    soup = BeautifulSoup(response.content, 'html.parser')

    # 3. Find the container for the meeting links
    # Meeting-document links appear after the main title; date-bearing anchor
    # text distinguishes them from navigation links.
    
    # Regex to find the date in MM-DD-YYYY format at the end of the link text
    date_pattern = re.compile(r'(\d{2}-\d{2}-\d{4})$')
    
    # Dictionary to store meetings, keyed by a unique identifier (Date + Normalized Title)
    meetings = {}
    
    # Common meeting details from the static text on the page
    default_time = "6:00 PM"
    default_location = "Town Hall Council Chambers, 310 McKeown Avenue, Patagonia"

    # Find all links on the page
    all_links = soup.find_all('a', href=True)

    for link in all_links:
        link_text = link.get_text(strip=True)
        link_href = link['href']
        
        # Check if the link text contains a date in the expected format
        match = date_pattern.search(link_text)
        
        if match:
            date_str = match.group(1)
            
            # Normalize the link text to get the meeting title and document type
            # Remove the date and any trailing/leading spaces
            base_title = date_pattern.sub('', link_text).strip()
            
            # Determine the document type and clean up the title
            doc_type = None
            
            if 'AGENDA' in base_title.upper():
                doc_type = 'Agenda'
                # Remove common document type words from the title
                title_parts = re.split(r'\s+(?:AMENDED\s+)?AGENDA', base_title, flags=re.IGNORECASE)
                meeting_title = title_parts[0].strip()
            elif 'MINUTES' in base_title.upper():
                doc_type = 'Minutes'
                title_parts = re.split(r'\s+MINUTES', base_title, flags=re.IGNORECASE)
                meeting_title = title_parts[0].strip()
            elif 'VIDEO' in base_title.upper():
                doc_type = 'Video'
                title_parts = re.split(r'\s+VIDEO', base_title, flags=re.IGNORECASE)
                meeting_title = title_parts[0].strip()
            elif 'NOTICE' in base_title.upper():
                doc_type = 'Notice'
                # Notices often contain the full meeting title
                title_parts = re.split(r'NOTICE\s+OF\s+', base_title, flags=re.IGNORECASE)
                meeting_title = title_parts[-1].strip() if len(title_parts) > 1 else base_title
                # Further clean up the title
                meeting_title = re.sub(r'\s+MEETING$', '', meeting_title, flags=re.IGNORECASE).strip()
            else:
                # Catch-all for other links that have a date but no clear doc type
                doc_type = 'Other'
                meeting_title = base_title
            
            # Further cleanup of the meeting title
            meeting_title = meeting_title.replace('-', ' ').strip()
            
            # Create a unique key for the meeting (Date + Title)
            # This handles cases where a single meeting has multiple documents
            meeting_key = f"{date_str}_{meeting_title}"
            
            # Initialize the meeting record if it doesn't exist
            if meeting_key not in meetings:
                meetings[meeting_key] = {
                    'Meeting Title/Name': meeting_title,
                    'Meeting Date': date_str,
                    'Meeting Time': default_time, # Default time, as it's not per-link
                    'Meeting Location': default_location, # Default location
                    'Agenda URL': None,
                    'Minutes URL': None,
                    'Video URL': None,
                    'Agenda Packet URL': None,
                    'Meeting Status': None,
                    'eComment/Public Comment URL': None,
                    'Meeting ID': None,
                }
            
            # Update the meeting record with the document URL
            if doc_type == 'Agenda':
                # Prefer the 'AMENDED AGENDA' if both exist, but for simplicity, just overwrite
                # as the last one found is usually the most complete/recent.
                meetings[meeting_key]['Agenda URL'] = link_href
            elif doc_type == 'Minutes':
                meetings[meeting_key]['Minutes URL'] = link_href
            elif doc_type == 'Video':
                meetings[meeting_key]['Video URL'] = link_href
            # Notice and Other links have no corresponding canonical URL field.
            
    # 4. Format the final list
    final_meetings = []
    for key, meeting in meetings.items():
        # Convert date format from MM-DD-YYYY to YYYY-MM-DD for standardization
        try:
            date_obj = datetime.strptime(meeting['Meeting Date'], '%m-%d-%Y')
            meeting['Meeting Date'] = date_obj.strftime('%Y-%m-%d')
        except ValueError:
            # Keep the source format if parsing fails.
            pass 
            
        final_meetings.append(meeting)

    return final_meetings

if __name__ == '__main__':
    CALENDAR_URL = "https://patagonia-az.gov/mayor-council/council-meetings-agendas-minutes/"
    
    print(f"Scraping {CALENDAR_URL}...")
    meetings_data = scrape_calendar(CALENDAR_URL)
    
    print(f"Found {len(meetings_data)} unique meetings.")
    
    # Print a few samples
    for i, meeting in enumerate(meetings_data[:5]):
        print(f"\n--- Meeting {i+1} ---")
        for key, value in meeting.items():
            if value:
                print(f"{key}: {value}")

    # For full output, uncomment the following line:
    # import json; print(json.dumps(meetings_data, indent=2))

import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

# Morenci is a census-designated place without a municipal calendar. This
# parser reads the Greenlee County public-notices source configured for it.

def parse_date_from_text(text):
    """
    Attempts to find and parse a date from a string using common formats.
    Returns the date string in YYYY-MM-DD format or None.
    """
    # Regex to find dates in various formats
    date_patterns = [
        # Month Name Day, Year (e.g., January 6, 2026)
        r'([A-Za-z]+ \d{1,2},? \d{4})',
        # YYYY-MM-DD or YYYY/MM/DD or YYYY.MM.DD
        r'(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})',
        # MM-DD-YYYY or MM/DD/YY etc.
        r'(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})',
    ]

    # Date formats to try for parsing
    date_formats = [
        '%B %d, %Y', '%B %d %Y', # Month Name Day, Year
        '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d',
        '%m-%d-%Y', '%m/%d/%Y', '%m.%d.%Y',
        '%m-%d-%y', '%m/%d/%y', '%m.%d.%y',
    ]

    for pattern in date_patterns:
        match = re.search(pattern, text)
        if match:
            date_str = match.group(1).replace('.', '-').replace('/', '-').replace(',', '')
            
            # Attempt to parse the date
            for fmt in date_formats:
                try:
                    # Handle two-digit year by assuming it's in the 21st century
                    if fmt.endswith('%y') and len(date_str.split('-')[-1]) == 2:
                        year = int(date_str.split('-')[-1])
                        current_year = datetime.now().year
                        century = (current_year // 100) * 100
                        full_year = century + year
                        if full_year > current_year + 1: # If year is too far in the future, assume previous century
                            full_year -= 100
                        
                        # Reconstruct date string with 4-digit year
                        parts = date_str.split('-')
                        parts[-1] = str(full_year)
                        date_str = '-'.join(parts)
                        fmt = fmt.replace('%y', '%Y') # Update format to 4-digit year
                        
                    dt_obj = datetime.strptime(date_str, fmt)
                    return dt_obj.strftime('%Y-%m-%d')
                except ValueError:
                    continue
    return None

def scrape_calendar(url):
    """
    Scrapes the Greenlee County Public Notices page for meeting information.
    """
    try:
        # 1. Fetch the HTML content
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL {url}: {e}")
        return []

    # 2. Parse the HTML
    soup = BeautifulSoup(response.content, 'html.parser')
    meetings = []

    # 3. Find all relevant links. The notices are in a main content area.
    # Scan href-bearing anchors for meeting-related keywords.
    
    # Find the main content area, which seems to be the div with class 'entry-content'
    # or the one containing the list of notices.
    # A general search for all <a> tags is still the most reliable given the custom site structure.
    all_links = soup.find_all('a', href=True)
    
    # Keep track of processed links to avoid duplicates.
    processed_links = set()

    for link in all_links:
        title_text = link.get_text(strip=True)
        href = link['href']
        
        # Skip links that are just the main page or navigation
        if href == url or href == '/public-notices/':
            continue

        # Filter for relevant meeting documents/videos
        keywords = ["Agenda", "Minutes", "Notice", "Meeting", "PSPRS", "P&Z", "BOS", "Video"]
        if not any(keyword in title_text for keyword in keywords):
            continue
        
        # Avoid processing the same link text/href combination multiple times
        if (title_text, href) in processed_links:
            continue
        processed_links.add((title_text, href))

        meeting = {
            'Meeting Title/Name': title_text,
            'Meeting Date': None,
            'Meeting Time': None,
            'Meeting Location': "Greenlee County Courthouse Annex, 253 5th Street, Clifton, Arizona", # General location from the notice
            'Agenda URL': None,
            'Minutes URL': None,
            'Video URL': None,
            'Agenda Packet URL': None,
            'Meeting Status': 'Confirmed', # Assuming confirmed unless otherwise noted
            'eComment/Public Comment URL': None,
            'Meeting ID': None,
        }

        # 4. Extract Meeting Date
        date_str = parse_date_from_text(title_text)
        if date_str:
            meeting['Meeting Date'] = date_str
            
        # 5. Determine document type and assign URL
        lower_title = title_text.lower()
        
        # Check for Video URL (based on the presence of YouTube links on the page)
        if 'youtube' in href.lower() or 'video' in lower_title:
            meeting['Video URL'] = href
            # For video links, the title is the meeting name
            if 'meeting' in lower_title:
                meeting['Meeting Title/Name'] = title_text.replace('YouTube Video', '').strip()
        elif 'minutes' in lower_title:
            meeting['Minutes URL'] = href
        elif 'agenda' in lower_title or 'notice' in lower_title:
            meeting['Agenda URL'] = href
        
        # Clean up the title if a date was found and is part of the title
        if meeting['Meeting Date']:
            # Attempt to remove the date from the title for a cleaner name
            # This is a heuristic and might not be perfect for all cases
            # Remove date and common separators from the title
            clean_title = re.sub(r'(\s*–\s*|\s*-\s*|\s*)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\s*[A-Za-z]+ \d{1,2},? \d{4}', '', title_text).strip()
            
            # Remove common prefixes/suffixes
            clean_title = re.sub(r'^(BOS|P&Z|PSPRS)\s+', '', clean_title, flags=re.IGNORECASE).strip()
            clean_title = re.sub(r'\s+(Agenda|Minutes|Notice|Meeting|Draft|Detailed Written Description)$', '', clean_title, flags=re.IGNORECASE).strip()
            
            if clean_title:
                meeting['Meeting Title/Name'] = clean_title
            
        # Require a title plus either a date or a document/video link.
        if meeting['Meeting Title/Name'] and (meeting['Meeting Date'] or meeting['Agenda URL'] or meeting['Minutes URL'] or meeting['Video URL']):
            meetings.append(meeting)

    return meetings

if __name__ == '__main__':
    url = "https://greenlee.az.gov/public-notices/"
    
    # Scrape the data
    scraped_meetings = scrape_calendar(url)
    
    # Print a summary of the results
    print(f"Scraped {len(scraped_meetings)} potential meeting records.")
    
    # Print the first few records for inspection
    for i, meeting in enumerate(scraped_meetings[:10]):
        print(f"\n--- Meeting {i+1} ---")
        for key, value in meeting.items():
            if value:
                print(f"{key}: {value}")

    pass

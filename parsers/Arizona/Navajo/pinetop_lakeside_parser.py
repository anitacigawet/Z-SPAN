import requests
import feedparser
import re
from datetime import datetime

# The base URL for the Town Council RSS feed
RSS_FEED_URL = "https://www.pinetoplakesideaz.gov/RSSFeed.aspx?ModID=65&CID=Town-Council-2"
CITY_NAME = "Pinetop-Lakeside"
CALENDAR_FORMAT = "CivicPlus RSS Feed"

def parse_meeting_title(title):
    """
    Parses the meeting title to extract the date, time, and clean title.
    Example title: "January 21, 2026 Town Council Special Meeting Agenda (PDF)"
    """
    # 1. Remove document type and any trailing info like "(PDF)" or "Update:..."
    clean_title = re.sub(r'\s*(\(PDF\)|Update:.*)$', '', title).strip()
    
    # 2. Regex to find a date pattern like "Month Day, Year" at the start
    date_match = re.match(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})', clean_title)
    
    meeting_date = None
    meeting_name = clean_title
    
    if date_match:
        date_str = date_match.group(1)
        try:
            # Attempt to parse the date
            meeting_date = datetime.strptime(date_str, '%B %d, %Y').strftime('%Y-%m-%d')
            # Remove the date from the title to get the clean meeting name
            meeting_name = clean_title[len(date_str):].strip()
        except ValueError:
            # If date parsing fails, use the original title
            pass
            
    # 3. Clean up the meeting name further by removing common meeting/document type suffixes.
    # This regex is designed to remove one or more of the specified terms, optionally
    # separated by "and", from the end of the string.
    suffixes = r'(Agenda|Minutes|Packet|Work Session|Regular Meeting|Special Meeting|Notice of Possible Quorum)'
    # Pattern to match one or more suffixes, possibly joined by "and" or just spaces, at the end.
    # The `(?: and )?` allows for an optional " and " between terms.
    # The `+` ensures it matches one or more occurrences.
    pattern = r'(?:\s+(?:and\s+)?' + suffixes + r')+\s*$'
    
    meeting_name = re.sub(pattern, '', meeting_name, flags=re.IGNORECASE).strip()
    
    # If the remaining name is empty, use the full clean title
    if not meeting_name:
        meeting_name = clean_title
        
    return meeting_date, meeting_name

def scrape_calendar(url=None):
    """
    Scrapes the Pinetop-Lakeside Town Council calendar from the RSS feed.
    """
    meetings = []
    
    try:
        # Use feedparser to handle the RSS feed
        feed = feedparser.parse(RSS_FEED_URL)
    except Exception as e:
        print(f"Error fetching or parsing RSS feed: {e}")
        return meetings

    for entry in feed.entries:
        title = entry.get('title', '')
        link = entry.get('link', '')
        guid = entry.get('guid', '')
        
        # Extract date and clean title
        meeting_date, meeting_name = parse_meeting_title(title)
        
        # Determine document URLs based on the title
        agenda_url = None
        minutes_url = None
        packet_url = None
        
        # Classify the row's single document link from its title.
        if "Agenda" in title:
            agenda_url = link
        if "Minutes" in title:
            minutes_url = link
        if "Packet" in title:
            packet_url = link
            
        # When the only document is not explicitly typed, preserve it as the
        # primary agenda URL.
        if not agenda_url and not minutes_url and not packet_url and link:
             agenda_url = link

        meeting = {
            'Meeting Title/Name': meeting_name,
            'Meeting Date': meeting_date,
            'Meeting Time': None,  # Not available in RSS feed
            'Meeting Location': None, # Not available in RSS feed
            'Agenda URL': agenda_url,
            'Minutes URL': minutes_url,
            'Video URL': None, # Not available in RSS feed
            'Agenda Packet URL': packet_url,
            'Meeting Status': None, # Not available in RSS feed
            'eComment/Public Comment URL': None, # Not available in RSS feed
            'Meeting ID': guid.split('/')[-1] if guid else None # Use the last part of the GUID as a unique ID
        }
        
        # Emit only rows with both a date and title.
        if meeting_date and meeting_name:
            meetings.append(meeting)
            
    return meetings

if __name__ == '__main__':
    print(f"Scraping calendar for {CITY_NAME}...")
    meetings = scrape_calendar()
    print(f"Found {len(meetings)} meetings.")
    
    for i, meeting in enumerate(meetings[:5]):
        print(f"\n--- Meeting {i+1} ---")
        for key, value in meeting.items():
            print(f"{key}: {value}")

    # The full list of meetings is available in the 'meetings' variable
    # for further processing or storage.

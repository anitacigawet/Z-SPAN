import requests
from bs4 import BeautifulSoup
from datetime import datetime
import re

# Base URL for the calendar page
BASE_URL = "https://miamiaz.gov"

def scrape_calendar(url):
    """
    Scrapes the Town of Miami, AZ public meetings calendar.

    The calendar is presented as a simple HTML table on the page.
    The function extracts the meeting date, title, agenda URL, and minutes URL.
    """
    meetings = []
    try:
        # 1. Fetch the page content
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL {url}: {e}")
        return []

    # 2. Parse the HTML
    soup = BeautifulSoup(response.content, 'html.parser')

    # 3. Find the main meeting table. The table is the only one with these specific headers.
    # Select the table containing the "Meeting" header.
    meeting_table = None
    for table in soup.find_all('table'):
        headers = [th.get_text(strip=True) for th in table.find_all('th')]
        if "Meeting" in headers and "Notice & Agenda" in headers and "Approved Minutes" in headers:
            meeting_table = table
            break

    if not meeting_table:
        print("Could not find the main meeting table.")
        return []

    # Get the index of the relevant columns
    headers = [th.get_text(strip=True) for th in meeting_table.find('thead').find_all('th')]
    try:
        meeting_col_idx = headers.index("Meeting")
        agenda_col_idx = headers.index("Notice & Agenda")
        minutes_col_idx = headers.index("Approved Minutes")
    except ValueError:
        print("Error: Could not find all required table headers.")
        return []

    # 4. Iterate over table rows (skipping the header row)
    for row in meeting_table.find('tbody').find_all('tr'):
        cells = row.find_all('td')
        if len(cells) < 4:
            continue # Skip incomplete rows

        # Extract raw text and links from the relevant columns
        meeting_text = cells[meeting_col_idx].get_text(strip=True)
        agenda_cell = cells[agenda_col_idx]
        minutes_cell = cells[minutes_col_idx]

        # --- Extract Meeting Date and Title ---
        # The format is typically "Month Day, Year Title"
        date_match = re.match(r"([A-Za-z]+ \d{1,2}, \d{4})", meeting_text)
        
        meeting_date_str = None
        meeting_title = meeting_text
        
        if date_match:
            date_part = date_match.group(1)
            try:
                # Attempt to parse the date
                meeting_date = datetime.strptime(date_part, "%B %d, %Y").strftime("%Y-%m-%d")
                meeting_date_str = meeting_date
                # The title is the rest of the string after the date
                meeting_title = meeting_text[len(date_part):].strip()
            except ValueError:
                # If date parsing fails, treat the whole thing as the title
                pass
        
        # Preserve the full text as the title when a date is not parseable.
        if not meeting_date_str:
            meeting_title = meeting_text
            meeting_date_str = None  # Use None when the date is unparseable.

        # --- Extract Agenda URL ---
        agenda_url = None
        # Prioritize links with "Agenda" or "Notice" text
        for link in agenda_cell.find_all('a'):
            link_text = link.get_text(strip=True).lower()
            if "agenda" in link_text or "notice" in link_text or "addendum" in link_text:
                href = link.get('href')
                if href:
                    # Construct absolute URL
                    agenda_url = href if href.startswith('http') else BASE_URL + href
                    # Break after finding the first relevant link (usually the most important one)
                    break

        # --- Extract Minutes URL ---
        minutes_url = None
        # Look for any link in the "Approved Minutes" column
        link = minutes_cell.find('a')
        if link:
            href = link.get('href')
            if href:
                # Construct absolute URL
                minutes_url = href if href.startswith('http') else BASE_URL + href

        # --- Standardize Output ---
        if meeting_title or meeting_date_str:
            meetings.append({
                "Meeting Title/Name": meeting_title,
                "Meeting Date": meeting_date_str,
                "Meeting Time": None,
                "Meeting Location": None,
                "Agenda URL": agenda_url,
                "Minutes URL": minutes_url,
                "Video URL": None,
                "Agenda Packet URL": None,
                "Meeting Status": None,
                "eComment/Public Comment URL": None,
                "Meeting ID": None,
            })

    return meetings

if __name__ == '__main__':
    CALENDAR_URL = "https://miamiaz.gov/departments/town-clerk/public-meetings/"
    print(f"Scraping meetings from: {CALENDAR_URL}\n")
    
    scraped_meetings = scrape_calendar(CALENDAR_URL)
    
    if scraped_meetings:
        print(f"Successfully scraped {len(scraped_meetings)} meetings.")
        print("\n--- Sample Meetings ---")
        # Print the first 5 meetings
        for i, meeting in enumerate(scraped_meetings[:5]):
            print(f"\nMeeting {i+1}:")
            for key, value in meeting.items():
                if value:
                    print(f"  {key}: {value}")
    else:
        print("No meetings were scraped.")

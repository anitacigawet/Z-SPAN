import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
from datetime import datetime

def scrape_calendar(url):
    """
    Scrapes the Hayden, AZ city council calendar for meeting information.

    Args:
        url (str): The URL of the meetings page.

    Returns:
        list: A list of dictionaries, each representing a meeting.
    """
    base_url = url.split('/meetings/')[0] + '/'
    meetings = []
    
    try:
        # 1. Fetch the page content
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching page: {e}")
        return []

    try:
        # 2. Parse the HTML
        soup = BeautifulSoup(response.content, 'html.parser')

        # Regex to find a date like "Month Day, Year"
        date_pattern = re.compile(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\s*,\s*\d{4}', re.IGNORECASE)
        
        # Find all <p> tags on the page.
        all_p_tags = soup.find_all('p')
        
        # A date-bearing paragraph starts each meeting entry.
        i = 0
        while i < len(all_p_tags):
            p_tag = all_p_tags[i]
            full_text = p_tag.get_text(separator=' ', strip=True)
            
            # 1. Check if this <p> tag contains a date (start of a new meeting)
            date_match = date_pattern.search(full_text)
            
            if date_match:
                meeting = {
                    'Meeting Title/Name': None,
                    'Meeting Date': None,
                    'Meeting Time': None,
                    'Meeting Location': "Town Hall: 520 Velasco Avenue", 
                    'Agenda URL': None,
                    'Minutes URL': None,
                    'Video URL': None,
                    'Agenda Packet URL': None,
                    'Meeting Status': None,
                    'eComment/Public Comment URL': None,
                    'Meeting ID': None,
                }
                
                # a. Date
                date_str = date_match.group(0).strip()
                
                # Attempt to parse the date
                try:
                    cleaned_date_str = re.sub(r'\s+', ' ', date_str).replace(' ,', ',').strip()
                    meeting['Meeting Date'] = datetime.strptime(cleaned_date_str, '%B %d, %Y').strftime('%Y-%m-%d')
                except ValueError:
                    i += 1
                    continue # Skip if date cannot be parsed

                # b. Title
                # The title is the text after the date in the same element
                title_raw = full_text[full_text.find(date_str) + len(date_str):].strip()
                
                # Clean up the title: remove link text and common separators
                title_clean = re.sub(r'(Notice & Agenda|Minutes|Minute|Amended Notice & Agenda)', '', title_raw, flags=re.IGNORECASE).strip()
                title_clean = re.sub(r'[\s\-\–\—\:\;]+', ' ', title_clean).strip()
                
                status = None
                if re.search(r'cancel(led|ed)', title_clean, re.IGNORECASE):
                    status = "Cancelled"
                    title_clean = re.sub(r'cancel(led|ed).*', '', title_clean, flags=re.IGNORECASE).strip()
                
                title_clean = title_clean.strip()
                if not title_clean:
                    title_clean = "City Council Meeting"
                
                meeting['Meeting Title/Name'] = title_clean
                meeting['Meeting Status'] = status
                
                # c. Links - look in the current and next two <p> tags
                
                # Check current <p> tag for links
                links_to_check = p_tag.find_all('a', href=True)
                
                # Check next two <p> tags for links
                for j in range(1, 3):
                    if i + j < len(all_p_tags):
                        links_to_check.extend(all_p_tags[i+j].find_all('a', href=True))
                
                # Process the collected links
                for link in links_to_check:
                    link_text = link.get_text(strip=True)
                    href = urljoin(base_url, link['href'])
                    
                    # Filter out anchor links
                    if href.endswith('#top') or href.endswith('#bottom') or href.endswith('#'):
                        continue
                    
                    if re.search(r'Notice & Agenda|Amended Notice & Agenda', link_text, re.IGNORECASE):
                        meeting['Agenda URL'] = href
                        meeting['Agenda Packet URL'] = href
                    elif re.search(r'Minutes|Minute', link_text, re.IGNORECASE):
                        meeting['Minutes URL'] = href
                
                meetings.append(meeting)
                
                # Advance past the date/title paragraph and its two link paragraphs.
                i += 3 
            else:
                i += 1 # Move to the next <p> tag
                
    except Exception as e:
        print(f"An unexpected error occurred during scraping: {e}")
        
    return meetings

if __name__ == '__main__':
    calendar_url = 'https://townofhaydenaz.gov/meetings/'
    scraped_meetings = scrape_calendar(calendar_url)
    
    print(f"Found {len(scraped_meetings)} meetings.")
    if scraped_meetings:
        print("\n--- Sample Meetings ---")
        for i, meeting in enumerate(scraped_meetings[:5]):
            print(f"\nMeeting {i+1}:")
            for key, value in meeting.items():
                print(f"  {key}: {value}")
        print("\n--- End Sample ---")
    

import requests
from bs4 import BeautifulSoup
import re

def scrape_calendar(url):
    """
    Scrapes all available meetings from the Municode Meetings list view.
    It handles pagination to ensure all meetings are collected.

    Args:
        url (str): The base URL of the Municode Meetings site (e.g., https://jerome-az.municodemeetings.com/).

    Returns:
        list: A list of dictionaries, where each dictionary represents a meeting.
    """
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'})
    
    base_list_url = url.rstrip('/') + '/meetings3'
    meetings_list = []
    page_num = 0
    
    while True:
        current_url = f"{base_list_url}?page={page_num}"

        
        try:
            response = session.get(current_url, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            # print(f"Error fetching page {page_num}: {e}")
            break

        meetings_soup = BeautifulSoup(response.content, 'html.parser')
        
        # The meetings are in a table within a div with class 'view-content'
        view_content = meetings_soup.find('div', class_='view-content')
        meetings_table = view_content.find('table') if view_content else None
        
        if not meetings_table:
            # This is a common issue with Municode. If the table is not found,
            # Absence can mean the list ended or the vendor structure changed.
            # print(f"Could not find the meetings table on page {page_num}. Stopping.")
            break

        # Skip the header row (first tr)
        rows = meetings_table.find_all('tr')[1:] 
        
        if not rows:
            # No meeting rows found, likely the end of the list
            break

        for row in rows:
            cols = row.find_all('td')
            if len(cols) < 7: # Expecting at least 7 columns
                continue

            # Column 0: Date/Time
            date_time_str = cols[0].get_text(strip=True)
            date_match = re.search(r'(\d{2}/\d{2}/\d{4})', date_time_str)
            time_match = re.search(r'(\d{1,2}:\d{2}(?:am|pm))', date_time_str, re.IGNORECASE)
            
            meeting_date = date_match.group(1) if date_match else None
            meeting_time = time_match.group(1) if time_match else None

            # Column 1: Meeting Title/Name
            title_element = cols[1].find('a')
            meeting_title = title_element.get_text(strip=True) if title_element else cols[1].get_text(strip=True)
            
            # Check for CANCELLED status in the title
            meeting_status = None
            if '*CANCELLED*' in meeting_title.upper():
                meeting_status = 'Cancelled'
                meeting_title = meeting_title.replace('*CANCELLED*', '').strip()
            
            # Helper to extract the best URL from a column (prefers PDF over HTML)
            def extract_url(col_index):
                links = cols[col_index].find_all('a')
                
                # Find the link with 'PDF' in the title attribute
                pdf_link = next((link for link in links if 'PDF' in link.get('title', '').upper()), None)
                
                # Fallback to the link with 'HTML' in the title attribute
                html_link = next((link for link in links if 'HTML' in link.get('title', '').upper()), None)
                
                # Return the best link's href, or None
                if pdf_link:
                    return pdf_link.get('href')
                elif html_link:
                    return html_link.get('href')
                return None

            # Column 2: Agenda
            agenda_url = extract_url(2)
            # Column 3: Agenda Packet
            agenda_packet_url = extract_url(3)
            # Column 4: Minutes
            minutes_url = extract_url(4)
            # Column 5: Video
            video_link = cols[5].find('a')
            video_url = video_link.get('href') if video_link else None
            
            # Construct the meeting dictionary
            meeting = {
                'Meeting Title/Name': meeting_title,
                'Meeting Date': meeting_date,
                'Meeting Time': meeting_time,
                'Meeting Location': None, # Not available in this list view
                'Agenda URL': agenda_url,
                'Minutes URL': minutes_url,
                'Video URL': video_url,
                'Agenda Packet URL': agenda_packet_url,
                'Meeting Status': meeting_status,
                'eComment/Public Comment URL': None, # Not available
                'Meeting ID': None, # Not explicitly available
            }
            
            meetings_list.append(meeting)

        # Check for the presence of a 'pager-next' link to determine if there's another page
        next_page_link = meetings_soup.find('li', class_='pager-next')
        if not next_page_link:
            break # No more pages
            
        page_num += 1

    return meetings_list

if __name__ == '__main__':
    CALENDAR_URL = "https://jerome-az.municodemeetings.com/"
    print(f"Scraping meetings from: {CALENDAR_URL}")
    
    meetings = scrape_calendar(CALENDAR_URL)
    
    if meetings:
        print(f"\nSuccessfully scraped {len(meetings)} meetings.")
        print("\n--- Sample Meetings (First 5) ---")
        for i, meeting in enumerate(meetings[:5]):
            print(f"Meeting {i+1}:")
            for key, value in meeting.items():
                print(f"  {key}: {value}")
        print("---------------------------------")
    else:
        print("\nFailed to scrape any meetings.")

    # Importers use scrape_calendar as the parser entry point.

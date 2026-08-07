"""Nogales — Granicus ViewPublisher meeting parser."""
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

def scrape_calendar(url):
    """Scrape meetings from Nogales Granicus publisher"""
    meetings = []
    
    # The actual data is in a Granicus iframe
    granicus_url = "https://nogalesaz.granicus.com/ViewPublisher.php?view_id=1"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        response = requests.get(granicus_url, headers=headers, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"Error fetching Nogales Granicus page: {e}")
        return []
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Granicus ViewPublisher typically has a table with meeting rows
    rows = soup.select('tr.listingRow, tr.listingRowAlt, tr[class*="Row"]')
    
    if not rows:
        # Try finding any table rows with meeting data
        rows = soup.find_all('tr')
    
    for row in rows:
        cells = row.find_all('td')
        if len(cells) < 2:
            continue
        
        # Try to extract meeting info from cells
        meeting_title = ''
        meeting_date = ''
        meeting_time = ''
        agenda_url = ''
        minutes_url = ''
        video_url = ''
        
        for i, cell in enumerate(cells):
            text = cell.get_text(strip=True)
            
            # Check for date patterns
            date_match = re.search(r'(\w+ \d{1,2},?\s*\d{4})', text)
            if date_match and not meeting_date:
                try:
                    parsed = datetime.strptime(date_match.group(1).replace(',', ''), '%B %d %Y')
                    meeting_date = parsed.strftime('%Y-%m-%d')
                except:
                    try:
                        parsed = datetime.strptime(date_match.group(1), '%B %d, %Y')
                        meeting_date = parsed.strftime('%Y-%m-%d')
                    except:
                        pass
            
            # Check for time patterns
            time_match = re.search(r'(\d{1,2}:\d{2}\s*[APap][Mm])', text)
            if time_match and not meeting_time:
                meeting_time = time_match.group(1)
            
            # Check for links
            links = cell.find_all('a')
            for link in links:
                href = link.get('href', '')
                link_text = link.get_text(strip=True).lower()
                
                if not href:
                    continue
                
                if not href.startswith('http'):
                    href = 'https://nogalesaz.granicus.com/' + href.lstrip('/')
                
                if 'agenda' in link_text:
                    agenda_url = href
                elif 'minute' in link_text:
                    minutes_url = href
                elif 'video' in link_text or 'watch' in link_text or 'player' in href.lower():
                    video_url = href
            
            # First substantial text cell is likely the title
            if text and not meeting_title and len(text) > 3 and not date_match and not time_match:
                meeting_title = text
        
        if meeting_date or meeting_title:
            meetings.append({
                'Meeting Title/Name': meeting_title or 'City Council Meeting',
                'Meeting Date': meeting_date,
                'Meeting Time': meeting_time,
                'Meeting Location': '777 North Grand Avenue, Nogales, AZ 85621',
                'Agenda URL': agenda_url,
                'Minutes URL': minutes_url,
                'Video URL': video_url,
                'Meeting Status': ''
            })
    
    return meetings

if __name__ == '__main__':
    results = scrape_calendar('https://www.nogalesaz.gov/165/Meetings-Agendas')
    print(f"Found {len(results)} meetings")
    for m in results[:5]:
        print(f"  {m['Meeting Date']} - {m['Meeting Title/Name']}")

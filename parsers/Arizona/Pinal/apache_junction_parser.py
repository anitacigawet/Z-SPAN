import requests
from bs4 import BeautifulSoup
from datetime import datetime
import re

def scrape_calendar(url):
    """Scrapes meetings from the Apache Junction Legistar calendar."""
    all_meetings = []
    current_year = datetime.now().year
    
    base_agenda_url = url.split('?')[0] + "?View=Agenda"
    base_domain = re.match(r"(https?://[^/]+)", url).group(0)

    # Only scrape current year and next year for speed
    for year in range(current_year, current_year + 2):
        scrape_url = f"{base_agenda_url}&Content=Meetings&Options=Advanced&Year={year}"
        
        try:
            response = requests.get(scrape_url, timeout=15)
            response.raise_for_status()
        except requests.exceptions.RequestException:
            continue

        soup = BeautifulSoup(response.content, 'html.parser')
        table = soup.find('table', id=re.compile(r'gridCalendar'))
        
        if not table:
            continue

        for row in table.find_all('tr')[1:]:
            cols = row.find_all('td')
            if len(cols) < 5:
                continue

            meeting = {
                'meeting_title': cols[0].get_text(strip=True),
                'meeting_date': cols[1].get_text(strip=True),
                'meeting_time': cols[3].get_text(strip=True) if len(cols) > 3 else '',
                'meeting_location': cols[4].get_text(strip=True) if len(cols) > 4 else '',
                'agenda_url': None,
                'minutes_url': None,
                'video_url': None,
            }
            
            # Extract links from remaining columns
            for i, col in enumerate(cols[5:], 5):
                link = col.find('a')
                if link and 'href' in link.attrs:
                    href = link['href']
                    if not href.startswith('http'):
                        href = requests.compat.urljoin(base_domain, href)
                    text = link.get_text(strip=True).lower()
                    if text == 'not available':
                        continue
                    if i == 6 and not meeting['agenda_url']:
                        meeting['agenda_url'] = href
                    elif i == 7 and not meeting['minutes_url']:
                        meeting['minutes_url'] = href
                    elif i == 8 and not meeting['video_url']:
                        meeting['video_url'] = href

            all_meetings.append(meeting)

    return all_meetings

def scrape_meetings(calendar_url=None):
    url = calendar_url or "https://apachejunction.legistar.com/Calendar.aspx"
    return scrape_calendar(url)

if __name__ == '__main__':
    meetings = scrape_meetings()
    print(f"Found {len(meetings)} meetings")
    for m in meetings[:3]:
        print(m)

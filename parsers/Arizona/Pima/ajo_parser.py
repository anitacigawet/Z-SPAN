import requests
from bs4 import BeautifulSoup

def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://www.ajowpccc.org/"

    meetings = []
    try:
        response = requests.get(calendar_url, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Scrape the main page for the next meeting
        next_meeting_element = soup.find('h2', string=lambda t: "Next WPCCC Meeting is" in t)
        if next_meeting_element:
            date_str = next_meeting_element.find_next('h2').get_text(strip=True)
            time_location_element = next_meeting_element.find_next('p')
            if time_location_element:
                time_location_str = time_location_element.get_text(strip=True)
                time_str = time_location_str.split('@')[1].strip().split(' ')[0]
                location_str = ' '.join(time_location_str.split('@')[2:])

                meetings.append({
                    'title': 'WPCCC Meeting',
                    'date': date_str,
                    'time': time_str,
                    'location': location_str
                })

        # Scrape the meeting notes page for past meetings
        notes_url = "https://www.ajowpccc.org/wpccc-meeting-notes"
        notes_response = requests.get(notes_url, timeout=15)
        notes_response.raise_for_status()
        notes_soup = BeautifulSoup(notes_response.text, 'html.parser')

        for link in notes_soup.select('a[href*="/s/"]'):
            if 'Meeting Notes' in link.text and '|' in link.text:
                date_str = link.text.split('|')[1].strip()
                meetings.append({
                    'title': 'Past WPCCC Meeting',
                    'date': date_str,
                    'agenda_url': 'https://www.ajowpccc.org' + link['href']
                })

    except requests.exceptions.RequestException as e:
        print(f"Error scraping {calendar_url}: {e}")

    return meetings

if __name__ == '__main__':
    meetings = scrape_calendar()
    print(f"Found {len(meetings)} meetings.")
    for meeting in meetings:
        print(meeting)

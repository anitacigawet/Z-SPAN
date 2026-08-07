"""Williams — CivicLive meeting parser."""

import requests
from bs4 import BeautifulSoup
from datetime import datetime

def scrape_calendar(url):
    '''
    Scrapes meeting data from the City of Williams, AZ website.

    Args:
        url (str): The URL of the meeting calendar page.

    Returns:
        list: A list of dictionaries, where each dictionary represents a meeting.
    '''
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except (requests.exceptions.RequestException, requests.exceptions.HTTPError) as e:
        print(f"Error fetching URL: {e}")
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    meetings = []

    table = soup.find('table')
    if not table:
        return []

    for row in table.find_all('tr')[1:]:
        cols = row.find_all('td')
        if len(cols) < 4:
            continue

        date_str = cols[0].text.strip()
        if "No Meeting" in date_str:
            continue

        try:
            # Handle cases where the date string might have extra text
            if '(' in date_str:
                date_str = date_str.split('(')[0].strip()
            meeting_date = datetime.strptime(date_str, '%B %d, %Y').strftime('%Y-%m-%d')
        except ValueError:
            continue

        agenda_link = cols[1].find('a')
        agenda_url = agenda_link['href'] if agenda_link and agenda_link.has_attr('href') else ''
        if agenda_url and not agenda_url.startswith('http'):
            agenda_url = f"https://www.williamsaz.gov{agenda_url}"


        minutes_link = cols[3].find('a')
        minutes_url = minutes_link['href'] if minutes_link and minutes_link.has_attr('href') else ''
        if minutes_url and not minutes_url.startswith('http'):
            minutes_url = f"https://www.williamsaz.gov{minutes_url}"


        meetings.append({
            'Meeting Title/Name': 'City Council',
            'Meeting Date': meeting_date,
            'Meeting Time': '',
            'Agenda URL': agenda_url,
            'Minutes URL': minutes_url,
            'Video URL': '',
            'Meeting Location': 'City Hall',
            'Meeting Status': ''
        })

    return meetings

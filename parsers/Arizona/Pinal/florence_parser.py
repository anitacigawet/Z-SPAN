
import json
from bs4 import BeautifulSoup
from polite_http import make_session

def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://www.florenceaz.gov/wp-json/tribe/events/v1/events?per_page=50"
    try:
        session = make_session()
        response = session.get(calendar_url, timeout=15)
        response.raise_for_status()  # Raise an exception for bad status codes
        data = response.json()
        meetings = []
        for event in data.get("events", []):
            title_lower = event.get("title", "").lower()
            if "town council" not in title_lower:
                continue

            # Extract video_url from the description
            video_url = None
            if "description" in event:
                soup = BeautifulSoup(event["description"], "html.parser")
                youtube_link = soup.find("a", href=lambda href: href and "youtube.com" in href)
                if youtube_link:
                    video_url = youtube_link["href"]

            meetings.append({
                "title": event.get("title"),
                "date": event.get("start_date", "").split(" ")[0],
                "time": event.get("start_date", "").split(" ")[1],
                "location": event.get("venue", {}).get("venue"),
                "agenda_url": event.get("url"),
                "video_url": video_url,
            })
        return meetings
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        print(f"Error scraping calendar: {e}")
        return []

if __name__ == "__main__":
    meetings = scrape_calendar()
    if meetings:
        for meeting in meetings:
            print(json.dumps(meeting, indent=4))
    else:
        print("No meetings found.")

import requests
from bs4 import BeautifulSoup
import re

def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://eloyaz.granicus.com/ViewPublisher.php?view_id=1"

    try:
        response = requests.get(calendar_url, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        meetings = []

        # Upcoming Events
        upcoming_events_div = soup.find("div", class_="upcomingEvents")
        if upcoming_events_div:
            upcoming_events_table = upcoming_events_div.find("table")
            if upcoming_events_table:
                for row in upcoming_events_table.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) == 2:
                        title = cells[0].text.strip()
                        date_time_str = cells[1].text.strip()
                        date_match = re.search(r"(\w+\s+\d{1,2},\s+\d{4})", date_time_str)
                        time_match = re.search(r"(\d{1,2}:\d{2}\s+[AP]M)", date_time_str)
                        date = date_match.group(1) if date_match else ""
                        time = time_match.group(1) if time_match else ""
                        meetings.append({"title": title, "date": date, "time": time})

        # Available Archives
        archive_table = soup.find("table", class_="listingTable")
        if archive_table:
            for row in archive_table.find("tbody").find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 4:
                    title = cells[0].text.strip()
                    date_time_str = cells[1].text.strip()
                    date_match = re.search(r"(\w+\s+\d{1,2},\s+\d{4})", date_time_str)
                    time_match = re.search(r"(\d{1,2}:\d{2}\s+[AP]M)", date_time_str)
                    date = date_match.group(1) if date_match else ""
                    time = time_match.group(1) if time_match else ""
                    agenda_link = cells[3].find("a", href=re.compile("Agenda"))
                    agenda_url = f"https://eloyaz.granicus.com/{agenda_link['href']}" if agenda_link else ""
                    video_url = ""
                    if len(cells) >= 5:
                        video_link = cells[4].find("a", text="Video")
                        if video_link and video_link.has_attr('href'):
                            video_url = f"https://eloyaz.granicus.com/{video_link['href']}"

                    meetings.append({
                        "title": title,
                        "date": date,
                        "time": time,
                        "agenda_url": agenda_url,
                        "video_url": video_url,
                    })

        return meetings

    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")
        return []

if __name__ == "__main__":
    meetings = scrape_calendar()
    if meetings:
        print(f"Found {len(meetings)} meetings.")
    else:
        print("No meetings found.")

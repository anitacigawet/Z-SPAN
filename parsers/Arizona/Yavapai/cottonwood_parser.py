
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://cottonwoodaz.granicus.com/ViewPublisher.php?view_id=1"

    try:
        response = requests.get(calendar_url, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching the calendar page: {e}")
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    meetings = []
    base_url = "https://cottonwoodaz.granicus.com/"

    # The HTML does not reliably distinguish upcoming from past events, so scan
    # every table.
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) > 1:
                title = cells[0].get_text(strip=True)
                date_time_str = cells[1].get_text(strip=True)
                date_parts = date_time_str.split("-")
                date = date_parts[0].strip()
                time = date_parts[1].strip() if len(date_parts) > 1 else None

                agenda_url = None
                minutes_url = None

                if len(cells) > 2:
                    agenda_link = cells[2].find("a", string="Agenda")
                    if agenda_link and agenda_link.has_attr("href"):
                        agenda_url = urljoin(base_url, agenda_link["href"])

                    minutes_link = cells[2].find("a", string="Minutes")
                    if minutes_link and minutes_link.has_attr("href"):
                        minutes_url = urljoin(base_url, minutes_link["href"])

                if title and date:
                    meetings.append({
                        "title": title,
                        "date": date,
                        "time": time,
                        "agenda_url": agenda_url,
                        "minutes_url": minutes_url,
                    })

    return meetings

if __name__ == "__main__":
    meetings = scrape_calendar()
    print(f"Found {len(meetings)} meetings.")
    for meeting in meetings:
        print(meeting)

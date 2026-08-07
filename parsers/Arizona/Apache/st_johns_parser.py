
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re

def scrape_calendar(calendar_url=None):
    if not calendar_url:
        calendar_url = "https://www.sjaz.us/meetings-agendas/"

    try:
        response = requests.get(
            calendar_url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching {calendar_url}: {e}")
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    meetings = []

    for year_header in soup.find_all("h3"):
        year_text = year_header.get_text()
        year_match = re.search(r'\d{4}', year_text)
        if year_match and ("City Council Meetings" in year_text or "Town Council Meeting" in year_text or "Town Council Meetings" in year_text):
            year = year_match.group(0)
            table = year_header.find_next("table")
            if table:
                for row in table.find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        date_str = cells[0].get_text(strip=True)
                        meeting_type = cells[1].get_text(strip=True)

                        if "rescheduled" in meeting_type.lower() or "cancelled" in meeting_type.lower():
                            continue

                        meeting = {
                            "title": meeting_type,
                            "date": f"{date_str}, {year}",
                            "time": None,
                            "location": None,
                            "agenda_url": None,
                            "minutes_url": None,
                            "video_url": None,
                        }

                        links = row.find_all("a")
                        for link in links:
                            link_text = link.get_text(strip=True).lower()
                            href = link.get("href")
                            if href:
                                full_url = urljoin(calendar_url, href)
                                if "agenda" in link_text:
                                    meeting["agenda_url"] = full_url
                                elif "minutes" in link_text:
                                    meeting["minutes_url"] = full_url

                        meetings.append(meeting)

    return meetings

if __name__ == "__main__":
    meetings = scrape_calendar()
    for meeting in meetings:
        print(meeting)
    print(f"Found {len(meetings)} meetings.")


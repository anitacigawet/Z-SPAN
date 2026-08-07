import requests
from bs4 import BeautifulSoup
import datetime

def scrape_calendar(calendar_url=None):
    meetings = []
    base_url = "https://public.destinyhosted.com/agenda_publish.cfm?id=24263&mt=ALL"

    current_date = datetime.date.today()
    # Scrape current month and the previous 11 months
    for i in range(6):
        month = current_date.month
        year = current_date.year

        url = f"{base_url}&get_month={month}&get_year={year}"

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")

            table = soup.find("table", id="meeting-table")

            if table:
                for row in table.find("tbody").find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        date_cell = cells[0]
                        date_link = date_cell.find("a")
                        if date_link:
                            date_str = date_link.text.strip()
                        else:
                            date_str = date_cell.text.strip()

                        title = cells[1].text.strip()

                        meeting = {
                            "title": title,
                            "date": date_str,
                        }

                        if date_link and date_link.has_attr("href"):
                            meeting["agenda_url"] = "https://public.destinyhosted.com/" + date_link["href"].lstrip("/")

                        if len(cells) > 2:
                            minutes_cell = cells[2]
                            minutes_link = minutes_cell.find("a")
                            if minutes_link and minutes_link.has_attr("href"):
                                meeting["minutes_url"] = "https://public.destinyhosted.com/" + minutes_link["href"].lstrip("/")

                        if len(cells) > 3:
                            video_cell = cells[3]
                            video_link = video_cell.find("a")
                            if video_link and video_link.has_attr("href"):
                                meeting["video_url"] = video_link["href"]

                        meetings.append(meeting)

        except requests.exceptions.RequestException as e:
            print(f"Error fetching {url}: {e}")

        # Move to the previous month
        if current_date.month == 1:
            current_date = current_date.replace(year=current_date.year - 1, month=12)
        else:
            current_date = current_date.replace(month=current_date.month - 1)

    return meetings

if __name__ == "__main__":
    meetings = scrape_calendar()
    print(f"Found {len(meetings)} meetings.")

import requests
from bs4 import BeautifulSoup
import dateparser

def scrape_calendar(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except (requests.exceptions.RequestException, requests.exceptions.HTTPError) as e:
        print(f"Error fetching the URL: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    meetings = []
    table = soup.find("table", id="ctl00_ContentPlaceHolder1_gridCalendar")

    if not table:
        return []

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        meeting_title = cells[0].text.strip()
        meeting_date_str = cells[1].text.strip()
        meeting_time = cells[2].text.strip()
        meeting_location = cells[3].text.strip()

        agenda_url = "Not available"
        agenda_cell = cells[5]
        if agenda_cell.find("a") and agenda_cell.find("a").has_attr("href"):
            if "Agenda" in agenda_cell.text:
                agenda_url = agenda_cell.find("a")["href"]

        minutes_url = "Not available"
        minutes_cell = cells[7]
        if minutes_cell.find("a") and minutes_cell.find("a").has_attr("href"):
            if "Minutes" in minutes_cell.text:
                minutes_url = minutes_cell.find("a")["href"]

        video_url = "Not available"
        if len(cells) > 8:
            video_cell = cells[8]
            if video_cell.find("a") and video_cell.find("a").has_attr("href"):
                if "Video" in video_cell.text:
                    video_url = video_cell.find("a")["href"]

        if agenda_url != "Not available" and not agenda_url.startswith("http"):
            agenda_url = "https://maricopa.legistar.com/" + agenda_url

        if minutes_url != "Not available" and not minutes_url.startswith("http"):
            minutes_url = "https://maricopa.legistar.com/" + minutes_url

        if video_url != "Not available" and not video_url.startswith("http"):
            video_url = "https://maricopa.legistar.com/" + video_url

        try:
            meeting_date = dateparser.parse(meeting_date_str).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        meetings.append({
            "Meeting Title/Name": meeting_title,
            "Meeting Date": meeting_date,
            "Meeting Time": meeting_time,
            "Meeting Location": meeting_location,
            "Agenda URL": agenda_url,
            "Minutes URL": minutes_url,
            "Video URL": video_url,
            "Meeting Status": "",
        })

    return meetings

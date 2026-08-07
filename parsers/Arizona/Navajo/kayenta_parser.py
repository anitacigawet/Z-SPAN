from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

def get_rendered_html(url, wait_time=5000):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, timeout=60000)
        page.wait_for_timeout(wait_time)
        html = page.content()
        browser.close()
        return html

def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://www.kayentatownship-nsn.gov/event/"

    try:
        html = get_rendered_html(calendar_url)
    except Exception as e:
        print(f"Error fetching calendar: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    meetings = []

    for day in soup.select(".tribe-events-calendar-month__day"): 
        date_link = day.select_one(".tribe-events-calendar-month__day-date-link")
        if date_link:
            date = date_link.text.strip()
            for event in day.select(".tribe-events-calendar-month__calendar-event"):
                title = event.select_one(".tribe-events-calendar-month__calendar-event-title").text.strip()
                time = event.select_one(".tribe-events-calendar-month__calendar-event-datetime").text.strip()
                meetings.append({
                    "title": title,
                    "date": date,
                    "time": time,
                })

    return meetings

if __name__ == "__main__":
    meetings = scrape_calendar()
    print(f"Found {len(meetings)} meetings.")
    for meeting in meetings:
        print(meeting)

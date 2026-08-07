
import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

async def get_rendered_html(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle")

        for _ in range(10):
            try:
                await page.locator("#startScreen").click(timeout=2000)
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                break

        html = await page.content()
        await browser.close()
        return html

def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://chinovalleyaz.portal.civicclerk.com/"

    html = asyncio.run(get_rendered_html(calendar_url))
    soup = BeautifulSoup(html, "html.parser")
    meetings = []

    for meeting_item in soup.select("a[role='button'][id]"):
        if not meeting_item.get("id") or not meeting_item.get("id").isdigit():
            continue

        title = meeting_item.select_one("h3").text.strip() if meeting_item.select_one("h3") else None

        text_nodes = [text.strip() for text in meeting_item.find_all(string=True, recursive=True) if text.strip()]
        
        date = None
        time = None
        location = None

        if text_nodes:
            # The first few text nodes usually contain date and time.
            # This is a heuristic and might need adjustment.
            date_time_info = text_nodes[0:5]
            date = " ".join(date_time_info[0:3])
            time = date_time_info[3]
            location = " ".join(date_time_info[4:])

        href = meeting_item.get("href")
        agenda_url = f"{calendar_url.rstrip('/')}{href}" if href else None

        if title and date:
            meetings.append({
                "title": title,
                "date": date,
                "time": time,
                "location": location,
                "agenda_url": agenda_url,
            })

    return meetings

if __name__ == "__main__":
    meetings_data = scrape_calendar()
    if meetings_data:
        print(f"Found {len(meetings_data)} meetings:")
        for meeting in meetings_data:
            print(meeting)
    else:
        print("No meetings found.")

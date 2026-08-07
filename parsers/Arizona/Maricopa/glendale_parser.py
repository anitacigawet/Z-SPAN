"""Glendale — Legistar meeting parser."""
from bs4 import BeautifulSoup

def scrape_calendar(calendar_url=None):
    url = calendar_url or "https://glendale-az.legistar.com/Calendar.aspx"
    meetings = []
    try:
        from playwright.sync_api import sync_playwright

        # Legistar's ASP.NET date-range control requires rendered interaction
        # to trigger the All Years postback.
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=15000)
            page.wait_for_load_state('networkidle', timeout=10000)
            # Change date range to All Years
            try:
                dropdown = page.locator('select[id*="drpDateRange"]')
                if dropdown.count() > 0:
                    dropdown.select_option(label="All Years")
                    page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                pass
            html = page.content()
            soup = BeautifulSoup(html, 'html.parser')
            table = soup.find('table', id='ctl00_ContentPlaceHolder1_gridCalendar')
            if table:
                for row in table.find_all('tr'):
                    cells = row.find_all('td')
                    if len(cells) < 4:
                        continue
                    title = cells[0].get_text(strip=True)
                    date_str = cells[1].get_text(strip=True)
                    time_str = cells[2].get_text(strip=True) if len(cells) > 2 else ''
                    location = cells[3].get_text(strip=True) if len(cells) > 3 else ''
                    agenda_url = None
                    minutes_url = None
                    video_url = None
                    for cell in cells:
                        for link in cell.find_all('a', href=True):
                            href = link['href']
                            lt = link.get_text(strip=True).lower()
                            full = href if href.startswith('http') else f'https://glendale-az.legistar.com/{href}'
                            if 'agenda' in lt:
                                agenda_url = full
                            elif 'minutes' in lt:
                                minutes_url = full
                            elif 'video' in lt:
                                video_url = full
                    if title and date_str:
                        meetings.append({
                            'title': title, 'date': date_str, 'time': time_str,
                            'location': location, 'agenda_url': agenda_url,
                            'minutes_url': minutes_url, 'video_url': video_url,
                        })
            browser.close()
    except Exception as e:
        print(f"Error scraping Glendale: {e}")
    return meetings

if __name__ == "__main__":
    results = scrape_calendar()
    print(f"Found {len(results)} meetings")
    for m in results[:5]:
        print(f"  {m['date']} - {m['title']}")

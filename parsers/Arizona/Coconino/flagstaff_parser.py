
import re
from datetime import date

import requests
from bs4 import BeautifulSoup


def _destiny_id(calendar_url):
    m = re.search(r"[?&]id=(\d+)", calendar_url or "")
    return m.group(1) if m else "35247"


def _parse_destiny_table(soup):
    meetings = []
    table = soup.find("table", class_="listclass")
    if not table:
        return meetings

    tbody = table.find("tbody")
    row_container = tbody if tbody else table

    for row in row_container.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        date_str = cells[0].get_text(strip=True)
        title = cells[1].get_text(strip=True)

        agenda_link = cells[0].find("a")
        if agenda_link:
            date_str = agenda_link.get_text(strip=True)

        meeting = {
            "title": title,
            "date": date_str,
        }

        for link in row.find_all("a"):
            link_text = link.get_text(strip=True).lower()
            href = link.get("href")
            if not href:
                continue

            if not href.startswith("http"):
                href = f"https://public.destinyhosted.com{href}"

            if "agenda" in link_text:
                meeting["agenda_url"] = href
            elif "minutes" in link_text:
                meeting["minutes_url"] = href
            elif "video" in link_text:
                meeting["video_url"] = href

        meetings.append(meeting)

    return meetings


def _fetch_month(board_id, year, month):
    url = (
        "https://public.destinyhosted.com/agenda_publish.cfm"
        f"?id={board_id}&mt=ALL&get_month={month}&get_year={year}"
    )
    try:
        response = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    return _parse_destiny_table(soup)


def _iter_months(start_y, start_m, end_y, end_m):
    y, m = start_y, start_m
    while (y, m) <= (end_y, end_m):
        yield y, m
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1


def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://public.destinyhosted.com/agenda_publish.cfm?id=35247"

    board_id = _destiny_id(calendar_url)
    today = date.today()

    start_y, start_m = today.year, today.month
    for _ in range(18):
        if start_m == 1:
            start_y -= 1
            start_m = 12
        else:
            start_m -= 1

    end_y, end_m = today.year, today.month
    for _ in range(12):
        if end_m == 12:
            end_y += 1
            end_m = 1
        else:
            end_m += 1

    seen = set()
    all_meetings = []

    for y, m in _iter_months(start_y, start_m, end_y, end_m):
        for row in _fetch_month(board_id, y, m):
            key = (
                row.get("title") or "",
                row.get("date") or "",
                row.get("agenda_url") or "",
            )
            if key in seen:
                continue
            seen.add(key)
            all_meetings.append(row)

    return all_meetings


if __name__ == "__main__":
    meetings = scrape_calendar()
    print(f"Found {len(meetings)} meetings (deduped across months).")

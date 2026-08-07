"""Peoria — NovusAgenda meeting parser."""
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

MAX_PAGES = 90


def _next_page_event_target(html):
    m = re.search(
        r'title="Next Page" href="javascript:__doPostBack\(&#39;([^&#]+)&#39;,&#39;[^&]*&#39;\)"',
        html,
    )
    return m.group(1) if m else None


def _parse_page_meetings(soup):
    meetings = []
    rows = soup.select("#SearchAgendasMeetings_radGridMeetings_ctl00 > tbody > tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        date_str = cells[0].get_text(strip=True)
        try:
            meeting_date = datetime.strptime(date_str, "%m/%d/%y").strftime("%Y-%m-%d")
        except ValueError:
            meeting_date = None

        meeting_type = cells[1].get_text(strip=True)
        meeting_location = cells[2].get_text(strip=True)

        agenda_url_tag = cells[3].find("a")
        agenda_url = agenda_url_tag["href"] if agenda_url_tag and agenda_url_tag.has_attr("href") else None
        if agenda_url and not agenda_url.startswith("http"):
            agenda_url = urljoin("https://peoriaaz.novusagenda.com/agendapublic/", agenda_url)

        minutes_url_tag = cells[5].find("a")
        minutes_url = minutes_url_tag["href"] if minutes_url_tag and minutes_url_tag.has_attr("href") else None
        if minutes_url and not minutes_url.startswith("http"):
            minutes_url = urljoin("https://peoriaaz.novusagenda.com/agendapublic/", minutes_url)

        meetings.append(
            {
                "Meeting Title/Name": meeting_type,
                "Meeting Date": meeting_date,
                "Meeting Time": "",
                "Agenda URL": agenda_url,
                "Minutes URL": minutes_url,
                "Video URL": None,
                "Meeting Location": meeting_location,
                "Meeting Status": "",
            }
        )
    return meetings


def _form_post_data(soup: BeautifulSoup, event_target: str):
    form = soup.find("form")
    if not form:
        return None
    data = {}
    for tag in form.find_all("input"):
        name = tag.get("name")
        if not name:
            continue
        data[name] = tag.get("value") or ""
    for tag in form.find_all("textarea"):
        name = tag.get("name")
        if name:
            data[name] = tag.get_text() or ""
    for tag in form.find_all("select"):
        name = tag.get("name")
        if not name:
            continue
        selected = tag.find("option", selected=True)
        if not selected:
            selected = tag.find("option")
        if selected is not None:
            data[name] = selected.get("value", selected.get_text(strip=True) or "")
        else:
            data[name] = ""
    data["__EVENTTARGET"] = event_target
    data["__EVENTARGUMENT"] = ""
    action = form.get("action")
    return data, action


def scrape_calendar(url):
    if not url:
        return []

    session = requests.Session()
    all_meetings = []
    # Deduplicate by date, title, and location. NovusAgenda's RadGrid can keep
    # rendering a "Next Page" link after the last data page, cycling back to
    # earlier rows and inflating the result.
    seen: set[tuple[str, str, str]] = set()
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    for _ in range(MAX_PAGES):
        soup = BeautifulSoup(resp.text, "html.parser")
        page_rows = _parse_page_meetings(soup)

        new_this_page = 0
        for meeting in page_rows:
            key = (
                meeting.get("Meeting Date") or "",
                meeting.get("Meeting Title/Name") or "",
                meeting.get("Meeting Location") or "",
            )
            if key in seen:
                continue
            seen.add(key)
            all_meetings.append(meeting)
            new_this_page += 1

        # If a full page produced zero new rows, the pager has cycled back to
        # already-seen content. Stop, regardless of whether a Next Page link
        # is still rendered.
        if new_this_page == 0:
            break

        nxt = _next_page_event_target(resp.text)
        if not nxt:
            break

        posted = _form_post_data(soup, nxt)
        if not posted:
            break
        data, action = posted
        post_url = urljoin(resp.url, action) if action else resp.url
        resp = session.post(post_url, data=data, headers=HEADERS, timeout=20)
        resp.raise_for_status()

    return all_meetings


if __name__ == "__main__":
    u = "https://peoriaaz.novusagenda.com/agendapublic/meetingsgeneral.aspx?meetingtype=1&Date=cus&From=01/01/2016&To=12/31/2026"
    m = scrape_calendar(u)
    print(f"Found {len(m)} meetings")

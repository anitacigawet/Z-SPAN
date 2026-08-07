"""Queen Creek — Granicus meeting parser."""
import sys
from datetime import datetime
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

DEFAULT_RSS = "https://queencreekaz.granicus.com/ViewPublisherRSS.php?view_id=3"
DEFAULT_HTML = "https://queencreekaz.granicus.com/ViewPublisher.php?view_id=3"
GRANICUS_BASE = "https://queencreekaz.granicus.com"

FIELD_MAPPING = {
    "Meeting Title/Name": "title",
    "Agenda URL": "link",
    "Minutes URL": "granicus_minutes_url",
    "Video URL": "granicus_video_url",
    "Agenda Packet URL": "granicus_agenda_packet_url",
    "Meeting ID": "granicus_meeting_id",
    "Meeting Status": "granicus_meeting_status",
    "eComment/Public Comment URL": "granicus_ecomment_url",
    "Meeting Location": "granicus_location",
}


def _meetings_from_rss(url):
    meetings = []
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"RSS parse error: {e}", file=sys.stderr)
        return meetings

    for entry in feed.entries:
        meeting = {
            "Meeting Title/Name": entry.get("title", "No Title Provided"),
            "Meeting Date": "",
            "Meeting Time": "",
        }
        published = entry.get("published")
        if published:
            try:
                dt_object = datetime.strptime(published, "%a, %d %b %Y %H:%M:%S %z")
                meeting["Meeting Date"] = dt_object.strftime("%Y-%m-%d")
                meeting["Meeting Time"] = dt_object.strftime("%H:%M:%S")
            except ValueError:
                pass

        for standard_field, granicus_field in FIELD_MAPPING.items():
            if standard_field == "Meeting Title/Name":
                continue
            value = entry.get(granicus_field)
            meeting[standard_field] = value if value is not None else ""

        meetings.append(meeting)

    return meetings


def _meetings_from_html(calendar_url):
    meetings = []
    try:
        response = requests.get(calendar_url, timeout=15)
        response.raise_for_status()
    except (requests.exceptions.RequestException, requests.exceptions.HTTPError) as e:
        print(f"HTML calendar fetch error: {e}", file=sys.stderr)
        return meetings

    soup = BeautifulSoup(response.content, "html.parser")
    table = soup.find("table")
    if not table:
        return meetings

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        title = cells[0].get_text(strip=True)
        date_str = cells[1].get_text(strip=True)
        agenda_url = ""
        if len(cells) > 2:
            agenda_link = cells[2].find("a")
            if agenda_link and agenda_link.get("href"):
                agenda_url = agenda_link["href"]
                if not agenda_url.startswith("http"):
                    agenda_url = urljoin(GRANICUS_BASE, agenda_url)

        meetings.append(
            {
                "Meeting Title/Name": title,
                "Meeting Date": date_str,
                "Meeting Time": "",
                "Meeting Location": "",
                "Agenda URL": agenda_url,
                "Minutes URL": "",
                "Video URL": "",
                "Agenda Packet URL": "",
                "Meeting Status": "",
                "eComment/Public Comment URL": "",
                "Meeting ID": "",
            }
        )

    return meetings


def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = DEFAULT_RSS

    parsed = urlparse(calendar_url)
    path_lower = (parsed.path or "").lower()

    if "viewpublisherrss.php" in path_lower:
        rss_url = calendar_url
    elif "viewpublisher.php" in path_lower:
        q = parsed.query
        rss_url = f"{GRANICUS_BASE}/ViewPublisherRSS.php?{q}" if q else DEFAULT_RSS
    else:
        rss_url = DEFAULT_RSS

    # Prefer Granicus RSS for the full feed; fall back to the ViewPublisher
    # HTML archive when RSS yields no meetings.
    meetings = _meetings_from_rss(rss_url)
    if meetings:
        return meetings

    html_url = DEFAULT_HTML
    if "viewpublisher.php" in path_lower:
        html_url = calendar_url

    return _meetings_from_html(html_url)


if __name__ == "__main__":
    m = scrape_calendar()
    print(f"Found {len(m)} meetings")

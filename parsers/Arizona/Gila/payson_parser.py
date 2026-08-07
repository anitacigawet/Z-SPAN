"""Payson — Granicus ViewPublisher meeting parser."""
import re
import sys
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    import feedparser
except ImportError:
    feedparser = None

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


def _rss_url(calendar_url):
    if not calendar_url:
        return "https://payson.granicus.com/ViewPublisherRSS.php?view_id=17"
    if "ViewPublisherRSS" in calendar_url:
        return calendar_url
    q = parse_qs(urlparse(calendar_url).query)
    ids = q.get("view_id") or q.get("View_ID")
    if ids:
        return f"https://payson.granicus.com/ViewPublisherRSS.php?view_id={ids[0]}"
    m = re.search(r"view_id=(\d+)", calendar_url, re.I)
    if m:
        return f"https://payson.granicus.com/ViewPublisherRSS.php?view_id={m.group(1)}"
    return "https://payson.granicus.com/ViewPublisherRSS.php?view_id=17"


def _from_rss(rss_url):
    meetings = []
    if feedparser is None:
        return meetings
    try:
        feed = feedparser.parse(rss_url)
    except Exception as e:
        print(f"Payson RSS error: {e}", file=sys.stderr)
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


def _from_html(calendar_url):
    try:
        response = requests.get(calendar_url, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching the URL: {e}")
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    meetings = []

    for table in soup.find_all("table", class_="listingTable"):
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) > 1:
                title = cells[0].get_text(strip=True)
                date_time_str = cells[1].get_text(strip=True)

                meeting = {
                    "title": title,
                    "date": "",
                    "time": "",
                    "agenda_url": None,
                    "minutes_url": None,
                    "video_url": None,
                }

                date_match = re.search(r"([A-Za-z]+\s\d{1,2},\s\d{4})", date_time_str)
                if date_match:
                    meeting["date"] = date_match.group(1)

                time_match = re.search(r"(\d{2}:\d{2}\s[AP]M)", date_time_str)
                if time_match:
                    meeting["time"] = time_match.group(1)

                for a in row.find_all("a", href=True):
                    link_text = a.get_text(strip=True).lower()
                    link_href = urljoin(calendar_url, a["href"])

                    if "agenda" in link_text:
                        meeting["agenda_url"] = link_href
                    elif "minutes" in link_text:
                        meeting["minutes_url"] = link_href
                    elif "video" in link_text:
                        meeting["video_url"] = link_href

                meetings.append(meeting)

    return meetings


def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://payson.granicus.com/ViewPublisher.php?view_id=17"

    # Prefer Granicus RSS for the full feed; fall back to the ViewPublisher
    # HTML archive when RSS yields no meetings.
    rss = _rss_url(calendar_url)
    meetings = _from_rss(rss)
    if meetings:
        return meetings

    return _from_html(calendar_url)


if __name__ == "__main__":
    meetings = scrape_calendar()
    for meeting in meetings[:3]:
        print(meeting)
    print(f"\nFound {len(meetings)} meetings.")

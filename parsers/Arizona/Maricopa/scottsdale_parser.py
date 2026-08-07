"""Scottsdale — Granicus ViewPublisher meeting parser."""
import re
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlparse

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _rss_url(calendar_url):
    if not calendar_url:
        return "https://scottsdale.granicus.com/ViewPublisherRSS.php?view_id=118"
    if "ViewPublisherRSS" in calendar_url:
        return calendar_url
    q = parse_qs(urlparse(calendar_url).query)
    ids = q.get("view_id") or q.get("View_ID")
    if ids:
        return f"https://scottsdale.granicus.com/ViewPublisherRSS.php?view_id={ids[0]}"
    m = re.search(r"view_id=(\d+)", calendar_url, re.I)
    if m:
        return f"https://scottsdale.granicus.com/ViewPublisherRSS.php?view_id={m.group(1)}"
    return "https://scottsdale.granicus.com/ViewPublisherRSS.php?view_id=118"


def _from_rss(rss_url):
    meetings = []
    if feedparser is None:
        return meetings
    try:
        feed = feedparser.parse(rss_url)
    except Exception as e:
        print(f"Scottsdale RSS error: {e}", file=sys.stderr)
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


def _from_html_archive(calendar_url):
    meetings = []
    try:
        response = requests.get(calendar_url, timeout=15, headers=HEADERS)
        response.raise_for_status()
    except (requests.exceptions.RequestException, requests.exceptions.HTTPError) as e:
        print(f"Scottsdale HTML error: {e}", file=sys.stderr)
        return meetings

    soup = BeautifulSoup(response.content, "html.parser")
    for row in soup.select("#archive tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        title = cells[0].text.strip()
        date = cells[1].text.strip()

        agenda_url = None
        agenda_link = cells[2].find("a")
        if agenda_link and agenda_link.get("href"):
            agenda_url = "https://scottsdale.granicus.com" + agenda_link["href"].replace("//", "/")

        video_url = None
        video_link = cells[4].find("a")
        if video_link and video_link.get("href"):
            video_url = video_link["href"]

        meetings.append(
            {
                "title": title,
                "date": date,
                "agenda_url": agenda_url,
                "video_url": video_url,
            }
        )

    return meetings


def scrape_calendar(calendar_url=None):
    if calendar_url is None:
        calendar_url = "https://scottsdale.granicus.com/ViewPublisher.php?view_id=118"

    # Prefer Granicus RSS for the full feed; fall back to the ViewPublisher
    # HTML archive when RSS yields no meetings.
    rss = _rss_url(calendar_url)
    meetings = _from_rss(rss)
    if meetings:
        return meetings

    return _from_html_archive(calendar_url)


if __name__ == "__main__":
    m = scrape_calendar()
    print(f"Found {len(m)} meetings")
    for meeting in m[:3]:
        print(f"Title: {meeting.get('Meeting Title/Name')}, Date: {meeting.get('Meeting Date')}")

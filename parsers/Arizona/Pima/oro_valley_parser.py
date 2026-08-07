#!/usr/bin/env python
# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup
import re

def scrape_calendar(calendar_url=None):
    if not calendar_url:
        calendar_url = "https://orovalleyaz.new.swagit.com/views/52"

    meetings = []
    try:
        response = requests.get(calendar_url, timeout=15)
        response.raise_for_status()
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"Error fetching calendar: {e}")
        return meetings

    soup = BeautifulSoup(response.content, "html.parser")
    base_url = "https://orovalleyaz.new.swagit.com"

    # Upcoming Events
    upcoming_events_table = soup.find("h3", string="Upcoming Events").find_next("table")
    if upcoming_events_table:
        for row in upcoming_events_table.find("tbody").find_all("tr"):
            cells = row.find_all("td")
            title = cells[0].text.strip()
            date_time_str = cells[1].text.strip()
            date_parts = date_time_str.split(' ')
            date = f"{date_parts[0]} {date_parts[1]} {date_parts[2]}"
            time = f"{date_parts[3]} {date_parts[4]}"

            links = cells[2].find_all("a")
            agenda_url = None
            video_url = None
            for link in links:
                if "Agenda" in link.text:
                    agenda_url = base_url + link["href"]
                if "Video" in link.text:
                    video_url = base_url + link["href"]

            meetings.append({
                "title": title,
                "date": date,
                "time": time,
                "agenda_url": agenda_url,
                "video_url": video_url,
            })

    return meetings

if __name__ == "__main__":
    meetings = scrape_calendar()
    print(f"Found {len(meetings)} meetings.")

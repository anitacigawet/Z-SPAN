"""Page — CivicClerk meeting parser."""
import json
import logging
import re
from datetime import datetime
from typing import Optional, Tuple

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

DEFAULT_PORTAL = "https://pageaz.portal.civicclerk.com/"


def _parse_event_datetime(event: dict) -> Tuple[Optional[str], Optional[str]]:
    raw = event.get("eventDate") or event.get("startDateTime")
    if not raw:
        return None, None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt_object = datetime.fromisoformat(s)
        return dt_object.strftime("%Y-%m-%d"), dt_object.strftime("%H:%M:%S")
    except ValueError:
        if "T" in str(raw):
            parts = str(raw).split("T", 1)
            return parts[0], (parts[1][:8] if len(parts[1]) >= 8 else None)
        return str(raw), None


def _event_title(event: dict) -> Optional[str]:
    return event.get("eventName") or event.get("title") or event.get("name")


def _event_location(event: dict) -> Optional[str]:
    loc = event.get("eventLocation") or event.get("location")
    if isinstance(loc, dict):
        return loc.get("address1") or loc.get("name")
    if isinstance(loc, str):
        return loc
    return None


def scrape_calendar(url=None):
    if not url:
        url = DEFAULT_PORTAL

    meetings = []
    try:
        # CivicClerk portal hosts map to the matching API subdomain's
        # /v1/Events endpoint.
        host = url.split("//", 1)[1].split("/")[0]
        subdomain = host.split(".")[0]
        api_base_url = f"https://{subdomain}.api.civicclerk.com/v1/Events"
        doc_base_url = f"https://{subdomain}.api.civicclerk.com/"
    except (IndexError, ValueError):
        logging.error("Could not parse portal URL: %s", url)
        return meetings

    params = {
        "$orderby": "eventDate desc",
        "$format": "json",
        "$top": 50,
        "$skip": 0,
    }

    while True:
        try:
            response = requests.get(api_base_url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logging.error("API request failed: %s", e)
            break
        except json.JSONDecodeError as e:
            logging.error("Invalid JSON: %s", e)
            break

        events = data.get("value", []) if isinstance(data, dict) else []
        if not events:
            break

        for event in events:
            title = _event_title(event)
            meeting_date, meeting_time = _parse_event_datetime(event)
            location = _event_location(event)
            event_id = event.get("id") or event.get("eventId")
            status = event.get("isPublished") or event.get("status")

            agenda_url = minutes_url = video_url = agenda_packet_url = None
            for doc in event.get("publishedFiles") or []:
                if not isinstance(doc, dict):
                    continue
                doc_type = doc.get("type")
                doc_url_path = doc.get("url")
                if not doc_url_path:
                    continue
                full = (
                    doc_url_path
                    if str(doc_url_path).startswith("http")
                    else doc_base_url + str(doc_url_path).lstrip("/")
                )
                if doc_type == "Agenda":
                    agenda_url = full
                elif doc_type == "Minutes":
                    minutes_url = full
                elif doc_type == "Video":
                    video_url = full
                elif doc_type == "Agenda Packet":
                    agenda_packet_url = full

            media_path = event.get("mediaSourcePath")
            if media_path and not video_url:
                video_url = (
                    media_path
                    if str(media_path).startswith("http")
                    else doc_base_url + str(media_path).lstrip("/")
                )

            portal_base = url.rstrip("/")
            if re.match(r"^https?://[^/]+\.portal\.civicclerk\.com/?$", portal_base):
                details_url = f"{portal_base}/event/{event_id}"
            else:
                details_url = f"https://{subdomain}.portal.civicclerk.com/event/{event_id}"

            if not agenda_url and event_id:
                agenda_url = details_url

            meetings.append(
                {
                    "Meeting Title/Name": title or "Meeting",
                    "Meeting Date": meeting_date or "",
                    "Meeting Time": meeting_time or "",
                    "Meeting Location": location or "",
                    "Agenda URL": agenda_url or "",
                    "Minutes URL": minutes_url or "",
                    "Video URL": video_url or "",
                    "Agenda Packet URL": agenda_packet_url or "",
                    "Meeting Status": status,
                    "eComment/Public Comment URL": "",
                    "Meeting ID": str(event_id) if event_id is not None else "",
                }
            )

        if len(events) < params["$top"]:
            break
        params["$skip"] += params["$top"]

    return meetings


if __name__ == "__main__":
    out = scrape_calendar(DEFAULT_PORTAL)
    print(f"Found {len(out)} meetings")

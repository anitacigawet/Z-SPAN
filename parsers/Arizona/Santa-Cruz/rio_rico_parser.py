import requests
from datetime import datetime
import json
import re

# The base URL for the CivicClerk portal
BASE_URL = "https://santacruzcoaz.portal.civicclerk.com"
# CivicClerk events endpoint used by the portal.
API_ENDPOINT = "https://santacruzcoaz.api.civicclerk.com/v1/Events"

def _get_file_url(event_id: int, file_type: str, file_id: int = None) -> str:
    """
    Constructs the URL for the meeting details page.
    """
    # The CivicClerk portal uses /event/{id} for the details page
    return f"{BASE_URL}/event/{event_id}"

def scrape_calendar(url: str) -> list:
    """
    Scrapes all available meetings from the CivicClerk portal using its internal OData API.

    :param url: The base URL of the CivicClerk portal (e.g., https://santacruzcoaz.portal.civicclerk.com)
    :return: A list of meeting dictionaries.
    """
    # Simple GET request to the /v1/Events endpoint.
    # This relies on the API defaulting to a wide date range and a large page size.
    
    headers = {
        "Accept": "application/json",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/"
    }

    print(f"Attempting to fetch data from: {API_ENDPOINT} with basic GET (no parameters)")
    
    try:
        response = requests.get(API_ENDPOINT, headers=headers, timeout=30)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        
        data = response.json()
        
        # OData responses often wrap the list in a 'value' key
        meetings_data = data.get("value", data) if isinstance(data, dict) else data
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data from CivicClerk API: {e}")
        return []
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON response: {e}")
        print(f"Response content: {response.text[:500]}...")
        return []

    if not isinstance(meetings_data, list):
        print(f"API returned non-list data structure: {type(meetings_data)}")
        return []

    print(f"Successfully retrieved {len(meetings_data)} raw meeting records.")
    if meetings_data:
        print("\n--- Raw Data Inspection (First Record) ---")
        print(json.dumps(meetings_data[0], indent=4))
        print("------------------------------------------\n")

    standardized_meetings = []
    for meeting in meetings_data:
        # Extract and format date and time
        meeting_dt_str = meeting.get("startDateTime")
        meeting_date = ""
        meeting_time = ""
        
        if meeting_dt_str:
            try:
                # Parse the ISO 8601 string
                if meeting_dt_str.endswith('Z'):
                    dt_object = datetime.fromisoformat(meeting_dt_str.replace("Z", "+00:00"))
                else:
                    dt_object = datetime.fromisoformat(meeting_dt_str)
                    
                meeting_date = dt_object.strftime("%Y-%m-%d")
                meeting_time = dt_object.strftime("%H:%M:%S")
            except ValueError:
                meeting_date = meeting_dt_str.split("T")[0] if "T" in meeting_dt_str else meeting_dt_str
                
        # Extract Event ID and Location
        # CivicClerk commonly exposes the event identifier as id or eventId.
        event_id = meeting.get("id") or meeting.get("eventId")
        location = meeting.get("location", "")
        
        # The links are all to the event details page, where the documents are embedded.
        details_url = _get_file_url(event_id, "details")
        
        # Meeting Status is often in the 'status' field (e.g., 'Final', 'Draft', 'Canceled')
        meeting_status = meeting.get("status", "")
        
        standardized_meeting = {
            "Meeting Title/Name": meeting.get("eventName", ""),
            "Meeting Date": meeting_date,
            "Meeting Time": meeting_time,
            "Meeting Location": location,
            "Agenda URL": details_url,
            "Minutes URL": details_url,
            "Video URL": details_url,
            "Agenda Packet URL": details_url,
            "Meeting Status": meeting_status,
            "eComment/Public Comment URL": details_url, # Use details page as proxy
            "Meeting ID": str(event_id) if event_id else "",
        }
        
        standardized_meetings.append(standardized_meeting)

    return standardized_meetings

if __name__ == '__main__':
    calendar_url = BASE_URL
    meetings = scrape_calendar(calendar_url)
    
    if meetings:
        print(f"\n--- Scrape Results Summary ---")
        print(f"Total meetings found: {len(meetings)}")
        print(f"First 5 meetings:")
        for i, m in enumerate(meetings[:5]):
            print(f"  {i+1}. {m['Meeting Date']} {m['Meeting Time']} - {m['Meeting Title/Name']}")
            print(f"     ID: {m['Meeting ID']}, Status: {m['Meeting Status']}")
            print(f"     Agenda URL: {m['Agenda URL']}")
        
        # Save the full list to a JSON file for inspection
        with open("rio_rico_meetings.json", "w") as f:
            json.dump(meetings, f, indent=4)
        print("\nFull results saved to rio_rico_meetings.json")
    else:
        print("\nNo meetings were scraped.")

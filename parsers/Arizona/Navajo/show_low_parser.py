import requests
from datetime import datetime
import json
import logging

# Set up basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def scrape_calendar(url):
    """
    Scrapes all available meetings from the CivicClerk portal by querying the
    underlying API, handling pagination.

    Args:
        url (str): The base URL of the CivicClerk portal (e.g., https://showlowaz.portal.civicclerk.com/)

    Returns:
        list: A list of dictionaries, each representing a meeting.
    """
    meetings = []
    
    # The correct CivicClerk API endpoint structure is typically:
    # https://[subdomain].api.civicclerk.com/v1/Events
    try:
        subdomain = url.split('//')[1].split('.')[0]
        api_base_url = f"https://{subdomain}.api.civicclerk.com/v1/Events"
        # Base URL for document links is the same as the API base, but without the /v1/Events
        doc_base_url = f"https://{subdomain}.api.civicclerk.com/"
    except IndexError:
        logging.error(f"Could not parse base URL: {url}")
        return meetings
    
    # Initial query parameters use pagination controls only.
    params = {
        '$orderby': 'eventDate desc', # Use eventDate as seen in the raw data
        '$format': 'json',
        '$top': 50, # Set a reasonable page size
        '$skip': 0  # Start at the beginning
    }
    
    # Loop to handle pagination
    while True:
        logging.info(f"Fetching page with $skip={params['$skip']}")
        
        try:
            response = requests.get(api_base_url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            events = data.get('value', [])
            
            # An empty event page ends pagination.
            if not events:
                break
                
            # Post-process the collected meetings
            for event in events:
                # Extract basic meeting details
                title = event.get('eventName')
                date_time_str = event.get('eventDate')
                location_obj = event.get('eventLocation', {})
                location = location_obj.get('address1') if location_obj else None
                status = event.get('isPublished')
                event_id = event.get('id')
                
                # Parse date and time
                meeting_date = None
                meeting_time = None
                if date_time_str:
                    try:
                        # CivicClerk dates are typically ISO 8601 format
                        dt_object = datetime.fromisoformat(date_time_str.replace('Z', '+00:00'))
                        meeting_date = dt_object.strftime('%Y-%m-%d')
                        meeting_time = dt_object.strftime('%H:%M:%S')
                    except ValueError:
                        logging.warning(f"Could not parse date/time: {date_time_str}")

                # Extract document URLs from publishedFiles
                agenda_url = None
                minutes_url = None
                video_url = None
                agenda_packet_url = None
                
                published_files = event.get('publishedFiles', [])
                for doc in published_files:
                    doc_type = doc.get('type')
                    doc_url_path = doc.get('url')
                    
                    if doc_url_path:
                        # Construct the full URL
                        full_doc_url = doc_base_url + doc_url_path
                        
                        if doc_type == 'Agenda':
                            agenda_url = full_doc_url
                        elif doc_type == 'Minutes':
                            minutes_url = full_doc_url
                        elif doc_type == 'Video':
                            # Video is often in a separate field, but sometimes here
                            video_url = full_doc_url
                        elif doc_type == 'Agenda Packet':
                            agenda_packet_url = full_doc_url
                
                # Also check for video in the dedicated media fields
                media_path = event.get('mediaSourcePath')
                if media_path and not video_url:
                    # The media path is relative to the base URL
                    video_url = doc_base_url + media_path
                
                # Construct the meeting dictionary
                meetings.append({
                    'Meeting Title/Name': title,
                    'Meeting Date': meeting_date,
                    'Meeting Time': meeting_time,
                    'Meeting Location': location,
                    'Agenda URL': agenda_url,
                    'Minutes URL': minutes_url,
                    'Video URL': video_url,
                    'Agenda Packet URL': agenda_packet_url,
                    'Meeting Status': status,
                    'eComment/Public Comment URL': None, # Not typically available in the main event API
                    'Meeting ID': event_id
                })
            
            logging.info(f"Fetched {len(events)} events on this page. Total so far: {len(meetings)}")
            
            # Stop after the final result page.
            if len(events) < params['$top']:
                break
            
            # Update $skip for the next iteration
            params['$skip'] += params['$top']
            
        except requests.exceptions.RequestException as e:
            logging.error(f"An error occurred during the API request: {e}")
            break
        except json.JSONDecodeError as e:
            logging.error(f"Failed to decode JSON response: {e}")
            break
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            break

    return meetings

if __name__ == '__main__':
    CITY_URL = "https://showlowaz.portal.civicclerk.com/"
    
    print(f"Starting scraper for {CITY_URL}...")
    all_meetings = scrape_calendar(CITY_URL)
    
    print(f"\n--- Scrape Results ---")
    print(f"Total meetings found: {len(all_meetings)}")
    
    sample_count = min(5, len(all_meetings))
    print(f"Displaying {sample_count} sample meetings:")
    for i, meeting in enumerate(all_meetings[:sample_count]):
        print(f"\nMeeting {i+1}:")
        for key, value in meeting.items():
            print(f"  {key}: {value}")

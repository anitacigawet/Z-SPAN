import requests
from bs4 import BeautifulSoup
from datetime import datetime
import re

# Base URL for the Fort Defiance Chapter Meeting Agenda and Minutes category
BASE_URL = "https://ftdefiance.navajochapters.org/blog/category/chapter-meeting-agenda-and-minutes/"

def standardize_date(date_str):
    """
    Converts a date string (e.g., 'Jun 22, 2021') into a standardized 'YYYY-MM-DD' format.
    """
    try:
        # The date format on the category page is 'Mon DD, YYYY'
        dt_obj = datetime.strptime(date_str, '%b %d, %Y')
        return dt_obj.strftime('%Y-%m-%d')
    except ValueError:
        return None

def scrape_post_details(post_url):
    """
    Visits an individual post page to extract download links for Agenda and Minutes.
    """
    details = {
        'agenda_url': None,
        'minutes_url': None,
        'video_url': None,
        'agenda_packet_url': None,
        'ecomment_url': None,
    }
    print(f"  -> Scraping details from: {post_url}")
    try:
        response = requests.get(post_url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"  -> Error fetching post details: {e}")
        return details # Return empty details on failure

    soup = BeautifulSoup(response.content, 'html.parser')
    
    # The content is typically within the 'entry-content' div
    content_div = soup.find('div', class_='entry-content')
    if not content_div:
        # Try 'entry-content-area' which is sometimes used
        content_div = soup.find('div', class_='entry-content-area')
        if not content_div:
            print("  -> Could not find content div.")
            return details

    # Find all links within the content
    links = content_div.find_all('a', href=True)
    
    for link in links:
        link_text = link.get_text(strip=True).lower()
        href = link['href']
        
        # Check for Agenda links
        if "agenda" in link_text and not details['agenda_url']:
            details['agenda_url'] = href
        
        # Check for Minutes links
        if "minutes" in link_text and not details['minutes_url']:
            details['minutes_url'] = href
            
    return details

def scrape_calendar(url=None):
    """
    Scrapes all available meetings from the Fort Defiance Chapter Meeting blog.
    """
    all_meetings = []
    current_page_url = BASE_URL
    page_num = 1

    while current_page_url:
        print(f"Scraping page {page_num}: {current_page_url}")
        try:
            response = requests.get(current_page_url, timeout=15)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching page {page_num}: {e}")
            break

        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Find all articles/posts on the current page
        posts = soup.find_all('article')
        
        if not posts:
            print("No more posts found on this page. Ending pagination.")
            break

        for post in posts:
            meeting = {
                'meeting_title': None,
                'meeting_date': None,
                'meeting_time': None,
                'meeting_location': None,
                'agenda_url': None,
                'minutes_url': None,
                'video_url': None,
                'agenda_packet_url': None,
                'meeting_status': None,
                'ecomment_url': None,
                'meeting_id': None,
            }
            
            # 1. Extract Title and Post URL
            title_tag = post.find('h2', class_='entry-title')
            if title_tag:
                link_tag = title_tag.find('a', href=True)
                if link_tag:
                    meeting['meeting_title'] = link_tag.get_text(strip=True)
                    post_url = link_tag['href']
                else:
                    continue
            else:
                continue

            # 2. Extract Date from the 'published' span
            date_span = post.find('span', class_='published')
            if date_span:
                date_str = date_span.get_text(strip=True)
                meeting['meeting_date'] = standardize_date(date_str)
            
            # 3. Scrape individual post details for download links
            # Every post in this category is meeting-related, so inspect each detail page.
            details = scrape_post_details(post_url)
            meeting['agenda_url'] = details['agenda_url']
            meeting['minutes_url'] = details['minutes_url']
            
            # 4. Add to list
            all_meetings.append(meeting)

        # 5. Find the next page URL (Older Entries)
        older_entries_link = soup.find('a', string='« Older Entries', href=True)
        if older_entries_link:
            current_page_url = older_entries_link['href']
            page_num += 1
        else:
            current_page_url = None # End loop

    return all_meetings

if __name__ == '__main__':
    # Example execution for testing
    meetings = scrape_calendar()
    print(f"\n--- Scraper Test Results ---")
    print(f"Total meetings found: {len(meetings)}")
    if meetings:
        print("\nFirst 7 meetings:")
        for m in meetings[:7]:
            print(f"  Title: {m['meeting_title']}")
            print(f"  Date: {m['meeting_date']}")
            print(f"  Agenda: {m['agenda_url']}")
            print(f"  Minutes: {m['minutes_url']}")
            print("-" * 20)

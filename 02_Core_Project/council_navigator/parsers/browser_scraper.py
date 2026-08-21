"""
Shared browser-based scraping utility using Playwright.
Used by parsers that need JavaScript rendering to access meeting data.
"""
import subprocess
import sys

def get_rendered_html(url, wait_selector=None, wait_time=5000, timeout=30000):
    """
    Fetch a URL using a headless browser and return the fully rendered HTML.
    
    Args:
        url: The URL to fetch
        wait_selector: Optional CSS selector to wait for before capturing HTML
        wait_time: Time in ms to wait after page load (default 5000ms)
        timeout: Navigation timeout in ms (default 30000ms)
    
    Returns:
        The fully rendered HTML string, or None on error
    """
    try:
        from playwright.sync_api import sync_playwright
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = context.new_page()
            
            try:
                page.goto(url, timeout=timeout, wait_until='networkidle')
            except Exception:
                try:
                    page.goto(url, timeout=timeout, wait_until='domcontentloaded')
                except Exception as e:
                    print(f"Navigation failed for {url}: {e}")
                    browser.close()
                    return None
            
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=10000)
                except Exception:
                    pass
            
            # Additional wait for dynamic content
            page.wait_for_timeout(wait_time)
            
            html = page.content()
            browser.close()
            return html
            
    except Exception as e:
        print(f"Browser scraping error for {url}: {e}")
        return None


def get_rendered_html_with_clicks(url, click_selectors=None, wait_time=3000, timeout=30000):
    """
    Fetch a URL, perform click actions (e.g., expanding sections), then return HTML.
    
    Args:
        url: The URL to fetch
        click_selectors: List of CSS selectors to click in order
        wait_time: Time in ms to wait after each action
        timeout: Navigation timeout in ms
    
    Returns:
        The fully rendered HTML string, or None on error
    """
    try:
        from playwright.sync_api import sync_playwright
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = context.new_page()
            
            try:
                page.goto(url, timeout=timeout, wait_until='networkidle')
            except Exception:
                try:
                    page.goto(url, timeout=timeout, wait_until='domcontentloaded')
                except Exception as e:
                    print(f"Navigation failed for {url}: {e}")
                    browser.close()
                    return None
            
            page.wait_for_timeout(wait_time)
            
            if click_selectors:
                for selector in click_selectors:
                    try:
                        page.click(selector, timeout=5000)
                        page.wait_for_timeout(wait_time)
                    except Exception:
                        pass
            
            html = page.content()
            browser.close()
            return html
            
    except Exception as e:
        print(f"Browser scraping error for {url}: {e}")
        return None


def get_page_text(url, wait_time=5000, timeout=30000):
    """
    Fetch a URL and return just the visible text content.
    Useful for simple pages where HTML parsing isn't needed.
    """
    try:
        from playwright.sync_api import sync_playwright
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
            page = browser.new_page()
            
            try:
                page.goto(url, timeout=timeout, wait_until='networkidle')
            except Exception:
                page.goto(url, timeout=timeout, wait_until='domcontentloaded')
            
            page.wait_for_timeout(wait_time)
            text = page.inner_text('body')
            browser.close()
            return text
            
    except Exception as e:
        print(f"Browser text extraction error for {url}: {e}")
        return None

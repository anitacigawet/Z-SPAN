import requests
from bs4 import BeautifulSoup

AGENDAS_PAGE = "https://www.eagaraz.gov/o/tofe/page/agendas-minutes"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _fallback():
    # The Eagar site is protected by a JavaScript client challenge. When plain
    # HTTP cannot reach the listing, return no rows rather than inventing a
    # placeholder meeting; the normal scrape still wins when the site responds.
    return []


def scrape_calendar(url=None):
    target = url or AGENDAS_PAGE
    try:
        response = requests.get(target, timeout=20, headers=HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        return _fallback()

    text = response.text
    if len(text) < 6000 or "Client Challenge" in text or "challenge" in text.lower():
        return _fallback()

    soup = BeautifulSoup(response.content, "html.parser")
    meetings = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr")[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            meetings.append(
                {
                    "Meeting Title/Name": cells[1] if len(cells) > 1 else cells[0],
                    "Meeting Date": cells[0],
                    "Meeting Time": "",
                    "Agenda URL": AGENDAS_PAGE,
                    "Minutes URL": "",
                    "Video URL": "",
                    "Meeting Location": "Eagar, AZ",
                    "Meeting Status": "",
                }
            )

    return meetings if meetings else _fallback()

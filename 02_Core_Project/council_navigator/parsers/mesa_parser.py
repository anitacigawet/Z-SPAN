"""Mesa City Council meetings from the official Legistar calendar."""

from __future__ import annotations

from legistar_current_adapter import scrape_legistar_current


DEFAULT_URL = "https://mesa.legistar.com/Calendar.aspx"
ALLOWED_TITLES = frozenset({
    "City Council",
    "City Council Study Session",
})
ALLOWED_MEDIA_HOSTS = frozenset({"youtu.be", "youtube.com", "www.youtube.com"})


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return Mesa City Council meetings from this calendar month forward."""
    return scrape_legistar_current(
        url,
        city_label="Mesa",
        allowed_titles=ALLOWED_TITLES,
        allowed_media_hosts=ALLOWED_MEDIA_HOSTS,
    )

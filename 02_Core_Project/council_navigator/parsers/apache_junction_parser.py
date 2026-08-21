"""Apache Junction City Council meetings from the official Legistar calendar."""

from __future__ import annotations

from legistar_current_adapter import scrape_legistar_current


DEFAULT_URL = "https://apachejunction.legistar.com/Calendar.aspx"
COUNCIL_TITLES = frozenset(
    {
        "City Council Executive Session",
        "City Council Meeting",
        "City Council Strategic Planning Retreat",
        "City Council Work Session",
    }
)
COUNCIL_PREFIXES = (
    "Special Joint Meeting of the Apache Junction City Council",
    "Special Meeting of the Apache Junction City Council",
    "Special Work Session of the Apache Junction City Council",
)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return Apache Junction council rows from this calendar month forward."""
    return scrape_legistar_current(
        url,
        city_label="Apache Junction",
        allowed_titles=COUNCIL_TITLES,
        allowed_title_prefixes=COUNCIL_PREFIXES,
    )


__all__ = ["scrape_calendar"]

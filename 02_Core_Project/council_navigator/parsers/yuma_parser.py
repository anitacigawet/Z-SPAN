"""Yuma City Council meetings from the official Legistar calendar."""

from __future__ import annotations

from legistar_current_adapter import scrape_legistar_current


DEFAULT_URL = "https://yuma-az.legistar.com/Calendar.aspx"
COUNCIL_TITLES = frozenset(
    {
        "City Council Citizen's Forum",
        "City Council Meeting",
        "City Council Worksession",
    }
)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return Yuma council rows from this calendar month forward."""
    return scrape_legistar_current(
        url,
        city_label="Yuma",
        allowed_titles=COUNCIL_TITLES,
    )


__all__ = ["scrape_calendar"]

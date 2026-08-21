"""Eloy City Council meetings from the official Granicus publisher."""

from __future__ import annotations

from granicus_current_adapter import scrape_granicus_current


DEFAULT_URL = "https://eloyaz.granicus.com/ViewPublisher.php?view_id=1"


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return Eloy City Council rows from this calendar month forward."""
    return scrape_granicus_current(
        url,
        city_label="Eloy",
        allowed_title_prefixes=("City Council",),
        excluded_titles=frozenset({"System Test", "New Event"}),
    )


__all__ = ["scrape_calendar"]

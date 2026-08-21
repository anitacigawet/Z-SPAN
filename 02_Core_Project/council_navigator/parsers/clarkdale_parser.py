"""Clarkdale Town Council meetings from the official CivicClerk portal."""

from __future__ import annotations

from datetime import date

from civicclerk_current_adapter import scrape_civicclerk_current


DEFAULT_URL = "https://clarkdaleaz.portal.civicclerk.com/"


def scrape_calendar(url: str = DEFAULT_URL, *, today: date | None = None) -> list[dict[str, str]]:
    """Return Clarkdale Town Council meetings from this calendar month forward."""
    return scrape_civicclerk_current(
        url,
        city_label="Clarkdale",
        governing_body_phrase="town council",
        exact_allowed_titles=frozenset({"Town Council"}),
        today=today,
    )

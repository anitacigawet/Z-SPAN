"""Prescott City Council meetings from the official CivicClerk portal."""

from __future__ import annotations

from datetime import date

from civicclerk_current_adapter import scrape_civicclerk_current


DEFAULT_URL = "https://prescottaz.portal.civicclerk.com/"


def scrape_calendar(url: str = DEFAULT_URL, *, today: date | None = None) -> list[dict[str, str]]:
    """Return Prescott City Council meetings from this calendar month forward."""
    return scrape_civicclerk_current(
        url,
        city_label="Prescott",
        governing_body_phrase="city council",
        exact_allowed_titles=frozenset({
            "City Council Study Session",
            "City Council Voting Meeting",
            "Special City Council Meeting",
        }),
        today=today,
    )

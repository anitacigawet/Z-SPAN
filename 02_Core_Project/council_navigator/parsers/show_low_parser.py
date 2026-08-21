"""Show Low City Council meetings from the official CivicClerk portal."""

from __future__ import annotations

from datetime import date
import logging
import re

from civicclerk_current_adapter import scrape_civicclerk_current


DEFAULT_URL = "https://showlowaz.portal.civicclerk.com/"
RESCHEDULED_TO_DATE_RE = re.compile(
    r"\brescheduled\s+to\s+(?:[a-z]+\s+\d{1,2}(?:,\s*\d{4})?|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


def scrape_calendar(url: str = DEFAULT_URL, *, today: date | None = None) -> list[dict[str, str]]:
    """Return Show Low City Council meetings from this calendar month forward."""
    meetings = scrape_civicclerk_current(
        url,
        city_label="Show Low",
        governing_body_phrase="city council",
        exact_allowed_titles=frozenset({"City Council"}),
        today=today,
    )
    retained: list[dict[str, str]] = []
    for meeting in meetings:
        title = meeting["meeting_title"]
        if RESCHEDULED_TO_DATE_RE.search(title):
            logger.warning(
                "Show Low CivicClerk row dropped: reason=explicit_rescheduled_to_another_date "
                "id=%s source_date=%s title=%r",
                meeting["meeting_id"],
                meeting["meeting_date"],
                title,
            )
            continue
        retained.append(meeting)
    return retained

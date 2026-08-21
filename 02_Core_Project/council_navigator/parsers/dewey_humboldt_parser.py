"""Current-month-forward Dewey-Humboldt Town Council parser."""

from __future__ import annotations

import logging

from granicus_current_adapter import scrape_granicus_current
from requests.exceptions import RequestException


DEFAULT_CALENDAR_URL = "https://dhaz.granicus.com/ViewPublisher.php?view_id=2"
logger = logging.getLogger(__name__)


def scrape_calendar(url: str | None = None) -> list[dict]:
    try:
        return scrape_granicus_current(
            url or DEFAULT_CALENDAR_URL,
            city_label="Dewey-Humboldt",
            allowed_title_prefixes=(
                "Town Council Regular Meeting",
                "Town Council Special Meeting",
                "Town Council Study Session Meeting",
            ),
            excluded_titles=frozenset({"Planning and Zoning Meeting"}),
            excluded_title_terms=("board", "commission", "committee"),
        )
    except RequestException as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Dewey-Humboldt official Granicus source could not be reached: "
            "failure_shape=honest-empty "
            "missing_scope=current_month_forward_town_council error=%s",
            exc,
        )
        return []


__all__ = ["scrape_calendar"]

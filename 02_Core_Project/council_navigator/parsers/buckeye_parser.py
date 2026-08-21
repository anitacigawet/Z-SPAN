"""Current-month-forward Buckeye City Council parser."""

from __future__ import annotations

import logging

from granicus_current_adapter import scrape_granicus_current
from requests.exceptions import RequestException


DEFAULT_CALENDAR_URL = "https://buckeyeaz.granicus.com/ViewPublisher.php?view_id=1"
logger = logging.getLogger(__name__)


def scrape_calendar(url: str | None = None) -> list[dict]:
    try:
        return scrape_granicus_current(
            url or DEFAULT_CALENDAR_URL,
            city_label="Buckeye",
            allowed_title_prefixes=(
                "Regular Council Meeting",
                "Special Council Meeting",
                "Regular and Special Council Meeting",
                "Council Workshop",
                "Council Executive Session",
                "Council Retreat",
            ),
            excluded_title_terms=(
                "board",
                "commission",
                "committee",
                "district",
                "corporation",
                "authority",
                "subcommittee",
                "youth council",
            ),
        )
    except RequestException as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Buckeye official Granicus source could not be reached: "
            "failure_shape=honest-empty "
            "missing_scope=current_month_forward_city_council error=%s",
            exc,
        )
        return []


__all__ = ["scrape_calendar"]

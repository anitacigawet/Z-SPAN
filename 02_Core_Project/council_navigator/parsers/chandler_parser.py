"""Current-window Chandler City Council AgendaQuick parser."""

from __future__ import annotations

import logging
import re

from requests.exceptions import HTTPError, RequestException

from destiny_current_adapter import scrape_destiny_current


DEFAULT_CALENDAR_URL = "https://public.destinyhosted.com/agenda_publish.cfm?id=24263"
CHANDLER_MEDIA_HOSTS = {"chandleraz.new.swagit.com"}
CITY_COUNCIL_RE = re.compile(r"^City Council\b", re.IGNORECASE)
NON_MEETING_RE = re.compile(r"\b(?:notice|quorum)\b", re.IGNORECASE)
BLOCK_STATUSES = {401, 403, 429}

logger = logging.getLogger(__name__)


def _title_allowed(title: str) -> bool:
    return bool(CITY_COUNCIL_RE.search(title)) and not bool(NON_MEETING_RE.search(title))


def scrape_calendar(calendar_url: str | None = None) -> list[dict]:
    """Return Chandler City Council rows for this month plus six ahead."""
    try:
        meetings = scrape_destiny_current(
            calendar_url or DEFAULT_CALENDAR_URL,
            media_hosts=CHANDLER_MEDIA_HOSTS,
            title_allow_predicate=_title_allowed,
        )
    except HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status not in BLOCK_STATUSES:
            raise
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Chandler AgendaQuick source blocked the bounded polite request: "
            "status=%s failure_shape=honest-empty missing_scope="
            "current_month_plus_six_city_council",
            status,
        )
        return []
    except RequestException as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Chandler AgendaQuick source could not be reached: "
            "failure_shape=honest-empty missing_scope="
            "current_month_plus_six_city_council error=%s",
            exc,
        )
        return []

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Chandler selected-month AgendaQuick pages explicitly contain no "
            "City Council rows in the bounded current-month-forward window"
        )
    return meetings


__all__ = ["scrape_calendar"]

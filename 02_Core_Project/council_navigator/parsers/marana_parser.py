"""Marana AgendaQuick calendar wrapper."""

from __future__ import annotations

import logging

from requests.exceptions import HTTPError, SSLError

from destiny_current_adapter import scrape_destiny_current

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://destinyhosted.com/agenda_publish.cfm?id=62726"
_BLOCK_STATUSES = {401, 403, 429}


def scrape_calendar(url: str | None = None) -> list[dict]:
    """Return this month plus six future months of official Marana meetings."""
    try:
        meetings = scrape_destiny_current(
            url or DEFAULT_URL,
            media_hosts={"maranaaz.new.swagit.com"},
        )
    except SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Marana AgendaQuick source failed verified TLS")
        return []
    except HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status not in _BLOCK_STATUSES:
            raise
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Marana AgendaQuick source blocked the polite request: status=%s", status)
        return []
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning("Marana AgendaQuick selected-month pages witnessed zero council rows")
    return meetings


if __name__ == "__main__":
    print(f"Found {len(scrape_calendar())} current/future Marana meetings.")

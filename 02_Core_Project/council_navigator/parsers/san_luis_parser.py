"""San Luis AgendaQuick calendar wrapper."""

import logging

from requests.exceptions import HTTPError, SSLError

from destiny_current_adapter import scrape_destiny_current


DEFAULT_URL = "https://public.destinyhosted.com/agenda_publish.cfm?id=72658"
_BLOCK_STATUSES = {401, 403, 429}

logger = logging.getLogger(__name__)


def scrape_calendar(url: str | None = None) -> list[dict]:
    """Return this month plus six future months of official San Luis meetings."""
    try:
        meetings = scrape_destiny_current(
            url or DEFAULT_URL,
            media_hosts={"youtube.com", "www.youtube.com", "youtu.be"},
        )
    except SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("San Luis AgendaQuick source failed verified TLS")
        return []
    except HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status not in _BLOCK_STATUSES:
            raise
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("San Luis AgendaQuick source blocked the polite request: status=%s", status)
        return []
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning("San Luis AgendaQuick selected-month pages witnessed zero council rows")
    return meetings


if __name__ == "__main__":
    print(f"Found {len(scrape_calendar())} current/future San Luis meetings.")

"""Current-window Queen Creek Town Council Granicus RSS parser."""

from __future__ import annotations

import logging
import re

from current_rss_adapter import SourceBlocked, scrape_granicus_rss


DEFAULT_CALENDAR_URL = (
    "https://queencreekaz.granicus.com/ViewPublisherRSS.php?view_id=3"
)
EXPECTED_HOST = "queencreekaz.granicus.com"
TOWN_COUNCIL_RE = re.compile(
    r"^(?:Town Council\b|Mid-Year Council Strategic Planning Session\b)",
    re.IGNORECASE,
)
NOTICE_RE = re.compile(r"\b(?:notice|quorum)\b", re.IGNORECASE)

logger = logging.getLogger(__name__)


def _title_allowed(title: str) -> bool:
    return bool(TOWN_COUNCIL_RE.search(title)) and not bool(NOTICE_RE.search(title))


def scrape_calendar(calendar_url: str | None = None) -> list[dict]:
    try:
        return scrape_granicus_rss(
            calendar_url or DEFAULT_CALENDAR_URL,
            expected_host=EXPECTED_HOST,
            title_allowed=_title_allowed,
        )
    except SourceBlocked as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Queen Creek official Granicus RSS is blocked: "
            "failure_shape=honest-empty "
            "missing_scope=current_month_forward_town_council error=%s",
            exc,
        )
        return []

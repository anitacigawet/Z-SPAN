"""Current-window Pinetop-Lakeside Town Council CivicPlus RSS parser."""

from __future__ import annotations

import logging
import re

from current_rss_adapter import SourceBlocked, scrape_civicplus_rss


RSS_FEED_URL = (
    "https://www.pinetoplakesideaz.gov/"
    "RSSFeed.aspx?ModID=65&CID=Town-Council-2"
)
EXPECTED_HOST = "www.pinetoplakesideaz.gov"
TOWN_COUNCIL_RE = re.compile(r"^Town Council\b", re.IGNORECASE)
NOTICE_RE = re.compile(r"\b(?:notice|quorum)\b", re.IGNORECASE)

logger = logging.getLogger(__name__)


def _title_allowed(title: str) -> bool:
    return bool(TOWN_COUNCIL_RE.search(title)) and not bool(NOTICE_RE.search(title))


def scrape_calendar(_calendar_url: str | None = None) -> list[dict]:
    try:
        return scrape_civicplus_rss(
            RSS_FEED_URL,
            expected_host=EXPECTED_HOST,
            title_allowed=_title_allowed,
        )
    except SourceBlocked as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Pinetop-Lakeside official Town Council RSS is blocked: "
            "failure_shape=honest-empty "
            "missing_scope=current_month_forward_town_council error=%s",
            exc,
        )
        return []

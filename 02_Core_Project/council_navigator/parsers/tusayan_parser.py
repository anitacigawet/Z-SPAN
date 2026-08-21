"""Current-window Tusayan Town Council AgendaQuick parser."""

from __future__ import annotations

import logging
import re

from requests.exceptions import HTTPError

from destiny_current_adapter import scrape_destiny_current


_NO_COUNCIL_ERROR = (
    "AgendaQuick mt=ALL exposed rows but none carried a trustworthy "
    "council/governing-body title signal"
)
_BLOCK_STATUSES = {401, 403, 429}
_COUNCIL_BODY_RE = re.compile(r"\btown council\b", re.IGNORECASE)
_NON_MEETING_RE = re.compile(
    r"\b(?:notice|quorum|commission|committee|authority|board)\b",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


class _TusayanCouncilScope:
    """Track whether rejected mt=ALL labels are safely identifiable."""

    def __init__(self) -> None:
        self.recognized_other_bodies: list[str] = []
        self.ambiguous_titles: list[str] = []

    def __call__(self, title: str) -> bool:
        if _COUNCIL_BODY_RE.search(title) and not _NON_MEETING_RE.search(title):
            return True
        if _NON_MEETING_RE.search(title):
            self.recognized_other_bodies.append(title)
        else:
            self.ambiguous_titles.append(title)
        return False


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Return Tusayan Town Council rows from this month plus six ahead."""
    scope = _TusayanCouncilScope()
    try:
        meetings = scrape_destiny_current(
            calendar_url,
            media_hosts={"youtube.com", "www.youtube.com", "youtu.be"},
            title_allow_predicate=scope,
        )
    except HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status not in _BLOCK_STATUSES:
            raise
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Tusayan AgendaQuick source blocked the bounded polite request: "
            "status=%s failure_shape=honest-empty missing_scope="
            "current_month_plus_six_future_months",
            status,
        )
        return []
    except ValueError as exc:
        if (
            str(exc) != _NO_COUNCIL_ERROR
            or scope.ambiguous_titles
            or not scope.recognized_other_bodies
        ):
            raise
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Tusayan selected-month AgendaQuick pages exposed only explicit "
            "non-council bodies or quorum notices in the bounded window: "
            "titles=%r",
            sorted(set(scope.recognized_other_bodies)),
        )
        return []

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Tusayan selected-month AgendaQuick pages explicitly contain no "
            "Town Council rows in the current-month-forward bounded window"
        )
    return meetings

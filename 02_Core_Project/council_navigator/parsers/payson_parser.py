"""Current-window Payson Town Council Granicus RSS parser."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from requests import RequestException

from current_rss_adapter import SourceBlocked, scrape_granicus_rss
from polite_http import make_session


DEFAULT_CALENDAR_URL = "https://payson.granicus.com/ViewPublisher.php?view_id=17"
EXPECTED_HOST = "payson.granicus.com"
EXPECTED_ATTACHMENT_HOST = "granicus_production_attachments.s3.amazonaws.com"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
TOWN_COUNCIL_RE = re.compile(r"^Payson Town Council\b", re.IGNORECASE)
NOTICE_RE = re.compile(r"\b(?:notice|quorum)\b", re.IGNORECASE)

logger = logging.getLogger(__name__)

# The official publisher currently carries two archive entries for the same
# 2026-08-04 special meeting.  Both publisher rows expose the same date and no
# time, and both AgendaViewer links redirect to the exact same official PDF
# attachment.  Keep the first/newer RSS entry and suppress only the known
# duplicate ID; do not collapse arbitrary same-day special meetings.
KNOWN_DUPLICATE_ARCHIVE = {
    "dropped_id": "2869",
    "kept_id": "2870",
    "meeting_date": "2026-08-04",
    "meeting_title": "Payson Town Council - Special",
}


def _title_allowed(title: str) -> bool:
    return bool(TOWN_COUNCIL_RE.search(title)) and not bool(NOTICE_RE.search(title))


def _agenda_viewer_url(row: dict, expected_id: str) -> str:
    source_url = row["agenda_url"] or row["video_url"]
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)
    view_id = (query.get("view_id") or [""])[0]
    clip_id = (query.get("clip_id") or [""])[0]
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != EXPECTED_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not view_id.isdigit()
        or clip_id != expected_id
    ):
        raise ValueError(
            "Payson known duplicate row lacks a trustworthy official clip URL: "
            f"meeting_id={expected_id} source_url={source_url!r}"
        )
    return urlunparse(
        parsed._replace(
            path="/AgendaViewer.php",
            query=urlencode({"view_id": view_id, "clip_id": expected_id}),
            fragment="",
        )
    )


def _canonical_attachment_url(location: str, agenda_url: str, meeting_id: str) -> str:
    absolute = urljoin(agenda_url, location.strip())
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host != EXPECTED_ATTACHMENT_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not parsed.path.startswith("/payson/")
        or not parsed.path.lower().endswith(".pdf")
    ):
        raise ValueError(
            "Payson known duplicate agenda redirect is not an allowlisted "
            "official attachment: "
            f"meeting_id={meeting_id} location={location!r}"
        )
    return urlunparse(
        parsed._replace(scheme="https", netloc=host, fragment="")
    )


def _verify_duplicate_attachment(dropped: dict, kept: dict) -> str:
    evidence: list[tuple[str, str]] = []
    with make_session() as session:
        for row in (dropped, kept):
            meeting_id = row["meeting_id"]
            agenda_url = _agenda_viewer_url(row, meeting_id)
            try:
                response_context = session.get(
                    agenda_url,
                    timeout=(10, 30),
                    stream=True,
                    allow_redirects=False,
                )
            except RequestException as exc:
                raise ValueError(
                    "Payson known duplicate agenda proof request failed: "
                    f"meeting_id={meeting_id} agenda_url={agenda_url} error={exc}"
                ) from exc
            with response_context as response:
                response_host = (urlparse(response.url).hostname or "").lower()
                if response_host != EXPECTED_HOST:
                    raise ValueError(
                        "Payson known duplicate agenda proof response host changed: "
                        f"meeting_id={meeting_id} host={response_host!r}"
                    )
                if response.status_code not in REDIRECT_STATUSES:
                    raise ValueError(
                        "Payson known duplicate agenda proof returned an unexpected "
                        "status: "
                        f"meeting_id={meeting_id} status={response.status_code}"
                    )
                location = response.headers.get("Location", "").strip()
                if not location:
                    raise ValueError(
                        "Payson known duplicate agenda proof omitted Location: "
                        f"meeting_id={meeting_id} status={response.status_code}"
                    )
                attachment_url = _canonical_attachment_url(
                    location, agenda_url, meeting_id
                )
                evidence.append((meeting_id, attachment_url))

    dropped_attachment = evidence[0][1]
    kept_attachment = evidence[1][1]
    if dropped_attachment != kept_attachment:
        raise ValueError(
            "Payson known duplicate agenda attachments differ: "
            f"dropped_id={evidence[0][0]} attachment={dropped_attachment!r} "
            f"kept_id={evidence[1][0]} attachment={kept_attachment!r}"
        )
    logger.info(
        "Payson known duplicate agenda attachment verified: "
        "dropped_id=%s kept_id=%s attachment=%s",
        evidence[0][0],
        evidence[1][0],
        dropped_attachment,
    )
    return dropped_attachment


def _drop_known_duplicate_archive(rows: list[dict]) -> list[dict]:
    by_id = {row["meeting_id"]: row for row in rows}
    dropped_id = KNOWN_DUPLICATE_ARCHIVE["dropped_id"]
    kept_id = KNOWN_DUPLICATE_ARCHIVE["kept_id"]
    dropped = by_id.get(dropped_id)
    kept = by_id.get(kept_id)
    if dropped is None:
        return rows
    if kept is None:
        logger.warning(
            "Payson known duplicate archive survivor is absent; preserving "
            "the remaining official row unchanged: present_id=%s absent_id=%s",
            dropped_id,
            kept_id,
        )
        return rows

    expected = (
        KNOWN_DUPLICATE_ARCHIVE["meeting_date"],
        KNOWN_DUPLICATE_ARCHIVE["meeting_title"],
        "",
    )
    dropped_key = (
        dropped["meeting_date"],
        dropped["meeting_title"],
        dropped["meeting_time"],
    )
    kept_key = (
        kept["meeting_date"],
        kept["meeting_title"],
        kept["meeting_time"],
    )
    if dropped_key != expected or kept_key != expected:
        raise ValueError(
            "Payson known duplicate archive evidence changed: "
            f"dropped_id={dropped_id} key={dropped_key!r} "
            f"kept_id={kept_id} key={kept_key!r}"
        )

    attachment_url = _verify_duplicate_attachment(dropped, kept)

    logger.warning(
        "Payson known duplicate Granicus archive row dropped: "
        "dropped_id=%s kept_id=%s date=%s title=%r "
        "attachment=%s evidence=same_official_publisher_date_and_agenda_attachment",
        dropped_id,
        kept_id,
        expected[0],
        expected[1],
        attachment_url,
    )
    return [row for row in rows if row["meeting_id"] != dropped_id]


def scrape_calendar(calendar_url: str | None = None) -> list[dict]:
    try:
        return _drop_known_duplicate_archive(
            scrape_granicus_rss(
                calendar_url or DEFAULT_CALENDAR_URL,
                expected_host=EXPECTED_HOST,
                title_allowed=_title_allowed,
            )
        )
    except SourceBlocked as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Payson official Granicus RSS is blocked: failure_shape=honest-empty "
            "missing_scope=current_month_forward_town_council error=%s",
            exc,
        )
        return []

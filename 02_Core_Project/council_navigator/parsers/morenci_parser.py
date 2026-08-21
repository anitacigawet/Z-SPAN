from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://greenlee.az.gov/public-notices/"
ALLOWED_HOSTS = {"greenlee.az.gov", "www.greenlee.az.gov"}
MAX_RESPONSE_BYTES = 2_000_000
BROWSER_CHALLENGE_TITLES = {"just a moment...", "one moment, please..."}
BROWSER_CHALLENGE_MARKERS = (
    "please wait while your request is being verified",
    "performing security verification",
)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Confirm that the registered source is county-only, not a Morenci council.

    The public manifest retains the legacy Morenci route label while binding it
    to the catalog's Greenlee County place and public-notices endpoint. County
    boards are not evidence of a Morenci municipal governing-body meeting, so an
    accessible county-only page is a witnessed honest empty for that route.
    """
    target = _validated_source_url(url or DEFAULT_URL)
    with make_session() as session:
        html = _fetch_text_bounded(session, target)
    soup = BeautifulSoup(html, "html.parser")
    title = _clean_text(soup.title)
    page_text = _clean_text(soup)[:20_000]
    if _is_browser_challenge(title, page_text):
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Morenci official source returned a browser-verification interstitial: "
            "title=%r missing_data_scope=county_source_fingerprint_and_all_current_meetings",
            title,
        )
        return []
    if "Greenlee County" not in title or "Public Notices" not in title:
        logger.warning(
            "vendor_fingerprint_failed expected=Greenlee_County_Public_Notices title=%r",
            title,
        )
        raise RuntimeError("Morenci registered source fingerprint drifted")
    if "Morenci Town Council" in page_text or "Morenci City Council" in page_text:
        raise RuntimeError("Morenci municipal governing-body evidence appeared; parser requires recon")
    logger.info(
        "vendor_fingerprint witness=Greenlee_County_Public_Notices county_source_not_morenci_municipal_body"
    )
    logger.warning(
        "no_flagship_governing_body reason=registered_source_is_county_public_notices "
        "excluded_bodies=board_of_supervisors,planning_and_zoning,county_committees"
    )
    logger.warning("health_empty_kind=confirmed_empty")
    return []


def _validated_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in ALLOWED_HOSTS:
        raise ValueError("Morenci registered source must use HTTPS on the Greenlee County host")
    return url


def _fetch_text_bounded(session: requests.Session, url: str) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        if _host(response.url) not in ALLOWED_HOSTS:
            raise ValueError(f"Morenci redirect reached disallowed host: {_host(response.url)}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Morenci response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _is_browser_challenge(title: str, page_text: str) -> bool:
    normalized_title = title.casefold()
    normalized_text = page_text.casefold()
    return normalized_title in BROWSER_CHALLENGE_TITLES and any(
        marker in normalized_text for marker in BROWSER_CHALLENGE_MARKERS
    )


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_URL), indent=2))

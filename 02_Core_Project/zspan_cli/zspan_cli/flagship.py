"""The flagship HTTP client — the CLI's only conversation with Z-SPAN.

Two endpoints:

  GET /v1/catalog/jurisdictions          — the availability list `pick` renders
  GET /api/cities/<name>/meetings?year=  — the catalog `pull` mirrors locally

Meeting records flow from the flagship to the CLI. Completed transcripts,
final outputs, and audit metadata return through the authenticated private
intake; provider keys and raw media do not. The User-Agent is an honest
`zspan-cli/<version>`: the neutral-UA rule exists to protect CITY sites,
while the flagship is our own server, where self-identification helps
its anti-bulk telemetry.

Failure semantics are F8-honest: network trouble and server trouble
raise FlagshipError with a plain sentence; "the flagship answered with
an empty list" is NOT an error and flows back to the caller, which is
responsible for saying so honestly (succeeded-empty ≠ failed-silent).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from zspan_cli import __version__

_TIMEOUT_SECONDS = 30
_UA = f"zspan-cli/{__version__}"
_MAX_CATALOG_PAGES = 100


class FlagshipError(Exception):
    """The flagship could not be reached or answered unusably."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _get(
    base_url: str,
    path: str,
    params: Optional[Dict[str, str]] = None,
    *,
    allow_404: bool = False,
    bearer: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    url = base_url.rstrip("/") + path
    headers = {"User-Agent": _UA}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        resp = requests.get(
            url,
            params=params or {},
            headers=headers,
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as e:
        raise FlagshipError(
            f"could not reach the Z-SPAN endpoint server at {base_url} "
            f"({type(e).__name__}). Check your connection, or your "
            f"flagship_url in ~/.zspan/config.json."
        ) from e
    if allow_404 and resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise FlagshipError(
            f"the endpoint server answered HTTP {resp.status_code} for {path}.",
            status=resp.status_code,
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise FlagshipError(
            f"the endpoint server's answer for {path} was not JSON.",
            status=resp.status_code,
        ) from e
    if not isinstance(data, dict):
        raise FlagshipError(
            f"unexpected response shape for {path}.", status=resp.status_code
        )
    return data


def _post(
    base_url: str,
    path: str,
    payload: Dict[str, Any],
    *,
    bearer: Optional[str] = None,
) -> Dict[str, Any]:
    """POST JSON to the flagship without ever reflecting credentials."""
    url = base_url.rstrip("/") + path
    headers = {"User-Agent": _UA}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        resp = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as e:
        raise FlagshipError(
            f"could not reach the Z-SPAN endpoint server at {base_url} "
            f"({type(e).__name__}). Check your connection, or your "
            f"flagship_url in ~/.zspan/config.json."
        ) from e
    if resp.status_code != 200:
        raise FlagshipError(
            f"the endpoint server answered HTTP {resp.status_code} for {path}.",
            status=resp.status_code,
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise FlagshipError(
            f"the endpoint server's answer for {path} was not JSON.",
            status=resp.status_code,
        ) from e
    if not isinstance(data, dict):
        raise FlagshipError(
            f"unexpected response shape for {path}.", status=resp.status_code
        )
    return data


def fetch_coverage(base_url: str) -> List[Dict[str, Any]]:
    """Flatten the public jurisdiction tree for ``zspan pick``.

    ``/api/coverage`` was the pre-catalog route and is no longer part of the
    public edge contract. The v1 jurisdiction endpoint is the durable facts
    boundary shared by ``home`` and ``pick``; deriving the legacy row shape
    here avoids maintaining a second coverage authority.
    """
    states = fetch_jurisdictions(base_url)
    rows: List[Dict[str, Any]] = []
    for state_row in states:
        if not isinstance(state_row, dict):
            raise FlagshipError(
                "the jurisdictions response carried a malformed state row."
            )
        state = state_row.get("state")
        counties = state_row.get("counties")
        if not isinstance(state, str) or not state or not isinstance(counties, list):
            raise FlagshipError(
                "the jurisdictions response carried a malformed state row."
            )
        for county_row in counties:
            if not isinstance(county_row, dict):
                raise FlagshipError(
                    "the jurisdictions response carried a malformed county row."
                )
            county = county_row.get("county")
            cities = county_row.get("cities")
            if (
                not isinstance(county, str)
                or not county
                or not isinstance(cities, list)
            ):
                raise FlagshipError(
                    "the jurisdictions response carried a malformed county row."
                )
            for city_row in cities:
                if not isinstance(city_row, dict):
                    raise FlagshipError(
                        "the jurisdictions response carried a malformed city row."
                    )
                city = city_row.get("city")
                meeting_count = city_row.get("meeting_count")
                covered = city_row.get("covered")
                if (
                    not isinstance(city, str)
                    or not city
                    or isinstance(meeting_count, bool)
                    or not isinstance(meeting_count, int)
                    or meeting_count < 0
                    or not isinstance(covered, bool)
                ):
                    raise FlagshipError(
                        "the jurisdictions response carried a malformed city row."
                    )
                rows.append({
                    "city": city,
                    "county": county,
                    "state": state,
                    "status": "covered" if covered else "not covered yet",
                    "published_count": meeting_count,
                })
    return rows


def fetch_meetings(base_url: str, city: str, year=None) -> Dict[str, Any]:
    """Fetch and enrich one city's versioned public meeting catalog.

    The v1 list is deliberately compact; each listed public id is resolved
    through the corresponding detail endpoint so the local workspace receives
    the processable video and document URLs. Pagination is bounded and cursor
    repetition fails loudly instead of returning a silent partial catalog.
    """
    params: Dict[str, str] = {"city": city}
    if year is not None and str(year).lower() != "all":
        params["year"] = str(year)

    events: List[Dict[str, Any]] = []
    seen_cursors: set[str] = set()
    seen_public_ids: set[str] = set()
    for _page_number in range(_MAX_CATALOG_PAGES):
        data = _get(base_url, "/v1/catalog/meetings", params)
        meetings = (data or {}).get("meetings")
        next_cursor = (data or {}).get("next_cursor")
        if not isinstance(meetings, list) or not isinstance(next_cursor, str):
            raise FlagshipError(
                f"the meetings response for {city} carried an unexpected shape."
            )

        for summary in meetings:
            if not isinstance(summary, dict):
                raise FlagshipError(
                    f"the meetings response for {city} carried a malformed row."
                )
            public_id = summary.get("public_id")
            if not isinstance(public_id, str) or not public_id:
                raise FlagshipError(
                    f"the meetings response for {city} carried a malformed public id."
                )
            if public_id in seen_public_ids:
                raise FlagshipError(
                    f"the meetings response for {city} repeated public id {public_id}."
                )
            seen_public_ids.add(public_id)
            detail = fetch_catalog_detail(base_url, public_id)
            if detail is None:
                raise FlagshipError(
                    f"the catalog listed {public_id}, but its detail disappeared."
                )
            documents = detail.get("documents")
            if not isinstance(documents, dict):
                raise FlagshipError(
                    f"the catalog detail for {public_id} carried no documents object."
                )
            local_processing = detail.get("local_processing")
            if not isinstance(local_processing, dict):
                raise FlagshipError(
                    f"the catalog detail for {public_id} carried no processing status."
                )
            events.append({
                "public_id": public_id,
                "city_name": detail.get("city") or "",
                "county": detail.get("county") or "",
                "state": detail.get("state") or "",
                "meeting_title": detail.get("title") or "",
                "meeting_date": detail.get("date") or "",
                "meeting_time": detail.get("time") or "",
                "meeting_location": detail.get("location") or "",
                "meeting_status": detail.get("meeting_status") or "",
                "agenda_url": documents.get("agenda_url") or "",
                "minutes_url": documents.get("minutes_url") or "",
                "agenda_packet_url": documents.get("packet_url") or "",
                "video_url": detail.get("video_url") or "",
                "availability": detail.get("availability") or "",
                "local_processing": local_processing,
            })

        if not next_cursor:
            return {
                "success": True,
                "events": events,
                "count": len(events),
                "is_stale": False,
                "last_scraped": "",
                "source": "v1_catalog",
            }
        if next_cursor in seen_cursors:
            raise FlagshipError(
                f"the meetings response for {city} repeated its pagination cursor."
            )
        seen_cursors.add(next_cursor)
        params["cursor"] = next_cursor

    raise FlagshipError(
        f"the meetings response for {city} exceeded {_MAX_CATALOG_PAGES} pages."
    )


def fetch_catalog_detail(base_url: str, public_id: str) -> Optional[Dict[str, Any]]:
    """One public catalog record; unknown ids are a normal None result."""
    return _get(
        base_url,
        f"/v1/catalog/meetings/{public_id}",
        allow_404=True,
    )


def fetch_jurisdictions(base_url: str) -> List[Dict[str, Any]]:
    data = _get(base_url, "/v1/catalog/jurisdictions")
    states = (data or {}).get("states")
    if not isinstance(states, list):
        raise FlagshipError("the jurisdictions response carried no states list.")
    return states


def exchange_cli_code(base_url: str, code: str, code_verifier: str) -> Dict[str, Any]:
    return _post(
        base_url,
        "/api/auth/cli/exchange",
        {"code": code, "code_verifier": code_verifier},
    )


def revoke_cli_token(base_url: str, bearer: str) -> Dict[str, Any]:
    return _post(base_url, "/api/auth/cli/revoke", {}, bearer=bearer)


def fetch_cli_me(base_url: str, bearer: str) -> Dict[str, Any]:
    data = _get(base_url, "/api/auth/cli/me", bearer=bearer)
    return data or {}


def register_generation(
    base_url: str, payload: Dict[str, Any], bearer: str
) -> Dict[str, Any]:
    return _post(
        base_url, "/api/generations/register", payload, bearer=bearer
    )


def submit_private_contribution(
    base_url: str, payload: Dict[str, Any], bearer: str
) -> Dict[str, Any]:
    """Send one complete, private meeting package to the flagship intake."""
    return _post(
        base_url, "/api/contributions/submit", payload, bearer=bearer
    )

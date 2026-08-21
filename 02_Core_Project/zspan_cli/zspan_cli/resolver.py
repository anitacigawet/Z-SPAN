"""Resolve public catalog ids into factual rows in the local workspace."""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable, Dict

from zspan_cli import media, workspace
from zspan_cli.config import flagship_url
from zspan_cli.flagship import fetch_catalog_detail

PUBLIC_ID_RE = re.compile(r"^m_[0-9A-Za-z]{22}$")


class ResolveError(Exception):
    """A public meeting id could not be resolved into a usable record."""


def looks_like_public_id(value: Any) -> bool:
    return isinstance(value, str) and PUBLIC_ID_RE.fullmatch(value) is not None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def resolve_and_import(
    public_id: str,
    config: Dict[str, Any],
    *,
    say: Callable[[str], None],
) -> sqlite3.Row:
    if not looks_like_public_id(public_id):
        raise ResolveError(
            "a public_id looks like m_ followed by 22 letters or digits; "
            "copy one from a meeting card on zspan.org."
        )

    detail = fetch_catalog_detail(flagship_url(config), public_id)
    if detail is None:
        raise ResolveError(
            f"{public_id} isn't in the public catalog — check the copied "
            "command, or the meeting may have been removed."
        )

    canonical = detail.get("public_id")
    if not looks_like_public_id(canonical):
        raise ResolveError("the endpoint server returned an unexpected catalog answer.")
    if canonical != public_id:
        say(f"Meeting id {public_id} was merged; adopting canonical id {canonical}.")

    for field in ("title", "date", "city"):
        if not isinstance(detail.get(field), str):
            raise ResolveError("the endpoint server returned an unexpected catalog answer.")

    video_url = _text(detail.get("video_url")).strip()
    if video_url:
        try:
            media.assert_safe_media_url(video_url)
        except media.MediaError as e:
            say(f"Video source refused during import: {e} The factual record was imported without it.")
            video_url = ""

    documents = detail.get("documents")
    documents = documents if isinstance(documents, dict) else {}
    local_processing = detail.get("local_processing")
    local_processing = local_processing if isinstance(local_processing, dict) else {}
    mapped = {
        "public_id": canonical,
        "city_name": _text(detail.get("city")),
        "county": _text(detail.get("county")),
        "state": _text(detail.get("state")),
        "meeting_title": _text(detail.get("title")),
        "meeting_date": _text(detail.get("date")),
        "meeting_time": _text(detail.get("time")),
        "meeting_location": _text(detail.get("location")),
        "meeting_status": _text(detail.get("meeting_status")),
        "agenda_url": _text(documents.get("agenda_url")),
        "minutes_url": _text(documents.get("minutes_url")),
        "agenda_packet_url": _text(documents.get("packet_url")),
        "video_url": video_url,
        "availability": _text(detail.get("availability")),
        "local_processing": local_processing,
    }

    conn = workspace.connect()
    try:
        workspace.upsert_meeting(conn, mapped, import_source="handoff")
        conn.commit()
        row = workspace.get_meeting_by_public_id(conn, canonical)
        if row is None:
            raise ResolveError("the meeting could not be read back from the local workspace.")
        return row
    finally:
        conn.close()

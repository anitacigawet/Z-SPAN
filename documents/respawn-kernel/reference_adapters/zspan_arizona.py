#!/usr/bin/env python3
"""Convert normalized Z-SPAN — Arizona rows to the Respawn meeting contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
LEGACY_TIME = re.compile(r"^(?:0?[1-9]|1[0-2]):[0-5][0-9] [AP]M$", re.IGNORECASE)
ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.\d+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)
ID_SUFFIX = re.compile(r"[^a-z0-9._-]+")
URL_ARTIFACTS = (
    ("agenda_url", "agenda"),
    ("agenda_packet_url", "agenda_packet"),
    ("minutes_url", "minutes"),
    ("video_url", "video"),
    ("ecomment_url", "public_comment"),
)
STATUS_MAP = {
    "scheduled": "scheduled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "minutes available": "completed",
    "agenda available": "unknown",
}


class ConversionError(ValueError):
    """Raised when a legacy row cannot be converted without guessing."""


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _require_text(row: dict[str, Any], key: str, row_number: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConversionError(f"row {row_number}: {key} must be a non-empty string")
    return value.strip()


def _normalize_date(value: str, row_number: int) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ConversionError(f"row {row_number}: meeting_date is not an ISO date: {value!r}") from exc


def _normalize_time(value: Any, timezone: str | None, row_number: int) -> dict[str, Any]:
    if value in (None, ""):
        return {"date": "", "time": None, "timezone": None, "precision": "date"}
    if not isinstance(value, str) or not LEGACY_TIME.fullmatch(value.strip()):
        raise ConversionError(f"row {row_number}: meeting_time must use H:MM AM/PM or be empty")
    if not timezone:
        raise ConversionError(f"row {row_number}: timezone is required when meeting_time is present")
    parsed = datetime.strptime(value.strip().upper(), "%I:%M %p")
    return {
        "date": "",
        "time": parsed.strftime("%H:%M"),
        "timezone": timezone,
        "precision": "minute",
    }


def _meeting_suffix(
    row: dict[str, Any],
    *,
    title: str,
    meeting_date: str,
    meeting_time: str,
    governing_body_id: str,
    source_id: str,
) -> tuple[str, str]:
    native_id = str(row.get("meeting_id") or "").strip()
    if native_id:
        readable = ID_SUFFIX.sub("-", native_id.lower()).strip("-._")[:48] or "native"
        # Vendor-native IDs are commonly unique only inside one source. Fold
        # the source into the public ID so two jurisdictions cannot collide.
        identity = "\x1f".join((source_id, native_id))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        return f"{readable}-{digest}", native_id

    identity = "\x1f".join((governing_body_id, source_id, title, meeting_date, meeting_time))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"derived-{digest}", ""


def _artifacts(row: dict[str, Any], *, locale: str, row_number: int) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for field_name, kind in URL_ARTIFACTS:
        raw = row.get(field_name, "")
        if raw in (None, ""):
            continue
        if not isinstance(raw, str) or not _is_http_url(raw.strip()):
            raise ConversionError(f"row {row_number}: {field_name} must be an absolute http(s) URL or empty")
        url = raw.strip()
        if url in seen_urls:
            continue
        seen_urls.add(url)
        artifacts.append({"kind": kind, "url": url, "language": locale})
    return artifacts


def convert_row(
    row: dict[str, Any],
    *,
    row_number: int,
    country_code: str,
    locale: str,
    timezone: str | None,
    governing_body_id: str,
    source_id: str,
    retrieved_at: str,
) -> dict[str, Any]:
    title = _require_text(row, "meeting_title", row_number)
    meeting_date = _normalize_date(_require_text(row, "meeting_date", row_number), row_number)
    raw_time = row.get("meeting_time", "")
    start = _normalize_time(raw_time, timezone, row_number)
    start["date"] = meeting_date

    status_text = str(row.get("meeting_status") or "").strip()
    lifecycle_status = STATUS_MAP.get(status_text.casefold(), "unknown")
    suffix, native_id = _meeting_suffix(
        row,
        title=title,
        meeting_date=meeting_date,
        meeting_time=str(raw_time or ""),
        governing_body_id=governing_body_id,
        source_id=source_id,
    )

    result: dict[str, Any] = {
        "id": f"{country_code}:meeting:{suffix}",
        "governing_body_id": governing_body_id,
        "names": {locale: title},
        "start": start,
        "lifecycle_status": lifecycle_status,
        "location": str(row.get("meeting_location") or "").strip(),
        "artifacts": _artifacts(row, locale=locale, row_number=row_number),
        "source_id": source_id,
        "source_native_id": native_id,
        "retrieved_at": retrieved_at,
    }
    if status_text:
        result["notes"] = f"Reference status from Z-SPAN — Arizona: {status_text}"
    return result


def convert_rows(
    rows: Iterable[dict[str, Any]],
    *,
    country_code: str,
    locale: str,
    timezone: str | None,
    governing_body_id: str,
    source_id: str,
    retrieved_at: str,
) -> dict[str, Any]:
    if not COUNTRY_CODE.fullmatch(country_code):
        raise ConversionError("country_code must contain exactly two uppercase letters")
    if not LANGUAGE_TAG.fullmatch(locale):
        raise ConversionError("locale must be a BCP 47-style language tag")
    if not governing_body_id.startswith(f"{country_code}:body:"):
        raise ConversionError(f"governing_body_id must begin with {country_code}:body:")
    if not source_id.startswith(f"{country_code}:source:"):
        raise ConversionError(f"source_id must begin with {country_code}:source:")
    if not ISO_DATETIME.fullmatch(retrieved_at):
        raise ConversionError("retrieved_at must be an ISO date-time with a UTC offset or Z")
    try:
        datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConversionError("retrieved_at must be an ISO date-time with a UTC offset or Z") from exc

    meetings: list[dict[str, Any]] = []
    ids: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ConversionError(f"row {row_number}: expected an object")
        converted = convert_row(
            row,
            row_number=row_number,
            country_code=country_code,
            locale=locale,
            timezone=timezone,
            governing_body_id=governing_body_id,
            source_id=source_id,
            retrieved_at=retrieved_at,
        )
        if converted["id"] in ids:
            raise ConversionError(f"row {row_number}: duplicate derived meeting ID {converted['id']}")
        ids.add(converted["id"])
        meetings.append(converted)
    return {"schema_version": 1, "meetings": meetings}


def _load_rows(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, list):
        return document
    if isinstance(document, dict) and isinstance(document.get("meetings"), list):
        return document["meetings"]
    raise ConversionError("input must be an array of meeting rows or an object with a meetings array")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert normalized Z-SPAN rows to Respawn meetings JSON.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--country-code", default="US")
    parser.add_argument("--locale", default="en-US")
    parser.add_argument("--timezone", help="IANA timezone required for rows with a meeting time")
    parser.add_argument("--governing-body-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--retrieved-at", required=True, help="Fixed ISO date-time for deterministic provenance")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output.exists():
        print(f"error: refusing to overwrite existing path: {args.output}", file=sys.stderr)
        return 2
    try:
        document = convert_rows(
            _load_rows(args.input),
            country_code=args.country_code,
            locale=args.locale,
            timezone=args.timezone,
            governing_body_id=args.governing_body_id,
            source_id=args.source_id,
            retrieved_at=args.retrieved_at,
        )
    except (ConversionError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Converted {len(document['meetings'])} meeting(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

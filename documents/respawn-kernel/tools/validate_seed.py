#!/usr/bin/env python3
"""Validate a Respawn country seed without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.\d+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)
REQUIRED_LOCALE_STRINGS = {
    "artifact_agenda",
    "artifact_agenda_packet",
    "artifact_audio",
    "artifact_detail",
    "artifact_minutes",
    "artifact_public_comment",
    "artifact_transcript",
    "artifact_video",
    "back_to_home",
    "breadcrumbs",
    "project_name",
    "project_description",
    "browse_jurisdictions",
    "child_jurisdictions",
    "coverage_summary",
    "governing_bodies",
    "governing_bodies_count",
    "jurisdiction",
    "jurisdictions_count",
    "languages",
    "latest_meetings",
    "meetings_count",
    "no_governing_bodies",
    "no_jurisdictions",
    "source_record",
    "status_cancelled",
    "status_completed",
    "status_scheduled",
    "status_unknown",
    "publication_pending",
    "no_meetings_found",
}
VETO_KEYS = {
    "approved",
    "country_allowed",
    "deployment_decision",
    "go_no_go",
    "no_go",
    "safe",
    "unsafe",
}


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _load_json(path: Path, report: ValidationReport) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        report.error(f"missing required file: {path}")
    except json.JSONDecodeError as exc:
        report.error(f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}")
    return None


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not ISO_DATETIME.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _require_mapping(value: Any, label: str, report: ValidationReport) -> dict[str, Any]:
    if not isinstance(value, dict):
        report.error(f"{label} must be an object")
        return {}
    return value


def _require_list(value: Any, label: str, report: ValidationReport) -> list[Any]:
    if not isinstance(value, list):
        report.error(f"{label} must be an array")
        return []
    return value


def _require_schema_version(document: dict[str, Any], label: str, report: ValidationReport) -> None:
    if document.get("schema_version") != 1:
        report.error(f"{label}.schema_version must be 1")


def _validate_localized_names(
    names: Any,
    *,
    primary_locale: str,
    label: str,
    report: ValidationReport,
) -> None:
    names = _require_mapping(names, label, report)
    for language, value in names.items():
        if not isinstance(language, str) or not LANGUAGE_TAG.fullmatch(language):
            report.error(f"{label} contains invalid language tag {language!r}")
        if not isinstance(value, str) or not value.strip():
            report.error(f"{label}.{language} must be a non-empty string")
    if not isinstance(names.get(primary_locale), str) or not names.get(primary_locale, "").strip():
        report.error(f"{label} must include a non-empty {primary_locale!r} name")


def _validate_url_list(values: Any, label: str, report: ValidationReport) -> None:
    urls = _require_list(values, label, report)
    if not urls:
        report.error(f"{label} must contain at least one source URL")
    for index, value in enumerate(urls):
        if not _is_http_url(value):
            report.error(f"{label}[{index}] must be an absolute http(s) URL")


def _validate_manifest(root: Path, report: ValidationReport) -> dict[str, Any]:
    manifest = _load_json(root / "manifest.json", report)
    manifest = _require_mapping(manifest, "manifest", report)
    project = _require_mapping(manifest.get("project"), "manifest.project", report)
    paths = _require_mapping(manifest.get("paths"), "manifest.paths", report)
    controls = _require_mapping(manifest.get("controls"), "manifest.controls", report)

    kernel_version = manifest.get("kernel_version")
    if not isinstance(kernel_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", kernel_version):
        report.error("manifest.kernel_version must use semantic version form N.N.N")

    code = project.get("country_code")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code):
        report.error("manifest.project.country_code must contain exactly two uppercase letters")
    locale = project.get("primary_locale")
    if not isinstance(locale, str) or not LANGUAGE_TAG.fullmatch(locale):
        report.error("manifest.project.primary_locale must be a BCP 47-style language tag")
    if not isinstance(project.get("name"), str) or not project.get("name", "").strip():
        report.error("manifest.project.name must be non-empty")

    expected_paths = {
        "country_profile": "country/profile.json",
        "jurisdictions": "data/jurisdictions.json",
        "governing_bodies": "data/governing-bodies.json",
        "sources": "data/sources.json",
        "meetings": "data/meetings.json",
        "locales": "locales",
    }
    for key, expected in expected_paths.items():
        if paths.get(key) != expected:
            report.error(f"manifest.paths.{key} must be {expected!r}")

    if controls.get("human_publication_required") is not True:
        report.error("manifest.controls.human_publication_required must be true")
    if controls.get("private_person_protection_required") is not True:
        report.error("manifest.controls.private_person_protection_required must be true")
    if controls.get("recipe_registry") != "sealed_local":
        report.error("manifest.controls.recipe_registry must be 'sealed_local'")
    return manifest


def _validate_country_profile(
    root: Path,
    manifest: dict[str, Any],
    report: ValidationReport,
) -> tuple[str, str, set[str]]:
    profile = _load_json(root / "country" / "profile.json", report)
    profile = _require_mapping(profile, "country profile", report)
    _require_schema_version(profile, "country profile", report)
    project = _require_mapping(manifest.get("project"), "manifest.project", report)
    expected_code = str(project.get("country_code", ""))
    primary_locale = str(project.get("primary_locale", ""))

    country = _require_mapping(profile.get("country"), "country profile.country", report)
    if country.get("code") != expected_code:
        report.error("country profile code must match manifest country code")
    _validate_localized_names(
        country.get("name"),
        primary_locale=primary_locale,
        label="country profile.country.name",
        report=report,
    )

    locales = _require_mapping(profile.get("locales"), "country profile.locales", report)
    if locales.get("primary") != primary_locale:
        report.error("country profile primary locale must match manifest")
    supported = _require_list(locales.get("supported"), "country profile.locales.supported", report)
    if primary_locale not in supported:
        report.error("country profile supported locales must contain the primary locale")

    model = _require_mapping(profile.get("jurisdiction_model"), "country profile.jurisdiction_model", report)
    levels = _require_list(model.get("levels"), "country profile.jurisdiction_model.levels", report)
    level_keys: set[str] = set()
    for index, raw_level in enumerate(levels):
        level = _require_mapping(raw_level, f"jurisdiction level {index}", report)
        key = level.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            report.error(f"jurisdiction level {index} has an invalid key")
            continue
        if key in level_keys:
            report.error(f"duplicate jurisdiction level key: {key}")
        level_keys.add(key)
        if not isinstance(level.get("may_hold_governing_bodies"), bool):
            report.error(f"jurisdiction level {key}.may_hold_governing_bodies must be boolean")
        _validate_localized_names(
            level.get("terms"),
            primary_locale=primary_locale,
            label=f"jurisdiction level {key}.terms",
            report=report,
        )
    for raw_level in levels:
        if not isinstance(raw_level, dict):
            continue
        for parent_key in _require_list(raw_level.get("parent_keys"), f"jurisdiction level {raw_level.get('key')}.parent_keys", report):
            if parent_key not in level_keys:
                report.error(f"jurisdiction level {raw_level.get('key')} names unknown parent level {parent_key!r}")

    context = _require_mapping(profile.get("operating_context"), "country profile.operating_context", report)
    if context.get("does_not_gate_bootstrap") is not True:
        report.error("operating_context.does_not_gate_bootstrap must be true")
    forbidden = sorted(VETO_KEYS.intersection(context))
    if forbidden:
        report.error(f"operating_context contains country-veto fields: {', '.join(forbidden)}")
    for field_name in ("public_meeting_rules", "public_record_rules"):
        if not isinstance(context.get(field_name), str) or not context.get(field_name, "").strip():
            report.error(f"operating_context.{field_name} must be a non-empty string")
    for field_name in (
        "source_access_constraints",
        "publication_constraints",
        "operator_considerations",
        "adaptations",
    ):
        values = _require_list(context.get(field_name), f"operating_context.{field_name}", report)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            report.error(f"operating_context.{field_name} must contain only non-empty strings")
    provenance = _require_list(context.get("provenance"), "operating_context.provenance", report)
    for index, entry_value in enumerate(provenance):
        entry = _require_mapping(entry_value, f"operating_context.provenance[{index}]", report)
        for field_name in ("title", "publisher", "accessed_on"):
            if not isinstance(entry.get(field_name), str) or not entry.get(field_name, "").strip():
                report.error(f"operating_context.provenance[{index}].{field_name} must be non-empty")
        if not _is_http_url(entry.get("url")):
            report.error(f"operating_context.provenance[{index}].url must be an absolute http(s) URL")
        if not _is_iso_date(entry.get("accessed_on")):
            report.error(f"operating_context.provenance[{index}].accessed_on must be an ISO date")

    research_status = profile.get("research_status")
    if research_status not in {"research_pending", "in_review", "researched"}:
        report.error("country profile.research_status is invalid")
    if research_status == "research_pending":
        report.warn("country research is still pending")
    return expected_code, primary_locale, level_keys


def _detect_parent_cycles(parents: dict[str, str | None], report: ValidationReport) -> None:
    for start in parents:
        seen: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in seen:
                report.error(f"jurisdiction parent cycle detected from {start!r} through {current!r}")
                break
            seen.add(current)
            current = parents.get(current)


def _validate_data(
    root: Path,
    *,
    country_code: str,
    primary_locale: str,
    level_keys: set[str],
    report: ValidationReport,
) -> None:
    jurisdiction_doc = _require_mapping(
        _load_json(root / "data" / "jurisdictions.json", report),
        "jurisdictions document",
        report,
    )
    _require_schema_version(jurisdiction_doc, "jurisdictions document", report)
    jurisdiction_ids: set[str] = set()
    parent_by_id: dict[str, str | None] = {}
    jurisdictions = _require_list(jurisdiction_doc.get("jurisdictions"), "jurisdictions", report)
    for index, raw in enumerate(jurisdictions):
        row = _require_mapping(raw, f"jurisdiction[{index}]", report)
        row_id = row.get("id")
        if not isinstance(row_id, str) or not re.fullmatch(rf"{re.escape(country_code)}:[a-z0-9][a-z0-9._-]*", row_id):
            report.error(f"jurisdiction[{index}].id must begin with {country_code}:")
            continue
        if row_id in jurisdiction_ids:
            report.error(f"duplicate jurisdiction id: {row_id}")
        jurisdiction_ids.add(row_id)
        parent_by_id[row_id] = row.get("parent_id")
        if row.get("level_key") not in level_keys:
            report.error(f"jurisdiction {row_id} uses unknown level {row.get('level_key')!r}")
        _validate_localized_names(row.get("names"), primary_locale=primary_locale, label=f"jurisdiction {row_id}.names", report=report)
        if row.get("governance_status") not in {"confirmed", "statistical_only", "historical", "disputed", "unknown"}:
            report.error(f"jurisdiction {row_id}.governance_status is invalid")
        _validate_url_list(row.get("source_urls"), f"jurisdiction {row_id}.source_urls", report)
    for row_id, parent_id in parent_by_id.items():
        if parent_id is not None and parent_id not in jurisdiction_ids:
            report.error(f"jurisdiction {row_id} names missing parent {parent_id!r}")
    _detect_parent_cycles(parent_by_id, report)

    bodies_doc = _require_mapping(
        _load_json(root / "data" / "governing-bodies.json", report),
        "governing-bodies document",
        report,
    )
    _require_schema_version(bodies_doc, "governing-bodies document", report)
    body_ids: set[str] = set()
    bodies = _require_list(bodies_doc.get("governing_bodies"), "governing_bodies", report)
    for index, raw in enumerate(bodies):
        row = _require_mapping(raw, f"governing_body[{index}]", report)
        row_id = row.get("id")
        if not isinstance(row_id, str) or not re.fullmatch(rf"{re.escape(country_code)}:body:[a-z0-9][a-z0-9._-]*", row_id):
            report.error(f"governing_body[{index}].id must begin with {country_code}:body:")
            continue
        if row_id in body_ids:
            report.error(f"duplicate governing-body id: {row_id}")
        body_ids.add(row_id)
        if row.get("jurisdiction_id") not in jurisdiction_ids:
            report.error(f"governing body {row_id} references unknown jurisdiction {row.get('jurisdiction_id')!r}")
        _validate_localized_names(row.get("names"), primary_locale=primary_locale, label=f"governing body {row_id}.names", report=report)
        if not isinstance(row.get("body_type"), str) or not re.fullmatch(r"[a-z][a-z0-9_]*", row.get("body_type", "")):
            report.error(f"governing body {row_id}.body_type is invalid")
        if row.get("status") not in {"active", "inactive", "unknown"}:
            report.error(f"governing body {row_id}.status is invalid")
        _validate_url_list(row.get("source_urls"), f"governing body {row_id}.source_urls", report)

    sources_doc = _require_mapping(
        _load_json(root / "data" / "sources.json", report),
        "sources document",
        report,
    )
    _require_schema_version(sources_doc, "sources document", report)
    source_ids: set[str] = set()
    sources = _require_list(sources_doc.get("sources"), "sources", report)
    for index, raw in enumerate(sources):
        row = _require_mapping(raw, f"source[{index}]", report)
        row_id = row.get("id")
        if not isinstance(row_id, str) or not re.fullmatch(rf"{re.escape(country_code)}:source:[a-z0-9][a-z0-9._-]*", row_id):
            report.error(f"source[{index}].id must begin with {country_code}:source:")
            continue
        if row_id in source_ids:
            report.error(f"duplicate source id: {row_id}")
        source_ids.add(row_id)
        if row.get("governing_body_id") not in body_ids:
            report.error(f"source {row_id} references unknown governing body {row.get('governing_body_id')!r}")
        if not _is_http_url(row.get("public_url")):
            report.error(f"source {row_id}.public_url must be an absolute http(s) URL")
        if row.get("recipe_visibility") != "sealed_local":
            report.error(f"source {row_id}.recipe_visibility must be 'sealed_local'")
        if row.get("kind") not in {"calendar", "agenda", "minutes", "video", "transcript", "combined"}:
            report.error(f"source {row_id}.kind is invalid")
        if row.get("status") not in {
            "research_pending",
            "adapter_candidate",
            "awaiting_independent_audit",
            "verified",
            "source_unavailable",
            "source_changed",
        }:
            report.error(f"source {row_id}.status is invalid")
        if not isinstance(row.get("adapter_family"), str) or not row.get("adapter_family", "").strip():
            report.error(f"source {row_id}.adapter_family must be non-empty")
        if row.get("last_verified_on") is not None and not _is_iso_date(row.get("last_verified_on")):
            report.error(f"source {row_id}.last_verified_on must be null or an ISO date")

    meetings_doc = _require_mapping(
        _load_json(root / "data" / "meetings.json", report),
        "meetings document",
        report,
    )
    _require_schema_version(meetings_doc, "meetings document", report)
    meeting_ids: set[str] = set()
    meetings = _require_list(meetings_doc.get("meetings"), "meetings", report)
    for index, raw in enumerate(meetings):
        row = _require_mapping(raw, f"meeting[{index}]", report)
        row_id = row.get("id")
        if not isinstance(row_id, str) or not re.fullmatch(rf"{re.escape(country_code)}:meeting:[a-z0-9][a-z0-9._-]*", row_id):
            report.error(f"meeting[{index}].id must begin with {country_code}:meeting:")
            continue
        if row_id in meeting_ids:
            report.error(f"duplicate meeting id: {row_id}")
        meeting_ids.add(row_id)
        if row.get("governing_body_id") not in body_ids:
            report.error(f"meeting {row_id} references unknown governing body {row.get('governing_body_id')!r}")
        if row.get("source_id") not in source_ids:
            report.error(f"meeting {row_id} references unknown source {row.get('source_id')!r}")
        _validate_localized_names(row.get("names"), primary_locale=primary_locale, label=f"meeting {row_id}.names", report=report)
        start = _require_mapping(row.get("start"), f"meeting {row_id}.start", report)
        if not _is_iso_date(start.get("date")):
            report.error(f"meeting {row_id}.start.date must be an ISO date")
        precision = start.get("precision")
        if precision not in {"date", "minute", "second"}:
            report.error(f"meeting {row_id}.start.precision is invalid")
        if precision == "date":
            if start.get("time") is not None or start.get("timezone") is not None:
                report.error(f"meeting {row_id} with date precision must have null time and timezone")
        else:
            time_value = start.get("time")
            expected_pattern = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]" + (r":[0-5][0-9]" if precision == "second" else "")
            if not isinstance(time_value, str) or not re.fullmatch(expected_pattern, time_value):
                report.error(f"meeting {row_id}.start.time does not match {precision} precision")
            if not isinstance(start.get("timezone"), str) or not start.get("timezone", "").strip():
                report.error(f"meeting {row_id}.start.timezone is required when time is known")
        if row.get("lifecycle_status") not in {"scheduled", "cancelled", "completed", "unknown"}:
            report.error(f"meeting {row_id}.lifecycle_status is invalid")
        if not _is_iso_datetime(row.get("retrieved_at")):
            report.error(f"meeting {row_id}.retrieved_at must be an ISO date-time")
        for artifact_index, artifact_value in enumerate(_require_list(row.get("artifacts"), f"meeting {row_id}.artifacts", report)):
            artifact = _require_mapping(artifact_value, f"meeting {row_id}.artifacts[{artifact_index}]", report)
            if artifact.get("kind") not in {"detail", "agenda", "agenda_packet", "minutes", "video", "audio", "transcript", "public_comment"}:
                report.error(f"meeting {row_id}.artifacts[{artifact_index}].kind is invalid")
            if not _is_http_url(artifact.get("url")):
                report.error(f"meeting {row_id}.artifacts[{artifact_index}].url must be an absolute http(s) URL")

    if not jurisdictions:
        report.warn("no jurisdictions have been enumerated")
    if not bodies:
        report.warn("no governing bodies have been registered")
    if not sources:
        report.warn("no public sources have been registered")
    if not meetings:
        report.warn("no meetings have been normalized")


def _validate_locales(root: Path, primary_locale: str, report: ValidationReport) -> None:
    locales_dir = root / "locales"
    if not locales_dir.is_dir():
        report.error(f"missing locales directory: {locales_dir}")
        return
    seen: set[str] = set()
    primary_status: str | None = None
    for path in sorted(locales_dir.glob("*.json")):
        locale_doc = _require_mapping(_load_json(path, report), f"locale {path.name}", report)
        _require_schema_version(locale_doc, f"locale {path.name}", report)
        locale = locale_doc.get("locale")
        if locale != path.stem:
            report.error(f"locale file {path.name} must declare locale {path.stem!r}")
        if not isinstance(locale, str) or not LANGUAGE_TAG.fullmatch(locale):
            report.error(f"locale file {path.name} has an invalid language tag")
            continue
        if locale in seen:
            report.error(f"duplicate locale declaration: {locale}")
        seen.add(locale)
        if locale_doc.get("direction") not in {"ltr", "rtl"}:
            report.error(f"locale {locale} direction must be 'ltr' or 'rtl'")
        if locale_doc.get("status") not in {"source", "draft", "back_check_pending", "reviewed"}:
            report.error(f"locale {locale} status is invalid")
        strings = _require_mapping(locale_doc.get("strings"), f"locale {locale}.strings", report)
        missing = sorted(key for key in REQUIRED_LOCALE_STRINGS if not isinstance(strings.get(key), str) or not strings.get(key, "").strip())
        if missing:
            report.error(f"locale {locale} is missing required strings: {', '.join(missing)}")
        if locale == primary_locale:
            primary_status = str(locale_doc.get("status", ""))
    if primary_locale not in seen:
        report.error(f"primary locale file is missing: locales/{primary_locale}.json")
    elif primary_status not in {"source", "reviewed"}:
        report.warn(f"primary locale {primary_locale} is not yet reviewed")


def validate_seed(root: Path) -> ValidationReport:
    root = root.expanduser().resolve()
    report = ValidationReport()
    if not root.is_dir():
        report.error(f"seed directory does not exist: {root}")
        return report
    manifest = _validate_manifest(root, report)
    country_code, primary_locale, level_keys = _validate_country_profile(root, manifest, report)
    _validate_data(
        root,
        country_code=country_code,
        primary_locale=primary_locale,
        level_keys=level_keys,
        report=report,
    )
    _validate_locales(root, primary_locale, report)
    for required_doc in (
        "README.md",
        "RESPAWN.md",
        "AGENTS.md",
        "TASKS.md",
        "DECISIONS.md",
        "LICENSE",
        "NOTICE",
        ".gitignore",
    ):
        if not (root / required_doc).is_file():
            report.error(f"missing required repository document: {required_doc}")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a Respawn country-library seed.")
    parser.add_argument("seed", type=Path, help="Path to the generated country repository")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = validate_seed(args.seed)
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    for error in report.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if report.ok:
        print(f"PASS: seed is structurally valid ({len(report.warnings)} warning(s))")
        return 0
    print(f"FAIL: {len(report.errors)} error(s), {len(report.warnings)} warning(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

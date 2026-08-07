#!/usr/bin/env python3
"""Build a standalone multilingual reference site from a Respawn seed."""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import posixpath
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = PROJECT_ROOT / "site_assets"
VALIDATOR_PATH = PROJECT_ROOT / "tools" / "validate_seed.py"
PUBLISHABLE_LOCALE_STATUSES = {"source", "reviewed"}


class BuildError(ValueError):
    """Raised when a seed cannot be rendered honestly."""


def _load_validator():
    spec = importlib.util.spec_from_file_location("respawn_site_validator", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise BuildError(f"could not load validator: {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"could not read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise BuildError(f"{path} must contain a JSON object")
    return document


def _localized(names: Any, locale: str, primary_locale: str) -> str:
    if not isinstance(names, dict):
        return ""
    candidates = (
        locale,
        locale.split("-", 1)[0],
        primary_locale,
        primary_locale.split("-", 1)[0],
        "en",
    )
    for candidate in candidates:
        value = names.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in sorted(names):
        value = names[key]
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _page_name(jurisdiction_id: str) -> str:
    digest = hashlib.sha256(jurisdiction_id.encode("utf-8")).hexdigest()[:20]
    return f"jurisdiction-{digest}.html"


def _relative_href(from_path: PurePosixPath, to_path: PurePosixPath) -> str:
    return posixpath.relpath(str(to_path), start=str(from_path.parent))


def _locale_page(locale: str, primary_locale: str, page: PurePosixPath) -> PurePosixPath:
    if locale == primary_locale:
        return page
    return PurePosixPath("locale") / locale / page


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _locale_nav(
    *,
    current_path: PurePosixPath,
    page: PurePosixPath,
    current_locale: str,
    primary_locale: str,
    locale_documents: dict[str, dict[str, Any]],
) -> str:
    if len(locale_documents) < 2:
        return ""
    links: list[str] = []
    for locale, document in locale_documents.items():
        label = _escape(document["language_name"])
        if locale == current_locale:
            links.append(f'<span aria-current="page">{label}</span>')
            continue
        target = _locale_page(locale, primary_locale, page)
        href = _escape(_relative_href(current_path, target))
        links.append(f'<a href="{href}" hreflang="{_escape(locale)}">{label}</a>')
    language_label = _escape(locale_documents[current_locale]["strings"]["languages"])
    return f'<nav class="language-nav" aria-label="{language_label}">{"".join(links)}</nav>'


def _shell(
    *,
    title: str,
    content: str,
    current_path: PurePosixPath,
    page: PurePosixPath,
    locale: str,
    primary_locale: str,
    locale_document: dict[str, Any],
    locale_documents: dict[str, dict[str, Any]],
    preview: bool,
) -> str:
    strings = locale_document["strings"]
    stylesheet = _escape(_relative_href(current_path, PurePosixPath("assets/styles.css")))
    robots = '<meta name="robots" content="noindex,nofollow">' if preview else ""
    preview_banner = ""
    if preview:
        preview_banner = (
            '<div class="preview-banner" role="status">'
            f'{_escape(strings["publication_pending"])}</div>'
        )
    nav = _locale_nav(
        current_path=current_path,
        page=page,
        current_locale=locale,
        primary_locale=primary_locale,
        locale_documents=locale_documents,
    )
    return f"""<!doctype html>
<html lang="{_escape(locale)}" dir="{_escape(locale_document['direction'])}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {robots}
  <title>{_escape(title)}</title>
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="{stylesheet}">
</head>
<body>
  {preview_banner}
  <div class="site-frame">
    {nav}
    {content}
  </div>
</body>
</html>
"""


def _coverage_stats(strings: dict[str, str], counts: tuple[int, int, int]) -> str:
    labels = (
        strings["jurisdictions_count"],
        strings["governing_bodies_count"],
        strings["meetings_count"],
    )
    cards = "".join(
        f'<div class="stat"><strong>{count}</strong><span>{_escape(label)}</span></div>'
        for count, label in zip(counts, labels)
    )
    return f'<section class="stats" aria-label="{_escape(strings["coverage_summary"])}">{cards}</section>'


def _jurisdiction_list(
    jurisdictions: Iterable[dict[str, Any]],
    *,
    locale: str,
    primary_locale: str,
    current_path: PurePosixPath,
    strings: dict[str, str],
) -> str:
    items: list[str] = []
    for row in jurisdictions:
        page = PurePosixPath("jurisdictions") / _page_name(row["id"])
        href = _escape(_relative_href(current_path, _locale_page(locale, primary_locale, page)))
        name = _escape(_localized(row["names"], locale, primary_locale))
        items.append(
            f'<li><a class="jurisdiction-link" href="{href}">'
            f'<span dir="auto">{name}</span><span aria-hidden="true">→</span></a></li>'
        )
    if not items:
        return f'<p class="empty-state">{_escape(strings["no_jurisdictions"])}</p>'
    return f'<ul class="jurisdiction-list">{"".join(items)}</ul>'


def _meeting_cards(
    meetings: Iterable[dict[str, Any]],
    *,
    locale: str,
    primary_locale: str,
    source_by_id: dict[str, dict[str, Any]],
    strings: dict[str, str],
) -> str:
    cards: list[str] = []
    for meeting in meetings:
        start = meeting["start"]
        visible_time = _escape(start["date"])
        datetime_value = start["date"]
        if start["time"]:
            visible_time = f'{visible_time} · {_escape(start["time"])}'
            datetime_value = f'{start["date"]}T{start["time"]}'
        status_key = f'status_{meeting["lifecycle_status"]}'
        source = source_by_id[meeting["source_id"]]
        artifacts = list(meeting.get("artifacts", []))
        artifact_link_parts: list[str] = []
        for item in artifacts:
            kind = item["kind"]
            label = strings.get(f"artifact_{kind}", kind.replace("_", " ").title())
            artifact_link_parts.append(
                f'<a href="{_escape(item["url"])}" rel="noreferrer">{_escape(label)}</a>'
            )
        artifact_links = "".join(artifact_link_parts)
        source_link = (
            f'<a href="{_escape(source["public_url"])}" rel="noreferrer">'
            f'{_escape(strings["source_record"])}</a>'
        )
        location = ""
        if meeting.get("location"):
            location = f'<p class="meeting-location">{_escape(meeting["location"])}</p>'
        cards.append(
            '<article class="meeting-card">'
            f'<div class="meeting-meta"><time datetime="{_escape(datetime_value)}">{visible_time}</time>'
            f'<span>{_escape(strings[status_key])}</span></div>'
            f'<h3 dir="auto">{_escape(_localized(meeting["names"], locale, primary_locale))}</h3>'
            f'{location}<div class="artifact-links">{artifact_links}{source_link}</div>'
            '</article>'
        )
    if not cards:
        return f'<p class="empty-state">{_escape(strings["no_meetings_found"])}</p>'
    return f'<div class="meeting-grid">{"".join(cards)}</div>'


def _breadcrumbs(
    jurisdiction: dict[str, Any],
    *,
    jurisdiction_by_id: dict[str, dict[str, Any]],
    locale: str,
    primary_locale: str,
    current_path: PurePosixPath,
    strings: dict[str, str],
) -> str:
    chain: list[dict[str, Any]] = []
    cursor: dict[str, Any] | None = jurisdiction
    while cursor is not None:
        chain.append(cursor)
        parent_id = cursor.get("parent_id")
        cursor = jurisdiction_by_id.get(parent_id) if parent_id else None
    chain.reverse()
    home_path = _locale_page(locale, primary_locale, PurePosixPath("index.html"))
    links = [
        f'<a href="{_escape(_relative_href(current_path, home_path))}">{_escape(strings["back_to_home"])}</a>'
    ]
    for item in chain[:-1]:
        target = _locale_page(
            locale,
            primary_locale,
            PurePosixPath("jurisdictions") / _page_name(item["id"]),
        )
        links.append(
            f'<a href="{_escape(_relative_href(current_path, target))}">'
            f'<span dir="auto">{_escape(_localized(item["names"], locale, primary_locale))}</span></a>'
        )
    links.append(
        f'<span aria-current="page" dir="auto">'
        f'{_escape(_localized(chain[-1]["names"], locale, primary_locale))}</span>'
    )
    separator = '<span aria-hidden="true">/</span>'
    return f'<nav class="breadcrumbs" aria-label="{_escape(strings["breadcrumbs"])}">{separator.join(links)}</nav>'


def _render_locale(
    output_root: Path,
    *,
    manifest: dict[str, Any],
    profile: dict[str, Any],
    locale: str,
    locale_document: dict[str, Any],
    locale_documents: dict[str, dict[str, Any]],
    jurisdictions: list[dict[str, Any]],
    bodies: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    meetings: list[dict[str, Any]],
    preview: bool,
) -> None:
    primary_locale = manifest["project"]["primary_locale"]
    strings = locale_document["strings"]
    jurisdiction_by_id = {row["id"]: row for row in jurisdictions}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for row in jurisdictions:
        children.setdefault(row.get("parent_id"), []).append(row)
    for rows in children.values():
        rows.sort(key=lambda row: _localized(row["names"], locale, primary_locale).casefold())
    bodies_by_jurisdiction: dict[str, list[dict[str, Any]]] = {}
    for row in bodies:
        bodies_by_jurisdiction.setdefault(row["jurisdiction_id"], []).append(row)
    source_by_id = {row["id"]: row for row in sources}
    meetings_by_body: dict[str, list[dict[str, Any]]] = {}
    for row in meetings:
        meetings_by_body.setdefault(row["governing_body_id"], []).append(row)
    for rows in meetings_by_body.values():
        rows.sort(key=lambda row: (row["start"]["date"], row["start"]["time"] or "", row["id"]), reverse=True)

    roots = children.get(None, [])
    if len(roots) == 1 and roots[0]["level_key"] == "country":
        navigation_rows = children.get(roots[0]["id"], []) or roots
    else:
        navigation_rows = roots
    latest = sorted(
        meetings,
        key=lambda row: (row["start"]["date"], row["start"]["time"] or "", row["id"]),
        reverse=True,
    )[:12]
    index_page = PurePosixPath("index.html")
    current_index = _locale_page(locale, primary_locale, index_page)
    index_content = (
        '<header class="hero">'
        f'<p class="eyebrow" dir="auto">{_escape(_localized(profile["country"]["name"], locale, primary_locale))}</p>'
        f'<h1>{_escape(strings["project_name"])}</h1>'
        f'<p class="intro">{_escape(strings["project_description"])}</p>'
        '</header>'
        + _coverage_stats(strings, (len(jurisdictions), len(bodies), len(meetings)))
        + f'<main><section><h2>{_escape(strings["browse_jurisdictions"])}</h2>'
        + _jurisdiction_list(
            navigation_rows,
            locale=locale,
            primary_locale=primary_locale,
            current_path=current_index,
            strings=strings,
        )
        + f'</section><section><h2>{_escape(strings["latest_meetings"])}</h2>'
        + _meeting_cards(
            latest,
            locale=locale,
            primary_locale=primary_locale,
            source_by_id=source_by_id,
            strings=strings,
        )
        + '</section></main>'
    )
    target = output_root / current_index
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _shell(
            title=strings["project_name"],
            content=index_content,
            current_path=current_index,
            page=index_page,
            locale=locale,
            primary_locale=primary_locale,
            locale_document=locale_document,
            locale_documents=locale_documents,
            preview=preview,
        ),
        encoding="utf-8",
    )

    for jurisdiction in jurisdictions:
        page = PurePosixPath("jurisdictions") / _page_name(jurisdiction["id"])
        current_path = _locale_page(locale, primary_locale, page)
        direct_bodies = sorted(
            bodies_by_jurisdiction.get(jurisdiction["id"], []),
            key=lambda row: _localized(row["names"], locale, primary_locale).casefold(),
        )
        body_sections: list[str] = []
        for body in direct_bodies:
            body_meetings = meetings_by_body.get(body["id"], [])
            body_sections.append(
                '<section class="body-section">'
                f'<h2 dir="auto">{_escape(_localized(body["names"], locale, primary_locale))}</h2>'
                + _meeting_cards(
                    body_meetings,
                    locale=locale,
                    primary_locale=primary_locale,
                    source_by_id=source_by_id,
                    strings=strings,
                )
                + '</section>'
            )
        if not body_sections:
            body_sections.append(f'<p class="empty-state">{_escape(strings["no_governing_bodies"])}</p>')
        content = (
            _breadcrumbs(
                jurisdiction,
                jurisdiction_by_id=jurisdiction_by_id,
                locale=locale,
                primary_locale=primary_locale,
                current_path=current_path,
                strings=strings,
            )
            + '<header class="page-header">'
            f'<p class="eyebrow">{_escape(strings["jurisdiction"])}</p>'
            f'<h1 dir="auto">{_escape(_localized(jurisdiction["names"], locale, primary_locale))}</h1>'
            '</header><main>'
            f'<section><h2>{_escape(strings["child_jurisdictions"])}</h2>'
            + _jurisdiction_list(
                children.get(jurisdiction["id"], []),
                locale=locale,
                primary_locale=primary_locale,
                current_path=current_path,
                strings=strings,
            )
            + f'</section><section><h2>{_escape(strings["governing_bodies"])}</h2>'
            + "".join(body_sections)
            + '</section></main>'
        )
        target = output_root / current_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _shell(
                title=f'{_localized(jurisdiction["names"], locale, primary_locale)} · {strings["project_name"]}',
                content=content,
                current_path=current_path,
                page=page,
                locale=locale,
                primary_locale=primary_locale,
                locale_document=locale_document,
                locale_documents=locale_documents,
                preview=preview,
            ),
            encoding="utf-8",
        )


def _confirmed_lineage(
    jurisdiction_id: str,
    jurisdiction_by_id: dict[str, dict[str, Any]],
) -> bool:
    seen: set[str] = set()
    current_id: str | None = jurisdiction_id
    while current_id is not None:
        if current_id in seen:
            return False
        seen.add(current_id)
        row = jurisdiction_by_id.get(current_id)
        if row is None or row.get("governance_status") != "confirmed":
            return False
        current_id = row.get("parent_id")
    return True


def _publishable_records(
    documents: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    all_jurisdictions = documents["jurisdictions"]["jurisdictions"]
    jurisdiction_by_id = {row["id"]: row for row in all_jurisdictions}
    jurisdictions = [row for row in all_jurisdictions if _confirmed_lineage(row["id"], jurisdiction_by_id)]
    jurisdiction_ids = {row["id"] for row in jurisdictions}
    bodies = [
        row for row in documents["bodies"]["governing_bodies"]
        if row["status"] == "active" and row["jurisdiction_id"] in jurisdiction_ids
    ]
    body_ids = {row["id"] for row in bodies}
    sources = [
        row for row in documents["sources"]["sources"]
        if row["status"] == "verified" and row["governing_body_id"] in body_ids
    ]
    source_ids = {row["id"] for row in sources}
    meetings = [
        row for row in documents["meetings"]["meetings"]
        if row["governing_body_id"] in body_ids and row["source_id"] in source_ids
    ]
    return jurisdictions, bodies, sources, meetings


def build_site(seed_root: Path, output: Path, *, publication_approved: bool = False) -> Path:
    """Build a deterministic site without overwriting an existing output path."""

    seed_root = seed_root.expanduser().resolve()
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing path: {output}")
    validator = _load_validator()
    report = validator.validate_seed(seed_root)
    if report.errors:
        raise BuildError("seed validation failed: " + "; ".join(report.errors))

    manifest = _load_json(seed_root / "manifest.json")
    profile = _load_json(seed_root / manifest["paths"]["country_profile"])
    documents = {
        "jurisdictions": _load_json(seed_root / manifest["paths"]["jurisdictions"]),
        "bodies": _load_json(seed_root / manifest["paths"]["governing_bodies"]),
        "sources": _load_json(seed_root / manifest["paths"]["sources"]),
        "meetings": _load_json(seed_root / manifest["paths"]["meetings"]),
    }
    all_locales: dict[str, dict[str, Any]] = {}
    for locale in profile["locales"]["supported"]:
        all_locales[locale] = _load_json(seed_root / manifest["paths"]["locales"] / f"{locale}.json")
    primary_locale = manifest["project"]["primary_locale"]

    if publication_approved:
        if profile["research_status"] != "researched":
            raise BuildError("approved publication requires country research_status=researched")
        locale_documents = {
            locale: document for locale, document in all_locales.items()
            if document["status"] in PUBLISHABLE_LOCALE_STATUSES
            and not any(str(value).startswith("[Translation needed]") for value in document["strings"].values())
        }
        if primary_locale not in locale_documents:
            raise BuildError("approved publication requires a reviewed primary locale with no translation placeholders")
    else:
        locale_documents = all_locales

    jurisdictions, bodies, sources, meetings = _publishable_records(documents)
    if publication_approved and not meetings:
        raise BuildError("approved publication requires at least one meeting from a verified source and confirmed jurisdiction")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="respawn-site-", dir=output.parent) as temp:
        staging = Path(temp) / "site"
        staging.mkdir()
        assets = staging / "assets"
        assets.mkdir()
        shutil.copy2(ASSET_ROOT / "styles.css", assets / "styles.css")
        for locale, locale_document in locale_documents.items():
            _render_locale(
                staging,
                manifest=manifest,
                profile=profile,
                locale=locale,
                locale_document=locale_document,
                locale_documents=locale_documents,
                jurisdictions=jurisdictions,
                bodies=bodies,
                sources=sources,
                meetings=meetings,
                preview=not publication_approved,
            )
        staging.rename(output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a static reference library from a Respawn country seed.")
    parser.add_argument("seed", type=Path, help="Country repository root")
    parser.add_argument("--output", required=True, type=Path, help="New site output directory")
    parser.add_argument(
        "--publication-approved",
        action="store_true",
        help="Build a public artifact after human approval; otherwise emit a no-index preview",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = build_site(args.seed, args.output, publication_approved=args.publication_approved)
    except (BuildError, FileExistsError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    mode = "publication artifact" if args.publication_approved else "no-index preview"
    print(f"Built {mode} at {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


KERNEL_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


create_seed_module = _load_module("respawn_site_create_seed", KERNEL_ROOT / "tools" / "create_seed.py")
build_site_module = _load_module("respawn_build_site", KERNEL_ROOT / "tools" / "build_site.py")


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


class ReferenceSiteTests(unittest.TestCase):
    def _create(self, root: Path, *, locale: str = "en") -> Path:
        return create_seed_module.create_seed(
            root / "seed",
            country_name="Example Country",
            country_code="XX",
            project_name="Example Civic Library",
            primary_locale=locale,
            language_name="Persian" if locale == "fa" else None,
        )

    def _write_json(self, path: Path, document) -> None:
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _populate_publishable_chain(self, seed: Path, *, meeting_title: str = "Regular meeting") -> None:
        profile_path = seed / "country" / "profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["research_status"] = "researched"
        self._write_json(profile_path, profile)
        self._write_json(seed / "data" / "jurisdictions.json", {
            "schema_version": 1,
            "jurisdictions": [{
                "id": "XX:country",
                "parent_id": None,
                "level_key": "country",
                "names": {"en": "Example Country"},
                "governance_status": "confirmed",
                "source_urls": ["https://example.org/country"],
            }],
        })
        self._write_json(seed / "data" / "governing-bodies.json", {
            "schema_version": 1,
            "governing_bodies": [{
                "id": "XX:body:country.assembly",
                "jurisdiction_id": "XX:country",
                "body_type": "assembly",
                "names": {"en": "Example Assembly"},
                "source_urls": ["https://example.org/assembly"],
                "status": "active",
            }],
        })
        self._write_json(seed / "data" / "sources.json", {
            "schema_version": 1,
            "sources": [{
                "id": "XX:source:country.calendar",
                "governing_body_id": "XX:body:country.assembly",
                "kind": "calendar",
                "public_url": "https://example.org/calendar",
                "adapter_family": "reference_html",
                "recipe_visibility": "sealed_local",
                "status": "verified",
                "last_verified_on": "2026-08-05",
            }],
        })
        self._write_json(seed / "data" / "meetings.json", {
            "schema_version": 1,
            "meetings": [{
                "id": "XX:meeting:one",
                "governing_body_id": "XX:body:country.assembly",
                "names": {"en": meeting_title},
                "start": {
                    "date": "2026-08-05",
                    "time": None,
                    "timezone": None,
                    "precision": "date",
                },
                "lifecycle_status": "scheduled",
                "location": "Public meeting room",
                "artifacts": [{
                    "kind": "agenda",
                    "url": "https://example.org/agenda",
                    "language": "en",
                }],
                "source_id": "XX:source:country.calendar",
                "source_native_id": "one",
                "retrieved_at": "2026-08-05T12:00:00+00:00",
            }],
        })

    def test_generated_seed_is_self_contained(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp))
            self.assertTrue((seed / "tools" / "validate_seed.py").is_file())
            self.assertTrue((seed / "tools" / "build_site.py").is_file())
            self.assertTrue((seed / "contracts" / "meeting.schema.json").is_file())
            self.assertTrue((seed / "site_assets" / "styles.css").is_file())
            generated_builder = _load_module(
                "generated_respawn_build_site",
                seed / "tools" / "build_site.py",
            )
            output = Path(temp) / "standalone-preview"
            generated_builder.build_site(seed, output)
            self.assertTrue((output / "index.html").is_file())

    def test_preview_is_noindex_and_respects_rtl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp), locale="fa")
            output = Path(temp) / "preview"
            build_site_module.build_site(seed, output)
            persian = (output / "index.html").read_text(encoding="utf-8")
            english = (output / "locale" / "en" / "index.html").read_text(encoding="utf-8")
            self.assertIn('<html lang="fa" dir="rtl">', persian)
            self.assertIn('<meta name="robots" content="noindex,nofollow">', persian)
            self.assertIn('<html lang="en" dir="ltr">', english)

    def test_publication_requires_research_and_a_publishable_meeting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp))
            with self.assertRaisesRegex(build_site_module.BuildError, "research_status=researched"):
                build_site_module.build_site(seed, Path(temp) / "site", publication_approved=True)

            profile_path = seed / "country" / "profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["research_status"] = "researched"
            self._write_json(profile_path, profile)
            with self.assertRaisesRegex(build_site_module.BuildError, "verified source"):
                build_site_module.build_site(seed, Path(temp) / "site", publication_approved=True)

    def test_approved_site_is_deterministic_and_escapes_record_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp))
            self._populate_publishable_chain(seed, meeting_title='<script>alert("x")</script>')
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            build_site_module.build_site(seed, first, publication_approved=True)
            build_site_module.build_site(seed, second, publication_approved=True)
            first_files = {
                path.relative_to(first): path.read_bytes()
                for path in first.rglob("*") if path.is_file()
            }
            second_files = {
                path.relative_to(second): path.read_bytes()
                for path in second.rglob("*") if path.is_file()
            }
            self.assertEqual(first_files, second_files)
            index = (first / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>", index)
            self.assertIn("&lt;script&gt;", index)
            self.assertNotIn("noindex", index)
            self.assertIn('href="https://example.org/agenda"', index)

            for html_path in first.rglob("*.html"):
                collector = _LinkCollector()
                collector.feed(html_path.read_text(encoding="utf-8"))
                for href in collector.hrefs:
                    parsed = urlparse(href)
                    if parsed.scheme or parsed.netloc:
                        continue
                    target = (html_path.parent / parsed.path).resolve()
                    self.assertTrue(target.is_file(), f"broken local link in {html_path}: {href}")

    def test_unverified_source_is_not_rendered_as_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp))
            self._populate_publishable_chain(seed)
            source_path = seed / "data" / "sources.json"
            sources = json.loads(source_path.read_text(encoding="utf-8"))
            sources["sources"][0]["status"] = "awaiting_independent_audit"
            self._write_json(source_path, sources)
            output = Path(temp) / "preview"
            build_site_module.build_site(seed, output)
            index = (output / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("Regular meeting", index)
            self.assertIn("No verified meetings are published here yet.", index)

    def test_builder_refuses_to_overwrite_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp))
            output = Path(temp) / "existing"
            output.mkdir()
            with self.assertRaises(FileExistsError):
                build_site_module.build_site(seed, output)


if __name__ == "__main__":
    unittest.main()

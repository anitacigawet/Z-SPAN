from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


KERNEL_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


create_seed_module = _load_module("respawn_create_seed", KERNEL_ROOT / "tools" / "create_seed.py")
validate_seed_module = _load_module("respawn_validate_seed", KERNEL_ROOT / "tools" / "validate_seed.py")


class RespawnKernelTests(unittest.TestCase):
    def _create(self, root: Path, *, locale: str = "en") -> Path:
        return create_seed_module.create_seed(
            root / "seed",
            country_name="Example Country",
            country_code="XX",
            project_name="Example Civic Library",
            primary_locale=locale,
            language_name="Example Language" if locale != "en" else None,
        )

    def test_all_contracts_are_valid_json(self) -> None:
        for path in sorted((KERNEL_ROOT / "contracts").glob("*.json")):
            with self.subTest(path=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(document.get("$schema"), "https://json-schema.org/draft/2020-12/schema")

    def test_generated_english_seed_is_structurally_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp))
            report = validate_seed_module.validate_seed(seed)
            self.assertEqual(report.errors, [])
            self.assertIn("country research is still pending", report.warnings)
            self.assertTrue((seed / "LICENSE").is_file())
            self.assertTrue((seed / "NOTICE").is_file())
            for path in seed.rglob("*"):
                if path.is_file():
                    self.assertNotIn("{{", path.read_text(encoding="utf-8"), path)

    def test_non_english_seed_gets_honest_draft_locale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp), locale="zh-CN")
            locale = json.loads((seed / "locales" / "zh-CN.json").read_text(encoding="utf-8"))
            self.assertEqual(locale["status"], "draft")
            self.assertTrue(locale["strings"]["project_description"].startswith("[Translation needed]"))
            report = validate_seed_module.validate_seed(seed)
            self.assertEqual(report.errors, [])
            self.assertIn("primary locale zh-CN is not yet reviewed", report.warnings)

    def test_generator_refuses_to_overwrite_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "seed"
            output.mkdir()
            with self.assertRaises(FileExistsError):
                create_seed_module.create_seed(
                    output,
                    country_name="Example Country",
                    country_code="XX",
                    project_name="Example Civic Library",
                    primary_locale="en",
                )

    def test_validator_rejects_country_veto_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp))
            profile_path = seed / "country" / "profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["operating_context"]["go_no_go"] = "no_go"
            profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
            report = validate_seed_module.validate_seed(seed)
            self.assertTrue(any("country-veto fields" in error for error in report.errors))

    def test_validator_rejects_jurisdiction_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp))
            profile_path = seed / "country" / "profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["jurisdiction_model"]["levels"].append({
                "key": "local_area",
                "terms": {"en": "Local area"},
                "parent_keys": ["country"],
                "may_hold_governing_bodies": True,
            })
            profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")

            jurisdictions_path = seed / "data" / "jurisdictions.json"
            jurisdictions = {
                "schema_version": 1,
                "jurisdictions": [
                    {
                        "id": "XX:a",
                        "parent_id": "XX:b",
                        "level_key": "local_area",
                        "names": {"en": "A"},
                        "governance_status": "confirmed",
                        "source_urls": ["https://example.org/a"],
                    },
                    {
                        "id": "XX:b",
                        "parent_id": "XX:a",
                        "level_key": "local_area",
                        "names": {"en": "B"},
                        "governance_status": "confirmed",
                        "source_urls": ["https://example.org/b"],
                    },
                ],
            }
            jurisdictions_path.write_text(json.dumps(jurisdictions, indent=2) + "\n", encoding="utf-8")
            report = validate_seed_module.validate_seed(seed)
            self.assertTrue(any("parent cycle" in error for error in report.errors))

    def test_validator_accepts_a_complete_reference_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            seed = self._create(Path(temp))
            profile_path = seed / "country" / "profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["jurisdiction_model"]["levels"].append({
                "key": "local_area",
                "terms": {"en": "Local area"},
                "parent_keys": ["country"],
                "may_hold_governing_bodies": True,
            })
            profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")

            documents = {
                "jurisdictions.json": {
                    "schema_version": 1,
                    "jurisdictions": [
                        {
                            "id": "XX:country",
                            "parent_id": None,
                            "level_key": "country",
                            "names": {"en": "Example Country"},
                            "governance_status": "confirmed",
                            "source_urls": ["https://example.org/country"],
                        },
                        {
                            "id": "XX:local.one",
                            "parent_id": "XX:country",
                            "level_key": "local_area",
                            "names": {"en": "Example Local Area"},
                            "governance_status": "confirmed",
                            "source_urls": ["https://example.org/local-area"],
                        },
                    ],
                },
                "governing-bodies.json": {
                    "schema_version": 1,
                    "governing_bodies": [{
                        "id": "XX:body:local.one",
                        "jurisdiction_id": "XX:local.one",
                        "body_type": "local_council",
                        "names": {"en": "Example Local Council"},
                        "source_urls": ["https://example.org/council"],
                        "status": "active",
                    }],
                },
                "sources.json": {
                    "schema_version": 1,
                    "sources": [{
                        "id": "XX:source:local.one.calendar",
                        "governing_body_id": "XX:body:local.one",
                        "kind": "calendar",
                        "public_url": "https://example.org/calendar",
                        "adapter_family": "reference_html",
                        "recipe_visibility": "sealed_local",
                        "status": "verified",
                        "last_verified_on": "2026-08-05",
                    }],
                },
                "meetings.json": {
                    "schema_version": 1,
                    "meetings": [{
                        "id": "XX:meeting:example-1",
                        "governing_body_id": "XX:body:local.one",
                        "names": {"en": "Regular meeting"},
                        "start": {
                            "date": "2026-08-05",
                            "time": "18:00:00",
                            "timezone": "Etc/UTC",
                            "precision": "second",
                        },
                        "lifecycle_status": "scheduled",
                        "location": "Public meeting room",
                        "artifacts": [{"kind": "detail", "url": "https://example.org/meeting/1", "language": "en"}],
                        "source_id": "XX:source:local.one.calendar",
                        "source_native_id": "1",
                        "retrieved_at": "2026-08-05T12:00:00+00:00",
                    }],
                },
            }
            for name, document in documents.items():
                (seed / "data" / name).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

            report = validate_seed_module.validate_seed(seed)
            self.assertEqual(report.errors, [])


if __name__ == "__main__":
    unittest.main()

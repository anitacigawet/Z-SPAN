from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


KERNEL_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = KERNEL_ROOT / "reference_adapters" / "zspan_arizona.py"


def _load_adapter():
    spec = importlib.util.spec_from_file_location("zspan_reference_adapter", ADAPTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {ADAPTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


adapter = _load_adapter()


class ZspanReferenceAdapterTests(unittest.TestCase):
    defaults = {
        "country_code": "US",
        "locale": "en-US",
        "timezone": "America/Phoenix",
        "governing_body_id": "US:body:az.kingman.council",
        "source_id": "US:source:az.kingman.calendar",
        "retrieved_at": "2026-08-05T12:00:00-07:00",
    }

    def _convert(self, rows):
        return adapter.convert_rows(rows, **self.defaults)

    def test_maps_timed_scheduled_row_and_artifacts(self) -> None:
        row = {
            "meeting_title": "Regular Council Meeting",
            "meeting_date": "2026-08-05",
            "meeting_time": "5:30 PM",
            "meeting_location": "Council Chambers",
            "meeting_status": "Scheduled",
            "agenda_url": "https://example.org/agenda",
            "minutes_url": "",
            "video_url": "https://example.org/video",
            "agenda_packet_url": "https://example.org/packet",
            "ecomment_url": "",
            "meeting_id": "12345",
        }
        meeting = self._convert([row])["meetings"][0]
        self.assertEqual(meeting["start"], {
            "date": "2026-08-05",
            "time": "17:30",
            "timezone": "America/Phoenix",
            "precision": "minute",
        })
        self.assertEqual(meeting["lifecycle_status"], "scheduled")
        self.assertEqual([item["kind"] for item in meeting["artifacts"]], ["agenda", "agenda_packet", "video"])
        self.assertEqual(meeting["source_native_id"], "12345")

    def test_preserves_date_when_time_is_absent(self) -> None:
        row = {
            "meeting_title": "Archived Meeting",
            "meeting_date": "2026-07-01",
            "meeting_time": "",
            "meeting_status": "Minutes Available",
            "minutes_url": "https://example.org/minutes",
            "meeting_id": "archive-1",
        }
        meeting = self._convert([row])["meetings"][0]
        self.assertEqual(meeting["start"], {
            "date": "2026-07-01",
            "time": None,
            "timezone": None,
            "precision": "date",
        })
        self.assertEqual(meeting["lifecycle_status"], "completed")

    def test_unknown_legacy_status_is_not_silently_reclassified(self) -> None:
        row = {
            "meeting_title": "Special Meeting",
            "meeting_date": "2026-08-07",
            "meeting_time": "",
            "meeting_status": "Vendor-specific status",
            "meeting_id": "special-1",
        }
        meeting = self._convert([row])["meetings"][0]
        self.assertEqual(meeting["lifecycle_status"], "unknown")
        self.assertIn("Vendor-specific status", meeting["notes"])

    def test_id_is_deterministic_without_native_id(self) -> None:
        row = {
            "meeting_title": "Budget Hearing",
            "meeting_date": "2026-09-01",
            "meeting_time": "6:00 PM",
        }
        first = self._convert([row])["meetings"][0]["id"]
        second = self._convert([row])["meetings"][0]["id"]
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("US:meeting:derived-"))

    def test_native_id_is_scoped_to_its_source(self) -> None:
        row = {
            "meeting_title": "Regular Meeting",
            "meeting_date": "2026-08-05",
            "meeting_time": "",
            "meeting_id": "12345",
        }
        first = self._convert([row])["meetings"][0]["id"]
        values = dict(self.defaults)
        values["source_id"] = "US:source:az.flagstaff.calendar"
        second = adapter.convert_rows([row], **values)["meetings"][0]["id"]
        self.assertNotEqual(first, second)

    def test_rejects_invalid_url_instead_of_emitting_it(self) -> None:
        row = {
            "meeting_title": "Regular Meeting",
            "meeting_date": "2026-08-05",
            "meeting_time": "",
            "agenda_url": "javascript:alert(1)",
        }
        with self.assertRaisesRegex(adapter.ConversionError, "agenda_url"):
            self._convert([row])

    def test_rejects_time_without_timezone(self) -> None:
        row = {
            "meeting_title": "Regular Meeting",
            "meeting_date": "2026-08-05",
            "meeting_time": "5:30 PM",
        }
        values = dict(self.defaults)
        values["timezone"] = None
        with self.assertRaisesRegex(adapter.ConversionError, "timezone is required"):
            adapter.convert_rows([row], **values)

    def test_rejects_duplicate_native_ids(self) -> None:
        row = {
            "meeting_title": "Regular Meeting",
            "meeting_date": "2026-08-05",
            "meeting_time": "",
            "meeting_id": "same-id",
        }
        with self.assertRaisesRegex(adapter.ConversionError, "duplicate derived meeting ID"):
            self._convert([row, dict(row)])

    def test_rejects_date_only_retrieval_timestamp(self) -> None:
        values = dict(self.defaults)
        values["retrieved_at"] = "2026-08-05"
        with self.assertRaisesRegex(adapter.ConversionError, "UTC offset or Z"):
            adapter.convert_rows([], **values)


if __name__ == "__main__":
    unittest.main()

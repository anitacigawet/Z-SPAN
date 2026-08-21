"""Unit tests for S-037 non-YouTube source resolution."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_PARSERS = _HERE.parent
if str(_PARSERS) not in sys.path:
    sys.path.insert(0, str(_PARSERS))

from scripts import transcribe_non_youtube as tny  # noqa: E402


class TestTranscriptionReadyUrl(unittest.TestCase):
    def test_vendor_wrappers_are_not_ready(self):
        wrappers = (
            "https://example.granicus.com/MediaPlayer.php?clip_id=1",
            "https://example.granicus.com/ViewPublisher.php?view_id=2",
            "https://example.legistar.com/Calendar.aspx",
            "https://example.granicus.com/ASX.php?view_id=2&clip_id=1",
        )
        for url in wrappers:
            with self.subTest(url=url):
                self.assertFalse(tny.is_transcription_ready_url(url))

    def test_media_suffix_in_query_does_not_make_wrapper_ready(self):
        self.assertFalse(
            tny.is_transcription_ready_url(
                "https://example.granicus.com/MediaPlayer.php?file=recording.mp4"
            )
        )

    def test_direct_mp4_is_ready(self):
        self.assertTrue(
            tny.is_transcription_ready_url(
                "https://archive-video.granicus.com/city/recording.MP4?token=abc"
            )
        )

    def test_youtube_url_is_ready(self):
        self.assertTrue(
            tny.is_transcription_ready_url(
                "https://www.youtube.com/watch?v=abcdefghijk"
            )
        )


class TestBullheadResolution(unittest.TestCase):
    @mock.patch.object(tny, "_fetch_bullhead_archive_index")
    def test_numeric_external_meeting_id_resolves_clip(self, mock_archive):
        mp4_url = (
            "https://archive-video.granicus.com/bullheadcity/"
            "bullheadcity_b386526a-1234.mp4"
        )
        mock_archive.return_value = {"1968": mp4_url}
        meeting = {
            "id": 42,
            "city_name": "Bullhead City",
            "meeting_date": "2026-07-14",
            "meeting_title": "City Council",
            "meeting_id": "1968",
            "video_url": (
                "https://bullheadcity.granicus.com/"
                "ViewPublisher.php?view_id=2"
            ),
            "agenda_url": "",
        }

        resolved = tny.resolve_bullhead_city(meeting)

        self.assertEqual(resolved.source_url, mp4_url)
        self.assertEqual(resolved.source_kind, "granicus_direct_mp4")
        mock_archive.assert_called_once_with()


class TestLakeHavasuResolution(unittest.TestCase):
    def setUp(self):
        tny._LH_ARCHIVE_CACHE.clear()
        tny._LH_ASX_CACHE.clear()

    def test_view_id_parsed_only_from_exact_lake_havasu_wrapper(self):
        valid = (
            "https://lakehavasucity.granicus.com/"
            "ViewPublisher.php?view_id=5"
        )
        self.assertEqual(tny._extract_lh_view_id(valid), "5")
        self.assertIsNone(
            tny._extract_lh_view_id(
                "https://evil.example/ViewPublisher.php?view_id=5"
            )
        )
        self.assertIsNone(
            tny._extract_lh_view_id(
                "https://lakehavasucity.granicus.com/MediaPlayer.php?view_id=5"
            )
        )

    @mock.patch.object(tny, "_fetch_text_bounded")
    def test_archive_cache_is_keyed_by_view_id(self, mock_fetch):
        mock_fetch.side_effect = (
            """
            <table id="archive"><tr>
              <td>Board of Adjustment on 2026-07-08 9:00 AM - Regular</td>
              <td><a href="AgendaViewer.php?clip_id=1805">Agenda</a></td>
            </tr></table>
            """,
            """
            <table id="archive"><tr>
              <td>City Council on 2026-07-14 6:00 PM - Regular</td>
              <td><a href="AgendaViewer.php?clip_id=1823">Agenda</a></td>
            </tr></table>
            """,
        )

        view_five = tny._fetch_lh_archive_index("5")
        view_two = tny._fetch_lh_archive_index("2")
        cached_view_five = tny._fetch_lh_archive_index("5")

        self.assertEqual(view_five[0]["view_id"], "5")
        self.assertEqual(view_two[0]["view_id"], "2")
        self.assertIs(cached_view_five, view_five)
        self.assertEqual(mock_fetch.call_count, 2)

    @mock.patch.object(tny, "_resolve_lh_mp4_url_from_asx")
    @mock.patch.object(tny, "_fetch_lh_archive_index")
    def test_wrapper_view_is_tried_first_and_used_for_asx(
        self,
        mock_archive,
        mock_asx,
    ):
        mock_archive.return_value = [{
            "date_iso": "2026-07-08",
            "body": "Board of Adjustment",
            "clip_id": "1805",
            "raw_header": "Board of Adjustment on 2026-07-08 9:00 AM",
            "view_id": "5",
        }]
        mp4_url = (
            "https://archive-video.granicus.com/lakehavasucity/"
            "lakehavasucity_1234abcd.mp4"
        )
        mock_asx.return_value = mp4_url
        meeting = {
            "id": 84,
            "city_name": "Lake Havasu City",
            "meeting_date": "2026-07-08",
            "meeting_title": "Board of Adjustment",
            "meeting_id": "",
            "video_url": (
                "https://lakehavasucity.granicus.com/"
                "ViewPublisher.php?view_id=5"
            ),
            "agenda_url": "",
        }

        resolved = tny.resolve_lake_havasu_city(meeting)

        self.assertEqual(resolved.source_url, mp4_url)
        mock_archive.assert_called_once_with("5")
        mock_asx.assert_called_once_with("1805", "5")

    @mock.patch.object(tny, "_resolve_lh_mp4_url_from_asx")
    @mock.patch.object(tny, "_fetch_lh_archive_index")
    def test_default_view_two_is_fallback_and_used_for_asx(
        self,
        mock_archive,
        mock_asx,
    ):
        mock_archive.side_effect = [[], [{
            "date_iso": "2026-07-14",
            "body": "City Council",
            "clip_id": "1823",
            "raw_header": "City Council on 2026-07-14 6:00 PM",
            "view_id": "2",
        }]]
        mock_asx.return_value = (
            "https://archive-video.granicus.com/lakehavasucity/"
            "lakehavasucity_abcd1234.mp4"
        )
        meeting = {
            "id": 85,
            "city_name": "Lake Havasu City",
            "meeting_date": "2026-07-14",
            "meeting_title": "City Council",
            "meeting_id": "",
            "video_url": (
                "https://lakehavasucity.granicus.com/"
                "ViewPublisher.php?view_id=5"
            ),
            "agenda_url": "",
        }

        resolved = tny.resolve_lake_havasu_city(meeting)

        self.assertEqual(resolved.source_kind, "granicus_direct_mp4")
        self.assertEqual(
            mock_archive.call_args_list,
            [mock.call("5"), mock.call("2")],
        )
        mock_asx.assert_called_once_with("1823", "2")


class TestWorkOrderAdapter(unittest.TestCase):
    def test_external_id_and_work_order_video_are_preserved(self):
        row = tny.wo_to_meeting_row({
            "meeting_id": 99,
            "meeting_external_id": "1968",
            "city_name": "Bullhead City",
            "youtube_video_url": "https://vendor.example/wrapper",
            "meeting_video_url": "https://vendor.example/other-wrapper",
        })

        self.assertEqual(row["id"], 99)
        self.assertEqual(row["meeting_id"], "1968")
        self.assertEqual(row["video_url"], "https://vendor.example/wrapper")


if __name__ == "__main__":
    unittest.main()

"""Recon-3 — tests for fetch_youtube_channel_videos.py.

All YouTube Data API calls are mocked. Tests cover the output JSON shape,
description truncation, error paths (channel resolution failures), and
CLI flag handling.

Run via:
    python3.11 test_fetch_youtube_channel_videos.py
or:
    python3.11 -m unittest scripts.test_fetch_youtube_channel_videos
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_PARSERS = _HERE.parent
if str(_PARSERS) not in sys.path:
    sys.path.insert(0, str(_PARSERS))

from scripts import fetch_youtube_channel_videos as fyt  # type: ignore  # noqa: E402
from youtube_data_api import Video, YouTubeDataApiError  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_channel_resource(channel_id: str, title: str, handle: str = "") -> dict:
    return {
        "id": channel_id,
        "snippet": {"title": title, "customUrl": handle},
        "contentDetails": {"relatedPlaylists": {"uploads": f"UU{channel_id[2:]}"}},
    }


def _fake_videos(n: int, prefix: str = "City Council Meeting") -> list[Video]:
    return [
        Video(
            video_id=f"vid{i:03d}",
            url=f"https://www.youtube.com/watch?v=vid{i:03d}",
            title=f"{prefix} - {2026 - i//50}-{((i%12)+1):02d}-{((i%28)+1):02d}",
            upload_date=date(2026, 1, 1),
            description=(
                "Live coverage of the regular council session. Topics: "
                "ordinances, budget items, public comment. Hosted by the City of "
                "Example, AZ. Subscribe for monthly updates and watch live."
            ),
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


class TestFetchOutputShape(unittest.TestCase):
    @mock.patch("scripts.fetch_youtube_channel_videos.list_channel_videos")
    @mock.patch("scripts.fetch_youtube_channel_videos.resolve_channel")
    def test_output_has_expected_top_level_keys(self, mock_resolve, mock_list):
        mock_resolve.return_value = _fake_channel_resource(
            "UC1234567890123456789012", "City of Example, AZ", "@cityofexample"
        )
        mock_list.return_value = _fake_videos(10)
        result = fyt.fetch_for_verification(
            "https://www.youtube.com/@cityofexample", api_key="FAKE_KEY"
        )
        for k in ("channel", "videos", "count", "channel_url_input"):
            self.assertIn(k, result)

    @mock.patch("scripts.fetch_youtube_channel_videos.list_channel_videos")
    @mock.patch("scripts.fetch_youtube_channel_videos.resolve_channel")
    def test_channel_subdict_has_expected_keys(self, mock_resolve, mock_list):
        mock_resolve.return_value = _fake_channel_resource(
            "UC1234567890123456789012", "City of Example, AZ", "@cityofexample"
        )
        mock_list.return_value = _fake_videos(3)
        result = fyt.fetch_for_verification(
            "https://www.youtube.com/@cityofexample", api_key="FAKE_KEY"
        )
        for k in ("url", "channel_id", "title", "handle"):
            self.assertIn(k, result["channel"])
        self.assertEqual(result["channel"]["title"], "City of Example, AZ")
        self.assertEqual(result["channel"]["handle"], "@cityofexample")

    @mock.patch("scripts.fetch_youtube_channel_videos.list_channel_videos")
    @mock.patch("scripts.fetch_youtube_channel_videos.resolve_channel")
    def test_video_entries_have_expected_keys(self, mock_resolve, mock_list):
        mock_resolve.return_value = _fake_channel_resource("UC" + "x" * 22, "T", "@h")
        mock_list.return_value = _fake_videos(5)
        result = fyt.fetch_for_verification(
            "https://www.youtube.com/@h", api_key="FAKE_KEY"
        )
        for v in result["videos"]:
            for k in ("title", "description", "upload_date", "url"):
                self.assertIn(k, v)
            # ISO date format on the way out (not a datetime object).
            self.assertRegex(v["upload_date"], r"^\d{4}-\d{2}-\d{2}$")

    @mock.patch("scripts.fetch_youtube_channel_videos.list_channel_videos")
    @mock.patch("scripts.fetch_youtube_channel_videos.resolve_channel")
    def test_count_matches_videos_list_length(self, mock_resolve, mock_list):
        mock_resolve.return_value = _fake_channel_resource("UC" + "x" * 22, "T", "@h")
        mock_list.return_value = _fake_videos(7)
        result = fyt.fetch_for_verification(
            "https://www.youtube.com/@h", api_key="FAKE_KEY"
        )
        self.assertEqual(result["count"], 7)
        self.assertEqual(len(result["videos"]), 7)


# ---------------------------------------------------------------------------
# Description truncation
# ---------------------------------------------------------------------------


class TestDescriptionTruncation(unittest.TestCase):
    def test_short_description_passes_through(self):
        self.assertEqual(fyt._truncate("hello world", 280), "hello world")

    def test_long_description_truncated_at_word_boundary(self):
        s = "word " * 200  # 1000 chars
        out = fyt._truncate(s, 100)
        self.assertLessEqual(len(out), 105)  # ellipsis + slack
        self.assertTrue(out.endswith("…"))
        # Should not end mid-word (last char before … should be word char OR space-stripped).
        self.assertNotIn("…", out[:-1])

    def test_empty_description_returns_empty(self):
        self.assertEqual(fyt._truncate("", 280), "")
        self.assertEqual(fyt._truncate("   ", 280), "")

    def test_truncation_at_no_word_boundary_falls_back_to_hard_cut(self):
        s = "a" * 500  # no spaces
        out = fyt._truncate(s, 50)
        self.assertLessEqual(len(out), 52)  # 50 chars + ellipsis
        self.assertTrue(out.endswith("…"))

    @mock.patch("scripts.fetch_youtube_channel_videos.list_channel_videos")
    @mock.patch("scripts.fetch_youtube_channel_videos.resolve_channel")
    def test_descriptions_in_output_respect_max_chars_arg(self, mock_resolve, mock_list):
        mock_resolve.return_value = _fake_channel_resource("UC" + "x" * 22, "T", "@h")
        mock_list.return_value = _fake_videos(3)
        result = fyt.fetch_for_verification(
            "https://www.youtube.com/@h",
            api_key="FAKE_KEY",
            max_description_chars=50,
        )
        for v in result["videos"]:
            self.assertLessEqual(len(v["description"]), 55)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths(unittest.TestCase):
    @mock.patch("scripts.fetch_youtube_channel_videos.resolve_channel")
    def test_resolve_channel_error_propagates(self, mock_resolve):
        mock_resolve.side_effect = YouTubeDataApiError("channel not found")
        with self.assertRaises(YouTubeDataApiError):
            fyt.fetch_for_verification(
                "https://www.youtube.com/@nope", api_key="FAKE_KEY"
            )

    @mock.patch("scripts.fetch_youtube_channel_videos.get_youtube_data_api_key")
    def test_missing_api_key_raises(self, mock_get_key):
        mock_get_key.return_value = ""
        with self.assertRaises(YouTubeDataApiError):
            fyt.fetch_for_verification("https://www.youtube.com/@x")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI(unittest.TestCase):
    @mock.patch("scripts.fetch_youtube_channel_videos.list_channel_videos")
    @mock.patch("scripts.fetch_youtube_channel_videos.resolve_channel")
    @mock.patch("scripts.fetch_youtube_channel_videos.get_youtube_data_api_key")
    def test_cli_json_output_is_valid_json(self, mock_key, mock_resolve, mock_list):
        mock_key.return_value = "FAKE_KEY"
        mock_resolve.return_value = _fake_channel_resource(
            "UC" + "x" * 22, "City of Example", "@example"
        )
        mock_list.return_value = _fake_videos(5)

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = fyt.main(
                ["--channel-url", "https://www.youtube.com/@example", "--json"]
            )
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["count"], 5)
        self.assertEqual(parsed["channel"]["handle"], "@example")

    @mock.patch("scripts.fetch_youtube_channel_videos.list_channel_videos")
    @mock.patch("scripts.fetch_youtube_channel_videos.resolve_channel")
    @mock.patch("scripts.fetch_youtube_channel_videos.get_youtube_data_api_key")
    def test_cli_resolve_error_returns_json_error_in_json_mode(
        self, mock_key, mock_resolve, mock_list
    ):
        mock_key.return_value = "FAKE_KEY"
        mock_resolve.side_effect = YouTubeDataApiError("channel not found")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = fyt.main(
                ["--channel-url", "https://www.youtube.com/@nope", "--json"]
            )
        self.assertEqual(rc, 1)
        parsed = json.loads(buf.getvalue())
        self.assertIn("fetch_error", parsed)
        self.assertEqual(parsed["count"], 0)

    @mock.patch("scripts.fetch_youtube_channel_videos.list_channel_videos")
    @mock.patch("scripts.fetch_youtube_channel_videos.resolve_channel")
    @mock.patch("scripts.fetch_youtube_channel_videos.get_youtube_data_api_key")
    def test_cli_max_videos_flag_propagates(self, mock_key, mock_resolve, mock_list):
        mock_key.return_value = "FAKE_KEY"
        mock_resolve.return_value = _fake_channel_resource("UC" + "x" * 22, "T", "@h")
        mock_list.return_value = _fake_videos(7)

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = fyt.main(
                [
                    "--channel-url",
                    "https://www.youtube.com/@h",
                    "--max-videos",
                    "7",
                    "--json",
                ]
            )
        self.assertEqual(rc, 0)
        # list_channel_videos was called with max_videos=7.
        mock_list.assert_called_once()
        _, kwargs = mock_list.call_args
        self.assertEqual(kwargs.get("max_videos"), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)

from __future__ import annotations

import asyncio
from unittest import mock

from zspan_pipeline import fetcher


def _existing_transcript() -> dict:
    return {
        "meeting_id": 101,
        "output_type": "transcript_words",
        "generated_at": "2026-07-20 12:00:00",
        "content": "{\"words\": [{\"word\": \"frozen\"}]}",
    }


def test_existing_transcript_is_not_overwritten():
    with (
        mock.patch.dict(
            "os.environ", {"ZSPAN_FORCE_TRANSCRIPT_OVERWRITE": ""}, clear=False,
        ),
        mock.patch.object(
            fetcher, "is_output_already_present", return_value=_existing_transcript(),
        ),
        mock.patch.object(
            fetcher, "_fetch_transcript_words", new=mock.AsyncMock(),
        ) as transcribe,
    ):
        result = asyncio.run(
            fetcher.fetch_one_output(101, "notebook", "transcript_words")
        )

    assert result["status"] == "skipped_existing"
    transcribe.assert_not_awaited()


def test_force_regenerate_does_not_bypass_transcript_guard():
    with (
        mock.patch.dict(
            "os.environ", {
                "ZSPAN_FORCE_REGENERATE": "1",
                "ZSPAN_FORCE_TRANSCRIPT_OVERWRITE": "",
            }, clear=False,
        ),
        mock.patch.object(fetcher, "FORCE_REGENERATE", True),
        mock.patch.object(
            fetcher, "is_output_already_present", return_value=_existing_transcript(),
        ),
        mock.patch.object(
            fetcher, "_fetch_transcript_words", new=mock.AsyncMock(),
        ) as transcribe,
    ):
        result = asyncio.run(
            fetcher.fetch_one_output(101, "notebook", "transcript_words")
        )

    assert result["status"] == "skipped_existing"
    transcribe.assert_not_awaited()


def test_transcript_specific_force_flag_is_the_only_bypass():
    expected = {"output_type": "transcript_words", "status": "ok"}
    with (
        mock.patch.dict(
            "os.environ", {"ZSPAN_FORCE_TRANSCRIPT_OVERWRITE": "1"}, clear=False,
        ),
        mock.patch.object(
            fetcher, "is_output_already_present", return_value=_existing_transcript(),
        ),
        mock.patch.object(
            fetcher, "_fetch_transcript_words",
            new=mock.AsyncMock(return_value=expected),
        ) as transcribe,
    ):
        result = asyncio.run(
            fetcher.fetch_one_output(101, "notebook", "transcript_words")
        )

    assert result == expected
    transcribe.assert_awaited_once_with(101, "notebook", "transcript_words")


def test_direct_transcript_strategy_call_is_also_guarded():
    with (
        mock.patch.dict(
            "os.environ", {"ZSPAN_FORCE_TRANSCRIPT_OVERWRITE": ""}, clear=False,
        ),
        mock.patch.object(
            fetcher, "is_output_already_present", return_value=_existing_transcript(),
        ),
        mock.patch.object(fetcher, "get_resolved_video_url") as resolve_video,
    ):
        result = asyncio.run(
            fetcher._fetch_transcript_words(101, "notebook", "transcript_words")
        )

    assert result["status"] == "skipped_existing"
    resolve_video.assert_not_called()

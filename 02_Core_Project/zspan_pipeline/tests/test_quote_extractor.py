"""Tests for complete-evidence quote extraction and tag normalization.

Session-106 hardening: LLM output can emit topic_tags outside the
controlled 5-tag vocabulary. Those must be dropped (per topic_tags
normalize_tags semantics) so the downstream topic-follow join doesn't
silently drop legitimate matches on foreign tag_ids.
"""

from types import SimpleNamespace
from unittest import mock

from zspan_pipeline import qdrant_quote_extractor as extractor

ExtractedQuote = extractor.ExtractedQuote


def _base_dict(**overrides):
    d = {
        "speaker_name": "Mayor Test",
        "speaker_role": "Mayor",
        "speaker_class": "council_member",
        "quote_text": "Sample quote text.",
        "topic_tags": [],
        "video_timestamp_seconds": 42,
        "chunk_index": 1,
    }
    d.update(overrides)
    return d


def test_from_dict_keeps_vocab_tags():
    quote = ExtractedQuote.from_dict(_base_dict(topic_tags=["data_centers", "water_rights"]))
    assert quote.topic_tags == ["data_centers", "water_rights"]


def test_from_dict_drops_out_of_vocab_tags():
    quote = ExtractedQuote.from_dict(
        _base_dict(topic_tags=["housing", "budget", "data_centers"])
    )
    assert quote.topic_tags == ["data_centers"]


def test_from_dict_falls_back_to_other_when_all_dropped():
    quote = ExtractedQuote.from_dict(_base_dict(topic_tags=["housing", "budget"]))
    assert quote.topic_tags == ["other"]


def test_from_dict_falls_back_to_other_when_empty():
    quote = ExtractedQuote.from_dict(_base_dict(topic_tags=[]))
    assert quote.topic_tags == ["other"]


def test_from_dict_falls_back_to_other_when_missing():
    d = _base_dict()
    del d["topic_tags"]
    quote = ExtractedQuote.from_dict(d)
    assert quote.topic_tags == ["other"]


def test_from_dict_keeps_other_when_llm_emits_it_directly():
    quote = ExtractedQuote.from_dict(_base_dict(topic_tags=["other"]))
    assert quote.topic_tags == ["other"]


def test_from_dict_filters_non_string_entries():
    quote = ExtractedQuote.from_dict(
        _base_dict(topic_tags=["data_centers", None, 42, {"nested": True}])
    )
    assert quote.topic_tags == ["data_centers"]


def test_complete_chunks_are_batched_once_in_chronological_order():
    chunks = [
        SimpleNamespace(chunk_index=index, body=str(index), start_seconds=float(index))
        for index in (3, 1, 2, 0)
    ]
    generation = extractor.qdrant_synthesizer.GenerationResult(
        content='{"quotes": []}',
        model_id=extractor.qdrant_synthesizer.FLAGSHIP_MODEL_ID,
        attempts=(),
    )
    seen_batches: list[list[int]] = []

    def fake_batch(**kwargs):
        seen_batches.append([chunk.chunk_index for chunk in kwargs["chunks"]])
        return [], generation

    with (
        mock.patch.object(extractor, "_load_extraction_prompt", return_value="prompt"),
        mock.patch.object(
            extractor.qdrant_synthesizer,
            "load_complete_meeting_chunks",
            return_value=chunks,
        ) as load_complete,
        mock.patch.object(
            extractor,
            "extract_quotes_from_batch",
            side_effect=fake_batch,
        ),
    ):
        extractor.extract_quotes_for_meeting(
            meeting_id=42,
            city_name="Test City",
            symbols_block="symbols",
            canonical_roster="roster",
            chunks_per_batch=2,
        )

    load_complete.assert_called_once_with(42)
    assert seen_batches == [[0, 1], [2, 3]]

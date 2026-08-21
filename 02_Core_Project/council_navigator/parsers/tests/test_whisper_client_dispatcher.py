"""Unit tests for the whisper_client dispatcher swap (commit d62cdb1).

The 2026-05-31 swap renamed the existing `transcribe_youtube` to
`_transcribe_youtube_via_openai` (private), added `transcribe_youtube_via_mac_node`,
and gave the public `transcribe_youtube` name to a DISPATCHER that routes
between them based on `user_settings.zspan_whisper_provider`.

These tests lock the routing behavior without making any real network calls:
  * Provider resolution from user_settings (default openai, mac_node when set)
  * Unknown provider values fall back to openai
  * Dispatcher routes correctly per provider
  * Mac-node errors do NOT silently fall through to OpenAI by default
  * Fallback override (zspan_whisper_fallback_to_openai=true) does cause fallthrough
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

import whisper_client as wc  # noqa: E402


# ── _resolve_whisper_provider ────────────────────────────────────


def test_resolve_provider_defaults_to_openai_when_unset():
    """Behavior pre-swap is preserved: if zspan_whisper_provider isn't
    set, callers get the original OpenAI path. Critical — the swap
    landed in production and shouldn't have changed behavior until
    James explicitly flipped the config.
    """
    with patch.object(wc, "load_user_settings", return_value={}):
        assert wc._resolve_whisper_provider() == "openai"


def test_resolve_provider_returns_mac_node_when_set():
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "mac_node"},
    ):
        assert wc._resolve_whisper_provider() == "mac_node"


def test_resolve_provider_returns_openai_when_explicitly_set():
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "openai"},
    ):
        assert wc._resolve_whisper_provider() == "openai"


def test_resolve_provider_returns_assemblyai_when_set():
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "assemblyai"},
    ):
        assert wc._resolve_whisper_provider() == "assemblyai"


def test_resolve_provider_falls_back_to_openai_for_unknown_value():
    """If someone typos the config (e.g., 'macnode' or 'OpenAI' with
    wrong case), don't crash — fall back to the safe default (openai)
    + log a warning. Better than 500'ing every call.
    """
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "definitely-not-real"},
    ):
        assert wc._resolve_whisper_provider() == "openai"


def test_resolve_provider_strips_whitespace_and_lowercases():
    """Be lenient with operator input — '  Mac_Node  ' should map to mac_node."""
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "  Mac_Node  "},
    ):
        assert wc._resolve_whisper_provider() == "mac_node"


# ── Dispatcher routing ───────────────────────────────────────────


def test_dispatch_to_openai_by_default(tmp_path):
    """transcribe_youtube with no zspan_whisper_provider set → routes
    to _transcribe_youtube_via_openai with all the same args.
    """
    sentinel = {"words": [], "duration_seconds": 0.0, "language": "en", "_via": "openai"}
    with patch.object(wc, "load_user_settings", return_value={}), \
         patch.object(wc, "_transcribe_youtube_via_openai", return_value=sentinel) as openai_mock, \
         patch.object(wc, "transcribe_youtube_via_mac_node") as mac_mock:
        result = wc.transcribe_youtube(
            "https://www.youtube.com/watch?v=test",
            tmp_path,
            False,
            prompt="vocabulary hints",
        )
    openai_mock.assert_called_once()
    mac_mock.assert_not_called()
    assert result is sentinel


def test_dispatch_to_mac_node_when_provider_set(tmp_path):
    """transcribe_youtube with zspan_whisper_provider='mac_node' →
    routes to transcribe_youtube_via_mac_node.
    """
    sentinel = {"words": [{"word": "hi", "start": 0.0, "end": 0.1}],
                "duration_seconds": 0.1, "language": "en", "_via": "mac"}
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "mac_node"},
    ), patch.object(
        wc, "transcribe_youtube_via_mac_node", return_value=sentinel,
    ) as mac_mock, patch.object(
        wc, "_transcribe_youtube_via_openai",
    ) as openai_mock:
        result = wc.transcribe_youtube(
            "https://www.youtube.com/watch?v=test",
            tmp_path,
            False,
            prompt=None,
        )
    mac_mock.assert_called_once()
    openai_mock.assert_not_called()
    assert result is sentinel


def test_dispatch_to_assemblyai_when_provider_set(tmp_path):
    sentinel = {
        "words": [{"word": "hello", "start": 0.0, "end": 0.2,
                   "speaker_id": "A"}],
        "provider": "assemblyai",
        "model": "universal-3-5-pro",
    }
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "assemblyai"},
    ), patch.object(
        wc, "transcribe_youtube_via_assemblyai", return_value=sentinel,
    ) as assemblyai_mock, patch.object(
        wc, "_transcribe_youtube_via_openai",
    ) as openai_mock, patch.object(
        wc, "transcribe_youtube_via_mac_node",
    ) as mac_mock:
        result = wc.transcribe_youtube(
            "https://www.youtube.com/watch?v=test",
            tmp_path,
            False,
            prompt="not sent to AssemblyAI",
        )

    assemblyai_mock.assert_called_once_with(
        "https://www.youtube.com/watch?v=test", tmp_path, False,
    )
    openai_mock.assert_not_called()
    mac_mock.assert_not_called()
    assert result is sentinel


# ── AssemblyAI adapter + REST path ──────────────────────────────


def test_assemblyai_adapter_converts_native_word_fields():
    result = wc._adapt_assemblyai_transcript({
        "status": "completed",
        "speech_model": None,
        "speech_models": ["universal-3-5-pro", "universal-2"],
        "acoustic_model": "assemblyai_default",
        "language_model": "assemblyai_default",
        "audio_duration": 2.5,
        "language_code": "en",
        "words": [
            {
                "text": "Hello",
                "start": 1250,
                "end": 1750,
                "speaker": "A",
                "confidence": 0.99,
            },
            {
                "text": "there",
                "start": 1800,
                "end": 2250,
                "speaker": None,
                "confidence": 0.98,
            },
        ],
    })

    assert result == {
        "words": [
            {"word": "Hello", "start": 1.25, "end": 1.75,
             "speaker_id": "A"},
            {"word": "there", "start": 1.8, "end": 2.25,
             "speaker_id": ""},
        ],
        "duration_seconds": 2.5,
        "language": "en",
        "provider": "assemblyai",
        "model": "universal-3-5-pro",
    }


def test_assemblyai_adapter_resolves_plural_speech_models_when_singular_is_none():
    result = wc._adapt_assemblyai_transcript({
        "status": "completed",
        "speech_model": None,
        "speech_models": ["universal-3-5-pro", "universal-2"],
        "acoustic_model": "assemblyai_default",
        "language_model": "assemblyai_default",
        "audio_duration": 780,
        "words": [
            {"text": "Welcome", "start": 0, "end": 500, "speaker": "A"},
        ],
    })

    assert result["model"] == "universal-3-5-pro"
    assert result["words"] == [
        {"word": "Welcome", "start": 0.0, "end": 0.5, "speaker_id": "A"},
    ]


@pytest.mark.parametrize(("model_metadata", "expected_model"), [
    ({"speech_model": "universal-3-5-pro",
      "speech_models": ["universal-2"],
      "acoustic_model": "assemblyai_default"}, "universal-3-5-pro"),
    ({"speech_model": None,
      "speech_models": ["", "universal-2"],
      "acoustic_model": "assemblyai_default"}, "universal-2"),
    ({"speech_model": None,
      "speech_models": [],
      "acoustic_model": "assemblyai_default"}, "assemblyai_default"),
    ({}, "unknown"),
])
def test_assemblyai_adapter_model_provenance_is_best_effort(
    model_metadata, expected_model,
):
    payload = {
        "status": "completed",
        "words": [
            {"text": "Welcome", "start": 0, "end": 500, "speaker": "A"},
        ],
        **model_metadata,
    }

    result = wc._adapt_assemblyai_transcript(payload)

    assert result["model"] == expected_model


@pytest.mark.parametrize("payload", [
    {"status": "error", "error": "audio could not be decoded"},
    {
        "status": "completed",
        "speech_model": None,
        "speech_models": ["universal-3-5-pro", "universal-2"],
        "words": [],
    },
    {
        "status": "completed",
        "speech_model": None,
        "speech_models": ["universal-3-5-pro", "universal-2"],
        "words": [{"text": "broken", "start": 0, "speaker": "A"}],
    },
])
def test_assemblyai_adapter_raises_on_failed_empty_or_malformed_payload(payload):
    with pytest.raises(wc.AssemblyAIError):
        wc._adapt_assemblyai_transcript(payload)


def test_assemblyai_rest_flow_uses_raw_auth_and_adapts_response(tmp_path):
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"mock audio")
    downloaded = wc.DownloadedAudio(
        path=audio_path, duration_seconds=1.0, title="Meeting",
    )

    upload_response = MagicMock(status_code=200, text="")
    upload_response.json.return_value = {
        "upload_url": "https://cdn.assemblyai.com/upload/mock",
    }
    submit_response = MagicMock(status_code=200, text="")
    submit_response.json.return_value = {"id": "transcript-123"}
    completed_response = MagicMock(status_code=200, text="")
    completed_response.json.return_value = {
        "status": "completed",
        "speech_model": None,
        "speech_models": ["universal-3-5-pro", "universal-2"],
        "acoustic_model": "assemblyai_default",
        "language_model": "assemblyai_default",
        "audio_duration": 1.0,
        "language_code": "en",
        "words": [
            {"text": "Ready", "speaker": "B", "start": 0, "end": 500},
        ],
    }

    with patch.object(wc, "_resolve_assemblyai_key", return_value="raw-key"), \
         patch.object(wc, "download_youtube_audio", return_value=downloaded), \
         patch.object(
             wc.requests, "post", side_effect=[upload_response, submit_response],
         ) as post_mock, patch.object(
             wc.requests, "get", return_value=completed_response,
         ) as get_mock:
        result = wc.transcribe_youtube_via_assemblyai(
            "https://www.youtube.com/watch?v=test", tmp_path, keep_audio=True,
        )

    upload_call, submit_call = post_mock.call_args_list
    assert upload_call.args[0] == "https://api.assemblyai.com/v2/upload"
    assert upload_call.kwargs["headers"]["Authorization"] == "raw-key"
    assert upload_call.kwargs["headers"]["Content-Type"] == "application/octet-stream"
    assert submit_call.args[0] == "https://api.assemblyai.com/v2/transcript"
    assert submit_call.kwargs["headers"]["Authorization"] == "raw-key"
    assert submit_call.kwargs["json"] == {
        "audio_url": "https://cdn.assemblyai.com/upload/mock",
        "speech_models": ["universal-3-5-pro", "universal-2"],
        "speaker_labels": True,
    }
    assert get_mock.call_args.args[0] == (
        "https://api.assemblyai.com/v2/transcript/transcript-123"
    )
    assert get_mock.call_args.kwargs["headers"]["Authorization"] == "raw-key"
    assert result["words"] == [
        {"word": "Ready", "start": 0.0, "end": 0.5, "speaker_id": "B"},
    ]
    assert result["provider"] == "assemblyai"
    assert result["model"] == "universal-3-5-pro"


def test_dispatch_passes_prompt_to_mac_node(tmp_path):
    """The Mac path accepts prompt= for whisper.cpp / faster-whisper
    initial_prompt vocabulary biasing. Dispatcher must forward it.
    """
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "mac_node"},
    ), patch.object(
        wc, "transcribe_youtube_via_mac_node", return_value={"words": []},
    ) as mac_mock:
        wc.transcribe_youtube(
            "https://www.youtube.com/watch?v=test",
            tmp_path,
            False,
            prompt="vocab hint",
        )
    _, kwargs = mac_mock.call_args
    assert kwargs.get("prompt") == "vocab hint"


# ── Strict-by-default error propagation ──────────────────────────


def test_mac_node_error_does_not_silently_fallback_by_default(tmp_path):
    """If Mac node errors out and zspan_whisper_fallback_to_openai is
    NOT set, the WhisperNodeError must propagate to the caller.
    Operator should notice the Mac failure rather than silently spending
    paid Whisper.
    """
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "mac_node"},
    ), patch.object(
        wc, "transcribe_youtube_via_mac_node",
        side_effect=wc.WhisperNodeError("mac unreachable"),
    ), patch.object(
        wc, "_transcribe_youtube_via_openai",
    ) as openai_mock:
        with pytest.raises(wc.WhisperNodeError):
            wc.transcribe_youtube(
                "https://www.youtube.com/watch?v=test",
                tmp_path,
                False,
            )
    openai_mock.assert_not_called()


def test_mac_node_config_error_does_not_silently_fallback_by_default(tmp_path):
    """Same as above but for WhisperConfigError (e.g., STATUS.json says
    up=false) — should propagate, not fall through.
    """
    with patch.object(
        wc, "load_user_settings",
        return_value={"zspan_whisper_provider": "mac_node"},
    ), patch.object(
        wc, "transcribe_youtube_via_mac_node",
        side_effect=wc.WhisperConfigError("status up=false"),
    ), patch.object(
        wc, "_transcribe_youtube_via_openai",
    ) as openai_mock:
        with pytest.raises(wc.WhisperConfigError):
            wc.transcribe_youtube(
                "https://www.youtube.com/watch?v=test",
                tmp_path,
                False,
            )
    openai_mock.assert_not_called()


# ── Opt-in fallback ──────────────────────────────────────────────


def test_mac_node_error_falls_through_when_fallback_opt_in_set(tmp_path):
    """When zspan_whisper_fallback_to_openai=true, Mac node errors are
    caught + the call falls through to OpenAI. Useful for unattended
    pipelines where availability > strict-Mac-only.
    """
    sentinel = {"words": [], "_via": "openai_fallback"}
    settings = {
        "zspan_whisper_provider": "mac_node",
        "zspan_whisper_fallback_to_openai": True,
    }
    with patch.object(wc, "load_user_settings", return_value=settings), \
         patch.object(
             wc, "transcribe_youtube_via_mac_node",
             side_effect=wc.WhisperNodeError("mac down"),
         ), patch.object(
             wc, "_transcribe_youtube_via_openai", return_value=sentinel,
         ) as openai_mock:
        result = wc.transcribe_youtube(
            "https://www.youtube.com/watch?v=test",
            tmp_path,
            False,
        )
    openai_mock.assert_called_once()
    assert result is sentinel


def test_fallback_does_not_kick_in_when_fallback_setting_false(tmp_path):
    """zspan_whisper_fallback_to_openai=false (explicit) should behave
    the same as not-set (strict).
    """
    settings = {
        "zspan_whisper_provider": "mac_node",
        "zspan_whisper_fallback_to_openai": False,
    }
    with patch.object(wc, "load_user_settings", return_value=settings), \
         patch.object(
             wc, "transcribe_youtube_via_mac_node",
             side_effect=wc.WhisperNodeError("mac down"),
         ), patch.object(
             wc, "_transcribe_youtube_via_openai",
         ) as openai_mock:
        with pytest.raises(wc.WhisperNodeError):
            wc.transcribe_youtube(
                "https://www.youtube.com/watch?v=test",
                tmp_path,
                False,
            )
    openai_mock.assert_not_called()


# ── Public API surface ───────────────────────────────────────────


def test_dispatcher_signature_drop_in_compatible_with_old_callers():
    """The dispatcher's signature must exactly match the OLD transcribe_youtube
    so zspan_pipeline/fetcher.py's existing call site (which uses
    functools.partial + positional args + keyword prompt) works without
    edits. Lock the signature.
    """
    import inspect
    sig = inspect.signature(wc.transcribe_youtube)
    params = list(sig.parameters.values())
    # Positional/keyword: youtube_url, work_dir, keep_audio (default False)
    # Keyword-only after *: prompt (default None)
    assert params[0].name == "youtube_url"
    assert params[1].name == "work_dir"
    assert params[2].name == "keep_audio"
    assert params[2].default is False
    # The * keyword-only marker means there's a "prompt" param somewhere
    prompt_param = sig.parameters.get("prompt")
    assert prompt_param is not None
    assert prompt_param.default is None


def test_whisper_node_error_is_subclass_of_whisper_error():
    """Exception hierarchy: WhisperNodeError should subclass WhisperError
    so existing `except WhisperError` clauses still catch Mac failures.
    """
    assert issubclass(wc.WhisperNodeError, wc.WhisperError)
    assert issubclass(wc.WhisperConfigError, wc.WhisperError)

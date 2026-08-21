"""Unit tests for the live-updating ack status tailer (chunk 1c).

Covers the pure functions: _event_to_status, _humanize_bash_desc,
_format_retry_status. The watch loop itself isn't tested here -- it
requires a live subprocess + Slack + filesystem state, which belongs
in an integration test, not a unit test.

Run via:
    cd 02_Core_Project/council_navigator/parsers
    python3.11 -m pytest tests/test_slack_listener_status_tailer.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the parsers dir importable so the test file can find
# slack_listener_status_tailer.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

import pytest

from slack_listener_status_tailer import (
    _event_to_status,
    _format_retry_status,
    _humanize_bash_desc,
)


# ===== _humanize_bash_desc ================================================


def test_humanize_recognizes_badges():
    assert _humanize_bash_desc("Tier 0: read operator badges") == "Checking the badges"


def test_humanize_recognizes_governor():
    assert _humanize_bash_desc("Tier 0: read ingestion governor for Kingman") == "Checking the ingestion pace"


def test_humanize_recognizes_escalations():
    assert _humanize_bash_desc("Read pending escalations") == "Checking escalations"


def test_humanize_recognizes_watcher_state():
    assert _humanize_bash_desc("Check watcher state file freshness") == "Checking watcher state"


def test_humanize_recognizes_parser_health():
    assert _humanize_bash_desc("read parser-health.json") == "Checking watcher state"


def test_humanize_recognizes_work_orders():
    assert _humanize_bash_desc("list pending work orders for Kingman") == "Checking the work-order queue"


def test_humanize_recognizes_memory():
    assert _humanize_bash_desc("read agent memory for the reviewer") == "Reading agent memory"


def test_humanize_recognizes_trigger():
    assert _humanize_bash_desc("trigger content-scout watcher") == "Triggering an agent"


def test_humanize_recognizes_escalate():
    assert _humanize_bash_desc("escalate decision to James") == "Posting reply"


def test_humanize_falls_back_for_unknown():
    """Unknown description gets a short 'Running:' prefix."""
    out = _humanize_bash_desc("Compute foo metrics from bar")
    assert out.startswith("Running:")
    assert "foo metrics" in out


def test_humanize_truncates_very_long():
    """Long descriptions get truncated with an ellipsis."""
    long = "do something very specific that does not match any keyword " * 3
    out = _humanize_bash_desc(long)
    assert out.startswith("Running:")
    assert out.endswith("...")
    # The fallback truncates at 47 chars + "..." = 50 chars of body after the
    # "Running: " prefix.
    body = out[len("Running: "):]
    assert len(body) <= 50


# ===== _format_retry_status ==============================================


def test_format_retry_first_attempt_no_retries():
    """0 or 1 attempts shouldn't surface as a 'retrying' message."""
    assert _format_retry_status(0, 0) == "Spawning the orchestrator"
    assert _format_retry_status(1, 0) == "Spawning the orchestrator"
    assert _format_retry_status(1, 1) == "Spawning the orchestrator"


def test_format_retry_attempt_2():
    out = _format_retry_status(2, 2)
    assert "attempt 2" in out
    assert "Claude crashed at startup" in out
    assert "retrying" in out


def test_format_retry_attempt_5():
    out = _format_retry_status(5, 5)
    assert "attempt 5" in out


# ===== _event_to_status ==================================================


def test_event_init_returns_reading_the_board():
    e = {"type": "system", "subtype": "init"}
    assert _event_to_status(e) == "Reading the board"


def test_event_thinking_returns_thinking():
    e = {"type": "system", "subtype": "thinking_tokens"}
    assert _event_to_status(e) == "Thinking"


def test_event_stream_event_returns_none():
    """Stream events fire many times per second -- explicitly ignored."""
    e = {"type": "stream_event"}
    assert _event_to_status(e) is None


def test_event_result_returns_none():
    """result events are handled by the watch loop's terminal-state path,
    not by the per-event mapper."""
    e = {"type": "result", "subtype": "success"}
    assert _event_to_status(e) is None


def test_event_rate_limit_returns_none():
    e = {"type": "rate_limit_event"}
    assert _event_to_status(e) is None


def test_event_assistant_bash_with_description():
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {
                        "command": "python3.11 scripts/orchestrator_board_read.py /api/operator/badges",
                        "description": "Tier 0: read operator badges",
                    },
                }
            ]
        },
    }
    assert _event_to_status(e) == "Checking the badges"


def test_event_assistant_bash_without_description():
    """Bash with no description falls back to a short shell preview."""
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "ls -la"},
                }
            ]
        },
    }
    out = _event_to_status(e)
    assert out is not None
    assert "ls" in out


def test_event_assistant_read_block():
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "C:\\Users\\james\\some\\parser-health.json"},
                }
            ]
        },
    }
    assert _event_to_status(e) == "Reading parser-health.json"


def test_event_assistant_glob_block():
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Glob",
                    "input": {"pattern": "agents/_scout_state/*.json"},
                }
            ]
        },
    }
    assert _event_to_status(e) == "Looking up files"


def test_event_assistant_grep_block():
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Grep",
                    "input": {"pattern": "foo"},
                }
            ]
        },
    }
    assert _event_to_status(e) == "Searching files"


def test_event_assistant_todo_block():
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "TodoWrite",
                    "input": {"todos": [{"subject": "do thing"}]},
                }
            ]
        },
    }
    assert _event_to_status(e) == "Planning steps"


def test_event_assistant_text_block_returns_writing_reply():
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Here is what I observed..."}
            ]
        },
    }
    assert _event_to_status(e) == "Writing reply"


def test_event_assistant_empty_text_block_ignored():
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": ""}
            ]
        },
    }
    # Empty text shouldn't trigger a "Writing reply" status.
    assert _event_to_status(e) is None


def test_event_assistant_unknown_tool_falls_back():
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "FancyNewTool",
                    "input": {},
                }
            ]
        },
    }
    assert _event_to_status(e) == "Using FancyNewTool"


def test_event_assistant_last_block_wins():
    """When multiple blocks are in one message, the LAST informative one
    is the most representative (it's what the orchestrator last did)."""
    e = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"description": "Tier 0: read operator badges"},
                },
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"description": "Read pending escalations"},
                },
            ]
        },
    }
    assert _event_to_status(e) == "Checking escalations"


def test_event_assistant_malformed_message_returns_none():
    e = {"type": "assistant", "message": "not a dict"}
    assert _event_to_status(e) is None


def test_event_assistant_no_content_returns_none():
    e = {"type": "assistant", "message": {}}
    assert _event_to_status(e) is None


def test_event_assistant_content_not_list_returns_none():
    e = {"type": "assistant", "message": {"content": "wrong shape"}}
    assert _event_to_status(e) is None


def test_event_user_returns_none():
    """user events are tool results echoed back -- not status-worthy."""
    e = {"type": "user", "message": {"content": [{"type": "tool_result"}]}}
    assert _event_to_status(e) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

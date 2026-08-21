"""Build the private contribution package required by the official client.

The user's provider key and downloaded media never enter this shape. The
package contains the public recording URL, transcript words, final rendered
outputs, and their deterministic audit metadata. Canonical hashes make a
retry byte-identical and let the flagship reject idempotency-key reuse.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable


SCHEMA_VERSION = "zspan.private-contribution.v1"
OUTPUT_TYPES = (
    "synopsis",
    "key_decisions",
    "community_calls_to_action",
    "episode_tagline",
)


class ContributionError(ValueError):
    """Local artifacts cannot form the required contribution package."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ContributionError(f"contribution contains non-JSON data: {exc}") from exc
    return text.encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def build_core(meeting_row: Any, transcript: Dict[str, Any], outputs: Dict[str, Any]) -> dict:
    public_id = str(meeting_row["public_id"] or "").strip()
    if not public_id:
        raise ContributionError("the meeting has no public catalog id")

    words = transcript.get("words")
    if not isinstance(words, list) or not words:
        raise ContributionError("the transcript has no words")
    clean_words = []
    for index, item in enumerate(words):
        if not isinstance(item, dict):
            raise ContributionError(f"transcript word {index} is not an object")
        try:
            word = str(item["word"]).strip()
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContributionError(f"transcript word {index} is incomplete") from exc
        if not word:
            raise ContributionError(f"transcript word {index} is empty")
        clean_words.append({"word": word, "start": start, "end": end})

    source_url = str(
        transcript.get("source_url") or meeting_row["video_url"] or ""
    ).strip()
    transcript_core = {
        "source_url": source_url,
        "duration_seconds": float(transcript.get("duration_seconds") or 0.0),
        "language": str(transcript.get("language") or "und").strip() or "und",
        "transcriber": str(transcript.get("transcriber") or "").strip(),
        "model": str(transcript.get("model") or "").strip(),
        "words": clean_words,
    }
    transcript_payload = {
        **transcript_core,
        "sha256": sha256_json(transcript_core),
    }

    if set(outputs) != set(OUTPUT_TYPES):
        missing = sorted(set(OUTPUT_TYPES) - set(outputs))
        extra = sorted(set(outputs) - set(OUTPUT_TYPES))
        raise ContributionError(
            f"rendered output set is incomplete (missing={missing}, extra={extra})"
        )
    output_payloads = []
    for output_type in OUTPUT_TYPES:
        row = outputs[output_type]
        content = row.get("content")
        if not isinstance(content, str):
            raise ContributionError(f"{output_type} has no text content")
        output_payloads.append({
            "output_type": output_type,
            "content": content,
            "provider": str(row.get("provider") or "").strip(),
            "model": str(row.get("model") or "").strip(),
            "gate_status": str(row.get("gate_status") or "").strip(),
            "gate_log": str(row.get("gate_log") or "").strip(),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "meeting_public_id": public_id,
        "transcript": transcript_payload,
        "outputs": output_payloads,
    }


def finish(core: dict, idempotency_key: str) -> dict:
    return {
        **core,
        "idempotency_key": idempotency_key,
        "payload_sha256": sha256_json(core),
    }


def assert_secrets_absent(payload: dict, secret_values: Iterable[str]) -> None:
    encoded = _canonical_bytes(payload).decode("utf-8")
    for secret in secret_values:
        if isinstance(secret, str) and len(secret) >= 12 and secret in encoded:
            raise ContributionError("a configured provider credential appeared in the package")

"""Detect transcript anomalies without deleting or rewriting raw evidence.

The repetition detector is deliberately conservative: an exact back-to-back
repetition is not enough.  A candidate must also be long-lived and cross a
large gap in the word timings.  A second detector records low token-entropy
windows.  Entropy corroborates a spatially-overlapping repetition span, but an
entropy-only hit is review evidence and never changes retrievability.

The original word dictionaries remain intact apart from an additive
``quarantine`` annotation created only by the repetition detector.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Sequence


logger = logging.getLogger(__name__)

DETECTOR_VERSION = "degenerate-repetition-v1"
ENTROPY_DETECTOR_VERSION = "windowed-token-entropy-v1"
ANALYSIS_VERSION = "transcript-anomaly-v2"
TRANSCRIPT_METADATA_KEY = "degenerate_span_quarantine"
WORD_ANNOTATION_KEY = "quarantine"
QUARANTINE_REASON = "degenerate_repetition"


@dataclass(frozen=True)
class DetectorConfig:
    """Tunable evidence floors for a retrievability-changing decision."""

    max_phrase_words: int = 20
    min_region_words: int = 20
    min_repetitions: int = 3
    min_span_seconds: float = 30.0
    min_internal_gap_seconds: float = 8.0


DEFAULT_CONFIG = DetectorConfig()


@dataclass(frozen=True)
class EntropyConfig:
    """Fixed-window token entropy settings for the review signal."""

    window_size_tokens: int = 60
    step_tokens: int = 10
    low_entropy_threshold_bits: float = 3.25


DEFAULT_ENTROPY_CONFIG = EntropyConfig()


@dataclass(frozen=True)
class _Candidate:
    start_index: int
    end_index: int
    phrase_length_words: int
    repetition_count: int
    duration_seconds: float
    max_internal_gap_seconds: float
    phrase: str

    @property
    def word_count(self) -> int:
        return self.end_index - self.start_index


@dataclass(frozen=True)
class QuarantineResult:
    detector_ran: bool
    changed: bool
    words_scanned: int
    quarantined_word_count: int
    spans: tuple[dict[str, Any], ...]
    candidate_regions_considered: int
    rejected_candidates: dict[str, int]
    entropy_profile: tuple[dict[str, Any], ...]
    entropy_regions: tuple[dict[str, Any], ...]
    entropy_only_region_count: int
    corroborated_span_count: int


def is_quarantined_word(word: Any) -> bool:
    """Return whether a transcript word carries any explicit quarantine."""
    if not isinstance(word, dict):
        return False
    if word.get("quarantined") is True:
        return True
    annotation = word.get(WORD_ANNOTATION_KEY)
    return isinstance(annotation, dict) and bool(annotation.get("reason"))


def _normalize_word(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _normalize_entropy_token(value: Any) -> str:
    """Normalize identity while retaining lexical punctuation as evidence."""
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _shannon_entropy_bits(tokens: Sequence[str]) -> float:
    counts = Counter(tokens)
    token_count = len(tokens)
    return -sum(
        (count / token_count) * math.log2(count / token_count)
        for count in counts.values()
    )


def _validate_entropy_config(config: EntropyConfig) -> None:
    if config.window_size_tokens < 1:
        raise ValueError("entropy window_size_tokens must be positive")
    if config.step_tokens < 1:
        raise ValueError("entropy step_tokens must be positive")
    if (
        not math.isfinite(config.low_entropy_threshold_bits)
        or config.low_entropy_threshold_bits < 0
    ):
        raise ValueError(
            "entropy low_entropy_threshold_bits must be finite and non-negative"
        )


def _merge_entropy_windows(
    words: Sequence[Any],
    low_windows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    components: list[list[dict[str, Any]]] = []
    component_end = -1
    for window in low_windows:
        if components and window["start_word_index"] < component_end:
            components[-1].append(window)
            component_end = max(
                component_end, window["end_word_index_exclusive"]
            )
        else:
            components.append([window])
            component_end = window["end_word_index_exclusive"]

    regions: list[dict[str, Any]] = []
    for region_id, component in enumerate(components):
        start_index = min(item["start_word_index"] for item in component)
        end_index = max(item["end_word_index_exclusive"] for item in component)
        minimum = min(component, key=lambda item: item["entropy_bits"])
        regions.append(
            {
                "region_id": region_id,
                "signal": "low_token_entropy",
                "start_word_index": start_index,
                "end_word_index_exclusive": end_index,
                "start_seconds": _finite_time(words[start_index], "start"),
                "end_seconds": _finite_time(words[end_index - 1], "end"),
                "window_count": len(component),
                "min_entropy_bits": minimum["entropy_bits"],
                "min_entropy_window_start_word_index": minimum[
                    "start_word_index"
                ],
            }
        )
        for window in component:
            window["region_id"] = region_id
    return regions


def profile_token_entropy(
    words: Sequence[Any],
    *,
    config: EntropyConfig = DEFAULT_ENTROPY_CONFIG,
) -> dict[str, Any]:
    """Return deterministic full-profile evidence for low token entropy.

    A fixed 60-token window is intentionally not shortened at document edges:
    a smaller sample has a lower attainable entropy and would not be comparable
    to the labeled threshold.  Windows containing malformed/empty word tokens
    remain visible in the profile with ``entropy_bits=None`` and never fire.
    """
    _validate_entropy_config(config)
    tokens = [
        _normalize_entropy_token(word.get("word", ""))
        if isinstance(word, dict)
        else ""
        for word in words
    ]
    profile: list[dict[str, Any]] = []
    valid_entropies: list[float] = []
    low_windows: list[dict[str, Any]] = []
    invalid_windows = 0
    last_start = len(tokens) - config.window_size_tokens
    if last_start >= 0:
        for start_index in range(0, last_start + 1, config.step_tokens):
            end_index = start_index + config.window_size_tokens
            window_tokens = tokens[start_index:end_index]
            if not all(window_tokens):
                entropy_bits = None
                signal_fired = False
                invalid_windows += 1
            else:
                unrounded_entropy = _shannon_entropy_bits(window_tokens)
                entropy_bits = round(unrounded_entropy, 6)
                valid_entropies.append(unrounded_entropy)
                signal_fired = (
                    unrounded_entropy <= config.low_entropy_threshold_bits
                )
            item = {
                "start_word_index": start_index,
                "end_word_index_exclusive": end_index,
                "entropy_bits": entropy_bits,
                "signal_fired": signal_fired,
                "region_id": None,
            }
            profile.append(item)
            if signal_fired:
                low_windows.append(item)

    regions = _merge_entropy_windows(words, low_windows)
    return {
        "status": (
            "completed"
            if profile
            else "insufficient_words_for_fixed_window"
        ),
        "detector_version": ENTROPY_DETECTOR_VERSION,
        "thresholds": asdict(config),
        "token_normalization": "NFKC+casefold+strip; punctuation retained",
        "windows_evaluated": len(profile),
        "invalid_windows": invalid_windows,
        "min_entropy_bits": (
            round(min(valid_entropies), 6) if valid_entropies else None
        ),
        "median_entropy_bits": (
            round(statistics.median(valid_entropies), 6)
            if valid_entropies
            else None
        ),
        "low_entropy_window_count": len(low_windows),
        "profile": profile,
        "regions": regions,
    }


def _finite_time(word: Any, field: str) -> float | None:
    if not isinstance(word, dict):
        return None
    value = word.get(field)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return None
    return float(value)


def _timing_evidence(
    words: Sequence[Any],
    start_index: int,
    end_index: int,
) -> tuple[float | None, float | None]:
    start = _finite_time(words[start_index], "start")
    end = _finite_time(words[end_index - 1], "end")
    if start is None or end is None or end < start:
        return None, None

    gaps: list[float] = []
    for index in range(start_index + 1, end_index):
        previous_end = _finite_time(words[index - 1], "end")
        current_start = _finite_time(words[index], "start")
        if previous_end is None or current_start is None:
            return None, None
        gaps.append(max(0.0, current_start - previous_end))
    return end - start, max(gaps, default=0.0)


def _candidate_failures(
    *,
    repetition_count: int,
    duration_seconds: float | None,
    max_internal_gap_seconds: float | None,
    config: DetectorConfig,
) -> tuple[str, ...]:
    failures: list[str] = []
    if repetition_count < config.min_repetitions:
        failures.append("too_few_repetitions")
    if duration_seconds is None or max_internal_gap_seconds is None:
        failures.append("invalid_or_missing_word_timings")
        return tuple(failures)
    if duration_seconds < config.min_span_seconds:
        failures.append("span_too_short")
    if max_internal_gap_seconds < config.min_internal_gap_seconds:
        failures.append("no_near_silence_timing_signal")
    return tuple(failures)


def _detect_candidates(
    words: Sequence[Any],
    config: DetectorConfig,
) -> tuple[list[_Candidate], int, Counter[str]]:
    normalized = [_normalize_word(word.get("word", "")) if isinstance(word, dict) else "" for word in words]
    accepted: list[_Candidate] = []
    rejected: Counter[str] = Counter()
    considered = 0

    for start_index in range(len(normalized)):
        max_phrase = min(
            config.max_phrase_words,
            (len(normalized) - start_index) // 2,
        )
        for phrase_length in range(1, max_phrase + 1):
            phrase = normalized[start_index:start_index + phrase_length]
            if not all(phrase):
                continue
            if (
                normalized[
                    start_index + phrase_length:start_index + 2 * phrase_length
                ]
                != phrase
            ):
                continue

            repetitions = 2
            while (
                start_index + (repetitions + 1) * phrase_length
                <= len(normalized)
                and normalized[
                    start_index + repetitions * phrase_length:
                    start_index + (repetitions + 1) * phrase_length
                ]
                == phrase
            ):
                repetitions += 1

            end_index = start_index + repetitions * phrase_length
            if end_index - start_index < config.min_region_words:
                continue
            considered += 1
            duration, max_gap = _timing_evidence(words, start_index, end_index)
            failures = _candidate_failures(
                repetition_count=repetitions,
                duration_seconds=duration,
                max_internal_gap_seconds=max_gap,
                config=config,
            )
            if failures:
                rejected.update(failures)
                continue

            assert duration is not None
            assert max_gap is not None
            accepted.append(
                _Candidate(
                    start_index=start_index,
                    end_index=end_index,
                    phrase_length_words=phrase_length,
                    repetition_count=repetitions,
                    duration_seconds=duration,
                    max_internal_gap_seconds=max_gap,
                    phrase=" ".join(
                        str(words[index].get("word", "")).strip()
                        for index in range(start_index, start_index + phrase_length)
                        if isinstance(words[index], dict)
                    ),
                )
            )

    return accepted, considered, rejected


def _merge_candidates(
    words: Sequence[Any],
    candidates: Sequence[_Candidate],
) -> list[dict[str, Any]]:
    components: list[list[_Candidate]] = []
    component_end = -1
    for candidate in sorted(
        candidates,
        key=lambda item: (item.start_index, item.end_index, item.phrase_length_words),
    ):
        if components and candidate.start_index <= component_end:
            components[-1].append(candidate)
            component_end = max(component_end, candidate.end_index)
        else:
            components.append([candidate])
            component_end = candidate.end_index

    spans: list[dict[str, Any]] = []
    for span_id, component in enumerate(components):
        start_index = min(candidate.start_index for candidate in component)
        end_index = max(candidate.end_index for candidate in component)
        representative = max(
            component,
            key=lambda item: (
                item.word_count,
                item.repetition_count,
                -item.phrase_length_words,
                item.duration_seconds,
            ),
        )
        start_seconds = _finite_time(words[start_index], "start")
        end_seconds = _finite_time(words[end_index - 1], "end")
        spans.append(
            {
                "span_id": span_id,
                "reason": QUARANTINE_REASON,
                "start_word_index": start_index,
                "end_word_index_exclusive": end_index,
                "word_count": end_index - start_index,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "duration_seconds": (
                    round(end_seconds - start_seconds, 6)
                    if start_seconds is not None and end_seconds is not None
                    else None
                ),
                "representative_phrase": representative.phrase,
                "representative_phrase_length_words": (
                    representative.phrase_length_words
                ),
                "representative_repetition_count": (
                    representative.repetition_count
                ),
                "max_internal_gap_seconds": round(
                    max(item.max_internal_gap_seconds for item in component),
                    6,
                ),
                "overlapping_candidate_count": len(component),
            }
        )
    return spans


def _ranges_overlap(
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> bool:
    return first_start < second_end and second_start < first_end


def _substantial_overlap(
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> bool:
    overlap = max(0, min(first_end, second_end) - max(first_start, second_start))
    shorter_range = min(first_end - first_start, second_end - second_start)
    return shorter_range > 0 and overlap * 2 >= shorter_range


def _combine_signal_evidence(
    spans: list[dict[str, Any]],
    entropy_evidence: dict[str, Any],
) -> tuple[int, int]:
    """Attach spatially-local decisions and return corroborated/review counts."""
    profile = entropy_evidence["profile"]
    regions = entropy_evidence["regions"]

    corroborated_span_count = 0
    for span in spans:
        overlapping_windows = [
            window
            for window in profile
            if window["entropy_bits"] is not None
            and _ranges_overlap(
                span["start_word_index"],
                span["end_word_index_exclusive"],
                window["start_word_index"],
                window["end_word_index_exclusive"],
            )
        ]
        agreeing_low_windows = [
            window
            for window in profile
            if window["signal_fired"]
            and _substantial_overlap(
                span["start_word_index"],
                span["end_word_index_exclusive"],
                window["start_word_index"],
                window["end_word_index_exclusive"],
            )
        ]
        agreeing_region_ids = sorted(
            {
                window["region_id"]
                for window in agreeing_low_windows
                if window["region_id"] is not None
            }
        )
        entropy_fired = bool(agreeing_region_ids)
        if entropy_fired:
            corroborated_span_count += 1
        span["signals_fired"] = (
            ["repetition", "low_token_entropy"]
            if entropy_fired
            else ["repetition"]
        )
        span["decision"] = (
            "quarantine_high_confidence"
            if entropy_fired
            else "quarantine_repetition"
        )
        span["entropy_evidence"] = {
            "detector_version": ENTROPY_DETECTOR_VERSION,
            "threshold_bits": entropy_evidence["thresholds"][
                "low_entropy_threshold_bits"
            ],
            "min_overlapping_entropy_bits": (
                min(
                    window["entropy_bits"]
                    for window in overlapping_windows
                )
                if overlapping_windows
                else None
            ),
            "overlapping_low_entropy_region_ids": agreeing_region_ids,
            "agreement_rule": "at_least_half_of_shorter_window_or_span",
        }

    entropy_only_region_count = 0
    for region in regions:
        overlapping_spans = [
            span
            for span in spans
            if region["region_id"]
            in span["entropy_evidence"]["overlapping_low_entropy_region_ids"]
        ]
        if overlapping_spans:
            region["signals_fired"] = ["repetition", "low_token_entropy"]
            region["decision"] = "quarantine_high_confidence"
        else:
            entropy_only_region_count += 1
            region["signals_fired"] = ["low_token_entropy"]
            region["decision"] = "flag_for_review"
        region["overlapping_repetition_span_ids"] = [
            span["span_id"] for span in overlapping_spans
        ]

    return corroborated_span_count, entropy_only_region_count


def _sync_repetition_annotations(
    words: Sequence[Any],
    spans: Sequence[dict[str, Any]],
) -> None:
    """Apply the desired repetition annotations without re-tagging matches."""
    desired: dict[int, dict[str, Any]] = {}
    for span in spans:
        annotation = {
            "reason": QUARANTINE_REASON,
            "detector_version": DETECTOR_VERSION,
            "span_id": span["span_id"],
        }
        for index in range(
            span["start_word_index"], span["end_word_index_exclusive"]
        ):
            desired[index] = annotation

    for index, word in enumerate(words):
        if not isinstance(word, dict):
            continue
        existing = word.get(WORD_ANNOTATION_KEY)
        wanted = desired.get(index)
        if (
            isinstance(existing, dict)
            and existing.get("detector_version") == DETECTOR_VERSION
        ):
            if wanted is None:
                word.pop(WORD_ANNOTATION_KEY)
            elif existing != wanted:
                word[WORD_ANNOTATION_KEY] = dict(wanted)
        elif wanted is not None and not is_quarantined_word(word):
            word[WORD_ANNOTATION_KEY] = dict(wanted)


def apply_degenerate_span_quarantine(
    transcript: dict[str, Any],
    *,
    config: DetectorConfig = DEFAULT_CONFIG,
    entropy_config: EntropyConfig = DEFAULT_ENTROPY_CONFIG,
) -> QuarantineResult:
    """Profile anomalies and add deterministic quarantine/review metadata.

    Re-running with the same version and configuration is byte-semantics
    idempotent. Existing annotations are preserved when they already express
    the desired decision; entropy-only evidence never annotates a word.
    """
    words = transcript.get("words")
    if not isinstance(words, list):
        raise ValueError("transcript content has no words list")

    before = json.dumps(
        transcript,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    candidates, considered, rejected = _detect_candidates(words, config)
    spans = _merge_candidates(words, candidates)
    entropy_evidence = profile_token_entropy(words, config=entropy_config)
    corroborated_span_count, entropy_only_region_count = (
        _combine_signal_evidence(spans, entropy_evidence)
    )
    _sync_repetition_annotations(words, spans)

    quarantined_word_count = sum(span["word_count"] for span in spans)
    transcript[TRANSCRIPT_METADATA_KEY] = {
        "status": "completed",
        "analysis_version": ANALYSIS_VERSION,
        "detector_version": DETECTOR_VERSION,
        "thresholds": asdict(config),
        "decision_policy": {
            "both_agree": "quarantine_high_confidence",
            "repetition_only": "quarantine_repetition",
            "entropy_only": "flag_for_review",
            "neither": "nothing",
            "agreement_rule": "at_least_half_of_shorter_window_or_span",
        },
        "words_scanned": len(words),
        "quarantined_word_count": quarantined_word_count,
        "candidate_regions_considered": considered,
        "rejected_candidates": dict(sorted(rejected.items())),
        "spans": spans,
        "entropy": entropy_evidence,
        "decision_summary": {
            "corroborated_repetition_spans": corroborated_span_count,
            "repetition_only_spans": len(spans) - corroborated_span_count,
            "entropy_only_review_regions": entropy_only_region_count,
            "review_required": entropy_only_region_count > 0,
        },
    }
    after = json.dumps(
        transcript,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return QuarantineResult(
        detector_ran=True,
        changed=before != after,
        words_scanned=len(words),
        quarantined_word_count=quarantined_word_count,
        spans=tuple(spans),
        candidate_regions_considered=considered,
        rejected_candidates=dict(sorted(rejected.items())),
        entropy_profile=tuple(entropy_evidence["profile"]),
        entropy_regions=tuple(entropy_evidence["regions"]),
        entropy_only_region_count=entropy_only_region_count,
        corroborated_span_count=corroborated_span_count,
    )


def log_quarantine_result(meeting_id: int, result: QuarantineResult) -> None:
    """Emit one complete per-meeting decision trail, including clean runs."""
    valid_entropies = [
        item["entropy_bits"]
        for item in result.entropy_profile
        if item["entropy_bits"] is not None
    ]
    logger.info(
        "transcript quarantine meeting=%d detector_ran=%s words_scanned=%d "
        "quarantined_words=%d spans=%d candidates_considered=%d "
        "rejected=%s entropy_windows=%d entropy_min_bits=%s "
        "entropy_median_bits=%s entropy_regions=%d entropy_only_review=%d "
        "corroborated_spans=%d changed=%s",
        meeting_id,
        result.detector_ran,
        result.words_scanned,
        result.quarantined_word_count,
        len(result.spans),
        result.candidate_regions_considered,
        result.rejected_candidates,
        len(result.entropy_profile),
        min(valid_entropies) if valid_entropies else None,
        statistics.median(valid_entropies) if valid_entropies else None,
        len(result.entropy_regions),
        result.entropy_only_region_count,
        result.corroborated_span_count,
        result.changed,
    )
    for span in result.spans:
        logger.warning(
            "transcript quarantine meeting=%d span=%d words=[%d,%d) "
            "seconds=[%s,%s] quarantined_words=%d reason=%s phrase=%r "
            "phrase_words=%d repetitions=%d max_internal_gap_seconds=%.3f "
            "overlapping_candidates=%d signals=%s decision=%s "
            "min_overlapping_entropy_bits=%s entropy_region_ids=%s",
            meeting_id,
            span["span_id"],
            span["start_word_index"],
            span["end_word_index_exclusive"],
            span["start_seconds"],
            span["end_seconds"],
            span["word_count"],
            span["reason"],
            span["representative_phrase"],
            span["representative_phrase_length_words"],
            span["representative_repetition_count"],
            span["max_internal_gap_seconds"],
            span["overlapping_candidate_count"],
            span["signals_fired"],
            span["decision"],
            span["entropy_evidence"]["min_overlapping_entropy_bits"],
            span["entropy_evidence"]["overlapping_low_entropy_region_ids"],
        )
    for region in result.entropy_regions:
        logger.warning(
            "transcript entropy meeting=%d region=%d words=[%d,%d) "
            "seconds=[%s,%s] windows=%d min_entropy_bits=%.6f "
            "signals=%s decision=%s repetition_span_ids=%s",
            meeting_id,
            region["region_id"],
            region["start_word_index"],
            region["end_word_index_exclusive"],
            region["start_seconds"],
            region["end_seconds"],
            region["window_count"],
            region["min_entropy_bits"],
            region["signals_fired"],
            region["decision"],
            region["overlapping_repetition_span_ids"],
        )

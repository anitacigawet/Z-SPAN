"""Unit tests for the deterministic neutrality-audit layer (S-133 v0.1).

Every case here is stdlib-only and instant — the deterministic layer takes
no LLM, no DB, no network. The ref cases encode Whisper's actually-observed
spoken renderings from the 2026-07-09 corpus run (m103225 Bullhead,
m103996 Lake Havasu), so a future pattern edit that regresses a known
transcript form fails loudly here instead of silently un-grounding frames.

Run via pytest when available, or directly (pytest is not in the worker venv):
    python -m zspan_pipeline.tests.test_neutrality_audit
"""
from __future__ import annotations

import re

from zspan_pipeline.neutrality_audit.deterministic import (
    Transcript,
    align_frames,
    check_frame_shape,
    cluster_vote_moments,
    extract_refs,
    parse_key_decisions,
    ref_grounding_pattern,
    scan_anchors,
)


# ── ref extraction: frame-side formats ─────────────────────────────────

def test_extract_refs_year_r_form():
    assert extract_refs("Adopt Resolution 2026-R-15 tentative budget") == {"r-15"}


def test_extract_refs_two_part_ordinance_no_year_echo():
    assert extract_refs("Ordinance 2026-6 amending Z24-002") == {"ord-2026-6"}


def test_extract_refs_two_part_resolution_lhc_format():
    assert extract_refs("Adopt Resolution 26-3923 appointing Derek Ross") == {"r-26-3923"}


def test_extract_refs_spaced_dash():
    assert extract_refs("resolution number 2026 r -14") == {"r-14"}


def test_extract_refs_empty():
    assert extract_refs("no refs in this text at all") == set()


# ── ref grounding: transcript-side spoken forms (Whisper-observed) ─────

def test_grounding_spaced_r_dash():
    spoken = "resolution number number 2026 r -15, adopting the tentative budget"
    assert re.search(ref_grounding_pattern("r-15"), spoken)


def test_grounding_ordinance_with_number_word():
    spoken = "adopt ordinance number 2026 -6, approving an amendment"
    assert re.search(ref_grounding_pattern("ord-2026-6"), spoken)


def test_grounding_two_part_resolution():
    spoken = "resolution 26-3923 appointing derek ross"
    assert re.search(ref_grounding_pattern("r-26-3923"), spoken)


def test_grounding_no_false_hit():
    assert not re.search(ref_grounding_pattern("r-99"), "the council discussed roads")


# ── signature scan + moment clustering ─────────────────────────────────

def _t(text: str) -> Transcript:
    return Transcript.from_words(text.split())


def test_weak_anchors_alone_found_no_moment():
    # probe-3's false-positive class: a bare numeric range is not a vote
    t = _t("the project will take 3 to 5 years to complete okay moving on")
    assert cluster_vote_moments(scan_anchors(t), t) == []


def test_strong_outcome_founds_moment():
    t = _t("all in favor say aye opposed none the motion carries unanimously")
    moments = cluster_vote_moments(scan_anchors(t), t)
    assert len(moments) == 1
    assert moments[0].strong_count >= 1


def test_gapped_motion_outcome_signature():
    # the m103995 teacher-loop entry: object between 'motion to' and 'passes'
    t = _t("the motion to approve the minutes passes seven to zero okay next item")
    moments = cluster_vote_moments(scan_anchors(t), t)
    assert len(moments) == 1


# ── frame shape grammar ────────────────────────────────────────────────

def test_shape_flags_enum_and_tally_consistency():
    frame = {"vote_result": "acclaimed", "tally": {"aye": 2, "nay": 5},
             "per_member_votes": []}
    flags = check_frame_shape(frame)
    assert any("vote_result_outside_enum" in f for f in flags)


def test_shape_clean_frame_no_flags():
    frame = {"vote_result": "passed", "vote_method": "voice",
             "tally": {"aye": 5, "nay": 0, "abstain": 0, "absent": 0},
             "per_member_votes": []}
    assert check_frame_shape(frame) == []


# ── cross-family alignment ─────────────────────────────────────────────

def test_align_shared_ref_dominates():
    a = [{"motion_reference": "Adopt Resolution 2026-R-15 tentative budget",
          "summary_sentence": "Budget adopted", "vote_result": "passed"}]
    b = [{"motion_reference": "Approve the tentative budget resolution R-15",
          "summary_sentence": "Tentative budget approved", "vote_result": "passed"}]
    result = align_frames(a, b)
    assert len(result.pairs) == 1
    assert result.pairs[0].matched_on == "refs"
    assert result.pairs[0].determinate_divergence == []


def test_align_vote_result_divergence_flags():
    a = [{"motion_reference": "Approve the lifeguard services agreement",
          "summary_sentence": "Lifeguard agreement approved", "vote_result": "passed"}]
    b = [{"motion_reference": "Lifeguard services agreement approval",
          "summary_sentence": "Lifeguard agreement failed", "vote_result": "failed"}]
    result = align_frames(a, b)
    assert len(result.pairs) == 1
    assert "vote_result" in result.pairs[0].determinate_divergence


# ── key_decisions parsing (both live shapes per S-128) ─────────────────

def test_parse_key_decisions_core_markup():
    text = "1. <core>Adopted resolution 2026 R-15</core> approving the **$1,000** budget.\n\n2. <core>Approved X</core>, <nuance>contingent on Y</nuance>."
    decisions, has_markup = parse_key_decisions(text)
    assert has_markup and len(decisions) == 2
    assert "<core>" not in decisions[0] and "**" not in decisions[0]


def test_parse_key_decisions_plain():
    text = "1. Approved the consent agenda.\n2. Adopted the fee schedule."
    decisions, has_markup = parse_key_decisions(text)
    assert not has_markup and len(decisions) == 2


if __name__ == "__main__":
    import sys
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        fn()
        print(f"✓ {name}")
    print(f"{len(fns)} tests pass")
    sys.exit(0)

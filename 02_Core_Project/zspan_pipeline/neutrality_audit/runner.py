"""Audit orchestration: transcript in → per-meeting report out.

Read-only against meetings_cache.db (URI mode=ro — the audit must never
write production tables; a future --persist into transcript_nodes is a
separate, deliberate step). Raw extractions are saved per meeting/family and
reused on re-runs, so the deterministic passes can be iterated for free
without re-spending LLM calls — the whole point of the two-stage split.

Report artifacts default to 03_Research/neutrality_audit_v01_runs/ at the
repo root: operator-side research data, untracked per the D-154 tooling/data
split (the code ships, the runs don't).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from . import deterministic as det
from . import extraction as ext

logger = logging.getLogger(__name__)

CORE_PROJECT_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = CORE_PROJECT_DIR.parent
DEFAULT_DB_PATH = CORE_PROJECT_DIR / "council_navigator" / "parsers" / "meetings_cache.db"
DEFAULT_OUT_DIR = REPO_ROOT / "03_Research" / "neutrality_audit_v01_runs"


# ── DB access (read-only) ─────────────────────────────────────────────


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_transcript_words(meeting_id: int, db_path: Path = DEFAULT_DB_PATH) -> list[str]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT content FROM notebook_outputs WHERE meeting_id=? AND "
            "output_type='transcript_words' ORDER BY id DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
    if not row:
        raise LookupError(f"m{meeting_id}: no transcript_words row")
    raw = json.loads(row["content"])
    words = raw.get("words") if isinstance(raw, dict) else raw
    return [w.get("word", "") for w in words if isinstance(w, dict) and w.get("word")]


def load_key_decisions(meeting_id: int, db_path: Path = DEFAULT_DB_PATH) -> Optional[str]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT content FROM notebook_outputs WHERE meeting_id=? AND "
            "output_type='key_decisions' ORDER BY id DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
    return row["content"] if row else None


def list_corpus_meetings(db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Every meeting with a transcript — the auditable corpus."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT no.meeting_id AS id, m.city_name, m.meeting_title, m.meeting_date, "
            "EXISTS(SELECT 1 FROM notebook_outputs k WHERE k.meeting_id=no.meeting_id "
            "AND k.output_type='key_decisions') AS has_kd "
            "FROM notebook_outputs no JOIN meetings m ON m.id=no.meeting_id "
            "WHERE no.output_type='transcript_words' "
            "GROUP BY no.meeting_id ORDER BY m.city_name, m.meeting_date",
        ).fetchall()
    return [dict(r) for r in rows]


# ── Per-meeting audit ─────────────────────────────────────────────────


def _raw_path(out_dir: Path, meeting_id: int, family: str, run: int = 0) -> Path:
    suffix = f"_run{run}" if run else ""
    return out_dir / "raw" / f"m{meeting_id}_{family}{suffix}.json"


def _extract_or_reuse(words: list[str], meeting_id: int, family: str, out_dir: Path,
                      *, reuse: bool, pace: float, run: int = 0,
                      llm_timeout: float = 420.0) -> dict[str, Any]:
    path = _raw_path(out_dir, meeting_id, family, run)
    if reuse and path.exists():
        logger.info("[m%d/%s] reusing saved extraction %s", meeting_id, family, path.name)
        return json.loads(path.read_text())
    result = ext.extract_frames(words, family, pace_seconds=pace, timeout=llm_timeout)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1))
    return result


def scan_meeting(meeting_id: int, *, db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """The zero-LLM pass alone: signature anchors + vote moments."""
    t = det.Transcript.from_words(load_transcript_words(meeting_id, db_path))
    anchors = det.scan_anchors(t)
    moments = det.cluster_vote_moments(anchors, t)
    return {
        "meeting_id": meeting_id,
        "words": len(t.words),
        "anchors": len(anchors),
        "signature_hits": det.signature_hit_summary(anchors),
        "vote_moments": len(moments),
        "moments": [det.to_jsonable(m) for m in moments],
    }


def audit_meeting(meeting_id: int, *, families: tuple[str, str] = ("claude", "openai"),
                  stability_runs: int = 1, reuse: bool = True, pace: float = 3.0,
                  llm_timeout: float = 420.0, db_path: Path = DEFAULT_DB_PATH,
                  out_dir: Path = DEFAULT_OUT_DIR) -> dict[str, Any]:
    """The full S-133 loop on one meeting: two-family extraction, grounding,
    consensus, output audit. Returns (and saves) the report dict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    words = load_transcript_words(meeting_id, db_path)
    t = det.Transcript.from_words(words)
    anchors = det.scan_anchors(t)
    moments = det.cluster_vote_moments(anchors, t)

    fam_a, fam_b = families
    res_a = _extract_or_reuse(words, meeting_id, fam_a, out_dir, reuse=reuse,
                              pace=pace, llm_timeout=llm_timeout)
    if pace:
        time.sleep(pace)
    res_b = _extract_or_reuse(words, meeting_id, fam_b, out_dir, reuse=reuse,
                              pace=pace, llm_timeout=llm_timeout)

    frames_a, frames_b = res_a["frames"], res_b["frames"]
    grounding_a = [det.ground_frame(i, f, t, moments) for i, f in enumerate(frames_a)]
    grounding_b = [det.ground_frame(i, f, t, moments) for i, f in enumerate(frames_b)]
    consensus = det.align_frames(frames_a, frames_b)

    # coverage-divergence disambiguation: a lone frame that grounds means the
    # OTHER family under-covered; one that doesn't is a fabrication candidate.
    only_a_grounded = [i for i in consensus.only_a if grounding_a[i].verdict == "grounded"]
    only_b_grounded = [j for j in consensus.only_b if grounding_b[j].verdict == "grounded"]
    only_a_suspect = [i for i in consensus.only_a if grounding_a[i].verdict == "ungrounded"]
    only_b_suspect = [j for j in consensus.only_b if grounding_b[j].verdict == "ungrounded"]

    kd_text = load_key_decisions(meeting_id, db_path)
    output_audit = (det.audit_output(kd_text, frames_a, grounding_a, t)
                    if kd_text else None)

    stability = None
    if stability_runs > 1:
        runs = [res_a] + [
            _extract_or_reuse(words, meeting_id, fam_a, out_dir,
                              reuse=reuse, pace=pace, run=k, llm_timeout=llm_timeout)
            for k in range(1, stability_runs)
        ]
        pairs = [det.align_frames(runs[0]["frames"], runs[k]["frames"])
                 for k in range(1, stability_runs)]
        stability = [{
            "run": k + 1,
            "frames": len(runs[k + 1]["frames"] if k + 1 < len(runs) else []),
            "matched": len(p.pairs),
            "determinate_divergences": sum(len(x.determinate_divergence) for x in p.pairs),
            "only_first": len(p.only_a),
            "only_repeat": len(p.only_b),
        } for k, p in enumerate(pairs)]

    report = {
        "meeting_id": meeting_id,
        "transcript_words": len(t.words),
        "signature_scan": {
            "anchors": len(anchors),
            "hits": det.signature_hit_summary(anchors),
            "vote_moments": len(moments),
        },
        "extraction": {
            fam_a: {k: res_a[k] for k in ("model", "segments", "parse_ok", "seconds", "notes")}
            | {"frames": len(frames_a)},
            fam_b: {k: res_b[k] for k in ("model", "segments", "parse_ok", "seconds", "notes")}
            | {"frames": len(frames_b)},
        },
        "grounding": {
            fam_a: _grounding_summary(grounding_a),
            fam_b: _grounding_summary(grounding_b),
            "detail": {fam_a: det.to_jsonable(grounding_a),
                       fam_b: det.to_jsonable(grounding_b)},
        },
        "consensus": det.to_jsonable(consensus) | {
            "coverage_divergence": {
                f"only_{fam_a}_grounded": only_a_grounded,
                f"only_{fam_b}_grounded": only_b_grounded,
                f"only_{fam_a}_fabrication_candidates": only_a_suspect,
                f"only_{fam_b}_fabrication_candidates": only_b_suspect,
            },
        },
        "output_audit": det.to_jsonable(output_audit) if output_audit else None,
        "signature_gaps": det.signature_gap_windows(frames_a, grounding_a, moments, t),
        "stability": stability,
        "frames": {fam_a: frames_a, fam_b: frames_b},
    }
    (out_dir / f"m{meeting_id}_report.json").write_text(json.dumps(report, indent=1))
    return report


def _grounding_summary(groundings: list[det.FrameGrounding]) -> dict[str, int]:
    out = {"grounded": 0, "ungrounded": 0, "unlocatable": 0, "shape_flags": 0}
    for g in groundings:
        out[g.verdict] += 1
        out["shape_flags"] += len(g.shape_flags)
    return out


# ── Corpus passes ─────────────────────────────────────────────────────


def corpus_scan(*, db_path: Path = DEFAULT_DB_PATH,
                out_dir: Path = DEFAULT_OUT_DIR) -> dict[str, Any]:
    """Zero-LLM signature abundance across every transcript-bearing meeting —
    the S-133 'vote frame is universal + abundant' claim, measured."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meetings = list_corpus_meetings(db_path)
    rows = []
    for m in meetings:
        scan = scan_meeting(m["id"], db_path=db_path)
        rows.append({
            "meeting_id": m["id"], "city": m["city_name"],
            "title": (m["meeting_title"] or "")[:60], "date": m["meeting_date"],
            "words": scan["words"], "anchors": scan["anchors"],
            "vote_moments": scan["vote_moments"],
            "has_key_decisions": bool(m["has_kd"]),
            "signature_hits": scan["signature_hits"],
        })
    # duplicate awareness, never silent dropping: rows sharing (city, date,
    # word count) are almost certainly one meeting under two DB rows (verified
    # 2026-07-09: 3 such pairs; one pair byte-identical, one 1 byte apart, one
    # same words re-encoded). Reported loudly; the row reconciliation is an
    # operator decision, so the audit counts them but names them.
    seen: dict[tuple, list[int]] = {}
    for r in rows:
        seen.setdefault((r["city"], r["date"], r["words"]), []).append(r["meeting_id"])
    duplicate_groups = [ids for ids in seen.values() if len(ids) > 1]
    result = {
        "meetings": len(rows),
        "distinct_meetings_estimate": len(rows) - sum(len(g) - 1 for g in duplicate_groups),
        "duplicate_groups": duplicate_groups,
        "with_zero_moments": [r["meeting_id"] for r in rows if r["vote_moments"] == 0],
        "rows": rows,
    }
    (out_dir / "corpus_signature_scan.json").write_text(json.dumps(result, indent=1))
    return result


def corpus_rollup(*, out_dir: Path = DEFAULT_OUT_DIR) -> dict[str, Any]:
    """Aggregate every saved per-meeting report into the corpus-level D-144
    measurement numbers."""
    reports = sorted(out_dir.glob("m*_report.json"))
    rows, totals = [], {
        "meetings": 0, "pairs": 0, "converged_pairs": 0, "diverged_pairs": 0,
        "decisions": 0, "backed": 0, "weakly_backed": 0, "unbacked": 0,
        "fabrication_candidates": 0, "signature_gaps": 0,
    }
    for path in reports:
        r = json.loads(path.read_text())
        cons, oa = r["consensus"], r.get("output_audit")
        cov = cons["coverage_divergence"]
        fab = sum(len(v) for k, v in cov.items() if "fabrication" in k)
        row = {
            "meeting_id": r["meeting_id"],
            "frames": {f: r["extraction"][f]["frames"] for f in r["extraction"]},
            "pairs": len(cons["pairs"]),
            "converged": cons["converged_pairs"],
            "diverged": cons["diverged_pairs"],
            "fabrication_candidates": fab,
            "decisions_backed": oa["backed"] if oa else None,
            "decisions_unbacked": oa["unbacked"] if oa else None,
            "signature_gaps": len(r["signature_gaps"]),
        }
        rows.append(row)
        totals["meetings"] += 1
        totals["pairs"] += row["pairs"]
        totals["converged_pairs"] += row["converged"]
        totals["diverged_pairs"] += row["diverged"]
        totals["fabrication_candidates"] += fab
        totals["signature_gaps"] += row["signature_gaps"]
        if oa:
            totals["decisions"] += len(oa["decisions"])
            totals["backed"] += oa["backed"]
            totals["unbacked"] += oa["unbacked"]
            totals["weakly_backed"] += sum(
                1 for d in oa["decisions"] if d["verdict"] == "weakly_backed")
    result = {"totals": totals, "rows": rows}
    (out_dir / "corpus_rollup.json").write_text(json.dumps(result, indent=1))
    return result


# ── Human-readable rendering ──────────────────────────────────────────


def render_meeting_summary(report: dict[str, Any]) -> str:
    fam_a, fam_b = list(report["extraction"].keys())
    cons = report["consensus"]
    lines = [
        f"— m{report['meeting_id']} · {report['transcript_words']:,} words · "
        f"{report['signature_scan']['vote_moments']} vote moments "
        f"({report['signature_scan']['anchors']} anchors)",
        f"  frames: {fam_a}={report['extraction'][fam_a]['frames']} "
        f"{fam_b}={report['extraction'][fam_b]['frames']} · "
        f"matched pairs={len(cons['pairs'])} "
        f"(converged={cons['converged_pairs']}, diverged={cons['diverged_pairs']})",
        f"  grounding {fam_a}: {report['grounding'][fam_a]} | "
        f"{fam_b}: {report['grounding'][fam_b]}",
    ]
    cov = cons["coverage_divergence"]
    fab = {k: v for k, v in cov.items() if "fabrication" in k and v}
    if fab:
        lines.append(f"  🔴 fabrication candidates: {fab}")
    div = [p for p in cons["pairs"] if p["determinate_divergence"]]
    for p in div:
        lines.append(f"  🔴 determinate divergence on pair "
                     f"({p['index_a']},{p['index_b']}): {p['determinate_divergence']}")
    oa = report.get("output_audit")
    if oa:
        lines.append(f"  key_decisions: {len(oa['decisions'])} claims → "
                     f"{oa['backed']} backed · "
                     f"{sum(1 for d in oa['decisions'] if d['verdict']=='weakly_backed')} weak · "
                     f"{oa['unbacked']} unbacked · "
                     f"markup={'core' if oa['has_core_markup'] else 'plain'}")
        for d in oa["decisions"]:
            if d["verdict"] == "unbacked":
                lines.append(f"    🔴 unbacked claim #{d['ordinal']}: {d['text'][:90]}")
    if report["signature_gaps"]:
        lines.append(f"  📚 signature-gap windows (teacher input): "
                     f"{len(report['signature_gaps'])}")
    if report.get("stability"):
        lines.append(f"  stability (within-{fam_a}): {report['stability']}")
    return "\n".join(lines)

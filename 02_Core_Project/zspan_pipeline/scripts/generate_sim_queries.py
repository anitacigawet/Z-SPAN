"""Generate the three cached signed-out Librarian answers for meetings.

Generation is deliberately local and serial: complete hash-verified transcript
evidence followed by the fixed flagship fallback chain per question. All three
answers are validated before one transaction replaces any prior generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


_THIS_DIR = Path(__file__).resolve().parent
_CORE_PROJECT_DIR = _THIS_DIR.parents[1]
_PARSERS_DIR = _CORE_PROJECT_DIR / "council_navigator" / "parsers"
for _import_dir in (_CORE_PROJECT_DIR, _PARSERS_DIR):
    if str(_import_dir) not in sys.path:
        sys.path.insert(0, str(_import_dir))

from zspan_pipeline.prompt_loader import load_prompt_with_meta  # noqa: E402
from zspan_pipeline.sim_query_synthesis import (  # noqa: E402
    SIM_QUERY_CURRENT_MODEL_IDS,
    SimQueryResult,
    SimQuerySynthesisError,
    synthesize_sim_query_answer,
)
from zspan_pipeline.sim_query_vocab import (  # noqa: E402
    SIM_QUERY_VOCAB_VERSION,
    bucket_for_title,
    sim_questions_for_title,
)


logger = logging.getLogger(__name__)

PROMPT_FILENAME = "sim_query_answer.md"
PROMPT_NAME = "sim_query_answer"
SLOTS = (0, 1, 2)

_CLOUD_RUNTIME_ENV_VARS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_REPLICA_ID",
)


def get_connection() -> sqlite3.Connection:
    """Open the centrally initialized DB only after local-runtime admission.

    ``database`` initializes the canonical schema when imported. Keeping that
    import lazy ensures an accidental Railway invocation reaches the explicit
    local-only guard before it can touch cloud storage.
    """
    from database import get_connection as database_get_connection

    return database_get_connection()


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str
    body: str
    sha256: str


@dataclass(frozen=True)
class MeetingTarget:
    meeting_id: int
    title: str
    public_id: str


@dataclass(frozen=True)
class GenerationOutcome:
    meeting_id: int
    status: str
    classification: str = ""
    failed_slot: int | None = None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now_z() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _assert_local_generation_environment() -> None:
    detected = [
        name for name in _CLOUD_RUNTIME_ENV_VARS if os.environ.get(name, "").strip()
    ]
    if detected:
        raise RuntimeError(
            "Sim-query generation is local-only; cloud runtime markers "
            f"detected ({', '.join(detected)}). Run this command locally "
            "where the Claude CLI and local vector index are available."
        )


def load_prompt_spec() -> PromptSpec:
    meta, body = load_prompt_with_meta(PROMPT_FILENAME)
    raw_version = meta.get("version")
    version = str(raw_version or "").strip()
    if not version:
        raise ValueError(
            f"{PROMPT_FILENAME} frontmatter must contain a nonempty version"
        )
    if not body.strip():
        raise ValueError(f"{PROMPT_FILENAME} has an empty model-facing body")
    return PromptSpec(
        name=PROMPT_NAME,
        version=version,
        body=body,
        sha256=_sha256_text(body),
    )


def load_meeting_target(
    conn: sqlite3.Connection,
    meeting_id: int,
) -> MeetingTarget:
    row = conn.execute(
        "SELECT id, meeting_title, public_id FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown meeting_id={meeting_id}")
    public_id = str(row["public_id"] or "").strip()
    if not public_id:
        raise ValueError(
            f"meeting_id={meeting_id} has no canonical public_id; run central "
            "database initialization before generation"
        )
    return MeetingTarget(
        meeting_id=int(row["id"]),
        title=str(row["meeting_title"] or "").strip(),
        public_id=public_id,
    )


def load_all_published_targets(
    conn: sqlite3.Connection,
) -> list[MeetingTarget]:
    rows = conn.execute(
        """
        SELECT m.id, m.meeting_title, m.public_id
        FROM meetings m
        WHERE m.is_published = 1
          AND EXISTS (
              SELECT 1
              FROM work_orders w
              WHERE w.meeting_id = m.id
                AND w.approved_at IS NOT NULL
          )
        ORDER BY m.id
        """
    ).fetchall()
    targets: list[MeetingTarget] = []
    for row in rows:
        public_id = str(row["public_id"] or "").strip()
        if not public_id:
            raise ValueError(
                f"published meeting_id={row['id']} has no canonical public_id"
            )
        targets.append(MeetingTarget(
            meeting_id=int(row["id"]),
            title=str(row["meeting_title"] or "").strip(),
            public_id=public_id,
        ))
    return targets


def _decode_chunk_ids(raw: object) -> list[int] | None:
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in decoded
        )
    ):
        return None
    return decoded


def has_complete_current_triplet(
    conn: sqlite3.Connection,
    target: MeetingTarget,
    prompt: PromptSpec,
) -> bool:
    questions = sim_questions_for_title(target.title)
    rows = conn.execute(
        """
        SELECT query_slot, question_text, answer_text, prompt_name,
               prompt_version, prompt_hash, vocab_version, query_hash,
               answer_digest, model_id, retrieved_chunk_ids, run_id,
               generated_at
        FROM episode_sim_queries
        WHERE meeting_id = ?
        ORDER BY query_slot
        """,
        (target.meeting_id,),
    ).fetchall()
    if len(rows) != 3 or [int(row["query_slot"]) for row in rows] != list(SLOTS):
        return False

    shared_run_ids = {str(row["run_id"] or "") for row in rows}
    shared_generated_at = {str(row["generated_at"] or "") for row in rows}
    if len(shared_run_ids) != 1 or "" in shared_run_ids:
        return False
    try:
        stored_run_id = next(iter(shared_run_ids))
        if str(uuid.UUID(stored_run_id)) != stored_run_id:
            return False
    except (ValueError, AttributeError):
        return False
    if len(shared_generated_at) != 1:
        return False
    stored_generated_at = next(iter(shared_generated_at))
    if not stored_generated_at.endswith("Z"):
        return False
    try:
        parsed_generated_at = datetime.fromisoformat(
            stored_generated_at[:-1] + "+00:00"
        )
    except ValueError:
        return False
    if parsed_generated_at.utcoffset() != timezone.utc.utcoffset(None):
        return False

    for slot, (row, question) in enumerate(zip(rows, questions)):
        answer_text = str(row["answer_text"] or "")
        if (
            int(row["query_slot"]) != slot
            or str(row["question_text"] or "") != question
            or not answer_text.strip()
            or str(row["prompt_name"] or "") != prompt.name
            or str(row["prompt_version"] or "") != prompt.version
            or str(row["prompt_hash"] or "") != prompt.sha256
            or str(row["vocab_version"] or "") != SIM_QUERY_VOCAB_VERSION
            or str(row["query_hash"] or "") != _sha256_text(question)
            or str(row["answer_digest"] or "") != _sha256_text(answer_text)
            or str(row["model_id"] or "") not in SIM_QUERY_CURRENT_MODEL_IDS
            or _decode_chunk_ids(row["retrieved_chunk_ids"]) is None
        ):
            return False
    return True


def _replace_triplet(
    conn: sqlite3.Connection,
    meeting_id: int,
    rows: Sequence[tuple[object, ...]],
) -> None:
    if len(rows) != 3 or [int(row[1]) for row in rows] != list(SLOTS):
        raise ValueError("sim-query replacement requires exact slots {0,1,2}")

    savepoint = "replace_episode_sim_queries"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute(
            "DELETE FROM episode_sim_queries WHERE meeting_id = ?",
            (meeting_id,),
        )
        conn.executemany(
            """
            INSERT INTO episode_sim_queries (
                meeting_id, query_slot, question_text, answer_text,
                prompt_name, prompt_version, prompt_hash, vocab_version,
                query_hash, answer_digest, model_id, retrieved_chunk_ids,
                run_id, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        persisted = conn.execute(
            """
            SELECT query_slot
            FROM episode_sim_queries
            WHERE meeting_id = ?
            ORDER BY query_slot
            """,
            (meeting_id,),
        ).fetchall()
        if [int(row[0]) for row in persisted] != list(SLOTS):
            raise RuntimeError(
                f"atomic sim-query write produced invalid slots for {meeting_id}"
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def generate_for_target(
    conn: sqlite3.Connection,
    target: MeetingTarget,
    prompt: PromptSpec,
    *,
    force: bool = False,
) -> GenerationOutcome:
    if not force and has_complete_current_triplet(conn, target, prompt):
        logger.info(
            "skipped current sim-query triplet for meeting_id=%d (public_id=%r)",
            target.meeting_id,
            target.public_id,
        )
        return GenerationOutcome(target.meeting_id, "skipped")

    questions = sim_questions_for_title(target.title)
    results: list[SimQueryResult] = []
    for slot, question in enumerate(questions):
        try:
            result = synthesize_sim_query_answer(
                meeting_id=target.meeting_id,
                question=question,
                prompt_body=prompt.body,
                conn=conn,
            )
        except SimQuerySynthesisError as exc:
            logger.error(
                "sim-query generation failed: classification=%s "
                "meeting_id=%d public_id=%r slot=%d question=%r error=%s; "
                "prior triplet preserved",
                exc.classification,
                target.meeting_id,
                target.public_id,
                slot,
                question,
                exc,
            )
            return GenerationOutcome(
                target.meeting_id,
                "failed",
                exc.classification,
                slot,
            )
        except Exception as exc:
            logger.exception(
                "sim-query generation failed: classification=synthesis_failed "
                "meeting_id=%d public_id=%r slot=%d question=%r; "
                "prior triplet preserved",
                target.meeting_id,
                target.public_id,
                slot,
                question,
            )
            return GenerationOutcome(
                target.meeting_id,
                "failed",
                "synthesis_failed",
                slot,
            )

        if not result.citation_check_pass:
            logger.error(
                "sim-query generation failed: classification=validation_failed "
                "meeting_id=%d public_id=%r slot=%d question=%r; "
                "citation timestamps did not resolve to retrieved chunks; "
                "prior triplet preserved",
                target.meeting_id,
                target.public_id,
                slot,
                question,
            )
            return GenerationOutcome(
                target.meeting_id,
                "failed",
                "validation_failed",
                slot,
            )
        if (
            not result.retrieved_chunk_ids
            or any(
                isinstance(chunk_id, bool)
                or not isinstance(chunk_id, int)
                or chunk_id < 0
                for chunk_id in result.retrieved_chunk_ids
            )
        ):
            logger.error(
                "sim-query generation failed: classification=validation_failed "
                "meeting_id=%d public_id=%r slot=%d returned invalid "
                "retrieved_chunk_ids=%r; prior triplet preserved",
                target.meeting_id,
                target.public_id,
                slot,
                result.retrieved_chunk_ids,
            )
            return GenerationOutcome(
                target.meeting_id,
                "failed",
                "validation_failed",
                slot,
            )
        if not result.answer_text.strip():
            logger.error(
                "sim-query generation failed: classification=synthesis_failed "
                "meeting_id=%d public_id=%r slot=%d returned empty answer; "
                "prior triplet preserved",
                target.meeting_id,
                target.public_id,
                slot,
            )
            return GenerationOutcome(
                target.meeting_id,
                "failed",
                "synthesis_failed",
                slot,
            )
        results.append(result)

    run_id = str(uuid.uuid4())
    generated_at = _utc_now_z()
    storage_rows: list[tuple[object, ...]] = []
    for slot, (question, result) in enumerate(zip(questions, results)):
        storage_rows.append((
            target.meeting_id,
            slot,
            question,
            result.answer_text,
            prompt.name,
            prompt.version,
            prompt.sha256,
            SIM_QUERY_VOCAB_VERSION,
            _sha256_text(question),
            _sha256_text(result.answer_text),
            result.model_id,
            json.dumps(
                result.retrieved_chunk_ids,
                separators=(",", ":"),
            ),
            run_id,
            generated_at,
        ))

    _replace_triplet(conn, target.meeting_id, storage_rows)
    all_chunks = [result.retrieved_chunk_ids for result in results]
    models = [result.model_id for result in results]
    logger.info(
        "wrote 3 sim queries for meeting_id=%d (public_id=%r, title=%r, "
        "bucket=%s, model=%s, prompt=%s, chunks=%s)",
        target.meeting_id,
        target.public_id,
        target.title,
        bucket_for_title(target.title),
        json.dumps(models, separators=(",", ":")),
        prompt.version,
        json.dumps(all_chunks, separators=(",", ":")),
    )
    return GenerationOutcome(target.meeting_id, "written")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--meeting-id", type=int)
    target.add_argument("--all-published", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate even when a complete current triplet exists",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required before --all-published incurs Sonnet calls",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _assert_local_generation_environment()

    if args.confirm and not args.all_published:
        raise ValueError("--confirm is only valid with --all-published")
    if args.meeting_id is not None and args.meeting_id <= 0:
        raise ValueError("--meeting-id must be a positive integer")

    conn = get_connection()
    try:
        if args.all_published:
            targets = load_all_published_targets(conn)
            logger.warning(
                "DRY-RUN SUMMARY: %d published meetings to process; "
                "approximately %d Sonnet calls total",
                len(targets),
                len(targets) * 3,
            )
            if not targets:
                return 0
            if not args.confirm:
                logger.error(
                    "--all-published requires --confirm before generation; "
                    "no Sonnet calls were made"
                )
                return 2
        else:
            targets = [load_meeting_target(conn, int(args.meeting_id))]

        prompt = load_prompt_spec()
        outcomes = [
            generate_for_target(
                conn,
                target,
                prompt,
                force=bool(args.force),
            )
            for target in targets
        ]
        return 1 if any(outcome.status == "failed" for outcome in outcomes) else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

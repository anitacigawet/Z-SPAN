"""D-180 public API boundary contract (BRA-A pass 1 red-state suite)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


_CORE_PROJECT_DIR = Path(__file__).resolve().parents[3]
_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
_CLI_PROJECT_DIR = _CORE_PROJECT_DIR / "zspan_cli"
for _path in (
    _CLI_PROJECT_DIR,
    _COUNCIL_NAVIGATOR_DIR,
    _CORE_PROJECT_DIR,
    _PARSERS_DIR,
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database, public_dto as dto

sys.modules["database"] = database

import slack_listener

with tempfile.TemporaryDirectory() as _import_temp_dir:
    with (
        mock.patch.object(
            database, "DB_PATH", str(Path(_import_temp_dir) / "import.db")
        ),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server


_VISIBLE_PUBLIC_ID = "m_" + "A" * 22
_DRAFT_PUBLIC_ID = "m_" + "B" * 22
_UNAPPROVED_PUBLIC_ID = "m_" + "C" * 22
_VISIBLE_ALIAS_PUBLIC_ID = "m_" + "Y" * 22
_UNKNOWN_PUBLIC_ID = "m_" + "Z" * 22
_INTERNAL_CANARY = "BRA_A_INTERNAL_CANARY_MUST_NOT_CROSS"

_EXCLUDED_FIELDS = {
    "id",
    "meeting_id",
    "work_order_id",
    "member_id",
    "source_node_id",
    "notebook_id",
    "city_id",
    "published_by",
    "publish_notes",
    "voided_at",
    "voided_by",
    "approved_by",
    "status_updated_by",
    "raw_data",
    "scraper_source",
    "created_at",
    "updated_at",
    "prompt_filename",
    "prompt_version",
    "prompt_hash",
    "query_hash",
    "answer_digest",
    "query_slot",
    "run_id",
    "retrieved_chunk_ids",
    "vocab_version",
    "detail_internal",
    "internal_canary",
}


class PublicApiBoundaryTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.db_patch = mock.patch.object(database, "DB_PATH", str(root / "public.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.preview_patch = mock.patch.dict(
            os.environ, {"ZSPAN_PREVIEW_ROOT": str(root / "preview")}
        )
        self.preview_patch.start()
        self.addCleanup(self.preview_patch.stop)

        database.init_db()
        self._seed_database(root / "preview")

        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()
        self.owner_client = api_server.app.test_client()
        self.owner_client.set_cookie(
            api_server.SESSION_COOKIE_NAME,
            "owner-session-canary",
        )
        self.intel_patch = mock.patch.object(
            api_server,
            "_load_city_intelligence",
            return_value={
                "county": "Test County",
                "state": "Arizona",
                "primary_source_url": "https://alpha.example/council",
            },
        )
        self.intel_patch.start()
        self.addCleanup(self.intel_patch.stop)

    def _seed_database(self, preview_root: Path) -> None:
        conn = database.get_connection()
        try:
            city_id = conn.execute(
                """
                INSERT INTO cities (
                    name, county, state, last_scraped, scrape_success,
                    total_meetings
                ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, 3)
                """,
                ("Alpha", "Test County", "Arizona"),
            ).lastrowid
            conn.executemany(
                """
                INSERT INTO meetings (
                    id, public_id, city_id, city_name, county, state,
                    meeting_title, meeting_date, meeting_time,
                    meeting_location, meeting_status, agenda_url,
                    minutes_url, video_url, agenda_packet_url, ecomment_url,
                    meeting_id, summary, raw_data, scraper_source,
                    is_published, published_at, published_by, publish_notes
                ) VALUES (
                    ?, ?, ?, 'Alpha', 'Test County', 'Arizona',
                    ?, ?, '6:00 PM', 'Council Chambers',
                    'Minutes Available', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, 'canary+test@example.com', ?
                )
                """,
                [
                    (
                        101,
                        _VISIBLE_PUBLIC_ID,
                        city_id,
                        "Visible Council Meeting",
                        "2026-07-15",
                        "https://alpha.example/agenda-visible",
                        "https://alpha.example/minutes-visible",
                        "https://alpha.example/video-visible.mp4",
                        "https://alpha.example/packet-visible",
                        "https://alpha.example/comment-visible",
                        "vendor-visible-id",
                        "visible internal summary " + _INTERNAL_CANARY,
                        json.dumps({"canary": _INTERNAL_CANARY}),
                        "Canary Name canary+test@example.com",
                        1,
                        "2026-07-16 09:00:00",
                        "Canary Name",
                    ),
                    (
                        102,
                        _DRAFT_PUBLIC_ID,
                        city_id,
                        "Draft Meeting",
                        # Dates sit above the ZSPAN_PUBLIC_DISPLAY_FLOOR
                        # (2026-06-01 default) so the catalog-list tests see
                        # all three rows; the floor has its own test below.
                        "2026-07-16",
                        "https://alpha.example/agenda-draft",
                        "",
                        "https://alpha.example/video-draft.mp4",
                        "",
                        "",
                        "vendor-draft-id",
                        _INTERNAL_CANARY,
                        json.dumps({"canary": _INTERNAL_CANARY}),
                        _INTERNAL_CANARY,
                        0,
                        None,
                        _INTERNAL_CANARY,
                    ),
                    (
                        103,
                        _UNAPPROVED_PUBLIC_ID,
                        city_id,
                        "Force-Published Unapproved",
                        "2026-07-17",
                        "https://alpha.example/agenda-unapproved",
                        "",
                        "https://alpha.example/video-unapproved.mp4",
                        "",
                        "",
                        "vendor-unapproved-id",
                        _INTERNAL_CANARY,
                        json.dumps({"canary": _INTERNAL_CANARY}),
                        _INTERNAL_CANARY,
                        1,
                        "2026-07-18 09:00:00",
                        _INTERNAL_CANARY,
                    ),
                ],
            )
            conn.executemany(
                """
                INSERT INTO work_orders (
                    meeting_id, state, youtube_video_url, approved_at,
                    approved_by
                ) VALUES (?, 'completed', ?, ?, ?)
                """,
                [
                    (
                        101,
                        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        "2026-07-16 08:00:00",
                        "Canary Name",
                    ),
                    (102, "", None, None),
                    (103, "", None, None),
                ],
            )
            conn.execute(
                """
                INSERT INTO meeting_public_id_aliases (
                    alias_public_id, canonical_meeting_id
                ) VALUES (?, 101)
                """,
                (_VISIBLE_ALIAS_PUBLIC_ID,),
            )

            for meeting_id in (101, 102, 103):
                run_id = f"00000000-0000-4000-8000-{meeting_id:012d}"
                for slot in range(3):
                    question = f"Question {slot + 1} for meeting {meeting_id}?"
                    answer = (
                        "Literal <script>alert('data')</script> stays data. [00:12]"
                        if meeting_id == 101 and slot == 2
                        else f"Answer {slot + 1} for meeting {meeting_id}. [00:12]"
                    )
                    conn.execute(
                        """
                        INSERT INTO episode_sim_queries (
                            meeting_id, query_slot, question_text, answer_text,
                            prompt_name, prompt_version, prompt_hash,
                            vocab_version, query_hash, answer_digest, model_id,
                            retrieved_chunk_ids, run_id, generated_at
                        ) VALUES (?, ?, ?, ?, 'sim_query_answer',
                                  'v1-2026-07-31', ?, 'v1-2026-07-31',
                                  ?, ?, 'claude-sonnet-4-6', ?, ?, ?)
                        """,
                        (
                            meeting_id,
                            slot,
                            question,
                            answer,
                            "a" * 64,
                            hashlib.sha256(question.encode("utf-8")).hexdigest(),
                            hashlib.sha256(answer.encode("utf-8")).hexdigest(),
                            json.dumps([slot + 10]),
                            run_id,
                            "2026-07-31T20:00:00Z",
                        ),
                    )

            for meeting_id in (101, 102, 103):
                for output_type in dto.PUBLIC_BROADCAST_OUTPUT_TYPES:
                    conn.execute(
                        """
                        INSERT INTO notebook_outputs (
                            meeting_id, notebook_id, output_type, content,
                            prompt_filename, prompt_version, error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            meeting_id,
                            "internal-notebook-" + str(meeting_id),
                            output_type,
                            f"{output_type} receipt for {meeting_id}",
                            _INTERNAL_CANARY,
                            "D-SECRET-" + _INTERNAL_CANARY,
                            None,
                        ),
                    )
            conn.execute(
                """
                INSERT INTO notebook_outputs (
                    meeting_id, notebook_id, output_type, content,
                    prompt_filename, prompt_version
                ) VALUES (101, 'internal-notebook-101', 'newsletter', ?, ?, ?)
                """,
                (_INTERNAL_CANARY, _INTERNAL_CANARY, _INTERNAL_CANARY),
            )
            conn.execute(
                """
                INSERT INTO notebook_outputs (
                    meeting_id, notebook_id, output_type, content,
                    prompt_filename, prompt_version
                ) VALUES (101, 'internal-notebook-101', 'transcript_words', ?, ?, ?)
                """,
                (
                    json.dumps({
                        "words": [
                            {"word": "project", "start": 10.0, "end": 10.5},
                            {"word": "introduced", "start": 10.5, "end": 11.0},
                            {"word": "discussion", "start": 11.0, "end": 11.5},
                            {"word": "ended", "start": 11.5, "end": 12.0},
                            {"word": "motion", "start": 312.001, "end": 312.5},
                            {"word": "carried", "start": 312.5, "end": 313.0},
                        ],
                        "duration_seconds": 313,
                    }),
                    _INTERNAL_CANARY,
                    _INTERNAL_CANARY,
                ),
            )

            member_id = conn.execute(
                """
                INSERT INTO council_members (
                    city_name, name, role, seat_id, term_started, term_ends,
                    source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Alpha",
                    "Alex Public",
                    "Mayor",
                    "mayor",
                    "2025-01-01",
                    "2028-12-31",
                    "https://alpha.example/council/alex-public",
                ),
            ).lastrowid
            for meeting_id in (101, 102, 103):
                conn.execute(
                    """
                    INSERT INTO tracked_claims (
                        member_id, meeting_id, claim_type, claim_text,
                        expected_outcome, time_horizon_months, topic_tags,
                        confidence, context, word_timings, status,
                        status_updated_at, status_updated_by, status_evidence
                    ) VALUES (?, ?, 'commitment', ?, 'Complete the project',
                              12, '["water"]', 'high', 'Meeting discussion',
                              '[{"word":"will","start_ms":100,"end_ms":200}]',
                              'active', CURRENT_TIMESTAMP, ?, 'Official update')
                    """,
                    (
                        member_id,
                        meeting_id,
                        f"Alex Public made claim for meeting {meeting_id}",
                        _INTERNAL_CANARY,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO corrections (
                        meeting_id, corrected_surface, status, summary_public,
                        detail_internal
                    ) VALUES (?, 'synopsis', 'corrected', ?, ?)
                    """,
                    (
                        meeting_id,
                        f"Public correction for meeting {meeting_id}",
                        _INTERNAL_CANARY,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO live_streams (
                        city_name, state, county, channel_id, video_id,
                        video_url, title, started_at, is_live, meeting_id
                    ) VALUES ('Alpha', 'Arizona', 'Test County', ?, ?, ?, ?,
                              '2026-07-19T10:00:00Z', 1, ?)
                    """,
                    (
                        f"channel-{meeting_id}",
                        f"video-{meeting_id}",
                        f"https://youtube.com/watch?v=video-{meeting_id}",
                        f"Live meeting {meeting_id}",
                        meeting_id,
                    ),
                )
            conn.execute(
                """
                INSERT INTO users (google_sub, email, display_name)
                VALUES ('traveler-1', 'traveler@example.com', 'Traveler')
                """
            )
            conn.execute(
                """
                INSERT INTO users (google_sub, email, display_name)
                VALUES ('identity-canary', 'canary+test@example.com', 'Canary Name')
                """
            )
            visible_work_order_id = conn.execute(
                "SELECT id FROM work_orders WHERE meeting_id = 101"
            ).fetchone()["id"]
            conn.execute(
                """
                INSERT INTO quote_verifications (
                    work_order_id, meeting_id, quote_id, verified_by
                ) VALUES (?, 101, 'identity-canary-quote', 'canary+test@example.com')
                """,
                (visible_work_order_id,),
            )
            conn.commit()
        finally:
            conn.close()

        preview_root.mkdir(parents=True)
        sidecars = {
            "quotes": {
                "quotes": [
                    {
                        "speaker_name": "Alex Public",
                        "speaker_role": "Mayor",
                        "speaker_class": "council_member",
                        "quote_text": "Alex Public said we will complete it.",
                        "topic_tags": ["water"],
                        "selection_rationale": "Decision evidence",
                        "video_timestamp_seconds": 12,
                        "word_timings": [
                            {
                                "word": "We",
                                "start_ms": 12000,
                                "end_ms": 12100,
                                "start": 12.0,
                                "end": 12.1,
                            }
                        ],
                        "chunk_index": 7,
                        "news_values": [_INTERNAL_CANARY],
                        "internal_canary": _INTERNAL_CANARY,
                    }
                ],
                "quote_count": 1,
                "extraction_started": _INTERNAL_CANARY,
            },
            "decisions": {
                "prose_output": "1. Approved the project.",
                "prose_list_count": 1,
                "citation_alignment": [{
                    "output_index": 1,
                    "source": "two_part_quote",
                    "item_evidence": {
                        "matched_word_index": 0,
                        "best_candidate_end_seconds": 11.0,
                    },
                    "action_evidence": {
                        "matched_word_index": 4,
                        "best_candidate_end_seconds": 313.0,
                    },
                }],
                "decisions": [
                    {
                        "index": 1,
                        "attribution": {
                            "speaker_name": "Alex Public",
                            "speaker_role": "Mayor",
                            "speaker_class": "council_member",
                        },
                        "verbatim_spans": [
                            {
                                "text": "Approved the project",
                                "char_start": 3,
                                "char_end": 23,
                                "start_seconds": 12,
                                "chunk_index": 7,
                                "signature_id": _INTERNAL_CANARY,
                            }
                        ],
                    }
                ],
                "audit_json": [{"rationale": _INTERNAL_CANARY}],
                "elapsed_seconds": 99,
            },
            "routing": {
                "routing": [
                    {
                        "quote_index": 0,
                        "bucket": "decision_bound",
                        "decision_index": 1,
                        "rationale": _INTERNAL_CANARY,
                        "attribution": {
                            "speaker_role": "Mayor",
                            "speaker_class": "council_member",
                        },
                    }
                ],
                "summary": {"internal_canary": _INTERNAL_CANARY},
            },
            "recusals": {
                "recusal_count": 1,
                "recusals": [
                    {
                        "speaker_name": "Alex Public",
                        "speaker_role": "Mayor",
                        "speaker_class": "council_member",
                        "rationale": "Disclosed conflict",
                        "matter": "Contract award",
                        "raw_text": "I will recuse.",
                        "citation": {
                            "source": "meeting recording",
                            "chunk_index": 8,
                            "decision_index": 1,
                            "video_timestamp_seconds": 42,
                        },
                        "internal_canary": _INTERNAL_CANARY,
                    }
                ],
            },
        }
        for meeting_id in (101, 102, 103):
            for sidecar_type, payload in sidecars.items():
                suffix = "" if sidecar_type == "quotes" else f"_{sidecar_type}"
                (preview_root / f"m{meeting_id}{suffix}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

    def _get(self, path: str, *, owner: bool = False):
        client = self.owner_client if owner else self.client
        upstream = SimpleNamespace(status_code=200)
        with mock.patch.object(api_server.requests, "get", return_value=upstream):
            return client.get(path)

    def _member_id(self) -> int:
        conn = database.get_connection()
        try:
            return int(conn.execute(
                "SELECT id FROM council_members WHERE city_name = 'Alpha'"
            ).fetchone()["id"])
        finally:
            conn.close()

    def _insert_quote(self, meeting_id: int = 101) -> int:
        conn = database.get_connection()
        try:
            quote_id = conn.execute(
                """
                INSERT INTO quotes (
                    meeting_id, member_id, speaker_name, speaker_role,
                    speaker_class, quote_text, topic_tags,
                    is_broadcast_hero, word_timings, verified_status,
                    content_hash
                ) VALUES (?, ?, 'Alex Public', 'Mayor', 'council_member',
                          'Alex Public promised to complete it.', '["water"]',
                          1, '[{"word":"Alex","start_ms":100,"end_ms":200}]',
                          'verified', 'speaker-strip-test-hash')
                """,
                (meeting_id, self._member_id()),
            ).lastrowid
            conn.commit()
            return int(quote_id)
        finally:
            conn.close()

    def _require_public_route(self, path: str, *, owner: bool = False):
        response = self._get(path, owner=owner)
        self.assertNotEqual(
            response.status_code,
            404,
            f"{path} is missing; expected red state until pass 2 implements it",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def assert_exact_fields(self, value: dict, fields: tuple[str, ...]) -> None:
        self.assertIsInstance(value, dict)
        self.assertEqual(set(value), set(fields))

    def assert_no_internal_fields(self, value, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                self.assertNotIn(key, _EXCLUDED_FIELDS, f"{path}.{key}")
                if key.endswith("_id") and key not in {
                    "public_id",
                    "meeting_public_id",
                    "seat_id",
                    "channel_id",
                    "video_id",
                }:
                    self.assertNotIsInstance(nested, int, f"numeric identity at {path}.{key}")
                self.assert_no_internal_fields(nested, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, nested in enumerate(value):
                self.assert_no_internal_fields(nested, f"{path}[{index}]")
            return
        if isinstance(value, str):
            self.assertNotIn(_INTERNAL_CANARY, value, path)

    def _assert_episode_card(self, row: dict) -> None:
        self.assert_exact_fields(row, dto.PUBLIC_EPISODE_CARD_FIELDS)

    def _assert_word_timings(self, timings: list) -> None:
        for timing in timings:
            self.assert_exact_fields(timing, dto.PUBLIC_WORD_TIMING_FIELDS)

    def _assert_decision_word_timings(self, timings: list) -> None:
        for timing in timings:
            self.assert_exact_fields(
                timing, dto.PUBLIC_DECISION_WORD_TIMING_FIELDS,
            )

    def _assert_public_shapes(self, path: str, payload: dict) -> None:
        if path == "/public-api/channels/tree":
            self.assert_exact_fields(payload, dto.PUBLIC_CHANNELS_TREE_FIELDS)
            for state in payload["states"]:
                self.assert_exact_fields(state, dto.PUBLIC_CHANNEL_STATE_FIELDS)
                for source in state["statewide_sources"]:
                    self.assert_exact_fields(source, dto.PUBLIC_CHANNEL_CITY_FIELDS)
                for source in state["regional_sources"]:
                    self.assert_exact_fields(source, dto.PUBLIC_CHANNEL_CITY_FIELDS)
                for county in state["counties"]:
                    self.assert_exact_fields(county, dto.PUBLIC_CHANNEL_COUNTY_FIELDS)
                    for source in county["sources"]:
                        self.assert_exact_fields(source, dto.PUBLIC_CHANNEL_CITY_FIELDS)
                    for city in county["cities"]:
                        self.assert_exact_fields(city, dto.PUBLIC_CHANNEL_CITY_FIELDS)
        elif path.endswith("/years"):
            self.assert_exact_fields(payload, dto.PUBLIC_CITY_YEARS_FIELDS)
        elif path.startswith("/public-api/cities/"):
            self.assert_exact_fields(payload, dto.PUBLIC_CITY_MEETINGS_FIELDS)
            for row in payload["events"]:
                self._assert_episode_card(row)
        elif path.startswith("/public-api/calendar/county/"):
            self.assert_exact_fields(payload, dto.PUBLIC_COUNTY_MEETINGS_FIELDS)
            for rows in payload["cities"].values():
                for row in rows:
                    self.assert_exact_fields(row, dto.PUBLIC_COUNTY_MEETING_FIELDS)
        elif path.startswith("/public-api/calendar/search"):
            self.assert_exact_fields(payload, dto.PUBLIC_SEARCH_FIELDS)
            for row in payload["results"]:
                self.assert_exact_fields(row, dto.PUBLIC_SEARCH_RESULT_FIELDS)
        elif path == "/public-api/calendar/stats":
            self.assert_exact_fields(payload, dto.PUBLIC_CALENDAR_STATS_FIELDS)
            for row in payload["top_cities"]:
                self.assert_exact_fields(row, dto.PUBLIC_CALENDAR_TOP_CITY_FIELDS)
        elif path == "/public-api/health":
            self.assert_exact_fields(payload, dto.PUBLIC_HEALTH_FIELDS)
        elif path == f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}":
            self.assert_exact_fields(payload, dto.PUBLIC_BROADCAST_FIELDS)
            self.assert_exact_fields(
                payload["completeness"], dto.PUBLIC_BROADCAST_COMPLETENESS_FIELDS
            )
            self.assertEqual(
                set(payload["outputs"]),
                set(dto.PUBLIC_BROADCAST_OUTPUT_TYPES) - {"synopsis"},
            )
            for output in payload["outputs"].values():
                self.assert_exact_fields(output, dto.PUBLIC_BROADCAST_OUTPUT_FIELDS)
        elif path.endswith("/sim-queries"):
            self.assert_exact_fields(payload, dto.PUBLIC_SIM_QUERIES_FIELDS)
            for item in payload["sim_queries"]:
                self.assert_exact_fields(item, dto.PUBLIC_SIM_QUERY_FIELDS)
        elif "/sidecars/quotes" in path:
            self.assert_exact_fields(payload, dto.PUBLIC_QUOTES_SIDECAR_FIELDS)
            for quote in payload["quotes"]:
                self.assert_exact_fields(quote, dto.PUBLIC_QUOTE_FIELDS)
                self._assert_word_timings(quote["word_timings"])
        elif "/sidecars/decisions" in path:
            self.assert_exact_fields(payload, dto.PUBLIC_DECISIONS_SIDECAR_FIELDS)
            for decision in payload["decisions"]:
                self.assert_exact_fields(decision, dto.PUBLIC_DECISION_FIELDS)
                for span in decision["verbatim_spans"]:
                    self.assert_exact_fields(span, dto.PUBLIC_DECISION_SPAN_FIELDS)
                    self._assert_decision_word_timings(span["word_timings"])
        elif "/sidecars/routing" in path:
            self.assert_exact_fields(payload, dto.PUBLIC_ROUTING_SIDECAR_FIELDS)
            for entry in payload["routing"]:
                self.assert_exact_fields(entry, dto.PUBLIC_ROUTING_ENTRY_FIELDS)
        elif "/sidecars/recusals" in path:
            self.assert_exact_fields(payload, dto.PUBLIC_RECUSALS_SIDECAR_FIELDS)
            for event in payload["recusals"]:
                self.assert_exact_fields(event, dto.PUBLIC_RECUSAL_FIELDS)
                self.assert_exact_fields(
                    event["citation"], dto.PUBLIC_RECUSAL_CITATION_FIELDS
                )
        elif path.endswith("/citation"):
            self._assert_citation(payload)
        elif path == "/public-api/cast/Alpha":
            self.assert_exact_fields(payload, dto.PUBLIC_CAST_ROSTER_FIELDS)
            for member in payload["members"]:
                self.assert_exact_fields(member, dto.PUBLIC_CAST_MEMBER_FIELDS)
        elif path == "/public-api/cast/Alpha/mayor":
            self.assert_exact_fields(payload, dto.PUBLIC_CAST_SEAT_FIELDS)
            self.assert_exact_fields(payload["member"], dto.PUBLIC_CAST_MEMBER_FIELDS)
        elif path == "/public-api/guide":
            self.assert_exact_fields(payload, dto.PUBLIC_GUIDE_FIELDS)
            for stream in payload["live"]:
                self.assert_exact_fields(stream, dto.PUBLIC_GUIDE_STREAM_FIELDS)
        elif path == "/public-api/travelers":
            self.assert_exact_fields(payload, dto.PUBLIC_TRAVELERS_FIELDS)
        elif path.startswith("/public-api/youtube/embed-check"):
            self.assert_exact_fields(payload, dto.PUBLIC_YOUTUBE_EMBED_FIELDS)
        else:
            self.fail(f"no DTO assertion registered for {path}")

    def _assert_citation(self, payload: dict) -> None:
        self.assert_exact_fields(payload, dto.PUBLIC_CITATION_RESPONSE_FIELDS)
        citation = payload["citation"]
        self.assertNotIn("tracked_claims", citation)
        self.assert_exact_fields(citation, dto.PUBLIC_CITATION_FIELDS)
        self.assert_exact_fields(citation["meeting"], dto.PUBLIC_CITATION_MEETING_FIELDS)
        self.assert_exact_fields(
            citation["publication"], dto.PUBLIC_CITATION_PUBLICATION_FIELDS
        )
        self.assert_exact_fields(citation["sources"], dto.PUBLIC_CITATION_SOURCES_FIELDS)
        self.assert_exact_fields(
            citation["sources"]["primary_video"],
            dto.PUBLIC_CITATION_PRIMARY_VIDEO_FIELDS,
        )
        self.assert_exact_fields(
            citation["transcription"], dto.PUBLIC_CITATION_TRANSCRIPTION_FIELDS
        )
        self.assert_exact_fields(citation["extraction"], dto.PUBLIC_CITATION_EXTRACTION_FIELDS)
        for output in citation["extraction"]["outputs"]:
            self.assert_exact_fields(output, dto.PUBLIC_CITATION_EXTRACTION_OUTPUT_FIELDS)
            self.assertIn(output["output_type"], dto.PUBLIC_BROADCAST_OUTPUT_TYPES)
        self.assert_exact_fields(
            citation["verification"], dto.PUBLIC_CITATION_VERIFICATION_FIELDS
        )
        self.assert_exact_fields(
            citation["verification"]["member_quotes"],
            dto.PUBLIC_CITATION_COUNT_SUMMARY_FIELDS,
        )
        self.assert_exact_fields(
            citation["corrections"], dto.PUBLIC_CITATION_CORRECTIONS_FIELDS
        )
        for entry in citation["corrections"]["corrections_dictionary"]:
            self.assert_exact_fields(entry, dto.PUBLIC_CITATION_DICTIONARY_ENTRY_FIELDS)
        self.assert_exact_fields(
            citation["human_review"], dto.PUBLIC_CITATION_HUMAN_REVIEW_FIELDS
        )

    def test_every_public_api_route_has_an_exact_recursive_dto(self):
        paths = [
            "/public-api/channels/tree",
            "/public-api/cities/Alpha/years",
            "/public-api/cities/Alpha/meetings?year=2026",
            "/public-api/calendar/county/Test%20County/meetings?state=Arizona",
            "/public-api/calendar/search?q=Visible&limit=10&offset=0",
            "/public-api/calendar/stats",
            "/public-api/health",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/quotes",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/decisions",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/routing",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/recusals",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/citation",
            "/public-api/cast/Alpha",
            "/public-api/cast/Alpha/mayor",
            "/public-api/guide",
            "/public-api/travelers",
            "/public-api/youtube/embed-check?video_id=dQw4w9WgXcQ",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self._require_public_route(path)
                payload = response.get_json()
                self._assert_public_shapes(path, payload)
                self.assert_no_internal_fields(payload)

    def test_all_public_routes_reject_identity_canaries(self):
        paths = (
            "/public-api/channels/tree",
            "/public-api/cities/Alpha/years",
            "/public-api/cities/Alpha/meetings?year=2026",
            "/public-api/calendar/county/Test%20County/meetings?state=Arizona",
            "/public-api/calendar/search?q=Visible&limit=10&offset=0",
            "/public-api/calendar/stats",
            "/public-api/health",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/quotes",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/decisions",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/routing",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/recusals",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/citation",
            "/public-api/cast/Alpha",
            "/public-api/cast/Alpha/mayor",
            "/public-api/guide",
            "/public-api/travelers",
            "/public-api/youtube/embed-check?video_id=dQw4w9WgXcQ",
            "/v1/catalog/jurisdictions",
            "/v1/catalog/meetings?city=Alpha",
            f"/v1/catalog/meetings/{_VISIBLE_PUBLIC_ID}",
        )
        self.assertEqual(len(paths), 22)
        forbidden = ("@", "canary", "jj" + "workaz", "Ja" + "mes Jones")
        for path in paths:
            with self.subTest(path=path):
                response = self._require_public_route(path)
                serialized = json.dumps(response.get_json(), sort_keys=True).casefold()
                for token in forbidden:
                    self.assertNotIn(token.casefold(), serialized)

        citation = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/citation"
        ).get_json()["citation"]
        self.assertNotIn("published_by", citation["publication"])
        self.assertNotIn("reviewer", citation["human_review"])

    def test_public_sim_queries_ready_projection_is_ordered_and_sealed(self):
        payload = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries"
        ).get_json()

        self.assertEqual(payload["public_id"], _VISIBLE_PUBLIC_ID)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(
            [item["question"] for item in payload["sim_queries"]],
            [
                "Question 1 for meeting 101?",
                "Question 2 for meeting 101?",
                "Question 3 for meeting 101?",
            ],
        )
        self.assertIn("<script>alert('data')</script>", payload["sim_queries"][2]["answer"])
        self._assert_public_shapes(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries",
            payload,
        )
        self.assert_no_internal_fields(payload)

    def test_public_sim_queries_no_rows_is_explicit_not_generated(self):
        conn = database.get_connection()
        try:
            conn.execute(
                "DELETE FROM episode_sim_queries WHERE meeting_id = 101"
            )
            conn.commit()
        finally:
            conn.close()

        payload = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries"
        ).get_json()
        self.assertEqual(payload, {
            "public_id": _VISIBLE_PUBLIC_ID,
            "status": "not_generated",
            "sim_queries": [],
        })

    def test_public_sim_queries_partial_storage_fails_closed(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """
                DELETE FROM episode_sim_queries
                WHERE meeting_id = 101 AND query_slot = 2
                """
            )
            conn.commit()
        finally:
            conn.close()

        response = self._get(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries"
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "sim_queries_corrupt"})

    def test_public_sim_queries_corrupt_provenance_fails_closed(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE episode_sim_queries SET answer_digest = 'not-a-digest'
                WHERE meeting_id = 101 AND query_slot = 1
                """
            )
            conn.commit()
        finally:
            conn.close()

        response = self._get(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries"
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "sim_queries_corrupt"})

    def test_public_sim_queries_date_only_generated_at_fails_closed(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE episode_sim_queries SET generated_at = '2026-07-31Z'
                WHERE meeting_id = 101
                """
            )
            conn.commit()
        finally:
            conn.close()

        response = self._get(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries"
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "sim_queries_corrupt"})

    def test_public_sim_queries_duplicate_result_fails_closed(self):
        conn = database.get_connection()
        try:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT query_slot, question_text, answer_text, prompt_name,
                       prompt_version, prompt_hash, vocab_version, query_hash,
                       answer_digest, model_id, retrieved_chunk_ids, run_id,
                       generated_at
                FROM episode_sim_queries
                WHERE meeting_id = 101 ORDER BY query_slot
                """
            ).fetchall()]
        finally:
            conn.close()
        fake_conn = mock.Mock()
        fake_conn.execute.return_value.fetchall.return_value = [*rows, rows[0]]

        with mock.patch.object(
            api_server,
            "get_connection",
            return_value=fake_conn,
        ):
            response = self._get(
                f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries"
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "sim_queries_corrupt"})
        fake_conn.close.assert_called_once_with()

    def test_public_sim_queries_alias_returns_canonical_public_id(self):
        payload = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_ALIAS_PUBLIC_ID}/sim-queries"
        ).get_json()
        self.assertEqual(payload["public_id"], _VISIBLE_PUBLIC_ID)
        self.assertEqual(payload["status"], "ready")

    def test_public_sim_queries_hidden_id_failures_are_identical(self):
        public_ids = (
            "malformed",
            "101",
            _UNKNOWN_PUBLIC_ID,
            _DRAFT_PUBLIC_ID,
            _UNAPPROVED_PUBLIC_ID,
        )
        responses = [
            self._get(f"/public-api/broadcasts/{public_id}/sim-queries")
            for public_id in public_ids
        ]
        for response in responses:
            self.assertEqual(response.status_code, 404)
        self.assertEqual(
            [response.get_json() for response in responses],
            [responses[0].get_json()] * len(responses),
        )

    def test_public_broadcast_uses_verified_decisions_sidecar(self):
        payload = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}"
        ).get_json()

        self.assertEqual(
            payload["outputs"]["key_decisions"]["content"],
            "1. Approved the project.",
        )
        self.assertNotEqual(
            payload["outputs"]["key_decisions"]["content"],
            "key_decisions receipt for 101",
        )

    def test_synopsis_display_cut_is_public_only(self):
        public_response = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}"
        )
        with mock.patch.object(
            api_server,
            "_require_owner",
            return_value=(SimpleNamespace(id=1, email="owner@example.com"), None),
        ):
            owner_response = self.owner_client.get("/api/notebook/101")

        self.assertEqual(
            owner_response.status_code,
            200,
            owner_response.get_data(as_text=True),
        )
        public_outputs = public_response.get_json()["outputs"]
        owner_outputs = owner_response.get_json()["outputs"]

        self.assertNotIn("synopsis", public_outputs)
        self.assertEqual(
            owner_outputs["synopsis"]["content"],
            "synopsis receipt for 101",
        )
        self.assertEqual(
            public_outputs["key_decisions"]["content"],
            "1. Approved the project.",
        )
        self.assertEqual(
            owner_outputs["key_decisions"]["content"],
            "key_decisions receipt for 101",
        )
        for outputs in (public_outputs, owner_outputs):
            self.assertEqual(
                outputs["episode_tagline"]["content"],
                "episode_tagline receipt for 101",
            )

    def test_void_and_restore_one_generation_across_public_operator_doors(self):
        conn = database.get_connection()
        try:
            owner = conn.execute(
                """
                SELECT id, email FROM users
                WHERE email = 'canary+test@example.com'
                """
            ).fetchone()
        finally:
            conn.close()
        actor = SimpleNamespace(id=owner["id"], email=owner["email"])
        readiness_before = database.check_publish_readiness(101)
        public_completeness_before = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}"
        ).get_json()["completeness"]
        route = "/api/notebook/101/outputs/key_decisions"

        with mock.patch.object(
            api_server, "_require_owner", return_value=(actor, None)
        ):
            voided = self.owner_client.post(f"{route}/void", json={})
            repeated = self.owner_client.post(f"{route}/void", json={})
            operator = self.owner_client.get("/api/notebook/101")

        self.assertEqual(voided.status_code, 200, voided.get_data(as_text=True))
        self.assertTrue(voided.get_json()["changed"])
        self.assertEqual(voided.get_json()["state"], "voided")
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.get_json()["changed"])
        self.assertIn("already voided", repeated.get_json()["message"])

        public = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}"
        ).get_json()
        self.assertNotIn("key_decisions", public["outputs"])
        self.assertEqual(
            public["completeness"],
            public_completeness_before,
        )
        self.assertEqual(
            self._get(
                f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/decisions"
            ).status_code,
            404,
        )
        citation = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/citation"
        ).get_json()["citation"]
        self.assertNotIn(
            "key_decisions",
            {
                output["output_type"]
                for output in citation["extraction"]["outputs"]
            },
        )

        self.assertEqual(operator.status_code, 200)
        operator_output = operator.get_json()["outputs"]["key_decisions"]
        self.assertIsNotNone(operator_output["voided_at"])
        self.assertEqual(operator_output["voided_by"], actor.email)
        self.assertEqual(
            operator_output["content"],
            "key_decisions receipt for 101",
        )
        readiness_voided = database.check_publish_readiness(101)
        self.assertEqual(
            readiness_voided["publishable"],
            readiness_before["publishable"],
        )
        self.assertEqual(
            readiness_voided["missing_outputs"],
            readiness_before["missing_outputs"],
        )

        with mock.patch.object(
            api_server, "_require_owner", return_value=(actor, None)
        ):
            restored = self.owner_client.post(f"{route}/restore", json={})
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.get_json()["changed"])
        self.assertEqual(restored.get_json()["state"], "live")

        public_restored = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}"
        ).get_json()
        self.assertIn("key_decisions", public_restored["outputs"])
        self.assertEqual(
            self._get(
                f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/decisions"
            ).status_code,
            200,
        )

        conn = database.get_connection()
        try:
            output = conn.execute(
                """
                SELECT voided_at, voided_by FROM notebook_outputs
                WHERE meeting_id = 101 AND output_type = 'key_decisions'
                """
            ).fetchone()
            events = conn.execute(
                """
                SELECT action, output_type, actor_user_id
                FROM operator_review_events
                WHERE meeting_id = 101
                ORDER BY id
                """
            ).fetchall()
        finally:
            conn.close()
        self.assertIsNone(output["voided_at"])
        self.assertIsNone(output["voided_by"])
        self.assertEqual(
            [(row["action"], row["output_type"]) for row in events],
            [
                ("void", "key_decisions"),
                ("void", "key_decisions"),
                ("restore", "key_decisions"),
            ],
        )
        self.assertTrue(all(row["actor_user_id"] == actor.id for row in events))

    def test_void_restore_endpoints_require_owner_and_reject_unknown_type(self):
        for action in ("void", "restore"):
            with self.subTest(action=action):
                response = self.client.post(
                    f"/api/notebook/101/outputs/synopsis/{action}",
                    json={},
                )
                self.assertEqual(response.status_code, 401)

        actor = SimpleNamespace(id=1, email="owner@example.com")
        with mock.patch.object(
            api_server, "_require_owner", return_value=(actor, None)
        ):
            unknown = self.owner_client.post(
                "/api/notebook/101/outputs/not_a_real_generation/void",
                json={},
            )
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.get_json()["error"], "unknown output type")

    def test_broadcast_endpoints_attach_exact_ccta_karaoke_timings(self):
        calls = [
            {
                "speaker_name": "Alex Public",
                "speaker_role": "Resident",
                "quote_text": "project introduced discussion ended",
                "video_timestamp_seconds": 10.0,
                "chunk_index": 1,
            },
            {
                "speaker_name": "Alex Public",
                "speaker_role": "Resident",
                "quote_text": "words absent from the transcript",
                "video_timestamp_seconds": 20.0,
                "chunk_index": 2,
            },
        ]
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE notebook_outputs
                SET content = ?
                WHERE meeting_id = 101
                  AND output_type = 'community_calls_to_action'
                """,
                (f"```json\n{json.dumps(calls)}\n```",),
            )
            conn.commit()
        finally:
            conn.close()

        public_payload = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}"
        ).get_json()
        with mock.patch.object(
            api_server, "_require_owner", return_value=({"id": 1}, None)
        ):
            operator_response = self._get("/api/notebook/101", owner=True)
        self.assertEqual(operator_response.status_code, 200)
        operator_payload = operator_response.get_json()

        expected = [
            [
                {"word": "project", "start_ms": 10000, "end_ms": 10500},
                {"word": "introduced", "start_ms": 10500, "end_ms": 11000},
                {"word": "discussion", "start_ms": 11000, "end_ms": 11500},
                {"word": "ended", "start_ms": 11500, "end_ms": 12000},
            ],
            [],
        ]
        for payload in (public_payload, operator_payload):
            self.assertEqual(
                payload["outputs"]["community_calls_to_action"][
                    "karaoke_word_timings"
                ],
                expected,
            )

    def test_broadcast_endpoints_genericize_stored_speaker_attribution(self):
        stored = {
            "quotes": [
                {
                    "speaker_name": "Ken Watkins",
                    "speaker_role": "Mayor",
                    "member_id": 42,
                    "quote_text": "Verbatim public comment.",
                    "nested": {
                        "speaker": "Another Person",
                        "speaker_title": "Resident",
                        "speaker_id": "SPEAKER_03",
                    },
                }
            ]
        }
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE notebook_outputs
                SET content = ?
                WHERE meeting_id = 101
                  AND output_type = 'community_calls_to_action'
                """,
                (f"```json\n{json.dumps(stored)}\n```",),
            )
            conn.commit()
        finally:
            conn.close()

        public_payload = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}"
        ).get_json()
        with mock.patch.object(
            api_server, "_require_owner", return_value=({"id": 1}, None)
        ):
            operator_response = self._get("/api/notebook/101", owner=True)
        self.assertEqual(operator_response.status_code, 200)
        operator_payload = operator_response.get_json()

        for payload in (public_payload, operator_payload):
            content = payload["outputs"]["community_calls_to_action"]["content"]
            quote = json.loads(content)["quotes"][0]
            self.assertEqual(quote["speaker_name"], "Speaker")
            self.assertEqual(quote["speaker_role"], "")
            self.assertIsNone(quote["member_id"])
            self.assertEqual(quote["nested"]["speaker"], "Speaker")
            self.assertEqual(quote["nested"]["speaker_title"], "")
            self.assertIsNone(quote["nested"]["speaker_id"])
            self.assertNotIn("Ken Watkins", content)
            self.assertNotIn("Another Person", content)

        conn = database.get_connection()
        try:
            stored_content = conn.execute(
                """
                SELECT content FROM notebook_outputs
                WHERE meeting_id = 101
                  AND output_type = 'community_calls_to_action'
                """
            ).fetchone()["content"]
        finally:
            conn.close()
        self.assertIn("Ken Watkins", stored_content)
        self.assertIn("Another Person", stored_content)

    def test_genericizer_is_attribution_scoped_and_preserves_prose(self):
        role_only = {
            "speaker_role": "Mayor",
            "speaker_class": "council_member",
            "word_timings": [{"word": "Alex", "start_ms": 100}],
            "claim_text": "Alex Public promised to finish the project.",
        }
        genericized = api_server._genericize_speaker_attribution(role_only)
        self.assertEqual(genericized["speaker_role"], "")
        self.assertEqual(genericized["speaker_class"], "council_member")
        self.assertEqual(genericized["word_timings"], role_only["word_timings"])
        self.assertEqual(genericized["claim_text"], role_only["claim_text"])
        self.assertEqual(role_only["speaker_role"], "Mayor")

        alias_only = {
            "canonical_name": "Alex Public",
            "content": "Alex Public spoke during discussion.",
        }
        self.assertEqual(
            api_server._genericize_speaker_attribution(alias_only), alias_only
        )

        prose = "  Alex Public said the name Jordan Lee in discussion.\n"
        self.assertEqual(
            api_server._genericize_speaker_attribution_in_content(prose), prose
        )

    def test_sidecar_doors_genericize_without_mutating_preview_files(self):
        preview_root = Path(os.environ["ZSPAN_PREVIEW_ROOT"])
        paths = {
            "quotes": preview_root / "m101.json",
            "decisions": preview_root / "m101_decisions.json",
            "routing": preview_root / "m101_routing.json",
            "recusals": preview_root / "m101_recusals.json",
        }
        before = {name: path.read_bytes() for name, path in paths.items()}

        with mock.patch.object(
            api_server, "_require_owner", return_value=({"id": 1}, None)
        ):
            owner_payloads = {
                output_type: self._get(
                    f"/api/preview/{output_type}/101", owner=True
                ).get_json()
                for output_type in paths
            }

        owner_quote = owner_payloads["quotes"]["quotes"][0]
        self.assertEqual(owner_quote["speaker_name"], "Speaker")
        self.assertEqual(owner_quote["speaker_role"], "")
        self.assertEqual(owner_quote["speaker_class"], "council_member")
        self.assertEqual(owner_quote["word_timings"][0]["word"], "We")
        self.assertIn("Alex Public", owner_quote["quote_text"])
        self.assertEqual(
            owner_payloads["decisions"]["decisions"][0]["attribution"]["speaker_name"],
            "Speaker",
        )
        self.assertEqual(
            owner_payloads["routing"]["routing"][0]["attribution"]["speaker_role"],
            "",
        )
        self.assertEqual(
            owner_payloads["recusals"]["recusals"][0]["speaker_name"],
            "Speaker",
        )

        public_payloads = {
            output_type: self._require_public_route(
                f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/{output_type}"
            ).get_json()
            for output_type in paths
        }
        public_quote = public_payloads["quotes"]["quotes"][0]
        self.assertEqual(public_quote["speaker_name"], "Speaker")
        self.assertEqual(public_quote["speaker_role"], "")
        self.assertEqual(public_quote["speaker_class"], "council_member")
        self.assertEqual(public_quote["word_timings"][0]["word"], "We")
        self.assertIn("Alex Public", public_quote["quote_text"])
        public_recusal = public_payloads["recusals"]["recusals"][0]
        self.assertEqual(public_recusal["speaker_name"], "Speaker")
        self.assertEqual(public_recusal["speaker_role"], "")
        owner_spans = owner_payloads["decisions"]["decisions"][0]["verbatim_spans"]
        public_spans = public_payloads["decisions"]["decisions"][0]["verbatim_spans"]
        self.assertEqual(len(owner_spans), 2)
        self.assertEqual(
            [{field: span.get(field) for field in dto.PUBLIC_DECISION_SPAN_FIELDS}
             for span in owner_spans],
            public_spans,
        )
        self.assertEqual(
            public_spans[0]["omission_marker"],
            "[Transcript omitted between verbatim passages: "
            "2 words · 00:05:01.001 elapsed]",
        )
        self.assertEqual(
            owner_spans[0]["word_timings"],
            [
                {"word": "project", "start": 10.0, "end": 10.5},
                {"word": "introduced", "start": 10.5, "end": 11.0},
            ],
        )
        self.assertEqual(public_spans[0]["word_timings"], owner_spans[0]["word_timings"])
        self._assert_decision_word_timings(public_spans[0]["word_timings"])
        self.assertNotIn("start_word_index", public_spans[0])
        self.assertNotIn("end_word_index", public_spans[0])
        for timing in public_spans[0]["word_timings"]:
            self.assertNotIn("start_word_index", timing)
            self.assertNotIn("end_word_index", timing)

        after = {name: path.read_bytes() for name, path in paths.items()}
        self.assertEqual(after, before)

    def test_quotes_meeting_genericizes_canonical_and_legacy_branches(self):
        self._insert_quote(101)
        legacy_content = json.dumps({
            "quotes": [{
                "speaker_name": "Jordan Lee",
                "speaker_role": "City Manager",
                "text": "Jordan Lee described the budget.",
                "word_timings": [
                    {"word": "Jordan", "start_ms": 2000, "end_ms": 2200}
                ],
            }]
        })
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO notebook_outputs (
                    meeting_id, notebook_id, output_type, content
                ) VALUES (102, 'legacy-test-notebook', 'council_quotes', ?)
                """,
                (legacy_content,),
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.object(
            api_server, "_require_owner", return_value=({"id": 1}, None)
        ):
            canonical = self._get(
                "/api/quotes/meeting/101", owner=True
            ).get_json()
            legacy = self._get(
                "/api/quotes/meeting/102", owner=True
            ).get_json()

        self.assertEqual(canonical["source"], "quotes_table")
        canonical_quote = canonical["quotes"][0]
        self.assertEqual(canonical_quote["speaker_name"], "Speaker")
        self.assertEqual(canonical_quote["speaker_role"], "")
        self.assertIsNone(canonical_quote["member_id"])
        self.assertEqual(canonical_quote["speaker_class"], "council_member")
        self.assertEqual(canonical_quote["word_timings"][0]["word"], "Alex")
        self.assertIn("Alex Public", canonical_quote["quote_text"])

        self.assertEqual(legacy["source"], "council_quotes_legacy")
        legacy_quote = legacy["quotes"][0]
        self.assertEqual(legacy_quote["speaker_name"], "Speaker")
        self.assertEqual(legacy_quote["speaker_role"], "")
        self.assertEqual(legacy_quote["speaker_class"], "staff")
        self.assertEqual(legacy_quote["word_timings"][0]["word"], "Jordan")
        self.assertIn("Jordan Lee", legacy_quote["quote_text"])

        conn = database.get_connection()
        try:
            stored_quote = dict(conn.execute(
                "SELECT speaker_name, speaker_role FROM quotes WHERE meeting_id = 101"
            ).fetchone())
            stored_legacy = conn.execute(
                "SELECT content FROM notebook_outputs "
                "WHERE meeting_id = 102 AND output_type = 'council_quotes'"
            ).fetchone()["content"]
        finally:
            conn.close()
        self.assertEqual(stored_quote, {
            "speaker_name": "Alex Public", "speaker_role": "Mayor",
        })
        self.assertEqual(stored_legacy, legacy_content)

    def test_compiler_and_owner_ledger_genericize_attribution_not_claim_prose(self):
        member_id = self._member_id()
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO transcript_nodes (
                    meeting_id, ordinal, speaker_id, speaker_name,
                    transcript_span_text, node_type, typed_fields,
                    parser_model, parser_confidence
                ) VALUES (101, 1, ?, 'Alex Public',
                          'Alex Public introduced the motion.', 'Motion', ?,
                          'test:model', 0.99)
                """,
                (
                    member_id,
                    json.dumps({
                        "speaker_name": "Alex Public",
                        "speaker_title": "Mayor",
                        "member_id": member_id,
                        "motion_text": "Alex Public moved approval.",
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.object(
            api_server, "_require_owner", return_value=({"id": 1}, None)
        ):
            compiler = self._get("/api/compiler/101", owner=True).get_json()
            owner_ledger = self._get("/api/ledger/Alpha", owner=True).get_json()

        claim = compiler["claims"][0]
        self.assertEqual(claim["speaker_name"], "Speaker")
        self.assertEqual(claim["speaker_title"], "")
        self.assertIsNone(claim["member_id"])
        self.assertIn("Alex Public", claim["claim_text"])
        node = compiler["nodes"][0]
        self.assertEqual(node["speaker_name"], "Speaker")
        self.assertEqual(node["speaker_title"], "")
        self.assertIsNone(node["speaker_id"])
        self.assertEqual(node["typed_fields"]["speaker_name"], "Speaker")
        self.assertEqual(node["typed_fields"]["speaker_title"], "")
        self.assertIsNone(node["typed_fields"]["member_id"])
        self.assertIn("Alex Public", node["typed_fields"]["motion_text"])
        self.assertIn("Alex Public", node["transcript_span_text"])

        ledger_claim = owner_ledger["tracked_claims"][0]
        self.assertEqual(ledger_claim["speaker_name"], "Speaker")
        self.assertEqual(ledger_claim["speaker_role"], "")
        self.assertIn("Alex Public", ledger_claim["claim_text"])
        self.assertIsNone(ledger_claim["member_id"])
        self.assertIsNone(ledger_claim["seat_id"])

    def test_seat_routes_preserve_member_and_genericize_nested_attribution(self):
        self._insert_quote(101)
        with mock.patch.object(
            api_server, "_require_owner", return_value=({"id": 1}, None)
        ):
            cast = self._get("/api/cast/Alpha/mayor", owner=True).get_json()

        self.assertEqual(cast["member"]["name"], "Alex Public")
        self.assertEqual(cast["member"]["role"], "Mayor")
        self.assertEqual(cast["member"]["seat_id"], "mayor")
        self.assertEqual(cast["quotes"][0]["speaker_role"], "")
        self.assertIn("Alex Public", cast["quotes"][0]["quote_text"])

        truth_book = {
            "member": {
                "id": self._member_id(),
                "name": "Alex Public",
                "role": "Mayor",
                "seat_id": "mayor",
            },
            "time_range": {"earliest": "2026-07-15", "latest": "2026-07-15"},
            "lanes": [{
                "topic": "water",
                "label": "Water",
                "entries": [{
                    "speaker_name": "Alex Public",
                    "speaker_role": "Mayor",
                    "member_id": self._member_id(),
                    "text": "Alex Public discussed the project.",
                }],
            }],
            "claims": [{
                "speaker_name": "Alex Public",
                "speaker_role": "Mayor",
                "member_id": self._member_id(),
                "claim_text": "Alex Public promised completion.",
            }],
        }
        with (
            mock.patch.object(
                api_server, "_require_owner", return_value=({"id": 1}, None)
            ),
            mock.patch.object(
                database, "get_truth_book_for_member", return_value=truth_book
            ),
        ):
            truth = self._get(
                "/api/truth-book/Alpha/mayor", owner=True
            ).get_json()

        self.assertEqual(truth["member"]["name"], "Alex Public")
        self.assertEqual(truth["member"]["role"], "Mayor")
        self.assertEqual(truth["member"]["seat_id"], "mayor")
        quote = truth["lanes"][0]["entries"][0]
        claim = truth["claims"][0]
        for attribution in (quote, claim):
            self.assertEqual(attribution["speaker_name"], "Speaker")
            self.assertEqual(attribution["speaker_role"], "")
            self.assertIsNone(attribution["member_id"])
        self.assertIn("Alex Public", quote["text"])
        self.assertIn("Alex Public", claim["claim_text"])

    def test_public_broadcast_omits_key_decisions_without_sidecar(self):
        preview_root = Path(os.environ["ZSPAN_PREVIEW_ROOT"])
        (preview_root / "m101_decisions.json").unlink()

        payload = self._require_public_route(
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}"
        ).get_json()

        self.assertNotIn("key_decisions", payload["outputs"])
        self.assertFalse(payload["completeness"]["complete"])
        self.assertEqual(
            set(payload["outputs"]),
            set(dto.PUBLIC_BROADCAST_OUTPUT_TYPES) - {"synopsis", "key_decisions"},
        )

    def test_public_completeness_uses_the_publishable_citation_gate(self):
        verdict = {
            "ready": True,
            "publishable": False,
            "required_ok": 4,
            "required_total": 4,
            "missing_outputs": [],
            "reasons": [],
            "publish_blockers": ["Citation coverage is incomplete."],
        }
        with mock.patch.object(
            database,
            "check_publish_readiness",
            return_value=verdict,
        ):
            payload = self._require_public_route(
                f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}"
            ).get_json()

        self.assertFalse(payload["completeness"]["complete"])
        self.assertEqual(payload["completeness"]["required_ok"], 4)
        self.assertEqual(payload["completeness"]["required_total"], 4)

    def test_visibility_gate_allows_only_published_and_approved_meeting(self):
        collection_paths = (
            "/public-api/cities/Alpha/meetings?year=all",
            "/public-api/calendar/county/Test%20County/meetings?state=Arizona",
            "/public-api/calendar/search?q=Meeting&limit=100&offset=0",
            "/public-api/guide",
        )
        for path in collection_paths:
            with self.subTest(path=path):
                response = self._require_public_route(path)
                encoded = json.dumps(response.get_json(), sort_keys=True)
                self.assertIn(_VISIBLE_PUBLIC_ID, encoded)
                self.assertNotIn(_DRAFT_PUBLIC_ID, encoded)
                self.assertNotIn(_UNAPPROVED_PUBLIC_ID, encoded)
                self.assertNotIn("Draft Meeting", encoded)
                self.assertNotIn("Force-Published Unapproved", encoded)

        years = self._require_public_route("/public-api/cities/Alpha/years").get_json()
        self.assertEqual(years["years"], ["2026"])
        stats = self._require_public_route("/public-api/calendar/stats").get_json()
        self.assertEqual(stats["total_meetings"], 1)
        tree = self._require_public_route("/public-api/channels/tree").get_json()
        alpha = next(
            city
            for state in tree["states"]
            for county in state["counties"]
            for city in county["cities"]
            if city["name"] == "Alpha"
        )
        self.assertEqual(alpha["meeting_count"], 1)

        visible_paths = (
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/quotes",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/citation",
        )
        for path in visible_paths:
            with self.subTest(path=path):
                self.assertEqual(self._require_public_route(path).status_code, 200)

        for public_id in (_DRAFT_PUBLIC_ID, _UNAPPROVED_PUBLIC_ID):
            for suffix in ("", "/sim-queries", "/sidecars/quotes", "/citation"):
                with self.subTest(public_id=public_id, suffix=suffix):
                    response = self._get(f"/public-api/broadcasts/{public_id}{suffix}")
                    self.assertEqual(response.status_code, 404)

    def test_public_id_is_the_only_public_meeting_route_identity(self):
        for path in (
            "/public-api/broadcasts/101",
            "/public-api/broadcasts/101/sim-queries",
            "/public-api/broadcasts/101/sidecars/quotes",
            "/public-api/broadcasts/101/citation",
        ):
            with self.subTest(path=path):
                self.assertEqual(self._get(path).status_code, 404)

    def test_public_routes_ignore_owner_cookie(self):
        paths = (
            "/public-api/channels/tree",
            "/public-api/cities/Alpha/meetings?year=2026",
            "/public-api/calendar/search?q=Visible",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sim-queries",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/sidecars/quotes",
            f"/public-api/broadcasts/{_VISIBLE_PUBLIC_ID}/citation",
            "/public-api/cast/Alpha",
            "/public-api/cast/Alpha/mayor",
        )
        with mock.patch.object(
            api_server,
            "_current_user_from_cookie",
            side_effect=AssertionError("public route inspected an owner cookie"),
        ):
            for path in paths:
                with self.subTest(path=path):
                    anonymous = self._require_public_route(path)
                    owner = self._require_public_route(path, owner=True)
                    self.assertEqual(anonymous.status_code, owner.status_code)
                    self.assertEqual(anonymous.get_json(), owner.get_json())

    def test_paused_page_public_apis_require_owner(self):
        paths = (
            "/public-api/ledger/Alpha",
            "/public-api/coverage",
            "/public-api/corrections",
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)

    def test_display_floor_hides_old_rows_from_list_but_not_detail(self):
        """The ZSPAN_PUBLIC_DISPLAY_FLOOR (2026-06-01 default) trims
        pre-floor rows from the /v1 catalog LIST (the coming-soon card
        feed) while direct public_id DETAIL lookups keep resolving —
        deep links to old meetings must never break. Operator-directed
        2026-07-26 session-95 ("only have the months of July and June")."""
        old_public_id = "m_" + "D" * 22
        conn = database.get_connection()
        try:
            city_id = conn.execute(
                "SELECT id FROM cities WHERE name = 'Alpha'"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO meetings (
                    id, public_id, city_id, city_name, county, state,
                    meeting_title, meeting_date, meeting_status
                ) VALUES (?, ?, ?, 'Alpha', 'Test County', 'Arizona',
                          'Pre-Floor Archive Meeting', '2024-01-15',
                          'Minutes Available')
                """,
                (104, old_public_id, city_id),
            )
            conn.commit()
        finally:
            conn.close()

        listing = self.client.get("/v1/catalog/meetings?city=Alpha")
        self.assertEqual(listing.status_code, 200)
        listed_ids = {
            row["public_id"] for row in listing.get_json()["meetings"]
        }
        self.assertNotIn(old_public_id, listed_ids)

        detail = self.client.get(f"/v1/catalog/meetings/{old_public_id}")
        self.assertEqual(detail.status_code, 200)

    def test_existing_v1_catalog_proves_the_temp_db_harness(self):
        jurisdictions = self.client.get("/v1/catalog/jurisdictions")
        self.assertEqual(jurisdictions.status_code, 200)
        payload = jurisdictions.get_json()
        self.assert_exact_fields(payload, dto.PUBLIC_V1_JURISDICTIONS_FIELDS)
        for state in payload["states"]:
            self.assert_exact_fields(state, dto.PUBLIC_V1_STATE_FIELDS)
            for county in state["counties"]:
                self.assert_exact_fields(county, dto.PUBLIC_V1_COUNTY_FIELDS)
                for city in county["cities"]:
                    self.assert_exact_fields(city, dto.PUBLIC_V1_CITY_FIELDS)

        listing = self.client.get("/v1/catalog/meetings?city=Alpha")
        self.assertEqual(listing.status_code, 200)
        list_payload = listing.get_json()
        self.assert_exact_fields(
            list_payload, dto.PUBLIC_V1_CATALOG_LIST_RESPONSE_FIELDS
        )
        self.assertEqual(len(list_payload["meetings"]), 3)
        by_public_id = {row["public_id"]: row for row in list_payload["meetings"]}
        self.assertEqual(by_public_id[_VISIBLE_PUBLIC_ID]["availability"], "published")
        self.assertEqual(by_public_id[_DRAFT_PUBLIC_ID]["availability"], "coming_soon")
        self.assertEqual(
            by_public_id[_UNAPPROVED_PUBLIC_ID]["availability"], "coming_soon"
        )
        for row in list_payload["meetings"]:
            self.assert_exact_fields(row, dto.PUBLIC_V1_CATALOG_LIST_FIELDS)
            self.assert_no_internal_fields(row)

        detail = self.client.get(f"/v1/catalog/meetings/{_VISIBLE_PUBLIC_ID}")
        self.assertEqual(detail.status_code, 200)
        detail_payload = detail.get_json()
        self.assert_exact_fields(detail_payload, dto.PUBLIC_V1_CATALOG_DETAIL_FIELDS)
        self.assert_exact_fields(detail_payload["documents"], dto.PUBLIC_V1_DOCUMENT_FIELDS)
        self.assert_exact_fields(
            detail_payload["local_processing"], dto.PUBLIC_V1_LOCAL_PROCESSING_FIELDS
        )
        self.assert_no_internal_fields(detail_payload)

    def test_parser_health_is_owner_only_and_strips_registry_secrets(self):
        anonymous = self.client.get("/api/parser-health")
        self.assertEqual(anonymous.status_code, 401)

        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO scrape_log (
                    city_name, scraped_at, success, meetings_found
                ) VALUES (?, ?, ?, ?)
                """,
                ("Alpha", "2026-07-24 12:34:56", 1, 4),
            )
            conn.commit()
        finally:
            conn.close()

        registry = {
            "Alpha": {
                "county": "Test County",
                "parser_file": "api_server.py",
                "calendar_url": "https://sealed.example/secret-feed",
                "calendar_format": "secret-vendor",
                "notes": "never emit me",
            }
        }
        with (
            mock.patch.object(api_server, "_require_owner", return_value=(object(), None)),
            mock.patch.object(api_server, "load_parser_index", return_value=registry),
        ):
            response = self.client.get("/api/parser-health")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(set(payload), {"parsers", "counts"})
        self.assertEqual(
            payload["counts"],
            {
                "total": 1,
                "parser_files_present": 1,
                "working": 1,
                "broken": 0,
                "untested": 0,
            },
        )
        self.assertEqual(
            payload["parsers"],
            [{
                "city": "Alpha",
                "county": "Test County",
                "parser_file": True,
                "status": "working",
                "meeting_count": 4,
                "last_scanned_at": "2026-07-24 12:34:56",
            }],
        )
        serialized = json.dumps(payload)
        self.assertNotIn("calendar_url", serialized)
        self.assertNotIn("sealed.example", serialized)
        self.assertNotIn("secret-vendor", serialized)
        self.assertNotIn("never emit me", serialized)

    def test_cast_member_projection_drops_contact_pii(self):
        """Cast surface is sealed against contact PII per D-153 sealed-
        aggregation rule (operator decision 2026-07-24). Even if a
        future SELECT starts fetching email/phone/address columns from
        council_members, _project_public_dto's allowlist strips them
        before the response body leaves the process."""
        polluted_source = {
            "seat_id": "mayor",
            "name": "Test Mayor",
            "role": "Mayor",
            "term_started": "2025-01-01",
            "term_ends": "2028-12-31",
            "source_url": "https://city.example/council/mayor",
            # PII fields that must NEVER cross the public boundary,
            # even if they're accidentally present in the source dict:
            "email": "mayor@city.example",
            "email_work": "mayor.office@city.example",
            "phone": "555-0100",
            "phone_cell": "555-0199",
            "address": "123 Main St, City, ST 12345",
            "home_address": "456 Private Ln, City, ST 12345",
            "staff_email": "staff@city.example",
        }
        projected = api_server._public_cast_member(polluted_source)
        self.assertEqual(set(projected.keys()), set(dto.PUBLIC_CAST_MEMBER_FIELDS))
        serialized = json.dumps(projected)
        for leak_marker in (
            "mayor@city.example",
            "mayor.office@city.example",
            "staff@city.example",
            "555-0100",
            "555-0199",
            "123 Main St",
            "456 Private Ln",
            # Catch-all: any @-shape ending in the planted test domain.
            # Narrower than a bare "@" (which would false-positive on
            # legitimate source_url values), still catches future test-
            # payload additions that reuse the planted domain.
            "@city.example",
        ):
            self.assertNotIn(leak_marker, serialized)


class DecisionExcerptTimingSelectionTests(unittest.TestCase):
    def setUp(self):
        self.words = [
            {"word": "one", "start": 1.0, "end": 1.2, "speaker": "internal"},
            {"word": "two", "start": 1.3, "end": 1.5, "index": 99},
            {"word": "three", "start": 3.0, "end": 3.2},
        ]

    def test_inclusive_indices_emit_only_word_start_end(self):
        timings = api_server._decision_span_word_timings({
            "text": "one two",
            "start_word_index": 0,
            "end_word_index": 1,
        }, self.words)
        self.assertEqual(timings, [
            {"word": "one", "start": 1.0, "end": 1.2},
            {"word": "two", "start": 1.3, "end": 1.5},
        ])

    def test_legacy_inclusive_seconds_window_reconstructs_exactly(self):
        timings = api_server._decision_span_word_timings({
            "text": "one two",
            "start_seconds": 1.0,
            "end_seconds": 1.5,
        }, self.words)
        self.assertEqual([row["word"] for row in timings], ["one", "two"])

    def test_text_mismatch_and_malformed_indices_omit_timings(self):
        self.assertIsNone(api_server._decision_span_word_timings({
            "text": "rewritten text",
            "start_word_index": 0,
            "end_word_index": 1,
        }, self.words))
        self.assertIsNone(api_server._decision_span_word_timings({
            "text": "one two",
            "start_word_index": 0,
        }, self.words))


if __name__ == "__main__":
    unittest.main()

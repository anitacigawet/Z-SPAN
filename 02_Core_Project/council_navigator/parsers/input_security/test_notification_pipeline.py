"""Slice 3B notification classification, fan-out, token, and drain tests."""

from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
if str(_COUNCIL_NAVIGATOR_DIR) not in sys.path:
    sys.path.insert(0, str(_COUNCIL_NAVIGATOR_DIR))

from parsers import account_system, database
from parsers import notification_pipeline, resend_adapter, unsubscribe_tokens
from parsers.topic_tags import MATCHER_VERSION


class NotificationTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = mock.patch.object(
            database,
            "DB_PATH",
            str(Path(self.temp_dir.name) / "notifications.db"),
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.secret_patch = mock.patch.dict(
            os.environ,
            {"ZSPAN_SESSION_SECRET": "slice-3b-test-signing-secret"},
            clear=False,
        )
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)
        database.init_db()

    def seed_meeting(
        self,
        meeting_id: int = 101,
        *,
        city_name: str = "Kingman",
        title: str = "Regular Council Meeting",
        published: bool = False,
        approved: bool = False,
    ) -> int:
        conn = database.get_connection()
        try:
            city_row = conn.execute(
                """
                SELECT id FROM cities
                WHERE name = ? AND county = 'Mohave County' AND state = 'Arizona'
                """,
                (city_name,),
            ).fetchone()
            if city_row is None:
                city_id = conn.execute(
                    """
                    INSERT INTO cities (name, county, state)
                    VALUES (?, 'Mohave County', 'Arizona')
                    """,
                    (city_name,),
                ).lastrowid
            else:
                city_id = city_row[0]
            conn.execute(
                """
                INSERT INTO meetings (
                    id, public_id, city_id, city_name, county, state,
                    meeting_title, meeting_date, meeting_status, is_published
                ) VALUES (
                    ?, ?, ?, ?, 'Mohave County', 'Arizona',
                    ?, '2026-07-30', 'Agenda Available', ?
                )
                """,
                (
                    meeting_id,
                    f"m_{meeting_id:022d}",
                    city_id,
                    city_name,
                    title,
                    int(published),
                ),
            )
            if approved:
                conn.execute(
                    """
                    INSERT INTO work_orders (meeting_id, state, approved_at)
                    VALUES (?, 'completed', '2026-07-30 12:00:00')
                    """,
                    (meeting_id,),
                )
            conn.commit()
            return meeting_id
        finally:
            conn.close()

    def seed_output(
        self,
        meeting_id: int,
        output_type: str,
        content: str,
        *,
        voided: bool = False,
    ) -> None:
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO notebook_outputs (
                    meeting_id, notebook_id, output_type, content, voided_at
                ) VALUES (?, 'test-notebook', ?, ?, ?)
                ON CONFLICT(meeting_id, output_type) DO UPDATE SET
                    content = excluded.content,
                    voided_at = excluded.voided_at
                """,
                (
                    meeting_id,
                    output_type,
                    content,
                    "2026-07-30 12:30:00" if voided else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def seed_user(self, suffix: str) -> int:
        return account_system.upsert_user_from_google(
            google_sub=f"sub-{suffix}",
            email=f"{suffix}@example.com",
        ).id

    def follow(self, user_id: int, target_type: str, target_key: str) -> None:
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO follows (user_id, target_type, target_key)
                VALUES (?, ?, ?)
                """,
                (user_id, target_type, target_key),
            )
            conn.commit()
        finally:
            conn.close()


class RecomputeMeetingTopicTagsTests(NotificationTestBase):
    def test_idempotent_replace_and_matcher_version_stamp(self):
        meeting_id = self.seed_meeting(
            title="Hyperscaler Zoning Special Session"
        )
        self.seed_output(
            meeting_id,
            "key_decisions",
            (
                "1. Protected Colorado River allocation [at 00:12:34]\n"
                "2) Approved <core>library funding</core> {source 4}"
            ),
        )

        first = notification_pipeline.recompute_meeting_topic_tags(meeting_id)
        second = notification_pipeline.recompute_meeting_topic_tags(meeting_id)
        self.assertEqual(first, second)
        self.assertEqual(
            {match.tag_id for match in first},
            {"data_centers", "water_rights", "education"},
        )

        conn = database.get_connection()
        try:
            stored = conn.execute(
                """
                SELECT tag_id, matcher_version
                FROM meeting_topic_tags
                WHERE meeting_id = ?
                ORDER BY tag_id
                """,
                (meeting_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(stored), 3)
        self.assertTrue(all(row["matcher_version"] == MATCHER_VERSION for row in stored))

        # A recompute is replace-not-append: stale rows disappear when the
        # high-signal source changes.
        conn = database.get_connection()
        try:
            conn.execute(
                "UPDATE meetings SET meeting_title = 'Regular Meeting' WHERE id = ?",
                (meeting_id,),
            )
            conn.execute(
                """
                UPDATE notebook_outputs
                SET content = '1. Adopted the annual budget.'
                WHERE meeting_id = ? AND output_type = 'key_decisions'
                """,
                (meeting_id,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(
            notification_pipeline.recompute_meeting_topic_tags(meeting_id),
            [],
        )
        conn = database.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM meeting_topic_tags WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_empty_inputs_are_safe_and_voided_outputs_are_inactive(self):
        meeting_id = self.seed_meeting(title="")
        self.seed_output(
            meeting_id,
            "episode_tagline",
            "LGBTQ recognition proclamation",
            voided=True,
        )
        self.assertEqual(
            notification_pipeline.recompute_meeting_topic_tags(meeting_id),
            [],
        )

    def test_missing_meeting_is_noop(self):
        self.assertEqual(
            notification_pipeline.recompute_meeting_topic_tags(999_999),
            [],
        )


class EnqueuePublishedMeetingTests(NotificationTestBase):
    def test_missing_and_not_public_preconditions(self):
        missing = (
            notification_pipeline.enqueue_published_meeting_notifications(
                999_999
            )
        )
        self.assertEqual(missing["skipped_reason"], "meeting_not_found")
        self.assertFalse(missing["enqueued"])

        meeting_id = self.seed_meeting(published=False, approved=True)
        hidden = (
            notification_pipeline.enqueue_published_meeting_notifications(
                meeting_id
            )
        )
        self.assertEqual(hidden["skipped_reason"], "not_publicly_visible")
        self.assertFalse(hidden["enqueued"])

    def test_city_only_fanout_ignores_topics_and_is_replay_idempotent(self):
        meeting_id = self.seed_meeting(
            title="Hyperscaler Zoning Special Session",
            published=True,
            approved=True,
        )
        notification_pipeline.recompute_meeting_topic_tags(meeting_id)

        city_user = self.seed_user("city")
        topic_user = self.seed_user("topic")
        both_user = self.seed_user("both")
        disabled_user = self.seed_user("disabled")

        # Mixed-case direct fixture confirms fan-out compares city keys in
        # both directions even if a legacy row predates canonicalization.
        self.follow(city_user, "city", "kInGmAn")
        self.follow(topic_user, "topic", "data_centers")
        self.follow(both_user, "city", "Kingman")
        self.follow(both_user, "topic", "data_centers")
        self.follow(disabled_user, "city", "Kingman")
        account_system.set_notification_prefs(
            disabled_user,
            digest_cadence="weekly",
            email_enabled=False,
        )

        first = (
            notification_pipeline.enqueue_published_meeting_notifications(
                meeting_id
            )
        )
        self.assertTrue(first["enqueued"])
        self.assertIsNone(first["skipped_reason"])
        self.assertEqual(first["recipient_count"], 2)

        conn = database.get_connection()
        try:
            rows = conn.execute(
                """
                SELECT user_id, reasons_json
                FROM notification_outbox
                WHERE meeting_id = ?
                ORDER BY user_id
                """,
                (meeting_id,),
            ).fetchall()
            event_count = conn.execute(
                """
                SELECT recipient_count FROM notification_events
                WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(
            {row["user_id"] for row in rows},
            {city_user, both_user},
        )
        self.assertNotIn(topic_user, {row["user_id"] for row in rows})
        self.assertNotIn(disabled_user, {row["user_id"] for row in rows})
        self.assertEqual(event_count, 2)

        both_row = next(row for row in rows if row["user_id"] == both_user)
        both_reasons = json.loads(both_row["reasons_json"])
        self.assertEqual(
            {reason["target_type"] for reason in both_reasons},
            {"city"},
        )
        self.assertEqual(len(both_reasons), 1)

        replay = (
            notification_pipeline.enqueue_published_meeting_notifications(
                meeting_id
            )
        )
        self.assertEqual(replay, {
            "enqueued": False,
            "meeting_id": meeting_id,
            "recipient_count": 0,
            "skipped_reason": "already_enqueued",
        })
        conn = database.get_connection()
        try:
            outbox_count = conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(outbox_count, 2)

    def test_enqueue_notification_with_matched_topics(self):
        meeting_id = self.seed_meeting(
            title="Hyperscaler Zoning Special Session",
            published=True,
            approved=True,
        )
        notification_pipeline.recompute_meeting_topic_tags(meeting_id)
        user_id = self.seed_user("matched-city-topic")
        # Legacy rows can predate API write-time canonicalization. Fanout is
        # intentionally case-insensitive; decoration must still resolve the
        # canonical city preference with an exact lookup.
        self.follow(user_id, "city", "kInGmAn")

        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO follow_city_topics (user_id, city_key, tag_id)
                VALUES (?, 'Kingman', 'data_centers')
                """,
                (user_id,),
            )
            conn.commit()
        finally:
            conn.close()

        result = (
            notification_pipeline.enqueue_published_meeting_notifications(
                meeting_id
            )
        )
        self.assertEqual(result["recipient_count"], 1)

        conn = database.get_connection()
        try:
            row = conn.execute(
                """
                SELECT reasons_json
                FROM notification_outbox
                WHERE user_id = ? AND meeting_id = ?
                """,
                (user_id, meeting_id),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(
            json.loads(row["reasons_json"]),
            [
                {
                    "label": "kInGmAn",
                    "matched_topic_tags": ["data_centers"],
                    "target_key": "kInGmAn",
                    "target_type": "city",
                }
            ],
        )

    def test_nonmatching_city_topic_does_not_filter_or_decorate(self):
        meeting_id = self.seed_meeting(
            title="Hyperscaler Zoning Special Session",
            published=True,
            approved=True,
        )
        notification_pipeline.recompute_meeting_topic_tags(meeting_id)
        user_id = self.seed_user("nonmatching-city-topic")
        self.follow(user_id, "city", "Kingman")
        account_system.set_city_topics(
            user_id,
            "Kingman",
            ["water_rights"],
        )

        result = (
            notification_pipeline.enqueue_published_meeting_notifications(
                meeting_id
            )
        )
        self.assertEqual(result["recipient_count"], 1)

        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT reasons_json FROM notification_outbox "
                "WHERE user_id = ? AND meeting_id = ?",
                (user_id, meeting_id),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            json.loads(row["reasons_json"]),
            [
                {
                    "label": "Kingman",
                    "target_key": "Kingman",
                    "target_type": "city",
                }
            ],
        )

    def test_zero_recipient_event_prevents_late_follow_email(self):
        meeting_id = self.seed_meeting(published=True, approved=True)
        first = (
            notification_pipeline.enqueue_published_meeting_notifications(
                meeting_id
            )
        )
        self.assertTrue(first["enqueued"])
        self.assertEqual(first["recipient_count"], 0)

        late_user = self.seed_user("late")
        self.follow(late_user, "city", "Kingman")
        replay = (
            notification_pipeline.enqueue_published_meeting_notifications(
                meeting_id
            )
        )
        self.assertEqual(replay["skipped_reason"], "already_enqueued")
        conn = database.get_connection()
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM notification_outbox"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT recipient_count FROM notification_events
                    WHERE meeting_id = ?
                    """,
                    (meeting_id,),
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_concurrent_calls_use_event_row_as_single_fanout_gate(self):
        meeting_id = self.seed_meeting(published=True, approved=True)
        user_id = self.seed_user("race")
        self.follow(user_id, "city", "Kingman")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    notification_pipeline.enqueue_published_meeting_notifications,
                    (meeting_id, meeting_id),
                )
            )

        self.assertEqual(
            sorted(result["enqueued"] for result in results),
            [False, True],
        )
        self.assertEqual(
            {
                result["skipped_reason"]
                for result in results
                if not result["enqueued"]
            },
            {"already_enqueued"},
        )
        conn = database.get_connection()
        try:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM notification_events
                    WHERE meeting_id = ?
                    """,
                    (meeting_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM notification_outbox
                    WHERE meeting_id = ?
                    """,
                    (meeting_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()


class UnsubscribeTokenTests(NotificationTestBase):
    def test_mint_verify_roundtrip_and_reuse_unused(self):
        user_id = self.seed_user("token")
        first = unsubscribe_tokens.ensure_token_for_user(user_id)
        second = unsubscribe_tokens.ensure_token_for_user(user_id)
        self.assertEqual(first, second)
        self.assertEqual(
            unsubscribe_tokens.verify_unsubscribe_token(first),
            user_id,
        )
        conn = database.get_connection()
        try:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM unsubscribe_tokens
                    WHERE user_id = ? AND used_at IS NULL
                    """,
                    (user_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_tampered_hmac_and_unknown_token_id_rejected(self):
        user_id = self.seed_user("tamper")
        raw = unsubscribe_tokens.ensure_token_for_user(user_id)
        token_id, signature = raw.split(".", 1)
        replacement = "0" if signature[-1] != "0" else "1"
        self.assertIsNone(
            unsubscribe_tokens.verify_unsubscribe_token(
                f"{token_id}.{signature[:-1]}{replacement}"
            )
        )
        self.assertIsNone(
            unsubscribe_tokens.verify_unsubscribe_token(
                f"unknown-token.{signature}"
            )
        )

    def test_malformed_shapes_rejected(self):
        user_id = self.seed_user("malformed")
        unsubscribe_tokens.ensure_token_for_user(user_id)
        for raw in ("", "no-dot", ".", ".sig", "id.", None):
            with self.subTest(raw=raw):
                self.assertIsNone(
                    unsubscribe_tokens.verify_unsubscribe_token(raw)
                )

    def test_verifier_fails_closed_when_storage_errors(self):
        with mock.patch.object(
            unsubscribe_tokens.database,
            "get_connection",
            side_effect=RuntimeError("database unavailable"),
        ):
            self.assertIsNone(
                unsubscribe_tokens.verify_unsubscribe_token(
                    "opaque.signature"
                )
            )

    def test_used_token_is_rejected_and_not_reused(self):
        user_id = self.seed_user("used")
        first = unsubscribe_tokens.ensure_token_for_user(user_id)
        token_id = first.split(".", 1)[0]
        unsubscribe_tokens.mark_token_used(token_id)
        unsubscribe_tokens.mark_token_used(token_id)
        self.assertIsNone(unsubscribe_tokens.verify_unsubscribe_token(first))
        second = unsubscribe_tokens.ensure_token_for_user(user_id)
        self.assertNotEqual(first, second)
        self.assertEqual(
            unsubscribe_tokens.verify_unsubscribe_token(second),
            user_id,
        )

    def test_expired_token_is_rejected_and_replaced(self):
        user_id = self.seed_user("expired")
        first = unsubscribe_tokens.ensure_token_for_user(user_id)
        token_id = first.split(".", 1)[0]
        conn = database.get_connection()
        try:
            conn.execute(
                """
                UPDATE unsubscribe_tokens
                SET expires_at = datetime('now', '-1 second')
                WHERE token_id = ?
                """,
                (token_id,),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertIsNone(unsubscribe_tokens.verify_unsubscribe_token(first))
        second = unsubscribe_tokens.ensure_token_for_user(user_id)
        self.assertNotEqual(first, second)


class ResendDrainTests(NotificationTestBase):
    def _enqueue_user(
        self,
        suffix: str,
        meeting_id: int,
        *,
        city_name: str = "Kingman",
        title: str = "Hyperscaler Zoning",
        matched_topic_tag: str | None = None,
    ) -> int:
        self.seed_meeting(
            meeting_id,
            city_name=city_name,
            title=title,
            published=True,
            approved=True,
        )
        user_id = self.seed_user(suffix)
        self.follow(user_id, "city", city_name)
        if matched_topic_tag:
            conn = database.get_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO meeting_topic_tags (
                        meeting_id, tag_id, evidence_field, trigger_phrase,
                        matcher_version
                    ) VALUES (?, ?, 'meeting_title', 'hyperscaler', ?)
                    """,
                    (meeting_id, matched_topic_tag, MATCHER_VERSION),
                )
                conn.commit()
            finally:
                conn.close()
            account_system.set_city_topics(
                user_id,
                city_name,
                [matched_topic_tag],
            )
        result = (
            notification_pipeline.enqueue_published_meeting_notifications(
                meeting_id
            )
        )
        self.assertEqual(result["recipient_count"], 1)
        return user_id

    def test_missing_api_key_is_noop(self):
        self._enqueue_user("no-key", 201)
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": ""}, clear=False):
            result = resend_adapter.drain_notification_outbox()
        self.assertEqual(result, {
            "attempted": 0,
            "sent": 0,
            "failed": 0,
            "skipped_no_api_key": True,
        })
        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT sent_at, attempt_count FROM notification_outbox"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row["sent_at"])
        self.assertEqual(row["attempt_count"], 0)

    def test_success_escapes_html_and_sets_delivery_headers(self):
        city = 'Kingman & <script>alert("city")</script>'
        title = 'Hyperscaler <img src=x onerror="alert(1)">'
        self._enqueue_user(
            "escape",
            202,
            city_name=city,
            title=title,
            matched_topic_tag="data_centers",
        )
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"id": "resend-message-202"}

        with (
            mock.patch.dict(
                os.environ,
                {
                    "RESEND_API_KEY": "resend-test-key",
                    "ZSPAN_PUBLIC_ORIGIN": "https://zspan.org",
                },
                clear=False,
            ),
            mock.patch.object(
                resend_adapter.requests,
                "post",
                return_value=response,
            ) as post,
        ):
            result = resend_adapter.drain_notification_outbox()

        self.assertEqual(result["sent"], 1)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 8)
        self.assertEqual(
            kwargs["headers"]["Idempotency-Key"],
            "zspan-outbox-1",
        )
        payload = kwargs["json"]
        self.assertNotIn("<script>", payload["html"])
        self.assertNotIn("<img", payload["html"])
        self.assertIn(html.escape(city, quote=True), payload["html"])
        self.assertIn(html.escape(title, quote=True), payload["html"])
        self.assertIn("Tagged:", payload["html"])
        self.assertIn(">Data Centers</span>", payload["html"])
        self.assertIn("tagged: Data Centers", payload["text"])
        self.assertNotIn("<script>", payload["text"])
        self.assertEqual(
            payload["headers"]["List-Unsubscribe-Post"],
            "List-Unsubscribe=One-Click",
        )
        self.assertRegex(
            payload["headers"]["List-Unsubscribe"],
            r"^<https://zspan\.org/api/unsubscribe\?token=.+>$",
        )

        conn = database.get_connection()
        try:
            stored = conn.execute(
                """
                SELECT sent_at, provider_message_id
                FROM notification_outbox
                """
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(stored["sent_at"])
        self.assertEqual(
            stored["provider_message_id"],
            "resend-message-202",
        )

    def test_failure_does_not_abort_batch_and_retry_key_is_stable(self):
        self._enqueue_user("first", 203)
        self._enqueue_user(
            "second",
            204,
            city_name="Bullhead City",
            title="Hyperscaler Zoning Follow-up",
        )
        success = mock.Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"id": "resend-message-204"}

        with (
            mock.patch.dict(
                os.environ,
                {"RESEND_API_KEY": "resend-test-key"},
                clear=False,
            ),
            mock.patch.object(
                resend_adapter.requests,
                "post",
                side_effect=[
                    resend_adapter.requests.Timeout("accepted then timed out"),
                    success,
                ],
            ) as post,
            self.assertLogs(resend_adapter.logger, level="ERROR"),
        ):
            result = resend_adapter.drain_notification_outbox()

        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(
            post.call_args_list[0].kwargs["headers"]["Idempotency-Key"],
            "zspan-outbox-1",
        )

        conn = database.get_connection()
        try:
            failed = conn.execute(
                """
                SELECT attempt_count, next_attempt_at, last_error
                FROM notification_outbox
                WHERE id = 1
                """
            ).fetchone()
            conn.execute(
                """
                UPDATE notification_outbox
                SET next_attempt_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(failed["attempt_count"], 1)
        self.assertRegex(
            failed["next_attempt_at"],
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
        )
        self.assertIn("Timeout", failed["last_error"])

        retry = mock.Mock()
        retry.raise_for_status.return_value = None
        retry.json.return_value = {"id": "resend-message-203"}
        with (
            mock.patch.dict(
                os.environ,
                {"RESEND_API_KEY": "resend-test-key"},
                clear=False,
            ),
            mock.patch.object(
                resend_adapter.requests,
                "post",
                return_value=retry,
            ) as retry_post,
        ):
            retry_result = resend_adapter.drain_notification_outbox()
        self.assertEqual(retry_result["sent"], 1)
        self.assertEqual(
            retry_post.call_args.kwargs["headers"]["Idempotency-Key"],
            "zspan-outbox-1",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

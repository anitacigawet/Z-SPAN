"""S-012 + D-095 — account_system helper tests.

Exercises every helper in `parsers.account_system` against an isolated
sqlite DB with the full account-system schema. End-to-end auth (chunks
2-3 of ACCOUNT_SYSTEM_SPEC) is NOT tested here — those require a live
Google Cloud Web OAuth client; these tests validate the data-access
layer that the auth flow consumes when it builds.

Per [D-100](../../../../01_Project_Overview/DECISIONS.md#d-100): defensive
unit tests for the schema + helpers.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
if str(_COUNCIL_NAVIGATOR_DIR) not in sys.path:
    sys.path.insert(0, str(_COUNCIL_NAVIGATOR_DIR))

from parsers import database, env_config
from parsers.account_system import (
    CreatorPromotionError,
    FOLLOW_CAP_PER_USER,
    FollowCapExceeded,
    clear_city_topics,
    follow_add,
    follow_remove,
    get_active_agreement,
    get_creator_download_summary,
    get_notification_prefs,
    get_user,
    get_user_by_google_sub,
    list_city_topics,
    list_follows,
    list_revival_requests,
    log_creator_download,
    promote_user_to_creator,
    revival_request_add,
    revoke_creator_role,
    set_city_topics,
    set_notification_prefs,
    upsert_user_from_google,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# The minimal schema this test set needs. We don't pull init_notebook_schema
# because it requires the whole `cities`/`meetings` graph; the account-system
# tables are self-contained.
_SCHEMA_SQL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT,
    avatar_url TEXT,
    role TEXT NOT NULL DEFAULT 'light' CHECK (role IN ('light','creator','verified-creator')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE follows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('city','county','topic','meeting')),
    target_key TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, target_type, target_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE follow_city_topics (
    user_id INTEGER NOT NULL,
    city_key TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, city_key, tag_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE channel_revival_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('city','county')),
    target_key TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, target_type, target_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE notification_prefs (
    user_id INTEGER PRIMARY KEY,
    digest_cadence TEXT NOT NULL DEFAULT 'weekly' CHECK (digest_cadence IN ('off','daily','weekly','monthly')),
    email_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE creator_agreements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    tos_version TEXT NOT NULL,
    disclaimer_version TEXT NOT NULL,
    disclaimer_acknowledged_at TIMESTAMP NOT NULL,
    signed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMP,
    revoked_reason TEXT,
    signup_ip_hash TEXT,
    -- Moderation columns the live schema gets from the init_db idempotent
    -- migration (database.py ~1609). Mirrored here so this fixture matches the
    -- post-migration production schema: promote_user_to_creator INSERTs
    -- operator_review_needed / moderation_reason / moderation_normalized_text,
    -- and the operator-review-resolution flow uses the other four. The fixture
    -- had drifted (it predated the moderation feature), erroring 11 tests.
    operator_review_needed INTEGER NOT NULL DEFAULT 0,
    moderation_reason TEXT,
    moderation_normalized_text TEXT,
    operator_resolved_at TIMESTAMP,
    operator_resolved_by TEXT,
    operator_action TEXT,
    operator_note TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE creator_downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('clip','summary','infographic','audio','video','other')),
    tos_version_at_download TEXT NOT NULL,
    download_source_ip_hash TEXT,
    downloaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE creator_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    asset_id TEXT,
    feedback_text TEXT NOT NULL,
    submitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    operator_review_needed INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""


class AccountSystemTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_db_path = _PROJECT_ROOT / "parsers" / (
            f"_test_account_system_{id(self)}.db"
        )
        if self.tmp_db_path.exists():
            self.tmp_db_path.unlink()
        conn = sqlite3.connect(self.tmp_db_path)
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        conn.close()
        self._patches = []
        patcher = mock.patch(
            "parsers.database.get_connection",
            side_effect=lambda: sqlite3.connect(self.tmp_db_path),
        )
        patcher.start()
        self._patches.append(patcher)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        try:
            if self.tmp_db_path.exists():
                self.tmp_db_path.unlink()
        except PermissionError:
            pass


class LocalFilePermissionTests(unittest.TestCase):
    def test_save_user_settings_writes_0600(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "private" / "user_settings.json"
            with mock.patch.object(
                env_config,
                "SETTINGS_PATH",
                str(settings_path),
            ):
                env_config.save_user_settings({"foo": "bar"})

            self.assertTrue(settings_path.is_file())
            if os.name == "posix":
                self.assertEqual(
                    stat.S_IMODE(settings_path.stat().st_mode),
                    0o600,
                )
                self.assertEqual(
                    stat.S_IMODE(settings_path.parent.stat().st_mode),
                    0o700,
                )

    def test_save_user_settings_atomic_on_crash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "private"
            parent.mkdir(mode=0o700)
            settings_path = parent / "user_settings.json"
            original = '{"original": true}\n'
            settings_path.write_text(original, encoding="utf-8")

            def fail_mid_write(_settings, fh, **_kwargs):
                fh.write('{"partial":')
                raise RuntimeError("simulated write failure")

            with (
                mock.patch.object(
                    env_config,
                    "SETTINGS_PATH",
                    str(settings_path),
                ),
                mock.patch.object(env_config.json, "dump", fail_mid_write),
                self.assertRaisesRegex(RuntimeError, "simulated write failure"),
            ):
                env_config.save_user_settings({"replacement": True})

            self.assertEqual(
                settings_path.read_text(encoding="utf-8"),
                original,
            )
            self.assertEqual(
                {path.name for path in parent.iterdir()},
                {settings_path.name},
            )

    def test_get_connection_creates_db_0600(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "private" / "meetings_cache.db"
            with mock.patch.object(database, "DB_PATH", str(db_path)):
                conn = database.get_connection()
                conn.close()

            self.assertTrue(db_path.is_file())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o600)


class UserUpsertTests(AccountSystemTestBase):
    def test_new_user_inserted_with_default_role(self):
        user = upsert_user_from_google(
            google_sub="google-sub-1",
            email="alice@example.com",
            display_name="Alice",
        )
        self.assertEqual(user.email, "alice@example.com")
        self.assertEqual(user.role, "light")
        self.assertEqual(user.display_name, "Alice")

    def test_existing_user_updated_not_duplicated(self):
        user1 = upsert_user_from_google(
            google_sub="google-sub-1",
            email="alice@example.com",
            display_name="Alice",
        )
        user2 = upsert_user_from_google(
            google_sub="google-sub-1",
            email="alice@example.com",
            display_name="Alice Updated",
        )
        self.assertEqual(user1.id, user2.id)
        self.assertEqual(user2.display_name, "Alice Updated")

    def test_get_user_by_id(self):
        user = upsert_user_from_google(
            google_sub="x", email="b@example.com",
        )
        self.assertEqual(get_user(user.id).email, "b@example.com")
        self.assertIsNone(get_user(99999))

    def test_get_user_by_google_sub(self):
        upsert_user_from_google(google_sub="g1", email="c@example.com")
        self.assertEqual(
            get_user_by_google_sub("g1").email, "c@example.com",
        )
        self.assertIsNone(get_user_by_google_sub("nonexistent"))


class FollowTests(AccountSystemTestBase):
    def setUp(self):
        super().setUp()
        self.user = upsert_user_from_google(
            google_sub="g", email="u@example.com",
        )

    def test_follow_add_idempotent(self):
        self.assertTrue(follow_add(self.user.id, "city", "Kingman"))
        self.assertFalse(follow_add(self.user.id, "city", "Kingman"))

    def test_follow_remove(self):
        follow_add(self.user.id, "city", "Kingman")
        self.assertTrue(follow_remove(self.user.id, "city", "Kingman"))
        self.assertFalse(follow_remove(self.user.id, "city", "Kingman"))

    def test_follow_remove_city_key_is_case_insensitive_only(self):
        # City follows unfollow regardless of case (COLLATE NOCASE, PR #217).
        follow_add(self.user.id, "city", "Kingman")
        self.assertTrue(follow_remove(self.user.id, "city", "kInGmAn"))
        self.assertFalse(follow_remove(self.user.id, "city", "Kingman"))
        # Non-city target keys stay strict-eq.
        follow_add(self.user.id, "meeting", "M-42")
        self.assertFalse(follow_remove(self.user.id, "meeting", "m-42"))
        self.assertTrue(follow_remove(self.user.id, "meeting", "M-42"))

    def test_list_follows(self):
        follow_add(self.user.id, "city", "Kingman")
        follow_add(self.user.id, "city", "Bullhead")
        follows = list_follows(self.user.id)
        keys = {f["target_key"] for f in follows}
        self.assertEqual(keys, {"Kingman", "Bullhead"})

    def test_set_and_list_city_topics(self):
        self.assertEqual(
            set_city_topics(self.user.id, "Kingman", ["data_centers"]),
            ["data_centers"],
        )
        self.assertEqual(
            list_city_topics(self.user.id),
            {"Kingman": ["data_centers"]},
        )

        self.assertEqual(set_city_topics(self.user.id, "Kingman", []), [])
        self.assertEqual(list_city_topics(self.user.id), {})

        self.assertEqual(
            set_city_topics(self.user.id, "Kingman", ["invalid"]),
            [],
        )
        self.assertEqual(list_city_topics(self.user.id), {})

        set_city_topics(
            self.user.id,
            "Kingman",
            ["WATER_RIGHTS", "water_rights", "other"],
        )
        self.assertEqual(
            list_city_topics(self.user.id),
            {"Kingman": ["water_rights"]},
        )
        self.assertEqual(clear_city_topics(self.user.id, "Kingman"), 1)
        self.assertEqual(list_city_topics(self.user.id), {})

    def test_follow_cap_enforced_on_new_targets(self):
        """Session-103 (product-slice2): the per-user cap blocks NEW
        targets past FOLLOW_CAP_PER_USER but keeps deletes + idempotent
        re-adds of existing follows working."""
        for i in range(FOLLOW_CAP_PER_USER):
            self.assertTrue(follow_add(self.user.id, "city", f"City{i}"))
        # Idempotent re-add of an existing target must NOT raise even at cap.
        self.assertFalse(follow_add(self.user.id, "city", "City0"))
        # A genuinely new target beyond the cap raises.
        with self.assertRaises(FollowCapExceeded):
            follow_add(self.user.id, "city", "OneTooMany")
        # Deletes still work at cap, and freeing a slot re-opens adds.
        self.assertTrue(follow_remove(self.user.id, "city", "City0"))
        self.assertTrue(follow_add(self.user.id, "city", "OneTooMany"))


class RevivalRequestTests(AccountSystemTestBase):
    def setUp(self):
        super().setUp()
        self.user = upsert_user_from_google(
            google_sub="g", email="u@example.com",
        )

    def test_add_and_list(self):
        self.assertTrue(revival_request_add(self.user.id, "city", "Globe"))
        self.assertFalse(revival_request_add(self.user.id, "city", "Globe"))
        requests = list_revival_requests(self.user.id)
        self.assertEqual(len(requests), 1)


class NotificationPrefsTests(AccountSystemTestBase):
    def setUp(self):
        super().setUp()
        self.user = upsert_user_from_google(
            google_sub="g", email="u@example.com",
        )

    def test_default_prefs(self):
        prefs = get_notification_prefs(self.user.id)
        self.assertEqual(prefs["digest_cadence"], "weekly")
        self.assertTrue(prefs["email_enabled"])

    def test_set_then_get(self):
        set_notification_prefs(
            self.user.id, digest_cadence="daily", email_enabled=False,
        )
        prefs = get_notification_prefs(self.user.id)
        self.assertEqual(prefs["digest_cadence"], "daily")
        self.assertFalse(prefs["email_enabled"])

    def test_upsert_overwrites(self):
        set_notification_prefs(self.user.id, digest_cadence="daily")
        set_notification_prefs(self.user.id, digest_cadence="monthly")
        prefs = get_notification_prefs(self.user.id)
        self.assertEqual(prefs["digest_cadence"], "monthly")


class CreatorPromotionTests(AccountSystemTestBase):
    def setUp(self):
        super().setUp()
        self.user = upsert_user_from_google(
            google_sub="g", email="u@example.com",
        )

    def test_promote_to_creator(self):
        agreement = promote_user_to_creator(
            user_id=self.user.id,
            tos_version="v1.0",
            disclaimer_version="d1.0",
            disclaimer_acknowledged_at="2026-06-10T12:00:00Z",
        )
        self.assertEqual(agreement.user_id, self.user.id)
        # role should flip to creator
        self.assertEqual(get_user(self.user.id).role, "creator")

    def test_promote_idempotent_same_tos(self):
        a1 = promote_user_to_creator(
            user_id=self.user.id,
            tos_version="v1.0",
            disclaimer_version="d1.0",
            disclaimer_acknowledged_at="2026-06-10T12:00:00Z",
        )
        a2 = promote_user_to_creator(
            user_id=self.user.id,
            tos_version="v1.0",
            disclaimer_version="d1.0",
            disclaimer_acknowledged_at="2026-06-10T12:01:00Z",
        )
        self.assertEqual(a1.id, a2.id)

    def test_promote_unknown_user_raises(self):
        with self.assertRaises(CreatorPromotionError):
            promote_user_to_creator(
                user_id=99999,
                tos_version="v1.0",
                disclaimer_version="d1.0",
                disclaimer_acknowledged_at="2026-06-10T12:00:00Z",
            )

    def test_revoke(self):
        promote_user_to_creator(
            user_id=self.user.id,
            tos_version="v1.0",
            disclaimer_version="d1.0",
            disclaimer_acknowledged_at="2026-06-10T12:00:00Z",
        )
        self.assertTrue(revoke_creator_role(self.user.id, reason="test"))
        self.assertEqual(get_user(self.user.id).role, "light")
        self.assertIsNone(get_active_agreement(self.user.id))

    def test_revoke_without_active_returns_false(self):
        self.assertFalse(revoke_creator_role(self.user.id, reason="test"))

    def test_get_active_agreement_returns_none_for_no_agreement(self):
        self.assertIsNone(get_active_agreement(self.user.id))


class CreatorDownloadTests(AccountSystemTestBase):
    def setUp(self):
        super().setUp()
        self.user = upsert_user_from_google(
            google_sub="g", email="u@example.com",
        )
        promote_user_to_creator(
            user_id=self.user.id,
            tos_version="v1.0",
            disclaimer_version="d1.0",
            disclaimer_acknowledged_at="2026-06-10T12:00:00Z",
        )

    def test_log_download(self):
        d = log_creator_download(
            user_id=self.user.id,
            asset_id="asset-001",
            asset_type="clip",
        )
        self.assertEqual(d.asset_id, "asset-001")
        self.assertEqual(d.tos_version_at_download, "v1.0")

    def test_log_download_requires_active_agreement(self):
        revoke_creator_role(self.user.id, reason="x")
        with self.assertRaises(CreatorPromotionError):
            log_creator_download(
                user_id=self.user.id,
                asset_id="asset-002",
                asset_type="clip",
            )

    def test_download_summary_aggregate(self):
        for i in range(5):
            log_creator_download(
                user_id=self.user.id,
                asset_id=f"asset-{i}",
                asset_type="clip",
            )
        summary = get_creator_download_summary(self.user.id)
        self.assertEqual(summary.total_downloads, 5)
        self.assertIsNotNone(summary.most_recent_at)

    def test_download_summary_empty(self):
        # different user with no downloads
        other = upsert_user_from_google(
            google_sub="g2", email="o@example.com",
        )
        summary = get_creator_download_summary(other.id)
        self.assertEqual(summary.total_downloads, 0)
        self.assertIsNone(summary.most_recent_at)


if __name__ == "__main__":
    unittest.main()

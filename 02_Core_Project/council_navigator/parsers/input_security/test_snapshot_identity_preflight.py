"""Snapshot push identity-preflight regression test."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_REPO_ROOT = Path(__file__).resolve().parents[4]
_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database


class SnapshotIdentityPreflightTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("sqlite3"), "sqlite3 CLI is required")
    def test_poisoned_role_aborts_and_names_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "snapshot.db")
            with mock.patch.object(database, "DB_PATH", database_path):
                database.init_db()
                conn = database.get_connection()
                try:
                    city_id = conn.execute(
                        "INSERT INTO cities (name, county, state) VALUES (?, ?, ?)",
                        ("Snapshot City", "Test County", "Arizona"),
                    ).lastrowid
                    conn.execute(
                        """
                        INSERT INTO meetings (
                            id, city_id, city_name, county, state, meeting_title,
                            meeting_date, meeting_status, published_by
                        ) VALUES (
                            901, ?, 'Snapshot City', 'Test County', 'Arizona',
                            'Snapshot Meeting', '2026-07-22', 'Scheduled',
                            'poisoned@example.test'
                        )
                        """,
                        (city_id,),
                    )
                    conn.execute(
                        """
                        INSERT INTO users (google_sub, email, display_name)
                        VALUES ('snapshot-role', 'role@example.test', 'Z-SPAN')
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO meetings (
                            id, city_id, city_name, county, state, meeting_title,
                            meeting_date, meeting_status, published_by, publish_notes
                        ) VALUES (
                            902, ?, 'Snapshot City', 'Test County', 'Arizona',
                            'Role Identity Meeting', '2026-07-22', 'Scheduled',
                            'Z-SPAN', 'Published by Z-SPAN'
                        )
                        """,
                        (city_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()

            env = dict(os.environ)
            env["ZSPAN_LOCAL_DB"] = database_path
            result = subprocess.run(
                ["bash", str(_REPO_ROOT / "ops" / "push_snapshot_to_railway.sh"), "--dry-run"],
                cwd=_REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Identity preflight failed", combined)
        self.assertIn("meetings.id=901", combined)
        self.assertNotIn("meetings.id=902", combined)


if __name__ == "__main__":
    unittest.main()

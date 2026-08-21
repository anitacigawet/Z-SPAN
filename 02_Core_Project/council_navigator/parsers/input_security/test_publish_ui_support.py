"""Server-side support for the operator publication UI."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
if str(_COUNCIL_NAVIGATOR_DIR) not in sys.path:
    sys.path.insert(0, str(_COUNCIL_NAVIGATOR_DIR))

from parsers import database
from parsers.google_oauth import compute_redirect_uri


def test_work_order_list_includes_actual_meeting_publication_state():
    with tempfile.TemporaryDirectory() as temp_dir:
        with mock.patch.object(database, "DB_PATH", str(Path(temp_dir) / "publish.db")):
            database.init_db()
            conn = database.get_connection()
            try:
                city_id = conn.execute(
                    """
                    INSERT INTO cities (name, county, state)
                    VALUES ('Test City', 'Test County', 'Arizona')
                    """
                ).lastrowid
                conn.executemany(
                    """
                    INSERT INTO meetings (
                        id, city_id, city_name, county, state, meeting_title,
                        meeting_date, is_published
                    ) VALUES (?, ?, 'Test City', 'Test County', 'Arizona', ?, ?, ?)
                    """,
                    [
                        (101, city_id, "Public meeting", "2026-07-15", 1),
                        (102, city_id, "Re-review meeting", "2026-07-16", 0),
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO work_orders (meeting_id, state, approved_at)
                    VALUES (?, 'completed', '2026-07-17 12:00:00')
                    """,
                    [(101,), (102,)],
                )
                conn.commit()
            finally:
                conn.close()

            rows = database.list_work_orders(limit=10)

    by_meeting_id = {row["meeting_id"]: row for row in rows}
    assert by_meeting_id[101]["is_published"] == 1
    assert by_meeting_id[102]["is_published"] == 0


def test_operator_hostname_uses_operator_oauth_callback():
    with mock.patch.dict("os.environ", {"ZSPAN_OAUTH_REDIRECT_URI": ""}):
        assert compute_redirect_uri("https://operator.zspan.org/") == (
            "https://operator.zspan.org/api/auth/google/callback"
        )


def test_explicit_oauth_redirect_override_still_wins():
    override = "https://override.example/auth/callback"
    with mock.patch.dict("os.environ", {"ZSPAN_OAUTH_REDIRECT_URI": override}):
        assert compute_redirect_uri("https://operator.zspan.org/") == override

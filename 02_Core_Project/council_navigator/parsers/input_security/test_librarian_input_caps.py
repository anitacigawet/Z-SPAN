"""Pre-parse body caps for the live Librarian/BYOK endpoint family."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_CORE_PROJECT_DIR = Path(__file__).resolve().parents[3]
_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_COUNCIL_NAVIGATOR_DIR, _CORE_PROJECT_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database

sys.modules["database"] = database

import slack_listener

with tempfile.TemporaryDirectory() as _import_temp_dir:
    with (
        mock.patch.object(
            database,
            "DB_PATH",
            str(Path(_import_temp_dir) / "import.db"),
        ),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server

from zspan_pipeline import byok_relay, byok_validate, qdrant_synthesizer


class LibrarianInputCapTests(unittest.TestCase):
    OVERSIZED_BODY = b"x" * 100_000

    def setUp(self) -> None:
        api_server.app.config.update(TESTING=True)
        api_server._reset_public_rate_limits_for_tests()
        self.client = api_server.app.test_client()

    def tearDown(self) -> None:
        api_server._reset_public_rate_limits_for_tests()

    def _post_oversized(self, path: str):
        return self.client.post(
            path,
            data=self.OVERSIZED_BODY,
            content_type="application/json",
        )

    def _assert_rejected_before_principal_lookup(
        self,
        response,
        principal_gate: mock.Mock,
    ) -> None:
        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.get_json()["status"],
            "payload_too_large",
        )
        principal_gate.assert_not_called()

    def test_rag_search_rejects_oversized_body_before_qdrant_or_db(self):
        with (
            mock.patch.object(
                api_server,
                "_byok_public_query_allowed",
            ) as principal_gate,
            mock.patch.object(
                qdrant_synthesizer,
                "retrieve_chunks",
            ) as retrieve,
        ):
            response = self._post_oversized("/api/rag-search/1")

        self._assert_rejected_before_principal_lookup(
            response,
            principal_gate,
        )
        retrieve.assert_not_called()

    def test_rag_search_rejects_observed_size_without_content_length(self):
        with mock.patch.object(
            api_server,
            "_byok_public_query_allowed",
        ) as principal_gate:
            response = self.client.open(
                "/api/rag-search/1",
                method="POST",
                data=self.OVERSIZED_BODY,
                content_type="application/json",
                environ_overrides={
                    "CONTENT_LENGTH": "",
                    "wsgi.input_terminated": True,
                },
            )

        self._assert_rejected_before_principal_lookup(
            response,
            principal_gate,
        )

    def test_relay_rejects_oversized_body_before_provider_or_db(self):
        with (
            mock.patch.object(
                api_server,
                "_byok_public_query_allowed",
            ) as principal_gate,
            mock.patch.object(byok_relay, "relay") as dispatch,
        ):
            response = self._post_oversized("/api/byok/relay")

        self._assert_rejected_before_principal_lookup(
            response,
            principal_gate,
        )
        dispatch.assert_not_called()

    def test_relay_stream_rejects_oversized_body_before_provider_or_db(self):
        with (
            mock.patch.object(
                api_server,
                "_byok_public_query_allowed",
            ) as principal_gate,
            mock.patch.object(byok_relay, "relay_stream") as dispatch,
        ):
            response = self._post_oversized("/api/byok/relay-stream")

        self._assert_rejected_before_principal_lookup(
            response,
            principal_gate,
        )
        dispatch.assert_not_called()

    def test_validate_key_rejects_oversized_body_before_provider_or_db(self):
        with (
            mock.patch.object(
                api_server,
                "_byok_public_query_allowed",
            ) as principal_gate,
            mock.patch.object(byok_validate, "validate_key") as validate,
        ):
            response = self._post_oversized("/api/byok/validate-key")

        self._assert_rejected_before_principal_lookup(
            response,
            principal_gate,
        )
        validate.assert_not_called()

    def test_gate_hash_ignores_query_material_beyond_raw_cap(self):
        prefix = "x" * 200
        first = api_server._librarian_gate_query_hash(prefix + "first")
        second = api_server._librarian_gate_query_hash(prefix + "second")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

"""Credential-placement tests for server-side BYOK validation."""

from __future__ import annotations

import unittest
from unittest import mock

from zspan_pipeline import byok_validate


class _Response:
    status_code = 200

    @staticmethod
    def json() -> dict[str, list[object]]:
        return {"models": []}


class GeminiValidationTransportTests(unittest.TestCase):
    def test_key_is_never_put_in_request_url(self) -> None:
        api_key = "AIza-test-secret-never-in-url"
        with mock.patch("requests.get", return_value=_Response()) as get:
            result = byok_validate.validate_gemini_key(api_key)

        call = get.call_args
        self.assertTrue(result["valid"])
        self.assertNotIn(api_key, call.args[0])
        self.assertNotIn("params", call.kwargs)
        self.assertEqual(call.kwargs["headers"]["X-Goog-Api-Key"], api_key)

    def test_provider_error_cannot_echo_header_key(self) -> None:
        api_key = "AIza-test-secret-never-in-errors"
        response = mock.Mock(status_code=400)
        response.json.return_value = {
            "error": {"message": f"X-Goog-Api-Key: {api_key} is invalid"}
        }
        with mock.patch("requests.get", return_value=response):
            result = byok_validate.validate_gemini_key(api_key)

        self.assertFalse(result["valid"])
        self.assertNotIn(api_key, result["error"])


if __name__ == "__main__":
    unittest.main()

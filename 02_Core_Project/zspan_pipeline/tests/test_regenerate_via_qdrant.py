"""Regression coverage for the single-output regeneration maintenance CLI."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from zspan_pipeline import qdrant_synthesizer
from zspan_pipeline.scripts import regenerate_via_qdrant as regenerate


class RegenerateViaQdrantTests(unittest.TestCase):
    def test_metadata_comes_from_local_database(self) -> None:
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {
            "notebook_id": None,
            "city_name": "Kingman",
        }
        with mock.patch.object(
            regenerate, "get_connection", return_value=connection
        ) as get_connection:
            notebook_id, city_name = regenerate.load_meeting_metadata(127899)

        self.assertEqual(notebook_id, "")
        self.assertEqual(city_name, "Kingman")
        get_connection.assert_called_once_with()
        connection.execute.assert_called_once_with(
            "SELECT notebook_id, city_name FROM meetings WHERE id = ?",
            (127899,),
        )
        connection.close.assert_called_once_with()

    def test_missing_meeting_fails_before_synthesis(self) -> None:
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = None
        with mock.patch.object(regenerate, "get_connection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "No meeting found with id=404"):
                regenerate.load_meeting_metadata(404)
        connection.close.assert_called_once_with()

    def test_main_regenerates_without_flask_session(self) -> None:
        result = qdrant_synthesizer.SynthesisResult(
            content="uncorrected synopsis",
            output_type="synopsis",
            meeting_id=127899,
            chunks=[],
            model_id="test-model",
            prompt_filename="synopsis.md",
        )
        argv = [
            "regenerate_via_qdrant",
            "--meeting-id",
            "127899",
            "--output",
            "synopsis",
        ]

        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                regenerate,
                "load_meeting_metadata",
                return_value=("", "Kingman"),
            ) as load_metadata,
            mock.patch.object(
                regenerate.qdrant_synthesizer,
                "synthesize_output",
                return_value=result,
            ) as synthesize,
            mock.patch.object(
                regenerate,
                "apply_city_corrections",
                return_value=("corrected synopsis", []),
            ) as apply_corrections,
            mock.patch.object(regenerate, "save_notebook_output") as save_output,
            mock.patch(
                "socket.create_connection",
                side_effect=AssertionError("unexpected network access"),
            ),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = regenerate.main()

        self.assertEqual(exit_code, 0)
        load_metadata.assert_called_once_with(127899)
        synthesize.assert_called_once()
        apply_corrections.assert_called_once_with("Kingman", "uncorrected synopsis")
        save_output.assert_called_once_with(
            meeting_id=127899,
            notebook_id="",
            output_type="synopsis",
            content="corrected synopsis",
            prompt_filename="synopsis.md",
            prompt_version="v1-rag-3-test-model",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

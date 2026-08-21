"""F7/F14: offline yt-dlp DNS-pinning and byte-cap tests."""
from __future__ import annotations

import socket
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_PARSERS_DIR = Path(__file__).resolve().parents[1]
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

import whisper_client as wc


def _yt_dlp_module(youtube_dl_class):
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = youtube_dl_class
    return module


class WhisperDownloadSecurityTests(unittest.TestCase):
    def test_yt_dlp_uses_the_initial_pinned_dns_answer(self):
        resolver_calls = 0
        observed_ips: list[str] = []

        def resolver(host, *args, **kwargs):
            nonlocal resolver_calls
            self.assertEqual(host, "www.youtube.com")
            resolver_calls += 1
            ip = "142.250.72.14" if resolver_calls == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

        class FakeYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def extract_info(self, _url, download):
                self.assert_download(download)
                for _ in range(2):
                    infos = socket.getaddrinfo(
                        "www.youtube.com", 443, type=socket.SOCK_STREAM
                    )
                    observed_ips.append(infos[0][4][0])
                output = Path(self.opts["outtmpl"].replace("%(ext)s", "webm"))
                output.write_bytes(b"audio")
                return {"ext": "webm", "duration": 2.0, "title": "Meeting"}

            @staticmethod
            def assert_download(download):
                if download is not True:
                    raise AssertionError("download=True required")

            def prepare_filename(self, _info):
                return self.opts["outtmpl"].replace("%(ext)s", "webm")

        with TemporaryDirectory() as tmp, \
             mock.patch.dict(
                 sys.modules, {"yt_dlp": _yt_dlp_module(FakeYoutubeDL)}
             ), mock.patch("socket.getaddrinfo", side_effect=resolver):
            result = wc.download_youtube_audio(
                "https://www.youtube.com/watch?v=test",
                Path(tmp) / "meeting-audio",
            )

        self.assertEqual(resolver_calls, 1)
        self.assertEqual(observed_ips, ["142.250.72.14", "142.250.72.14"])
        self.assertEqual(result.title, "Meeting")

    def test_running_byte_cap_aborts_and_cleans_partial_files(self):
        captured_opts: dict = {}

        class FakeYoutubeDL:
            def __init__(self, opts):
                captured_opts.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def extract_info(self, _url, download):
                output_template = captured_opts["outtmpl"]
                current = Path(output_template.replace("%(ext)s", "webm"))
                partial = Path(str(current) + ".part")
                current.write_bytes(b"oversized-current")
                partial.write_bytes(b"oversized-partial")
                captured_opts["progress_hooks"][0]({
                    "status": "downloading",
                    "downloaded_bytes": wc.SOURCE_DOWNLOAD_MAX_BYTES + 1,
                    "filename": str(current),
                    "tmpfilename": str(partial),
                })
                raise AssertionError("the byte-cap hook must abort")

        with TemporaryDirectory() as tmp, \
             mock.patch.dict(
                 sys.modules, {"yt_dlp": _yt_dlp_module(FakeYoutubeDL)}
             ), mock.patch(
                 "socket.getaddrinfo",
                 return_value=[(
                     socket.AF_INET,
                     socket.SOCK_STREAM,
                     6,
                     "",
                     ("142.250.72.14", 0),
                 )],
             ):
            output_basepath = Path(tmp) / "meeting-audio"
            unrelated = Path(tmp) / "meeting-audio.notes"
            unrelated.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(wc.WhisperDownloadError, "500 MiB"):
                wc.download_youtube_audio(
                    "https://www.youtube.com/watch?v=test", output_basepath
                )

            self.assertFalse(Path(str(output_basepath) + ".webm").exists())
            self.assertFalse(Path(str(output_basepath) + ".webm.part").exists())
            self.assertTrue(unrelated.exists())

        self.assertEqual(
            captured_opts["max_filesize"], wc.SOURCE_DOWNLOAD_MAX_BYTES
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

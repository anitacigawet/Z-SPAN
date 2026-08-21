"""RR-8 flagship SSRF guard (safe_fetch) — the server-side counterpart of the
CLI media-guard tests. Verifies the host guard, per-hop redirect validation,
and the streamed/declared size cap. Network fully stubbed."""
from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

_PARSERS_DIR = Path(__file__).resolve().parents[1]
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

import safe_fetch


def _gai(mapping):
    def _fn(host, *a, **k):
        ip = mapping.get(host)
        if ip is None:
            raise socket.gaierror("no such host")
        return [(0, 0, 0, "", (ip, 0))]
    return _fn


class _Resp:
    def __init__(self, status, location=None, body=b"", content_length=None):
        self.status_code = status
        self.is_redirect = status in (301, 302, 303, 307, 308) and bool(location)
        self.headers = {}
        if location:
            self.headers["Location"] = location
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class AssertSafeUrlTests(unittest.TestCase):
    def test_rejects_non_web_scheme(self):
        for u in ("file:///etc/passwd", "data:text/plain,hi", "ftp://e.com/x"):
            with self.assertRaises(safe_fetch.UnsafeUrlError):
                safe_fetch.assert_safe_url(u)

    def test_rejects_credentials_and_private_and_mixed(self):
        with mock.patch("socket.getaddrinfo", side_effect=_gai({"e.com": "93.184.216.34"})):
            with self.assertRaises(safe_fetch.UnsafeUrlError):
                safe_fetch.assert_safe_url("https://u:p@e.com/x")
        for ip in ("127.0.0.1", "169.254.169.254", "10.0.0.5", "::1"):
            with mock.patch("socket.getaddrinfo", side_effect=_gai({"e.com": ip})):
                with self.assertRaises(safe_fetch.UnsafeUrlError):
                    safe_fetch.assert_safe_url("https://e.com/x")
        with mock.patch("socket.getaddrinfo",
                        side_effect=_gai({"e.com": "93.184.216.34"})) as _:
            # mixed public+private via multiple addrinfo entries
            with mock.patch("socket.getaddrinfo",
                            return_value=[(0, 0, 0, "", ("93.184.216.34", 0)),
                                          (0, 0, 0, "", ("192.168.1.9", 0))]):
                with self.assertRaises(safe_fetch.UnsafeUrlError):
                    safe_fetch.assert_safe_url("https://e.com/x")

    def test_allows_public(self):
        with mock.patch("socket.getaddrinfo", side_effect=_gai({"e.com": "93.184.216.34"})):
            safe_fetch.assert_safe_url("https://e.com/x")  # must not raise


class PinnedDnsTests(unittest.TestCase):
    def test_initial_host_cannot_rebind_during_download(self):
        calls = 0

        def resolver(host, *args, **kwargs):
            nonlocal calls
            self.assertEqual(host, "media.example.com")
            calls += 1
            ip = "93.184.216.34" if calls == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

        with mock.patch("socket.getaddrinfo", side_effect=resolver):
            with safe_fetch.pinned_dns("https://media.example.com/audio"):
                first = socket.getaddrinfo(
                    "media.example.com", 443, type=socket.SOCK_STREAM
                )
                second = socket.getaddrinfo(
                    "media.example.com", 443, type=socket.SOCK_STREAM
                )

        self.assertEqual(calls, 1)
        self.assertEqual(first[0][4][0], "93.184.216.34")
        self.assertEqual(second[0][4][0], "93.184.216.34")

    def test_discovered_hosts_are_validated_then_pinned(self):
        calls: dict[str, int] = {}

        def resolver(host, *args, **kwargs):
            calls[host] = calls.get(host, 0) + 1
            answers = {
                "www.youtube.com": "142.250.72.14",
                "rr1.googlevideo.com": (
                    "151.101.1.1" if calls[host] == 1 else "10.0.0.2"
                ),
                "private.invalid": "169.254.169.254",
            }
            ip = answers[host]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

        with mock.patch("socket.getaddrinfo", side_effect=resolver):
            with safe_fetch.pinned_dns("https://www.youtube.com/watch?v=x"):
                first = socket.getaddrinfo("rr1.googlevideo.com", 443)
                second = socket.getaddrinfo("rr1.googlevideo.com", 443)
                with self.assertRaises(safe_fetch.UnsafeUrlError):
                    socket.getaddrinfo("private.invalid", 443)

        self.assertEqual(calls["rr1.googlevideo.com"], 1)
        self.assertEqual(first[0][4][0], "151.101.1.1")
        self.assertEqual(second[0][4][0], "151.101.1.1")


class RedirectResolverTests(unittest.TestCase):
    def test_redirect_to_private_refused(self):
        gai = _gai({"pub.com": "93.184.216.34", "int.com": "169.254.169.254"})
        hops = {"https://pub.com/x": _Resp(302, "https://int.com/x")}
        with mock.patch("socket.getaddrinfo", side_effect=gai), \
             mock.patch("requests.request", side_effect=lambda m, u, **k: hops[u]):
            with self.assertRaises(safe_fetch.UnsafeUrlError):
                safe_fetch.resolve_redirects_safely("https://pub.com/x")

    def test_too_many_redirects_refused(self):
        gai = _gai({f"h{i}.com": "93.184.216.34" for i in range(12)})
        hops = {f"https://h{i}.com/x": _Resp(302, f"https://h{i + 1}.com/x")
                for i in range(12)}
        with mock.patch("socket.getaddrinfo", side_effect=gai), \
             mock.patch("requests.request", side_effect=lambda m, u, **k: hops[u]):
            with self.assertRaises(safe_fetch.UnsafeUrlError):
                safe_fetch.resolve_redirects_safely("https://h0.com/x")

    def test_resolves_terminal(self):
        gai = _gai({"pub.com": "93.184.216.34", "cdn.com": "151.101.0.1"})
        hops = {"https://pub.com/x": _Resp(302, "https://cdn.com/f"),
                "https://cdn.com/f": _Resp(200)}
        with mock.patch("socket.getaddrinfo", side_effect=gai), \
             mock.patch("requests.request", side_effect=lambda m, u, **k: hops[u]):
            self.assertEqual(
                safe_fetch.resolve_redirects_safely("https://pub.com/x"),
                "https://cdn.com/f",
            )


class SafeFetchTests(unittest.TestCase):
    def _patches(self, terminal_resp):
        gai = _gai({"pub.com": "93.184.216.34"})
        return (
            mock.patch("socket.getaddrinfo", side_effect=gai),
            mock.patch("requests.request", side_effect=lambda m, u, **k: _Resp(200)),
            mock.patch("requests.get", side_effect=lambda u, **k: terminal_resp),
        )

    def test_streamed_size_cap(self):
        p1, p2, p3 = self._patches(_Resp(200, body=b"x" * 100))
        with p1, p2, p3:
            with self.assertRaises(safe_fetch.UnsafeUrlError):
                safe_fetch.safe_fetch("https://pub.com/x", max_bytes=10)

    def test_declared_size_cap(self):
        p1, p2, p3 = self._patches(_Resp(200, body=b"ok", content_length=999))
        with p1, p2, p3:
            with self.assertRaises(safe_fetch.UnsafeUrlError):
                safe_fetch.safe_fetch("https://pub.com/x", max_bytes=10)

    def test_returns_body_within_cap(self):
        p1, p2, p3 = self._patches(_Resp(200, body=b"hello"))
        with p1, p2, p3:
            self.assertEqual(safe_fetch.safe_fetch("https://pub.com/x"), b"hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)

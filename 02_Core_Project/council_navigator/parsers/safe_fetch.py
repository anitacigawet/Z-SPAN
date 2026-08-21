"""Server-side SSRF guard for flagship outbound fetches.

The flagship pulls two kinds of attacker-influenceable URLs from scraped
meeting data: agenda documents (fed to subprocess text extractors) and video
sources (fed to yt-dlp). A poisoned upstream — a malicious city site emitting
a `video_url` / `agenda_url` pointing at `http://localhost`, the operator's
LAN, or `169.254.169.254` — is the S-144 "water-carrier / poisoned-upstream"
threat. This module is the flagship counterpart of the CLI's
`zspan_cli.media.assert_safe_media_url` (+ its DIV-011 redirect resolver):

- `assert_safe_url(url)` — the single-URL host guard (scheme, no embedded
  credentials, resolves ONLY to public addresses).
- `pinned_dns(url)` — the yt-dlp guard: validate once, then pin each public
  downloader hostname for the complete synchronous download.
- `safe_fetch(url, ...)` — a redirect-validating, size-capped GET for document
  fetches: every 30x hop is re-validated before it is followed, and the body
  is streamed under a hard byte cap so a hostile server can't OOM the box.

Kept dependency-light (stdlib + `requests`, already a parser dep) so it is
importable from any flagship module without a heavy import.
"""
from __future__ import annotations

import ipaddress
import socket
from contextlib import contextmanager
from threading import RLock
from typing import Iterator
from urllib.parse import urljoin, urlparse

import requests

_ALLOWED_SCHEMES = ("http", "https")
_MAX_REDIRECTS = 5
# 25 MB is generous for an agenda PDF/DOCX; a source claiming more is either
# not a document or a resource-exhaustion attempt — refuse either way.
_DEFAULT_MAX_BYTES = 25 * 1024 * 1024

# ``socket.getaddrinfo`` is process-global, so pinned download contexts must
# not overlap and restore the exact resolver they replaced. The context is
# intentionally narrow: callers wrap only the synchronous yt-dlp operation.
_DNS_PIN_LOCK = RLock()


class UnsafeUrlError(Exception):
    """A URL was refused before any bytes were fetched (SSRF guard) — the
    caller should skip it, not treat it as a transient download failure."""


def _is_disallowed_ip(ip_str: str) -> bool:
    """True for any address the fetcher must never be pointed at —
    loopback, private, link-local, multicast, reserved, unspecified."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable -> refuse rather than guess
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def _url_host(url: str) -> str:
    """Validate URL syntax/policy and return its normalized hostname."""
    raw = (url or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError as e:
        raise UnsafeUrlError(f"malformed URL ({url!r})") from e
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"refusing a non-web URL scheme ({parsed.scheme or 'none'!r}); "
            "only http/https sources are allowed."
        )
    if parsed.username or parsed.password:
        raise UnsafeUrlError("refusing a source URL that embeds credentials.")
    host = (parsed.hostname or "").strip()
    if not host:
        raise UnsafeUrlError(f"refusing a URL with no host ({url!r}).")
    return host.rstrip(".").lower()


def _public_ips_from_addrinfo(host: str, infos: list[tuple]) -> frozenset[str]:
    """Extract a non-empty, all-public IP set from a resolver response."""
    ips: set[str] = set()
    for info in infos:
        try:
            ip_str = info[4][0]
        except (IndexError, TypeError):
            raise UnsafeUrlError(
                f"refusing malformed DNS results for {host!r}."
            ) from None
        if _is_disallowed_ip(ip_str):
            raise UnsafeUrlError(
                f"refusing to fetch {host!r} — it resolves to a non-public "
                f"address ({ip_str})."
            )
        ips.add(str(ipaddress.ip_address(ip_str)))
    if not ips:
        raise UnsafeUrlError(f"refusing empty DNS results for {host!r}.")
    return frozenset(ips)


def _resolve_public_host(
    host: str,
    resolver=socket.getaddrinfo,
) -> frozenset[str]:
    """Resolve ``host`` once and return the complete validated public set."""
    try:
        infos = resolver(host, None)
    except socket.gaierror as e:
        raise UnsafeUrlError(
            f"couldn't resolve the source host {host!r} ({e})."
        ) from e
    return _public_ips_from_addrinfo(host, infos)


def assert_safe_url(url: str) -> None:
    """Raise unless ``url`` is http(s), credential-free, and all-public."""
    _resolve_public_host(_url_host(url), socket.getaddrinfo)


def _pinned_addrinfo(
    host: str,
    ips: frozenset[str],
    port,
    family: int,
    socktype: int,
    proto: int,
) -> list[tuple]:
    """Build getaddrinfo-compatible rows without another DNS lookup."""
    if port is None:
        resolved_port = 0
    elif isinstance(port, int):
        resolved_port = port
    else:
        try:
            resolved_port = int(port)
        except (TypeError, ValueError):
            try:
                resolved_port = socket.getservbyname(str(port), "tcp")
            except OSError as exc:
                raise socket.gaierror(
                    socket.EAI_SERVICE, f"unsupported service {port!r}"
                ) from exc

    rows: list[tuple] = []
    for ip_str in sorted(ips):
        ip = ipaddress.ip_address(ip_str)
        ip_family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        if family not in (0, socket.AF_UNSPEC, ip_family):
            continue
        result_type = socktype or socket.SOCK_STREAM
        result_proto = proto
        if not result_proto and result_type == socket.SOCK_STREAM:
            result_proto = socket.IPPROTO_TCP
        sockaddr = (
            (ip_str, resolved_port, 0, 0)
            if ip.version == 6
            else (ip_str, resolved_port)
        )
        rows.append((ip_family, result_type, result_proto, host, sockaddr))
    if not rows:
        raise socket.gaierror(socket.EAI_FAMILY, "no pinned address for family")
    return rows


@contextmanager
def pinned_dns(url: str) -> Iterator[None]:
    """Pin public DNS answers for one synchronous downloader operation.

    The URL host is resolved and validated before the resolver is replaced.
    Hosts discovered later by yt-dlp (for example ``googlevideo.com``) are
    resolved once through the original resolver, rejected if any answer is
    non-public, and then pinned for the remainder of the context. This closes
    the validation/use DNS-rebinding window without blocking legitimate CDN
    host discovery.
    """
    with _DNS_PIN_LOCK:
        original_getaddrinfo = socket.getaddrinfo
        initial_host = _url_host(url)
        pinned: dict[str, frozenset[str]] = {
            initial_host: _resolve_public_host(initial_host, original_getaddrinfo)
        }

        def _getaddrinfo(
            query_host,
            port,
            family=0,
            type=0,
            proto=0,
            flags=0,
        ):
            if isinstance(query_host, bytes):
                normalized = query_host.decode("idna").rstrip(".").lower()
            elif isinstance(query_host, str):
                normalized = query_host.rstrip(".").lower()
            else:
                return original_getaddrinfo(
                    query_host, port, family, type, proto, flags
                )

            known = pinned.get(normalized)
            if known is not None:
                return _pinned_addrinfo(
                    normalized, known, port, family, type, proto
                )

            try:
                infos = original_getaddrinfo(
                    query_host, port, family, type, proto, flags
                )
            except socket.gaierror as exc:
                raise UnsafeUrlError(
                    f"couldn't resolve downloader host {normalized!r} ({exc})."
                ) from exc
            pinned[normalized] = _public_ips_from_addrinfo(normalized, infos)
            return infos

        socket.getaddrinfo = _getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


def resolve_redirects_safely(url: str, *, timeout: float = 15.0) -> str:
    """Follow the redirect chain with auto-redirect disabled, re-validating
    every hop with `assert_safe_url` before requesting it, and return the
    terminal URL. Raises UnsafeUrlError on an unsafe hop or too many hops."""
    current = url
    seen: set[str] = set()
    for _ in range(_MAX_REDIRECTS + 1):
        assert_safe_url(current)
        if current in seen:
            raise UnsafeUrlError("refusing a source URL whose redirects loop.")
        seen.add(current)
        try:
            resp = requests.request(
                "GET", current,
                allow_redirects=False, stream=True, timeout=timeout,
                headers={"Accept": "*/*"},
            )
        except requests.RequestException as e:
            raise UnsafeUrlError(
                f"couldn't check the source URL for redirects ({e})."
            ) from e
        try:
            location = resp.headers.get("Location")
            if resp.is_redirect and location:
                current = urljoin(current, location)
                continue
            return current
        finally:
            resp.close()
    raise UnsafeUrlError(
        f"refusing a source URL with more than {_MAX_REDIRECTS} redirects."
    )


def safe_fetch(
    url: str,
    *,
    timeout: float = 20.0,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    headers: dict | None = None,
) -> bytes:
    """SSRF-safe document GET: validate every redirect hop, then stream the
    terminal response under a hard byte cap. Returns the body bytes.

    Raises UnsafeUrlError for an unsafe/oversized target and requests
    exceptions for genuine transport failures (the caller distinguishes
    "refused" from "server was down").
    """
    terminal = resolve_redirects_safely(url, timeout=timeout)
    req_headers = {"Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    # allow_redirects=False: we already resolved to the terminal URL, and we
    # never want a fresh redirect here to escape the validated chain.
    resp = requests.get(
        terminal, headers=req_headers, timeout=timeout,
        stream=True, allow_redirects=False,
    )
    try:
        resp.raise_for_status()
        declared = resp.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise UnsafeUrlError(
                        f"refusing a {int(declared)}-byte response "
                        f"(cap {max_bytes})."
                    )
            except ValueError:
                pass  # bogus header — the streamed cap below still bounds it
        chunks = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.extend(chunk)
            if len(chunks) > max_bytes:
                raise UnsafeUrlError(
                    f"source response exceeded the {max_bytes}-byte cap."
                )
        return bytes(chunks)
    finally:
        resp.close()

"""Video source resolve + audio download for `zspan process`.

The V0 source classes mirror the PLAYER-1 classifier's, minus the vendor
rescue cases.
Everything downloads through yt-dlp — one dependency, resumable .part
files, and its stock User-Agent keeps source-site fetches neutrally
identified rather than spoofed. Downloads are sequential by design;
never parallelize a fetch against a source site.

Audio-only, smallest stream ("worstaudio/worst") — the flagship's own
choice for transcription fetches (whisper_client.py precedent): speech
transcribes fine at low bitrate and the download stays small.
"""
from __future__ import annotations

import ipaddress
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable, Iterator, Optional
from urllib.parse import urljoin, urlparse

# Source classes `classify_video_url` can return.
KIND_YOUTUBE = "youtube"
KIND_DIRECT_MEDIA = "direct_media"
KIND_VENDOR_PAGE = "vendor_page"     # Granicus player/iframe class — V0 skips honestly
KIND_UNKNOWN = "unknown"

_YOUTUBE_HOST_SUFFIXES = ("youtube.com", "youtu.be")

_DIRECT_MEDIA_EXTENSIONS = (
    ".mp4", ".m4v", ".mov", ".webm", ".mkv",
    ".m4a", ".mp3", ".wav", ".aac", ".ogg",
)

# Vendor archive pages that hold a player, not a media file. Resolving
# these needs the flagship-side vendor machinery; the CLI says so plainly
# instead of guessing.
_VENDOR_PAGE_MARKERS = (
    "mediaplayer.php", "/mediaplayer", "/player/clip/", "/player/camera/",
    ".asx", "insight.granicus", "swagit.com/play", "videoplayer.telvue",
)


class MediaError(Exception):
    """A media step failed in a way the user should read, not a bug."""


_ALLOWED_MEDIA_SCHEMES = ("http", "https")
MEDIA_DOWNLOAD_MAX_BYTES = 500 * 1024 * 1024

# ``getaddrinfo`` is process-global. Serialize these intentionally short,
# synchronous yt-dlp contexts and always restore the resolver we replaced.
_DNS_PIN_LOCK = RLock()


def _is_disallowed_ip(ip_str: str) -> bool:
    """True for any address the fetcher must never be pointed at —
    loopback, private, link-local, multicast, reserved, unspecified."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → refuse rather than guess
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def _media_url_host(url: str) -> str:
    """Validate media URL syntax/policy and return a normalized hostname."""
    raw = (url or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        raise MediaError(f"refusing to fetch a malformed URL ({url!r}).")
    if parsed.scheme.lower() not in _ALLOWED_MEDIA_SCHEMES:
        raise MediaError(
            f"refusing a non-web URL scheme ({parsed.scheme or 'none'!r}); "
            "only http/https media sources are allowed — not file://, "
            "data://, or other schemes."
        )
    if parsed.username or parsed.password:
        raise MediaError("refusing a source URL that embeds credentials.")
    host = (parsed.hostname or "").strip()
    if not host:
        raise MediaError(f"refusing a URL with no host ({url!r}).")
    return host.rstrip(".").lower()


def _public_ips_from_addrinfo(host: str, infos: list[tuple]) -> frozenset[str]:
    """Extract a non-empty, all-public IP set from resolver output."""
    ips: set[str] = set()
    for info in infos:
        try:
            ip_str = info[4][0]
        except (IndexError, TypeError):
            raise MediaError(
                f"refusing malformed DNS results for {host!r}."
            ) from None
        if _is_disallowed_ip(ip_str):
            raise MediaError(
                f"refusing to fetch {host!r} — it resolves to a non-public "
                f"address ({ip_str}). This build won't let a catalog row "
                "point the downloader at localhost, the LAN, or a cloud "
                "metadata endpoint."
            )
        ips.add(str(ipaddress.ip_address(ip_str)))
    if not ips:
        raise MediaError(f"refusing empty DNS results for {host!r}.")
    return frozenset(ips)


def _resolve_public_media_host(
    host: str,
    resolver=socket.getaddrinfo,
) -> frozenset[str]:
    try:
        infos = resolver(host, None)
    except socket.gaierror as e:
        raise MediaError(
            f"couldn't resolve the source host {host!r} ({e}) — skipping "
            "rather than guessing."
        ) from e
    return _public_ips_from_addrinfo(host, infos)


def assert_safe_media_url(url: str) -> None:
    """Reject non-web/credentialed URLs or any non-public DNS answer."""
    _resolve_public_media_host(_media_url_host(url), socket.getaddrinfo)


def _pinned_addrinfo(
    host: str,
    ips: frozenset[str],
    port,
    family: int,
    socktype: int,
    proto: int,
) -> list[tuple]:
    """Build getaddrinfo-compatible rows without another DNS query."""
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
def pinned_media_dns(url: str) -> Iterator[None]:
    """Pin every all-public hostname used during one yt-dlp operation.

    The initial host is resolved once before the resolver is replaced. Any
    extractor/CDN host discovered later is validated on its first lookup and
    pinned thereafter. A rebinding answer can therefore never replace an IP
    set already approved within the download.
    """
    with _DNS_PIN_LOCK:
        original_getaddrinfo = socket.getaddrinfo
        initial_host = _media_url_host(url)
        pinned: dict[str, frozenset[str]] = {
            initial_host: _resolve_public_media_host(
                initial_host, original_getaddrinfo
            )
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
                raise MediaError(
                    f"couldn't resolve downloader host {normalized!r} ({exc})."
                ) from exc
            pinned[normalized] = _public_ips_from_addrinfo(normalized, infos)
            return infos

        socket.getaddrinfo = _getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


_MAX_MEDIA_REDIRECTS = 5


def resolve_media_redirects_safely(url: str, *, timeout: float = 15.0) -> str:
    """Follow the source URL's redirect chain with auto-redirect DISABLED,
    re-running `assert_safe_media_url` on EVERY hop before requesting it, and
    return the terminal URL to hand to yt-dlp.

    Closes DIV-011: `assert_safe_media_url` alone only guards the URL the
    catalog handed us; yt-dlp follows 30x redirects internally, so a public
    URL that redirects to localhost / the LAN / 169.254.169.254 would slip
    past the single-URL guard. By resolving the chain here — validating each
    Location before we touch it — and giving yt-dlp the already-terminal URL,
    no internal yt-dlp hop lands on a private target for the common
    (fixed-target) case. Credentialed and non-web redirect targets are
    refused by `assert_safe_media_url` at the top of the next iteration.
    """
    import requests  # declared dep; lazy so import cost stays off init/pick

    current = url
    seen: set[str] = set()
    for _ in range(_MAX_MEDIA_REDIRECTS + 1):
        assert_safe_media_url(current)  # validate THIS hop before requesting it
        if current in seen:
            raise MediaError("refusing a source URL whose redirects loop.")
        seen.add(current)
        try:
            resp = requests.request(
                "GET", current,
                allow_redirects=False, stream=True, timeout=timeout,
                headers={"Accept": "*/*"},
            )
        except requests.RequestException as e:
            raise MediaError(
                f"couldn't check the source URL for redirects ({e}) — skipping "
                "rather than fetching a target we couldn't validate."
            ) from e
        try:
            location = resp.headers.get("Location")
            if resp.is_redirect and location:
                current = urljoin(current, location)
                continue
            return current  # terminal hop — its host was validated above
        finally:
            resp.close()  # stream=True: never pulls the body
    raise MediaError(
        f"refusing a source URL with more than {_MAX_MEDIA_REDIRECTS} redirects."
    )


def _safe_source_url(url: str, kind: str) -> str:
    """The URL to actually hand to yt-dlp: always SSRF-guarded, and for the
    direct-media class fully redirect-resolved so no internal yt-dlp hop can
    land on a private target (DIV-011). YouTube stays as-handed — youtube.com
    is a trusted host and its resolution isn't a plain HTTP redirect chain."""
    assert_safe_media_url(url)
    if kind == KIND_DIRECT_MEDIA:
        return resolve_media_redirects_safely(url)
    return url


@dataclass
class DownloadedMedia:
    path: Path
    source_url: str
    kind: str
    bytes: int


_YOUTUBE_PRIMARY_CLIENT = "android_vr"
_YOUTUBE_403_FALLBACK_CLIENT = "web_safari"


def _youtube_client_opts(base_opts: dict, client: str) -> dict:
    """Return one yt-dlp attempt's options with exactly one player client."""
    return {
        **base_opts,
        "extractor_args": {
            "youtube": {"player_client": [client]},
        },
    }


def _clear_partial_downloads(
    dest_dir: Path,
    meeting_id: int,
    progress_data: dict | None = None,
) -> None:
    """Remove only this meeting's yt-dlp partial/current output files."""
    parent = dest_dir.resolve()
    prefix = f"{meeting_id}."
    for path in dest_dir.glob(f"{meeting_id}.*"):
        if not path.is_file():
            continue
        if (
            path.name.endswith((".part", ".ytdl"))
            or ".part-Frag" in path.name
        ):
            path.unlink(missing_ok=True)

    for key in ("filename", "tmpfilename"):
        raw_path = (progress_data or {}).get(key)
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path).resolve()
        if path.parent == parent and path.name.startswith(prefix):
            path.unlink(missing_ok=True)


def _download_progress_hook(
    dest_dir: Path,
    meeting_id: int,
    progress: Callable[[str], None],
):
    """Build the milestone reporter plus unknown-length 500 MiB guard."""
    milestones = {25, 50, 75}

    def _hook(progress_data: dict) -> None:
        if progress_data.get("status") != "downloading":
            return
        done = progress_data.get("downloaded_bytes") or 0
        if done > MEDIA_DOWNLOAD_MAX_BYTES:
            _clear_partial_downloads(dest_dir, meeting_id, progress_data)
            raise MediaError(
                "source exceeded the 500 MiB download cap "
                f"({done} bytes received)."
            )

        total = (
            progress_data.get("total_bytes")
            or progress_data.get("total_bytes_estimate")
            or 0
        )
        if not total:
            return
        pct = int(done * 100 / total)
        for mark in sorted(milestones):
            if pct >= mark:
                milestones.discard(mark)
                progress(f"  ... {mark}% of {total / 1_048_576:.0f} MB")

    return _hook


def _youtube_403_message(yt_dlp) -> str:
    installed_version = yt_dlp.version.__version__
    return (
        "YouTube returned HTTP 403 even after an alternate player-client "
        "retry. Three common causes:\n"
        "- A VPN or datacenter IP is blocked on the media stream; try "
        "without the VPN.\n"
        f"- The installed yt-dlp version is {installed_version} and may be "
        "stale; run `pip install -U yt-dlp`.\n"
        "- No JavaScript runtime is available for extraction; installing "
        "Deno or Node is the common fix."
    )


def _download_with_youtube_403_retry(
    yt_dlp,
    base_opts: dict,
    fetch_url: str,
    kind: str,
    dest_dir: Path,
    meeting_id: int,
) -> None:
    """Download once, with one alternate-client retry for YouTube 403s."""
    first_opts = base_opts
    if kind == KIND_YOUTUBE:
        first_opts = _youtube_client_opts(
            base_opts, _YOUTUBE_PRIMARY_CLIENT
        )

    try:
        with yt_dlp.YoutubeDL(first_opts) as ydl:
            ydl.download([fetch_url])
        return
    except yt_dlp.utils.DownloadError as first_error:
        if kind != KIND_YOUTUBE or "403" not in str(first_error):
            raise

    _clear_partial_downloads(dest_dir, meeting_id)
    retry_opts = _youtube_client_opts(
        base_opts, _YOUTUBE_403_FALLBACK_CLIENT
    )
    retry_opts["continuedl"] = False
    try:
        with yt_dlp.YoutubeDL(retry_opts) as ydl:
            ydl.download([fetch_url])
    except yt_dlp.utils.DownloadError as retry_error:
        if "403" in str(retry_error):
            raise MediaError(_youtube_403_message(yt_dlp)) from retry_error
        raise


def classify_video_url(url: str) -> str:
    """Which V0 source class a meeting's video_url falls in."""
    raw = (url or "").strip()
    if not raw:
        return KIND_UNKNOWN
    low = raw.lower()
    for marker in _VENDOR_PAGE_MARKERS:
        if marker in low:
            return KIND_VENDOR_PAGE
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return KIND_UNKNOWN
    if any(host == s or host.endswith("." + s) for s in _YOUTUBE_HOST_SUFFIXES):
        return KIND_YOUTUBE
    path = urlparse(raw).path.lower()
    if path.endswith(_DIRECT_MEDIA_EXTENSIONS):
        return KIND_DIRECT_MEDIA
    return KIND_UNKNOWN


def unsupported_reason(kind: str, url: str) -> str:
    """The honest sentence for a source class V0 doesn't fetch (F8: name
    the class, never a generic failure)."""
    if kind == KIND_VENDOR_PAGE:
        return (
            f"this meeting's video lives behind a vendor player page ({url}) — "
            "resolving those needs the flagship's vendor-archive machinery, "
            "which this build doesn't carry yet. Skipping honestly."
        )
    return (
        f"this meeting's video URL isn't a source this build can fetch ({url}). "
        "Supported today: YouTube links and direct media files (.mp4/.m4a/...)."
    )


def download_audio(
    url: str,
    dest_dir: Path,
    meeting_id: int,
    *,
    progress: Callable[[str], None] = print,
) -> DownloadedMedia:
    """Fetch the meeting's audio (or full media when audio-only isn't
    offered) into dest_dir as <meeting_id>.<ext>. Reuses a finished file
    from a prior run; yt-dlp's .part machinery resumes interrupted ones.
    """
    kind = classify_video_url(url)
    if kind not in (KIND_YOUTUBE, KIND_DIRECT_MEDIA):
        raise MediaError(unsupported_reason(kind, url))
    assert_safe_media_url(url)

    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_media_file(dest_dir, meeting_id)
    if existing:
        progress(f"  reusing already-downloaded media: {existing.name}")
        return DownloadedMedia(existing, url, kind, existing.stat().st_size)

    import yt_dlp  # heavy import stays lazy — init/pick/pull never pay it

    ydl_opts = {
        # Single-file audio stream when the source offers one (YouTube
        # does), whole file otherwise (direct MP4) — no merge step, so no
        # system-ffmpeg requirement on the local path.
        "format": "worstaudio/worst",
        "outtmpl": str(dest_dir / f"{meeting_id}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,   # our milestone lines are the progress surface
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "max_filesize": MEDIA_DOWNLOAD_MAX_BYTES,
        "progress_hooks": [
            _download_progress_hook(dest_dir, meeting_id, progress)
        ],
    }
    # Redirect-resolve just before the fetch (after the reuse check, so a
    # cached file never pays a network round-trip). yt-dlp gets the validated
    # terminal URL, not the raw catalog URL (DIV-011).
    fetch_url = _safe_source_url(url, kind)
    try:
        with pinned_media_dns(fetch_url):
            _download_with_youtube_403_retry(
                yt_dlp, ydl_opts, fetch_url, kind, dest_dir, meeting_id
            )
    except yt_dlp.utils.DownloadError as e:
        raise MediaError(
            f"the download from the source site failed: {e}. "
            "If this repeats, the source may be gone or blocked — the "
            "flagship's catalog row is the place to check."
        ) from e

    found = _find_media_file(dest_dir, meeting_id)
    if not found:
        raise MediaError(
            "the source download finished but no media file landed — "
            f"nothing matching {meeting_id}.* in {dest_dir}."
        )
    return DownloadedMedia(found, url, kind, found.stat().st_size)


def download_video(
    url: str,
    dest_dir: Path,
    meeting_id: int,
    *,
    progress: Callable[[str], None] = print,
) -> DownloadedMedia:
    """Fetch the meeting's WATCHABLE video (≤720p, mp4-preferred,
    progressive so no ffmpeg merge is needed) into dest_dir — the
    embed-disabled rescue: when a channel disallows YouTube embedding,
    the local site plays this file instead, so the wall never shows.
    Council footage is talking heads + slides; 720p is plenty. Reuses a
    finished file from a prior run."""
    kind = classify_video_url(url)
    if kind not in (KIND_YOUTUBE, KIND_DIRECT_MEDIA):
        raise MediaError(unsupported_reason(kind, url))
    assert_safe_media_url(url)

    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_media_file(dest_dir, meeting_id)
    if existing:
        progress(f"  reusing already-downloaded video: {existing.name}")
        return DownloadedMedia(existing, url, kind, existing.stat().st_size)

    import yt_dlp  # heavy import stays lazy

    # With ffmpeg on PATH, merge the real 720p streams (YouTube serves
    # 720p+ video-only); without it, the best progressive file (tops
    # out ~360p on YouTube — watchable, slides get soft). Named in the
    # progress line so nobody wonders why quality differs by machine.
    import shutil
    have_ffmpeg = shutil.which("ffmpeg") is not None
    if have_ffmpeg:
        fmt = ("bv*[ext=mp4][height<=720]+ba[ext=m4a]/"
               "bv*[height<=720]+ba/"
               "best[height<=720]/best")
    else:
        fmt = "best[ext=mp4][height<=720]/best[height<=720]/best"
        progress("  (no ffmpeg on PATH — fetching the best single-file "
                 "stream; installing ffmpeg unlocks 720p)")

    ydl_opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / f"{meeting_id}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "max_filesize": MEDIA_DOWNLOAD_MAX_BYTES,
        "progress_hooks": [
            _download_progress_hook(dest_dir, meeting_id, progress)
        ],
    }
    # DIV-011: redirect-resolve to a validated terminal URL before fetch.
    fetch_url = _safe_source_url(url, kind)
    try:
        with pinned_media_dns(fetch_url):
            _download_with_youtube_403_retry(
                yt_dlp, ydl_opts, fetch_url, kind, dest_dir, meeting_id
            )
    except yt_dlp.utils.DownloadError as e:
        raise MediaError(
            f"the video download from the source site failed: {e}. "
            "The audio pipeline is unaffected — this only skips the "
            "local-playback rescue."
        ) from e

    found = _find_media_file(dest_dir, meeting_id)
    if not found:
        raise MediaError(
            "the video download finished but no file landed — "
            f"nothing matching {meeting_id}.* in {dest_dir}."
        )
    return DownloadedMedia(found, url, kind, found.stat().st_size)


def _find_media_file(dest_dir: Path, meeting_id: int) -> Optional[Path]:
    """The finished (non-.part) media file for a meeting, if any."""
    candidates = [
        p for p in dest_dir.glob(f"{meeting_id}.*")
        if p.is_file() and not p.name.endswith(".part") and not p.name.endswith(".ytdl")
    ]
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None

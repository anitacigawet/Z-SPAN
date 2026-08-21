"""The local server behind `zspan open` — stdlib only.

Two modes, resolved at startup:

SPA mode (the product): serves the REAL zspan.org client — the built
bundle from the repo's dist/public — rather than recreating a
lookalike, so a local workspace renders through the same client the
site does, with a
small shim layer standing in for the site's endpoints, backed by the
private workspace. The visitor-state client hides every operator
surface on its own (there is no operator here — it's all automatic);
endpoints outside the visitor path answer an honest JSON 404. The
hologram boot plays in the terminal itself (boot.py) before the browser
opens — the site is just the site.

Fallback mode: when no built client is found on disk (a pip install
without the repo), the lean render.py pages serve instead — a stand-in,
not the product, and the page says how to get the real one.

Loopback-only by design — this is the user's own workspace on their own
machine. The remote assets are the YouTube embeds and (online, when
reachable) the flagship's guide/coverage data.
"""
from __future__ import annotations

import collections
import json
import mimetypes
import os
import queue
import threading
import time
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional, Tuple

from zspan_cli import media, render, workspace
from zspan_cli.auth import current_auth
from zspan_cli.config import (
    PROCESSING_ACK_TEXT,
    has_processing_ack,
    home_jurisdiction,
    load_config,
    record_processing_ack,
)

_FLAGSHIP_PROXY_TIMEOUT = 6
_FLAGSHIP_QUOTES_TIMEOUT = 4
_FLAGSHIP_LINK_CACHE_TTL = 60.0
_FLAGSHIP_LINK_CACHE_LOCK = threading.Lock()
_FLAGSHIP_LINK_CACHE: tuple[float, str] = (float("-inf"), "down")
_FLAGSHIP_MEETING_QUOTES_LOCK = threading.Lock()
# No TTL by design: quotes + routing are separate client fetches joined by
# index. One flagship generation per serve run prevents a publish-state flip
# from rebinding a name to a different decision mid-session; the next launcher
# run picks up newly published data.
_FLAGSHIP_MEETING_QUOTES_CACHE: dict[int, list[dict]] = {}
_LOCAL_FOLLOWS_ERROR = (
    "following is a flagship-account feature; the local workspace doesn't store follows"
)


# ---------------------------------------------------------------- activity bus
#
# The HQ skybox's local feed. On the flagship, /api/hq/traffic-events
# streams visitor requests as fiber-optic stars; here the SAME stream
# carries the workspace's own activity — every request this server
# handles + every pipeline step (per-segment transcription, retrieval,
# synthesis calls, gate verdicts, Librarian queries). Flagship events
# stay contentless by design (visitor privacy); local events carry a
# `detail` payload because the only person watching is the machine's
# own user — that's what makes hover-a-star-and-read-it safe.

_ACTIVITY_LOCK = threading.Lock()
_ACTIVITY_SUBSCRIBERS: list = []  # queue.Queue per connected watcher
# A new serve process starts with this buffer empty and never persists it:
# summaries and timestamps describe this serve session's shift, not older work.
_RECENT_ACTIVITY: collections.deque = collections.deque(maxlen=200)

# Kinds → the flagship's path_class vocabulary (color ignores it; kept
# honest so a future flagship-side reader isn't surprised).
_KIND_PATH_CLASS = {
    "request": "api",
    "pipeline": "api",
    "media": "api",
    "transcription": "api",
    "index": "api",
    "retrieval": "api",
    "synthesis": "api",
    "gate": "api",
    "librarian": "api",
    "watcher": "other",
}


def publish_activity(kind: str, label: str, detail: str = "", *,
                     status: int = 200, path_class: str = "") -> None:
    """One event onto every watcher's queue. Never blocks and never
    raises — a slow or dead watcher must not slow the pipeline."""
    from datetime import datetime, timezone

    evt = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": int(status),
        "path_class": path_class or _KIND_PATH_CLASS.get(kind, "other"),
        "bot_classification": "human",
        "source": "local",
        "kind": kind,
        "label": str(label)[:160],
        "detail": str(detail or "")[:400],
    }
    with _ACTIVITY_LOCK:
        _RECENT_ACTIVITY.append((time.monotonic(), evt))
        subscribers = list(_ACTIVITY_SUBSCRIBERS)
    for q in subscribers:
        try:
            q.put_nowait(evt)
        except queue.Full:
            pass  # drop for that watcher; the sky misses one star


def _request_label(path: str) -> tuple[str, str]:
    """(human label, path_class) for a request star — words, not routes
    (the route itself rides in `detail`)."""
    parts = [p for p in path.split("/") if p]
    if parts[:1] == ["api"]:
        rest = parts[1:]
        if rest[:2] == ["channels", "tree"]:
            return "the channels tree", "broadcast"
        if rest[:1] == ["cities"] and len(rest) >= 3:
            from urllib.parse import unquote
            return f"the {unquote(rest[1])} catalog", "broadcast"
        if rest[:1] == ["notebook"]:
            return "a broadcast's outputs", "broadcast"
        if rest[:1] == ["preview"]:
            return "decision receipts", "broadcast"
        if rest[:1] in (["guide"], ["coverage"], ["corrections"], ["cast"]):
            return f"flagship {rest[0]} data", "guide"
        if rest[:2] == ["youtube", "embed-check"]:
            return "a video embed check", "api"
        if rest[:1] == ["rag-search"]:
            return "Librarian retrieval", "api"
        return path.lstrip("/"), "api"
    name = Path(path).name
    if "." in name:
        return name, "static"
    return "the site itself", "other"


# Repeating watcher heartbeats — the poll endpoints the page fires on a
# timer. Publishing those would fill the sky with the watcher's own
# pulse instead of real work.
def _is_activity_exempt(path: str) -> bool:
    if path in ("/api/hq/traffic-events", "/api/hq/status"):
        return True
    if path.startswith("/api/local/process/"):
        tail = path.rsplit("/", 1)[-1]
        return tail in ("status", "active")
    # Video playback fires a range request per seek — a seek storm
    # isn't pipeline work and would drown the sky.
    if path.startswith("/media/video/"):
        return True
    return False


def resolve_webapp_dir() -> Optional[Path]:
    """The built zspan.org client, if present: ZSPAN_WEBAPP_DIR → config
    webapp_dir → the repo's dist/public (run-from-clone) → the downloaded
    release bundle at ~/.zspan/webapp (the pip form). None →
    fallback mode."""
    candidates = []
    env = os.environ.get("ZSPAN_WEBAPP_DIR", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    try:
        cfg = load_config() or {}
        if cfg.get("webapp_dir"):
            candidates.append(Path(str(cfg["webapp_dir"])).expanduser())
    except Exception:
        pass
    candidates.append(
        Path(__file__).resolve().parents[2] / "council_navigator" / "dist" / "public"
    )
    from zspan_cli.bundle import webapp_install_dir
    candidates.append(webapp_install_dir())
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "zspan-local"
    webapp_dir: Optional[Path] = None  # set by start_server
    _last_status: int = 200  # what the response actually said — the star's color

    # ------------------------------------------------------------ plumbing

    def _send_json(self, status: int, payload) -> None:
        self._last_status = status
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        self._last_status = status
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass  # the CLI's own lines are the log; per-request noise isn't

    # --------------------------------------------------- loopback-only guard
    #
    # A local HTTP server + a browser is the classic cross-origin /
    # DNS-rebinding target: any web page the user visits can POST to
    # http://127.0.0.1:<port> and here spend the user's API key (the
    # Process + Librarian-synthesize routes) or OVERWRITE the stored key
    # (the cloud-key paste path in _kick_process), and a rebinding attack
    # could read the private workspace over the shim GETs. The random
    # OS-assigned port is obscurity, not a wall. Two client-agnostic checks
    # close it — no per-session secret, so the real zspan.org bundle serves
    # unmodified:
    #   * Host must be loopback — the browser sends the hostname the user
    #     navigated to; a rebind points evil.com at 127.0.0.1 but still
    #     sends `Host: evil.com`, so a loopback-only allowlist kills it.
    #   * Mutating requests also require a loopback (or absent) Origin —
    #     the browser always attaches Origin on a cross-site POST, so a
    #     foreign Origin is a malicious page and is refused.
    _LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

    @staticmethod
    def _hostname_only(host: str) -> str:
        host = host.strip()
        if host.startswith("["):  # IPv6 literal, e.g. [::1]:8799
            return host[1:].split("]", 1)[0]
        return host.rsplit(":", 1)[0] if ":" in host else host

    def _host_is_loopback(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return True  # HTTP/1.0 / non-browser tooling; browsers always send Host
        return self._hostname_only(host).lower() in self._LOOPBACK_HOSTS

    def _origin_is_loopback(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True  # no Origin → not a browser cross-site request
        from urllib.parse import urlparse
        try:
            return (urlparse(origin).hostname or "").lower() in self._LOOPBACK_HOSTS
        except ValueError:
            return False

    def _reject_forbidden(self) -> None:
        self._last_status = 403
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _guard_ok(self, *, mutating: bool) -> bool:
        if not self._host_is_loopback():
            self._reject_forbidden()
            return False
        if mutating and not self._origin_is_loopback():
            self._reject_forbidden()
            return False
        return True

    def _publish_request(self, path: str) -> None:
        if _is_activity_exempt(path):
            return
        label, path_class = _request_label(path)
        publish_activity(
            "request", label, f"{self.command} {path}",
            status=self._last_status, path_class=path_class,
        )

    # ------------------------------------------------------------ routing

    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        if not self._guard_ok(mutating=False):
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/hq/traffic-events":
            self._serve_activity_stream()
            return
        self._last_status = 200
        try:
            if path.startswith("/media/video/"):
                self._route_local_video(path)
            elif path.startswith("/api/"):
                self._route_api(path)
            elif self.webapp_dir is not None:
                self._route_spa(path)
            else:
                self._route_fallback(path.rstrip("/") or "/")
        except Exception as e:  # a route bug must never kill the server
            self._send_html(500, render.error_page(f"{type(e).__name__}: {e}"))
        self._publish_request(path)

    # ------------------------------------------------------------ SSE

    def _serve_activity_stream(self) -> None:
        """One watcher's live feed — the flagship's /api/hq/traffic-events
        contract (SSE, `data: <json>` per event), fed by the local bus.
        Holds this handler thread until the watcher disconnects; the
        server is threaded and its threads are daemons, so open skies
        never block shutdown."""
        q: "queue.Queue" = queue.Queue(maxsize=256)
        with _ACTIVITY_LOCK:
            _ACTIVITY_SUBSCRIBERS.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            # The connect itself is real activity — every watcher's sky
            # (including this one's) gets the star.
            publish_activity("watcher", "a watcher connected to the sky",
                             "live activity feed opened")
            while True:
                try:
                    evt = q.get(timeout=15)
                    payload = json.dumps(evt, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # watcher left — normal
        finally:
            with _ACTIVITY_LOCK:
                if q in _ACTIVITY_SUBSCRIBERS:
                    _ACTIVITY_SUBSCRIBERS.remove(q)

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._guard_ok(mutating=False):
            return
        # Some players (Safari notably) HEAD-probe media before ranged
        # GETs. Headers only, no body — the video route handles the rest.
        path = self.path.split("?", 1)[0]
        if path.startswith("/media/video/"):
            self._route_local_video(path, head_only=True)
            return
        self.send_response(405)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard_ok(mutating=True):
            return
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.split("/") if p]
        self._last_status = 200
        body = {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, OSError):
            body = {}
        # Local mutations: kick the pipeline for a meeting (the site's
        # Process button) + the two Librarian calls (retrieval + the
        # loopback synthesis on the stored key). Everything else read-only.
        # The Librarian pair publish their own richer activity events, so
        # the generic request star is skipped for them.
        if parts == ["api", "auth", "logout"]:
            # The client ignores this body and reloads into a still-signed-in
            # state by design: CLI identity is config-backed, not a cookie.
            # The note remains useful to anyone calling the endpoint directly.
            self._send_json(200, {
                "ok": True,
                "note": "local sign-in is managed from the terminal: zspan logout",
            })
            self._publish_request(path)
            return
        if parts == ["api", "follows"]:
            self._send_json(200, {"ok": False, "error": _LOCAL_FOLLOWS_ERROR})
            self._publish_request(path)
            return
        if parts[:3] == ["api", "local", "process"] and len(parts) == 5 \
                and parts[3].isdigit() and parts[4] == "approval":
            self._send_json(*_submit_process_approval(int(parts[3]), body))
            self._publish_request(path)
            return
        if parts[:3] == ["api", "local", "process"] and len(parts) == 4 \
                and parts[3].isdigit():
            self._send_json(*_kick_process(int(parts[3]), body))
            self._publish_request(path)
            return
        if parts[:2] == ["api", "rag-search"] and len(parts) == 3 \
                and parts[2].isdigit():
            conn = workspace.connect()
            try:
                self._send_json(*_rag_search(conn, int(parts[2]), body))
            finally:
                conn.close()
            return
        if parts[:4] == ["api", "local", "librarian", "synthesize"]:
            self._send_json(*_librarian_synthesize(body))
            return
        self._send_json(404, {
            "success": False,
            "error": "this local workspace build serves read-only views; "
                     "the endpoint you called isn't part of it",
        })
        self._publish_request(path)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._guard_ok(mutating=True):
            return
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.split("/") if p]
        self._last_status = 200
        if parts == ["api", "follows"]:
            self._send_json(200, {"ok": False, "error": _LOCAL_FOLLOWS_ERROR})
        else:
            self._send_json(404, {
                "success": False,
                "error": "this local workspace build serves read-only views; "
                         "the endpoint you called isn't part of it",
            })
        self._publish_request(path)

    # ------------------------------------------------------------ local video

    def _route_local_video(self, path: str, head_only: bool = False) -> None:
        """The embed-disabled rescue's playback: watchable copies from
        the workspace's media/video dir, served with HTTP Range support
        — seeks and the karaoke clock need partial reads, and a
        whole-file read per request would be gigabytes."""
        from zspan_cli.config import videos_dir

        vdir = videos_dir().resolve()
        target = (vdir / Path(path).name).resolve()
        try:
            target.relative_to(vdir)
        except ValueError:
            self._send_json(404, {"error": "no such local video"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "no such local video"})
            return

        size = target.stat().st_size
        ctype = mimetypes.guess_type(str(target))[0] or "video/mp4"
        start, end, status = 0, size - 1, 200
        range_header = (self.headers.get("Range") or "").strip()
        if range_header.startswith("bytes="):
            spec = range_header[6:].split(",")[0].strip()
            s, _, e = spec.partition("-")
            try:
                if s:
                    start = int(s)
                    if e:
                        end = min(int(e), size - 1)
                elif e:  # suffix form: the last N bytes
                    start = max(0, size - int(e))
            except ValueError:
                start, end = 0, size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206

        self._last_status = status
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        try:
            with target.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the player hung up mid-stream — normal during seeks

    # ------------------------------------------------------------ SPA mode

    def _route_spa(self, path: str) -> None:
        target = self._safe_static(path)
        if target is not None:
            self._send_file(target)
            return
        # SPA fallback — the client routes via ?view= on /; unknown
        # extensionless paths get index.html, real missing assets get 404.
        if "." not in Path(path).name:
            self._send_file(self.webapp_dir / "index.html")
            return
        self._send_json(404, {"error": "asset not found in the local bundle"})

    def _safe_static(self, path: str) -> Optional[Path]:
        rel = path.lstrip("/") or "index.html"
        target = (self.webapp_dir / rel).resolve()
        try:
            target.relative_to(self.webapp_dir.resolve())
        except ValueError:
            return None  # traversal attempt — outside the bundle
        return target if target.is_file() else None

    def _send_file(self, target: Path) -> None:
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if target.suffix in (".js", ".css", ".png", ".jpg", ".mp4", ".webm", ".woff2"):
            self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------ API shims

    def _route_api(self, path: str) -> None:
        parts = [p for p in path.split("/") if p][1:]  # drop leading "api"
        conn = workspace.connect()
        try:
            if parts == ["auth", "me"]:
                # The site mirrors the CLI's terminal sign-in read-only. This
                # is deliberately reloaded per request and is not a cookie session.
                auth = current_auth(load_config())
                if auth is None:
                    self._send_json(200, {"authenticated": False, "user": None})
                else:
                    display_name = auth.get("display_name")
                    self._send_json(200, {
                        "authenticated": True,
                        "user": {
                            # CLI auth has no flagship database user id; zero is
                            # the documented CLI-session sentinel.
                            "user_id": 0,
                            "email": auth.get("email")
                            if isinstance(auth.get("email"), str) else "",
                            "display_name": display_name
                            if isinstance(display_name, str) else None,
                            "avatar_url": None,
                            "role": "light",
                            "is_owner": False,
                            "is_operator_search_principal": False,
                            "follows": [],
                        },
                    })
            elif parts == ["auth", "google", "login"]:
                # A direct hit gets an honest local explanation instead of
                # falling through to the flagship-only endpoint error.
                auth = current_auth(load_config())
                if auth is not None and isinstance(auth.get("email"), str):
                    state = (
                        "You’re already signed in as <strong>"
                        f"{escape(auth['email'])}</strong> via the CLI."
                    )
                else:
                    state = (
                        "Sign in from the terminal: <code>zspan login</code>."
                    )
                self._send_html(200, (
                    "<!doctype html><html lang=\"en\"><head>"
                    "<meta charset=\"utf-8\"><meta name=\"viewport\" "
                    "content=\"width=device-width,initial-scale=1\">"
                    "<meta name=\"robots\" content=\"noindex\">"
                    "<title>Z-SPAN local sign-in</title></head><body>"
                    "<main><h1>This is your local workspace copy.</h1>"
                    f"<p>{state}</p><p>Close this tab and continue.</p>"
                    "</main></body></html>"
                ))
            elif parts == ["follows"]:
                # No local follow store: this canonical empty result also
                # clears any optimistic follow state after a refused mutation.
                self._send_json(200, {"success": True, "follows": []})
            elif parts[:2] == ["system", "status"]:
                self._send_json(200, {"ok": True, "status": "ok",
                                      "mode": "local-workspace"})
            elif parts == ["hq", "status"]:
                self._send_json(200, _hq_status(conn))
            elif parts[:2] == ["channels", "tree"]:
                self._send_json(200, _channels_tree(conn))
            elif len(parts) == 3 and parts[0] == "cities" and parts[2] == "meetings":
                self._send_json(200, _city_meetings(conn, parts[1]))
            elif len(parts) == 2 and parts[0] == "notebook" and parts[1].isdigit():
                payload, status = _notebook(conn, int(parts[1]))
                self._send_json(status, payload)
            elif len(parts) == 3 and parts[0] == "meetings" and parts[2] == "publish-status":
                self._send_json(200, _publish_status(conn, parts[1]))
            elif parts[:1] == ["preview"] and len(parts) == 3 \
                    and parts[2].isdigit():
                payload, status = _preview(conn, parts[1], int(parts[2]))
                self._send_json(status, payload)
            elif parts[:1] == ["preview"]:
                self._send_json(200, {
                    "success": True, "decisions": [], "quotes": [],
                    "recusals": [], "routing": None, "items": [],
                })
            elif parts[:1] == ["cast"]:
                status, payload = _flagship_proxy(path)
                if payload.get("offline"):
                    # Cast's client contract is members-shaped; the generic
                    # flagship fallback has no reason to carry this key.
                    payload["members"] = []
                self._send_json(status, payload)
            elif parts[:1] == ["settings"]:
                self._send_json(200, {})
            elif parts[:2] == ["calendar", "events"]:
                self._send_json(200, {"success": True, "events": []})
            elif parts[:1] in (["guide"], ["coverage"], ["corrections"]):
                self._send_json(*_flagship_proxy(path))
            elif parts[:2] == ["youtube", "embed-check"]:
                self._send_json(200, _embed_check(self.path))
            elif parts[:2] == ["local", "process"] and len(parts) == 4 \
                    and parts[3] == "status" and parts[2].isdigit():
                self._send_json(200, _process_status(int(parts[2])))
            elif parts[:3] == ["local", "process", "setup"]:
                # What the Process split-button needs to know: is the
                # faster cloud path ready on a stored key? (The key
                # itself never travels to the page — presence only.)
                try:
                    cfg = load_config() or {}
                except Exception:
                    cfg = {}
                from zspan_cli.providers import (
                    CODEX_DEFAULT_MODEL,
                    codex_available,
                )
                self._send_json(200, {
                    "cloud_ready": bool((cfg.get("api_keys") or {}).get("openai")),
                    # The synthesis-engine choice: the installed Codex
                    # CLI (user's own subscription) — offered only when
                    # the binary actually exists on this machine.
                    "codex_available": codex_available(cfg),
                    "codex_model": CODEX_DEFAULT_MODEL,
                })
            elif parts[:3] == ["local", "librarian", "setup"]:
                self._send_json(200, _librarian_setup())
            elif parts[:3] == ["local", "process", "active"]:
                # The HQ page's processing indicator: is a pipeline run
                # live anywhere right now, and for which meeting?
                self._send_json(200, _process_active(conn))
            else:
                self._send_json(404, {
                    "success": False,
                    "error": "this endpoint isn't part of the local "
                             "workspace build (operator and pipeline "
                             "surfaces live on the flagship)",
                })
        finally:
            conn.close()

    # ------------------------------------------------------------ fallback

    def _route_fallback(self, path: str) -> None:
        if path == "/":
            conn = workspace.connect()
            try:
                rows = workspace.all_meetings(conn)
                outputs_by_id = {
                    int(r["id"]): workspace.load_outputs(conn, int(r["id"]))
                    for r in rows
                }
                self._send_html(200, render.index_page(rows, outputs_by_id))
            finally:
                conn.close()
        elif path.startswith("/meeting/"):
            raw_id = path.rsplit("/", 1)[1]
            if not raw_id.isdigit():
                self._send_html(404, render.not_found_page())
                return
            conn = workspace.connect()
            try:
                row = workspace.get_meeting(conn, int(raw_id))
                if row is None:
                    self._send_html(404, render.not_found_page())
                    return
                outputs = workspace.load_outputs(conn, int(raw_id))
                transcript = _load_transcript(row)
                self._send_html(200, render.meeting_page(row, outputs, transcript))
            finally:
                conn.close()
        else:
            self._send_html(404, render.not_found_page())


# ---------------------------------------------------------------- shim data


def _local_video_url(meeting_id: int) -> Optional[str]:
    """The watchable local copy's serving path, when the embed-disabled
    rescue has fetched one — the player-facing overlay: an .mp4 URL
    classifies as direct media, so the html5 adapter (full clock,
    karaoke, seeks) takes over and the embed wall never renders."""
    from zspan_cli.config import videos_dir

    vdir = videos_dir()
    if not vdir.is_dir():
        return None
    candidates = [
        p for p in vdir.glob(f"{meeting_id}.*")
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda p: p.stat().st_size)
    return f"/media/video/{best.name}"


def _local_meeting_row(record, *, citation_ready: bool = True) -> tuple[dict, bool, bool]:
    """Overlay local video/process truth onto one workspace meeting.

    Returns ``(row, visible, processed)``.  A meeting belongs in local
    channel lists only when this machine can process its source now, or
    when the workspace already holds processing state for it. Cached outputs
    count as processing state even if a prior interrupted run never stamped
    ``processed_at``; the reading surface calls them finished only when
    per-decision citation coverage is ready.
    """
    values = dict(record)
    row = json.loads(values["source_row_json"])
    raw_url = row.get("video_url")
    video_url = raw_url.strip() if isinstance(raw_url, str) else ""
    klass = media.classify_video_url(video_url) if video_url else "none"
    processable = klass in (media.KIND_YOUTUBE, media.KIND_DIRECT_MEDIA)
    has_cached_outputs = int(values.get("output_count") or 0) > 0
    has_processing_state = bool(values.get("processed_at")) or has_cached_outputs
    processed = has_processing_state and citation_ready

    row["local_video_class"] = klass
    row["local_processable"] = processable
    row.pop("availability", None)
    if values.get("id") is not None:
        # The SPA is speaking to this workspace, so its route identity is
        # the local primary key even when the source row carried a legacy
        # flagship integer (or no integer at all for a v1 handoff).
        row["id"] = int(values["id"])
    if processed:
        row["notebook_id"] = f"local-{int(values['id'])}"
        row["is_published"] = True
        row["episode_tagline"] = (
            values.get("local_tagline") or "Processed locally"
        )
    else:
        row["notebook_id"] = None
        row["is_published"] = False
        row["episode_tagline"] = None
        row["episode_tags"] = None
    return row, processable or has_processing_state, processed


def _channels_tree(conn) -> dict:
    """The flagship's /api/channels/tree shape, built from the workspace.

    Two contract details the client depends on (ChannelsPage:1493-1530):
    per-city `status` — "live" (≥1 locally-processed broadcast) or
    "cached" (processable meetings pulled, nothing processed yet) — feeds
    the county rollup (no status reads as scaffold → "COMING SOON"); and
    county names travel WITHOUT the " County" suffix (the site's vocabulary).
    Counts and date bounds cover the same visible rows the city endpoint
    serves, so an unusable source cannot inflate the channel navigation."""
    from datetime import datetime, timezone

    tree: dict = {}
    try:
        home = home_jurisdiction(load_config())
    except Exception:
        home = None
    where = ""
    params: tuple = ()
    home_city = ((home or {}).get("city") or "").strip()
    if home_city:
        where = (
            "WHERE NOT (import_source = 'handoff' "
            "AND LOWER(COALESCE(city, '')) != LOWER(?))"
        )
        params = (home_city,)
    for r in conn.execute(
        """SELECT m.id, m.state, m.county, m.city, m.meeting_date,
                  m.source_row_json, m.processed_at,
                  (SELECT COUNT(*) FROM outputs o WHERE o.meeting_id = m.id)
                  AS output_count
           FROM meetings m """ + where,
        params,
    ):
        has_processing_state = bool(r["processed_at"]) or int(r["output_count"] or 0) > 0
        citation_ready = (
            _meeting_citation_ready(conn, int(r["id"]))
            if has_processing_state else True
        )
        _row, visible, processed = _local_meeting_row(
            r, citation_ready=citation_ready,
        )
        if not visible:
            continue
        state = r["state"] or "—"
        county = (r["county"] or "—").strip()
        if county.lower().endswith(" county"):
            county = county[: -len(" county")].strip()
        cities = tree.setdefault(state, {}).setdefault(county, {})
        city = cities.setdefault(r["city"], {
            "name": r["city"],
            "meeting_count": 0,
            "broadcast_count": 0,
            "last_meeting": None,
            "first_meeting": None,
            "status": "cached",
        })
        city["meeting_count"] += 1
        city["broadcast_count"] += int(processed)
        meeting_date = r["meeting_date"]
        if meeting_date:
            if (
                city["first_meeting"] is None
                or meeting_date < city["first_meeting"]
            ):
                city["first_meeting"] = meeting_date
            if (
                city["last_meeting"] is None
                or meeting_date > city["last_meeting"]
            ):
                city["last_meeting"] = meeting_date
        if processed:
            city["status"] = "live"
    return {
        "ok": True,
        "states": [
            {"state": state,
             "counties": [
                 {"county": county,
                  "cities": sorted(cities.values(), key=lambda c: c["name"])}
                 for county, cities in sorted(counties.items())
             ]}
            for state, counties in sorted(tree.items())
        ],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "local-workspace",
    }


def _city_meetings(conn, city: str) -> dict:
    """Replay the flagship's catalog rows with LOCAL process-state
    overlaid. The client's watchable-card contract (ChannelsPage:928-947):
    notebook_id truthy + episode_tagline/tags + is_published=true. The
    flagship's own publish state is meaningless here — what's watchable
    locally is what THIS workspace processed, so the overlay sets those
    markers from local state (and clears any flagship ones, so a
    flagship-published broadcast the user never processed doesn't render
    as a card whose content this machine doesn't hold)."""
    from urllib.parse import unquote

    city = unquote(city)
    rows = conn.execute(
        """SELECT m.id, m.source_row_json, m.pulled_at, m.processed_at,
                  (SELECT COUNT(*) FROM outputs o WHERE o.meeting_id = m.id)
                  AS output_count,
                  (SELECT content FROM outputs o
                   WHERE o.meeting_id = m.id AND o.output_type = 'episode_tagline')
                  AS local_tagline
           FROM meetings m WHERE m.city = ?
           ORDER BY m.meeting_date DESC""",
        (city,),
    ).fetchall()
    events = []
    for r in rows:
        has_processing_state = bool(r["processed_at"]) or int(r["output_count"] or 0) > 0
        citation_ready = (
            _meeting_citation_ready(conn, int(r["id"]))
            if has_processing_state else True
        )
        row, visible, _processed = _local_meeting_row(
            r, citation_ready=citation_ready,
        )
        if visible:
            events.append(row)
    return {
        "success": True,
        "events": events,
        "count": len(events),
        "source": "workspace",
        "is_stale": False,
        "last_scraped": max((r["pulled_at"] for r in rows), default=None),
    }


def _ccta_word_timings(
    content: str | None, words: list[dict],
) -> list[list[dict]]:
    """Index-preserving karaoke sidecar for the stored CCTA JSON array."""
    if not content or not words:
        return []
    from zspan_cli import discussion
    from zspan_cli.gate import split_ccta

    elements = split_ccta(content)
    if elements is None:
        return []
    timings: list[list[dict]] = []
    for element in elements:
        if not isinstance(element, dict):
            timings.append([])
            continue
        quote_text = element.get("quote_text")
        if not isinstance(quote_text, str):
            timings.append([])
            continue
        timings.append(discussion.align_verbatim_quote(
            quote_text, words, element.get("video_timestamp_seconds"),
        ))
    return timings


def _notebook(conn, meeting_id: int):
    """The flagship's /api/notebook/<id> shape over workspace outputs.
    A locally-processed meeting is 'published' to its own operator-less
    workspace — approved_at carries processed_at, or the latest cached
    output timestamp when an interrupted run did not stamp the meeting."""
    row = workspace.get_meeting(conn, meeting_id)
    if row is None:
        return {"success": False,
                "error": f"No meeting found with id={meeting_id}"}, 404
    outputs = workspace.load_outputs(conn, meeting_id)
    cached_output_at = max(
        (output["created_at"] or "" for output in outputs.values()),
        default="",
    ) or None
    key_decisions = (outputs.get("key_decisions") or {}).get("content") or ""
    discussion_payload = _discussion_payload(conn, meeting_id)
    citation_coverage = _citation_coverage(key_decisions, discussion_payload)
    citation_ready = citation_coverage["state"] != "citation_incomplete"
    approved_at = (row["processed_at"] or cached_output_at) if citation_ready else None
    transcript = _load_transcript(row)
    transcript_words = transcript.get("words") if transcript else []
    payload_outputs = {}
    for otype, o in outputs.items():
        payload_output = {
            "content": o["content"],
            "error": None,
            "generated_at": o["created_at"],
            "prompt_filename": f"{otype}.md",
            "prompt_version": o["model"],
            "gate_status": o["gate_status"],
            "ribbon_token": o["ribbon_token"],
            "registration_state": o["registration_state"],
        }
        if otype == "community_calls_to_action":
            payload_output["karaoke_word_timings"] = _ccta_word_timings(
                o["content"], transcript_words or [],
            )
        payload_outputs[otype] = payload_output
    n = len(payload_outputs)
    required_citations = len(citation_coverage["required_decision_indices"])
    produced_citations = len(citation_coverage["produced_decision_indices"])
    completeness_reasons = []
    if not citation_ready:
        missing = ", ".join(
            str(index) for index in citation_coverage["missing_decision_indices"]
        )
        completeness_reasons.append(
            f"Key-decision citations missing for decision(s): {missing}."
        )
    return {
        "success": True,
        "local_workspace": True,
        "meeting_id": int(row["id"]),
        "meeting_title": row["title"],
        "meeting_date": row["meeting_date"],
        "city": row["city"],
        "county": row["county"],
        "notebook_id": None,
        # The embed-disabled rescue's local copy wins when present —
        # the .mp4 path routes the site's player to the html5 adapter.
        "video_url": _local_video_url(meeting_id) or row["video_url"],
        "approved_at": approved_at,
        "completeness": {
            "complete": n > 0 and citation_ready,
            "required_ok": n + produced_citations,
            "required_total": n + required_citations,
            "reasons": completeness_reasons,
            "citation_coverage": citation_coverage,
        },
        "outputs": payload_outputs,
    }, 200


def _publish_status(conn, raw_id: str) -> dict:
    row = workspace.get_meeting(conn, int(raw_id)) if raw_id.isdigit() else None
    cached_output_at = None
    if row is not None:
        cached = conn.execute(
            "SELECT MAX(created_at) AS latest FROM outputs WHERE meeting_id = ?",
            (int(row["id"]),),
        ).fetchone()
        cached_output_at = cached["latest"] if cached else None
    processed_at = (row["processed_at"] or cached_output_at) if row else None
    if row is None:
        citation_coverage = _citation_coverage("", {})
    else:
        outputs = workspace.load_outputs(conn, int(row["id"]))
        key_decisions = (outputs.get("key_decisions") or {}).get("content") or ""
        citation_coverage = _citation_coverage(
            key_decisions, _discussion_payload(conn, int(row["id"])),
        )
    citation_ready = citation_coverage["state"] != "citation_incomplete"
    published_at = processed_at if citation_ready else None
    processed = bool(published_at)
    meeting = {
        "is_published": processed,
        "published_at": published_at,
        "citation_coverage": citation_coverage,
    }
    return {
        "success": True,
        "is_published": processed,
        "approved_at": published_at,
        "meeting": meeting,
    }


_HQ_DEPARTMENTS = (
    ("pipeline-operator", "Pipeline Operator", "PIPELINE OPS", ("pipeline",)),
    ("ingestion", "Ingestion / Media", "INGEST", ("media",)),
    ("transcription", "Whisper Transcription", "WHISPER", ("transcription",)),
    ("synthesis", "Synthesis / RAG", "RAG", ("synthesis", "retrieval", "index")),
    ("verification", "Grounding Gate", "VERIFY", ("gate",)),
)
_HQ_STAGE_DEPARTMENTS = {
    kind: department_id
    for department_id, _name, _short, kinds in _HQ_DEPARTMENTS[1:]
    for kind in kinds
}
_HQ_EVENT_MODELS = {
    "media": "Media",
    "transcription": "Whisper",
    "index": "Local index",
    "retrieval": "Local index",
    "gate": "Grounding gate",
}


def _flagship_base_url() -> str:
    """The same point-of-use flagship resolution used by every proxy/probe."""
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    return (os.environ.get("ZSPAN_FLAGSHIP_URL", "").strip()
            or cfg.get("flagship_url") or "https://zspan.org")


def _flagship_link_status() -> str:
    """Reachability only, cached so the HQ page cannot become a poller."""
    import requests

    global _FLAGSHIP_LINK_CACHE
    now = time.monotonic()
    with _FLAGSHIP_LINK_CACHE_LOCK:
        cached_at, cached_status = _FLAGSHIP_LINK_CACHE
        if now - cached_at < _FLAGSHIP_LINK_CACHE_TTL:
            return cached_status
        try:
            response = requests.get(
                _flagship_base_url().rstrip("/") + "/api/system/status",
                timeout=1.5,
                headers={"User-Agent": "zspan-cli-local-open"},
            )
            status = "up" if response.status_code < 500 else "down"
        except Exception:
            status = "down"
        _FLAGSHIP_LINK_CACHE = (time.monotonic(), status)
        return status


def _latest_activity(activity: list, kinds: tuple[str, ...], *,
                     since: Optional[float] = None):
    matches = [
        item for item in activity
        if item[1].get("kind") in kinds
        and (since is None or item[0] >= since)
    ]
    return max(matches, key=lambda item: item[0]) if matches else None


def _hq_status(conn) -> dict:
    """The local machine's real HQ departments and capabilities."""
    from zspan_cli.providers import codex_available

    with _ACTIVITY_LOCK:
        activity = list(_RECENT_ACTIVITY)
    process_identity = _process_active(conn)
    with _PROCESS_LOCK:
        process_state = {
            "meeting_id": _PROCESS_STATE["meeting_id"],
            "running": bool(_PROCESS_STATE["running"]),
            "engine": _PROCESS_STATE.get("engine"),
            "run_started_monotonic": _PROCESS_STATE.get("run_started_monotonic"),
        }
    if process_identity.get("meeting_id") != process_state["meeting_id"]:
        process_identity = {}

    running = process_state["running"]
    run_started = process_state["run_started_monotonic"]
    run_activity = (
        [item for item in activity if item[0] >= run_started]
        if running and isinstance(run_started, (int, float)) else []
    )
    newest_stage = max(
        (item for item in run_activity
         if item[1].get("kind") in _HQ_STAGE_DEPARTMENTS),
        key=lambda item: item[0],
        default=None,
    )
    running_stage_id = (
        _HQ_STAGE_DEPARTMENTS[newest_stage[1]["kind"]]
        if newest_stage is not None else None
    )

    departments = []
    for department_id, name, short, kinds in _HQ_DEPARTMENTS:
        latest = _latest_activity(activity, kinds)
        is_running = running and (
            department_id == "pipeline-operator"
            or department_id == running_stage_id
        )
        active_event = _latest_activity(run_activity, kinds) if is_running else None
        objective = active_event[1]["label"] if active_event else ""
        detail = active_event[1]["detail"] if active_event else ""
        event_kind = active_event[1]["kind"] if active_event else ""

        if department_id == "pipeline-operator" and is_running and not active_event:
            objective = str(process_identity.get("title") or "")
            detail = " · ".join(
                str(process_identity.get(field) or "")
                for field in ("city", "meeting_date")
                if process_identity.get(field)
            )
        if department_id == "pipeline-operator":
            model = "Pipeline"
        elif event_kind == "synthesis":
            model = str(process_state["engine"] or "Engine")
        else:
            model = _HQ_EVENT_MODELS.get(event_kind, "")

        agents = []
        if is_running:
            agents.append({
                "id": f"{department_id}-{event_kind or 'run'}",
                "model": model,
                "status": "in-progress",
                "objective": objective,
                "detail": detail,
            })
        departments.append({
            "id": department_id,
            "name": name,
            "short": short,
            "kind": "pipeline",
            "state": "running" if is_running else "idle",
            "currentObjective": objective if is_running else None,
            "recentSummary": (
                None if is_running or latest is None
                else f"Last: {latest[1]['label']}"
            ),
            "activeAgentCount": len(agents),
            "lastActiveAt": latest[1].get("ts") if latest else None,
            "escalationCount": 0,
            "agents": agents,
        })

    try:
        conn.execute("SELECT 1").fetchone()
        workspace_status = "up"
    except Exception:
        workspace_status = "down"
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    api_keys = cfg.get("api_keys") or {}
    keyed_engine_available = isinstance(api_keys, dict) and any(
        isinstance(value, str) and bool(value.strip())
        for value in api_keys.values()
    )
    engine_status = "up" if codex_available(cfg) or keyed_engine_available else "down"
    services = [
        {"id": "api", "label": "Local server", "status": "up", "isCore": True},
        {"id": "workspace", "label": "Workspace DB",
         "status": workspace_status, "isCore": True},
        {"id": "flagship", "label": "Flagship link",
         "status": _flagship_link_status(), "isCore": False},
        {"id": "engine", "label": "Synthesis engine",
         "status": engine_status, "isCore": False},
    ]
    if any(service["isCore"] and service["status"] == "down"
           for service in services):
        overall_status = "maintenance"
    elif any(service["status"] != "up" for service in services):
        overall_status = "degraded"
    else:
        overall_status = "operational"

    return {
        "ok": True,
        "source": "local-workspace",
        "building": {"overallStatus": overall_status},
        "departments": departments,
        "infrastructure": {"services": services},
        "funding": {
            "balanceUsd": 0,
            "monthlyBurnUsd": 0,
            "runwayMonths": 0,
            "lastUpdated": None,
            "source": "not applicable — local workspace",
        },
    }


def _flagship_proxy(path: str):
    """Guide/coverage/corrections/cast pass through to the flagship when
    reachable — the operator's 'the guide thing can be an endpoint that
    we serve' call. Offline → honest empty, never a fake."""
    import requests

    base = _flagship_base_url()
    try:
        resp = requests.get(base.rstrip("/") + path,
                            timeout=_FLAGSHIP_PROXY_TIMEOUT,
                            headers={"User-Agent": "zspan-cli-local-open"})
        if resp.status_code == 200:
            return 200, resp.json()
    except Exception:
        pass
    return 200, {
        "ok": False, "success": False, "offline": True,
        "error": "the Z-SPAN endpoint server isn't reachable right now — "
                 "this surface shows live flagship data when online",
        "channels": [], "cities": [], "corrections": [], "events": [],
    }


def _has_valid_word_timings(row: dict) -> bool:
    """The public client's karaoke requires a non-empty numeric ms array."""
    timings = row.get("word_timings")
    if not isinstance(timings, list) or not timings:
        return False
    for timing in timings:
        if not isinstance(timing, dict):
            return False
        for field in ("start_ms", "end_ms"):
            value = timing.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
    return True


def _eligible_flagship_quote(row) -> bool:
    """Mirror Cast's public verification filter, then require exact timing."""
    if not isinstance(row, dict):
        return False
    status = str(row.get("verified_status") or "").strip().lower()
    if status not in {"pending", "verified", "approved"}:
        return False
    return _has_valid_word_timings(row)


def _flagship_meeting_quotes(flagship_meeting_id: int) -> list[dict]:
    """Fetch one serve-lifetime generation of public, timed named quotes."""
    import requests

    with _FLAGSHIP_MEETING_QUOTES_LOCK:
        if flagship_meeting_id in _FLAGSHIP_MEETING_QUOTES_CACHE:
            return _FLAGSHIP_MEETING_QUOTES_CACHE[flagship_meeting_id]

        rows: list[dict] = []
        try:
            response = requests.get(
                _flagship_base_url().rstrip("/")
                + f"/api/quotes/meeting/{flagship_meeting_id}",
                timeout=_FLAGSHIP_QUOTES_TIMEOUT,
                headers={"User-Agent": "zspan-cli-local-open"},
            )
            if response.status_code == 200:
                payload = response.json()
                fetched = (
                    payload.get("quotes")
                    if isinstance(payload, dict) and payload.get("success")
                    and not payload.get("offline") else []
                )
                if isinstance(fetched, list):
                    rows = [row for row in fetched if _eligible_flagship_quote(row)]
        except Exception:
            pass

        _FLAGSHIP_MEETING_QUOTES_CACHE[flagship_meeting_id] = rows
        return rows


def _embed_check(full_path: str) -> dict:
    """Real oEmbed probe like the flagship's — embed-disabled videos get
    the player's honest external-open overlay instead of a dead iframe."""
    import requests
    from urllib.parse import parse_qs, urlparse

    video_id = (parse_qs(urlparse(full_path).query).get("video_id") or [""])[0]
    if not video_id:
        return {"embeddable": True}
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}",
                    "format": "json"},
            timeout=5,
        )
        return {"embeddable": resp.status_code == 200}
    except Exception:
        return {"embeddable": True}  # offline — the player surfaces its own error


def _load_transcript(row):
    """The karaoke's word timings — absent or unreadable transcript just
    means no karaoke strip, never a dead page."""
    path = row["transcript_path"]
    if not path:
        return None
    from zspan_cli import transcribe
    try:
        return transcribe.load_transcript(Path(path))
    except transcribe.TranscribeError:
        return None


# ---------------------------------------------------------------- Discussion
#
# The flagship's .preview sidecar endpoints, computed deterministically
# over the workspace: /api/preview/decisions serves the cached
# key_decisions as prose_output (so the site's new-discipline decisions
# renderer takes over), /api/preview/quotes + /routing carry the
# located decision windows with full word timings (discussion.py) so
# the Discussion accordion + SyncedQuote karaoke light up unchanged.
# /api/preview/recusals answers an honest empty — recusal detection is
# flagship machinery this workspace doesn't run.

_DISCUSSION_CACHE: dict = {}  # meeting_id → (fingerprint, payload)


def _timing_span(quote: dict) -> tuple[float, float]:
    timings = quote["word_timings"]
    return float(timings[0]["start_ms"]), float(timings[-1]["end_ms"])


def _flagship_quote_dto(row: dict) -> dict:
    """Project only the public fields BroadcastPage already consumes."""
    fields = (
        "speaker_name", "speaker_role", "speaker_class", "quote_text",
        "video_timestamp_seconds", "word_timings", "selection_rationale",
    )
    return {field: row[field] for field in fields if field in row}


def _merge_flagship_quotes(payload: dict, flagship_quotes: list[dict]) -> dict:
    """Replace each owned record window with its speaker-scoped quotes."""
    quotes = payload.get("quotes")
    routing = payload.get("routing")
    if not isinstance(quotes, list) or not isinstance(routing, list):
        return payload

    window_spans: dict[int, tuple[float, float]] = {}
    for index, quote in enumerate(quotes):
        if isinstance(quote, dict) and quote.get("speaker_class") == "record" \
                and _has_valid_word_timings(quote):
            window_spans[index] = _timing_span(quote)

    owned: dict[int, list[dict]] = {}
    for quote in flagship_quotes:
        if not _eligible_flagship_quote(quote):
            continue
        quote_start, quote_end = _timing_span(quote)
        owner = None
        greatest_overlap = 0.0
        for window_index, (window_start, window_end) in window_spans.items():
            overlap = min(quote_end, window_end) - max(quote_start, window_start)
            # Strictly greater preserves the earliest window on ties.
            if overlap > greatest_overlap:
                owner = window_index
                greatest_overlap = overlap
        if owner is not None:
            owned.setdefault(owner, []).append(quote)

    if not owned:
        return payload

    routes_by_quote: dict[int, list[dict]] = {}
    for route in routing:
        if isinstance(route, dict) and isinstance(route.get("quote_index"), int):
            routes_by_quote.setdefault(route["quote_index"], []).append(route)

    merged_quotes: list[dict] = []
    merged_routing: list[dict] = []
    for old_index, record_quote in enumerate(quotes):
        replacements = owned.get(old_index)
        entries = (
            [_flagship_quote_dto(row) for row in replacements]
            if replacements else [record_quote]
        )
        for entry in entries:
            new_index = len(merged_quotes)
            merged_quotes.append(entry)
            for route in routes_by_quote.get(old_index, []):
                merged_routing.append({**route, "quote_index": new_index})

    merged = {**payload, "quotes": merged_quotes, "routing": merged_routing}
    if isinstance(payload.get("summary"), dict):
        summary = dict(payload["summary"])
        for bucket, field in (
            ("standalone", "standalone_count"),
            ("decision_bound", "decision_bound_count"),
            ("drop", "drop_count"),
        ):
            summary[field] = sum(
                1 for route in merged_routing if route.get("bucket") == bucket
            )
        merged["summary"] = summary
    return merged


def _discussion_payload(conn, meeting_id: int) -> dict:
    """Locate-once-per-state: the computation reruns only when the
    key_decisions content, transcript file, or meeting identity changes."""
    row = workspace.get_meeting(conn, meeting_id)
    if row is None:
        return {"quotes": [], "routing": [], "summary": None}
    outputs = workspace.load_outputs(conn, meeting_id)
    kd = (outputs.get("key_decisions") or {}).get("content") or ""
    tpath = row["transcript_path"] or ""
    try:
        mtime = os.path.getmtime(tpath) if tpath else 0.0
    except OSError:
        mtime = 0.0
    fingerprint = (
        hash(kd), tpath, mtime, row["flagship_row_id"], row["video_url"],
    )

    cached = _DISCUSSION_CACHE.get(meeting_id)
    if cached and cached[0] == fingerprint:
        return cached[1]

    transcript = _load_transcript(row)
    from zspan_cli import discussion
    words = transcript.get("words") if transcript else []
    payload = discussion.build_discussion(kd, words or [])
    if kd and words:
        if payload["quotes"]:
            publish_activity(
                "index",
                f"located {len(payload['quotes'])} decision moment(s) in the record",
                "; ".join(q["selection_rationale"] for q in payload["quotes"][:3]),
            )
        flagship_id = row["flagship_row_id"]
        transcript_source = transcript.get("source_url")
        current_video = row["video_url"]
        if isinstance(flagship_id, int) \
                and isinstance(transcript_source, str) and transcript_source \
                and isinstance(current_video, str) and current_video \
                and transcript_source == current_video:
            # Local source equality catches re-pull recording drift. The
            # flagship-side recording identity is not exposed here; timing
            # overlap remains the fail-safe (wrong recording → no overlap →
            # honest nameless), never a guessed attribution.
            payload = _merge_flagship_quotes(
                payload, _flagship_meeting_quotes(flagship_id),
            )
    _DISCUSSION_CACHE[meeting_id] = (fingerprint, payload)
    return payload


def _citation_coverage(key_decisions: str, payload: dict) -> dict:
    """Read-time coverage, derived from cached prose + canonical routes."""
    from zspan_cli import discussion

    summary = payload.get("summary") if isinstance(payload, dict) else None
    coverage = summary.get("citation_coverage") if isinstance(summary, dict) else None
    if isinstance(coverage, dict):
        return coverage
    routing = payload.get("routing", []) if isinstance(payload, dict) else []
    return discussion.citation_coverage(key_decisions, routing)


def _meeting_citation_ready(conn, meeting_id: int) -> bool:
    outputs = workspace.load_outputs(conn, meeting_id)
    key_decisions = (outputs.get("key_decisions") or {}).get("content") or ""
    coverage = _citation_coverage(
        key_decisions, _discussion_payload(conn, meeting_id),
    )
    return coverage["state"] != "citation_incomplete"


def _preview(conn, kind: str, meeting_id: int):
    """(payload, status) for /api/preview/<kind>/<meeting_id>."""
    if kind == "decisions":
        outputs = workspace.load_outputs(conn, meeting_id)
        kd = (outputs.get("key_decisions") or {}).get("content") or ""
        if not kd:
            return {}, 200  # no sidecar → the client's legacy fallback
        payload = _discussion_payload(conn, meeting_id)
        return {
            "prose_output": kd,
            "citation_coverage": _citation_coverage(kd, payload),
        }, 200
    if kind == "quotes":
        payload = _discussion_payload(conn, meeting_id)
        return {"quotes": payload["quotes"],
                "quote_count": len(payload["quotes"])}, 200
    if kind == "routing":
        payload = _discussion_payload(conn, meeting_id)
        return {"routing": payload["routing"],
                "summary": payload.get("summary")}, 200
    if kind == "recusals":
        return {"recusal_count": 0, "recusals": []}, 200
    return {"success": True, "items": []}, 200


# ---------------------------------------------------------------- the Librarian
#
# The site's ask-anything panel, unlocked over the private workspace. A
# private workspace is the user's own tool; the flagship's restriction on
# this panel is a serving-strangers policy and doesn't apply on loopback.
# Two calls mirror the flagship's bring-your-own-key split:
#
#   POST /api/rag-search/<id>            retrieval — workspace chunks +
#                                        the canonical rag_search prompt
#   POST /api/local/librarian/synthesize the loopback synthesis on the
#                                        STORED key (`zspan init`),
#                                        direct-to-provider from this
#                                        machine. No relay semantics to
#                                        inherit: the browser can't call
#                                        OpenAI/Anthropic itself (their
#                                        APIs answer no CORS), and the
#                                        key never travels to the page.


_LIBRARIAN_PROMPT_CACHE: dict = {}


def _librarian_prompt() -> str:
    """rag_search_v1.md body (frontmatter-stripped) — the same system
    prompt the flagship ships to BYOK clients. Cached per process."""
    if "body" not in _LIBRARIAN_PROMPT_CACHE:
        from zspan_cli import synthesize as syn
        try:
            cfg = load_config() or {}
        except Exception:
            cfg = {}
        prompts_dir = syn.resolve_prompts_dir(cfg)
        _LIBRARIAN_PROMPT_CACHE["body"] = syn.load_canonical_prompt(
            "rag_search_v1", prompts_dir
        )
    return _LIBRARIAN_PROMPT_CACHE["body"]


def _local_provenance(query: str, template_body: str) -> dict:
    """The flagship packet's shape with honest local values. run_id stays
    EMPTY on purpose: a workspace attesting to itself proves nothing —
    the transcript is the anchor, not the packet — and an empty run_id is
    exactly what hides the client's verify-run link."""
    import hashlib
    from datetime import datetime, timezone

    return {
        "run_id": "",
        "vector_ids": [],
        "prompt_template_hash":
            "sha256:" + hashlib.sha256(template_body.encode("utf-8")).hexdigest(),
        "prompt_template_version": "local-workspace",
        "query_hash": "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _rag_search(conn, meeting_id: int, body: dict):
    """The flagship's /api/rag-search response shape over the workspace's
    chunk matrix — same request fields, same chunk fields, so the client
    code paths are identical here and on zspan.org."""
    query = (body.get("query") or "").strip()
    if not query:
        return 400, {"success": False, "error": "query is required"}
    if len(query) > 500:
        return 400, {"success": False, "error": "query too long (max 500 chars)"}
    try:
        top_k = int(body.get("top_k", 12))
    except (TypeError, ValueError):
        return 400, {"success": False, "error": "top_k must be an int"}
    top_k = max(1, min(50, top_k))

    row = workspace.get_meeting(conn, meeting_id)
    if row is None:
        return 404, {"success": False,
                     "error": f"No meeting found with id={meeting_id}"}

    try:
        template = _librarian_prompt()
    except Exception as e:
        return 500, {"success": False, "error": str(e)}

    chunk_rows, matrix = workspace.load_chunk_matrix(conn, meeting_id)
    if not chunk_rows:
        return 200, {
            "success": True,
            "meeting_id": meeting_id,
            "query": query,
            "chunks": [],
            "interpreted_as": "not_indexed",
            "provenance": _local_provenance(query, template),
            "recommended_system_prompt": template,
        }

    from zspan_cli import pipeline as pl
    try:
        query_vec = pl.embed_query(query)
    except pl.PipelineError as e:
        return 500, {"success": False, "error": str(e)}

    chunks = [
        {
            "chunk_index": int(chunk_rows[i]["chunk_index"]),
            "vector_id": "",
            "body": chunk_rows[i]["text"],
            "start_seconds": float(chunk_rows[i]["start_seconds"] or 0.0),
            "end_seconds": float(chunk_rows[i]["end_seconds"] or 0.0),
            "speaker_turns": None,
            "score": score,
        }
        for i, score in pl.top_k_cosine(matrix, query_vec, k=top_k)
    ]
    publish_activity(
        "librarian", "Librarian searched the record", query,
    )
    return 200, {
        "success": True,
        "meeting_id": meeting_id,
        "query": query,
        "chunks": chunks,
        "provenance": _local_provenance(query, template),
        "recommended_system_prompt": template,
    }


def _resolve_librarian_engine(cfg: dict):
    """(provider, api_key, model) for the Librarian panel — the
    configured engine first; the installed Codex CLI when keyless, which
    with local transcription lets the whole served site run with zero
    stored keys; the first stored key as the visible fallback when a
    configured codex engine is missing its binary.
    Raises PipelineSetupError only when NO engine exists at all."""
    from zspan_cli.processing import PipelineSetupError, resolve_synthesis_setup
    from zspan_cli.providers import codex_available

    try:
        return resolve_synthesis_setup(cfg)
    except PipelineSetupError:
        api_keys = (cfg or {}).get("api_keys") or {}
        if codex_available(cfg):
            return resolve_synthesis_setup(cfg, provider_override="codex")
        if api_keys:
            return resolve_synthesis_setup(
                cfg, provider_override=next(iter(api_keys)))
        raise


def _librarian_setup() -> dict:
    """Presence-only readiness for the panel unlock: provider, model,
    engine kind, and the key's display fingerprint travel — the key
    itself never does (the LocalProcessGate cloud-probe precedent).
    A keyless machine with the Codex CLI installed arms as engine
    "codex" (fingerprint empty — there is no key to fingerprint)."""
    from zspan_cli.config import key_fingerprint
    from zspan_cli.processing import PipelineSetupError

    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    try:
        provider, api_key, model = _resolve_librarian_engine(cfg)
    except PipelineSetupError:
        return {"ready": False}
    return {
        "ready": True,
        "provider": provider,
        "model": model,
        "engine": "codex" if provider == "codex" else "key",
        "fingerprint": key_fingerprint(api_key) if api_key else "",
    }


def _librarian_synthesize(body: dict):
    """One question through the user's stored provider. The page sends
    the system prompt + assembled user message (the flagship's own
    client-side envelope); this machine makes the provider call."""
    from zspan_cli import synthesize as syn
    from zspan_cli.processing import PipelineSetupError

    system_prompt = (body.get("system_prompt") or "").strip()
    user_message = (body.get("user_message") or "").strip()
    if not user_message:
        return 400, {"success": False, "error": "user_message is required"}
    try:
        max_tokens = max(256, min(4096, int(body.get("max_tokens", 1024))))
    except (TypeError, ValueError):
        max_tokens = 1024
    try:
        temperature = max(0.0, min(1.0, float(body.get("temperature", 0.2))))
    except (TypeError, ValueError):
        temperature = 0.2

    try:
        config = load_config()
    except Exception as e:
        return 500, {"success": False, "error": str(e)}
    try:
        provider, api_key, model = _resolve_librarian_engine(config or {})
    except PipelineSetupError as e:
        return 400, {
            "success": False,
            "error": str(e),
        }

    # The question line out of the assembled envelope — the star's payload.
    question = user_message.splitlines()[0]
    if question.startswith("CURRENT QUESTION:"):
        question = question[len("CURRENT QUESTION:"):].strip()

    try:
        result = syn.synthesize_chat(
            provider, api_key, model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except syn.SynthesisError as e:
        publish_activity("librarian", f"Librarian answer failed ({provider})",
                         str(e), status=502)
        return 502, {"success": False, "error": str(e)}

    publish_activity(
        "librarian", f"Librarian answered via {provider} ({model})",
        question,
    )
    # The flagship-shaped provider id keys the client's cost table
    # (PROVIDER_RATES) and reads plainly in the per-turn footer.
    provider_id = f"google-{model}" if provider == "gemini" else f"{provider}-{model}"
    return 200, {
        "success": True,
        "answer": result["answer"],
        "provider": provider,
        "provider_id": provider_id,
        "model": model,
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
    }


# ---------------------------------------------------------------- process runs

# One pipeline at a time, whole machine — transcription is CPU-heavy and
# synthesis spends the user's key; parallel runs help nobody (the
# flagship's own one-work-order-at-a-time cadence, kept locally).
_PROCESS_LOCK = threading.Lock()
_PROCESS_STATE: dict = {
    "meeting_id": None, "lines": [], "running": False,
    "done": False, "ok": None, "error": None,
    "engine": None, "run_started_monotonic": None,
    "pending_approval": None, "approval_waiter": None,
}


class _WebApprovalWaiter:
    """One in-memory handoff between the pipeline and one browser decision."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.decision = None


def _approval_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _web_approve_chunk(**review):
    """Publish a review envelope and wait until the local page decides.

    There is deliberately no timeout: losing or reloading the page leaves the
    envelope in status until the operator returns. Server shutdown resolves a
    pending wait as ABORT_ALL so teardown can never turn silence into approval.
    """
    from zspan_cli import approval

    retrieved_chunks = review["retrieved_chunks"]
    payload = {
        "output_type": review["output_type"],
        "chunk_index": review["chunk_index"],
        "chunk_total": review["chunk_total"],
        "provider": review["provider"],
        "model": review["model"],
        "key_fingerprint": review["key_fingerprint_str"],
        "retrieval_query": review["retrieval_query"],
        "retrieved_chunks": [
            {
                "display_text": (
                    f"[{_approval_timestamp(chunk.start_seconds)}]  "
                    f"(chunk #{chunk.chunk_index}, score {chunk.score:.3f})\n"
                    f"{approval.strip_display_ansi(chunk.text)}"
                ),
            }
            for chunk in retrieved_chunks
        ],
        "canonical_prompt": approval.strip_display_ansi(
            review["canonical_prompt"]
        ),
        # This value is intentionally untouched: it is the exact string passed
        # to synthesize after approval, and the web breakpoint promises that.
        "full_envelope": review["full_envelope"],
    }
    waiter = _WebApprovalWaiter()
    with _PROCESS_LOCK:
        if not _PROCESS_STATE.get("running"):
            return approval.ApprovalDecision.ABORT_ALL
        if _PROCESS_STATE.get("pending_approval") is not None:
            raise RuntimeError("a web synthesis approval is already pending")
        _PROCESS_STATE["pending_approval"] = payload
        _PROCESS_STATE["approval_waiter"] = waiter
        # Visible on every surface (including a page served from an older
        # bundle without the modal) so a waiting run never reads as a stall.
        _PROCESS_STATE["lines"].append(
            f"review open — {payload['output_type']} is waiting for your "
            "decision on this page; nothing sends until you choose"
        )

    waiter.event.wait()
    if waiter.decision is None:
        return approval.ApprovalDecision.ABORT_ALL
    return waiter.decision


def _submit_process_approval(meeting_id: int, body: Optional[dict] = None):
    """Validate and deliver the current browser breakpoint decision."""
    from zspan_cli import approval

    if not isinstance(body, dict) or set(body) != {"decision"}:
        return 400, {
            "accepted": False,
            "error": "send exactly one approval decision",
        }
    decisions = {
        "proceed": approval.ApprovalDecision.PROCEED,
        "skip": approval.ApprovalDecision.SKIP,
        "abort": approval.ApprovalDecision.ABORT_ALL,
    }
    decision = decisions.get(body.get("decision"))
    if decision is None:
        return 400, {
            "accepted": False,
            "error": "approval decision must be proceed, skip, or abort",
        }

    with _PROCESS_LOCK:
        if not _PROCESS_STATE.get("running"):
            return 409, {
                "accepted": False,
                "pending": False,
                "error": "no processing run is waiting for approval",
            }
        if _PROCESS_STATE.get("meeting_id") != meeting_id:
            return 409, {
                "accepted": False,
                "pending": False,
                "error": "that meeting is not the active processing run",
            }
        waiter = _PROCESS_STATE.get("approval_waiter")
        if waiter is None or _PROCESS_STATE.get("pending_approval") is None:
            return 409, {
                "accepted": False,
                "pending": False,
                "error": "no approval is currently pending",
            }
        waiter.decision = decision
        _PROCESS_STATE["pending_approval"] = None
        _PROCESS_STATE["approval_waiter"] = None
        waiter.event.set()
    return 200, {"accepted": True, "pending": False}


def _abort_pending_web_approval() -> None:
    """Wake a waiting pipeline as an operator abort during server shutdown."""
    from zspan_cli import approval

    with _PROCESS_LOCK:
        waiter = _PROCESS_STATE.get("approval_waiter")
        if waiter is None:
            return
        waiter.decision = approval.ApprovalDecision.ABORT_ALL
        _PROCESS_STATE["pending_approval"] = None
        _PROCESS_STATE["approval_waiter"] = None
        waiter.event.set()


def _kick_process(meeting_id: int, body: Optional[dict] = None):
    from zspan_cli import processing

    body = body or {}
    mode = (body.get("mode") or "local").strip().lower()
    # The synthesis-engine choice: "key" = the stored API key;
    # "codex" = the installed Codex CLI on the
    # user's own subscription (keyless — with local transcription the
    # whole pipeline can run with zero stored keys).
    engine = (body.get("synthesis_engine") or "key").strip().lower()

    with _PROCESS_LOCK:
        if _PROCESS_STATE["running"]:
            active = _PROCESS_STATE["meeting_id"]
            return 409, {
                "started": False,
                "error": (f"meeting {active} is processing right now — one at "
                          "a time keeps your machine (and your key) happy."),
            }
        try:
            config = load_config()
        except Exception as e:
            return 500, {"started": False, "error": str(e)}
        config = config or {}
        if not has_processing_ack(config):
            if body.get("acknowledge_local_processing") is True:
                config = record_processing_ack(config)
            else:
                return 428, {
                    "started": False,
                    "ack_required": True,
                    "error": (
                        PROCESSING_ACK_TEXT
                        + " — acknowledge in the terminal (`zspan process` or "
                        "`zspan open` asks once), then retry."
                    ),
                }
        from zspan_cli.providers import codex_available, codex_unavailable_message
        api_keys = config.get("api_keys") or {}
        if engine not in ("key", "codex"):
            return 400, {
                "started": False,
                "error": f"unknown synthesis engine: {engine}",
            }
        if engine == "codex":
            if not codex_available(config):
                return 400, {
                    "started": False,
                    "error": codex_unavailable_message(config),
                }
        elif not api_keys:
            # The shipped page initializes this selector to "key". With no
            # key there is no meaningful keyed choice, so its primary Process
            # click uses the reachable keyless engine automatically.
            if codex_available(config):
                engine = "codex"
            else:
                return 400, {
                    "started": False,
                    "error": (
                        "no synthesis engine is reachable — "
                        f"{codex_unavailable_message(config)} "
                        "No API-key provider is configured."
                    ),
                }

        if mode == "cloud":
            # A key pasted into the panel stores into the same local
            # config file `zspan init` writes (0600, this machine only)
            # — the page never gets a key back, only readiness.
            pasted = (body.get("openai_key") or "").strip()
            if pasted:
                from zspan_cli.config import save_config
                api_keys = dict(config.get("api_keys") or {})
                api_keys["openai"] = pasted
                config["api_keys"] = api_keys
                save_config(config)
            if not (config.get("api_keys") or {}).get("openai"):
                return 400, {
                    "started": False,
                    "error": "cloud transcription runs on an OpenAI key and "
                             "none is stored — paste one in the panel or use "
                             "the free local mode.",
                }

        provider_override = "codex" if engine == "codex" else ""
        run_provider, _api_key, _model = processing.resolve_synthesis_setup(
            config, provider_override=provider_override,
        )
        _PROCESS_STATE.update(
            meeting_id=meeting_id, lines=[], running=True,
            done=False, ok=None, error=None,
            engine="Codex" if run_provider == "codex" else run_provider,
            run_started_monotonic=time.monotonic(),
            pending_approval=None, approval_waiter=None,
        )

    def _progress(msg: str) -> None:
        with _PROCESS_LOCK:
            _PROCESS_STATE["lines"].append(str(msg))

    def _activity(kind: str, label: str, detail: str = "", status: int = 200) -> None:
        publish_activity(kind, label, detail, status=status)

    def _run() -> None:
        try:
            result = processing.run_pipeline(
                meeting_id, config=config, progress=_progress,
                cloud_transcribe=(mode == "cloud"),
                provider_override=provider_override,
                approval_fn=_web_approve_chunk,
                activity=_activity,
            )
            with _PROCESS_LOCK:
                _PROCESS_STATE.update(
                    running=False, done=True, ok=bool(result["ok"]),
                )
        except Exception as e:
            publish_activity("pipeline", "processing failed", str(e), status=500)
            with _PROCESS_LOCK:
                _PROCESS_STATE["lines"].append(str(e))
                _PROCESS_STATE.update(
                    running=False, done=True, ok=False, error=str(e),
                )

    threading.Thread(target=_run, daemon=True).start()
    return 200, {"started": True}


def _process_status(meeting_id: int) -> dict:
    with _PROCESS_LOCK:
        if _PROCESS_STATE["meeting_id"] != meeting_id:
            return {"running": False, "done": False, "lines": [],
                    "ok": None, "error": None}
        return {
            "running": _PROCESS_STATE["running"],
            "done": _PROCESS_STATE["done"],
            "ok": _PROCESS_STATE["ok"],
            "error": _PROCESS_STATE["error"],
            "lines": list(_PROCESS_STATE["lines"]),
            "pending_approval": _PROCESS_STATE.get("pending_approval"),
        }


def _process_active(conn) -> dict:
    """Whatever pipeline run this server currently holds (or last
    finished) — the HQ indicator's poll. Meeting identity travels so
    the done-state can deep-link into the broadcast."""
    with _PROCESS_LOCK:
        state = {
            "meeting_id": _PROCESS_STATE["meeting_id"],
            "running": _PROCESS_STATE["running"],
            "done": _PROCESS_STATE["done"],
            "ok": _PROCESS_STATE["ok"],
        }
    if state["meeting_id"] is None:
        return {"active": False, **state}
    row = workspace.get_meeting(conn, int(state["meeting_id"]))
    if row is not None:
        state["city"] = row["city"]
        state["title"] = row["title"]
        state["meeting_date"] = row["meeting_date"]
    return {"active": bool(state["running"]), **state}


# ---------------------------------------------------------------- lifecycle


class _LocalThreadingHTTPServer(ThreadingHTTPServer):
    def shutdown(self) -> None:
        _abort_pending_web_approval()
        super().shutdown()


def start_server(port: int = 0, webapp_dir: Optional[Path] = None) -> ThreadingHTTPServer:
    """Bind 127.0.0.1 (port 0 = OS-assigned free port) and serve on a
    daemon thread. Returns the server; .server_address[1] is the port."""
    handler = type("BoundHandler", (_Handler,), {"webapp_dir": webapp_dir})
    server = _LocalThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def open_workspace(
    meeting_id: Optional[int] = None,
    *,
    port: int = 0,
    open_browser: bool = True,
    say: Callable[[str], None] = print,
) -> Tuple[ThreadingHTTPServer, str]:
    """Start serving; return (server, url). The browser-open belongs to
    the caller's choreography now (the terminal boot resolves first),
    but open_browser=True still works for direct/programmatic use."""
    webapp = resolve_webapp_dir()
    server = start_server(port, webapp_dir=webapp)
    actual_port = server.server_address[1]

    if webapp is not None:
        path = (f"/?view=broadcast&meetingId={meeting_id}"
                if meeting_id is not None else "/")
        say("Serving the Z-SPAN site locally over your private workspace.")
    else:
        path = f"/meeting/{meeting_id}" if meeting_id is not None else "/"
        say("Built site bundle not found — serving the lean fallback view.")
        say("  (`zspan open --fetch-bundle` downloads the full zspan.org "
            "experience one time, or run from a clone with dist/public built.)")

    url = f"http://127.0.0.1:{actual_port}{path}"
    say(f"  {url}")
    say("  (served from your computer; your key and raw media stay local.")
    say("   Completed work uses Z-SPAN's private intake. Ctrl-C stops it.)")
    if open_browser:
        webbrowser.open(url)
    return server, url

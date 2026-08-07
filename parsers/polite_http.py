"""polite_http — considerate-guest HTTP for parsers.

One shared session factory that makes every parser a courteous visitor to the
source websites. Two guarantees:

1. **Per-host pacing on every request — never zero.** A random gap between
   consecutive requests to the same host prevents momentarily loading a small
   server and keeps a parser from tripping a bot-detector by firing a rapid
   burst. Two tiers:
     - **custom / self-hosted hosts** (small servers) get a 4.0-7.0s gap;
     - **robust vendor platforms** (Legistar, Granicus, IQM2, CivicPlus, Destiny,
       PrimeGov and similar CDN/enterprise infra) get 3.0-5.0s (never < 3s).
   Requests to different hosts do not wait on each other. Speed is not a goal.

2. **One neutral, static User-Agent, forced on every request.** A normal browser
   string, identical for every parser, overriding whatever a parser sets. A
   bot-identifying UA can invite a WAF block rather than prevent one; a shared,
   project-specific UA would also misrepresent a contributor's local request as
   the canonical instance's. Reachability for site owners lives at the project
   level, not on every HTTP request.

If a specific site's terms ever require identification, the documented drop-in
is a UA pointing at a one-paragraph human explainer page, deployed per-site,
never globally.

Two ways to get it:
    # (a) Explicit — a parser opts in:
    from polite_http import make_session
    session = make_session()          # instead of requests.Session()

    # (b) Automatic — the loader wraps the scrape call in `polite_requests()`,
    #     transparently routing the parser's `requests` calls through the polite
    #     path and forcing the neutral UA. Contributors inherit pacing + neutral
    #     UA from the harness and cannot omit either.
"""
from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

# One neutral, static, current browser UA — shared by every parser.
POLITE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# Robust vendor platforms — CDN/enterprise infra. They get the LIGHTER pacing
# tier (not zero). Everything not matching gets the heavier custom-host tier.
VENDOR_HOST_SUFFIXES = (
    "legistar.com", "granicus.com", "iqm2.com", "civicclerk.com", "civicplus.com",
    "primegov.com", "destinyhosted.com", "revize.com", "municodemeetings.com",
    "novusagenda.com", "escribemeetings.com", "boarddocs.com", "swagit.com",
    "civicengage.com", "civicweb.net", "agendaquick.com", "onbase.com",
    "amazonaws.com", "cloudfront.net", "azurewebsites.net", "googleapis.com",
    "google.com",  # Google Calendar / Docs-hosted feeds
)

# Pacing windows (seconds, random per gap). Every request is paced — never zero.
# Deliberately generous: what matters is never looking like an attack, never
# getting blocked. A few extra seconds per request costs nothing.
VENDOR_MIN_GAP, VENDOR_MAX_GAP = 3.0, 5.0   # robust infra — generous, never < 3s
CUSTOM_MIN_GAP, CUSTOM_MAX_GAP = 4.0, 7.0   # small/self-hosted — even more headroom


def _is_vendor_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == suf or host.endswith("." + suf) for suf in VENDOR_HOST_SUFFIXES)


def _gap_for(host: str) -> float:
    if _is_vendor_host(host):
        return random.uniform(VENDOR_MIN_GAP, VENDOR_MAX_GAP)
    return random.uniform(CUSTOM_MIN_GAP, CUSTOM_MAX_GAP)


class _PoliteAdapter(HTTPAdapter):
    """Transport adapter: forces the neutral UA + paces consecutive requests per host.

    Thread-safe per-host last-request clock. EVERY host is paced (vendor lighter,
    custom heavier) — never zero. Transparent to parser logic.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_request_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def send(self, request, *args, **kwargs):
        # Force the neutral UA on EVERY request, overriding whatever the parser set.
        request.headers["User-Agent"] = POLITE_UA
        host = urlparse(request.url).netloc.split(":")[0].lower()
        if host:
            with self._lock:
                last = self._last_request_at.get(host)
                if last is not None:
                    wait = _gap_for(host) - (time.monotonic() - last)
                    if wait > 0:
                        time.sleep(wait)
                self._last_request_at[host] = time.monotonic()
        return super().send(request, *args, **kwargs)


# Module-level shared adapter so the per-host pacing clock persists across a
# parser's many requests (and across sequential scrapes sharing a host — strictly
# more polite). One dict, negligible memory.
_SHARED_ADAPTER = _PoliteAdapter()

# Capture the REAL Session class at import time. polite_requests() patches
# requests.Session to make_session, so make_session must call THIS, not the
# (possibly patched) requests.Session — otherwise it recurses infinitely.
_OrigSession = requests.Session


def make_session() -> requests.Session:
    """Return a requests.Session with the neutral UA + shared per-host pacing installed."""
    session = _OrigSession()
    session.headers.update({"User-Agent": POLITE_UA})
    session.mount("http://", _SHARED_ADAPTER)
    session.mount("https://", _SHARED_ADAPTER)
    return session


@contextmanager
def polite_requests():
    """Scoped patch: route the `requests` module's calls through polite sessions.

    Wrap a parser invocation in this and any `requests.get/post/request(...)` or
    `requests.Session()` the parser uses becomes polite (neutral UA + per-host
    pacing) for the duration, then everything is restored. Scoped so it never
    touches the app's OTHER HTTP (YouTube API, claude -p, etc.).
    """
    orig_get, orig_post, orig_request = requests.get, requests.post, requests.request
    orig_session = requests.Session
    shared = make_session()  # own cookie jar; shares the module pacing clock

    def _get(url, **kw): return shared.get(url, **kw)
    def _post(url, **kw): return shared.post(url, **kw)
    def _request(method, url, **kw): return shared.request(method, url, **kw)

    requests.get, requests.post, requests.request = _get, _post, _request
    requests.Session = make_session  # parser-created sessions also get pacing + neutral UA
    try:
        yield
    finally:
        requests.get, requests.post, requests.request = orig_get, orig_post, orig_request
        requests.Session = orig_session

"""D-099 Phase 2 C5 — DB backend dispatch for the bridge.

Call `install_db_backend()` BEFORE any `from database import ...` to
swap SQLite-direct database.py for the HTTP shim
(database_http_client.py) when `ZSPAN_DB_BACKEND=http`.

The swap works by rebinding `sys.modules['database']` to the
http-client module — subsequent `from database import X` then resolves
to the HTTP-backed X, with the same signature/return shape.

Prerequisite: the parsers/ directory must already be on `sys.path`
(the bridge's worker/fetcher/scanner each install this themselves
at module load).

Why this lives in its own file:
  - Callable from multiple bridge entry surfaces (worker.py / fetcher.py
    / scanner.py) without duplicating the env-var check or the
    sys.modules dance.
  - Idempotent across nested-import call chains — subsequent invocations
    see the rebind and return without redoing it.
  - The whole D-099 Phase 2 backend dispatch lives in one ~25-line file
    rather than scattered across the bridge.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


def install_db_backend() -> str:
    """Resolve `ZSPAN_DB_BACKEND` and install the matching backend into
    sys.modules['database']. Returns the active backend name
    ('sqlite' or 'http'). Safe to call multiple times."""
    backend = (os.environ.get("ZSPAN_DB_BACKEND") or "").strip().lower()
    if backend != "http":
        return "sqlite"

    # Idempotency: if a previous call already rebound, skip the re-import.
    existing = sys.modules.get("database")
    if existing is not None and getattr(existing, "__name__", "") == "database_http_client":
        return "http"

    import database_http_client  # noqa: F401 — requires parsers/ on sys.path
    sys.modules["database"] = database_http_client
    logger.info(
        "DB backend = http (database -> database_http_client via "
        "ZSPAN_DB_BACKEND=http)"
    )
    return "http"

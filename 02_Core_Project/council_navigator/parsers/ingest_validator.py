"""ingest_validator — the front-door gate on the Z-SPAN "water carrier".

Until now the pipeline validated everything EXCEPT the raw scrape itself:

  - video → meeting matches carry `haiku_match` confidence tiers,
  - officials' names self-correct through `city_vocabulary_corrections`,
  - synthesis output is checked by the S-133 neutrality audit,

but a raw scrape became cache with no plausibility check at all. A stub parser
returning fabricated "Sample Meeting" rows, or a JavaScript / Cloudflare
challenge shell rendered as a "meeting", sailed straight into the SQLite cache
that the whole downstream pipeline trusts and serves. (The worked example that
prompted this: meeting id 102764, Eagar's "Sample Meeting - JavaScript
Required", cached as a real episode.) This module is the missing front door:
the one place a raw-scraped listing is checked BEFORE it becomes cache.

Design — deterministic-first per D-085 ("don't rent the core"):

  Tier 1  deterministic structural validation. $0, stdlib-only, ALWAYS ON.
          Catches the fabrication / wall / dead-end class with high precision:
          placeholder titles ("Sample Meeting", "Example Meeting"), challenge-
          page shells ("Just a moment", "enable JavaScript", "Client
          Challenge"), sample statuses, empty titles.

  Tier 2  cheap-LLM teacher. OPT-IN via ZSPAN_INGEST_LLM_CHECK, OFF by default
          so it never fires a surprise paid call. A tiny YES/NO + reasoning
          call on a cheap model (default gpt-4o-mini), fired ONLY on a listing
          Tier 1 marks `uncertain`. Its job is to teach Tier 3, not to run on
          every scrape.

  Tier 3  learned signatures. A Tier-2 ruling can distil into a deterministic
          marker Tier 1 then applies for free forever — the
          city_vocabulary_corrections auto_apply pattern. This module ships the
          store + load seam; the full backtest-before-promote loop (S-133's
          guardrail against a bad signature) is the documented next step and is
          NOT auto-enabled here.

The verdict is per-row (drop the fabricated rows) AND listing-level (reject a
whole wall). A fully-rejected listing becomes an HONEST EMPTY — the F8
"succeeded-empty vs failed-silent" distinction, surfaced (logged) not hidden.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent

# ── Tier 1 signatures ────────────────────────────────────────────────────
# Title substrings NO real council-meeting listing ever carries: the stub-
# parser placeholders + the common scrape-wall shells. Matched case-
# insensitively as substrings against the meeting title.
_PLACEHOLDER_TITLE_MARKERS: Tuple[str, ...] = (
    "sample meeting",
    "example meeting",
    "placeholder meeting",
    "placeholder",
    "test meeting",
    "lorem ipsum",
    "dummy meeting",
    "no meetings found",
    "coming soon",
)
_WALL_TITLE_MARKERS: Tuple[str, ...] = (
    "javascript required",
    "javascript is required",
    "enable javascript",
    "client challenge",
    "checking your browser",
    "just a moment",          # Cloudflare interstitial <title>
    "access denied",
    "are you human",
    "captcha",
    "please verify you are",
    "403 forbidden",
    "404 not found",
    "page not found",
    "an error occurred",
    "service unavailable",
)
# meeting_status values a stub emits that a real listing never would.
_PLACEHOLDER_STATUSES: Tuple[str, ...] = ("sample", "placeholder", "test", "dummy")

# Tier 2 config (opt-in; OFF unless the env flag is set).
_LLM_ENV_FLAG = "ZSPAN_INGEST_LLM_CHECK"
_LLM_MODEL = os.environ.get("ZSPAN_INGEST_LLM_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

# Tier 3 learned-marker store (JSON; the seam, not the full learning loop).
_LEARNED_STORE = Path(
    os.environ.get("ZSPAN_INGEST_LEARNED_STORE", str(_HERE / "ingest_learned_markers.json"))
)


# ── verdict types ────────────────────────────────────────────────────────
@dataclass
class RowVerdict:
    accepted: bool
    reason: str = ""
    tier: str = "deterministic"


@dataclass
class IngestVerdict:
    """The gate's ruling on one scraped listing.

    status:
      "empty"      the scrape returned nothing (honest — not a failure)
      "ok"         a plausible listing; `accepted` are the rows to cache
      "rejected"   every row was a fabrication/wall → cache nothing (an honest
                   empty, surfaced so callers can log it)
      "uncertain"  Tier 1 passed the rows but the listing looks synthetic;
                   Tier 2 (if enabled) ruled, else ACCEPT-and-flag (fail-open —
                   downstream S-133 + publish review are the backstops)
    """
    status: str
    accepted: List[Dict] = field(default_factory=list)
    rejected: List[Tuple[Dict, str]] = field(default_factory=list)
    reason: str = ""
    tier: str = "deterministic"

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    def rejection_summary(self, limit: int = 5) -> str:
        parts = [f"{title_of(r)!r}: {why}" for r, why in self.rejected[:limit]]
        if self.rejected_count > limit:
            parts.append(f"(+{self.rejected_count - limit} more)")
        return "; ".join(parts)


# ── field access (works pre- and post-normalize) ─────────────────────────
def title_of(meeting: Dict) -> str:
    return str(
        meeting.get("meeting_title")
        or meeting.get("Meeting Title/Name")
        or meeting.get("title")
        or ""
    ).strip()


def _status_of(meeting: Dict) -> str:
    return str(
        meeting.get("meeting_status")
        or meeting.get("Meeting Status")
        or meeting.get("status")
        or ""
    ).strip().lower()


def _date_of(meeting: Dict) -> str:
    return str(
        meeting.get("meeting_date")
        or meeting.get("Meeting Date")
        or meeting.get("date")
        or ""
    ).strip()


# ── Tier 1 — deterministic ───────────────────────────────────────────────
def classify_row(meeting: Dict, learned_markers: Tuple[str, ...] = ()) -> RowVerdict:
    """Per-row Tier 1: reject clear fabrications / wall shells."""
    title = title_of(meeting)
    if not title:
        return RowVerdict(False, "empty title — degenerate row")

    low = title.lower()
    for marker in _PLACEHOLDER_TITLE_MARKERS:
        if marker in low:
            return RowVerdict(False, f"placeholder title marker: {marker!r}")
    for marker in _WALL_TITLE_MARKERS:
        if marker in low:
            return RowVerdict(False, f"scrape-wall marker in title: {marker!r}")
    for marker in learned_markers:
        if marker and marker in low:
            return RowVerdict(False, f"learned marker: {marker!r}", tier="learned")

    if _status_of(meeting) in _PLACEHOLDER_STATUSES:
        return RowVerdict(False, f"placeholder status: {_status_of(meeting)!r}")

    return RowVerdict(True)


def _looks_uniform_synthetic(accepted: List[Dict]) -> Optional[str]:
    """Listing-level WEAK signal: rows that individually pass but collectively
    look synthetic. Returns a reason (→ escalate to Tier 2) or None. Never a
    hard reject on its own — real repetitive calendars exist, so this only
    flags; the decision to drop needs Tier 2 or a learned marker."""
    if len(accepted) < 2:
        return None
    dates = [d for d in (_date_of(m) for m in accepted) if d]
    titles = [title_of(m).lower() for m in accepted]
    if dates and len(set(dates)) == 1:
        return f"all {len(accepted)} rows share one date ({dates[0]})"
    if dates and len(dates) == len(accepted) and all(d.endswith("-01") for d in dates):
        return "every row dated the first of a month — synthetic-date shape"
    if len(set(titles)) == 1 and len(accepted) <= 3:
        return f"all {len(accepted)} rows share one title ({titles[0]!r})"
    return None


# ── Tier 2 — cheap-LLM teacher (opt-in, OFF by default) ──────────────────
def llm_enabled() -> bool:
    return os.environ.get(_LLM_ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_openai_key() -> str:
    """env OPENAI_API_KEY -> user_settings.json -> empty (mirrors quote_cleaner)."""
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key.strip()
    try:
        settings_file = _HERE / "user_settings.json"
        if settings_file.exists():
            data = json.loads(settings_file.read_text())
            return (data.get("openai_api_key") or "").strip()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("ingest Tier-2 key read failed: %s", e)
    return ""


def _llm_listing_check(accepted: List[Dict], city_name: str) -> Optional[Tuple[bool, str]]:
    """Ask a cheap model whether this is a genuine government-meeting listing.
    Returns (is_real, reasoning) or None if unavailable (no key / lib / error).
    Callers MUST have checked llm_enabled() first — this never self-gates the
    env flag, so the only way it fires is an explicit opt-in."""
    try:
        import requests  # local import — Tier 2 is optional
    except Exception:
        return None
    key = _resolve_openai_key()
    if not key:
        logger.warning("ingest Tier-2 requested but no OpenAI key — skipping (fail-open)")
        return None
    sample = "\n".join(
        f"- {title_of(m)} ({_date_of(m) or 'no date'})" for m in accepted[:20]
    )
    prompt = (
        f"Below are items scraped from a city website for "
        f"'{city_name or 'a city'}', meant to be that city's public "
        f"government / council meetings:\n\n{sample}\n\n"
        "Is this a genuine listing of government / public meetings (NOT a "
        "website error, placeholder, login wall, or unrelated content)? "
        "Answer YES or NO on the first line, then one sentence of reasoning."
    )
    try:
        resp = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": _LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 60,
                "temperature": 0,
            },
            timeout=20,
        )
        resp.raise_for_status()
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.warning("ingest Tier-2 call failed (%s) — fail-open", e)
        return None
    if not text:
        return None
    first = text.splitlines()[0].strip().lower()
    is_real = first.startswith("yes")
    reasoning = text.split("\n", 1)[1].strip() if "\n" in text else text
    return (is_real, reasoning or text)


# ── Tier 3 — learned signatures (persistence seam) ───────────────────────
def load_learned_markers() -> Tuple[str, ...]:
    """Deterministic title markers learned from prior Tier-2 rulings. Consulted
    by classify_row for free. Empty tuple when the store is absent."""
    try:
        if _LEARNED_STORE.exists():
            data = json.loads(_LEARNED_STORE.read_text())
            return tuple(str(m).lower().strip() for m in data.get("title_markers", []) if str(m).strip())
    except Exception as e:
        logger.warning("could not load learned ingest markers: %s", e)
    return ()


def record_learned_marker(marker: str, source: str = "tier2") -> None:
    """Append a learned title marker to the store. This is the SEAM — a marker
    recorded here is applied by Tier 1 on the next scrape. The full guardrail
    (backtest a candidate against known-good corpora before it can auto-apply,
    per S-133) is the documented next step; callers should only record markers
    that have cleared that check."""
    marker = (marker or "").lower().strip()
    if not marker:
        return
    try:
        data = {"title_markers": []}
        if _LEARNED_STORE.exists():
            data = json.loads(_LEARNED_STORE.read_text())
        markers = [str(m).lower().strip() for m in data.get("title_markers", [])]
        if marker not in markers:
            markers.append(marker)
        data["title_markers"] = markers
        _LEARNED_STORE.write_text(json.dumps(data, indent=2))
        logger.info("recorded learned ingest marker %r (source=%s)", marker, source)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("could not record learned ingest marker %r: %s", marker, e)


# ── the gate ─────────────────────────────────────────────────────────────
def validate_listing(
    meetings: List[Dict],
    city_name: str = "",
    *,
    allow_llm: Optional[bool] = None,
    learned_markers: Optional[Tuple[str, ...]] = None,
) -> IngestVerdict:
    """Gate a raw-scraped listing before it becomes cache. See IngestVerdict
    for the status semantics. `allow_llm` overrides the env flag (tests pass
    False to stay offline); `learned_markers` overrides the store (tests pass
    an explicit tuple)."""
    if not meetings:
        return IngestVerdict(status="empty", reason="scrape returned 0 rows")

    if learned_markers is None:
        learned_markers = load_learned_markers()

    accepted: List[Dict] = []
    rejected: List[Tuple[Dict, str]] = []
    for m in meetings:
        v = classify_row(m, learned_markers)
        (accepted if v.accepted else rejected).append(m if v.accepted else (m, v.reason))

    # Everything rejected → the whole listing was a fabrication / wall.
    if not accepted:
        return IngestVerdict(
            status="rejected",
            rejected=rejected,
            reason=f"all {len(meetings)} row(s) rejected as fabrication/wall",
        )

    # Some survived — is the survivor set suspiciously synthetic?
    suspicious = _looks_uniform_synthetic(accepted)
    if not suspicious:
        return IngestVerdict(
            status="ok",
            accepted=accepted,
            rejected=rejected,
            reason=f"{len(accepted)} accepted, {len(rejected)} rejected",
        )

    allow = llm_enabled() if allow_llm is None else allow_llm
    if allow:
        ruling = _llm_listing_check(accepted, city_name)
        if ruling is not None and not ruling[0]:
            # Tier 2 says NOT a real listing → reject the survivors too.
            return IngestVerdict(
                status="rejected",
                rejected=rejected + [(m, f"tier2: {ruling[1]}") for m in accepted],
                reason=f"tier-2 rejected: {ruling[1]}",
                tier="llm",
            )
        return IngestVerdict(
            status="ok",
            accepted=accepted,
            rejected=rejected,
            reason=f"accepted (tier-2 cleared synthetic-shape flag: {suspicious})",
            tier="llm",
        )

    # Tier 2 off → fail-open on the ambiguous case, but say so loudly.
    return IngestVerdict(
        status="uncertain",
        accepted=accepted,
        rejected=rejected,
        reason=f"accepted-and-flagged (Tier 2 off): {suspicious}",
    )

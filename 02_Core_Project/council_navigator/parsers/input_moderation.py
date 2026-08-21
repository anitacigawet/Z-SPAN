"""input_moderation — pre-submission moderation gate for user-supplied text.

S-008 V0 / pillar 3b per
[`01_Project_Overview/S008_INPUT_SECURITY_SPEC.md`](../../01_Project_Overview/S008_INPUT_SECURITY_SPEC.md)
chunk 3 + [`01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md`](../../01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md)
surfaces S-11 (creator feedback) + S-12 (suggestion query).

Single shared primitive serves all user-input surfaces:
- creator_feedback   — creator notes submitted alongside asset downloads
- suggestion_query   — login-gated user query against processed episodes
- creator_signup     — free-text fields on the Creator Network signup form

The function applies, in order:
1. Surface-configurable length cap
2. Unicode NFC normalization + control-char strip
3. Bidi-control rejection
4. Structural fence-marker rejection
5. Surface-configurable URL cap
6. Shell-pattern / script-tag / javascript-uri rejection
7. Per-user per-surface per-day rate-limit check (DB-backed)

Per the redline 2026-06-09 decision, V0 SHIPS WITHOUT LLM-class
moderation. A Haiku-class classifier is documented as V1+ follow-up
(`S-008-V1-3b-llm-classifier` in the SPEC's open questions).

Per [D-100](../../01_Project_Overview/DECISIONS.md#d-100): defensive
primitive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

logger = logging.getLogger(__name__)


# Supported surface names. Adding a new surface requires explicit code
# surgery — the constants drive both the rate-limit storage key and the
# default caps.
SurfaceName = Literal[
    "creator_feedback",
    "suggestion_query",
    "creator_signup",
]


@dataclass(frozen=True)
class SurfaceConfig:
    """Per-surface moderation configuration.

    The defaults follow the SPEC's chunk 3 § 1-5 + § 12 entries. Callers
    can override on a per-call basis via moderate_user_input(...) kwargs.
    """

    max_length: int
    max_urls: int
    per_user_per_day_cap: int
    allow_http_urls: bool = True  # http urls allowed in the count budget


SURFACE_DEFAULTS: dict[str, SurfaceConfig] = {
    "creator_feedback": SurfaceConfig(
        max_length=2_000,
        max_urls=3,
        per_user_per_day_cap=20,
    ),
    "suggestion_query": SurfaceConfig(
        max_length=500,
        max_urls=0,
        per_user_per_day_cap=50,
    ),
    "creator_signup": SurfaceConfig(
        # signup form aggregates display_name + handle + creator-context note.
        # 500 chars covers a typical context paragraph + room for short names.
        max_length=500,
        max_urls=0,
        per_user_per_day_cap=5,  # legitimate signup attempts are rare
    ),
}


@dataclass
class ModerationResult:
    """Result of moderate_user_input.

    - accept=True + reason="clean" → caller persists normalized_text
    - accept=True + reason="flagged" → caller persists but tags
      operator_review_needed=True (the row reaches a review queue)
    - accept=False + reason=<rule> → caller returns 400 with the reason
    """

    accept: bool
    reason: str
    normalized_text: Optional[str] = None


def _surface_config(
    surface: str, override: Optional[SurfaceConfig] = None
) -> SurfaceConfig:
    if override is not None:
        return override
    cfg = SURFACE_DEFAULTS.get(surface)
    if cfg is None:
        raise ValueError(
            f"surface {surface!r} has no default config; pass override="
            f"SurfaceConfig(...) explicitly"
        )
    return cfg


def _check_rate_limit(
    surface: str, user_id: int, cap: int
) -> tuple[bool, int]:
    """Return (within_cap, current_count). Returns (True, 0) if the
    DB-backed counter is unavailable — fail-open is the right posture
    for V0 since the deterministic rules carry the structural floor."""
    try:
        from parsers import database  # noqa: PLC0415
        conn = database.get_connection()
        cursor = conn.cursor()
        # Use SQLite's date() to bucket by UTC day. The user_input_attempts
        # table lives in init_notebook_schema; if it doesn't exist (older
        # DB), the COUNT errors and we fail-open per the docstring above.
        cursor.execute(
            """
            SELECT COUNT(*) FROM user_input_attempts
            WHERE user_id = ? AND surface = ? AND date(submitted_at) = date('now')
            """,
            (user_id, surface),
        )
        row = cursor.fetchone()
        count = int(row[0]) if row else 0
        conn.close()
        return (count < cap, count)
    except Exception as e:
        logger.warning(
            "input_moderation._check_rate_limit failed (fail-open): %s", e,
        )
        return (True, 0)


def _record_attempt(
    surface: str, user_id: int, accept: bool, reason: str
) -> None:
    """Append a row to user_input_attempts. Best-effort; never raises."""
    try:
        from parsers import database  # noqa: PLC0415
        conn = database.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_input_attempts (
                user_id, surface, accept, reason
            ) VALUES (?, ?, ?, ?)
            """,
            (user_id, surface, 1 if accept else 0, reason),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(
            "input_moderation._record_attempt failed: %s", e,
        )


def moderate_user_input(
    text: str,
    *,
    surface: SurfaceName,
    user_id: int,
    config_override: Optional[SurfaceConfig] = None,
    record_attempt: bool = True,
) -> ModerationResult:
    """Apply the V0 deterministic-rule pre-submission moderation pass.

    Args:
        text: raw user-supplied text.
        surface: SurfaceName indicating which set of caps to apply.
        user_id: the JWT-resolved user id; drives the per-user rate limit.
        config_override: optional SurfaceConfig to override the surface
            defaults (mostly for testing).
        record_attempt: when True (default), the result lands in
            user_input_attempts. Pass False from tests that don't want
            DB writes.

    Returns:
        ModerationResult; see dataclass docstring for the three shapes.
    """
    # Compose with the cross-surface primitive — it carries the unicode
    # hygiene + structural-marker rejection + shell-pattern checks. Dual
    # import path handles both runtime contexts (tests run from the
    # Navigator dir with `parsers` as a package; Flask runs from
    # parsers/ cwd where the sibling submodule is `input_security`).
    try:
        try:
            from parsers.input_security.primitives import (  # noqa: PLC0415
                moderate_basic_input,
            )
        except ImportError:
            from input_security.primitives import (  # type: ignore[no-redef,import-not-found]  # noqa: PLC0415
                moderate_basic_input,
            )
    except Exception as e:
        logger.error(
            "input_moderation.moderate_user_input: primitives import "
            "failed (%s); refusing the input as a safety floor", e,
        )
        return ModerationResult(
            accept=False, reason="primitives_unavailable", normalized_text=None,
        )

    cfg = _surface_config(surface, config_override)

    # Step 1: rate-limit pre-flight (cheap rejection before any string work).
    within, count = _check_rate_limit(surface, user_id, cfg.per_user_per_day_cap)
    if not within:
        result = ModerationResult(
            accept=False,
            reason=f"rate_limited:{count}/{cfg.per_user_per_day_cap}_per_day",
            normalized_text=None,
        )
        if record_attempt:
            _record_attempt(surface, user_id, False, result.reason)
        return result

    # Step 2: deterministic content rules.
    basic = moderate_basic_input(
        text,
        max_length=cfg.max_length,
        max_urls=cfg.max_urls,
    )

    result = ModerationResult(
        accept=basic.accept,
        reason=basic.reason,
        normalized_text=basic.normalized_text,
    )

    if record_attempt:
        _record_attempt(surface, user_id, basic.accept, basic.reason)

    return result

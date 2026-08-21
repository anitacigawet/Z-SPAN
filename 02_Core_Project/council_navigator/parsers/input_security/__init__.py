"""Input security primitives for Z-SPAN's S-008 V0 deterministic hardening pass.

See `01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md` for the surface catalog
this module's primitives serve, and `01_Project_Overview/S008_INPUT_SECURITY_SPEC.md`
for the chunk-by-chunk build plan.

Defensive-only per `01_Project_Overview/DECISIONS.md § D-100`. This module
contains structural defenses; independent verification routes to the
Antigravity-Jules-Gemini-Pro path per James's Google AI Pro quota.
"""

from .primitives import (
    StructuralFenceError,
    UnicodeRejectionError,
    fence_with_nonce,
    extract_fenced_payload,
    contains_fence_marker,
    normalize_user_text,
    reject_if_bidi_controls,
    reject_if_mixed_script,
    sha256_content_hash,
)

__all__ = [
    "StructuralFenceError",
    "UnicodeRejectionError",
    "fence_with_nonce",
    "extract_fenced_payload",
    "contains_fence_marker",
    "normalize_user_text",
    "reject_if_bidi_controls",
    "reject_if_mixed_script",
    "sha256_content_hash",
]

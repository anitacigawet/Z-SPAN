"""S-008 V0 cross-surface defensive primitives.

These helpers implement the threat-model's P-class primitives in concrete code:

- P1 / P12 — structural separation of instruction from data, via per-run
  nonce fencing that adversarial content cannot guess + cannot pre-embed.
- P11 (partial) — unicode normalization + bidi-control rejection, to defend
  against rendering-layer trickery in user-supplied identifiers.
- P6 (partial) — content-hash helper for audit trail (`action_argument_origin`).

The threat-model that drives each surface's use of these primitives is at
`01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md`.

Defensive-only per `01_Project_Overview/DECISIONS.md § D-100`. No attack
scenarios live in this file.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from dataclasses import dataclass

# ── Errors ──────────────────────────────────────────────────────────────────


class StructuralFenceError(ValueError):
    """Raised when a fence boundary is malformed or the payload contains
    structural markers it should not."""


class UnicodeRejectionError(ValueError):
    """Raised when user-supplied text contains unicode the policy rejects
    (bidi controls, mixed-script combinations not on the allow profile)."""


# ── Fencing ─────────────────────────────────────────────────────────────────

_FENCE_BEGIN_RE = re.compile(
    r"<zspan-content-begin nonce=\"([0-9a-f]{32})\"[^>]*>", re.IGNORECASE
)
_FENCE_END_RE = re.compile(
    r"<zspan-content-end nonce=\"([0-9a-f]{32})\"[^>]*>", re.IGNORECASE
)

_FENCE_MARKER_PATTERNS = (
    "<zspan-content-begin",
    "<zspan-content-end",
)


def _new_nonce() -> str:
    """Generate a per-fence nonce (128 bits of entropy, hex-encoded).

    The nonce makes the fence boundary non-spoofable by content the caller
    embeds inside the fence — an adversarial payload would have to predict
    the nonce to inject a forged boundary, and `secrets.token_hex(16)` is
    unpredictable to anything reading the payload.
    """
    return secrets.token_hex(16)


def fence_with_nonce(payload: str, *, label: str | None = None) -> str:
    """Wrap untrusted ``payload`` in a per-fence-nonce delimiter pair.

    The fence is structural-marker safe even when the payload itself contains
    arbitrary user content: each fence call generates a fresh nonce, and the
    end marker repeats that same nonce. If the payload contains a literal
    "<zspan-content-begin" substring, the call raises StructuralFenceError —
    the caller is responsible for either rejecting the input or for
    transforming the literal marker before fencing.

    Per P12 + S-1/S-7 acceptance: the structural markers contain a per-run
    nonce so they cannot be guessed by adversarial content.

    Args:
        payload: the untrusted text to fence.
        label: optional human-readable label that appears alongside the
            begin marker. Cosmetic; not load-bearing.

    Returns:
        A multi-line string with the begin marker, the payload, and the end
        marker on their own lines.

    Raises:
        StructuralFenceError: if the payload contains literal fence markers.
    """
    if contains_fence_marker(payload):
        raise StructuralFenceError(
            "payload contains a fence-marker substring; "
            "callers must reject or transform such inputs before fencing"
        )
    nonce = _new_nonce()
    label_suffix = f" label=\"{label}\"" if label else ""
    return (
        f"<zspan-content-begin nonce=\"{nonce}\"{label_suffix}>\n"
        f"{payload}\n"
        f"<zspan-content-end nonce=\"{nonce}\">"
    )


def extract_fenced_payload(fenced: str) -> str:
    """Extract the payload between a matched fence pair.

    Confirms begin and end nonces match. Raises StructuralFenceError on any
    structural anomaly (missing markers, nonce mismatch, multiple begin
    markers, etc.).

    Useful for tests + for any downstream code that needs to undo the fence.
    Most callers will pass the fenced text to a model and never need to
    extract it back themselves; this helper exists for completeness +
    unit-test verification.
    """
    begin_matches = _FENCE_BEGIN_RE.findall(fenced)
    end_matches = _FENCE_END_RE.findall(fenced)
    if len(begin_matches) != 1:
        raise StructuralFenceError(
            f"expected exactly one begin marker, found {len(begin_matches)}"
        )
    if len(end_matches) != 1:
        raise StructuralFenceError(
            f"expected exactly one end marker, found {len(end_matches)}"
        )
    if begin_matches[0] != end_matches[0]:
        raise StructuralFenceError("begin nonce does not match end nonce")

    begin = _FENCE_BEGIN_RE.search(fenced)
    end = _FENCE_END_RE.search(fenced)
    assert begin is not None and end is not None
    payload = fenced[begin.end(): end.start()]
    # Strip the single newlines fence_with_nonce inserts around the payload.
    if payload.startswith("\n"):
        payload = payload[1:]
    if payload.endswith("\n"):
        payload = payload[:-1]
    return payload


def contains_fence_marker(text: str) -> bool:
    """Cheap pre-check: does ``text`` contain any literal fence marker?

    Callers use this before fencing (to decide whether to reject the input
    outright) and after receiving model output (to detect responses that
    smuggle structural markers back into a downstream context). Case-
    insensitive on the marker name; nonce checks happen separately.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _FENCE_MARKER_PATTERNS)


# ── Unicode hygiene ─────────────────────────────────────────────────────────

# Bidi controls per Unicode 15.0:
#   U+202A LRE, U+202B RLE, U+202C PDF, U+202D LRO, U+202E RLO,
#   U+2066 LRI, U+2067 RLI, U+2068 FSI, U+2069 PDI.
_BIDI_CONTROL_CHARS = frozenset(
    chr(cp) for cp in (
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2066, 0x2067, 0x2068, 0x2069,
    )
)

# Unicode scripts we permit by default for civic-input identifiers. Latin is
# the default English-civic baseline; specific cities may extend (e.g. a
# Hispanic-majority city may add Latin Extended + Common). The reject-if-
# mixed-script helper takes an explicit `allow` set so callers do not
# silently expand the policy.
_DEFAULT_ALLOWED_SCRIPTS = frozenset({"Latin", "Common", "Inherited"})


def normalize_user_text(text: str) -> str:
    """NFC-normalize ``text`` and strip non-printable control characters
    other than \\n and \\t.

    NFC is the W3C-recommended normalization for input that gets compared,
    persisted, or hashed. Stripping non-printable controls removes homoglyph-
    adjacent rendering trickery from display strings (does not affect the
    semantic content of legitimate civic input).
    """
    nfc = unicodedata.normalize("NFC", text)
    # Drop control chars except \n and \t. \r becomes \n; the rest are dropped.
    out_chars: list[str] = []
    for ch in nfc:
        if ch == "\r":
            out_chars.append("\n")
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("C") and ch not in ("\n", "\t"):
            continue
        out_chars.append(ch)
    return "".join(out_chars)


def reject_if_bidi_controls(text: str) -> None:
    """Raise UnicodeRejectionError if ``text`` contains any bidi control
    character.

    Bidi controls are a known display-spoofing primitive (a malicious
    string can render visually as one thing while computing as another).
    Civic-input strings do not need bidi controls; rejecting them is a
    cheap structural defense.
    """
    bad = [ch for ch in text if ch in _BIDI_CONTROL_CHARS]
    if bad:
        codepoints = ", ".join(f"U+{ord(ch):04X}" for ch in bad)
        raise UnicodeRejectionError(
            f"input contains bidi control characters: {codepoints}"
        )


def _script_of(ch: str) -> str:
    """Best-effort script classification.

    Uses unicodedata.name to extract the script qualifier from the character's
    name (the standard Python stdlib does not expose Script directly).
    Returns "Common" for ASCII letters/digits/punctuation and "Inherited" for
    combining marks. Anything not in the known list returns the first word of
    the unicode name as a script proxy.
    """
    if not ch:
        return "Common"
    cp = ord(ch)
    if cp < 0x0080:
        # ASCII range — Latin letters/digits/punctuation/spaces.
        if ch.isalpha():
            return "Latin"
        return "Common"
    cat = unicodedata.category(ch)
    if cat.startswith("M"):
        return "Inherited"
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return "Common"
    # Names like "CYRILLIC SMALL LETTER A" start with the script.
    return name.split(" ", 1)[0].capitalize()


def reject_if_mixed_script(
    text: str,
    *,
    allow: frozenset[str] = _DEFAULT_ALLOWED_SCRIPTS,
) -> None:
    """Raise UnicodeRejectionError if ``text`` contains characters outside
    the allow-list of unicode scripts.

    The default allow-list is Latin + Common + Inherited (English-civic
    baseline). Callers can pass a wider allow-list when the surface
    legitimately accepts other scripts (e.g. Spanish-language city
    intelligence, names with non-Latin characters); the wider list is an
    explicit policy choice, not a silent default.

    Defends against mixed-script homoglyph attacks (an adversarial display
    string that mixes Cyrillic and Latin letters to render as a known token
    while computing differently).
    """
    forbidden: list[tuple[str, str]] = []
    for ch in text:
        if ch.isspace():
            continue
        # Punctuation + symbols + numbers + marks are script-neutral by
        # category; only letter-category characters carry a script identity
        # that the mixed-script policy cares about.
        cat = unicodedata.category(ch)
        if cat.startswith(("P", "S", "N", "M")):
            continue
        script = _script_of(ch)
        if script not in allow:
            forbidden.append((ch, script))
    if forbidden:
        sample = ", ".join(
            f"{repr(ch)}({script})" for ch, script in forbidden[:5]
        )
        raise UnicodeRejectionError(
            f"input contains characters outside the allowed script set: {sample}"
        )


# ── Audit-trail hashing ─────────────────────────────────────────────────────


def sha256_content_hash(content: str) -> str:
    """SHA-256 hex digest of the UTF-8 encoding of ``content``.

    Used by audit-log writers to record an `action_argument_origin` proof
    of the row content the agent acted on. Adversarial content that later
    changes can be detected post-hoc by re-hashing + comparing.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ── Convenience: full pre-submission moderation primitive ───────────────────


@dataclass
class ModerationResult:
    """Result of `moderate_basic_input`. Used by the S-008 chunk 3
    moderation primitive consumers (creator_feedback + suggestion_query
    surfaces — see S008_INPUT_SECURITY_SPEC.md chunk 3)."""

    accept: bool
    reason: str
    normalized_text: str | None


def moderate_basic_input(
    text: str,
    *,
    max_length: int,
    allow_scripts: frozenset[str] = _DEFAULT_ALLOWED_SCRIPTS,
    max_urls: int = 0,
) -> ModerationResult:
    """Apply the V0 deterministic-rule pre-submission moderation pass.

    Per S008_INPUT_SECURITY_SPEC.md chunk 3, this is the structural floor
    for user-input surfaces. LLM-class moderation (a Haiku classifier) is
    a V1+ extension; chunk 3 ships deterministic rules first.

    Returns ModerationResult(accept, reason, normalized_text):
      - accept=True, reason="clean" — text passes all rules; normalized_text
        is the NFC + control-stripped form callers should persist.
      - accept=False, reason=<rule> — text fails; caller rejects with
        the reason in a structured 400 error.

    Args:
        text: raw user-supplied text.
        max_length: surface-configurable length cap.
        allow_scripts: unicode scripts the surface permits.
        max_urls: cap on the number of URLs in the text. Zero (default)
            means URLs are not allowed at all.
    """
    if not isinstance(text, str):
        return ModerationResult(
            accept=False, reason="non_string", normalized_text=None
        )
    if len(text) > max_length:
        return ModerationResult(
            accept=False, reason="too_long", normalized_text=None
        )

    try:
        reject_if_bidi_controls(text)
    except UnicodeRejectionError:
        return ModerationResult(
            accept=False, reason="bidi_controls", normalized_text=None
        )

    if contains_fence_marker(text):
        return ModerationResult(
            accept=False, reason="fence_marker_in_input", normalized_text=None
        )

    normalized = normalize_user_text(text)

    try:
        reject_if_mixed_script(normalized, allow=allow_scripts)
    except UnicodeRejectionError:
        return ModerationResult(
            accept=False, reason="mixed_script", normalized_text=None
        )

    # Crude URL count: matches the common shape `http(s)://...` + bare
    # `www.` prefixes. Deliberately conservative — the goal is structural
    # rejection of URL-heavy payloads, not full URL parsing.
    url_count = (
        normalized.lower().count("http://")
        + normalized.lower().count("https://")
        + len(re.findall(r"\bwww\.[\w.-]+", normalized, flags=re.IGNORECASE))
    )
    if url_count > max_urls:
        return ModerationResult(
            accept=False, reason="too_many_urls", normalized_text=None
        )

    # Shell-escape-shaped substrings the V0 rule rejects: backticks,
    # `$(`, `<script`, `javascript:`, `data:` URI prefix. Caller surfaces
    # do not legitimately need these in civic-input contexts.
    lowered = normalized.lower()
    for needle in ("`", "$(", "<script", "javascript:", "data:text/html",
                   "data:application"):
        if needle in lowered:
            return ModerationResult(
                accept=False,
                reason=f"shell_or_script_pattern:{needle.strip()}",
                normalized_text=None,
            )

    return ModerationResult(
        accept=True, reason="clean", normalized_text=normalized
    )

"""Invitation-gated email/password authentication for Z-SPAN accounts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlsplit

import requests

try:
    from parsers import database
    from parsers.account_system import User, normalize_account_email
except ImportError:  # Direct imports from parsers/ at runtime.
    import database  # type: ignore[no-redef]
    from account_system import User, normalize_account_email  # type: ignore[no-redef]


logger = logging.getLogger(__name__)

PASSWORD_MIN_CHARS = 15
PASSWORD_MAX_CHARS = 256
PASSWORD_MAX_BYTES = 1024
DISPLAY_NAME_MAX_CHARS = 80
EMAIL_MAX_CHARS = 254

# OWASP's current scrypt floor. Tests may patch these module constants for
# speed, but the production path does not accept weaker request parameters.
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM_BYTES = 256 * 1024 * 1024

FAILED_ATTEMPT_LIMIT = 5
LOCKOUT_SECONDS = 15 * 60
RESET_TOKEN_TTL_SECONDS = 60 * 60
RESET_REQUEST_COOLDOWN_SECONDS = 60

RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_SENDER_ADDRESS = "Z-SPAN <notifications@zspan.org>"
DEFAULT_PUBLIC_ORIGIN = "https://zspan.org"
HTTP_TIMEOUT_SECONDS = 8

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+$")
_RESET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_DUMMY_SALT = b"zspan-password-auth-dummy-salt"
_DUMMY_HASH = b"\0" * SCRYPT_DKLEN


class PasswordValidationError(ValueError):
    """A password cannot be accepted under the public account contract."""


class AccountInputError(ValueError):
    """A registration field has an invalid public shape."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_email(email: object) -> str:
    if not isinstance(email, str):
        raise AccountInputError("Enter a valid email address.")
    normalized = normalize_account_email(email)
    if len(normalized) > EMAIL_MAX_CHARS or _EMAIL_RE.fullmatch(normalized) is None:
        raise AccountInputError("Enter a valid email address.")
    return normalized


def validate_display_name(display_name: object) -> str:
    if not isinstance(display_name, str):
        raise AccountInputError("Enter the name you would like to use.")
    normalized = " ".join(display_name.split())
    if (
        not normalized
        or len(normalized) > DISPLAY_NAME_MAX_CHARS
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized)
    ):
        raise AccountInputError("Enter the name you would like to use.")
    return normalized


def validate_password(password: object) -> str:
    if not isinstance(password, str):
        raise PasswordValidationError("Enter a password.")
    encoded = password.encode("utf-8")
    if len(password) < PASSWORD_MIN_CHARS:
        raise PasswordValidationError(
            f"Use at least {PASSWORD_MIN_CHARS} characters."
        )
    if len(password) > PASSWORD_MAX_CHARS or len(encoded) > PASSWORD_MAX_BYTES:
        raise PasswordValidationError("That password is too long.")
    return password


def _derive_password(
    password: str,
    salt: bytes,
    *,
    n: Optional[int] = None,
    r: Optional[int] = None,
    p: Optional[int] = None,
    dklen: Optional[int] = None,
) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N if n is None else n,
        r=SCRYPT_R if r is None else r,
        p=SCRYPT_P if p is None else p,
        dklen=SCRYPT_DKLEN if dklen is None else dklen,
        maxmem=SCRYPT_MAXMEM_BYTES,
    )


def _new_credential(password: str) -> dict[str, object]:
    validated = validate_password(password)
    salt = secrets.token_bytes(16)
    derived = _derive_password(validated, salt)
    return {
        "password_hash": base64.urlsafe_b64encode(derived).decode("ascii"),
        "password_salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "scrypt_n": SCRYPT_N,
        "scrypt_r": SCRYPT_R,
        "scrypt_p": SCRYPT_P,
        "scrypt_dklen": SCRYPT_DKLEN,
    }


def _verify_credential(password: str, row: object) -> bool:
    try:
        salt = base64.urlsafe_b64decode(row["password_salt"])
        expected = base64.urlsafe_b64decode(row["password_hash"])
        actual = _derive_password(
            password,
            salt,
            n=int(row["scrypt_n"]),
            r=int(row["scrypt_r"]),
            p=int(row["scrypt_p"]),
            dklen=int(row["scrypt_dklen"]),
        )
    except (KeyError, TypeError, ValueError):
        logger.exception("Stored password credential is invalid")
        return False
    return hmac.compare_digest(actual, expected)


def _consume_dummy_verification(password: str) -> None:
    try:
        actual = _derive_password(password, _DUMMY_SALT)
        hmac.compare_digest(actual, _DUMMY_HASH)
    except ValueError:
        # Oversized/otherwise malformed passwords are still rejected without
        # allowing the caller to influence stored verifier parameters.
        pass


def _user_from_row(row: object) -> User:
    return User(
        int(row["id"]),
        row["google_sub"],
        str(row["email"]),
        row["display_name"],
        row["avatar_url"],
        str(row["role"]),
        str(row["created_at"]),
        str(row["last_seen_at"]),
    )


def register_invited_user(
    *,
    email: object,
    display_name: object,
    password: object,
    invitation_token: object,
    forbidden_emails: frozenset[str] = frozenset(),
) -> tuple[str, Optional[User]]:
    """Create one local account and consume its invitation atomically."""
    normalized_email = validate_email(email)
    normalized_name = validate_display_name(display_name)
    if normalized_email in forbidden_emails:
        return "email_unavailable", None
    if not isinstance(invitation_token, str):
        return "invitation_unavailable", None
    # Reject random public probes before invoking the memory-hard verifier.
    # The atomic redemption below remains authoritative if a real card changes
    # state between this cheap preflight and the transaction.
    if database.get_invitation_status(invitation_token) != "active":
        return "invitation_unavailable", None
    credential = _new_credential(validate_password(password))

    conn = database.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM users WHERE lower(email) = ?",
            (normalized_email,),
        ).fetchone()
        if existing is not None:
            conn.rollback()
            return "email_unavailable", None

        cursor = conn.execute(
            """
            INSERT INTO users (
                google_sub, email, display_name, avatar_url, role
            ) VALUES (NULL, ?, ?, NULL, 'light')
            """,
            (normalized_email, normalized_name),
        )
        user_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO password_credentials (
                user_id, password_hash, password_salt,
                scrypt_n, scrypt_r, scrypt_p, scrypt_dklen
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                credential["password_hash"],
                credential["password_salt"],
                credential["scrypt_n"],
                credential["scrypt_r"],
                credential["scrypt_p"],
                credential["scrypt_dklen"],
            ),
        )
        invitation_result = database.redeem_invitation_token_with_connection(
            conn,
            user_id,
            invitation_token,
        )
        if invitation_result != "redeemed":
            conn.rollback()
            return "invitation_unavailable", None

        row = conn.execute(
            """
            SELECT id, google_sub, email, display_name, avatar_url, role,
                   created_at, last_seen_at
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        conn.commit()
        if row is None:
            raise RuntimeError("invited account did not persist")
        return "registered", _user_from_row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def authenticate_password(
    *,
    email: object,
    password: object,
    invitation_token: object = None,
) -> tuple[str, Optional[User]]:
    """Verify a local credential and optionally consume an invitation."""
    try:
        normalized_email = validate_email(email)
    except AccountInputError:
        normalized_email = ""
    supplied_password = password if isinstance(password, str) else ""

    conn = database.get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                u.id, u.google_sub, u.email, u.display_name, u.avatar_url,
                u.role, u.created_at, u.last_seen_at,
                p.password_hash, p.password_salt,
                p.scrypt_n, p.scrypt_r, p.scrypt_p, p.scrypt_dklen,
                p.failed_attempts, p.locked_until
            FROM users AS u
            JOIN password_credentials AS p ON p.user_id = u.id
            WHERE lower(u.email) = ?
            """,
            (normalized_email,),
        ).fetchone()

        if row is None:
            _consume_dummy_verification(supplied_password)
            return "invalid", None

        password_matches = _verify_credential(supplied_password, row)
        now = _utcnow()
        locked_until = _parse_timestamp(row["locked_until"])
        if locked_until is not None and locked_until > now:
            return "locked", None

        if not password_matches:
            next_failed = int(row["failed_attempts"]) + 1
            next_locked_until = (
                _timestamp(now + timedelta(seconds=LOCKOUT_SECONDS))
                if next_failed >= FAILED_ATTEMPT_LIMIT
                else None
            )
            conn.execute(
                """
                UPDATE password_credentials
                SET failed_attempts = ?, locked_until = ?, last_failed_at = ?
                WHERE user_id = ?
                """,
                (next_failed, next_locked_until, _timestamp(now), int(row["id"])),
            )
            conn.commit()
            return ("locked" if next_locked_until else "invalid"), None

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE password_credentials
            SET failed_attempts = 0, locked_until = NULL, last_failed_at = NULL
            WHERE user_id = ?
            """,
            (int(row["id"]),),
        )
        conn.execute(
            "UPDATE users SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(row["id"]),),
        )
        if isinstance(invitation_token, str) and invitation_token:
            invitation_result = database.redeem_invitation_token_with_connection(
                conn,
                int(row["id"]),
                invitation_token,
            )
            if invitation_result not in {"redeemed", "already_granted"}:
                # Authentication still succeeds. The invitation page will show
                # the honest unavailable state after the redirect.
                logger.info(
                    "password login did not consume invitation: %s",
                    invitation_result,
                )
        conn.commit()
        refreshed = conn.execute(
            """
            SELECT id, google_sub, email, display_name, avatar_url, role,
                   created_at, last_seen_at
            FROM users WHERE id = ?
            """,
            (int(row["id"]),),
        ).fetchone()
        return "authenticated", _user_from_row(refreshed)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_password_reset_token(email: object) -> tuple[Optional[str], Optional[str]]:
    """Create a one-hour reset bearer for an existing local credential."""
    try:
        normalized_email = validate_email(email)
    except AccountInputError:
        return None, None

    conn = database.get_connection()
    try:
        row = conn.execute(
            """
            SELECT u.id, u.email
            FROM users AS u
            JOIN password_credentials AS p ON p.user_id = u.id
            WHERE lower(u.email) = ?
            """,
            (normalized_email,),
        ).fetchone()
        if row is None:
            return None, None

        recent = conn.execute(
            """
            SELECT 1
            FROM password_reset_tokens
            WHERE user_id = ?
              AND created_at > datetime('now', ?)
            LIMIT 1
            """,
            (int(row["id"]), f"-{RESET_REQUEST_COOLDOWN_SECONDS} seconds"),
        ).fetchone()
        if recent is not None:
            return None, None

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        now = _utcnow()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE password_reset_tokens
            SET used_at = ?
            WHERE user_id = ? AND used_at IS NULL
            """,
            (_timestamp(now), int(row["id"])),
        )
        conn.execute(
            """
            INSERT INTO password_reset_tokens (
                user_id, token_hash, expires_at
            ) VALUES (?, ?, ?)
            """,
            (
                int(row["id"]),
                token_hash,
                _timestamp(now + timedelta(seconds=RESET_TOKEN_TTL_SECONDS)),
            ),
        )
        conn.commit()
        return raw_token, str(row["email"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_password(
    *,
    token: object,
    password: object,
) -> tuple[str, Optional[User]]:
    """Consume one reset bearer, replace the verifier, and return its user."""
    validated_password = validate_password(password)
    raw_token = token if isinstance(token, str) else ""
    token_hash = (
        hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        if _RESET_TOKEN_RE.fullmatch(raw_token)
        else ""
    )
    now = _utcnow()

    conn = database.get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                r.id AS reset_id, r.user_id,
                u.id, u.google_sub, u.email, u.display_name, u.avatar_url,
                u.role, u.created_at, u.last_seen_at
            FROM password_reset_tokens AS r
            JOIN users AS u ON u.id = r.user_id
            WHERE r.token_hash = ? AND r.used_at IS NULL AND r.expires_at > ?
            """,
            (token_hash, _timestamp(now)),
        ).fetchone()
        if row is None:
            return "invalid", None

        # Do the memory-hard work only after a real, live reset bearer was
        # found. Random public requests therefore cannot turn this endpoint
        # into an unauthenticated scrypt resource-exhaustion primitive.
        credential = _new_credential(validated_password)

        conn.execute("BEGIN IMMEDIATE")
        still_live = conn.execute(
            """
            SELECT id
            FROM password_reset_tokens
            WHERE id = ? AND used_at IS NULL AND expires_at > ?
            """,
            (int(row["reset_id"]), _timestamp(_utcnow())),
        ).fetchone()
        if still_live is None:
            conn.rollback()
            return "invalid", None

        conn.execute(
            """
            UPDATE password_credentials
            SET password_hash = ?, password_salt = ?,
                scrypt_n = ?, scrypt_r = ?, scrypt_p = ?, scrypt_dklen = ?,
                failed_attempts = 0, locked_until = NULL, last_failed_at = NULL,
                password_changed_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (
                credential["password_hash"],
                credential["password_salt"],
                credential["scrypt_n"],
                credential["scrypt_r"],
                credential["scrypt_p"],
                credential["scrypt_dklen"],
                int(row["user_id"]),
            ),
        )
        conn.execute(
            """
            UPDATE password_reset_tokens
            SET used_at = ?
            WHERE user_id = ? AND used_at IS NULL
            """,
            (_timestamp(now), int(row["user_id"])),
        )
        conn.commit()
        return "reset", _user_from_row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _public_origin() -> str:
    origin = (
        os.environ.get("ZSPAN_PUBLIC_ORIGIN", DEFAULT_PUBLIC_ORIGIN).strip()
        or DEFAULT_PUBLIC_ORIGIN
    ).rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(char in origin for char in "\r\n<>")
    ):
        raise ValueError("ZSPAN_PUBLIC_ORIGIN must be a bare http(s) origin")
    return origin


def send_password_reset_email(email: str, raw_token: str) -> bool:
    """Send a reset link without ever logging or persisting the bearer."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        logger.warning("Password reset email skipped: RESEND_API_KEY is not configured")
        return False

    # Keep the recovery bearer in the URL fragment. Fragments are available to
    # the SPA but are never sent in the HTTP request or ordinary access logs.
    reset_url = f"{_public_origin()}/login#{urlencode({'reset': raw_token})}"
    safe_url = html.escape(reset_url, quote=True)
    payload = {
        "from": (
            os.environ.get("ZSPAN_SENDER_ADDRESS", DEFAULT_SENDER_ADDRESS).strip()
            or DEFAULT_SENDER_ADDRESS
        ),
        "to": [email],
        "subject": "Reset your Z-SPAN password",
        "text": (
            "Use this link to choose a new Z-SPAN password. "
            f"The link expires in one hour:\n\n{reset_url}\n\n"
            "If you did not request this, you can ignore this email."
        ),
        "html": (
            "<!doctype html><html><body>"
            "<p>Use this link to choose a new Z-SPAN password. "
            "The link expires in one hour.</p>"
            f'<p><a href="{safe_url}">Choose a new password</a></p>'
            "<p>If you did not request this, you can ignore this email.</p>"
            "</body></html>"
        ),
    }
    try:
        response = requests.post(
            RESEND_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": (
                    "zspan-password-reset-"
                    + hashlib.sha256(raw_token.encode("ascii")).hexdigest()
                ),
            },
            json=payload,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return True
    except Exception:
        logger.exception("Password reset email delivery failed")
        return False

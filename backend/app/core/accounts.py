"""Human password authentication and short-lived browser sessions.

Passwords authenticate people at the OAuth consent surface. They never become MCP
credentials and never enter the room domain: after consent, the client still receives the
same PKCE-protected, audience-bound agent token as before.

The expensive Argon2 work runs off the event loop and behind a small semaphore. Without
both, a handful of login attempts could either freeze every room request or exhaust the
512 MiB Hosted-lite machine while each verifier allocated its memory cost concurrently.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from dataclasses import dataclass

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from ..db import database as db
from ..domain import ids
from ..domain.identity import User
from ..util import from_iso, hash_token, is_past, iso_in, new_token, utcnow, utcnow_iso
from . import store

PASSWORD_MAX_LENGTH = 1024
SESSION_TTL_SECONDS = 8 * 3600
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_BLOCK_SECONDS = 15 * 60
LOGIN_FAILURE_LIMIT = 5
VERIFY_EMAIL_TTL_SECONDS = 24 * 3600
RESET_PASSWORD_TTL_SECONDS = 3600
ACCOUNT_FORM_TTL_SECONDS = 15 * 60

_password_hasher = PasswordHasher()
_password_workers = asyncio.Semaphore(2)

# A missing account must still pay the same password-verification cost as an existing one,
# otherwise the login endpoint becomes an email-address oracle by timing alone.
_dummy_password_hash = _password_hasher.hash(new_token())


class LoginDenied(Exception):
    """One deliberately generic login failure for unknown users, bad passwords and blocks."""

    def __init__(self, *, retry_after: int | None = None) -> None:
        super().__init__("Incorrect email or password.")
        self.retry_after = retry_after


class AccountExists(Exception):
    """The normalized email is already registered."""


class InvalidAccountAction(Exception):
    """A verification/reset bearer is unknown, expired, or already consumed."""


@dataclass(frozen=True)
class BrowserSession:
    token_hash: str
    user: User
    csrf_token: str
    expires_at: str


@dataclass(frozen=True)
class RegistrationResult:
    user: User
    verification_token: str


@dataclass(frozen=True)
class AccountBrowserFlow:
    token_hash: str
    purpose: str
    csrf_token: str
    expires_at: str


def normalize_email(email: str) -> str:
    """The current account model treats one normalized email as one global user."""
    return email.strip().lower()[:320]


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
        raise ValueError("Enter a valid email address.")
    return normalized


def validate_password_hash(encoded: str) -> None:
    """Refuse a typo or a weaker Argon2 variant in deployment configuration."""
    try:
        parameters = extract_parameters(encoded)
    except InvalidHashError as exc:
        raise ValueError("OPERATOR_PASSWORD_HASH is not a valid Argon2 encoded hash.") from exc
    if parameters.type is not Type.ID:
        raise ValueError("OPERATOR_PASSWORD_HASH must use Argon2id.")


def hash_password(password: str) -> str:
    """Generate a verifier for provisioning tools; plaintext is never persisted."""
    if len(password) < 15:
        raise ValueError("Password must be at least 15 characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")
    return _password_hasher.hash(password)


async def set_password_hash(user_id: str, encoded: str) -> bool:
    """Install or rotate a verifier and revoke that user's browser sessions.

    Returns whether anything changed. Startup calls this on every boot, so keeping the
    unchanged path read-only avoids signing a user out on every deploy.
    """
    validate_password_hash(encoded)
    now = utcnow_iso()
    async with db.transaction() as tx:
        row = await tx.fetch_one(
            "SELECT password_hash FROM user_password_credentials WHERE user_id = ?", (user_id,)
        )
        changed = row is None or not hmac.compare_digest(row["password_hash"], encoded)
        if changed:
            await tx.execute(
                """
                INSERT INTO user_password_credentials (
                    user_id, password_hash, created_at, password_changed_at
                ) VALUES (?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    password_changed_at = excluded.password_changed_at
                """,
                (user_id, encoded, now, now),
            )
            await tx.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
        await tx.execute(
            """
            INSERT INTO account_status (
                user_id, email_verified_at, created_at, updated_at
            ) VALUES (?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                email_verified_at = COALESCE(account_status.email_verified_at,
                                             excluded.email_verified_at),
                updated_at = excluded.updated_at
            """,
            (user_id, now, now, now),
        )
    return changed


async def mark_email_verified(user_id: str) -> None:
    """Provisioned accounts are trusted by the provisioning act itself."""
    now = utcnow_iso()
    await db.execute(
        """
        INSERT INTO account_status (user_id, email_verified_at, created_at, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            email_verified_at = COALESCE(account_status.email_verified_at,
                                         excluded.email_verified_at),
            updated_at = excluded.updated_at
        """,
        (user_id, now, now, now),
    )


async def _hash_password_async(password: str) -> str:
    async with _password_workers:
        return await asyncio.to_thread(hash_password, password)


def _personal_org_slug(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    stem = "".join(c if c.isalnum() else "-" for c in local)
    stem = "-".join(part for part in stem.split("-") if part)[:32] or "account"
    return f"{stem}-{new_token(5).lower().replace('_', '').replace('-', '')}"


async def register_account(*, email: str, display_name: str, password: str) -> RegistrationResult:
    """Create one free account and its personal organization, still unverified."""
    normalized = validate_email(email)
    clean_name = display_name.strip()
    if not clean_name or len(clean_name) > 80:
        raise ValueError("Display name must be between 1 and 80 characters.")
    encoded = await _hash_password_async(password)
    now = utcnow_iso()
    org_id = ids.new_id(ids.ORG)
    user_id = ids.new_id(ids.USER)
    verification_token = new_token()
    async with db.transaction() as tx:
        existing = await tx.fetch_one("SELECT 1 FROM users WHERE lower(email) = ?", (normalized,))
        if existing is not None:
            raise AccountExists
        await tx.execute(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES (?,?,?,?)",
            (org_id, f"{clean_name}'s Cottage", _personal_org_slug(normalized), now),
        )
        await tx.execute(
            "INSERT INTO users (id, org_id, email, display_name, role, created_at) "
            "VALUES (?,?,?,?, 'owner', ?)",
            (user_id, org_id, normalized, clean_name, now),
        )
        await tx.execute(
            "INSERT INTO user_password_credentials "
            "(user_id, password_hash, created_at, password_changed_at) VALUES (?,?,?,?)",
            (user_id, encoded, now, now),
        )
        await tx.execute(
            "INSERT INTO account_status (user_id, created_at, updated_at) VALUES (?,?,?)",
            (user_id, now, now),
        )
        await tx.execute(
            "INSERT INTO account_action_tokens "
            "(token_hash, user_id, purpose, created_at, expires_at) VALUES (?,?,?,?,?)",
            (
                hash_token(verification_token),
                user_id,
                "verify_email",
                now,
                iso_in(VERIFY_EMAIL_TTL_SECONDS),
            ),
        )
        row = await tx.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
        assert row is not None
    return RegistrationResult(user=store.to_user(row), verification_token=verification_token)


async def consume_email_verification(token: str) -> User:
    now = utcnow_iso()
    token_hash = hash_token(token)
    async with db.transaction() as tx:
        row = await tx.fetch_one(
            """
            SELECT a.*, u.*
              FROM account_action_tokens a JOIN users u ON u.id = a.user_id
             WHERE a.token_hash = ? AND a.purpose = 'verify_email'
            """,
            (token_hash,),
        )
        if row is None or row["consumed_at"] or is_past(row["expires_at"]):
            raise InvalidAccountAction
        affected = await tx.execute(
            "UPDATE account_action_tokens SET consumed_at = ? "
            "WHERE token_hash = ? AND consumed_at IS NULL AND expires_at > ?",
            (now, token_hash, now),
        )
        if affected == 0:
            raise InvalidAccountAction
        await tx.execute(
            "UPDATE account_status SET email_verified_at = ?, updated_at = ? WHERE user_id = ?",
            (now, now, row["user_id"]),
        )
    return store.to_user(row)


async def create_email_verification(email: str) -> tuple[User, str] | None:
    normalized = normalize_email(email)
    row = await db.fetch_one(
        """
        SELECT u.*
          FROM users u JOIN account_status s ON s.user_id = u.id
         WHERE lower(u.email) = ? AND s.email_verified_at IS NULL
           AND s.disabled_at IS NULL
        """,
        (normalized,),
    )
    if row is None:
        return None
    token = new_token()
    now = utcnow_iso()
    async with db.transaction() as tx:
        await tx.execute(
            "UPDATE account_action_tokens SET consumed_at = ? "
            "WHERE user_id = ? AND purpose = 'verify_email' AND consumed_at IS NULL",
            (now, row["id"]),
        )
        await tx.execute(
            "INSERT INTO account_action_tokens "
            "(token_hash, user_id, purpose, created_at, expires_at) VALUES (?,?,?,?,?)",
            (
                hash_token(token),
                row["id"],
                "verify_email",
                now,
                iso_in(VERIFY_EMAIL_TTL_SECONDS),
            ),
        )
    return store.to_user(row), token


async def create_password_reset(email: str) -> tuple[User, str] | None:
    normalized = normalize_email(email)
    row = await db.fetch_one(
        """
        SELECT u.*
          FROM users u JOIN account_status s ON s.user_id = u.id
         WHERE lower(u.email) = ? AND s.email_verified_at IS NOT NULL
           AND s.disabled_at IS NULL
        """,
        (normalized,),
    )
    if row is None:
        return None
    token = new_token()
    now = utcnow_iso()
    async with db.transaction() as tx:
        await tx.execute(
            "UPDATE account_action_tokens SET consumed_at = ? "
            "WHERE user_id = ? AND purpose = 'reset_password' AND consumed_at IS NULL",
            (now, row["id"]),
        )
        await tx.execute(
            "INSERT INTO account_action_tokens "
            "(token_hash, user_id, purpose, created_at, expires_at) VALUES (?,?,?,?,?)",
            (
                hash_token(token),
                row["id"],
                "reset_password",
                now,
                iso_in(RESET_PASSWORD_TTL_SECONDS),
            ),
        )
    return store.to_user(row), token


async def reset_password(token: str, password: str) -> User:
    encoded = await _hash_password_async(password)
    now = utcnow_iso()
    token_hash = hash_token(token)
    async with db.transaction() as tx:
        row = await tx.fetch_one(
            """
            SELECT a.*, u.*
              FROM account_action_tokens a JOIN users u ON u.id = a.user_id
             WHERE a.token_hash = ? AND a.purpose = 'reset_password'
            """,
            (token_hash,),
        )
        if row is None or row["consumed_at"] or is_past(row["expires_at"]):
            raise InvalidAccountAction
        affected = await tx.execute(
            "UPDATE account_action_tokens SET consumed_at = ? "
            "WHERE token_hash = ? AND consumed_at IS NULL AND expires_at > ?",
            (now, token_hash, now),
        )
        if affected == 0:
            raise InvalidAccountAction
        await tx.execute(
            "UPDATE user_password_credentials SET password_hash = ?, password_changed_at = ? "
            "WHERE user_id = ?",
            (encoded, now, row["user_id"]),
        )
        await tx.execute(
            "UPDATE web_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now, row["user_id"]),
        )
    return store.to_user(row)


async def create_account_browser_flow(purpose: str) -> tuple[str, AccountBrowserFlow]:
    token = new_token()
    csrf = new_token()
    now = utcnow_iso()
    expires_at = iso_in(ACCOUNT_FORM_TTL_SECONDS)
    token_hash = hash_token(token)
    await db.execute(
        "INSERT INTO account_browser_flows "
        "(flow_hash, purpose, csrf_token, created_at, expires_at) VALUES (?,?,?,?,?)",
        (token_hash, purpose, csrf, now, expires_at),
    )
    return token, AccountBrowserFlow(token_hash, purpose, csrf, expires_at)


async def consume_account_browser_flow(token: str | None, *, purpose: str, csrf_token: str) -> bool:
    if not token or not csrf_token:
        return False
    now = utcnow_iso()
    token_hash = hash_token(token)
    async with db.transaction() as tx:
        row = await tx.fetch_one(
            "SELECT * FROM account_browser_flows WHERE flow_hash = ? AND purpose = ?",
            (token_hash, purpose),
        )
        if (
            row is None
            or row["consumed_at"]
            or is_past(row["expires_at"])
            or not hmac.compare_digest(row["csrf_token"], csrf_token)
        ):
            return False
        return (
            await tx.execute(
                "UPDATE account_browser_flows SET consumed_at = ? "
                "WHERE flow_hash = ? AND consumed_at IS NULL AND expires_at > ?",
                (now, token_hash, now),
            )
            == 1
        )


def _bucket(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}:{value}".encode()).hexdigest()


def _login_buckets(email: str, remote_address: str) -> tuple[str, str]:
    return _bucket("account", email), _bucket("ip", remote_address or "unknown")


async def _blocked_until(buckets: tuple[str, str]) -> str | None:
    placeholders = ",".join("?" for _ in buckets)
    rows = await db.fetch_all(
        f"SELECT blocked_until FROM login_attempts WHERE bucket_hash IN ({placeholders})",
        buckets,
    )
    active = [
        row["blocked_until"]
        for row in rows
        if row["blocked_until"] and not is_past(row["blocked_until"])
    ]
    return max(active) if active else None


async def _record_failure(buckets: tuple[str, str]) -> str | None:
    now = utcnow()
    now_iso = utcnow_iso()
    latest_block: str | None = None
    async with db.transaction() as tx:
        for bucket in buckets:
            row = await tx.fetch_one(
                "SELECT * FROM login_attempts WHERE bucket_hash = ?", (bucket,)
            )
            within_window = bool(
                row
                and (now - from_iso(row["last_failed_at"])).total_seconds() <= LOGIN_WINDOW_SECONDS
            )
            failures = int(row["failures"]) + 1 if within_window and row else 1
            first_failed_at = row["first_failed_at"] if within_window and row else now_iso
            blocked_until = (
                iso_in(LOGIN_BLOCK_SECONDS, since=now) if failures >= LOGIN_FAILURE_LIMIT else None
            )
            await tx.execute(
                """
                INSERT INTO login_attempts (
                    bucket_hash, failures, first_failed_at, last_failed_at, blocked_until
                ) VALUES (?,?,?,?,?)
                ON CONFLICT(bucket_hash) DO UPDATE SET
                    failures = excluded.failures,
                    first_failed_at = excluded.first_failed_at,
                    last_failed_at = excluded.last_failed_at,
                    blocked_until = excluded.blocked_until
                """,
                (bucket, failures, first_failed_at, now_iso, blocked_until),
            )
            if blocked_until and (latest_block is None or blocked_until > latest_block):
                latest_block = blocked_until
    return latest_block


async def _verify_password(encoded: str, password: str) -> bool:
    candidate = password if len(password) <= PASSWORD_MAX_LENGTH else ""
    async with _password_workers:
        try:
            return bool(await asyncio.to_thread(_password_hasher.verify, encoded, candidate))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


async def authenticate_password(email: str, password: str, remote_address: str) -> User:
    """Authenticate without disclosing whether an email, credential or throttle exists."""
    normalized = normalize_email(email)
    buckets = _login_buckets(normalized, remote_address)
    existing_block = await _blocked_until(buckets)
    row = await db.fetch_one(
        """
        SELECT u.*, c.password_hash,
               s.email_verified_at AS account_email_verified_at,
               s.disabled_at AS account_disabled_at
          FROM users u
          LEFT JOIN user_password_credentials c ON c.user_id = u.id
          LEFT JOIN account_status s ON s.user_id = u.id
         WHERE lower(u.email) = ?
        """,
        (normalized,),
    )
    encoded = (
        row["password_hash"] if row is not None and row["password_hash"] else _dummy_password_hash
    )
    verified = await _verify_password(encoded, password)

    if (
        existing_block
        or row is None
        or not row["password_hash"]
        or not row["account_email_verified_at"]
        or row["account_disabled_at"]
        or not verified
    ):
        new_block = await _record_failure(buckets)
        blocked_until = (
            max(v for v in (existing_block, new_block) if v)
            if (existing_block or new_block)
            else None
        )
        retry_after = None
        if blocked_until:
            retry_after = max(1, int((from_iso(blocked_until) - utcnow()).total_seconds()))
        raise LoginDenied(retry_after=retry_after)

    # A successful login clears only its account bucket. Clearing the IP bucket would let
    # one valid account erase password-spraying evidence for every other account at that IP.
    await db.execute("DELETE FROM login_attempts WHERE bucket_hash = ?", (buckets[0],))
    if _password_hasher.check_needs_rehash(encoded):
        replacement = await asyncio.to_thread(_password_hasher.hash, password)
        await db.execute(
            "UPDATE user_password_credentials SET password_hash = ?, password_changed_at = ? "
            "WHERE user_id = ?",
            (replacement, utcnow_iso(), row["id"]),
        )
    return store.to_user(row)


async def create_session(user_id: str) -> tuple[str, BrowserSession]:
    token = new_token()
    token_hash = hash_token(token)
    csrf_token = new_token()
    now = utcnow_iso()
    expires_at = iso_in(SESSION_TTL_SECONDS)
    await db.execute(
        """
        INSERT INTO web_sessions (
            token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (token_hash, user_id, csrf_token, now, now, expires_at),
    )
    session = await load_session(token)
    assert session is not None
    return token, session


async def load_session(token: str | None) -> BrowserSession | None:
    if not token:
        return None
    token_hash = hash_token(token)
    row = await db.fetch_one(
        """
        SELECT s.token_hash, s.csrf_token, s.expires_at, s.revoked_at, u.*
          FROM web_sessions s JOIN users u ON u.id = s.user_id
         WHERE s.token_hash = ?
        """,
        (token_hash,),
    )
    if row is None or row["revoked_at"] or is_past(row["expires_at"]):
        return None
    await db.execute(
        "UPDATE web_sessions SET last_seen_at = ? WHERE token_hash = ?",
        (utcnow_iso(), token_hash),
    )
    return BrowserSession(
        token_hash=token_hash,
        user=store.to_user(row),
        csrf_token=row["csrf_token"],
        expires_at=row["expires_at"],
    )


def csrf_matches(session: BrowserSession, candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(session.csrf_token, candidate)


async def revoke_session(token: str | None) -> None:
    if not token:
        return
    await db.execute(
        "UPDATE web_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
        (utcnow_iso(), hash_token(token)),
    )

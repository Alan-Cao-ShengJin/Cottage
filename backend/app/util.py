"""Time, hashing, and small shared helpers.

All timestamps in the system are RFC 3339 UTC strings with a `Z` suffix. They sort
lexicographically, which lets SQL do time comparisons on TEXT columns without a
date type — and keeps the schema portable (ADR-009).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

#: Python 3.10 has no `datetime.UTC`; keep the alias in one place.
UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return to_iso(utcnow())


def to_iso(moment: datetime) -> str:
    """RFC 3339 UTC, millisecond precision, always `Z`-suffixed."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso_in(seconds: float, *, since: datetime | None = None) -> str:
    return to_iso((since or utcnow()) + timedelta(seconds=seconds))


def seconds_until(value: str | None, *, now: datetime | None = None) -> float:
    """Seconds remaining until `value`. Negative once it has passed."""
    if not value:
        return 0.0
    return (from_iso(value) - (now or utcnow())).total_seconds()


def is_past(value: str | None, *, now: datetime | None = None) -> bool:
    """True when `value` is a timestamp that has already elapsed.

    A `None` deadline is not past — an absent expiry means "no expiry", and
    treating it as expired would silently revoke things.
    """
    if not value:
        return False
    return seconds_until(value, now=now) <= 0


def new_token(nbytes: int = 32) -> str:
    """A ≥256-bit bearer token. Shown once, stored hashed."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Hash for at-rest storage. Tokens are high-entropy random, so a plain SHA-256
    is right here: there is no low-entropy secret to protect against brute force,
    and a KDF would only add latency to every authenticated request."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(candidate_hash: str, stored_hash: str) -> bool:
    return hmac.compare_digest(candidate_hash, stored_hash)


def normalize_target(target: str) -> str:
    """Canonical form for an overlap-detection key.

    Targets arrive from different agents describing the same thing — `./src/api.py`
    and `src/api.py` are the same file. Normalizing here is what makes overlap
    detection work across independently-written clients.
    """
    cleaned = target.strip().replace("\\", "/").lower()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def normalize_title(title: str) -> str:
    """Lowercased, punctuation-light form used for duplicate-task comparison."""
    kept = [ch if ch.isalnum() or ch.isspace() else " " for ch in title.lower()]
    return " ".join("".join(kept).split())

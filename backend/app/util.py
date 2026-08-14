"""Small shared helpers: time, ids, join codes."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

# Ambiguity-free alphabet: no O/0, I/1, or similar look-alikes.
JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
JOIN_CODE_LENGTH = 6


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_token() -> str:
    return secrets.token_urlsafe(24)


def new_join_code() -> str:
    return "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LENGTH))


def normalize_join_code(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")

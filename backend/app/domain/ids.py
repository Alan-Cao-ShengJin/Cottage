"""Prefixed, lexicographically sortable identifiers.

Sortable-by-creation ids make the event log and any id-ordered listing debuggable
without a join on timestamps. The prefix makes a stray id in a log line
self-describing, and makes "you passed a task id where a room id goes" a cheap
check rather than a mystery.
"""

from __future__ import annotations

import os
import time
from typing import Final

# Crockford base32: no I, L, O, U -> no ambiguity when a human retypes an id.
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_CHARS: Final = 10  # 50 bits of ms timestamp: good past year 36000
_RANDOM_CHARS: Final = 12  # 60 bits of entropy

ORG: Final = "org"
USER: Final = "usr"
IDENTITY: Final = "aid"
ROOM: Final = "room"
INVITATION: Final = "inv"
PARTICIPANT: Final = "par"
CONNECTION: Final = "con"
ATTACHMENT: Final = "att"
EVENT: Final = "evt"
MESSAGE: Final = "msg"
WORK: Final = "wrk"
TASK: Final = "tsk"
PROPOSAL: Final = "prp"
CLAIM: Final = "clm"
ARTIFACT: Final = "art"
CONFLICT: Final = "cft"
DIRECTIVE: Final = "dir"
CREDENTIAL: Final = "cred"
COMMAND: Final = "cmd"


def _encode(value: int, chars: int) -> str:
    out = [""] * chars
    for i in range(chars - 1, -1, -1):
        out[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_id(prefix: str) -> str:
    """`<prefix>_<time><random>`. Monotonic enough to sort by creation."""
    stamp = _encode(int(time.time() * 1000), _TIME_CHARS)
    rand = _encode(int.from_bytes(os.urandom(8), "big"), _RANDOM_CHARS)
    return f"{prefix}_{stamp}{rand}"


def has_prefix(value: str, prefix: str) -> bool:
    return value.startswith(f"{prefix}_")

"""Async SQLite access with a real transaction boundary.

Two things this module exists to provide, both required by the architecture:

1. **A multi-statement transaction.** The event log is only a system of record if a
   mutation and its event append commit together (D-003). The previous
   connection-per-query helper could not express that, so `transaction()` is the
   primary API and the single-shot helpers are conveniences built on it.

2. **Engine neutrality (ADR-009).** Nothing here exposes SQLite locking semantics
   to callers. `Tx.execute` returns the affected row count, which is how every
   invariant in `core/` is enforced — a conditional `UPDATE ... WHERE <expected
   state>` that affects 0 rows means "the precondition no longer holds". That
   pattern, plus UNIQUE constraints, is all the concurrency control the domain
   uses, and it behaves the same on PostgreSQL.

`BEGIN IMMEDIATE` is used for write transactions because it is how *this* engine
avoids a mid-transaction upgrade failure. It is an implementation detail of the
adapter, not a semantic the domain relies on: correctness comes from the
conditional writes, and on a swap to PostgreSQL this becomes a plain `BEGIN`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import settings
from ..util import utcnow_iso

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2

_db_path: Path = settings.database_path

#: How long to wait for a competing writer before surfacing "database is locked".
#: Generous, because a lock wait here is normal contention, not an error.
BUSY_TIMEOUT_SECONDS = 10.0


def set_database_path(path: Path | str) -> None:
    """Point the process at a different database file (used by tests)."""
    global _db_path
    _db_path = Path(path)


def get_database_path() -> Path:
    return _db_path


class Tx:
    """A transaction handle. Every method runs inside the open transaction."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write; return the number of affected rows.

        The return value is load-bearing. A conditional update that affects 0 rows
        is how `core/` learns that a precondition (task still open, fence still
        current, invitation not yet exhausted) stopped holding.
        """
        cur = await self._conn.execute(sql, tuple(params))
        try:
            return int(cur.rowcount)
        finally:
            await cur.close()

    async def insert(self, sql: str, params: Iterable[Any] = ()) -> None:
        await self.execute(sql, params)

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def fetch_value(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = await self.fetch_one(sql, params)
        return None if row is None else row[0]


@asynccontextmanager
async def _connection() -> AsyncIterator[aiosqlite.Connection]:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(_db_path, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = aiosqlite.Row
    try:
        # Manage transactions explicitly; the driver's implicit BEGIN would open a
        # deferred transaction and defeat the point of `transaction(write=True)`.
        conn.isolation_level = None
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
        yield conn
    finally:
        await conn.close()


@asynccontextmanager
async def transaction(*, write: bool = True) -> AsyncIterator[Tx]:
    """Open a transaction. Commits on clean exit, rolls back on any exception.

    A rollback takes the state mutation *and* its event append with it, which is
    the atomicity guarantee the event log depends on.
    """
    async with _connection() as conn:
        await conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        tx = Tx(conn)
        try:
            yield tx
        except BaseException:
            await conn.execute("ROLLBACK")
            raise
        else:
            await conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Single-statement conveniences (reads, and writes with no invariant to hold)
# ---------------------------------------------------------------------------


async def fetch_one(sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
    async with transaction(write=False) as tx:
        return await tx.fetch_one(sql, params)


async def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    async with transaction(write=False) as tx:
        return await tx.fetch_all(sql, params)


async def fetch_value(sql: str, params: Iterable[Any] = ()) -> Any:
    async with transaction(write=False) as tx:
        return await tx.fetch_value(sql, params)


async def execute(sql: str, params: Iterable[Any] = ()) -> int:
    async with transaction() as tx:
        return await tx.execute(sql, params)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Apply the schema. Safe on every boot."""
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with _connection() as conn:
        await conn.executescript(sql)
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow_iso()),
        )
        await conn.commit()
    log.info("database ready at %s (schema v%d)", _db_path, SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# JSON column helpers
# ---------------------------------------------------------------------------


def dumps(value: Any) -> str:
    """Serialize a JSON column. Compact and key-sorted so rows diff cleanly."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def loads(raw: str | bytes | None, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        log.warning("malformed JSON column; falling back to default")
        return default


def str_list(raw: str | bytes | None) -> list[str]:
    value = loads(raw, [])
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [str(v) for v in value]
    return []

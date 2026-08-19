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

3. **Connection reuse.** Connections are pooled, because aiosqlite gives every
   connection its own worker thread: opening one per transaction charged a thread
   spawn, a file open, PRAGMA setup and a teardown to every single-row read. The pool
   hands a connection out *exclusively* for the life of a transaction and takes it back
   afterwards, so nothing about the transaction boundary changes — two concurrent
   transactions never touch the same connection, exactly as before.

`BEGIN IMMEDIATE` is used for write transactions because it is how *this* engine
avoids a mid-transaction upgrade failure. It is an implementation detail of the
adapter, not a semantic the domain relies on: correctness comes from the
conditional writes, and on a swap to PostgreSQL this becomes a plain `BEGIN`.
"""

from __future__ import annotations

import asyncio
import contextlib
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
SCHEMA_VERSION = 4

_db_path: Path = settings.database_path

#: How long to wait for a competing writer before surfacing "database is locked".
#: Generous, because a lock wait here is normal contention, not an error.
BUSY_TIMEOUT_SECONDS = 10.0

#: How many connections may be handed out at once. Bounded on purpose: each aiosqlite
#: connection is a worker thread, so an unbounded pool would reintroduce, under load,
#: the thread storm the pool exists to remove. A transaction that finds every slot taken
#: waits for one rather than opening a ninth connection — which is also a fairer queue
#: than SQLite's own lock wait, because it queues before `BEGIN IMMEDIATE` rather than
#: inside it.
POOL_SIZE = 8


def set_database_path(path: Path | str) -> None:
    """Point the process at a different database file (used by tests).

    Synchronous, so it cannot close the pool itself; `_get_pool` notices the path
    changed and builds a new one. To close the old connections at a known moment rather
    than on the next transaction, `await shutdown()` first.
    """
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


async def _open_connection(path: Path) -> aiosqlite.Connection:
    """Open one connection, configured the way every connection here must be."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(
        path,
        timeout=BUSY_TIMEOUT_SECONDS,
        # Manage transactions explicitly; the driver's implicit BEGIN would open a
        # deferred transaction and defeat the point of `transaction(write=True)`.
        #
        # Passed to `connect` rather than assigned afterwards, and that is not a style
        # choice. aiosqlite drives the sqlite3 connection from its own worker thread, but
        # the `isolation_level` *property setter* runs on the calling thread — and from
        # Python 3.12 that setter enforces sqlite3's same-thread check, so assigning it
        # here raises ProgrammingError. Passing it as a connect kwarg means sqlite3 applies
        # it while constructing the connection, inside the worker thread, on every version.
        isolation_level=None,
    )
    conn.row_factory = aiosqlite.Row
    # Per-connection, not per-database: both reset with the connection, so a pooled
    # connection that skipped them would enforce no foreign keys and never wait on a
    # busy lock. Set here, in the one place a connection is born, so there is no second
    # path that can forget.
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
    return conn


@asynccontextmanager
async def _connection() -> AsyncIterator[aiosqlite.Connection]:
    """A private, unpooled connection. Used for schema work at boot only."""
    conn = await _open_connection(_db_path)
    try:
        yield conn
    finally:
        await conn.close()


class _Pool:
    """A bounded set of reusable connections to one database file.

    Exclusivity is the whole contract: `acquire` removes a connection from the pool and
    `release` puts it back, so a connection is never shared by two transactions in
    flight. That is what lets `transaction()` keep issuing raw `BEGIN`/`COMMIT` on it.

    Bound to the event loop it was first used on, because the semaphore is. A different
    loop (a test's, typically) gets a different pool rather than a cross-loop await.
    """

    def __init__(self, path: Path, loop: asyncio.AbstractEventLoop, size: int) -> None:
        self.path = path
        self.loop = loop
        self._idle: list[aiosqlite.Connection] = []
        self._slots = asyncio.Semaphore(size)
        self._closed = False

    async def acquire(self) -> aiosqlite.Connection:
        await self._slots.acquire()
        try:
            if self._idle:
                return self._idle.pop()
            return await _open_connection(self.path)
        except BaseException:
            self._slots.release()
            raise

    async def release(self, conn: aiosqlite.Connection, *, discard: bool = False) -> None:
        """Return a connection, or close it if its transaction state is unknown.

        `discard` is not a nicety. A connection whose COMMIT or ROLLBACK failed may still
        be inside a transaction; handing it to the next caller would make their `BEGIN`
        fail, or worse, silently enrol their work in someone else's transaction.
        """
        try:
            if discard or self._closed:
                with contextlib.suppress(Exception):
                    await conn.close()
            else:
                self._idle.append(conn)
        finally:
            self._slots.release()

    async def close(self) -> None:
        self._closed = True
        idle, self._idle = self._idle, []
        for conn in idle:
            with contextlib.suppress(Exception):
                await conn.close()


_pool: _Pool | None = None


async def _get_pool() -> _Pool:
    """The pool for the current database path and loop, created on first use.

    Replaced rather than mutated when either changes, so a test that repoints the path
    cannot be served a connection to the previous file.
    """
    global _pool
    loop = asyncio.get_running_loop()
    pool = _pool
    if pool is None or pool.path != _db_path or pool.loop is not loop:
        # Publish the replacement before awaiting anything, so a concurrent caller sees
        # the new pool instead of racing to build a second one.
        stale, pool = pool, _Pool(_db_path, loop, POOL_SIZE)
        _pool = pool
        if stale is not None:
            await stale.close()
    return pool


async def shutdown() -> None:
    """Close every pooled connection. Idempotent; call on app teardown."""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        await pool.close()


@asynccontextmanager
async def transaction(*, write: bool = True) -> AsyncIterator[Tx]:
    """Open a transaction. Commits on clean exit, rolls back on any exception.

    A rollback takes the state mutation *and* its event append with it, which is
    the atomicity guarantee the event log depends on.
    """
    pool = await _get_pool()
    conn = await pool.acquire()
    healthy = True
    try:
        try:
            await conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        except BaseException:
            healthy = False
            raise
        tx = Tx(conn)
        try:
            yield tx
        except BaseException:
            try:
                await conn.execute("ROLLBACK")
            except BaseException:
                # The original exception is what the caller needs to see; this one only
                # tells us the connection is no longer safe to reuse.
                healthy = False
            raise
        else:
            try:
                await conn.execute("COMMIT")
            except BaseException:
                healthy = False
                raise
    finally:
        await pool.release(conn, discard=not healthy)


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


#: Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` is a no-op on
#: an existing table, so a new column would silently never appear on any database created
#: by an earlier version — and the failure surfaces later as a confusing OperationalError.
#: Additive columns are the only migration this project needs so far; anything that
#: requires rewriting data will need a real numbered-migration mechanism (see
#: docs/ROADMAP.md, Postgres blocker).
ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # D-080. Existing rooms had no long-form cold-start context.
    ("rooms", "charter", "TEXT NOT NULL DEFAULT ''"),
    ("principal_tokens", "client_id", "TEXT"),
    ("principal_tokens", "scope", "TEXT NOT NULL DEFAULT ''"),
    ("principal_tokens", "audience", "TEXT"),
    ("principal_tokens", "last_used_at", "TEXT"),
    # Defaults to 'account', which is right for every row that predates it: before
    # invitations became credentials (D-025) an identity could only be created by, or
    # bound by, someone holding an account. The exception is the permissive local
    # development path, whose self-named identities are retroactively labelled
    # 'account' — throwaway data on a laptop, and the guard refuses that path in public.
    ("agent_identities", "provenance", "TEXT NOT NULL DEFAULT 'account'"),
    # D-032. NULL on every pre-existing row, which is correct: a connection opened
    # before attachments existed belongs to no durable runtime, and treating it as
    # ephemeral is the honest reading rather than inventing continuity for it.
    ("connections", "attachment_id", "TEXT"),
    # D-034. Exactly one of these is set while a lease is held, or neither. They are
    # cleared wherever the claim is cleared, so an executor never outlives its lease.
    ("tasks", "executor_attachment_id", "TEXT"),
    ("tasks", "executor_connection_id", "TEXT"),
    # D-045. 'running' on every pre-existing row, which is the correct reading: no
    # human has steered work that predates the ability to steer it.
    ("tasks", "steering", "TEXT NOT NULL DEFAULT 'running'"),
    ("tasks", "steering_reason", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "steering_by_participant_id", "TEXT"),
    ("tasks", "steering_at", "TEXT"),
    # D-054. Every pre-existing attachment is 'unspecified' with no executor named,
    # which is the correct reading: a runtime that never declared what it was for
    # should not be described as anything, and guessing from `host_class` would be
    # the vendor-label error in a new costume.
    ("attachments", "runtime_role", "TEXT NOT NULL DEFAULT 'unspecified'"),
    ("attachments", "executor_kind", "TEXT NOT NULL DEFAULT ''"),
    ("attachments", "executor_model", "TEXT NOT NULL DEFAULT ''"),
    # D-055. NULL on every question asked before runtimes were recorded, which the
    # comparison treats as "unidentifiable" and therefore permits — an unknown runtime
    # is not evidence of self-answering.
    ("questions", "asked_by_attachment_id", "TEXT"),
    ("questions", "answered_by_attachment_id", "TEXT"),
    # D-059. NULL on every declaration written before the two clocks were separated,
    # and readers coalesce it to `heartbeat_at`. That is the honest reading rather than
    # a convenience: back then `heartbeat_at` was refreshed *only* by declare/update,
    # which is exactly the progress evidence this column now carries.
    ("work_declarations", "progress_at", "TEXT"),
    # D-062. Epoch 1 and never drained on every pre-existing attachment, which is the
    # correct reading: a runtime nobody has stopped is on its first run. Note the CHECK
    # in schema.sql is absent on databases migrated this way — SQLite cannot add one
    # with ALTER TABLE — so the invariant is enforced by the only writer that bumps it.
    ("attachments", "epoch", "INTEGER NOT NULL DEFAULT 1"),
    ("attachments", "drained_at", "TEXT"),
    ("attachments", "drained_reason", "TEXT NOT NULL DEFAULT ''"),
    ("attachments", "operational_state", "TEXT NOT NULL DEFAULT 'monitoring'"),
    ("attachments", "operational_summary", "TEXT NOT NULL DEFAULT ''"),
    ("attachments", "waiting_reason", "TEXT NOT NULL DEFAULT ''"),
    ("attachments", "operational_task_id", "TEXT"),
    ("attachments", "operational_work_id", "TEXT"),
    ("attachments", "operational_updated_at", "TEXT"),
    # D-090. `agent` on every message written before a person could be relayed through an
    # agent, which is the honest reading: back then the room had no way to say otherwise,
    # and the wake rule treated all of them as the participant speaking for itself.
    ("messages", "speaking_for", "TEXT NOT NULL DEFAULT 'agent'"),
)


async def _existing_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


async def _apply_additive_columns(conn: aiosqlite.Connection) -> None:
    tables: dict[str, set[str]] = {}
    for table, column, ddl in ADDITIVE_COLUMNS:
        if table not in tables:
            tables[table] = await _existing_columns(conn, table)
        if not tables[table]:
            continue  # table does not exist yet; the schema script just created it
        if column in tables[table]:
            continue
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        tables[table].add(column)
        log.info("migrated: added %s.%s", table, column)


async def init_db() -> None:
    """Apply the schema, then any additive column migrations. Safe on every boot."""
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with _connection() as conn:
        await conn.executescript(sql)
        await _apply_additive_columns(conn)
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

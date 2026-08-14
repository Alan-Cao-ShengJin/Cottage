"""Tiny async SQLite layer.

Deliberately not an ORM: V0 has ~5 tables and every query is a one-liner. The
schema is applied idempotently at startup, which gives deterministic
initialization without a migration framework. When the schema needs to evolve
beyond V0, add numbered files next to schema.sql and bump `SCHEMA_VERSION`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import settings
from ..util import utcnow_iso

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1

_db_path: Path = settings.database_path


def set_database_path(path: Path | str) -> None:
    """Point the process at a different SQLite file (used by tests)."""
    global _db_path
    _db_path = Path(path)


def get_database_path() -> Path:
    return _db_path


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    """Open a connection with sane defaults. SQLite handles a V0 workload fine."""
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(_db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        await conn.close()


async def init_db() -> None:
    """Apply the schema. Safe to run on every boot."""
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with connect() as conn:
        await conn.executescript(sql)
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow_iso()),
        )
        await conn.commit()
    log.info("database ready at %s (schema v%d)", _db_path, SCHEMA_VERSION)


async def fetch_one(sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
    async with connect() as conn:
        async with conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()


async def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    async with connect() as conn:
        async with conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())


async def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write and return lastrowid."""
    async with connect() as conn:
        cur = await conn.execute(sql, tuple(params))
        await conn.commit()
        return int(cur.lastrowid or 0)


async def execute_many(statements: list[tuple[str, Iterable[Any]]]) -> None:
    """Run several writes in one transaction."""
    async with connect() as conn:
        for sql, params in statements:
            await conn.execute(sql, tuple(params))
        await conn.commit()

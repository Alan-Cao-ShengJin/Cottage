#!/usr/bin/env python
"""Measure the cost of the database access layer under concurrency.

Exists because "the tests pass" is not evidence for a connection-management change.
`transaction()` is the only way into SQLite from `core/`, so the number that matters
is how many transactions per second it sustains with many callers in flight — the
shape a room full of participants actually produces.

Deliberately uses only the public surface of `app.db.database` (`set_database_path`,
`init_db`, `transaction`, `fetch_one`, `execute`), so the *same script* runs against
the connection-per-transaction version and any pooled successor. Nothing here reaches
into internals, which is what makes the before/after numbers comparable.

The write transaction is two statements, not one: a row plus an append to a log table.
That is the real shape of every mutation in this codebase (D-003 — the state change and
its event commit together), and a single-statement write would flatter the pooled
version by understating the work done while the connection is held.

`--baseline` measures a *previous* version of the module in the same process shape, so
the before/after numbers come from one script on one machine rather than from two runs
separated by an edit:

    git show <rev>:backend/app/db/database.py > old_database.py
    ... --baseline old_database.py --label before

Usage:
    backend\\.venv\\Scripts\\python.exe scripts/bench_db.py
    backend\\.venv\\Scripts\\python.exe scripts/bench_db.py --label after --rounds 5
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import statistics
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

#: Bound by `_load_db()` before any phase runs, so `--baseline` can substitute an older
#: copy of the module. Everything below goes through this name and nothing else.
db: ModuleType


def _load_db(baseline: Path | None) -> ModuleType:
    """Import `app.db.database`, or a standalone copy of it standing in that slot.

    The copy is registered under the real module name so its `from ..config import
    settings` resolves against the real package — the alternative, editing the file
    under test, makes the two measurements non-reproducible.
    """
    if baseline is None:
        return importlib.import_module("app.db.database")

    importlib.import_module("app.db")  # parent package, for the relative imports
    spec = importlib.util.spec_from_file_location("app.db.database", baseline)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load baseline module from {baseline}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.db.database"] = module
    spec.loader.exec_module(module)
    # The copy lives outside the package, so its `Path(__file__).with_name("schema.sql")`
    # points at a file that is not there. Repoint it at the real schema: the benchmark is
    # measuring connection management, and both versions must apply the same schema.
    module.SCHEMA_PATH = REPO_ROOT / "backend" / "app" / "db" / "schema.sql"
    return module

BENCH_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS bench_rows (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        room  TEXT NOT NULL,
        body  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bench_events (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        room  TEXT NOT NULL,
        kind  TEXT NOT NULL
    )
    """,
)


async def _one_read(index: int) -> None:
    await db.fetch_one(
        "SELECT id, body FROM bench_rows WHERE room = ? ORDER BY id LIMIT 1",
        (f"room-{index % 8}",),
    )


async def _one_write(index: int) -> None:
    # A mutation and its event append in one transaction: the atomicity guarantee the
    # event log depends on, and the reason `transaction()` exists at all.
    async with db.transaction() as tx:
        await tx.execute(
            "INSERT INTO bench_rows (room, body) VALUES (?, ?)",
            (f"room-{index % 8}", f"body-{index}"),
        )
        await tx.execute(
            "INSERT INTO bench_events (room, kind) VALUES (?, ?)",
            (f"room-{index % 8}", "bench.written"),
        )


async def _run_phase(
    op: Callable[[int], Awaitable[None]], *, count: int, concurrency: int
) -> tuple[float, list[float]]:
    """Run `count` operations with at most `concurrency` in flight.

    Returns wall-clock seconds for the whole phase and every individual latency, because
    throughput alone hides the case where the mean improves while the tail collapses.
    """
    gate = asyncio.Semaphore(concurrency)
    latencies: list[float] = []

    async def one(index: int) -> None:
        async with gate:
            started = time.perf_counter()
            await op(index)
            latencies.append(time.perf_counter() - started)

    started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(count)))
    return time.perf_counter() - started, latencies


def _summarise(name: str, elapsed: float, latencies: list[float]) -> dict[str, float]:
    ordered = sorted(latencies)
    return {
        "ops": len(latencies),
        "seconds": elapsed,
        "ops_per_sec": len(latencies) / elapsed if elapsed else 0.0,
        "p50_ms": ordered[len(ordered) // 2] * 1000,
        "p95_ms": ordered[int(len(ordered) * 0.95)] * 1000,
        "max_ms": ordered[-1] * 1000,
    }


async def _round(*, reads: int, writes: int, concurrency: int) -> dict[str, dict[str, float]]:
    with tempfile.TemporaryDirectory() as tmp:
        original = db.get_database_path()
        db.set_database_path(Path(tmp) / "bench.db")
        try:
            await db.init_db()
            for ddl in BENCH_SCHEMA:
                await db.execute(ddl)
            # Seed, so reads hit rows rather than an empty table.
            for i in range(64):
                await _one_write(i)

            read_elapsed, read_lat = await _run_phase(
                _one_read, count=reads, concurrency=concurrency
            )
            write_elapsed, write_lat = await _run_phase(
                _one_write, count=writes, concurrency=concurrency
            )
        finally:
            shutdown = getattr(db, "shutdown", None)
            if shutdown is not None:  # present only once a pool exists
                await shutdown()
            db.set_database_path(original)

    return {
        "reads": _summarise("reads", read_elapsed, read_lat),
        "writes": _summarise("writes", write_elapsed, write_lat),
    }


def _median_of(rounds: list[dict[str, dict[str, float]]], phase: str) -> dict[str, float]:
    keys = rounds[0][phase].keys()
    return {k: statistics.median(r[phase][k] for r in rounds) for k in keys}


async def main_async(args: argparse.Namespace) -> int:
    global db
    db = _load_db(Path(args.baseline) if args.baseline else None)
    print(
        f"bench_db · label={args.label} · reads={args.reads} writes={args.writes} "
        f"concurrency={args.concurrency} rounds={args.rounds}"
    )
    print(f"module: {db.__file__}\n")
    results = []
    for n in range(args.rounds):
        result = await _round(reads=args.reads, writes=args.writes, concurrency=args.concurrency)
        results.append(result)
        print(
            f"  round {n + 1}: "
            f"reads {result['reads']['ops_per_sec']:8.1f}/s  "
            f"writes {result['writes']['ops_per_sec']:8.1f}/s"
        )

    print(f"\n{'':10} {'ops/s':>10} {'p50 ms':>9} {'p95 ms':>9} {'max ms':>9}")
    for phase in ("reads", "writes"):
        m = _median_of(results, phase)
        print(
            f"  {phase:8} {m['ops_per_sec']:10.1f} {m['p50_ms']:9.2f} "
            f"{m['p95_ms']:9.2f} {m['max_ms']:9.2f}"
        )
    print("\n(medians across rounds)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reads", type=int, default=2000)
    parser.add_argument("--writes", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--label", default="after", help="free text, echoed in the header")
    parser.add_argument(
        "--baseline",
        help="path to an older copy of database.py to measure instead of the current one",
    )
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

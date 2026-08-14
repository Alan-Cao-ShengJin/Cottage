"""The single write path: idempotency, transaction, then publish.

Every mutating operation in `core/` runs through `execute_command`, which gives
three guarantees uniformly instead of per-service:

1. **A duplicate `command_id` never executes twice.** The receipt id is inserted
   *before* the body runs, so the UNIQUE primary key — not a check-then-act race —
   is the arbiter. A concurrent duplicate loses the insert, rolls back whatever it
   staged, and returns the winner's stored result. This matters most for the
   long-poll clients that cannot distinguish a timeout from a failure and will
   retry (`docs/PROTOCOL.md` §2).

2. **State and events commit together.** The body receives one `Tx`; if anything
   raises, the mutation and its event append roll back as a unit (D-003).

3. **The bus is notified only after commit.** Publishing inside the transaction
   would wake a consumer that could then read state which still has a chance to
   roll back (ADR-008).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import aiosqlite

from ..db import database as db
from ..domain.events import EventEnvelope
from ..util import utcnow_iso
from .bus import bus

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class CommandOutcome:
    """What a command body produced."""

    result: dict[str, Any] = field(default_factory=dict)
    events: list[EventEnvelope] = field(default_factory=list)
    #: True when this was a replay of an already-executed command_id.
    replayed: bool = False

    @property
    def seq(self) -> int | None:
        return self.events[-1].seq if self.events else None


CommandBody = Callable[[db.Tx], Awaitable[CommandOutcome]]


async def _load_receipt(command_id: str) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT seq, result FROM command_receipts WHERE command_id = ?", (command_id,)
    )
    if row is None:
        return None
    return {"seq": row["seq"], "result": db.loads(row["result"], {})}


async def execute_command(
    *,
    command_id: str | None,
    command_type: str,
    room_id: str,
    participant_id: str | None,
    body: CommandBody,
) -> CommandOutcome:
    """Run one mutating command. See the module docstring for the guarantees."""
    if command_id:
        existing = await _load_receipt(command_id)
        if existing is not None:
            log.debug("replaying command %s (%s)", command_id, command_type)
            return CommandOutcome(result=existing["result"], replayed=True)

    try:
        async with db.transaction() as tx:
            if command_id:
                # Reserve the id first: the UNIQUE PK, not the read above, is what
                # actually excludes a concurrent duplicate.
                await tx.execute(
                    """
                    INSERT INTO command_receipts
                        (command_id, room_id, participant_id, command_type, seq, result, created_at)
                    VALUES (?,?,?,?,NULL,'{}',?)
                    """,
                    (command_id, room_id, participant_id, command_type, utcnow_iso()),
                )

            outcome = await body(tx)

            if command_id:
                await tx.execute(
                    "UPDATE command_receipts SET seq = ?, result = ? WHERE command_id = ?",
                    (outcome.seq, db.dumps(outcome.result), command_id),
                )
    except aiosqlite.IntegrityError:
        # Lost the reservation race. The transaction rolled back, so nothing this
        # attempt staged survives; hand back what the winner produced.
        if command_id:
            existing = await _load_receipt(command_id)
            if existing is not None:
                return CommandOutcome(result=existing["result"], replayed=True)
        raise

    # Committed. Notify consumers, which then re-read the log from their cursors.
    for event in outcome.events:
        await bus.publish(event.room_id, event.seq)

    return outcome


async def publish_committed(events: list[EventEnvelope]) -> None:
    """Notify the bus for events committed outside `execute_command`.

    Used by the reaper, which batches unrelated rooms in one pass and therefore does
    not fit the one-command-one-room shape.
    """
    for event in events:
        await bus.publish(event.room_id, event.seq)

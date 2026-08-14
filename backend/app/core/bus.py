"""Notify-then-read fanout (ADR-008).

The bus carries **only** `(room_id, seq)`. It never carries event content. A
consumer that is told "room X reached seq 91" goes and reads the log from its own
cursor. Two consequences, both deliberate:

* **A dropped notification cannot lose data.** The worst case is latency until the
  next notification or the consumer's own timeout, because the cursor still says
  what it has not read. A content-carrying bus with a bounded queue would silently
  drop events under load, and a coordination product cannot tolerate that.
* **Slow consumers degrade alone.** No per-subscriber buffering means no
  head-of-line blocking and no unbounded memory in the publisher.

In-process for M1: one backend owns fanout. This module is the seam where a broker
(Redis/NATS) slots in — since a broker only has to deliver a hint, at-least-once
delivery of a `(room_id, seq)` pair is sufficient, and duplicates are harmless.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class RoomBus:
    """Per-room high-water marks plus waiters.

    Uses one `asyncio.Condition` per room rather than per-subscriber queues,
    because every waiter wants the same fact ("has the room moved past N?") and
    re-reads the log itself.
    """

    def __init__(self) -> None:
        self._high_water: dict[str, int] = {}
        self._conditions: dict[str, asyncio.Condition] = {}
        self._waiters: dict[str, int] = {}

    def _condition(self, room_id: str) -> asyncio.Condition:
        cond = self._conditions.get(room_id)
        if cond is None:
            cond = asyncio.Condition()
            self._conditions[room_id] = cond
        return cond

    async def publish(self, room_id: str, seq: int) -> None:
        """Announce that `room_id` has committed up to `seq`.

        Call this **after** the transaction commits. Publishing inside the
        transaction would wake a consumer that could then read state which still
        has a chance to roll back.
        """
        cond = self._condition(room_id)
        async with cond:
            if seq > self._high_water.get(room_id, 0):
                self._high_water[room_id] = seq
            cond.notify_all()

    def high_water(self, room_id: str) -> int:
        return self._high_water.get(room_id, 0)

    def prime(self, room_id: str, seq: int) -> None:
        """Seed the high-water mark from persisted state.

        Needed after a restart, and when a consumer attaches to a room this process
        has not published for yet: without it the first waiter would block even
        though the log is already ahead of its cursor.
        """
        if seq > self._high_water.get(room_id, 0):
            self._high_water[room_id] = seq

    async def wait_for(self, room_id: str, since_seq: int, timeout: float) -> int:
        """Block until the room's high-water mark exceeds `since_seq`.

        Returns the current mark, which may still equal `since_seq` on timeout —
        the caller treats that as "nothing new", not as an error. This is the whole
        implementation of the MCP long-poll (`docs/PROTOCOL.md` §5).
        """
        cond = self._condition(room_id)
        try:
            async with cond:
                self._waiters[room_id] = self._waiters.get(room_id, 0) + 1
                try:
                    await asyncio.wait_for(
                        cond.wait_for(lambda: self._high_water.get(room_id, 0) > since_seq),
                        timeout=timeout,
                    )
                finally:
                    remaining = self._waiters.get(room_id, 1) - 1
                    if remaining > 0:
                        self._waiters[room_id] = remaining
                    else:
                        self._waiters.pop(room_id, None)
        except asyncio.TimeoutError:
            pass
        return self._high_water.get(room_id, 0)

    def waiter_count(self, room_id: str) -> int:
        """Diagnostics: how many consumers are blocked on this room."""
        return self._waiters.get(room_id, 0)

    def forget(self, room_id: str) -> None:
        """Drop state for a purged room."""
        self._high_water.pop(room_id, None)
        self._conditions.pop(room_id, None)
        self._waiters.pop(room_id, None)

    def clear(self) -> None:
        """Reset everything. Test helper — the bus is a module-level singleton, so
        it is cleared in place rather than replaced, or modules that imported it by
        name would keep the old object."""
        self._high_water.clear()
        self._conditions.clear()
        self._waiters.clear()


bus = RoomBus()

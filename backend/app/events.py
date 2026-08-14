"""In-process pub/sub for room events.

Powers three consumers:
  1. the browser SSE stream,
  2. the server-side autonomous agent runner (push),
  3. the MCP `wait_for_room_activity` long-poll that Claude Code uses (pull).

In-process is the right call for V0: one backend process owns all room state.
The publish/subscribe seam is where a cross-process broker would slot in later.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

log = logging.getLogger(__name__)

QUEUE_MAXSIZE = 200


@dataclass
class RoomEvent:
    room_id: str
    type: str  # message | agent_joined | agent_left | memory_updated | task_updated | room_expired | ...
    payload: dict[str, Any] = field(default_factory=dict)

    def as_sse(self) -> dict[str, Any]:
        return {"type": self.type, "room_id": self.room_id, **self.payload}


Listener = Callable[[RoomEvent], Awaitable[None]]


class EventHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[RoomEvent]]] = {}
        # Monotonic per-room counter + condition, so a poller can block until
        # something actually changed rather than spinning.
        self._revision: dict[str, int] = {}
        self._conditions: dict[str, asyncio.Condition] = {}
        self._listeners: list[Listener] = []

    # -- server-side listeners (agent runner) ---------------------------
    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    # -- publish --------------------------------------------------------
    async def publish(self, event: RoomEvent) -> None:
        for queue in list(self._subscribers.get(event.room_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - slow browser tab
                log.warning("dropping SSE event for room %s: subscriber queue full", event.room_id)

        cond = self._condition(event.room_id)
        async with cond:
            self._revision[event.room_id] = self._revision.get(event.room_id, 0) + 1
            cond.notify_all()

        for listener in self._listeners:
            # Listeners must never block or kill the publisher.
            asyncio.create_task(self._safe_notify(listener, event))

    @staticmethod
    async def _safe_notify(listener: Listener, event: RoomEvent) -> None:
        try:
            await listener(event)
        except Exception:  # pragma: no cover - defensive
            log.exception("event listener failed for %s", event.type)

    # -- SSE subscription ------------------------------------------------
    @asynccontextmanager
    async def subscription(self, room_id: str) -> AsyncIterator[asyncio.Queue[RoomEvent]]:
        """Yield a queue of room events. A plain queue (rather than an async
        generator) keeps the SSE heartbeat simple: `wait_for(queue.get())` is
        safe to cancel on every timeout tick."""
        queue: asyncio.Queue[RoomEvent] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.setdefault(room_id, set()).add(queue)
        try:
            yield queue
        finally:
            subs = self._subscribers.get(room_id)
            if subs:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(room_id, None)

    # -- revision polling (MCP long-poll) --------------------------------
    def _condition(self, room_id: str) -> asyncio.Condition:
        cond = self._conditions.get(room_id)
        if cond is None:
            cond = asyncio.Condition()
            self._conditions[room_id] = cond
        return cond

    def revision(self, room_id: str) -> int:
        return self._revision.get(room_id, 0)

    async def wait_for_change(self, room_id: str, since_revision: int, timeout: float) -> int:
        """Block until the room's revision moves past `since_revision`.

        Returns the current revision (unchanged if the timeout elapsed).
        """
        cond = self._condition(room_id)
        try:
            async with cond:
                await asyncio.wait_for(
                    cond.wait_for(lambda: self.revision(room_id) > since_revision),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            pass
        return self.revision(room_id)

    def subscriber_count(self, room_id: str) -> int:
        return len(self._subscribers.get(room_id, ()))

    def clear(self) -> None:
        """Drop all subscribers, listeners and revisions. Test helper."""
        self._subscribers.clear()
        self._revision.clear()
        self._conditions.clear()
        self._listeners.clear()


hub = EventHub()

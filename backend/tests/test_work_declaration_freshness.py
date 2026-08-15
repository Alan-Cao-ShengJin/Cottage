"""A busy worker's card must stay fresh, and a wedged one must not (D-059).

Reported by two independent participants in one room, which is why it is a protocol
bug and not a client bug. Our own companion emitted `work.stale reason=heartbeat_lapsed`
mid-step on a task it went on to complete. The Codex participant reported the same thing
in its own words — polling kept its liveness at `live_poll` while nothing refreshed the
current-work heartbeat — and its private workaround, `update_current_work` every
105-115s, still lost the race against the 120s threshold.

The room was grading the *same silence* two contradictory ways at once. These tests pin
the fix and, just as importantly, its cost: staleness must stay reachable, or a status
nothing can ever have is a status that means nothing.
"""

from __future__ import annotations

import pytest

from app.core import checkpoints, presence, store, tasks, work
from app.db import database as db
from app.domain.commands import (
    AppendCheckpointCommand,
    ClaimTaskCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
)
from app.domain.room import RoomPolicy
from app.util import iso_in

pytestmark = pytest.mark.asyncio


async def _age(work_id: str, *, heartbeat: int | None = None, progress: int | None = None) -> None:
    """Backdate a declaration's clocks, standing in for time the test cannot wait."""
    if heartbeat is not None:
        await db.execute(
            "UPDATE work_declarations SET heartbeat_at = ? WHERE id = ?",
            (iso_in(-heartbeat), work_id),
        )
    if progress is not None:
        await db.execute(
            "UPDATE work_declarations SET progress_at = ? WHERE id = ?",
            (iso_in(-progress), work_id),
        )


async def test_a_long_step_does_not_go_stale_while_the_worker_heartbeats(make_room, join):
    """The reported failure, exactly: one step outlives `work_stale_after_seconds`.

    The worker is healthy and beating on its normal cadence; it simply has not finished
    thinking. Before the fix the sweeper read the untouched `heartbeat_at` and blocked
    the card with `heartbeat_lapsed` while the work was in flight.
    """
    room = await make_room()
    member = await join(room, display_name="Worker")
    declared = await work.declare(
        participant=member.participant,
        command=DeclareWorkCommand(headline="Thinking hard about auth", targets=["src/auth.py"]),
    )

    # Three minutes into a single step — past the 120s window, nothing declared since.
    await _age(declared.id, heartbeat=180, progress=180)
    # ...and the worker beats, as a live one does from inside a step.
    await presence.heartbeat(connection_id=member.connection_id, participant=member.participant)

    events = await work.mark_stale_declarations(await room.refresh())

    assert events == [], "a beating worker mid-step is not stale"
    assert (await store.load_work(declared.id)).status.value == "active"


async def test_the_beat_does_not_make_staleness_unreachable(make_room, join):
    """The cost of deriving freshness from the transport, stated as a test.

    A worker wedged inside a step keeps beating while nothing advances. The board is
    allowed to be wrong about it for `work_progress_stale_after_seconds` and no longer,
    which is what makes the new reason worth having.
    """
    room = await make_room(
        policy=RoomPolicy(work_stale_after_seconds=120, work_progress_stale_after_seconds=300)
    )
    member = await join(room, display_name="Wedged")
    declared = await work.declare(
        participant=member.participant,
        command=DeclareWorkCommand(headline="Stuck on a tool call"),
    )

    await _age(declared.id, progress=600)
    await presence.heartbeat(connection_id=member.connection_id, participant=member.participant)

    events = await work.mark_stale_declarations(await room.refresh())

    # A healthy transport around stalled work is its own finding, not presence loss.
    assert [e.type.value for e in events] == ["work.stale"]
    assert events[0].payload["reason"] == "no_progress"
    assert (await store.load_work(declared.id)).status.value == "blocked"


async def test_no_progress_is_emitted_once(make_room, join):
    """The constraint the fix had to preserve: one `work.stale` per declaration."""
    room = await make_room(policy=RoomPolicy(work_progress_stale_after_seconds=300))
    member = await join(room, display_name="Wedged")
    declared = await work.declare(
        participant=member.participant, command=DeclareWorkCommand(headline="Stuck")
    )
    await _age(declared.id, progress=600)

    first = await work.mark_stale_declarations(await room.refresh())
    await presence.heartbeat(connection_id=member.connection_id, participant=member.participant)
    second = await work.mark_stale_declarations(await room.refresh())

    assert len(first) == 1
    assert second == [], "the flip to blocked is what makes it non-repeating"


async def test_a_silent_transport_still_lapses(make_room, join):
    """`heartbeat_lapsed` is narrowed, not deleted.

    With nothing beating for the seat, the card is untrustworthy for the original
    reason. This is the case the old code was trying to catch and kept catching busy
    workers with instead.
    """
    room = await make_room()
    member = await join(room, display_name="Quiet")
    declared = await work.declare(
        participant=member.participant, command=DeclareWorkCommand(headline="Silent")
    )
    await _age(declared.id, heartbeat=600, progress=60)

    events = await work.mark_stale_declarations(await room.refresh())

    assert len(events) == 1
    assert events[0].payload["reason"] == "heartbeat_lapsed"


async def test_owner_presence_lost_is_untouched(make_room, join):
    """A declaration whose owner is genuinely gone must still go stale.

    Explicitly out of scope for the fix and asserted so it stays that way: presence loss
    outranks both timers, because "the owner vanished" explains the silence and
    `heartbeat_lapsed` would describe the symptom instead of the cause.
    """
    room = await make_room()
    member = await join(room, display_name="Vanished")
    declared = await work.declare(
        participant=member.participant, command=DeclareWorkCommand(headline="Abandoned mid-flight")
    )

    # The process died: no beat reaches the connection, so neither clock advances.
    await db.execute(
        "UPDATE connections SET last_heartbeat_at = ? WHERE participant_id = ?",
        (iso_in(-3600), member.participant.id),
    )
    await _age(declared.id, heartbeat=3600, progress=3600)

    events = await work.mark_stale_declarations(await room.refresh())

    assert len(events) == 1
    assert events[0].payload["reason"] == "owner_presence_lost"


async def test_a_checkpoint_refreshes_the_progress_clock(make_room, join):
    """Progress is evidence of progress in a way a transport beat is not.

    A worker checkpointing per step can therefore run indefinitely without its card
    rotting, which is the behaviour that makes the progress window safe to set at
    something longer than a single step.
    """
    room = await make_room(policy=RoomPolicy(work_progress_stale_after_seconds=300))
    member = await join(room, display_name="Stepper")
    task = await tasks.create(participant=room.owner, command=CreateTaskCommand(title="Long job"))
    claimed = await tasks.claim(
        participant=member.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    declared = await work.declare(
        participant=member.participant,
        command=DeclareWorkCommand(headline="Working: long job", task_id=task.id),
    )

    await _age(declared.id, heartbeat=600, progress=600)
    await checkpoints.append(
        participant=member.participant,
        command=AppendCheckpointCommand(
            task_id=task.id, fence=claimed.claim.fence, summary="finished step 3"
        ),
    )

    refreshed = await store.load_work(declared.id)
    assert refreshed.progress_at > iso_in(-60), "the checkpoint moved the progress clock"
    assert refreshed.heartbeat_at > iso_in(-60), "and vouches for the runtime too"
    assert await work.mark_stale_declarations(await room.refresh()) == []

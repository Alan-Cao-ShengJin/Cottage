"""Hydration: what a control surface gets when it arrives cold.

The requirement (D-030, reordered to first in D-033) is that a human can open another
authorized control surface and continue without asking every agent to recap. The
constraint (D-031, conceded) is that this delivers **operational state and not
conversation** — so these tests assert both halves, including the negative one, because
the failure mode nobody would notice is hydration quietly being treated as continuity.
"""

from __future__ import annotations

import pytest

from app.core import messages as message_service
from app.core import projections, tasks
from app.core import work as work_service
from app.domain.commands import (
    ClaimTaskCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    PostMessageCommand,
)
from app.domain.disclosure import Audience, Disclosure
from app.domain.room import RoomPolicy


@pytest.fixture()
async def room_with_two(fresh_db, org, make_room, join):
    fixture = await make_room(name="Hydration", policy=RoomPolicy(allow_attended_claims=True))
    worker = await join(fixture, display_name="Worker", transport="sse")
    peer = await join(fixture, display_name="Peer", transport="sse")
    return fixture, worker, peer


async def test_a_cold_surface_gets_its_own_work_leases_and_cursor(room_with_two):
    """The three facts a resuming runtime cannot reconstruct for itself."""
    room, worker, _peer = room_with_two

    await tasks.create(
        participant=worker.participant,
        command=CreateTaskCommand(title="Migrate billing", targets=["db/schema.sql"]),
    )
    task = await tasks.create(
        participant=worker.participant,
        command=CreateTaskCommand(title="Rotate the signing key", targets=["app/auth.py"]),
    )
    held = await tasks.claim(
        participant=worker.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    assert held.claim is not None
    await work_service.declare(
        participant=worker.participant,
        command=DeclareWorkCommand(headline="Rotating keys", targets=["app/auth.py"]),
    )

    state = await projections.hydrate(room_id=room.room.id, recipient=worker.participant)

    assert [w["headline"] for w in state["your_work"]] == ["Rotating keys"]

    # The lease carries the fence and the time left, which is precisely what a runtime
    # that lost its context cannot work out by looking at the task.
    assert len(state["your_leases"]) == 1
    lease = state["your_leases"][0]
    assert lease["task_id"] == task.id
    assert lease["fence"] == held.claim.fence
    assert lease["seconds_remaining"] > 0
    assert lease["targets"] == ["app/auth.py"]

    # And a cursor to resume the stream from, consistent with the read above.
    assert state["cursor"] >= 1


async def test_hydration_is_yours_alone_and_not_a_second_snapshot(room_with_two):
    """A peer's work and leases are not part of what you resume.

    This is the property that keeps hydration cheap. If it drifted into returning the
    board it would become `get_room_state` with a different name, and the reason it
    exists — a cold surface paying for one participant's state rather than everyone's —
    would quietly disappear.
    """
    room, worker, peer = room_with_two

    peers_task = await tasks.create(
        participant=peer.participant, command=CreateTaskCommand(title="Peer's own job")
    )
    await tasks.claim(participant=peer.participant, command=ClaimTaskCommand(task_id=peers_task.id))
    await work_service.declare(
        participant=peer.participant,
        command=DeclareWorkCommand(headline="Peer is busy", targets=["peer.py"]),
    )

    state = await projections.hydrate(room_id=room.room.id, recipient=worker.participant)

    assert state["your_work"] == []
    assert state["your_leases"] == []
    assert state["needs_you"] == 0


async def test_what_is_waiting_on_you_is_counted_not_inferred(room_with_two):
    """`needs_you` distinguishes "nothing waiting" from "nothing loaded".

    A cold surface reading empty lists cannot tell those apart, and the difference
    decides whether it goes back to sleep or starts work.
    """
    room, worker, peer = room_with_two

    proposed = await tasks.create(
        participant=peer.participant,
        command=CreateTaskCommand(
            title="Please take this one",
            propose_to_participant_id=worker.participant.id,
        ),
    )
    await message_service.post(
        participant=peer.participant,
        command=PostMessageCommand(
            body="Only for you",
            disclosure=Disclosure(
                audience=Audience.PARTICIPANT, to_participant_id=worker.participant.id
            ),
        ),
    )

    state = await projections.hydrate(room_id=room.room.id, recipient=worker.participant)

    assert [p["task_id"] for p in state["proposed_to_you"]] == [proposed.id]
    assert [m["body"] for m in state["addressed_to_you"]] == ["Only for you"]
    assert state["needs_you"] == 2

    # The peer who sent both is waiting on nothing.
    peer_state = await projections.hydrate(room_id=room.room.id, recipient=peer.participant)
    assert peer_state["needs_you"] == 0


async def test_hydration_does_not_claim_to_be_conversation_history(room_with_two):
    """The concession in D-031, asserted rather than left to good intentions.

    Hydration cannot convey what a human asked or which tradeoffs were rejected. The
    risk is not that it fails to — it is that it ships first and is quietly treated as
    though it had, so the flag saying otherwise is part of the payload.
    """
    room, worker, peer = room_with_two

    await message_service.post(
        participant=peer.participant,
        command=PostMessageCommand(body="A long discussion about tradeoffs"),
    )

    state = await projections.hydrate(room_id=room.room.id, recipient=worker.participant)

    assert state["is_conversation_history"] is False
    # Room-wide conversation is not in here. It is in the room, deliberately.
    assert "messages" not in state
    assert state["addressed_to_you"] == []

"""A runtime that was told to stop may not act, whether or not it actually stopped.

The incident behind this. Two same-label companions were started, both claimed one
task, and both were stopped by killing their supervisors. Ten minutes later a commit
appeared in the repository: the supervisors had died, the CLI children they spawned had
not, and one orphan finished its task under an explicit freeze. The room saw none of it,
because an orphan sends no heartbeat and so has no seat, no claim and no work card -
while its write access to the shared tree was undiminished.

The attempted fix was OS containment: process groups, a watchdog, job objects. Review
found three separate races, and then the real problem underneath them - a POSIX child
can call `setsid()` and leave its process group, so group-kill is escapable by design.
More importantly it is the wrong layer to be working at. **In the hosted product the
runtime is on the customer's machine.** There is no process group we can signal, no
cgroup we can write to, and no privilege we hold there. Any containment story that
assumes we own the box describes a laptop, not the product.

So the room stops trying to end the process and revokes its permission instead. That
needs no cooperation from the runtime and no privilege on its host: the drained runtime
may keep running, keep its token, keep its fence, and still change nothing here.

What these tests pin is that refusal, and the two properties that make it worth having:

  * it is enforced at the point where any command becomes somebody's executor, so it
    covers claim, renew, checkpoint, complete and release without those handlers
    knowing the concept exists; and
  * it is **sticky** - reconnecting is not a way to become allowed again. That is the
    property the whole design rests on, because a survivor reconnecting is exactly what
    an orphan does, and a drain that a reconnect cleared would be theatre.
"""

from __future__ import annotations

import pytest

from app.core import presence, store, tasks
from app.core.errors import Forbidden, StaleRuntime
from app.db import database as db
from app.domain.commands import ClaimTaskCommand, ConnectCommand, CreateTaskCommand
from app.domain.events import EventType

from .conftest import FULL_CAPABILITIES

pytestmark = pytest.mark.asyncio

LABEL = "worker-a"


async def _attach(member, *, label: str = LABEL) -> str:
    """Open a connection carrying a runtime label, and return the attachment id.

    Declares the same capabilities the shared `join` fixture does, so a refusal in
    these tests is the drain and never an eligibility rule that happened to fire first.
    """
    negotiated = await presence.connect(
        participant=member.participant,
        command=ConnectCommand(attachment_label=label, capabilities=FULL_CAPABILITIES),
        transport="long_poll",
    )
    assert negotiated.connection.attachment_id is not None
    return negotiated.connection.attachment_id


async def _claim_a_task(member, *, title: str = "Some work") -> str:
    created = await tasks.create(
        participant=member.participant, command=CreateTaskCommand(title=title)
    )
    await tasks.claim(participant=member.participant, command=ClaimTaskCommand(task_id=created.id))
    return created.id


async def test_a_drained_runtime_cannot_claim_even_though_it_is_still_running(make_room, join):
    """The whole point: the process is alive and holds a valid token. It is refused."""
    fixture = await make_room()
    member = await join(fixture, display_name="Worker", connect=False)
    attachment_id = await _attach(member)

    await presence.drain_runtime(
        room=fixture.room,
        participant=member.participant,
        attachment_id=attachment_id,
        reason="orphan suspected after a kill that could not be verified",
    )

    # Nothing was signalled and nothing died. The runtime reconnects - as a survivor
    # would - and tries to work.
    await _attach(member)
    created = await tasks.create(
        participant=fixture.owner, command=CreateTaskCommand(title="Work it must not take")
    )
    with pytest.raises(StaleRuntime) as caught:
        await tasks.claim(
            participant=member.participant, command=ClaimTaskCommand(task_id=created.id)
        )
    assert caught.value.details["attachment_id"] == attachment_id
    assert "orphan suspected" in caught.value.details["reason"]


async def test_reconnecting_does_not_clear_a_drain(make_room, join):
    """The property the design rests on. A survivor that reconnects is the same survivor.

    If a fresh connection cleared the drain, an orphan would escape simply by doing the
    thing orphans do, and the refusal would be decorative.
    """
    fixture = await make_room()
    member = await join(fixture, display_name="Worker", connect=False)
    attachment_id = await _attach(member)
    await presence.drain_runtime(
        room=fixture.room, participant=member.participant, attachment_id=attachment_id
    )

    # Same label, so the reconnect lands on the same durable runtime rather than
    # inventing a new one - which is the behaviour that makes affinity work, and the
    # behaviour that would otherwise be the escape hatch.
    again = await _attach(member)
    assert again == attachment_id

    row = await db.fetch_one("SELECT drained_at FROM attachments WHERE id = ?", (attachment_id,))
    assert row is not None and row["drained_at"] is not None


async def test_a_drain_bumps_the_epoch_and_a_resume_does_not_roll_it_back(make_room, join):
    """The epoch counts runs. A resumed runtime is a new run, not the old one restored."""
    fixture = await make_room()
    member = await join(fixture, display_name="Worker", connect=False)
    attachment_id = await _attach(member)

    before = await db.fetch_one("SELECT epoch FROM attachments WHERE id = ?", (attachment_id,))
    assert before is not None and int(before["epoch"]) == 1

    drained = await presence.drain_runtime(
        room=fixture.room, participant=member.participant, attachment_id=attachment_id
    )
    assert drained.result["epoch"] == 2

    resumed = await presence.resume_runtime(
        room=fixture.room,
        participant=member.participant,
        attachment_id=attachment_id,
        note="confirmed the old process is gone",
    )
    assert resumed.result["was_drained"] is True
    assert resumed.result["epoch"] == 2, "a resume is not a rewind"


async def test_a_resumed_runtime_can_work_again(make_room, join):
    """Refusal has to be reversible, or a false alarm costs a worker permanently."""
    fixture = await make_room()
    member = await join(fixture, display_name="Worker", connect=False)
    attachment_id = await _attach(member)
    await presence.drain_runtime(
        room=fixture.room, participant=member.participant, attachment_id=attachment_id
    )
    await presence.resume_runtime(
        room=fixture.room, participant=member.participant, attachment_id=attachment_id
    )

    await _attach(member)
    task_id = await _claim_a_task(member)
    task = await store.load_task_for_room(fixture.room.id, task_id)
    assert task is not None and task.claim is not None


async def test_draining_twice_is_a_no_op_rather_than_an_error(make_room, join):
    """A supervisor draining a runtime it cannot see will do it twice. That is correct
    behaviour on its part, so it must not be punished for it."""
    fixture = await make_room()
    member = await join(fixture, display_name="Worker", connect=False)
    attachment_id = await _attach(member)

    first = await presence.drain_runtime(
        room=fixture.room, participant=member.participant, attachment_id=attachment_id
    )
    second = await presence.drain_runtime(
        room=fixture.room, participant=member.participant, attachment_id=attachment_id
    )
    assert first.result["already_drained"] is False
    assert second.result["already_drained"] is True
    assert second.result["epoch"] == first.result["epoch"], "a second drain is not a second run"


async def test_a_drain_is_recorded_in_the_log_with_who_and_why(make_room, join):
    """A stop nobody can attribute is indistinguishable from a crash (principle 1)."""
    fixture = await make_room()
    member = await join(fixture, display_name="Worker", connect=False)
    attachment_id = await _attach(member)

    await presence.drain_runtime(
        room=fixture.room,
        participant=member.participant,
        attachment_id=attachment_id,
        reason="duplicate executor",
    )

    rows = await db.fetch_all(
        "SELECT payload, actor_participant_id FROM room_events "
        "WHERE room_id = ? AND type = ? ORDER BY seq",
        (fixture.room.id, EventType.RUNTIME_DRAINED.value),
    )
    assert len(rows) == 1
    payload = db.loads(rows[0]["payload"])
    assert payload["attachment_id"] == attachment_id
    assert payload["reason"] == "duplicate executor"
    assert payload["epoch"] == 2
    assert rows[0]["actor_participant_id"] == member.participant.id


async def test_one_seat_cannot_drain_another_seats_runtime(make_room, join):
    """Stopping someone else's worker is acting as them, and `room.admin` is not that.

    The owner here is a room admin and is still refused, which is the case worth
    pinning: the check is ownership, not privilege (docs/SECURITY.md).
    """
    fixture = await make_room()
    member = await join(fixture, display_name="Worker", connect=False)
    attachment_id = await _attach(member)

    with pytest.raises(Forbidden):
        await presence.drain_runtime(
            room=fixture.room,
            participant=fixture.owner,
            attachment_id=attachment_id,
            reason="admin overreach",
        )

    row = await db.fetch_one("SELECT drained_at FROM attachments WHERE id = ?", (attachment_id,))
    assert row is not None and row["drained_at"] is None


async def test_a_runtime_with_no_attachment_row_is_unknown_rather_than_drained(make_room, join):
    """An ephemeral client never had a runtime row. Refusing it would invent a stop out
    of an absence and break every connection that declines to name itself (D-034)."""
    fixture = await make_room()
    member = await join(fixture, display_name="Ephemeral")  # connects without a label

    task_id = await _claim_a_task(member)
    task = await store.load_task_for_room(fixture.room.id, task_id)
    assert task is not None and task.claim is not None


async def test_the_drain_is_reachable_over_http_and_mcp(make_room, join):
    """A guarantee with no door is not a guarantee.

    The first cut of D-062 implemented the refusal in core and wired it to nothing, so
    it was correct, tested, and impossible to invoke from any client. Both adapters are
    asserted here rather than one, because they are separate translations of the same
    command and either could be the one that is missing.
    """
    from app.adapters.mcp import server as mcp_tools

    fixture = await make_room()
    member = await join(fixture, display_name="Worker", connect=False)
    attachment_id = await _attach(member)

    drained = await mcp_tools.drain_runtime(
        attachment_id=attachment_id,
        reason="stopped it, could not verify",
        participant_token=member.token,
    )
    assert drained["ok"] is True and drained["epoch"] == 2

    # The refusal is what the caller sees through the adapter too: an error result
    # rather than an exception, because an MCP client cannot catch a Python type.
    await _attach(member)
    created = await tasks.create(
        participant=fixture.owner, command=CreateTaskCommand(title="Not for a drained runtime")
    )
    refused = await mcp_tools.claim_task(task_id=created.id, participant_token=member.token)
    assert refused["ok"] is False
    assert refused["error"] == "stale_runtime"

    resumed = await mcp_tools.resume_runtime(
        attachment_id=attachment_id, note="confirmed gone", participant_token=member.token
    )
    assert resumed["ok"] is True and resumed["was_drained"] is True

    # And the native transport, which is a separate translation of the same command.
    import httpx

    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://drain.test"
    ) as client:
        response = await client.post(
            f"/api/rooms/{fixture.room.id}/runtimes/drain",
            json={"attachment_id": attachment_id, "reason": "over http"},
            headers={"Authorization": f"Bearer {member.token}"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["epoch"] == 3, "a second drain is a third run"

        # Reconnect first, exactly as a survivor would. Without this the honest error
        # is `capability_unsupported` - the drain closed its connections, so it has no
        # runtime at all - and the sticky refusal would never be exercised.
        await _attach(member)
        refused_over_http = await client.post(
            f"/api/rooms/{fixture.room.id}/tasks/claim",
            json={"task_id": created.id},
            headers={"Authorization": f"Bearer {member.token}"},
        )
        assert refused_over_http.status_code == 409
        assert refused_over_http.json()["error"] == "stale_runtime"

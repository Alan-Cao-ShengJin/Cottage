"""The MCP surface for checkpoints and questions (D-050, D-051).

Adapter-level rather than core-level, deliberately. Every defect that reached a real
client on 2026-08-15 was in an adapter, a projection or a route shape and none was in
`core/` â€” including one where the guard and its docstring were widened correctly and
the *call site* was not, invisible because the adapter had no tests at all (D-046,
D-049). Green in `core` says nothing about the door a real client comes through.
"""

from __future__ import annotations

import pytest

from app.adapters.mcp import compact
from app.adapters.mcp import server as mcp_server
from app.core import projections, tasks
from app.domain.commands import ClaimTaskCommand, CreateTaskCommand

pytestmark = pytest.mark.asyncio


async def _claimed(room, member, *, title="Ship the thing"):
    task = await tasks.create(participant=room.owner, command=CreateTaskCommand(title=title))
    return await tasks.claim(
        participant=member.participant, command=ClaimTaskCommand(task_id=task.id)
    )


async def test_a_worker_can_checkpoint_over_mcp(make_room, join):
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed(room, worker)

    result = await mcp_server.record_checkpoint(
        task_id=task.id,
        fence=task.claim.fence,
        summary="Built the image; next is the deploy.",
        phase="built",
        next_action="deploy to staging",
        participant_token=worker.token,
    )
    assert result["ok"] is True
    assert result["checkpoint"]["summary"] == "Built the image; next is the deploy."
    assert result["checkpoint"]["resume_state"]["next_action"] == "deploy to staging"


async def test_a_checkpoint_with_no_bookmark_fields_records_no_bookmark(make_room, join):
    """Absent, not an empty object.

    An empty `ResumeState` would make `has_resume_state` true for every checkpoint,
    which turns the one honest signal about hidden state into noise.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed(room, worker)

    result = await mcp_server.record_checkpoint(
        task_id=task.id,
        fence=task.claim.fence,
        summary="Just a note.",
        participant_token=worker.token,
    )
    assert result["checkpoint"]["resume_state"] is None


async def test_the_round_trip_a_blocked_worker_and_a_human_actually_perform(make_room, join):
    """The gate-6 shape, over the adapter both ends really use.

    A worker stands down rather than guessing at a deploy target; a human sees the
    question in the coordination view; answering it returns the work; the worker
    re-claims. Every step through the MCP surface, because that is where the previous
    four defects lived.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed(room, worker, title="Deploy the service")

    asked = await mcp_server.ask_question(
        body="Production or staging? I will not guess at a deploy target.",
        task_id=task.id,
        blocking=True,
        fence=task.claim.fence,
        checkpoint_summary="Built and tested; stopped before the deploy step.",
        participant_token=worker.token,
    )
    assert asked["ok"] is True

    # The human's view: the question is present, and the parked task does not
    # advertise itself as available.
    snapshot = await projections.snapshot(room_id=room.room.id, recipient=room.owner)
    view = compact.room_state(snapshot)
    assert view["open_questions"][0]["blocking"] is True
    parked = next(t for t in view["tasks"] if t["task_id"] == task.id)
    assert parked["status"] == "waiting_input"
    assert parked["claimable"] is False

    answered = await mcp_server.answer_question(
        question_id=asked["question"]["id"],
        body="Staging only. Never production from an unattended run.",
        participant_token=room.owner_token,
    )
    assert answered["ok"] is True

    reclaimed = await tasks.claim(
        participant=worker.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    assert reclaimed.claim is not None
    assert reclaimed.claim.fence > task.claim.fence


async def test_a_worker_answering_itself_is_refused_at_the_adapter_too(make_room, join):
    """The refusal has to survive translation, not only exist in `core`."""
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed(room, worker)
    asked = await mcp_server.ask_question(
        body="Shall I proceed?",
        task_id=task.id,
        blocking=True,
        fence=task.claim.fence,
        participant_token=worker.token,
    )

    result = await mcp_server.answer_question(
        question_id=asked["question"]["id"],
        body="Yes, obviously.",
        participant_token=worker.token,
    )
    assert result["ok"] is False
    assert result["error"] == "forbidden"


async def test_the_compact_view_never_carries_another_seat_s_bookmark(make_room, join):
    """The coordination view is the most-read projection, so it is the worst place to leak.

    Checked here as well as in `core` because a compact projection builds its own
    payload rather than filtering the domain object, which is exactly how a field
    that is correctly hidden in one place reappears in another.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    peer = await join(room, display_name="Peer")
    task = await _claimed(room, worker)
    await mcp_server.record_checkpoint(
        task_id=task.id,
        fence=task.claim.fence,
        summary="Public summary.",
        phase="secret-phase",
        participant_token=worker.token,
    )

    snapshot = await projections.snapshot(room_id=room.room.id, recipient=peer.participant)
    assert "secret-phase" not in str(compact.room_state(snapshot))

    events = await projections.visible_events_since(
        room_id=room.room.id, recipient=peer.participant, since_seq=0
    )
    assert "secret-phase" not in str(compact.events(events))


async def test_a_stale_fence_is_reported_rather_than_swallowed(make_room, join):
    """An adapter that returned `ok` on a refused write would be worse than one that crashed."""
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed(room, worker)

    result = await mcp_server.record_checkpoint(
        task_id=task.id,
        fence=task.claim.fence - 1,
        summary="From a lease I no longer hold.",
        participant_token=worker.token,
    )
    assert result["ok"] is False
    assert result["error"] == "stale_fence"

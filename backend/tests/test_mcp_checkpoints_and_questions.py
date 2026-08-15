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
from app.core.errors import Forbidden
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


async def test_the_self_answer_refusal_fires_only_for_a_named_runtime(make_room, join):
    """The rule as it actually is over MCP, stated rather than assumed.

    The refusal is scoped to the *runtime*, and a runtime is identified only when the
    caller names its connection (D-055 correction). The MCP tools do not currently
    thread a connection id, so a client that asks and answers through them is
    unidentified on both sides and is **permitted**.

    That is the deliberate direction of the rule — unidentified permits, because
    refusing on an absence bites hardest against clients that declare least — but the
    consequence deserves an assertion rather than a comment, because "the adapter is
    laxer than core" is exactly the kind of gap this project keeps finding late.

    The guarantee lost is small: blocking is voluntary, so a worker that wanted to
    carry on could simply never have blocked. What remains is attribution, and
    `answered_by_attachment_id` records who replied.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")

    # Unidentified on both sides: permitted, and recorded as same-seat.
    lax = await _claimed(room, worker, title="Unnamed runtime")
    asked = await mcp_server.ask_question(
        body="Shall I proceed?",
        task_id=lax.id,
        blocking=True,
        fence=lax.claim.fence,
        participant_token=worker.token,
    )
    permitted = await mcp_server.answer_question(
        question_id=asked["question"]["id"],
        body="Yes, obviously.",
        participant_token=worker.token,
    )
    assert permitted["ok"] is True, "an unidentified caller is not evidence of self-answering"

    # Named on both sides: refused, which is the property the rule exists for.
    from app.core import questions as questions_svc
    from app.domain.commands import AnswerQuestionCommand, AskQuestionCommand

    strict = await _claimed(room, worker, title="Named runtime")
    named = await questions_svc.ask(
        participant=worker.participant,
        command=AskQuestionCommand(
            body="And this one?",
            task_id=strict.id,
            blocking=True,
            fence=strict.claim.fence,
            connection_id=worker.connection_id,
        ),
    )
    with pytest.raises(Forbidden):
        await questions_svc.answer(
            participant=worker.participant,
            command=AnswerQuestionCommand(
                question_id=named.id, body="Yes.", connection_id=worker.connection_id
            ),
        )


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

"""Worker → human, and the blocking case that costs the asker its lease (D-051).

The property under test is that asking carries **no authority**. A question must be
answerable by an ordinary participant, must never be issuable as an instruction, and
must never let its asker unblock itself — because the moment any of those three
slips, the room has grown a second control plane with none of the first one's rules.
"""

from __future__ import annotations

import pytest

from app.core import checkpoints, questions, store, tasks
from app.core.errors import Forbidden, InvalidCommand, StaleFence
from app.domain.commands import (
    AnswerQuestionCommand,
    AskQuestionCommand,
    ClaimTaskCommand,
    CreateTaskCommand,
)
from app.domain.events import EventType
from app.domain.room import ParticipantRole, Scope
from app.domain.task import TaskStatus

pytestmark = pytest.mark.asyncio


async def _claimed_task(room, member, *, title: str = "Deploy the thing"):
    task = await tasks.create(participant=room.owner, command=CreateTaskCommand(title=title))
    claimed = await tasks.claim(
        participant=member.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    assert claimed.claim is not None
    return claimed


async def test_asking_needs_no_administrative_grant(make_room, join):
    """The whole reason a question is not a reversed directive.

    Issuing a directive requires `room.admin` precisely so a worker cannot
    manufacture instructions. If asking needed the same grant, an unattended worker
    could never raise anything — and if a directive could be reversed, every worker
    would hold the authority that check exists to withhold.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    assert Scope.ROOM_ADMIN not in worker.participant.scopes

    question = await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(body="Which staging cluster should I target?"),
    )
    assert question.is_open
    assert question.asked_by_participant_id == worker.participant.id


async def test_a_non_blocking_question_leaves_the_work_alone(make_room, join):
    """The default, and it has to be, or a worker cannot work unattended.

    A worker that halts on every uncertainty is one that stops at the first
    ambiguity and waits for a human who may be asleep.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)

    await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(body="Any preference on naming?", task_id=task.id),
    )

    after = await store.load_task(task.id)
    assert after.status is TaskStatus.CLAIMED
    assert after.claim is not None
    assert after.claim.participant_id == worker.participant.id


async def test_a_blocking_question_checkpoints_parks_and_releases_atomically(make_room, join):
    """All three together or none.

    A task parked with no record of where its worker had got to is exactly the state
    a resume needs and exactly what a crash between two commands would destroy.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)

    question = await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(
            body="Production or staging? I will not guess at a deploy target.",
            task_id=task.id,
            blocking=True,
            fence=task.claim.fence,
            checkpoint_summary="Built and tested; stopped before the deploy step.",
        ),
    )

    after = await store.load_task(task.id)
    assert after.status is TaskStatus.WAITING_INPUT
    assert after.claim is None, "the lease is released, not merely paused"
    assert question.blocking

    recorded = await checkpoints.latest_for_task(task.id, recipient=worker.participant)
    assert recorded[-1].summary == "Built and tested; stopped before the deploy step."
    assert recorded[-1].fence == task.claim.fence, "written under the lease it still held"


async def test_parked_work_is_not_handed_to_the_next_worker(make_room, join):
    """Otherwise the room churns holders through one unanswered question.

    Each new claimant discovers the same missing information and stands down again,
    which looks like activity and is not.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    other = await join(room, display_name="Other worker")
    task = await _claimed_task(room, worker)

    await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(
            body="Which region?", task_id=task.id, blocking=True, fence=task.claim.fence
        ),
    )

    with pytest.raises(InvalidCommand) as exc:
        await tasks.claim(participant=other.participant, command=ClaimTaskCommand(task_id=task.id))
    assert "unanswered question" in str(exc.value)


async def test_the_answer_returns_the_work_to_open(make_room, join):
    """Back to `open`, not back to its old holder.

    The worker may have died while waiting, and handing a lease to a runtime that is
    not there reproduces the stuck-work failure leases exist to avoid.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)
    question = await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(
            body="Which region?", task_id=task.id, blocking=True, fence=task.claim.fence
        ),
    )

    await questions.answer(
        participant=room.owner,
        command=AnswerQuestionCommand(question_id=question.id, body="Staging, sin region."),
    )

    after = await store.load_task(task.id)
    assert after.status is TaskStatus.OPEN
    reclaimed = await tasks.claim(
        participant=worker.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    assert reclaimed.claim is not None
    assert reclaimed.claim.fence > task.claim.fence, "a new generation, so no late write lands"


async def test_a_runtime_cannot_answer_its_own_question(make_room, join):
    """The refusal that keeps blocking meaningful.

    A runtime able to unblock itself has not asked a question; it has taken a pause
    it can end whenever it likes.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)
    question = await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(
            body="Shall I proceed?",
            task_id=task.id,
            blocking=True,
            fence=task.claim.fence,
            connection_id=worker.connection_id,
        ),
    )

    with pytest.raises(Forbidden):
        await questions.answer(
            participant=worker.participant,
            command=AnswerQuestionCommand(
                question_id=question.id,
                body="Yes, obviously.",
                connection_id=worker.connection_id,
            ),
        )


async def test_the_human_at_the_same_seat_may_answer_their_own_worker(make_room, join):
    """Scoped to the runtime, not the seat — found by running it live (D-055).

    A person's chat surface and their companion worker are one participant. Refusing
    per seat therefore blocked the one человек most obviously entitled to answer: the
    human whose worker had just stood down and asked them something.

    It is still recorded as a same-seat answer, because a reader deciding how much
    independent input a worker received needs to know which kind it was.
    """
    from app.core import presence
    from app.domain.capabilities import Capability, HostClass
    from app.domain.commands import ConnectCommand

    room = await make_room()
    seat = await join(room, display_name="Alan's agent")
    companion = await presence.connect(
        participant=seat.participant,
        command=ConnectCommand(
            capabilities=[
                Capability.CAN_RECEIVE_EVENTS,
                Capability.SUPPORTS_POLL,
                Capability.CAN_EXECUTE_BACKGROUND,
                Capability.CAN_INITIATE_FOLLOWUP,
                Capability.SUPPORTS_TOOLS,
            ],
            host_class=HostClass.PERSISTENT_LOCAL,
            attachment_label="worker-main",
        ),
        transport="long_poll",
    )
    task = await tasks.create(participant=room.owner, command=CreateTaskCommand(title="Work"))
    claimed = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=companion.connection.id),
    )
    question = await questions.ask(
        participant=seat.participant,
        command=AskQuestionCommand(
            body="Which environment?",
            task_id=task.id,
            blocking=True,
            fence=claimed.claim.fence,
            connection_id=companion.connection.id,
        ),
    )

    answer = await questions.answer(
        participant=seat.participant,
        command=AnswerQuestionCommand(
            question_id=question.id,
            body="Staging.",
            # The human's surface — a different runtime of the same seat.
            connection_id=seat.connection_id,
        ),
    )
    assert answer.answered_by_participant_id == seat.participant.id
    assert (await store.load_task(task.id)).status is TaskStatus.OPEN


async def test_an_ordinary_participant_may_answer(make_room, join):
    """Answering is not an exercise of authority.

    Routing replies through the control plane would mean only room admins could ever
    unblock a worker, which turns an ordinary conversation into a privilege.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    peer = await join(room, display_name="Peer")
    assert Scope.ROOM_ADMIN not in peer.participant.scopes
    task = await _claimed_task(room, worker)
    question = await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(
            body="Which region?", task_id=task.id, blocking=True, fence=task.claim.fence
        ),
    )

    answer = await questions.answer(
        participant=peer.participant,
        command=AnswerQuestionCommand(question_id=question.id, body="sin."),
    )
    assert answer.answered_by_participant_id == peer.participant.id
    assert (await store.load_task(task.id)).status is TaskStatus.OPEN


async def test_a_stale_runtime_cannot_park_work_it_no_longer_holds(make_room, join):
    """Blocking releases a lease, so it is fenced like every other release."""
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)

    with pytest.raises(StaleFence):
        await questions.ask(
            participant=worker.participant,
            command=AskQuestionCommand(
                body="Which region?",
                task_id=task.id,
                blocking=True,
                fence=task.claim.fence - 1,
            ),
        )


async def test_blocking_without_a_task_is_refused(make_room, join):
    """Blocking means "this work cannot proceed", and work means a task.

    Without the link there is nothing to park, and an unenforceable block is a
    message wearing a uniform — the same argument that shaped control directives.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")

    with pytest.raises(InvalidCommand):
        await questions.ask(
            participant=worker.participant,
            command=AskQuestionCommand(body="Generally, what should I do?", blocking=True),
        )


async def test_blocking_without_a_fence_is_refused(make_room, join):
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)

    with pytest.raises(InvalidCommand) as exc:
        await questions.ask(
            participant=worker.participant,
            command=AskQuestionCommand(body="Which region?", task_id=task.id, blocking=True),
        )
    assert "fence" in str(exc.value)


async def test_answering_twice_is_refused(make_room, join):
    room = await make_room()
    worker = await join(room, display_name="Worker")
    peer = await join(room, display_name="Peer")
    question = await questions.ask(
        participant=worker.participant, command=AskQuestionCommand(body="Which region?")
    )
    await questions.answer(
        participant=peer.participant,
        command=AnswerQuestionCommand(question_id=question.id, body="sin."),
    )
    with pytest.raises(InvalidCommand):
        await questions.answer(
            participant=room.owner,
            command=AnswerQuestionCommand(question_id=question.id, body="no, fra."),
        )


async def test_the_work_card_does_not_outlive_the_parked_task(make_room, join):
    """The defect the first live stop exposed, in a second place (D-049).

    A board asserting "Working: deploy the thing" against a task its holder stood
    down from is worse than showing nothing.
    """
    from app.core import work
    from app.domain.commands import DeclareWorkCommand

    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)
    await work.declare(
        participant=worker.participant,
        command=DeclareWorkCommand(headline="Deploying the thing", task_id=task.id),
    )

    await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(
            body="Which region?", task_id=task.id, blocking=True, fence=task.claim.fence
        ),
    )

    still_open = await store.list_open_work(room.room.id)
    assert [w for w in still_open if w.task_id == task.id] == []


async def test_open_questions_show_both_directions(make_room, join):
    """What is waiting on you, and what you are waiting on, in one list.

    Split into two projections and one of them stops being read.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")

    mine = await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(body="Which region?", to_participant_id=room.owner.id),
    )
    theirs = await questions.ask(
        participant=room.owner,
        command=AskQuestionCommand(
            body="Are you nearly done?", to_participant_id=worker.participant.id
        ),
    )

    for_worker = {
        q.id for q in await questions.open_for(worker.participant.id, room_id=room.room.id)
    }
    assert {mine.id, theirs.id} <= for_worker


async def test_hydration_counts_what_you_can_act_on_not_what_you_await(make_room, join):
    """Counting your own outstanding question would tell a worker it has work.

    What it has is patience, and a `needs_you` that conflates the two makes an
    unattended loop spin.
    """
    from app.core import projections

    room = await make_room()
    worker = await join(room, display_name="Worker")
    await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(body="Which region?", to_participant_id=room.owner.id),
    )

    mine = await projections.hydrate(room_id=room.room.id, recipient=worker.participant)
    theirs = await projections.hydrate(room_id=room.room.id, recipient=room.owner)
    assert mine["needs_you"] == 0, "waiting on someone else is not work"
    assert theirs["needs_you"] >= 1, "being asked is"


async def test_the_room_can_see_the_question_and_the_answer(make_room, join):
    """Room-public even when addressed.

    Restricting the body would mean an unanswered question is invisible to the one
    participant who happened to know, which is how questions go stale.
    """
    from app.core import projections

    room = await make_room()
    worker = await join(room, display_name="Worker")
    peer = await join(room, display_name="Peer")
    question = await questions.ask(
        participant=worker.participant,
        command=AskQuestionCommand(body="Which region?", to_participant_id=room.owner.id),
    )
    await questions.answer(
        participant=room.owner,
        command=AnswerQuestionCommand(question_id=question.id, body="sin."),
    )

    seen = await projections.visible_events_since(
        room_id=room.room.id, recipient=peer.participant, since_seq=0
    )
    types = [e["type"] for e in seen]
    assert EventType.QUESTION_ASKED.value in types
    assert EventType.QUESTION_ANSWERED.value in types


async def test_an_observer_cannot_ask(make_room, join):
    """Asking is speaking. A seat that may not speak may not ask either."""
    room = await make_room()
    observer = await join(room, display_name="Watcher", role=ParticipantRole.OBSERVER)

    with pytest.raises(Forbidden):
        await questions.ask(
            participant=observer.participant, command=AskQuestionCommand(body="What is going on?")
        )

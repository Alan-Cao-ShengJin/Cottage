"""Durable progress on a task (D-050).

The property under test throughout is that a checkpoint is **evidence**, not a
comment: it is fenced like every other claim about work in flight, it cannot be
rewritten, and its two halves reach two different audiences without a projection
having to remember to redact anything.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core import checkpoints, directives, projections, tasks
from app.core.errors import LeaseRequired, StaleFence
from app.db import database as db
from app.domain.checkpoint import ResumeState
from app.domain.commands import (
    AppendCheckpointCommand,
    ClaimTaskCommand,
    CreateTaskCommand,
    IssueDirectiveCommand,
)
from app.domain.directive import DirectiveAction
from app.domain.events import EventType

pytestmark = pytest.mark.asyncio


async def _claimed_task(room, member, *, title: str = "Real work"):
    task = await tasks.create(participant=room.owner, command=CreateTaskCommand(title=title))
    claimed = await tasks.claim(
        participant=member.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    assert claimed.claim is not None
    return claimed


async def test_a_checkpoint_survives_the_runtime_that_wrote_it(make_room, join):
    """The whole point: progress that a restart does not erase.

    Before this the worker counted steps in local memory, so "what has it actually
    done?" was answerable only by the worker, and only while it lived.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)

    await checkpoints.append(
        participant=worker.participant,
        command=AppendCheckpointCommand(
            task_id=task.id,
            fence=task.claim.fence,
            summary="Read the failing test and reproduced it locally. Next: the fix.",
            resume_state=ResumeState(phase="reproducing", next_action="write the fix"),
        ),
    )

    recorded = await checkpoints.latest_for_task(task.id, recipient=worker.participant)
    assert [c.summary for c in recorded] == [
        "Read the failing test and reproduced it locally. Next: the fix."
    ]
    assert recorded[0].resume_state is not None
    assert recorded[0].resume_state.next_action == "write the fix"


async def test_the_resume_state_does_not_reach_another_participant(make_room, join):
    """Two audiences, and the private half stays private in *both* paths.

    Checked through the projection and through the event log, because they are
    separate filters and a leak needs only one of them to be wrong.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    peer = await join(room, display_name="Peer")
    task = await _claimed_task(room, worker)

    await checkpoints.append(
        participant=worker.participant,
        command=AppendCheckpointCommand(
            task_id=task.id,
            fence=task.claim.fence,
            summary="Applied the fix; tests green.",
            resume_state=ResumeState(phase="verifying", completed_step_ids=["s1", "s2"]),
        ),
    )

    theirs = await checkpoints.latest_for_task(task.id, recipient=peer.participant)
    assert theirs[0].summary == "Applied the fix; tests green.", "the public half is public"
    assert theirs[0].resume_state is None, "the private half is not"

    visible = await projections.visible_events_since(
        room_id=room.room.id, recipient=peer.participant, since_seq=0
    )
    types = [e["type"] for e in visible]
    assert EventType.TASK_CHECKPOINTED.value in types
    assert EventType.TASK_RESUME_STATE_RECORDED.value not in types


async def test_the_public_half_admits_that_a_private_half_exists(make_room, join):
    """ "There is state you cannot see" is not itself a secret.

    Hiding the fact would make the room's account of a worker's progress quietly
    incomplete, which is a subtler failure than withholding the content.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    peer = await join(room, display_name="Peer")
    task = await _claimed_task(room, worker)

    await checkpoints.append(
        participant=worker.participant,
        command=AppendCheckpointCommand(
            task_id=task.id,
            fence=task.claim.fence,
            summary="Halfway.",
            resume_state=ResumeState(phase="mid"),
        ),
    )
    visible = await projections.visible_events_since(
        room_id=room.room.id, recipient=peer.participant, since_seq=0
    )
    checkpointed = [e for e in visible if e["type"] == EventType.TASK_CHECKPOINTED.value]
    assert checkpointed[0]["payload"]["has_resume_state"] is True


async def test_a_stale_runtime_cannot_append_to_the_record(make_room, join):
    """A checkpoint asserts something about a run, so it is fenced like a completion.

    Without this, a zombie worker could keep writing plausible progress against work
    that had already moved to someone else.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)

    with pytest.raises(StaleFence):
        await checkpoints.append(
            participant=worker.participant,
            command=AppendCheckpointCommand(
                task_id=task.id, fence=task.claim.fence - 1, summary="From the past."
            ),
        )


async def test_you_cannot_checkpoint_work_you_do_not_hold(make_room, join):
    room = await make_room()
    worker = await join(room, display_name="Worker")
    bystander = await join(room, display_name="Bystander")
    task = await _claimed_task(room, worker)

    with pytest.raises(Exception) as exc:
        await checkpoints.append(
            participant=bystander.participant,
            command=AppendCheckpointCommand(
                task_id=task.id, fence=task.claim.fence, summary="I did this."
            ),
        )
    assert exc.type.__name__ in {"LeaseConflict", "LeaseRequired"}


async def test_an_unclaimed_task_cannot_be_checkpointed(make_room, join):
    """ "Nobody holds it" is not "you hold it" — the D-027 shape, applied here.

    A checkpoint with no lease behind it would put progress on the board with no
    trail showing who was doing the work.
    """
    room = await make_room()
    member = await join(room, display_name="Member")
    task = await tasks.create(participant=room.owner, command=CreateTaskCommand(title="Unclaimed"))

    with pytest.raises(LeaseRequired):
        await checkpoints.append(
            participant=member.participant,
            command=AppendCheckpointCommand(task_id=task.id, fence=0, summary="Progress!"),
        )


async def test_a_paused_worker_may_still_say_where_it_got_to(make_room, join):
    """Pause forbids progress; recording is the opposite of progressing.

    This is the behaviour that makes a pause resumable rather than merely frozen.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)

    await directives.issue(
        participant=room.owner,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.PAUSE,
            task_id=task.id,
            reason="hold while we decide",
        ),
    )

    written = await checkpoints.append(
        participant=worker.participant,
        command=AppendCheckpointCommand(
            task_id=task.id,
            fence=task.claim.fence,
            summary="Paused at step 3 of 5; nothing left half-applied.",
        ),
    )
    assert written.summary.startswith("Paused at step 3")


async def test_a_retry_does_not_double_the_record(make_room, join):
    """The moment a worker checkpoints is the moment it is most likely to be cut off.

    So the retry must be safe, and `command_id` is what makes it so — the same
    guarantee every other command already has, applied where it matters most.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)

    command = AppendCheckpointCommand(
        command_id="cmd-checkpoint-1",
        task_id=task.id,
        fence=task.claim.fence,
        summary="Step one done.",
    )
    first = await checkpoints.append(participant=worker.participant, command=command)
    second = await checkpoints.append(participant=worker.participant, command=command)

    assert first.id == second.id
    assert await checkpoints.count_for_task(task.id) == 1


async def test_nothing_in_the_codebase_can_edit_a_checkpoint():
    """Append-only enforced by absence, and asserted so the absence stays deliberate.

    A checkpoint that could be edited would be a claim about the past the past does
    not support, and the whole value here is that the sequence is evidence.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        for text in [path.read_text(encoding="utf-8")]
        if "UPDATE task_checkpoints" in text or "DELETE FROM task_checkpoints" in text
    ]
    assert offenders == [], f"checkpoints must stay append-only, but {offenders} mutate them"


async def test_the_latest_window_keeps_the_newest_and_returns_them_in_order(make_room, join):
    """Truncating from the wrong end returns ancient history.

    Returning it backwards makes a progress record read as a countdown. Both halves
    are easy to get wrong and neither is visible without asserting it.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)

    for i in range(1, 8):
        await checkpoints.append(
            participant=worker.participant,
            command=AppendCheckpointCommand(
                task_id=task.id, fence=task.claim.fence, summary=f"step {i}"
            ),
        )

    window = await checkpoints.latest_for_task(task.id, recipient=worker.participant, limit=3)
    assert [c.summary for c in window] == ["step 5", "step 6", "step 7"]
    assert await checkpoints.count_for_task(task.id) == 7


async def test_hydration_carries_your_own_progress_and_not_someone_elses(make_room, join):
    """What a restarted runtime actually reads.

    Hydration is the resume path, so a checkpoint that is durable but not hydrated
    would be durable and useless.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    other = await join(room, display_name="Other")
    mine = await _claimed_task(room, worker, title="Mine")
    theirs = await _claimed_task(room, other, title="Theirs")

    await checkpoints.append(
        participant=worker.participant,
        command=AppendCheckpointCommand(
            task_id=mine.id,
            fence=mine.claim.fence,
            summary="mine: halfway",
            resume_state=ResumeState(phase="halfway"),
        ),
    )
    await checkpoints.append(
        participant=other.participant,
        command=AppendCheckpointCommand(
            task_id=theirs.id, fence=theirs.claim.fence, summary="theirs: halfway"
        ),
    )

    payload = await projections.hydrate(room_id=room.room.id, recipient=worker.participant)
    assert set(payload["checkpoints"]) == {mine.id}
    entry = payload["checkpoints"][mine.id][0]
    assert entry["summary"] == "mine: halfway"
    assert entry["resume_state"]["phase"] == "halfway"


async def test_the_resume_state_refuses_a_field_it_was_not_designed_for(make_room, join):
    """The pressure to widen this will be constant.

    The field an executor most wants is "everything I was thinking", and the schema
    exists so adding it has to be a deliberate act with a diff rather than a dict
    quietly growing a key.
    """
    with pytest.raises(ValidationError):
        ResumeState(phase="x", reasoning="because the user seemed to want it")


async def test_an_admin_can_audit_the_private_half_and_that_is_stated(make_room, join):
    """Not a convenience — a consistency requirement.

    Room admins can already audit every directed payload in a room they administer
    (`docs/SECURITY.md` §6). A projection stricter than the event filter would mean
    the same bytes are readable from the log and hidden from the view, so anyone
    reasoning about admin visibility would be reasoning about the wrong answer.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)
    await checkpoints.append(
        participant=worker.participant,
        command=AppendCheckpointCommand(
            task_id=task.id,
            fence=task.claim.fence,
            summary="Public.",
            resume_state=ResumeState(phase="private"),
        ),
    )

    as_admin = await checkpoints.latest_for_task(task.id, recipient=room.owner)
    assert as_admin[0].resume_state is not None
    assert as_admin[0].resume_state.phase == "private"


async def test_the_row_records_which_runtime_wrote_it(make_room, join):
    """Attribution is per seat; provenance is per runtime, and both are needed.

    "Was this the worker or the human's session?" is exactly the question a room
    with a companion attachment has to be able to answer (D-044).
    """
    room = await make_room()
    worker = await join(room, display_name="Worker")
    task = await _claimed_task(room, worker)
    await checkpoints.append(
        participant=worker.participant,
        command=AppendCheckpointCommand(
            task_id=task.id, fence=task.claim.fence, summary="From the worker."
        ),
    )
    row = await db.fetch_one("SELECT * FROM task_checkpoints WHERE task_id = ?", (task.id,))
    assert row["participant_id"] == worker.participant.id
    assert row["fence"] == task.claim.fence

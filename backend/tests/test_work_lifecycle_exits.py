"""A work declaration must end on every exit from a task, not only the interesting one (D-057).

Reported from the room by the ChatGPT participant, watching a companion between tasks:
it saw `work.stale` where it expected `work.ended`. The card had not been closed when
the task completed — it had simply rotted until the staleness sweep noticed nobody was
touching it.

The underlying mistake is worth naming because it is cheap to repeat: this was fixed on
the `stop` path *when the stop proof exposed it*, and nowhere else. Fixing a defect on
the path that surfaced it is half a fix when the bug is in a lifecycle, and this
lifecycle has four exits.

`presence_lost` is the fourth and is genuinely a sweep — a runtime that vanished cannot
close its own card. The other three are acts, and an act should not leave litter.
"""

from __future__ import annotations

import pytest

from app.core import directives, store, tasks, work
from app.db import database as db
from app.domain.commands import (
    CancelTaskCommand,
    ClaimTaskCommand,
    CompleteTaskCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    IssueDirectiveCommand,
)
from app.domain.directive import DirectiveAction
from app.domain.work import WorkEndReason

pytestmark = pytest.mark.asyncio


async def _working(room, member, *, title="Deploy the thing"):
    """A claimed task with an open work declaration against it, as a worker leaves it."""
    task = await tasks.create(participant=room.owner, command=CreateTaskCommand(title=title))
    claimed = await tasks.claim(
        participant=member.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    declared = await work.declare(
        participant=member.participant,
        command=DeclareWorkCommand(headline=f"Working: {title}", task_id=task.id),
    )
    return claimed, declared


async def _card(work_id: str) -> dict:
    row = await db.fetch_one("SELECT * FROM work_declarations WHERE id = ?", (work_id,))
    assert row is not None
    return {"ended_at": row["ended_at"], "end_reason": row["end_reason"]}


async def test_completing_a_task_closes_its_work_card(make_room, join):
    """The exit that was missed, and the one a companion takes most often.

    Between two tasks an unattended worker is idle, and a board that still says
    "Working: deploy the thing" is describing a process that has moved on.
    """
    room = await make_room()
    member = await join(room, display_name="Worker")
    claimed, declared = await _working(room, member)

    await tasks.complete(
        participant=member.participant,
        command=CompleteTaskCommand(task_id=claimed.id, fence=claimed.claim.fence, result="done"),
    )

    card = await _card(declared.id)
    assert card["ended_at"] is not None, "closed on completion, not left to go stale"
    assert card["end_reason"] == WorkEndReason.COMPLETED.value


async def test_cancelling_a_task_closes_its_work_card(make_room, join):
    room = await make_room()
    member = await join(room, display_name="Worker")
    claimed, declared = await _working(room, member)

    await tasks.cancel(
        participant=room.owner,
        command=CancelTaskCommand(task_id=claimed.id, reason="no longer needed"),
    )

    card = await _card(declared.id)
    assert card["ended_at"] is not None
    assert card["end_reason"] == WorkEndReason.SUPERSEDED.value


async def test_stopping_a_task_still_closes_its_work_card(make_room, join):
    """The path that already worked, kept working.

    Included because the fix generalised `end_for_task_tx`, and a generalisation that
    quietly changes the behaviour it was extracted from is the usual way this goes
    wrong.
    """
    room = await make_room()
    member = await join(room, display_name="Worker")
    claimed, declared = await _working(room, member)

    await directives.issue(
        participant=room.owner,
        command=IssueDirectiveCommand(
            target_participant_id=member.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="stand down",
        ),
    )

    card = await _card(declared.id)
    assert card["ended_at"] is not None
    assert card["end_reason"] == WorkEndReason.SUPERSEDED.value


async def test_the_reason_distinguishes_finished_from_taken_away(make_room, join):
    """ "Finished" and "a human stopped you" are different facts about the same card.

    A reader deciding whether the work actually got done needs to tell them apart, and
    a single `superseded` for both would have made the board's history unreadable.
    """
    room = await make_room()
    member = await join(room, display_name="Worker")

    finished, finished_card = await _working(room, member, title="One")
    await tasks.complete(
        participant=member.participant,
        command=CompleteTaskCommand(task_id=finished.id, fence=finished.claim.fence, result="done"),
    )

    halted, halted_card = await _working(room, member, title="Two")
    await directives.issue(
        participant=room.owner,
        command=IssueDirectiveCommand(
            target_participant_id=member.participant.id,
            action=DirectiveAction.STOP,
            task_id=halted.id,
            reason="stand down",
        ),
    )

    assert (await _card(finished_card.id))["end_reason"] == WorkEndReason.COMPLETED.value
    assert (await _card(halted_card.id))["end_reason"] == WorkEndReason.SUPERSEDED.value


async def test_a_worker_between_tasks_declares_nothing(make_room, join):
    """What the room should say about an idle-but-live companion: nothing at all.

    This is the assertion behind the report. The participant is still connected and
    still polling; it simply is not working on anything, and the board must be able to
    represent that without implying it has gone away.
    """
    room = await make_room()
    member = await join(room, display_name="Companion")
    claimed, _ = await _working(room, member)
    await tasks.complete(
        participant=member.participant,
        command=CompleteTaskCommand(task_id=claimed.id, fence=claimed.claim.fence, result="done"),
    )

    still_open = await store.list_open_work(room.room.id)
    assert [w for w in still_open if w.participant_id == member.participant.id] == []

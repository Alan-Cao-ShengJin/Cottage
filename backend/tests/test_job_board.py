"""The durable job board (D-088).

Two product rules carry this file. A supervisor's human asking for something creates a
*board entry*, not an assignment — the orchestrator allocates. And nothing deletes a job:
every ending carries an attributable reason and the history survives it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import jobs, tasks
from app.core.errors import Forbidden, InvalidCommand, NotFound
from app.domain.commands import (
    AcceptJobCommand,
    AssignJobCommand,
    CloseJobCommand,
    CreateTaskCommand,
    PostJobCommand,
    SetJobStateCommand,
    UpdateJobCommand,
)
from app.domain.job import JobState

pytestmark = pytest.mark.asyncio

HUMAN_WORDS = "can you please just make the reconnect thing stop dropping people"


def _post(**kwargs) -> PostJobCommand:
    payload = {
        "title": "Fix reconnect drops",
        "desired_outcome": "A dropped transport no longer ends a participant's work card.",
        "human_instruction": HUMAN_WORDS,
        "requested_urgency": 80,
        "targets": ["backend/app/core/presence.py"],
    }
    payload.update(kwargs)
    return PostJobCommand(**payload)


async def test_a_human_request_becomes_a_job_that_keeps_the_words(make_room, join):
    """The person's own wording is preserved beside the normalised outcome.

    A paraphrase cannot be un-paraphrased once the intent is disputed, which is the whole
    reason the board stores both.
    """
    room = await make_room()
    codex = await join(room, display_name="Codex")

    posted = await jobs.post(participant=codex.participant, command=_post())
    job = await jobs.get(room.room.id, posted["job_id"])

    assert job.state is JobState.POSTED
    assert job.human_instruction == HUMAN_WORDS
    assert job.desired_outcome.startswith("A dropped transport")
    assert job.posted_by_participant_id == codex.participant.id
    assert job.assigned_to_participant_id is None, "posting is not assigning"
    assert job.requested_urgency == 80
    assert job.priority == 0, "urgency is a request; priority is the orchestrator's decision"
    assert [h.to_state for h in job.history] == [JobState.POSTED]


async def test_a_supervisor_cannot_allocate_to_itself(make_room, join):
    """The rule the board exists for. My human asked, therefore I do it — is refused."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    posted = await jobs.post(participant=codex.participant, command=_post())

    with pytest.raises(Forbidden):
        await jobs.assign(
            participant=codex.participant,
            command=AssignJobCommand(
                job_id=posted["job_id"],
                to_participant_id=codex.participant.id,
                reason="my human asked me",
            ),
        )
    job = await jobs.get(room.room.id, posted["job_id"], with_history=False)
    assert job.assigned_to_participant_id is None


async def test_the_orchestrator_may_allocate_to_a_different_supervisor(make_room, join):
    """The requesting supervisor may not be the one that ends up doing it."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    gemini = await join(room, display_name="Gemini")
    posted = await jobs.post(participant=codex.participant, command=_post())

    assigned = await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"],
            to_participant_id=gemini.participant.id,
            reason="Gemini has capacity and owns that module",
        ),
    )
    assert assigned["assigned_to_participant_id"] == gemini.participant.id
    assert assigned["previous_assignee_participant_id"] is None
    assert assigned["state"] == JobState.ASSIGNED.value

    job = await jobs.get(room.room.id, posted["job_id"])
    assert job.assigned_by_participant_id == room.owner.id
    assert job.assigned_at is not None
    assert job.posted_by_participant_id == codex.participant.id, "provenance is untouched"


async def test_reallocation_records_the_previous_owner_and_needs_a_reason(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    gemini = await join(room, display_name="Gemini")
    posted = await jobs.post(participant=codex.participant, command=_post())
    await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"], to_participant_id=codex.participant.id, reason="first pick"
        ),
    )
    await jobs.accept(
        participant=codex.participant, command=AcceptJobCommand(job_id=posted["job_id"])
    )

    moved = await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"],
            to_participant_id=gemini.participant.id,
            reason="Codex is fully allocated to the P0",
        ),
    )
    assert moved["previous_assignee_participant_id"] == codex.participant.id

    job = await jobs.get(room.room.id, posted["job_id"])
    assert job.state is JobState.ASSIGNED, "a new owner has not accepted anything yet"
    assert job.accepted_at is None
    reasons = [h.reason for h in job.history]
    assert "Codex is fully allocated to the P0" in reasons

    # An empty reason is refused by the command model itself.
    with pytest.raises(ValueError):
        AssignJobCommand(
            job_id=posted["job_id"], to_participant_id=gemini.participant.id, reason=""
        )


async def test_only_the_assignee_accepts_and_acceptance_is_idempotent(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    gemini = await join(room, display_name="Gemini")
    posted = await jobs.post(participant=codex.participant, command=_post())
    await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"], to_participant_id=codex.participant.id, reason="yours"
        ),
    )

    with pytest.raises(Forbidden):
        await jobs.accept(
            participant=gemini.participant, command=AcceptJobCommand(job_id=posted["job_id"])
        )

    first = await jobs.accept(
        participant=codex.participant,
        command=AcceptJobCommand(job_id=posted["job_id"], note="on it"),
    )
    assert first["state"] == JobState.ACCEPTED.value
    again = await jobs.accept(
        participant=codex.participant, command=AcceptJobCommand(job_id=posted["job_id"])
    )
    assert again["already_accepted"] is True


async def test_only_the_orchestrator_moves_priority(make_room, join):
    """Priority is the allocation decision; a seat that could raise its own would allocate."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    posted = await jobs.post(participant=codex.participant, command=_post())

    with pytest.raises(Forbidden):
        await jobs.update(
            participant=codex.participant,
            command=UpdateJobCommand(job_id=posted["job_id"], priority=100),
        )

    ranked = await jobs.update(
        participant=room.owner,
        command=UpdateJobCommand(job_id=posted["job_id"], priority=100),
    )
    assert ranked["changed"] == ["priority"]
    job = await jobs.get(room.room.id, posted["job_id"], with_history=False)
    assert job.priority == 100
    assert job.requested_urgency == 80, "what was asked for is still readable"


async def test_a_terminal_state_cannot_be_set_through_set_state(make_room, join):
    """One exit, because a second one would eventually be the one that forgot the reason."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    posted = await jobs.post(participant=codex.participant, command=_post())
    await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"], to_participant_id=codex.participant.id, reason="yours"
        ),
    )

    with pytest.raises(InvalidCommand) as exc:
        await jobs.set_state(
            participant=codex.participant,
            command=SetJobStateCommand(job_id=posted["job_id"], state=JobState.COMPLETED),
        )
    assert "close" in str(exc.value)


async def test_a_job_moves_through_live_states_and_points_at_its_task(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    posted = await jobs.post(participant=codex.participant, command=_post())
    await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"], to_participant_id=codex.participant.id, reason="yours"
        ),
    )
    await jobs.accept(
        participant=codex.participant, command=AcceptJobCommand(job_id=posted["job_id"])
    )
    task = await tasks.create(
        participant=codex.participant,
        command=CreateTaskCommand(title="Fix the reconnect path"),
    )

    active = await jobs.set_state(
        participant=codex.participant,
        command=SetJobStateCommand(job_id=posted["job_id"], state=JobState.ACTIVE, task_id=task.id),
    )
    assert active["state"] == JobState.ACTIVE.value
    job = await jobs.get(room.room.id, posted["job_id"], with_history=False)
    assert job.task_id == task.id

    blocked = await jobs.set_state(
        participant=codex.participant,
        command=SetJobStateCommand(
            job_id=posted["job_id"], state=JobState.BLOCKED, reason="waiting on the schema change"
        ),
    )
    assert blocked["state"] == JobState.BLOCKED.value


async def test_one_task_cannot_serve_two_jobs(make_room, join):
    """Two jobs on one lease would make "which intent is this serving" unanswerable."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    first = await jobs.post(participant=codex.participant, command=_post(title="First"))
    second = await jobs.post(participant=codex.participant, command=_post(title="Second"))
    for job_id in (first["job_id"], second["job_id"]):
        await jobs.assign(
            participant=room.owner,
            command=AssignJobCommand(
                job_id=job_id, to_participant_id=codex.participant.id, reason="yours"
            ),
        )
    task = await tasks.create(
        participant=codex.participant, command=CreateTaskCommand(title="One piece of work")
    )
    await jobs.set_state(
        participant=codex.participant,
        command=SetJobStateCommand(job_id=first["job_id"], state=JobState.ACTIVE, task_id=task.id),
    )

    with pytest.raises(InvalidCommand) as exc:
        await jobs.set_state(
            participant=codex.participant,
            command=SetJobStateCommand(
                job_id=second["job_id"], state=JobState.ACTIVE, task_id=task.id
            ),
        )
    assert exc.value.details["job_id"] == first["job_id"]


async def test_every_ending_records_a_reason_and_survives(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    posted = await jobs.post(participant=codex.participant, command=_post())
    await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"], to_participant_id=codex.participant.id, reason="yours"
        ),
    )

    closed = await jobs.close(
        participant=codex.participant,
        command=CloseJobCommand(
            job_id=posted["job_id"],
            state=JobState.COMPLETED,
            reason="worker output reviewed; tests green",
        ),
    )
    assert closed["state"] == JobState.COMPLETED.value
    assert closed["closed_at"]

    # Still fully readable, with its provenance and its whole history.
    job = await jobs.get(room.room.id, posted["job_id"])
    assert job.is_terminal
    assert job.terminal_reason == "worker output reviewed; tests green"
    assert job.terminated_by_participant_id == codex.participant.id
    assert job.human_instruction == HUMAN_WORDS
    assert [h.to_state for h in job.history] == [
        JobState.POSTED,
        JobState.ASSIGNED,
        JobState.COMPLETED,
    ]

    # And it cannot be reopened or re-closed.
    with pytest.raises(InvalidCommand):
        await jobs.close(
            participant=room.owner,
            command=CloseJobCommand(
                job_id=posted["job_id"], state=JobState.CANCELLED, reason="changed my mind"
            ),
        )
    with pytest.raises(InvalidCommand):
        await jobs.update(
            participant=room.owner,
            command=UpdateJobCommand(job_id=posted["job_id"], desired_outcome="different"),
        )


async def test_supersession_must_name_its_replacement(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    old = await jobs.post(participant=codex.participant, command=_post(title="Vague version"))
    new = await jobs.post(participant=codex.participant, command=_post(title="Better version"))

    with pytest.raises(InvalidCommand):
        await jobs.close(
            participant=room.owner,
            command=CloseJobCommand(
                job_id=old["job_id"], state=JobState.SUPERSEDED, reason="reformulated"
            ),
        )
    with pytest.raises(InvalidCommand):
        await jobs.close(
            participant=room.owner,
            command=CloseJobCommand(
                job_id=old["job_id"],
                state=JobState.SUPERSEDED,
                reason="itself",
                superseded_by_job_id=old["job_id"],
            ),
        )

    done = await jobs.close(
        participant=room.owner,
        command=CloseJobCommand(
            job_id=old["job_id"],
            state=JobState.SUPERSEDED,
            reason="reformulated with acceptance criteria",
            superseded_by_job_id=new["job_id"],
        ),
    )
    assert done["superseded_by_job_id"] == new["job_id"]


async def test_a_poster_may_withdraw_its_own_request(make_room, join):
    """Not an exercise of authority over anyone — it is their own request."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    gemini = await join(room, display_name="Gemini")
    posted = await jobs.post(participant=codex.participant, command=_post())

    with pytest.raises(Forbidden):
        await jobs.close(
            participant=gemini.participant,
            command=CloseJobCommand(
                job_id=posted["job_id"], state=JobState.CANCELLED, reason="not mine to cancel"
            ),
        )
    withdrawn = await jobs.close(
        participant=codex.participant,
        command=CloseJobCommand(
            job_id=posted["job_id"], state=JobState.CANCELLED, reason="my human changed their mind"
        ),
    )
    assert withdrawn["state"] == JobState.CANCELLED.value


async def test_two_concurrent_allocations_produce_one_winner(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    gemini = await join(room, display_name="Gemini")
    posted = await jobs.post(participant=codex.participant, command=_post())

    results = await asyncio.gather(
        jobs.assign(
            participant=room.owner,
            command=AssignJobCommand(
                job_id=posted["job_id"], to_participant_id=codex.participant.id, reason="left"
            ),
        ),
        jobs.assign(
            participant=room.owner,
            command=AssignJobCommand(
                job_id=posted["job_id"], to_participant_id=gemini.participant.id, reason="right"
            ),
        ),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    job = await jobs.get(room.room.id, posted["job_id"])

    # The guarantee is that no allocation is *lost*, not that a second one is refused:
    # reassignment is a legal act, and the guard is on the observed pre-state. So either
    # one landed and the other was refused, or the second saw the first and recorded it as
    # the previous owner. What must never happen is two allocations both believing the job
    # was unowned, which would leave the board with an assignee nobody chose.
    assert winners, results
    assert job.assigned_to_participant_id == winners[-1]["assigned_to_participant_id"]
    if len(winners) == 2:
        assert (
            winners[1]["previous_assignee_participant_id"]
            == (winners[0]["assigned_to_participant_id"])
        ), "the second allocation must have observed the first"
    transitions = [h for h in job.history if h.to_state is JobState.ASSIGNED]
    assert len(transitions) == len(winners), "one history row per allocation that landed"


async def test_the_board_reports_its_total_and_scopes_by_room(make_room, join):
    first = await make_room(name="First")
    second = await make_room(name="Second")
    codex = await join(first, display_name="Codex")
    for i in range(3):
        await jobs.post(participant=codex.participant, command=_post(title=f"Job {i}"))

    rows, total = await jobs.board_for_room(first.room.id, limit=2)
    assert total == 3
    assert len(rows) == 2, "truncated, and the total says so"

    other_rows, other_total = await jobs.board_for_room(second.room.id)
    assert other_total == 0 and other_rows == []

    with pytest.raises(NotFound):
        await jobs.get(second.room.id, rows[0].id)


async def test_an_unallocated_job_cannot_be_progressed(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    posted = await jobs.post(participant=codex.participant, command=_post())

    with pytest.raises((InvalidCommand, Forbidden)):
        await jobs.set_state(
            participant=codex.participant,
            command=SetJobStateCommand(job_id=posted["job_id"], state=JobState.ACTIVE),
        )

"""Workers and supervisor capacity (D-077, D-088).

A worker is the supervisor's account of something downstream. These tests are mostly about
what that means when it is inconvenient: a declaration is never presence, a completing
worker never completes the job, and a capacity a stale seat claims is not believed.
"""

from __future__ import annotations

import pytest

from app.core import jobs, presence, store, workers
from app.core.errors import Forbidden, InvalidCommand, NotFound
from app.domain.commands import (
    AssignJobCommand,
    FinishWorkerCommand,
    PostJobCommand,
    RegisterWorkerCommand,
    ReportCapacityCommand,
    UpdateWorkerCommand,
)
from app.domain.room import Liveness, MembershipState, ParticipantRole
from app.domain.worker import SupervisorCapacity, WorkerProvenance, WorkerState

pytestmark = pytest.mark.asyncio


def _register(**kwargs) -> RegisterWorkerCommand:
    payload = {
        "label": "backend-1",
        "display_name": "Backend worker",
        "assignment": "Implement the reconnect path and its test",
        "declared_runtime": "codex-cli",
        "declared_model": "gpt-6",
    }
    payload.update(kwargs)
    return RegisterWorkerCommand(**payload)


async def test_a_worker_belongs_to_exactly_one_supervisor(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")

    result = await workers.register(participant=codex.participant, command=_register())
    worker = await workers.get(room.room.id, result["worker_id"])

    assert worker.supervisor_participant_id == codex.participant.id
    assert worker.state is WorkerState.STARTING
    assert worker.provenance is WorkerProvenance.DECLARED
    assert worker.attachment_id is None, "a declared worker is not a runtime in this room"
    mine = await workers.workers_for(room.room.id, supervisor_participant_id=codex.participant.id)
    assert [w.id for w in mine] == [worker.id]


async def test_a_worker_records_the_goal_version_and_job_that_produced_it(make_room, join):
    """The provenance that stops stale work from completing a newer goal."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    posted = await jobs.post(
        participant=codex.participant,
        command=PostJobCommand(title="Fix reconnect", human_instruction="please fix it"),
    )
    await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"], to_participant_id=codex.participant.id, reason="yours"
        ),
    )

    result = await workers.register(
        participant=codex.participant,
        command=_register(related_job_id=posted["job_id"], created_by_goal_version=41),
    )
    worker = await workers.get(room.room.id, result["worker_id"])
    assert worker.related_job_id == posted["job_id"]
    assert worker.created_by_goal_version == 41

    under_41 = await workers.active_workers_under_goal_version(
        room.room.id, codex.participant.id, goal_version=41
    )
    assert [w.id for w in under_41] == [worker.id]
    under_42 = await workers.active_workers_under_goal_version(
        room.room.id, codex.participant.id, goal_version=42
    )
    assert under_42 == [], "a replacement can tell its own workers from the old goal's"


async def test_redeclaring_the_same_label_updates_rather_than_duplicating(make_room, join):
    """A restarted supervisor must not double the room's idea of its capacity."""
    room = await make_room()
    codex = await join(room, display_name="Codex")

    first = await workers.register(participant=codex.participant, command=_register())
    second = await workers.register(
        participant=codex.participant,
        command=_register(assignment="Same worker, new assignment"),
    )
    assert second["worker_id"] == first["worker_id"]
    assert second["redeclared"] is True

    all_workers = await workers.workers_for(room.room.id)
    assert len(all_workers) == 1
    assert all_workers[0].assignment == "Same worker, new assignment"
    assert all_workers[0].attempts == 1, "the re-declaration counts as another attempt"


async def test_another_seat_cannot_report_on_someone_elses_worker(make_room, join):
    """Reporting for a worker you do not own would forge attribution."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    gemini = await join(room, display_name="Gemini")
    mine = await workers.register(participant=codex.participant, command=_register())

    with pytest.raises(Forbidden):
        await workers.update_state(
            participant=gemini.participant,
            command=UpdateWorkerCommand(
                worker_id=mine["worker_id"], state=WorkerState.WORKING, summary="not mine"
            ),
        )
    with pytest.raises(Forbidden):
        await workers.finish(
            participant=gemini.participant,
            command=FinishWorkerCommand(
                worker_id=mine["worker_id"], state=WorkerState.COMPLETED, summary="not mine"
            ),
        )
    # Not even the orchestrator, which holds room.admin: stopping someone else's worker is
    # acting as them. It steers the supervisor instead.
    with pytest.raises(Forbidden):
        await workers.finish(
            participant=room.owner,
            command=FinishWorkerCommand(
                worker_id=mine["worker_id"], state=WorkerState.STOPPED, summary="admin override"
            ),
        )


async def test_a_waiting_worker_must_name_what_it_waits_on(make_room, join):
    """An unexplained wait is indistinguishable from a hang."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    mine = await workers.register(participant=codex.participant, command=_register())

    with pytest.raises(InvalidCommand):
        await workers.update_state(
            participant=codex.participant,
            command=UpdateWorkerCommand(worker_id=mine["worker_id"], state=WorkerState.WAITING),
        )

    ok = await workers.update_state(
        participant=codex.participant,
        command=UpdateWorkerCommand(
            worker_id=mine["worker_id"],
            state=WorkerState.WAITING,
            summary="paused on a dependency",
            waiting_reason="waiting for the schema migration in JOB-118",
        ),
    )
    assert ok["state"] == WorkerState.WAITING.value


async def test_a_terminal_state_cannot_be_set_through_update_state(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    mine = await workers.register(participant=codex.participant, command=_register())

    with pytest.raises(InvalidCommand) as exc:
        await workers.update_state(
            participant=codex.participant,
            command=UpdateWorkerCommand(
                worker_id=mine["worker_id"], state=WorkerState.COMPLETED, summary="done"
            ),
        )
    assert "finish" in str(exc.value)


async def test_a_completing_worker_does_not_complete_the_job(make_room, join):
    """The review gate. An executor may not mark the room's work done on its own say-so."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    posted = await jobs.post(
        participant=codex.participant,
        command=PostJobCommand(title="Fix reconnect", human_instruction="please fix it"),
    )
    await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"], to_participant_id=codex.participant.id, reason="yours"
        ),
    )
    mine = await workers.register(
        participant=codex.participant, command=_register(related_job_id=posted["job_id"])
    )

    finished = await workers.finish(
        participant=codex.participant,
        command=FinishWorkerCommand(
            worker_id=mine["worker_id"],
            state=WorkerState.COMPLETED,
            summary="implemented and tested",
            result_reference="ckp_123",
        ),
    )
    assert finished["awaiting_supervisor_review"] is True

    job = await jobs.get(room.room.id, posted["job_id"], with_history=False)
    assert not job.is_terminal, "the job is still open until its supervisor reviews the output"

    # Idempotent: reporting the same ending twice is not an error.
    again = await workers.finish(
        participant=codex.participant,
        command=FinishWorkerCommand(
            worker_id=mine["worker_id"], state=WorkerState.COMPLETED, summary="again"
        ),
    )
    assert again["already_finished"] is True


async def test_a_failed_worker_leaves_its_supervisor_untouched(make_room, join):
    """A worker is downstream: its failure is evidence, not a change to the seat."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    mine = await workers.register(participant=codex.participant, command=_register())

    await workers.finish(
        participant=codex.participant,
        command=FinishWorkerCommand(
            worker_id=mine["worker_id"],
            state=WorkerState.FAILED,
            summary="the test suite never terminated",
            result_reference="log:companion.log#L2201",
        ),
    )

    seat = await store.load_participant_for_room(room.room.id, codex.participant.id)
    assert seat.state is MembershipState.JOINED
    view = await presence.presence_for_room(await store.load_room(room.room.id))
    assert view[codex.participant.id].liveness is not Liveness.DISCONNECTED, (
        "a worker failing is not a presence change"
    )

    worker = await workers.get(room.room.id, mine["worker_id"])
    assert worker.state is WorkerState.FAILED
    assert worker.result_reference == "log:companion.log#L2201", "failure evidence is retained"


async def test_a_supervisor_can_replace_a_failed_worker(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    first = await workers.register(participant=codex.participant, command=_register())
    await workers.finish(
        participant=codex.participant,
        command=FinishWorkerCommand(
            worker_id=first["worker_id"], state=WorkerState.FAILED, summary="crashed"
        ),
    )

    # A finished worker is not revived; a fresh label is a fresh worker, and the failed one
    # stays readable as evidence.
    with pytest.raises(InvalidCommand):
        await workers.update_state(
            participant=codex.participant,
            command=UpdateWorkerCommand(
                worker_id=first["worker_id"], state=WorkerState.WORKING, summary="back from dead"
            ),
        )
    second = await workers.register(
        participant=codex.participant, command=_register(label="backend-2")
    )
    assert second["worker_id"] != first["worker_id"]
    assert len(await workers.workers_for(room.room.id)) == 2


async def test_an_observer_owns_no_workers(make_room, join):
    room = await make_room()
    watcher = await join(room, display_name="Watcher", role=ParticipantRole.OBSERVER)

    with pytest.raises(Forbidden):
        await workers.register(participant=watcher.participant, command=_register())


async def test_a_declared_worker_may_not_claim_an_attachment(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")

    with pytest.raises(InvalidCommand):
        await workers.register(
            participant=codex.participant,
            command=_register(attachment_id="att_someone_elses"),
        )


async def test_a_room_attachment_worker_must_own_its_attachment(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    gemini = await join(room, display_name="Gemini")

    with pytest.raises(InvalidCommand):
        await workers.register(
            participant=codex.participant,
            command=_register(provenance=WorkerProvenance.ROOM_ATTACHMENT),
        )
    with pytest.raises(Forbidden):
        await workers.register(
            participant=codex.participant,
            command=_register(
                provenance=WorkerProvenance.ROOM_ATTACHMENT,
                attachment_id=f"att_not_{gemini.participant.id}",
            ),
        )


async def test_offline_capacity_cannot_be_declared(make_room, join):
    """It is derived from liveness; a runtime that stopped beating cannot report itself gone."""
    room = await make_room()
    codex = await join(room, display_name="Codex")

    with pytest.raises(InvalidCommand) as exc:
        await workers.report_capacity(
            participant=codex.participant,
            command=ReportCapacityCommand(declared=SupervisorCapacity.OFFLINE),
        )
    assert "derived" in str(exc.value)


async def test_capacity_counts_come_from_rows_not_from_the_caller(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await workers.report_capacity(
        participant=codex.participant,
        command=ReportCapacityCommand(
            declared=SupervisorCapacity.AVAILABLE, max_concurrent_workers=3
        ),
    )
    for label in ("w1", "w2"):
        registered = await workers.register(
            participant=codex.participant, command=_register(label=label)
        )
        await workers.update_state(
            participant=codex.participant,
            command=UpdateWorkerCommand(
                worker_id=registered["worker_id"], state=WorkerState.WORKING, summary="busy"
            ),
        )
    posted = await jobs.post(
        participant=codex.participant,
        command=PostJobCommand(title="A job", human_instruction="do it"),
    )
    await jobs.assign(
        participant=room.owner,
        command=AssignJobCommand(
            job_id=posted["job_id"], to_participant_id=codex.participant.id, reason="yours"
        ),
    )

    report = await workers.capacity_for(room.room.id, codex.participant.id)
    assert report.declared is SupervisorCapacity.AVAILABLE
    assert report.active_workers == 2, "counted, not claimed"
    assert report.owned_jobs == 1
    assert report.free_slots == 1
    assert report.effective is SupervisorCapacity.AVAILABLE


async def test_capacity_reads_fully_allocated_when_the_slots_are_gone(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await workers.report_capacity(
        participant=codex.participant,
        command=ReportCapacityCommand(
            declared=SupervisorCapacity.AVAILABLE, max_concurrent_workers=1
        ),
    )
    registered = await workers.register(participant=codex.participant, command=_register())
    await workers.update_state(
        participant=codex.participant,
        command=UpdateWorkerCommand(
            worker_id=registered["worker_id"], state=WorkerState.WORKING, summary="busy"
        ),
    )

    report = await workers.capacity_for(room.room.id, codex.participant.id)
    assert report.effective is SupervisorCapacity.FULLY_ALLOCATED, "the declaration is overridden"
    assert report.declared is SupervisorCapacity.AVAILABLE, "and the claim is still readable"
    assert report.free_slots == 0

    # Finishing the worker frees the slot again, from the rows.
    await workers.finish(
        participant=codex.participant,
        command=FinishWorkerCommand(
            worker_id=registered["worker_id"], state=WorkerState.COMPLETED, summary="done"
        ),
    )
    after = await workers.capacity_for(room.room.id, codex.participant.id)
    assert after.active_workers == 0
    assert after.effective is SupervisorCapacity.AVAILABLE


async def test_a_stale_seat_never_advertises_availability(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await workers.report_capacity(
        participant=codex.participant,
        command=ReportCapacityCommand(
            declared=SupervisorCapacity.AVAILABLE, max_concurrent_workers=4
        ),
    )

    for grade in (Liveness.STALE, Liveness.DISCONNECTED):
        report = await workers.capacity_for(room.room.id, codex.participant.id, liveness=grade)
        assert report.effective is SupervisorCapacity.OFFLINE, grade
        assert report.declared is SupervisorCapacity.AVAILABLE


async def test_blocked_survives_free_slots(make_room, join):
    """Capacity is a judgement, not arithmetic on a slot count."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await workers.report_capacity(
        participant=codex.participant,
        command=ReportCapacityCommand(
            declared=SupervisorCapacity.BLOCKED,
            max_concurrent_workers=4,
            note="waiting on the migration nobody has merged",
        ),
    )
    report = await workers.capacity_for(room.room.id, codex.participant.id)
    assert report.free_slots == 4
    assert report.effective is SupervisorCapacity.BLOCKED


async def test_workers_and_capacity_are_room_scoped(make_room, join):
    first = await make_room(name="First")
    second = await make_room(name="Second")
    codex = await join(first, display_name="Codex")
    mine = await workers.register(participant=codex.participant, command=_register())

    assert await workers.workers_for(second.room.id) == []
    with pytest.raises(NotFound):
        await workers.get(second.room.id, mine["worker_id"])

    summary = await workers.worker_summary_for_room(first.room.id)
    assert summary[codex.participant.id][WorkerState.STARTING.value] == 1
    assert await workers.worker_summary_for_room(second.room.id) == {}

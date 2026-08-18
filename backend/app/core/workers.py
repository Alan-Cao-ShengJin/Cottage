"""Workers and supervisor capacity (D-077, D-088).

A worker is **downstream**. It is not a participant, holds no scopes, and the room has
never seen it — everything recorded here is its supervisor's account of an executor that
supervisor is accountable for. D-077 settled that boundary, and three things keep it:
membership has exactly one entry path, one provisioned companion must not show the room N
seats, and a worker's authority is its supervisor's.

So the honesty rule from principle 5 applies one level down. `state` is a *declaration*: a
worker that dies silently stays `working` until its supervisor notices, which is why every
reader shows `last_activity_at` beside it and why nothing here ever feeds presence.

**Capacity is a judgement plus a count.** "Two workers running" says nothing about whether a
third would help — the supervisor may be blocked, its host saturated, its goal serial. So the
seat declares what it can take and the room counts the rows itself, and both travel together.
`offline` is the one value a caller may never declare: it is derived from liveness, because a
runtime that has stopped beating cannot be trusted to report that it is gone.
"""

from __future__ import annotations

from typing import Any

from ..db import database as db
from ..domain import ids
from ..domain.commands import (
    FinishWorkerCommand,
    RegisterWorkerCommand,
    ReportCapacityCommand,
    UpdateWorkerCommand,
)
from ..domain.events import EventType
from ..domain.job import OWNED_JOB_STATES
from ..domain.room import Liveness, MembershipState, Participant, RoomRole, Scope
from ..domain.worker import (
    ACTIVE_WORKER_STATES,
    TERMINAL_WORKER_STATES,
    CapacityReport,
    SupervisorCapacity,
    Worker,
    WorkerProvenance,
    WorkerState,
)
from ..util import utcnow_iso
from . import authz, eventlog, privacy, roles, store
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import Forbidden, InvalidCommand, NotFound

#: Room positions that may own workers. An observer is excluded because it has already told
#: the room it is not here to work.
WORKER_OWNING_ROLES: tuple[RoomRole, ...] = (RoomRole.ORCHESTRATOR, RoomRole.SUPERVISOR)

#: Non-terminal states `update_state` may set. Terminal states belong to `finish`, which is
#: the path that stamps a completion time and a result reference.
LIVE_WORKER_STATES: frozenset[WorkerState] = frozenset(
    {WorkerState.STARTING, WorkerState.WORKING, WorkerState.WAITING, WorkerState.STOPPING}
)

#: Liveness grades at which a declared capacity may no longer be believed.
_UNTRUSTED_LIVENESS: frozenset[Liveness] = frozenset({Liveness.DISCONNECTED, Liveness.STALE})


def _to_worker(row: Any) -> Worker:
    return Worker(
        id=row["id"],
        room_id=row["room_id"],
        supervisor_participant_id=row["supervisor_participant_id"],
        supervisor_attachment_id=row["supervisor_attachment_id"],
        label=row["label"],
        display_name=row["display_name"],
        provenance=WorkerProvenance(row["provenance"]),
        attachment_id=row["attachment_id"],
        assignment=row["assignment"],
        related_job_id=row["related_job_id"],
        related_task_id=row["related_task_id"],
        related_work_id=row["related_work_id"],
        created_by_goal_version=(
            int(row["created_by_goal_version"])
            if row["created_by_goal_version"] is not None
            else None
        ),
        declared_runtime=row["declared_runtime"],
        declared_model=row["declared_model"],
        state=WorkerState(row["state"]),
        summary=row["summary"],
        waiting_reason=row["waiting_reason"],
        result_reference=row["result_reference"],
        attempts=int(row["attempts"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        last_activity_at=row["last_activity_at"],
        completed_at=row["completed_at"],
        retired_at=row["retired_at"],
    )


async def workers_for(
    room_id: str,
    *,
    supervisor_participant_id: str | None = None,
    include_retired: bool = False,
) -> list[Worker]:
    clauses = ["room_id = ?"]
    params: list[Any] = [room_id]
    if supervisor_participant_id:
        clauses.append("supervisor_participant_id = ?")
        params.append(supervisor_participant_id)
    if not include_retired:
        clauses.append("retired_at IS NULL")
    rows = await db.fetch_all(
        f"SELECT * FROM workers WHERE {' AND '.join(clauses)} ORDER BY created_at ASC", params
    )
    return [_to_worker(r) for r in rows]


async def get(room_id: str, worker_id: str) -> Worker:
    row = await db.fetch_one(
        "SELECT * FROM workers WHERE id = ? AND room_id = ?", (worker_id, room_id)
    )
    if row is None:
        raise NotFound("No such worker in this room.", worker_id=worker_id)
    return _to_worker(row)


async def _require_worker_owner(participant: Participant, worker_id: str) -> Worker:
    """Load a worker and refuse a caller that does not own it.

    `require_owns` rather than an admin check: reporting on someone else's worker would
    forge attribution, and attribution is the only integrity guarantee the room has. A room
    admin that wants a worker stopped steers its supervisor.
    """
    worker = await get(participant.room_id, worker_id)
    authz.require_owns(participant, worker.supervisor_participant_id, what="worker")
    return worker


async def register(*, participant: Participant, command: RegisterWorkerCommand) -> dict[str, Any]:
    """Declare a worker this seat owns and answers for.

    `label` is stable, so a supervisor that restarts and re-declares its pool lands on the
    same rows rather than doubling the room's idea of its capacity — the same rule an
    attachment label follows, for the same reason.

    `created_by_goal_version` is recorded as given. That is the provenance which stops stale
    work from completing a newer goal: output from a worker spawned under v41 keeps saying
    so after v42 lands.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    authz.require_scope(participant, Scope.WORK_DECLARE)

    caller_role = await roles.role_for(participant)
    if caller_role not in WORKER_OWNING_ROLES:
        raise Forbidden(
            f"A {caller_role.value} does not own workers. Only a supervisor or the "
            "orchestrator delegates execution.",
            room_role=caller_role.value,
        )

    # The pairing the schema deliberately does not CHECK, asserted here instead: a CHECK
    # referencing `attachment_id` would block a room purge, because the attachment's
    # ON DELETE SET NULL would then violate it.
    if command.provenance is WorkerProvenance.ROOM_ATTACHMENT:
        if not command.attachment_id:
            raise InvalidCommand(
                "A room_attachment worker must name the attachment it runs as.",
                provenance=command.provenance.value,
            )
        owned = await db.fetch_one(
            "SELECT id FROM attachments WHERE id = ? AND participant_id = ?",
            (command.attachment_id, participant.id),
        )
        if owned is None:
            raise Forbidden(
                "That attachment is not a runtime of your own seat.",
                attachment_id=command.attachment_id,
            )
        clash = await db.fetch_one(
            "SELECT id FROM workers WHERE attachment_id = ? AND retired_at IS NULL AND label <> ?",
            (command.attachment_id, command.label),
        )
        if clash is not None:
            raise InvalidCommand(
                "That runtime is already declared as another worker; one runtime is one "
                "worker, or the board would believe it had twice the capacity it has.",
                attachment_id=command.attachment_id,
                worker_id=clash["id"],
            )
    elif command.attachment_id:
        raise InvalidCommand(
            "A declared worker lives behind its supervisor, so it has no attachment in "
            "this room. Use provenance=room_attachment if it really is a runtime here.",
            provenance=command.provenance.value,
        )

    if command.related_job_id:
        job_row = await db.fetch_one(
            "SELECT id FROM jobs WHERE id = ? AND room_id = ?", (command.related_job_id, room.id)
        )
        if job_row is None:
            raise NotFound("No such job in this room.", job_id=command.related_job_id)
    if command.related_task_id:
        task_row = await db.fetch_one(
            "SELECT id FROM tasks WHERE id = ? AND room_id = ?",
            (command.related_task_id, room.id),
        )
        if task_row is None:
            raise NotFound("No such task in this room.", task_id=command.related_task_id)

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[command.assignment, command.display_name],
        known_participant_ids=known,
    )

    existing = await db.fetch_one(
        "SELECT id FROM workers WHERE supervisor_participant_id = ? AND label = ?",
        (participant.id, command.label),
    )
    worker_id = existing["id"] if existing is not None else ids.new_id(ids.WORKER)
    # Which runtime of this seat is spawning, when the caller named its connection. Recorded
    # so a restarted supervisor can tell its own workers from a previous run's; NULL means
    # the caller did not say, never that there is no runtime (D-034).
    supervisor_attachment_id: str | None = None
    if command.connection_id:
        conn = await db.fetch_one(
            "SELECT attachment_id FROM connections WHERE id = ? AND room_id = ? "
            "AND participant_id = ? AND closed_at IS NULL",
            (command.connection_id, room.id, participant.id),
        )
        if conn is None:
            raise InvalidCommand(
                "That connection is not an open connection of your own seat.",
                connection_id=command.connection_id,
            )
        supervisor_attachment_id = conn["attachment_id"]
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        # An upsert on the stable label, so a restart re-declares rather than duplicates.
        # The UNIQUE(supervisor_participant_id, label) constraint is what makes this safe
        # under a concurrent second declaration: one insert wins, the other updates.
        await tx.execute(
            """
            INSERT INTO workers (
                id, room_id, supervisor_participant_id, supervisor_attachment_id,
                attachment_id, label, display_name, provenance, assignment, related_job_id,
                related_task_id, related_work_id, created_by_goal_version, declared_runtime,
                declared_model, state, summary, created_at, last_activity_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(supervisor_participant_id, label) DO UPDATE SET
                supervisor_attachment_id = excluded.supervisor_attachment_id,
                attachment_id = excluded.attachment_id,
                display_name = excluded.display_name,
                provenance = excluded.provenance,
                assignment = excluded.assignment,
                related_job_id = excluded.related_job_id,
                related_task_id = excluded.related_task_id,
                related_work_id = excluded.related_work_id,
                created_by_goal_version = excluded.created_by_goal_version,
                declared_runtime = excluded.declared_runtime,
                declared_model = excluded.declared_model,
                state = excluded.state,
                summary = excluded.summary,
                waiting_reason = '',
                result_reference = '',
                attempts = workers.attempts + 1,
                completed_at = NULL,
                retired_at = NULL,
                last_activity_at = excluded.last_activity_at
            """,
            (
                worker_id,
                room.id,
                participant.id,
                supervisor_attachment_id,
                command.attachment_id,
                command.label,
                command.display_name,
                command.provenance.value,
                command.assignment,
                command.related_job_id,
                command.related_task_id,
                command.related_work_id,
                command.created_by_goal_version,
                command.declared_runtime,
                command.declared_model,
                WorkerState.STARTING.value,
                command.assignment[:200],
                now,
                now,
            ),
        )
        row = await tx.fetch_one(
            "SELECT id FROM workers WHERE supervisor_participant_id = ? AND label = ?",
            (participant.id, command.label),
        )
        resolved_id = row["id"] if row is not None else worker_id
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.WORKER_REGISTERED,
            actor=actor_for(participant),
            payload={
                "worker_id": resolved_id,
                "supervisor_participant_id": participant.id,
                "label": command.label,
                "display_name": command.display_name,
                "provenance": command.provenance.value,
                "attachment_id": command.attachment_id,
                "assignment": command.assignment,
                "related_job_id": command.related_job_id,
                "related_task_id": command.related_task_id,
                "created_by_goal_version": command.created_by_goal_version,
                "declared_runtime": command.declared_runtime,
                "declared_model": command.declared_model,
                "redeclared": existing is not None,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={
                "worker_id": resolved_id,
                "label": command.label,
                "state": WorkerState.STARTING.value,
                "redeclared": existing is not None,
            },
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="worker.register",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def update_state(*, participant: Participant, command: UpdateWorkerCommand) -> dict[str, Any]:
    """Report a worker's non-terminal state. The supervisor's claim, never presence."""
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    authz.require_scope(participant, Scope.WORK_DECLARE)
    worker = await _require_worker_owner(participant, command.worker_id)

    if command.state not in LIVE_WORKER_STATES:
        raise InvalidCommand(
            f"`{command.state.value}` ends a worker; use finish so the result is recorded.",
            state=command.state.value,
            live_states=sorted(s.value for s in LIVE_WORKER_STATES),
        )
    if worker.state in TERMINAL_WORKER_STATES:
        raise InvalidCommand(
            "That worker has already finished; register a new one instead of reviving it.",
            worker_id=worker.id,
            state=worker.state.value,
        )
    if command.state is WorkerState.WAITING and not command.waiting_reason.strip():
        # The same rule `runtime_state` applies to a runtime posture, for the same reason:
        # an unexplained wait is indistinguishable from a hang.
        raise InvalidCommand(
            "A waiting worker must name what it is waiting on.", worker_id=worker.id
        )

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[command.summary, command.waiting_reason],
        known_participant_ids=known,
    )
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE workers SET state = ?, summary = ?, waiting_reason = ?, "
            "started_at = COALESCE(started_at, ?), last_activity_at = ? "
            "WHERE id = ? AND room_id = ? AND supervisor_participant_id = ? "
            "AND completed_at IS NULL",
            (
                command.state.value,
                command.summary,
                command.waiting_reason,
                now if command.state is WorkerState.WORKING else None,
                now,
                worker.id,
                room.id,
                participant.id,
            ),
        )
        if affected == 0:
            raise InvalidCommand(
                "That worker finished while this report was in flight.", worker_id=worker.id
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.WORKER_STATE_CHANGED,
            actor=actor_for(participant),
            payload={
                "worker_id": worker.id,
                "state": command.state.value,
                "previous_state": worker.state.value,
                "summary": command.summary,
                "waiting_reason": command.waiting_reason,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={"worker_id": worker.id, "state": command.state.value}, events=[event]
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="worker.update_state",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def finish(*, participant: Participant, command: FinishWorkerCommand) -> dict[str, Any]:
    """End a worker: completed, failed or stopped.

    **A worker completing does not complete the job.** Its supervisor still reviews the
    output and may accept it, ask for rework, replace the worker, or escalate. That review
    gate is the reason `worker.finished` and `job.closed` are different events issued by
    different acts — collapsing them would let an executor mark the room's work done on its
    own say-so, which is the authorization defect D-026 records.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    authz.require_scope(participant, Scope.WORK_DECLARE)
    worker = await _require_worker_owner(participant, command.worker_id)

    if command.state not in TERMINAL_WORKER_STATES:
        raise InvalidCommand(
            f"`{command.state.value}` is not an ending; use update_state.",
            state=command.state.value,
            terminal_states=sorted(s.value for s in TERMINAL_WORKER_STATES),
        )
    if worker.state is command.state and worker.completed_at is not None:
        return {
            "worker_id": worker.id,
            "state": worker.state.value,
            "completed_at": worker.completed_at,
            "already_finished": True,
        }

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[command.summary, command.result_reference],
        known_participant_ids=known,
    )
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE workers SET state = ?, summary = ?, result_reference = ?, "
            "waiting_reason = '', completed_at = ?, last_activity_at = ? "
            "WHERE id = ? AND room_id = ? AND supervisor_participant_id = ? "
            "AND completed_at IS NULL",
            (
                command.state.value,
                command.summary,
                command.result_reference,
                now,
                now,
                worker.id,
                room.id,
                participant.id,
            ),
        )
        if affected == 0:
            raise InvalidCommand(
                "That worker has already finished.",
                worker_id=worker.id,
                state=worker.state.value,
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.WORKER_FINISHED,
            actor=actor_for(participant),
            payload={
                "worker_id": worker.id,
                "state": command.state.value,
                "summary": command.summary,
                "result_reference": command.result_reference,
                "attempts": worker.attempts,
                "created_by_goal_version": worker.created_by_goal_version,
                "related_job_id": worker.related_job_id,
                # Said explicitly on the event, because a reader watching a room needs to
                # know this is not the job finishing.
                "awaiting_supervisor_review": command.state is WorkerState.COMPLETED,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={
                "worker_id": worker.id,
                "state": command.state.value,
                "completed_at": now,
                "awaiting_supervisor_review": command.state is WorkerState.COMPLETED,
            },
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="worker.finish",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def _counts_for(room_id: str, participant_id: str) -> tuple[int, int, int]:
    """Active workers, blocked workers, owned jobs — counted from rows, never accepted."""
    active = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM workers WHERE room_id = ? AND supervisor_participant_id = ? "
            "AND retired_at IS NULL AND state IN ("
            + ",".join("?" for _ in ACTIVE_WORKER_STATES)
            + ")",
            [room_id, participant_id, *(s.value for s in ACTIVE_WORKER_STATES)],
        )
        or 0
    )
    blocked = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM workers WHERE room_id = ? AND supervisor_participant_id = ? "
            "AND retired_at IS NULL AND state = ?",
            (room_id, participant_id, WorkerState.WAITING.value),
        )
        or 0
    )
    owned = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM jobs WHERE room_id = ? AND assigned_to_participant_id = ? "
            "AND state IN (" + ",".join("?" for _ in OWNED_JOB_STATES) + ")",
            [room_id, participant_id, *(s.value for s in OWNED_JOB_STATES)],
        )
        or 0
    )
    return active, blocked, owned


async def capacity_for(
    room_id: str, participant_id: str, *, liveness: Liveness | None = None
) -> CapacityReport:
    """What this supervisor says it can take, beside what the room counted.

    `effective` is what an orchestrator should allocate against, and it is deliberately not
    just the declaration: a seat whose runtime stopped beating cannot advertise
    availability, and a seat with no free slots is fully allocated whatever it claimed. The
    declaration is still returned, so a reader can see the two disagree.
    """
    row = await db.fetch_one(
        "SELECT * FROM supervisor_capacity WHERE room_id = ? AND participant_id = ?",
        (room_id, participant_id),
    )
    declared = (
        SupervisorCapacity(row["declared"]) if row is not None else SupervisorCapacity.AVAILABLE
    )
    max_workers = int(row["max_concurrent_workers"]) if row is not None else 1
    active, blocked, owned = await _counts_for(room_id, participant_id)

    if liveness is not None and liveness in _UNTRUSTED_LIVENESS:
        effective = SupervisorCapacity.OFFLINE
    elif declared is SupervisorCapacity.BLOCKED:
        effective = SupervisorCapacity.BLOCKED
    elif max_workers and active >= max_workers:
        effective = SupervisorCapacity.FULLY_ALLOCATED
    else:
        effective = declared

    return CapacityReport(
        room_id=room_id,
        supervisor_participant_id=participant_id,
        declared=declared,
        max_concurrent_workers=max_workers,
        note=row["note"] if row is not None else "",
        declared_at=row["declared_at"] if row is not None else None,
        active_workers=active,
        blocked_workers=blocked,
        owned_jobs=owned,
        effective=effective,
    )


async def report_capacity(
    *, participant: Participant, command: ReportCapacityCommand
) -> dict[str, Any]:
    """Declare how much more this seat can take on."""
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    authz.require_scope(participant, Scope.WORK_DECLARE)

    if command.declared is SupervisorCapacity.OFFLINE:
        raise InvalidCommand(
            "`offline` is derived from your connections, not declared — a runtime that has "
            "stopped beating cannot report that it is gone. Say `blocked` if you cannot "
            "make progress.",
            declared=command.declared.value,
        )

    privacy.inspect_content(command.note, max_text_chars=2000)
    now = utcnow_iso()
    active, blocked, owned = await _counts_for(room.id, participant.id)

    async def body(tx: db.Tx) -> CommandOutcome:
        await tx.execute(
            "INSERT INTO supervisor_capacity (participant_id, room_id, declared, "
            "max_concurrent_workers, note, declared_at, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(participant_id) DO UPDATE SET declared = excluded.declared, "
            "max_concurrent_workers = excluded.max_concurrent_workers, "
            "note = excluded.note, declared_at = excluded.declared_at, "
            "updated_at = excluded.updated_at",
            (
                participant.id,
                room.id,
                command.declared.value,
                command.max_concurrent_workers,
                command.note,
                now,
                now,
            ),
        )
        effective = (
            SupervisorCapacity.FULLY_ALLOCATED
            if command.max_concurrent_workers and active >= command.max_concurrent_workers
            else command.declared
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.CAPACITY_CHANGED,
            actor=actor_for(participant),
            payload={
                "participant_id": participant.id,
                "declared": command.declared.value,
                "effective": effective.value,
                "max_concurrent_workers": command.max_concurrent_workers,
                "active_workers": active,
                "owned_jobs": owned,
                "blocked_workers": blocked,
                "note": command.note,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={
                "participant_id": participant.id,
                "declared": command.declared.value,
                "effective": effective.value,
                "active_workers": active,
                "owned_jobs": owned,
            },
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="supervisor.capacity.report",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def worker_summary_for_room(room_id: str) -> dict[str, dict[str, int]]:
    """Per-supervisor worker counts by state, for projections that show the hierarchy."""
    rows = await db.fetch_all(
        "SELECT supervisor_participant_id, state, COUNT(*) AS n FROM workers "
        "WHERE room_id = ? AND retired_at IS NULL GROUP BY supervisor_participant_id, state",
        (room_id,),
    )
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        out.setdefault(row["supervisor_participant_id"], {})[row["state"]] = int(row["n"])
    return out


async def active_workers_under_goal_version(
    room_id: str, participant_id: str, *, goal_version: int
) -> list[Worker]:
    """Workers a superseded goal version spawned, so a replacement can act on them (§24)."""
    rows = await db.fetch_all(
        "SELECT * FROM workers WHERE room_id = ? AND supervisor_participant_id = ? "
        "AND created_by_goal_version = ? AND retired_at IS NULL AND state IN ("
        + ",".join("?" for _ in ACTIVE_WORKER_STATES)
        + ")",
        [room_id, participant_id, goal_version, *(s.value for s in ACTIVE_WORKER_STATES)],
    )
    return [_to_worker(r) for r in rows]


async def _joined(room_id: str, participant_id: str) -> Participant:
    seat = await store.load_participant_for_room(room_id, participant_id)
    if seat.state is not MembershipState.JOINED:
        raise InvalidCommand("That seat is not active in this room.", participant_id=participant_id)
    return seat

"""The job board: durable human intent, and where it went (D-088).

A **job** is what a human asked for, recorded so it cannot quietly disappear. A **task** is
the lease-bearing unit that eventually does it. `tasks` keeps the fence, the lease and the
executor — the room must never have two answers to "who holds this" — and `jobs` keeps
intent, provenance and allocation. A job points at its task; it never duplicates one.

Two product rules are enforced here rather than documented:

**A supervisor receiving a request does not thereby own the work.** `post` is available to
any working seat, and it is the *only* thing a supervisor does with its human's request.
Allocation is `assign`, which is orchestrator-only. Without that split a room is N private
queues that happen to share a log, and the supervisor whose human shouted loudest wins.

**Nothing deletes a job.** Every terminal transition carries an attributable reason, and the
row plus its `job_events` history survive it. `close` is the only exit, and the schema
refuses a terminal state with no reason — so an implementation that forgot would fail
loudly rather than silently losing why something was dropped.
"""

from __future__ import annotations

from typing import Any

from ..db import database as db
from ..domain import ids
from ..domain.commands import (
    AcceptJobCommand,
    AssignJobCommand,
    CloseJobCommand,
    PostJobCommand,
    SetJobStateCommand,
    UpdateJobCommand,
)
from ..domain.events import EventType
from ..domain.job import (
    OWNED_JOB_STATES,
    TERMINAL_JOB_STATES,
    Job,
    JobEvent,
    JobOrigin,
    JobState,
)
from ..domain.room import MembershipState, Participant, PrivacyClass, Scope
from ..util import utcnow_iso
from . import authz, eventlog, privacy, roles, store
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import Forbidden, InvalidCommand, NotFound

#: Board page size. The untruncated total always travels with the rows (D-043).
DEFAULT_BOARD_LIMIT = 50

#: Non-terminal states `set_state` may move a job to. Terminal moves belong to `close`,
#: which is the only path that records a reason.
LIVE_STATES: frozenset[JobState] = frozenset({JobState.ACTIVE, JobState.PAUSED, JobState.BLOCKED})


def _to_job(row: Any, history: tuple[JobEvent, ...] = ()) -> Job:
    return Job(
        id=row["id"],
        room_id=row["room_id"],
        title=row["title"],
        desired_outcome=row["desired_outcome"],
        human_instruction=row["human_instruction"],
        room_goal_relationship=row["room_goal_relationship"],
        constraints=tuple(db.str_list(row["constraints_json"])),
        acceptance_criteria=tuple(db.str_list(row["acceptance_criteria"])),
        targets=tuple(db.str_list(row["targets"])),
        requested_urgency=int(row["requested_urgency"]),
        priority=int(row["priority"]),
        state=JobState(row["state"]),
        origin=JobOrigin(row["origin"]),
        posted_by_participant_id=row["posted_by_participant_id"],
        on_behalf_of_participant_id=row["on_behalf_of_participant_id"],
        source_goal_id=row["source_goal_id"],
        source_goal_version=(
            int(row["source_goal_version"]) if row["source_goal_version"] is not None else None
        ),
        parent_job_id=row["parent_job_id"],
        assigned_to_participant_id=row["assigned_to_participant_id"],
        assigned_by_participant_id=row["assigned_by_participant_id"],
        assigned_at=row["assigned_at"],
        accepted_at=row["accepted_at"],
        assigned_goal_version=(
            int(row["assigned_goal_version"]) if row["assigned_goal_version"] is not None else None
        ),
        task_id=row["task_id"],
        terminal_reason=row["terminal_reason"],
        terminated_by_participant_id=row["terminated_by_participant_id"],
        superseded_by_job_id=row["superseded_by_job_id"],
        privacy_class=PrivacyClass(row["privacy_class"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
        history=history,
    )


def _to_job_event(row: Any) -> JobEvent:
    return JobEvent(
        job_id=row["job_id"],
        room_id=row["room_id"],
        ordinal=int(row["ordinal"]),
        from_state=JobState(row["from_state"]) if row["from_state"] else None,
        to_state=JobState(row["to_state"]),
        actor_participant_id=row["actor_participant_id"],
        reason=row["reason"],
        seq=int(row["seq"]),
        created_at=row["created_at"],
    )


async def _record_transition_tx(
    tx: db.Tx,
    *,
    job_id: str,
    room_id: str,
    from_state: JobState | None,
    to_state: JobState,
    actor_participant_id: str | None,
    reason: str,
    seq: int,
) -> int:
    """Append one history row. Ordinal is allocated from the rows already there.

    A `MAX(ordinal) + 1` inside the mutating transaction rather than a counter column: the
    primary key `(job_id, ordinal)` is what actually prevents a duplicate, so a second
    writer loses the insert instead of overwriting a transition.
    """
    ordinal = int(
        await tx.fetch_value(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM job_events WHERE job_id = ?",
            (job_id,),
        )
        or 1
    )
    await tx.execute(
        "INSERT INTO job_events (job_id, ordinal, room_id, from_state, to_state, "
        "actor_participant_id, reason, seq, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            ordinal,
            room_id,
            from_state.value if from_state else None,
            to_state.value,
            actor_participant_id,
            reason,
            seq,
            utcnow_iso(),
        ),
    )
    return ordinal


async def get(room_id: str, job_id: str, *, with_history: bool = True) -> Job:
    """Load one job, scoped to its room so an id from elsewhere is simply absent."""
    row = await db.fetch_one("SELECT * FROM jobs WHERE id = ? AND room_id = ?", (job_id, room_id))
    if row is None:
        raise NotFound("No such job in this room.", job_id=job_id)
    history: tuple[JobEvent, ...] = ()
    if with_history:
        rows = await db.fetch_all(
            "SELECT * FROM job_events WHERE job_id = ? ORDER BY ordinal ASC", (job_id,)
        )
        history = tuple(_to_job_event(r) for r in rows)
    return _to_job(row, history)


async def board_for_room(
    room_id: str,
    *,
    states: tuple[JobState, ...] | None = None,
    assignee_participant_id: str | None = None,
    limit: int = DEFAULT_BOARD_LIMIT,
) -> tuple[list[Job], int]:
    """The board, highest priority first, with the untruncated total beside it."""
    clauses = ["room_id = ?"]
    params: list[Any] = [room_id]
    if states:
        clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
        params.extend(s.value for s in states)
    if assignee_participant_id:
        clauses.append("assigned_to_participant_id = ?")
        params.append(assignee_participant_id)
    where = " AND ".join(clauses)
    total = int(await db.fetch_value(f"SELECT COUNT(*) FROM jobs WHERE {where}", params) or 0)
    rows = await db.fetch_all(
        f"SELECT * FROM jobs WHERE {where} ORDER BY priority DESC, created_at ASC LIMIT ?",
        [*params, max(1, limit)],
    )
    return [_to_job(r) for r in rows], total


async def jobs_for_participant(room_id: str, participant_id: str) -> list[Job]:
    """Jobs this seat is currently accountable for."""
    rows = await db.fetch_all(
        "SELECT * FROM jobs WHERE room_id = ? AND assigned_to_participant_id = ? "
        "AND state IN (" + ",".join("?" for _ in OWNED_JOB_STATES) + ") "
        "ORDER BY priority DESC, created_at ASC",
        [room_id, participant_id, *(s.value for s in OWNED_JOB_STATES)],
    )
    return [_to_job(r) for r in rows]


async def _joined_participant(room_id: str, participant_id: str) -> Participant:
    try:
        seat = await store.load_participant_for_room(room_id, participant_id)
    except NotFound:
        raise NotFound(
            "That participant is not in this room.", participant_id=participant_id
        ) from None
    if seat.state is not MembershipState.JOINED:
        raise InvalidCommand(
            "That seat is not an active participant of this room.",
            participant_id=participant_id,
            state=seat.state.value,
        )
    return seat


async def post(*, participant: Participant, command: PostJobCommand) -> dict[str, Any]:
    """Put durable human intent on the board.

    This is what a supervisor does with a request from its human — *not* start working. The
    orchestrator then evaluates it against the room's other jobs, its dependencies, and who
    actually has capacity. The poster may say how urgent it thinks the request is; where it
    ranks is `priority`, which the orchestrator owns. Both are kept so a supervisor can see
    that its request was ranked rather than ignored.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    authz.require_scope(participant, Scope.TASK_PROPOSE)

    if command.on_behalf_of_participant_id:
        await _joined_participant(room.id, command.on_behalf_of_participant_id)
    if command.parent_job_id:
        await get(room.id, command.parent_job_id, with_history=False)

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[
            command.title,
            command.desired_outcome,
            command.human_instruction,
            command.room_goal_relationship,
            *command.constraints,
            *command.acceptance_criteria,
        ],
        known_participant_ids=known,
    )

    job_id = ids.new_id(ids.JOB)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        await tx.execute(
            """
            INSERT INTO jobs (
                id, room_id, title, desired_outcome, human_instruction,
                room_goal_relationship, constraints_json, acceptance_criteria, targets,
                requested_urgency, priority, state, origin, posted_by_participant_id,
                on_behalf_of_participant_id, source_goal_id, source_goal_version,
                parent_job_id, privacy_class, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                room.id,
                command.title,
                command.desired_outcome,
                command.human_instruction,
                command.room_goal_relationship,
                db.dumps(list(command.constraints)),
                db.dumps(list(command.acceptance_criteria)),
                db.dumps(list(command.targets)),
                command.requested_urgency,
                # Not seeded from requested_urgency: the requester's urgency is a claim,
                # and the board's ranking is the orchestrator's decision. Copying one into
                # the other would let whoever asks loudest set room priority.
                0,
                JobState.POSTED.value,
                command.origin.value,
                participant.id,
                command.on_behalf_of_participant_id,
                command.source_goal_id,
                command.source_goal_version,
                command.parent_job_id,
                decision.privacy_class.value,
                now,
                now,
            ),
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.JOB_POSTED,
            actor=actor_for(participant),
            payload={
                "job_id": job_id,
                "title": command.title,
                "desired_outcome": command.desired_outcome,
                "human_instruction": command.human_instruction,
                "on_behalf_of_participant_id": command.on_behalf_of_participant_id,
                "origin": command.origin.value,
                "requested_urgency": command.requested_urgency,
                "targets": list(command.targets),
                "constraints": list(command.constraints),
                "acceptance_criteria": list(command.acceptance_criteria),
                "source_goal_id": command.source_goal_id,
                "source_goal_version": command.source_goal_version,
                "parent_job_id": command.parent_job_id,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        await _record_transition_tx(
            tx,
            job_id=job_id,
            room_id=room.id,
            from_state=None,
            to_state=JobState.POSTED,
            actor_participant_id=participant.id,
            reason=command.human_instruction or command.title,
            seq=event.seq,
        )
        return CommandOutcome(
            result={"job_id": job_id, "state": JobState.POSTED.value}, events=[event]
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="job.post",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def update(*, participant: Participant, command: UpdateJobCommand) -> dict[str, Any]:
    """Revise what a job asks for, or where it ranks.

    The poster and the assignee may revise the description; only the orchestrator may move
    `priority`, because priority is the allocation decision and a seat that can raise its
    own is a seat that allocates.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    job = await get(room.id, command.job_id, with_history=False)
    if job.is_terminal:
        raise InvalidCommand(
            "That job is closed; its record is final.",
            job_id=job.id,
            state=job.state.value,
        )

    caller_role = await roles.role_for(participant)
    is_orchestrator = caller_role.value == "orchestrator"
    if participant.id not in {job.posted_by_participant_id, job.assigned_to_participant_id}:
        authz.require_orchestrator(
            participant, caller_role, action="revise another seat's job", reason="revision"
        )
    if command.priority is not None and not is_orchestrator:
        raise Forbidden(
            "Only the orchestrator sets a job's priority; state your urgency when you "
            "post it and the orchestrator ranks it.",
            job_id=job.id,
        )

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[
            command.desired_outcome or "",
            *(command.constraints or []),
            *(command.acceptance_criteria or []),
        ],
        known_participant_ids=known,
    )

    changed: dict[str, Any] = {}
    if command.priority is not None:
        changed["priority"] = command.priority
    if command.desired_outcome is not None:
        changed["desired_outcome"] = command.desired_outcome
    if command.targets is not None:
        changed["targets"] = db.dumps(list(command.targets))
    if command.constraints is not None:
        changed["constraints_json"] = db.dumps(list(command.constraints))
    if command.acceptance_criteria is not None:
        changed["acceptance_criteria"] = db.dumps(list(command.acceptance_criteria))
    if not changed:
        raise InvalidCommand("Nothing to update.", job_id=job.id)

    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        assignments = ", ".join(f"{name} = ?" for name in changed)
        affected = await tx.execute(
            f"UPDATE jobs SET {assignments}, updated_at = ? WHERE id = ? AND room_id = ? "
            "AND closed_at IS NULL",
            [*changed.values(), now, job.id, room.id],
        )
        if affected == 0:
            raise InvalidCommand(
                "That job was closed while this revision was in flight.", job_id=job.id
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.JOB_UPDATED,
            actor=actor_for(participant),
            payload={
                "job_id": job.id,
                # The field names that moved, so a reader need not diff two snapshots.
                "changed": sorted("constraints" if k == "constraints_json" else k for k in changed),
                "priority": command.priority,
                "desired_outcome": command.desired_outcome,
                "targets": list(command.targets) if command.targets is not None else None,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"job_id": job.id, "changed": sorted(changed)}, events=[event])

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="job.update",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def assign(*, participant: Participant, command: AssignJobCommand) -> dict[str, Any]:
    """Allocate or reallocate a job. Orchestrator only.

    One operation for both, because they are the same decision made twice and a reader
    following a job needs them in one stream. `previous_assignee_participant_id` is what
    makes a reallocation legible, and a reason is required either way — an unexplained move
    is indistinguishable from a mistake.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    caller_role = await roles.role_for(participant)
    authz.require_orchestrator(
        participant, caller_role, action="allocate a job", reason=command.reason
    )

    job = await get(room.id, command.job_id, with_history=False)
    if job.is_terminal:
        raise InvalidCommand(
            "That job is closed and cannot be allocated.", job_id=job.id, state=job.state.value
        )
    assignee = await _joined_participant(room.id, command.to_participant_id)
    privacy.inspect_content(command.reason, max_text_chars=2000)

    previous = job.assigned_to_participant_id
    now = utcnow_iso()
    # Reassigning a job that was already accepted returns it to `assigned`: the new owner
    # has not accepted anything yet, and leaving it `accepted` would assert that it had.
    next_state = JobState.ASSIGNED

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE jobs SET assigned_to_participant_id = ?, assigned_by_participant_id = ?, "
            "assigned_at = ?, accepted_at = NULL, assigned_goal_version = ?, state = ?, "
            "updated_at = ? WHERE id = ? AND room_id = ? AND closed_at IS NULL "
            "AND state = ? AND COALESCE(assigned_to_participant_id, '') = ?",
            (
                assignee.id,
                participant.id,
                now,
                command.assigned_goal_version,
                next_state.value,
                now,
                job.id,
                room.id,
                job.state.value,
                previous or "",
            ),
        )
        if affected == 0:
            raise InvalidCommand(
                "That job moved while this allocation was in flight; re-read it.",
                job_id=job.id,
                expected_state=job.state.value,
                expected_assignee=previous,
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.JOB_ASSIGNED,
            actor=actor_for(participant),
            payload={
                "job_id": job.id,
                "assigned_to_participant_id": assignee.id,
                "previous_assignee_participant_id": previous,
                "assigned_by_participant_id": participant.id,
                "assigned_goal_version": command.assigned_goal_version,
                "reason": command.reason,
            },
            causation_id=command.command_id,
        )
        await _record_transition_tx(
            tx,
            job_id=job.id,
            room_id=room.id,
            from_state=job.state,
            to_state=next_state,
            actor_participant_id=participant.id,
            reason=command.reason,
            seq=event.seq,
        )
        return CommandOutcome(
            result={
                "job_id": job.id,
                "assigned_to_participant_id": assignee.id,
                "previous_assignee_participant_id": previous,
                "state": next_state.value,
            },
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="job.assign",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def accept(*, participant: Participant, command: AcceptJobCommand) -> dict[str, Any]:
    """Take accountability for an allocated job. Assignee only.

    Separate from assignment because "told to own it" and "owns it" are different facts,
    and the gap between them is diagnostic: a job assigned an hour ago and never accepted
    says something about that supervisor that a single status could not.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    job = await get(room.id, command.job_id, with_history=False)
    authz.require_owns(participant, job.assigned_to_participant_id or "", what="assigned job")
    if job.is_terminal:
        raise InvalidCommand("That job is closed.", job_id=job.id, state=job.state.value)
    if job.accepted_at is not None:
        return {"job_id": job.id, "state": job.state.value, "already_accepted": True}

    privacy.inspect_content(command.note, max_text_chars=2000)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE jobs SET state = ?, accepted_at = ?, updated_at = ? WHERE id = ? "
            "AND room_id = ? AND state = ? AND assigned_to_participant_id = ? "
            "AND accepted_at IS NULL",
            (
                JobState.ACCEPTED.value,
                now,
                now,
                job.id,
                room.id,
                JobState.ASSIGNED.value,
                participant.id,
            ),
        )
        if affected == 0:
            raise InvalidCommand(
                "That job is not awaiting your acceptance.",
                job_id=job.id,
                state=job.state.value,
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.JOB_ACCEPTED,
            actor=actor_for(participant),
            payload={"job_id": job.id, "participant_id": participant.id, "note": command.note},
            causation_id=command.command_id,
        )
        await _record_transition_tx(
            tx,
            job_id=job.id,
            room_id=room.id,
            from_state=JobState.ASSIGNED,
            to_state=JobState.ACCEPTED,
            actor_participant_id=participant.id,
            reason=command.note,
            seq=event.seq,
        )
        return CommandOutcome(
            result={"job_id": job.id, "state": JobState.ACCEPTED.value}, events=[event]
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="job.accept",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def set_state(*, participant: Participant, command: SetJobStateCommand) -> dict[str, Any]:
    """Move a job between live states: active, paused, blocked.

    Terminal states are refused here on purpose. `close` is the only exit, because it is
    the path that records who ended it and why, and a second exit would eventually be the
    one that forgot.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    if command.state not in LIVE_STATES:
        raise InvalidCommand(
            f"`{command.state.value}` ends a job; use close so the reason is recorded.",
            state=command.state.value,
            live_states=sorted(s.value for s in LIVE_STATES),
        )

    job = await get(room.id, command.job_id, with_history=False)
    if job.is_terminal:
        raise InvalidCommand("That job is closed.", job_id=job.id, state=job.state.value)
    caller_role = await roles.role_for(participant)
    if participant.id != job.assigned_to_participant_id:
        authz.require_orchestrator(
            participant,
            caller_role,
            action="move a job somebody else owns",
            reason=command.reason or command.state.value,
        )
    if job.assigned_to_participant_id is None:
        raise InvalidCommand(
            "An unallocated job has nothing to progress; assign it first.", job_id=job.id
        )

    if command.task_id is not None:
        task_row = await db.fetch_one(
            "SELECT id FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room.id)
        )
        if task_row is None:
            raise NotFound("No such task in this room.", task_id=command.task_id)
        clash = await db.fetch_one(
            "SELECT id FROM jobs WHERE task_id = ? AND id <> ?", (command.task_id, job.id)
        )
        if clash is not None:
            # Surfaced as a domain error rather than letting uq_jobs_task raise: two jobs
            # pointing at one lease would make "which intent is this work serving"
            # unanswerable, and the caller needs to know which job already owns it.
            raise InvalidCommand(
                "That task already serves another job.",
                task_id=command.task_id,
                job_id=clash["id"],
            )

    privacy.inspect_content(command.reason, max_text_chars=2000)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE jobs SET state = ?, task_id = COALESCE(?, task_id), updated_at = ? "
            "WHERE id = ? AND room_id = ? AND state = ? AND closed_at IS NULL",
            (command.state.value, command.task_id, now, job.id, room.id, job.state.value),
        )
        if affected == 0:
            raise InvalidCommand(
                "That job moved while this change was in flight; re-read it.",
                job_id=job.id,
                expected_state=job.state.value,
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.JOB_STATE_CHANGED,
            actor=actor_for(participant),
            payload={
                "job_id": job.id,
                "state": command.state.value,
                "previous_state": job.state.value,
                "reason": command.reason,
                "task_id": command.task_id or job.task_id,
            },
            causation_id=command.command_id,
        )
        await _record_transition_tx(
            tx,
            job_id=job.id,
            room_id=room.id,
            from_state=job.state,
            to_state=command.state,
            actor_participant_id=participant.id,
            reason=command.reason,
            seq=event.seq,
        )
        return CommandOutcome(
            result={"job_id": job.id, "state": command.state.value}, events=[event]
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="job.set_state",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def close(*, participant: Participant, command: CloseJobCommand) -> dict[str, Any]:
    """End a job with an attributable reason. The only exit.

    Who may do what:

    * **completed** — the owning supervisor, having reviewed its workers' output, or the
      orchestrator accepting it on the room's behalf;
    * **cancelled / rejected** — the orchestrator, or the poster withdrawing its own
      request, which is not an exercise of authority over anyone;
    * **superseded** — the orchestrator, and it must name the replacement. A supersession
      that names nothing is a cancellation wearing the wrong label.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    if command.state not in TERMINAL_JOB_STATES:
        raise InvalidCommand(
            f"`{command.state.value}` is not an ending; use set_state.",
            state=command.state.value,
            terminal_states=sorted(s.value for s in TERMINAL_JOB_STATES),
        )
    if not command.reason.strip():
        raise InvalidCommand("Closing a job requires a reason.", job_id=command.job_id)

    job = await get(room.id, command.job_id, with_history=False)
    if job.is_terminal:
        raise InvalidCommand("That job is already closed.", job_id=job.id, state=job.state.value)

    caller_role = await roles.role_for(participant)
    is_owner = participant.id == job.assigned_to_participant_id
    is_poster = participant.id == job.posted_by_participant_id
    owner_completing = is_owner and command.state is JobState.COMPLETED
    poster_withdrawing = is_poster and command.state in {
        JobState.CANCELLED,
        JobState.REJECTED,
    }
    if not (owner_completing or poster_withdrawing):
        authz.require_orchestrator(
            participant,
            caller_role,
            action=f"close a job as {command.state.value}",
            reason=command.reason,
        )

    superseded_by = command.superseded_by_job_id
    if command.state is JobState.SUPERSEDED:
        if not superseded_by:
            raise InvalidCommand(
                "A superseded job must name the job that replaces it.", job_id=job.id
            )
        if superseded_by == job.id:
            raise InvalidCommand("A job cannot supersede itself.", job_id=job.id)
        await get(room.id, superseded_by, with_history=False)
    elif superseded_by:
        raise InvalidCommand(
            "`superseded_by_job_id` only applies when superseding.",
            state=command.state.value,
        )

    privacy.inspect_content(command.reason, max_text_chars=2000)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE jobs SET state = ?, terminal_reason = ?, terminated_by_participant_id = ?, "
            "superseded_by_job_id = ?, closed_at = ?, updated_at = ? WHERE id = ? "
            "AND room_id = ? AND closed_at IS NULL AND state = ?",
            (
                command.state.value,
                command.reason,
                participant.id,
                superseded_by,
                now,
                now,
                job.id,
                room.id,
                job.state.value,
            ),
        )
        if affected == 0:
            raise InvalidCommand(
                "That job was closed or moved while this was in flight; re-read it.",
                job_id=job.id,
                expected_state=job.state.value,
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.JOB_CLOSED,
            actor=actor_for(participant),
            payload={
                "job_id": job.id,
                "state": command.state.value,
                "reason": command.reason,
                "terminated_by_participant_id": participant.id,
                "superseded_by_job_id": superseded_by,
            },
            causation_id=command.command_id,
        )
        await _record_transition_tx(
            tx,
            job_id=job.id,
            room_id=room.id,
            from_state=job.state,
            to_state=command.state,
            actor_participant_id=participant.id,
            reason=command.reason,
            seq=event.seq,
        )
        return CommandOutcome(
            result={
                "job_id": job.id,
                "state": command.state.value,
                "closed_at": now,
                "superseded_by_job_id": superseded_by,
            },
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="job.close",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}

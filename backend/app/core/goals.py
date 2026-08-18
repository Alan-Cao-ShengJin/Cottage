"""Supervisor goals: versioned direction the orchestrator fully controls (D-088).

A goal is the orchestrator's control surface over one supervisor, and it is *disposable on
purpose*. "Stop working on JOB-118, park its workers, own JOB-122 as P0, spawn one backend
and one test worker, report in ten minutes" is a single decision. Appending it to whatever
that supervisor was already told would leave two directives in force and no way to know
which is current, so replacement is total.

Three things stop a disposable directive from becoming an unaccountable one.

**A fence.** `supervisor_goals.current_version` is bumped by a conditional UPDATE whose
affected-row count arbitrates, exactly as `rooms.event_seq` allocates a `seq`. A caller
states which generation it is acting against; if another revision landed first it gets
`revision_conflict` carrying the current version. There is deliberately no "latest" mode —
a blind overwrite is precisely how a stale orchestrator turn undoes a newer decision, and
retrying it would make the race invisible rather than resolving it.

**Append-only history.** Every version ever issued stays in `supervisor_goal_versions` with
its supersession stamped forward, so "what was this supervisor told when it spawned that
worker" is still answerable ten revisions later. That is what makes stale worker output
attributable instead of merely wrong.

**A contract the orchestrator cannot rewrite.** `goal.IMMUTABLE_CONTRACT` holds the
obligations that survive any replacement — presence honesty, lease discipline, obeying
stop, reporting failure as failure, never relaying credentials or reasoning. They live in
the protocol and in this module's `immutable_contract()`; there is no command field that
can reach them, which is asserted by a test rather than promised in prose.

Acknowledgement is **evidence, never permission** (ADR-012). The new version is current the
moment it commits; a supervisor's acknowledgement records that it saw it. That split is what
makes "issued but never acknowledged" a state the room can state plainly instead of an
ambiguity.
"""

from __future__ import annotations

from typing import Any

from ..db import database as db
from ..domain import ids
from ..domain.commands import (
    AcknowledgeGoalCommand,
    CloseGoalCommand,
    ReplaceGoalCommand,
)
from ..domain.events import EventType
from ..domain.goal import (
    IMMUTABLE_CONTRACT,
    GoalSource,
    GoalStatus,
    GoalVersion,
    SupervisorGoal,
    WorkerDisposition,
)
from ..domain.room import MembershipState, Participant, PrivacyClass, RoomRole, Scope
from ..util import utcnow_iso
from . import authz, eventlog, privacy, roles, store
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import Forbidden, InvalidCommand, NotFound, RevisionConflict

#: Room positions that may hold a goal. The orchestrator is included because it is also a
#: supervisor for its own human and spawns its own workers like anyone else; an observer is
#: excluded because it has already told the room it is not here to work.
GOAL_HOLDING_ROLES: tuple[RoomRole, ...] = (RoomRole.ORCHESTRATOR, RoomRole.SUPERVISOR)

#: Newest-first history page size, capped so a projection cannot grow without bound. The
#: total is always returned beside the rows, because a count that can truncate must say so
#: (D-043).
DEFAULT_HISTORY_LIMIT = 20


def immutable_contract() -> tuple[str, ...]:
    """The obligations no goal may override. Returned verbatim for a runtime to present."""
    return IMMUTABLE_CONTRACT


def _to_version(row: Any) -> GoalVersion:
    return GoalVersion(
        goal_id=row["goal_id"],
        version=int(row["version"]),
        room_id=row["room_id"],
        objective=row["objective"],
        instructions=row["instructions"],
        worker_plan=row["worker_plan"],
        related_job_ids=tuple(db.str_list(row["related_job_ids"])),
        dependencies=tuple(db.str_list(row["dependencies"])),
        constraints=tuple(db.str_list(row["constraints_json"])),
        acceptance_criteria=tuple(db.str_list(row["acceptance_criteria"])),
        reporting_requirements=row["reporting_requirements"],
        worker_disposition=WorkerDisposition(row["worker_disposition"]),
        reason=row["reason"],
        priority=int(row["priority"]),
        source=GoalSource(row["source"]),
        privacy_class=PrivacyClass(row["privacy_class"]),
        issued_by_participant_id=row["issued_by_participant_id"],
        replaces_version=(
            int(row["replaces_version"]) if row["replaces_version"] is not None else None
        ),
        created_seq=int(row["created_seq"]),
        created_at=row["created_at"],
        superseded_at=row["superseded_at"],
        superseded_by_version=(
            int(row["superseded_by_version"]) if row["superseded_by_version"] is not None else None
        ),
        acknowledged_at=row["acknowledged_at"],
        acknowledged_note=row["acknowledged_note"],
        acknowledged_rejected=bool(row["acknowledged_rejected"]),
    )


def _to_goal(row: Any, current: GoalVersion | None = None) -> SupervisorGoal:
    return SupervisorGoal(
        id=row["id"],
        room_id=row["room_id"],
        supervisor_participant_id=row["supervisor_participant_id"],
        current_version=int(row["current_version"]),
        status=GoalStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
        current=current,
    )


async def current_for(room_id: str, participant_id: str) -> SupervisorGoal | None:
    """This seat's active goal with its current version loaded, or None.

    Scoped by room like every other read here: a global id lookup would answer "does this
    exist" for a room the caller cannot see, which is an existence oracle.
    """
    row = await db.fetch_one(
        "SELECT * FROM supervisor_goals WHERE room_id = ? AND supervisor_participant_id = ? "
        "AND status = ?",
        (room_id, participant_id, GoalStatus.ACTIVE.value),
    )
    if row is None:
        return None
    version_row = await db.fetch_one(
        "SELECT * FROM supervisor_goal_versions WHERE goal_id = ? AND version = ?",
        (row["id"], int(row["current_version"])),
    )
    return _to_goal(row, _to_version(version_row) if version_row is not None else None)


async def goals_for_room(room_id: str) -> list[SupervisorGoal]:
    """Every active goal in the room, current version loaded, oldest first."""
    rows = await db.fetch_all(
        "SELECT * FROM supervisor_goals WHERE room_id = ? AND status = ? ORDER BY created_at ASC",
        (room_id, GoalStatus.ACTIVE.value),
    )
    out: list[SupervisorGoal] = []
    for row in rows:
        version_row = await db.fetch_one(
            "SELECT * FROM supervisor_goal_versions WHERE goal_id = ? AND version = ?",
            (row["id"], int(row["current_version"])),
        )
        out.append(_to_goal(row, _to_version(version_row) if version_row is not None else None))
    return out


async def history_for(
    room_id: str, goal_id: str, *, limit: int = DEFAULT_HISTORY_LIMIT
) -> tuple[list[GoalVersion], int]:
    """Every version this goal has had, newest first, with the untruncated total."""
    total = await db.fetch_value(
        "SELECT COUNT(*) FROM supervisor_goal_versions WHERE goal_id = ? AND room_id = ?",
        (goal_id, room_id),
    )
    rows = await db.fetch_all(
        "SELECT * FROM supervisor_goal_versions WHERE goal_id = ? AND room_id = ? "
        "ORDER BY version DESC LIMIT ?",
        (goal_id, room_id, max(1, limit)),
    )
    return [_to_version(row) for row in rows], int(total or 0)


async def _require_goal_holder(room_id: str, participant_id: str) -> tuple[Participant, RoomRole]:
    """Load the target seat and refuse one that cannot hold a goal."""
    try:
        target = await store.load_participant_for_room(room_id, participant_id)
    except NotFound:
        raise NotFound(
            "That participant is not in this room.",
            participant_id=participant_id,
            room_id=room_id,
        ) from None
    if target.state is not MembershipState.JOINED:
        raise InvalidCommand(
            "That seat is not an active participant of this room.",
            participant_id=target.id,
            state=target.state.value,
        )
    target_role = await roles.role_for(target)
    if target_role not in GOAL_HOLDING_ROLES:
        raise InvalidCommand(
            f"A {target_role.value} cannot hold a goal. Only a supervisor or the "
            "orchestrator executes work in a room.",
            participant_id=target.id,
            room_role=target_role.value,
        )
    return target, target_role


async def replace(*, participant: Participant, command: ReplaceGoalCommand) -> dict[str, Any]:
    """Set or wholly replace a supervisor's active goal.

    Two callers are legitimate, and they are gated differently:

    * **the orchestrator**, replacing any supervisor's goal, which is the mechanism the
      hierarchy runs on. `require_orchestrator` demands `room.admin`, the orchestrator
      position, and a stated reason;
    * **a supervisor refining its own goal**, which is limited to detail the orchestrator
      left open — `instructions` and `reporting_requirements`. It may never move its own
      objective, priority, worker plan or related jobs, because a supervisor that can
      re-scope itself is a supervisor the orchestrator has not actually allocated.

    Anything else is refused. Note what is *not* a caller: nobody edits a goal in place.
    Every change is a new version with the previous one stamped superseded.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    authz.require_scope(participant, Scope.WORK_DECLARE)

    caller_role = await roles.role_for(participant)
    target, _ = await _require_goal_holder(room.id, command.target_supervisor_participant_id)
    self_revision = target.id == participant.id

    existing = await current_for(room.id, target.id)

    if self_revision and caller_role is not RoomRole.ORCHESTRATOR:
        # A supervisor refining its own direction. Everything the orchestrator decided
        # must be carried forward unchanged, and we compare against the live row rather
        # than trusting the caller to resend it faithfully.
        if existing is None or existing.current is None:
            raise Forbidden(
                "A supervisor cannot give itself a goal. Post a job for the orchestrator "
                "to allocate instead.",
                participant_id=participant.id,
            )
        held = existing.current
        immovable = {
            "objective": (command.objective.strip(), held.objective.strip()),
            "priority": (command.priority, held.priority),
            "worker_plan": (command.worker_plan.strip(), held.worker_plan.strip()),
            "related_job_ids": (tuple(command.related_job_ids), held.related_job_ids),
            "acceptance_criteria": (tuple(command.acceptance_criteria), held.acceptance_criteria),
            "constraints": (tuple(command.constraints), held.constraints),
        }
        moved = [name for name, (new, old) in immovable.items() if new != old]
        if moved:
            raise Forbidden(
                "A supervisor may only refine `instructions` and `reporting_requirements` "
                "on its own goal; the orchestrator owns everything else. Post a job or ask "
                "the orchestrator to re-goal you.",
                fields=moved,
            )
        source = GoalSource.SUPERVISOR
    else:
        authz.require_orchestrator(
            participant,
            caller_role,
            action="replace a supervisor's active goal",
            reason=command.reason or "goal replacement",
        )
        source = GoalSource.ORCHESTRATOR

    # The fence is checked before anything is written, and again inside the transaction by
    # the guarded UPDATE. Checking here buys a precise error; checking there is what makes
    # it correct under concurrency.
    if existing is None:
        if command.expected_version is not None:
            raise RevisionConflict(
                "This supervisor has no active goal, so there is no version to replace.",
                expected_version=command.expected_version,
                current_version=None,
            )
    else:
        if command.expected_version is None:
            raise RevisionConflict(
                "This supervisor already has an active goal. State the version you are "
                "replacing; there is no blind-overwrite mode.",
                current_version=existing.current_version,
                goal_id=existing.id,
            )
        if command.expected_version != existing.current_version:
            raise RevisionConflict(
                "That goal version is no longer current; re-read before replacing it.",
                expected_version=command.expected_version,
                current_version=existing.current_version,
                goal_id=existing.id,
            )

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[
            command.objective,
            command.instructions,
            command.worker_plan,
            command.reporting_requirements,
            command.reason,
            *command.constraints,
            *command.acceptance_criteria,
            *command.dependencies,
        ],
        known_participant_ids=known,
    )

    goal_id = existing.id if existing is not None else ids.new_id(ids.GOAL)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        if existing is None:
            await tx.execute(
                "INSERT INTO supervisor_goals (id, room_id, supervisor_participant_id, "
                "current_version, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (goal_id, room.id, target.id, 1, GoalStatus.ACTIVE.value, now, now),
            )
            version = 1
            replaces = None
        else:
            # The allocator. 0 rows means another revision landed between the read above
            # and this write: the caller is stale, and stale is not "retry".
            affected = await tx.execute(
                "UPDATE supervisor_goals SET current_version = current_version + 1, "
                "updated_at = ? WHERE id = ? AND room_id = ? AND status = ? "
                "AND current_version = ?",
                (
                    now,
                    goal_id,
                    room.id,
                    GoalStatus.ACTIVE.value,
                    command.expected_version,
                ),
            )
            if affected == 0:
                fresh = await tx.fetch_one(
                    "SELECT current_version, status FROM supervisor_goals WHERE id = ?",
                    (goal_id,),
                )
                raise RevisionConflict(
                    "Another revision replaced this goal first; re-read before replacing it.",
                    expected_version=command.expected_version,
                    current_version=(int(fresh["current_version"]) if fresh else None),
                    goal_id=goal_id,
                )
            version = int(command.expected_version or 0) + 1
            replaces = command.expected_version
            # Stamp the outgoing version forward. Same transaction, so a reader can never
            # see two live versions or none.
            await tx.execute(
                "UPDATE supervisor_goal_versions SET superseded_at = ?, "
                "superseded_by_version = ? WHERE goal_id = ? AND version = ? "
                "AND superseded_at IS NULL",
                (now, version, goal_id, replaces),
            )

        await tx.execute(
            """
            INSERT INTO supervisor_goal_versions (
                goal_id, version, room_id, objective, instructions, worker_plan,
                related_job_ids, dependencies, constraints_json, acceptance_criteria,
                reporting_requirements, worker_disposition, reason, priority, source,
                privacy_class, issued_by_participant_id, replaces_version, created_seq,
                created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                goal_id,
                version,
                room.id,
                command.objective.strip(),
                command.instructions,
                command.worker_plan,
                db.dumps(list(command.related_job_ids)),
                db.dumps(list(command.dependencies)),
                db.dumps(list(command.constraints)),
                db.dumps(list(command.acceptance_criteria)),
                command.reporting_requirements,
                command.worker_disposition.value,
                command.reason,
                command.priority,
                source.value,
                decision.privacy_class.value,
                participant.id,
                replaces,
                room.event_seq + 1,
                now,
            ),
        )

        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.GOAL_REPLACED,
            actor=actor_for(participant),
            payload={
                "goal_id": goal_id,
                "target_supervisor_participant_id": target.id,
                "new_version": version,
                "previous_version": replaces,
                "replaces_version": replaces,
                "objective": command.objective.strip(),
                "instructions": command.instructions,
                "worker_plan": command.worker_plan,
                "related_job_ids": list(command.related_job_ids),
                "dependencies": list(command.dependencies),
                "constraints": list(command.constraints),
                "acceptance_criteria": list(command.acceptance_criteria),
                "reporting_requirements": command.reporting_requirements,
                "worker_disposition": command.worker_disposition.value,
                "priority": command.priority,
                "reason": command.reason,
                "issued_by_participant_id": participant.id,
                "source": source.value,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={
                "goal_id": goal_id,
                "version": version,
                "previous_version": replaces,
                "target_supervisor_participant_id": target.id,
                "worker_disposition": command.worker_disposition.value,
            },
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="supervisor.goal.replace",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def acknowledge(
    *, participant: Participant, command: AcknowledgeGoalCommand
) -> dict[str, Any]:
    """Record that the target supervisor observed a version.

    **This does not gate the effect.** The goal took effect in the transaction that wrote
    it, and a supervisor that never acknowledges is still bound by it — that is the whole
    reason effect and observation are separate fields (ADR-012). What acknowledgement buys
    the room is the ability to say "issued but never seen" instead of leaving the two
    indistinguishable.

    A supervisor may acknowledge *and* reject. Rejection is information the orchestrator
    needs, not a veto it must honour.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)

    goal_row = await db.fetch_one(
        "SELECT * FROM supervisor_goals WHERE id = ? AND room_id = ?",
        (command.goal_id, room.id),
    )
    if goal_row is None:
        raise NotFound("No such goal in this room.", goal_id=command.goal_id)
    authz.require_owns(participant, goal_row["supervisor_participant_id"], what="goal")

    current_version = int(goal_row["current_version"])
    if command.version != current_version:
        raise InvalidCommand(
            "That goal version has been superseded; acknowledge the current one instead.",
            acknowledged_version=command.version,
            current_version=current_version,
            goal_id=command.goal_id,
        )

    known = [p.id for p in await store.list_participants(room.id)]
    privacy.inspect_content(command.note, max_text_chars=2000)
    del known  # note is the caller's own words about its own goal; no audience to resolve

    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        # Guarded so the first acknowledgement is the recorded one: a second call is
        # idempotent rather than an overwrite that loses when it was first seen.
        affected = await tx.execute(
            "UPDATE supervisor_goal_versions SET acknowledged_at = ?, acknowledged_note = ?, "
            "acknowledged_rejected = ? WHERE goal_id = ? AND version = ? "
            "AND acknowledged_at IS NULL",
            (
                now,
                command.note,
                1 if command.rejected else 0,
                command.goal_id,
                command.version,
            ),
        )
        if affected == 0:
            existing_ack = await tx.fetch_one(
                "SELECT acknowledged_at, acknowledged_note, acknowledged_rejected "
                "FROM supervisor_goal_versions WHERE goal_id = ? AND version = ?",
                (command.goal_id, command.version),
            )
            if existing_ack is None:
                raise NotFound(
                    "No such goal version.",
                    goal_id=command.goal_id,
                    version=command.version,
                )
            return CommandOutcome(
                result={
                    "goal_id": command.goal_id,
                    "version": command.version,
                    "acknowledged_at": existing_ack["acknowledged_at"],
                    "already_acknowledged": True,
                }
            )

        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.GOAL_ACKNOWLEDGED,
            actor=actor_for(participant),
            payload={
                "goal_id": command.goal_id,
                "version": command.version,
                "participant_id": participant.id,
                "note": command.note,
                "rejected": command.rejected,
                "issued_at_seq": int(goal_row["current_version"]),
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={
                "goal_id": command.goal_id,
                "version": command.version,
                "acknowledged_at": now,
                "rejected": command.rejected,
            },
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="supervisor.goal.acknowledge",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def close(*, participant: Participant, command: CloseGoalCommand) -> dict[str, Any]:
    """Stand a goal down: achieved, or abandoned.

    The holder may report its own goal `achieved` — it is the party that knows. Abandoning
    is the orchestrator's call, because a supervisor that can abandon its own direction has
    not been allocated anything.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)

    goal_row = await db.fetch_one(
        "SELECT * FROM supervisor_goals WHERE id = ? AND room_id = ?",
        (command.goal_id, room.id),
    )
    if goal_row is None:
        raise NotFound("No such goal in this room.", goal_id=command.goal_id)
    if command.status is GoalStatus.ACTIVE:
        raise InvalidCommand(
            "Closing a goal means `achieved` or `abandoned`; use replace to change an active goal.",
            status=command.status.value,
        )

    holder_id = goal_row["supervisor_participant_id"]
    caller_role = await roles.role_for(participant)
    holder_reporting_achieved = (
        participant.id == holder_id and command.status is GoalStatus.ACHIEVED
    )
    if not holder_reporting_achieved:
        authz.require_orchestrator(
            participant,
            caller_role,
            action="close another seat's goal",
            reason=command.reason,
        )

    privacy.inspect_content(command.reason, max_text_chars=2000)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE supervisor_goals SET status = ?, closed_at = ?, updated_at = ? "
            "WHERE id = ? AND room_id = ? AND status = ?",
            (
                command.status.value,
                now,
                now,
                command.goal_id,
                room.id,
                GoalStatus.ACTIVE.value,
            ),
        )
        if affected == 0:
            raise InvalidCommand(
                "That goal is already closed.",
                goal_id=command.goal_id,
                status=goal_row["status"],
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.GOAL_CLOSED,
            actor=actor_for(participant),
            payload={
                "goal_id": command.goal_id,
                "version": int(goal_row["current_version"]),
                "status": command.status.value,
                "reason": command.reason,
                "participant_id": holder_id,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={
                "goal_id": command.goal_id,
                "status": command.status.value,
                "closed_at": now,
            },
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="supervisor.goal.close",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}

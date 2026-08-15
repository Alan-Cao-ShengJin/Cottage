"""Task graph and claims-as-leases.

This is where the product's correctness lives, so the mechanism is stated plainly.

**One valid claim per task.** A claim is taken by a single conditional UPDATE whose
WHERE clause encodes the entire precondition: the task is unclaimed, or its lease
has expired, or the caller already holds it. If that UPDATE affects zero rows,
someone else won — and that is reported as `lease_conflict`, not retried. No
process-level lock and no engine-specific locking semantics are involved, so the
guarantee survives a move to PostgreSQL (ADR-009).

**A stale claimant can never mutate.** Every mutation of a held task carries a
`fence`, compared against the task's current fence. `fence` is monotonic per task,
persisted across release and expiry, and never reused. A lease id alone cannot give
this property: a process that lost its lease while suspended, then woke up, would
still hold a lease id that *looks* current. It cannot hold a current fence, because
the reclaim that displaced it incremented one.

**The fence is not a credential.** It says *is this the current state*, never *may I
act on it*. It is published in the room projection and in `task.claimed`, because
every participant needs it to reason about staleness — so anything it could authorize,
it would authorize for everyone. Ownership is a separate check (`_assert_holder`), and
every mutation of a *held* task needs both. Scope says what kind of thing a
participant may do; ownership says which instance. Checking only scope grants
everyone everything of that kind — which is exactly what happened to `complete` and
`update` until D-026.

**Expiry does not depend on the reaper.** `store.to_task` drops an expired claim on
every read, so the moment a lease lapses it is invisible to readers and reclaimable
by writers. The reaper only controls how quickly the durable status change and the
`task.claim_expired` event land.
"""

from __future__ import annotations

import logging

from ..config import settings
from ..db import database as db
from ..domain import ids
from ..domain.commands import (
    CancelTaskCommand,
    ClaimTaskCommand,
    CompleteTaskCommand,
    CreateTaskCommand,
    ReleaseClaimCommand,
    RenewClaimCommand,
    TakeOverExecutionCommand,
    UpdateTaskCommand,
)
from ..domain.events import EventEnvelope, EventType
from ..domain.room import Participant, Room, Scope
from ..domain.task import HALTED_STEERING, HELD_TASK_STATUSES, Steering, Task, TaskStatus
from ..util import is_past, iso_in, normalize_target, utcnow_iso
from . import authz, conflicts, eventlog, presence, privacy, store
from .actors import SYSTEM_ACTOR, actor_for
from .dispatch import CommandOutcome, execute_command, publish_committed
from .errors import (
    CapabilityUnsupported,
    ExecutorConflict,
    InvalidCommand,
    LeaseConflict,
    LeaseRequired,
    NotFound,
    StaleFence,
    SteeringHalted,
)

log = logging.getLogger(__name__)

#: Statuses from which a claim may be taken. `open` covers both a never-claimed
#: task and one whose lease lapsed, because a lapsed lease reads as `open`.
CLAIMABLE_STATUSES = ("open", "claimed", "in_progress", "blocked")


# ---------------------------------------------------------------------------
# Task creation
# ---------------------------------------------------------------------------


async def create(*, participant: Participant, command: CreateTaskCommand) -> Task:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.TASK_PROPOSE)
    authz.require_writable(room)

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[command.title, command.description, command.targets],
        known_participant_ids=known,
    )
    if command.propose_to_participant_id and command.propose_to_participant_id not in known:
        raise InvalidCommand(
            "Cannot propose to a participant who is not in this room.",
            to_participant_id=command.propose_to_participant_id,
        )
    if command.claim_immediately and command.propose_to_participant_id:
        raise InvalidCommand("A task cannot be both proposed to someone else and self-claimed.")

    targets = _normalized_targets(command.targets)
    task_id = ids.new_id(ids.TASK)
    now = utcnow_iso()
    status = TaskStatus.PROPOSED if command.propose_to_participant_id else TaskStatus.OPEN

    runtime = None
    executor = None
    if command.claim_immediately:
        # Resolved before the transaction so a capability refusal costs nothing.
        executor = await presence.resolve_executor(
            participant=participant, connection_id=command.connection_id
        )
        runtime = await presence.runtime_policy_for(participant, room, executor=executor)
        _require_may_claim(runtime)

    async def body(tx: db.Tx) -> CommandOutcome:
        await tx.execute(
            """
            INSERT INTO tasks (
                id, room_id, title, description, status, targets, priority,
                created_by_participant_id, fence, result, privacy_class,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,0,'',?,?,?)
            """,
            (
                task_id,
                room.id,
                command.title,
                command.description,
                status.value,
                db.dumps(targets),
                command.priority,
                participant.id,
                decision.privacy_class.value,
                now,
                now,
            ),
        )
        events: list[EventEnvelope] = [
            await eventlog.append(
                tx,
                room_id=room.id,
                type_=EventType.TASK_CREATED,
                actor=actor_for(participant),
                payload={
                    "task_id": task_id,
                    "title": command.title,
                    "description": command.description,
                    "status": status.value,
                    "targets": targets,
                    "priority": command.priority,
                    "created_by_participant_id": participant.id,
                },
                disclosure=decision,
                causation_id=command.command_id,
            )
        ]

        events += await conflicts.detect_duplicate_task_tx(
            tx,
            room=room,
            task_id=task_id,
            participant=participant,
            title=command.title,
            targets=targets,
        )

        if command.propose_to_participant_id:
            events.append(
                await _propose_tx(
                    tx,
                    room=room,
                    participant=participant,
                    task_id=task_id,
                    to_participant_id=command.propose_to_participant_id,
                    note=command.description[:500],
                )
            )
        elif command.claim_immediately and runtime is not None and executor is not None:
            claimed = await _claim_tx(
                tx,
                room=room,
                participant=participant,
                task_id=task_id,
                lease_seconds=runtime.max_lease_seconds,
                heartbeat_interval_s=runtime.heartbeat_interval_s,
                executor=executor,
            )
            events += claimed

        return CommandOutcome(result={"task_id": task_id}, events=events)

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="task.create",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    # On a replay the body never ran, so `task_id` above refers to nothing. The
    # receipt holds the id the original attempt created — an idempotent command must
    # return the original entity, not a phantom.
    return await store.load_task(str(outcome.result.get("task_id", task_id)))


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


def _require_may_claim(runtime) -> None:
    """Claim eligibility comes from negotiated capabilities, never a vendor label.

    The refusal message names the missing capability so the client can fix its
    declaration rather than guess.
    """
    if not runtime.may_claim:
        raise CapabilityUnsupported(
            runtime.claim_denied_reason or "This participant may not claim work.",
            may_claim=False,
            delivery_mode=runtime.delivery_mode.value,
        )


async def _claim_tx(
    tx: db.Tx,
    *,
    room: Room,
    participant: Participant,
    task_id: str,
    lease_seconds: int,
    heartbeat_interval_s: int,
    executor: presence.Executor,
) -> list[EventEnvelope]:
    """Take the lease with one conditional UPDATE. Raises on loss.

    The WHERE clause is the whole concurrency control:
      * the task exists in this room and is not terminal;
      * and *either* nothing holds it, or what holds it has expired, or it is
        already held by this same participant (idempotent re-claim).
    """
    row = await tx.fetch_one("SELECT * FROM tasks WHERE id = ? AND room_id = ?", (task_id, room.id))
    if row is None:
        raise NotFound("Task does not exist.", task_id=task_id)
    if row["status"] in {"done", "cancelled"}:
        raise InvalidCommand(
            "That task is already finished.", task_id=task_id, status=row["status"]
        )
    _require_not_halted(row, action="claim")

    # The idempotent re-claim branch below matches on participant, which is the seat
    # rather than the runtime. Without this check a chat surface could re-claim its
    # own seat's lease and quietly become the executor of work a sibling worker is
    # still performing — a takeover with none of a takeover's visibility (D-035).
    if (
        row["claim_lease_id"]
        and not is_past(row["claim_expires_at"])
        and row["claim_participant_id"] == participant.id
    ):
        await _require_executor_or_dead(
            tx, row, participant=participant, executor=executor, action="re-claim"
        )

    now = utcnow_iso()
    lease_id = ids.new_id(ids.CLAIM)
    expires_at = iso_in(lease_seconds)

    affected = await tx.execute(
        """
        UPDATE tasks
        SET fence = fence + 1,
            status = 'claimed',
            claim_lease_id = ?,
            claim_participant_id = ?,
            claim_fence = fence + 1,
            claim_claimed_at = ?,
            claim_expires_at = ?,
            claim_heartbeat_interval_s = ?,
            claim_renewed_at = NULL,
            executor_attachment_id = ?,
            executor_connection_id = ?,
            updated_at = ?
        WHERE id = ?
          AND status NOT IN ('done','cancelled')
          AND (
                claim_lease_id IS NULL
             OR claim_expires_at <= ?
             OR claim_participant_id = ?
          )
        """,
        (
            lease_id,
            participant.id,
            now,
            expires_at,
            heartbeat_interval_s,
            executor.attachment_id,
            executor.connection_id,
            now,
            task_id,
            now,
            participant.id,
        ),
    )

    if affected == 0:
        # Someone holds a valid lease. Tell the caller who and until when, so it can
        # decide to wait, pick different work, or raise it in the room.
        #
        # The `claim_race` conflict is deliberately *not* recorded here: raising
        # rolls this transaction back, so anything appended would vanish. `claim`
        # records it afterwards, in its own committed transaction.
        holder_id = row["claim_participant_id"]
        holder = await store.load_participant(holder_id) if holder_id else None
        raise LeaseConflict(
            "Another participant holds a valid lease on this task.",
            task_id=task_id,
            held_by_participant_id=holder_id,
            held_by_display_name=holder.identity.display_name if holder else None,
            expires_at=row["claim_expires_at"],
        )

    fence = int(await tx.fetch_value("SELECT fence FROM tasks WHERE id = ?", (task_id,)))
    event = await eventlog.append(
        tx,
        room_id=room.id,
        type_=EventType.TASK_CLAIMED,
        actor=actor_for(participant),
        payload={
            "task_id": task_id,
            "participant_id": participant.id,
            "lease_id": lease_id,
            "fence": fence,
            "expires_at": expires_at,
            "heartbeat_interval_s": heartbeat_interval_s,
            "lease_seconds": lease_seconds,
            # Which runtime, not just which seat. A room reading only participant ids
            # cannot tell a worker claiming from its human's chat window claiming.
            "executor_attachment_id": executor.attachment_id,
            "executor_connection_id": executor.connection_id,
        },
    )
    return [event]


async def claim(*, participant: Participant, command: ClaimTaskCommand) -> Task:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.TASK_CLAIM)
    authz.require_writable(room)

    executor = await presence.resolve_executor(
        participant=participant, connection_id=command.connection_id
    )
    runtime = await presence.runtime_policy_for(participant, room, executor=executor)
    _require_may_claim(runtime)

    lease_seconds = min(
        command.requested_lease_seconds or runtime.max_lease_seconds,
        runtime.max_lease_seconds,
        settings.max_lease_seconds,
    )

    # Snapshot the holder before attempting, so a lost race can be recorded as a
    # conflict in its own committed transaction rather than being rolled back with
    # the failed claim.
    before = await store.load_task(command.task_id)

    async def body(tx: db.Tx) -> CommandOutcome:
        events = await _claim_tx(
            tx,
            room=room,
            participant=participant,
            task_id=command.task_id,
            lease_seconds=lease_seconds,
            heartbeat_interval_s=runtime.heartbeat_interval_s,
            executor=executor,
        )
        return CommandOutcome(result={"task_id": command.task_id}, events=events)

    try:
        await execute_command(
            command_id=command.command_id,
            command_type="task.claim",
            room_id=room.id,
            participant_id=participant.id,
            body=body,
        )
    except LeaseConflict:
        # Record the race after the failed attempt rolled back. A concurrent race is
        # information the room needs, so it is persisted on its own.
        if before.claim is not None or before.status in HELD_TASK_STATUSES:
            await _record_claim_race(
                room=room,
                task_id=command.task_id,
                loser=participant,
                winner_participant_id=(before.claim.participant_id if before.claim else None),
            )
        raise

    return await store.load_task(command.task_id)


async def _record_claim_race(
    *, room: Room, task_id: str, loser: Participant, winner_participant_id: str | None
) -> None:
    async with db.transaction() as tx:
        events = await conflicts.record_claim_race_tx(
            tx,
            room=room,
            task_id=task_id,
            loser=loser,
            winner_participant_id=winner_participant_id,
        )
    await publish_committed(events)


async def renew(*, participant: Participant, command: RenewClaimCommand) -> Task:
    """Extend a lease the caller still holds.

    Renewal is only valid *before* expiry. After expiry the participant must
    re-claim and receives a new fence — quietly resurrecting a lapsed lease would
    reintroduce exactly the zombie-writer problem fencing exists to prevent.
    """
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.TASK_CLAIM)
    authz.require_writable(room)

    runtime = await presence.runtime_policy_for(participant, room)
    extend = min(
        command.extend_seconds or runtime.max_lease_seconds,
        runtime.max_lease_seconds,
        settings.max_lease_seconds,
    )
    now = utcnow_iso()
    expires_at = iso_in(extend)

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room.id)
        )
        if row is None:
            raise NotFound("Task does not exist.", task_id=command.task_id)
        _assert_fence(row, command.fence)

        affected = await tx.execute(
            """
            UPDATE tasks
            SET claim_expires_at = ?, claim_renewed_at = ?, updated_at = ?
            WHERE id = ?
              AND claim_participant_id = ?
              AND claim_fence = ?
              AND claim_expires_at > ?
            """,
            (expires_at, now, now, command.task_id, participant.id, command.fence, now),
        )
        if affected == 0:
            raise LeaseConflict(
                "Your lease has already expired; re-claim the task to get a new fence.",
                task_id=command.task_id,
                fence=command.fence,
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.TASK_CLAIM_RENEWED,
            actor=actor_for(participant),
            payload={
                "task_id": command.task_id,
                "participant_id": participant.id,
                "fence": command.fence,
                "expires_at": expires_at,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"task_id": command.task_id}, events=[event])

    await execute_command(
        command_id=command.command_id,
        command_type="task.renew_claim",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await store.load_task(command.task_id)


async def release(*, participant: Participant, command: ReleaseClaimCommand) -> Task:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.TASK_CLAIM)

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room.id)
        )
        if row is None:
            raise NotFound("Task does not exist.", task_id=command.task_id)
        _assert_fence(row, command.fence)
        # D-035 reversed the earlier position that release is harmless. Giving work
        # up while another runtime is still doing it frees a third party to start
        # the same external action, which is the same end state as seizing it.
        if command.force:
            require_override_authority(
                participant, command.reason, what="release another runtime's work"
            )
        else:
            await _require_executor_or_dead(
                tx,
                row,
                participant=participant,
                connection_id=command.connection_id,
                action="release",
            )

        affected = await tx.execute(
            """
            UPDATE tasks
            SET status = 'open', claim_lease_id = NULL, claim_participant_id = NULL,
                claim_fence = NULL, claim_claimed_at = NULL, claim_expires_at = NULL,
                claim_heartbeat_interval_s = NULL, claim_renewed_at = NULL,
                executor_attachment_id = NULL, executor_connection_id = NULL,
                updated_at = ?
            WHERE id = ? AND claim_participant_id = ? AND claim_fence = ?
            """,
            (utcnow_iso(), command.task_id, participant.id, command.fence),
        )
        if affected == 0:
            # Nothing to release. Idempotent: a retry after a successful release
            # should not fail.
            return CommandOutcome(result={"task_id": command.task_id})
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.TASK_CLAIM_RELEASED,
            actor=actor_for(participant),
            payload={
                "task_id": command.task_id,
                "participant_id": participant.id,
                "fence": command.fence,
                "note": command.note,
                # Stamped rather than merely permitted. An override with no trace is
                # indistinguishable from a bug in the affinity rule it overrode.
                "forced": command.force,
                "reason": command.reason,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"task_id": command.task_id}, events=[event])

    await execute_command(
        command_id=command.command_id,
        command_type="task.release_claim",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await store.load_task(command.task_id)


async def release_all_claims_tx(
    tx: db.Tx, *, participant: Participant, reason: str
) -> list[EventEnvelope]:
    """Release every claim a participant holds, inside the caller's transaction.

    Called on graceful leave and on losing the last connection. `fence` is *not*
    reset, so a claimant that comes back gets a strictly greater fence and its old
    one stays permanently unusable.
    """
    rows = await tx.fetch_all(
        "SELECT id, claim_fence FROM tasks WHERE claim_participant_id = ? AND room_id = ?",
        (participant.id, participant.room_id),
    )
    events: list[EventEnvelope] = []
    for row in rows:
        affected = await tx.execute(
            """
            UPDATE tasks
            SET status = 'open', claim_lease_id = NULL, claim_participant_id = NULL,
                claim_fence = NULL, claim_claimed_at = NULL, claim_expires_at = NULL,
                claim_heartbeat_interval_s = NULL, claim_renewed_at = NULL,
                executor_attachment_id = NULL, executor_connection_id = NULL,
                updated_at = ?
            WHERE id = ? AND claim_participant_id = ?
            """,
            (utcnow_iso(), row["id"], participant.id),
        )
        if affected == 0:
            continue
        events.append(
            await eventlog.append(
                tx,
                room_id=participant.room_id,
                type_=EventType.TASK_CLAIM_RELEASED,
                actor=actor_for(participant),
                payload={
                    "task_id": row["id"],
                    "participant_id": participant.id,
                    "fence": row["claim_fence"],
                    "reason": reason,
                },
            )
        )
    return events


# ---------------------------------------------------------------------------
# Task mutation (fence-guarded)
# ---------------------------------------------------------------------------


def _assert_fence(row, fence: int | None) -> None:
    """Reject a mutation that does not present the task's current fence.

    Held tasks require a fence. Unheld tasks accept `None`, because there is no
    lease to be stale against. A *lower* fence is always refused — that is the
    zombie-writer case, and it is the single most important check in the system.
    """
    current = row["claim_fence"]
    expired = is_past(row["claim_expires_at"]) if row["claim_expires_at"] else True
    held = current is not None and not expired

    if not held:
        return
    if fence is None:
        raise StaleFence(
            "This task is held under a lease; present the current fence to modify it.",
            current_fence=int(current),
        )
    if int(fence) != int(current):
        raise StaleFence(
            "Your fence is not the task's current fence — you no longer hold this "
            "lease. Re-read the task before acting.",
            provided_fence=int(fence),
            current_fence=int(current),
        )


def _assert_holder(row, participant: Participant) -> None:
    """Reject a mutation to a held task by anyone but its holder.

    The fence is *not* a capability: it is published in the room projection and in
    `task.claimed` events, because every participant needs it to reason about
    staleness. So presenting the current fence proves only that the caller read the
    board — never that it holds the lease. Ownership is a separate check, and it is
    the one that makes a lease exclusive for anything beyond claim/renew/release.
    """
    holder = row["claim_participant_id"]
    expired = is_past(row["claim_expires_at"]) if row["claim_expires_at"] else True
    if holder is None or expired or holder == participant.id:
        return
    raise LeaseConflict(
        "Another participant holds this task under a live lease. Wait for it to be "
        "released or to expire; presenting its fence does not transfer it.",
        task_id=row["id"],
        held_by_participant_id=holder,
    )


def _require_live_lease(row, participant: Participant) -> None:
    """The full precondition for an operation that only a holder may perform.

    Three parts, and the third is the one D-026 left out: an *active* lease, held by
    *this* caller, at the current fence. `_assert_holder` alone is satisfied by a task
    nobody holds — and "nobody holds it" is not the same as "you hold it". Completing
    work you never claimed leaves the board asserting a job was done with no lease
    trail showing anyone did it (D-027).
    """
    _assert_holder(row, participant)
    holder = row["claim_participant_id"]
    expired = is_past(row["claim_expires_at"]) if row["claim_expires_at"] else True
    if holder is None or expired:
        raise LeaseRequired(
            "You hold no lease on this task, so you cannot finish it. Claim it first — "
            "the claim is what records that you were the one doing the work.",
            task_id=row["id"],
            status=row["status"],
        )


def require_override_authority(participant: Participant, reason: str, *, what: str) -> None:
    """Who may act on work they are not doing, and on what terms.

    `room.admin` and a reason. Nothing else — and in particular **not** whether the
    identity is a human principal, which is what this check used to accept.

    That was wrong in a way worth keeping written down, because it looked safe:
    `identity.kind` is stamped server-side, not supplied by the caller, so it is
    unforgeable. But unforgeable is not the property needed here. `kind` says whose
    identity this is; it says nothing about who is at the keyboard right now, so an
    unattended runtime holding a human-kind participant's credentials would have
    manufactured "a human said stop" merely by sharing the seat. Provenance is
    **attribution, not verification** (`docs/SECURITY.md`), and this is authorization.

    Human-ness is still recorded, as provenance on the event, where a claim about
    who acted belongs. It is never sufficient on its own.
    """
    if Scope.ROOM_ADMIN not in participant.scopes:
        raise ExecutorConflict(
            f"Only a room admin may {what}. Being a human principal is not the same "
            "as being authorized: the room can attribute an action to your identity, "
            "but it cannot verify that a person is present when it happens.",
            participant_id=participant.id,
        )
    if not reason.strip():
        raise InvalidCommand(
            f"To {what} you must give a reason, which is recorded in the room. An "
            "override nobody can audit is worse than no override.",
        )


async def take_over_execution(
    *, participant: Participant, command: TakeOverExecutionCommand
) -> Task:
    """Move execution of a lease from one runtime of a seat to another.

    Deliberately its own command rather than a flag on claim. Becoming the executor
    of work another runtime started is a real event in the room — the alternative is
    that it happens as a side effect of an idempotent re-claim, where nothing in the
    log distinguishes it from ordinary lease renewal.

    The fence increments, which is what makes it safe: the displaced runtime's next
    mutation fails as stale rather than landing late on work it no longer owns.
    """
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.TASK_CLAIM)
    authz.require_writable(room)

    executor = await presence.resolve_executor(
        participant=participant, connection_id=command.connection_id
    )

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room.id)
        )
        if row is None:
            raise NotFound("Task does not exist.", task_id=command.task_id)
        _assert_fence(row, command.fence)
        _require_live_lease(row, participant)

        previous = await presence.executor_of(row, tx=tx)
        if previous.ref == executor.ref:
            # Already the executor. Idempotent rather than an error, so a retry after
            # an ambiguous failure does not look like a second takeover.
            return CommandOutcome(result={"task_id": command.task_id})

        affected = await tx.execute(
            """
            UPDATE tasks
            SET fence = fence + 1,
                claim_fence = fence + 1,
                executor_attachment_id = ?,
                executor_connection_id = ?,
                updated_at = ?
            WHERE id = ? AND claim_participant_id = ? AND claim_fence = ?
              AND claim_expires_at > ?
            """,
            (
                executor.attachment_id,
                executor.connection_id,
                utcnow_iso(),
                command.task_id,
                participant.id,
                command.fence,
                utcnow_iso(),
            ),
        )
        if affected == 0:
            raise StaleFence(
                "The lease moved while you were taking it over. Read the task again.",
                task_id=command.task_id,
                fence=command.fence,
            )

        fence = int(
            await tx.fetch_value("SELECT fence FROM tasks WHERE id = ?", (command.task_id,))
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.TASK_EXECUTOR_CHANGED,
            actor=actor_for(participant),
            payload={
                "task_id": command.task_id,
                "participant_id": participant.id,
                "fence": fence,
                "previous_executor_ref": previous.ref,
                "previous_executor_live": previous.is_live,
                "executor_attachment_id": executor.attachment_id,
                "executor_connection_id": executor.connection_id,
                "reason": command.reason,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"task_id": command.task_id}, events=[event])

    await execute_command(
        command_id=command.command_id,
        command_type="task.take_over_execution",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await store.load_task(command.task_id)


def _require_not_halted(row, *, action: str) -> None:
    """Refuse progress on work a human has paused or stopped.

    Checked at `claim`, `complete` and `update` rather than returned as a field the
    worker is asked to respect. `stopped` blocks re-claim specifically, because
    without that "stop" would only mean "stop until your next loop iteration" — the
    worker would release, re-claim, and carry on having technically obeyed.
    """
    steering = Steering(row["steering"])
    if steering not in HALTED_STEERING:
        return
    if steering is Steering.PAUSED and action == "claim":
        # Pausing keeps the holder's place; it does not put the task beyond reach.
        return
    raise SteeringHalted(
        f"A human {steering.value} this task, so you may not {action} it: "
        f"{row['steering_reason'] or 'no reason given'}. It resumes when they say so.",
        task_id=row["id"],
        steering=steering.value,
        steering_reason=row["steering_reason"],
        steered_by_participant_id=row["steering_by_participant_id"],
    )


async def apply_steering_tx(
    tx: db.Tx,
    *,
    room: Room,
    participant: Participant,
    task_id: str,
    steering: Steering,
    reason: str,
    priority: int | None,
) -> list[EventEnvelope]:
    """Apply a human's directive to a task, inside the caller's transaction.

    Not a public command. Authority lives one layer up in `core.directives`, which
    decides *whether* someone may steer; this decides what steering does. Keeping
    them apart is what stops the enforcement point and the authorization point from
    drifting into each other.

    The holder and the executor are untouched, which is the entire difference
    between steering and seizing: a chat surface can stop a worker without becoming
    the thing that now has to finish the job. `stop` is the one action that also
    releases the lease — halting a task while leaving it held by someone who has
    been told to stop would freeze the work rather than free it, and the point of
    stopping is usually that somebody else should pick it up later.
    """
    row = await tx.fetch_one("SELECT * FROM tasks WHERE id = ? AND room_id = ?", (task_id, room.id))
    if row is None:
        raise NotFound("Task does not exist.", task_id=task_id)
    if row["status"] in {"done", "cancelled"}:
        raise InvalidCommand(
            "That task is already finished; there is nothing to steer.",
            task_id=task_id,
            status=row["status"],
        )

    previous = Steering(row["steering"])
    now = utcnow_iso()
    resolved_priority = row["priority"] if priority is None else priority
    await tx.execute(
        """
        UPDATE tasks
        SET steering = ?, steering_reason = ?, steering_by_participant_id = ?,
            steering_at = ?, priority = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            steering.value,
            reason,
            participant.id,
            now,
            resolved_priority,
            now,
            task_id,
        ),
    )
    events = [
        await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.TASK_STEERED,
            actor=actor_for(participant),
            payload={
                "task_id": task_id,
                "steering": steering.value,
                "previous": previous.value,
                "reason": reason,
                "priority": resolved_priority,
                "steered_by_participant_id": participant.id,
                # Stated rather than implied: steering did not move the work.
                "holder_participant_id": row["claim_participant_id"],
                "executor_attachment_id": row["executor_attachment_id"],
                "executor_connection_id": row["executor_connection_id"],
            },
        )
    ]

    if steering in HALTED_STEERING:
        # The work card outlives the task otherwise. After the first live stop the
        # board still read "Working: deploy the staging environment" against a task
        # nobody held and nobody could claim — asserting activity that had been
        # forbidden minutes earlier.
        from . import work as work_service

        events += await work_service.end_for_task_tx(
            tx, room=room, task_id=task_id, actor=participant, reason=f"steered {steering.value}"
        )

    if steering is Steering.STOPPED and row["claim_lease_id"]:
        # `fence` is deliberately not reset, so the stopped worker's fence stays
        # permanently unusable and a late write from it cannot land.
        affected = await tx.execute(
            """
            UPDATE tasks
            SET status = 'open', claim_lease_id = NULL, claim_participant_id = NULL,
                claim_fence = NULL, claim_claimed_at = NULL, claim_expires_at = NULL,
                claim_heartbeat_interval_s = NULL, claim_renewed_at = NULL,
                executor_attachment_id = NULL, executor_connection_id = NULL,
                updated_at = ?
            WHERE id = ? AND claim_lease_id = ?
            """,
            (now, task_id, row["claim_lease_id"]),
        )
        if affected:
            events.append(
                await eventlog.append(
                    tx,
                    room_id=room.id,
                    type_=EventType.TASK_CLAIM_RELEASED,
                    actor=actor_for(participant),
                    payload={
                        "task_id": task_id,
                        "participant_id": row["claim_participant_id"],
                        "fence": row["claim_fence"],
                        "note": reason,
                        "forced": True,
                        "reason": f"stopped by directive: {reason}",
                    },
                )
            )
    return events


async def _caller_executor(
    participant: Participant, connection_id: str | None, *, tx: db.Tx | None = None
) -> presence.Executor:
    """The caller's runtime, or nothing if it has no open connection.

    A caller with nothing open cannot be the runtime executing anything, so the
    empty executor is the truthful answer rather than an error — and it will not
    match a live one, which is exactly the outcome that case deserves.
    """
    try:
        return await presence.resolve_executor(
            participant=participant, connection_id=connection_id, tx=tx
        )
    except CapabilityUnsupported:
        return presence.Executor(attachment_id=None, connection_id=None, connections=())


async def _require_executor_or_dead(
    tx: db.Tx,
    row,
    *,
    participant: Participant,
    connection_id: str | None = None,
    executor: presence.Executor | None = None,
    action: str,
) -> None:
    """Only the runtime that started the work may act on it while it is still alive.

    Holding the lease is the seat's authority; executing is one runtime's. D-035
    settled that these come apart, using release as the example: a chat surface
    releasing a worker's lease is exactly as dangerous as seizing it, because both
    end with two runtimes free to perform the same external action. Cottage fencing
    protects Cottage state, never a deployment already half-done.

    A recorded executor with nothing live behind it is not an obstacle: the work is
    unattended by anyone, and refusing here would turn a crashed worker into a
    permanently stuck task, which is the locks-not-leases failure we exist to avoid.

    The caller's own runtime is resolved *only* when there is something to compare
    against. A lease with no recorded executor imposes no affinity, so demanding
    that the caller identify its runtime there would invent a requirement out of an
    absence — and would fail every pre-existing lease in the database.
    """
    current = await presence.executor_of(row, tx=tx)
    if current.ref is None or not current.is_live:
        return
    if executor is None:
        executor = await _caller_executor(participant, connection_id, tx=tx)
    if current.ref == executor.ref:
        return
    raise ExecutorConflict(
        f"Another live runtime of your own participant is executing this task, so you "
        f"may not {action} it. Take it over explicitly if that runtime is finished or "
        f"wrong — a takeover is visible in the room, and silently becoming the executor "
        f"is not.",
        task_id=row["id"],
        executor_ref=current.ref,
        your_executor_ref=executor.ref,
    )


async def update(*, participant: Participant, command: UpdateTaskCommand) -> Task:
    room = await store.load_room(participant.room_id)
    # `task.progress`, not `task.propose`: revising work you hold is a different
    # authority from creating work for someone else, and a runtime credential needs
    # only the first (D-048).
    authz.require_scope(participant, Scope.TASK_PROGRESS)
    authz.require_writable(room)
    privacy.inspect_content(command.title or "", command.description or "", command.targets or [])

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room.id)
        )
        if row is None:
            raise NotFound("Task does not exist.", task_id=command.task_id)
        _assert_fence(row, command.fence)
        _assert_holder(row, participant)
        _require_not_halted(row, action="update")
        await _require_executor_or_dead(
            tx,
            row,
            participant=participant,
            connection_id=command.connection_id,
            action="update",
        )

        title = command.title if command.title is not None else row["title"]
        description = command.description if command.description is not None else row["description"]
        targets = (
            _normalized_targets(command.targets)
            if command.targets is not None
            else db.str_list(row["targets"])
        )
        priority = command.priority if command.priority is not None else int(row["priority"])
        status = row["status"]
        if command.in_progress and status == "claimed":
            status = "in_progress"

        now = utcnow_iso()
        # The holder condition is repeated in SQL, not just checked above: under a
        # weaker isolation level than SQLite's a claim could land between the SELECT
        # and this UPDATE, and the affected-row count is what makes the guarantee
        # engine-neutral (ADR-009).
        affected = await tx.execute(
            """
            UPDATE tasks
            SET title = ?, description = ?, targets = ?, priority = ?, status = ?,
                updated_at = ?
            WHERE id = ?
              AND (claim_participant_id IS NULL OR claim_participant_id = ?
                   OR claim_expires_at <= ?)
            """,
            (
                title,
                description,
                db.dumps(targets),
                priority,
                status,
                now,
                command.task_id,
                participant.id,
                now,
            ),
        )
        if affected == 0:
            raise LeaseConflict(
                "Another participant claimed this task while you were editing it.",
                task_id=command.task_id,
            )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.TASK_UPDATED,
            actor=actor_for(participant),
            payload={
                "task_id": command.task_id,
                "title": title,
                "description": description,
                "targets": targets,
                "priority": priority,
                "status": status,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"task_id": command.task_id}, events=[event])

    await execute_command(
        command_id=command.command_id,
        command_type="task.update",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await store.load_task(command.task_id)


async def complete(*, participant: Participant, command: CompleteTaskCommand) -> Task:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.TASK_CLAIM)
    authz.require_writable(room)

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[command.result],
        known_participant_ids=known,
    )

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room.id)
        )
        if row is None:
            raise NotFound("Task does not exist.", task_id=command.task_id)
        if row["status"] == "done":
            return CommandOutcome(result={"task_id": command.task_id})
        _assert_fence(row, command.fence)
        # Steering is checked *before* the lease, and the order is the whole point of
        # the message. `stop` releases the hold, so a stopped worker asking to finish
        # has no lease — and would be told "claim it first", which is true, useless,
        # and one round trip away from being told the actual reason. The room knows
        # exactly why the lease went away; it should say so first.
        _require_not_halted(row, action="complete")
        _require_live_lease(row, participant)
        await _require_executor_or_dead(
            tx,
            row,
            participant=participant,
            connection_id=command.connection_id,
            action="complete",
        )

        now = utcnow_iso()
        # See the note in `update`: the holder condition belongs in the WHERE clause
        # so the exclusivity is the database's, not the read-then-write window's.
        affected = await tx.execute(
            """
            UPDATE tasks
            SET status = 'done', result = ?, completed_at = ?, updated_at = ?,
                claim_lease_id = NULL, claim_participant_id = NULL, claim_fence = NULL,
                claim_claimed_at = NULL, claim_expires_at = NULL,
                executor_attachment_id = NULL, executor_connection_id = NULL,
                claim_heartbeat_interval_s = NULL, claim_renewed_at = NULL
            WHERE id = ? AND status NOT IN ('done','cancelled')
              AND claim_participant_id = ? AND claim_expires_at > ?
            """,
            (command.result, now, now, command.task_id, participant.id, now),
        )
        if affected == 0:
            current = await tx.fetch_one("SELECT * FROM tasks WHERE id = ?", (command.task_id,))
            if current is not None and current["status"] not in ("done", "cancelled"):
                # The lease went away between the read and the write — expiry, or a
                # reclaim by someone else. Re-deriving the reason from the row keeps
                # the message true rather than merely convenient.
                _require_live_lease(current, participant)
            raise InvalidCommand("That task is already finished.", task_id=command.task_id)

        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.TASK_COMPLETED,
            actor=actor_for(participant),
            payload={
                "task_id": command.task_id,
                "participant_id": participant.id,
                "result": command.result,
                "fence": command.fence,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"task_id": command.task_id}, events=[event])

    await execute_command(
        command_id=command.command_id,
        command_type="task.complete",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await store.load_task(command.task_id)


async def cancel(*, participant: Participant, command: CancelTaskCommand) -> Task:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.TASK_PROPOSE)
    authz.require_writable(room)

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room.id)
        )
        if row is None:
            raise NotFound("Task does not exist.", task_id=command.task_id)
        # Only the creator or an admin may cancel: a claimant that wants out should
        # release, and any participant cancelling another's task would be a denial
        # of service on the board.
        if row["created_by_participant_id"] != participant.id:
            authz.require_admin(participant)

        affected = await tx.execute(
            """
            UPDATE tasks
            SET status = 'cancelled', updated_at = ?, claim_lease_id = NULL,
                claim_participant_id = NULL, claim_fence = NULL,
                claim_claimed_at = NULL, claim_expires_at = NULL,
                claim_heartbeat_interval_s = NULL, claim_renewed_at = NULL
            WHERE id = ? AND status NOT IN ('done','cancelled')
            """,
            (utcnow_iso(), command.task_id),
        )
        if affected == 0:
            return CommandOutcome(result={"task_id": command.task_id})
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.TASK_CANCELLED,
            actor=actor_for(participant),
            payload={"task_id": command.task_id, "reason": command.reason},
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"task_id": command.task_id}, events=[event])

    await execute_command(
        command_id=command.command_id,
        command_type="task.cancel",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await store.load_task(command.task_id)


# ---------------------------------------------------------------------------
# Proposals (minimal in M1; accept/reject/delegate depth is M3)
# ---------------------------------------------------------------------------


async def _propose_tx(
    tx: db.Tx,
    *,
    room: Room,
    participant: Participant,
    task_id: str,
    to_participant_id: str,
    note: str,
) -> EventEnvelope:
    proposal_id = ids.new_id(ids.PROPOSAL)
    await tx.execute(
        """
        INSERT INTO task_proposals (
            id, room_id, task_id, to_participant_id, proposed_by_participant_id,
            note, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (proposal_id, room.id, task_id, to_participant_id, participant.id, note, utcnow_iso()),
    )
    return await eventlog.append(
        tx,
        room_id=room.id,
        type_=EventType.TASK_PROPOSED,
        actor=actor_for(participant),
        payload={
            "proposal_id": proposal_id,
            "task_id": task_id,
            "to_participant_id": to_participant_id,
            "note": note,
        },
    )


# ---------------------------------------------------------------------------
# Lease reaper
# ---------------------------------------------------------------------------


async def reap_expired_leases() -> list[EventEnvelope]:
    """Return expired-lease tasks to `open` and emit `task.claim_expired`.

    Idempotent and racing-safe against a concurrent reclaim: the UPDATE is guarded
    on the same `claim_fence` that was read, so if someone reclaimed in between,
    zero rows are affected and this pass simply skips the task.
    """
    now = utcnow_iso()
    rows = await db.fetch_all(
        """
        SELECT id, room_id, claim_participant_id, claim_fence, claim_expires_at,
               executor_attachment_id, executor_connection_id
        FROM tasks
        WHERE claim_lease_id IS NOT NULL AND claim_expires_at <= ?
        """,
        (now,),
    )
    events: list[EventEnvelope] = []
    for row in rows:
        async with db.transaction() as tx:
            affected = await tx.execute(
                """
                UPDATE tasks
                SET status = 'open', claim_lease_id = NULL, claim_participant_id = NULL,
                    claim_fence = NULL, claim_claimed_at = NULL, claim_expires_at = NULL,
                    claim_heartbeat_interval_s = NULL, claim_renewed_at = NULL,
                    executor_attachment_id = NULL, executor_connection_id = NULL,
                    updated_at = ?
                WHERE id = ? AND claim_fence = ? AND claim_expires_at <= ?
                """,
                (utcnow_iso(), row["id"], row["claim_fence"], utcnow_iso()),
            )
            if affected == 0:
                continue
            events.append(
                await eventlog.append(
                    tx,
                    room_id=row["room_id"],
                    type_=EventType.TASK_CLAIM_EXPIRED,
                    actor=SYSTEM_ACTOR,
                    payload={
                        "task_id": row["id"],
                        "participant_id": row["claim_participant_id"],
                        "fence": row["claim_fence"],
                        "expired_at": row["claim_expires_at"],
                        # Carried into the log because the row loses it here. A
                        # recovery claim has to say which runtime went quiet
                        # mid-flight, and the event is the only place that survives
                        # (D-036, D-039).
                        "executor_attachment_id": row["executor_attachment_id"],
                        "executor_connection_id": row["executor_connection_id"],
                        "reason": "lease_expired",
                    },
                )
            )
        log.info("lease expired on task %s (fence %s)", row["id"], row["claim_fence"])

    await publish_committed(events)
    return events


def _normalized_targets(targets: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for raw in targets:
        key = normalize_target(raw)
        if key:
            seen.setdefault(key, None)
    return list(seen)

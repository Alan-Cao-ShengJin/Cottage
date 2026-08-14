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
    UpdateTaskCommand,
)
from ..domain.events import EventEnvelope, EventType
from ..domain.room import Participant, Room, Scope
from ..domain.task import HELD_TASK_STATUSES, Task, TaskStatus
from ..util import is_past, iso_in, normalize_target, utcnow_iso
from . import authz, conflicts, eventlog, presence, privacy, store
from .actors import SYSTEM_ACTOR, actor_for
from .dispatch import CommandOutcome, execute_command, publish_committed
from .errors import (
    CapabilityUnsupported,
    InvalidCommand,
    LeaseConflict,
    NotFound,
    StaleFence,
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
    if command.claim_immediately:
        # Resolved before the transaction so a capability refusal costs nothing.
        runtime = await presence.runtime_policy_for(participant, room)
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
        elif command.claim_immediately and runtime is not None:
            claimed = await _claim_tx(
                tx,
                room=room,
                participant=participant,
                task_id=task_id,
                lease_seconds=runtime.max_lease_seconds,
                heartbeat_interval_s=runtime.heartbeat_interval_s,
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
        },
    )
    return [event]


async def claim(*, participant: Participant, command: ClaimTaskCommand) -> Task:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.TASK_CLAIM)
    authz.require_writable(room)

    runtime = await presence.runtime_policy_for(participant, room)
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

        affected = await tx.execute(
            """
            UPDATE tasks
            SET status = 'open', claim_lease_id = NULL, claim_participant_id = NULL,
                claim_fence = NULL, claim_claimed_at = NULL, claim_expires_at = NULL,
                claim_heartbeat_interval_s = NULL, claim_renewed_at = NULL,
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


async def update(*, participant: Participant, command: UpdateTaskCommand) -> Task:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.TASK_PROPOSE)
    authz.require_writable(room)
    privacy.inspect_content(command.title or "", command.description or "", command.targets or [])

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room.id)
        )
        if row is None:
            raise NotFound("Task does not exist.", task_id=command.task_id)
        _assert_fence(row, command.fence)

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

        await tx.execute(
            """
            UPDATE tasks
            SET title = ?, description = ?, targets = ?, priority = ?, status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                description,
                db.dumps(targets),
                priority,
                status,
                utcnow_iso(),
                command.task_id,
            ),
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

        now = utcnow_iso()
        affected = await tx.execute(
            """
            UPDATE tasks
            SET status = 'done', result = ?, completed_at = ?, updated_at = ?,
                claim_lease_id = NULL, claim_participant_id = NULL, claim_fence = NULL,
                claim_claimed_at = NULL, claim_expires_at = NULL,
                claim_heartbeat_interval_s = NULL, claim_renewed_at = NULL
            WHERE id = ? AND status NOT IN ('done','cancelled')
            """,
            (command.result, now, now, command.task_id),
        )
        if affected == 0:
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
        SELECT id, room_id, claim_participant_id, claim_fence, claim_expires_at
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

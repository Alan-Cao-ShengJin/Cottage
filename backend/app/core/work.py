"""Current-work declarations — the primary product surface.

A declaration answers "what is this participant doing right now", which is what
makes concurrent work by separately-owned agents divisible instead of colliding.
Two design points carry weight:

* **Targets are the coordination key**, not the prose headline. Normalized target
  overlap between two active declarations is what raises `overlapping_work`, so two
  independently-written clients can collide detectably without agreeing on wording.
* **Staleness is derived from presence**, not asserted. A declaration whose owner
  stopped heartbeating is shown but marked stale, because the alternative — quietly
  keeping it as current — is how a room ends up coordinating around work that
  stopped an hour ago.
* **Two clocks, because being alive and making progress are two claims** (D-059).
  `heartbeat_at` answers "is the owner's runtime still here" and is refreshed by the
  connection heartbeat, so a worker inside a single long step stops being reported as
  stuck. `progress_at` answers "did the work itself move" and is refreshed only by
  declare, update, or a checkpoint on the task — never by a transport beat. Collapsing
  them would make staleness unreachable for anything with a live socket, which is a
  status that means nothing.
"""

from __future__ import annotations

import logging

from ..db import database as db
from ..domain import ids
from ..domain.commands import DeclareWorkCommand, EndWorkCommand, UpdateWorkCommand
from ..domain.events import EventEnvelope, EventType
from ..domain.room import Participant, Room, Scope
from ..domain.work import WorkDeclaration, WorkEndReason, WorkStatus
from ..util import from_iso, normalize_target, utcnow, utcnow_iso
from . import authz, conflicts, eventlog, privacy, store
from .actors import SYSTEM_ACTOR, actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import InvalidCommand

log = logging.getLogger(__name__)


async def declare(*, participant: Participant, command: DeclareWorkCommand) -> WorkDeclaration:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.WORK_DECLARE)
    authz.require_writable(room)

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[command.headline, command.note, command.targets],
        known_participant_ids=known,
    )

    targets = _normalized_targets(command.targets)
    work_id = ids.new_id(ids.WORK)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        await tx.execute(
            """
            INSERT INTO work_declarations (
                id, room_id, participant_id, headline, status, targets, task_id,
                note, privacy_class, started_at, updated_at, heartbeat_at,
                progress_at, expected_done_by
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                work_id,
                room.id,
                participant.id,
                command.headline,
                command.status.value,
                db.dumps(targets),
                command.task_id,
                command.note,
                decision.privacy_class.value,
                now,
                now,
                now,
                now,
                command.expected_done_by,
            ),
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.WORK_DECLARED,
            actor=actor_for(participant),
            payload={
                "work_id": work_id,
                "participant_id": participant.id,
                "headline": command.headline,
                "status": command.status.value,
                "targets": targets,
                "task_id": command.task_id,
                "note": command.note,
                "expected_done_by": command.expected_done_by,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        events: list[EventEnvelope] = [event]
        # Detection runs inside the same transaction so a conflict cannot exist
        # without the declaration that caused it, or vice versa.
        events += await conflicts.detect_overlapping_work_tx(
            tx, room=room, work_id=work_id, participant=participant, targets=targets
        )
        return CommandOutcome(result={"work_id": work_id}, events=events)

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="work.declare",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    # A replay must return the declaration the first attempt created; `work_id` above
    # was generated for a body that never ran.
    return await store.load_work(str(outcome.result.get("work_id", work_id)))


async def update(*, participant: Participant, command: UpdateWorkCommand) -> WorkDeclaration:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.WORK_DECLARE)
    authz.require_writable(room)

    existing = await store.load_work(command.work_id)
    if existing.room_id != room.id:
        raise InvalidCommand("That work declaration belongs to another room.")
    # Ownership is checked separately from scope: holding `work.declare` does not
    # let you rewrite someone else's declaration, which would forge attribution.
    authz.require_owns(participant, existing.participant_id, what="work declarations")
    if existing.ended_at:
        raise InvalidCommand("That work declaration has already ended.")

    privacy.inspect_content(command.headline or "", command.note or "", command.targets or [])

    headline = command.headline if command.headline is not None else existing.headline
    note = command.note if command.note is not None else existing.note
    status = command.status or existing.status
    targets = (
        _normalized_targets(command.targets) if command.targets is not None else existing.targets
    )
    expected = (
        command.expected_done_by
        if command.expected_done_by is not None
        else existing.expected_done_by
    )
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        await tx.execute(
            """
            UPDATE work_declarations
            SET headline = ?, status = ?, targets = ?, note = ?,
                expected_done_by = ?, updated_at = ?, heartbeat_at = ?, progress_at = ?
            WHERE id = ? AND ended_at IS NULL
            """,
            (
                headline,
                status.value,
                db.dumps(targets),
                note,
                expected,
                now,
                now,
                # An explicit update is the owner speaking about *this* work, so it
                # counts as progress — this is the one call a client can still make to
                # say "yes, really, still moving" when a step is unusually long.
                now,
                command.work_id,
            ),
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.WORK_UPDATED,
            actor=actor_for(participant),
            payload={
                "work_id": command.work_id,
                "participant_id": participant.id,
                "headline": headline,
                "status": status.value,
                "targets": targets,
                "note": note,
                "expected_done_by": expected,
            },
            causation_id=command.command_id,
        )
        events: list[EventEnvelope] = [event]
        if command.targets is not None:
            events += await conflicts.detect_overlapping_work_tx(
                tx, room=room, work_id=command.work_id, participant=participant, targets=targets
            )
        return CommandOutcome(result={"work_id": command.work_id}, events=events)

    await execute_command(
        command_id=command.command_id,
        command_type="work.update",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await store.load_work(command.work_id)


async def end(*, participant: Participant, command: EndWorkCommand) -> WorkDeclaration:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.WORK_DECLARE)

    existing = await store.load_work(command.work_id)
    authz.require_owns(participant, existing.participant_id, what="work declarations")

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            """
            UPDATE work_declarations
            SET ended_at = ?, end_reason = ?, status = ?, updated_at = ?, note = ?
            WHERE id = ? AND ended_at IS NULL
            """,
            (
                utcnow_iso(),
                WorkEndReason.COMPLETED.value,
                WorkStatus.DONE.value,
                utcnow_iso(),
                command.note or existing.note,
                command.work_id,
            ),
        )
        if affected == 0:
            # Already ended. Idempotent by design: a retrying client should not get
            # an error for a state that is already what it wanted.
            return CommandOutcome(result={"work_id": command.work_id})
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.WORK_ENDED,
            actor=actor_for(participant),
            payload={
                "work_id": command.work_id,
                "participant_id": participant.id,
                "reason": WorkEndReason.COMPLETED.value,
                "note": command.note,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"work_id": command.work_id}, events=[event])

    await execute_command(
        command_id=command.command_id,
        command_type="work.end",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await store.load_work(command.work_id)


async def end_all_open_tx(
    tx: db.Tx, *, participant: Participant, reason: str
) -> list[EventEnvelope]:
    """End every open declaration for a participant, inside the caller's transaction.

    Called on graceful leave and on losing the last connection: an unowned "current
    work" card is worse than no card, because other participants coordinate around it.
    """
    rows = await tx.fetch_all(
        "SELECT id FROM work_declarations WHERE participant_id = ? AND ended_at IS NULL",
        (participant.id,),
    )
    if not rows:
        return []

    end_reason = (
        WorkEndReason.PRESENCE_LOST.value
        if reason in {"presence_lost", "participant_left"}
        else WorkEndReason.ABANDONED.value
    )
    now = utcnow_iso()
    events: list[EventEnvelope] = []
    for row in rows:
        await tx.execute(
            "UPDATE work_declarations SET ended_at = ?, end_reason = ?, updated_at = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (now, end_reason, now, row["id"]),
        )
        events.append(
            await eventlog.append(
                tx,
                room_id=participant.room_id,
                type_=EventType.WORK_ENDED,
                actor=actor_for(participant),
                payload={
                    "work_id": row["id"],
                    "participant_id": participant.id,
                    "reason": end_reason,
                    "detail": reason,
                },
            )
        )
    return events


async def end_for_task_tx(
    tx: db.Tx,
    *,
    room: Room,
    task_id: str,
    actor: Participant,
    reason: str,
    end_reason: WorkEndReason = WorkEndReason.SUPERSEDED,
) -> list[EventEnvelope]:
    """End the declarations attached to a task that has just stopped or finished.

    A work card outlives its task otherwise. The stop test left one reading
    "Working: deploy the staging environment" against a task nobody held and
    nobody could claim — the board asserting activity that had been forbidden
    minutes earlier, which is worse than showing nothing.

    **Called from every terminal path, not only the interesting one.** This was
    wired into `stop` when the stop proof exposed it, and *not* into `complete` or
    `cancel` — so a worker that finished normally left its card open until staleness
    reaped it, and the room reported a busy worker between tasks. Found by the
    ChatGPT participant watching a companion go idle and seeing `work.stale` instead
    of `work.ended` (D-057). The lesson is that fixing a defect on the path that
    surfaced it is half a fix: the bug was in the *lifecycle*, and the lifecycle has
    four exits.

    `end_reason` distinguishes them, because "finished" and "superseded by a human
    stopping you" are different facts about the same card and a reader deciding
    whether the work got done needs to tell them apart.
    """
    rows = await tx.fetch_all(
        "SELECT id, participant_id FROM work_declarations "
        "WHERE room_id = ? AND task_id = ? AND ended_at IS NULL",
        (room.id, task_id),
    )
    now = utcnow_iso()
    events: list[EventEnvelope] = []
    for row in rows:
        await tx.execute(
            "UPDATE work_declarations SET ended_at = ?, end_reason = ?, updated_at = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (now, end_reason.value, now, row["id"]),
        )
        events.append(
            await eventlog.append(
                tx,
                room_id=room.id,
                type_=EventType.WORK_ENDED,
                actor=actor_for(actor),
                payload={
                    "work_id": row["id"],
                    "participant_id": row["participant_id"],
                    "reason": end_reason.value,
                    "detail": reason,
                    "task_id": task_id,
                },
            )
        )
    return events


async def touch_owner_heartbeats(participant_id: str) -> None:
    """The owner's connection beat, so its open declarations are still current.

    One beat means "I am here and so is my work" (D-059). Before this, a participant
    could be graded `live_poll` while the room called its declared work
    `heartbeat_lapsed` — the same silence read two contradictory ways, and the board
    reported working agents as stuck. Two independent participants hit it in one room,
    and the second had already written the client-side workaround and *still* lost the
    race against a 120s threshold. A liveness signal a client must send twice, on two
    clocks, for two subsystems, will be sent once.

    Deliberately not an event, for the same reason heartbeats themselves are not:
    heartbeats at 20s x N participants would drown the log (`docs/PROTOCOL.md` §3).
    This writes a liveness timestamp, which is derived-state maintenance, not a state
    change the room needs to replay — nothing about *what is being worked on* moves.

    Declarations already flipped to `blocked` are refreshed too but stay blocked: the
    heartbeat is evidence about the runtime, not a retraction of the stale finding, and
    only the owner can say the work is live again.
    """
    await db.execute(
        "UPDATE work_declarations SET heartbeat_at = ? WHERE participant_id = ? "
        "AND ended_at IS NULL",
        (utcnow_iso(), participant_id),
    )


async def note_progress_tx(tx: db.Tx, *, room_id: str, task_id: str) -> None:
    """A checkpoint landed on this task, so the work attached to it demonstrably moved.

    This is the clock a transport beat cannot forge. A checkpoint is a worker saying
    what it just finished, which is evidence of progress in a way "my socket is open"
    is not — so it, not the heartbeat, is what holds off `no_progress`.

    Inside the caller's transaction: the checkpoint and the freshness it implies are
    one fact, and a crash between them would leave the room believing a worker that
    just reported a completed step had stopped moving.
    """
    now = utcnow_iso()
    await tx.execute(
        "UPDATE work_declarations SET heartbeat_at = ?, progress_at = ? "
        "WHERE room_id = ? AND task_id = ? AND ended_at IS NULL",
        (now, now, room_id, task_id),
    )


async def mark_stale_declarations(room: Room) -> list[EventEnvelope]:
    """Emit `work.stale` for declarations that can no longer be trusted as current.

    Three ways to get here, and they say different things (D-059):

    * `owner_presence_lost` — the owner is stale or gone. Untouched by the two-clock
      change and deliberately so; it is the path that was always right.
    * `heartbeat_lapsed` — nothing has beaten for this seat. Now a real transport
      silence rather than "the worker was busy for two minutes".
    * `no_progress` — beating steadily, but no declare, update, or checkpoint inside
      `work_progress_stale_after_seconds`. This is what a wedged worker looks like, and
      it is why the heartbeat refresh does not make staleness unreachable.

    Emitted once per declaration — the guard is `status != 'blocked'` plus the
    freshness checks, and the status flip to `blocked` is what makes it non-repeating.
    """
    from . import presence

    presences = await presence.presence_for_room(room)
    cutoff = room.policy.work_stale_after_seconds
    progress_cutoff = room.policy.work_progress_stale_after_seconds
    now = utcnow()
    events: list[EventEnvelope] = []

    for work in await store.list_open_work(room.id):
        view = presences.get(work.participant_id)
        heartbeat_age = (now - from_iso(work.heartbeat_at)).total_seconds()
        progress_age = (now - from_iso(work.progress_at)).total_seconds()
        owner_gone = view is None or view.liveness.value in {"stale", "disconnected"}
        stalled = progress_age > progress_cutoff
        if not (owner_gone or heartbeat_age > cutoff or stalled):
            continue
        if work.status == WorkStatus.BLOCKED:
            continue
        # Ordered by what a reader should act on. A vanished owner explains the
        # silence, so it outranks both timers; a dead transport explains missing
        # progress, so it outranks that.
        if owner_gone:
            reason = "owner_presence_lost"
        elif heartbeat_age > cutoff:
            reason = "heartbeat_lapsed"
        else:
            reason = "no_progress"
        async with db.transaction() as tx:
            affected = await tx.execute(
                "UPDATE work_declarations SET status = 'blocked', updated_at = ? "
                "WHERE id = ? AND ended_at IS NULL AND status != 'blocked'",
                (utcnow_iso(), work.id),
            )
            if affected == 0:
                continue
            events.append(
                await eventlog.append(
                    tx,
                    room_id=room.id,
                    type_=EventType.WORK_STALE,
                    actor=SYSTEM_ACTOR,
                    payload={
                        "work_id": work.id,
                        "participant_id": work.participant_id,
                        "last_heartbeat_at": work.heartbeat_at,
                        "last_progress_at": work.progress_at,
                        "reason": reason,
                    },
                )
            )
    return events


async def is_stale(work: WorkDeclaration, room: Room, *, now=None) -> bool:
    """Both clocks, since either one lapsing makes the card untrustworthy."""
    at = now or utcnow()
    heartbeat_age = (at - from_iso(work.heartbeat_at)).total_seconds()
    progress_age = (at - from_iso(work.progress_at)).total_seconds()
    return (
        heartbeat_age > room.policy.work_stale_after_seconds
        or progress_age > room.policy.work_progress_stale_after_seconds
    )


def _normalized_targets(targets: list[str]) -> list[str]:
    """Deduplicated, canonical target keys.

    Normalizing at the boundary means overlap detection compares apples to apples
    even though `./src/api.py` and `src/api.py` came from different clients.
    """
    seen: dict[str, None] = {}
    for raw in targets:
        key = normalize_target(raw)
        if key:
            seen.setdefault(key, None)
    return list(seen)

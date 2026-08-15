"""The control plane: how a human steers a running agent (D-045).

Two rules shape everything here.

**Authority is a grant, never an inference.** Issuing a directive requires
`room.admin`. It deliberately does *not* accept "the issuing identity is a human
principal" as authorization, even though that fact is stamped server-side and cannot
be forged by a caller. Unforgeable is not the property needed: `kind` says whose
identity this is, not who is at the keyboard, so an unattended runtime holding a
human-kind participant's credentials could otherwise manufacture "a human said stop"
out of its own token. Human-ness is recorded as provenance, where a claim about who
acted belongs, and is never sufficient on its own.

**Effect and observation are orthogonal.** A control directive applies in the same
transaction it is issued in. Waiting for the target to acknowledge would make
stopping a runaway worker depend on the cooperation of the runaway worker — the
exact property the whole feature exists to avoid. Acknowledgement is recorded
separately as evidence that the target noticed, so *applied but never acknowledged*
is a state the room can state plainly rather than an awkward gap in a lifecycle.

`input` is the single exception: there is no room state to halt, so nothing can be
applied until the target consumes it. Waiting there is intrinsic, not a failure.
"""

from __future__ import annotations

import logging

from ..db import database as db
from ..domain import ids
from ..domain.commands import AcknowledgeDirectiveCommand, IssueDirectiveCommand
from ..domain.directive import (
    CONTROL_ACTIONS,
    Directive,
    DirectiveAction,
    EffectStatus,
)
from ..domain.events import EventEnvelope, EventType
from ..domain.identity import PrincipalKind
from ..domain.room import Participant
from ..domain.task import Steering
from ..util import utcnow_iso
from . import authz, eventlog, store, tasks
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import Forbidden, InvalidCommand, NotFound

log = logging.getLogger(__name__)

#: What each control action means to the task layer.
_STEERING_FOR: dict[DirectiveAction, Steering] = {
    DirectiveAction.PAUSE: Steering.PAUSED,
    DirectiveAction.STOP: Steering.STOPPED,
    DirectiveAction.RESUME: Steering.RUNNING,
    DirectiveAction.REPRIORITIZE: Steering.RUNNING,
}


async def issue(*, participant: Participant, command: IssueDirectiveCommand) -> Directive:
    """Direct a participant, applying any control effect in the same transaction."""
    room = await store.load_room(participant.room_id)
    authz.require_writable(room)

    halting = command.action in {DirectiveAction.PAUSE, DirectiveAction.STOP}
    tasks.require_override_authority(
        participant,
        command.reason if halting else (command.reason or "-"),
        what=f"{command.action.value} another participant's work",
    )

    target = await store.load_participant(command.target_participant_id)
    if target.room_id != room.id:
        raise NotFound(
            "That participant is not in this room.",
            target_participant_id=command.target_participant_id,
        )
    if command.action in CONTROL_ACTIONS and command.task_id is None:
        raise InvalidCommand(
            "A control directive needs a task. Pausing or stopping 'in general' is "
            "not something the task layer can enforce, and an unenforceable "
            "directive is a message wearing a uniform.",
            action=command.action.value,
        )

    directive_id = ids.new_id(ids.DIRECTIVE)
    now = utcnow_iso()
    applies_now = command.action in CONTROL_ACTIONS
    # Attribution only. Recorded because an audit wants to know that a human
    # principal issued this, and never consulted to decide whether they may.
    human_origin = participant.identity.kind == PrincipalKind.HUMAN

    async def body(tx: db.Tx) -> CommandOutcome:
        events: list[EventEnvelope] = []
        if applies_now and command.task_id is not None:
            events += await tasks.apply_steering_tx(
                tx,
                room=room,
                participant=participant,
                task_id=command.task_id,
                steering=_STEERING_FOR[command.action],
                reason=command.reason,
                priority=command.priority,
            )

        issued = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.DIRECTIVE_ISSUED,
            actor=actor_for(participant),
            payload={
                "directive_id": directive_id,
                "target_participant_id": target.id,
                "task_id": command.task_id,
                "action": command.action.value,
                "reason": command.reason,
                "priority": command.priority,
                "human_origin": human_origin,
                "effect_status": (
                    EffectStatus.APPLIED.value if applies_now else EffectStatus.PENDING.value
                ),
            },
        )
        events.append(issued)

        await tx.execute(
            """
            INSERT INTO directives (
                id, room_id, target_participant_id, task_id, action, reason,
                issued_by_participant_id, human_origin, created_seq, effect_status,
                created_at, applied_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                directive_id,
                room.id,
                target.id,
                command.task_id,
                command.action.value,
                command.reason,
                participant.id,
                1 if human_origin else 0,
                issued.seq,
                EffectStatus.APPLIED.value if applies_now else EffectStatus.PENDING.value,
                now,
                now if applies_now else None,
            ),
        )
        return CommandOutcome(result={"directive_id": directive_id}, events=events)

    await execute_command(
        command_id=command.command_id,
        command_type="directive.issue",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await load(directive_id)


async def acknowledge(
    *, participant: Participant, command: AcknowledgeDirectiveCommand
) -> Directive:
    """Record that the target saw it — and, for `input`, that it consumed it.

    Never re-applies or undoes an effect. Acknowledging a stop does not re-stop
    anything; it only means the room can now say the worker knew.
    """
    room = await store.load_room(participant.room_id)

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM directives WHERE id = ? AND room_id = ?",
            (command.directive_id, room.id),
        )
        if row is None:
            raise NotFound("Directive does not exist.", directive_id=command.directive_id)
        if row["target_participant_id"] != participant.id:
            raise Forbidden(
                "Only the participant a directive was addressed to may acknowledge it. "
                "Acknowledgement is evidence that the target saw it, so someone else "
                "acknowledging would be evidence of nothing.",
                directive_id=command.directive_id,
            )
        if row["acknowledged_at"]:
            # Idempotent: a retry after an ambiguous failure must not look like a
            # second, later observation.
            return CommandOutcome(result={"directive_id": command.directive_id})

        action = DirectiveAction(row["action"])
        now = utcnow_iso()
        effect = EffectStatus(row["effect_status"])
        if effect is EffectStatus.PENDING:
            # Only `input` can still be pending, and consuming it is what applies it.
            effect = EffectStatus.REJECTED if command.rejected else EffectStatus.APPLIED

        await tx.execute(
            """
            UPDATE directives
            SET acknowledged_at = ?, acknowledged_by_participant_id = ?,
                effect_status = ?, applied_at = COALESCE(applied_at, ?)
            WHERE id = ? AND acknowledged_at IS NULL
            """,
            (
                now,
                participant.id,
                effect.value,
                now if effect is EffectStatus.APPLIED else None,
                command.directive_id,
            ),
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.DIRECTIVE_ACKNOWLEDGED,
            actor=actor_for(participant),
            payload={
                "directive_id": command.directive_id,
                "action": action.value,
                "effect_status": effect.value,
                "rejected": command.rejected,
                "note": command.note,
                "issued_at_seq": int(row["created_seq"]),
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"directive_id": command.directive_id}, events=[event])

    await execute_command(
        command_id=command.command_id,
        command_type="directive.acknowledge",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await load(command.directive_id)


async def load(directive_id: str) -> Directive:
    row = await db.fetch_one("SELECT * FROM directives WHERE id = ?", (directive_id,))
    if row is None:
        raise NotFound("Directive does not exist.", directive_id=directive_id)
    return store.to_directive(row)


async def open_for(
    participant_id: str, *, tx: db.Tx | None = None, limit: int = 25
) -> list[Directive]:
    """Directives still wanting this participant's attention, oldest first.

    Oldest first because these are instructions: a worker that reads its newest
    directive and stops has skipped the ones before it. Ordinary room events are
    read newest-first for context, which is the opposite need and the reason these
    are carried separately rather than mixed in.
    """
    sql = """
        SELECT * FROM directives
        WHERE target_participant_id = ?
          AND (acknowledged_at IS NULL OR effect_status = 'pending')
        ORDER BY created_seq ASC
        LIMIT ?
    """
    rows = await (
        tx.fetch_all(sql, (participant_id, limit))
        if tx is not None
        else db.fetch_all(sql, (participant_id, limit))
    )
    return [store.to_directive(r) for r in rows]

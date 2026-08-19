"""Validated operational state for persistent runtime attachments.

Operational state is a projected coordination fact, not presence. The caller may
describe its current work posture, but core resolves the attachment from a live
connection and validates referenced work before committing it. Liveness remains
derived exclusively by ``core.presence``.
"""

from __future__ import annotations

from typing import Any

from ..db import database as db
from ..domain.commands import SetRuntimeStateCommand
from ..domain.disclosure import Disclosure
from ..domain.events import EventType
from ..domain.room import Participant, RuntimeOperationalState, Scope
from ..util import utcnow_iso
from . import authz, eventlog, privacy, store
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import InvalidCommand, NotFound


async def set_state(*, participant: Participant, command: SetRuntimeStateCommand) -> dict[str, Any]:
    """Project the caller runtime's work posture and append its durable event."""
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_scope(participant, Scope.WORK_DECLARE)
    authz.require_writable(room)

    summary = command.summary.strip()
    waiting_reason = command.waiting_reason.strip()
    if command.state is RuntimeOperationalState.WORKING and not summary:
        raise InvalidCommand("Working runtime state requires a truthful summary.")
    if command.state is RuntimeOperationalState.WAITING and not waiting_reason:
        raise InvalidCommand("Waiting runtime state requires the dependency it is waiting on.")
    if command.state is RuntimeOperationalState.MONITORING:
        summary = summary or "Monitoring room activity"
        waiting_reason = ""
    # Monitoring ends active cognition, not necessarily the runtime's durable task
    # context. Preserve explicit references in every state; the validation below
    # still prevents a runtime from inventing tasks or another seat's work card.
    task_id = command.task_id
    work_id = command.work_id

    known = [p.id for p in await store.list_participants(room.id)]
    privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=Disclosure(),
        content=[summary, waiting_reason],
        known_participant_ids=known,
    )

    async def body(tx: db.Tx) -> CommandOutcome:
        conn = await tx.fetch_one(
            "SELECT attachment_id FROM connections WHERE id = ? AND room_id = ? "
            "AND participant_id = ? AND closed_at IS NULL",
            (command.connection_id, room.id, participant.id),
        )
        if conn is None:
            raise InvalidCommand(
                "Runtime state requires a live connection owned by this participant.",
                connection_id=command.connection_id,
            )
        attachment_id = conn["attachment_id"]
        if attachment_id is None:
            raise InvalidCommand(
                "Runtime state requires a durable attachment; reconnect with an attachment label."
            )

        attachment = await tx.fetch_one(
            "SELECT * FROM attachments WHERE id = ? AND participant_id = ?",
            (attachment_id, participant.id),
        )
        if attachment is None:
            raise NotFound("Runtime attachment does not exist.", attachment_id=attachment_id)

        if task_id is not None:
            task = await tx.fetch_one(
                "SELECT claim_participant_id, executor_attachment_id, status FROM tasks "
                "WHERE id = ? AND room_id = ?",
                (task_id, room.id),
            )
            if task is None:
                raise NotFound("Task does not exist.", task_id=task_id)
            if command.state is RuntimeOperationalState.WORKING and (
                task["claim_participant_id"] != participant.id
                or task["executor_attachment_id"] != attachment_id
            ):
                raise InvalidCommand(
                    "A runtime may only report working on a task it is currently executing.",
                    task_id=task_id,
                    attachment_id=attachment_id,
                )

        if work_id is not None:
            work = await tx.fetch_one(
                "SELECT participant_id, ended_at FROM work_declarations "
                "WHERE id = ? AND room_id = ?",
                (work_id, room.id),
            )
            if work is None:
                raise NotFound("Work declaration does not exist.", work_id=work_id)
            if work["participant_id"] != participant.id or work["ended_at"] is not None:
                raise InvalidCommand("Runtime state may only reference your current work.")

        previous = {
            "state": attachment["operational_state"],
            "summary": attachment["operational_summary"],
            "waiting_reason": attachment["waiting_reason"],
            "task_id": attachment["operational_task_id"],
            "work_id": attachment["operational_work_id"],
        }
        current = {
            "state": command.state.value,
            "summary": summary,
            "waiting_reason": waiting_reason,
            "task_id": task_id,
            "work_id": work_id,
        }
        if previous == current:
            return CommandOutcome(result={"attachment_id": attachment_id, **current})

        updated_at = utcnow_iso()
        await tx.execute(
            "UPDATE attachments SET operational_state = ?, operational_summary = ?, "
            "waiting_reason = ?, operational_task_id = ?, operational_work_id = ?, "
            "operational_updated_at = ? WHERE id = ?",
            (
                command.state.value,
                summary,
                waiting_reason,
                task_id,
                work_id,
                updated_at,
                attachment_id,
            ),
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.RUNTIME_STATE_CHANGED,
            actor=actor_for(participant),
            payload={
                "participant_id": participant.id,
                "attachment_id": attachment_id,
                **current,
                "updated_at": updated_at,
                "previous_state": previous["state"],
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={"attachment_id": attachment_id, **current, "updated_at": updated_at},
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="runtime.state.set",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}

"""Live activity notes — the narration between state changes (D-082).

Read `domain/activity.py` for what this is and, more importantly, what it is not.
The implementation is short because the design is a restriction: a note appends one
event and touches nothing else.

Two things it deliberately does *not* do, both of which would have been easy:

**It writes no projection.** There is no activity table to fall out of step with the
log, which is this codebase's recurring defect shape (D-046, D-049, D-053, D-061).
A feed whose current view is derived from the event stream cannot drift from it.

**It refreshes no clock.** A note is not evidence that work moved, so it must not
touch `progress_at` — that is exactly the property D-059 built the second clock to
preserve. A wedged worker narrating "still working" every ten seconds must still go
`no_progress`, or the narration channel becomes a way to look busy without being
busy. Saying so is not the same as doing so, and only `declare`, `update` and
`checkpoint` are allowed to claim the latter.

When a connection is supplied it validates and records the durable runtime
attachment that produced the note. It deliberately does not refresh that
connection: monitor heartbeats remain independent from narration, so repeatedly
saying "still working" cannot manufacture liveness either.
"""

from __future__ import annotations

import logging

from ..db import database as db
from ..domain.activity import ActivityPhase
from ..domain.commands import NoteActivityCommand
from ..domain.events import EventType
from ..domain.room import Participant, Scope
from . import authz, eventlog, privacy, store
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import InvalidCommand

log = logging.getLogger(__name__)

#: Phases where naming what is being run is the whole point of the note.
_TOOL_PHASES = {ActivityPhase.TOOL_STARTED, ActivityPhase.TOOL_FINISHED}


async def note(*, participant: Participant, command: NoteActivityCommand) -> dict:
    """Append one breadcrumb. Changes no state, grants nothing, blocks nothing."""
    room = await store.load_room(participant.room_id)
    # `work.declare` rather than a new scope: this is the same authority as saying
    # what you are doing, at a finer grain. A participant that may declare its work
    # may narrate it, and one that may not, may not — inventing a second scope for
    # the same sentence would let the two drift apart.
    authz.require_scope(participant, Scope.WORK_DECLARE)
    authz.require_writable(room)

    if command.phase in _TOOL_PHASES and not command.tool:
        raise InvalidCommand(
            "A tool phase must name the tool it is bracketing, or a watcher sees a "
            "duration start and end with nothing in between.",
            phase=command.phase.value,
        )

    # Free text, arriving frequently, from an agent that may be untrusted — so the
    # full boundary, not a length check. `tool` is inspected too: it is the field
    # most likely to receive a command line, and command lines carry credentials.
    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[command.summary, command.tool],
        known_participant_ids=known,
    )

    async def body(tx: db.Tx) -> CommandOutcome:
        attachment_id: str | None = None
        if command.connection_id:
            connection = await tx.fetch_one(
                "SELECT attachment_id FROM connections WHERE id = ? AND room_id = ? "
                "AND participant_id = ? AND closed_at IS NULL",
                (command.connection_id, room.id, participant.id),
            )
            if connection is None:
                raise InvalidCommand(
                    "Activity attribution requires a live connection owned by this participant.",
                    connection_id=command.connection_id,
                )
            attachment_id = connection["attachment_id"]

        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.ACTIVITY_NOTED,
            actor=actor_for(participant),
            payload={
                "participant_id": participant.id,
                "attachment_id": attachment_id,
                "phase": command.phase.value,
                "summary": command.summary,
                "tool": command.tool,
                "task_id": command.task_id,
                "work_id": command.work_id,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        # Deliberately the whole body. No projection write, no clock refresh.
        return CommandOutcome(
            result={"phase": command.phase.value, "attachment_id": attachment_id},
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="activity.note",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}

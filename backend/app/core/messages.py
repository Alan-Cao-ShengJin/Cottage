"""Messages — the coordination annotation channel.

Messages are deliberately a *minor* surface (`docs/PRODUCT.md` §1). They exist to
annotate coordination — "I'm blocked on your task", "why did you change that
target" — and they carry an `about_ref` so a message attaches to the task, work
item, or artifact it concerns instead of floating in a transcript.

They are still free text, which is exactly why every one goes through the disclosure
boundary: a message body is the easiest place for an agent to accidentally paste a
credential or a chunk of its private context.
"""

from __future__ import annotations

import logging

from ..db import database as db
from ..domain import ids
from ..domain.commands import PostMessageCommand
from ..domain.events import EventType
from ..domain.room import Participant, Scope
from ..util import utcnow_iso
from . import authz, eventlog, privacy, store
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command

log = logging.getLogger(__name__)


async def post(*, participant: Participant, command: PostMessageCommand) -> dict:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.MESSAGE_POST)
    authz.require_writable(room)

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[command.body],
        known_participant_ids=known,
        max_text_chars=room.policy.max_message_chars,
    )

    message_id = ids.new_id(ids.MESSAGE)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.MESSAGE_POSTED,
            actor=actor_for(participant),
            payload={
                "message_id": message_id,
                "participant_id": participant.id,
                "body": command.body,
                "about_ref": command.about_ref,
                "to_participant_id": decision.to_participant_id,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        # The projection stores the seq the event got, so a UI reading messages
        # directly can still order them against the event stream.
        await tx.execute(
            """
            INSERT INTO messages (
                id, room_id, seq, participant_id, body, about_ref,
                privacy_class, audience, to_participant_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                message_id,
                room.id,
                event.seq,
                participant.id,
                command.body,
                command.about_ref,
                decision.privacy_class.value,
                decision.audience.value,
                decision.to_participant_id,
                now,
            ),
        )
        return CommandOutcome(result={"message_id": message_id}, events=[event])

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="message.post",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    # A replay returns the original message id, not the one generated above for a
    # body that never ran.
    return {
        "message_id": str(outcome.result.get("message_id", message_id)),
        "seq": outcome.seq,
        "replayed": outcome.replayed,
    }

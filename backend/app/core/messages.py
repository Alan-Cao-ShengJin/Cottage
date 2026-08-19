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
from ..domain.message import Speaker
from ..domain.room import Participant, Scope
from ..util import utcnow_iso
from . import authz, eventlog, privacy, store
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import InvalidCommand

log = logging.getLogger(__name__)


async def post(*, participant: Participant, command: PostMessageCommand) -> dict:
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.MESSAGE_POST)
    authz.require_writable(room)

    if command.speaking_as and command.speaking_for is not Speaker.HUMAN:
        # A name for a person, attached to a message the caller says is its own words. One of
        # the two is wrong and the room cannot tell which, so it refuses rather than picking:
        # storing the name would attribute the agent's words to a person, and dropping it
        # would silently discard an attribution somebody asked for.
        raise InvalidCommand(
            "`speaking_as` names the person you are relaying, so it needs "
            'speaking_for="human". Leave it empty when the words are your own.',
            speaking_for=command.speaking_for.value,
        )

    known = [p.id for p in await store.list_participants(room.id)]
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=command.disclosure,
        content=[command.body, command.speaking_as],
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
                # Whose words these are, as the author declared them. On the event because
                # that is what `relevance` reads to decide whether other agents are woken,
                # and because a reader six months from now needs to know that "anyone want
                # lunch?" was a person talking and not a coordination instruction (D-090).
                "speaking_for": command.speaking_for.value,
                #: Self-asserted, and readers show the seat beside it.
                "speaking_as": command.speaking_as,
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
                privacy_class, audience, to_participant_id, speaking_for, speaking_as,
                created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
                command.speaking_for.value,
                command.speaking_as,
                now,
            ),
        )
        return CommandOutcome(result={"message_id": message_id, "created_at": now}, events=[event])

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
        # The row's own timestamp, so a client rendering a receipt shows when the room
        # recorded this rather than when its adapter happened to look at a clock.
        "created_at": str(outcome.result.get("created_at", now)),
    }

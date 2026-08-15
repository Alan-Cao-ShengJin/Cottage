"""Granting and revoking authority inside a room (D-045).

This exists because B uncovered a blocker rather than because the roadmap asked
for it: `room.admin` was held only by the seat that created a room, and there was
no way to grant it. So the humans' own control surfaces — their ChatGPT session,
their Claude Code session — could never steer their own workers. `participant.
scopes_changed` was in the event registry with nothing able to emit it, which is
the documentation-and-code disagreement CLAUDE.md says to stop and resolve.
"""

from __future__ import annotations

import pytest

from app.core import rooms, store
from app.core.errors import Forbidden, NotFound
from app.db import database as db
from app.domain.commands import SetParticipantRoleCommand
from app.domain.identity import TrustTier
from app.domain.room import ParticipantRole, Scope

pytestmark = pytest.mark.asyncio


async def test_an_owner_can_make_a_collaborator_an_admin(make_room, join):
    """The blocker itself: a control surface that can be given authority."""
    room = await make_room()
    surface = await join(room, display_name="Alan's chat")
    assert Scope.ROOM_ADMIN not in surface.participant.scopes

    updated = await rooms.set_participant_role(
        participant=room.owner,
        command=SetParticipantRoleCommand(
            target_participant_id=surface.participant.id,
            role=ParticipantRole.OWNER,
            reason="this is the surface I steer from",
        ),
    )
    assert Scope.ROOM_ADMIN in updated.scopes
    assert (await store.load_participant(surface.participant.id)).scopes == updated.scopes


async def test_a_collaborator_cannot_promote_themselves(make_room, join):
    """Otherwise the authority model is decoration."""
    room = await make_room()
    surface = await join(room, display_name="Ambitious agent")

    with pytest.raises(Forbidden):
        await rooms.set_participant_role(
            participant=surface.participant,
            command=SetParticipantRoleCommand(
                target_participant_id=surface.participant.id,
                role=ParticipantRole.OWNER,
            ),
        )


async def test_a_grant_cannot_exceed_the_roles_defaults(make_room, join):
    """Narrowing only, the same rule invitations obey.

    A promotion is a move within the rules, never a way around them — otherwise
    `set_role` becomes the one place privileges can be minted from nothing.
    """
    room = await make_room()
    observer = await join(room, display_name="Watcher", role=ParticipantRole.OBSERVER)

    updated = await rooms.set_participant_role(
        participant=room.owner,
        command=SetParticipantRoleCommand(
            target_participant_id=observer.participant.id,
            role=ParticipantRole.OBSERVER,
            scopes=[Scope.ROOM_ADMIN, Scope.TASK_CLAIM, Scope.ROOM_READ],
        ),
    )
    assert Scope.ROOM_ADMIN not in updated.scopes
    assert Scope.TASK_CLAIM not in updated.scopes
    assert Scope.ROOM_READ in updated.scopes


async def test_an_untrusted_identity_stays_denied_even_when_promoted(make_room, join):
    """Trust clamping outranks a role, and a promotion must not be a way past it."""
    room = await make_room()
    stranger = await join(room, display_name="Stranger", trust=TrustTier.UNTRUSTED)

    updated = await rooms.set_participant_role(
        participant=room.owner,
        command=SetParticipantRoleCommand(
            target_participant_id=stranger.participant.id,
            role=ParticipantRole.OWNER,
            reason="trying to hand the room to a guest",
        ),
    )
    assert Scope.ROOM_ADMIN not in updated.scopes
    assert Scope.TASK_CLAIM not in updated.scopes


async def test_promoting_someone_from_another_room_is_not_found(make_room, join):
    room = await make_room()
    other = await make_room(name="Somewhere else")
    outsider = await join(other, display_name="Elsewhere")

    with pytest.raises(NotFound):
        await rooms.set_participant_role(
            participant=room.owner,
            command=SetParticipantRoleCommand(
                target_participant_id=outsider.participant.id,
                role=ParticipantRole.OWNER,
            ),
        )


async def test_the_change_is_an_event_with_who_did_it(make_room, join):
    """A privilege change nobody can audit is worse than none."""
    room = await make_room()
    surface = await join(room, display_name="Alan's chat")

    await rooms.set_participant_role(
        participant=room.owner,
        command=SetParticipantRoleCommand(
            target_participant_id=surface.participant.id,
            role=ParticipantRole.OWNER,
            reason="control surface for the unattended worker",
        ),
    )
    rows = await db.fetch_all(
        "SELECT * FROM room_events WHERE type = 'participant.scopes_changed' AND room_id = ?",
        (room.room.id,),
    )
    assert len(rows) == 1
    payload = db.loads(rows[0]["payload"], {})
    assert payload["participant_id"] == surface.participant.id
    assert payload["changed_by_participant_id"] == room.owner.id
    assert "unattended worker" in payload["reason"]
    assert Scope.ROOM_ADMIN.value in payload["scopes"]


async def test_demotion_works_and_is_not_special_cased(make_room, join):
    """Including of yourself. Refusing would be guessing at an intent plainly stated."""
    room = await make_room()
    surface = await join(room, display_name="Alan's chat")
    promoted = await rooms.set_participant_role(
        participant=room.owner,
        command=SetParticipantRoleCommand(
            target_participant_id=surface.participant.id, role=ParticipantRole.OWNER
        ),
    )
    assert Scope.ROOM_ADMIN in promoted.scopes

    demoted = await rooms.set_participant_role(
        participant=promoted,
        command=SetParticipantRoleCommand(
            target_participant_id=promoted.id,
            role=ParticipantRole.COLLABORATOR,
            reason="handing control back",
        ),
    )
    assert Scope.ROOM_ADMIN not in demoted.scopes

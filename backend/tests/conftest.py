"""Test fixtures: a throwaway database per test, plus a small room-building kit.

The bus is a module-level singleton, so it is cleared in place rather than replaced
— modules that imported it by name would otherwise keep pointing at the old object.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from app.core import presence, rooms
from app.core.bus import bus
from app.db import database as db
from app.domain.capabilities import Capability, HostClass
from app.domain.commands import (
    ConnectCommand,
    CreateInvitationCommand,
    CreateRoomCommand,
    JoinRoomCommand,
)
from app.domain.identity import PrincipalKind, TrustTier
from app.domain.room import (
    Participant,
    ParticipantRole,
    Room,
    RoomPolicy,
    RoomVisibility,
    Scope,
)

#: A fully-capable declaration: pushable, unattended, can execute. The baseline for
#: a participant that may hold a normal-length lease.
FULL_CAPABILITIES = [
    Capability.CAN_RECEIVE_EVENTS,
    Capability.SUPPORTS_PUSH,
    Capability.SUPPORTS_RESUME,
    Capability.CAN_INITIATE_FOLLOWUP,
    Capability.CAN_EXECUTE_BACKGROUND,
    Capability.SUPPORTS_TOOLS,
    Capability.SUPPORTS_ARTIFACTS,
]

#: Declares it only acts while a human is engaged. Used to prove lease policy tracks
#: capabilities rather than labels.
ATTENDED_CAPABILITIES = [
    Capability.CAN_RECEIVE_EVENTS,
    Capability.SUPPORTS_POLL,
    Capability.REQUIRES_HUMAN_PRESENCE,
    Capability.SUPPORTS_TOOLS,
]


@pytest_asyncio.fixture
async def fresh_db(tmp_path: Path):
    original = db.get_database_path()
    db.set_database_path(tmp_path / "test.db")
    await db.init_db()
    bus.clear()
    yield
    bus.clear()
    # Close the pooled connections while this test's loop is still running. Without it
    # the worker threads for a deleted tmp_path database would outlive the test.
    await db.shutdown()
    db.set_database_path(original)


@dataclass
class Member:
    """A joined participant plus the handles a test needs to act as it."""

    participant: Participant
    token: str
    connection_id: str
    identity_id: str

    async def reload(self) -> Participant:
        from app.core import store

        self.participant = await store.load_participant(self.participant.id)
        return self.participant


@dataclass
class RoomFixture:
    room: Room
    owner_user_id: str
    org_id: str
    #: The creator, joined as owner by `room.create` itself.
    owner: Participant
    owner_token: str
    #: The shareable join token minted with the room.
    join_token: str

    async def refresh(self) -> Room:
        from app.core import store

        self.room = await store.load_room(self.room.id)
        return self.room


@pytest_asyncio.fixture
async def org():
    org_id, user_id = await rooms.ensure_org_and_user(
        org_name="Acme", org_slug="acme", email="owner@acme.test", display_name="Owner"
    )
    return org_id, user_id


@pytest_asyncio.fixture
async def make_room(fresh_db, org):
    org_id, user_id = org

    async def _make(
        *,
        visibility: RoomVisibility = RoomVisibility.INTERNAL,
        policy: RoomPolicy | None = None,
        name: str = "Test room",
    ) -> RoomFixture:
        from app.core import store

        user_row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
        created = await rooms.create_room(
            user=store.to_user(user_row),
            command=CreateRoomCommand(name=name, visibility=visibility, policy=policy),
            creator_display_name="Room Owner",
        )
        return RoomFixture(
            room=created.room,
            owner_user_id=user_id,
            org_id=org_id,
            owner=created.participant,
            owner_token=created.participant_token,
            join_token=created.join_token,
        )

    return _make


@pytest_asyncio.fixture
async def join(fresh_db):
    """Invite + join + connect, returning a `Member`.

    Bootstrapping through the real invitation path (rather than inserting a
    participant row) is deliberate: it means every test exercises scope resolution
    and trust clamping instead of bypassing them.
    """

    async def _join(
        room_fixture: RoomFixture,
        *,
        display_name: str,
        role: ParticipantRole = ParticipantRole.COLLABORATOR,
        scopes: list[Scope] | None = None,
        capabilities: list[Capability] | None = None,
        host_class: HostClass = HostClass.PERSISTENT_LOCAL,
        kind: PrincipalKind = PrincipalKind.AGENT,
        transport: str = "sse",
        org_id: str | None = None,
        trust: TrustTier = TrustTier.MEMBER,
        connect: bool = True,
    ) -> Member:
        from app.core import store

        # The room's owner exists because `room.create` made them one, so nothing here
        # has to seed a participant row by hand. Every join below goes through real
        # invitation redemption, which means scope resolution and trust clamping are
        # exercised on every test rather than bypassed.
        issued = await rooms.create_invitation(
            participant=room_fixture.owner,
            command=CreateInvitationCommand(role=role, scopes=scopes),
        )

        identity = await rooms.create_identity(
            org_id=org_id or room_fixture.org_id,
            owner_user_id=room_fixture.owner_user_id,
            display_name=display_name,
            kind=kind,
            host_class=host_class,
            capabilities=capabilities or FULL_CAPABILITIES,
            trust=trust,
        )
        result = await rooms.join_room(
            identity=identity,
            command=JoinRoomCommand(
                invitation_token=issued.token,
                display_name=display_name,
                host_class=host_class,
                capabilities=capabilities or FULL_CAPABILITIES,
            ),
        )
        connection_id = ""
        if connect:
            negotiated = await presence.connect(
                participant=result.participant,
                command=ConnectCommand(
                    capabilities=capabilities or FULL_CAPABILITIES, host_class=host_class
                ),
                transport=transport,
            )
            connection_id = negotiated.connection.id
        return Member(
            participant=await store.load_participant(result.participant.id),
            token=result.participant_token,
            connection_id=connection_id,
            identity_id=identity.id,
        )

    return _join


# There is deliberately no `_ensure_admin` helper here any more. It used to seed an
# owner participant row directly, because minting the first invitation required an admin
# that could not yet exist. `room.create` now joins its creator, so the bootstrap paradox
# is gone and no test bypasses invitation redemption.

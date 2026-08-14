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
        room = await rooms.create_room(
            user=store.to_user(user_row),
            command=CreateRoomCommand(name=name, visibility=visibility, policy=policy),
        )
        return RoomFixture(room=room, owner_user_id=user_id, org_id=org_id)

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

        # An admin participant is needed to mint invitations; the room owner is
        # bootstrapped once and reused.
        admin = await _ensure_admin(room_fixture)

        issued = await rooms.create_invitation(
            participant=admin,
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


_ADMINS: dict[str, Participant] = {}


async def _ensure_admin(room_fixture: RoomFixture) -> Participant:
    """The room's first participant, created directly as owner.

    This is the one place a test bypasses invitation redemption, because minting the
    first invitation requires an admin to already exist. Everything else goes through
    the real path.
    """
    from app.core import store

    cached = _ADMINS.get(room_fixture.room.id)
    if cached is not None:
        return cached

    identity = await rooms.create_identity(
        org_id=room_fixture.org_id,
        owner_user_id=room_fixture.owner_user_id,
        display_name="Room Owner",
        kind=PrincipalKind.HUMAN,
        host_class=HostClass.BROWSER_HUMAN,
        capabilities=FULL_CAPABILITIES,
    )
    from app.core.authz import effective_scopes
    from app.domain import ids
    from app.util import hash_token, new_token, utcnow_iso

    participant_id = ids.new_id(ids.PARTICIPANT)
    token = new_token()
    await db.execute(
        """
        INSERT INTO participants (
            id, room_id, agent_identity_id, org_id, role, scopes, trust, state,
            display_name, token_hash, joined_at
        ) VALUES (?,?,?,?,'owner',?,'member','joined',?,?,?)
        """,
        (
            participant_id,
            room_fixture.room.id,
            identity.id,
            room_fixture.org_id,
            db.dumps(
                [s.value for s in effective_scopes(ParticipantRole.OWNER, None, TrustTier.MEMBER)]
            ),
            "Room Owner",
            hash_token(token),
            utcnow_iso(),
        ),
    )
    participant = await store.load_participant(participant_id)
    _ADMINS[room_fixture.room.id] = participant
    return participant


@pytest.fixture(autouse=True)
def _clear_admin_cache():
    yield
    _ADMINS.clear()

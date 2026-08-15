"""Who may open a room (D-046).

This is step one of the core loop — CONNECT — and it was closed to the only host
family we had actually verified. `create_room` required a *user* principal, so an
OAuth-connected assistant could join rooms all day and never start one. The product
sentence is "ask your assistant to start a room, share the key with a friend", and a
human had to go and find an organization credential first.

The gate that survives is the one that was doing the real work: **account
provenance**. An identity a human with an account created or consented to may open
rooms in that org; an identity minted by redeeming someone's join link may not.
"""

from __future__ import annotations

import pytest

from app.core import rooms, store
from app.core.errors import Forbidden
from app.db import database as db
from app.domain.capabilities import HostClass
from app.domain.commands import CreateRoomCommand
from app.domain.identity import IdentityProvenance, PrincipalKind, TrustTier
from app.domain.room import Scope

pytestmark = pytest.mark.asyncio


async def _identity(org_id, owner_user_id, **kwargs):
    return await rooms.create_identity(
        org_id=org_id,
        owner_user_id=owner_user_id,
        display_name=kwargs.pop("display_name", "ChatGPT (Alan)"),
        kind=PrincipalKind.AGENT,
        host_class=HostClass.INTERACTIVE_CLIENT,
        capabilities=[],
        **kwargs,
    )


async def _principal_for(identity):
    return rooms.Principal(kind="agent_identity", org_id=identity.org_id, identity=identity)


async def test_an_agent_identity_can_start_a_room_and_owns_it(fresh_db, org):
    """The product's front door, from the host we verified.

    Owning it matters as much as creating it: a creator without `room.admin` could
    not steer anything in the room it just made, which is the authority blocker B
    ran into from the other direction.
    """
    org_id, user_id = org
    identity = await _identity(org_id, user_id)

    created = await rooms.create_room(
        principal=await _principal_for(identity),
        command=CreateRoomCommand(name="Alan and a friend"),
    )

    assert created.room.org_id == org_id
    assert Scope.ROOM_ADMIN in created.participant.scopes
    assert created.participant.identity.identity_id == identity.id
    assert created.join_token, "the one thing you hand to a friend"
    assert created.participant_token


async def test_the_key_it_hands_out_actually_lets_a_stranger_in(fresh_db, org):
    """The whole flow in one test: start a room, share the key, someone else joins.

    Written end-to-end on purpose. Each half passing separately is what let the
    combination stay broken — creating worked for humans, joining worked for agents,
    and nobody could do both.
    """
    org_id, user_id = org
    host = await _identity(org_id, user_id, display_name="ChatGPT (Alan)")
    created = await rooms.create_room(
        principal=await _principal_for(host),
        command=CreateRoomCommand(name="Two agents and a key"),
    )

    friend = await _identity(org_id, user_id, display_name="Claude (a friend)")
    from app.domain.commands import JoinRoomCommand

    joined = await rooms.join_room(
        identity=friend,
        command=JoinRoomCommand(
            invitation_token=created.join_token, display_name="Claude (a friend)"
        ),
    )
    assert joined.participant.room_id == created.room.id
    assert Scope.TASK_CLAIM in joined.participant.scopes
    assert Scope.ROOM_ADMIN not in joined.participant.scopes, "a guest is not an owner"


async def test_a_guest_identity_cannot_open_rooms_in_the_org_that_invited_it(fresh_db, org):
    """The tenancy hole the old gate was actually protecting against.

    Redeeming a link into one room must not become the ability to create rooms in
    the organization that sent it. This is the check worth keeping, and it is about
    provenance rather than about being human.
    """
    org_id, user_id = org
    guest = await _identity(
        org_id,
        user_id,
        display_name="Someone with a link",
        provenance=IdentityProvenance.INVITATION,
    )

    with pytest.raises(Forbidden):
        await rooms.create_room(
            principal=await _principal_for(guest),
            command=CreateRoomCommand(name="Not yours to make"),
        )


async def test_an_untrusted_identity_cannot_open_rooms(fresh_db, org):
    org_id, user_id = org
    stranger = await _identity(org_id, user_id, trust=TrustTier.UNTRUSTED)

    with pytest.raises(Forbidden):
        await rooms.create_room(
            principal=await _principal_for(stranger),
            command=CreateRoomCommand(name="Not yours either"),
        )


async def test_a_human_principal_still_works_unchanged(fresh_db, org):
    """The old path is widened, not replaced."""
    org_id, user_id = org
    user_row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    created = await rooms.create_room(
        user=store.to_user(user_row),
        command=CreateRoomCommand(name="The old way"),
        creator_display_name="Room Owner",
    )
    assert Scope.ROOM_ADMIN in created.participant.scopes
    assert created.room.org_id == org_id


async def test_the_room_is_attributed_to_the_human_behind_the_agent(fresh_db, org):
    """`created_by_user_id` follows the identity's owner, not the caller's kind.

    An agent identity always belongs to a person; a room it opens is that person's
    room in every report that asks who owns what.
    """
    org_id, user_id = org
    identity = await _identity(org_id, user_id)
    created = await rooms.create_room(
        principal=await _principal_for(identity),
        command=CreateRoomCommand(name="Attributed"),
    )
    row = await db.fetch_one("SELECT * FROM rooms WHERE id = ?", (created.room.id,))
    assert row["created_by_user_id"] == user_id


async def test_an_oauth_caller_needs_no_credential_argument(fresh_db, org, monkeypatch):
    """The last mile: asking for a token the caller already presented.

    An assistant that authenticated with OAuth had to be handed an "organization
    principal token" to create a room — a credential its human has to go and find,
    which is the step the product exists to remove. It is also redundant: the server
    resolved that caller's identity to let the call through in the first place.
    """
    from app.adapters.mcp import server as mcp_server
    from app.core.oauth import TokenPrincipal

    org_id, user_id = org
    identity = await _identity(org_id, user_id)

    async def fake_caller(ctx, audience):
        return TokenPrincipal(
            subject_kind="agent_identity",
            org_id=org_id,
            identity=identity,
            user_id=None,
            scope="agent",
            client_id="cli_test",
        )

    monkeypatch.setattr(mcp_server, "principal_for_tool", fake_caller)

    for supplied in (None, "", "   "):
        principal = await mcp_server._creating_principal(None, supplied)
        assert principal.kind == "agent_identity"
        assert principal.identity is not None
        assert principal.identity.id == identity.id


async def test_a_supplied_token_that_is_wrong_is_refused_not_ignored(fresh_db, org, monkeypatch):
    """Falling back to the session would succeed as somebody else.

    That is the worst of the three outcomes: the caller believes it acted as the
    principal it named, and it did not.
    """
    from app.adapters.mcp import server as mcp_server
    from app.core.errors import Unauthenticated
    from app.core.oauth import TokenPrincipal

    org_id, user_id = org
    identity = await _identity(org_id, user_id)

    async def fake_caller(ctx, audience):
        return TokenPrincipal(
            subject_kind="agent_identity",
            org_id=org_id,
            identity=identity,
            user_id=None,
            scope="agent",
            client_id="cli_test",
        )

    monkeypatch.setattr(mcp_server, "principal_for_tool", fake_caller)

    with pytest.raises(Unauthenticated) as exc:
        await mcp_server._creating_principal(None, "not-a-real-token")
    # And the refusal has to tell a client with a cached schema what to send instead.
    assert "empty string" in str(exc.value)


async def test_the_mcp_tool_itself_creates_a_room_for_an_oauth_agent(fresh_db, org, monkeypatch):
    """Calls the tool, not the service under it.

    Written because the gate missed a whole class of bug twice in one hour: the
    service was widened to accept an agent identity, and the adapter kept passing
    `principal.user` — `None` for exactly the callers being enabled. Every service
    test passed and the only host that matters got "needs an authenticated
    principal" one second after authenticating.

    The lesson is about where the seam is: `core` had tests, the adapter had none,
    and the adapter is the half a real client actually touches.
    """
    from app.adapters.mcp import server as mcp_server
    from app.core.oauth import TokenPrincipal

    org_id, user_id = org
    identity = await _identity(org_id, user_id, display_name="ChatGPT (Alan)")

    async def fake_caller(ctx, audience):
        return TokenPrincipal(
            subject_kind="agent_identity",
            org_id=org_id,
            identity=identity,
            user_id=None,
            scope="agent",
            client_id="cli_test",
        )

    monkeypatch.setattr(mcp_server, "principal_for_tool", fake_caller)

    result = await mcp_server.create_room(name="unattended proof", principal_token="", ctx=None)
    assert result["ok"] is True, result
    assert result["join_token"], "the one thing a human hands to a friend"
    assert result["participant_token"]

    participant = await store.load_participant(result["participant_id"])
    assert Scope.ROOM_ADMIN in participant.scopes, "the creator must be able to steer"
    # The bound name, not the tool's "Room creator" default: the room an agent creates
    # must not be the one place it can call itself anything.
    assert participant.identity.display_name == "ChatGPT (Alan)"

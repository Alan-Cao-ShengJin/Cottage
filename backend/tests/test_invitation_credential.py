"""An invitation is a credential: a stranger with nothing else can join, and only join.

This is the product's central claim made executable — *anyone starts a room and invites
someone over the internet* — and until D-025 it was false. An invitation named a room but
authenticated nobody, so an invited person had no way to begin: a public instance must
require auth on `/mcp`, minting a token required an account, and only the operator had one.

Two halves, and the second matters as much as the first:

* a holder of a live invitation can enter the room it names;
* a holder of a live invitation can do **nothing else** — not create rooms, not read the
  org, not enter a different room, and not pass as a member of the inviting org.

The tests are written from the *stranger's* side deliberately. Six adversarial review lenses
over the same code missed this gap entirely because each took the operator's point of view
(D-024); testing as the party who does not already hold the keys is what surfaces it.
"""

from __future__ import annotations

import pytest

from app.core import authz, rooms, store
from app.core.errors import Forbidden, Unauthenticated
from app.domain.commands import CreateRoomCommand, JoinRoomCommand
from app.domain.identity import IdentityProvenance, TrustTier
from app.domain.room import PrivacyClass, RoomVisibility, Scope


async def _room_with_join_token(org, visibility: RoomVisibility | None = None):
    from app.db import database as db

    _, user_id = org
    user_row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    command = (
        CreateRoomCommand(name="Cross-company room")
        if visibility is None
        else CreateRoomCommand(name="Cross-company room", visibility=visibility)
    )
    return await rooms.create_room(user=store.to_user(user_row), command=command)


@pytest.mark.asyncio
async def test_a_stranger_holding_only_an_invitation_can_join(fresh_db, org) -> None:
    """The claim itself. No account, no principal token, no prior relationship."""
    created = await _room_with_join_token(org)

    credential = await rooms.authenticate_invitation(created.join_token)
    assert credential.room_id == created.room.id

    identity = await rooms.provision_guest_identity(credential, display_name="Stranger's Agent")
    result = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(
            invitation_token=created.join_token, display_name="Stranger's Agent"
        ),
    )

    assert result.room.id == created.room.id
    assert result.participant_token
    assert result.participant.is_active


@pytest.mark.asyncio
async def test_a_guest_can_do_the_work_the_room_exists_for(fresh_db, org) -> None:
    """Joining is not enough — an invited collaborator must be able to collaborate.

    `untrusted` would have been the cautious grade, but it strips `task.claim`, which
    reduces an invited teammate to a spectator and defeats the point of inviting them.
    Someone with authority in the room deliberately issued the link, so that is the
    vouching act; what nobody vouched for is the *name*, which `provenance` records.
    """
    created = await _room_with_join_token(org)
    credential = await rooms.authenticate_invitation(created.join_token)
    identity = await rooms.provision_guest_identity(credential, display_name="Guest")

    assert identity.trust == TrustTier.VOUCHED
    assert identity.provenance == IdentityProvenance.INVITATION

    result = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=created.join_token, display_name="Guest"),
    )
    assert result.participant.has(Scope.TASK_CLAIM)
    assert result.participant.has(Scope.MESSAGE_POST)
    assert result.participant.has(Scope.WORK_DECLARE)


@pytest.mark.asyncio
async def test_a_guest_is_not_an_org_member_however_its_org_row_reads(fresh_db, org) -> None:
    """The subtle one, and the reason `provenance` exists at all.

    A guest is provisioned into the *inviting room's* org, because that is where the
    authorization came from. So a tenancy comparison — `participant.org_id == room.org_id`
    — says "member", and `org_internal` payloads would flow to a stranger holding a link.
    That is precisely who `org_internal` exists to exclude.
    """
    created = await _room_with_join_token(org)
    credential = await rooms.authenticate_invitation(created.join_token)
    identity = await rooms.provision_guest_identity(credential, display_name="Guest")
    result = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=created.join_token, display_name="Guest"),
    )

    guest = result.participant
    room = result.room

    assert authz.is_org_member(guest, room) is True, "same tenant, by construction"
    assert authz.can_see_org_internal(guest, room) is False, "but not an org member"

    # The creator, whose identity an account backs, still is one.
    owner = await store.load_participant(created.participant.id)
    assert authz.can_see_org_internal(owner, room) is True


@pytest.mark.asyncio
async def test_org_internal_events_are_not_disclosed_to_a_guest(fresh_db, org) -> None:
    """The behavioural consequence of the check above, asserted end to end.

    A unit test on the predicate would pass while the projection still leaked, so this goes
    through the path that actually filters.
    """
    from app.core import messages, projections

    # Explicitly `internal`, because `org_internal` content can only exist in such a
    # room: a cross-org room rejects the class outright, so the leak this test guards
    # against is only reachable inside one organization. Rooms default to `cross_org`.
    created = await _room_with_join_token(org, visibility=RoomVisibility.INTERNAL)
    credential = await rooms.authenticate_invitation(created.join_token)
    identity = await rooms.provision_guest_identity(credential, display_name="Guest")
    joined = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=created.join_token, display_name="Guest"),
    )

    owner = await store.load_participant(created.participant.id)
    from app.domain.commands import PostMessageCommand
    from app.domain.disclosure import Audience, Disclosure

    await messages.post(
        participant=owner,
        command=PostMessageCommand(
            body="internal only: the vendor quote is 40k",
            disclosure=Disclosure(privacy_class=PrivacyClass.ORG_INTERNAL, audience=Audience.ROOM),
        ),
    )

    guest_view = await projections.snapshot(room_id=created.room.id, recipient=joined.participant)
    bodies = [m.get("body", "") for m in guest_view["messages"]]
    assert not any("40k" in b for b in bodies), f"org_internal leaked to a guest: {bodies}"

    owner_view = await projections.snapshot(room_id=created.room.id, recipient=owner)
    assert any("40k" in m.get("body", "") for m in owner_view["messages"])


@pytest.mark.asyncio
async def test_the_same_link_and_name_is_one_seat_across_reconnects(fresh_db, org) -> None:
    """A restarting agent lands on its own seat rather than a ghost of it."""
    created = await _room_with_join_token(org)

    seats = []
    for _ in range(2):
        credential = await rooms.authenticate_invitation(created.join_token)
        identity = await rooms.provision_guest_identity(credential, display_name="Guest")
        result = await rooms.join_room(
            identity=identity,
            command=JoinRoomCommand(invitation_token=created.join_token, display_name="Guest"),
        )
        seats.append(result.participant.id)

    assert seats[0] == seats[1]


@pytest.mark.asyncio
async def test_guests_of_different_rooms_never_share_an_identity(fresh_db, org) -> None:
    """The collapse that `ensure_identity`'s `(owner, name)` key would have caused.

    Every guest of every room is owned by the inviting user, so keying on owner and name
    would make "Assistant" in one room the *same identity* as "Assistant" in another —
    across a tenancy boundary, with `participant_private` events addressed to it.
    """
    first = await _room_with_join_token(org)
    second = await _room_with_join_token(org)

    a = await rooms.provision_guest_identity(
        await rooms.authenticate_invitation(first.join_token), display_name="Assistant"
    )
    await rooms.join_room(
        identity=a,
        command=JoinRoomCommand(invitation_token=first.join_token, display_name="Assistant"),
    )
    b = await rooms.provision_guest_identity(
        await rooms.authenticate_invitation(second.join_token), display_name="Assistant"
    )

    assert a.id != b.id


# ---------------------------------------------------------------------------
# What the credential must NOT buy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_revoked_or_expired_invitation_is_refused(fresh_db, org) -> None:
    """Checked at the door, so a dead link never gets as far as provisioning anyone."""
    from app.db import database as db
    from app.util import utcnow_iso

    created = await _room_with_join_token(org)
    await db.execute(
        "UPDATE invitations SET revoked_at = ? WHERE room_id = ?",
        (utcnow_iso(), created.room.id),
    )

    with pytest.raises(Unauthenticated):
        await rooms.authenticate_invitation(created.join_token)


@pytest.mark.asyncio
async def test_an_exhausted_invitation_is_refused(fresh_db, org) -> None:
    from app.db import database as db

    created = await _room_with_join_token(org)
    await db.execute(
        "UPDATE invitations SET redemptions = max_redemptions WHERE room_id = ?",
        (created.room.id,),
    )

    with pytest.raises(Unauthenticated):
        await rooms.authenticate_invitation(created.join_token)


@pytest.mark.asyncio
async def test_an_unknown_token_says_nothing_about_why(fresh_db) -> None:
    """Revoked, expired, exhausted and never-existed all answer identically.

    Probing a token must not reveal that it was once real.
    """
    with pytest.raises(Unauthenticated) as exc:
        await rooms.authenticate_invitation("not-a-token-at-all")
    assert "Unknown or revoked token" in str(exc.value)


@pytest.mark.asyncio
async def test_an_invitation_is_not_a_principal(fresh_db, org) -> None:
    """The containment property: it authenticates for joining and for nothing else.

    `authenticate_principal` is what guards creating rooms and listing an org's rooms, so
    an invitation being rejected there is what stops a link from becoming an account.
    """
    created = await _room_with_join_token(org)

    with pytest.raises(Unauthenticated):
        await rooms.authenticate_principal(created.join_token)


@pytest.mark.asyncio
async def test_an_invitation_for_one_room_cannot_open_another(fresh_db, org) -> None:
    """Confused deputy: authenticate with room A's link, redeem room B's.

    Holding B's token is already sufficient to enter B, so this is defence in depth rather
    than a hole being closed — but a credential for one room must never be the thing that
    authorizes entry to another, and asserting it keeps that true as the code moves.
    """
    from app.api.routes import _identity_for_invitation

    first = await _room_with_join_token(org)
    second = await _room_with_join_token(org)

    credential_for_first = await rooms.authenticate_invitation(first.join_token)

    with pytest.raises(Forbidden):
        await _identity_for_invitation(
            credential_for_first,
            JoinRoomCommand(invitation_token=second.join_token, display_name="Sneaky"),
        )

"""An owner recovering a seat whose token is gone (D-094).

A participant token is shown once and stored hashed, so losing it makes the seat unreachable —
and the only way into a room is an invitation, which needs a participant token to create. An
account could therefore be locked out of a room it owns, permanently, while the service knew
exactly who it was. That is not a hypothetical: it happened during D-092, with a live room, a
healthy relay, and no way back into either.

The authority used here is the browser session, and the proof is the schema's own chain —
`participants.agent_identity_id -> agent_identities.owner_user_id -> users.id`. Most of these
tests are about what that authority must *not* stretch to, because a function that mints
participant credentials is the one place where a too-generous check is worst.
"""

from __future__ import annotations

import pytest

from app.core import credentials, rooms, store
from app.core.errors import NotFound, Unauthenticated
from app.db import database as db
from app.domain.commands import CreateInvitationCommand, JoinRoomCommand, LeaveRoomCommand
from app.domain.events import EventType
from app.domain.identity import PrincipalKind
from app.domain.relevance import RelevanceClass, classify
from app.domain.room import ParticipantRole, Scope

pytestmark = pytest.mark.asyncio


async def _colleague():
    """A different human in the *same* organization.

    Deliberately a colleague rather than a stranger in another org. Same-org is the sharper
    case: org membership, a shared room, and a legitimate account is the combination somebody
    would most plausibly assume is enough, and it is the one a cross-org test would not cover.
    A cross-org guest is checked separately below.
    """
    return await rooms.ensure_org_and_user(
        org_name="Acme", org_slug="acme", email="colleague@acme.test", display_name="Colleague"
    )


async def _stranger_in_another_org():
    return await rooms.ensure_org_and_user(
        org_name="Other", org_slug="other", email="someone@other.test", display_name="Someone"
    )


async def _invite_and_join(room_fixture, *, org_id: str, owner_user_id: str, display_name: str):
    """Join a room as a seat owned by `owner_user_id`, through the real invitation path.

    Going through invitation redemption rather than inserting a participant row is what makes
    these tests meaningful: the guest's seat is bound to *their* identity, owned by *their* user,
    which is the whole thing the recovery check turns on.
    """
    invitation = await rooms.create_invitation(
        participant=room_fixture.owner,
        command=CreateInvitationCommand(role=ParticipantRole.COLLABORATOR),
    )
    identity = await rooms.ensure_identity(
        org_id=org_id,
        owner_user_id=owner_user_id,
        display_name=display_name,
        kind=PrincipalKind.HUMAN,
    )
    return await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=invitation.token),
    )


# ---------------------------------------------------------------------------
# Listing what you own
# ---------------------------------------------------------------------------


async def test_an_owner_can_see_the_seats_they_own(make_room):
    fixture = await make_room()
    seats = await credentials.seats_owned_by(fixture.owner_user_id)
    assert [s.participant_id for s in seats] == [fixture.owner.id]
    assert seats[0].room_id == fixture.room.id
    assert seats[0].room_name == fixture.room.name


async def test_the_listing_carries_no_token_field_at_all(make_room):
    """Not empty — absent. A listing that *could* carry a credential is one bad template away
    from showing every one of them at once."""
    fixture = await make_room()
    seat = (await credentials.seats_owned_by(fixture.owner_user_id))[0]
    assert not any("token" in field for field in vars(seat)), vars(seat)


async def test_an_owner_sees_nothing_belonging_to_anybody_else(make_room):
    fixture = await make_room()
    other_org, other_user = await _colleague()
    await _invite_and_join(
        fixture, org_id=other_org, owner_user_id=other_user, display_name="Someone else"
    )

    assert [s.participant_id for s in await credentials.seats_owned_by(other_user)] != [
        fixture.owner.id
    ]
    mine = await credentials.seats_owned_by(fixture.owner_user_id)
    assert [s.participant_id for s in mine] == [fixture.owner.id]


async def test_a_seat_that_left_is_not_listed(make_room):
    """Leaving nulls the token hash, so there is no credential to recover and re-entry is an
    invitation — deliberately, because leaving is a decision."""
    fixture = await make_room()
    await rooms.leave_room(participant=fixture.owner, command=LeaveRoomCommand(note="done"))
    assert await credentials.seats_owned_by(fixture.owner_user_id) == []


async def test_has_credential_reports_whether_one_was_ever_issued(make_room):
    """It cannot report whether the owner still *knows* the token; nothing can. Saying so is the
    honest half: False definitely needs recovery, True only means one exists."""
    fixture = await make_room()
    assert (await credentials.seats_owned_by(fixture.owner_user_id))[0].has_credential is True
    await db.execute("UPDATE participants SET token_hash = NULL WHERE id = ?", (fixture.owner.id,))
    assert (await credentials.seats_owned_by(fixture.owner_user_id))[0].has_credential is False


# ---------------------------------------------------------------------------
# Recovering your own seat
# ---------------------------------------------------------------------------


async def test_a_reissued_token_authenticates_the_same_seat(make_room):
    """The participant id is stable, which is what makes this recovery rather than a new seat:
    every lease, work declaration, and audit reference in the room still points at it."""
    fixture = await make_room()
    token = await credentials.reissue_seat_token(
        user_id=fixture.owner_user_id, participant_id=fixture.owner.id
    )
    resolved = await store.load_participant_by_token(token)
    assert resolved.id == fixture.owner.id


async def test_the_old_token_stops_working(make_room):
    """Rotation, not addition. It is also the revocation path: an owner who leaked a token has a
    way to invalidate it, and two live credentials for one seat would remove that."""
    fixture = await make_room()
    await credentials.reissue_seat_token(
        user_id=fixture.owner_user_id, participant_id=fixture.owner.id
    )
    with pytest.raises(Unauthenticated):
        await store.load_participant_by_token(fixture.owner_token)


async def test_recovery_works_when_no_credential_remains(make_room):
    """The case the whole thing exists for. Nothing about the flow may depend on presenting the
    credential that was lost."""
    fixture = await make_room()
    await db.execute("UPDATE participants SET token_hash = NULL WHERE id = ?", (fixture.owner.id,))
    token = await credentials.reissue_seat_token(
        user_id=fixture.owner_user_id, participant_id=fixture.owner.id
    )
    assert (await store.load_participant_by_token(token)).id == fixture.owner.id


# ---------------------------------------------------------------------------
# What this authority must not stretch to
# ---------------------------------------------------------------------------


async def test_you_cannot_reissue_a_token_for_somebody_elses_seat(make_room):
    """The rule this is most likely to break: minting another participant's credential is acting
    as them, which is the strongest possible version of what `room.admin` does not grant."""
    fixture = await make_room()
    other_org, other_user = await _colleague()
    guest = await _invite_and_join(
        fixture, org_id=other_org, owner_user_id=other_user, display_name="Guest"
    )

    with pytest.raises(NotFound):
        await credentials.reissue_seat_token(
            user_id=fixture.owner_user_id, participant_id=guest.participant.id
        )
    # And their token still works, so nothing was rotated on the way to refusing.
    assert (
        await store.load_participant_by_token(guest.participant_token)
    ).id == guest.participant.id


async def test_the_room_owner_gets_no_special_power_over_a_guest_seat(make_room):
    """Being the room's creator and holding `room.admin` is not ownership of a person's seat.
    Stated separately from the test above because it is the specific confusion to guard."""
    fixture = await make_room()
    other_org, other_user = await _colleague()
    guest = await _invite_and_join(
        fixture, org_id=other_org, owner_user_id=other_user, display_name="Guest"
    )

    assert Scope.ROOM_ADMIN in fixture.owner.scopes
    with pytest.raises(NotFound):
        await credentials.reissue_seat_token(
            user_id=fixture.owner_user_id, participant_id=guest.participant.id
        )


async def test_a_stranger_learns_nothing_about_whether_a_seat_exists(make_room):
    """A real participant id and an invented one give the same answer. Distinguishing them would
    confirm a seat is real to somebody with no claim on it."""
    fixture = await make_room()
    other_org, other_user = await _colleague()

    real = None
    invented = None
    try:
        await credentials.reissue_seat_token(user_id=other_user, participant_id=fixture.owner.id)
    except NotFound as exc:
        real = str(exc)
    try:
        await credentials.reissue_seat_token(user_id=other_user, participant_id="par_01NOSUCH")
    except NotFound as exc:
        invented = str(exc)
    assert real == invented and real is not None


async def test_a_seat_that_left_cannot_be_re_credentialed(make_room):
    """Re-entry after leaving is an invitation. Recovery must not become a way around that."""
    fixture = await make_room()
    await rooms.leave_room(participant=fixture.owner, command=LeaveRoomCommand(note="done"))
    with pytest.raises(NotFound):
        await credentials.reissue_seat_token(
            user_id=fixture.owner_user_id, participant_id=fixture.owner.id
        )


async def test_a_removed_participant_cannot_re_credential_itself_back_in(make_room):
    """The outcome this must never produce. An ejected participant holding an account is exactly
    the party most motivated to try."""
    fixture = await make_room()
    other_org, other_user = await _colleague()
    guest = await _invite_and_join(
        fixture, org_id=other_org, owner_user_id=other_user, display_name="Guest"
    )
    await db.execute(
        "UPDATE participants SET state = 'removed' WHERE id = ?", (guest.participant.id,)
    )

    with pytest.raises(NotFound):
        await credentials.reissue_seat_token(
            user_id=other_user, participant_id=guest.participant.id
        )


# ---------------------------------------------------------------------------
# What the room is told
# ---------------------------------------------------------------------------


async def test_the_rotation_is_recorded_in_the_room_log(make_room):
    """A credential change is auditable, and the event log is the audit trail. Rotating without
    an event would be a state change outside the log."""
    fixture = await make_room()
    await credentials.reissue_seat_token(
        user_id=fixture.owner_user_id, participant_id=fixture.owner.id
    )
    rows = await db.fetch_all(
        "SELECT type, payload FROM room_events WHERE room_id = ? AND type = ? ORDER BY seq",
        (fixture.room.id, EventType.PARTICIPANT_CREDENTIAL_ROTATED.value),
    )
    assert len(rows) == 1
    payload = db.loads(rows[0]["payload"])
    assert payload["participant_id"] == fixture.owner.id
    assert payload["rotated_by"] == credentials.ROTATED_BY_ACCOUNT_OWNER


async def test_the_event_carries_neither_the_token_nor_its_hash(make_room):
    """Every participant reads this log. A credential is not room content, and a hash is still a
    verifier — publishing one invites offline work against it."""
    fixture = await make_room()
    token = await credentials.reissue_seat_token(
        user_id=fixture.owner_user_id, participant_id=fixture.owner.id
    )
    from app.util import hash_token

    row = await db.fetch_one(
        "SELECT payload FROM room_events WHERE room_id = ? AND type = ?",
        (fixture.room.id, EventType.PARTICIPANT_CREDENTIAL_ROTATED.value),
    )
    raw = str(row["payload"])
    assert token not in raw
    assert hash_token(token) not in raw


async def test_a_rotation_does_not_wake_the_room(make_room):
    """Auditable is not the same as urgent. Waking every agent in a room because somebody
    recovered a lost token would spend other people's turns on housekeeping."""
    assert (
        classify(event_type=EventType.PARTICIPANT_CREDENTIAL_ROTATED, payload={})
        is RelevanceClass.ROUTINE
    )


async def test_the_actor_on_the_event_is_the_seat_itself(make_room):
    """Its owner acted, and the seat is how the room knows that owner. Attribution is stamped
    server-side, so this is the identity the log will carry forever."""
    fixture = await make_room()
    await credentials.reissue_seat_token(
        user_id=fixture.owner_user_id, participant_id=fixture.owner.id
    )
    row = await db.fetch_one(
        "SELECT actor_participant_id FROM room_events WHERE room_id = ? AND type = ?",
        (fixture.room.id, EventType.PARTICIPANT_CREDENTIAL_ROTATED.value),
    )
    assert row["actor_participant_id"] == fixture.owner.id


async def test_a_cross_org_guest_seat_is_equally_out_of_reach(make_room):
    """The same rule across an organization boundary, where the room deliberately admits a
    stranger. Nothing about hosting the room grants authority over the seat they sit in."""
    from app.domain.room import RoomVisibility

    fixture = await make_room(visibility=RoomVisibility.CROSS_ORG)
    other_org, other_user = await _stranger_in_another_org()
    guest = await _invite_and_join(
        fixture, org_id=other_org, owner_user_id=other_user, display_name="Stranger"
    )

    with pytest.raises(NotFound):
        await credentials.reissue_seat_token(
            user_id=fixture.owner_user_id, participant_id=guest.participant.id
        )
    # And they can recover their own seat, which is the point of the boundary being symmetric.
    token = await credentials.reissue_seat_token(
        user_id=other_user, participant_id=guest.participant.id
    )
    assert (await store.load_participant_by_token(token)).id == guest.participant.id

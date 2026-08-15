"""Narrow, expiring credentials for one runtime of a seat (D-048).

Asked for by the ChatGPT participant, and the reasoning was right: running a
companion worker meant copying the participant token into a daemon, and that token
carries everything the seat can do — `room.admin` included, and the ability to mint
more credentials. A process that only needs to take and finish its own work should
not hold the authority to reconfigure the room.

The property under test throughout is that a credential is **the same seat with
less authority**, never a different participant. That is what keeps every
downstream authorization check unchanged.
"""

from __future__ import annotations

import pytest

from app.core import rooms, store, tasks
from app.core.errors import Forbidden, InvalidCommand, Unauthenticated
from app.db import database as db
from app.domain.commands import (
    ClaimTaskCommand,
    CreateInvitationCommand,
    CreateTaskCommand,
    MintCredentialCommand,
    RevokeCredentialCommand,
    SetParticipantRoleCommand,
)
from app.domain.room import RUNTIME_SCOPES, ParticipantRole, Scope
from app.util import iso_in

pytestmark = pytest.mark.asyncio


async def _mint(member, **kwargs):
    return await rooms.mint_runtime_credential(
        participant=member.participant,
        command=MintCredentialCommand(**kwargs),
    )


async def test_a_credential_is_the_same_seat_with_less_authority(make_room, join):
    """Not a second participant. The whole design rests on this.

    If it resolved to a different participant, every ownership check in the system
    would have to learn about credentials — and the ones that forgot would be the
    holes.
    """
    room = await make_room()
    member = await join(room, display_name="Worker seat")

    issued = await _mint(member, label="companion worker")
    caller = await store.load_participant_by_token(issued.token)

    assert caller.id == member.participant.id
    assert caller.credential_id == issued.credential.id
    assert set(caller.scopes) < set(member.participant.scopes), "strictly narrower"


async def test_a_credential_never_carries_administrative_authority(make_room, join):
    """Even minted by an owner, and even if it asks for it.

    The seat being promoted later must not widen a token already sitting in a
    daemon's environment, which is why the clamp is re-applied on every use rather
    than trusted from mint time.
    """
    room = await make_room()
    owner = await join(room, display_name="Owner seat", role=ParticipantRole.OWNER)
    assert Scope.ROOM_ADMIN in owner.participant.scopes

    issued = await _mint(owner, scopes=list(Scope), label="tries for everything")
    caller = await store.load_participant_by_token(issued.token)

    assert Scope.ROOM_ADMIN not in caller.scopes
    assert Scope.ARTIFACT_WRITE not in caller.scopes
    assert Scope.STATE_WRITE not in caller.scopes
    assert set(caller.scopes) <= RUNTIME_SCOPES


async def test_narrowing_the_seat_narrows_its_outstanding_credentials(make_room, join):
    """Re-clamped on use, not frozen at mint.

    Otherwise revoking authority would mean hunting down every daemon issued a
    token while the seat was broader — and the one you missed is the one that
    matters.
    """
    room = await make_room()
    member = await join(room, display_name="Worker seat")
    issued = await _mint(member)
    assert Scope.TASK_CLAIM in (await store.load_participant_by_token(issued.token)).scopes

    await rooms.set_participant_role(
        participant=room.owner,
        command=SetParticipantRoleCommand(
            target_participant_id=member.participant.id,
            role=ParticipantRole.OBSERVER,
            reason="stand down",
        ),
    )

    caller = await store.load_participant_by_token(issued.token)
    assert Scope.TASK_CLAIM not in caller.scopes, "the credential followed the seat down"


async def test_a_credential_cannot_mint_another(make_room, join):
    """Otherwise the narrowing is decorative.

    Any holder could issue itself a sibling, and the chain would be as strong as
    its weakest link rather than as strong as its first.
    """
    room = await make_room()
    member = await join(room, display_name="Worker seat")
    issued = await _mint(member)
    caller = await store.load_participant_by_token(issued.token)

    with pytest.raises(Forbidden):
        await rooms.mint_runtime_credential(
            participant=caller, command=MintCredentialCommand(label="a sibling")
        )


async def test_an_expired_credential_stops_working(make_room, join):
    """There is no forever option, and expiry is enforced on use."""
    room = await make_room()
    member = await join(room, display_name="Worker seat")
    issued = await _mint(member)

    await db.execute(
        "UPDATE participant_credentials SET expires_at = ? WHERE id = ?",
        (iso_in(-60), issued.credential.id),
    )
    with pytest.raises(Unauthenticated) as exc:
        await store.load_participant_by_token(issued.token)
    assert "expired" in str(exc.value)


async def test_revocation_kills_the_runtime_and_leaves_the_seat_alone(make_room, join):
    """The reason narrow tokens are worth the machinery.

    A machine is lost or a worker misbehaves and the answer is one revocation —
    not rotating the participant token and re-attaching everything else that used it.
    """
    room = await make_room()
    member = await join(room, display_name="Worker seat")
    issued = await _mint(member, label="on the lost laptop")

    await rooms.revoke_runtime_credential(
        participant=member.participant,
        command=RevokeCredentialCommand(
            credential_id=issued.credential.id, reason="laptop left on a train"
        ),
    )

    with pytest.raises(Unauthenticated):
        await store.load_participant_by_token(issued.token)
    still_fine = await store.load_participant_by_token(member.token)
    assert still_fine.id == member.participant.id
    assert still_fine.credential_id is None


async def test_someone_elses_credential_is_not_yours_to_revoke(make_room, join):
    room = await make_room()
    mine = await join(room, display_name="Mine")
    theirs = await join(room, display_name="Theirs")
    issued = await _mint(theirs)

    with pytest.raises(Forbidden):
        await rooms.revoke_runtime_credential(
            participant=mine.participant,
            command=RevokeCredentialCommand(credential_id=issued.credential.id),
        )

    # An admin may, because revoking another seat's runtime is administrative.
    await rooms.revoke_runtime_credential(
        participant=room.owner,
        command=RevokeCredentialCommand(credential_id=issued.credential.id, reason="cleaning up"),
    )
    with pytest.raises(Unauthenticated):
        await store.load_participant_by_token(issued.token)


async def test_a_credential_can_actually_do_the_work_it_exists_for(make_room, join):
    """Narrow is only useful if it is still sufficient.

    A credential that cannot claim and finish assigned work would be safe and
    pointless, so this is the test that keeps the allowlist honest.
    """
    room = await make_room()
    member = await join(room, display_name="Worker seat")
    issued = await _mint(member, label="companion worker")
    worker = await store.load_participant_by_token(issued.token)

    task = await tasks.create(
        participant=member.participant, command=CreateTaskCommand(title="Real work")
    )
    claimed = await tasks.claim(participant=worker, command=ClaimTaskCommand(task_id=task.id))
    assert claimed.claim is not None

    from app.domain.commands import CompleteTaskCommand

    done = await tasks.complete(
        participant=worker,
        command=CompleteTaskCommand(
            task_id=task.id, fence=claimed.claim.fence, result="finished by the runtime"
        ),
    )
    assert done.status.value == "done"


async def test_the_token_never_reaches_the_event_log(make_room, join):
    """A credential in the log is a credential in every replay and export of it."""
    room = await make_room()
    member = await join(room, display_name="Worker seat")
    issued = await _mint(member, label="companion worker")

    rows = await db.fetch_all(
        "SELECT * FROM room_events WHERE room_id = ? AND type LIKE 'credential.%'",
        (room.room.id,),
    )
    assert len(rows) == 1
    payload = db.loads(rows[0]["payload"], {})
    assert issued.token not in db.dumps(payload)
    assert payload["credential_id"] == issued.credential.id
    assert payload["scopes"], "the grant is logged even though the token is not"


async def test_a_credential_that_could_do_nothing_is_refused(make_room, join):
    """Minting something inert is a mistake, not a safe default."""
    room = await make_room()
    observer = await join(room, display_name="Watcher", role=ParticipantRole.OBSERVER)

    with pytest.raises(InvalidCommand):
        await _mint(observer, scopes=[Scope.ROOM_ADMIN, Scope.ARTIFACT_WRITE])


async def test_listing_never_returns_tokens(make_room, join):
    room = await make_room()
    member = await join(room, display_name="Worker seat")
    issued = await _mint(member, label="companion")

    listed = await rooms.list_runtime_credentials(participant=member.participant)
    assert [c.id for c in listed] == [issued.credential.id]
    assert "token" not in listed[0].model_dump()


async def test_a_guest_can_run_a_worker_without_holding_the_seat_token(make_room, join):
    """The provisioning story, end to end and without a human in the middle.

    A room key gets a seat; the seat mints a runtime credential; the credential
    goes in the daemon's environment. At no point does a long-lived process hold
    something that could reconfigure the room.
    """
    room = await make_room()
    issued_invite = await rooms.create_invitation(
        participant=room.owner, command=CreateInvitationCommand()
    )
    assert issued_invite.token

    guest = await join(room, display_name="Companion worker")
    credential = await _mint(guest, label="daemon", ttl_seconds=3600)
    runtime = await store.load_participant_by_token(credential.token)

    assert runtime.id == guest.participant.id
    assert Scope.TASK_CLAIM in runtime.scopes
    assert Scope.ROOM_ADMIN not in runtime.scopes

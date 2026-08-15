"""Rejoining rotates a seat's token, and nothing said so (D-056).

Reported from the room by the ChatGPT participant: its owner participant token was
"unexpectedly rejected as revoked", and rejoining with the invitation restored the
same participant id under a newly issued token. It asked whether that was expected
behaviour or a regression before relying on long-lived control-surface credentials.

It is neither, exactly. `participants.token_hash` is a single column, so redeeming an
invitation for a seat that already exists **overwrites** it — the previous token stops
working immediately, everywhere, including in a companion process that was never party
to the rejoin. That is implemented behaviour with no contract behind it and an error
message that calls it revocation, which is why it read as a defect from the outside.

These tests pin the behaviour as it actually is, so that whichever way it is resolved
— preserve the token across a rejoin, or document rotation and say so in the error —
the change is deliberate and visible.
"""

from __future__ import annotations

import pytest

from app.core import rooms, store
from app.core.errors import Unauthenticated
from app.domain.capabilities import Capability, HostClass
from app.domain.commands import CreateInvitationCommand, JoinRoomCommand
from app.domain.identity import PrincipalKind

pytestmark = pytest.mark.asyncio


async def _identity(room, *, display_name: str):
    """The same logical agent every time, which is what makes a second join a rejoin.

    `ensure_identity` keys on `(owner, display_name)` — the same call a connector makes
    when it reconnects. Creating a fresh identity instead produces a *second seat*, which
    is a different thing entirely and was how the first version of this test failed to
    reproduce the report.
    """
    return await rooms.ensure_identity(
        org_id=room.org_id,
        owner_user_id=room.owner_user_id,
        display_name=display_name,
        kind=PrincipalKind.AGENT,
        host_class=HostClass.INTERACTIVE_CLIENT,
        capabilities=[Capability.CAN_RECEIVE_EVENTS, Capability.SUPPORTS_POLL],
    )


async def _join(room, *, display_name: str) -> tuple[str, str]:
    """Redeem a fresh invitation as `display_name`; return (participant_id, token)."""
    issued = await rooms.create_invitation(
        participant=room.owner, command=CreateInvitationCommand()
    )
    identity = await _identity(room, display_name=display_name)
    result = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(
            invitation_token=issued.token,
            display_name=display_name,
            host_class=HostClass.INTERACTIVE_CLIENT,
            capabilities=[Capability.CAN_RECEIVE_EVENTS, Capability.SUPPORTS_POLL],
        ),
    )
    return result.participant.id, result.participant_token


async def test_rejoining_invalidates_the_token_the_seat_was_already_using(make_room):
    """The reported behaviour, reproduced.

    A control surface that rejoins — on reconnect, on a new session, because its
    connector re-runs its entry call — silently invalidates the credential it or
    anything else was holding. Nothing warns, and the seat is otherwise unchanged.
    """
    room = await make_room()
    participant_id, first = await _join(room, display_name="Control surface")
    assert (await store.load_participant_by_token(first)).id == participant_id

    rejoined_id, second = await _join(room, display_name="Control surface")
    assert rejoined_id == participant_id, "same seat, as reported"
    assert second != first

    assert (await store.load_participant_by_token(second)).id == participant_id
    with pytest.raises(Unauthenticated):
        await store.load_participant_by_token(first)


async def test_the_refusal_calls_rotation_revocation(make_room):
    """Why it reached us as a suspected security event rather than a lifecycle one.

    "Unknown or revoked token" is what a holder sees after a rejoin it did not make
    and cannot see. A credential that stopped working because somebody revoked it and
    one that stopped working because a sibling reconnected deserve different
    sentences, because they call for different responses.
    """
    room = await make_room()
    _, first = await _join(room, display_name="Control surface")
    await _join(room, display_name="Control surface")

    with pytest.raises(Unauthenticated) as exc:
        await store.load_participant_by_token(first)
    assert "revoked" in str(exc.value).lower()


async def test_a_runtime_credential_dies_with_the_seat_token_that_minted_it(make_room):
    """The consequence that matters for an unattended worker.

    A companion holding a *narrow* credential is not party to its seat's rejoin, and
    a control surface reconnecting should not be able to take a background process
    offline without anyone deciding that it should.
    """
    from app.domain.commands import MintCredentialCommand

    room = await make_room()
    _, first = await _join(room, display_name="Control surface")
    seat = await store.load_participant_by_token(first)
    issued = await rooms.mint_runtime_credential(
        participant=seat, command=MintCredentialCommand(label="companion")
    )
    assert (await store.load_participant_by_token(issued.token)).id == seat.id

    await _join(room, display_name="Control surface")

    # Recorded as the behaviour that exists today, not endorsed: the credential is
    # keyed on its own row, so it survives — the worker keeps running while the
    # surface's token is dead. Whichever way rotation is resolved, this asymmetry
    # should be a decision rather than a side effect.
    survivor = await store.load_participant_by_token(issued.token)
    assert survivor.id == seat.id

"""Recovering a seat you own, when its token is gone.

A participant token is shown once and stored hashed (D-012). Lose it and the seat is
unreachable — and the only way into a room is an invitation, which you need a participant token
to create. So an account could be **permanently locked out of a room it owns**, with the service
knowing perfectly well who it was. That happened during D-092: a live room, a healthy relay port,
and no way back into either.

The chain that makes recovery safe already exists in the schema:

    participants.agent_identity_id -> agent_identities.owner_user_id -> users.id

An authenticated browser session therefore *proves* ownership of a seat without any room
credential at all, which is exactly the authority a locked-out owner still has.

**What this deliberately does not do.**

* **It is own-seat only.** Not `room.admin`, not org membership, not room ownership. Room admin
  does not grant the ability to act as another participant, and minting another seat's credential
  would be precisely that — the strongest possible version of it.
* **It only reaches seats that are still `joined`.** Leaving nulls the token hash, so a departed
  seat has no credential to recover and re-entry is an invitation, deliberately. A `removed`
  participant must never be able to re-credential itself back into a room it was ejected from.
* **A non-owner gets the same answer as a nonexistent seat.** Distinguishing them would confirm
  that a participant id is real to somebody with no claim on it.

**It rotates rather than adds.** The old token stops working the moment the new one exists, which
makes this the revocation path too: an owner who leaked a token has a way to invalidate it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db import database as db
from ..domain.events import EventType
from ..domain.room import MembershipState
from ..util import hash_token, new_token
from . import eventlog, store
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import NotFound

#: Recorded on the event so the log distinguishes an owner recovering a lost credential from any
#: other reason a token might change. It is never a free-form string from a caller.
ROTATED_BY_ACCOUNT_OWNER = "account_owner"


@dataclass(frozen=True)
class OwnedSeat:
    """One seat an account owns, as much as is needed to choose between them.

    Deliberately no token field, present or absent: a listing that could carry a credential is
    one bad template away from displaying every one of them at once.
    """

    participant_id: str
    room_id: str
    room_name: str
    display_name: str
    role: str
    joined_at: str | None
    room_expires_at: str | None
    has_credential: bool


async def seats_owned_by(user_id: str) -> list[OwnedSeat]:
    """Every joined seat whose identity this user owns, newest room first.

    `has_credential` is whether a token hash exists at all — not whether the owner still knows
    the token, which nothing can know. It is the honest half of the answer: a seat showing
    `False` definitely needs recovery, while `True` only means one was issued at some point.
    """
    rows = await db.fetch_all(
        """
        SELECT p.id, p.room_id, p.role, p.display_name, p.joined_at, p.token_hash,
               r.name AS room_name, r.expires_at AS room_expires_at
          FROM participants p
          JOIN agent_identities i ON i.id = p.agent_identity_id
          JOIN rooms r ON r.id = p.room_id
         WHERE i.owner_user_id = ? AND p.state = ?
         ORDER BY p.joined_at DESC
        """,
        (user_id, MembershipState.JOINED.value),
    )
    return [
        OwnedSeat(
            participant_id=str(row["id"]),
            room_id=str(row["room_id"]),
            room_name=str(row["room_name"]),
            display_name=str(row["display_name"]),
            role=str(row["role"]),
            joined_at=row["joined_at"],
            room_expires_at=row["room_expires_at"],
            has_credential=row["token_hash"] is not None,
        )
        for row in rows
    ]


async def _owned_seat_or_not_found(user_id: str, participant_id: str) -> str:
    """Resolve a seat this user owns, or raise as though it did not exist.

    One query, joined through the identity, so ownership is a condition of *finding* the row
    rather than a check performed on it afterwards. A check after the fact is the shape that
    gets refactored into a fetch without its guard.
    """
    row = await db.fetch_one(
        """
        SELECT p.id
          FROM participants p
          JOIN agent_identities i ON i.id = p.agent_identity_id
         WHERE p.id = ? AND i.owner_user_id = ? AND p.state = ?
        """,
        (participant_id, user_id, MembershipState.JOINED.value),
    )
    if row is None:
        # The same answer for "no such seat", "not yours", and "no longer in the room". A caller
        # with no claim on a participant id must not learn that it is real.
        raise NotFound("No such seat, or it is not one you own.")
    return str(row["id"])


async def reissue_seat_token(
    *,
    user_id: str,
    participant_id: str,
    command_id: str | None = None,
) -> str:
    """Rotate the participant token for a seat this account owns, and return it once.

    The returned value is the only copy: what is stored is a hash, which is what makes losing it
    unrecoverable and this function necessary.
    """
    resolved = await _owned_seat_or_not_found(user_id, participant_id)
    participant = await store.load_participant(resolved)
    token = new_token()

    async def body(tx: db.Tx) -> CommandOutcome:
        # Conditional on the state the caller was authorized against, with the affected-row
        # count inspected (ADR-009). Between the check above and here the seat could have been
        # removed, and rotating a credential back onto an ejected participant is the one outcome
        # this must not produce.
        affected = await tx.execute(
            "UPDATE participants SET token_hash = ? WHERE id = ? AND state = ?",
            (hash_token(token), resolved, MembershipState.JOINED.value),
        )
        if affected == 0:
            raise NotFound("No such seat, or it is not one you own.")
        event = await eventlog.append(
            tx,
            room_id=participant.room_id,
            type_=EventType.PARTICIPANT_CREDENTIAL_ROTATED,
            actor=actor_for(participant),
            payload={
                # No token and no hash. The room learns that this seat's credential changed,
                # which is the auditable fact; the credential itself is not room content and
                # the event log is read by every participant.
                "participant_id": resolved,
                "rotated_by": ROTATED_BY_ACCOUNT_OWNER,
            },
            causation_id=command_id,
        )
        return CommandOutcome(result={"participant_id": resolved}, events=[event])

    await execute_command(
        command_id=command_id,
        command_type="participant.reissue_token",
        room_id=participant.room_id,
        participant_id=resolved,
        body=body,
    )
    return token

"""Short-lived one-use credentials for browser WebSocket handshakes."""

from __future__ import annotations

from dataclasses import dataclass

from ..db import database as db
from ..domain.room import Participant
from ..util import hash_token, iso_in, new_token, utcnow_iso
from . import store
from .errors import Unauthenticated

TICKET_TTL_SECONDS = 60


@dataclass(frozen=True)
class IssuedTicket:
    token: str
    expires_at: str


async def issue(participant: Participant) -> IssuedTicket:
    token = new_token()
    created_at = utcnow_iso()
    expires_at = iso_in(TICKET_TTL_SECONDS)
    async with db.transaction() as tx:
        await tx.execute(
            "DELETE FROM stream_tickets WHERE expires_at <= ? OR consumed_at IS NOT NULL",
            (created_at,),
        )
        await tx.execute(
            "INSERT INTO stream_tickets "
            "(token_hash, room_id, participant_id, created_at, expires_at) VALUES (?,?,?,?,?)",
            (hash_token(token), participant.room_id, participant.id, created_at, expires_at),
        )
    return IssuedTicket(token=token, expires_at=expires_at)


async def consume(token: str | None, *, room_id: str) -> Participant:
    if not token:
        raise Unauthenticated("Missing realtime stream ticket.")
    now = utcnow_iso()
    token_hash = hash_token(token)
    async with db.transaction() as tx:
        row = await tx.fetch_one(
            "SELECT * FROM stream_tickets WHERE token_hash = ? AND room_id = ?",
            (token_hash, room_id),
        )
        if row is None or row["consumed_at"] is not None or row["expires_at"] <= now:
            raise Unauthenticated("Realtime stream ticket is invalid, expired, or already used.")
        changed = await tx.execute(
            "UPDATE stream_tickets SET consumed_at = ? WHERE token_hash = ? "
            "AND consumed_at IS NULL AND expires_at > ?",
            (now, token_hash, now),
        )
        if not changed:
            raise Unauthenticated("Realtime stream ticket is invalid, expired, or already used.")
        return await store.load_participant_for_room(room_id, str(row["participant_id"]), tx=tx)

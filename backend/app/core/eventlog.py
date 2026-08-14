"""The room event log: system of record (D-003).

Every state change in a room appends exactly one event, inside the same
transaction as the mutation. That single rule is what makes reconnect/replay, the
audit trail, and conflict archaeology all work without bespoke machinery.

Sequence allocation is `UPDATE rooms SET event_seq = event_seq + 1` followed by a
read of the new value, both inside the caller's transaction. On any engine, a
competing transaction either waits for or serializes behind that row update, so
two events can never receive the same `seq` — and the `(room_id, seq)` primary key
makes a duplicate a hard failure rather than silent corruption. No engine-specific
locking is involved (ADR-009).
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import database as db
from ..domain import ids
from ..domain.disclosure import Audience, DisclosureDecision
from ..domain.events import EventActor, EventEnvelope, EventType
from ..domain.room import PrivacyClass
from ..util import utcnow_iso
from .errors import InvalidCursor, NotFound, ResumeGap

log = logging.getLogger(__name__)

#: Hard cap on a single replay response, so a client resuming from `seq=0` on a
#: busy room cannot ask for an unbounded page.
MAX_REPLAY_BATCH = 500


async def append(
    tx: db.Tx,
    *,
    room_id: str,
    type_: EventType,
    actor: EventActor,
    payload: dict[str, Any] | None = None,
    disclosure: DisclosureDecision | None = None,
    causation_id: str | None = None,
) -> EventEnvelope:
    """Allocate the next `seq` for the room and append one event.

    Must be called inside the transaction that performs the state mutation. The
    caller keeps the returned envelope so it can publish `(room_id, seq)` to the
    bus *after* the transaction commits — publishing before commit would let a
    consumer read a state that can still roll back.
    """
    affected = await tx.execute(
        "UPDATE rooms SET event_seq = event_seq + 1 WHERE id = ?",
        (room_id,),
    )
    if affected == 0:
        # No row means no such room. Raising here also rolls back the mutation the
        # caller already staged, which is the correct outcome.
        raise NotFound("Room does not exist.", room_id=room_id)

    seq = int(await tx.fetch_value("SELECT event_seq FROM rooms WHERE id = ?", (room_id,)))

    privacy_class = disclosure.privacy_class if disclosure else PrivacyClass.ROOM_PUBLIC
    audience = disclosure.audience if disclosure else Audience.ROOM
    restricted = disclosure.restricted_to_participant_ids if disclosure else None

    envelope = EventEnvelope(
        room_id=room_id,
        seq=seq,
        id=ids.new_id(ids.EVENT),
        type=type_,
        ts=utcnow_iso(),
        actor=actor,
        privacy_class=privacy_class,
        audience=audience,
        restricted_to_participant_ids=restricted,
        causation_id=causation_id,
        payload=payload or {},
    )

    await tx.execute(
        """
        INSERT INTO room_events (
            room_id, seq, id, type, ts,
            actor_participant_id, actor_display_name, actor_kind, actor_org_id,
            privacy_class, audience, restricted_to_participant_ids,
            causation_id, payload
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            envelope.room_id,
            envelope.seq,
            envelope.id,
            envelope.type.value,
            envelope.ts,
            envelope.actor.participant_id,
            envelope.actor.display_name,
            envelope.actor.kind.value if envelope.actor.kind else None,
            envelope.actor.org_id,
            envelope.privacy_class.value,
            envelope.audience.value,
            db.dumps(restricted) if restricted is not None else None,
            envelope.causation_id,
            db.dumps(envelope.payload),
        ),
    )
    return envelope


def _row_to_envelope(row: Any) -> EventEnvelope:
    restricted_raw = row["restricted_to_participant_ids"]
    return EventEnvelope(
        room_id=row["room_id"],
        seq=int(row["seq"]),
        id=row["id"],
        type=EventType(row["type"]),
        ts=row["ts"],
        actor=EventActor(
            participant_id=row["actor_participant_id"],
            display_name=row["actor_display_name"],
            kind=row["actor_kind"],
            org_id=row["actor_org_id"],
        ),
        privacy_class=PrivacyClass(row["privacy_class"]),
        audience=Audience(row["audience"]),
        restricted_to_participant_ids=(
            db.str_list(restricted_raw) if restricted_raw is not None else None
        ),
        causation_id=row["causation_id"],
        payload=db.loads(row["payload"], {}),
    )


async def read_since(
    room_id: str,
    since_seq: int,
    *,
    limit: int = MAX_REPLAY_BATCH,
    tx: db.Tx | None = None,
) -> list[EventEnvelope]:
    """Events with `seq > since_seq`, in order.

    Unfiltered by privacy — filtering is the caller's job via
    `privacy.visible_to`, so that this function stays the one true reader and the
    filter cannot be forgotten in one code path and applied in another.
    """
    sql = "SELECT * FROM room_events WHERE room_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?"
    params = (room_id, since_seq, min(limit, MAX_REPLAY_BATCH))
    rows = await (tx.fetch_all(sql, params) if tx else db.fetch_all(sql, params))
    return [_row_to_envelope(r) for r in rows]


async def current_seq(room_id: str, *, tx: db.Tx | None = None) -> int:
    sql = "SELECT event_seq FROM rooms WHERE id = ?"
    value = await (tx.fetch_value(sql, (room_id,)) if tx else db.fetch_value(sql, (room_id,)))
    if value is None:
        raise NotFound("Room does not exist.", room_id=room_id)
    return int(value)


async def validate_cursor(room_id: str, since_seq: int, *, tx: db.Tx | None = None) -> int:
    """Check a resume cursor against the room, returning the room's current seq.

    Two distinct failures, deliberately not merged: a cursor *ahead* of the room is
    a client bug (`invalid_cursor`), while a cursor *behind the retained floor* is a
    legitimate consequence of truncation the client must recover from
    (`resume_gap`). Collapsing them would leave a client unable to tell "I am
    broken" from "I need to re-snapshot".
    """
    sql = "SELECT event_seq, retained_from_seq FROM rooms WHERE id = ?"
    row = await (tx.fetch_one(sql, (room_id,)) if tx else db.fetch_one(sql, (room_id,)))
    if row is None:
        raise NotFound("Room does not exist.", room_id=room_id)

    seq = int(row["event_seq"])
    retained_from = int(row["retained_from_seq"])

    if since_seq > seq:
        raise InvalidCursor(
            "Resume cursor is ahead of the room's current sequence.",
            since_seq=since_seq,
            current_seq=seq,
        )
    # `retained_from` is the oldest seq still present. A cursor of exactly
    # `retained_from - 1` is still resumable: the next event it wants is retained.
    if since_seq > 0 and since_seq < retained_from - 1:
        raise ResumeGap(
            "History before this cursor has been truncated; re-snapshot the room.",
            since_seq=since_seq,
            retained_from_seq=retained_from,
        )
    return seq


async def event_count(room_id: str) -> int:
    value = await db.fetch_value("SELECT COUNT(*) FROM room_events WHERE room_id = ?", (room_id,))
    return int(value or 0)

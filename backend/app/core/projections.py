"""Room snapshot: the read model, filtered per recipient.

`snapshot` is the frame a client gets when it resumes from `seq=0`. Two properties
matter:

* **Snapshot and cursor are read in one transaction.** `snapshot_seq` is the room's
  seq as of the same consistent read as the content. Without that, a client could
  miss an event that landed between reading the content and reading the seq, or
  double-apply one — the exact silent-gap failure `docs/PROTOCOL.md` §5 forbids.

* **Filtering is per recipient, server-side.** Two participants snapshotting the same
  room legitimately get different content. Doing this in the client would mean
  shipping the filtered-out data to the client that must not see it.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import database as db
from ..domain.room import Participant, PrivacyClass, Room, Scope
from ..util import from_iso, utcnow
from . import presence, privacy, store

log = logging.getLogger(__name__)

MAX_SNAPSHOT_MESSAGES = 200


def _visible_record(
    *,
    recipient: Participant,
    room: Room,
    privacy_class: PrivacyClass,
    owner_participant_id: str | None,
    restricted_to: list[str] | None = None,
) -> bool:
    """Same rules as `privacy.visible_to`, applied to a projection row.

    Kept as one helper rather than repeated per table, because a filter that is
    correct in three places and forgotten in a fourth is the usual way this leaks.
    """
    is_admin_of_owner = recipient.has(Scope.ROOM_ADMIN) and recipient.org_id == room.org_id

    if restricted_to is not None and recipient.id not in restricted_to and not is_admin_of_owner:
        return False
    if privacy_class == PrivacyClass.ORG_INTERNAL:
        return recipient.org_id == room.org_id
    if privacy_class == PrivacyClass.PARTICIPANT_PRIVATE:
        return owner_participant_id == recipient.id or is_admin_of_owner
    return True


def _identity_view(
    participant: Participant, *, room: Room, recipient: Participant
) -> dict[str, Any]:
    """Identity as this recipient may see it.

    In a cross-org room a foreign participant is minimized to display name, org
    name, host label, and capabilities — no email, no user id, no sibling identities
    (`docs/SECURITY.md` §4). Since `IdentitySummary` already excludes those, the
    minimization here is about *not* widening it later by accident.
    """
    summary = participant.identity
    return {
        "identity_id": summary.identity_id,
        "display_name": summary.display_name,
        "org_id": summary.org_id,
        "org_name": summary.org_name,
        "kind": summary.kind.value,
        "host_class": summary.host_class.value,
        # The description is a human-written public blurb, shareable across orgs by
        # design. Anything org-sensitive belongs in the room, under a privacy class.
        "description": summary.description,
        "trust": summary.trust.value,
    }


async def snapshot(*, room_id: str, recipient: Participant) -> dict[str, Any]:
    """Everything `recipient` may see about the room, plus the cursor it starts at."""
    async with db.transaction(write=False) as tx:
        room = await store.load_room(room_id, tx=tx)
        snapshot_seq = int(
            await tx.fetch_value("SELECT event_seq FROM rooms WHERE id = ?", (room_id,))
        )
        participants = await store.list_participants(room_id, tx=tx)
        tasks = await store.list_tasks(room_id, tx=tx)
        work_rows = await tx.fetch_all(
            "SELECT * FROM work_declarations WHERE room_id = ? AND ended_at IS NULL "
            "ORDER BY started_at ASC",
            (room_id,),
        )
        message_rows = await tx.fetch_all(
            "SELECT * FROM messages WHERE room_id = ? ORDER BY seq DESC LIMIT ?",
            (room_id, MAX_SNAPSHOT_MESSAGES),
        )
        conflicts = await store.list_conflicts(room_id, tx=tx)

    presences = await presence.presence_for_room(room)
    now = utcnow()
    stale_cutoff = room.policy.work_stale_after_seconds

    work: list[dict[str, Any]] = []
    for row in work_rows:
        if not _visible_record(
            recipient=recipient,
            room=room,
            privacy_class=PrivacyClass(row["privacy_class"]),
            owner_participant_id=row["participant_id"],
        ):
            continue
        owner_presence = presences.get(row["participant_id"])
        heartbeat_age = (now - from_iso(row["heartbeat_at"])).total_seconds()
        owner_gone = owner_presence is None or owner_presence.liveness.value in {
            "stale",
            "disconnected",
        }
        declaration = store.to_work(row, stale=owner_gone or heartbeat_age > stale_cutoff)
        work.append(declaration.model_dump(mode="json"))

    messages: list[dict[str, Any]] = []
    for row in reversed(message_rows):
        restricted = (
            sorted({row["to_participant_id"], row["participant_id"]})
            if row["to_participant_id"]
            else None
        )
        if not _visible_record(
            recipient=recipient,
            room=room,
            privacy_class=PrivacyClass(row["privacy_class"]),
            owner_participant_id=row["participant_id"],
            restricted_to=restricted,
        ):
            continue
        messages.append(
            {
                "id": row["id"],
                "seq": int(row["seq"]),
                "participant_id": row["participant_id"],
                "body": row["body"],
                "about_ref": row["about_ref"],
                "privacy_class": row["privacy_class"],
                "to_participant_id": row["to_participant_id"],
                "created_at": row["created_at"],
            }
        )

    visible_tasks = [
        t.model_dump(mode="json")
        for t in tasks
        if _visible_record(
            recipient=recipient,
            room=room,
            privacy_class=t.privacy_class,
            owner_participant_id=t.created_by_participant_id,
        )
    ]

    return {
        "type": "snapshot",
        "protocol": "arp/1",
        "room": room.model_dump(mode="json"),
        # The cursor this snapshot is consistent with. The client resumes from here.
        "snapshot_seq": snapshot_seq,
        "you": {
            "participant_id": recipient.id,
            "role": recipient.role.value,
            "scopes": [s.value for s in recipient.scopes],
            "trust": recipient.trust.value,
            "org_id": recipient.org_id,
        },
        "participants": [
            {
                **p.model_dump(mode="json", exclude={"identity"}),
                "identity": _identity_view(p, room=room, recipient=recipient),
                "presence": (
                    presences[p.id].model_dump(mode="json") if p.id in presences else None
                ),
            }
            for p in participants
        ],
        "work": work,
        "tasks": visible_tasks,
        "messages": messages,
        "conflicts": [c.model_dump(mode="json") for c in conflicts],
    }


async def visible_events_since(
    *, room_id: str, recipient: Participant, since_seq: int, limit: int = 500
) -> list[dict[str, Any]]:
    """Replay for one recipient: read the log, then filter.

    The recipient may see gaps in `seq` where events it is not authorized for were
    filtered out. That is expected and documented — `seq` stays authoritative, and a
    client must not treat a gap as loss (`docs/SECURITY.md` §6).
    """
    from . import eventlog

    room = await store.load_room(room_id)
    events = await eventlog.read_since(room_id, since_seq, limit=limit)
    return [
        e.model_dump(mode="json")
        for e in privacy.filter_events(events, recipient=recipient, room=room)
    ]

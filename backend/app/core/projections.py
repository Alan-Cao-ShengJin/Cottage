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
from ..domain.identity import IdentityProvenance
from ..domain.room import Participant, PrivacyClass, Room, Scope
from ..domain.task import TERMINAL_TASK_STATUSES, ConflictStatus, Steering, TaskStatus
from ..util import from_iso, utcnow
from . import authz, eventlog, presence, privacy, store
from . import checkpoints as checkpoints_svc
from . import directives as directives_svc
from . import questions as questions_svc
from .errors import ResumeGap

log = logging.getLogger(__name__)

MAX_SNAPSHOT_MESSAGES = 200
#: Smaller than the snapshot cap on purpose: hydration is a resume payload for one
#: participant, and every line of it is spent context for the model reading it.
MAX_HYDRATION_MESSAGES = 25

#: Enough for a worker to choose from; not the board.
MAX_CLAIMABLE = 25


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
        # Delegated rather than compared inline. The comparison this replaced —
        # `recipient.org_id == room.org_id` — is tenancy, and tenancy stopped being
        # sufficient once an invitation could provision a guest *into* the room's org
        # (D-025). Two copies of a rule diverge; one predicate cannot.
        return authz.can_see_org_internal(recipient, room)
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
        # Whether anyone vouched for the *name* above. A guest who redeemed a link chose
        # its own, and a name that looks identical to a credential-bound one is exactly
        # the confusion this field exists to prevent — attribution is the product's
        # integrity guarantee, so it has to say how much the attribution is worth.
        "provenance": summary.provenance.value,
        "name_is_self_asserted": summary.provenance == IdentityProvenance.INVITATION,
    }


async def snapshot(*, room_id: str, recipient: Participant) -> dict[str, Any]:
    """Everything `recipient` may see about the room, plus the cursor it starts at."""
    async with db.transaction(write=False) as tx:
        room = await store.load_room(room_id, tx=tx)
        open_directives = [
            d.model_dump(mode="json") for d in await directives_svc.open_for(recipient.id, tx=tx)
        ]
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
        # Room-wide, not per-recipient: an unanswered question is coordination state
        # the whole room needs to see. Restricting it to the addressee is how a
        # question that one person could have answered goes stale unseen (D-051).
        open_question_rows = await tx.fetch_all(
            "SELECT * FROM questions WHERE room_id = ? AND answered_at IS NULL "
            "ORDER BY created_seq ASC LIMIT ?",
            (room_id, questions_svc.MAX_OPEN_QUESTIONS),
        )

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
        # FIRST, and literally first rather than merely early: a directive is an
        # instruction to this participant, and everything else here is context. A
        # worker reading top-down therefore acts on what it was told before it
        # reasons about the board. The event stream itself stays ordered by seq —
        # the guarantee is about this payload, not a re-sequenced log (D-045).
        "directives_for_you": open_directives,
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
        "open_questions": [
            store.to_question(r).model_dump(mode="json") for r in open_question_rows
        ],
        "conflicts": [c.model_dump(mode="json") for c in conflicts],
    }


async def hydrate(
    *, room_id: str, recipient: Participant, since_seq: int | None = None
) -> dict[str, Any]:
    """What *this* participant needs to resume, rather than everything about the room.

    A control surface arrives cold. `snapshot` answers "what is going on here" and
    costs the whole board; this answers "what was I doing, what is waiting on me, and
    where do I resume" — so a human can open another authorized surface and continue
    without asking every agent to recap (D-030).

    It is deliberately **operational state, not conversation**. A hydration payload
    cannot say what a human asked or which tradeoffs were weighed, and it must never be
    presented as though it could: that is what continuity notes are for, and shipping
    this first does not stand in for them (D-031).

    Pass `since_seq` — the cursor you last saw — to have messages addressed to you
    counted as waiting. Without it they are returned but not counted, because the room
    stores no read state and inventing one here would be a claim the data cannot
    support.

    Everything here is derived from the event log and filtered through the same
    visibility rules as every other projection, so it discloses nothing a snapshot
    would not.

    On the cursor: `event_seq` is read before the content in the same read
    transaction, so a consumer cannot *miss* an event — anything committed after the
    cursor is replayed from it. Under an engine weaker than SQLite's snapshot reads a
    later row may appear alongside an earlier cursor, so the guarantee is **no missed
    event, with replay possible**; consumers must be idempotent, which the fence and
    `command_id` receipts already require of them.
    """
    truncated = False
    if since_seq is not None:
        # An *ahead* cursor is a client bug and stays an error: quietly reporting zero
        # unseen would hide it. A cursor below the retained floor is a legitimate
        # consequence of truncation, and hydration is precisely what a lost surface
        # calls — so it still gets its state, with the count marked unknown rather
        # than understated (D-043).
        try:
            await eventlog.validate_cursor(room_id, since_seq)
        except ResumeGap:
            truncated = True

    async with db.transaction(write=False) as tx:
        open_directives = [
            d.model_dump(mode="json") for d in await directives_svc.open_for(recipient.id, tx=tx)
        ]
        room = await store.load_room(room_id, tx=tx)
        cursor = int(await tx.fetch_value("SELECT event_seq FROM rooms WHERE id = ?", (room_id,)))
        # Counted by the database, never by len() of the returned page. The payload is
        # capped at MAX_HYDRATION_MESSAGES; the count is not, and deriving one from the
        # other would report "50 waiting" when 200 were waiting — an exact-looking
        # wrong number, which is worse than an admitted approximation because a
        # controller treats it as decision state (D-043).
        addressed_since = (
            int(
                await tx.fetch_value(
                    "SELECT COUNT(*) FROM messages WHERE room_id = ? AND to_participant_id = ? "
                    "AND seq > ?",
                    (room_id, recipient.id, since_seq),
                )
                or 0
            )
            if since_seq is not None and not truncated
            else None
        )
        tasks = await store.list_tasks(room_id, tx=tx)
        work_rows = await tx.fetch_all(
            "SELECT * FROM work_declarations WHERE room_id = ? AND participant_id = ? "
            "AND ended_at IS NULL ORDER BY started_at ASC",
            (room_id, recipient.id),
        )
        proposal_rows = await tx.fetch_all(
            "SELECT * FROM task_proposals WHERE room_id = ? AND to_participant_id = ? "
            "AND resolution IS NULL ORDER BY created_at ASC",
            (room_id, recipient.id),
        )
        addressed_rows = await tx.fetch_all(
            "SELECT * FROM messages WHERE room_id = ? AND to_participant_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (room_id, recipient.id, MAX_HYDRATION_MESSAGES),
        )
        open_questions = await questions_svc.open_for(recipient.id, room_id=room_id, tx=tx)
        answers = await questions_svc.answers_for(recipient.id, room_id=room_id, tx=tx)
        conflicts = await store.list_conflicts(room_id, tx=tx)
        # Only for work this participant currently holds. A resuming runtime needs
        # where *it* got to; every other task's history is the board's business and
        # is one call away. Read inside the same transaction as the cursor, so the
        # progress and the resume point cannot disagree.
        held_ids = [
            t.id for t in tasks if t.claim is not None and t.claim.participant_id == recipient.id
        ]
        checkpoint_state = {
            task_id: [
                c.model_dump(mode="json")
                for c in await checkpoints_svc.latest_for_task(task_id, recipient=recipient, tx=tx)
            ]
            for task_id in held_ids
        }

    now = utcnow()
    by_id = {t.id: t for t in tasks}

    # Leases, with the two facts a resuming runtime cannot reconstruct for itself: the
    # fence it must present, and how long it has before the room takes the work back.
    leases: list[dict[str, Any]] = []
    for task in tasks:
        if task.claim is None or task.claim.participant_id != recipient.id:
            continue
        remaining = (from_iso(task.claim.expires_at) - now).total_seconds()
        leases.append(
            {
                "task_id": task.id,
                "title": task.title,
                "status": task.status.value,
                "fence": task.claim.fence,
                "expires_at": task.claim.expires_at,
                "seconds_remaining": max(0, int(remaining)),
                "targets": task.targets,
            }
        )

    # What this participant could pick up right now. Without it a worker looking for
    # work has to pull the whole board every cycle — which is the cost hydration
    # exists to avoid, so omitting it would have made the cheap read useless to the
    # one caller that reads most often. Steering-halted tasks are excluded because
    # claiming them is refused anyway, and offering work that cannot be taken is a
    # board that lies (D-045).
    claimable = [
        {
            "task_id": task.id,
            "title": task.title,
            "priority": task.priority,
            "targets": task.targets,
            "created_at": task.created_at,
        }
        for task in sorted(tasks, key=lambda t: (-t.priority, t.created_at))
        if task.claim is None
        and task.status is TaskStatus.OPEN
        and task.steering is Steering.RUNNING
    ][:MAX_CLAIMABLE]

    # Fails *closed* on a dangling task reference. The earlier form admitted the
    # proposal whenever its task was missing, which let a free-form `note` through with
    # no visibility check at all — the wrong direction for a privacy projection, even
    # though a foreign key should make the case unreachable. A privacy filter that
    # cannot evaluate its subject must omit it, not wave it through.
    proposals = [
        {
            "proposal_id": row["id"],
            "task_id": row["task_id"],
            "title": by_id[row["task_id"]].title,
            "proposed_by_participant_id": row["proposed_by_participant_id"],
            "note": row["note"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
        for row in proposal_rows
        if row["task_id"] in by_id
        # A proposal outlives its task: completing or cancelling a task does not
        # resolve the offers pointing at it, so an unresolved proposal can name work
        # that is already finished. Offering it is worse than useless — a worker that
        # accepts is refused, retries, and makes no progress while looking busy.
        # Found by an unattended worker doing exactly that, live.
        and by_id[row["task_id"]].status not in TERMINAL_TASK_STATUSES
        and _visible_record(
            recipient=recipient,
            room=room,
            privacy_class=by_id[row["task_id"]].privacy_class,
            owner_participant_id=by_id[row["task_id"]].created_by_participant_id,
        )
    ]

    addressed = [
        {
            "id": row["id"],
            "seq": int(row["seq"]),
            "participant_id": row["participant_id"],
            "body": row["body"],
            "created_at": row["created_at"],
        }
        for row in reversed(addressed_rows)
    ]

    involving_me = [
        c.model_dump(mode="json")
        for c in conflicts
        if c.status is ConflictStatus.OPEN and recipient.id in c.participant_ids
    ]

    return {
        # FIRST, and literally first rather than merely early: a directive is an
        # instruction to this participant, and everything else here is context. A
        # worker reading top-down therefore acts on what it was told before it
        # reasons about the board. The event stream itself stays ordered by seq —
        # the guarantee is about this payload, not a re-sequenced log (D-045).
        "directives_for_you": open_directives,
        "type": "hydration",
        "protocol": "arp/1",
        "room": {
            "id": room.id,
            "name": room.name,
            "purpose": room.purpose,
            "status": room.status.value,
        },
        "you": {
            "participant_id": recipient.id,
            "display_name": recipient.identity.display_name,
            "role": recipient.role.value,
            "scopes": [s.value for s in recipient.scopes],
            "trust": recipient.trust.value,
        },
        # Resume the stream from here. Named `cursor` to match await_room_events.
        "cursor": cursor,
        "your_work": [store.to_work(r).model_dump(mode="json") for r in work_rows],
        "your_leases": leases,
        # Keyed by task id, oldest-first within each. The resume payload is present
        # only on this participant's own checkpoints — a room admin reading someone
        # else's hydration is not a path that exists, so there is nothing to widen
        # here (D-050).
        "checkpoints": checkpoint_state,
        # Both directions in one list: what is waiting on you, and what you are
        # waiting on. Split into two and one of them stops being read.
        "open_questions": [q.model_dump(mode="json") for q in open_questions],
        # Replies to questions *you* asked. Here rather than only on the event stream
        # because a restarted runtime starts at the current cursor, so the one event
        # it most needs is the one already behind it (D-051).
        "answers_for_you": answers,
        "claimable": claimable,
        "proposed_to_you": proposals,
        # Named for what it is. These are the most recent messages addressed to you,
        # not your unread ones — the room keeps no read state, so calling them unread
        # would be a claim the data cannot support.
        "recent_addressed_to_you": addressed,
        # Counted by the database, capped by nothing. `null` means "not knowable from
        # what you gave me" — no cursor, or a cursor below the retained floor — which
        # is different from zero and must not be rendered as it.
        "addressed_since_cursor": addressed_since,
        "history_truncated": truncated,
        "retained_from_seq": room.retained_from_seq if truncated else None,
        "blocking_you": involving_me,
        # Said explicitly rather than left to be inferred from empty lists, because a
        # cold surface has no way to tell "nothing is waiting" from "nothing loaded".
        # Counts only objectively unresolved state, plus messages newer than a cursor
        # the caller supplied. An open proposal and an open conflict are waiting on you
        # by their own status; a message is only waiting if you have not seen it.
        # A question addressed to you is unresolved state by its own status, exactly
        # like a proposal — so it counts. One you asked does not: waiting on someone
        # else is not something you can act on, and counting it would tell a worker
        # it has work when what it has is patience.
        "needs_you": (
            len(proposals)
            + len(involving_me)
            + (addressed_since or 0)
            + sum(1 for q in open_questions if q.asked_by_participant_id != recipient.id)
        ),
        "is_conversation_history": False,
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

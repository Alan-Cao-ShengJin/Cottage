"""Conflict detection: duplicate tasks, overlapping work, claim races.

Detection is **advisory and never blocking** (`docs/PROTOCOL.md` §8). The room
surfaces a collision and lets the participants resolve it. That is deliberate: a
heuristic that blocked work would make a false positive into a hard stop, and these
are heuristics — two agents legitimately editing the same file is sometimes fine and
sometimes a disaster, and the room cannot tell which.

Detection also never *silently resolves*. Losing a claim race, colliding on a state
key, or diverging on an artifact all produce a durable record. Hiding a lost race
would leave two agents each believing they own the work.

Every detector runs inside the caller's transaction, so a conflict cannot exist
without the contribution that caused it (or vice versa).
"""

from __future__ import annotations

import logging

from ..db import database as db
from ..domain import ids
from ..domain.events import EventEnvelope, EventType
from ..domain.room import Participant, Room
from ..domain.task import ConflictKind, ConflictStatus
from ..util import normalize_title, utcnow_iso
from . import eventlog
from .actors import SYSTEM_ACTOR, actor_for

log = logging.getLogger(__name__)

#: Two titles are "the same work" if their normalized forms match exactly, or if
#: one contains the other and they share a target. Kept lexical on purpose:
#: embedding similarity would mean paying for inference, which this product does
#: not do (ADR-006).
MIN_TITLE_WORDS_FOR_CONTAINMENT = 3


async def _record_tx(
    tx: db.Tx,
    *,
    room: Room,
    kind: ConflictKind,
    subject_refs: list[str],
    participant_ids: list[str],
    detail: str,
    actor=SYSTEM_ACTOR,
) -> EventEnvelope:
    conflict_id = ids.new_id(ids.CONFLICT)
    await tx.execute(
        """
        INSERT INTO conflicts (
            id, room_id, kind, status, subject_refs, participant_ids, detail, detected_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            conflict_id,
            room.id,
            kind.value,
            ConflictStatus.OPEN.value,
            db.dumps(subject_refs),
            db.dumps(participant_ids),
            detail,
            utcnow_iso(),
        ),
    )
    return await eventlog.append(
        tx,
        room_id=room.id,
        type_=EventType.CONFLICT_DETECTED,
        actor=actor,
        payload={
            "conflict_id": conflict_id,
            "kind": kind.value,
            "subject_refs": subject_refs,
            "participant_ids": participant_ids,
            "detail": detail,
        },
    )


async def _already_open(
    tx: db.Tx, *, room_id: str, kind: ConflictKind, subject_refs: list[str]
) -> bool:
    """Avoid re-raising the same open conflict on every update.

    Without this, a participant editing its declaration repeatedly would flood the
    board with duplicates of one collision.
    """
    rows = await tx.fetch_all(
        "SELECT subject_refs FROM conflicts WHERE room_id = ? AND kind = ? AND status = 'open'",
        (room_id, kind.value),
    )
    wanted = set(subject_refs)
    return any(set(db.str_list(r["subject_refs"])) == wanted for r in rows)


# ---------------------------------------------------------------------------
# Duplicate tasks
# ---------------------------------------------------------------------------


def _titles_match(a: str, b: str, *, shares_target: bool) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if not shares_target:
        return False
    shorter, longer = sorted((na, nb), key=len)
    return len(shorter.split()) >= MIN_TITLE_WORDS_FOR_CONTAINMENT and shorter in longer


async def detect_duplicate_task_tx(
    tx: db.Tx,
    *,
    room: Room,
    task_id: str,
    participant: Participant,
    title: str,
    targets: list[str],
) -> list[EventEnvelope]:
    """Flag a new task that looks like existing non-terminal work."""
    rows = await tx.fetch_all(
        """
        SELECT id, title, targets, created_by_participant_id
        FROM tasks
        WHERE room_id = ? AND id != ? AND status NOT IN ('done','cancelled')
        """,
        (room.id, task_id),
    )
    target_set = set(targets)
    events: list[EventEnvelope] = []

    for row in rows:
        other_targets = set(db.str_list(row["targets"]))
        shares = bool(target_set & other_targets)
        if not _titles_match(title, row["title"], shares_target=shares):
            continue
        refs = sorted([task_id, row["id"]])
        if await _already_open(
            tx, room_id=room.id, kind=ConflictKind.DUPLICATE_TASK, subject_refs=refs
        ):
            continue
        overlap = sorted(target_set & other_targets)
        detail = (
            f"“{title}” looks like the existing task “{row['title']}”."
            + (f" Shared targets: {', '.join(overlap)}." if overlap else "")
            + " Nobody is blocked — decide whether to merge, or keep both deliberately."
        )
        events.append(
            await _record_tx(
                tx,
                room=room,
                kind=ConflictKind.DUPLICATE_TASK,
                subject_refs=refs,
                participant_ids=sorted({participant.id, row["created_by_participant_id"]}),
                detail=detail,
                actor=actor_for(participant),
            )
        )
    return events


# ---------------------------------------------------------------------------
# Overlapping work
# ---------------------------------------------------------------------------


async def detect_overlapping_work_tx(
    tx: db.Tx,
    *,
    room: Room,
    work_id: str,
    participant: Participant,
    targets: list[str],
) -> list[EventEnvelope]:
    """Flag two active declarations touching intersecting targets.

    This is the "avoid conflicts" step of the core loop doing its job: the point is
    for both agents to find out *now*, not when their edits collide.
    """
    if not targets:
        return []

    rows = await tx.fetch_all(
        """
        SELECT id, participant_id, headline, targets
        FROM work_declarations
        WHERE room_id = ? AND id != ? AND ended_at IS NULL
        """,
        (room.id, work_id),
    )
    target_set = set(targets)
    events: list[EventEnvelope] = []

    for row in rows:
        if row["participant_id"] == participant.id:
            # One participant working on two things that touch the same file is its
            # own business, not a coordination problem.
            continue
        overlap = sorted(target_set & set(db.str_list(row["targets"])))
        if not overlap:
            continue
        refs = sorted([work_id, row["id"]])
        if await _already_open(
            tx, room_id=room.id, kind=ConflictKind.OVERLAPPING_WORK, subject_refs=refs
        ):
            continue
        other = await tx.fetch_one(
            "SELECT display_name FROM participants WHERE id = ?", (row["participant_id"],)
        )
        other_name = other["display_name"] if other else "another participant"
        detail = (
            f"You and {other_name} are both working on: {', '.join(overlap)}. "
            f"Their declaration: “{row['headline']}”. Coordinate before editing."
        )
        events.append(
            await _record_tx(
                tx,
                room=room,
                kind=ConflictKind.OVERLAPPING_WORK,
                subject_refs=refs,
                participant_ids=sorted({participant.id, row["participant_id"]}),
                detail=detail,
                actor=actor_for(participant),
            )
        )
    return events


# ---------------------------------------------------------------------------
# Claim races
# ---------------------------------------------------------------------------


async def record_claim_race_tx(
    tx: db.Tx,
    *,
    room: Room,
    task_id: str,
    loser: Participant,
    winner_participant_id: str | None,
) -> list[EventEnvelope]:
    """Record that two participants went for one task.

    Kept even though the loser already got a `lease_conflict` error, because the
    *room* needs to know the work was contended — that is a signal the task graph is
    under-specified, and it is invisible if only the loser hears about it.
    """
    refs = [task_id]
    # The winner may be unknown if its claim lapsed between the failed attempt and this
    # read; record the loser alone rather than dropping the conflict entirely.
    involved = {loser.id}
    if winner_participant_id:
        involved.add(winner_participant_id)
    participants = sorted(involved)
    if await _already_open(tx, room_id=room.id, kind=ConflictKind.CLAIM_RACE, subject_refs=refs):
        return []
    winner = None
    if winner_participant_id:
        winner = await tx.fetch_one(
            "SELECT display_name FROM participants WHERE id = ?", (winner_participant_id,)
        )
    winner_name = winner["display_name"] if winner else "another participant"
    detail = (
        f"{loser.identity.display_name} tried to claim a task already leased by "
        f"{winner_name}. Two participants wanted the same work — consider splitting it."
    )
    return [
        await _record_tx(
            tx,
            room=room,
            kind=ConflictKind.CLAIM_RACE,
            subject_refs=refs,
            participant_ids=participants,
            detail=detail,
        )
    ]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def resolve_tx(
    tx: db.Tx,
    *,
    room: Room,
    participant: Participant,
    conflict_id: str,
    resolution: str,
    dismissed: bool = False,
) -> list[EventEnvelope]:
    status = ConflictStatus.DISMISSED if dismissed else ConflictStatus.RESOLVED
    affected = await tx.execute(
        "UPDATE conflicts SET status = ?, resolved_at = ?, resolution = ? "
        "WHERE id = ? AND room_id = ? AND status = 'open'",
        (status.value, utcnow_iso(), resolution, conflict_id, room.id),
    )
    if affected == 0:
        return []
    return [
        await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.CONFLICT_RESOLVED,
            actor=actor_for(participant),
            payload={
                "conflict_id": conflict_id,
                "status": status.value,
                "resolution": resolution,
            },
        )
    ]

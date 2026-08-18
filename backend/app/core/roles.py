"""Room roles: who coordinates whom (D-088).

A seat's **room role** is its position in the coordination hierarchy, and it is a
different question from its authority. `participants.role` resolves to scopes and is
what "never reduce standing" is measured on; this answers "who allocates work and who
executes it". Keeping them apart is what stops a coordination position from minting
privileges (ADR-013) — every orchestrator act is gated on `room.admin` *plus* this
position *plus* a stated reason, in `authz.require_orchestrator`.

**Legacy rooms need no migration write.** A room created before this existed has no
role rows, and a backfill that wrote events into finished rooms would be inventing
history. So the role is stored going forward and *derived* on read for seats that have
none: an owner reads as the orchestrator, an observer as an observer, anything else as
a supervisor. That is the same read-side widening `store._widen_split_scopes` uses for
the scope split (D-053), and for the same reason: the state that matters is the one
already in production, and it cannot be assumed to have been rewritten.

One consequence is stated rather than hidden: a legacy room with two owners derives two
orchestrators. `room_roles` resolves that by seniority — earliest `joined_at` wins and
the rest read as supervisors — so a room always has exactly one coordinator even before
anyone assigns one explicitly.
"""

from __future__ import annotations

from typing import Any

from ..db import database as db
from ..domain.commands import AssignRoomRoleCommand
from ..domain.events import EventType
from ..domain.room import MembershipState, Participant, ParticipantRole, RoomRole, RoomRoleSource
from ..util import utcnow_iso
from . import authz, eventlog, store
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import InvalidCommand, NotFound


def derived_role(role: ParticipantRole) -> RoomRole:
    """What a seat with no stored role reads as.

    Not a guess: an owner is the seat that created the room or was promoted to run it,
    and an observer has told us it is not working. Everything else is a human's
    representative, which is what a supervisor is.
    """
    if role is ParticipantRole.OWNER:
        return RoomRole.ORCHESTRATOR
    if role is ParticipantRole.OBSERVER:
        return RoomRole.OBSERVER
    return RoomRole.SUPERVISOR


async def assign_tx(
    tx: db.Tx,
    *,
    room_id: str,
    participant_id: str,
    room_role: RoomRole,
    source: RoomRoleSource,
    assigned_by_participant_id: str | None = None,
    reason: str = "",
    assigned_seq: int = 0,
) -> None:
    """Write a seat's role inside the caller's transaction.

    Used by room creation and join, so a seat never exists without a position, and the
    position lands in the same transaction as the membership row it describes.

    The orchestrator row is guarded by a partial unique index rather than by a read
    here: two concurrent promotions must not both believe they won, and the engine
    arbitrating that is the portable form of the guarantee (ADR-009).
    """
    now = utcnow_iso()
    await tx.execute(
        """
        INSERT INTO participant_roles (
            participant_id, room_id, room_role, assigned_by_participant_id,
            assigned_seq, reason, source, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(participant_id) DO UPDATE SET
            room_role = excluded.room_role,
            assigned_by_participant_id = excluded.assigned_by_participant_id,
            assigned_seq = excluded.assigned_seq,
            reason = excluded.reason,
            source = excluded.source,
            retired_at = NULL,
            updated_at = excluded.updated_at
        """,
        (
            participant_id,
            room_id,
            room_role.value,
            assigned_by_participant_id,
            assigned_seq,
            reason,
            source.value,
            now,
            now,
        ),
    )


async def retire_tx(tx: db.Tx, *, participant_id: str) -> None:
    """Stand a seat down without replacing it.

    The row is retired rather than deleted so it stays a valid audit reference, and so
    the partial unique index stops counting it — which is what lets a replacement
    orchestrator be inserted at all.
    """
    await tx.execute(
        "UPDATE participant_roles SET room_role = ?, retired_at = ?, updated_at = ? "
        "WHERE participant_id = ? AND retired_at IS NULL",
        (RoomRole.UNASSIGNED.value, utcnow_iso(), utcnow_iso(), participant_id),
    )


async def role_for(participant: Participant) -> RoomRole:
    """This seat's position, stored or derived. Never raises."""
    row = await db.fetch_one(
        "SELECT room_role FROM participant_roles WHERE participant_id = ? AND retired_at IS NULL",
        (participant.id,),
    )
    if row is None:
        return derived_role(participant.role)
    try:
        return RoomRole(row["room_role"])
    except ValueError:
        # A value this build does not know: read it as unassigned rather than raising.
        # Forward compatibility is required of clients (docs/PROTOCOL.md §2); a server
        # that crashes on its own future data holds itself to a lower standard.
        return RoomRole.UNASSIGNED


async def room_roles(room_id: str) -> dict[str, RoomRole]:
    """Every joined seat's position, with legacy ambiguity resolved by seniority.

    Returns a mapping rather than rows because every caller wants "what is this seat"
    for a set of seats it already loaded, and a second query per participant is how a
    projection turns into an N+1.
    """
    rows = await db.fetch_all(
        """
        SELECT p.id, p.role, p.joined_at, r.room_role
          FROM participants p
          LEFT JOIN participant_roles r
            ON r.participant_id = p.id AND r.retired_at IS NULL
         WHERE p.room_id = ? AND p.state = ?
         ORDER BY p.joined_at ASC, p.id ASC
        """,
        (room_id, MembershipState.JOINED.value),
    )
    out: dict[str, RoomRole] = {}
    orchestrator_taken = False
    for row in rows:
        stored = row["room_role"]
        if stored:
            try:
                role = RoomRole(stored)
            except ValueError:
                role = RoomRole.UNASSIGNED
        else:
            role = derived_role(ParticipantRole(row["role"]))
        if role is RoomRole.ORCHESTRATOR:
            # Seniority breaks a legacy tie. A stored row cannot collide here — the
            # partial unique index already refused a second one — so this only ever
            # demotes a *derived* duplicate, and it demotes the later joiner.
            if orchestrator_taken:
                role = RoomRole.SUPERVISOR
            else:
                orchestrator_taken = True
        out[row["id"]] = role
    return out


async def orchestrator_of(room_id: str) -> str | None:
    """The participant id currently coordinating this room, if there is one."""
    for participant_id, role in (await room_roles(room_id)).items():
        if role is RoomRole.ORCHESTRATOR:
            return participant_id
    return None


async def assign(*, participant: Participant, command: AssignRoomRoleCommand) -> dict[str, Any]:
    """Place another seat in the hierarchy. Orchestrator only.

    This is the recovery and reorganisation path: promoting a supervisor after an
    orchestrator is gone, standing down a seat that has left, or naming a coordinator
    in a room that never had one. It is deliberately the *only* way a seat's position
    changes after join, because a position that could change implicitly would make
    "who is coordinating" a question with two answers.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)
    authz.require_writable(room)
    caller_role = await role_for(participant)

    target = await store.load_participant_for_room(room.id, command.target_participant_id)
    if target.state is not MembershipState.JOINED:
        raise InvalidCommand(
            "That seat is not an active participant of this room.",
            target_participant_id=target.id,
            state=target.state.value,
        )

    promoting_self_into_an_empty_chair = (
        command.room_role is RoomRole.ORCHESTRATOR
        and target.id == participant.id
        and await orchestrator_of(room.id) is None
    )
    if promoting_self_into_an_empty_chair:
        # The room has no coordinator, so there is nobody with the authority to appoint
        # one; requiring the orchestrator gate here would make an orchestrator loss
        # unrecoverable without operator surgery (§25). `room.admin` still applies, the
        # act is logged with its reason, and the partial unique index means exactly one
        # of several simultaneous volunteers wins.
        authz.require_admin(participant)
        if not command.reason.strip():
            raise InvalidCommand("A stated reason is required to take over coordination.")
    else:
        authz.require_orchestrator(
            participant,
            caller_role,
            action="assign a seat's room role",
            reason=command.reason,
        )

    previous = await role_for(target)
    if previous is command.room_role:
        return {
            "participant_id": target.id,
            "room_role": command.room_role.value,
            "previous_room_role": previous.value,
            "unchanged": True,
        }

    async def body(tx: db.Tx) -> CommandOutcome:
        if command.room_role is RoomRole.ORCHESTRATOR:
            # Stand the incumbent down first: the index permits exactly one live
            # orchestrator, so a handover is two writes in one transaction.
            incumbent = await tx.fetch_one(
                "SELECT participant_id FROM participant_roles "
                "WHERE room_id = ? AND room_role = ? AND retired_at IS NULL",
                (room.id, RoomRole.ORCHESTRATOR.value),
            )
            if incumbent is not None and incumbent["participant_id"] != target.id:
                await retire_tx(tx, participant_id=incumbent["participant_id"])

        if command.room_role is RoomRole.UNASSIGNED:
            await retire_tx(tx, participant_id=target.id)
        else:
            await assign_tx(
                tx,
                room_id=room.id,
                participant_id=target.id,
                room_role=command.room_role,
                source=RoomRoleSource.ASSIGNED,
                assigned_by_participant_id=participant.id,
                reason=command.reason,
                assigned_seq=room.event_seq + 1,
            )

        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.ROOM_ROLE_ASSIGNED,
            actor=actor_for(participant),
            payload={
                "participant_id": target.id,
                "room_role": command.room_role.value,
                "previous_role": previous.value,
                "source": RoomRoleSource.ASSIGNED.value,
                "assigned_by_participant_id": participant.id,
                "reason": command.reason,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={
                "participant_id": target.id,
                "room_role": command.room_role.value,
                "previous_room_role": previous.value,
            },
            events=[event],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="participant.room_role.assign",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return {**outcome.result, "seq": outcome.seq, "replayed": outcome.replayed}


async def require_participant_of_room(room_id: str, participant_id: str) -> Participant:
    """Load a seat, refusing an id from another room.

    A global id lookup followed by a room check is an existence oracle; every loader
    here scopes by room for that reason.
    """
    try:
        return await store.load_participant_for_room(room_id, participant_id)
    except NotFound:
        raise NotFound(
            "That participant is not in this room.",
            participant_id=participant_id,
            room_id=room_id,
        ) from None

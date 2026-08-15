"""Row → domain mapping and loaders.

One place that knows the table shapes, so services read domain objects and never
raw rows. Also the single place that applies **lazy lease expiry**: an expired claim
is invisible to every reader the instant it lapses, whether or not the reaper has
run yet (`docs/PROTOCOL.md` §4). Correctness therefore does not depend on a
background task firing on time — the reaper only controls how quickly the *event*
and the durable status change land.

Note what a loader never does: mutate. A read that wrote would make replay
non-deterministic and put an event append on a read path.
"""

from __future__ import annotations

from typing import Any

from ..db import database as db
from ..domain.capabilities import CapabilityProfile, DeliveryMode, HostClass
from ..domain.directive import Directive, DirectiveAction, EffectStatus
from ..domain.identity import (
    AgentIdentity,
    Capability,
    IdentityProvenance,
    IdentitySummary,
    Organization,
    OrgRole,
    PrincipalKind,
    TrustTier,
    User,
)
from ..domain.room import (
    Connection,
    Invitation,
    InvitationTargetKind,
    MembershipState,
    Participant,
    ParticipantRole,
    RetentionPolicy,
    Room,
    RoomPolicy,
    RoomStatus,
    RoomVisibility,
    Scope,
)
from ..domain.task import (
    Conflict,
    ConflictKind,
    ConflictStatus,
    Steering,
    Task,
    TaskClaim,
    TaskStatus,
)
from ..domain.work import WorkDeclaration, WorkEndReason, WorkStatus
from ..util import hash_token, is_past
from .errors import NotFound, Unauthenticated

# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def to_organization(row: Any) -> Organization:
    return Organization(
        id=row["id"], name=row["name"], slug=row["slug"], created_at=row["created_at"]
    )


def to_user(row: Any) -> User:
    return User(
        id=row["id"],
        org_id=row["org_id"],
        email=row["email"],
        display_name=row["display_name"],
        role=OrgRole(row["role"]),
        created_at=row["created_at"],
    )


def to_identity(row: Any) -> AgentIdentity:
    return AgentIdentity(
        id=row["id"],
        org_id=row["org_id"],
        owner_user_id=row["owner_user_id"],
        display_name=row["display_name"],
        kind=PrincipalKind(row["kind"]),
        host_class=HostClass(row["host_class"]),
        description=row["description"],
        declared_capabilities=[Capability(c) for c in db.str_list(row["declared_capabilities"])],
        trust=TrustTier(row["trust"]),
        provenance=IdentityProvenance(row["provenance"]),
        created_at=row["created_at"],
    )


def to_room(row: Any) -> Room:
    return Room(
        id=row["id"],
        org_id=row["org_id"],
        name=row["name"],
        purpose=row["purpose"],
        visibility=RoomVisibility(row["visibility"]),
        status=RoomStatus(row["status"]),
        event_seq=int(row["event_seq"]),
        retained_from_seq=int(row["retained_from_seq"]),
        policy=RoomPolicy(**db.loads(row["policy"], {})),
        retention=RetentionPolicy(**db.loads(row["retention"], {})),
        created_at=row["created_at"],
        created_by_user_id=row["created_by_user_id"],
        expires_at=row["expires_at"],
        closed_at=row["closed_at"],
    )


def to_invitation(row: Any) -> Invitation:
    return Invitation(
        id=row["id"],
        room_id=row["room_id"],
        target_kind=InvitationTargetKind(row["target_kind"]),
        target_value=row["target_value"],
        role=ParticipantRole(row["role"]),
        scopes=[Scope(s) for s in db.str_list(row["scopes"])],
        max_redemptions=int(row["max_redemptions"]),
        redemptions=int(row["redemptions"]),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        created_by_participant_id=row["created_by_participant_id"],
        revoked_at=row["revoked_at"],
    )


def to_participant(row: Any, identity: IdentitySummary) -> Participant:
    """Map a participant row.

    The participant's own `display_name` wins over the identity's. Display name is a
    per-room property — the same agent may present as "Reviewer" in one room and
    "Builder" in another — and the column was being written on join but never read,
    so the room silently showed the identity-level name instead.
    """
    # Every caller passes a `participants` row, so the column is always present.
    row_name = row["display_name"]
    if row_name and row_name != identity.display_name:
        identity = identity.model_copy(update={"display_name": row_name})
    return Participant(
        id=row["id"],
        room_id=row["room_id"],
        agent_identity_id=row["agent_identity_id"],
        org_id=row["org_id"],
        role=ParticipantRole(row["role"]),
        scopes=[Scope(s) for s in db.str_list(row["scopes"])],
        trust=TrustTier(row["trust"]),
        state=MembershipState(row["state"]),
        identity=identity,
        joined_at=row["joined_at"],
        left_at=row["left_at"],
    )


def to_connection(row: Any) -> Connection:
    return Connection(
        id=row["id"],
        room_id=row["room_id"],
        participant_id=row["participant_id"],
        attachment_id=row["attachment_id"],
        host_class=HostClass(row["host_class"]),
        profile=CapabilityProfile(**db.loads(row["profile"], {})),
        delivery_mode=DeliveryMode(row["delivery_mode"]),
        heartbeat_interval_s=int(row["heartbeat_interval_s"]),
        opened_at=row["opened_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        last_delivered_seq=int(row["last_delivered_seq"]),
        closed_at=row["closed_at"],
    )


def to_directive(row: Any) -> Directive:
    return Directive(
        id=row["id"],
        room_id=row["room_id"],
        target_participant_id=row["target_participant_id"],
        task_id=row["task_id"],
        action=DirectiveAction(row["action"]),
        reason=row["reason"],
        issued_by_participant_id=row["issued_by_participant_id"],
        human_origin=bool(row["human_origin"]),
        created_seq=int(row["created_seq"]),
        effect_status=EffectStatus(row["effect_status"]),
        created_at=row["created_at"],
        applied_at=row["applied_at"],
        acknowledged_at=row["acknowledged_at"],
        acknowledged_by_participant_id=row["acknowledged_by_participant_id"],
    )


def to_task(row: Any) -> Task:
    """Map a task row, applying lease expiry as seen by any reader.

    An expired claim is dropped from the returned object and a held status falls
    back to `open`. The row still carries the stale claim until the reaper rewrites
    it; `fence` is read from the row either way, so the fence a reclaim allocates is
    still strictly greater than the expired one.
    """
    status = TaskStatus(row["status"])
    claim: TaskClaim | None = None

    if row["claim_lease_id"] and not is_past(row["claim_expires_at"]):
        claim = TaskClaim(
            lease_id=row["claim_lease_id"],
            participant_id=row["claim_participant_id"],
            fence=int(row["claim_fence"]),
            claimed_at=row["claim_claimed_at"],
            expires_at=row["claim_expires_at"],
            heartbeat_interval_s=int(row["claim_heartbeat_interval_s"] or 0),
            renewed_at=row["claim_renewed_at"],
            # Read from the row only while the lease is live. An expired lease has
            # no executor, which is the same sentence as having no claim.
            executor_attachment_id=row["executor_attachment_id"],
            executor_connection_id=row["executor_connection_id"],
        )
    elif row["claim_lease_id"] and status in {
        TaskStatus.CLAIMED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.BLOCKED,
    }:
        # Lease lapsed: the task is available again as far as readers are concerned.
        status = TaskStatus.OPEN

    return Task(
        id=row["id"],
        room_id=row["room_id"],
        title=row["title"],
        description=row["description"],
        status=status,
        targets=db.str_list(row["targets"]),
        priority=int(row["priority"]),
        created_by_participant_id=row["created_by_participant_id"],
        fence=int(row["fence"]),
        claim=claim,
        steering=Steering(row["steering"]),
        steering_reason=row["steering_reason"],
        steering_by_participant_id=row["steering_by_participant_id"],
        steering_at=row["steering_at"],
        result=row["result"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def to_work(row: Any, *, stale: bool = False) -> WorkDeclaration:
    return WorkDeclaration(
        id=row["id"],
        room_id=row["room_id"],
        participant_id=row["participant_id"],
        headline=row["headline"],
        status=WorkStatus(row["status"]),
        targets=db.str_list(row["targets"]),
        task_id=row["task_id"],
        note=row["note"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        heartbeat_at=row["heartbeat_at"],
        expected_done_by=row["expected_done_by"],
        ended_at=row["ended_at"],
        end_reason=WorkEndReason(row["end_reason"]) if row["end_reason"] else None,
        stale=stale,
    )


def to_conflict(row: Any) -> Conflict:
    return Conflict(
        id=row["id"],
        room_id=row["room_id"],
        kind=ConflictKind(row["kind"]),
        status=ConflictStatus(row["status"]),
        subject_refs=db.str_list(row["subject_refs"]),
        participant_ids=db.str_list(row["participant_ids"]),
        detail=row["detail"],
        detected_at=row["detected_at"],
        resolved_at=row["resolved_at"],
        resolution=row["resolution"],
    )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


async def _one(sql: str, params: tuple, tx: db.Tx | None) -> Any:
    return await (tx.fetch_one(sql, params) if tx else db.fetch_one(sql, params))


async def _all(sql: str, params: tuple, tx: db.Tx | None) -> list[Any]:
    return await (tx.fetch_all(sql, params) if tx else db.fetch_all(sql, params))


async def load_room(room_id: str, *, tx: db.Tx | None = None) -> Room:
    row = await _one("SELECT * FROM rooms WHERE id = ?", (room_id,), tx)
    if row is None:
        raise NotFound("Room does not exist.", room_id=room_id)
    return to_room(row)


async def load_identity_summary(identity_id: str, *, tx: db.Tx | None = None) -> IdentitySummary:
    row = await _one(
        """
        SELECT i.*, o.name AS org_name
        FROM agent_identities i JOIN organizations o ON o.id = i.org_id
        WHERE i.id = ?
        """,
        (identity_id,),
        tx,
    )
    if row is None:
        raise NotFound("Agent identity does not exist.", identity_id=identity_id)
    return IdentitySummary(
        identity_id=row["id"],
        display_name=row["display_name"],
        org_id=row["org_id"],
        org_name=row["org_name"],
        kind=PrincipalKind(row["kind"]),
        host_class=HostClass(row["host_class"]),
        description=row["description"],
        trust=TrustTier(row["trust"]),
        provenance=IdentityProvenance(row["provenance"]),
    )


async def load_participant(participant_id: str, *, tx: db.Tx | None = None) -> Participant:
    row = await _one("SELECT * FROM participants WHERE id = ?", (participant_id,), tx)
    if row is None:
        raise NotFound("Participant does not exist.", participant_id=participant_id)
    identity = await load_identity_summary(row["agent_identity_id"], tx=tx)
    return to_participant(row, identity)


async def load_participant_by_token(token: str, *, tx: db.Tx | None = None) -> Participant:
    """Resolve a participant bearer token. Room-scoped and revocable by design."""
    row = await _one("SELECT * FROM participants WHERE token_hash = ?", (hash_token(token),), tx)
    if row is None:
        raise Unauthenticated("Unknown or revoked participant token.")
    identity = await load_identity_summary(row["agent_identity_id"], tx=tx)
    return to_participant(row, identity)


async def find_participant_by_identity(
    room_id: str, identity_id: str, *, tx: db.Tx | None = None
) -> Participant | None:
    row = await _one(
        "SELECT * FROM participants WHERE room_id = ? AND agent_identity_id = ?",
        (room_id, identity_id),
        tx,
    )
    if row is None:
        return None
    identity = await load_identity_summary(row["agent_identity_id"], tx=tx)
    return to_participant(row, identity)


async def list_participants(room_id: str, *, tx: db.Tx | None = None) -> list[Participant]:
    rows = await _all(
        """
        SELECT p.*, i.display_name AS i_display_name, i.kind AS i_kind,
               i.host_class AS i_host_class, i.description AS i_description,
               i.trust AS i_trust, i.provenance AS i_provenance,
               o.name AS org_name
        FROM participants p
        JOIN agent_identities i ON i.id = p.agent_identity_id
        JOIN organizations o ON o.id = i.org_id
        WHERE p.room_id = ?
        ORDER BY p.joined_at IS NULL, p.joined_at ASC
        """,
        (room_id,),
        tx,
    )
    out: list[Participant] = []
    for row in rows:
        summary = IdentitySummary(
            identity_id=row["agent_identity_id"],
            display_name=row["i_display_name"],
            org_id=row["org_id"],
            org_name=row["org_name"],
            kind=PrincipalKind(row["i_kind"]),
            host_class=HostClass(row["i_host_class"]),
            description=row["i_description"],
            trust=TrustTier(row["i_trust"]),
            provenance=IdentityProvenance(row["i_provenance"]),
        )
        out.append(to_participant(row, summary))
    return out


async def list_open_connections(room_id: str, *, tx: db.Tx | None = None) -> list[Connection]:
    rows = await _all(
        "SELECT * FROM connections WHERE room_id = ? AND closed_at IS NULL",
        (room_id,),
        tx,
    )
    return [to_connection(r) for r in rows]


async def load_connection(connection_id: str, *, tx: db.Tx | None = None) -> Connection:
    row = await _one("SELECT * FROM connections WHERE id = ?", (connection_id,), tx)
    if row is None:
        raise NotFound("Connection does not exist.", connection_id=connection_id)
    return to_connection(row)


async def load_task(task_id: str, *, tx: db.Tx | None = None) -> Task:
    row = await _one("SELECT * FROM tasks WHERE id = ?", (task_id,), tx)
    if row is None:
        raise NotFound("Task does not exist.", task_id=task_id)
    return to_task(row)


async def list_tasks(room_id: str, *, tx: db.Tx | None = None) -> list[Task]:
    rows = await _all(
        "SELECT * FROM tasks WHERE room_id = ? ORDER BY priority DESC, created_at ASC",
        (room_id,),
        tx,
    )
    return [to_task(r) for r in rows]


async def load_work(work_id: str, *, tx: db.Tx | None = None) -> WorkDeclaration:
    row = await _one("SELECT * FROM work_declarations WHERE id = ?", (work_id,), tx)
    if row is None:
        raise NotFound("Work declaration does not exist.", work_id=work_id)
    return to_work(row)


async def list_open_work(room_id: str, *, tx: db.Tx | None = None) -> list[WorkDeclaration]:
    rows = await _all(
        """
        SELECT * FROM work_declarations
        WHERE room_id = ? AND ended_at IS NULL
        ORDER BY started_at ASC
        """,
        (room_id,),
        tx,
    )
    return [to_work(r) for r in rows]


async def list_conflicts(room_id: str, *, tx: db.Tx | None = None) -> list[Conflict]:
    rows = await _all(
        "SELECT * FROM conflicts WHERE room_id = ? ORDER BY detected_at DESC LIMIT 200",
        (room_id,),
        tx,
    )
    return [to_conflict(r) for r in rows]

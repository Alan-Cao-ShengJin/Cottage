"""Room lifecycle, invitations, and authenticated joining.

Membership has exactly one entry path: redeeming an invitation. There is no
"create a participant" call, so a room cannot gain a member through any code path
that skipped scope resolution and trust clamping.

Rejoining reuses the existing participant row (schema `UNIQUE (room_id,
agent_identity_id)`), which keeps a participant id stable across reconnects. That
matters because participant ids appear in claims, provenance, and every event in
the log — a new id per reconnect would make the audit trail unreadable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..db import database as db
from ..domain import ids
from ..domain.capabilities import Capability, HostClass
from ..domain.commands import (
    CreateInvitationCommand,
    CreateRoomCommand,
    JoinRoomCommand,
    LeaveRoomCommand,
)
from ..domain.events import EventActor, EventType
from ..domain.identity import AgentIdentity, PrincipalKind, TrustTier, User
from ..domain.room import (
    ROLE_RANK,
    Invitation,
    InvitationTargetKind,
    LeaveReason,
    Participant,
    ParticipantRole,
    RetentionPolicy,
    Room,
    RoomPolicy,
    RoomStatus,
    RoomVisibility,
    Scope,
)
from ..util import hash_token, is_past, iso_in, new_token, utcnow_iso
from . import authz, eventlog, store
from .actors import SYSTEM_ACTOR, actor_for
from .dispatch import CommandOutcome, execute_command, publish_committed
from .errors import Forbidden, InvalidCommand, NotFound, RoomClosed, Unauthenticated

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


async def _insert_participant_tx(
    tx: db.Tx,
    *,
    participant_id: str,
    room_id: str,
    identity: AgentIdentity,
    role: ParticipantRole,
    scopes: list[Scope],
    trust: TrustTier,
    display_name: str,
    token_hash: str,
) -> None:
    """Insert a participant row. The only place a membership row is created."""
    await tx.execute(
        """
        INSERT INTO participants (
            id, room_id, agent_identity_id, org_id, role, scopes, trust,
            state, display_name, token_hash, joined_at
        ) VALUES (?,?,?,?,?,?,?,'joined',?,?,?)
        """,
        (
            participant_id,
            room_id,
            identity.id,
            identity.org_id,
            role.value,
            db.dumps([s.value for s in scopes]),
            trust.value,
            display_name,
            token_hash,
            utcnow_iso(),
        ),
    )


async def _insert_invitation_tx(
    tx: db.Tx,
    *,
    invitation_id: str,
    room_id: str,
    token_hash: str,
    target_kind: InvitationTargetKind,
    target_value: str | None,
    role: ParticipantRole,
    scopes: list[Scope],
    max_redemptions: int,
    expires_at: str | None,
    created_by_participant_id: str | None,
) -> None:
    await tx.execute(
        """
        INSERT INTO invitations (
            id, room_id, token_hash, target_kind, target_value, role, scopes,
            max_redemptions, redemptions, expires_at, created_at,
            created_by_participant_id
        ) VALUES (?,?,?,?,?,?,?,?,0,?,?,?)
        """,
        (
            invitation_id,
            room_id,
            token_hash,
            target_kind.value,
            target_value,
            role.value,
            db.dumps([s.value for s in scopes]),
            max_redemptions,
            expires_at,
            utcnow_iso(),
            created_by_participant_id,
        ),
    )


async def ensure_identity(
    *,
    org_id: str,
    owner_user_id: str,
    display_name: str,
    kind: PrincipalKind = PrincipalKind.AGENT,
    host_class: HostClass = HostClass.UNKNOWN,
    description: str = "",
    capabilities: list[Capability] | None = None,
) -> AgentIdentity:
    """Get or create an identity for a user, keyed on `(owner, display_name)`.

    **One user owns many identities**, and that is the point rather than an edge case:
    a person brings Claude Code *and* Codex *and* ChatGPT, and each is a separate
    participant with its own presence, capabilities, and leases. Keying on display name
    is what lets the same authenticated human join one room three times as three agents.

    An earlier version returned a single identity per user, which quietly capped every
    person at one seat per room and made a second join a *rejoin* — rotating the first
    seat's token out from under it. Found by a smoke test where a user tried to add a
    second participant and lost the first one's session.
    """
    row = await db.fetch_one(
        """
        SELECT * FROM agent_identities
        WHERE owner_user_id = ? AND display_name = ?
        LIMIT 1
        """,
        (owner_user_id, display_name),
    )
    if row is not None:
        return store.to_identity(row)
    return await create_identity(
        org_id=org_id,
        owner_user_id=owner_user_id,
        display_name=display_name,
        kind=kind,
        host_class=host_class,
        description=description,
        capabilities=capabilities,
    )


async def ensure_human_identity(user: User, *, display_name: str | None = None) -> AgentIdentity:
    """Get or create the identity a human presents as. See `ensure_identity`."""
    return await ensure_identity(
        org_id=user.org_id,
        owner_user_id=user.id,
        display_name=display_name or user.display_name,
        kind=PrincipalKind.HUMAN,
        host_class=HostClass.BROWSER_HUMAN,
    )


@dataclass
class CreatedRoom:
    """Everything the creator needs, from one call.

    `participant_token` makes the creator a member immediately; `join_token` is the
    single thing they share with everyone else. Both are returned exactly once.
    """

    room: Room
    participant: Participant
    participant_token: str
    join_token: str
    invitation_id: str


#: How many people may redeem the room's default join link, and for how long. A room
#: is a working space, not a public broadcast, so this is bounded — an admin can mint
#: further invitations with different limits.
DEFAULT_JOIN_MAX_REDEMPTIONS = 50
DEFAULT_JOIN_TTL_SECONDS = 7 * 24 * 3600


async def create_room(
    *,
    user: User,
    command: CreateRoomCommand,
    creator_display_name: str | None = None,
) -> CreatedRoom:
    """Create a room, join the creator as owner, and mint a shareable join token.

    One call, one transaction, three events (`room.created`, `participant.joined`,
    `invitation.created`).

    Creating the room and joining it used to be separate steps, which produced a
    bootstrap paradox: minting the first invitation requires an admin *participant*,
    but becoming a participant requires an invitation. Callers were seeding the owner
    row by hand — including our own tests, which is a reliable sign the API was wrong.

    Membership still has exactly one entry path for everyone else. The creator is not
    an exception to that rule so much as its origin: they are the authority the first
    invitation derives from, so their row is created by the same authenticated act that
    creates the room.
    """
    room_id = ids.new_id(ids.ROOM)
    participant_id = ids.new_id(ids.PARTICIPANT)
    invitation_id = ids.new_id(ids.INVITATION)
    now = utcnow_iso()

    policy = command.policy or RoomPolicy()
    retention = command.retention or RetentionPolicy()
    expires_at = iso_in(retention.ttl_seconds) if retention.ttl_seconds else None

    identity = await ensure_human_identity(user, display_name=creator_display_name)
    display_name = creator_display_name or identity.display_name
    owner_scopes = authz.effective_scopes(ParticipantRole.OWNER, None, TrustTier.MEMBER)
    join_scopes = authz.effective_scopes(ParticipantRole.COLLABORATOR, None, TrustTier.MEMBER)

    participant_token = new_token()
    join_token = new_token()
    join_expires_at = iso_in(
        min(DEFAULT_JOIN_TTL_SECONDS, retention.ttl_seconds or DEFAULT_JOIN_TTL_SECONDS)
    )

    actor = EventActor(
        participant_id=participant_id,
        display_name=display_name,
        kind=identity.kind,
        org_id=identity.org_id,
    )

    async def body(tx: db.Tx) -> CommandOutcome:
        await tx.execute(
            """
            INSERT INTO rooms (
                id, org_id, name, purpose, visibility, status, event_seq,
                retained_from_seq, policy, retention, created_at,
                created_by_user_id, expires_at
            ) VALUES (?,?,?,?,?,'open',0,1,?,?,?,?,?)
            """,
            (
                room_id,
                user.org_id,
                command.name,
                command.purpose,
                command.visibility.value,
                db.dumps(policy.model_dump()),
                db.dumps(retention.model_dump()),
                now,
                user.id,
                expires_at,
            ),
        )
        created = await eventlog.append(
            tx,
            room_id=room_id,
            type_=EventType.ROOM_CREATED,
            actor=actor,
            payload={
                "name": command.name,
                "purpose": command.purpose,
                "visibility": command.visibility.value,
                "policy": policy.model_dump(),
                "retention": retention.model_dump(),
            },
            causation_id=command.command_id,
        )

        await _insert_participant_tx(
            tx,
            participant_id=participant_id,
            room_id=room_id,
            identity=identity,
            role=ParticipantRole.OWNER,
            scopes=owner_scopes,
            trust=TrustTier.MEMBER,
            display_name=display_name,
            token_hash=hash_token(participant_token),
        )
        joined = await eventlog.append(
            tx,
            room_id=room_id,
            type_=EventType.PARTICIPANT_JOINED,
            actor=actor,
            payload={
                "participant_id": participant_id,
                "display_name": display_name,
                "org_id": identity.org_id,
                "kind": identity.kind.value,
                "role": ParticipantRole.OWNER.value,
                "scopes": [s.value for s in owner_scopes],
                "trust": TrustTier.MEMBER.value,
                "declared_capabilities": [],
                "rejoined": False,
                "reason": "room_creator",
            },
            causation_id=command.command_id,
        )

        await _insert_invitation_tx(
            tx,
            invitation_id=invitation_id,
            room_id=room_id,
            token_hash=hash_token(join_token),
            target_kind=InvitationTargetKind.LINK,
            target_value=None,
            role=ParticipantRole.COLLABORATOR,
            scopes=join_scopes,
            max_redemptions=DEFAULT_JOIN_MAX_REDEMPTIONS,
            expires_at=join_expires_at,
            created_by_participant_id=participant_id,
        )
        invited = await eventlog.append(
            tx,
            room_id=room_id,
            type_=EventType.INVITATION_CREATED,
            actor=actor,
            payload={
                "invitation_id": invitation_id,
                "target_kind": InvitationTargetKind.LINK.value,
                "target_value": None,
                "role": ParticipantRole.COLLABORATOR.value,
                "scopes": [s.value for s in join_scopes],
                "max_redemptions": DEFAULT_JOIN_MAX_REDEMPTIONS,
                "expires_at": join_expires_at,
                "reason": "default_join_link",
            },
            causation_id=command.command_id,
        )

        return CommandOutcome(
            result={
                "room_id": room_id,
                "participant_id": participant_id,
                "invitation_id": invitation_id,
            },
            events=[created, joined, invited],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="room.create",
        room_id=room_id,
        participant_id=participant_id,
        body=body,
    )

    resolved_room = str(outcome.result.get("room_id", room_id))
    resolved_participant = str(outcome.result.get("participant_id", participant_id))
    resolved_invitation = str(outcome.result.get("invitation_id", invitation_id))

    if outcome.replayed:
        # Same reasoning as `create_invitation` and `join_room` (D-012): both tokens are
        # stored hashed, so a replay cannot return the originals. Rotate rather than
        # hand back tokens that do not work. No duplicate room, participant, or event.
        participant_token = new_token()
        join_token = new_token()
        await db.execute(
            "UPDATE participants SET token_hash = ? WHERE id = ?",
            (hash_token(participant_token), resolved_participant),
        )
        await db.execute(
            "UPDATE invitations SET token_hash = ? WHERE id = ?",
            (hash_token(join_token), resolved_invitation),
        )

    return CreatedRoom(
        room=await store.load_room(resolved_room),
        participant=await store.load_participant(resolved_participant),
        participant_token=participant_token,
        join_token=join_token,
        invitation_id=resolved_invitation,
    )


async def close_room(*, participant: Participant, reason: str = "") -> Room:
    room = await store.load_room(participant.room_id)
    authz.require_admin(participant)
    authz.require_writable(room)

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE rooms SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'open'",
            (utcnow_iso(), room.id),
        )
        if affected == 0:
            raise RoomClosed("Room is already closed.", room_id=room.id)
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.ROOM_CLOSED,
            actor=actor_for(participant),
            payload={"reason": reason},
        )
        return CommandOutcome(result={"room_id": room.id}, events=[event])

    await execute_command(
        command_id=None,
        command_type="room.close",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await store.load_room(room.id)


async def expire_due_rooms() -> list[str]:
    """Close rooms whose TTL has elapsed.

    Separate from purge: closing stops writes but keeps content readable for the
    grace window, so participants can see why the room stopped accepting work
    (`docs/SECURITY.md` §7).
    """
    rows = await db.fetch_all(
        "SELECT id, expires_at FROM rooms WHERE status = 'open' AND expires_at IS NOT NULL"
    )
    closed: list[str] = []
    for row in rows:
        if not is_past(row["expires_at"]):
            continue
        room_id = row["id"]
        async with db.transaction() as tx:
            affected = await tx.execute(
                "UPDATE rooms SET status = 'closed', closed_at = ? "
                "WHERE id = ? AND status = 'open'",
                (utcnow_iso(), room_id),
            )
            if affected == 0:
                continue
            event = await eventlog.append(
                tx,
                room_id=room_id,
                type_=EventType.ROOM_CLOSED,
                actor=SYSTEM_ACTOR,
                payload={"reason": "retention_ttl_elapsed"},
            )
        await publish_committed([event])
        closed.append(room_id)
    return closed


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


@dataclass
class IssuedInvitation:
    """The token is returned exactly once, here. Only its hash is stored."""

    invitation: Invitation
    token: str


async def create_invitation(
    *, participant: Participant, command: CreateInvitationCommand
) -> IssuedInvitation:
    room = await store.load_room(participant.room_id)
    authz.require_admin(participant)
    authz.require_writable(room)

    if command.target_kind == InvitationTargetKind.EMAIL and not command.target_value:
        raise InvalidCommand("An email invitation needs `target_value`.")
    if command.target_kind == InvitationTargetKind.ORG and not command.target_value:
        raise InvalidCommand("An org invitation needs `target_value` (the org id).")
    if command.target_kind == InvitationTargetKind.EMAIL and command.max_redemptions != 1:
        raise InvalidCommand("An email invitation is single-use by definition.")

    scopes = authz.effective_scopes(command.role, command.scopes, TrustTier.MEMBER)
    token = new_token()
    invitation_id = ids.new_id(ids.INVITATION)
    expires_at = iso_in(command.ttl_seconds) if command.ttl_seconds else None

    async def body(tx: db.Tx) -> CommandOutcome:
        await _insert_invitation_tx(
            tx,
            invitation_id=invitation_id,
            room_id=room.id,
            token_hash=hash_token(token),
            target_kind=command.target_kind,
            target_value=command.target_value,
            role=command.role,
            scopes=scopes,
            max_redemptions=command.max_redemptions,
            expires_at=expires_at,
            created_by_participant_id=participant.id,
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.INVITATION_CREATED,
            actor=actor_for(participant),
            payload={
                "invitation_id": invitation_id,
                "target_kind": command.target_kind.value,
                # The target is logged so cross-company exposure is auditable; the
                # token never is.
                "target_value": command.target_value,
                "role": command.role.value,
                "scopes": [s.value for s in scopes],
                "max_redemptions": command.max_redemptions,
                "expires_at": expires_at,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(result={"invitation_id": invitation_id}, events=[event])

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="invitation.create",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    resolved_id = str(outcome.result.get("invitation_id", invitation_id))

    if outcome.replayed:
        # A replayed command normally returns the original result — but the original
        # token was only ever held by the caller and is stored hashed, so there is
        # nothing to return. Rotating it is the honest resolution: the same
        # authenticated admin is asking again, the old token stops working, and no
        # duplicate invitation or event is created. Documented in PROTOCOL.md §2.
        token = new_token()
        await db.execute(
            "UPDATE invitations SET token_hash = ? WHERE id = ?",
            (hash_token(token), resolved_id),
        )

    row = await db.fetch_one("SELECT * FROM invitations WHERE id = ?", (resolved_id,))
    if row is None:
        raise NotFound("Invitation no longer exists.", invitation_id=resolved_id)
    return IssuedInvitation(invitation=store.to_invitation(row), token=token)


async def revoke_invitation(*, participant: Participant, invitation_id: str) -> None:
    authz.require_admin(participant)

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE invitations SET revoked_at = ? "
            "WHERE id = ? AND room_id = ? AND revoked_at IS NULL",
            (utcnow_iso(), invitation_id, participant.room_id),
        )
        if affected == 0:
            raise NotFound("No such active invitation.", invitation_id=invitation_id)
        event = await eventlog.append(
            tx,
            room_id=participant.room_id,
            type_=EventType.INVITATION_REVOKED,
            actor=actor_for(participant),
            payload={"invitation_id": invitation_id},
        )
        return CommandOutcome(events=[event])

    await execute_command(
        command_id=None,
        command_type="invitation.revoke",
        room_id=participant.room_id,
        participant_id=participant.id,
        body=body,
    )


def _validate_redeemable(
    invitation: Invitation, identity: AgentIdentity, user_email: str | None
) -> None:
    """Every reason an invitation can be unusable, checked in one place.

    All failures raise `Forbidden` with the same shape and no detail about *which*
    check failed beyond the message, so probing a token teaches an attacker nothing
    about whether it merely expired or never existed.
    """
    if invitation.revoked_at:
        raise Forbidden("This invitation has been revoked.")
    if is_past(invitation.expires_at):
        raise Forbidden("This invitation has expired.")
    if invitation.is_exhausted:
        raise Forbidden("This invitation has already been used.")
    if (
        invitation.target_kind == InvitationTargetKind.ORG
        and invitation.target_value != identity.org_id
    ):
        raise Forbidden("This invitation was issued to a different organization.")
    if invitation.target_kind == InvitationTargetKind.EMAIL:
        target = (invitation.target_value or "").strip().lower()
        if not user_email or target != user_email.strip().lower():
            raise Forbidden("This invitation was issued to a different address.")


@dataclass
class JoinResult:
    participant: Participant
    room: Room
    #: Returned once. The client presents it on every later call.
    participant_token: str


async def join_room(
    *,
    identity: AgentIdentity,
    command: JoinRoomCommand,
    owner_email: str | None = None,
) -> JoinResult:
    """Redeem an invitation and become (or re-become) a participant.

    `capabilities` here seeds the identity's declared set for display. What actually
    governs behavior is negotiated per *connection* (`core/presence.py`), because the
    same agent may attach from a pushable transport now and a poll-only one later.
    """
    token_hash = hash_token(command.invitation_token)
    row = await db.fetch_one("SELECT * FROM invitations WHERE token_hash = ?", (token_hash,))
    if row is None:
        raise Unauthenticated("Unknown invitation token.")
    invitation = store.to_invitation(row)

    room = await store.load_room(invitation.room_id)
    if room.status != RoomStatus.OPEN:
        raise RoomClosed("This room is closed.", room_id=room.id)
    _validate_redeemable(invitation, identity, owner_email)

    if room.visibility == RoomVisibility.INTERNAL and identity.org_id != room.org_id:
        raise Forbidden(
            "This room is internal to another organization.",
            room_visibility=room.visibility.value,
        )

    # A foreign-org identity in a cross-org room is untrusted until vouched for.
    trust = identity.trust
    if identity.org_id != room.org_id and trust == TrustTier.MEMBER:
        trust = (
            TrustTier.VOUCHED
            if invitation.target_kind == InvitationTargetKind.ORG
            else TrustTier.UNTRUSTED
        )

    existing = await store.find_participant_by_identity(room.id, identity.id)

    # Redeeming an invitation must never *reduce* standing in a room. An owner who
    # clicks the room's own collaborator join link would otherwise demote themselves
    # out of their own room and have their token rotated out from under them — found by
    # a smoke test where the creator re-joined. So a rejoin keeps the higher role, and
    # keeps the existing scopes with it, since those were resolved for that role.
    # A genuine promotion still works: a higher-role invitation upgrades.
    role = invitation.role
    scopes = authz.effective_scopes(invitation.role, invitation.scopes, trust)
    if existing is not None and ROLE_RANK[existing.role] > ROLE_RANK[invitation.role]:
        role = existing.role
        scopes = authz.effective_scopes(existing.role, existing.scopes, trust)

    participant_token = new_token()
    now = utcnow_iso()
    participant_id = existing.id if existing else ids.new_id(ids.PARTICIPANT)
    display_name = (
        command.display_name
        or (existing.identity.display_name if existing else None)
        or identity.display_name
    )
    declared = [c.value for c in (command.capabilities or identity.declared_capabilities)]

    async def body(tx: db.Tx) -> CommandOutcome:
        # Consume a redemption with a guarded update. The CHECK on the table plus
        # this predicate mean two concurrent redemptions of a single-use invitation
        # cannot both succeed on any engine.
        affected = await tx.execute(
            """
            UPDATE invitations SET redemptions = redemptions + 1
            WHERE id = ? AND revoked_at IS NULL AND redemptions < max_redemptions
            """,
            (invitation.id,),
        )
        if affected == 0:
            raise Forbidden("This invitation is no longer usable.")

        if command.capabilities or command.host_class != HostClass.UNKNOWN:
            await tx.execute(
                """
                UPDATE agent_identities
                SET declared_capabilities = ?, host_class = ?, description = ?
                WHERE id = ?
                """,
                (
                    db.dumps(declared),
                    command.host_class.value
                    if command.host_class != HostClass.UNKNOWN
                    else identity.host_class.value,
                    command.description or identity.description,
                    identity.id,
                ),
            )

        if existing:
            await tx.execute(
                """
                UPDATE participants
                SET role = ?, scopes = ?, trust = ?, state = 'joined',
                    display_name = ?, token_hash = ?, joined_at = ?, left_at = NULL
                WHERE id = ?
                """,
                (
                    role.value,
                    db.dumps([s.value for s in scopes]),
                    trust.value,
                    display_name,
                    hash_token(participant_token),
                    now,
                    participant_id,
                ),
            )
        else:
            await _insert_participant_tx(
                tx,
                participant_id=participant_id,
                room_id=room.id,
                identity=identity,
                role=role,
                scopes=scopes,
                trust=trust,
                display_name=display_name,
                token_hash=hash_token(participant_token),
            )

        redeemed = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.INVITATION_REDEEMED,
            actor=EventActor(
                participant_id=participant_id,
                display_name=display_name,
                kind=identity.kind,
                org_id=identity.org_id,
            ),
            payload={"invitation_id": invitation.id, "participant_id": participant_id},
            causation_id=command.command_id,
        )
        joined = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.PARTICIPANT_JOINED,
            actor=EventActor(
                participant_id=participant_id,
                display_name=display_name,
                kind=identity.kind,
                org_id=identity.org_id,
            ),
            payload={
                "participant_id": participant_id,
                "display_name": display_name,
                "org_id": identity.org_id,
                "kind": identity.kind.value,
                "host_class": command.host_class.value,
                "role": role.value,
                "scopes": [s.value for s in scopes],
                "trust": trust.value,
                "declared_capabilities": declared,
                "rejoined": existing is not None,
            },
            causation_id=command.command_id,
        )
        return CommandOutcome(
            result={"participant_id": participant_id, "room_id": room.id},
            events=[redeemed, joined],
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="room.join",
        room_id=room.id,
        participant_id=participant_id,
        body=body,
    )
    participant_id = str(outcome.result.get("participant_id", participant_id))

    if outcome.replayed:
        # Same reasoning as `create_invitation`: the participant token is stored
        # hashed, so a replay cannot hand back the original. Rotate it rather than
        # returning a token that does not work. Membership and events are untouched.
        participant_token = new_token()
        await db.execute(
            "UPDATE participants SET token_hash = ? WHERE id = ?",
            (hash_token(participant_token), participant_id),
        )

    participant = await store.load_participant(participant_id)
    return JoinResult(
        participant=participant,
        room=await store.load_room(room.id),
        participant_token=participant_token,
    )


async def leave_room(*, participant: Participant, command: LeaveRoomCommand) -> None:
    """Graceful departure.

    Releasing claims and ending work declarations here is what makes a clean
    disconnect instantaneous rather than lease-expiry-latency slow.
    """
    from . import presence, tasks, work

    authz.require_active(participant)

    async def body(tx: db.Tx) -> CommandOutcome:
        events = []
        events += await tasks.release_all_claims_tx(
            tx, participant=participant, reason="participant_left"
        )
        events += await work.end_all_open_tx(tx, participant=participant, reason="participant_left")
        events += await presence.close_all_connections_tx(tx, participant=participant)

        await tx.execute(
            "UPDATE participants SET state = 'left', left_at = ?, token_hash = NULL WHERE id = ?",
            (utcnow_iso(), participant.id),
        )
        events.append(
            await eventlog.append(
                tx,
                room_id=participant.room_id,
                type_=EventType.PARTICIPANT_LEFT,
                actor=actor_for(participant),
                payload={
                    "participant_id": participant.id,
                    "reason": LeaveReason.GRACEFUL.value,
                    "note": command.note,
                },
                causation_id=command.command_id,
            )
        )
        return CommandOutcome(result={"participant_id": participant.id}, events=events)

    await execute_command(
        command_id=command.command_id,
        command_type="room.leave",
        room_id=participant.room_id,
        participant_id=participant.id,
        body=body,
    )


# ---------------------------------------------------------------------------
# Dev identity bootstrap (M1 only; real auth is M5)
# ---------------------------------------------------------------------------


async def ensure_org_and_user(
    *, org_name: str, org_slug: str, email: str, display_name: str
) -> tuple[str, str]:
    """Idempotently create an org + owner user. Returns `(org_id, user_id)`."""
    now = utcnow_iso()
    row = await db.fetch_one("SELECT id FROM organizations WHERE slug = ?", (org_slug,))
    org_id = row["id"] if row else ids.new_id(ids.ORG)
    if row is None:
        await db.execute(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES (?,?,?,?)",
            (org_id, org_name, org_slug, now),
        )
    row = await db.fetch_one("SELECT id FROM users WHERE email = ?", (email,))
    user_id = row["id"] if row else ids.new_id(ids.USER)
    if row is None:
        await db.execute(
            "INSERT INTO users (id, org_id, email, display_name, role, created_at) "
            "VALUES (?,?,?,?,'owner',?)",
            (user_id, org_id, email, display_name, now),
        )
    return org_id, user_id


async def create_identity(
    *,
    org_id: str,
    owner_user_id: str,
    display_name: str,
    kind: PrincipalKind = PrincipalKind.AGENT,
    host_class: HostClass = HostClass.UNKNOWN,
    description: str = "",
    capabilities: list[Capability] | None = None,
    trust: TrustTier = TrustTier.MEMBER,
) -> AgentIdentity:
    identity_id = ids.new_id(ids.IDENTITY)
    await db.execute(
        """
        INSERT INTO agent_identities (
            id, org_id, owner_user_id, display_name, kind, host_class,
            description, declared_capabilities, trust, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            identity_id,
            org_id,
            owner_user_id,
            display_name,
            kind.value,
            host_class.value,
            description,
            db.dumps([c.value for c in (capabilities or [])]),
            trust.value,
            utcnow_iso(),
        ),
    )
    row = await db.fetch_one("SELECT * FROM agent_identities WHERE id = ?", (identity_id,))
    return store.to_identity(row)


async def issue_principal_token(
    *, subject_kind: str, subject_id: str, org_id: str, label: str = ""
) -> str:
    token = new_token()
    await db.execute(
        """
        INSERT INTO principal_tokens
            (token_hash, subject_kind, subject_id, org_id, label, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (hash_token(token), subject_kind, subject_id, org_id, label, utcnow_iso()),
    )
    return token


async def set_principal_token(
    *, token: str, subject_kind: str, subject_id: str, org_id: str, label: str = ""
) -> None:
    """Install a specific token value. Dev bootstrap only, so a fixed token from
    config can be used without copying a generated one out of the logs."""
    await db.execute(
        """
        INSERT OR REPLACE INTO principal_tokens
            (token_hash, subject_kind, subject_id, org_id, label, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (hash_token(token), subject_kind, subject_id, org_id, label, utcnow_iso()),
    )


async def provisioning_context_for_invitation(token: str) -> tuple[str, str]:
    """Resolve `(org_id, owner_user_id)` for provisioning an identity from an invitation.

    Used by adapters that have an invitation token but no pre-provisioned agent
    identity — a local agent connecting over MCP for the first time. The invitation is
    the authorization, so the identity is created in the inviting room's org and owned
    by the room's creator.

    This exists only because M1 has no agent-identity credential; M5 replaces it with
    real provisioning, and `core` is unaffected because it only ever sees an
    `AgentIdentity`. Deliberately reveals nothing about the room beyond what the token
    holder is already entitled to.
    """
    row = await db.fetch_one(
        """
        SELECT r.org_id AS org_id, r.created_by_user_id AS user_id
        FROM invitations i JOIN rooms r ON r.id = i.room_id
        WHERE i.token_hash = ?
        """,
        (hash_token(token),),
    )
    if row is None:
        raise Unauthenticated("Unknown invitation token.")
    return row["org_id"], row["user_id"]


@dataclass
class Principal:
    """An authenticated caller, before any room is involved."""

    kind: str  # user | agent_identity
    org_id: str
    user: User | None = None
    identity: AgentIdentity | None = None


async def authenticate_principal(token: str) -> Principal:
    row = await db.fetch_one(
        "SELECT * FROM principal_tokens WHERE token_hash = ?", (hash_token(token),)
    )
    if row is None or row["revoked_at"] or is_past(row["expires_at"]):
        raise Unauthenticated("Unknown or revoked token.")

    if row["subject_kind"] == "user":
        user_row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (row["subject_id"],))
        if user_row is None:
            raise Unauthenticated("Token subject no longer exists.")
        return Principal(kind="user", org_id=row["org_id"], user=store.to_user(user_row))

    identity_row = await db.fetch_one(
        "SELECT * FROM agent_identities WHERE id = ?", (row["subject_id"],)
    )
    if identity_row is None:
        raise Unauthenticated("Token subject no longer exists.")
    return Principal(
        kind="agent_identity", org_id=row["org_id"], identity=store.to_identity(identity_row)
    )


async def list_rooms_for_org(org_id: str, limit: int = 50) -> list[Room]:
    """Rooms owned by one org. Scoped by construction: there is no unscoped list."""
    rows = await db.fetch_all(
        "SELECT * FROM rooms WHERE org_id = ? AND status != 'purged' "
        "ORDER BY created_at DESC LIMIT ?",
        (org_id, limit),
    )
    return [store.to_room(r) for r in rows]


async def room_summary(room: Room) -> dict[str, Any]:
    participants = await store.list_participants(room.id)
    return {
        "room": room.model_dump(mode="json"),
        "participant_count": sum(1 for p in participants if p.is_active),
    }


__all__ = [
    "SYSTEM_ACTOR",
    "IssuedInvitation",
    "JoinResult",
    "Principal",
    "actor_for",
    "authenticate_principal",
    "close_room",
    "create_identity",
    "create_invitation",
    "create_room",
    "ensure_org_and_user",
    "expire_due_rooms",
    "issue_principal_token",
    "join_room",
    "leave_room",
    "list_rooms_for_org",
    "revoke_invitation",
    "room_summary",
    "set_principal_token",
]

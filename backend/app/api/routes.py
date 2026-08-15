"""ARP over HTTP: commands as POSTs, events as a resumable SSE stream.

The native transport. A browser and any first-class client use this; the MCP and A2A
adapters call the same `core` services, so no behavior is defined here — only
translation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..config import settings
from ..core import (
    authz,
    eventlog,
    messages,
    presence,
    privacy,
    projections,
    rooms,
    store,
    tasks,
    work,
)
from ..core.bus import bus
from ..core.errors import Forbidden, ResumeGap
from ..domain.capabilities import SUGGESTED_CAPABILITIES, Capability
from ..domain.commands import (
    CancelTaskCommand,
    ClaimTaskCommand,
    CompleteTaskCommand,
    ConnectCommand,
    CreateInvitationCommand,
    CreateRoomCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    EndWorkCommand,
    HeartbeatCommand,
    JoinRoomCommand,
    LeaveRoomCommand,
    PostMessageCommand,
    ReleaseClaimCommand,
    RenewClaimCommand,
    TakeOverExecutionCommand,
    UpdateTaskCommand,
    UpdateWorkCommand,
)
from ..domain.events import ControlFrame
from ..domain.room import Scope
from .auth import (
    JoinCredentialDep,
    ParticipantDep,
    PrincipalDep,
    StreamParticipantDep,
    require_user,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict[str, Any]:
    """Minimal liveness for anything already talking to the ARP surface.

    `/healthz` is the deployment-facing one: same liveness, plus the URLs this instance is
    advertising, which is where a wrong `PUBLIC_BASE_URL` becomes visible. Kept separate
    because a probe on the API surface should not depend on server composition.
    """
    return {"status": "ok", "protocol": "arp/1"}


@router.get("/capabilities")
async def describe_capabilities() -> dict[str, Any]:
    """What a client may declare, and what each transport can honor.

    Published so a client can negotiate honestly instead of guessing — and so the
    fact that capabilities, not provider labels, drive behavior is discoverable from
    the API itself.
    """
    return {
        "protocol": "arp/1",
        "capabilities": [c.value for c in Capability],
        "transports": {
            name: sorted(c.value for c in caps)
            for name, caps in presence.TRANSPORT_CAPABILITIES.items()
        },
        "host_class_defaults": {
            hc.value: sorted(c.value for c in caps) for hc, caps in SUGGESTED_CAPABILITIES.items()
        },
        "note": (
            "host_class is a descriptive label supplying defaults only. Declared "
            "capabilities determine delivery mode, lease eligibility, and lease length."
        ),
        "mcp_url": f"{settings.public_base_url.rstrip('/')}/mcp",
    }


# ---------------------------------------------------------------------------
# Rooms (user principal)
# ---------------------------------------------------------------------------


@router.post("/rooms", status_code=201)
async def create_room(principal: PrincipalDep, command: CreateRoomCommand) -> dict[str, Any]:
    """Create a room, join as owner, and get a shareable join token — one call.

    `participant_token` is the creator's; `join_token` is the single thing to hand to
    everyone else. Both are returned exactly once and stored only as hashes.
    """
    user = require_user(principal)
    created = await rooms.create_room(user=user, command=command)
    return {
        "ok": True,
        "room": created.room.model_dump(mode="json"),
        "participant": created.participant.model_dump(mode="json"),
        "participant_token": created.participant_token,
        "join_token": created.join_token,
        "mcp_url": f"{settings.public_base_url.rstrip('/')}/mcp",
        "next_step": (
            "Share join_token. An agent joins with the MCP tool "
            "join_room(invitation_token=...); a human joins at POST /api/rooms/join."
        ),
    }


@router.get("/rooms")
async def list_rooms(
    principal: PrincipalDep, limit: int = Query(default=50, ge=1, le=100)
) -> dict[str, Any]:
    """Scoped to the principal's org. There is no unscoped content list."""
    rows = await rooms.list_rooms_for_org(principal.org_id, limit)
    return {"ok": True, "rooms": [r.model_dump(mode="json") for r in rows]}


@router.post("/rooms/{room_id}/invitations", status_code=201)
async def create_invitation(
    room_id: str, participant: ParticipantDep, command: CreateInvitationCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    issued = await rooms.create_invitation(participant=participant, command=command)
    return {
        "ok": True,
        "invitation": issued.invitation.model_dump(mode="json"),
        # Returned exactly once. Only the hash is stored.
        "token": issued.token,
    }


@router.post("/rooms/join", status_code=201)
async def join_room(credential: JoinCredentialDep, command: JoinRoomCommand) -> dict[str, Any]:
    """Redeem an invitation.

    Three kinds of caller take the same path, because membership has exactly one entry
    point: an agent identity, a human with an account, and — since D-025 — **a stranger
    holding nothing but the invitation itself.** That last case is the product's central
    claim, and until the invitation became a credential there was no way to express it.
    """
    if isinstance(credential, rooms.InvitationCredential):
        identity = await _identity_for_invitation(credential, command)
        owner_email = None
    elif credential.identity is not None:
        identity = credential.identity
        owner_email = None
    else:
        user = require_user(credential)
        identity = await _identity_for_user(user, command)
        owner_email = user.email

    result = await rooms.join_room(identity=identity, command=command, owner_email=owner_email)
    return {
        "ok": True,
        "participant": result.participant.model_dump(mode="json"),
        "room": result.room.model_dump(mode="json"),
        "participant_token": result.participant_token,
    }


async def _identity_for_user(user, command: JoinRoomCommand):
    """A human joining gets (or reuses) the `human` identity in their org."""
    return await rooms.ensure_human_identity(user, display_name=command.display_name)


async def _identity_for_invitation(
    credential: rooms.InvitationCredential, command: JoinRoomCommand
):
    """A guest joining on the strength of the invitation alone.

    The body's `invitation_token` must be the same invitation that authenticated the
    request. They are almost always identical — a client sends one token twice — but a
    mismatch is the confused-deputy shape worth refusing outright: a credential for one
    room must never be the thing that authorizes entry into another.
    """
    presented = await rooms.authenticate_invitation(command.invitation_token)
    if presented.invitation.id != credential.invitation.id:
        raise Forbidden(
            "The invitation you authenticated with is not the one you are redeeming.",
            authenticated_room_id=credential.room_id,
        )
    return await rooms.provision_guest_identity(
        credential,
        # A guest with no account has no name to fall back on, so an omitted one is
        # labelled rather than left blank — the room shows it as self-asserted anyway.
        display_name=command.display_name or "Guest",
        host_class=command.host_class,
        description=command.description,
        capabilities=command.capabilities,
    )


@router.post("/rooms/{room_id}/leave")
async def leave_room(
    room_id: str, participant: ParticipantDep, command: LeaveRoomCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    await rooms.leave_room(participant=participant, command=command)
    return {"ok": True}


@router.post("/rooms/{room_id}/close")
async def close_room(room_id: str, participant: ParticipantDep) -> dict[str, Any]:
    _assert_room(participant, room_id)
    room = await rooms.close_room(participant=participant)
    return {"ok": True, "room": room.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


@router.post("/rooms/{room_id}/connect", status_code=201)
async def connect(
    room_id: str, participant: ParticipantDep, command: ConnectCommand
) -> dict[str, Any]:
    """Negotiate capabilities and open a connection.

    The response tells the client exactly what it got — including whether it may
    claim work and why not, if not — so it never coordinates against an assumption we
    did not agree to.
    """
    _assert_room(participant, room_id)
    negotiated = await presence.connect(participant=participant, command=command, transport="sse")
    return {
        "ok": True,
        "connection_id": negotiated.connection.id,
        # The durable runtime this transport landed on, or null if none was declared.
        # A client cannot otherwise learn whether its label was recognised as the same
        # runtime it used last time, which is the one thing the label is for.
        "attachment_id": negotiated.connection.attachment_id,
        "negotiated": [c.value for c in negotiated.connection.negotiated_capabilities],
        "delivery_mode": negotiated.runtime.delivery_mode.value,
        "heartbeat_interval_s": negotiated.runtime.heartbeat_interval_s,
        "may_claim": negotiated.runtime.may_claim,
        "claim_denied_reason": negotiated.runtime.claim_denied_reason,
        "max_lease_seconds": negotiated.runtime.max_lease_seconds,
        "lease_renewable_unattended": negotiated.runtime.lease_renewable_unattended,
        "current_seq": negotiated.current_seq,
        "since_seq": negotiated.since_seq,
    }


@router.post("/rooms/{room_id}/heartbeat")
async def heartbeat(
    room_id: str, participant: ParticipantDep, command: HeartbeatCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    await presence.heartbeat(connection_id=command.connection_id, participant=participant)
    return {"ok": True}


@router.post("/rooms/{room_id}/disconnect")
async def disconnect(
    room_id: str, participant: ParticipantDep, connection_id: str = Query(...)
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    await presence.disconnect(connection_id=connection_id, participant=participant)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("/rooms/{room_id}/snapshot")
async def get_snapshot(room_id: str, participant: ParticipantDep) -> dict[str, Any]:
    _assert_room(participant, room_id)
    authz.require_scope(participant, Scope.ROOM_READ)
    return await projections.snapshot(room_id=room_id, recipient=participant)


@router.get("/rooms/{room_id}/hydrate")
async def get_hydration(
    room_id: str,
    participant: ParticipantDep,
    since_seq: int | None = Query(default=None, ge=0),
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    authz.require_scope(participant, Scope.ROOM_READ)
    return await projections.hydrate(room_id=room_id, recipient=participant, since_seq=since_seq)


@router.get("/rooms/{room_id}/events")
async def get_events(
    room_id: str,
    participant: ParticipantDep,
    since_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    """Pull replay. Same semantics as the stream; only the delivery differs."""
    _assert_room(participant, room_id)
    authz.require_scope(participant, Scope.EVENTS_SUBSCRIBE)
    current = await eventlog.validate_cursor(room_id, since_seq)
    events = await projections.visible_events_since(
        room_id=room_id, recipient=participant, since_seq=since_seq, limit=limit
    )
    return {"ok": True, "events": events, "cursor": current, "current_seq": current}


# ---------------------------------------------------------------------------
# Live stream
# ---------------------------------------------------------------------------


def _sse(event_type: str, data: dict[str, Any], *, seq: int | None = None) -> str:
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/rooms/{room_id}/stream")
async def stream(
    room_id: str,
    request: Request,
    participant: StreamParticipantDep,
    since_seq: int = Query(default=0, ge=0),
    connection_id: str | None = Query(default=None),
) -> StreamingResponse:
    """Resumable SSE.

    `since_seq=0` opens with a snapshot frame whose `snapshot_seq` is read in the
    same transaction as its content, then streams events strictly greater than that
    seq. That boundary is why a reconnect can neither miss nor duplicate an event
    (`docs/PROTOCOL.md` §5).

    A resume cursor below the retained floor yields a `resume_gap` frame rather than
    a silent partial replay, because a client that cannot tell it lost history would
    coordinate on stale state.
    """
    _assert_room(participant, room_id)
    authz.require_scope(participant, Scope.EVENTS_SUBSCRIBE)

    async def source():
        cursor = since_seq
        try:
            current = await eventlog.validate_cursor(room_id, cursor)
        except ResumeGap as exc:
            yield _sse(ControlFrame.RESUME_GAP.value, exc.to_payload())
            cursor = 0
            current = await eventlog.current_seq(room_id)

        if cursor == 0:
            frame = await projections.snapshot(room_id=room_id, recipient=participant)
            cursor = int(frame["snapshot_seq"])
            yield _sse(ControlFrame.SNAPSHOT.value, frame, seq=cursor)

        # Seed the bus so a waiter does not block on a room this process has not
        # published for yet.
        bus.prime(room_id, current)
        room = await store.load_room(room_id)

        while True:
            if await request.is_disconnected():
                break

            # Read the raw page first, then filter. The cursor advances to the
            # highest seq actually *read*, not to the room's current seq — reading
            # the room's seq after the page would skip anything that landed in
            # between, which is precisely the silent-miss failure the protocol
            # forbids. Events filtered out for this recipient still advance the
            # cursor, or a private event would be re-read forever.
            batch = await eventlog.read_since(room_id, cursor)
            if batch:
                for event in privacy.filter_events(batch, recipient=participant, room=room):
                    yield _sse(event.type.value, event.model_dump(mode="json"), seq=event.seq)
                cursor = batch[-1].seq
                # A full page means more is waiting; drain before blocking.
                if len(batch) >= eventlog.MAX_REPLAY_BATCH:
                    continue

            if connection_id:
                await presence.heartbeat(
                    connection_id=connection_id, participant=participant, seq=cursor
                )

            reached = await bus.wait_for(
                room_id, cursor, timeout=float(settings.sse_keepalive_seconds)
            )
            if reached <= cursor:
                yield ": keepalive\n\n"

    return StreamingResponse(
        source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Coordination commands
# ---------------------------------------------------------------------------


@router.post("/rooms/{room_id}/messages", status_code=201)
async def post_message(
    room_id: str, participant: ParticipantDep, command: PostMessageCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    return {"ok": True, **await messages.post(participant=participant, command=command)}


@router.post("/rooms/{room_id}/work", status_code=201)
async def declare_work(
    room_id: str, participant: ParticipantDep, command: DeclareWorkCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    declaration = await work.declare(participant=participant, command=command)
    return {"ok": True, "work": declaration.model_dump(mode="json")}


@router.patch("/rooms/{room_id}/work")
async def update_work(
    room_id: str, participant: ParticipantDep, command: UpdateWorkCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    declaration = await work.update(participant=participant, command=command)
    return {"ok": True, "work": declaration.model_dump(mode="json")}


@router.post("/rooms/{room_id}/work/end")
async def end_work(
    room_id: str, participant: ParticipantDep, command: EndWorkCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    declaration = await work.end(participant=participant, command=command)
    return {"ok": True, "work": declaration.model_dump(mode="json")}


@router.post("/rooms/{room_id}/tasks", status_code=201)
async def create_task(
    room_id: str, participant: ParticipantDep, command: CreateTaskCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    task = await tasks.create(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.patch("/rooms/{room_id}/tasks")
async def update_task(
    room_id: str, participant: ParticipantDep, command: UpdateTaskCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    task = await tasks.update(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.post("/rooms/{room_id}/tasks/claim")
async def claim_task(
    room_id: str, participant: ParticipantDep, command: ClaimTaskCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    task = await tasks.claim(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.post("/rooms/{room_id}/tasks/renew")
async def renew_claim(
    room_id: str, participant: ParticipantDep, command: RenewClaimCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    task = await tasks.renew(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.post("/rooms/{room_id}/tasks/release")
async def release_claim(
    room_id: str, participant: ParticipantDep, command: ReleaseClaimCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    task = await tasks.release(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.post("/rooms/{room_id}/tasks/take-over")
async def take_over_execution(
    room_id: str, participant: ParticipantDep, command: TakeOverExecutionCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    task = await tasks.take_over_execution(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.post("/rooms/{room_id}/tasks/complete")
async def complete_task(
    room_id: str, participant: ParticipantDep, command: CompleteTaskCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    task = await tasks.complete(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.post("/rooms/{room_id}/tasks/cancel")
async def cancel_task(
    room_id: str, participant: ParticipantDep, command: CancelTaskCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    task = await tasks.cancel(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


def _assert_room(participant, room_id: str) -> None:
    """A participant token is scoped to one room; using it elsewhere is a 403.

    Returning `Forbidden` rather than `NotFound` is safe here because the caller
    already proved membership of *some* room — there is no existence to leak.
    """
    if participant.room_id != room_id:
        raise Forbidden(
            "This participant token belongs to a different room.",
            token_room_id=participant.room_id,
        )

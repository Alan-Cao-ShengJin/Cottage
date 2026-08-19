"""ARP over HTTP: commands as POSTs, events as a resumable SSE stream.

The native transport. A browser and any first-class client use this; the MCP and A2A
adapters call the same `core` services, so no behavior is defined here â€” only
translation.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..config import settings
from ..core import (
    activity,
    authz,
    checkpoints,
    directives,
    eventlog,
    goals,
    jobs,
    messages,
    presence,
    privacy,
    projections,
    questions,
    roles,
    rooms,
    runtime_state,
    store,
    stream_tickets,
    tasks,
    work,
    workers,
)
from ..core.bus import bus
from ..core.errors import Forbidden, InvalidCommand, ResumeGap, RoomClosed, RoomError
from ..domain import relevance
from ..domain.capabilities import SUGGESTED_CAPABILITIES, Capability
from ..domain.commands import (
    AcceptJobCommand,
    AcknowledgeDirectiveCommand,
    AcknowledgeGoalCommand,
    AnswerQuestionCommand,
    AppendCheckpointCommand,
    AskQuestionCommand,
    AssignJobCommand,
    AssignRoomRoleCommand,
    CancelTaskCommand,
    ClaimTaskCommand,
    CloseGoalCommand,
    CloseJobCommand,
    CompleteTaskCommand,
    ConnectCommand,
    CreateInvitationCommand,
    CreateRoomCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    DrainRuntimeCommand,
    EndWorkCommand,
    ExtendRoomCommand,
    FinishWorkerCommand,
    HeartbeatCommand,
    IssueDirectiveCommand,
    JoinRoomCommand,
    LeaveRoomCommand,
    MintCredentialCommand,
    NoteActivityCommand,
    PostJobCommand,
    PostMessageCommand,
    RegisterWorkerCommand,
    ReleaseClaimCommand,
    RenewClaimCommand,
    ReplaceGoalCommand,
    ReportCapacityCommand,
    ResumeRuntimeCommand,
    RevokeCredentialCommand,
    SetJobStateCommand,
    SetParticipantRoleCommand,
    SetRuntimeStateCommand,
    TakeOverExecutionCommand,
    UpdateJobCommand,
    UpdateRoomCharterCommand,
    UpdateTaskCommand,
    UpdateWorkCommand,
    UpdateWorkerCommand,
)
from ..domain.events import ControlFrame
from ..domain.job import JobState
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

    Published so a client can negotiate honestly instead of guessing â€” and so the
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
    """Create a room, join as owner, and get a shareable join token â€” one call.

    `participant_token` is the creator's; `join_token` is the single thing to hand to
    everyone else. Both are returned exactly once and stored only as hashes.
    """
    # No `require_user` here any more: an agent identity is an org-level principal
    # too, and gating this on a human credential made step one of the core loop
    # impossible for the hosts we have actually verified (D-046). The provenance
    # check that does the real work lives in the service, with the rest of the
    # tenancy rules.
    created = await rooms.create_room(principal=principal, command=command)
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
    point: an agent identity, a human with an account, and â€” since D-025 â€” **a stranger
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
    request. They are almost always identical â€” a client sends one token twice â€” but a
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
        # labelled rather than left blank â€” the room shows it as self-asserted anyway.
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


@router.post("/rooms/{room_id}/extend")
async def extend_room(
    room_id: str, participant: ParticipantDep, command: ExtendRoomCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    room = await rooms.extend_room(participant=participant, command=command)
    return {"ok": True, "room": room.model_dump(mode="json")}


@router.put("/rooms/{room_id}/charter")
async def update_room_charter(
    room_id: str, participant: ParticipantDep, command: UpdateRoomCharterCommand
) -> dict[str, Any]:
    """Replace the room-public cold-start charter. Requires `room.admin`."""
    _assert_room(participant, room_id)
    room = await rooms.update_room_charter(participant=participant, command=command)
    return {"ok": True, "room": room.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


@router.post("/rooms/{room_id}/runtimes/drain")
async def drain_runtime(
    room_id: str, participant: ParticipantDep, command: DrainRuntimeCommand
) -> dict[str, Any]:
    """Refuse further work from one of your own runtimes (D-062).

    This does not stop the process â€” nothing here can, and on the hosted path the
    process is on someone else's machine. It withdraws the runtime's permission, which
    is the guarantee the room can actually keep. Sticky: reconnecting does not undo it.
    """
    _assert_room(participant, room_id)
    room = await rooms.store.load_room(room_id)
    outcome = await presence.drain_runtime(
        room=room,
        participant=participant,
        attachment_id=command.attachment_id,
        reason=command.reason,
        command_id=command.command_id,
    )
    return outcome.result


@router.post("/rooms/{room_id}/runtimes/resume")
async def resume_runtime(
    room_id: str, participant: ParticipantDep, command: ResumeRuntimeCommand
) -> dict[str, Any]:
    """Undo a drain, asserting that the old process is genuinely gone."""
    _assert_room(participant, room_id)
    room = await rooms.store.load_room(room_id)
    outcome = await presence.resume_runtime(
        room=room,
        participant=participant,
        attachment_id=command.attachment_id,
        note=command.note,
        command_id=command.command_id,
    )
    return outcome.result


@router.post("/rooms/{room_id}/connect", status_code=201)
async def connect(
    room_id: str, participant: ParticipantDep, command: ConnectCommand
) -> dict[str, Any]:
    """Negotiate capabilities and open a connection.

    The response tells the client exactly what it got â€” including whether it may
    claim work and why not, if not â€” so it never coordinates against an assumption we
    did not agree to.
    """
    _assert_room(participant, room_id)
    # The client's own declaration, defaulting to SSE for callers that predate the
    # field. This route cannot observe which transport you will use, so asking is
    # more honest than assuming (D-047).
    negotiated = await presence.connect(
        participant=participant,
        command=command,
        transport=command.transport or "sse",
    )
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
    wait_seconds: float = Query(default=0, ge=0, le=settings.max_long_poll_seconds),
) -> dict[str, Any]:
    """Pull or long-poll replay with a cursor for the page actually consumed.

    The cursor advances over privacy-filtered events too, but never beyond the raw
    page read. Advancing straight to the room high-water mark would silently skip a
    second page or an event committed between the read and response.
    """
    _assert_room(participant, room_id)
    authz.require_scope(participant, Scope.EVENTS_SUBSCRIBE)
    current = await eventlog.validate_cursor(room_id, since_seq)
    bus.prime(room_id, current)
    batch = await eventlog.read_since(room_id, since_seq, limit=limit)
    if not batch and wait_seconds > 0:
        await bus.wait_for(room_id, since_seq, timeout=wait_seconds)
        batch = await eventlog.read_since(room_id, since_seq, limit=limit)
    room = await store.load_room(room_id)
    # Each event carries the class `domain.relevance` gives it *for this reader*. The
    # WebSocket wake channel uses the same judgement to decide what to deliver; here nothing
    # is withheld, the class is simply stated, so a polling client stops re-deriving it.
    #
    # That mattered concretely: the companion had grown its own three-tier table, and two
    # classifiers with different vocabularies is worse than either (D-089). It is computed
    # per recipient because the interesting rules — a message read back to its author, an
    # allocation naming this seat — are about who is reading.
    events = []
    for event in privacy.filter_events(batch, recipient=participant, room=room):
        body = event.model_dump(mode="json")
        body["relevance"] = relevance.classify(
            event_type=event.type,
            payload=event.payload,
            actor_participant_id=event.actor.participant_id,
            viewer_participant_id=participant.id,
            actor_kind=event.actor.kind,
            viewer_kind=participant.identity.kind,
        ).value
        events.append(body)
    cursor = batch[-1].seq if batch else since_seq
    result: dict[str, Any] = {
        "ok": True,
        "events": events,
        "cursor": cursor,
        "current_seq": await eventlog.current_seq(room_id),
    }
    return result


@router.post("/rooms/{room_id}/stream-ticket")
async def create_stream_ticket(room_id: str, participant: ParticipantDep) -> dict[str, Any]:
    """Exchange the durable participant credential for a one-use realtime ticket."""
    _assert_room(participant, room_id)
    authz.require_scope(participant, Scope.EVENTS_SUBSCRIBE)
    issued = await stream_tickets.issue(participant)
    return {"ok": True, "ticket": issued.token, "expires_at": issued.expires_at}


# ---------------------------------------------------------------------------
# Live stream
# ---------------------------------------------------------------------------


@router.websocket("/rooms/{room_id}/ws")
async def websocket_stream(websocket: WebSocket, room_id: str) -> None:
    """Resumable WebSocket delivery backed by the durable room event log.

    Two subscriber shapes over one socket, chosen by `classes`:

    * `classes=all` (default) — every visible frame, plus the opening snapshot and
      periodic keepalives. This is the browser: it is building a live view of the
      board, and rendering a frame costs it nothing.
    * `classes=judgement` — only events `domain.relevance` classes as needing a
      decision, no snapshot, no keepalives. This is a **wake channel** for a
      model-backed agent, where every delivered frame bills someone. Silence is the
      correct output when nothing needs deciding, and a host that turns frames into
      model turns will rate-limit or drop a firehose — so a keepalive every few
      seconds would defeat the entire subscription.

    The filtered mode deliberately does not honor the snapshot half of the resume
    contract (`docs/PROTOCOL.md` §5). A wake subscriber keeps no mirror to reconcile;
    it is told *that* something happened and reads the room over its own transport.
    The cursor still advances across suppressed events, so nothing is replayed twice
    and the position stays honest.
    """
    try:
        participant = await stream_tickets.consume(
            websocket.query_params.get("ticket"), room_id=room_id
        )
        authz.require_scope(participant, Scope.EVENTS_SUBSCRIBE)
        since_seq = int(websocket.query_params.get("since_seq", "0"))
        if since_seq < 0:
            raise ValueError
        classes = websocket.query_params.get("classes", "all")
        if classes not in ("all", "judgement"):
            raise ValueError
    except (RoomError, ValueError):
        await websocket.close(code=4401)
        return

    wake_only = classes == "judgement"
    connection_id = websocket.query_params.get("connection_id")
    await websocket.accept()
    cursor = since_seq
    try:
        try:
            current = await eventlog.validate_cursor(room_id, cursor)
        except ResumeGap as exc:
            # Still reported when filtering: losing history is itself a thing the
            # reader must decide about, and it is the one case where a wake
            # subscriber has to go and re-read the room.
            await websocket.send_json(
                {"frame": ControlFrame.RESUME_GAP.value, "data": exc.to_payload()}
            )
            cursor = 0
            current = await eventlog.current_seq(room_id)

        if cursor == 0:
            if wake_only:
                cursor = current
                await websocket.send_json(
                    {"frame": ControlFrame.READY.value, "data": {"cursor": cursor}}
                )
            else:
                frame = await projections.snapshot(room_id=room_id, recipient=participant)
                cursor = int(frame["snapshot_seq"])
                await websocket.send_json({"frame": ControlFrame.SNAPSHOT.value, "data": frame})

        bus.prime(room_id, current)
        room = await store.load_room(room_id)
        while True:
            batch = await eventlog.read_since(room_id, cursor)
            if batch:
                for event in privacy.filter_events(batch, recipient=participant, room=room):
                    if wake_only and not relevance.wakes(
                        event_type=event.type,
                        payload=event.payload,
                        actor_participant_id=event.actor.participant_id,
                        viewer_participant_id=participant.id,
                        actor_kind=event.actor.kind,
                        viewer_kind=participant.identity.kind,
                    ):
                        continue
                    await websocket.send_json(
                        {"frame": "event", "event": event.model_dump(mode="json")}
                    )
                # Advances past suppressed events too. A filtered subscriber that
                # rewound to the last frame it was *sent* would re-read every
                # judgement event behind a quiet stretch on the next reconnect.
                cursor = batch[-1].seq
                if len(batch) >= eventlog.MAX_REPLAY_BATCH:
                    continue

            if connection_id:
                with contextlib.suppress(RoomClosed):
                    await presence.heartbeat(
                        connection_id=connection_id, participant=participant, seq=cursor
                    )
            reached = await bus.wait_for(
                room_id, cursor, timeout=float(settings.sse_keepalive_seconds)
            )
            if reached <= cursor and not wake_only:
                await websocket.send_json({"frame": ControlFrame.KEEPALIVE.value})
    except (WebSocketDisconnect, RuntimeError):
        return


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
    (`docs/PROTOCOL.md` Â§5).

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
            # highest seq actually *read*, not to the room's current seq â€” reading
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
                # Closed-room history remains readable. The implicit beat is a
                # best-effort liveness write, not a condition of replay access.
                with contextlib.suppress(RoomClosed):
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


@router.post("/rooms/{room_id}/activity", status_code=201)
async def note_activity(
    room_id: str, participant: ParticipantDep, command: NoteActivityCommand
) -> dict[str, Any]:
    """One breadcrumb of live narration (D-082). Changes no coordination state."""
    _assert_room(participant, room_id)
    return await activity.note(participant=participant, command=command)


@router.put("/rooms/{room_id}/runtime-state")
async def set_runtime_state(
    room_id: str, participant: ParticipantDep, command: SetRuntimeStateCommand
) -> dict[str, Any]:
    """Project the caller attachment's validated work posture.

    This does not set presence. The attachment is derived from ``connection_id`` and
    its liveness continues to come only from heartbeats.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await runtime_state.set_state(participant=participant, command=command)}


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


@router.post("/rooms/{room_id}/tasks/update")
@router.patch("/rooms/{room_id}/tasks")
async def update_task(
    room_id: str, participant: ParticipantDep, command: UpdateTaskCommand
) -> dict[str, Any]:
    """Revise a task, or move it to `in_progress`.

    Two paths for one operation, and the second is the one that matters. Every
    sibling â€” claim, renew, release, complete, cancel, take-over, steer â€” is
    `POST /tasks/<verb>`, and update alone was `PATCH /tasks`. A worker following
    the pattern its neighbours set got `405 Method Not Allowed` while trying to
    say it had started, so the board could not distinguish *held* from *being
    worked* for any client that had not read the route table.

    An API whose shape you cannot infer from its own siblings is a defect even
    when every individual route is defensible. `PATCH` stays for callers already
    using it; it is not the form to reach for.
    """
    _assert_room(participant, room_id)
    task = await tasks.update(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.post("/rooms/{room_id}/tasks/claim")
async def claim_task(
    room_id: str, participant: ParticipantDep, command: ClaimTaskCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    await store.load_task_for_room(room_id, command.task_id)
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


@router.post("/rooms/{room_id}/credentials", status_code=201)
async def mint_credential(
    room_id: str, participant: ParticipantDep, command: MintCredentialCommand
) -> dict[str, Any]:
    """Mint a narrow, expiring token for one of your own runtimes.

    Returned once and stored only as a hash. Put it in the worker's environment,
    never in room content â€” the disclosure screen would refuse it there anyway,
    which is the screen doing its job rather than a limitation of it.
    """
    _assert_room(participant, room_id)
    issued = await rooms.mint_runtime_credential(participant=participant, command=command)
    return {
        "ok": True,
        "credential": issued.credential.model_dump(mode="json"),
        "token": issued.token,
        "next_step": (
            "Put this in the worker's COTTAGE_PARTICIPANT_TOKEN environment variable "
            "and launch it from there â€” never as a command-line argument, where every "
            "process listing on that machine can read it. It can take and finish work "
            "assigned to it and nothing else, and revoking it does not touch your seat."
        ),
    }


@router.post("/rooms/{room_id}/credentials/revoke")
async def revoke_credential(
    room_id: str, participant: ParticipantDep, command: RevokeCredentialCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    credential = await rooms.revoke_runtime_credential(participant=participant, command=command)
    return {"ok": True, "credential": credential.model_dump(mode="json")}


@router.get("/rooms/{room_id}/credentials")
async def list_credentials(room_id: str, participant: ParticipantDep) -> dict[str, Any]:
    """Your seat's runtime credentials, without their tokens."""
    _assert_room(participant, room_id)
    creds = await rooms.list_runtime_credentials(participant=participant)
    return {"ok": True, "credentials": [c.model_dump(mode="json") for c in creds]}


@router.post("/rooms/{room_id}/participants/role")
async def set_participant_role(
    room_id: str, participant: ParticipantDep, command: SetParticipantRoleCommand
) -> dict[str, Any]:
    """Promote or demote a participant. Admin only; narrowing rules still apply."""
    _assert_room(participant, room_id)
    updated = await rooms.set_participant_role(participant=participant, command=command)
    return {"ok": True, "participant": updated.model_dump(mode="json")}


@router.post("/rooms/{room_id}/directives", status_code=201)
async def issue_directive(
    room_id: str, participant: ParticipantDep, command: IssueDirectiveCommand
) -> dict[str, Any]:
    """Direct a participant: pause, stop, resume, reprioritise, or supply input.

    Control actions take effect in this request, not when the target notices. The
    response therefore describes what already happened, not what has been asked for.
    """
    _assert_room(participant, room_id)
    directive = await directives.issue(participant=participant, command=command)
    return {"ok": True, "directive": directive.model_dump(mode="json")}


@router.post("/rooms/{room_id}/directives/acknowledge")
async def acknowledge_directive(
    room_id: str, participant: ParticipantDep, command: AcknowledgeDirectiveCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    directive = await directives.acknowledge(participant=participant, command=command)
    return {"ok": True, "directive": directive.model_dump(mode="json")}


@router.get("/rooms/{room_id}/directives")
async def list_open_directives(room_id: str, participant: ParticipantDep) -> dict[str, Any]:
    """What is waiting for *you*. Oldest first: these are instructions, not context."""
    _assert_room(participant, room_id)
    open_ = await directives.open_for(participant.id)
    return {"ok": True, "directives": [d.model_dump(mode="json") for d in open_]}


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


@router.post("/rooms/{room_id}/tasks/checkpoint", status_code=201)
async def append_checkpoint(
    room_id: str, participant: ParticipantDep, command: AppendCheckpointCommand
) -> dict[str, Any]:
    """Record durable progress on work you hold (D-050)."""
    _assert_room(participant, room_id)
    checkpoint = await checkpoints.append(participant=participant, command=command)
    return {"ok": True, "checkpoint": checkpoint.model_dump(mode="json")}


@router.get("/rooms/{room_id}/tasks/{task_id}")
async def get_task(room_id: str, task_id: str, participant: ParticipantDep) -> dict[str, Any]:
    """One task as it stands right now.

    Cheaper than a snapshot and narrower than hydration, for the caller that has one
    question: *is this still mine to work on?* An unattended worker in the middle of
    a long step asks exactly that, and asking it should not cost the whole board.
    """
    _assert_room(participant, room_id)
    authz.require_scope(participant, Scope.TASK_READ)
    task = await store.load_task_for_room(room_id, task_id)
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.get("/rooms/{room_id}/tasks/{task_id}/checkpoints")
async def list_checkpoints(
    room_id: str,
    task_id: str,
    participant: ParticipantDep,
    limit: int = Query(default=checkpoints.DEFAULT_LATEST, ge=1, le=checkpoints.MAX_PAGE),
) -> dict[str, Any]:
    """The latest N, oldest-first, with the total so truncation is visible (D-043)."""
    _assert_room(participant, room_id)
    authz.require_scope(participant, Scope.TASK_READ)
    await store.load_task_for_room(room_id, task_id)
    rows = await checkpoints.latest_for_task(
        task_id, recipient=participant, limit=limit, room_id=room_id
    )
    total = await checkpoints.count_for_task(task_id, room_id=room_id)
    return {
        "ok": True,
        "checkpoints": [c.model_dump(mode="json") for c in rows],
        "total": total,
        "truncated": total > len(rows),
    }


@router.post("/rooms/{room_id}/questions", status_code=201)
async def ask_question(
    room_id: str, participant: ParticipantDep, command: AskQuestionCommand
) -> dict[str, Any]:
    """Ask, optionally standing down from the work until it is answered (D-051)."""
    _assert_room(participant, room_id)
    question = await questions.ask(participant=participant, command=command)
    return {"ok": True, "question": question.model_dump(mode="json")}


@router.post("/rooms/{room_id}/questions/answer", status_code=201)
async def answer_question(
    room_id: str, participant: ParticipantDep, command: AnswerQuestionCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    answer = await questions.answer(participant=participant, command=command)
    return {"ok": True, "answer": answer.model_dump(mode="json")}


@router.get("/rooms/{room_id}/questions")
async def list_open_questions(room_id: str, participant: ParticipantDep) -> dict[str, Any]:
    """Unanswered questions this participant should act on, or is waiting on."""
    _assert_room(participant, room_id)
    authz.require_scope(participant, Scope.ROOM_READ)
    rows = await questions.open_for(participant.id, room_id=room_id)
    return {"ok": True, "questions": [q.model_dump(mode="json") for q in rows]}


@router.post("/rooms/{room_id}/tasks/cancel")
async def cancel_task(
    room_id: str, participant: ParticipantDep, command: CancelTaskCommand
) -> dict[str, Any]:
    _assert_room(participant, room_id)
    task = await tasks.cancel(participant=participant, command=command)
    return {"ok": True, "task": task.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# The coordination hierarchy: room roles, jobs, goals, capacity, workers (D-088/D-089)
# ---------------------------------------------------------------------------
#
# Every one of these takes the command model straight from the body and hands it to the
# same core service the MCP adapter calls. The shapes deliberately mirror their task-board
# siblings — `POST /<collection>` to create, `POST /<collection>/<verb>` to act,
# `GET /<collection>` to read — because routes.py has already paid for the alternative
# once: `update_task` was PATCH-only, a worker followed the pattern its neighbours set and
# got a 405 while trying to say it had started, and the conclusion recorded above is that
# an API whose shape you cannot infer from its own siblings is a defect.


@router.post("/rooms/{room_id}/participants/room-role")
async def assign_room_role(
    room_id: str, participant: ParticipantDep, command: AssignRoomRoleCommand
) -> dict[str, Any]:
    """Place a seat in the coordination hierarchy, or stand it down.

    Deliberately a different route from `/participants/role`, which rewrites a scope list.
    Position and authority are separate axes (ADR-013) and merging them is how a
    coordination label starts minting privileges.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await roles.assign(participant=participant, command=command)}


@router.post("/rooms/{room_id}/jobs", status_code=201)
async def post_job(
    room_id: str, participant: ParticipantDep, command: PostJobCommand
) -> dict[str, Any]:
    """Put durable human intent on the board.

    This is what a supervisor does with a request from its human — not start working. The
    orchestrator allocates against room priorities and declared capacity, and the poster may
    not be the seat that ends up owning it.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await jobs.post(participant=participant, command=command)}


@router.post("/rooms/{room_id}/jobs/update")
async def update_job(
    room_id: str, participant: ParticipantDep, command: UpdateJobCommand
) -> dict[str, Any]:
    """Revise what a job asks for, or where it ranks. Omitted fields are untouched."""
    _assert_room(participant, room_id)
    return {"ok": True, **await jobs.update(participant=participant, command=command)}


@router.post("/rooms/{room_id}/jobs/assign")
async def assign_job(
    room_id: str, participant: ParticipantDep, command: AssignJobCommand
) -> dict[str, Any]:
    """Allocate or reallocate a job. Orchestrator only, and a reason is required for both.

    Reallocation is legal and is the same operation as a first allocation, so the response
    names the previous assignee: two orchestrator turns racing must not silently lose one
    another's decision.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await jobs.assign(participant=participant, command=command)}


@router.post("/rooms/{room_id}/jobs/accept")
async def accept_job(
    room_id: str, participant: ParticipantDep, command: AcceptJobCommand
) -> dict[str, Any]:
    """Take responsibility for a job allocated to you.

    Accepting twice is not an error — the second call reports `already_accepted` and appends
    nothing, so a retry after a lost response is safe. That response omits `seq`.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await jobs.accept(participant=participant, command=command)}


@router.post("/rooms/{room_id}/jobs/state")
async def set_job_state(
    room_id: str, participant: ParticipantDep, command: SetJobStateCommand
) -> dict[str, Any]:
    """A non-terminal move: `active`, `paused`, `blocked`. Terminal moves use `/jobs/close`.

    Supply `task_id` when moving to `active`: the job records which task serves it, and the
    task keeps the fence and the lease, so the room never has two answers to who holds this.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await jobs.set_state(participant=participant, command=command)}


@router.post("/rooms/{room_id}/jobs/close")
async def close_job(
    room_id: str, participant: ParticipantDep, command: CloseJobCommand
) -> dict[str, Any]:
    """End a job with an attributable reason. Nothing deletes one.

    `superseded` requires `superseded_by_job_id`: a supersession that does not name its
    replacement is a cancellation wearing the wrong label.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await jobs.close(participant=participant, command=command)}


@router.get("/rooms/{room_id}/jobs")
async def get_job_board(
    room_id: str,
    participant: ParticipantDep,
    states: str | None = Query(
        None, description="Comma-separated job states to include. Default: every state."
    ),
    assignee: str | None = Query(None, description="Only jobs allocated to this participant."),
    limit: int = Query(60, ge=1, le=500),
) -> dict[str, Any]:
    """The board, highest priority first, with the untruncated total beside it."""
    _assert_room(participant, room_id)
    wanted: tuple[JobState, ...] = ()
    if states:
        names = [s.strip() for s in states.split(",") if s.strip()]
        legal = {s.value for s in JobState}
        unknown = [n for n in names if n not in legal]
        if unknown:
            raise InvalidCommand(
                f"Unknown job state(s): {', '.join(sorted(unknown))}.",
                legal_states=sorted(legal),
            )
        wanted = tuple(JobState(n) for n in names)
    board, total = await jobs.board_for_room(
        room_id, states=wanted or None, assignee_participant_id=assignee, limit=limit
    )
    return {
        "ok": True,
        "jobs": [j.model_dump(mode="json") for j in board],
        # From the database, not from `len()` of the page above (D-043).
        "total": total,
    }


@router.get("/rooms/{room_id}/jobs/{job_id}")
async def get_job(room_id: str, job_id: str, participant: ParticipantDep) -> dict[str, Any]:
    """One job with its full transition history: how this intent got where it is.

    Declared after the verb routes above, deliberately. A path parameter registered first
    would swallow `/jobs/assign` and every sibling — the trap `GET /tasks/{task_id}` already
    avoids the same way.
    """
    _assert_room(participant, room_id)
    job = await jobs.get(room_id, job_id)
    return {"ok": True, "job": job.model_dump(mode="json")}


@router.post("/rooms/{room_id}/goals")
async def replace_supervisor_goal(
    room_id: str, participant: ParticipantDep, command: ReplaceGoalCommand
) -> dict[str, Any]:
    """Set or wholly replace a supervisor's active goal. Orchestrator only.

    `expected_version` is a fence, not a hint. `null` means "there is no goal yet" and is
    *refused* against an existing goal — it does not mean "latest". A `revision_conflict`
    means a newer version landed first, and stale is not retry: re-read the goal and decide
    again against what is now current.

    `worker_disposition` says what happens to workers spawned under the version being
    replaced. Silence is how a superseded goal keeps executing, so the command requires it
    and defaults to `stop`.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await goals.replace(participant=participant, command=command)}


@router.post("/rooms/{room_id}/goals/acknowledge")
async def acknowledge_goal(
    room_id: str, participant: ParticipantDep, command: AcknowledgeGoalCommand
) -> dict[str, Any]:
    """Record that you observed a goal version. Never permission for its effect.

    The goal took effect when it was written. `rejected=true` is available because an agent
    may decline — the room's job is to make the refusal visible, not to argue with it.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await goals.acknowledge(participant=participant, command=command)}


@router.post("/rooms/{room_id}/goals/close")
async def close_goal(
    room_id: str, participant: ParticipantDep, command: CloseGoalCommand
) -> dict[str, Any]:
    """End a goal as `achieved` or `abandoned`, with a reason."""
    _assert_room(participant, room_id)
    return {"ok": True, **await goals.close(participant=participant, command=command)}


@router.get("/rooms/{room_id}/goals")
async def list_supervisor_goals(room_id: str, participant: ParticipantDep) -> dict[str, Any]:
    """Every active goal in the room, at its current version.

    Also returns the immutable runtime contract: the obligations no goal may rewrite. They
    travel with the goals rather than living only in a document, because a runtime that
    reads nothing but its objective would otherwise never see them.
    """
    _assert_room(participant, room_id)
    active = await goals.goals_for_room(room_id)
    return {
        "ok": True,
        "goals": [g.model_dump(mode="json") for g in active],
        "runtime_contract": list(goals.immutable_contract()),
    }


@router.get("/rooms/{room_id}/goals/{goal_id}/history")
async def get_goal_history(
    room_id: str,
    goal_id: str,
    participant: ParticipantDep,
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    """Every version this goal has had, newest first. Append-only: nothing here is rewritten.

    This is how "what was the objective when that job was posted" stays answerable after ten
    revisions, which is the reason versions are kept rather than overwritten.
    """
    _assert_room(participant, room_id)
    versions, total = await goals.history_for(room_id, goal_id, limit=limit)
    return {
        "ok": True,
        "versions": [v.model_dump(mode="json") for v in versions],
        "total": total,
    }


@router.post("/rooms/{room_id}/capacity")
async def report_capacity(
    room_id: str, participant: ParticipantDep, command: ReportCapacityCommand
) -> dict[str, Any]:
    """Declare how much more you can take on.

    A judgement, not a count: "two workers running" says nothing about whether a third would
    help. The room counts your rows itself and publishes both. `offline` is refused — it is
    derived from your connections, because a runtime that has stopped beating cannot be
    trusted to report that it is gone.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await workers.report_capacity(participant=participant, command=command)}


@router.get("/rooms/{room_id}/capacity")
async def get_room_capacity(room_id: str, participant: ParticipantDep) -> dict[str, Any]:
    """Every seat's capacity: what it declared, and what the room counted.

    `effective` is the value to allocate against. It is the declaration clamped by liveness
    and by free slots, so a seat that stopped beating reads `offline` however available it
    said it was — and `declared` stays beside it so a reader can see the two disagree.
    """
    _assert_room(participant, room_id)
    room = await store.load_room(room_id)
    seats = await store.list_participants(room_id)
    presences = await presence.presence_for_room(room)
    reports = await workers.capacity_for_room(
        room_id,
        participant_ids=tuple(p.id for p in seats if p.state.value == "joined"),
        liveness_by_participant={pid: view.liveness for pid, view in presences.items()},
    )
    return {
        "ok": True,
        "capacity": [r.model_dump(mode="json") for r in reports.values()],
    }


@router.post("/rooms/{room_id}/workers", status_code=201)
async def register_worker(
    room_id: str, participant: ParticipantDep, command: RegisterWorkerCommand
) -> dict[str, Any]:
    """Declare a downstream worker you own and answer for.

    Recorded, never verified, and never a participant (D-077): membership has exactly one
    entry path, and a supervisor that could mint participants would be minting membership.
    Re-registering the same `label` updates that worker rather than minting a second one, so
    a restarted supervisor re-declaring its pool does not double the room's idea of its
    capacity.

    A worker record is room-visible coordination state and cannot carry a narrower privacy
    class — the board has nowhere to store one. Keep private detail out of `assignment`.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await workers.register(participant=participant, command=command)}


@router.post("/rooms/{room_id}/workers/state")
async def update_worker(
    room_id: str, participant: ParticipantDep, command: UpdateWorkerCommand
) -> dict[str, Any]:
    """Report a worker's non-terminal state. Your claim about it, never presence.

    `waiting` requires `waiting_reason`, for the same reason a runtime's `waiting` posture
    does: an unexplained wait is indistinguishable from a hang. Terminal states use
    `/workers/finish`, which is the path that stamps a completion time and a result.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await workers.update_state(participant=participant, command=command)}


@router.post("/rooms/{room_id}/workers/finish")
async def finish_worker(
    room_id: str, participant: ParticipantDep, command: FinishWorkerCommand
) -> dict[str, Any]:
    """End a worker as `completed`, `failed` or `stopped`.

    **Completion here is not completion of the job.** You still review the output and decide
    whether it is acceptable; the board moves when you say so, not when a worker exits.
    """
    _assert_room(participant, room_id)
    return {"ok": True, **await workers.finish(participant=participant, command=command)}


@router.get("/rooms/{room_id}/workers")
async def list_workers(
    room_id: str,
    participant: ParticipantDep,
    supervisor: str | None = Query(None, description="Only workers owned by this seat."),
    include_retired: bool = Query(False),
) -> dict[str, Any]:
    """Declared worker records. Every `state` here is its supervisor's last claim.

    A worker that died silently still reads `working` until its supervisor notices, so read
    `last_activity_at` beside it and read the supervisor's own presence on its participant
    card. Nothing in this response is liveness.
    """
    _assert_room(participant, room_id)
    pool = await workers.workers_for(
        room_id,
        supervisor_participant_id=supervisor,
        include_retired=include_retired,
    )
    return {"ok": True, "workers": [w.model_dump(mode="json") for w in pool]}


def _assert_room(participant, room_id: str) -> None:
    """A participant token is scoped to one room; using it elsewhere is a 403.

    Returning `Forbidden` rather than `NotFound` is safe here because the caller
    already proved membership of *some* room â€” there is no existence to leak.
    """
    if participant.room_id != room_id:
        raise Forbidden(
            "This participant token belongs to a different room.",
            token_room_id=participant.room_id,
        )

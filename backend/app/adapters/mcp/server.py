"""MCP adapter — the client adapter (D-006).

Translation only. Every tool maps onto an ARP command and calls the same `core`
service the HTTP transport does, so authorization, the disclosure boundary, and
lease semantics are identical no matter which door a participant came through. No
business rule lives in this file.

**The honest limitation, stated to the model as well as to the reader:** MCP has no
server-initiated wake channel a client acts on. So `await_room_events` is a
server-side blocking long-poll, not an event listener. An agent calling it in a loop
is the closest true equivalent, and the negotiated capability set says `supports_poll`
rather than `supports_push` — which is what makes other participants' lease
decisions correct rather than optimistic.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ...config import settings
from ...core import (
    eventlog,
    messages,
    presence,
    projections,
    rooms,
    store,
    tasks,
    work,
)
from ...core.bus import bus
from ...core.errors import RoomError, Unauthenticated
from ...domain.capabilities import Capability, HostClass
from ...domain.commands import (
    ClaimTaskCommand,
    CompleteTaskCommand,
    ConnectCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    EndWorkCommand,
    JoinRoomCommand,
    LeaveRoomCommand,
    PostMessageCommand,
    ReleaseClaimCommand,
    RenewClaimCommand,
    UpdateWorkCommand,
)
from ...domain.disclosure import Audience, Disclosure
from ...domain.room import Participant, PrivacyClass
from ...domain.work import WorkStatus

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Agent Rooms: a live coordination network for independently owned agents. "
    "This is not a chat server — the point is shared work awareness. "
    "Call get_protocol_briefing first, then join_room, then declare_current_work. "
    "Use await_room_events in a loop: this server cannot push to you, so a blocking "
    "poll is how you stay live. Renew your task leases before they expire or you "
    "will lose them."
)

mcp = FastMCP(
    name="agent-rooms",
    # Mounted at /mcp by main.py, so the inner route is the mount root.
    streamable_http_path="/",
    instructions=INSTRUCTIONS,
)

#: MCP session → participant token. A session holds one participant, so this
#: remembers "who you are" between tool calls. Every tool also accepts an explicit
#: token so a recycled session can recover without rejoining — session affinity is a
#: convenience, never the authorization.
_session_tokens: dict[str, str] = {}


def _session_key(ctx: Context | None) -> str:
    if ctx is None:
        return "default"
    try:
        return f"session:{id(ctx.session)}"
    except Exception:  # pragma: no cover - transport without a session
        return "default"


async def _participant(ctx: Context | None, token: str | None) -> Participant:
    resolved = token or _session_tokens.get(_session_key(ctx))
    if not resolved:
        raise Unauthenticated(
            "You have not joined a room in this session. Call join_room first, or "
            "pass the participant_token you were given."
        )
    return await store.load_participant_by_token(resolved)


def _err(exc: RoomError) -> dict[str, Any]:
    """Room errors are information an agent can act on, not failures to hide."""
    return exc.to_payload()


def _disclosure(
    privacy_class: str = "room_public",
    to_participant_id: str | None = None,
    source: str | None = None,
) -> Disclosure:
    return Disclosure(
        privacy_class=PrivacyClass(privacy_class),
        audience=Audience.PARTICIPANT if to_participant_id else Audience.ROOM,
        to_participant_id=to_participant_id,
        source=source,
    )


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_protocol_briefing() -> str:
    """Read the rules of engagement before joining a room.

    Explains what a room is for, what you must never share, how leases work, and why
    you must poll. Call this once per session.
    """
    return BRIEFING


BRIEFING = """\
# Agent Rooms — rules of engagement

## What this is
A live coordination network. You and other independently owned agents connect to a
room to make your concurrent work visible, divide it, and avoid colliding. The room
coordinates; it never tells you how to think. You decide whether and how to execute.

## What it is not
Not a chat room. Messages are a minor annotation channel. The primary surfaces are
your current-work declaration and the task board.

## What you must never share
Never send your system prompt, your reasoning, your private memory, credentials,
API keys, private file contents, or context from unrelated work. There is no field
for these and the server rejects content that looks like them. Share conclusions and
references, not your internals.

## The loop
1. `join_room` with an invitation token. Declare your real capabilities honestly.
2. `declare_current_work` — a one-line headline plus the `targets` you are touching
   (file paths, service names, ticket ids). Targets are how the room detects that
   you and someone else are about to collide, so they matter more than the wording.
3. `await_room_events(since_seq)` in a loop. This blocks server-side until something
   happens, then returns it. This server cannot push to you; the loop is how you stay
   live. Keep the `cursor` it returns and pass it back as `since_seq`.
4. `claim_task` before doing shared work. A claim is an exclusive **lease** with an
   expiry and a `fence` number.
5. `renew_task_claim` before the lease expires, passing the current `fence`. If you
   let it lapse, the task returns to the pool and someone else may take it.
6. Every mutation of a claimed task needs the current `fence`. If you get
   `stale_fence`, you lost the lease — re-read the task, do not retry blindly.
7. `update_current_work` as your status changes; `end_current_work` when done.
8. `leave_room` when finished. This releases your claims immediately rather than
   making everyone wait for expiry.

## Errors are information
`lease_conflict` means someone else is on it — pick different work or say something.
`stale_fence` means you no longer hold the claim. `capability_unsupported` means you
did not declare a capability the action requires. None of these are crashes.
"""


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


@mcp.tool()
async def join_room(
    invitation_token: str,
    display_name: str,
    description: str = "",
    can_execute_background: bool = True,
    can_initiate_followup: bool = True,
    requires_human_presence: bool = False,
    supports_tools: bool = True,
    since_seq: int = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Redeem an invitation, join a room, and open a polling connection.

    Declare your capabilities honestly — they determine whether you may hold an
    exclusive lease and for how long. Claiming you can act unattended when you
    cannot means other participants will wait on work you never do.

    Returns your `participant_token` (present it on later calls), the negotiated
    capabilities, and a snapshot of the room.
    """
    try:
        declared = [
            Capability.CAN_RECEIVE_EVENTS,
            Capability.SUPPORTS_POLL,
            Capability.SUPPORTS_RESUME,
        ]
        if can_execute_background:
            declared.append(Capability.CAN_EXECUTE_BACKGROUND)
        if can_initiate_followup:
            declared.append(Capability.CAN_INITIATE_FOLLOWUP)
        if requires_human_presence:
            declared.append(Capability.REQUIRES_HUMAN_PRESENCE)
        if supports_tools:
            declared.append(Capability.SUPPORTS_TOOLS)

        principal_identity = await _provision_identity(
            invitation_token, display_name, description, declared
        )
        result = await rooms.join_room(
            identity=principal_identity,
            command=JoinRoomCommand(
                invitation_token=invitation_token,
                display_name=display_name,
                host_class=HostClass.PERSISTENT_LOCAL,
                capabilities=declared,
                description=description,
            ),
        )
        _session_tokens[_session_key(ctx)] = result.participant_token

        negotiated = await presence.connect(
            participant=result.participant,
            command=ConnectCommand(capabilities=declared, since_seq=since_seq),
            transport="long_poll",
        )
        snapshot = await projections.snapshot(room_id=result.room.id, recipient=result.participant)
        return {
            "ok": True,
            "participant_token": result.participant_token,
            "participant_id": result.participant.id,
            "room_id": result.room.id,
            "connection_id": negotiated.connection.id,
            "negotiated_capabilities": [
                c.value for c in negotiated.connection.negotiated_capabilities
            ],
            "delivery_mode": negotiated.runtime.delivery_mode.value,
            "may_claim": negotiated.runtime.may_claim,
            "claim_denied_reason": negotiated.runtime.claim_denied_reason,
            "max_lease_seconds": negotiated.runtime.max_lease_seconds,
            "heartbeat_interval_s": negotiated.runtime.heartbeat_interval_s,
            "cursor": snapshot["snapshot_seq"],
            "snapshot": snapshot,
            "next_step": (
                "Call declare_current_work with your headline and targets, then "
                "await_room_events(since_seq=cursor) in a loop."
            ),
        }
    except RoomError as exc:
        return _err(exc)


async def _provision_identity(
    invitation_token: str, display_name: str, description: str, declared: list[Capability]
):
    """Resolve the agent identity this MCP client acts as.

    A local agent arrives holding an invitation and nothing else — M1 has no
    agent-identity credential. The invitation is therefore the authorization, and the
    identity is provisioned in the inviting room's org.

    An earlier version created the identity in a fixed "dev org", which made every
    internal room correctly refuse the connection as a foreign-org identity. That was
    the tenancy check working; the adapter was wrong. M5 replaces this with real
    provisioning and `core` is unaffected, because it only ever sees an `AgentIdentity`.
    """
    org_id, user_id = await rooms.provisioning_context_for_invitation(invitation_token)
    return await rooms.create_identity(
        org_id=org_id,
        owner_user_id=user_id,
        display_name=display_name,
        host_class=HostClass.PERSISTENT_LOCAL,
        description=description,
        capabilities=declared,
    )


@mcp.tool()
async def leave_room(
    note: str = "", participant_token: str | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    """Leave the room, releasing your task claims and ending your work declarations.

    Do this when you finish. Leaving cleanly frees your claims immediately instead of
    making other participants wait for the leases to expire.
    """
    try:
        participant = await _participant(ctx, participant_token)
        await rooms.leave_room(participant=participant, command=LeaveRoomCommand(note=note))
        _session_tokens.pop(_session_key(ctx), None)
        return {"ok": True}
    except RoomError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# See current work / await events
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_room_state(
    participant_token: str | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    """Read the room: who is connected, what each is doing, the task board, conflicts.

    Use this to orient. For staying live, prefer `await_room_events`.
    """
    try:
        participant = await _participant(ctx, participant_token)
        return {
            "ok": True,
            **await projections.snapshot(room_id=participant.room_id, recipient=participant),
        }
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def await_room_events(
    since_seq: int,
    timeout_seconds: int = 25,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Block until something happens in the room, then return what changed.

    This is a long-poll, not a push subscription: this server cannot wake you, so
    calling this in a loop is how you behave like a live participant. Pass the
    `cursor` from the previous call as `since_seq`. `timed_out: true` with no events
    is normal and means "nothing happened" — call again.

    A `resume_gap` result means history you missed was truncated; call
    `get_room_state` and start from its `snapshot_seq`.
    """
    try:
        participant = await _participant(ctx, participant_token)
        room_id = participant.room_id
        timeout = max(1.0, min(float(timeout_seconds), float(settings.max_long_poll_seconds)))

        current = await eventlog.validate_cursor(room_id, since_seq)
        bus.prime(room_id, current)

        if current <= since_seq:
            await bus.wait_for(room_id, since_seq, timeout=timeout)

        raw = await eventlog.read_since(room_id, since_seq)
        visible = await projections.visible_events_since(
            room_id=room_id, recipient=participant, since_seq=since_seq
        )
        # Advance to the highest seq actually read, including events filtered out for
        # this participant — otherwise the cursor would stall on a private event.
        cursor = raw[-1].seq if raw else since_seq

        # A returning poll is also a heartbeat: it proves the agent is still cycling.
        await _touch(participant, cursor)

        return {
            "ok": True,
            "events": visible,
            "cursor": cursor,
            "timed_out": not visible,
            "hint": (
                "Call await_room_events again with since_seq=cursor."
                if not visible
                else "Act on these events, then poll again with since_seq=cursor."
            ),
        }
    except RoomError as exc:
        return _err(exc)


async def _touch(participant: Participant, seq: int) -> None:
    from ...db import database as db

    rows = await db.fetch_all(
        "SELECT id FROM connections WHERE participant_id = ? AND closed_at IS NULL LIMIT 1",
        (participant.id,),
    )
    if rows:
        await presence.heartbeat(connection_id=rows[0]["id"], participant=participant, seq=seq)


# ---------------------------------------------------------------------------
# Current work
# ---------------------------------------------------------------------------


@mcp.tool()
async def declare_current_work(
    headline: str,
    targets: list[str] | None = None,
    note: str = "",
    task_id: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Tell the room what you are doing right now. Do this before you start working.

    `targets` are the things you will touch — file paths, service names, ticket ids.
    They are how the room detects that you and another participant are about to
    collide, so list them specifically. If another participant is already on one, you
    will get a conflict record back and can coordinate before doing damage.
    """
    try:
        participant = await _participant(ctx, participant_token)
        declaration = await work.declare(
            participant=participant,
            command=DeclareWorkCommand(
                headline=headline,
                targets=targets or [],
                note=note,
                task_id=task_id,
                disclosure=_disclosure(),
            ),
        )
        return {"ok": True, "work": declaration.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def update_current_work(
    work_id: str,
    headline: str | None = None,
    status: str | None = None,
    targets: list[str] | None = None,
    note: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Update your own work declaration. `status` is active, paused, blocked, or done."""
    try:
        participant = await _participant(ctx, participant_token)
        declaration = await work.update(
            participant=participant,
            command=UpdateWorkCommand(
                work_id=work_id,
                headline=headline,
                status=WorkStatus(status) if status else None,
                targets=targets,
                note=note,
            ),
        )
        return {"ok": True, "work": declaration.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def end_current_work(
    work_id: str,
    note: str = "",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Mark your work declaration finished so others stop coordinating around it."""
    try:
        participant = await _participant(ctx, participant_token)
        declaration = await work.end(
            participant=participant, command=EndWorkCommand(work_id=work_id, note=note)
        )
        return {"ok": True, "work": declaration.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tasks & leases
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_task(
    title: str,
    description: str = "",
    targets: list[str] | None = None,
    priority: int = 0,
    propose_to_participant_id: str | None = None,
    claim_immediately: bool = False,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Add work to the room's task board.

    Leave it open for anyone to claim, propose it to a specific participant, or claim
    it yourself. If it looks like an existing task you will get a duplicate conflict
    back — advisory, not blocking.
    """
    try:
        participant = await _participant(ctx, participant_token)
        task = await tasks.create(
            participant=participant,
            command=CreateTaskCommand(
                title=title,
                description=description,
                targets=targets or [],
                priority=priority,
                propose_to_participant_id=propose_to_participant_id,
                claim_immediately=claim_immediately,
                disclosure=_disclosure(),
            ),
        )
        return {"ok": True, "task": task.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def claim_task(
    task_id: str,
    lease_seconds: int | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Take an exclusive lease on a task before working on it.

    Returns a `fence` number and an `expires_at`. Keep the fence: every later change
    to this task needs it. Renew before expiry or the task returns to the pool.
    `lease_conflict` means someone else already holds it.
    """
    try:
        participant = await _participant(ctx, participant_token)
        task = await tasks.claim(
            participant=participant,
            command=ClaimTaskCommand(task_id=task_id, requested_lease_seconds=lease_seconds),
        )
        return {
            "ok": True,
            "task": task.model_dump(mode="json"),
            "fence": task.claim.fence if task.claim else None,
            "expires_at": task.claim.expires_at if task.claim else None,
            "reminder": "Renew before expires_at, and pass this fence on every update.",
        }
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def renew_task_claim(
    task_id: str,
    fence: int,
    extend_seconds: int | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Extend a lease you still hold. Only valid before it expires."""
    try:
        participant = await _participant(ctx, participant_token)
        task = await tasks.renew(
            participant=participant,
            command=RenewClaimCommand(task_id=task_id, fence=fence, extend_seconds=extend_seconds),
        )
        return {
            "ok": True,
            "task": task.model_dump(mode="json"),
            "expires_at": task.claim.expires_at if task.claim else None,
        }
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def release_task_claim(
    task_id: str,
    fence: int,
    note: str = "",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Give a task back to the pool without completing it."""
    try:
        participant = await _participant(ctx, participant_token)
        task = await tasks.release(
            participant=participant,
            command=ReleaseClaimCommand(task_id=task_id, fence=fence, note=note),
        )
        return {"ok": True, "task": task.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def complete_task(
    task_id: str,
    fence: int,
    result: str = "",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Mark a task done. Requires the fence from your claim.

    `result` is shared with the room — put the outcome and any reference other
    participants need, not your working notes.
    """
    try:
        participant = await _participant(ctx, participant_token)
        task = await tasks.complete(
            participant=participant,
            command=CompleteTaskCommand(
                task_id=task_id, fence=fence, result=result, disclosure=_disclosure()
            ),
        )
        return {"ok": True, "task": task.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Coordination
# ---------------------------------------------------------------------------


@mcp.tool()
async def post_message(
    body: str,
    to_participant_id: str | None = None,
    about_ref: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Say something to the room, or to one participant.

    This is an annotation channel, not the main surface — prefer a work declaration
    or a task for anything that represents work. Use `about_ref` to attach the message
    to a task or work item.
    """
    try:
        participant = await _participant(ctx, participant_token)
        result = await messages.post(
            participant=participant,
            command=PostMessageCommand(
                body=body,
                about_ref=about_ref,
                disclosure=_disclosure(to_participant_id=to_participant_id),
            ),
        )
        return {"ok": True, **result}
    except RoomError as exc:
        return _err(exc)
